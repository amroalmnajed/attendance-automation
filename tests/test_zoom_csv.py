"""tests/test_zoom_csv.py — Tests for Zoom CSV parsing and attendance calculation."""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

from attendance.zoom_csv import (
    archive_csv,
    calculate_attendance,
    clean_zoom_name,
    determine_session_type,
    extract_date_from_filename,
    extract_meeting_id_from_filename,
    find_teacher,
    parse_zoom_csv,
    save_meeting_id_mapping,
    validate_csv_structure,
    write_decision_log,
)


# ─── CSV parsing tests ────────────────────────────────────────────────────────

def test_parse_zoom_csv_basic():
    """Test basic CSV parsing with sample data."""
    csv_content = """Name (original name),Join time,Leave time,Duration (minutes),Guest
Yaman,09/14/2026 10:00:00 AM,09/14/2026 11:30:00 AM,90,No
Ahmed Ali,09/14/2026 10:05:00 AM,09/14/2026 11:00:00 AM,55,No
Sara Mohamed,09/14/2026 10:10:00 AM,09/14/2026 10:30:00 AM,20,No
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, encoding="utf-8") as f:
        f.write(csv_content)
        csv_path = f.name

    try:
        participants = parse_zoom_csv(csv_path)
        assert len(participants) == 3
        assert participants[0]["name"] == "Yaman"
        assert participants[0]["guest"] is False
        assert participants[0]["duration_minutes"] == 90
        assert participants[1]["name"] == "Ahmed Ali"
        assert participants[2]["name"] == "Sara Mohamed"
    finally:
        Path(csv_path).unlink()


def test_parse_zoom_csv_with_guest():
    """Test CSV parsing with guest participants."""
    csv_content = """Name (original name),Join time,Leave time,Duration (minutes),Guest
Yaman,09/14/2026 10:00:00 AM,09/14/2026 11:30:00 AM,90,No
Guest User,09/14/2026 10:15:00 AM,09/14/2026 10:45:00 AM,30,Yes
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, encoding="utf-8") as f:
        f.write(csv_content)
        csv_path = f.name

    try:
        participants = parse_zoom_csv(csv_path)
        assert len(participants) == 2
        assert participants[0]["guest"] is False
        assert participants[1]["guest"] is True
        assert participants[1]["name"] == "Guest User"
    finally:
        Path(csv_path).unlink()


def test_parse_zoom_csv_with_utf8_bom():
    """Test CSV parsing with UTF-8 BOM."""
    csv_content = "\ufeffName (original name),Join time,Leave time,Duration (minutes),Guest\nYaman,09/14/2026 10:00:00 AM,09/14/2026 11:30:00 AM,90,No\n"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, encoding="utf-8-sig") as f:
        f.write(csv_content)
        csv_path = f.name

    try:
        participants = parse_zoom_csv(csv_path)
        assert len(participants) == 1
        assert participants[0]["name"] == "Yaman"
    finally:
        Path(csv_path).unlink()


def test_validate_csv_structure():
    """Test CSV structure validation."""
    # Valid headers
    assert validate_csv_structure(["Name (original name)", "Join time", "Leave time", "Duration (minutes)"]) is True
    assert validate_csv_structure(["name", "join time", "leave time", "duration"]) is True  # Case-insensitive

    # Missing required columns
    assert validate_csv_structure(["Name", "Join time"]) is False
    assert validate_csv_structure(["Name", "Join time", "Leave time"]) is False


