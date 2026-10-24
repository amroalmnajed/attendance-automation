"""webapp/pages/zoom.py — Zoom ingestion screen (W4).

Calls attendance.zoom_workflow.run_zoom() with two paths:
- Meeting ID + date (Zoom API fetch) — auto-detects Meeting ID and latest date
- CSV file upload
"""
from __future__ import annotations

import os
import tempfile
from datetime import date
from pathlib import Path
from typing import Optional

import streamlit as st
import yaml

from attendance.utils.progress import STAGE_MATCHING, STAGE_READING, STAGE_SAVING
from attendance.zoom_workflow import run_zoom
from attendance.zoom_api import list_recent_meeting_instances
from webapp.config import (
    CONFIG_PATH,
    COURSES,
    course_has_session_types,
    detect_next_session_number,
    get_allowed_session_types,
    get_course_meeting_id,
    normalize_session_type,
)
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

    # Session controls (crash & main have no session type; g9 & g10 do)
    has_types = course_has_session_types(course_key)
    session_type = None
    default_session = detect_next_session_number(course_key)

    if has_types:
        col1, col2 = st.columns([1, 2])
        with col1:
            session_num = st.number_input(
                "Session number",
                min_value=1,
                max_value=999,
                value=default_session,
                step=1,
                key=f"zoom_session_{course_key}",
                help="The session number to write to (auto-detected from sheet)",
            )
            st.caption(f"Auto-detected next: **Session {default_session}**")
        with col2:
            allowed_types = get_allowed_session_types(course_key, int(session_num))
            type_options = allowed_types + ["Custom / Type your own..."]
            selected_type_option = st.selectbox(
                "Session type",
                options=type_options,
                help="Select or enter session type for this course",
            )
            if selected_type_option == "Custom / Type your own...":
                custom_type = st.text_input(
                    "Type session type (e.g. sat center)",
                    placeholder="e.g. sat center",
                )
                session_type = normalize_session_type(custom_type, course_key, int(session_num))
                if custom_type and session_type != custom_type:
                    st.caption(f"Mapped to: **{session_type}**")
            else:
                session_type = selected_type_option
    else:
        session_num = st.number_input(
            "Session number",
            min_value=1,
            max_value=999,
            value=default_session,
            step=1,
            key=f"zoom_session_{course_key}",
            help="The session number to write to (auto-detected from sheet)",
        )
        st.caption(f"Auto-detected next: **Session {default_session}**")
        session_type = None

    # Dry-run toggle (default OFF: uploads write unless explicitly previewed)
    dry_run = st.toggle(
        "Dry run (preview only)",
        value=False,
        help="When ON, changes are previewed but NOT written to the sheet.",
    )

    st.divider()

    # Check if we are waiting for user input to retry an ambiguous or missing-teacher Zoom run
    prompt = st.session_state.get("zoom_prompt")
    req = st.session_state.get("zoom_req")

    if prompt and req:
        _render_prompt_resolution(prompt, req)
        return

    # Two input methods: meeting ID or CSV upload
    input_method = st.radio(
        "Input method",
        options=["Auto-Fetch from Zoom API", "CSV file upload"],
        horizontal=True,
    )

    resolved_type = normalize_session_type(session_type, course_key, int(session_num)) if session_type else None

    if input_method == "Auto-Fetch from Zoom API":
        _render_api_fetch_method(course_key, int(session_num), resolved_type, dry_run, zoom_cfg)
    else:
        _render_csv_upload_method(course_key, int(session_num), resolved_type, dry_run, zoom_cfg)

    # Display last summary if exists and no active prompt
    last_summary = st.session_state.get("zoom_last_summary")
    if last_summary:
        st.divider()
        render_summary(last_summary)
        if st.button("Clear results", use_container_width=True):
            st.session_state.pop("zoom_last_summary", None)
            st.session_state.pop("zoom_prompt", None)
            st.session_state.pop("zoom_req", None)
            _cleanup_temp_csv()
            st.rerun()


