"""webapp/config.py — Course and secret configuration for the webapp.

CourseConfig / _secret() / COURSES pattern for webapp configuration.
Reads course keys and display names from config/attendance.yaml (ground truth)
instead of hard-coding them, so the webapp never drifts from the workflow config.
Falls back to the known course keys (crash/main/g9/g10) when the YAML is absent
(e.g. during unit tests that do not have the full repo mounted).
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

# ---------------------------------------------------------------------------
# Streamlit is optional so this module is importable in plain pytest sessions.
# ---------------------------------------------------------------------------
try:
    import streamlit as st  # type: ignore[import-untyped]
    _HAS_ST = True
except ImportError:  # pragma: no cover
    _HAS_ST = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _secret(key: str, default: str = "") -> str:
    """Return a secret value.

    Resolution order:
      1. ``st.secrets[key]``  — Streamlit Community Cloud / local secrets.toml
      2. ``os.environ[key]``  — Railway / any 12-factor environment
      3. ``default``
    """
    if _HAS_ST:
        try:
            return str(st.secrets[key])  # type: ignore[attr-defined]
        except Exception:
            # KeyError (key absent), FileNotFoundError (no secrets.toml),
            # StreamlitAPIException (called outside Streamlit context in tests)
            pass
    return os.environ.get(key, default)


# ---------------------------------------------------------------------------
# CourseConfig dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CourseConfig:
    """Per-course settings (one entry per course in config/attendance.yaml)."""

    key: str        # matches the YAML course key, e.g. "crash"
    label: str      # human-readable display name shown in the sidebar
    sheet_id: str   # Google Sheets ID resolved from secrets / env vars


# ---------------------------------------------------------------------------
# Load courses from config/attendance.yaml
# ---------------------------------------------------------------------------

# Resolve the repo root from this file's location: webapp/config.py → repo root
_REPO_ROOT: Path = Path(__file__).resolve().parents[1]
_CONFIG_YAML: Path = _REPO_ROOT / "config" / "attendance.yaml"

# The canonical course keys as of the last known good state of attendance.yaml.
# Used only as a fallback when the YAML cannot be read (e.g. in unit tests).
CONFIG_ERROR: Optional[str] = None


def _load_courses() -> dict[str, CourseConfig]:
    """Return a dict of ``{course_key: CourseConfig}`` read from attendance.yaml.

    Sheet IDs are pulled from ``st.secrets`` / env vars using the convention
    ``{KEY.upper()}_SHEET_ID``, e.g. ``CRASH_SHEET_ID``.
    """
    global CONFIG_ERROR
    if not _CONFIG_YAML.exists():
        CONFIG_ERROR = f"Configuration file not found: {_CONFIG_YAML}"
        return {}
    try:
        with _CONFIG_YAML.open(encoding="utf-8") as fh:
            raw: dict = yaml.safe_load(fh) or {}
        course_values = raw.get("attendance", {}).get("courses", {})
        if not isinstance(course_values, dict):
            raise ValueError("attendance.courses must be a mapping")
    except (OSError, yaml.YAMLError, ValueError, AttributeError) as exc:
        CONFIG_ERROR = f"Unable to load attendance configuration: {exc}"
        return {}

    courses: dict[str, CourseConfig] = {}
    for key, vals in course_values.items():
        courses[key] = CourseConfig(
            key=key,
            label=vals.get("name", key) if isinstance(vals, dict) else key,
            sheet_id=_secret(f"{key.upper()}_SHEET_ID"),
        )

    if not courses:
        CONFIG_ERROR = "attendance.courses is empty"

    return courses


# Module-level registry — imported by app.py and every page module
COURSES: dict[str, CourseConfig] = _load_courses()

# Absolute path to the workflow config — pass as config_path= in workflow calls
CONFIG_PATH: str = str(_CONFIG_YAML)


# ---------------------------------------------------------------------------
# Session type & Zoom meeting resolution helpers
# ---------------------------------------------------------------------------

def course_has_session_types(course_key: str) -> bool:
    """Return True if this course requires session types (only g9 and g10).

    Crash and Main courses use a single-column layout without session types.
    """
    return bool(course_key and course_key.lower() in ("g9", "g10"))


def get_allowed_session_types(course_key: str, session_num: int = 1) -> list[str]:
    """Return the list of allowed session types for the course and session number."""
    if not course_has_session_types(course_key):
        return []

    try:
        if _CONFIG_YAML.exists():
            with _CONFIG_YAML.open(encoding="utf-8") as fh:
                raw: dict = yaml.safe_load(fh) or {}
            course_cfg = raw.get("attendance", {}).get("courses", {}).get(course_key.lower(), {})
            types_cfg = course_cfg.get("types", {})
            if "any" in types_cfg:
                return list(types_cfg["any"])
            parity = "odd" if int(session_num) % 2 == 1 else "even"
            if parity in types_cfg:
                return list(types_cfg[parity])
    except (OSError, yaml.YAMLError, TypeError, ValueError):
        return []
    return []


def normalize_session_type(input_str: Optional[str], course_key: str, session_num: int = 1) -> Optional[str]:
    """Smartly resolve a user-typed string (e.g. 'sat center') to the exact sheet session type."""
    if not input_str or not input_str.strip():
        return None

    if not course_has_session_types(course_key):
        return None

    allowed = get_allowed_session_types(course_key, session_num)
    if not allowed:
        allowed = get_allowed_session_types(course_key, 1) + get_allowed_session_types(course_key, 2)

    cleaned = input_str.strip()

    # Exact match case-insensitive
    for opt in allowed:
        if opt.strip().lower() == cleaned.lower():
            return opt

    day_abbrevs = {
        "sat": "saturday",
        "sun": "sunday",
        "mon": "monday",
        "tue": "tuesday",
        "tues": "tuesday",
        "wed": "wednesday",
        "thu": "thursday",
        "thur": "thursday",
        "thurs": "thursday",
        "fri": "friday",
    }

    words = re.findall(r"[a-z0-9]+", cleaned.lower())
    expanded_words = [day_abbrevs.get(w, w) for w in words]
    expanded_phrase = " ".join(expanded_words)

    for opt in allowed:
        opt_words = re.findall(r"[a-z0-9]+", opt.lower())
        opt_phrase = " ".join(opt_words)
        if expanded_phrase == opt_phrase:
            return opt

    for opt in allowed:
        opt_lower = opt.lower()
        if all(w in opt_lower for w in expanded_words):
            return opt

    try:
        from rapidfuzz import fuzz, process
        best = process.extractOne(expanded_phrase, allowed, scorer=fuzz.WRatio)
        if best and best[1] >= 65:
            return best[0]
    except Exception:
        pass

    return cleaned


def get_course_meeting_id(course_key: str) -> str:
    """Return the Zoom meeting ID mapped to this course from config."""
    try:
        if _CONFIG_YAML.exists():
            with _CONFIG_YAML.open(encoding="utf-8") as fh:
                raw: dict = yaml.safe_load(fh) or {}
            mappings = raw.get("attendance", {}).get("zoom_csv", {}).get("meeting_id_mappings", {})
            for m_id, c in mappings.items():
                if str(c).lower() == str(course_key).lower():
                    return str(m_id)
    except (OSError, yaml.YAMLError, TypeError, ValueError):
        return ""
    return ""


_detected_sessions_cache: dict[str, tuple[float, int]] = {}
_DETECT_CACHE_TTL = 180.0  # cache 3 minutes


def invalidate_session_detection_cache(course_key: Optional[str] = None) -> None:
    """Invalidate cached session numbers."""
    if course_key:
        _detected_sessions_cache.pop(course_key.lower().strip(), None)
    else:
        _detected_sessions_cache.clear()


def detect_next_session_number(course_key: str, cfg: Optional[dict] = None) -> int:
    """Auto-detect the next/current session number for a given course.

    Checks:
    1. In-memory cache (TTL 3 minutes).
    2. Live Google Sheet header and student rows (finds max session with data, returns max+1).
    3. Fallback to attendance history (_history/<course>.json).
    4. Fallback to SQLite review queue items.
    5. Safe default: 1.
    """
    course_key = (course_key or "").lower().strip()
    if not course_key:
        return 1

    now = time.monotonic()
    if course_key in _detected_sessions_cache:
        cached_time, cached_val = _detected_sessions_cache[course_key]
        if now - cached_time < _DETECT_CACHE_TTL:
            return cached_val

    detected: Optional[int] = None
    try:
        from core.auth import get_credentials
        import gspread

        if cfg is None and _CONFIG_YAML.exists():
            with _CONFIG_YAML.open(encoding="utf-8") as fh:
                cfg = yaml.safe_load(fh) or {}

        course_cfg = (cfg or {}).get("attendance", {}).get("courses", {}).get(course_key, {})
        sheet_id_env = course_cfg.get("sheet_id_env", f"{course_key.upper()}_SHEET_ID")
        sheet_id = os.environ.get(sheet_id_env) or _secret(sheet_id_env)
        tab = course_cfg.get("tab")
        header_row = int(course_cfg.get("header_row", 1))
        first_student_row = int(course_cfg.get("first_student_row", 2))

        if sheet_id and tab:
            creds = get_credentials()
            gc = gspread.authorize(creds)
            ws = gc.open_by_key(sheet_id).worksheet(tab)
            all_vals = ws.get_all_values()
            if len(all_vals) >= header_row:
                header = all_vals[header_row - 1]
                if course_cfg.get("layout") == "single":
                    pat = re.compile(course_cfg.get("session_header_pattern", r"^Session (\d+)$"))
                else:
                    pat = re.compile(course_cfg.get("attendance_header_pattern", r"^S(\d+) Attendance$"))

                student_rows = all_vals[first_student_row - 1:]
                filled_sessions: list[int] = []
                max_header_session = 1

                for idx, cell in enumerate(header):
                    m = pat.match(cell.strip())
                    if m:
                        s_num = int(m.group(1))
                        if s_num > max_header_session:
                            max_header_session = s_num
                        has_data = any(row[idx].strip() for row in student_rows if idx < len(row))
                        if has_data:
                            filled_sessions.append(s_num)

                if filled_sessions:
                    detected = min(max(filled_sessions) + 1, max_header_session)
                else:
                    detected = 1
    except Exception:
        pass

    # Fallback to attendance history
    if detected is None:
        try:
            from attendance import history as history_mod
            entries = history_mod.entries("review_queue", course_key)
            valid_sessions = [int(e["session"]) for e in entries if str(e.get("session", "")).isdigit()]
            if valid_sessions:
                detected = max(valid_sessions) + 1
        except Exception:
            pass

    # Fallback to SQLite review queue
    if detected is None:
        try:
            from attendance.review_queue import ReviewQueue
            rq = ReviewQueue(store_dir="review_queue")
            items = rq.list_pending()
            rq_sessions = [int(i["session"]) for i in items if str(i.get("course", "")).lower() == course_key and str(i.get("session", "")).isdigit()]
            if rq_sessions:
                detected = max(rq_sessions)
        except Exception:
            pass

    final_val = detected if (detected and detected > 0) else 1
    _detected_sessions_cache[course_key] = (now, final_val)
    return final_val

