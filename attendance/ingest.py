"""attendance/ingest.py — Telegram bot ingest channel."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import yaml
import shutil
import uuid
import re
from pathlib import Path
from dotenv import load_dotenv

import cv2

load_dotenv(override=True)

if sys.stdout.encoding != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from telegram import (
    Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, filters,
    ContextTypes, ConversationHandler, CallbackQueryHandler,
)
from telegram.constants import ChatAction, ParseMode

from core.logger import get_logger
from attendance import _net
from attendance import absent as absent_mod
from attendance import marks as marks_mod
from attendance.pipeline import run, run_multi, run_text
from attendance.normalizer import canonical_mark
from attendance.review_queue import ReviewQueue
from attendance.writer import WriteCell, batch_write
from attendance.session_resolver import resolve
from attendance import alias_admin
from attendance import roles
from attendance import reminders as reminders_mod
from attendance import health as health_mod
from attendance.aliases import AliasStore

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


async def _deliver_summary(chat_id: int, bot, summary, review_dir: str) -> None:
    """Send the summary; a delivery failure must not be reported as a processing failure."""
    try:
        await _send_summary(chat_id, bot, summary, review_dir)
    except Exception:
        log.error("ingest_summary_delivery_failed",
                  extra={"chat_id": chat_id, "upload_id": getattr(summary, "upload_id", None)},
                  exc_info=True)
        await _notify_failure(
            bot, chat_id,
            f"⚠️ Sheet processed (upload ID: {getattr(summary, 'upload_id', 'unknown')}), "
            "but I couldn't display the summary. Use /pending to check review items.",
        )


# ─── Helper ───────────────────────────────────────────────────────────────────

def _course_type_list(cfg: dict, course: str) -> list[str]:
    """Return all allowed session-type strings for a typed_pair course."""
    types_cfg = cfg["attendance"]["courses"].get(course, {}).get("types", {})
    result: list[str] = []
    for bucket in types_cfg.values():
        result.extend(bucket)
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


# ─── /start ───────────────────────────────────────────────────────────────────

@_guard("teacher", conv=True)
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📷 Send a photo of the sheet", callback_data="mode|photo")],
        [InlineKeyboardButton("📋 Paste names directly",      callback_data="mode|names")],
    ])
    await update.message.reply_text(
        "👋 *Attendance Bot*\n\nHow do you want to add attendance?",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )
    return PHOTO_FLOW


@_guard("teacher", conv=True)
async def handle_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry-point callback for the two-mode selector on /start."""
    query = update.callback_query
    await query.answer()
    mode = query.data.split("|")[1]

    if mode == "photo":
        await query.edit_message_text(
            "📷 Send me the attendance sheet photo now.\n\n"
            "_Tip: Add a caption like `g10 s3 Saturday Center` to skip the prompts._",
            parse_mode=ParseMode.MARKDOWN,
        )
        return PHOTO_FLOW

    # names mode — ask for course first
    await query.edit_message_text(
        "📋 *Paste-names flow*\n\nStep 1 of 3 — Which course?",
        reply_markup=_course_keyboard(),
        parse_mode=ParseMode.MARKDOWN,
    )
    return NAMES_COURSE


# ─── Image upload flow ────────────────────────────────────────────────────────

@_guard("teacher", conv=True)
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.photo:
        photo_file = await update.message.photo[-1].get_file()
    elif update.message.document:
        doc = update.message.document
        if (doc.mime_type and doc.mime_type.startswith("image/")) or (
            doc.file_name and doc.file_name.lower().endswith((".jpg", ".jpeg", ".png"))
        ):
            photo_file = await doc.get_file()
        else:
            await update.message.reply_text("Please send a valid photo.")
            return ConversationHandler.END
    else:
        await update.message.reply_text("Please send a valid photo.")
        return ConversationHandler.END

    tmp_dir = Path(tempfile.gettempdir()) / "attendance_bot"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    img_path = tmp_dir / f"{photo_file.file_id}.jpg"
    await photo_file.download_to_drive(custom_path=img_path)

    ud = context.user_data
    opts = _input_settings()
    now = time.monotonic()
    group_id = getattr(update.message, "media_group_id", None)
    existing: list[str] = list(ud.get("image_paths") or ([ud["image_path"]] if ud.get("image_path") else []))

    # Another photo for the SAME upload: same Telegram album, or sent right after the
    # previous one while the upload menu is still open.
    same_upload = bool(existing) and opts["multi_enabled"] and (
        (group_id is not None and group_id == ud.get("media_group_id"))
        or (now - float(ud.get("last_photo_at", 0.0)) <= opts["append_window_s"])
    )

    if same_upload:
        if str(img_path) in existing:
            return PHOTO_FLOW                                  # same photo sent twice: ignore
        if len(existing) >= opts["max_photos"]:
            await update.message.reply_text(
                f"📷 That's the maximum of {opts['max_photos']} photos for one upload — "
                "I've kept the first ones. Tap ▶️ Start Processing when you're ready."
            )
            try:
                os.remove(img_path)
            except OSError:
                pass
            return PHOTO_FLOW
        existing.append(str(img_path))
        ud["image_paths"] = existing
        ud["last_photo_at"] = now
        if update.message.caption:
            _apply_caption(ud, update.message.caption)
        menu = ud.get("menu_message")
        try:
            if menu is None:
                raise RuntimeError("no menu to update")
            await _send_upload_menu(menu, context, edit=True)
        except Exception:
            await update.message.reply_text(f"📎 Photo {len(existing)} added. Tap ▶️ Start Processing when all photos are in.")
        return PHOTO_FLOW

    # A brand-new upload: drop any abandoned photos from an earlier, unfinished one.
    for old in existing:
        try:
            os.remove(old)
        except OSError:
            pass

    ud["image_path"] = str(img_path)
    ud["image_paths"] = [str(img_path)]
    ud["media_group_id"] = group_id
    ud["last_photo_at"] = now
    ud["course"]  = "g10"
    ud["session"] = 1
    ud["type"]    = "Not Set"

    if update.message.caption:
        _apply_caption(ud, update.message.caption)

    ud["menu_message"] = await _send_upload_menu(update.message, context)
    return PHOTO_FLOW


