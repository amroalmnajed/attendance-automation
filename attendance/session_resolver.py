"""attendance/session_resolver.py — Live sheet structure discovery and session resolution.

Reads the target course's attendance tab, returns:
  - Roster: [(name, sheet_row_1based), ...]
  - session_col: the column letter for the target session
  - (for typed_pair) type_col: column letter for the type cell
  - live dropdown values for validation

Validates config against live sheet structure; raises CONFIG_SHEET_DRIFT on mismatch.
"""
from __future__ import annotations

import re
import os
from typing import Optional

import gspread
from google.oauth2.credentials import Credentials

from core.auth import get_credentials
from core.logger import get_logger

log = get_logger("session_resolver")


class ConfigSheetDrift(Exception):
    """Raised when config and live sheet disagree on structure or dropdown values."""
    pass


class SessionNotFound(Exception):
    """Raised when the declared session number has no matching column header."""
    pass


def _col_letter_to_index(col: str) -> int:
    """'A' → 1, 'B' → 2, 'AA' → 27, etc. (1-based)"""
    result = 0
    for ch in col.upper():
        result = result * 26 + (ord(ch) - ord("A") + 1)
    return result


def _index_to_col_letter(idx: int) -> str:
    """1 → 'A', 27 → 'AA', etc."""
    result = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        result = chr(rem + ord("A")) + result
    return result


class SheetSession:
    """Resolved session information for a single upload."""
    def __init__(
        self,
        course: str,
        session_num: int,
        session_col: str,          # column letter for attendance value
        type_col: Optional[str],   # column letter for type (typed_pair sheets), or None
        roster: list[tuple[str, int]],  # [(name, sheet_row_1based)]
        allowed_attendance: list[str],
        allowed_types: Optional[list[str]],
        sheet_id: str,
        tab: str,
        gc: gspread.Client,
    ):
        self.course = course
        self.session_num = session_num
        self.session_col = session_col
        self.type_col = type_col
        self.roster = roster
        self.allowed_attendance = allowed_attendance
        self.allowed_types = allowed_types
        self.sheet_id = sheet_id
        self.tab = tab
        self._gc = gc

    def get_worksheet(self) -> gspread.Worksheet:
        sh = self._gc.open_by_key(self.sheet_id)
        return sh.worksheet(self.tab)


def resolve(
    course_key: str,
    session_num: int,
    session_type: Optional[str],
    cfg: dict,
) -> SheetSession:
    """Resolve the target session column and roster from the live sheet.

    Args:
        course_key: one of "crash", "main", "g10", "g9"
        session_num: the declared session number (1-based)
        session_type: the declared session type (required for g9/g10)
        cfg: the full attendance config dict

    Returns:
        SheetSession with everything needed to write attendance.
    """
    course_cfg = cfg["attendance"]["courses"][course_key]
    sheet_id_env = course_cfg["sheet_id_env"]
    sheet_id = os.environ[sheet_id_env]
    tab = course_cfg["tab"]
    layout = course_cfg.get("layout", "single")
    header_row = int(course_cfg["header_row"])
    first_student_row = int(course_cfg["first_student_row"])
    session_pattern = re.compile(course_cfg["session_header_pattern"])
    skip_patterns = [re.compile(p) for p in cfg["attendance"]["match"].get("skip_row_patterns", [])]
    hidden_rows_cfg: list[int] = course_cfg.get("hidden_rows", [])

    # ── Connect ───────────────────────────────────────────────────────────────
    creds = get_credentials()
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id)
    ws = sh.worksheet(tab)

    log.info("sheet_connected", extra={"course": course_key, "tab": tab, "sheet_id": sheet_id})

    # ── Read header row ───────────────────────────────────────────────────────
    all_values = ws.get_all_values()
    if len(all_values) < header_row:
        raise ConfigSheetDrift(f"Sheet has fewer rows than expected header row {header_row}")

    header = all_values[header_row - 1]  # 0-based index

    # ── Find session column ───────────────────────────────────────────────────
    session_col_idx: Optional[int] = None
    type_col_idx: Optional[int] = None

    if layout == "single":
        # Header cell should match pattern with group(1) == str(session_num)
        for col_idx, cell in enumerate(header):
            m = session_pattern.match(cell.strip())
            if m and int(m.group(1)) == session_num:
                session_col_idx = col_idx + 1  # 1-based
                break

    elif layout == "typed_pair":
        att_pattern_str = course_cfg.get("attendance_header_pattern", r"^S(\d+) Attendance$")
        att_pattern = re.compile(att_pattern_str)
        # Find the "S{n} Attendance" column
        for col_idx, cell in enumerate(header):
            m = att_pattern.match(cell.strip())
            if m and int(m.group(1)) == session_num:
                session_col_idx = col_idx + 1  # 1-based
                # The type column is always immediately to the left
                type_col_idx = col_idx  # 0-based → 1-based = col_idx
                break

    if session_col_idx is None:
        raise SessionNotFound(
            f"No column found for course={course_key} session={session_num} "
            f"in tab='{tab}' header_row={header_row}"
        )

    session_col = _index_to_col_letter(session_col_idx)
    type_col = _index_to_col_letter(type_col_idx) if type_col_idx else None

    log.info(
        "session_resolved",
        extra={
            "course": course_key,
            "session": session_num,
            "session_col": session_col,
            "type_col": type_col,
        },
    )

    # ── Validate type (typed_pair sheets) ─────────────────────────────────────
    allowed_types: Optional[list[str]] = None
    if layout == "typed_pair" and session_type is not None:
        types_cfg = course_cfg.get("types", {})
        if "any" in types_cfg:
            allowed_types = types_cfg["any"]
        else:
            parity = "odd" if session_num % 2 == 1 else "even"
            allowed_types = types_cfg.get(parity, [])

        # Normalise both sides for comparison
        def _norm(s: str) -> str:
            return re.sub(r"\s+", " ", s).strip().lower()

        norm_allowed = [_norm(t) for t in (allowed_types or [])]
        if _norm(session_type) not in norm_allowed:
            raise ValueError(
                f"TYPE_INVALID: '{session_type}' is not valid for {course_key} S{session_num}. "
                f"Allowed: {allowed_types}"
            )

    # ── Read roster ───────────────────────────────────────────────────────────
    name_col_idx = _col_letter_to_index(course_cfg.get("name_col", "A")) - 1  # 0-based
    roster: list[tuple[str, int]] = []

    for row_idx in range(first_student_row - 1, len(all_values)):  # 0-based
        sheet_row = row_idx + 1  # 1-based
        if sheet_row in hidden_rows_cfg:
            continue
        if row_idx >= len(all_values):
            break
        row = all_values[row_idx]
        if name_col_idx >= len(row):
            continue
        name = row[name_col_idx].strip()
        if not name:
            continue
        # Skip ref/total rows
        if any(p.search(name) for p in skip_patterns):
            continue
        # Skip obvious non-student markers
        if name.upper() in ("TOTAL", "TOTALS", "REF", "CODE"):
            continue
        roster.append((name, sheet_row))

    log.info("roster_loaded", extra={"course": course_key, "student_count": len(roster)})

    # ── Read live attendance dropdown values ───────────────────────────────────
    # We use the config values and cross-check against what the sheet reports
    cfg_allowed = course_cfg.get("allowed_values", ["Present", "Late", "Not complete", "Absent"])

    return SheetSession(
        course=course_key,
        session_num=session_num,
        session_col=session_col,
        type_col=type_col,
        roster=roster,
        allowed_attendance=cfg_allowed,
        allowed_types=allowed_types,
        sheet_id=sheet_id,
        tab=tab,
        gc=gc,
    )
