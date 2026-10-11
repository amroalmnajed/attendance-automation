"""Feature 11 - role-based bot access. One rule per test."""
import asyncio
from types import SimpleNamespace

import pytest

from attendance import roles
from attendance.bot import admin, common, upload
from attendance.bot import app as bot_app
from tests.botutil import handler, patch_bot

CFG = {"attendance": {"admin": {"telegram_ids": [1]}, "teacher": {"telegram_ids": [2]}}}
ADMIN, TEACHER, STRANGER = 1, 2, 999

# who may use which handler (owner's answers: teachers AND admins may tap abs| and undo with /revert, rev|)
EXPECTED_LEVEL = {
    "teacher": ["start_cmd", "handle_mode_callback", "handle_photo", "handle_upload_callback",
                "handle_names_course_callback", "handle_names_session_text", "handle_names_type_callback",
                "handle_names_paste", "handle_names_done", "handle_names_confirm", "cancel_cmd",
                "cmd_revert", "handle_revert_callback", "handle_absent_callback"],
    "admin": ["handle_search_callback", "search_input_handler", "handle_resolution_callback",
              "cmd_report", "cmd_streaks", "cmd_pending"],
    "strict_admin": ["cmd_aliases", "cmd_health"],
}
ALL = [(lvl, n) for lvl, names in EXPECTED_LEVEL.items() for n in names]


def _decide(uid, level, cfg=CFG):
    return roles.decide(uid, level, cfg, env={}).allowed


@pytest.mark.parametrize("uid,level,allowed", [
    (ADMIN, "teacher", True), (TEACHER, "teacher", True), (STRANGER, "teacher", False),
    (ADMIN, "admin", True), (TEACHER, "admin", False), (STRANGER, "admin", False),
    (ADMIN, "strict_admin", True), (TEACHER, "strict_admin", False), (STRANGER, "strict_admin", False),
    (None, "teacher", False),
])
def test_role_by_level_table(uid, level, allowed):
    assert _decide(uid, level) is allowed


def test_every_handler_has_the_agreed_level():
    for lvl, name in ALL:
        assert getattr(handler(name), "required_level", None) == lvl, name


def test_ids_merge_env_and_config_and_ignore_junk():
    cfg = {"attendance": {"teacher": {"telegram_ids": [5, "oops"]}}}
    assert roles.teacher_ids(cfg, {"TEACHER_TELEGRAM_IDS": "7; 8,,x"}) == {5, 7, 8}


def test_no_allowlist_keeps_everything_open_except_strict_admin():
    assert _decide(STRANGER, "teacher", {"attendance": {}})
    assert _decide(STRANGER, "admin", {"attendance": {}})
    assert not _decide(STRANGER, "strict_admin", {"attendance": {}})     # /aliases, /health stay closed
    assert roles.startup_warnings({"attendance": {}}, {})                  # clear warning at startup


def test_teachers_only_config_closes_admin_commands_and_warns():
    cfg = {"attendance": {"teacher": {"telegram_ids": [2]}}}
    assert _decide(TEACHER, "teacher", cfg) and not _decide(TEACHER, "admin", cfg)
    assert roles.startup_warnings(cfg, {})
    assert roles.startup_warnings(CFG, {}) == []


# ─── Real handlers: a stranger is refused, sees only their own ID, nothing runs ──

def _update(uid, callback=False):
    out = {"answers": [], "replies": [], "edits": []}

    async def answer(text=None, show_alert=False, **_k):
        out["answers"].append((text, show_alert))

    async def edit(*a, **k):
        out["edits"].append(a)

    async def reply(text, **_k):
        out["replies"].append(text)
    msg = SimpleNamespace(reply_text=reply, text="Ali", chat_id=5, photo=[], document=None, caption=None)
    q = SimpleNamespace(answer=answer, edit_message_text=edit, data="x|y", message=msg) if callback else None
    upd = SimpleNamespace(effective_user=SimpleNamespace(id=uid), message=None if callback else msg,
                          callback_query=q, effective_chat=SimpleNamespace(id=5))
    return upd, out


