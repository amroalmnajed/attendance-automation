"""Per-course attendance history for the post-write summary comparison (Feature 6).

Stores COUNTS ONLY (never student names) in ``<review_dir>/_history/<course>.json``,
so previous sessions never have to be re-read from Google Sheets.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from attendance._fslock import atomic_write_json, file_lock
from core.logger import get_logger

log = get_logger("history")

KEEP_ENTRIES = 20


def settings(cfg: dict) -> dict:
    """Effective ``attendance.summary_compare`` settings (safe defaults)."""
    sect = ((cfg or {}).get("attendance", {}) or {}).get("summary_compare", {}) or {}
    return {
        "enabled": bool(sect.get("enabled", True)),
        "drop_threshold_pct": float(sect.get("drop_threshold_pct", 30)),
        "min_previous": int(sect.get("min_previous", 5)),
    }


def classify(value) -> Optional[str]:
    """'present' / 'late' / 'absent' for a canonical sheet value, else None."""
    v = str(value or "").strip().lower()
    return v if v in ("present", "late", "absent") else None


def count_marks(values) -> dict:
    counts = {"present": 0, "late": 0, "absent": 0}
    for v in values:
        k = classify(v)
        if k:
            counts[k] += 1
    return counts


def _path(review_dir: str, course: str) -> Path:
    safe = "".join(c for c in str(course).lower() if c.isalnum() or c in "-_") or "course"
    return Path(review_dir) / "_history" / f"{safe}.json"


def _load(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def entries(review_dir: str, course: str) -> list[dict]:
    """All recorded entries for a course (oldest first). Read-only; never raises."""
    return _load(_path(review_dir, course))


def previous(review_dir: str, course: str, session) -> Optional[dict]:
    """Most recent recorded entry for this course from a DIFFERENT session."""
    for e in reversed(_load(_path(review_dir, course))):
        if str(e.get("session")) != str(session) and isinstance(e.get("counts"), dict):
            return e
    return None


def record(review_dir: str, course: str, session, upload_id: str, counts: dict, review: int) -> None:
    """Remember this session's counts (a re-upload of the same session replaces its entry)."""
    path = _path(review_dir, course)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path):
        entries = [e for e in _load(path) if str(e.get("session")) != str(session)]
        entries.append({
            "session": str(session),
            "upload_id": upload_id,
            "counts": {**counts, "review": int(review)},
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        atomic_write_json(path, entries[-KEEP_ENTRIES:])


def drop_warning(counts: dict, prev: Optional[dict], s: dict) -> str:
    """Plain-language warning when attendance (Present+Late) fell sharply vs the previous session."""
    if not prev:
        return ""
    pc = prev.get("counts") or {}
    prev_att = int(pc.get("present", 0)) + int(pc.get("late", 0))
    now_att = int(counts.get("present", 0)) + int(counts.get("late", 0))
    if prev_att < s["min_previous"] or prev_att <= 0:
        return ""
    drop = (prev_att - now_att) * 100.0 / prev_att
    if drop >= s["drop_threshold_pct"]:
        return f"Attendance is down {drop:.0f}% vs the previous session ({prev_att} → {now_att}). Check the photo is complete."
    return ""
