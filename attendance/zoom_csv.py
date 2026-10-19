"""attendance/zoom_csv.py — CSV parsing and attendance calculation for Zoom participant reports.

Handles both CSV file parsing and attendance calculation logic shared by CSV upload
and Zoom API fetch paths.
"""
from __future__ import annotations

import csv
import json
import os
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import yaml
from rapidfuzz import fuzz

from attendance._fslock import file_lock
from core.logger import get_logger

log = get_logger("zoom_csv")


# ─── Zoom display-name cleaner ────────────────────────────────────────────────

_ARABIC_CHAR = re.compile(r'[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]')
_LATIN_CHAR = re.compile(r'[a-zA-Z]')
_PAREN_OR_BRACKET = re.compile(r'[\(\[][^\)\]]*[\)\]]')
_DEVICE_SUFFIX = re.compile(r'[-–—/]\s*(?:i?pad|i?phone|tablet|pc|laptop|android|ios|mac|windows|host|co-host|teacher|e\d+)\b', re.IGNORECASE)
_TRAILING_DASH = re.compile(r'[\s\-–—/:]+$')
_LEADING_DASH = re.compile(r'^[\s\-–—/:]+')


def clean_zoom_name(name: str) -> str:
    """Strip Zoom display-name artefacts that hurt fuzzy matching.

    Handles:
    1. Bilingual names (English first or Arabic first):
       "Ahmed Idris -- أحمد إدريس"  →  "Ahmed Idris"
       "أحمد إدريس -- Ahmed Idris"  →  "Ahmed Idris"
    2. Parenthetical suffixes & aliases:
       "Omar Morad (Omar Morad e116)"  →  "Omar Morad"
       "(Omar Morad)"  →  "Omar Morad" (preserves content if wrapped entirely)
    3. Bracket tags:
       "Ahmed [iPad]"  →  "Ahmed"
    4. Device suffixes after hyphens/slashes:
       "Omar - iPad"  →  "Omar"
    5. Pure Arabic or already clean names are preserved safely.
    """
    if not name or not name.strip():
        return ""

    raw = name.strip()

    # Step 1: Strip known device / role suffixes after hyphens or slashes
    cleaned = _DEVICE_SUFFIX.sub('', raw)

    # Step 2: Strip parenthetical or bracket groups
    without_parens = _PAREN_OR_BRACKET.sub('', cleaned).strip()
    if without_parens:
        cleaned = without_parens
    else:
        # If the entire string was inside parens/brackets, strip only outer delimiters
        cleaned = re.sub(r'^[\(\[]+|[\)\]]+$', '', cleaned).strip()

    # Step 3: Handle bilingual names (keep the Latin/English half matching our rosters)
    has_arabic = bool(_ARABIC_CHAR.search(cleaned))
    has_latin = bool(_LATIN_CHAR.search(cleaned))

    if has_arabic and has_latin:
        # Split on common bilingual delimiters: --, -, —, /, |, :
        parts = re.split(r'\s*[-–—/|:]+\s*', cleaned)
        latin_parts = [p.strip() for p in parts if _LATIN_CHAR.search(p) and not _ARABIC_CHAR.search(p)]
        if latin_parts:
            cleaned = " ".join(latin_parts)
        else:
            # If no clear delimiter separator, remove Arabic script characters
            cleaned = _ARABIC_CHAR.sub('', cleaned).strip()

    # Step 4: Clean leading / trailing delimiters
    cleaned = _LEADING_DASH.sub('', cleaned)
    cleaned = _TRAILING_DASH.sub('', cleaned)

    # Collapse multiple whitespace
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()

    return cleaned if cleaned else raw


# ─── CSV parsing ───────────────────────────────────────────────────────────────

