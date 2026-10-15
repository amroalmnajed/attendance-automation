"""attendance/bot/common.py — shared bot helpers: role guard, config, error text, blocking runner, conversation states."""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
import yaml

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ConversationHandler

from attendance import _net, roles
from core.logger import get_logger


load_dotenv(override=True)


if sys.stdout.encoding != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


log = get_logger("ingest")


def _guard(level: str, conv: bool = False):
    """Role check for a handler (Feature 11). Config/log are looked up at call time."""
    return roles.guard(level, lambda: _load_cfg(), lambda: log,
                       deny_return=ConversationHandler.END if conv else None)


COURSES = ["crash", "main", "g10", "g9"]


# Max time the bot waits for a blocking pipeline run before telling the user.
PIPELINE_TIMEOUT_S = 300.0


# ── Conversation states ───────────────────────────────────────────────────────
PHOTO_FLOW    = 1   # waiting for photo or inline buttons (image path)


SEARCH_ROSTER = 2   # waiting for roster search text


NAMES_COURSE  = 3   # paste-names flow: waiting for course choice


NAMES_SESSION = 4   # paste-names flow: waiting for session number


NAMES_TYPE    = 5   # paste-names flow: waiting for session type


NAMES_PASTE   = 6   # paste-names flow: waiting for the name list


NAMES_CONFIRM = 7   # paste-names flow: waiting for "Process / Fix / Cancel" after the preview


ZOOM_MEETING_ID = 8   # waiting for meeting ID input


ZOOM_PICK_INSTANCE = 9  # waiting for user to pick from multiple instances


ZOOM_PICK_TYPE = 10    # waiting for session type selection


ZOOM_TEACHER_TIME = 11 # waiting for manual meeting start time


ZOOM_SESSION_NUM = 12  # waiting for session number input


ZOOM_CONFIRM = 13     # waiting for CONFIRM/CANCEL decision


def _load_cfg(path: str = "config/attendance.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ─── Error-handling helpers (Phase 3) ─────────────────────────────────────────

class _PipelineTimeout(Exception):
    """Pipeline did not finish within PIPELINE_TIMEOUT_S."""


class _StaleSession(Exception):
    """Conversation state (user_data) is missing — e.g. bot restarted mid-flow."""


def _require(user_data: dict, *keys: str) -> list:
    try:
        return [user_data[k] for k in keys]
    except KeyError as exc:
        raise _StaleSession(str(exc)) from exc


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


async def _notify_failure(bot, chat_id: int, text: str, status_msg=None) -> None:
    """Best-effort: edit the status message, else send a new one. Never raises."""
    if status_msg is not None:
        try:
            await status_msg.edit_text(text)
            return
        except Exception:
            pass
    try:
        await bot.send_message(chat_id=chat_id, text=text)
    except Exception:
        log.error("ingest_notify_failed", extra={"chat_id": chat_id}, exc_info=True)


# Friendly text for each pipeline stage (see attendance.pipeline.STAGE_*).
STAGE_TEXT = {
    "reading": "📷 Reading the photo…",
    "matching": "🔎 Matching names…",
    "saving": "✍️ Saving to the sheet…",
}
PROGRESS_MIN_INTERVAL_S = 2.0


async def _edit_status(status_msg, text: str) -> None:
    try:
        await status_msg.edit_text(text)
    except Exception:
        pass        # cosmetic only (message deleted, "not modified", flood limit, ...)


def make_progress_callback(status_msg, loop, *, initial: str | None = None,
                           min_interval_s: float | None = None, clock=time.monotonic):
    """Thread-safe ``progress(stage)`` for pipeline.run/run_multi (they run in a worker thread).

    ``initial`` names the stage whose text the message already shows (it is not re-sent).
    Edits ``status_msg`` on the bot's event loop at most once per ``min_interval_s`` (default 2 s;
    stages that arrive sooner are DROPPED, not delayed) and never when the text is unchanged.
    Every failure is swallowed: progress text is cosmetic and must not affect the run.
    """
    interval = PROGRESS_MIN_INTERVAL_S if min_interval_s is None else min_interval_s
    lock = threading.Lock()
    state = {"last_at": clock() if initial else None, "last_text": STAGE_TEXT.get(initial)}

    def progress(stage: str) -> None:
        try:
            text = STAGE_TEXT.get(stage)
            if text is None:
                return
            with lock:
                now = clock()
                if text == state["last_text"]:
                    return
                if state["last_at"] is not None and now - state["last_at"] < interval:
                    return
                state["last_at"], state["last_text"] = now, text
            asyncio.run_coroutine_threadsafe(_edit_status(status_msg, text), loop)
        except Exception:
            pass

    return progress


async def _run_blocking(fn, timeout_s: float | None = None):
    """Run blocking fn in the default executor; raise _PipelineTimeout instead of hanging.

    asyncio.wait (not wait_for) so a TimeoutError raised *inside* fn (e.g. a network
    timeout) is not confused with our own timeout. The worker thread cannot be killed.
    """
    fut = asyncio.get_running_loop().run_in_executor(None, fn)
    fut.add_done_callback(lambda f: f.cancelled() or f.exception())  # mark retrieved
    done, _ = await asyncio.wait({fut}, timeout=PIPELINE_TIMEOUT_S if timeout_s is None else timeout_s)
    if not done:
        raise _PipelineTimeout()
    return fut.result()


# ─── Helper ───────────────────────────────────────────────────────────────────

def _course_type_list(cfg: dict, course: str) -> list[str]:
    """Return all allowed session-type strings for a typed_pair course."""
    types_cfg = cfg["attendance"]["courses"].get(course, {}).get("types") or {}
    result: list[str] = []
    for bucket in types_cfg.values():
        if isinstance(bucket, (list, tuple)):
            result.extend(t for t in bucket if isinstance(t, str) and t.strip())
    return result


def _course_keyboard() -> InlineKeyboardMarkup:
    row1 = [InlineKeyboardButton(c.upper(), callback_data=f"nc|course|{c}") for c in COURSES[:2]]
    row2 = [InlineKeyboardButton(c.upper(), callback_data=f"nc|course|{c}") for c in COURSES[2:]]
    return InlineKeyboardMarkup([row1, row2])


def _input_settings() -> dict:
    """Effective ``attendance.ingest`` options for the new input features. Never raises."""
    try:
        sect = (_load_cfg().get("attendance", {}) or {}).get("ingest", {}) or {}
    except Exception:
        sect = {}
    multi = sect.get("multi_photo", {}) or {}
    mode = str(sect.get("names_preview", "on_issues")).lower()
    return {
        "names_preview": mode if mode in ("always", "on_issues", "never") else "on_issues",
        "multi_enabled": bool(multi.get("enabled", True)),
        "max_photos": max(1, int(multi.get("max_photos", 6))),
        "append_window_s": float(multi.get("append_window_s", 120)),
    }


def _get_sheet_url(course: str, cfg: dict) -> str:
    """Get the Google Sheet URL for a course."""
    course_cfg = (cfg.get("attendance") or {}).get("courses", {}).get(course, {})
    sheet_id_env = course_cfg.get("sheet_id_env")
    if not sheet_id_env:
        return ""

    sheet_id = os.environ.get(sheet_id_env)
    if not sheet_id:
        return ""

    return f"https://docs.google.com/spreadsheets/d/{sheet_id}"
