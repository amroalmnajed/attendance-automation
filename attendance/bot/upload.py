"""attendance/bot/upload.py — photo upload flow: /start, photo intake, upload menu, pipeline run."""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from datetime import datetime

from attendance.pipeline import run, run_multi
from attendance.session_resolver import resolve
from attendance.writer import check_existing

from attendance.bot.common import (
    COURSES,
    NAMES_COURSE,
    PHOTO_FLOW,
    PIPELINE_TIMEOUT_S,
    STAGE_TEXT,
    _course_keyboard,
    _get_sheet_url,
    _guard,
    _input_settings,
    _load_cfg,
    _notify_failure,
    _require,
    _run_blocking,
    _user_error_message,
    log,
    make_progress_callback,
)
from attendance.bot.review import _deliver_summary


# ─── /start ───────────────────────────────────────────────────────────────────

@_guard("authorized", conv=True)
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


@_guard("authorized", conv=True)
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

@_guard("authorized", conv=True)
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

    cfg = _load_cfg()
    sheet_url = _get_sheet_url(c, cfg)
    sheet_line = f"📄 Sheet: [{sheet_url}]({sheet_url})\n" if sheet_url else ""

    text = f"📋 *New Attendance Upload*\n{photos_line}{sheet_line}Please confirm the details:"

    if edit:
        await message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
        return message
    return await message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)


@_guard("authorized", conv=True)
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
        loop = asyncio.get_running_loop()
        loop.create_task(_run_pipeline_flow(update, context, query.message.chat_id))
        return ConversationHandler.END

    elif action == "overwrite":
        try:
            await query.message.delete()
        except Exception:
            pass
        loop = asyncio.get_running_loop()
        loop.create_task(_run_pipeline_flow(update, context, query.message.chat_id, force=True))
        return ConversationHandler.END

    elif action == "cancel":
        try:
            await query.edit_message_text("Cancelled. No changes made.")
        except Exception:
            await query.message.reply_text("Cancelled. No changes made.")
        tmp_paths = list(context.user_data.get("image_paths") or [])
        img_path = context.user_data.get("image_path")
        for tmp in {p for p in [*tmp_paths, img_path] if p}:
            try:
                os.remove(tmp)
            except OSError:
                pass
        context.user_data.clear()
        return ConversationHandler.END

    return PHOTO_FLOW


async def _run_pipeline_flow(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    force: bool = False,
) -> None:
    # Runs as a fire-and-forget task: an uncaught exception here would vanish and leave
    # the user staring at "Processing…", so EVERYTHING is inside the try.
    img_path = context.user_data.get("image_path")
    tmp_paths: list[str] = list(context.user_data.get("image_paths") or ([img_path] if img_path else []))
    status_msg = None
    summary = None
    review_dir = "review_queue"
    cleanup_needed = True
    try:
        course, session_num, utype, img_path = _require(
            context.user_data, "course", "session", "type", "image_path")
        session_type = utype if utype != "Not Set" else None
        if not tmp_paths:
            tmp_paths = [img_path]

        cfg = _load_cfg()

        # Duplicate sheet upload guard
        course_cfg = cfg.get("attendance", {}).get("courses", {}).get(course)
        if not force and course_cfg:
            try:
                session_info = resolve(course, session_num, session_type, cfg)
                row_nums = [r for _, r in session_info.roster]
                exists = await _run_blocking(
                    lambda: check_existing(
                        session_info.sheet_id,
                        session_info.tab,
                        row_nums,
                        cfg=cfg,
                        col=session_info.session_col,
                    )
                )
                if exists:
                    cleanup_needed = False
                    today_str = datetime.now().strftime("%Y-%m-%d")
                    warn_text = (
                        f"⚠️ Attendance already exists for {course.upper()} Session {session_num} on {today_str}."
                    )
                    kb = InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton("✅ Overwrite", callback_data="up|overwrite"),
                            InlineKeyboardButton("❌ Cancel", callback_data="up|cancel"),
                        ]
                    ])
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=warn_text,
                        reply_markup=kb,
                    )
                    return
            except Exception as exc:
                log.warning("check_existing_failed", extra={"error": str(exc), "course": course})

        status_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=STAGE_TEXT["reading"],
        )
        progress = make_progress_callback(status_msg, asyncio.get_running_loop(), initial="reading")
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception:
            pass  # cosmetic only

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
                    force=force,
                    config_path="config/attendance.yaml",
                    review_store_dir=review_dir,
                    progress=progress,
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
                    force=force,
                    config_path="config/attendance.yaml",
                    review_store_dir=review_dir,
                    progress=progress,
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
        if cleanup_needed:
            for tmp in {p for p in [*tmp_paths, img_path] if p}:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            context.user_data.clear()
