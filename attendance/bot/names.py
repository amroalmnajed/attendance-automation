"""attendance/bot/names.py — paste-names flow: course/session/type pickers, /done preview, processing."""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from attendance import marks as marks_mod
from attendance.pipeline import run_text

from attendance.bot.common import (
    NAMES_CONFIRM,
    NAMES_PASTE,
    NAMES_SESSION,
    NAMES_TYPE,
    _StaleSession,
    _course_type_list,
    _get_sheet_url,
    _guard,
    _input_settings,
    _load_cfg,
    _notify_failure,
    _require,
    _run_blocking,
    _user_error_message,
    log,
)
from attendance.bot.review import _deliver_summary


# ─── Paste-names flow ─────────────────────────────────────────────────────────

@_guard("authorized", conv=True)
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

    sheet_url = _get_sheet_url(course, cfg)
    sheet_line = f"\n📄 Sheet: [{sheet_url}]({sheet_url})" if sheet_url else ""

    await query.edit_message_text(
        f"📋 Course: *{course.upper()}*{sheet_line}\n\nStep 2 of {'3' if needs_type else '3'} — Enter the session number (e.g. `5`):",
        parse_mode=ParseMode.MARKDOWN,
    )
    return NAMES_SESSION


@_guard("authorized", conv=True)
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
        if not all_types:   # an empty InlineKeyboardMarkup is a Telegram BadRequest
            log.error("names_no_session_types", extra={"course": course})
            await update.message.reply_text(
                f"⚠ No session types are configured for {course.upper()}. "
                "Please tell the admin, then send /start again.")
            context.user_data.clear()
            return ConversationHandler.END
        # Build numbered keyboard — one button per type
        kb = []
        for i, t in enumerate(all_types):
            kb.append([InlineKeyboardButton(t, callback_data=f"nc|type|{i}")])
        context.user_data["nc_type_list"] = all_types

        sheet_url = _get_sheet_url(course, cfg)
        sheet_line = f"\n📄 Sheet: [{sheet_url}]({sheet_url})" if sheet_url else ""

        await update.message.reply_text(
            f"📋 Course: *{course.upper()}* · Session: *S{session}*{sheet_line}\n\nStep 3 of 3 — Pick the session type:",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode=ParseMode.MARKDOWN,
        )
        return NAMES_TYPE

    # No type needed — go straight to paste step
    context.user_data["nc_type"] = None
    return await _ask_for_names(update.message, context, course, session, None)


@_guard("authorized", conv=True)
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


@_guard("authorized", conv=True)
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


@_guard("authorized", conv=True)
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


@_guard("authorized", conv=True)
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
