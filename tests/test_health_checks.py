"""Tests for T9: Secret rotation health check at startup."""
from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from attendance.health_checks import check_secrets, find_secret_issues, is_placeholder
from attendance.bot import app as bot_app
from tests.botutil import patch_bot


# ─── is_placeholder tests ─────────────────────────────────────────────────────

def test_is_placeholder_empty():
    assert is_placeholder("") is True
    assert is_placeholder("   ") is True
    assert is_placeholder(None) is False


def test_is_placeholder_angle_brackets():
    assert is_placeholder("<your-crash-sheet-id>") is True
    assert is_placeholder("<your-gemini-api-key>") is True
    assert is_placeholder("<optional-sheet-id>") is True


def test_is_placeholder_substrings():
    assert is_placeholder("your-service-account-key.json") is True
    assert is_placeholder("YOUR_API_KEY_HERE") is True
    assert is_placeholder("placeholder_id") is True
    assert is_placeholder("changeme") is True
    assert is_placeholder("dummy_value") is True


def test_is_placeholder_real_values():
    assert is_placeholder("1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms") is False
    assert is_placeholder("AIzaSyD-738947238947289") is False
    assert is_placeholder("production_sheet_2026") is False


# ─── find_secret_issues tests ─────────────────────────────────────────────────

def test_missing_sheet_id_detected():
    cfg = {
        "attendance": {
            "courses": {
                "crash": {"sheet_id_env": "CRASH_SHEET_ID"},
            },
            "ocr": {"provider": "mock"},
        }
    }
    env = {}  # CRASH_SHEET_ID not in env
    issues = find_secret_issues(cfg, env)
    assert len(issues) == 1
    assert issues[0]["var"] == "CRASH_SHEET_ID"
    assert issues[0]["issue"] == "missing"


def test_empty_sheet_id_detected():
    cfg = {
        "attendance": {
            "courses": {
                "main": {"sheet_id_env": "MAIN_SHEET_ID"},
            },
            "ocr": {"provider": "mock"},
        }
    }
    env = {"MAIN_SHEET_ID": "   "}
    issues = find_secret_issues(cfg, env)
    assert len(issues) == 1
    assert issues[0]["var"] == "MAIN_SHEET_ID"
    assert issues[0]["issue"] == "empty"


def test_placeholder_sheet_id_detected():
    cfg = {
        "attendance": {
            "courses": {
                "g10": {"sheet_id_env": "G10_SHEET_ID"},
            },
            "ocr": {"provider": "mock"},
        }
    }
    env = {"G10_SHEET_ID": "<your-g10-sheet-id>"}
    issues = find_secret_issues(cfg, env)
    assert len(issues) == 1
    assert issues[0]["var"] == "G10_SHEET_ID"
    assert issues[0]["issue"] == "placeholder"


def test_gemini_api_key_check():
    cfg = {
        "attendance": {
            "courses": {},
            "ocr": {"provider": "gemini"},
        }
    }
    # Missing
    issues = find_secret_issues(cfg, {})
    assert any(i["var"] == "GEMINI_API_KEY" and i["issue"] == "missing" for i in issues)

    # Placeholder
    issues = find_secret_issues(cfg, {"GEMINI_API_KEY": "<your-gemini-api-key>"})
    assert any(i["var"] == "GEMINI_API_KEY" and i["issue"] == "placeholder" for i in issues)

    # Valid
    issues = find_secret_issues(cfg, {"GEMINI_API_KEY": "AIzaSyValidKey123"})
    assert not any(i["var"] == "GEMINI_API_KEY" for i in issues)


def test_credentials_file_not_found(tmp_path):
    non_existent = str(tmp_path / "missing_sa.json")
    cfg = {
        "core": {"auth": {"credentials_env": "GOOGLE_APPLICATION_CREDENTIALS"}},
        "attendance": {"courses": {}, "ocr": {"provider": "mock"}},
    }
    env = {"GOOGLE_APPLICATION_CREDENTIALS": non_existent}
    issues = find_secret_issues(cfg, env)
    assert any(i["var"] == "GOOGLE_APPLICATION_CREDENTIALS" and i["issue"] == "file_not_found" for i in issues)


def test_all_valid_secrets_no_issues(tmp_path):
    sa_file = tmp_path / "valid_sa.json"
    sa_file.write_text("{}", encoding="utf-8")

    cfg = {
        "core": {"auth": {"credentials_env": "GOOGLE_APPLICATION_CREDENTIALS"}},
        "attendance": {
            "courses": {
                "crash": {"sheet_id_env": "CRASH_SHEET_ID"},
                "main": {"sheet_id_env": "MAIN_SHEET_ID"},
            },
            "ocr": {"provider": "gemini"},
        },
    }
    env = {
        "CRASH_SHEET_ID": "1234567890abcdef",
        "MAIN_SHEET_ID": "0987654321fedcba",
        "GEMINI_API_KEY": "AIzaSyRealKey456",
        "GOOGLE_APPLICATION_CREDENTIALS": str(sa_file),
    }
    issues = find_secret_issues(cfg, env)
    assert len(issues) == 0


# ─── check_secrets logging & stdout alerts ─────────────────────────────────────

def test_check_secrets_logs_and_prints(capsys, caplog):
    cfg = {
        "attendance": {
            "courses": {
                "g9": {"sheet_id_env": "G9_SHEET_ID"},
            },
            "ocr": {"provider": "mock"},
        }
    }
    env = {"G9_SHEET_ID": "<your-g9-sheet-id>"}

    with caplog.at_level(logging.WARNING):
        messages = check_secrets(cfg, env, log_alerts=True, print_alerts=True)

    assert len(messages) == 1
    assert "G9_SHEET_ID" in messages[0]

    # Verify stdout alert
    captured = capsys.readouterr()
    assert "[HEALTH CHECK WARNING]" in captured.out
    assert "G9_SHEET_ID" in captured.out

    # Verify log output
    assert any("secret_health_check_failed" in r.message or "G9_SHEET_ID" in str(r.__dict__) for r in caplog.records)


# ─── Bot startup integration ─────────────────────────────────────────────────

def test_main_calls_secret_check_at_startup(monkeypatch, caplog, capsys):
    from telegram.ext import Application
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TEST-TOKEN-NOT-REAL")
    monkeypatch.setenv("CRASH_SHEET_ID", "<your-crash-sheet-id>")

    dummy_cfg = {
        "attendance": {
            "courses": {
                "crash": {"sheet_id_env": "CRASH_SHEET_ID", "layout": "single"},
            },
            "ocr": {"provider": "mock"},
            "authorized": {"telegram_ids": [123]},
        }
    }
    patch_bot(monkeypatch, "_load_cfg", lambda *a, **k: dummy_cfg)
    monkeypatch.setattr(Application, "run_polling", lambda self, *a, **k: None)

    with caplog.at_level(logging.WARNING):
        bot_app.main()

    # Check alert was printed to stdout
    captured = capsys.readouterr()
    assert "[HEALTH CHECK WARNING]" in captured.out
    assert "CRASH_SHEET_ID" in captured.out

    # Check warning was logged
    records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("secret_health_check_failed" in r.message for r in records)
