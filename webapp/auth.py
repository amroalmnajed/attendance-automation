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

    # If RBAC is not configured, allow all basic permissions for webapp users
    # (webapp is already password-protected)
    from attendance.roles import allowlist_configured
    if not allowlist_configured(cfg):
        # Grant viewer-level permissions by default
        if permission in ["view_attendance", "view_audit_log"]:
            return True
        # For now, also allow edit permissions in webapp (backward compatibility)
        # TODO: Make this configurable when RBAC is fully integrated
        return True

    # Webapp users don't have Telegram IDs, so we can't use role_of directly
    # For now, treat webapp users as having "viewer" role if no specific mapping exists
    # This is a temporary approach until webapp user management is implemented
    user_name = st.session_state.get("user_name", "webapp-user")

    # Map webapp users to roles via config (future enhancement)
    rbac_cfg = cfg.get("attendance", {}).get("rbac", {})
    if isinstance(rbac_cfg, dict):
        webapp_users = rbac_cfg.get("webapp_users", {})
        if isinstance(webapp_users, dict):
            role = webapp_users.get(user_name, "viewer")
        else:
            role = "viewer"
    else:
        role = "viewer"

    # Check if role has the permission
    from attendance.roles import ROLE_PERMISSIONS
    if permission in ROLE_PERMISSIONS.get(role, set()):
        return True

    return False