def test_parse_zoom_csv_duplicates():
    """Test handling duplicate entries (same student multiple times)."""
    csv_content = """Name (original name),Join time,Leave time,Duration (minutes),Guest
Omar Wael,09/14/2026 10:00:00 AM,09/14/2026 10:30:00 AM,30,No
Omar Wael,09/14/2026 10:35:00 AM,09/14/2026 11:00:00 AM,25,No
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, encoding="utf-8") as f:
        f.write(csv_content)
        csv_path = f.name

    try:
        participants = parse_zoom_csv(csv_path)
        assert len(participants) == 2  # Both entries parsed
        assert all(p["name"] == "Omar Wael" for p in participants)
    finally:
        Path(csv_path).unlink()


# ─── Teacher detection tests ───────────────────────────────────────────────────

def test_find_teacher_exact_match():
    """Test teacher detection with exact name match."""
    participants = [
        {"name": "Yaman", "join_time": datetime(2026, 9, 14, 10, 0), "leave_time": datetime(2026, 9, 14, 11, 30), "duration_minutes": 90, "guest": False},
        {"name": "Ahmed Ali", "join_time": datetime(2026, 9, 14, 10, 5), "leave_time": datetime(2026, 9, 14, 11, 0), "duration_minutes": 55, "guest": False},
    ]
    teacher = find_teacher(participants, "Yaman", 50)
    assert teacher is not None
    assert teacher["name"] == "Yaman"
    assert teacher["join_time"] == datetime(2026, 9, 14, 10, 0)
    assert teacher["leave_time"] == datetime(2026, 9, 14, 11, 30)


def test_find_teacher_fuzzy_match():
    """Test teacher detection with fuzzy matching."""
    participants = [
        {"name": "Yaman (iPad)", "join_time": datetime(2026, 9, 14, 10, 0), "leave_time": datetime(2026, 9, 14, 11, 30), "duration_minutes": 90, "guest": False},
        {"name": "Ahmed Ali", "join_time": datetime(2026, 9, 14, 10, 5), "leave_time": datetime(2026, 9, 14, 11, 0), "duration_minutes": 55, "guest": False},
    ]
    teacher = find_teacher(participants, "Yaman", 50)
    assert teacher is not None
    assert teacher["name"] == "Yaman"


def test_find_teacher_rejoin_merge():
    """Test teacher rejoin merging (earliest join, latest leave)."""
    participants = [
        {"name": "Yaman", "join_time": datetime(2026, 9, 14, 10, 0), "leave_time": datetime(2026, 9, 14, 10, 30), "duration_minutes": 30, "guest": False},
        {"name": "Yaman (iPhone)", "join_time": datetime(2026, 9, 14, 10, 45), "leave_time": datetime(2026, 9, 14, 11, 30), "duration_minutes": 45, "guest": False},
    ]
    teacher = find_teacher(participants, "Yaman", 50)
    assert teacher is not None
    assert teacher["join_time"] == datetime(2026, 9, 14, 10, 0)  # Earliest
    assert teacher["leave_time"] == datetime(2026, 9, 14, 11, 30)  # Latest
    assert teacher["duration_minutes"] == 75  # Sum


def test_find_teacher_not_found():
    """Test teacher not found case."""
    participants = [
        {"name": "Ahmed Ali", "join_time": datetime(2026, 9, 14, 10, 5), "leave_time": datetime(2026, 9, 14, 11, 0), "duration_minutes": 55, "guest": False},
    ]
    teacher = find_teacher(participants, "Yaman", 50)
    assert teacher is None


# ─── Attendance calculation tests ───────────────────────────────────────────────

def test_calculate_attendance_present():
    """Test Present mark (on time, sufficient duration)."""
    participants = [
        {"name": "Ahmed Ali", "join_time": datetime(2026, 9, 14, 10, 5), "leave_time": datetime(2026, 9, 14, 11, 15), "duration_minutes": 70, "guest": False},
    ]
    config = {
        "teacher_join_buffer_minutes": 5,
        "late_threshold_minutes": 15,
        "min_duration_minutes": 45,
        "early_leave_threshold_minutes": 20,
    }
    meeting_start = datetime(2026, 9, 14, 10, 0)
    meeting_end = datetime(2026, 9, 14, 11, 30)

    results = calculate_attendance(participants, config, meeting_start, meeting_end)
    assert results is not None
    assert len(results) == 1
    name, mark, detail = results[0]
    assert name == "Ahmed Ali"
    assert mark == "Present"
    assert detail["rule_fired"] == "on_time"


def test_calculate_attendance_late():
    """Test Late mark (joined after threshold)."""
    participants = [
        {"name": "Ahmed Ali", "join_time": datetime(2026, 9, 14, 10, 21), "leave_time": datetime(2026, 9, 14, 11, 15), "duration_minutes": 54, "guest": False},
    ]
    config = {
        "teacher_join_buffer_minutes": 5,
        "late_threshold_minutes": 15,
        "min_duration_minutes": 45,
        "early_leave_threshold_minutes": 20,
    }
    meeting_start = datetime(2026, 9, 14, 10, 0)
    meeting_end = datetime(2026, 9, 14, 11, 30)

    results = calculate_attendance(participants, config, meeting_start, meeting_end)
    assert results is not None
    assert len(results) == 1
    name, mark, detail = results[0]
    assert mark == "Late"
    assert detail["rule_fired"] == "late_arrival"


def test_calculate_attendance_absent():
    """Test Absent mark (insufficient duration)."""
    participants = [
        {"name": "Ahmed Ali", "join_time": datetime(2026, 9, 14, 10, 5), "leave_time": datetime(2026, 9, 14, 11, 15), "duration_minutes": 25, "guest": False},
    ]
    config = {
        "teacher_join_buffer_minutes": 5,
        "late_threshold_minutes": 15,
        "min_duration_minutes": 45,
        "early_leave_threshold_minutes": 20,
    }
    meeting_start = datetime(2026, 9, 14, 10, 0)
    meeting_end = datetime(2026, 9, 14, 11, 30)

    results = calculate_attendance(participants, config, meeting_start, meeting_end)
    assert results is not None
    assert len(results) == 1
    name, mark, detail = results[0]
    assert mark == "Absent"
    assert detail["rule_fired"] == "duration_too_short"


def test_calculate_attendance_not_complete():
    """Test Not Complete mark (left too early before teacher)."""
    participants = [
        {"name": "Ahmed Ali", "join_time": datetime(2026, 9, 14, 10, 5), "leave_time": datetime(2026, 9, 14, 10, 50), "duration_minutes": 45, "guest": False},
    ]
    config = {
        "teacher_join_buffer_minutes": 5,
        "late_threshold_minutes": 15,
        "min_duration_minutes": 45,
        "early_leave_threshold_minutes": 20,
    }
    meeting_start = datetime(2026, 9, 14, 10, 0)
    meeting_end = datetime(2026, 9, 14, 11, 30)

    results = calculate_attendance(participants, config, meeting_start, meeting_end)
    assert results is not None
    assert len(results) == 1
    name, mark, detail = results[0]
    assert mark == "Not complete"
    assert detail["rule_fired"] == "early_leave"


def test_calculate_attendance_teacher_buffer_shifts_threshold():
    """Test that teacher_join_buffer_minutes visibly shifts Late threshold."""
    participants = [
        {"name": "Ahmed Ali", "join_time": datetime(2026, 9, 14, 10, 11), "leave_time": datetime(2026, 9, 14, 11, 15), "duration_minutes": 64, "guest": False},
    ]
    # With 5 min buffer, effective start is 10:05, so 10:10 is Late (5 min after)
    config_buffer = {
        "teacher_join_buffer_minutes": 5,
        "late_threshold_minutes": 5,
        "min_duration_minutes": 45,
        "early_leave_threshold_minutes": 20,
    }
    # With 0 min buffer, effective start is 10:00, so 10:10 is Late (10 min after)
    config_no_buffer = {
        "teacher_join_buffer_minutes": 0,
        "late_threshold_minutes": 5,
        "min_duration_minutes": 45,
        "early_leave_threshold_minutes": 20,
    }
    meeting_start = datetime(2026, 9, 14, 10, 0)
    meeting_end = datetime(2026, 9, 14, 11, 30)

    results_buffer = calculate_attendance(participants, config_buffer, meeting_start, meeting_end)
    results_no_buffer = calculate_attendance(participants, config_no_buffer, meeting_start, meeting_end)

    assert results_buffer is not None
    assert results_no_buffer is not None
    # With buffer: 10:10 - (10:00 + 5) = 5 min after threshold → Late
    assert results_buffer[0][1] == "Late"
    # Without buffer: 10:10 - 10:00 = 10 min after threshold → Late
    assert results_no_buffer[0][1] == "Late"


def test_calculate_attendance_duplicate_duration_summation():
    """Test that duplicate sessions sum durations."""
    participants = [
        {"name": "Ahmed Ali", "join_time": datetime(2026, 9, 14, 10, 0), "leave_time": datetime(2026, 9, 14, 10, 30), "duration_minutes": 30, "guest": False},
        {"name": "Ahmed Ali", "join_time": datetime(2026, 9, 14, 10, 35), "leave_time": datetime(2026, 9, 14, 11, 15), "duration_minutes": 40, "guest": False},
    ]
    config = {
        "teacher_join_buffer_minutes": 5,
        "late_threshold_minutes": 15,
        "min_duration_minutes": 45,
        "early_leave_threshold_minutes": 20,
    }
    meeting_start = datetime(2026, 9, 14, 10, 0)
    meeting_end = datetime(2026, 9, 14, 11, 30)

    results = calculate_attendance(participants, config, meeting_start, meeting_end)
    assert results is not None
    assert len(results) == 1
    name, mark, detail = results[0]
    assert detail["total_duration"] == 70  # 30 + 40
    assert mark == "Present"  # Total duration >= 45


def test_calculate_attendance_teacher_not_found():
    """Test that teacher not found returns None."""
    participants = [
        {"name": "Ahmed Ali", "join_time": datetime(2026, 9, 14, 10, 5), "leave_time": datetime(2026, 9, 14, 11, 0), "duration_minutes": 55, "guest": False},
    ]
    config = {}
    results = calculate_attendance(participants, config, None, None)
    assert results is None


# ─── Filename parsing tests ───────────────────────────────────────────────────

def test_extract_meeting_id_from_filename():
    """Test meeting ID extraction from filename."""
    assert extract_meeting_id_from_filename("participants_81795786552_2026_09_13.csv") == "81795786552"
    assert extract_meeting_id_from_filename("participants_123456789_2026_10_14.csv") == "123456789"
    assert extract_meeting_id_from_filename("invalid_filename.csv") is None
    assert extract_meeting_id_from_filename("participants_abc_2026_09_13.csv") is None


def test_extract_date_from_filename():
    """Test date extraction from filename."""
    date = extract_date_from_filename("participants_81795786552_2026_09_13.csv")
    assert date is not None
    assert date.year == 2026
    assert date.month == 9
    assert date.day == 13

    date = extract_date_from_filename("participants_123456789_2026_10_14.csv")
    assert date is not None
    assert date.month == 10
    assert date.day == 14

    assert extract_date_from_filename("invalid_filename.csv") is None


# ─── Session type detection tests ─────────────────────────────────────────────

def test_determine_session_type_unambiguous():
    """Test session type auto-detection when unambiguous."""
    cfg = {
        "attendance": {
            "courses": {
                "g10": {
                    "layout": "typed_pair",
                    "types": {
                        "odd": ["Saturday - Center", "Saturday - Online"],
                        "even": ["Tuesday - Center", "Tuesday - Online"],
                    },
                }
            }
        }
    }
    # Saturday has one Online variant
    date = datetime(2026, 9, 12)  # Saturday
    session_type = determine_session_type(date, "g10", cfg)
    assert session_type == "Saturday - Online"


def test_determine_session_type_hyphen_attached_day():
    """Test session type auto-detection with hyphen-attached strings like 'Sunday- Online'."""
    cfg = {
        "attendance": {
            "courses": {
                "g10": {
                    "layout": "typed_pair",
                    "types": {
                        "odd": ["Sunday- Center", "Sunday- Online"],
                    },
                }
            }
        }
    }
    date = datetime(2026, 9, 13)  # Sunday
    session_type = determine_session_type(date, "g10", cfg)
    assert session_type == "Sunday- Online"


def test_determine_session_type_ambiguous():
    """Test session type ambiguity when multiple Online variants match."""
    cfg = {
        "attendance": {
            "courses": {
                "g10": {
                    "layout": "typed_pair",
                    "types": {
                        "any": ["Saturday - Online", "Saturday - Online 2"],  # Multiple Online on same day
                    },
                }
            }
        }
    }
    date = datetime(2026, 9, 12)  # Saturday
    session_type = determine_session_type(date, "g10", cfg)
    assert session_type == "ambiguous"


def test_determine_session_type_no_online():
    """Test when no Online type matches the day."""
    cfg = {
        "attendance": {
            "courses": {
                "g10": {
                    "layout": "typed_pair",
                    "types": {
                        "odd": ["Saturday - Center"],  # Only Center, no Online
                    },
                }
            }
        }
    }
    date = datetime(2026, 9, 12)  # Saturday
    session_type = determine_session_type(date, "g10", cfg)
    assert session_type == ""


# ─── CSV archiving tests ─────────────────────────────────────────────────────

def test_archive_csv():
    """Test CSV archiving."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create source CSV
        source = Path(tmpdir) / "participants_123_2026_09_13.csv"
        source.write_text("Name,Join time\nYaman,10:00")

        archive_folder = Path(tmpdir) / "archive"
        archive_csv(str(source), str(archive_folder))

        assert not source.exists()  # Source moved
        archived = archive_folder / "participants_123_2026_09_13.csv"
        assert archived.exists()


