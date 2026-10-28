"""webapp/pages/viewer.py — Attendance viewer & reporting (W6).

Session viewer with search, summary metrics, and reports (/report and /streaks equivalents).
Sources counts via attendance.history for consistency with bot's /report.
"""
from __future__ import annotations

from pathlib import Path

import streamlit as st
import yaml

from attendance.config_runtime import _user_error_message
from attendance.history import count_marks, entries
from attendance.reporter import generate_session_report, generate_streaks_report
from attendance.review_queue import ReviewQueue
from attendance.session_resolver import resolve as session_resolve
from webapp.config import CONFIG_PATH, COURSES


def _load_config() -> dict:
    """Load the attendance config."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def _page_viewer() -> None:
    """Render the attendance viewer screen."""
    st.header("📊 Attendance Viewer")

    # Get selected course from sidebar (set by app.py)
    course_key = st.session_state.get("selected_course")
    if not course_key or course_key not in COURSES:
        st.warning("Please select a course from the sidebar.")
        return

    course_label = COURSES[course_key].label
    cfg = _load_config()
    review_dir = cfg.get("attendance", {}).get("review", {}).get("local_dir", "review_queue")

    st.subheader(f"Course: {course_label}")

    # Tabs: Session Viewer | Reports
    tab1, tab2 = st.tabs(["Session Viewer", "Reports"])

    with tab1:
        _render_session_viewer(course_key, course_label, cfg, review_dir)

    with tab2:
        _render_reports(course_key, course_label, cfg, review_dir)


def _render_session_viewer(course_key: str, course_label: str, cfg: dict, review_dir: str) -> None:
    """Render the session viewer with search and metrics."""
    st.subheader("Session Viewer")

    # Load history entries
    history_entries = entries(review_dir, course_key)

    if not history_entries:
        st.info("No attendance history recorded yet. Upload some photos to see session data.")
        return

    # Session selector
    session_nums = [e.get("session") for e in history_entries]
    selected_session = st.selectbox(
        "Select session",
        options=session_nums,
        index=len(session_nums) - 1,  # Default to most recent
        format_func=lambda s: f"Session {s}",
    )

    # Search by student name
    search_name = st.text_input(
        "Search by student name (optional)",
        placeholder="Type a name to filter...",
        help="Filter the attendance list to show only matching students.",
    )

    # Get session info to access roster
    try:
        session_info = session_resolve(
            course_key=course_key,
            session_num=int(selected_session),
            session_type=None,
            cfg=cfg,
        )
    except Exception as e:
        st.error(_user_error_message(e, what="session"))
        return

    # Display attendance table
    st.subheader(f"Attendance for Session {selected_session}")

    # Get column data for the session
    col_idx = 0
    for ch in session_info.session_col.upper():
        col_idx = col_idx * 26 + (ord(ch) - ord("A") + 1)

    ws = session_info.get_worksheet()
    col_data = ws.col_values(col_idx)

    # Prepare data for display
    attendance_data = []
    for name, row in session_info.roster:
        if search_name and search_name.lower() not in name.lower():
            continue

        val = col_data[row - 1] if row <= len(col_data) else ""
        attendance_data.append({
            "Student": name,
            "Row": row,
            "Status": val.strip() or "Empty",
        })

    if not attendance_data:
        st.warning("No students match your search.")
        return

    # Display as table
    st.dataframe(attendance_data, width='stretch')

    # Summary metrics
    st.subheader("Summary Metrics")
    values = [row["Status"] for row in attendance_data]
    counts = count_marks(values)

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Present", counts["present"])
    col2.metric("Late", counts["late"])
    col3.metric("Absent", counts["absent"])
    col4.metric("Total", len(attendance_data))

    # Review backlog for this session (queried from SQLite ReviewQueue)
    review_backlog = 0
    try:
        queue = ReviewQueue(store_dir=review_dir)
        all_pending = queue.list_pending()
        review_backlog = len([
            i for i in all_pending
            if i.get("course") == course_key and str(i.get("session")) == str(selected_session)
        ])
    except Exception:
        review_backlog = 0

    if review_backlog > 0:
        st.warning(f"⚠️ {review_backlog} item(s) in review queue for this session")
    else:
        st.success("✅ No pending review items for this session")


def _render_reports(course_key: str, course_label: str, cfg: dict, review_dir: str) -> None:
    """Render the reports section (/report and /streaks equivalents)."""
    st.subheader("Reports")

    # Report type selector
    report_type = st.radio(
        "Report type",
        options=["Session Report", "Streaks Report"],
        horizontal=True,
    )

    if report_type == "Session Report":
        _render_session_report(course_key, course_label, cfg, review_dir)
    else:
        _render_streaks_report(course_key, course_label, cfg)


def _render_session_report(course_key: str, course_label: str, cfg: dict, review_dir: str) -> None:
    """Render the session report (equivalent to /report)."""
    st.subheader("Session Report")

    # Session selector
    session_num = st.number_input(
        "Session number",
        min_value=1,
        max_value=999,
        value=1,
        step=1,
    )

    if st.button("Generate Report", type="primary"):
        with st.spinner("Generating report…"):
            try:
                report = generate_session_report(course_key, session_num, cfg, Path(review_dir))

                st.subheader(f"Session Report: {course_label.upper()} S{session_num}")
                st.metric("Total Students", report["total_students"])
                st.metric("Present", report["present"], f"{report['attendance_rate']:.1f}%")
                st.metric("Review Backlog", report["review_backlog"])

                st.subheader("Absentees")
                if report["absentees"]:
                    for name in report["absentees"]:
                        st.text(f"• {name}")
                else:
                    st.success("No absentees for this session")

            except Exception as e:
                st.error(_user_error_message(e, what="report"))


def _render_streaks_report(course_key: str, course_label: str, cfg: dict) -> None:
    """Render the streaks report (equivalent to /streaks)."""
    st.subheader("Streaks Report")

    consecutive_absences = st.number_input(
        "Consecutive absences to check",
        min_value=1,
        max_value=10,
        value=3,
        step=1,
        help="Find students who have been absent for this many consecutive sessions.",
    )

    if st.button("Generate Streaks Report", type="primary"):
        with st.spinner("Generating streaks report…"):
            try:
                streaks = generate_streaks_report(course_key, consecutive_absences, cfg)

                st.subheader(f"Students with {consecutive_absences}+ consecutive absences")

                if streaks:
                    for streak in streaks:
                        st.text(f"• {streak['name']} (Row {streak['row']}) - {streak['missed']} consecutive absences")
                else:
                    st.success(f"No students with {consecutive_absences}+ consecutive absences")

            except Exception as e:
                st.error(_user_error_message(e, what="streaks report"))


if __name__ == "__main__":
    _page_viewer()
