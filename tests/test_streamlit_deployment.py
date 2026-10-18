"""Regression tests for Streamlit Community Cloud runtime support."""
from __future__ import annotations

import base64
import json

import pytest


def test_base64_service_account_credentials_are_supported(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Cloud credentials must not depend on a service_account.json file."""
    from core import auth

    info = {"type": "service_account", "project_id": "test-project"}
    encoded = base64.b64encode(json.dumps(info).encode("utf-8")).decode("ascii")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setenv("GOOGLE_CREDENTIALS_B64", encoded)
    sentinel = object()

    def fake_from_info(received, *, scopes):
        assert received == info
        assert scopes == auth.SCOPES
        return sentinel

    monkeypatch.setattr(auth.service_account.Credentials, "from_service_account_info", fake_from_info)
    assert auth.get_credentials() is sentinel


def test_invalid_base64_credentials_fail_with_a_clear_error(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from core import auth

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setenv("GOOGLE_CREDENTIALS_B64", "not-base64")

    with pytest.raises(RuntimeError, match="GOOGLE_CREDENTIALS_B64"):
        auth.get_credentials()
