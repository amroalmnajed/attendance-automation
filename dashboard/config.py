"""Central configuration: env loading, constants, course registry."""
from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Load .env if it exists, then .env.local if it exists
load_dotenv()
env_local_path = os.path.join(os.path.dirname(__file__), ".env.local")
if os.path.exists(env_local_path):
    load_dotenv(env_local_path)

# Also try to load sheet IDs from parent .env if not set
parent_env = os.path.join(os.path.dirname(__file__), "..", ".env")
if os.path.exists(parent_env):
    load_dotenv(parent_env, override=False)


# ── Secrets resolution (st.secrets in cloud, env locally) ──────────────

def _secret(name: str) -> str:
    """Look up a secret in st.secrets first, then environment."""
    try:
        import streamlit as st  # local import so this module is importable from scripts
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        # Not running under Streamlit — fall through to env.
        pass
    # Always check env as fallback
    return os.getenv(name, "")


@dataclass(frozen=True)
class CourseConfig:
    """One course = one spreadsheet + one main tab + one review tab."""
    key: str
    display_name: str
    sheet_id: str
    tab_name: str
    review_tab: str
    session_pattern: str
    attendance_pattern: str | None  # For typed_pair layout (g9/g10)


# Course registry — matches the bot's env vars.
# Courses with an empty sheet_id are hidden from the UI.
COURSES: list[CourseConfig] = [
    CourseConfig(
        key="crash",
        display_name="Crash Course",
        sheet_id=_secret("CRASH_SHEET_ID"),
        tab_name=_secret("CRASH_TAB") or "Attendance",
        review_tab=_secret("REVIEW_TAB") or "ReviewQueue",
        session_pattern=_secret("CRASH_SESSION_PATTERN") or r"^Session\s+\d+$",
        attendance_pattern=None,
    ),
    CourseConfig(
        key="main",
        display_name="Main Course",
        sheet_id=_secret("MAIN_SHEET_ID"),
        tab_name=_secret("MAIN_TAB") or "Attendance",
        review_tab=_secret("REVIEW_TAB") or "ReviewQueue",
        session_pattern=_secret("MAIN_SESSION_PATTERN") or r"^Session\s+\d+$",
        attendance_pattern=None,
    ),
    CourseConfig(
        key="g9",
        display_name="Grade 9",
        sheet_id=_secret("G9_SHEET_ID"),
        tab_name=_secret("G9_TAB") or "Attendance",
        review_tab=_secret("REVIEW_TAB") or "ReviewQueue",
        session_pattern=_secret("G9_SESSION_PATTERN") or r"^S\d+$",
        attendance_pattern=_secret("G9_ATTENDANCE_PATTERN") or r"^S\d+\s+Attendance$",
    ),
    CourseConfig(
        key="g10",
        display_name="Grade 10",
        sheet_id=_secret("G10_SHEET_ID"),
        tab_name=_secret("G10_TAB") or "Attendance",
        review_tab=_secret("REVIEW_TAB") or "ReviewQueue",
        session_pattern=_secret("G10_SESSION_PATTERN") or r"^S\d+$",
        attendance_pattern=_secret("G10_ATTENDANCE_PATTERN") or r"^S\d+\s+Attendance$",
    ),
]


def get_course(key: str) -> CourseConfig:
    for course in COURSES:
        if course.key == key:
            return course
    raise KeyError(f"Unknown course: {key}")


# Status values the dashboard recognises inside a session cell.
STATUS_PRESENT = "Present"
STATUS_ABSENT = "Absent"
STATUS_NEEDS_REVIEW = "Needs Review"

# Session header regex (matches the bot's default).
SESSION_PATTERN = os.getenv("SESSION_HEADER_PATTERN", r"^Session\s+\d+$")

# Seconds a sheet read stays cached before a fresh fetch is forced.
CACHE_TTL_SECONDS = 60

# How many candidate names the review UI offers per item.
CANDIDATE_COUNT = 3


def get_credentials_b64() -> str:
    value = _secret("GOOGLE_CREDENTIALS_B64")
    if not value:
        raise RuntimeError(
            "GOOGLE_CREDENTIALS_B64 is not set. "
            "Add it to .env (local) or st.secrets (cloud)."
        )
    return value


def get_credentials_json() -> dict:
    """Decode the base64 blob into the service-account JSON dict."""
    raw = get_credentials_b64()
    return json.loads(base64.b64decode(raw).decode("utf-8"))


def get_password() -> str:
    value = _secret("DASHBOARD_PASSWORD") or _secret("password")
    if not value:
        raise RuntimeError("Dashboard password is not set (DASHBOARD_PASSWORD / st.secrets.password).")
    return value
