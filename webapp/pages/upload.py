"""webapp/pages/upload.py — Photo upload & processing screen (W2).

Calls attendance.pipeline.run() / run_multi() with the same parameters the bot uses.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import streamlit as st

from attendance.pipeline import STAGE_MATCHING, STAGE_READING, STAGE_SAVING, run, run_multi
from core.notifier import RunSummary
from webapp.config import CONFIG_PATH, COURSES


def render_summary(summary: RunSummary) -> None:
    """Render a RunSummary in Streamlit (pure display logic, no side effects)."""
    tag = "🧪 [DRY-RUN] " if summary.dry_run else "✅ "
    st.success(f"{tag}*Attendance Recorded!*")

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Auto-match", summary.matched)
    col2.metric("Written", summary.written)
    col3.metric("Flagged", summary.flagged, delta_color="inverse")
    if summary.unmatched > 0:
        col4.metric("Unmatched", summary.unmatched)

    # Session stats (if available)
    if summary.present_count + summary.late_count + summary.absent_count > 0:
        st.subheader("📊 This Session")
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Present", summary.present_count)
        col2.metric("Late", summary.late_count)
        col3.metric("Absent", summary.absent_count)
        col4.metric("Review", summary.flagged)

    # Sheet URL
    if summary.sheet_url:
        st.markdown(f"🔗 [View on Google Sheet]({summary.sheet_url})")

    # Review queue link for flagged items
    if summary.flagged > 0:
        st.info(
            f"⚠️ {summary.flagged} item(s) need review. "
            f"Go to the **Review Queue** page to resolve them (upload ID: `{summary.upload_id}`)."
        )

    # Errors
    if summary.errors:
        st.error("Errors:")
        for err in summary.errors:
            st.text(f"• {err}")

    # Absent offer (W7 will surface the actual action)
    if summary.absent_offer:
        st.info(
            f"📢 {summary.missing_count} student(s) are missing from this upload. "
            "Use the Admin page to mark them absent (W7)."
        )


def _page_upload() -> None:
    """Render the photo upload screen."""
    st.header("📤 Upload Photos")

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

    # Photo uploader
    uploaded_files = st.file_uploader(
        "Upload attendance photo(s)",
        type=["jpg", "jpeg", "png"],
        accept_multiple_files=True,
        help="Upload one or more photos of the attendance sheet. Multiple photos are merged as one upload.",
    )

    # Dry-run toggle (default ON per Rule 7)
    dry_run = st.toggle(
        "Dry run (preview only)",
        value=True,
        help="When ON, changes are previewed but NOT written to the sheet. Turn OFF to write for real.",
    )

    # Process button
    if not uploaded_files:
        st.info("Upload one or more photos to begin.")
        return

    if st.button("Process", type="primary", use_container_width=True):
        # Save uploaded files to temp directory
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            image_paths = []

            for uploaded_file in uploaded_files:
                # Preserve file extension
                ext = Path(uploaded_file.name).suffix or ".jpg"
                temp_path = tmpdir_path / f"{uploaded_file.name}{ext}"
                temp_path.write_bytes(uploaded_file.getbuffer().tobytes())
                image_paths.append(str(temp_path))

            # Progress callback
            def progress(stage: str) -> None:
                if stage == STAGE_READING:
                    st.toast("📷 Reading the photo…", icon="📷")
                elif stage == STAGE_MATCHING:
                    st.toast("🔎 Matching names…", icon="🔎")
                elif stage == STAGE_SAVING:
                    st.toast("✍️ Saving to the sheet…", icon="✍️")

            # Run pipeline
            with st.spinner("Processing…"):
                try:
                    if len(image_paths) == 1:
                        summary = run(
                            image_path=image_paths[0],
                            course=course_key,
                            session_num=session_num,
                            session_type=session_type or None,
                            dry_run=dry_run,
                            config_path=CONFIG_PATH,
                            progress=progress,
                        )
                    else:
                        summary = run_multi(
                            image_paths=image_paths,
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
