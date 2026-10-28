"""webapp/pages/dashboard.py — Dashboard page with metrics, quick actions, and review queue preview.

Shows attendance overview, recent activity, and quick access to common workflows.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import streamlit as st
import yaml

from attendance.history import count_marks, entries
from attendance.review_queue import ReviewQueue
from webapp.config import CONFIG_PATH


def _load_config() -> dict:
    """Load the attendance config."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def _page_dashboard() -> None:
    """Render the dashboard page."""
    st.header("📊 Dashboard")

    # Load config
    cfg = _load_config()
    review_dir = cfg.get("attendance", {}).get("review", {}).get("local_dir", "review_queue")
    course = st.session_state.get("selected_course")

    if not course:
        st.warning("Please select a course from the sidebar.")
        return

    # Initialize queue
    queue = ReviewQueue(store_dir=review_dir)

    # ─── Metrics Section ────────────────────────────────────────────────────────
    st.subheader("Attendance Overview")

    # Get latest session data from history
    history_entries = entries(review_dir, course)
    latest_entry = history_entries[-1] if history_entries else None

    if latest_entry:
        counts = latest_entry.get("counts", {})
        present = counts.get("present", 0)
        late = counts.get("late", 0)
        absent = counts.get("absent", 0)
        review_backlog = counts.get("review", 0)
        total = present + late + absent

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Present", present, delta_color="normal")
        col2.metric("Late", late, delta_color="normal")
        col3.metric("Absent", absent, delta_color="inverse")
        col4.metric("Review Backlog", review_backlog, delta_color="inverse")

        # Show session info
        session_num = latest_entry.get("session", "Unknown")
        upload_id = latest_entry.get("upload_id", "")
        ts = latest_entry.get("ts", "")
        if ts:
            try:
                dt = datetime.fromisoformat(ts)
                time_str = dt.strftime("%b %d, %Y")
            except:
                time_str = ts
        else:
            time_str = "Unknown"

        st.caption(f"Latest session: {session_num} • Uploaded: {time_str}")
    else:
        st.info("No attendance data recorded yet. Upload a photo to get started.")

    st.divider()

    # ─── Quick Actions ──────────────────────────────────────────────────────────
    st.subheader("Quick Actions")

    col1, col2, col3, col4 = st.columns(4)

    with col1:
        if st.button("📤 Upload Photos", width='stretch'):
            st.session_state["_nav"] = "📤 Upload Photos"
            st.rerun()

    with col2:
        if st.button("✏️ Paste Names", width='stretch'):
            st.session_state["_nav"] = "✏️ Paste Names"
            st.rerun()

    with col3:
        if st.button("🎦 Zoom", width='stretch'):
            st.session_state["_nav"] = "🎦 Zoom"
            st.rerun()

    with col4:
        if st.button("🔍 Review Queue", width='stretch'):
            st.session_state["_nav"] = "🔍 Review Queue"
            st.rerun()

    st.divider()

    # ─── Review Queue Preview ──────────────────────────────────────────────────
    st.subheader("Review Queue Preview")

    pending_items = queue.list_pending()
    total_pending = len(pending_items)

    if total_pending > 0:
        st.info(f"🔍 {total_pending} item(s) awaiting review")

        # Show first 5 items as preview
        preview_items = pending_items[:5]

        for i, item in enumerate(preview_items):
            with st.container():
                col1, col2, col3 = st.columns([3, 2, 1])

                raw_name = item.get("raw_name", "Unknown")
                reason = item.get("reason", "Unknown")
                upload_id = item.get("upload_id", "")

                with col1:
                    st.write(f"**{raw_name}**")
                    st.caption(f"Reason: {reason}")

                with col2:
                    if upload_id:
                        st.caption(f"Upload: {upload_id[:8]}...")

                with col3:
                    if st.button("Review", key=f"review_preview_{i}", width='stretch'):
                        st.session_state["last_upload_id"] = upload_id
                        st.session_state["_nav"] = "🔍 Review Queue"
                        st.rerun()

                if i < len(preview_items) - 1:
                    st.divider()

        if total_pending > 5:
            st.caption(f"... and {total_pending - 5} more items")

        if st.button("View All Review Items", width='stretch'):
            st.session_state["_nav"] = "🔍 Review Queue"
            st.rerun()
    else:
        st.success("✅ No items in review queue")

    st.divider()

    # ─── Recent Sessions ───────────────────────────────────────────────────────
    st.subheader("Recent Sessions")

    if history_entries:
        # Show last 5 sessions
        recent_entries = list(reversed(history_entries[-5:]))

        for entry in recent_entries:
            session = entry.get("session", "Unknown")
            counts = entry.get("counts", {})
            present = counts.get("present", 0)
            late = counts.get("late", 0)
            absent = counts.get("absent", 0)
            review = counts.get("review", 0)
            ts = entry.get("ts", "")

            if ts:
                try:
                    dt = datetime.fromisoformat(ts)
                    time_str = dt.strftime("%b %d, %Y %H:%M")
                except:
                    time_str = ts
            else:
                time_str = "Unknown"

            col1, col2, col3, col4, col5 = st.columns([2, 1, 1, 1, 1])
            col1.write(f"**Session {session}**")
            col2.metric("Present", present)
            col3.metric("Late", late)
            col4.metric("Absent", absent)
            col5.metric("Review", review)

            st.caption(f"Recorded: {time_str}")
            st.divider()
    else:
        st.info("No session history yet. Upload photos to build attendance history.")
