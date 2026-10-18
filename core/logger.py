"""core/logger.py — Structured logging + audit trail for the attendance automation system.

The audit log records every cell write: who (upload_id), what (sheet/tab/cell/old→new),
when (ISO-8601 Africa/Cairo). It is append-only and lives in logs/audit.jsonl.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytz

CAIRO = pytz.timezone("Africa/Cairo")

# ─── Structured JSON formatter ────────────────────────────────────────────────

class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(CAIRO).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Any extra fields passed via logging.extra are included.
        for key, val in record.__dict__.items():
            if key not in (
                "msg", "args", "levelname", "levelno", "pathname",
                "filename", "module", "exc_info", "exc_text", "stack_info",
                "lineno", "funcName", "created", "msecs", "relativeCreated",
                "thread", "threadName", "processName", "process", "name",
                "taskName",
            ) and not key.startswith("_"):
                payload[key] = val
        return json.dumps(payload, ensure_ascii=False)


# ─── Module-level setup ───────────────────────────────────────────────────────

_LOG_DIR = Path(os.getenv("LOG_DIR", "logs")).resolve()
try:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    # Repo checkout is read-only (e.g. Streamlit Community Cloud mounts
    # /mount/src/... read-only) — fall back to a writable temp directory.
    import tempfile
    _LOG_DIR = Path(tempfile.gettempdir()) / "attendance-automation-logs"
    _LOG_DIR.mkdir(parents=True, exist_ok=True)

_AUDIT_PATH = _LOG_DIR / "audit.jsonl"

def _build_logger(name: str, level_str: str = "INFO") -> logging.Logger:
    level = getattr(logging, level_str.upper(), logging.INFO)
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # already configured in this process
    logger.setLevel(level)

    # Console handler — JSON lines
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(_JsonFormatter())
    logger.addHandler(ch)

    # File handler — JSON lines in logs/app.jsonl
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(_LOG_DIR / "app.jsonl", encoding="utf-8")
    fh.setFormatter(_JsonFormatter())
    logger.addHandler(fh)

    logger.propagate = False
    return logger


def get_logger(name: str = "attendance") -> logging.Logger:
    """Return (or create) the named structured logger."""
    level = os.getenv("LOG_LEVEL", "INFO")
    return _build_logger(name, level)


# ─── Audit log ───────────────────────────────────────────────────────────────

def audit_write(
    *,
    upload_id: str,
    course: str,
    sheet_id: str,
    tab: str,
    cell: str,
    old_value: str | None,
    new_value: str,
    dry_run: bool,
) -> None:
    """Append one cell-write event to the append-only audit log."""
    record = {
        "ts": datetime.now(CAIRO).isoformat(),
        "upload_id": upload_id,
        "course": course,
        "sheet_id": sheet_id,
        "tab": tab,
        "cell": cell,
        "old_value": old_value,
        "new_value": new_value,
        "dry_run": dry_run,
    }
    with _AUDIT_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