def parse_zoom_csv(file_path: str) -> list[dict[str, Any]]:
    """Parse Zoom participant CSV file and return normalized participant records.

    Args:
        file_path: Path to CSV file

    Returns:
        List of participant records with:
        - name (str)
        - email (str or None)
        - join_time (datetime)
        - leave_time (datetime)
        - duration_minutes (float)
        - guest (bool)

    Raises:
        ValueError: If CSV structure is invalid
    """
    path = Path(file_path)
    if not path.exists():
        raise ValueError(f"CSV file not found: {file_path}")

    # Read with UTF-8, handle BOM if present
    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames

        if not headers:
            raise ValueError("CSV file has no headers")

        # Validate required columns (case-insensitive)
        # Map flexible column names to standard keys
        header_map = {}
        for h in headers:
            h_lower = h.lower()
            if "name" in h_lower:
                header_map["name"] = h
            elif "join time" in h_lower or "join" in h_lower:
                header_map["join_time"] = h
            elif "leave time" in h_lower or "leave" in h_lower:
                header_map["leave_time"] = h
            elif "duration" in h_lower:
                header_map["duration"] = h
            elif "guest" in h_lower:
                header_map["guest"] = h

        required = ["name", "join_time", "leave_time", "duration"]
        missing = [col for col in required if col not in header_map]

        if missing:
            raise ValueError(f"CSV missing required columns: {missing}")

        participants = []
        for row in reader:
            try:
                # Use mapped column names
                name_col = header_map["name"]
                join_col = header_map["join_time"]
                leave_col = header_map["leave_time"]
                duration_col = header_map["duration"]
                guest_col = header_map.get("guest")

                name = row.get(name_col, "").strip()
                if not name:
                    continue

                # Parse datetime: "MM/DD/YYYY HH:MM:SS AM/PM"
                join_str = row.get(join_col, "")
                leave_str = row.get(leave_col, "")

                join_time = _parse_zoom_datetime(join_str)
                leave_time = _parse_zoom_datetime(leave_str)

                # Duration from CSV (in minutes)
                duration_str = row.get(duration_col, "0")
                duration_minutes = float(duration_str) if duration_str else 0.0

                # Guest status
                guest_str = row.get(guest_col, "No") if guest_col else "No"
                guest = guest_str.strip().lower() == "yes"

                # Email (optional)
                email_col = header_map.get("email")
                email = row.get(email_col, "").strip() if email_col else None

                participants.append({
                    "name": clean_zoom_name(name) or name,
                    "email": email,
                    "join_time": join_time,
                    "leave_time": leave_time,
                    "duration_minutes": duration_minutes,
                    "guest": guest,
                })

            except Exception as e:
                log.warning("csv_row_parse_error", extra={"row": row, "error": str(e)})
                continue

    log.info("csv_parsed", extra={"file": file_path, "participants": len(participants)})
    return participants


def _parse_zoom_datetime(dt_str: str) -> Optional[datetime]:
    """Parse Zoom datetime format: "MM/DD/YYYY HH:MM:SS AM/PM"."""
    if not dt_str or not dt_str.strip():
        return None

    # Try multiple formats
    formats = [
        "%m/%d/%Y %I:%M:%S %p",
        "%m/%d/%Y %I:%M %p",
        "%Y-%m-%dT%H:%M:%SZ",  # ISO format from API
    ]

    for fmt in formats:
        try:
            return datetime.strptime(dt_str.strip(), fmt)
        except ValueError:
            continue

    log.warning("datetime_parse_failed", extra={"dt_str": dt_str})
    return None


def validate_csv_structure(headers: list[str]) -> bool:
    """Validate that CSV has required columns.

    Args:
        headers: List of column names from CSV

    Returns:
        True if valid, False otherwise
    """
    # Use flexible matching like the parser
    header_map = {}
    for h in headers:
        h_lower = h.lower()
        if "name" in h_lower:
            header_map["name"] = h
        elif "join time" in h_lower or "join" in h_lower:
            header_map["join_time"] = h
        elif "leave time" in h_lower or "leave" in h_lower:
            header_map["leave_time"] = h
        elif "duration" in h_lower:
            header_map["duration"] = h

    required = ["name", "join_time", "leave_time", "duration"]
    return all(col in header_map for col in required)


# ─── Teacher detection ─────────────────────────────────────────────────────────

