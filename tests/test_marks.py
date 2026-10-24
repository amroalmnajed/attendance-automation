"""Feature 1 — mixed attendance marks in text / paste mode (attendance.marks)."""
from __future__ import annotations

import pytest

from attendance import marks
from attendance.marks import (
    KIND_DEFAULT, KIND_MARKED, KIND_UNRECOGNIZED,
    format_preview, has_issues, parse_line, parse_lines, parse_mark, split_names_and_marks,
)


@pytest.mark.parametrize("line, name, canonical", [
    ("Ahmed Ali | L", "Ahmed Ali", "Late"),
    ("Sara - absent", "Sara", "Absent"),
    ("Omar, Late", "Omar", "Late"),
    ("Mona: P", "Mona", "Present"),
    ("Karim , A", "Karim", "Absent"),
    ("Omar | NC", "Omar", "Not complete"),
    ("Omar | Not complete", "Omar", "Not complete"),
    ("Sara-absent", "Sara", "Absent"),            # unspaced dash is fine when the tail is a known mark
    ("Mona|✓", "Mona", "Present"),
])
def test_english_marks_are_mapped(line, name, canonical):
    p = parse_line(line)
    assert (p.name, p.canonical, p.kind) == (name, canonical, KIND_MARKED)


@pytest.mark.parametrize("token, canonical", [
    ("p", "Present"), ("PRESENT", "Present"), ("Late", "Late"), ("l", "Late"),
    ("A", "Absent"), ("ABSENT", "Absent"), ("nc", "Not complete"), ("NOT COMPLETE", "Not complete"),
])
def test_marks_are_case_insensitive(token, canonical):
    assert parse_mark(token) == canonical


@pytest.mark.parametrize("line, name, canonical", [
    ("منى | غائب", "منى", "Absent"),
    ("سارة، متأخرة", "سارة", "Late"),                # Arabic comma + feminine/hamza variants
    ("علي - حاضر", "علي", "Present"),
    ("هالة - غير مكتمل", "هالة", "Not complete"),    # multi-word Arabic mark
    ("يوسف: غايب", "يوسف", "Absent"),                # colloquial spelling
])
def test_arabic_marks_are_mapped(line, name, canonical):
    p = parse_line(line)
    assert (p.name, p.canonical, p.kind) == (name, canonical, KIND_MARKED)


def test_line_without_mark_defaults_to_present():
    p = parse_line("Youssef Hassan")
    assert (p.name, p.canonical, p.kind, p.mark_raw) == ("Youssef Hassan", "Present", KIND_DEFAULT, None)
    assert p.effective_mark == "Present"


def test_hyphenated_names_are_not_split():
    p = parse_line("Abdel-Aziz Hassan")
    assert p.name == "Abdel-Aziz Hassan" and p.kind == KIND_DEFAULT


def test_trailing_separator_with_nothing_after_is_just_the_name():
    p = parse_line("Sara |")
    assert (p.name, p.canonical, p.kind) == ("Sara", "Present", KIND_DEFAULT)


@pytest.mark.parametrize("line", ["Ali - abcent", "Sara | ??", "Omar, lte"])
def test_unrecognised_mark_is_never_guessed(line):
    p = parse_line(line)
    assert p.kind == KIND_UNRECOGNIZED and p.canonical is None
    # the raw token is passed on, so the workflow's mark gate sends the row to review
    assert p.effective_mark == p.mark_raw
    assert normalize_is_none(p.effective_mark)


def normalize_is_none(mark: str) -> bool:
    from attendance.normalizer import normalize_attendance_mark
    return normalize_attendance_mark(mark) is None


def test_long_tail_after_a_separator_is_treated_as_part_of_the_name():
    p = parse_line("Ahmed Ali - Hassan Mahmoud Saleh")
    assert p.kind == KIND_DEFAULT and p.name == "Ahmed Ali - Hassan Mahmoud Saleh"


def test_blank_lines_are_dropped_and_indices_are_contiguous():
    parsed = parse_lines(["Ahmed", "", "   ", "Sara | A"])
    assert [(p.index, p.name) for p in parsed] == [(1, "Ahmed"), (2, "Sara")]


def test_split_names_and_marks_are_parallel_lists():
    parsed = parse_lines(["Ahmed Ali | L", "Sara", "Omar | zzz"])
    names, mks = split_names_and_marks(parsed)
    assert names == ["Ahmed Ali", "Sara", "Omar"]
    assert mks == ["Late", "Present", "zzz"]
    assert has_issues(parsed)


def test_preview_shows_each_line_and_flags_problems():
    text = format_preview(parse_lines(["Ahmed Ali | L", "Sara", "Omar | zzz"]))
    assert "1. Ahmed Ali → Late" in text
    assert "2. Sara → Present (no mark → Present)" in text
    assert "3. Omar → ⚠️ “zzz” not recognised (will go to review)" in text
    assert "Needs review 1" in text


def test_preview_truncates_long_lists():
    text = format_preview(parse_lines([f"Student {i}" for i in range(60)]), max_lines=10)
    assert "… and 50 more" in text and "Student 59" not in text


def test_module_does_not_change_the_ocr_mark_map():
    """Text-mode extras live in marks.py; OCR's mark map must stay as it was."""
    from attendance.normalizer import normalize_attendance_mark
    assert normalize_attendance_mark("غائب") is None          # text-mode only
    assert normalize_attendance_mark("L") == "Late"           # unchanged
