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

    # Load review queue for updates
    review_df, _ = sheets.load_review_queue(course)

    success_count = 0
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

        # Find the session column
        session_num = row.get("session_num", "")
        session_col_name = None
        for col in session_cols.keys():
            # For crash/main: "Session 1", "Session 2", etc.
            # For g9/g10: "S1 Attendance", "S2 Attendance", etc.
            # Use word boundary to avoid "1" matching "Session 10", "Session 11", etc.
            if re.search(rf'\b{re.escape(str(session_num))}\b', col):
                session_col_name = col
                break

        if not session_col_name:
            errors.append(f"Session column for session {session_num} not found")
            continue

        # Check if already has a value (idempotency)
        existing_value = att_df.at[student_row_idx, session_col_name]
        if existing_value and existing_value.strip():
            errors.append(f"Student '{resolved_name}' already has '{existing_value}' for {session_col_name}")
            continue

        # Write to attendance sheet
        try:
            # Write to main attendance sheet
            written = sheets.write_attendance(
                course=course,
                student_name=resolved_name,
                session_column=session_col_name,
                value=resolved_value,
            )

            if not written:
                errors.append(f"Failed to write '{resolved_name}' to {session_col_name}")
                continue

            # Update review item status
            sheets.update_review_item(
                course=course,
                item_id=item_id,
                resolved_name=resolved_name,
                resolved_value=resolved_value,
                resolved_by="admin",  # TODO: Get from session state when proper auth is added
            )

            success_count += 1
        except Exception as e:
            errors.append(f"Error writing '{resolved_name}': {e}")

    # Show results
    if success_count > 0:
        st.success(f"Successfully approved {success_count} items")
    if errors:
        with st.expander("Errors occurred (click to expand)", expanded=False):
            for err in errors:
                st.error(f"  • {err}")