def find_teacher(
    participants: list[dict[str, Any]],
    teacher_name: str,
    fuzzy_threshold: float,
) -> Optional[dict[str, Any]]:
    """Find teacher in participant list with fuzzy matching.

    Merges rejoins: returns single record with earliest join and latest leave.

    Args:
        participants: List of participant records
        teacher_name: Teacher name to search for
        fuzzy_threshold: Fuzzy match threshold (0-100)

    Returns:
        Merged teacher record with:
        - name (str)
        - join_time (datetime) - earliest join
        - leave_time (datetime) - latest leave
        - duration_minutes (float) - total duration
        Or None if not found
    """
    if not participants:
        return None

    # Find all fuzzy matches (exact matches will score 100)
    teacher_clean = clean_zoom_name(teacher_name).lower()
    teacher_entries = []
    for p in participants:
        p_name = p.get("name", "")
        p_clean = clean_zoom_name(p_name).lower()
        score = max(
            fuzz.token_sort_ratio(p_name.lower(), teacher_name.lower()),
            fuzz.token_sort_ratio(p_clean, teacher_clean),
            fuzz.token_set_ratio(p_name.lower(), teacher_name.lower()),
            fuzz.token_set_ratio(p_clean, teacher_clean),
        )
        if score >= fuzzy_threshold:
            teacher_entries.append(p)

    if not teacher_entries:
        log.warning("teacher_not_found", extra={"teacher_name": teacher_name})
        return None

    def _to_naive(dt: Optional[datetime]) -> Optional[datetime]:
        if dt is not None and getattr(dt, "tzinfo", None) is not None:
            return dt.replace(tzinfo=None)
        return dt

    # Merge rejoins: take earliest join and latest leave
    earliest_join = _to_naive(min(p["join_time"] for p in teacher_entries if p["join_time"]))
    latest_leave = _to_naive(max(p["leave_time"] for p in teacher_entries if p["leave_time"]))
    total_duration = sum(p["duration_minutes"] for p in teacher_entries)

    merged = {
        "name": teacher_name,
        "join_time": earliest_join,
        "leave_time": latest_leave,
        "duration_minutes": total_duration,
    }

    log.info("teacher_found", extra={
        "teacher_name": teacher_name,
        "join": earliest_join.isoformat() if earliest_join else None,
        "leave": latest_leave.isoformat() if latest_leave else None,
        "entries": len(teacher_entries),
    })

    return merged


# ─── Attendance calculation ─────────────────────────────────────────────────────

def calculate_attendance(
    participants: list[dict[str, Any]],
    config: dict,
    meeting_start_time: Optional[datetime],
    meeting_end_time: Optional[datetime],
    teacher_name: Optional[str] = None,
) -> Optional[list[tuple[str, str, dict[str, Any]]]]:
    """Calculate attendance marks for all participants.

    Args:
        participants: List of participant records
        config: Zoom CSV configuration dict
        meeting_start_time: Teacher's join time (or None if teacher not found)
        meeting_end_time: Teacher's leave time (or None)
        teacher_name: Teacher's name to exclude from student attendance results.
            Uses the same fuzzy threshold as find_teacher.

    Returns:
        List of (name, attendance_mark, detail) tuples, or None if teacher not found.
        detail dict contains: total_duration, first_join, last_leave, effective_start,
        teacher_leave_time, rule_fired
    """
    if meeting_start_time is None:
        return None

    def _to_naive(dt: Optional[datetime]) -> Optional[datetime]:
        if dt is not None and getattr(dt, "tzinfo", None) is not None:
            return dt.replace(tzinfo=None)
        return dt

    meeting_start_time = _to_naive(meeting_start_time)
    meeting_end_time = _to_naive(meeting_end_time)

    teacher_join_buffer = timedelta(minutes=config.get("teacher_join_buffer_minutes", 5))
    late_threshold = timedelta(minutes=config.get("late_threshold_minutes", 15))
    min_duration = config.get("min_duration_minutes", 45)
    early_leave_threshold = timedelta(minutes=config.get("early_leave_threshold_minutes", 20))
    duplicate_handling = config.get("duplicate_handling", "sum_duration")
    fuzzy_threshold = config.get("teacher_fuzzy_match_threshold", 80)

    effective_start = meeting_start_time + teacher_join_buffer

    # Exclude teacher from student attendance (they were already used to establish meeting times)
    def _is_teacher(name: str) -> bool:
        if not teacher_name:
            return False
        teacher_list = [teacher_name] if isinstance(teacher_name, str) else list(teacher_name)
        clean = clean_zoom_name(name).lower()
        for t in teacher_list:
            if not t:
                continue
            t_clean = clean_zoom_name(t).lower()
            score = max(
                fuzz.token_sort_ratio(name.lower(), t.lower()),
                fuzz.token_sort_ratio(clean, t_clean),
                fuzz.token_set_ratio(name.lower(), t.lower()),
                fuzz.token_set_ratio(clean, t_clean),
            )
            if score >= fuzzy_threshold:
                return True
        return False

    # Group by cleaned name (handle duplicates), skipping the teacher
    by_name: dict[str, list[dict[str, Any]]] = {}
    for p in participants:
        name = clean_zoom_name(p["name"]) or p["name"].strip()
        if not name or _is_teacher(name):
            continue
        if name not in by_name:
            by_name[name] = []
        by_name[name].append(p)

    results = []
    for name, entries in by_name.items():
        # Sum durations across duplicate sessions
        total_duration = sum(p["duration_minutes"] for p in entries)
        first_join = _to_naive(min(p["join_time"] for p in entries if p["join_time"]))
        last_leave = _to_naive(max(p["leave_time"] for p in entries if p["leave_time"]))

        # Compute duration from timestamps for comparison
        if first_join and last_leave:
            computed_duration = (last_leave - first_join).total_seconds() / 60
            if abs(computed_duration - total_duration) > 1.0:
                log.warning("duration_mismatch", extra={
                    "student_name": name,
                    "reported": total_duration,
                    "computed": computed_duration,
                })
                # Use Zoom's reported duration as per plan
        else:
            computed_duration = total_duration

        # Determine attendance mark
        mark = "Absent"
        rule_fired = "duration_too_short"

        if total_duration >= min_duration:
            if first_join <= effective_start + late_threshold:
                mark = "Present"
                rule_fired = "on_time"
            else:
                mark = "Late"
                rule_fired = "late_arrival"

        # Override to "Not complete" if left too early before teacher
        if meeting_end_time and last_leave:
            time_after_leave = meeting_end_time - last_leave
            if time_after_leave > early_leave_threshold:
                mark = "Not complete"
                rule_fired = "early_leave"

        detail = {
            "total_duration": total_duration,
            "first_join": first_join.isoformat() if first_join else None,
            "last_leave": last_leave.isoformat() if last_leave else None,
            "effective_start": effective_start.isoformat(),
            "teacher_leave_time": meeting_end_time.isoformat() if meeting_end_time else None,
            "rule_fired": rule_fired,
        }

        results.append((name, mark, detail))

    log.info("attendance_calculated", extra={"participants": len(results)})
    return results


