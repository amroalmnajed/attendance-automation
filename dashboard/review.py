"""Review Queue page: load, display, and resolve uncertain name matches."""
from __future__ import annotations

import re
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from config import STATUS_ABSENT, STATUS_PRESENT, CourseConfig
import sheets


def render(course: CourseConfig) -> None:
    """Render the Review Queue page for the given course."""
    st.header("Review Queue")

    # Load data
    df, read_at = sheets.load_review_queue(course)
    st.caption(f"Last sheet read: {read_at.strftime('%H:%M:%S UTC')}")

    # Filter to pending items only
    pending = df[df["status"] == "pending"].copy()

    if pending.empty:
        st.info("No pending review items. All clear! 🎉")
        return

    st.subheader(f"{len(pending)} pending items")

    # Selection state
    if "selected_review_ids" not in st.session_state:
        st.session_state.selected_review_ids = set()

    # Select all / deselect all buttons
    col1, col2 = st.columns([1, 1])
    with col1:
        if st.button("☐ Select all"):
            st.session_state.selected_review_ids = set(pending["id"].tolist())
            st.rerun()
    with col2:
        if st.button("☑ Deselect all"):
            st.session_state.selected_review_ids = set()
            st.rerun()

    st.divider()

    # Render each item as an expander
    for idx, row in pending.iterrows():
        item_id = row["id"]
        is_selected = item_id in st.session_state.selected_review_ids

        with st.expander(f"{'✓ ' if is_selected else ''}{row['raw_name']} (Session {row.get('session_num', '?')})", expanded=False):
            col1, col2 = st.columns([3, 1])

            with col1:
                # Checkbox for selection
                if st.checkbox("Select for bulk approval", value=is_selected, key=f"select_{item_id}"):
                    st.session_state.selected_review_ids.add(item_id)
                else:
                    st.session_state.selected_review_ids.discard(item_id)

                # Build candidate options
                candidates = []
                for i in range(1, 4):
                    name = row.get(f"candidate_{i}", "").strip()
                    score = row.get(f"candidate_{i}_score", "")
                    if name:
                        candidates.append(f"{name} ({score}%)")

                # Add "none of these" option
                candidates.append("None of these / mark absent")

                # Resolution dropdown
                resolution = st.selectbox(
                    "Choose the correct name:",
                    candidates,
                    key=f"resolution_{item_id}",
                )

            with col2:
                st.caption(f"Reason: {row.get('reason_code', 'N/A')}")
                st.caption(f"Created: {row.get('created_at', 'N/A')}")

    # Bulk approve button
    st.divider()
    selected_ids = st.session_state.selected_review_ids
    if len(selected_ids) > 0:
        selected_rows = pending[pending["id"].isin(selected_ids)]
        if st.button(f"✅ Approve {len(selected_ids)} selected items", type="primary", use_container_width=True):
            with st.spinner("Approving..."):
                approve_selected(selected_rows, course)
            st.session_state.selected_review_ids = set()
            st.rerun()
    else:
        st.caption("No items selected. Check the boxes above to select items for bulk approval.")


def approve_selected(selected: pd.DataFrame, course: CourseConfig) -> None:
    """Approve the selected review items and write to the main attendance sheet."""
    if selected.empty:
        return

    # Load the main attendance sheet
    att_df, _, session_cols = sheets.load_attendance(course)

    # Prepare batch updates
    attendance_updates = []
    type_updates = []  # For g9/g10 typed-pair courses
    review_updates = []
    errors = []

    for idx, row in selected.iterrows():
        item_id = row["id"]

        # Get the resolution from session state
        resolution_key = f"resolution_{item_id}"
        resolution = st.session_state.get(resolution_key, "")

        if not resolution:
            errors.append(f"No resolution selected for {row['raw_name']}")
            continue

        # Parse resolution
        if "None of these" in resolution:
            resolved_name = ""
            resolved_value = STATUS_ABSENT
        else:
            # Extract name from "Name (score%)" format
            resolved_name = resolution.split(" (")[0].strip()
            resolved_value = STATUS_PRESENT

        # Find the student row in attendance sheet
        student_row_idx = att_df[att_df["student"] == resolved_name].index
        if len(student_row_idx) == 0:
            errors.append(f"Student '{resolved_name}' not found in roster")
            continue

        student_row_idx = student_row_idx[0]

        # Use session_column from the review item if available (exact match)
        # Fall back to regex reconstruction for backward compatibility
        session_col_name = row.get("session_column", "")
        if not session_col_name:
            # Backward compatible: reconstruct from session_num
            session_num = row.get("session_num", "")
            for col in session_cols.keys():
                # For crash/main: "Session 1", "Session 2", etc.
                # For g9/g10: "S1 Attendance", "S2 Attendance", etc.
                # Use word boundary to avoid "1" matching "Session 10", "Session 11", etc.
                if re.search(rf'\b{re.escape(str(session_num))}\b', col):
                    session_col_name = col
                    break

        if not session_col_name:
            errors.append(f"Session column for session {row.get('session_num', '?')} not found")
            continue

        # Check if already has a value (idempotency)
        existing_value = att_df.at[student_row_idx, session_col_name]
        if existing_value and existing_value.strip():
            errors.append(f"Student '{resolved_name}' already has '{existing_value}' for {session_col_name}")
            continue

        # Add to batch updates
        attendance_updates.append((resolved_name, session_col_name, resolved_value))

        # For typed-pair courses (g9/g10), also write the session type
        session_type = row.get("session_type", "")
        if session_type and course.attendance_pattern:
            # Find the type column (left of attendance column for typed-pair)
            type_col_name = session_col_name.replace(" Attendance", "")
            if type_col_name in session_cols:
                type_updates.append((resolved_name, type_col_name, session_type))

        resolved_by = st.session_state.get("user_name", "admin") or "admin"
        review_updates.append((item_id, resolved_name, resolved_value, resolved_by))

    # Execute batch writes - attendance first
    att_success_count, att_errors = sheets.batch_write_attendance(course, attendance_updates)
    errors.extend(att_errors)

    # Execute batch writes - type cells for g9/g10
    if type_updates:
        type_success_count, type_errors = sheets.batch_write_attendance(course, type_updates)
        errors.extend(type_errors)

    # Mark review items as approved (only if attendance write succeeded)
    if att_success_count > 0:
        review_success_count, review_errors = sheets.batch_update_review_items(course, review_updates)
        errors.extend(review_errors)

    # Show results
    if att_success_count > 0:
        st.success(f"Successfully approved {att_success_count} items")
    if errors:
        with st.expander("Errors occurred (click to expand)", expanded=False):
            for err in errors:
                st.error(f"  • {err}")