def _apply_caption(user_data: dict, caption: str) -> None:
    """Parse an optional caption such as ``g10 s3 Saturday Center`` into the upload settings."""
    cap_lower = caption.lower()
    for c in COURSES:
        if c in cap_lower:
            user_data["course"] = c
            break
    s_match = re.search(r"s(\d+)", cap_lower)
    if s_match:
        user_data["session"] = int(s_match.group(1))

    if "saturday"  in cap_lower: user_data["type"] = "Saturday - Center"  if "center" in cap_lower else "Saturday - Online"
    elif "sunday"  in cap_lower: user_data["type"] = "Sunday- Center"     if "center" in cap_lower else "Sunday- Online"
    elif "monday"  in cap_lower: user_data["type"] = "Monday - Online"
    elif "tuesday" in cap_lower: user_data["type"] = "Tuesday - Center"   if "center" in cap_lower else "Tuesday  - Online"
    elif "wednesday" in cap_lower: user_data["type"] = "Wednesday - Online"
    elif "thursday" in cap_lower: user_data["type"] = "Thursday - Center" if "center" in cap_lower else "Thursday - Online"
    elif "friday"  in cap_lower: user_data["type"] = "Friday - Center"    if "center" in cap_lower else "Friday - Online"


async def _send_upload_menu(message, context, edit: bool = False):
    c = context.user_data["course"]
    s = context.user_data["session"]
    t = context.user_data["type"]

    kb = [
        [InlineKeyboardButton(f"Course: {c.upper()}", callback_data="up|course"),
         InlineKeyboardButton(f"Session: S{s}",       callback_data="up|session")],
        [InlineKeyboardButton(f"Type: {t}",            callback_data="up|type")],
        [InlineKeyboardButton("▶️ Start Processing",   callback_data="up|start")],
    ]
    n_photos = len(context.user_data.get("image_paths") or [])
    photos_line = f"📷 {n_photos} photos in this upload\n" if n_photos > 1 else ""
    text = f"📋 *New Attendance Upload*\n{photos_line}Please confirm the details:"

    if edit:
        await message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
        return message
    return await message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)


@_guard("teacher", conv=True)
async def handle_upload_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if not query.data.startswith("up|"):
        return PHOTO_FLOW

    action = query.data.split("|", 1)[1]   # maxsplit=1 — safe against future pipe chars

    if action == "course":
        idx = COURSES.index(context.user_data["course"])
        context.user_data["course"] = COURSES[(idx + 1) % len(COURSES)]
        await _send_upload_menu(query.message, context, edit=True)
        return PHOTO_FLOW

    elif action == "session":
        context.user_data["session"] = (context.user_data["session"] % 20) + 1
        await _send_upload_menu(query.message, context, edit=True)
        return PHOTO_FLOW

    elif action == "type":
        types = [
            "Saturday - Center", "Saturday - Online",
            "Sunday- Center", "Sunday- Online",
            "Monday - Online",
            "Tuesday - Center", "Tuesday  - Online",
            "Wednesday - Online",
            "Thursday - Center", "Thursday - Online",
            "Friday - Center", "Friday - Online",
            "Not Set",
        ]
        curr = context.user_data["type"]
        context.user_data["type"] = types[(types.index(curr) + 1) % len(types)] if curr in types else types[0]
        await _send_upload_menu(query.message, context, edit=True)
        return PHOTO_FLOW

    elif action == "start":
        await query.message.delete()
        loop = asyncio.get_event_loop()
        loop.create_task(_run_pipeline_flow(update, context, query.message.chat_id))
        return ConversationHandler.END

    return PHOTO_FLOW


async def _run_pipeline_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    # Runs as a fire-and-forget task: an uncaught exception here would vanish and leave
    # the user staring at "Processing…", so EVERYTHING is inside the try.
    img_path = context.user_data.get("image_path")
    tmp_paths: list[str] = list(context.user_data.get("image_paths") or ([img_path] if img_path else []))
    status_msg = None
    summary = None
    review_dir = "review_queue"
    try:
        course, session_num, utype, img_path = _require(
            context.user_data, "course", "session", "type", "image_path")
        session_type = utype if utype != "Not Set" else None
        if not tmp_paths:
            tmp_paths = [img_path]

        status_msg = await context.bot.send_message(
            chat_id=chat_id,
            text="⏳ Processing sheet (OpenCV + Gemini OCR + Google Sheets)...",
        )
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception:
            pass  # cosmetic only

        cfg = _load_cfg()
        review_dir = cfg["attendance"]["review"].get("local_dir", "review_queue")

        review_images_dir = Path("review_images")
        review_images_dir.mkdir(parents=True, exist_ok=True)
        persistent_paths: list[Path] = []
        for src in tmp_paths:
            dest = review_images_dir / f"{uuid.uuid4().hex}{Path(src).suffix}"
            shutil.copy2(src, dest)
            persistent_paths.append(dest)
        persistent_img_path = persistent_paths[0]

        if len(persistent_paths) == 1:
            summary = await _run_blocking(
                lambda: run(
                    image_path=str(persistent_img_path),
                    course=course,
                    session_num=session_num,
                    session_type=session_type,
                    dry_run=cfg["core"]["app"].get("dry_run_default", True),
                    force=False,
                    config_path="config/attendance.yaml",
                    review_store_dir=review_dir,
                )
            )
        else:
            # One upload, several photos: OCR each, merge rows, match and write once.
            summary = await _run_blocking(
                lambda: run_multi(
                    image_paths=[str(p) for p in persistent_paths],
                    course=course,
                    session_num=session_num,
                    session_type=session_type,
                    dry_run=cfg["core"]["app"].get("dry_run_default", True),
                    force=False,
                    config_path="config/attendance.yaml",
                    review_store_dir=review_dir,
                ),
                timeout_s=PIPELINE_TIMEOUT_S * len(persistent_paths),
            )
    except Exception as exc:
        log.error("ingest_pipeline_error",
                  extra={"flow": "photo", "chat_id": chat_id, "error": str(exc)}, exc_info=True)
        await _notify_failure(context.bot, chat_id, _user_error_message(exc, "sheet"), status_msg)
    else:
        try:
            await status_msg.delete()
        except Exception:
            pass
        await _deliver_summary(chat_id, context.bot, summary, review_dir)
    finally:
        for tmp in {p for p in [*tmp_paths, img_path] if p}:
            try:
                os.remove(tmp)
            except OSError:
                pass
        context.user_data.clear()


