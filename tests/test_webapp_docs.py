"""tests/test_webapp_docs.py — Tests for webapp documentation (W10).

Tests that documentation files exist and are readable.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def test_readme_exists():
    """Test that webapp/README.md exists."""
    readme_path = Path("webapp/README.md")
    assert readme_path.exists()


def test_readme_is_readable():
    """Test that webapp/README.md is readable."""
    readme_path = Path("webapp/README.md")
    content = readme_path.read_text(encoding="utf-8")
    assert len(content) > 0
    assert "Attendance Webapp" in content


def test_deploy_exists():
    """Test that webapp/DEPLOY.md exists."""
    deploy_path = Path("webapp/DEPLOY.md")
    assert deploy_path.exists()


def test_deploy_is_readable():
    """Test that webapp/DEPLOY.md is readable."""
    deploy_path = Path("webapp/DEPLOY.md")
    content = deploy_path.read_text(encoding="utf-8")
    assert len(content) > 0
    assert "Deployment Guide" in content


def test_secrets_example_exists():
    """Test that webapp/.streamlit/secrets.toml.example exists."""
    secrets_path = Path("webapp/.streamlit/secrets.toml.example")
    assert secrets_path.exists()


def test_secrets_example_is_readable():
    """Test that webapp/.streamlit/secrets.toml.example is readable."""
    secrets_path = Path("webapp/.streamlit/secrets.toml.example")
    content = secrets_path.read_text(encoding="utf-8")
    assert len(content) > 0
    assert "WEBAPP_PASSWORD" in content
    assert "GOOGLE_CREDENTIALS_B64" in content
    assert "GEMINI_API_KEY" in content



