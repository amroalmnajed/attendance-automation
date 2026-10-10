"""attendance/ingest.py — Telegram bot ingest channel.

Listens for photos, asks for course/session details, runs the attendance pipeline,
and replies with the summary.

Usage:
  python -m attendance.ingest
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import yaml
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(override=True)

if sys.stdout.encoding != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)

from core.logger import get_logger
from core.notifier import send
from attendance.pipeline import run

log = get_logger("ingest")

# Conversation states
PHOTO, COURSE, SESSION, TYPE = range(4)

COURSES = ["crash", "main", "g10", "g9"]

def _load_cfg(path: str = "config/attendance.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the upload conversation."""
    await update.message.reply_text(
        "Send me a photo of an attendance sheet to process.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return PHOTO

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Download the photo and ask for the course."""
    photo_file = await update.message.photo[-1].get_file()
    
    # Save temporarily
    tmp_dir = Path(tempfile.gettempdir()) / "attendance_bot"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    img_path = tmp_dir / f"{photo_file.file_id}.jpg"
    await photo_file.download_to_drive(custom_path=img_path)
    
    context.user_data['image_path'] = str(img_path)
    
    reply_keyboard = [[c] for c in COURSES]
    await update.message.reply_text(
        "Got the photo! Which course is this for?",
        reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True),
    )
    return COURSE

async def handle_course(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    course = update.message.text.lower()
    if course not in COURSES:
        await update.message.reply_text(f"Invalid course. Choose from: {', '.join(COURSES)}")
        return COURSE
        
    context.user_data['course'] = course
    await update.message.reply_text(
        "What is the session number? (e.g. 1)",
        reply_markup=ReplyKeyboardRemove(),
    )
    return SESSION

async def handle_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    session_text = update.message.text
    if not session_text.isdigit():
        await update.message.reply_text("Please enter a valid number for the session.")
        return SESSION
        
    context.user_data['session'] = int(session_text)
    course = context.user_data['course']
    
    cfg = _load_cfg()
    course_cfg = cfg["attendance"]["courses"][course]
    
    if course_cfg.get("layout") == "typed_pair":
        types = []
        types_dict = course_cfg.get("types", {})
        if "any" in types_dict:
            types.extend(types_dict["any"])
        else:
            session_num = context.user_data['session']
            if session_num % 2 == 1:
                types.extend(types_dict.get("odd", []))
            else:
                types.extend(types_dict.get("even", []))
                
        reply_keyboard = [[t] for t in types]
        await update.message.reply_text(
            "What is the session type?",
            reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True),
        )
        return TYPE
    else:
        # No type needed, run pipeline
        context.user_data['type'] = None
        await _run_pipeline(update, context)
        return ConversationHandler.END

async def handle_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['type'] = update.message.text
    await _run_pipeline(update, context)
    return ConversationHandler.END

async def _run_pipeline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    course = context.user_data['course']
    session_num = context.user_data['session']
    session_type = context.user_data['type']
    img_path = context.user_data['image_path']
    
    await update.message.reply_text(
        f"Processing {course.upper()} Session {session_num} {session_type or ''}...",
        reply_markup=ReplyKeyboardRemove(),
    )
    
    cfg = _load_cfg()
    review_dir = cfg["attendance"]["review"].get("local_dir", "review_queue")
    
    try:
        # Run pipeline in a background thread so it doesn't block the async event loop
        loop = asyncio.get_running_loop()
        summary = await loop.run_in_executor(
            None,
            lambda: run(
                image_path=img_path,
                course=course,
                session_num=session_num,
                session_type=session_type,
                dry_run=cfg["core"]["app"].get("dry_run_default", True),
                force=False,
                config_path="config/attendance.yaml",
                review_store_dir=review_dir,
            )
        )
        
        # Send summary back to telegram
        await update.message.reply_text(
            f"```text\n{summary.text()}\n```",
            parse_mode="MarkdownV2"
        )
        
        if summary.flagged > 0:
            await update.message.reply_text(
                f"⚠️ {summary.flagged} item(s) flagged for review.\n"
                f"Use the CLI to resolve them:\n"
                f"`python -m attendance.cli review list`"
            )
            
    except Exception as exc:
        log.error("ingest_pipeline_error", extra={"error": str(exc)})
        await update.message.reply_text(f"❌ Error processing sheet: {exc}")
    finally:
        # Cleanup temp image
        try:
            os.remove(img_path)
        except OSError:
            pass
        context.user_data.clear()

async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the conversation."""
    await update.message.reply_text("Upload cancelled.", reply_markup=ReplyKeyboardRemove())
    context.user_data.clear()
    return ConversationHandler.END

def main():
    load_dotenv(override=True)
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print("ERROR: TELEGRAM_BOT_TOKEN not found in .env", file=sys.stderr)
        sys.exit(1)
        
    app = ApplicationBuilder().token(token).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start_cmd),
            CommandHandler("upload", start_cmd),
            MessageHandler(filters.PHOTO, handle_photo),
        ],
        states={
            PHOTO: [MessageHandler(filters.PHOTO, handle_photo)],
            COURSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_course)],
            SESSION: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_session)],
            TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_type)],
        },
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
    )

    app.add_handler(conv_handler)
    
    log.info("telegram_bot_started", extra={"status": "polling"})
    print("Telegram bot is running. Send it a photo to start.")
    app.run_polling()

if __name__ == "__main__":
    main()