# ─── Paste-names flow ─────────────────────────────────────────────────────────

@_guard("teacher", conv=True)
async def handle_names_course_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """nc|course|<course> — user picked a course."""
    query = update.callback_query
    await query.answer()

    parts = query.data.split("|", 2)
    course = parts[2]
    context.user_data["nc_course"] = course

    cfg = _load_cfg()
    needs_type = cfg["attendance"]["courses"].get(course, {}).get("layout") == "typed_pair"
    context.user_data["nc_needs_type"] = needs_type

    await query.edit_message_text(
        f"📋 Course: *{course.upper()}*\n\nStep 2 of {'3' if needs_type else '3'} — Enter the session number (e.g. `5`):",
        parse_mode=ParseMode.MARKDOWN,
    )
    return NAMES_SESSION


@_guard("teacher", conv=True)
async def handle_names_session_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User typed the session number."""
    text = update.message.text.strip()
    try:
        session = int(text)
    except ValueError:
        await update.message.reply_text("❌ Please enter a valid number (e.g. `3`).", parse_mode=ParseMode.MARKDOWN)
        return NAMES_SESSION

    context.user_data["nc_session"] = session
    course = context.user_data["nc_course"]

    if context.user_data.get("nc_needs_type"):
        cfg = _load_cfg()
        all_types = _course_type_list(cfg, course)
        # Build numbered keyboard — one button per type
        kb = []
        for i, t in enumerate(all_types):
            kb.append([InlineKeyboardButton(t, callback_data=f"nc|type|{i}")])
        context.user_data["nc_type_list"] = all_types

        await update.message.reply_text(
            f"📋 Course: *{course.upper()}* · Session: *S{session}*\n\nStep 3 of 3 — Pick the session type:",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode=ParseMode.MARKDOWN,
        )
        return NAMES_TYPE

    # No type needed — go straight to paste step
    context.user_data["nc_type"] = None
    return await _ask_for_names(update.message, context, course, session, None)


@_guard("teacher", conv=True)
async def handle_names_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """nc|type|<index> — user picked a session type."""
    query = update.callback_query
    await query.answer()

    idx   = int(query.data.split("|", 2)[2])
    stype = context.user_data["nc_type_list"][idx]
    context.user_data["nc_type"] = stype

    course  = context.user_data["nc_course"]
    session = context.user_data["nc_session"]

    return await _ask_for_names(query.message, context, course, session, stype, edit=True)


async def _ask_for_names(message, context, course: str, session: int, stype: str | None, edit: bool = False) -> int:
    type_line = f"Type: *{stype}*\n" if stype else ""
    text = (
        f"✅ All set!\n"
        f"Course: *{course.upper()}* · Session: *S{session}*\n"
        f"{type_line}\n"
        "Now send me the names — *one name per message*, or all at once with each name on a new line.\n"
        "Optional mark after a name: `Ahmed Ali | L`, `Sara - absent`, `Omar, Late` "
        "(P / L / A / NC — no mark means Present).\n"
        "When you're done, send /done"
    )
    context.user_data["nc_names"] = []

    if edit:
        await message.edit_text(text, parse_mode=ParseMode.MARKDOWN)
    else:
        await message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

    return NAMES_PASTE


@_guard("teacher", conv=True)
async def handle_names_paste(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Accumulate pasted names. /done triggers processing."""
    text = update.message.text.strip()

    # Accept multi-line messages — split on newlines
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    context.user_data.setdefault("nc_names", []).extend(lines)
    count = len(context.user_data["nc_names"])

    await update.message.reply_text(
        f"✅ Added {len(lines)} name(s). Total so far: *{count}*.\n"
        "Keep sending names, or send /done to process.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return NAMES_PASTE


@_guard("teacher", conv=True)
async def handle_names_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User sent /done — parse marks, show how each line was read, then run the text pipeline.

    Always ends the conversation on failure. If a line has a mark we do not recognise
    (and ``attendance.ingest.names_preview`` allows it) the user is asked to confirm first.
    """
    parsed = marks_mod.parse_lines(context.user_data.get("nc_names", []))
    if not parsed:
        await update.message.reply_text("⚠️ No names collected yet. Send at least one name first.")
        return NAMES_PASTE

    chat_id = update.message.chat_id
    try:
        _require(context.user_data, "nc_course", "nc_session")
        mode = _input_settings()["names_preview"]
    except Exception as exc:
        log.error("ingest_text_pipeline_error",
                  extra={"flow": "names", "chat_id": chat_id, "error": str(exc)}, exc_info=True)
        await _notify_failure(context.bot, chat_id, _user_error_message(exc, "names"))
        context.user_data.clear()
        return ConversationHandler.END

    names, marks = marks_mod.split_names_and_marks(parsed)

    # Plain text (no parse_mode): names may contain Markdown-reserved characters.
    if mode != "never":
        try:
            await update.message.reply_text(marks_mod.format_preview(parsed))
        except Exception:
            log.warning("names_preview_send_failed", extra={"chat_id": chat_id}, exc_info=True)

    if mode == "always" or (mode == "on_issues" and marks_mod.has_issues(parsed)):
        context.user_data["nc_parsed"] = list(zip(names, marks))
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Process now", callback_data="pv|go")],
            [InlineKeyboardButton("✏️ Fix & resend", callback_data="pv|fix"),
             InlineKeyboardButton("✖️ Cancel", callback_data="pv|cancel")],
        ])
        note = ("Lines marked ⚠️ will go to the review queue instead of being written. "
                if marks_mod.has_issues(parsed) else "")
        await update.message.reply_text(f"{note}Does this look right?", reply_markup=kb)
        return NAMES_CONFIRM

    return await _process_names(context, chat_id, names, marks)


@_guard("teacher", conv=True)
async def handle_names_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """pv|go / pv|fix / pv|cancel — answer to the parse preview."""
    query = update.callback_query
    await query.answer()
    action = query.data.split("|", 1)[1]
    chat_id = query.message.chat_id

    if action == "go":
        try:
            pending, _course, _session = _require(context.user_data, "nc_parsed", "nc_course", "nc_session")
        except _StaleSession as exc:
            await _notify_failure(context.bot, chat_id, _user_error_message(exc, "names"))
            context.user_data.clear()
            return ConversationHandler.END
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        names = [n for n, _ in pending]
        marks = [m for _, m in pending]
        return await _process_names(context, chat_id, names, marks)

    if action == "fix":
        context.user_data["nc_names"] = []
        context.user_data.pop("nc_parsed", None)
        await query.edit_message_text("✏️ No problem — send the corrected list, then /done.")
        return NAMES_PASTE

    context.user_data.clear()
    await query.edit_message_text("✖️ Cancelled. Nothing was processed.")
    return ConversationHandler.END


async def _process_names(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                         names: list[str], marks: list[str]) -> int:
    """Run the text pipeline for already-parsed names. Always ends the conversation."""
    summary = None
    review_dir = "review_queue"
    try:
        course, session = _require(context.user_data, "nc_course", "nc_session")
        stype = context.user_data.get("nc_type")

        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏳ Processing *{len(names)}* name(s) for *{course.upper()} S{session}*…",
            parse_mode=ParseMode.MARKDOWN,
        )

        cfg = _load_cfg()
        review_dir = cfg["attendance"]["review"].get("local_dir", "review_queue")

        summary = await _run_blocking(
            lambda: run_text(
                names=names,
                marks=marks,
                course=course,
                session_num=session,
                session_type=stype,
                dry_run=cfg["core"]["app"].get("dry_run_default", True),
                force=False,
                config_path="config/attendance.yaml",
                review_store_dir=review_dir,
            )
        )
    except Exception as exc:
        log.error("ingest_text_pipeline_error",
                  extra={"flow": "names", "chat_id": chat_id, "error": str(exc)}, exc_info=True)
        await _notify_failure(context.bot, chat_id, _user_error_message(exc, "names"))
    else:
        await _deliver_summary(chat_id, context.bot, summary, review_dir)
    finally:
        context.user_data.clear()

    return ConversationHandler.END


# ─── Shared summary sender ────────────────────────────────────────────────────

async def _send_summary(chat_id: int, bot, summary, review_dir: str) -> None:
    kb = []
    if summary.sheet_url:
        kb.append([InlineKeyboardButton("📊 Open Google Sheet", url=summary.sheet_url)])
    missing_n = getattr(summary, "missing_count", 0)
    if getattr(summary, "absent_offer", False) and isinstance(missing_n, int) and missing_n > 0:
        label = f"🙈 Mark {missing_n} absent?" + (" (dry-run)" if getattr(summary, "dry_run", False) else "")
        kb.append([InlineKeyboardButton(label, callback_data=f"abs|{summary.upload_id}")])
    kb.append([InlineKeyboardButton("🔄 Undo / Revert", callback_data=f"rev|{summary.upload_id}")])

    await bot.send_message(
        chat_id=chat_id,
        text=summary.text(),
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.MARKDOWN,
    )

    if summary.flagged > 0:
        await _send_next_review_item(chat_id, bot, summary.upload_id, review_dir)


# ─── Image crop helper ────────────────────────────────────────────────────────

def _crop_image(img_path: str, bbox: list[float]) -> str:
    if not bbox or len(bbox) != 4:
        return img_path
    try:
        img = cv2.imread(img_path)
        if img is None:
            return img_path
        h, w = img.shape[:2]
        ymin, xmin, ymax, xmax = bbox
        y1, y2 = max(0, int(ymin * h) - 20), min(h, int(ymax * h) + 20)
        x1, x2 = max(0, int(xmin * w) - 20), min(w, int(xmax * w) + 20)
        crop = img[y1:y2, x1:x2]
        crop_path = re.sub(r"\.(jpe?g|png)$", r"_crop.\1", img_path, flags=re.IGNORECASE)
        cv2.imwrite(crop_path, crop)
        return crop_path
    except Exception:
        return img_path


# ─── Review queue sender ──────────────────────────────────────────────────────

async def _send_next_review_item(chat_id: int, bot, upload_id: str, review_dir: str) -> None:
    loop = asyncio.get_event_loop()

    def _get_queue_data():
        q = ReviewQueue(store_dir=review_dir)
        p = q.list_pending(upload_id)
        t = len(q._load(upload_id).get("items", []))
        return p, t

    pending, total = await loop.run_in_executor(None, _get_queue_data)

    if not pending:
        await bot.send_message(chat_id=chat_id, text="🎉 All review items for this sheet have been resolved!")
        return

    item = pending[0]
    current_idx = total - len(pending) + 1

    img_path  = item.get("image_ref", "")
    bbox      = item.get("bounding_box")
    crop_path = _crop_image(img_path, bbox) if bbox and os.path.exists(img_path) else img_path

    keyboard = []
    for match in item.get("top3", []):
        cb_data  = f"res|{upload_id[:12]}|{item['item_id'][:8]}|{match['roster_row']}"
        btn_text = f"[{match['score']}%] {match['roster_name']}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=cb_data)])

    keyboard.append([
        InlineKeyboardButton("❌ Absent", callback_data=f"res|{upload_id[:12]}|{item['item_id'][:8]}|absent"),
        InlineKeyboardButton("🔍 Search", callback_data=f"srch|{upload_id[:12]}|{item['item_id'][:8]}"),
    ])
    keyboard.append([InlineKeyboardButton("⏭️ Skip / Ignore", callback_data=f"res|{upload_id[:12]}|{item['item_id'][:8]}|0")])

    caption = (
        f"⚠️ *Review Required ({current_idx} of {total})*\n"
        f"Paper name: `{item['raw_name']}` (Row {item['paper_row_index']})\n"
        f"Confidence: {item['ocr_confidence']*100:.0f}%"
    )

    try:
        if os.path.exists(crop_path):
            loop = asyncio.get_event_loop()
            file_bytes = await loop.run_in_executor(None, lambda: open(crop_path, "rb").read())
            await bot.send_photo(
                chat_id=chat_id,
                photo=file_bytes,
                caption=caption,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            raise FileNotFoundError
    except Exception:
        await bot.send_message(
            chat_id=chat_id,
            text=caption,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN,
        )


# ─── Resolution callback ──────────────────────────────────────────────────────

@_guard("admin")
async def handle_resolution_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data
    if not data.startswith("res|"):
        return

    parts = data.split("|", 3)          # ["res", short_upload_id, item_id, action]
    if len(parts) != 4:
        return
    _, short_upload_id, item_id, action = parts

    cfg        = _load_cfg()
    review_dir = cfg["attendance"]["review"].get("local_dir", "review_queue")

    loop = asyncio.get_event_loop()

    def _find_upload():
        queue    = ReviewQueue(store_dir=review_dir)
        full_uid = None
        for p in Path(review_dir).glob(f"{short_upload_id}*.json"):
            full_uid = p.stem
            break
        if not full_uid:
            return None, None, None
        return full_uid, queue, queue.list_pending(full_uid)

    full_upload_id, queue, pending = await loop.run_in_executor(None, _find_upload)

    if not full_upload_id:
        await query.edit_message_caption(caption="❌ Error: Queue file not found or expired.")
        return

    target_item = next((i for i in pending if i["item_id"].startswith(item_id)), None)

    if not target_item:
        await query.edit_message_caption(caption="✅ Item already resolved.")
        return

    course       = target_item["course"]
    session_info = resolve(course, target_item["session"], target_item.get("session_type"), cfg)

    roster_row       = 0
    roster_name      = "IGNORED"
    attendance_value = "Absent"

    if action == "absent":
        roster_name = "MARKED_ABSENT"
    elif action == "0":
        roster_name = "IGNORED"
    else:
        try:
            roster_row = int(action)
        except ValueError:
            await query.edit_message_caption(caption="❌ Invalid action.")
            return
        roster_name      = next((m["roster_name"] for m in target_item["top3"] if m["roster_row"] == roster_row), "Unknown")
        attendance_value = canonical_mark(target_item.get("raw_mark"))

    def _do_write():
        if roster_row > 0:
            ws = session_info.get_worksheet()
            cells: list[WriteCell] = [
                WriteCell(
                    roster_name=roster_name,
                    sheet_row=roster_row,
                    col=session_info.session_col,
                    old_value=None,
                    new_value=attendance_value,
                )
            ]

            # For typed_pair courses (g9/g10) also write the session-type cell
            # that sits immediately to the left of the Sx Attendance column.
            # Previously this was silently omitted, leaving the type column blank
            # even though the attendance value was written correctly.
            session_type = target_item.get("session_type")
            if session_info.type_col and session_type:
                from attendance.pipeline import _col_idx
                type_col_data = ws.col_values(_col_idx(session_info.type_col))
                type_current = type_col_data[roster_row - 1] if roster_row <= len(type_col_data) else ""
                if not type_current.strip():
                    cells.append(WriteCell(
                        roster_name=roster_name,
                        sheet_row=roster_row,
                        col=session_info.type_col,
                        old_value=type_current or None,
                        new_value=session_type,
                    ))

            batch_write(
                session_info,
                cells,
                upload_id=full_upload_id,
                dry_run=cfg["core"]["app"].get("dry_run_default", True),
                retry_cfg=cfg["attendance"]["write"].get("retry", {}),
            )

            match_cfg = cfg["attendance"].get("match", {})
            if match_cfg.get("learn_aliases", True):
                AliasStore(
                    path=match_cfg.get("aliases_file", "config/learned_aliases.json"),
                    min_confirmations=int(match_cfg.get("alias_min_confirmations", 1)),
                ).learn(course, target_item["raw_name"], roster_name)
            if not cfg["core"]["app"].get("dry_run_default", True):
                from attendance import variants as variants_mod      # Feature 15: suggestion only, no effect on matching
                variants_mod.record_resolution(cfg, course, target_item["raw_name"], roster_name)

        queue.resolve(
            full_upload_id,
            item_id,
            chosen_roster_row=roster_row,
            chosen_roster_name=roster_name,
            attendance_value=attendance_value,
            resolved_by="telegram",
        )

    try:
        await loop.run_in_executor(None, _do_write)
        await query.message.delete()
        await _send_next_review_item(query.message.chat_id, context.bot, full_upload_id, review_dir)
    except Exception as e:
        try:
            await query.edit_message_caption(caption=f"❌ Error writing to sheet: {e}")
        except Exception:
            await query.edit_message_text(text=f"❌ Error writing to sheet: {e}")


# ─── Search flow ──────────────────────────────────────────────────────────────

@_guard("admin", conv=True)
async def handle_search_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    parts = query.data.split("|", 2)   # ["srch", short_upload_id, item_id]
    if len(parts) != 3:
        return ConversationHandler.END

    _, short_upload_id, item_id = parts
    context.user_data["search_upload_id"] = short_upload_id
    context.user_data["search_item_id"]   = item_id

    await query.message.reply_text(
        "🔍 Type a few letters of the student's name to search the roster:",
        reply_markup=ReplyKeyboardRemove(),
    )
    return SEARCH_ROSTER


@_guard("admin", conv=True)
async def search_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    search_term    = update.message.text.lower()
    short_upload_id = context.user_data.get("search_upload_id")
    item_id        = context.user_data.get("search_item_id")

    if not short_upload_id or not item_id:
        await update.message.reply_text("Search context lost. Please try again.")
        return ConversationHandler.END

    cfg        = _load_cfg()
    review_dir = cfg["attendance"]["review"].get("local_dir", "review_queue")

    loop = asyncio.get_event_loop()

    def _find_upload():
        queue    = ReviewQueue(store_dir=review_dir)
        full_uid = None
        for p in Path(review_dir).glob(f"{short_upload_id}*.json"):
            full_uid = p.stem
            break
        if not full_uid:
            return None, None, None
        return full_uid, queue, queue.list_pending(full_uid)

    full_upload_id, queue, pending = await loop.run_in_executor(None, _find_upload)

    if not full_upload_id:
        await update.message.reply_text("❌ Queue file not found.")
        return ConversationHandler.END

    target_item = next((i for i in pending if i["item_id"].startswith(item_id)), None)
    if not target_item:
        await update.message.reply_text("✅ Item already resolved.")
        return ConversationHandler.END

    course       = target_item["course"]
    session_info = resolve(course, target_item["session"], target_item.get("session_type"), cfg)

    matches = [r for r in session_info.roster if search_term in r[0].lower()][:5]

    if not matches:
        await update.message.reply_text("No students found matching that name. Try again or /cancel.")
        return SEARCH_ROSTER

    keyboard = []
    for roster_name, roster_row in matches:
        cb_data = f"res|{short_upload_id}|{item_id}|{roster_row}"
        keyboard.append([InlineKeyboardButton(roster_name, callback_data=cb_data)])

    await update.message.reply_text("Found these students:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ConversationHandler.END


# ─── Administrative commands ──────────────────────────────────────────────────

@_guard("admin")
async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    parts = update.message.text.split()
    if len(parts) != 3:
        await update.message.reply_text("Usage: /report <course> <session>\nExample: /report g10 3")
        return
    try:
        course, session = parts[1].lower(), int(parts[2])
    except ValueError:
        await update.message.reply_text("❌ Session must be a number.")
        return

    cfg = _load_cfg()
    try:
        from attendance.reporter import generate_session_report
        review_dir = Path(cfg["attendance"]["review"].get("local_dir", "review_queue"))
        loop = asyncio.get_event_loop()
        rep = await loop.run_in_executor(None, generate_session_report, course, session, cfg, review_dir)

        msg  = f"📊 *Report: {course.upper()} S{session}*\n"
        msg += f"Present: {rep['present']} / {rep['total_students']} ({rep['attendance_rate']:.1f}%)\n"
        msg += f"Review Backlog: {rep['review_backlog']} items\n\n*Absentees:*\n"
        msg += "\n".join(f"• {a}" for a in rep["absentees"]) if rep["absentees"] else "• None"
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"❌ Error generating report: {e}")


@_guard("admin")
async def cmd_streaks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    parts = update.message.text.split()
    if len(parts) < 2:
        await update.message.reply_text("Usage: /streaks <course> [consecutive=3]")
        return
    course = parts[1].lower()
    try:
        consec = int(parts[2]) if len(parts) > 2 else 3
    except ValueError:
        await update.message.reply_text("❌ Consecutive sessions must be a number.")
        return

    cfg = _load_cfg()
    try:
        from attendance.reporter import generate_streaks_report
        loop = asyncio.get_event_loop()
        streaks = await loop.run_in_executor(None, generate_streaks_report, course, consec, cfg)

        msg = f"📉 *Chronic Absentees ({course.upper()} - past {consec} sessions)*\n"
        msg += "\n".join(f"• Row {s['row']}: {s['name']}" for s in streaks) if streaks else "• None found"
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


@_guard("teacher")
async def cmd_revert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    parts = update.message.text.split()
    if len(parts) < 2:
        await update.message.reply_text("Usage: /revert <upload_id>\nExample: /revert a1b2c3d4")
        return
    upload_id = parts[1]
    cfg = _load_cfg()
    try:
        from attendance.reporter import read_audit_log, plan_revert
        from attendance.session_resolver import SheetSession
        from core.auth import get_credentials
        import gspread

        loop = asyncio.get_event_loop()

        def _do_revert():
            audit_records    = read_audit_log()
            plan             = plan_revert(upload_id, audit_records)
            if not plan:
                return False, "No records found."

            writes_by_target = [r for r in audit_records if r.upload_id == upload_id]
            course           = writes_by_target[0].course

            cells_to_write = [
                WriteCell(
                    roster_name=f"Cell {p.cell}",
                    sheet_row=p.sheet_row,
                    col=p.col,
                    old_value=p.current_in_audit,
                    new_value=p.revert_to_value or "",
                )
                for p in plan if not p.overwritten_since
            ]
            if not cells_to_write:
                return False, "All cells overwritten since, or nothing to revert."

            gc           = gspread.authorize(get_credentials())
            session_info = SheetSession(
                course=course, session_num=0, session_col="", type_col=None,
                roster=[], allowed_attendance=[], allowed_types=[],
                sheet_id=writes_by_target[0].sheet_id,
                tab=writes_by_target[0].tab,
                gc=gc,
            )
            res = batch_write(
                session_info, cells_to_write,
                upload_id=f"rev-{upload_id}"[:16],
                dry_run=False,
                retry_cfg=cfg["attendance"]["write"].get("retry", {}),
            )
            return True, f"Reverted {len(res.written)} cells."

        success, msg = await loop.run_in_executor(None, _do_revert)
        await update.message.reply_text(("✅ " if success else "❌ ") + msg)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


@_guard("teacher")
async def handle_revert_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the 🔄 Undo / Revert inline button (callback_data='rev|<upload_id>')."""
    query = update.callback_query
    await query.answer()

    upload_id = query.data.split("|", 1)[1]
    cfg = _load_cfg()
    try:
        from attendance.reporter import read_audit_log, plan_revert
        from attendance.session_resolver import SheetSession
        from core.auth import get_credentials
        import gspread

        loop = asyncio.get_event_loop()

        def _do_revert():
            audit_records    = read_audit_log()
            plan             = plan_revert(upload_id, audit_records)
            if not plan:
                return False, "No records found."

            writes_by_target = [r for r in audit_records if r.upload_id == upload_id]
            course           = writes_by_target[0].course

            cells_to_write = [
                WriteCell(
                    roster_name=f"Cell {p.cell}",
                    sheet_row=p.sheet_row,
                    col=p.col,
                    old_value=p.current_in_audit,
                    new_value=p.revert_to_value or "",
                )
                for p in plan if not p.overwritten_since
            ]
            if not cells_to_write:
                return False, "All cells overwritten since, or nothing to revert."

            gc           = gspread.authorize(get_credentials())
            session_info = SheetSession(
                course=course, session_num=0, session_col="", type_col=None,
                roster=[], allowed_attendance=[], allowed_types=[],
                sheet_id=writes_by_target[0].sheet_id,
                tab=writes_by_target[0].tab,
                gc=gc,
            )
            res = batch_write(
                session_info, cells_to_write,
                upload_id=f"rev-{upload_id}"[:16],
                dry_run=False,
                retry_cfg=cfg["attendance"]["write"].get("retry", {}),
            )
            return True, f"Reverted {len(res.written)} cells."

        success, msg = await loop.run_in_executor(None, _do_revert)
        await query.edit_message_text(("✅ " if success else "❌ ") + msg)
    except Exception as e:
        await query.edit_message_text(f"❌ Error: {e}")


_ABSENT_INFLIGHT: set[str] = set()


def _absent_result_text(res: "absent_mod.AbsentResult") -> str:
    """User-facing text for the outcome of the 'Mark N absent?' button."""
    if res.ok:
        if res.dry_run:
            return (f"🧪 [DRY-RUN] Would mark {res.marked} student(s) Absent. "
                    "Nothing was written to the sheet.")
        return (f"✅ Marked {res.marked} student(s) Absent."
                + (f" {res.already_filled} already had a value and were left alone." if res.already_filled else "")
                + "\nMade a mistake? Use Undo below.")
    return {
        "expired": "⚠️ That absent list has expired or was already handled, so nothing was changed. "
                   "Send the sheet again if you still need it.",
        "disabled": "ℹ️ Marking missing students absent isn't available for this course.",
        "pending_review": (f"⏸ {res.pending_review} review item(s) from this upload are still pending — "
                           "those students may actually be on the sheet. Resolve them first, then tap the button again."),
        "value_not_allowed": "❌ This sheet doesn't have an “Absent” option in its dropdown, so I didn't write anything. "
                             "Please contact the admin.",
        "nothing_to_mark": "ℹ️ Nothing to mark — every student who wasn't on the sheet already has a value.",
    }.get(res.reason, "❌ I couldn't mark anyone absent. Nothing was changed.")


@_guard("teacher")
async def handle_absent_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the 🙈 Mark N absent? button (callback_data='abs|<upload_id>'). crash/main only."""
    query = update.callback_query
    await query.answer()
    upload_id = query.data.split("|", 1)[1]
    chat_id = query.message.chat_id

    if upload_id in _ABSENT_INFLIGHT:
        return                                   # double-tap while the first is still running
    _ABSENT_INFLIGHT.add(upload_id)
    try:
        cfg = _load_cfg()
        review_dir = cfg["attendance"]["review"].get("local_dir", "review_queue")
        res = await _run_blocking(
            lambda: absent_mod.mark_missing_absent(upload_id, cfg=cfg, review_dir=review_dir))
    except Exception as exc:
        log.error("ingest_absent_error", extra={"chat_id": chat_id, "upload_id": upload_id, "error": str(exc)},
                  exc_info=True)
        await _notify_failure(context.bot, chat_id, _user_error_message(exc, "absent list"))
        return
    finally:
        _ABSENT_INFLIGHT.discard(upload_id)

    kb = None
    if res.ok and not res.dry_run:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Undo these absences", callback_data=f"rev|{res.abs_id}")]])
    try:
        await context.bot.send_message(chat_id=chat_id, text=_absent_result_text(res), reply_markup=kb)
    except Exception:
        log.error("ingest_absent_reply_failed", extra={"chat_id": chat_id}, exc_info=True)

    if res.ok or res.reason in ("expired", "nothing_to_mark"):
        # Take the button off the summary so it is not tapped twice (best effort).
        try:
            rows = query.message.reply_markup.inline_keyboard if query.message.reply_markup else []
            kept = [[b for b in row if not str(b.callback_data or "").startswith("abs|")] for row in rows]
            kept = [row for row in kept if row]
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(kept) if kept else None)
        except Exception:
            pass


@_guard("admin")
async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg        = _load_cfg()
    review_dir = Path(cfg["attendance"]["review"].get("local_dir", "review_queue"))

    if not review_dir.exists():
        await update.message.reply_text("No pending items.")
        return

    queue           = ReviewQueue(store_dir=str(review_dir))
    pending_uploads = [
        (f.stem, len(queue.list_pending(f.stem)))
        for f in review_dir.glob("*.json")
        if queue.list_pending(f.stem)
    ]

    if not pending_uploads:
        await update.message.reply_text("No pending items.")
        return

    msg = "*Pending Review Queues:*\n"
    for uid, count in pending_uploads:
        msg += f"• `{uid}` : {count} items\n"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


@_guard("strict_admin")
async def cmd_aliases(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only: /aliases [list [course] | flagged | remove <course> <paper name>].

    Unavailable (not open to everyone) until an admin is configured: ADMIN_TELEGRAM_IDS env var
    and/or attendance.admin.telegram_ids. Full role checks for every handler arrive in Phase 4.
    """
    try:
        cfg = _load_cfg()
        ids = alias_admin.admin_ids(cfg)
        user = getattr(update, "effective_user", None)
        uid = getattr(user, "id", None)
        if not ids:
            text = ("🔒 Alias management is unavailable: no admin is configured yet. "
                    "Set ADMIN_TELEGRAM_IDS (or attendance.admin.telegram_ids) and restart the bot.")
        elif uid not in ids:
            text = "🔒 This command is for admins only."
        else:
            text = alias_admin.handle(list(getattr(context, "args", None) or []), cfg)
    except Exception as exc:   # never log names; only the error type/message
        log.error("aliases_cmd_failed", extra={"error": str(exc)})
        text = "❌ Couldn't manage aliases right now. Please try again or use the command line."
    await update.message.reply_text(text[:3900])


@_guard("strict_admin")
async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only: plain-language status (Gemini / Sheets failures, review age, last alerts, reminders)."""
    try:
        cfg = _load_cfg()
        jq = getattr(getattr(context, "application", None), "job_queue", None)
        text = health_mod.status_text(cfg, scheduler=jq is not None)
    except Exception as exc:
        log.error("health_cmd_failed", extra={"error": type(exc).__name__})
        text = "❌ Couldn't read the system status right now. Please try again."
    await update.message.reply_text(text[:3900])


async def _global_error_handler(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catch-all for exceptions escaping any handler: log, and tell the user if we can."""
    exc = context.error
    log.error("ingest_unhandled_error",
              extra={"error": str(exc), "update_type": type(update).__name__}, exc_info=exc)
    chat = getattr(update, "effective_chat", None)
    if chat is not None:
        await _notify_failure(context.bot, chat.id, _user_error_message(exc, "request"))


@_guard("teacher", conv=True)
async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Cancelled.", reply_markup=ReplyKeyboardRemove())
    context.user_data.clear()
    return ConversationHandler.END


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print("ERROR: TELEGRAM_BOT_TOKEN not found in .env", file=sys.stderr)
        sys.exit(1)

    try:   # Feature 11: say clearly at startup who can use the bot
        for _w in roles.startup_warnings(_load_cfg()):
            log.warning("bot_access_warning", extra={"warning": _w})
    except Exception as exc:
        log.warning("bot_access_check_failed", extra={"error": type(exc).__name__})

    app = ApplicationBuilder().token(token).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start",  start_cmd),
            CommandHandler("upload", start_cmd),
            MessageHandler(filters.PHOTO | filters.Document.ALL, handle_photo),
            CallbackQueryHandler(handle_mode_callback,   pattern=r"^mode\|"),
            CallbackQueryHandler(handle_search_callback, pattern=r"^srch\|"),
        ],
        states={
            PHOTO_FLOW: [
                CallbackQueryHandler(handle_mode_callback,   pattern=r"^mode\|"),
                CallbackQueryHandler(handle_upload_callback, pattern=r"^up\|"),
                MessageHandler(filters.PHOTO | filters.Document.ALL, handle_photo),
            ],
            NAMES_COURSE: [
                CallbackQueryHandler(handle_names_course_callback, pattern=r"^nc\|course\|"),
            ],
            NAMES_SESSION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_names_session_text),
            ],
            NAMES_TYPE: [
                CallbackQueryHandler(handle_names_type_callback, pattern=r"^nc\|type\|"),
            ],
            NAMES_PASTE: [
                CommandHandler("done", handle_names_done),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_names_paste),
            ],
            NAMES_CONFIRM: [
                CallbackQueryHandler(handle_names_confirm, pattern=r"^pv\|"),
            ],
            SEARCH_ROSTER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, search_input_handler),
                MessageHandler(filters.PHOTO | filters.Document.ALL, handle_photo),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
    )

    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(handle_resolution_callback, pattern=r"^res\|"))
    app.add_handler(CallbackQueryHandler(handle_revert_callback,     pattern=r"^rev\|"))
    app.add_handler(CallbackQueryHandler(handle_absent_callback,     pattern=r"^abs\|"))
    app.add_handler(CommandHandler("report",  cmd_report))
    app.add_handler(CommandHandler("streaks", cmd_streaks))
    app.add_handler(CommandHandler("revert",  cmd_revert))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("aliases", cmd_aliases))
    app.add_handler(CommandHandler("health",  cmd_health))
    app.add_error_handler(_global_error_handler)

    try:   # Feature 13: remember where health state lives, schedule the review-age check
        health_mod.configure(_load_cfg())
        health_mod.install(app, _load_cfg)
    except Exception as exc:
        log.warning("health_install_failed", extra={"error": type(exc).__name__})

    try:   # Feature 12: optional reminders; a missing scheduler package never stops the bot
        reminders_mod.install(app, _load_cfg)
    except Exception as exc:
        log.warning("reminders_install_failed", extra={"error": type(exc).__name__})

    log.info("telegram_bot_started", extra={"status": "polling"})
    print("Telegram bot is running. Send /start to begin.")
    app.run_polling()


if __name__ == "__main__":
    main()
