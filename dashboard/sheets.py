"""All Google Sheets I/O. No other module should import gspread."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import gspread
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials

from config import (
    CACHE_TTL_SECONDS,
    STATUS_ABSENT,
    STATUS_PRESENT,
    CourseConfig,
    get_credentials_json,
)


_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
]


@st.cache_resource(show_spinner=False)
def get_client() -> gspread.Client:
    creds = Credentials.from_service_account_info(
        get_credentials_json(), scopes=_SCOPES
    )
    return gspread.authorize(creds)


def _worksheet(sheet_id: str, tab: str) -> gspread.Worksheet:
    if not sheet_id:
        raise ValueError("Sheet ID is empty — check your .env / st.secrets.")
    return get_client().open_by_key(sheet_id).worksheet(tab)


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def read_values(sheet_id: str, tab: str) -> tuple[list[list[str]], datetime]:
    """
    Return (2-D grid, UTC timestamp of the actual network read).
    On cache hit, the timestamp is the original read time — that's the point.
    """
    ws = _worksheet(sheet_id, tab)
    values = ws.get_all_values() or []
    return values, datetime.now(timezone.utc)


def bust_cache() -> None:
    """Force the next read_values() to hit the network."""
    read_values.clear()


def mark_read(read_at: datetime) -> None:
    """Record the timestamp so the sidebar can display freshness."""
    st.session_state.last_read_at = read_at


# ── Header discovery ───────────────────────────────────────────────────

def find_session_columns(headers: list[str], pattern: str, attendance_pattern: str | None = None) -> dict[str, int]:
    """
    Map each session header to its 0-based column index.
    For typed_pair layout (g9/g10), use attendance_pattern to find "S1 Attendance" columns.
    For single layout (crash/main), use pattern to find "Session 1" columns.
    """
    if attendance_pattern:
        # typed_pair: look for "S1 Attendance", "S2 Attendance", etc.
        regex = re.compile(attendance_pattern)
    else:
        # single: look for "Session 1", "Session 2", etc.
        regex = re.compile(pattern)
    return {
        header: index
        for index, header in enumerate(headers)
        if regex.match(header.strip())
    }


def find_name_column(headers: list[str], session_cols: dict[str, int]) -> int:
    """
    Look for the "Name" column explicitly. If not found, fall back to the first
    non-empty, non-session header.
    """
    session_indices = set(session_cols.values())
    # First try to find "Name" column (case-insensitive)
    for index, header in enumerate(headers):
        if header.strip().lower() == "name":
            return index
    # Fallback: first non-empty, non-session header
    for index, header in enumerate(headers):
        if index not in session_indices and header.strip():
            return index
    raise ValueError("Could not find a student-name column in the header row.")


# ── Main attendance tab ────────────────────────────────────────────────

def load_attendance(course: CourseConfig) -> tuple[pd.DataFrame, datetime, dict[str, int]]:
    """
    Load the main attendance grid.
    Returns (df, read_at, session_columns).
    df columns: 'student', then one per session header.
    """
    values, read_at = read_values(course.sheet_id, course.tab_name)
    mark_read(read_at)

    if not values:
        return pd.DataFrame(columns=["student"]), read_at, {}

    headers = values[0]
    session_cols = find_session_columns(headers, course.session_pattern, course.attendance_pattern)
    if not session_cols:
        # Tab exists but no session columns yet — return an empty frame with the headers
        # we saw, so the UI can show something useful instead of crashing.
        return pd.DataFrame(columns=["student", *headers]), read_at, {}

    name_idx = find_name_column(headers, session_cols)
    rows = values[1:]
    records: list[dict[str, Any]] = []
    for row in rows:
        if len(row) <= name_idx:
            continue
        student = row[name_idx].strip()
        if not student:
            continue
        record: dict[str, Any] = {"student": student}
        for header, col_idx in session_cols.items():
            cell = row[col_idx].strip() if col_idx < len(row) else ""
            record[header] = cell
        records.append(record)

    if not records:
        return pd.DataFrame(columns=["student", *session_cols.keys()]), read_at, session_cols
    return pd.DataFrame(records), read_at, session_cols


# ── Review queue tab ───────────────────────────────────────────────────

REVIEW_REQUIRED_COLUMNS = [
    "id", "upload_id", "course", "session_num", "raw_name",
    "candidate_1", "candidate_1_score",
    "candidate_2", "candidate_2_score",
    "candidate_3", "candidate_3_score",
    "reason_code", "status",
    "resolved_name", "resolved_value", "resolved_by", "resolved_at",
    "created_at",
]


def load_review_queue(course: CourseConfig) -> tuple[pd.DataFrame, datetime]:
    """
    Load the ReviewQueue tab. Always returns a DataFrame with the required columns
    (missing ones filled with '') so downstream code never has to guard for KeyError.
    """
    values, read_at = read_values(course.sheet_id, course.review_tab)
    if not values:
        return pd.DataFrame(columns=REVIEW_REQUIRED_COLUMNS), read_at

    headers = values[0]
    rows = values[1:]
    df = pd.DataFrame(rows, columns=headers) if rows else pd.DataFrame(columns=headers)
    for col in REVIEW_REQUIRED_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    return df, read_at


# ── Write operations ─────────────────────────────────────────────────────

def write_attendance(
    course: CourseConfig,
    student_name: str,
    session_column: str,
    value: str,
) -> tuple[bool, str]:
    """
    Write an attendance value to the main attendance sheet.
    Returns (success, error_message).
    """
    try:
        ws = _worksheet(course.sheet_id, course.tab_name)

        # Find the student row
        values = ws.get_all_values()
        if not values:
            return False, "Sheet is empty"

        headers = values[0]
        session_cols = find_session_columns(headers, course.session_pattern, course.attendance_pattern)
        name_idx = find_name_column(headers, session_cols)

        # Find the session column index
        if session_column not in session_cols:
            return False, f"Session column '{session_column}' not found in sheet"
        session_col_idx = session_cols[session_column]

        # Find the student row
        for row_idx, row in enumerate(values[1:], start=2):  # +2 because 0-indexed + header row
            if len(row) > name_idx and row[name_idx].strip() == student_name:
                # Check if already has a value
                if len(row) > session_col_idx and row[session_col_idx].strip():
                    return False, f"Student '{student_name}' already has a value in '{session_column}'"

                # Write the value
                ws.update_cell(row_idx, session_col_idx + 1, value)  # +1 because gspread is 1-indexed
                bust_cache()  # Invalidate cache after write
                return True, ""

        return False, f"Student '{student_name}' not found in roster"
    except Exception as e:
        import logging
        logging.exception(f"Failed to write attendance for {student_name}: {e}")
        return False, f"Write failed: {e}"


def batch_write_attendance(
    course: CourseConfig,
    updates: list[tuple[str, str, str]],  # (student_name, session_column, value)
) -> tuple[int, list[str]]:
    """
    Batch write multiple attendance values to the main attendance sheet.
    Returns (success_count, error_messages).
    """
    if not updates:
        return 0, []

    try:
        ws = _worksheet(course.sheet_id, course.tab_name)

        # Load sheet data once
        values = ws.get_all_values()
        if not values:
            return 0, ["Sheet is empty"]

        headers = values[0]
        session_cols = find_session_columns(headers, course.session_pattern, course.attendance_pattern)
        name_idx = find_name_column(headers, session_cols)

        # Prepare batch updates
        batch_updates = []
        errors = []

        for student_name, session_column, value in updates:
            # Find the session column index
            if session_column not in session_cols:
                errors.append(f"Session column '{session_column}' not found for student '{student_name}'")
                continue

            session_col_idx = session_cols[session_column]

            # Find the student row
            student_found = False
            for row_idx, row in enumerate(values[1:], start=2):
                if len(row) > name_idx and row[name_idx].strip() == student_name:
                    student_found = True
                    # Check if already has a value
                    if len(row) > session_col_idx and row[session_col_idx].strip():
                        errors.append(f"Student '{student_name}' already has a value in '{session_column}'")
                        break

                    # Add to batch updates
                    batch_updates.append((row_idx, session_col_idx + 1, value))  # +1 for gspread 1-indexed
                    break

            if not student_found:
                errors.append(f"Student '{student_name}' not found in roster")

        # Execute batch update
        if batch_updates:
            ws.batch_update(batch_updates)
            bust_cache()

        return len(batch_updates), errors
    except Exception as e:
        import logging
        logging.exception(f"Failed to batch write attendance: {e}")
        return 0, [f"Batch write failed: {e}"]


def update_review_item(
    course: CourseConfig,
    item_id: str,
    resolved_name: str,
    resolved_value: str,
    resolved_by: str,
) -> tuple[bool, str]:
    """
    Update a review item to mark it as resolved.
    Returns (success, error_message).
    """
    try:
        ws = _worksheet(course.sheet_id, course.review_tab)

        # Find the item row by id
        values = ws.get_all_values()
        if not values:
            return False, "Review queue sheet is empty"

        headers = values[0]
        id_idx = headers.index("id") if "id" in headers else -1
        if id_idx == -1:
            return False, "'id' column not found in review queue"

        for row_idx, row in enumerate(values[1:], start=2):
            if len(row) > id_idx and row[id_idx] == item_id:
                # Update the row
                now = datetime.now(timezone.utc).isoformat()

                # Find column indices
                status_idx = headers.index("status") if "status" in headers else -1
                resolved_name_idx = headers.index("resolved_name") if "resolved_name" in headers else -1
                resolved_value_idx = headers.index("resolved_value") if "resolved_value" in headers else -1
                resolved_by_idx = headers.index("resolved_by") if "resolved_by" in headers else -1
                resolved_at_idx = headers.index("resolved_at") if "resolved_at" in headers else -1

                updates = []
                if status_idx >= 0:
                    updates.append((row_idx, status_idx + 1, "approved"))
                if resolved_name_idx >= 0:
                    updates.append((row_idx, resolved_name_idx + 1, resolved_name))
                if resolved_value_idx >= 0:
                    updates.append((row_idx, resolved_value_idx + 1, resolved_value))
                if resolved_by_idx >= 0:
                    updates.append((row_idx, resolved_by_idx + 1, resolved_by))
                if resolved_at_idx >= 0:
                    updates.append((row_idx, resolved_at_idx + 1, now))

                if updates:
                    ws.batch_update(updates)
                    bust_cache()  # Invalidate cache after write
                return True, ""

        return False, f"Review item '{item_id}' not found"
    except Exception as e:
        import logging
        logging.exception(f"Failed to update review item {item_id}: {e}")
        return False, f"Update failed: {e}"


def batch_update_review_items(
    course: CourseConfig,
    updates: list[tuple[str, str, str, str]],  # (item_id, resolved_name, resolved_value, resolved_by)
) -> tuple[int, list[str]]:
    """
    Batch update multiple review items to mark them as resolved.
    Returns (success_count, error_messages).
    """
    if not updates:
        return 0, []

    try:
        ws = _worksheet(course.sheet_id, course.review_tab)

        # Load sheet data once
        values = ws.get_all_values()
        if not values:
            return 0, ["Review queue sheet is empty"]

        headers = values[0]
        id_idx = headers.index("id") if "id" in headers else -1
        if id_idx == -1:
            return 0, ["'id' column not found in review queue"]

        # Prepare batch updates
        batch_updates = []
        errors = []
        now = datetime.now(timezone.utc).isoformat()

        # Find column indices once
        status_idx = headers.index("status") if "status" in headers else -1
        resolved_name_idx = headers.index("resolved_name") if "resolved_name" in headers else -1
        resolved_value_idx = headers.index("resolved_value") if "resolved_value" in headers else -1
        resolved_by_idx = headers.index("resolved_by") if "resolved_by" in headers else -1
        resolved_at_idx = headers.index("resolved_at") if "resolved_at" in headers else -1

        # Build lookup of item_id -> row_idx
        id_to_row = {}
        for row_idx, row in enumerate(values[1:], start=2):
            if len(row) > id_idx and row[id_idx]:
                id_to_row[row[id_idx]] = row_idx

        # Process each update
        for item_id, resolved_name, resolved_value, resolved_by in updates:
            if item_id not in id_to_row:
                errors.append(f"Review item '{item_id}' not found")
                continue

            row_idx = id_to_row[item_id]

            # Add to batch updates
            if status_idx >= 0:
                batch_updates.append((row_idx, status_idx + 1, "approved"))
            if resolved_name_idx >= 0:
                batch_updates.append((row_idx, resolved_name_idx + 1, resolved_name))
            if resolved_value_idx >= 0:
                batch_updates.append((row_idx, resolved_value_idx + 1, resolved_value))
            if resolved_by_idx >= 0:
                batch_updates.append((row_idx, resolved_by_idx + 1, resolved_by))
            if resolved_at_idx >= 0:
                batch_updates.append((row_idx, resolved_at_idx + 1, now))

        # Execute batch update
        if batch_updates:
            ws.batch_update(batch_updates)
            bust_cache()

        return len(updates) - len(errors), errors
    except Exception as e:
        import logging
        logging.exception(f"Failed to batch update review items: {e}")
        return 0, [f"Batch update failed: {e}"]
