"""webapp/pages/review.py — Review queue screen (W5).

Reads the real SQLite queue via attendance.review_queue.ReviewQueue,
renders cards using format_review_card, displays crops, and resolves items
using resolver_ops.resolve_item.
"""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from io import StringIO

import streamlit as st
import yaml

from attendance import writer
from attendance.db import get_session, Course
from attendance.review_cards import format_review_card
from attendance.review_crop import _crop_image
from attendance.review_queue import ReviewQueue
from attendance.resolver_ops import resolve_item
from attendance.session_resolver import resolve as session_resolve
from attendance.session_patterns import init_pattern_engine
from attendance.suggestions import init_suggestion_engine
from core.logger import get_logger
from core.paths import data_path
from webapp.config import CONFIG_PATH

log = get_logger("webapp_review")


def _load_config() -> dict:
    """Load the attendance config."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def _get_course_id(course_key: str) -> Optional[int]:
    """Look up course_id from course_key using the database.
    
    Args:
        course_key: Course key string (e.g., "crash", "main")
    
    Returns:
        Course ID (integer) or None if not found
    """
    try:
        session = get_session()
        try:
            course = session.query(Course).filter(Course.course_key == course_key).first()
            return course.course_id if course else None
        finally:
            session.close()
    except Exception:
        return None


def _export_selected_items(selected_items: list[dict], format: str = "csv") -> str:
    """Export selected review items to CSV or JSON format.
    
    Args:
        selected_items: List of review item dictionaries
        format: Either 'csv' or 'json'
    
    Returns:
        String content of the exported data
    """
    if format == "json":
        # Export as JSON with resolution fields pre-populated
        export_data = []
        for item in selected_items:
            export_data.append({
                "item_id": item.get("item_id"),
                "upload_id": item.get("upload_id"),
                "raw_name": item.get("raw_name"),
                "course": item.get("course"),
                "session": item.get("session"),
                "session_type": item.get("session_type"),
                "paper_row_index": item.get("paper_row_index"),
                "ocr_confidence": item.get("ocr_confidence"),
                "resolved_name": "",  # To be filled by user
                "roster_row": "",  # To be filled by user (0 for absent)
                "attendance_value": "Present",  # Default
            })
        return json.dumps(export_data, indent=2, ensure_ascii=False)
    
    elif format == "csv":
        # Export as CSV
        output = StringIO()
        fieldnames = [
            "item_id", "upload_id", "raw_name", "course", "session",
            "session_type", "paper_row_index", "ocr_confidence",
            "resolved_name", "roster_row", "attendance_value"
        ]
        writer_csv = csv.DictWriter(output, fieldnames=fieldnames)
        writer_csv.writeheader()
        
        for item in selected_items:
            writer_csv.writerow({
                "item_id": item.get("item_id"),
                "upload_id": item.get("upload_id"),
                "raw_name": item.get("raw_name"),
                "course": item.get("course"),
                "session": item.get("session"),
                "session_type": item.get("session_type"),
                "paper_row_index": item.get("paper_row_index"),
                "ocr_confidence": item.get("ocr_confidence"),
                "resolved_name": "",  # To be filled by user
                "roster_row": "",  # To be filled by user (0 for absent)
                "attendance_value": "Present",  # Default
            })
        
        return output.getvalue()
    
    else:
        raise ValueError(f"Unsupported export format: {format}")


def _import_resolutions(import_data: str, format: str = "csv") -> list[dict]:
    """Parse imported resolution data from CSV or JSON.
    
    Args:
        import_data: String content of the imported data
        format: Either 'csv' or 'json'
    
    Returns:
        List of resolution dictionaries with keys: item_id, resolved_name, roster_row, attendance_value
    """
    if format == "json":
        data = json.loads(import_data)
        resolutions = []
        for row in data:
            resolutions.append({
                "item_id": row.get("item_id"),
                "resolved_name": row.get("resolved_name", ""),
                "roster_row": int(row.get("roster_row", 0)) if row.get("roster_row") else 0,
                "attendance_value": row.get("attendance_value", "Present"),
            })
        return resolutions
    
    elif format == "csv":
        resolutions = []
        reader = csv.DictReader(StringIO(import_data))
        for row in reader:
            resolutions.append({
                "item_id": row.get("item_id"),
                "resolved_name": row.get("resolved_name", ""),
                "roster_row": int(row.get("roster_row", 0)) if row.get("roster_row") else 0,
                "attendance_value": row.get("attendance_value", "Present"),
            })
        return resolutions
    
    else:
        raise ValueError(f"Unsupported import format: {format}")


def _page_review() -> None:
    """Render the review queue screen."""
    st.header("🔍 Review Queue")

    # Keyboard shortcuts help
    with st.expander("⌨️ Keyboard Shortcuts"):
        st.markdown("""
        **Navigation:**
        - Use the expand/collapse arrows to navigate between items
        - Click buttons to resolve items quickly

        **Tips:**
        - Use bulk actions to resolve multiple items at once
        - Use inline editing to correct OCR text or matched names
        - Use dry-run mode to preview changes before writing
        """)

    # Load config
    cfg = _load_config()
    review_dir = str(data_path(cfg.get("attendance", {}).get("review", {}).get("local_dir", "review_queue")))

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

    # Global dry-run toggle (default False so users don't have to turn it off on every card)
    dry_run = st.toggle(
        "Dry run (preview only - do not write to Google Sheet)",
        value=st.session_state.get("review_global_dry_run", False),
        key="review_global_dry_run",
        help="When OFF (default), resolving an item writes directly to the Google Sheet. When ON, changes are previewed only.",
    )

    # Bulk actions section
    with st.expander("🔧 Bulk Actions"):
        st.write("Select multiple items and perform bulk operations")

        # Bulk select all / deselect all
        col_select1, col_select2 = st.columns(2)
        with col_select1:
            if st.button("Select all visible items", key="bulk_select_all"):
                for item in pending_items:
                    item_id = item.get("item_id", "")
                    st.session_state[f"bulk_select_{item_id}"] = True
                st.rerun()
        with col_select2:
            if st.button("Deselect all", key="bulk_deselect_all"):
                for item in pending_items:
                    item_id = item.get("item_id", "")
                    st.session_state.pop(f"bulk_select_{item_id}", None)
                st.rerun()

        # Bulk resolve options
        st.subheader("Bulk Resolve")
        bulk_roster_row = st.number_input("Roster row (0 for absent)", min_value=0, value=0, key="bulk_roster_row")
        bulk_attendance = st.selectbox("Attendance value", ["Present", "Absent", "Late", "Excused"], key="bulk_attendance")

        if st.button("Apply bulk resolution", key="bulk_resolve"):
            selected_count = 0
            for item in pending_items:
                item_id = item.get("item_id", "")
                item_upload_id = item.get("upload_id", "")
                if st.session_state.get(f"bulk_select_{item_id}", False):
                    _resolve_item_action(
                        item,
                        item_upload_id,
                        item_id,
                        queue,
                        cfg,
                        dry_run,
                        resolved_by=st.session_state.get("user_name", "webapp-user"),
                        chosen_row=bulk_roster_row,
                        attendance_value=bulk_attendance,
                    )
                    selected_count += 1
            if selected_count > 0:
                st.success(f"✅ Bulk resolved {selected_count} items")
                st.rerun()
            else:
                st.warning("No items selected")

        # Bulk mark absent
        if st.button("Bulk mark all selected as Absent", key="bulk_absent"):
            selected_count = 0
            for item in pending_items:
                item_id = item.get("item_id", "")
                item_upload_id = item.get("upload_id", "")
                if st.session_state.get(f"bulk_select_{item_id}", False):
                    _resolve_item_action(
                        item,
                        item_upload_id,
                        item_id,
                        queue,
                        cfg,
                        dry_run,
                        resolved_by=st.session_state.get("user_name", "webapp-user"),
                        chosen_row=0,
                        attendance_value="Absent",
                    )
                    selected_count += 1
            if selected_count > 0:
                st.success(f"✅ Bulk marked {selected_count} items as Absent")
                st.rerun()
            else:
                st.warning("No items selected")

        # Bulk apply session type
        session_type_input = st.text_input("Session type to apply", key="bulk_session_type_input")
        if st.button("Bulk apply session type", key="bulk_session_type"):
            selected_count = 0
            for item in pending_items:
                item_id = item.get("item_id", "")
                if st.session_state.get(f"bulk_select_{item_id}", False):
                    # Update session type in the item
                    item["session_type"] = session_type_input
                    # Store for later resolution
                    st.session_state[f"session_type_{item_id}"] = session_type_input
                    selected_count += 1
            if selected_count > 0:
                st.success(f"✅ Applied session type to {selected_count} items")
                st.rerun()
            else:
                st.warning("No items selected")

        # Bulk export/import
        st.subheader("Bulk Export / Import")
        
        # Export section
        col_export1, col_export2 = st.columns(2)
        with col_export1:
            export_format = st.selectbox("Export format", ["csv", "json"], key="bulk_export_format")
        with col_export2:
            if st.button("Export selected items", key="bulk_export"):
                selected_items = []
                for item in pending_items:
                    item_id = item.get("item_id", "")
                    if st.session_state.get(f"bulk_select_{item_id}", False):
                        selected_items.append(item)
                
                if selected_items:
                    try:
                        export_content = _export_selected_items(selected_items, format=export_format)
                        st.download_button(
                            label=f"Download {export_format.upper()} file",
                            data=export_content,
                            file_name=f"review_export_{upload_id_filter or 'all'}.{export_format}",
                            mime="text/csv" if export_format == "csv" else "application/json",
                            key=f"download_export_{export_format}"
                        )
                        st.success(f"✅ Prepared export for {len(selected_items)} items")
                    except Exception as e:
                        st.error(f"❌ Export failed: {str(e)}")
                else:
                    st.warning("No items selected for export")
        
        # Import section
        st.markdown("**Import resolutions**")
        import_file = st.file_uploader(
            "Upload resolution file (CSV or JSON)",
            type=["csv", "json"],
            key="bulk_import_file"
        )
        
        if import_file:
            try:
                import_format = "json" if import_file.name.endswith(".json") else "csv"
                import_data = import_file.read().decode("utf-8")
                resolutions = _import_resolutions(import_data, format=import_format)
                
                st.info(f"Loaded {len(resolutions)} resolution(s) from file")
                
                # Preview first few resolutions
                with st.expander("Preview imported resolutions"):
                    preview_data = resolutions[:5]
                    for i, res in enumerate(preview_data, 1):
                        st.text(f"{i}. Item ID: {res['item_id'][:8]}... → {res['resolved_name'] or 'Absent'} (row {res['roster_row']}, {res['attendance_value']})")
                    if len(resolutions) > 5:
                        st.text(f"... and {len(resolutions) - 5} more")
                
                if st.button("Apply imported resolutions", key="bulk_import_apply"):
                    applied_count = 0
                    failed_count = 0
                    
                    for resolution in resolutions:
                        item_id = resolution["item_id"]
                        # Find the item in pending_items
                        item = next((i for i in pending_items if i.get("item_id") == item_id), None)
                        
                        if item:
                            try:
                                resolved_name = resolution["resolved_name"]
                                roster_row = resolution["roster_row"]
                                attendance_value = resolution["attendance_value"]
                                
                                # If a resolved name is provided, look up the roster row
                                if resolved_name and roster_row == 0:
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
                                        match = next((r for r in session_info.roster if r[0] == resolved_name), None)
                                        if match:
                                            roster_row = match[1]
                                    except Exception:
                                        pass  # Fall back to roster_row=0 (absent)
                                
                                _resolve_item_action(
                                    item,
                                    item.get("upload_id", ""),
                                    item_id,
                                    queue,
                                    cfg,
                                    dry_run,
                                    resolved_by=st.session_state.get("user_name", "webapp-user"),
                                    chosen_row=roster_row,
                                    attendance_value=attendance_value,
                                )
                                applied_count += 1
                            except Exception as e:
                                st.error(f"Failed to apply resolution for item {item_id[:8]}: {str(e)}")
                                failed_count += 1
                        else:
                            failed_count += 1
                    
                    if applied_count > 0:
                        st.success(f"✅ Applied {applied_count} resolution(s)")
                        if failed_count > 0:
                            st.warning(f"⚠️ Failed to apply {failed_count} resolution(s)")
                        st.rerun()
                    else:
                        st.error("❌ No resolutions were applied")
                        
            except Exception as e:
                st.error(f"❌ Import failed: {str(e)}")

    # Render each item
    for idx, item in enumerate(pending_items, start=1):
        item_upload_id = item.get("upload_id", "")
        item_id = item.get("item_id", "")

        # Bulk selection checkbox
        selected = st.checkbox(
            f"Select item {idx}",
            value=st.session_state.get(f"bulk_select_{item_id}", False),
            key=f"bulk_select_{item_id}",
        )

        with st.expander(f"Item {idx}: {item.get('raw_name', '?')} (ID: {item_id[:8]}...)", expanded=(idx == 1)):
            _render_review_item(item, item_upload_id, item_id, queue, cfg, dry_run=dry_run)


def _render_review_item(
    item: dict,
    upload_id: str,
    item_id: str,
    queue: ReviewQueue,
    cfg: dict,
    dry_run: bool = False,
) -> None:
    """Render a single review item with card, crop, and resolution buttons."""
    # Format the card (caption and button rows)
    total_items = len(queue.list_pending(upload_id))
    current_idx = total_items - len([i for i in queue.list_pending(upload_id) if i.get("item_id") > item_id])
    caption, button_rows = format_review_card(item, upload_id=upload_id, position=(current_idx, total_items))

    # Display caption as markdown
    st.markdown(caption)

    # Side-by-side layout: Image and OCR text
    col_img, col_ocr = st.columns([2, 1])

    # Left column: Image display
    with col_img:
        # Display crop image if available (with multi-path lookup and fallback)
        img_path = item.get("image_ref", "")
        bbox = item.get("bounding_box")
        actual_img_path = None

        if img_path:
            cand_paths = [
                Path(img_path),
                Path("review_images") / Path(img_path).name,
                Path("review_images") / img_path,
            ]
            for p in cand_paths:
                if p.exists() and p.is_file():
                    actual_img_path = str(p.resolve())
                    break

        if actual_img_path:
            crop_bytes = None
            full_bytes = None
            crop_path = None
            if bbox:
                try:
                    crop_path = _crop_image(actual_img_path, bbox)
                    if crop_path and os.path.exists(crop_path):
                        with open(crop_path, "rb") as fh:
                            crop_bytes = fh.read()
                except Exception:
                    crop_bytes = None
                finally:
                    if crop_path and crop_path != actual_img_path:
                        try:
                            os.remove(crop_path)
                        except OSError:
                            pass

            # Load full image for optional viewing
            try:
                with open(actual_img_path, "rb") as fh:
                    full_bytes = fh.read()
            except Exception:
                full_bytes = None

            # Fallback to loading the original image if crop was unavailable or bbox missing
            if not crop_bytes and full_bytes:
                crop_bytes = full_bytes

            if crop_bytes:
                # OCR confidence overlay display
                ocr_conf = item.get("ocr_confidence", 0)
                conf_pct = int(round(float(ocr_conf) * 100)) if ocr_conf else 0
                conf_color = "🟢" if conf_pct >= 85 else "🟡" if conf_pct >= 70 else "🔴"

                caption_text = f"Flagged row (cropped) — OCR Confidence: {conf_color} {conf_pct}%" if bbox else f"Photo reference — OCR Confidence: {conf_color} {conf_pct}%"
                st.image(crop_bytes, caption=caption_text)

                # Show full image button
                if full_bytes and bbox:
                    with st.expander("📷 View full image"):
                        st.image(full_bytes, caption="Full original image")
            else:
                st.info("Could not load image preview")
        else:
            st.info("No image available for this item (e.g. pasted text or Zoom entry)")

    # Right column: OCR text and details
    with col_ocr:
        st.subheader("📝 OCR Text")
        raw_name = item.get("raw_name", "")
        if raw_name:
            st.code(raw_name, language=None)
        else:
            st.info("No OCR text available")

        # Smart suggestions from historical patterns
        course_key = item.get("course")
        if raw_name and course_key:
            try:
                # Look up course_id from course_key
                course_id = _get_course_id(course_key)
                if course_id:
                    suggestion_engine = init_suggestion_engine()
                    # Build roster from item context
                    roster = item.get("roster", [])
                    if roster:
                        suggestions = suggestion_engine.get_suggestions(raw_name, course_id, roster)
                        if suggestions:
                            st.subheader("💡 Smart Suggestions")
                            for idx, (name, conf) in enumerate(suggestions):
                                conf_pct = int(round(conf * 100))
                                if st.button(f"{name} ({conf_pct}%)", key=f"sugg_{item_id}_{idx}"):
                                    st.session_state[f"corrected_name_{item_id}"] = name
                                    st.success(f"Suggestion selected: {name}")
                                    st.rerun()
            except Exception:
                pass  # Gracefully degrade if suggestions fail

        # Inline editing: Edit OCR text (for correction)
        with st.expander("✏️ Edit OCR text"):
            corrected_name = st.text_input("Corrected name", value=raw_name, key=f"edit_ocr_{item_id}")
            if st.button("Apply correction", key=f"apply_ocr_{item_id}"):
                # Store corrected name in session state for resolution
                st.session_state[f"corrected_name_{item_id}"] = corrected_name
                st.success("OCR text corrected for this item")

        # Additional OCR details
        st.subheader("📊 Details")
        st.metric("Handwriting Confidence", f"{int(round(float(item.get('ocr_confidence') or 0) * 100))}%")
        st.metric("Paper Row", item.get("paper_row_index", "?"))
        st.metric("Photo Index", item.get("photo_index", "?"))

        # Session info
        st.subheader("📅 Session")
        st.text(f"Course: {item.get('course', '?')}")
        st.text(f"Session: {item.get('session', '?')}")
        st.text(f"Type: {item.get('session_type', '?')}")

    # Resolution buttons
    st.subheader("Resolve this item")

    # Get user name from session state for audit trail
    resolved_by = st.session_state.get("user_name", "webapp-user")

    # Inline editing: Choose attendance value
    attendance_options = ["Present", "Absent", "Late", "Excused"]
    attendance_choice = st.selectbox(
        "Attendance value",
        attendance_options,
        index=0,
        key=f"attendance_choice_{item_id}",
        help="Select the attendance value to write to the sheet"
    )

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
                            _resolve_item_action(item, upload_id, item_id, queue, cfg, dry_run, resolved_by, chosen_row=0, attendance_value=attendance_choice)
                    elif roster_row_str == "0":
                        if st.button("⏭️ Skip / Ignore", key=f"skip_{item_id}"):
                            _resolve_item_action(item, upload_id, item_id, queue, cfg, dry_run, resolved_by, chosen_row=0, attendance_value=attendance_choice)
                    else:
                        # Candidate match
                        try:
                            roster_row = int(roster_row_str)
                            roster_name = text.split("(")[0].strip().replace("🟢 ", "").replace("🟡 ", "").replace("🔴 ", "")
                            # Inline editing: Allow editing the matched name
                            with st.expander(f"✏️ Edit match for {roster_name}"):
                                edited_name = st.text_input("Student name", value=roster_name, key=f"edit_name_{item_id}_{roster_row}")
                                if st.button("Apply name change", key=f"apply_name_{item_id}_{roster_row}"):
                                    st.session_state[f"edited_name_{item_id}_{roster_row}"] = edited_name
                                    st.success(f"Name updated to: {edited_name}")
                                    st.rerun()

                            # Use edited name if available
                            final_name = st.session_state.get(f"edited_name_{item_id}_{roster_row}", roster_name)
                            display_text = text.replace(roster_name, final_name) if final_name != roster_name else text

                            if st.button(display_text, key=f"candidate_{item_id}_{roster_row}"):
                                # If name was edited, re-resolve the roster row
                                chosen_row_final = roster_row
                                if final_name != roster_name:
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
                                        # Find the roster row for the edited name
                                        match = next((r for r in session_info.roster if r[0] == final_name), None)
                                        if match:
                                            chosen_row_final = match[1]
                                    except Exception as exc:
                                        log.debug("webapp_review_swallowed", extra={"error": str(exc)})
                                        # Fall back to original row if lookup fails
                                _resolve_item_action(item, upload_id, item_id, queue, cfg, dry_run, resolved_by, chosen_row=chosen_row_final, attendance_value=attendance_choice)
                        except ValueError:
                            pass
                elif len(parts) >= 3 and parts[0] == "srch":
                    if st.button("🔍 Search roster", key=f"btn_search_{item_id}"):
                        st.session_state["search_upload_id"] = upload_id
                        st.session_state["search_item_id"] = item_id
                        st.rerun()

    # Search roster (if triggered)
    if st.session_state.get("search_upload_id") == upload_id and st.session_state.get("search_item_id") == item_id:
        st.subheader("🔍 Search Roster")
        search_term = st.text_input("Type a few letters of the student's name", key=f"input_search_{item_id}")

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
            # Get corrected OCR text from session state if available
            corrected_raw_name = st.session_state.get(f"corrected_name_{item_id}")
            result = resolve_item(
                upload_id=upload_id,
                item_id=item_id,
                chosen_row=chosen_row,
                queue=queue,
                writer=writer,
                cfg=cfg,
                dry_run=dry_run,
                attendance_value=attendance_value,
                corrected_raw_name=corrected_raw_name,
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


if __name__ == "__main__":
    _page_review()