@pytest.fixture
def with_cfg(monkeypatch):
    patch_bot(monkeypatch, "_load_cfg", lambda *a, **k: CFG)


@pytest.mark.parametrize("lvl,name", ALL)
def test_stranger_is_refused_and_sees_only_own_id(with_cfg, lvl, name):
    fn = handler(name)
    is_cb = name.endswith("_callback")
    upd, out = _update(STRANGER, callback=is_cb)
    ctx = SimpleNamespace(bot=SimpleNamespace(), user_data={}, args=[])
    asyncio.run(fn(upd, ctx))
    shown = " ".join(out["replies"] + [a[0] or "" for a in out["answers"]])
    assert "999" in shown or name == "cmd_aliases"           # own ID shown (aliases shows the admin hint)
    assert "🔒" in shown
    assert not out["edits"]                                   # handler body never ran
    if is_cb:
        assert out["answers"] and out["answers"][0][1] is True   # spinner stopped with an alert


def test_refusal_never_contains_course_or_student_data(with_cfg):
    upd, out = _update(STRANGER)
    asyncio.run(admin.cmd_report(upd, SimpleNamespace(bot=None, user_data={}, args=[])))
    text = out["replies"][0].lower()
    for word in ("crash", "g10", "g9", "main", "student", "present"):
        assert word not in text.replace("admins only", "")


def test_teacher_is_refused_on_admin_command_but_not_on_teacher_command(monkeypatch, with_cfg):
    upd, out = _update(TEACHER)
    asyncio.run(admin.cmd_report(upd, SimpleNamespace(bot=None, user_data={}, args=[])))
    assert "admins only" in out["replies"][0]
    upd, out = _update(TEACHER)
    asyncio.run(upload.start_cmd(upd, SimpleNamespace(bot=None, user_data={}, args=[])))
    assert not any("🔒" in r for r in out["replies"])         # allowed through to the real handler


def test_role_check_error_means_refusal_not_access(monkeypatch):
    patch_bot(monkeypatch, "_load_cfg", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("secret-x")))
    upd, out = _update(ADMIN)
    ran = []

    @roles.guard("teacher", lambda: common._load_cfg(), None)
    async def handler(u, c):
        ran.append(1)
    asyncio.run(handler(upd, SimpleNamespace(bot=None)))
    assert not ran and out["replies"][0].startswith("❌") and "secret-x" not in out["replies"][0]


def test_refusal_failure_never_crashes_or_grants():
    class Boom:
        async def reply_text(self, *a, **k):
            raise RuntimeError("telegram down")
    upd = SimpleNamespace(effective_user=SimpleNamespace(id=STRANGER), message=Boom(), callback_query=None)
    ran = []

    @roles.guard("teacher", lambda: CFG)
    async def handler(u, c):
        ran.append(1)
    asyncio.run(handler(upd, SimpleNamespace(bot=None)))
    assert not ran


# ─── Startup: the real main() wiring (no network, no polling) ─────────────────

def test_bot_starts_and_registers_health_without_the_scheduler_package(monkeypatch, tmp_path):
    from telegram.ext import Application, CommandHandler
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TEST-TOKEN-NOT-REAL")
    patch_bot(monkeypatch, "_load_cfg", lambda *a, **k: {
        "attendance": {"review": {"local_dir": str(tmp_path / "rq")},
                       "reminders": {"enabled": True}, "admin": {"telegram_ids": [1]}}})
    captured = {}
    monkeypatch.setattr(Application, "run_polling", lambda self, *a, **k: captured.setdefault("app", self))
    bot_app.main()                                   # reminders ON + no job queue must not stop startup
    app = captured["app"]
    cmds = {c for hs in app.handlers.values() for h in hs if isinstance(h, CommandHandler) for c in h.commands}
    assert {"health", "aliases", "report", "revert"} <= cmds
