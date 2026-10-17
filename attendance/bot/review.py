"""attendance/bot/review.py — summary delivery, review cards, resolution and roster-search handlers."""
from __future__ import annotations

import asyncio
import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from attendance.normalizer import canonical_mark
from attendance.review_queue import ReviewQueue
from attendance.resolver_ops import resolve_item
from attendance.session_resolver import resolve

from attendance.bot.cards import escape_markdown, format_review_card
from attendance.bot.crop import _crop_image
from attendance.bot.common import SEARCH_ROSTER, _guard, _load_cfg, _notify_failure, _user_error_message, log


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

# ─── Review queue sender ──────────────────────────────────────────────────────

async def _send_next_review_item(chat_id: int, bot, upload_id: str, review_dir: str) -> None:
    loop = asyncio.get_running_loop()

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
    crop_path = _crop_image(img_path, bbox) if img_path and os.path.exists(img_path) else img_path

    caption, rows = format_review_card(item, upload_id=upload_id, position=(current_idx, total))
    keyboard = [[InlineKeyboardButton(text, callback_data=data) for text, data in row] for row in rows]

    try:
        try:
            if os.path.exists(crop_path):
                loop = asyncio.get_running_loop()
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
    finally:
        if crop_path and crop_path != img_path:      # only the temp crop; never the stored review image
            try:
                os.remove(crop_path)
            except OSError:
                pass


# ─── Resolution callback ──────────────────────────────────────────────────────

@_guard("authorized")
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

    loop = asyncio.get_running_loop()

    def _find_upload():
        queue    = ReviewQueue(store_dir=review_dir)
        full_uid = queue.find_by_prefix(short_upload_id)
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

    def _do_resolve():
        from attendance.aliases import AliasStore
        from attendance import writer

        match_cfg = cfg.get("attendance", {}).get("match", {})
        alias_store = AliasStore(
            path=match_cfg.get("aliases_file", "config/learned_aliases.json"),
            min_confirmations=int(match_cfg.get("alias_min_confirmations", 1)),
        )

        return resolve_item(
            upload_id=full_upload_id,
            item_id=target_item["item_id"],
            chosen_row=roster_row,
            queue=queue,
            writer=writer,
            alias_store=alias_store,
            cfg=cfg,
            dry_run=cfg["core"]["app"].get("dry_run_default", True),
            attendance_value=attendance_value,
            roster_name=roster_name,
            resolved_by="telegram",
        )

    try:
        res = await loop.run_in_executor(None, _do_resolve)
        if not res.success:
            raise RuntimeError(res.error or "Resolution failed")
        await query.message.delete()
        await _send_next_review_item(query.message.chat_id, context.bot, full_upload_id, review_dir)
    except Exception as exc:
        try:
            await query.edit_message_caption(caption=_user_error_message(exc, "sheet write"))
        except Exception:
            await query.edit_message_text(text=_user_error_message(exc, "sheet write"))


# ─── Search flow ──────────────────────────────────────────────────────────────

@_guard("authorized", conv=True)
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


@_guard("authorized", conv=True)
async def search_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    search_term    = update.message.text.lower()
    short_upload_id = context.user_data.get("search_upload_id")
    item_id        = context.user_data.get("search_item_id")

    if not short_upload_id or not item_id:
        await update.message.reply_text("Search context lost. Please try again.")
        return ConversationHandler.END

    cfg        = _load_cfg()
    review_dir = cfg["attendance"]["review"].get("local_dir", "review_queue")

    loop = asyncio.get_running_loop()

    def _find_upload():
        queue    = ReviewQueue(store_dir=review_dir)
        full_uid = queue.find_by_prefix(short_upload_id)
        if not full_uid:
            return None, None, None
        return full_uid, queue, queue.list_pending(full_uid)

    try:
        full_upload_id, queue, pending = await loop.run_in_executor(None, _find_upload)

        if not full_upload_id:
            await update.message.reply_text("❌ Queue file not found.")
            return ConversationHandler.END

        target_item = next((i for i in pending if i["item_id"].startswith(item_id)), None)
        if not target_item:
            await update.message.reply_text("✅ Item already resolved.")
            return ConversationHandler.END

        course       = target_item["course"]
        # Blocking Sheets I/O: keep it off the event loop (see HANDOFF.md #16).
        session_info = await asyncio.get_running_loop().run_in_executor(
            None, lambda: resolve(course, target_item["session"], target_item.get("session_type"), cfg))

        matches = [r for r in session_info.roster if search_term in r[0].lower()][:5]

        if not matches:
            await update.message.reply_text("No students found matching that name. Try again or /cancel.")
            return SEARCH_ROSTER

        keyboard = []
        for roster_name, roster_row in matches:
            cb_data = f"res|{short_upload_id}|{item_id}|{roster_row}"
            keyboard.append([InlineKeyboardButton(escape_markdown(roster_name), callback_data=cb_data)])

        await update.message.reply_text("Found these students:", reply_markup=InlineKeyboardMarkup(keyboard))
        return ConversationHandler.END
    except Exception as exc:
        await update.message.reply_text(_user_error_message(exc, "roster search"))
        return ConversationHandler.END
