"""attendance/absent.py — "Mark missing students absent" (crash and main courses ONLY).

After an upload, the bot can offer to mark every roster student who was not on
the sheet as Absent. This module owns the data side of that:

* ``record_pending``  — called by the workflow after an upload; saves what the
  button needs (no student names; row numbers only) and returns the counts.
* ``mark_missing_absent`` — called when the button is tapped; re-reads the live
  sheet and writes "Absent" through the normal ``batch_write`` path.

Safety
------
* g9 / g10 can never be offered, whatever the config says (``HARD_EXCLUDED_COURSES``).
* Only EMPTY cells are written. A cell that already has any value is skipped, so
  ``overwrite: refuse`` is respected (there is no force path here).
* The upload's own dry-run flag is honoured: a dry-run upload only previews.
* Real writes go through ``batch_write`` and are audited under their own
  upload id (``abs_upload_id``), so ``/revert`` works on them.
* ``global missing_student_policy`` is never read or changed.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from attendance._fslock import atomic_write_json, file_lock
from attendance._net import retry_call
from core.logger import get_logger

log = get_logger("absent")

HARD_EXCLUDED_COURSES = frozenset({"g9", "g10"})
DEFAULT_COURSES = ("crash", "main")


# ─── Settings ─────────────────────────────────────────────────────────────────

def settings(cfg: dict) -> dict:
    """Effective ``attendance.absent_button`` settings (safe defaults)."""
    sect = ((cfg or {}).get("attendance", {}) or {}).get("absent_button", {}) or {}
    courses = {str(c).lower() for c in (sect.get("courses") or DEFAULT_COURSES)}
    return {
        "enabled": bool(sect.get("enabled", True)),
        "courses": courses - HARD_EXCLUDED_COURSES,
        "min_matched": int(sect.get("min_matched", 3)),
        "block_when_pending_review": bool(sect.get("block_when_pending_review", True)),
    }


def is_offered(cfg: dict, course: str) -> bool:
    s = settings(cfg)
    return s["enabled"] and course.lower() in s["courses"]


def abs_upload_id(upload_id: str) -> str:
    """Audit id for the absent write. 16 chars, derived from the upload id."""
    return ("a" + upload_id)[:16]


# ─── Pending-offer store (sidecar files, row numbers only) ────────────────────

def _store_dir(review_dir: str | Path) -> Path:
    return Path(review_dir) / "_absent"


def _path(review_dir: str | Path, upload_id: str) -> Path:
    return _store_dir(review_dir) / f"{upload_id}.json"


def load_pending(review_dir: str | Path, upload_id: str) -> Optional[dict]:
    p = _path(review_dir, upload_id)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def compute_missing(
    roster: list[tuple[str, int]],
    col_data: list[str],
    matched_rows: set[int],
    candidate_rows: set[int],
    *,
    exclude_candidates: bool,
) -> list[tuple[str, int]]:
    """Roster students with an empty cell who were not matched on the sheet."""
    missing: list[tuple[str, int]] = []
    for name, row in roster:
        current = col_data[row - 1] if 0 < row <= len(col_data) else ""
        if current.strip():
            continue                      # already has a value: never touched
        if row in matched_rows:
            continue
        if exclude_candidates and row in candidate_rows:
            continue
        missing.append((name, row))
    return missing


def record_pending(
    *,
    cfg: dict,
    review_dir: str | Path,
    upload_id: str,
    course: str,
    session_num: int,
    session_type: Optional[str],
    dry_run: bool,
    roster: list[tuple[str, int]],
    col_data: list[str],
    matched_rows: set[int],
    candidate_rows: set[int],
) -> tuple[bool, int, int]:
    """Decide whether to offer the button. Returns (offer, missing_count, held_back)."""
    if not is_offered(cfg, course):
        return False, 0, 0
    s = settings(cfg)
    if len(matched_rows) < s["min_matched"]:
        # Garbled/blank/mostly-unreadable upload: never suggest marking the class absent.
        return False, 0, 0

    strict = compute_missing(roster, col_data, matched_rows, candidate_rows, exclude_candidates=True)
    loose = compute_missing(roster, col_data, matched_rows, candidate_rows, exclude_candidates=False)
    if not strict:
        return False, 0, len(loose)

    record = {
        "upload_id": upload_id,
        "course": course,
        "session": session_num,
        "session_type": session_type,
        "dry_run": bool(dry_run),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "matched_rows": sorted(matched_rows),
        "candidate_rows": sorted(candidate_rows),
    }
    p = _path(review_dir, upload_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(p):
        atomic_write_json(p, record)
    return True, len(strict), len(loose) - len(strict)


# ─── The button action ────────────────────────────────────────────────────────

@dataclass
class AbsentResult:
    ok: bool
    reason: str = ""              # "", "expired", "disabled", "pending_review", "value_not_allowed", "nothing_to_mark"
    marked: int = 0               # cells written (or that WOULD be written in dry-run)
    already_filled: int = 0       # skipped because the cell had a value
    pending_review: int = 0
    dry_run: bool = False
    abs_id: str = ""


def mark_missing_absent(
    upload_id: str,
    *,
    cfg: dict,
    review_dir: str | Path,
    session_info=None,            # injectable for tests; resolved live otherwise
) -> AbsentResult:
    from attendance.review_queue import ReviewQueue
    from attendance.session_resolver import resolve
    from attendance.utils.column_utils import col_letter_to_index
    from attendance.writer import WriteCell, batch_write

    rec = load_pending(review_dir, upload_id)
    if rec is None:
        return AbsentResult(ok=False, reason="expired")

    course = rec["course"]
    if not is_offered(cfg, course):
        return AbsentResult(ok=False, reason="disabled")

    s = settings(cfg)
    dry_run = bool(rec.get("dry_run", True))
    abs_id = abs_upload_id(upload_id)

    if s["block_when_pending_review"]:
        pending = len(ReviewQueue(store_dir=str(review_dir)).list_pending(upload_id))
        if pending:
            return AbsentResult(ok=False, reason="pending_review", pending_review=pending, dry_run=dry_run)

    retry_cfg = (cfg.get("attendance", {}).get("write", {}) or {}).get("retry", {}) or {}
    if session_info is None:
        session_info = resolve(
            course_key=course, session_num=int(rec["session"]),
            session_type=rec.get("session_type"), cfg=cfg,
        )

    absent_value = next((v for v in session_info.allowed_attendance if v.strip().lower() == "absent"), None)
    if absent_value is None:
        return AbsentResult(ok=False, reason="value_not_allowed", dry_run=dry_run)

    ws = session_info.get_worksheet()
    col_idx = col_letter_to_index(session_info.session_col)
    col_data = retry_call(
        lambda: ws.col_values(col_idx), op="sheets_read_col",
        max_attempts=int(retry_cfg.get("max_attempts", 6)),
        base_s=float(retry_cfg.get("base_s", 1.0)),
        cap_s=float(retry_cfg.get("cap_s", 32.0)),
        jitter=bool(retry_cfg.get("jitter", True)),
        context={"upload_id": upload_id},
    )

    matched = set(rec.get("matched_rows", []))
    candidates = set(rec.get("candidate_rows", []))
    # Real mode: the live sheet is the truth (resolved review items now have values).
    # Dry-run: nothing was written, so fall back to the candidate exclusion.
    missing = compute_missing(session_info.roster, col_data, matched, candidates, exclude_candidates=dry_run)
    filled = sum(
        1 for _, row in session_info.roster
        if 0 < row <= len(col_data) and col_data[row - 1].strip() and row not in matched
    )

    if not missing:
        return AbsentResult(ok=False, reason="nothing_to_mark", already_filled=filled, dry_run=dry_run, abs_id=abs_id)

    cells = [
        WriteCell(roster_name=name, sheet_row=row, col=session_info.session_col,
                  old_value=None, new_value=absent_value)
        for name, row in missing
    ]
    result = batch_write(session_info, cells, upload_id=abs_id, dry_run=dry_run, retry_cfg=retry_cfg)
    log.info("absent_marked", extra={"upload_id": upload_id, "abs_id": abs_id,
                                      "marked": len(result.written), "dry_run": dry_run})
    return AbsentResult(ok=True, marked=len(result.written), already_filled=filled,
                        dry_run=dry_run, abs_id=abs_id)
