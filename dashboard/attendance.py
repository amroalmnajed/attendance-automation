"""Attendance page: view and filter attendance data by session."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from config import CourseConfig
import sheets


def render(course: CourseConfig) -> None:
    """Render the Attendance page for the given course."""
    st.header("Attendance")

    # Load attendance data
    df, read_at, session_cols = sheets.load_attendance(course)
    st.caption(f"Last sheet read: {read_at.strftime('%H:%M:%S UTC')}")

    if df.empty or not session_cols:
        st.info("No attendance data found in this sheet.")
        return

    # Session selector
    session_options = list(session_cols.keys())
    if not session_options:
        st.info("No session columns found in this sheet.")
        return

    selected_session = st.selectbox(
        "Select Session",
        options=session_options,
        index=0,
        key="attendance_session_selector",
    )

    # Search box
    search_query = st.text_input("🔍 Search by student name", placeholder="Type to filter...")

    # Filter by selected session
    if selected_session not in df.columns:
        st.error(f"Session column '{selected_session}' not found in data.")
        return

    # Count summary
    total = len(df)
    present = 0
    absent = 0
    needs_review = 0
    other = 0

    for _, row in df.iterrows():
        status = row.get(selected_session, "").strip().lower()
        if not status:
            continue
        elif "present" in status or "late" in status:
            present += 1
        elif "absent" in status:
            absent += 1
        elif "review" in status or "pending" in status:
            needs_review += 1
        else:
            other += 1

    # Display summary cards
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Total Students", total)
    with col2:
        st.metric("Present", present, delta=None)
    with col3:
        st.metric("Absent", absent, delta=None)
    with col4:
        st.metric("Needs Review", needs_review, delta=None)

    st.divider()

    # Apply search filter
    if search_query:
        filtered_df = df[df["student"].str.contains(search_query, case=False, na=False)]
    else:
        filtered_df = df.copy()

    # Build display dataframe
    display_data = []
    for _, row in filtered_df.iterrows():
        student = row["student"]
        status = row.get(selected_session, "").strip()

        # Determine status indicator
        if not status:
            status_display = "—"
        elif "present" in status.lower():
            status_display = f"🟢 {status}"
        elif "absent" in status.lower():
            status_display = f"🔴 {status}"
        elif "late" in status.lower():
            status_display = f"🟡 {status}"
        else:
            status_display = f"⚪ {status}"

        display_data.append({
            "Student": student,
            "Status": status_display,
            "Raw Value": status,
        })

    if not display_data:
        st.info("No students match your search.")
        return

    display_df = pd.DataFrame(display_data)

    # Render table
    st.dataframe(
        display_df[["Student", "Status"]],
        column_config={
            "Student": st.column_config.TextColumn("Student", width="large"),
            "Status": st.column_config.TextColumn("Status", width="medium"),
            "Raw Value": None,
        },
        hide_index=True,
        use_container_width=True,
    )

    # Show raw values if needed for debugging
    with st.expander("Show raw values"):
        st.dataframe(display_df[["Student", "Raw Value"]], hide_index=True, use_container_width=True)
