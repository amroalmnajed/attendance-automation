"""attendance/bot/app.py — bot wiring: error handler, /cancel and main()."""
from __future__ import annotations

import os
import sys

from telegram import ReplyKeyboardRemove, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from attendance import health as health_mod, reminders as reminders_mod, roles
from attendance.bot.menu import cmd_help, post_init
from attendance.config_check import ConfigError, validate_cfg

from attendance.bot.common import (
    NAMES_CONFIRM,
    NAMES_COURSE,
    NAMES_PASTE,
    NAMES_SESSION,
    NAMES_TYPE,
    PHOTO_FLOW,
    SEARCH_ROSTER,
    _guard,
    _load_cfg,
    _notify_failure,
    _user_error_message,
    log,
)
from attendance.bot.upload import (
    handle_mode_callback,
    handle_photo,
    handle_upload_callback,
    start_cmd,
)
from attendance.bot.names import (
    handle_names_confirm,
    handle_names_course_callback,
    handle_names_done,
    handle_names_paste,
    handle_names_session_text,
    handle_names_type_callback,
)
from attendance.bot.review import (
    handle_resolution_callback,
    handle_search_callback,
    search_input_handler,
)
from attendance.bot.admin import (
    cmd_aliases,
    cmd_health,
    cmd_pending,
    cmd_report,
    cmd_revert,
    cmd_streaks,
    handle_absent_callback,
    handle_revert_callback,
)


async def _global_error_handler(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catch-all for exceptions escaping any handler: log, and tell the user if we can."""
    exc = context.error
    log.error("ingest_unhandled_error",
              extra={"error": str(exc), "update_type": type(update).__name__}, exc_info=exc)
    chat = getattr(update, "effective_chat", None)
    if chat is not None:
        await _notify_failure(context.bot, chat.id, _user_error_message(exc, "request"))


@_guard("authorized", conv=True)
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

    try:   # fail fast on config mistakes (e.g. a typed_pair course with no session types)
        validate_cfg(_load_cfg())
    except ConfigError as exc:
        log.error("bot_config_invalid", extra={"error": str(exc)})
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:   # unreadable config is handled elsewhere; keep old startup behaviour
        log.warning("bot_config_check_skipped", extra={"error": type(exc).__name__})

    try:   # Feature 11: say clearly at startup who can use the bot
        for _w in roles.startup_warnings(_load_cfg()):
            log.warning("bot_access_warning", extra={"warning": _w})
    except Exception as exc:
        log.warning("bot_access_check_failed", extra={"error": type(exc).__name__})

    app = ApplicationBuilder().token(token).post_init(post_init).build()

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
    app.add_handler(CommandHandler("help",    cmd_help))
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
