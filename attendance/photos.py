"""attendance/photos.py — Merge OCR rows from several photos of one attendance sheet.

A single upload may span several photos (a Telegram album, or photos sent in
quick succession). Each photo is OCR'd separately, then the rows are merged
here — *before* matching — into one list for one session.

Dedup rules
-----------
* The same name on two different photos is merged into ONE row.
* If their marks agree (or one photo has no mark), the better-read row wins.
* If their marks CONFLICT (e.g. Present vs Absent), the row is kept once and
  tagged ``conflict_marks`` so the workflow sends it to human review
  (reason ``PHOTO_MARK_CONFLICT``) instead of guessing.
* A name that appears more than once on the SAME photo is never merged: two
  students can share a first name. Ambiguous repeats across photos are likewise
  left alone; the matcher's duplicate-roster detection will flag them.
* Unreadable rows are never merged.

Every merged row keeps ``source_image`` / ``photo_index`` so review items can
show (and crop) the right photo, while all photos share one ``upload_id``.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
from typing import Optional

from attendance.normalizer import normalize_attendance_mark, normalize_for_matching
from attendance.ocr import OcrRow
from core.logger import get_logger

log = get_logger("photos")


def _dedupe_key(row: OcrRow) -> Optional[str]:
    raw = (row.raw_name or "").strip()
    if not raw or "unreadable" in raw.lower():
        return None
    key = normalize_for_matching(raw)
    return key or None


def _mark_label(mark: Optional[str]) -> str:
    """Canonical value when known, else the raw text, else 'blank'."""
    m = (mark or "").strip()
    if not m:
        return "blank"
    return normalize_attendance_mark(m) or m


def _marks_agree(a: Optional[str], b: Optional[str]) -> bool:
    la, lb = _mark_label(a), _mark_label(b)
    return la.lower() == lb.lower()


def _combine(kept: OcrRow, other: OcrRow) -> OcrRow:
    """Merge a duplicate (`other`, from a later photo) into `kept`."""
    kept_blank = not (kept.status_mark or "").strip()
    other_blank = not (other.status_mark or "").strip()

    # Already conflicted across earlier photos: just record any new distinct mark.
    if kept.conflict_marks:
        label = _mark_label(other.status_mark)
        if not other_blank and label.lower() not in {m.lower() for m in kept.conflict_marks}:
            return replace(kept, conflict_marks=[*kept.conflict_marks, label])
        return kept

    if kept_blank and other_blank:
        return kept if kept.confidence >= other.confidence else other
    if kept_blank:
        return other
    if other_blank:
        return kept
    if _marks_agree(kept.status_mark, other.status_mark):
        return kept if kept.confidence >= other.confidence else other

    log.warning(
        "photo_mark_conflict",
        extra={"photo_a": kept.photo_index, "photo_b": other.photo_index,
               "row_a": kept.row_index, "row_b": other.row_index},
    )
    return replace(kept, conflict_marks=[_mark_label(kept.status_mark), _mark_label(other.status_mark)])


def merge_photo_rows(per_photo_rows: list[list[OcrRow]], image_paths: list[str]) -> list[OcrRow]:
    """Merge rows from several photos (in photo order) into a single list."""
    if len(per_photo_rows) != len(image_paths):
        raise ValueError("per_photo_rows and image_paths must be the same length")

    merged: list[OcrRow] = []
    earlier: dict[str, list[int]] = {}      # key -> merged indexes contributed by EARLIER photos

    for photo_no, (rows, path) in enumerate(zip(per_photo_rows, image_paths), start=1):
        tagged = [replace(r, source_image=path, photo_index=photo_no) for r in rows]
        in_this_photo = Counter(k for k in (_dedupe_key(r) for r in tagged) if k)
        added_now: dict[str, list[int]] = {}

        for row in tagged:
            key = _dedupe_key(row)
            can_merge = (
                key is not None
                and in_this_photo[key] == 1
                and len(earlier.get(key, [])) == 1
            )
            if can_merge:
                idx = earlier[key][0]
                merged[idx] = _combine(merged[idx], row)
                continue
            merged.append(row)
            if key:
                added_now.setdefault(key, []).append(len(merged) - 1)

        for key, idxs in added_now.items():
            earlier.setdefault(key, []).extend(idxs)

    return merged
