"""Tests for startup secret & health checks (T9 / FreeAI update)."""
import pytest
from attendance.health_checks import find_secret_issues, check_secrets


def test_freeai_api_key_missing():
    cfg = {"attendance": {"ocr": {"provider": "freeai"}}}
    env = {}
    issues = find_secret_issues(cfg, env)
    assert any(i["var"] == "FREEAI_API_KEY" and i["issue"] == "missing" for i in issues)


def test_freeai_api_key_empty():
    cfg = {"attendance": {"ocr": {"provider": "freeai"}}}
    env = {"FREEAI_API_KEY": "   "}
    issues = find_secret_issues(cfg, env)
    assert any(i["var"] == "FREEAI_API_KEY" and i["issue"] == "empty" for i in issues)


def test_freeai_api_key_placeholder():
    cfg = {"attendance": {"ocr": {"provider": "freeai"}}}
    env = {"FREEAI_API_KEY": "<your-freeai-api-key>"}
    issues = find_secret_issues(cfg, env)
    assert any(i["var"] == "FREEAI_API_KEY" and i["issue"] == "placeholder" for i in issues)


def test_freeai_api_key_valid():
    cfg = {"attendance": {"ocr": {"provider": "freeai"}}}
    env = {"FREEAI_API_KEY": "sk-real-valid-key-12345"}
    issues = find_secret_issues(cfg, env)
    assert not any(i["var"] == "FREEAI_API_KEY" for i in issues)


def test_gemini_api_key_when_provider_gemini():
    cfg = {"attendance": {"ocr": {"provider": "gemini"}}}
    env = {}
    issues = find_secret_issues(cfg, env)
    assert any(i["var"] == "GEMINI_API_KEY" and i["issue"] == "missing" for i in issues)