def _render_api_fetch_method(
    course_key: str,
    session_num: int,
    session_type: Optional[str],
    dry_run: bool,
    zoom_cfg: dict,
) -> None:
    """Render the Zoom API fetch method with auto-detected meeting ID and instances."""
    st.subheader("📡 Fetch from Zoom API")

    # 1. Auto-detect Meeting ID from Course
    auto_meeting_id = get_course_meeting_id(course_key)
    override_meeting = st.checkbox("Change Meeting ID", value=not bool(auto_meeting_id))

    if override_meeting or not auto_meeting_id:
        meeting_id = st.text_input(
            "Meeting ID",
            value=auto_meeting_id,
            placeholder="e.g., 81887392957",
            help="Zoom meeting ID to fetch participants from.",
        ).strip()
    else:
        meeting_id = auto_meeting_id
        st.info(f"📹 **Meeting ID**: `{meeting_id}` (Auto-detected for course **{course_key.upper()}**)")

    if not meeting_id:
        st.warning("Please enter a Meeting ID to continue.")
        return

    # 2. Auto-detect Meeting Instance / Date
    zoom_api_cfg = zoom_cfg.get("zoom_api", {})
    instances = []
    fetch_err = None

    try:
        instances = list_recent_meeting_instances(meeting_id, zoom_api_cfg)
    except Exception as e:
        fetch_err = str(e)

    selected_date_str = None
    selected_instance_uuid = None

    if instances:
        instances_sorted = sorted(instances, key=lambda x: x.get("start_time", ""), reverse=True)
        options = []
        for idx, inst in enumerate(instances_sorted[:15]):
            st_str = (inst.get("start_time") or "").replace("T", " ")[:16]
            tag = " [Latest]" if idx == 0 else ""
            options.append(f"{st_str}{tag}")

        selected_opt = st.selectbox(
            "Meeting Date / Instance (Auto-detected latest meeting)",
            options=options,
            index=0,
            help="Zoom meetings for this ID. The most recent meeting is selected automatically.",
        )
        sel_idx = options.index(selected_opt)
        selected_instance = instances_sorted[sel_idx]
        selected_instance_uuid = selected_instance.get("uuid")
        selected_date_str = selected_instance.get("start_time")
    else:
        if fetch_err:
            st.caption(f"Note: Could not query past instances automatically ({fetch_err}). Using manual date.")
        meeting_date = st.date_input("Meeting date", value=date.today())
        selected_date_str = meeting_date.isoformat()

    if st.button("Fetch & Process Attendance", type="primary", use_container_width=True):
        source = {
            "meeting_id": meeting_id,
            "instance_uuid": selected_instance_uuid,
            "date": selected_date_str,
        }
        req = {
            "source": source,
            "course_key": course_key,
            "session_num": session_num,
            "session_type": session_type,
            "dry_run": dry_run,
        }
        _execute_zoom_run(req)


def _render_csv_upload_method(
    course_key: str,
    session_num: int,
    session_type: Optional[str],
    dry_run: bool,
    zoom_cfg: dict,
) -> None:
    """Render the CSV file upload / archive picker method."""
    st.subheader("📁 Process CSV File")

    # Check for archived CSV files for convenience
    archive_folder = Path(zoom_cfg.get("archive_folder", "csv_archive"))
    archived_files = []
    if archive_folder.exists():
        for f in sorted(archive_folder.glob("*.csv"), key=os.path.getmtime, reverse=True):
            if course_key.lower() in f.name.lower() or not any(c in f.name.lower() for c in COURSES):
                archived_files.append(f)

    csv_source_mode = "Upload a new CSV file"
    if archived_files:
        csv_source_mode = st.radio(
            "CSV Source",
            options=["Select recently archived CSV", "Upload a new CSV file"],
            horizontal=True,
        )

    target_csv_path = None

    if csv_source_mode == "Select recently archived CSV" and archived_files:
        chosen_archive = st.selectbox(
            "Select recent Zoom CSV (Auto-detected latest on top)",
            options=[str(f.name) for f in archived_files[:10]],
            index=0,
        )
        if chosen_archive:
            target_csv_path = str(archive_folder / chosen_archive)
            st.caption(f"Selected: `{chosen_archive}`")
    else:
        uploaded_file = st.file_uploader(
            "Upload Zoom participant CSV export",
            type=["csv"],
            help="Upload the Zoom participant CSV export file.",
        )
        if uploaded_file:
            _cleanup_temp_csv()
            suffix = Path(uploaded_file.name).suffix or ".csv"
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(uploaded_file.getbuffer().tobytes())
                target_csv_path = tmp.name
            st.session_state["zoom_csv_temp_path"] = target_csv_path

    if st.button("Process CSV", type="primary", use_container_width=True):
        if not target_csv_path:
            st.error("Please select or upload a CSV file.")
            return

        req = {
            "source": target_csv_path,
            "course_key": course_key,
            "session_num": session_num,
            "session_type": session_type,
            "dry_run": dry_run,
        }
        _execute_zoom_run(req)


