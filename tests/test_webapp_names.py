"""tests/test_webapp_names.py — Tests for webapp paste-names page (W3).

Tests the paste-names parsing and preview logic.
"""
from __future__ import annotations

import pytest

from attendance.marks import format_preview, has_issues, parse_lines, split_names_and_marks


def test_parse_basic_names():
    """Test parsing basic name-only lines."""
    lines = ["Ahmed Ali", "Sara Mohamed", "Omar Hassan"]
    parsed = parse_lines(lines)

    assert len(parsed) == 3
    assert parsed[0].name == "Ahmed Ali"
    assert parsed[1].name == "Sara Mohamed"
    assert parsed[2].name == "Omar Hassan"


def test_parse_names_with_marks():
    """Test parsing names with explicit marks."""
    lines = ["Ahmed Ali | L", "Sara | absent", "Omar | Not complete"]
    parsed = parse_lines(lines)

    assert len(parsed) == 3
    assert parsed[0].name == "Ahmed Ali"
    assert parsed[0].canonical == "Late"
    assert parsed[1].name == "Sara"
    assert parsed[1].canonical == "Absent"
    assert parsed[2].name == "Omar"
    assert parsed[2].canonical == "Not complete"


def test_parse_default_marks():
    """Test that names without marks default to Present."""
    lines = ["Ahmed Ali", "Sara"]
    parsed = parse_lines(lines)

    assert len(parsed) == 2
    assert parsed[0].canonical == "Present"
    assert parsed[1].canonical == "Present"


def test_parse_unrecognized_mark():
    """Test that unrecognized marks are flagged."""
    lines = ["Ahmed Ali | unknown"]
    parsed = parse_lines(lines)

    assert len(parsed) == 1
    assert parsed[0].kind == "unrecognized"


def test_split_names_and_marks():
    """Test splitting parsed lines into names and marks lists."""
    lines = ["Ahmed Ali | L", "Sara", "Omar | absent"]
    parsed = parse_lines(lines)
    names, marks = split_names_and_marks(parsed)

    assert names == ["Ahmed Ali", "Sara", "Omar"]
    assert marks == ["Late", "Present", "Absent"]


def test_has_issues():
    """Test has_issues detection."""
    # No issues
    lines = ["Ahmed Ali | L", "Sara"]
    parsed = parse_lines(lines)
    assert has_issues(parsed) is False

    # Has issues
    lines = ["Ahmed Ali | unknown"]
    parsed = parse_lines(lines)
    assert has_issues(parsed) is True


def test_format_preview_basic():
    """Test format_preview with basic input."""
    lines = ["Ahmed Ali", "Sara | L"]
    parsed = parse_lines(lines)
    preview = format_preview(parsed, markdown=False)

    assert "How I read your list" in preview
    assert "Ahmed Ali" in preview
    assert "Present" in preview
    assert "Late" in preview


def test_format_preview_with_issues():
    """Test format_preview shows unrecognized marks."""
    lines = ["Ahmed Ali | unknown"]
    parsed = parse_lines(lines)
    preview = format_preview(parsed, markdown=False)

    assert "not recognised" in preview
    assert "⚠️" in preview


def test_format_preview_max_lines():
    """Test format_preview truncates at max_lines."""
    lines = [f"Student {i}" for i in range(50)]
    parsed = parse_lines(lines)
    preview = format_preview(parsed, markdown=False, max_lines=10)

    assert "and 40 more" in preview


def test_parse_empty_lines():
    """Test that empty lines are dropped."""
    lines = ["Ahmed Ali", "", "  ", "Sara"]
    parsed = parse_lines(lines)

    assert len(parsed) == 2
    assert parsed[0].name == "Ahmed Ali"
    assert parsed[1].name == "Sara"


def test_parse_whitespace_handling():
    """Test that whitespace is handled correctly."""
    lines = ["  Ahmed Ali  ", "Sara |  L  "]
    parsed = parse_lines(lines)

    assert parsed[0].name == "Ahmed Ali"
    assert parsed[1].name == "Sara"
    assert parsed[1].canonical == "Late"
