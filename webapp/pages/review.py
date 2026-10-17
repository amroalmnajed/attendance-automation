"""webapp/pages/review.py — Review queue screen (W5).

Reads the real SQLite queue via attendance.review_queue.ReviewQueue,
renders cards using format_review_card, displays crops, and resolves items
using resolver_ops.resolve_item.
"""
from __future__ import annotations

import os

import streamlit as st
import yaml

from attendance import writer
from attendance.bot.cards import format_review_card
from attendance.bot.crop import _crop_image
from attendance.review_queue import ReviewQueue
from attendance.resolver_ops import resolve_item
from attendance.session_resolver import resolve as session_resolve
from webapp.config import CONFIG_PATH


def _load_config() -> dict:
    """Load the attendance config."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def _page_review() -> None:
    """Render the review queue screen."""
    st.header("🔍 Review Queue")

    # Load config
    cfg = _load_config()
    review_dir = cfg.get("attendance", {}).get("review", {}).get("local_dir", "review_queue")

    # Initialize queue
    queue = ReviewQueue(store_dir=review_dir)

    # Filter by upload_id if available from session state
    upload_id_filter = st.session_state.get("last_upload_id")
    if upload_id_filter:
        st.info(f"Showing items for upload ID: `{upload_id_filter}`")
        if st.button("Show all pending items"):
            st.session_state.pop("last_upload_id", None)
            st.rerun()
    else:
        st.info("Showing all pending review items")

    # List pending items
    pending_items = queue.list_pending(upload_id=upload_id_filter)

    if not pending_items:
        st.success("🎉 No pending review items!")
        return

    st.subheader(f"{len(pending_items)} pending item(s)")

    # Render each item
    for idx, item in enumerate(pending_items, start=1):
        item_upload_id = item.get("upload_id", "")
        item_id = item.get("item_id", "")

        with st.expander(f"Item {idx}: {item.get('raw_name', '?')} (ID: {item_id[:8]}...)", expanded=(idx == 1)):
            _render_review_item(item, item_upload_id, item_id, queue, cfg)


def _render_review_item(item: dict, upload_id: str, item_id: str, queue: ReviewQueue, cfg: dict) -> None:
    """Render a single review item with card, crop, and resolution buttons."""
    # Format the card (caption and button rows)
    total_items = len(queue.list_pending(upload_id))
    current_idx = total_items - len([i for i in queue.list_pending(upload_id) if i.get("item_id") > item_id])
    caption, button_rows = format_review_card(item, upload_id=upload_id, position=(current_idx, total_items))

    # Display caption as markdown
    st.markdown(caption)

    # Display crop image if available
    img_path = item.get("image_ref", "")
    bbox = item.get("bounding_box")
    if img_path and os.path.exists(img_path):
        crop_path = _crop_image(img_path, bbox)
        try:
            st.image(crop_path, caption="Flagged row (cropped)", use_container_width=True)
        finally:
            # Clean up temp crop file
            if crop_path and crop_path != img_path:
                try:
                    os.remove(crop_path)
                except OSError:
                    pass
    else:
        st.info("No image available for this item")

    # Resolution buttons
    st.subheader("Resolve this item")

    # Get user name from session state for audit trail
    resolved_by = st.session_state.get("user_name", "webapp-user")

    # Dry-run toggle for resolution
    dry_run = st.toggle("Dry run (preview only)", value=True, key=f"dry_run_{item_id}")

    # Render candidate buttons from format_review_card
    for row in button_rows:
        cols = st.columns(len(row))
        for col_idx, (text, callback_data) in enumerate(row):
            with cols[col_idx]:
                # Parse callback_data: "res|upload_id|item_id|roster_row" or "srch|upload_id|item_id"
                parts = callback_data.split("|")
                if len(parts) >= 4 and parts[0] == "res":
                    _, _, _, roster_row_str = parts[:4]
                    if roster_row_str == "absent":
                        if st.button("❌ Absent", key=f"absent_{item_id}"):
                            _resolve_item_action(item, upload_id, item_id, queue, cfg, dry_run, resolved_by, chosen_row=0, attendance_value="Absent")
                    elif roster_row_str == "0":
                        if st.button("⏭️ Skip / Ignore", key=f"skip_{item_id}"):
                            _resolve_item_action(item, upload_id, item_id, queue, cfg, dry_run, resolved_by, chosen_row=0, attendance_value="Absent")
                    else:
                        # Candidate match
                        try:
                            roster_row = int(roster_row_str)
                            roster_name = text.split("(")[0].strip().replace("🟢 ", "").replace("🟡 ", "").replace("🔴 ", "")
                            if st.button(text, key=f"candidate_{item_id}_{roster_row}"):
                                # Derive attendance value from context (default to Present)
                                _resolve_item_action(item, upload_id, item_id, queue, cfg, dry_run, resolved_by, chosen_row=roster_row, attendance_value="Present")
                        except ValueError:
                            pass
                elif len(parts) >= 3 and parts[0] == "srch":
                    if st.button("🔍 Search roster", key=f"search_{item_id}"):
                        st.session_state["search_upload_id"] = upload_id
                        st.session_state["search_item_id"] = item_id
                        st.rerun()

    # Search roster (if triggered)
    if st.session_state.get("search_upload_id") == upload_id and st.session_state.get("search_item_id") == item_id:
        st.subheader("🔍 Search Roster")
        search_term = st.text_input("Type a few letters of the student's name", key=f"search_{item_id}")

        if search_term:
            # Get session info to access roster
            course = item.get("course")
            session_num = item.get("session")
            session_type = item.get("session_type")

            try:
                session_info = session_resolve(
                    course_key=course,
                    session_num=session_num,
                    session_type=session_type,
                    cfg=cfg,
                )

                # Search roster
                matches = [r for r in session_info.roster if search_term.lower() in r[0].lower()][:5]

                if matches:
                    for roster_name, roster_row in matches:
                        if st.button(roster_name, key=f"search_result_{item_id}_{roster_row}"):
                            _resolve_item_action(item, upload_id, item_id, queue, cfg, dry_run, resolved_by, chosen_row=roster_row, attendance_value="Present")
                else:
                    st.warning("No students found matching that name.")
            except Exception as e:
                st.error(f"Failed to load roster: {str(e)}")

        if st.button("Cancel search", key=f"cancel_search_{item_id}"):
            st.session_state.pop("search_upload_id", None)
            st.session_state.pop("search_item_id", None)
            st.rerun()


def _resolve_item_action(
    item: dict,
    upload_id: str,
    item_id: str,
    queue: ReviewQueue,
    cfg: dict,
    dry_run: bool,
    resolved_by: str,
    chosen_row: int,
    attendance_value: str,
) -> None:
    """Execute the resolution using resolver_ops.resolve_item."""
    with st.spinner("Resolving…"):
        try:
            result = resolve_item(
                upload_id=upload_id,
                item_id=item_id,
                chosen_row=chosen_row,
                queue=queue,
                writer=writer,
                cfg=cfg,
                dry_run=dry_run,
                attendance_value=attendance_value,
                resolved_by=resolved_by,
            )

            if result.success:
                if dry_run:
                    st.success("✅ Resolution previewed (dry run)")
                else:
                    if result.written:
                        st.success("✅ Resolved and written to sheet")
                    else:
                        st.info("✅ Resolved (no sheet write needed)")
                    if result.alias_learned:
                        st.info("📚 Alias learned")
                st.rerun()
            else:
                st.error(f"❌ Resolution failed: {result.error}")
        except Exception as e:
            st.error(f"❌ Resolution error: {str(e)}")