# ─── Meeting ID mapping tests ─────────────────────────────────────────────────

def test_save_meeting_id_mapping():
    """Test saving meeting ID mapping to config."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "config.yaml"
        config_path.write_text("attendance:\n  zoom_csv:\n    meeting_id_mappings: {}")

        save_meeting_id_mapping("123456789", "g10", str(config_path))

        with open(config_path, "r") as f:
            cfg = yaml.safe_load(f)
        assert cfg["attendance"]["zoom_csv"]["meeting_id_mappings"]["123456789"] == "g10"


# ─── Decision log tests ─────────────────────────────────────────────────────

def test_write_decision_log():
    """Test writing decision log."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_folder = Path(tmpdir) / "logs"
        config = {"log_folder": str(log_folder)}

        participants_detail = [
            {"name": "Ahmed Ali", "mark": "Present", "detail": {"rule_fired": "on_time"}},
            {"name": "Sara", "mark": "Late", "detail": {"rule_fired": "late_arrival"}},
        ]

        write_decision_log("test_run", participants_detail, config)

        log_files = list(log_folder.glob("*.log"))
        assert len(log_files) == 1

        with open(log_files[0], "r") as f:
            log_data = json.load(f)
        assert log_data["run_id"] == "test_run"
        assert len(log_data["participants"]) == 2


