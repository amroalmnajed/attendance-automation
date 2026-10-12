"""attendance/bot/admin.py — admin/teacher commands: report, streaks, revert, absent, pending, aliases, health."""
from __future__ import annotations

import asyncio
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from attendance import absent as absent_mod, alias_admin, health as health_mod
from attendance.review_queue import ReviewQueue
from attendance.writer import WriteCell, batch_write

from attendance.bot.cards import escape_markdown
from attendance.bot.common import (
    _guard,
    _load_cfg,
    _notify_failure,
    _run_blocking,
    _user_error_message,
    log,
)


# ─── Administrative commands ──────────────────────────────────────────────────

@_guard("authorized")
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
        loop = asyncio.get_running_loop()
        rep = await loop.run_in_executor(None, generate_session_report, course, session, cfg, review_dir)

        msg  = f"📊 *Report: {course.upper()} S{session}*\n"
        msg += f"Present: {rep['present']} / {rep['total_students']} ({rep['attendance_rate']:.1f}%)\n"
        msg += f"Review Backlog: {rep['review_backlog']} items\n\n*Absentees:*\n"
        msg += "\n".join(f"• {escape_markdown(a)}" for a in rep["absentees"]) if rep["absentees"] else "• None"
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as exc:
        await update.message.reply_text(_user_error_message(exc, "report"))


@_guard("authorized")
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
        loop = asyncio.get_running_loop()
        streaks = await loop.run_in_executor(None, generate_streaks_report, course, consec, cfg)

        msg = f"📉 *Chronic Absentees ({course.upper()} - past {consec} sessions)*\n"
        msg += "\n".join(f"• Row {s['row']}: {escape_markdown(s['name'])}" for s in streaks) if streaks else "• None found"
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as exc:
        await update.message.reply_text(_user_error_message(exc, "streaks report"))


@_guard("authorized")
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

        loop = asyncio.get_running_loop()

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
    except Exception as exc:
        await update.message.reply_text(_user_error_message(exc, "revert"))


@_guard("authorized")
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

        loop = asyncio.get_running_loop()

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
    except Exception as exc:
        await query.edit_message_text(_user_error_message(exc, "revert"))


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


@_guard("authorized")
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


@_guard("authorized")
async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg        = _load_cfg()
    review_dir = Path(cfg["attendance"]["review"].get("local_dir", "review_queue"))

    queue           = ReviewQueue(store_dir=str(review_dir))
    pending_uploads = [
        (uid, len(queue.list_pending(uid)))
        for uid in queue._store.list_all_upload_ids()
        if queue.list_pending(uid)
    ]

    if not pending_uploads:
        await update.message.reply_text("No pending items.")
        return

    msg = "*Pending Review Queues:*\n"
    for uid, count in pending_uploads:
        msg += f"• `{uid}` : {count} items\n"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


@_guard("authorized")
async def cmd_aliases(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manage learned name aliases: /aliases [list [course] | flagged | remove <course> <paper name>]."""
    try:
        cfg = _load_cfg()
        ids = alias_admin.admin_ids(cfg)
        user = getattr(update, "effective_user", None)
        uid = getattr(user, "id", None)
        if not ids:
            text = ("🔒 Alias management is unavailable: no authorized user is configured yet. "
                    "Set AUTHORIZED_TELEGRAM_IDS (or attendance.authorized.telegram_ids) and restart the bot.")
        elif uid not in ids:
            text = "🔒 This command is for authorized users only."
        else:
            text = alias_admin.handle(list(getattr(context, "args", None) or []), cfg)
    except Exception as exc:   # never log names; only the error type/message
        log.error("aliases_cmd_failed", extra={"error": str(exc)})
        text = "❌ Couldn't manage aliases right now. Please try again or use the command line."
    await update.message.reply_text(text[:3900])


@_guard("authorized")
async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Plain-language status (Gemini / Sheets failures, review age, last alerts, reminders)."""
    try:
        cfg = _load_cfg()
        jq = getattr(getattr(context, "application", None), "job_queue", None)
        text = health_mod.status_text(cfg, scheduler=jq is not None)
    except Exception as exc:
        log.error("health_cmd_failed", extra={"error": type(exc).__name__})
        text = "❌ Couldn't read the system status right now. Please try again."
    await update.message.reply_text(text[:3900])
