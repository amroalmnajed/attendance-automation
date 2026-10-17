"""tests/test_webapp_viewer.py — Tests for webapp attendance viewer page (W6).

Tests the viewer page configuration loading and report functions.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from webapp.pages.viewer import _load_config


def test_load_config_returns_dict():
    """Test that _load_config returns a dict."""
    cfg = _load_config()
    assert isinstance(cfg, dict)


def test_load_config_has_attendance_section():
    """Test that the loaded config has an attendance section."""
    cfg = _load_config()
    assert "attendance" in cfg


def test_count_marks_function_exists():
    """Test that count_marks function exists and is callable."""
    from attendance.history import count_marks

    assert callable(count_marks)


def test_count_marks_basic():
    """Test count_marks with basic values."""
    from attendance.history import count_marks

    values = ["Present", "Late", "Absent", "Present", "present"]
    counts = count_marks(values)

    assert counts["present"] == 3
    assert counts["late"] == 1
    assert counts["absent"] == 1


def test_count_marks_case_insensitive():
    """Test that count_marks is case-insensitive."""
    from attendance.history import count_marks

    values = ["PRESENT", "late", "ABSENT"]
    counts = count_marks(values)

    assert counts["present"] == 1
    assert counts["late"] == 1
    assert counts["absent"] == 1


def test_count_marks_empty_values():
    """Test count_marks with empty/invalid values."""
    from attendance.history import count_marks

    values = ["", "Not complete", "Present"]
    counts = count_marks(values)

    assert counts["present"] == 1
    assert counts["late"] == 0
    assert counts["absent"] == 0


def test_entries_function_exists():
    """Test that entries function exists and is callable."""
    from attendance.history import entries

    assert callable(entries)


def test_generate_session_report_function_exists():
    """Test that generate_session_report function exists and is callable."""
    from attendance.reporter import generate_session_report

    assert callable(generate_session_report)


def test_generate_streaks_report_function_exists():
    """Test that generate_streaks_report function exists and is callable."""
    from attendance.reporter import generate_streaks_report

    assert callable(generate_streaks_report)


def test_session_resolve_function_exists():
    """Test that session_resolve function exists and is callable."""
    from attendance.session_resolver import resolve

    assert callable(resolve)
