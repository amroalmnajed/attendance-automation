"""tests/test_webapp_auth.py — default-deny behavior for check_permission."""
from __future__ import annotations

import pytest


@pytest.fixture
def webapp_user(monkeypatch):
    """Set a webapp user name in session state and silence st.error."""
    import types

    import streamlit as st

    fake_state = types.SimpleNamespace(
        get=lambda k, d=None: "alice" if k == "user_name" else d
    )
    monkeypatch.setattr(st, "session_state", fake_state)
    monkeypatch.setattr(st, "error", lambda *a, **k: None)
    return "alice"


def _cfg_with_rbac(webapp_users=None):
    rbac = {
        "enabled": True,
        "roles": {"editor": {"telegram_ids": [123]}},
    }
    if webapp_users is not None:
        rbac["webapp_users"] = webapp_users
    return {"attendance": {"rbac": rbac}}


def test_unconfigured_user_cannot_edit(webapp_user, monkeypatch):
    from webapp import auth

    monkeypatch.setattr(auth, "_load_config", lambda: {})
    assert auth.check_permission("view_attendance") is True
    assert auth.check_permission("view_audit_log") is True
    assert auth.check_permission("edit_attendance") is False


def test_unconfigured_user_cannot_manage(webapp_user, monkeypatch):
    from webapp import auth

    monkeypatch.setattr(auth, "_load_config", lambda: {})
    assert auth.check_permission("manage_roster") is False
    assert auth.check_permission("manage_users") is False


def test_editor_role_can_edit(webapp_user, monkeypatch):
    from webapp import auth

    monkeypatch.setattr(
        auth,
        "_load_config",
        lambda: _cfg_with_rbac(webapp_users={"alice": "editor"}),
    )
    assert auth.check_permission("edit_attendance") is True
    assert auth.check_permission("view_attendance") is True
    assert auth.check_permission("manage_users") is False


def test_unmapped_user_denied_write_when_rbac_configured(webapp_user, monkeypatch):
    from webapp import auth

    monkeypatch.setattr(
        auth,
        "_load_config",
        lambda: _cfg_with_rbac(webapp_users={"bob": "admin"}),
    )
    assert auth.check_permission("view_attendance") is True
    assert auth.check_permission("edit_attendance") is False
