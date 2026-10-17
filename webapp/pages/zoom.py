"""webapp/pages/zoom.py — Zoom ingestion screen (W4).

Calls attendance.pipeline.run_zoom() with two paths:
- Meeting ID + date (Zoom API fetch)
- CSV file upload
"""
from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

import streamlit as st
import yaml

from attendance.pipeline import run_zoom
from core.notifier import RunSummary
from webapp.config import CONFIG_PATH, COURSES
from webapp.pages.upload import render_summary


def _load_config() -> dict:
    """Load the attendance config to check zoom_csv.enabled."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def _page_zoom() -> None:
    """Render the Zoom ingestion screen."""
    st.header("🎦 Zoom Ingestion")

    # Check if Zoom is enabled
    cfg = _load_config()
    zoom_cfg = cfg.get("attendance", {}).get("zoom_csv", {})
    if not zoom_cfg.get("enabled", False):
        st.error(
            "⚠️ Zoom CSV integration is disabled in config/attendance.yaml. "
            "Set `attendance.zoom_csv.enabled: true` to enable this feature."
        )
        return

    # Get selected course from sidebar (set by app.py)
    course_key = st.session_state.get("selected_course")
    if not course_key or course_key not in COURSES:
        st.warning("Please select a course from the sidebar.")
        return

    course_label = COURSES[course_key].label

    st.subheader(f"Course: {course_label}")

    # Session controls
    col1, col2 = st.columns([1, 2])
    with col1:
        session_num = st.number_input(
            "Session number",
            min_value=1,
            max_value=999,
            value=1,
            step=1,
            help="The session number to write to (e.g., Session 1, Session 2, etc.)",
        )

    with col2:
        # Optional session type (only for typed_pair layouts like g9/g10)
        session_type = st.text_input(
            "Session type (optional)",
            placeholder="e.g., Saturday - Center",
            help="Required for typed_pair layouts (g9/g10). Leave blank for single-column layouts.",
        )

    # Dry-run toggle (default ON per Rule 7)
    dry_run = st.toggle(
        "Dry run (preview only)",
        value=True,
        help="When ON, changes are previewed but NOT written to the sheet. Turn OFF to write for real.",
    )

    st.divider()

    # Two input methods: meeting ID or CSV upload
    input_method = st.radio(
        "Input method",
        options=["Meeting ID + Date (Zoom API)", "CSV file upload"],
        horizontal=True,
    )

    if input_method == "Meeting ID + Date (Zoom API)":
        _render_meeting_id_method(course_key, session_num, session_type, dry_run)
    else:
        _render_csv_upload_method(course_key, session_num, session_type, dry_run)


def _render_meeting_id_method(
    course_key: str,
    session_num: int,
    session_type: str,
    dry_run: bool,
) -> None:
    """Render the meeting ID + date input method."""
    st.subheader("📡 Fetch from Zoom API")

    col1, col2 = st.columns([1, 1])
    with col1:
        meeting_id = st.text_input(
            "Meeting ID",
            placeholder="e.g., 81795786552",
            help="The Zoom meeting ID to fetch participants from.",
        )

    with col2:
        meeting_date = st.date_input(
            "Meeting date",
            help="The date of the meeting.",
        )

    if st.button("Fetch & Process", type="primary", use_container_width=True):
        if not meeting_id:
            st.error("Please enter a meeting ID.")
            return

        if not meeting_date:
            st.error("Please select a meeting date.")
            return

        # Convert date to ISO string
        date_str = meeting_date.isoformat()

        source = {
            "meeting_id": meeting_id,
            "date": date_str,
        }

        _process_zoom(source, course_key, session_num, session_type, dry_run)


def _render_csv_upload_method(
    course_key: str,
    session_num: int,
    session_type: str,
    dry_run: bool,
) -> None:
    """Render the CSV file upload method."""
    st.subheader("📁 Upload CSV File")

    uploaded_file = st.file_uploader(
        "Upload Zoom CSV export",
        type=["csv"],
        help="Upload the Zoom participant CSV export file.",
    )

    if uploaded_file:
        st.info(f"Selected file: {uploaded_file.name}")

    if st.button("Process CSV", type="primary", use_container_width=True):
        if not uploaded_file:
            st.error("Please upload a CSV file.")
            return

        # Save uploaded file to temp directory
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            csv_path = tmpdir_path / uploaded_file.name
            csv_path.write_bytes(uploaded_file.getbuffer().tobytes())

            _process_zoom(str(csv_path), course_key, session_num, session_type, dry_run)


def _process_zoom(
    source: str | dict,
    course_key: str,
    session_num: int,
    session_type: str,
    dry_run: bool,
) -> None:
    """Process Zoom data through the pipeline."""
    # Progress callback
    def progress(stage: str) -> None:
        from attendance.pipeline import STAGE_MATCHING, STAGE_READING, STAGE_SAVING

        if stage == STAGE_READING:
            st.toast("📷 Reading Zoom data…", icon="📷")
        elif stage == STAGE_MATCHING:
            st.toast("🔎 Matching names…", icon="🔎")
        elif stage == STAGE_SAVING:
            st.toast("✍️ Saving to the sheet…", icon="✍️")

    # First attempt
    with st.spinner("Processing…"):
        try:
            summary = run_zoom(
                source=source,
                course=course_key,
                session_num=session_num,
                session_type=session_type or None,
                dry_run=dry_run,
                config_path=CONFIG_PATH,
                progress=progress,
            )

            # Handle teacher_not_found
            if summary.teacher_not_found:
                st.warning(
                    "⚠️ Teacher not found in participant list. "
                    "Please provide the manual start time below."
                )
                manual_start_time = st.text_input(
                    "Manual start time",
                    placeholder="e.g., 2024-01-15 18:00 or 6:00 PM",
                    help="Accepted formats: YYYY-MM-DD HH:MM, HH:MM, HH:MM AM/PM, etc.",
                )

                if st.button("Retry with manual time", type="primary"):
                    if manual_start_time:
                        summary = run_zoom(
                            source=source,
                            course=course_key,
                            session_num=session_num,
                            session_type=session_type or None,
                            dry_run=dry_run,
                            config_path=CONFIG_PATH,
                            manual_start_time=manual_start_time,
                            progress=progress,
                        )
                    else:
                        st.error("Please enter a manual start time.")
                        return

            # Handle session_type_ambiguous
            if summary.session_type_ambiguous:
                st.warning(
                    "⚠️ Session type is ambiguous. Please specify it explicitly."
                )
                explicit_session_type = st.selectbox(
                    "Select session type",
                    options=["Saturday - Center", "Saturday - Online", "Sunday- Center", "Sunday- Online",
                             "Monday - Online", "Tuesday - Center", "Tuesday  - Online",
                             "Wednesday - Online", "Thursday - Center", "Thursday - Online",
                             "Friday - Center", "Friday - Online"],
                    help="Choose the correct session type for this meeting.",
                )

                if st.button("Retry with session type", type="primary"):
                    summary = run_zoom(
                        source=source,
                        course=course_key,
                        session_num=session_num,
                        session_type=explicit_session_type,
                        dry_run=dry_run,
                        config_path=CONFIG_PATH,
                        progress=progress,
                    )

            # Display results
            st.divider()
            render_summary(summary)

            # Store upload_id in session state for review queue navigation
            st.session_state["last_upload_id"] = summary.upload_id

        except Exception as e:
            st.error(f"Processing failed: {str(e)}")
