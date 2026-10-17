"""webapp/pages/names.py — Paste-names screen (W3).

Calls attendance.pipeline.run_text() with preview-before-submit UX.
"""
from __future__ import annotations

import streamlit as st

from attendance.marks import format_preview, has_issues, parse_lines, split_names_and_marks
from attendance.pipeline import run_text
from core.notifier import RunSummary
from webapp.config import CONFIG_PATH, COURSES
from webapp.pages.upload import render_summary


def _page_names() -> None:
    """Render the paste-names screen."""
    st.header("✏️ Paste Names")

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

    # Textarea for pasting names
    st.subheader("Paste names and marks")
    st.markdown(
        """
        Enter one student per line. Use the format `Name | Mark` to specify attendance marks.
        
        Examples:
        - `Ahmed Ali` → Present (default)
        - `Sara | L` → Late
        - `Omar | absent` → Absent
        - `Laila | Not complete` → Not complete
        """
    )

    names_text = st.text_area(
        "Names and marks",
        placeholder="Ahmed Ali\nSara | L\nOmar | absent",
        height=200,
        help="Paste your list here. One line per student. Use | to separate name from mark.",
    )

    # Dry-run toggle (default ON per Rule 7)
    dry_run = st.toggle(
        "Dry run (preview only)",
        value=True,
        help="When ON, changes are previewed but NOT written to the sheet. Turn OFF to write for real.",
    )

    if not names_text:
        st.info("Paste your list of names to begin.")
        return

    # Parse the input
    lines = [line.strip() for line in names_text.split("\n") if line.strip()]
    parsed = parse_lines(lines)

    if not parsed:
        st.warning("No valid names found. Please check your input.")
        return

    # Show preview
    st.divider()
    st.subheader("🔎 Preview")
    preview_text = format_preview(parsed, markdown=False)
    st.text(preview_text)

    # Check for issues
    if has_issues(parsed):
        st.warning(
            "⚠️ Some marks are not recognized and will go to the review queue. "
            "Please review the preview above before processing."
        )
        require_confirm = True
    else:
        require_confirm = False

    # Process button
    st.divider()

    if require_confirm:
        col1, col2 = st.columns([1, 1])
        with col1:
            if st.button("✅ Process anyway", type="primary", use_container_width=True):
                _process_names(parsed, course_key, session_num, session_type, dry_run)
        with col2:
            if st.button("✏️ Fix input", use_container_width=True):
                st.rerun()
    else:
        if st.button("✅ Process", type="primary", use_container_width=True):
            _process_names(parsed, course_key, session_num, session_type, dry_run)


def _process_names(
    parsed: list,
    course_key: str,
    session_num: int,
    session_type: str,
    dry_run: bool,
) -> None:
    """Process the parsed names through the pipeline."""
    # Split into names and marks lists
    names, marks = split_names_and_marks(parsed)

    # Progress callback (same stages as photo upload)
    def progress(stage: str) -> None:
        from attendance.pipeline import STAGE_MATCHING, STAGE_READING, STAGE_SAVING

        if stage == STAGE_READING:
            st.toast("📷 Reading names…", icon="📷")
        elif stage == STAGE_MATCHING:
            st.toast("🔎 Matching names…", icon="🔎")
        elif stage == STAGE_SAVING:
            st.toast("✍️ Saving to the sheet…", icon="✍️")

    # Run pipeline
    with st.spinner("Processing…"):
        try:
            summary = run_text(
                names=names,
                marks=marks,
                course=course_key,
                session_num=session_num,
                session_type=session_type or None,
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
