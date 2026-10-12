"""T9 - command menu per role and /help."""
import asyncio
import re
from types import SimpleNamespace

from attendance.bot import app as bot_app
from attendance.bot import menu
from tests.botutil import patch_bot

CFG = {"attendance": {"admin": {"telegram_ids": [1, 3]}, "teacher": {"telegram_ids": [2]}}}
TEACHER_CMDS = ["start", "upload", "done", "cancel", "revert", "help"]
ADMIN_EXTRA = ["report", "streaks", "pending", "aliases", "health"]


def test_commands_follow_telegram_rules():
    names = [c for c, _, _ in menu.COMMANDS]
    assert len(names) == len(set(names))
    for c, d, lvl in menu.COMMANDS:
        assert re.fullmatch(r"[a-z0-9_]{1,32}", c) and 1 <= len(d) <= 256
        assert lvl in ("teacher", "admin", "strict_admin")


def test_teacher_and_admin_menus():
    assert [c for c, _ in menu.commands_for_level(admin=False)] == TEACHER_CMDS
    assert [c for c, _ in menu.commands_for_level(admin=True)] == TEACHER_CMDS + ADMIN_EXTRA


def test_help_lists_only_what_the_caller_may_use():
    t = [c for c, _ in menu.allowed_commands(2, CFG)]
    a = [c for c, _ in menu.allowed_commands(1, CFG)]
    assert t == TEACHER_CMDS and a == TEACHER_CMDS + ADMIN_EXTRA
    assert menu.allowed_commands(999, CFG) == []
    assert "/report" not in menu.help_text(2, CFG) and "/report" in menu.help_text(1, CFG)
    assert menu.help_text(999, CFG) == "You don't have access to this bot."


def test_every_menu_command_has_a_handler(monkeypatch, tmp_path):
    from telegram.ext import Application, CommandHandler
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TEST-TOKEN-NOT-REAL")
    patch_bot(monkeypatch, "_load_cfg", lambda *a, **k: {"attendance": {"review": {"local_dir": str(tmp_path)}}})
    captured = {}
    monkeypatch.setattr(Application, "run_polling", lambda self, *a, **k: captured.setdefault("app", self))
    bot_app.main()
    app = captured["app"]
    registered = {c for hs in app.handlers.values() for h in hs for c in getattr(h, "commands", ())}
    for h in app.handlers.get(0, []):                       # commands inside the conversation
        for ep in getattr(h, "entry_points", ()):
            registered |= set(getattr(ep, "commands", ()))
        for st in getattr(h, "states", {}).values():
            for x in st:
                registered |= set(getattr(x, "commands", ()))
        for fb in getattr(h, "fallbacks", ()):
            registered |= set(getattr(fb, "commands", ()))
    assert {c for c, _, _ in menu.COMMANDS} <= registered
    assert app.post_init is menu.post_init


class _Bot:
    def __init__(self, fail_for=None):
        self.calls, self.fail_for = [], fail_for

    async def set_my_commands(self, commands, scope=None):
        chat = getattr(scope, "chat_id", None)
        if chat is not None and chat == self.fail_for:
            raise RuntimeError("chat not found")
        self.calls.append(([c.command for c in commands], chat))


def test_post_init_sets_default_and_per_admin_menus(monkeypatch):
    patch_bot(monkeypatch, "_load_cfg", lambda *a, **k: CFG)
    bot = _Bot()
    asyncio.run(menu.post_init(SimpleNamespace(bot=bot)))
    assert bot.calls[0] == (TEACHER_CMDS, None)
    assert bot.calls[1:] == [(TEACHER_CMDS + ADMIN_EXTRA, 1), (TEACHER_CMDS + ADMIN_EXTRA, 3)]


def test_post_init_failures_never_raise(monkeypatch):
    patch_bot(monkeypatch, "_load_cfg", lambda *a, **k: CFG)
    bot = _Bot(fail_for=1)
    asyncio.run(menu.post_init(SimpleNamespace(bot=bot)))
    assert (TEACHER_CMDS + ADMIN_EXTRA, 3) in bot.calls          # one bad admin chat doesn't block the next
    patch_bot(monkeypatch, "_load_cfg", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    asyncio.run(menu.post_init(SimpleNamespace(bot=_Bot())))


def _update(uid):
    out = []

    async def reply_text(text, **kw):
        out.append(text)
    return SimpleNamespace(effective_user=SimpleNamespace(id=uid), message=SimpleNamespace(reply_text=reply_text),
                           callback_query=None), out


def test_cmd_help_replies_by_role_and_refuses_strangers(monkeypatch):
    patch_bot(monkeypatch, "_load_cfg", lambda *a, **k: CFG)
    upd, out = _update(2)
    asyncio.run(menu.cmd_help(upd, SimpleNamespace(bot=None, user_data={}, args=[])))
    assert "/revert" in out[0] and "/health" not in out[0]
    upd, out = _update(999)
    asyncio.run(menu.cmd_help(upd, SimpleNamespace(bot=None, user_data={}, args=[])))
    assert "/revert" not in out[0]