# ─── clean_zoom_name tests ───────────────────────────────────────────────────

def test_clean_zoom_name():
    """Test cleaning Zoom display names."""
    # Bilingual: English first
    assert clean_zoom_name("Ahmed Idris -- أحمد إدريس") == "Ahmed Idris"
    assert clean_zoom_name("Ahmed Idris - أحمد إدريس") == "Ahmed Idris"

    # Bilingual: Arabic first
    assert clean_zoom_name("أحمد إدريس -- Ahmed Idris") == "Ahmed Idris"
    assert clean_zoom_name("أحمد إدريس - Ahmed Idris") == "Ahmed Idris"

    # Parenthetical alias
    assert clean_zoom_name("Omar Morad (Omar Morad e116)") == "Omar Morad"
    assert clean_zoom_name("Yaman (Teacher)") == "Yaman"

    # Bracket alias
    assert clean_zoom_name("Ahmed [iPad]") == "Ahmed"
    assert clean_zoom_name("Yaman [Host]") == "Yaman"

    # Whole name inside parentheses or brackets
    assert clean_zoom_name("(Omar Morad)") == "Omar Morad"
    assert clean_zoom_name("[Omar Morad]") == "Omar Morad"

    # Device suffixes after hyphen / slash
    assert clean_zoom_name("Omar - iPad") == "Omar"
    assert clean_zoom_name("Sarah / iPhone") == "Sarah"

    # Pure Arabic name preserved
    assert clean_zoom_name("أحمد إدريس") == "أحمد إدريس"

    # Already clean name
    assert clean_zoom_name("Omar Morad") == "Omar Morad"

    # Blank / empty
    assert clean_zoom_name("") == ""
    assert clean_zoom_name("   ") == ""


