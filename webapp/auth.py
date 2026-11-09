"""Small shared-password gate for the private Streamlit app.

Integrates with attendance.roles for RBAC permission checks.
"""
from __future__ import annotations

import hmac
import os

import streamlit as st
import yaml

from webapp.config import CONFIG_PATH


def _load_config() -> dict:
    """Load the attendance config."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def require_login() -> bool:
    """Render the login form until a user has supplied the configured password."""
    if st.session_state.get("authenticated"):
        return True

    expected = os.environ.get("WEBAPP_PASSWORD", "")
    st.title("Attendance Manager")
    if not expected or expected == "change-me":
        st.error("This app is not configured. Set a strong WEBAPP_PASSWORD in Streamlit secrets.")
        return False

    with st.form("login"):
        name = st.text_input("Your name", max_chars=100)
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in", type="primary")

    if submitted:
        if hmac.compare_digest(password, expected):
            st.session_state["authenticated"] = True
            st.session_state["user_name"] = name.strip() or "webapp-user"
            st.rerun()
        else:
            st.error("Incorrect password.")
    return False


def check_permission(permission: str, course: str | None = None) -> bool:
    """Check if the current webapp user has a specific permission.

    Webapp users are treated as having the "viewer" role by default unless
    RBAC is configured in attendance.yaml.

    Args:
        permission: Permission to check (e.g., "view_audit_log", "edit_attendance").
        course: Optional course name for per-course access control.

    Returns:
        True if the user has the permission, False otherwise.
    """
    cfg = _load_config()
    user_name = st.session_state.get("user_name", "webapp-user")

    from attendance.roles import ROLE_PERMISSIONS, allowlist_configured

    view_permissions = {"view_attendance", "view_audit_log"}

    rbac_cfg = cfg.get("attendance", {}).get("rbac", {})
    webapp_users = rbac_cfg.get("webapp_users", {}) if isinstance(rbac_cfg, dict) else {}
    if not isinstance(webapp_users, dict):
        webapp_users = {}

    role = webapp_users.get(user_name)

    if not allowlist_configured(cfg) or role is None:
        # Default-deny for anything destructive: without an explicit role
        # mapping the webapp user gets read-only access at most.
        if permission in view_permissions:
            return True
        st.error(
            f"You don't have permission to '{permission}'. "
            "Ask an admin to grant you a role in attendance.yaml "
            "(attendance.rbac.webapp_users)."
        )
        return False

    return permission in ROLE_PERMISSIONS.get(role, set())
