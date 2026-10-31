"""webapp/pages/audit.py — Audit log viewer screen.

Displays comprehensive audit logs with filtering and search capabilities.
"""
from __future__ import annotations

import streamlit as st
from datetime import datetime, timedelta, timezone

from attendance.audit import query_audit_logs, get_audit_summary, log_user_action
from webapp.auth import check_permission


def _page_audit() -> None:
    """Render the audit log viewer screen."""
    st.header("📋 Audit Log Viewer")

    # Permission check
    if not check_permission("view_audit_log"):
        st.error("🔒 You don't have permission to view audit logs. Please contact an admin.")
        return

    # Log the audit log view action
    user_name = st.session_state.get("user_name", "webapp-user")
    log_user_action("view_audit_log", user_name)

    # Filters sidebar
    with st.sidebar:
        st.subheader("Filters")

        # Action filter
        action_filter = st.text_input("Action (e.g., resolve_item, write_attendance)", key="audit_action")

        # Entity type filter
        entity_type_filter = st.selectbox(
            "Entity Type",
            ["All", "review_item", "attendance_record", "student", "user_action", "system_event"],
            key="audit_entity_type",
        )
        entity_type_filter = None if entity_type_filter == "All" else entity_type_filter

        # User filter
        user_filter = st.text_input("User", key="audit_user")

        # Date range filter
        st.subheader("Date Range")
        date_range = st.radio(
            "Time period",
            ["Last 24 hours", "Last 7 days", "Last 30 days", "Custom"],
            key="audit_date_range",
        )

        if date_range == "Last 24 hours":
            start_date = datetime.now(timezone.utc) - timedelta(days=1)
            end_date = datetime.now(timezone.utc)
        elif date_range == "Last 7 days":
            start_date = datetime.now(timezone.utc) - timedelta(days=7)
            end_date = datetime.now(timezone.utc)
        elif date_range == "Last 30 days":
            start_date = datetime.now(timezone.utc) - timedelta(days=30)
            end_date = datetime.now(timezone.utc)
        else:  # Custom
            start_date = st.date_input("Start date", datetime.now(timezone.utc) - timedelta(days=7), key="audit_start_date")
            end_date = st.date_input("End date", datetime.now(timezone.utc), key="audit_end_date")
            start_date = datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc)
            end_date = datetime.combine(end_date, datetime.max.time(), tzinfo=timezone.utc)

        # Limit
        limit = st.number_input("Results per page", min_value=10, max_value=500, value=50, key="audit_limit")

        st.button("Apply Filters", key="audit_apply_filters")

    # Summary statistics
    st.subheader("📊 Summary")
    summary = get_audit_summary(start_date=start_date, end_date=end_date)

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Total Logs", summary["total_logs"])
    with col2:
        st.metric("Unique Actions", len(summary["action_counts"]))
    with col3:
        st.metric("Unique Users", len(summary["user_counts"]))

    # Action counts
    if summary["action_counts"]:
        st.subheader("Actions Breakdown")
        for action, count in sorted(summary["action_counts"].items(), key=lambda x: x[1], reverse=True):
            st.write(f"- {action}: {count}")

    # User counts
    if summary["user_counts"]:
        st.subheader("User Activity")
        for user, count in sorted(summary["user_counts"].items(), key=lambda x: x[1], reverse=True):
            st.write(f"- {user}: {count}")

    # Query audit logs
    st.subheader("📋 Audit Logs")

    # Pagination
    offset = st.session_state.get("audit_offset", 0)
    col_prev, col_page, col_next = st.columns([1, 2, 1])
    with col_prev:
        if st.button("← Previous", key="audit_prev") and offset > 0:
            st.session_state["audit_offset"] = offset - limit
            st.rerun()
    with col_page:
        st.write(f"Page {offset // limit + 1}")
    with col_next:
        if st.button("Next →", key="audit_next"):
            st.session_state["audit_offset"] = offset + limit
            st.rerun()

    # Query logs
    logs = query_audit_logs(
        action=action_filter if action_filter else None,
        entity_type=entity_type_filter,
        user=user_filter if user_filter else None,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        offset=offset,
    )

    if not logs:
        st.info("No audit logs found matching the filters")
        return

    # Display logs
    for log in logs:
        with st.expander(
            f"{log['timestamp']} — {log['action']} — {log['entity_type']} — {log['user']}",
            expanded=False,
        ):
            st.write(f"**Log ID:** {log['log_id']}")
            st.write(f"**Action:** {log['action']}")
            st.write(f"**Entity Type:** {log['entity_type']}")
            st.write(f"**Entity ID:** {log['entity_id'] or 'N/A'}")
            st.write(f"**User:** {log['user']}")
            st.write(f"**Timestamp:** {log['timestamp']}")

            if log['old_value']:
                st.write("**Old Value:**")
                st.json(log['old_value'])

            if log['new_value']:
                st.write("**New Value:**")
                st.json(log['new_value'])

            if log['metadata']:
                st.write("**Metadata:**")
                st.json(log['metadata'])


if __name__ == "__main__":
    _page_audit()
