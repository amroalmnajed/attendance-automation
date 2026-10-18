"""Small shared-password gate for the private Streamlit app."""
from __future__ import annotations

import hmac
import os

import streamlit as st


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
