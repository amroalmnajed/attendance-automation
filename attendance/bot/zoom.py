"""attendance/bot/zoom.py — Telegram handlers for Zoom attendance flows.

Implements /zoom_fetch, /zoom_upload, and /zoom_reprocess commands with a shared wizard.
"""
from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from attendance.pipeline import run_zoom
from attendance.zoom_api import list_recent_meeting_instances
from attendance.zoom_csv import (
    archive_csv,
    determine_session_type,
    extract_date_from_filename,
    extract_meeting_id_from_filename,
)

from attendance.bot.common import (
    COURSES,
    ZOOM_CONFIRM,
    ZOOM_MEETING_ID,
    ZOOM_PICK_INSTANCE,
    ZOOM_PICK_TYPE,
    ZOOM_SESSION_NUM,
    ZOOM_TEACHER_TIME,
    _StaleSession,
    _course_type_list,
    _get_sheet_url,
    _guard,
    _load_cfg,
    _run_blocking,
    _user_error_message,
    log,
)


# ─── Commands ─────────────────────────────────────────────────────────────────

@_guard("authorized", conv=True)
async def cmd_zoom_fetch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start Zoom API fetch flow - ask for meeting ID or course nickname."""
    context.user_data.clear()
    await update.message.reply_text(
        "📹 *Zoom Attendance — API Fetch*\n\n"
        "Enter the Zoom meeting ID (e.g., `81795786552`) or course nickname (e.g., `g10`):",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ZOOM_MEETING_ID


@_guard("authorized", conv=True)
async def cmd_zoom_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start CSV upload flow - prompt user to upload CSV file."""
    context.user_data.clear()
    await update.message.reply_text(
        "📹 *Zoom Attendance — CSV Upload*\n\n"
        "Please upload the Zoom participant CSV file.\n\n"
        "Expected filename format: `participants_<meeting_id>_<date>.csv`\n"
        "Example: `participants_81795786552_2026_09_13.csv`",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ZOOM_MEETING_ID


@_guard("authorized", conv=True)
async def cmd_zoom_reprocess(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """List archived CSVs and let user pick one to reprocess."""
    context.user_data.clear()
    cfg = _load_cfg()
    zoom_cfg = cfg.get("attendance", {}).get("zoom_csv", {})
    archive_folder = zoom_cfg.get("archive_folder", "csv_archive")

    archive_path = Path(archive_folder)
    if not archive_path.exists():
        await update.message.reply_text(
            "❌ No archived CSVs found. Archive folder doesn't exist yet."
        )
        return ConversationHandler.END

    csv_files = list(archive_path.glob("*.csv"))
    if not csv_files:
        await update.message.reply_text(
            "❌ No archived CSVs found in the archive folder."
        )
        return ConversationHandler.END

    # Build keyboard with file names
    kb = []
    for i, csv_file in enumerate(csv_files[:10]):  # Limit to 10 to avoid message size issues
        kb.append([InlineKeyboardButton(csv_file.name, callback_data=f"zoom|reprocess|{csv_file.name}")])

    if len(csv_files) > 10:
        kb.append([InlineKeyboardButton(f"... and {len(csv_files) - 10} more", callback_data="zoom|ignore")])

    await update.message.reply_text(
        "📹 *Zoom Attendance — Reprocess Archived CSV*\n\n"
        "Select a CSV file to reprocess:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.MARKDOWN,
    )
    return ZOOM_MEETING_ID


# ─── Meeting ID / Course input ─────────────────────────────────────────────────

@_guard("authorized", conv=True)
async def handle_zoom_meeting_id_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User entered meeting ID or course nickname."""
    text = update.message.text.strip()
    cfg = _load_cfg()
    zoom_cfg = cfg.get("attendance", {}).get("zoom_csv", {})
    mappings = zoom_cfg.get("meeting_id_mappings") or {}

    # Check if it's a course nickname
    if text.lower() in COURSES:
        course = text.lower()
        # Find a meeting ID for this course
        meeting_ids = [mid for mid, c in mappings.items() if c == course]
        if not meeting_ids:
            await update.message.reply_text(
                f"❌ No meeting ID mapped to course `{course}` yet.\n"
                f"Please enter the Zoom meeting ID directly.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return ZOOM_MEETING_ID
        # Use the most recent mapping
        meeting_id = meeting_ids[0]
        context.user_data["zoom_meeting_id"] = meeting_id
        context.user_data["zoom_course"] = course
        context.user_data["zoom_source_type"] = "api"
        return await _handle_instances_or_date(update, context, meeting_id, course)

    # Check if it's a numeric meeting ID
    if text.isdigit():
        meeting_id = text
        context.user_data["zoom_meeting_id"] = meeting_id

        # Look up course from mappings
        course = mappings.get(meeting_id)
        if course:
            context.user_data["zoom_course"] = course
            context.user_data["zoom_source_type"] = "api"
            return await _handle_instances_or_date(update, context, meeting_id, course)
        else:
            # Course not mapped - ask user
            kb = []
            for c in COURSES:
                kb.append([InlineKeyboardButton(c.upper(), callback_data=f"zoom|course|{c}")])
            await update.message.reply_text(
                f"📹 Meeting ID: `{meeting_id}`\n\n"
                f"Which course is this for?",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode=ParseMode.MARKDOWN,
            )
            return ZOOM_MEETING_ID

    await update.message.reply_text(
        "❌ Please enter a valid meeting ID (numbers only) or course nickname (e.g., `g10`).",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ZOOM_MEETING_ID


async def _handle_instances_or_date(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    meeting_id: str,
    course: str,
) -> int:
    """Check for multiple instances (API fetch) or ask for date (CSV upload)."""
    source_type = context.user_data.get("zoom_source_type", "api")

    if source_type == "csv":
        # CSV upload - skip date prompt (already extracted from filename)
        return await _handle_session_type(update, context)

    if source_type == "api":
        # List instances for recurring meetings
        try:
            instances = await _run_blocking(
                lambda: list_recent_meeting_instances(meeting_id, _load_cfg().get("attendance", {}).get("zoom_csv", {}).get("zoom_api", {}))
            )
            if len(instances) == 0:
                # No instances found — ask the user for the date so we can still proceed
                await update.effective_message.reply_text(
                    f"⚠️ No recent meeting instances found for `{meeting_id}`.\n\n"
                    f"Enter the meeting date manually (format: `YYYY-MM-DD`, e.g., `{datetime.now().strftime('%Y-%m-%d')}`):",
                    parse_mode=ParseMode.MARKDOWN,
                )
                context.user_data["zoom_source_type"] = "api"
                return ZOOM_TEACHER_TIME
            elif len(instances) >= 1:
                # Sort instances by start_time (most recent first) and use the most recent
                instances_sorted = sorted(instances, key=lambda x: x.get("start_time", ""), reverse=True)
                inst = instances_sorted[0]
                context.user_data["zoom_instance_uuid"] = inst.get("uuid")
                context.user_data["zoom_date"] = inst.get("start_time", "")

                # If there were multiple instances, inform the user which one was auto-selected
                if len(instances) > 1:
                    await update.effective_message.reply_text(
                        f"📹 Meeting ID: `{meeting_id}`\n\n"
                        f"Found {len(instances)} instances. Auto-selected the most recent:\n"
                        f"`{inst.get('start_time', 'Unknown')}`\n\n"
                        f"Proceeding to session type selection...",
                        parse_mode=ParseMode.MARKDOWN,
                    )

                return await _handle_session_type(update, context)
        except Exception as e:
            log.warning("zoom_instances_failed", extra={"error": str(e), "meeting_id": meeting_id})
            error_msg = str(e)
            # Don't end — fall back to asking for date
            await update.effective_message.reply_text(
                f"⚠️ Could not fetch meeting instances: {error_msg}\n\n"
                f"Enter the meeting date manually (format: `YYYY-MM-DD`, e.g., `{datetime.now().strftime('%Y-%m-%d')}`):",
                parse_mode=ParseMode.MARKDOWN,
            )
            context.user_data["zoom_source_type"] = "api"
            return ZOOM_TEACHER_TIME


# ─── Callback handlers ───────────────────────────────────────────────────────

@_guard("authorized", conv=True)
async def handle_zoom_course_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """zoom|course|<course> — user picked a course for unmapped meeting ID."""
    query = update.callback_query
    await query.answer()

    course = query.data.split("|", 2)[2]
    meeting_id = context.user_data["zoom_meeting_id"]

    context.user_data["zoom_course"] = course
    # Only set to "api" if not already set (e.g., by CSV upload flow)
    if "zoom_source_type" not in context.user_data:
        context.user_data["zoom_source_type"] = "api"

    return await _handle_instances_or_date(update, context, meeting_id, course)


@_guard("authorized", conv=True)
async def handle_zoom_instance_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """zoom|instance|<index> — user picked an instance from recurring meeting."""
    query = update.callback_query
    await query.answer()

    idx = int(query.data.split("|", 2)[2])
    instances = context.user_data.get("zoom_instances", [])
    if idx >= len(instances):
        await query.edit_message_text("❌ Invalid instance selection.")
        return ConversationHandler.END

    selected = instances[idx]
    context.user_data["zoom_instance_uuid"] = selected.get("uuid")
    context.user_data["zoom_date"] = selected.get("start_time", "")

    course = context.user_data["zoom_course"]
    await query.edit_message_text(
        f"📹 Instance selected: `{selected.get('start_time', 'Unknown')}`\n\n"
        f"Proceeding to session type selection...",
        parse_mode=ParseMode.MARKDOWN,
    )

    return await _handle_session_type(update, context)


@_guard("authorized", conv=True)
async def handle_zoom_reprocess_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """zoom|reprocess|<filename> — user picked an archived CSV to reprocess."""
    query = update.callback_query
    await query.answer()

    filename = query.data.split("|", 2)[2]
    cfg = _load_cfg()
    zoom_cfg = cfg.get("attendance", {}).get("zoom_csv", {})
    archive_folder = zoom_cfg.get("archive_folder", "csv_archive")

    csv_path = Path(archive_folder) / filename
    if not csv_path.exists():
        await query.edit_message_text(f"❌ File not found: {filename}")
        return ConversationHandler.END

    # Extract meeting ID and date from filename
    meeting_id = extract_meeting_id_from_filename(filename)
    date = extract_date_from_filename(filename)

    if not meeting_id or not date:
        await query.edit_message_text(
            f"❌ Filename doesn't match expected pattern: {filename}\n"
            f"Expected: participants_<meeting_id>_<date>.csv"
        )
        return ConversationHandler.END

    # Look up course from mappings
    mappings = zoom_cfg.get("meeting_id_mappings") or {}
    course = mappings.get(meeting_id)

    if not course:
        await query.edit_message_text(
            f"❌ Meeting ID `{meeting_id}` not mapped to a course.\n"
            f"Please map it in config or use /zoom_fetch instead."
        )
        return ConversationHandler.END

    context.user_data["zoom_meeting_id"] = meeting_id
    context.user_data["zoom_course"] = course
    context.user_data["zoom_source_type"] = "csv"
    context.user_data["zoom_csv_path"] = str(csv_path)
    context.user_data["zoom_date"] = date.isoformat()

    await query.edit_message_text(
        f"📹 Reprocessing: `{filename}`\n\n"
        f"Meeting ID: `{meeting_id}` · Course: `{course.upper()}` · Date: `{date.strftime('%Y-%m-%d')}`\n\n"
        f"Proceeding to session type selection...",
        parse_mode=ParseMode.MARKDOWN,
    )

    return await _handle_session_type(update, context)


# ─── Date / Teacher time input ─────────────────────────────────────────────────

@_guard("authorized", conv=True)
async def handle_zoom_teacher_time_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User entered meeting date (or teacher start time if teacher not found)."""
    text = update.message.text.strip()

    # Check if this is a time (manual teacher start time fallback)
    if ":" in text or text.upper().endswith(("AM", "PM")):
        context.user_data["zoom_manual_start_time"] = text
        return await _process_zoom_attendance(update, context)

    # This is a date
    try:
        date = datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid format. Please enter a date (`YYYY-MM-DD`, e.g., `2026-09-13`) "
            "or a start time (`HH:MM AM/PM`, e.g., `10:30 AM`).",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ZOOM_TEACHER_TIME

    context.user_data["zoom_date"] = date.isoformat()
    return await _handle_session_type(update, context)


async def _handle_session_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Auto-detect session type or prompt user if ambiguous."""
    course = context.user_data["zoom_course"]
    date_str = context.user_data.get("zoom_date")
    if date_str:
        try:
            date = datetime.fromisoformat(str(date_str).replace("Z", "+00:00"))
        except Exception:
            date = datetime.now()
    else:
        date = datetime.now()
    cfg = _load_cfg()

    session_type = determine_session_type(date, course, cfg)

    if session_type == "ambiguous":
        # Ask user to pick
        all_types = _course_type_list(cfg, course)
        online_types = [t for t in all_types if "online" in t.lower()]

        kb = []
        for i, t in enumerate(online_types):
            kb.append([InlineKeyboardButton(t, callback_data=f"zoom|type|{i}")])
        context.user_data["zoom_type_list"] = online_types

        await update.effective_message.reply_text(
            f"📹 Multiple Online session types match this date.\n\n"
            f"Please select the correct type:",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode=ParseMode.MARKDOWN,
        )
        return ZOOM_PICK_TYPE

    context.user_data["zoom_session_type"] = session_type

    # Ask for session number if not already set
    if "zoom_session_num" not in context.user_data:
        course = context.user_data["zoom_course"]
        cfg = _load_cfg()
        sheet_url = _get_sheet_url(course, cfg)
        sheet_line = f"\n📄 Sheet: [{sheet_url}]({sheet_url})" if sheet_url else ""

        await update.effective_message.reply_text(
            f"📹 Session type: `{session_type}`{sheet_line}\n\n"
            f"Enter the session number (e.g., `1` for Session 1):",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ZOOM_SESSION_NUM

    return await _process_zoom_attendance(update, context)


@_guard("authorized", conv=True)
async def handle_zoom_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """zoom|type|<index> — user picked a session type."""
    query = update.callback_query
    await query.answer()

    idx = int(query.data.split("|", 2)[2])
    type_list = context.user_data.get("zoom_type_list", [])
    if idx >= len(type_list):
        await query.edit_message_text("❌ Invalid type selection.")
        return ConversationHandler.END

    session_type = type_list[idx]
    context.user_data["zoom_session_type"] = session_type

    course = context.user_data["zoom_course"]
    cfg = _load_cfg()
    sheet_url = _get_sheet_url(course, cfg)
    sheet_line = f"\n📄 Sheet: [{sheet_url}]({sheet_url})" if sheet_url else ""

    await query.edit_message_text(
        f"📹 Session type: `{session_type}`{sheet_line}\n\n"
        f"Enter the session number (e.g., `1` for Session 1):",
        parse_mode=ParseMode.MARKDOWN,
    )

    return ZOOM_SESSION_NUM


# ─── CSV file upload handler ─────────────────────────────────────────────────

@_guard("authorized", conv=True)
async def handle_zoom_csv_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle CSV file upload from user."""
    document = update.message.document
    if not document or not document.file_name.endswith(".csv"):
        await update.message.reply_text("❌ Please upload a CSV file.")
        return ZOOM_MEETING_ID

    # Download to temp directory
    temp_dir = Path(tempfile.gettempdir())
    csv_path = temp_dir / document.file_name

    try:
        file = await document.get_file()
        await file.download_to_drive(csv_path)
    except Exception as e:
        log.error("csv_download_failed", extra={"error": str(e), "filename": document.file_name})
        await update.message.reply_text("❌ Failed to download the CSV file.")
        return ZOOM_MEETING_ID

    # Extract meeting ID and date from filename
    meeting_id = extract_meeting_id_from_filename(document.file_name)
    date = extract_date_from_filename(document.file_name)

    if not meeting_id or not date:
        await update.message.reply_text(
            f"❌ Filename doesn't match expected pattern: `{document.file_name}`\n\n"
            f"Expected: `participants_<meeting_id>_<date>.csv`\n"
            f"Example: `participants_81795786552_2026_09_13.csv`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ZOOM_MEETING_ID

    # Look up course from mappings
    cfg = _load_cfg()
    zoom_cfg = cfg.get("attendance", {}).get("zoom_csv", {})
    mappings = zoom_cfg.get("meeting_id_mappings") or {}
    course = mappings.get(meeting_id)

    if not course:
        # Ask user for course
        kb = []
        for c in COURSES:
            kb.append([InlineKeyboardButton(c.upper(), callback_data=f"zoom|course|{c}")])
        await update.message.reply_text(
            f"📹 Meeting ID: `{meeting_id}` · Date: `{date.strftime('%Y-%m-%d')}`\n\n"
            f"Which course is this for?",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode=ParseMode.MARKDOWN,
        )
        context.user_data["zoom_csv_path"] = str(csv_path)
        context.user_data["zoom_meeting_id"] = meeting_id
        context.user_data["zoom_date"] = date.isoformat()
        context.user_data["zoom_source_type"] = "csv"
        return ZOOM_MEETING_ID

    context.user_data["zoom_meeting_id"] = meeting_id
    context.user_data["zoom_course"] = course
    context.user_data["zoom_csv_path"] = str(csv_path)
    context.user_data["zoom_date"] = date.isoformat()
    context.user_data["zoom_source_type"] = "csv"

    return await _handle_instances_or_date(update, context, meeting_id, course)


# ─── Attendance processing ────────────────────────────────────────────────────

async def _process_zoom_attendance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Run Zoom attendance pipeline with dry-run first, then confirm."""
    try:
        course = context.user_data["zoom_course"]
        source_type = context.user_data.get("zoom_source_type", "api")

        # Get session number from user_data or default to 1
        session_num = context.user_data.get("zoom_session_num", 1)

        session_type = context.user_data.get("zoom_session_type")

        # Build source
        if source_type == "csv":
            source = context.user_data["zoom_csv_path"]
        else:
            source = {
                "meeting_id": context.user_data["zoom_meeting_id"],
                "instance_uuid": context.user_data.get("zoom_instance_uuid"),
                "date": context.user_data.get("zoom_date"),
            }

        # Run dry-run
        status_msg = await update.effective_message.reply_text("🔍 Processing attendance (dry-run)...")

        manual_start = context.user_data.get("zoom_manual_start_time")

        def run_dry():
            return run_zoom(
                source=source,
                course=course,
                session_num=session_num,
                session_type=session_type,
                dry_run=True,
                config_path="config/attendance.yaml",
                review_store_dir="review_queue",
                manual_start_time=manual_start,
            )

        summary = await _run_blocking(run_dry)

        if summary.teacher_not_found:
            await status_msg.edit_text(
                "⚠️ Teacher not found in participant list.\n\n"
                "Please enter the meeting start time (format: `HH:MM AM/PM`, e.g., `10:30 AM`):",
                parse_mode=ParseMode.MARKDOWN,
            )
            return ZOOM_TEACHER_TIME

        if summary.session_type_ambiguous:
            # This should have been handled earlier, but just in case
            await status_msg.edit_text("⚠️ Session type ambiguous. Please try again.")
            return ConversationHandler.END

        if summary.errors:
            error_text = "\n".join(summary.errors[:3])
            # Check if it's a "report not ready" error and suggest CSV upload
            if "not ready" in error_text.lower():
                await status_msg.edit_text(
                    f"❌ Errors occurred:\n\n{error_text}\n\n"
                    f"💡 *Tip:* If the report isn't ready yet, you can use `/zoom_upload` to upload the CSV file manually from Zoom instead.",
                    parse_mode=ParseMode.MARKDOWN,
                )
            else:
                await status_msg.edit_text(
                    f"❌ Errors occurred:\n\n{error_text}",
                    parse_mode=ParseMode.MARKDOWN,
                )
            return ConversationHandler.END

        # Show dry-run preview
        preview_text = summary.text()
        await status_msg.edit_text(
            f"{preview_text}\n\n"
            f"🧪 *This is a DRY-RUN preview.*\n\n"
            f"Reply `CONFIRM` to write to the sheet, or `CANCEL` to discard.",
            parse_mode=ParseMode.MARKDOWN,
        )

        context.user_data["zoom_source"] = source
        context.user_data["zoom_summary"] = summary
        return ZOOM_CONFIRM

    except _StaleSession:
        await update.effective_message.reply_text("⚠️ Session expired. Please start over with /zoom_fetch or /zoom_upload.")
        return ConversationHandler.END
    except Exception as e:
        log.error("zoom_process_error", extra={"error": str(e)}, exc_info=True)
        await update.effective_message.reply_text(_user_error_message(e, "Zoom attendance"))
        return ConversationHandler.END


@_guard("authorized", conv=True)
async def handle_zoom_session_num_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User entered session number."""
    text = update.message.text.strip()

    try:
        session_num = int(text)
        if session_num < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "❌ Please enter a valid session number (e.g., `1`, `2`, `3`...).",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ZOOM_SESSION_NUM

    context.user_data["zoom_session_num"] = session_num
    return await _process_zoom_attendance(update, context)


@_guard("authorized", conv=True)
async def handle_zoom_confirm_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User confirmed or cancelled the dry-run preview."""
    text = update.message.text.strip().upper()

    if text == "CANCEL":
        await update.effective_message.reply_text("❌ Cancelled. No changes were made.")
        context.user_data.clear()
        return ConversationHandler.END

    if text != "CONFIRM":
        await update.effective_message.reply_text(
            "❌ Please reply `CONFIRM` to write, or `CANCEL` to discard.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ZOOM_CONFIRM

    # Run actual write
    try:
        source = context.user_data["zoom_source"]
        course = context.user_data["zoom_course"]
        session_num = context.user_data.get("zoom_session_num", 1)
        session_type = context.user_data.get("zoom_session_type")
        summary = context.user_data["zoom_summary"]

        status_msg = await update.effective_message.reply_text("✍️ Writing to sheet...")

        manual_start = context.user_data.get("zoom_manual_start_time")

        def run_write():
            return run_zoom(
                source=source,
                course=course,
                session_num=session_num,
                session_type=session_type,
                dry_run=False,
                config_path="config/attendance.yaml",
                review_store_dir="review_queue",
                manual_start_time=manual_start,
            )

        final_summary = await _run_blocking(run_write)

        # Archive CSV if it was a local file
        if isinstance(source, str):
            cfg = _load_cfg()
            zoom_cfg = cfg.get("attendance", {}).get("zoom_csv", {})
            try:
                archive_csv(source, zoom_cfg.get("archive_folder", "csv_archive"))
            except Exception as e:
                log.warning("csv_archive_failed", extra={"error": str(e)})

        if status_msg is not None and hasattr(status_msg, "edit_text"):
            await status_msg.edit_text(final_summary.text(), parse_mode=ParseMode.MARKDOWN)
        else:
            await update.effective_message.reply_text(final_summary.text(), parse_mode=ParseMode.MARKDOWN)
        context.user_data.clear()
        return ConversationHandler.END

    except Exception as e:
        log.error("zoom_write_error", extra={"error": str(e)}, exc_info=True)
        await update.effective_message.reply_text(_user_error_message(e, "Zoom attendance write"))
        return ConversationHandler.END
