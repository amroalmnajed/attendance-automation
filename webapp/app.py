"""webapp/app.py — Streamlit entrypoint for the attendance-automation webapp.

Run from the repo root:
    streamlit run webapp/app.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Ensure the repo root is on sys.path so that attendance.*, core.* imports work
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import streamlit as st  # noqa: E402 (must come after sys.path fixup)

# ---------------------------------------------------------------------------
# Page config (must be the first Streamlit call)
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Attendance Manager",
    page_icon="📋",
    layout="wide",
    initial_sidebar_state="expanded",
)

from webapp.runtime import configure_runtime_secrets  # noqa: E402

configure_runtime_secrets(st)

from webapp.auth import require_login  # noqa: E402
from webapp.config import CONFIG_ERROR, COURSES  # noqa: E402

# ---------------------------------------------------------------------------
# Constants & Navigation
# ---------------------------------------------------------------------------
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
# Sidebar
# ---------------------------------------------------------------------------

def _render_sidebar() -> str:
    """Render the sidebar and return the selected internal page key."""
    with st.sidebar:
        st.markdown("## 📋 Attendance Manager")
        st.caption(f"Signed in as {st.session_state.get('user_name', 'webapp-user')}")
        if st.button("Sign out", use_container_width=True):
            st.session_state.clear()
            st.rerun()

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
# Page renderers
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
# Main entry point (Universal role, direct access)
# ---------------------------------------------------------------------------

def main() -> None:
    if not require_login():
        return
    if CONFIG_ERROR:
        st.error(f"Configuration unavailable: {CONFIG_ERROR}")
        return
    page_key = _render_sidebar()
    renderer = _PAGE_RENDERERS.get(page_key, _page_upload)
    renderer()  # type: ignore[operator]


if __name__ == "__main__":
    main()
