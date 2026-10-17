"""webapp/config.py — Course and secret configuration for the webapp.

Ported from dashboard/config.py's CourseConfig / _secret() / COURSES pattern.
Reads course keys and display names from config/attendance.yaml (ground truth)
instead of hard-coding them, so the webapp never drifts from the pipeline config.
Falls back to the known course keys (crash/main/g9/g10) when the YAML is absent
(e.g. during unit tests that do not have the full repo mounted).
"""
from __future__ import annotations

import os
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
_FALLBACK_KEYS: tuple[str, ...] = ("crash", "main", "g9", "g10")


def _load_courses() -> dict[str, CourseConfig]:
    """Return a dict of ``{course_key: CourseConfig}`` read from attendance.yaml.

    Sheet IDs are pulled from ``st.secrets`` / env vars using the convention
    ``{KEY.upper()}_SHEET_ID``, e.g. ``CRASH_SHEET_ID``.
    """
    if not _CONFIG_YAML.exists():
        # Fallback: use known keys so the app starts without the full repo
        return {
            key: CourseConfig(
                key=key,
                label=key.upper(),
                sheet_id=_secret(f"{key.upper()}_SHEET_ID"),
            )
            for key in _FALLBACK_KEYS
        }

    with _CONFIG_YAML.open(encoding="utf-8") as fh:
        raw: dict = yaml.safe_load(fh) or {}

    courses: dict[str, CourseConfig] = {}
    for key, vals in raw.get("courses", {}).items():
        courses[key] = CourseConfig(
            key=key,
            label=vals.get("name", key) if isinstance(vals, dict) else key,
            sheet_id=_secret(f"{key.upper()}_SHEET_ID"),
        )

    # If YAML had no courses block at all, fall back gracefully
    if not courses:
        return {
            key: CourseConfig(
                key=key,
                label=key.upper(),
                sheet_id=_secret(f"{key.upper()}_SHEET_ID"),
            )
            for key in _FALLBACK_KEYS
        }

    return courses


# Module-level registry — imported by app.py and every page module
COURSES: dict[str, CourseConfig] = _load_courses()

# Absolute path to the pipeline config — pass as config_path= in pipeline calls
CONFIG_PATH: str = str(_CONFIG_YAML)
