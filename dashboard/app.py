"""Streamlit entrypoint: password gate + sidebar shell + page routing."""
from __future__ import annotations

import streamlit as st

from config import COURSES, get_course, get_password
import sheets
import review
import attendance


st.set_page_config(page_title="Teacher's Desk", page_icon="📚", layout="wide")


def gate() -> bool:
    """Return True when the session is authed; otherwise render the login form."""
    if st.session_state.get("authed"):
        return True

    st.markdown("## 📚 Teacher's Desk")
    st.caption("Review · Fix · Understand")
    st.divider()

    with st.form("login", clear_on_submit=False):
        password = st.text_input("Password", type="password", label_visibility="collapsed")
        submitted = st.form_submit_button("Enter", use_container_width=True)

    if submitted:
        try:
            expected = get_password()
        except RuntimeError as exc:
            st.error(str(exc))
            return False
        if password == expected:
            st.session_state.authed = True
            st.rerun()
        else:
            st.error("Wrong password.")
    return False


def sidebar() -> str:
    """Render the sidebar and return the selected course key."""
    with st.sidebar:
        st.title("📚 Teacher's Desk")
        st.caption("Review · Fix · Understand")
        st.divider()

        # User name input for audit trail
        if "user_name" not in st.session_state:
            st.session_state.user_name = ""

        user_name = st.text_input(
            "Your name (for audit log)",
            value=st.session_state.user_name,
            placeholder="Enter your name",
            key="user_name_input"
        )
        st.session_state.user_name = user_name

        st.divider()

        available = [c for c in COURSES if c.sheet_id]
        if not available:
            st.error("No courses configured. Set *_SHEET_ID in .env or st.secrets.")
            st.stop()

        course_key = st.selectbox(
            "Course",
            options=[c.key for c in available],
            format_func=lambda k: next(c.display_name for c in available if c.key == k),
        )
        st.session_state.course_key = course_key

        st.divider()
        page = st.radio(
            "Navigate",
            options=["Review Queue", "Attendance"],
            label_visibility="collapsed",
        )
        st.session_state.page = page

        st.divider()
        if st.button("🔄 Refresh data", use_container_width=True):
            sheets.bust_cache()
            st.rerun()

        last_read = st.session_state.get("last_read_at")
        if last_read:
            st.caption(f"Last sheet read: {last_read.strftime('%H:%M:%S UTC')}")
        else:
            st.caption("Last sheet read: —")

        st.divider()
        if st.button("🚪 Sign out", use_container_width=True):
            st.session_state.clear()
            st.rerun()

    return course_key


def main() -> None:
    if not gate():
        return

    course_key = sidebar()
    course = get_course(course_key)
    page = st.session_state.get("page", "Review Queue")

    # Render the selected page
    if page == "Review Queue":
        review.render(course)
    elif page == "Attendance":
        attendance.render(course)
    else:
        st.header(page)


if __name__ == "__main__":
    main()
