"""attendance/config_runtime.py — pure runtime helpers shared by webapp and bot.

No Telegram dependencies. Safe to import without python-telegram-bot installed.
"""
from __future__ import annotations

import yaml

from attendance import _net
from core.logger import get_logger

log = get_logger("ingest")

COURSES = ["crash", "main", "g10", "g9"]

STAGE_TEXT = {
    "reading": "📷 Reading the photo…",
    "matching": "🔎 Matching names…",
    "saving": "✍️ Saving to the sheet…",
}


def _load_cfg(path: str = "config/attendance.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


class _PipelineTimeout(Exception):
    """Pipeline did not finish within timeout."""


class _StaleSession(Exception):
    """Conversation state (user_data) is missing — e.g. session expired or reset."""


def _user_error_message(exc: BaseException, what: str = "sheet") -> str:
    """Map an exception to a short user-safe message. Never includes str(exc)."""
    if isinstance(exc, _PipelineTimeout):
        return (f"❌ Processing the {what} took too long and was stopped. It may still finish "
                "in the background — check /pending before sending it again.")
    if isinstance(exc, _StaleSession):
        return "⚠️ This session expired (the bot was restarted or reset). Please send /start and try again."
    status = _net.http_status(exc)
    if status == 429:
        return "❌ Google services are busy right now (quota limit). Please wait a minute and try again."
    if _net.is_network_error(exc) or status in _net.TRANSIENT_STATUS:
        return "❌ Couldn't reach Google services (network problem). Please try again in a moment."
    if status in (401, 403):
        return "❌ The bot doesn't have permission to access the Google Sheet. Please contact the admin."
    if isinstance(exc, OSError):
        return "❌ A file the bot needs couldn't be read or saved. Please resend and try again."
    return (f"❌ Something went wrong while processing the {what}. Please try again, "
            "or contact the admin if it keeps happening.")
