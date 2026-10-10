"""attendance/ingest.py — Telegram bot ingest channel."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
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
from attendance.pipeline import run, run_text
from attendance.normalizer import canonical_mark
from attendance.review_queue import ReviewQueue
from attendance.writer import WriteCell, batch_write
from attendance.session_resolver import resolve
from attendance.aliases import AliasStore

log = get_logger("ingest")

COURSES = ["crash", "main", "g10", "g9"]

# ── Conversation states ───────────────────────────────────────────────────────
PHOTO_FLOW    = 1   # waiting for photo or inline buttons (image path)
SEARCH_ROSTER = 2   # waiting for roster search text
NAMES_COURSE  = 3   # paste-names flow: waiting for course choice
NAMES_SESSION = 4   # paste-names flow: waiting for session number
NAMES_TYPE    = 5   # paste-names flow: waiting for session type
NAMES_PASTE   = 6   # paste-names flow: waiting for the name list

def _load_cfg(path: str = "config/attendance.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


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


# ─── /start ───────────────────────────────────────────────────────────────────

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

    context.user_data["image_path"] = str(img_path)
    context.user_data["course"]  = "g10"
    context.user_data["session"] = 1
    context.user_data["type"]    = "Not Set"

    # Parse optional caption
    caption = update.message.caption or ""
    if caption:
        cap_lower = caption.lower()
        for c in COURSES:
            if c in cap_lower:
                context.user_data["course"] = c
                break
        s_match = re.search(r"s(\d+)", cap_lower)
        if s_match:
            context.user_data["session"] = int(s_match.group(1))

        if "saturday"  in cap_lower: context.user_data["type"] = "Saturday - Center"  if "center" in cap_lower else "Saturday - Online"
        elif "sunday"  in cap_lower: context.user_data["type"] = "Sunday- Center"     if "center" in cap_lower else "Sunday- Online"
        elif "monday"  in cap_lower: context.user_data["type"] = "Monday - Online"
        elif "tuesday" in cap_lower: context.user_data["type"] = "Tuesday - Center"   if "center" in cap_lower else "Tuesday  - Online"
        elif "wednesday" in cap_lower: context.user_data["type"] = "Wednesday - Online"
        elif "thursday" in cap_lower: context.user_data["type"] = "Thursday - Center" if "center" in cap_lower else "Thursday - Online"
        elif "friday"  in cap_lower: context.user_data["type"] = "Friday - Center"    if "center" in cap_lower else "Friday - Online"

    await _send_upload_menu(update.message, context)
    return PHOTO_FLOW


async def _send_upload_menu(message, context, edit: bool = False) -> None:
    c = context.user_data["course"]
    s = context.user_data["session"]
    t = context.user_data["type"]

    kb = [
        [InlineKeyboardButton(f"Course: {c.upper()}", callback_data="up|course"),
         InlineKeyboardButton(f"Session: S{s}",       callback_data="up|session")],
        [InlineKeyboardButton(f"Type: {t}",            callback_data="up|type")],
        [InlineKeyboardButton("▶️ Start Processing",   callback_data="up|start")],
    ]
    text = "📋 *New Attendance Upload*\nPlease confirm the details:"

    if edit:
        await message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
    else:
        await message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)


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
    course       = context.user_data["course"]
    session_num  = context.user_data["session"]
    session_type = context.user_data["type"] if context.user_data["type"] != "Not Set" else None
    img_path     = context.user_data["image_path"]

    status_msg = await context.bot.send_message(
        chat_id=chat_id,
        text="⏳ Processing sheet (OpenCV + Gemini OCR + Google Sheets)...",
    )
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    cfg = _load_cfg()
    review_dir = cfg["attendance"]["review"].get("local_dir", "review_queue")

    review_images_dir = Path("review_images")
    review_images_dir.mkdir(parents=True, exist_ok=True)
    persistent_img_path = review_images_dir / f"{uuid.uuid4().hex}{Path(img_path).suffix}"
    shutil.copy2(img_path, persistent_img_path)

    try:
        loop = asyncio.get_event_loop()
        summary = await loop.run_in_executor(
            None,
            lambda: run(
                image_path=str(persistent_img_path),
                course=course,
                session_num=session_num,
                session_type=session_type,
                dry_run=cfg["core"]["app"].get("dry_run_default", True),
                force=False,
                config_path="config/attendance.yaml",
                review_store_dir=review_dir,
            ),
        )

        await status_msg.delete()
        await _send_summary(chat_id, context.bot, summary, review_dir)

    except Exception as exc:
        log.error("ingest_pipeline_error", extra={"error": str(exc)})
        try:
            await status_msg.edit_text(f"❌ Error processing sheet: {exc}")
        except Exception:
            await context.bot.send_message(chat_id=chat_id, text=f"❌ Error processing sheet: {exc}")
    finally:
        try:
            os.remove(img_path)
        except OSError:
            pass
        context.user_data.clear()


# ─── Paste-names flow ─────────────────────────────────────────────────────────

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
        "When you're done, send /done"
    )
    context.user_data["nc_names"] = []

    if edit:
        await message.edit_text(text, parse_mode=ParseMode.MARKDOWN)
    else:
        await message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

    return NAMES_PASTE


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


async def handle_names_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User sent /done — run the text pipeline."""
    names = context.user_data.get("nc_names", [])
    if not names:
        await update.message.reply_text("⚠️ No names collected yet. Send at least one name first.")
        return NAMES_PASTE

    course   = context.user_data["nc_course"]
    session  = context.user_data["nc_session"]
    stype    = context.user_data.get("nc_type")
    chat_id  = update.message.chat_id

    await update.message.reply_text(
        f"⏳ Processing *{len(names)}* name(s) for *{course.upper()} S{session}*…",
        parse_mode=ParseMode.MARKDOWN,
    )

    cfg = _load_cfg()
    review_dir = cfg["attendance"]["review"].get("local_dir", "review_queue")

    try:
        loop = asyncio.get_event_loop()
        summary = await loop.run_in_executor(
            None,
            lambda: run_text(
                names=names,
                course=course,
                session_num=session,
                session_type=stype,
                dry_run=cfg["core"]["app"].get("dry_run_default", True),
                force=False,
                config_path="config/attendance.yaml",
                review_store_dir=review_dir,
            ),
        )
        await _send_summary(chat_id, context.bot, summary, review_dir)
    except Exception as exc:
        log.error("ingest_text_pipeline_error", extra={"error": str(exc)})
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Error processing names: {exc}")
    finally:
        context.user_data.clear()

    return ConversationHandler.END


# ─── Shared summary sender ────────────────────────────────────────────────────

async def _send_summary(chat_id: int, bot, summary, review_dir: str) -> None:
    kb = []
    if summary.sheet_url:
        kb.append([InlineKeyboardButton("📊 Open Google Sheet", url=summary.sheet_url)])
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
    app.add_handler(CommandHandler("report",  cmd_report))
    app.add_handler(CommandHandler("streaks", cmd_streaks))
    app.add_handler(CommandHandler("revert",  cmd_revert))
    app.add_handler(CommandHandler("pending", cmd_pending))

    log.info("telegram_bot_started", extra={"status": "polling"})
    print("Telegram bot is running. Send /start to begin.")
    app.run_polling()


if __name__ == "__main__":
    main()
