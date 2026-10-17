"""attendance/bot/menu.py — Telegram command menu per role, and /help.

Teachers see start/upload/done/cancel/revert/help in the "/" menu (default scope). Admins get the
extra admin commands through a per-chat scope. ``/help`` lists exactly the commands the caller may
use, decided by the same ``roles.decide`` the handlers' guards use.
"""
from __future__ import annotations

from telegram import BotCommand, Update
from telegram.ext import ContextTypes

from attendance import roles
from attendance.bot.common import _guard, _load_cfg, log

# (command, description, required level) — order is the order shown in the menu.
COMMANDS: list[tuple[str, str, str]] = [
    ("start", "Start a new attendance upload", "authorized"),
    ("upload", "Upload an attendance sheet", "authorized"),
    ("done", "Finish pasting names", "authorized"),
    ("cancel", "Cancel what you're doing", "authorized"),
    ("revert", "Undo a recent upload", "authorized"),
    ("help", "Show what I can do", "authorized"),
    ("report", "Attendance report", "authorized"),
    ("streaks", "Students with repeated absences", "authorized"),
    ("pending", "Items waiting for review", "authorized"),
    ("aliases", "Manage learned name aliases", "authorized"),
    ("health", "Bot health status", "authorized"),
    ("zoom_fetch", "Fetch Zoom attendance via API", "authorized"),
    ("zoom_upload", "Upload Zoom CSV file", "authorized"),
    ("zoom_reprocess", "Reprocess archived Zoom CSV", "authorized"),
    ("reload_config", "Reload bot configuration", "strict_admin"),
]


def commands_for_level() -> list[tuple[str, str]]:
    """Menu entries for authorized users."""
    return [(c, d) for c, d, lvl in COMMANDS if lvl == "authorized"]


def allowed_commands(uid, cfg) -> list[tuple[str, str]]:
    """Commands this user may run, using the same decision the handler guards make."""
    return [(c, d) for c, d, lvl in COMMANDS if roles.decide(uid, lvl, cfg).allowed]


def help_text(uid, cfg) -> str:
    cmds = allowed_commands(uid, cfg)
    if not cmds:
        return "You don't have access to this bot."
    lines = ["Here's what you can do:", ""] + [f"/{c} — {d}" for c, d in cmds]
    lines += ["", "Tip: send a photo of the sheet, or type /start and pick a course."]
    lines += ["", "Zoom commands: /zoom_fetch, /zoom_upload, /zoom_reprocess"]
    return "\n".join(lines)


@_guard("authorized")
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List the commands the caller may use."""
    try:
        uid = getattr(getattr(update, "effective_user", None), "id", None)
        text = help_text(uid, _load_cfg())
    except Exception as exc:
        log.error("help_cmd_failed", extra={"error": type(exc).__name__})
        text = "❌ Couldn't build the help text right now. Please try again."
    await update.message.reply_text(text)


async def post_init(application) -> None:
    """Publish the "/" menus. Every failure is logged and swallowed: a menu must never stop the bot."""
    try:
        cfg = _load_cfg()
        bot = application.bot
        await bot.set_my_commands([BotCommand(c, d) for c, d in commands_for_level()])
    except Exception as exc:
        log.warning("command_menu_failed", extra={"error": type(exc).__name__})
