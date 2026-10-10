"""Phase 3 — Telegram bot resilience: pipeline/network failures must produce a
user-friendly message, get logged, and never leave the chat in a hanging state."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from attendance import ingest

SECRET = "secret-internal-detail /srv/creds.json"


class _CodedError(Exception):
    """Mimics google-genai / gspread errors that expose an HTTP status as .code."""
    def __init__(self, code):
        super().__init__(f"{code} {SECRET}")
        self.code = code


# ─── Fakes ────────────────────────────────────────────────────────────────────

class FakeStatus:
    def __init__(self, fail_edit=False):
        self.fail_edit, self.edits, self.deleted = fail_edit, [], False

    async def edit_text(self, text, **kw):
        if self.fail_edit:
            raise RuntimeError("edit failed")
        self.edits.append(text)

    async def delete(self):
        self.deleted = True


class FakeBot:
    def __init__(self, fail_edit=False, fail_send=False):
        self.status = FakeStatus(fail_edit)
        self.fail_send, self.sent = fail_send, []

    async def send_message(self, chat_id, text, **kw):
        if self.fail_send:
            raise RuntimeError("send failed")
        self.sent.append(text)
        return self.status

    async def send_chat_action(self, **kw):
        pass

    @property
    def all_text(self):
        return self.status.edits + self.sent


def _ctx(bot, **user_data):
    return SimpleNamespace(bot=bot, user_data=dict(user_data))


def _photo_ctx(bot, tmp_path):
    img = tmp_path / "sheet.jpg"
    img.write_bytes(b"x")
    return _ctx(bot, course="g10", session=3, type="Not Set", image_path=str(img))


def _names_ctx(bot):
    return _ctx(bot, nc_names=["Ali", "Sara"], nc_course="g10", nc_session=3, nc_type=None)


def _update():
    msg = SimpleNamespace(chat_id=42)
    reply_log = []

    async def reply_text(text, **kw):
        reply_log.append(text)

    msg.reply_text = reply_text
    return SimpleNamespace(message=msg, replies=reply_log)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # review_images/ is created relative to cwd
    cfg = {"core": {"app": {"dry_run_default": True}},
           "attendance": {"review": {"local_dir": "rq"}}}
    monkeypatch.setattr(ingest, "_load_cfg", lambda *a, **k: cfg)
    log = MagicMock()
    monkeypatch.setattr(ingest, "log", log)
    return log


def _boom(exc):
    def _run(*a, **k):
        raise exc
    return _run


def _run_photo(bot, ctx):
    asyncio.run(ingest._run_pipeline_flow(_update(), ctx, 42))


# ─── _user_error_message ──────────────────────────────────────────────────────

@pytest.mark.parametrize("exc, needle", [
    (TimeoutError("read timed out"), "network"),
    (ConnectionError("reset"), "network"),
    (_CodedError(429), "busy"),
    (_CodedError(403), "permission"),
    (_CodedError(503), "network"),
    (FileNotFoundError(SECRET), "file"),
    (ValueError(SECRET), "went wrong"),
])
def test_user_error_message_is_friendly_and_leaks_nothing(exc, needle):
    msg = ingest._user_error_message(exc)
    assert needle in msg.lower()
    assert "secret-internal-detail" not in msg and "Traceback" not in msg


# ─── Photo flow (_run_pipeline_flow) ──────────────────────────────────────────

@pytest.mark.parametrize("exc", [
    RuntimeError(SECRET), TimeoutError("timed out"), ConnectionError("down"),
])
def test_photo_pipeline_exception_gives_friendly_message(monkeypatch, tmp_path, _env, exc):
    monkeypatch.setattr(ingest, "run", _boom(exc))
    bot = FakeBot()
    ctx = _photo_ctx(bot, tmp_path)
    _run_photo(bot, ctx)
    assert bot.status.edits and bot.status.edits[-1].startswith("❌")
    assert "secret-internal-detail" not in " ".join(bot.all_text)
    assert _env.error.called
    assert ctx.user_data == {}


def test_photo_config_load_failure_is_reported_not_swallowed(monkeypatch, tmp_path, _env):
    """Previously _load_cfg ran outside the try → silent task death, stuck 'Processing…'."""
    monkeypatch.setattr(ingest, "_load_cfg", _boom(FileNotFoundError(SECRET)))
    bot = FakeBot()
    _run_photo(bot, _photo_ctx(bot, tmp_path))
    assert any(t.startswith("❌") for t in bot.all_text)
    assert _env.error.called


def test_photo_image_copy_failure_is_reported(monkeypatch, tmp_path):
    bot = FakeBot()
    ctx = _photo_ctx(bot, tmp_path)
    ctx.user_data["image_path"] = str(tmp_path / "missing.jpg")
    _run_photo(bot, ctx)
    assert any(t.startswith("❌") for t in bot.all_text)


def test_photo_falls_back_to_new_message_if_edit_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(ingest, "run", _boom(RuntimeError(SECRET)))
    bot = FakeBot(fail_edit=True)
    _run_photo(bot, _photo_ctx(bot, tmp_path))
    assert any(t.startswith("❌") for t in bot.sent)


def test_photo_stale_state_asks_to_restart(tmp_path):
    bot = FakeBot()
    _run_photo(bot, _ctx(bot))  # user_data empty (bot restarted / cleared)
    assert any("/start" in t for t in bot.all_text)


def test_photo_timeout_does_not_hang(monkeypatch, tmp_path):
    monkeypatch.setattr(ingest, "PIPELINE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(ingest, "run", lambda *a, **k: time.sleep(0.3))
    bot = FakeBot()
    start = time.monotonic()
    _run_photo(bot, _photo_ctx(bot, tmp_path))
    assert time.monotonic() - start < 2
    assert any("too long" in t for t in bot.all_text)


def test_photo_summary_delivery_failure_is_not_reported_as_processing_error(monkeypatch, tmp_path):
    summary = SimpleNamespace(upload_id="abc123", flagged=0, sheet_url=None)
    monkeypatch.setattr(ingest, "run", lambda *a, **k: summary)

    async def bad_send(*a, **k):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(ingest, "_send_summary", bad_send)
    bot = FakeBot()
    _run_photo(bot, _photo_ctx(bot, tmp_path))
    text = " ".join(bot.all_text)
    assert "abc123" in text and "Error processing" not in text


def test_photo_success_path_unchanged(monkeypatch, tmp_path):
    summary = SimpleNamespace(upload_id="u1", flagged=0, sheet_url=None)
    monkeypatch.setattr(ingest, "run", lambda *a, **k: summary)
    sent = []

    async def ok_send(chat_id, bot, s, review_dir):
        sent.append(s)

    monkeypatch.setattr(ingest, "_send_summary", ok_send)
    bot = FakeBot()
    _run_photo(bot, _photo_ctx(bot, tmp_path))
    assert sent == [summary] and bot.status.deleted


# ─── Paste-names flow (handle_names_done) ─────────────────────────────────────

def _done(ctx):
    upd = _update()
    result = asyncio.run(ingest.handle_names_done(upd, ctx))
    return result, upd


@pytest.mark.parametrize("exc", [RuntimeError(SECRET), TimeoutError("t/o")])
def test_names_pipeline_exception_gives_friendly_message(monkeypatch, _env, exc):
    monkeypatch.setattr(ingest, "run_text", _boom(exc))
    bot = FakeBot()
    ctx = _names_ctx(bot)
    result, _ = _done(ctx)
    assert result == ingest.ConversationHandler.END
    assert bot.sent and bot.sent[-1].startswith("❌")
    assert "secret-internal-detail" not in " ".join(bot.sent)
    assert _env.error.called
    assert ctx.user_data == {}


def test_names_config_failure_ends_conversation(monkeypatch, _env):
    monkeypatch.setattr(ingest, "_load_cfg", _boom(OSError(SECRET)))
    bot = FakeBot()
    result, _ = _done(_names_ctx(bot))
    assert result == ingest.ConversationHandler.END
    assert any(t.startswith("❌") for t in bot.sent)


def test_names_stale_state_asks_to_restart():
    bot = FakeBot()
    ctx = _ctx(bot, nc_names=["Ali"])  # nc_course / nc_session lost
    result, _ = _done(ctx)
    assert result == ingest.ConversationHandler.END
    assert any("/start" in t for t in bot.sent)


def test_names_timeout_does_not_hang(monkeypatch):
    monkeypatch.setattr(ingest, "PIPELINE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(ingest, "run_text", lambda *a, **k: time.sleep(0.3))
    bot = FakeBot()
    result, _ = _done(_names_ctx(bot))
    assert result == ingest.ConversationHandler.END
    assert any("too long" in t for t in bot.sent)


def test_names_empty_list_keeps_waiting():
    bot = FakeBot()
    result, upd = _done(_ctx(bot, nc_names=[]))
    assert result == ingest.NAMES_PASTE and upd.replies


# ─── Global error handler ─────────────────────────────────────────────────────

def _err_ctx(bot, exc):
    return SimpleNamespace(bot=bot, error=exc)


def test_global_error_handler_logs_and_notifies(_env):
    bot = FakeBot()
    upd = SimpleNamespace(effective_chat=SimpleNamespace(id=7))
    asyncio.run(ingest._global_error_handler(upd, _err_ctx(bot, RuntimeError(SECRET))))
    assert _env.error.called
    assert bot.sent and "secret-internal-detail" not in bot.sent[0]


def test_global_error_handler_without_update_only_logs(_env):
    bot = FakeBot()
    asyncio.run(ingest._global_error_handler(None, _err_ctx(bot, ConnectionError("poll"))))
    assert _env.error.called and bot.sent == []


def test_global_error_handler_survives_notify_failure(_env):
    bot = FakeBot(fail_send=True)
    upd = SimpleNamespace(effective_chat=SimpleNamespace(id=7))
    asyncio.run(ingest._global_error_handler(upd, _err_ctx(bot, RuntimeError("x"))))  # must not raise