def _render_prompt_resolution(prompt: dict, req: dict) -> None:
    """Render prompts when teacher is not found or session type is ambiguous."""
    p_type = prompt.get("type")

    if p_type == "teacher_not_found":
        st.warning(
            "⚠️ Teacher not found in participant list. "
            "Please provide the manual start time below."
        )
        manual_start_time = st.text_input(
            "Manual start time",
            placeholder="e.g., 2026-10-17 18:00 or 6:00 PM",
            help="Accepted formats: YYYY-MM-DD HH:MM, HH:MM, HH:MM AM/PM, etc.",
        )
        col1, col2 = st.columns([1, 1])
        with col1:
            if st.button("Retry with manual time", type="primary", use_container_width=True):
                if manual_start_time.strip():
                    _execute_zoom_run(
                        req=req,
                        manual_start_time=manual_start_time.strip(),
                    )
                else:
                    st.error("Please enter a manual start time.")
        with col2:
            if st.button("Cancel", use_container_width=True):
                _reset_zoom_state()
                st.rerun()

    elif p_type == "session_type_ambiguous":
        st.warning("⚠️ Session type is ambiguous. Please specify it explicitly.")
        allowed = get_allowed_session_types(req["course_key"], req["session_num"])
        explicit_session_type = st.selectbox(
            "Select session type",
            options=allowed or [
                "Saturday - Center", "Saturday - Online",
                "Sunday- Center", "Sunday- Online",
                "Monday - Online",
                "Tuesday - Center", "Tuesday  - Online",
                "Wednesday - Online",
                "Thursday - Center", "Thursday - Online",
                "Friday - Center", "Friday - Online",
            ],
            help="Choose the correct session type for this meeting.",
        )
        col1, col2 = st.columns([1, 1])
        with col1:
            if st.button("Retry with session type", type="primary", use_container_width=True):
                _execute_zoom_run(
                    req=req,
                    explicit_session_type=explicit_session_type,
                )
        with col2:
            if st.button("Cancel", use_container_width=True):
                _reset_zoom_state()
                st.rerun()


def _execute_zoom_run(
    req: dict,
    manual_start_time: Optional[str] = None,
    explicit_session_type: Optional[str] = None,
) -> None:
    """Execute Zoom ingestion workflow with state management."""
    def progress(stage: str) -> None:
        if stage == STAGE_READING:
            st.toast("📷 Reading Zoom data…", icon="📷")
        elif stage == STAGE_MATCHING:
            st.toast("🔎 Matching names…", icon="🔎")
        elif stage == STAGE_SAVING:
            st.toast("✍️ Saving to the sheet…", icon="✍️")

    effective_session_type = explicit_session_type or req.get("session_type") or None

    with st.spinner("Processing Zoom data…"):
        try:
            summary = run_zoom(
                source=req["source"],
                course=req["course_key"],
                session_num=int(req["session_num"]),
                session_type=effective_session_type,
                dry_run=req["dry_run"],
                config_path=CONFIG_PATH,
                manual_start_time=manual_start_time,
                progress=progress,
            )

            if summary.teacher_not_found and not manual_start_time:
                st.session_state["zoom_prompt"] = {"type": "teacher_not_found"}
                st.session_state["zoom_req"] = req
                st.rerun()
                return

            if summary.session_type_ambiguous and not explicit_session_type:
                st.session_state["zoom_prompt"] = {"type": "session_type_ambiguous"}
                st.session_state["zoom_req"] = req
                st.rerun()
                return

            # Success or completed run
            st.session_state["zoom_last_summary"] = summary
            st.session_state["last_upload_id"] = summary.upload_id
            st.session_state.pop("zoom_prompt", None)
            st.session_state.pop("zoom_req", None)
            _cleanup_temp_csv()
            st.rerun()

        except Exception as e:
            st.error(f"Processing failed: {str(e)}")


def _reset_zoom_state() -> None:
    st.session_state.pop("zoom_prompt", None)
    st.session_state.pop("zoom_req", None)
    _cleanup_temp_csv()


def _cleanup_temp_csv() -> None:
    csv_path = st.session_state.pop("zoom_csv_temp_path", None)
    if csv_path and os.path.exists(csv_path):
        try:
            os.remove(csv_path)
        except OSError:
            pass


if __name__ == "__main__":
    _page_zoom()
