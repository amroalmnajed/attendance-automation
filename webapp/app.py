"""webapp/app.py — Streamlit entrypoint for the attendance-automation webapp.

Run from the repo root:
    streamlit run webapp/app.py

This webapp replaces the Telegram bot as the primary interface by calling the
same attendance/pipeline.py functions the bot and CLI call.  See HANDOFF_WEBAPP.md
for the full task list and rules.

W1: scaffold, auth gate, sidebar with course selector, empty page stubs.
"""
from __future__ import annotations

import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Ensure the repo root is on sys.path so that attendance.*, core.* imports
# work regardless of where Streamlit was launched from.
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import streamlit as st  # noqa: E402 (must come after sys.path fixup)

from webapp.config import CONFIG_PATH, COURSES, _secret  # noqa: E402

# ---------------------------------------------------------------------------
# Page config (must be the first Streamlit call)
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Attendance Manager",
    page_icon="📋",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_WEBAPP_PASSWORD: str = _secret("WEBAPP_PASSWORD")

# Page registry: display label → internal key
_PAGES: dict[str, str] = {
    "📤 Upload Photos": "upload",
    "✏️ Paste Names": "names",
    "🎦 Zoom": "zoom",
    "🔍 Review Queue": "review",
    "📊 Attendance Viewer": "viewer",
    "🛠️ Admin": "admin",
}


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _is_authenticated() -> bool:
    return bool(st.session_state.get("authenticated", False))


def _show_login_screen() -> None:
    """Render the password + name gate.  On success, sets session state and reruns."""
    col_l, col_c, col_r = st.columns([1, 2, 1])
    with col_c:
        st.title("📋 Attendance Manager")
        st.write("")

        if not _WEBAPP_PASSWORD:
            st.error(
                "⚠️ **WEBAPP_PASSWORD** is not set.  "
                "Add it to `.streamlit/secrets.toml` or as an environment variable, "
                "then restart the app."
            )
            return

        with st.form("login_form", clear_on_submit=False):
            st.subheader("Sign in")
            name = st.text_input(
                "Your name",
                placeholder="e.g. Amro",
                help="Stored in audit trails for review resolutions and sheet writes.",
            )
            pwd = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Sign in", use_container_width=True)

        if submitted:
            if pwd == _WEBAPP_PASSWORD:
                st.session_state["authenticated"] = True
                st.session_state["user_name"] = name.strip() or "webapp-user"
                st.rerun()
            else:
                st.error("Incorrect password — try again.")


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

def _render_sidebar() -> str:
    """Render the sidebar and return the selected internal page key."""
    with st.sidebar:
        st.markdown("## 📋 Attendance")
        user = st.session_state.get("user_name", "unknown")
        st.caption(f"Signed in as **{user}**")

        if st.button("Sign out", use_container_width=True, type="secondary"):
            st.session_state.clear()
            st.rerun()

        st.divider()

        # Course selector — stored in session state for use by all page modules
        course_keys = list(COURSES.keys())
        if course_keys:
            st.selectbox(
                "Course",
                options=course_keys,
                format_func=lambda k: COURSES[k].label if k in COURSES else k,
                key="selected_course",
                help="All uploads and reports on this page use this course's sheet.",
            )
        else:
            st.warning("No courses found — check `config/attendance.yaml`.")

        st.divider()

        # Navigation
        page_label = st.radio(
            "Navigate",
            options=list(_PAGES.keys()),
            key="_nav",
            label_visibility="collapsed",
        )

    return _PAGES[page_label]


# ---------------------------------------------------------------------------
# Page renderers (stubs — filled in W2-W7)
# ---------------------------------------------------------------------------

def _page_upload() -> None:
    from webapp.pages.upload import _page_upload as render_upload
    render_upload()


def _page_names() -> None:
    from webapp.pages.names import _page_names as render_names
    render_names()


def _page_zoom() -> None:
    from webapp.pages.zoom import _page_zoom as render_zoom
    render_zoom()


def _page_review() -> None:
    from webapp.pages.review import _page_review as render_review
    render_review()


def _page_viewer() -> None:
    from webapp.pages.viewer import _page_viewer as render_viewer
    render_viewer()


def _page_admin() -> None:
    from webapp.pages.admin import _page_admin as render_admin
    render_admin()


_PAGE_RENDERERS: dict[str, object] = {
    "upload": _page_upload,
    "names": _page_names,
    "zoom": _page_zoom,
    "review": _page_review,
    "viewer": _page_viewer,
    "admin": _page_admin,
}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if not _is_authenticated():
        _show_login_screen()
        return

    page_key = _render_sidebar()
    renderer = _PAGE_RENDERERS.get(page_key, _page_upload)
    renderer()  # type: ignore[operator]


main()