# ─── run_zoom pipeline tests ─────────────────────────────────────────────────

def test_run_zoom_teacher_not_found(tmp_path):
    """When teacher is not in CSV and no manual start time provided, sets teacher_not_found=True."""
    from attendance.pipeline import run_zoom

    csv_file = tmp_path / "participants_81795786552_2026_09_13.csv"
    csv_file.write_text(
        "Name (Original Name),User Email,Join Time,Leave Time,Duration (Minutes),Guest\n"
        "Ahmed Ali,ahmed@example.com,09/13/2026 10:35:00 AM,09/13/2026 11:35:00 AM,60,Yes\n",
        encoding="utf-8"
    )

    summary = run_zoom(
        source=str(csv_file),
        course="g10",
        session_num=1,
        dry_run=True,
    )
    assert summary.teacher_not_found is True


def test_run_zoom_with_manual_start_time(tmp_path, monkeypatch):
    """When teacher is missing but manual_start_time is given, proceeds with attendance calculation."""
    from attendance.pipeline import run_zoom

    csv_file = tmp_path / "participants_81795786552_2026_09_13.csv"
    csv_file.write_text(
        "Name (Original Name),User Email,Join Time,Leave Time,Duration (Minutes),Guest\n"
        "Ahmed Ali,ahmed@example.com,09/13/2026 10:35:00 AM,09/13/2026 11:35:00 AM,60,Yes\n",
        encoding="utf-8"
    )

    captured_ocr_rows = []

    def mock_process_rows(**kw):
        captured_ocr_rows.extend(kw.get("ocr_rows", []))
        return kw["summary"]

    monkeypatch.setattr("attendance.pipeline._process_rows", mock_process_rows)

    summary = run_zoom(
        source=str(csv_file),
        course="g10",
        session_num=1,
        dry_run=True,
        manual_start_time="10:30 AM",
    )
    assert summary.teacher_not_found is False
    assert len(captured_ocr_rows) == 1
    assert captured_ocr_rows[0].raw_name == "Ahmed Ali"
    assert captured_ocr_rows[0].status_mark == "Present"

