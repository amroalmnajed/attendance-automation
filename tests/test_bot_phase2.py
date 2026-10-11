"""Phase 2 bot behaviour: mixed marks preview/confirm, multi-photo uploads, absent button."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from attendance import absent
from attendance.bot import admin, common, names, review, upload
from tests.botutil import patch_bot


# ─── Fakes ────────────────────────────────────────────────────────────────────

class FakeBot:
    def __init__(self):
        self.sent = []          # (text, kwargs)

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((text, kw))
        return SimpleNamespace(edit_text=self._noop, delete=self._noop)

    async def send_chat_action(self, **kw):
        pass

    async def _noop(self, *a, **k):
        pass

    @property
    def texts(self):
        return [t for t, _ in self.sent]


def ctx(bot=None, **user_data):
    return SimpleNamespace(bot=bot or FakeBot(), user_data=dict(user_data))


def text_update(chat_id=42):
    replies = []

    async def reply_text(text, **kw):
        replies.append((text, kw))

    return SimpleNamespace(message=SimpleNamespace(chat_id=chat_id, reply_text=reply_text), replies=replies)


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    cfg = {"core": {"app": {"dry_run_default": True}},
           "attendance": {"review": {"local_dir": "rq"}, "ingest": {}}}
    patch_bot(monkeypatch, "_load_cfg", lambda *a, **k: cfg)
    patch_bot(monkeypatch, "log", MagicMock())
    return cfg


def names_ctx(lines, bot=None):
    return ctx(bot, nc_names=list(lines), nc_course="crash", nc_session=3, nc_type=None)


# ─── Paste-names: marks, preview, confirm ─────────────────────────────────────

def test_marks_are_parsed_previewed_and_passed_to_the_pipeline(monkeypatch):
    seen = {}
    patch_bot(monkeypatch, "run_text", lambda **kw: seen.update(kw) or SimpleNamespace(upload_id="u", flagged=0, sheet_url=None))

    async def ok_send(*a, **k):
        pass
    patch_bot(monkeypatch, "_send_summary", ok_send)

    c, upd = names_ctx(["Ahmed Ali | L", "Sara - absent", "Omar"]), text_update()
    result = asyncio.run(names.handle_names_done(upd, c))

    assert result == common.ConversationHandler.END
    assert seen["names"] == ["Ahmed Ali", "Sara", "Omar"]
    assert seen["marks"] == ["Late", "Absent", "Present"]
    preview = upd.replies[0][0]
    assert "Ahmed Ali → Late" in preview and "Sara → Absent" in preview and "Omar → Present" in preview
    assert "parse_mode" not in upd.replies[0][1]            # names may contain Markdown characters
    assert len(upd.replies) == 1                            # clean list: no confirm step
    assert c.user_data == {}


def test_unrecognised_mark_asks_for_confirmation_instead_of_processing(monkeypatch):
    patch_bot(monkeypatch, "run_text", lambda **kw: pytest.fail("must wait for confirmation"))
    c, upd = names_ctx(["Ahmed Ali | L", "Omar | zzz"]), text_update()
    result = asyncio.run(names.handle_names_done(upd, c))

    assert result == common.NAMES_CONFIRM
    assert "not recognised" in upd.replies[0][0]
    markup = upd.replies[1][1]["reply_markup"]
    assert [b.callback_data for row in markup.inline_keyboard for b in row] == ["pv|go", "pv|fix", "pv|cancel"]
    assert c.user_data["nc_parsed"] == [("Ahmed Ali", "Late"), ("Omar", "zzz")]


def _callback(data, chat_id=42):
    edits = []

    async def answer():
        pass

    async def edit_message_text(text, **kw):
        edits.append(text)

    async def edit_message_reply_markup(**kw):
        edits.append("markup-removed")

    q = SimpleNamespace(data=data, answer=answer, edit_message_text=edit_message_text,
                        edit_message_reply_markup=edit_message_reply_markup,
                        message=SimpleNamespace(chat_id=chat_id))
    return SimpleNamespace(callback_query=q), edits


def test_confirm_go_processes_with_the_parsed_marks(monkeypatch):
    seen = {}
    patch_bot(monkeypatch, "run_text", lambda **kw: seen.update(kw) or SimpleNamespace(upload_id="u", flagged=0, sheet_url=None))

    async def ok_send(*a, **k):
        pass
    patch_bot(monkeypatch, "_send_summary", ok_send)

    c = names_ctx([])
    c.user_data["nc_parsed"] = [("Ahmed Ali", "Late"), ("Omar", "zzz")]
    upd, _ = _callback("pv|go")
    assert asyncio.run(names.handle_names_confirm(upd, c)) == common.ConversationHandler.END
    assert seen["names"] == ["Ahmed Ali", "Omar"] and seen["marks"] == ["Late", "zzz"]
    assert c.user_data == {}


def test_confirm_fix_clears_the_list_and_waits_for_a_new_one():
    c = names_ctx(["Omar | zzz"])
    c.user_data["nc_parsed"] = [("Omar", "zzz")]
    upd, edits = _callback("pv|fix")
    assert asyncio.run(names.handle_names_confirm(upd, c)) == common.NAMES_PASTE
    assert c.user_data["nc_names"] == [] and "nc_parsed" not in c.user_data and "corrected list" in edits[0]


def test_confirm_cancel_processes_nothing(monkeypatch):
    patch_bot(monkeypatch, "run_text", lambda **kw: pytest.fail("cancelled"))
    c = names_ctx([])
    c.user_data["nc_parsed"] = [("Omar", "zzz")]
    upd, edits = _callback("pv|cancel")
    assert asyncio.run(names.handle_names_confirm(upd, c)) == common.ConversationHandler.END
    assert c.user_data == {} and "Cancelled" in edits[0]


def test_confirm_after_restart_asks_to_start_over(monkeypatch):
    patch_bot(monkeypatch, "run_text", lambda **kw: pytest.fail("no state"))
    c = ctx()
    upd, _ = _callback("pv|go")
    assert asyncio.run(names.handle_names_confirm(upd, c)) == common.ConversationHandler.END
    assert any("/start" in t for t in c.bot.texts)


@pytest.mark.parametrize("mode, lines, expect_confirm, expect_preview", [
    ("always", ["Ahmed"], True, True),
    ("never", ["Omar | zzz"], False, False),
    ("on_issues", ["Ahmed"], False, True),
])
def test_names_preview_modes(monkeypatch, env, mode, lines, expect_confirm, expect_preview):
    env["attendance"]["ingest"]["names_preview"] = mode
    patch_bot(monkeypatch, "run_text", lambda **kw: SimpleNamespace(upload_id="u", flagged=0, sheet_url=None))

    async def ok_send(*a, **k):
        pass
    patch_bot(monkeypatch, "_send_summary", ok_send)

    upd = text_update()
    result = asyncio.run(names.handle_names_done(upd, names_ctx(lines)))
    assert (result == common.NAMES_CONFIRM) is expect_confirm
    assert any("How I read your list" in t for t, _ in upd.replies) is expect_preview


# ─── Multi-photo ──────────────────────────────────────────────────────────────

def photo_update(file_id, *, group=None, caption=None):
    replies, downloads = [], []

    class F:
        def __init__(self):
            self.file_id = file_id

        async def download_to_drive(self, custom_path):
            downloads.append(str(custom_path))
            open(custom_path, "wb").write(b"img")

    class P:
        async def get_file(self):
            return F()

    async def reply_text(text, **kw):
        replies.append(text)
        return SimpleNamespace(edit_text=lambda *a, **k: asyncio.sleep(0))

    msg = SimpleNamespace(photo=[P()], document=None, caption=caption, media_group_id=group,
                          reply_text=reply_text, chat_id=42)
    return SimpleNamespace(message=msg, replies=replies)


@pytest.fixture
def tmpdir_redirect(monkeypatch, tmp_path):
    monkeypatch.setattr(upload.tempfile, "gettempdir", lambda: str(tmp_path))


def test_album_photos_join_one_upload_and_keep_the_caption_settings(tmpdir_redirect):
    c = ctx()
    asyncio.run(upload.handle_photo(photo_update("a1", group="G1", caption="crash s4"), c))
    asyncio.run(upload.handle_photo(photo_update("a2", group="G1"), c))
    asyncio.run(upload.handle_photo(photo_update("a3", group="G1"), c))
    assert len(c.user_data["image_paths"]) == 3
    assert (c.user_data["course"], c.user_data["session"]) == ("crash", 4)     # not reset by later photos
    assert c.user_data["image_path"] == c.user_data["image_paths"][0]


def test_photos_sent_in_quick_succession_join_the_open_upload(tmpdir_redirect):
    c = ctx()
    asyncio.run(upload.handle_photo(photo_update("b1"), c))
    asyncio.run(upload.handle_photo(photo_update("b2"), c))                      # no album id, within the window
    assert len(c.user_data["image_paths"]) == 2


def test_a_late_photo_starts_a_fresh_upload(tmpdir_redirect, env):
    env["attendance"]["ingest"]["multi_photo"] = {"append_window_s": 0}
    c = ctx()
    asyncio.run(upload.handle_photo(photo_update("c1", caption="crash s2"), c))
    first = c.user_data["image_paths"][0]
    asyncio.run(upload.handle_photo(photo_update("c2"), c))
    assert len(c.user_data["image_paths"]) == 1 and c.user_data["image_paths"][0] != first
    assert c.user_data["course"] == "g10"                                        # reset like a brand-new upload
    import os
    assert not os.path.exists(first)                                             # abandoned photo cleaned up


def test_multi_photo_can_be_turned_off(tmpdir_redirect, env):
    env["attendance"]["ingest"]["multi_photo"] = {"enabled": False}
    c = ctx()
    asyncio.run(upload.handle_photo(photo_update("d1", group="G"), c))
    asyncio.run(upload.handle_photo(photo_update("d2", group="G"), c))
    assert len(c.user_data["image_paths"]) == 1


def test_photo_limit_is_enforced_politely(tmpdir_redirect, env):
    env["attendance"]["ingest"]["multi_photo"] = {"max_photos": 2}
    c = ctx()
    for i in range(3):
        upd = photo_update(f"e{i}", group="G")
        asyncio.run(upload.handle_photo(upd, c))
    assert len(c.user_data["image_paths"]) == 2
    assert "maximum of 2 photos" in upd.replies[-1]


def test_start_processing_uses_run_multi_for_several_photos(monkeypatch, tmp_path):
    imgs = []
    for n in range(2):
        p = tmp_path / f"t{n}.jpg"
        p.write_bytes(b"x")
        imgs.append(str(p))
    seen = {}
    patch_bot(monkeypatch, "run", lambda **kw: pytest.fail("single-photo path must not run"))
    patch_bot(monkeypatch, "run_multi", lambda **kw: seen.update(kw) or SimpleNamespace(upload_id="u", flagged=0, sheet_url=None))

    async def ok_send(*a, **k):
        pass
    patch_bot(monkeypatch, "_send_summary", ok_send)

    c = ctx(course="crash", session=3, type="Not Set", image_path=imgs[0], image_paths=imgs)
    asyncio.run(upload._run_pipeline_flow(SimpleNamespace(), c, 42))
    assert len(seen["image_paths"]) == 2 and all(p.startswith("review_images") for p in seen["image_paths"])
    assert callable(seen["progress"])                                           # staged progress messages
    assert not any(__import__("os").path.exists(p) for p in imgs)               # temp photos removed
    assert c.user_data == {}


def test_single_photo_still_uses_run(monkeypatch, tmp_path):
    img = tmp_path / "one.jpg"
    img.write_bytes(b"x")
    seen = {}
    patch_bot(monkeypatch, "run", lambda **kw: seen.update(kw) or SimpleNamespace(upload_id="u", flagged=0, sheet_url=None))
    patch_bot(monkeypatch, "run_multi", lambda **kw: pytest.fail("single photo must use run()"))

    async def ok_send(*a, **k):
        pass
    patch_bot(monkeypatch, "_send_summary", ok_send)

    c = ctx(course="crash", session=3, type="Not Set", image_path=str(img))
    asyncio.run(upload._run_pipeline_flow(SimpleNamespace(), c, 42))
    assert "image_path" in seen
    assert callable(seen["progress"])


# ─── Absent button ────────────────────────────────────────────────────────────

def _buttons(bot):
    markup = bot.sent[-1][1]["reply_markup"]
    return [b for row in markup.inline_keyboard for b in row]


def run_summary(**kw):
    base = dict(upload_id="up1", sheet_url=None, flagged=0, dry_run=False,
                absent_offer=False, missing_count=0)
    base.update(kw)
    s = SimpleNamespace(**base)
    s.text = lambda: "summary"
    return s


def test_summary_shows_the_button_with_the_count():
    bot = FakeBot()
    asyncio.run(review._send_summary(42, bot, run_summary(absent_offer=True, missing_count=4), "rq"))
    assert [(b.text, b.callback_data) for b in _buttons(bot)][0] == ("🙈 Mark 4 absent?", "abs|up1")


def test_summary_marks_the_button_as_dry_run():
    bot = FakeBot()
    asyncio.run(review._send_summary(42, bot, run_summary(absent_offer=True, missing_count=2, dry_run=True), "rq"))
    assert "(dry-run)" in _buttons(bot)[0].text


@pytest.mark.parametrize("kw", [dict(), dict(absent_offer=True, missing_count=0), dict(absent_offer=False, missing_count=5)])
def test_summary_has_no_absent_button_otherwise(kw):
    bot = FakeBot()
    asyncio.run(review._send_summary(42, bot, run_summary(**kw), "rq"))
    assert not any(b.callback_data.startswith("abs|") for b in _buttons(bot) if b.callback_data)


def _abs_query(data="abs|up1"):
    edits = []

    async def answer():
        pass

    async def edit_message_reply_markup(**kw):
        edits.append(kw)

    msg = SimpleNamespace(chat_id=42, reply_markup=None)
    q = SimpleNamespace(data=data, answer=answer, message=msg, edit_message_reply_markup=edit_message_reply_markup)
    return SimpleNamespace(callback_query=q), edits


def test_button_real_write_reports_and_offers_undo(monkeypatch):
    monkeypatch.setattr(absent, "mark_missing_absent",
                        lambda uid, **kw: absent.AbsentResult(ok=True, marked=3, abs_id="aup1", dry_run=False))
    c = ctx()
    asyncio.run(admin.handle_absent_callback(_abs_query()[0], c))
    text, kw = c.bot.sent[-1]
    assert "Marked 3 student(s) Absent" in text
    assert kw["reply_markup"].inline_keyboard[0][0].callback_data == "rev|aup1"      # /revert works on it


def test_button_dry_run_says_nothing_was_written_and_has_no_undo(monkeypatch):
    monkeypatch.setattr(absent, "mark_missing_absent",
                        lambda uid, **kw: absent.AbsentResult(ok=True, marked=3, abs_id="aup1", dry_run=True))
    c = ctx()
    asyncio.run(admin.handle_absent_callback(_abs_query()[0], c))
    text, kw = c.bot.sent[-1]
    assert "DRY-RUN" in text and "Nothing was written" in text and kw["reply_markup"] is None


@pytest.mark.parametrize("res, needle", [
    (absent.AbsentResult(ok=False, reason="pending_review", pending_review=2), "2 review item"),
    (absent.AbsentResult(ok=False, reason="expired"), "expired"),
    (absent.AbsentResult(ok=False, reason="nothing_to_mark"), "Nothing to mark"),
    (absent.AbsentResult(ok=False, reason="value_not_allowed"), "Absent"),
    (absent.AbsentResult(ok=False, reason="disabled"), "isn't available"),
])
def test_button_explains_why_nothing_happened(monkeypatch, res, needle):
    monkeypatch.setattr(absent, "mark_missing_absent", lambda uid, **kw: res)
    c = ctx()
    asyncio.run(admin.handle_absent_callback(_abs_query()[0], c))
    assert needle in c.bot.texts[-1]


def test_button_failure_is_friendly_and_leaks_nothing(monkeypatch):
    def boom(uid, **kw):
        raise RuntimeError("secret-internal-detail /srv/creds.json")
    monkeypatch.setattr(absent, "mark_missing_absent", boom)
    c = ctx()
    asyncio.run(admin.handle_absent_callback(_abs_query()[0], c))
    assert c.bot.texts[-1].startswith("❌") and "secret-internal-detail" not in c.bot.texts[-1]


def test_double_tap_while_running_is_ignored(monkeypatch):
    calls = []
    monkeypatch.setattr(absent, "mark_missing_absent", lambda uid, **kw: calls.append(uid) or absent.AbsentResult(ok=True, marked=1, abs_id="a"))
    admin._ABSENT_INFLIGHT.add("up1")
    try:
        c = ctx()
        asyncio.run(admin.handle_absent_callback(_abs_query()[0], c))
        assert calls == [] and c.bot.sent == []
    finally:
        admin._ABSENT_INFLIGHT.discard("up1")
