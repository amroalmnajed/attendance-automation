"""attendance/config_check.py — fail fast on config mistakes that only bite at runtime.

Replaced by Pydantic model validation in attendance/config_model.py (T10).
Preserves backward compatibility for all existing callers and tests.
"""
from __future__ import annotations

from attendance.config_model import (
    ConfigError,
    _types_ok,
    config_problems,
    validate_cfg,
)

__all__ = ["ConfigError", "_types_ok", "config_problems", "validate_cfg"]