# ─── Filename parsing ────────────────────────────────────────────────────────────

def extract_meeting_id_from_filename(filename: str) -> Optional[str]:
    """Extract meeting ID from filename pattern: participants_<meeting_id>_<date>.csv

    Args:
        filename: CSV filename or path

    Returns:
        Meeting ID string or None if pattern doesn't match
    """
    # Use re.split so Windows-style paths (C:\Users\...) are stripped on Linux too,
    # where pathlib.Path only splits on '/' and would leave the backslash path intact.
    base_name = re.split(r"[\\/]", filename)[-1]
    pattern = r"^participants_(\d+)_\d{4}_\d{2}_\d{2}(?:\s*\(\d+\))?\.csv$"
    match = re.match(pattern, base_name, re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def extract_date_from_filename(filename: str) -> Optional[datetime]:
    """Extract date from filename pattern: participants_<meeting_id>_<date>.csv
    Supports duplicate copy suffixes like (1).csv.

    Date format: YYYY_MM_DD

    Args:
        filename: CSV filename or path

    Returns:
        datetime object or None if pattern doesn't match
    """
    base_name = re.split(r"[\\/]", filename)[-1]
    pattern = r"^participants_\d+_(\d{4}_\d{2}_\d{2})(?:\s*\(\d+\))?\.csv$"
    match = re.match(pattern, base_name, re.IGNORECASE)
    if match:
        date_str = match.group(1)
        try:
            return datetime.strptime(date_str, "%Y_%m_%d")
        except ValueError:
            return None
    return None


# ─── Session type detection ──────────────────────────────────────────────────────

def determine_session_type(
    date: datetime,
    course: str,
    cfg: dict,
) -> str:
    """Auto-detect session type from date and course configuration.

    Only applies to Online sessions (ignores Center).

    Args:
        date: Meeting date
        course: Course key (e.g., "g10")
        cfg: Full configuration dict

    Returns:
        Session type string, or "ambiguous" if multiple Online variants match
    """
    courses_cfg = cfg.get("attendance", {}).get("courses", {})
    course_cfg = courses_cfg.get(course, {})

    if course_cfg.get("layout") != "typed_pair":
        # Not a typed_pair course, no type column
        return ""

    types_cfg = course_cfg.get("types", {})
    if not types_cfg:
        return ""

    # Get day of week (full name, lowercase)
    day_of_week = date.strftime("%A").lower()

    # Collect all Online types for this day
    online_types = []
    for parity, type_list in types_cfg.items():
        if not isinstance(type_list, (list, tuple)):
            continue
        for t in type_list:
            if not isinstance(t, str):
                continue
            if "online" in t.lower():
                # Check if this type matches the day (e.g. "Saturday - Online" or "Sunday- Online")
                parts = re.split(r"[\s\-]+", t.strip())
                type_day = parts[0].lower() if parts else ""
                if type_day == day_of_week and t not in online_types:
                    online_types.append(t)

    if len(online_types) == 1:
        return online_types[0]
    elif len(online_types) > 1:
        log.warning("ambiguous_session_type", extra={
            "date": date.isoformat(),
            "course": course,
            "candidates": online_types,
        })
        return "ambiguous"
    else:
        log.warning("no_online_type_found", extra={"date": date.isoformat(), "course": course})
        return ""


# ─── CSV archiving ─────────────────────────────────────────────────────────────

def archive_csv(file_path: str, archive_folder: str) -> None:
    """Move processed CSV to archive folder.

    Args:
        file_path: Path to CSV file
        archive_folder: Archive folder path
    """
    src = Path(file_path)
    if not src.exists():
        log.warning("csv_not_found_for_archive", extra={"file": file_path})
        return

    archive_path = Path(archive_folder)
    archive_path.mkdir(parents=True, exist_ok=True)

    dest = archive_path / src.name
    shutil.move(str(src), str(dest))
    log.info("csv_archived", extra={"from": file_path, "to": str(dest)})


# ─── Meeting ID mapping ─────────────────────────────────────────────────────────

def get_course_for_meeting_id(mappings: dict, meeting_id: str | int | None) -> Optional[str]:
    """Look up course for a meeting ID, supporting both str and int keys.

    Args:
        mappings: meeting_id_mappings dict from config
        meeting_id: Zoom meeting ID (str or int)

    Returns:
        Course string or None if not found
    """
    if not mappings or meeting_id is None:
        return None
    mid_str = str(meeting_id).strip()
    if mid_str in mappings:
        return mappings[mid_str]
    try:
        mid_int = int(mid_str)
        if mid_int in mappings:
            return mappings[mid_int]
    except (ValueError, TypeError):
        pass
    return None


def save_meeting_id_mapping(meeting_id: str, course: str, config_path: str) -> None:
    """Add new meeting ID to course mapping in config file.

    Uses file_lock for safe concurrent edits.

    Args:
        meeting_id: Zoom meeting ID
        course: Course key
        config_path: Path to config file
    """
    config_file = Path(config_path)

    with file_lock(config_file):
        with open(config_file, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

        zoom_cfg = cfg.setdefault("attendance", {}).setdefault("zoom_csv", {})
        mappings = zoom_cfg.setdefault("meeting_id_mappings", {})

        mid_str = str(meeting_id).strip()
        existing = get_course_for_meeting_id(mappings, mid_str)
        if not existing:
            mappings[mid_str] = course
            tmp = config_file.with_name(config_file.name + f".{os.getpid()}.tmp")
            with open(tmp, "w", encoding="utf-8") as f_out:
                yaml.safe_dump(cfg, f_out, allow_unicode=True, sort_keys=False)
            os.replace(tmp, config_file)
            log.info("meeting_id_mapped", extra={"meeting_id": mid_str, "course": course})
        else:
            log.info("meeting_id_already_mapped", extra={"meeting_id": mid_str, "existing": existing})


# ─── Decision logging ───────────────────────────────────────────────────────────

def write_decision_log(
    run_id: str,
    participants_detail: list[dict[str, Any]],
    config: dict,
) -> None:
    """Write per-run decision log to log folder as JSON file.

    Filename includes date+timestamp to disambiguate multiple instances.

    Args:
        run_id: Unique run identifier (e.g., meeting_id)
        participants_detail: List of participant detail dicts
        config: Zoom CSV configuration dict
    """
    log_folder = config.get("log_folder", "logs/zoom_csv")
    log_path = Path(log_folder)
    log_path.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{run_id}_{timestamp}.log"
    filepath = log_path / filename

    log_entry = {
        "run_id": run_id,
        "timestamp": datetime.now().isoformat(),
        "participants": participants_detail,
    }

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(log_entry, f, ensure_ascii=False, indent=2)

    log.info("decision_log_written", extra={"file": str(filepath)})
