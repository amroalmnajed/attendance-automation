"""attendance/pipeline.py — Top-level orchestrator for Sub-System 1.

Ties together: preprocess → OCR (2 passes) → normalise → match → gate → write/review.
Returns a RunSummary suitable for the notifier.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Optional

import yaml

from attendance.gates import check_all_gates
from attendance.aliases import AliasStore
from attendance.matcher import MatchDecision, MatchResult, ReasonCode, Candidate, build_matcher
from attendance.normalizer import normalize_attendance_mark
from attendance.ocr import OcrRow, build_provider
from attendance.preprocess import prepare
from attendance.review_queue import ReviewQueue
from attendance.session_resolver import SheetSession, resolve
from attendance.writer import WriteCell, batch_write
from core.logger import get_logger
from core.notifier import RunSummary

log = get_logger("pipeline")


def load_config(path: str = "config/attendance.yaml") -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        raise RuntimeError(f"Configuration file not found: {path}")


def _upload_id(image_bytes: bytes, course: str, session: int) -> str:
    h = hashlib.sha256(image_bytes + course.encode() + str(session).encode())
    return h.hexdigest()[:16]


def run(
    *,
    image_path: str,
    course: str,
    session_num: int,
    session_type: Optional[str] = None,
    dry_run: bool = True,
    force: bool = False,
    config_path: str = "config/attendance.yaml",
    review_store_dir: str = "review_queue",
) -> RunSummary:
    """Execute the full attendance pipeline for a single image."""
    cfg = load_config(config_path)
    att_cfg = cfg.get("attendance", {})
    ocr_cfg = att_cfg.get("ocr", {})
    preprocess_cfg = ocr_cfg.get("preprocess", {})

    try:
        raw_bytes = Path(image_path).read_bytes()
        upload_id = _upload_id(raw_bytes, course, session_num)
    except FileNotFoundError:
        upload_id = "error_missing_file"
        summary = RunSummary(upload_id=upload_id, course=course, session=str(session_num), dry_run=dry_run)
        summary.errors.append(f"Image file not found: {image_path}")
        return summary
    except Exception as e:
        upload_id = "error_reading_file"
        summary = RunSummary(upload_id=upload_id, course=course, session=str(session_num), dry_run=dry_run)
        summary.errors.append(f"Failed to read image file: {str(e)}")
        return summary

    log.info("pipeline_start", extra={"upload_id": upload_id, "image": image_path, "dry_run": dry_run})

    summary = RunSummary(
        upload_id=upload_id,
        course=course,
        session=str(session_num),
        dry_run=dry_run,
    )

    image_bytes = prepare(
        raw_bytes,
        orient=preprocess_cfg.get("orient", True),
        deskew=preprocess_cfg.get("deskew", True),
        contrast=preprocess_cfg.get("contrast", "mild"),
        max_side_px=int(preprocess_cfg.get("max_side_px", 2000)),
        strip_exif=preprocess_cfg.get("strip_exif", True),
    )

    provider = build_provider(ocr_cfg)
    min_conf = float(ocr_cfg.get("min_confidence", 0.75))

    # Resolve the session/roster up front (instead of after OCR) when the OCR
    # step is allowed to see the roster — it needs the real names *before* it
    # reads the photo to have any chance of telling similar students apart.
    session_info: Optional[SheetSession] = None
    roster_names: Optional[list[str]] = None
    if ocr_cfg.get("send_roster_to_provider", False):
        try:
            session_info = resolve(course_key=course, session_num=session_num, session_type=session_type, cfg=cfg)
            roster_names = [name for name, _ in session_info.roster]
        except Exception as exc:
            # Don't let an early resolve failure kill the run here — fall back to
            # resolving again (and surfacing the real error) inside _process_rows.
            log.warning("early_session_resolve_failed", extra={"error": str(exc), "upload_id": upload_id})
            session_info = None
            roster_names = None

    log.info("ocr_pass_1")
    try:
        rows_pass1 = provider.read(image_bytes, roster=roster_names)
    except Exception as exc:
        log.error("ocr_api_error", extra={"error": str(exc), "upload_id": upload_id})
        summary.errors.append(f"API_ERROR: OCR pass 1 failed: {exc}")
        return summary

    rows_pass2 = []
    n_passes = int(ocr_cfg.get("passes", 2))
    if n_passes >= 2:
        log.info("ocr_pass_2")
        try:
            rows_pass2 = provider.read(image_bytes, roster=roster_names)
        except Exception as exc:
            log.warning("ocr_pass2_failed", extra={"error": str(exc)})

    ocr_rows = _merge_passes(rows_pass1, rows_pass2)

    return _process_rows(
        ocr_rows=ocr_rows,
        course=course,
        session_num=session_num,
        session_type=session_type,
        dry_run=dry_run,
        force=force,
        cfg=cfg,
        min_conf=min_conf,
        upload_id=upload_id,
        image_path=image_path,
        review_store_dir=review_store_dir,
        summary=summary,
        session_info=session_info,
    )


def run_text(
    *,
    names: list[str],
    course: str,
    session_num: int,
    session_type: Optional[str] = None,
    dry_run: bool = True,
    force: bool = False,
    config_path: str = "config/attendance.yaml",
    review_store_dir: str = "review_queue",
) -> RunSummary:
    """Execute the attendance pipeline using a provided list of names."""
    cfg = load_config(config_path)
    
    upload_id = hashlib.sha256(str(names).encode() + course.encode() + str(session_num).encode()).hexdigest()[:16]
    log.info("pipeline_text_start", extra={"upload_id": upload_id, "names_count": len(names), "dry_run": dry_run})

    summary = RunSummary(
        upload_id=upload_id,
        course=course,
        session=str(session_num),
        dry_run=dry_run,
    )

    ocr_rows = [
        OcrRow(raw_name=name.strip(), status_mark="Present", confidence=1.0, row_index=i+1)
        for i, name in enumerate(names) if name.strip()
    ]

    return _process_rows(
        ocr_rows=ocr_rows,
        course=course,
        session_num=session_num,
        session_type=session_type,
        dry_run=dry_run,
        force=force,
        cfg=cfg,
        min_conf=1.0,
        upload_id=upload_id,
        image_path="text_input",
        review_store_dir=review_store_dir,
        summary=summary,
    )


def _process_rows(
    *,
    ocr_rows: list[OcrRow],
    course: str,
    session_num: int,
    session_type: Optional[str],
    dry_run: bool,
    force: bool,
    cfg: dict,
    min_conf: float,
    upload_id: str,
    image_path: str,
    review_store_dir: str,
    summary: RunSummary,
    session_info: Optional[SheetSession] = None,
) -> RunSummary:
    att_cfg = cfg["attendance"]
    match_cfg = att_cfg["match"]
    write_cfg = att_cfg["write"]

    if session_info is None:
        session_info = resolve(
            course_key=course,
            session_num=session_num,
            session_type=session_type,
            cfg=cfg,
        )

    ws = session_info.get_worksheet()
    summary.sheet_url = f"https://docs.google.com/spreadsheets/d/{session_info.sheet_id}/edit#gid={ws.id}"

    col_range = f"{session_info.session_col}:{session_info.session_col}"
    col_data = ws.col_values(_col_idx(session_info.session_col))

    alias_store = AliasStore(
        path=match_cfg.get("aliases_file", "config/learned_aliases.json"),
        min_confirmations=int(match_cfg.get("alias_min_confirmations", 1)),
    ) if match_cfg.get("learn_aliases", True) else None
    
    matcher = build_matcher(
        match_cfg,
        alias_lookup=(lambda name: alias_store.lookup(course, name)) if alias_store else None,
    )
    matcher.reset()
    review_queue = ReviewQueue(store_dir=review_store_dir)

    cells_to_write: list[WriteCell] = []
    NOISE_WORDS = {"name", "student", "student name", "الاسم", "اسم", "اسم الطالب", "م"}
    MIN_CHARS = 2

    for ocr_row in ocr_rows:
        raw_stripped = ocr_row.raw_name.strip()
        raw_clean = raw_stripped.lower()
        if not raw_clean or len(raw_stripped) < MIN_CHARS or raw_clean in NOISE_WORDS:
            continue

        match_result = matcher.match(
            raw_name=ocr_row.raw_name,
            ocr_confidence=ocr_row.confidence,
            roster=session_info.roster,
            paper_row_index=ocr_row.row_index,
            min_confidence=min_conf,
            ocr_suggested_name=ocr_row.matched_roster_name,
            ocr_suggested_confidence=ocr_row.confidence,
        )

        if match_result.decision != MatchDecision.AUTO_ACCEPT:
            review_queue.enqueue(
                upload_id=upload_id,
                course=course,
                session=session_num,
                image_ref=image_path,
                ocr_row=ocr_row,
                match_result=match_result,
                raw_mark=ocr_row.status_mark,
                session_type=session_type,
            )
            summary.flagged += 1
            continue

        raw_mark = ocr_row.status_mark or "Present"
        canonical = normalize_attendance_mark(raw_mark)

        roster_row = match_result.best.roster_row
        if roster_row <= 0 or roster_row > len(col_data):
            current = ""
        else:
            current = col_data[roster_row - 1]

        gate = check_all_gates(
            match_result=match_result,
            raw_mark=raw_mark,
            allowed_attendance=session_info.allowed_attendance,
            current_cell_value=current,
            force=force,
        )

        if not gate.passed:
            review_queue.enqueue(
                upload_id=upload_id,
                course=course,
                session=session_num,
                image_ref=image_path,
                ocr_row=ocr_row,
                match_result=match_result,
                raw_mark=raw_mark,
                session_type=session_type,
                reason=gate.reason,
            )
            summary.flagged += 1
            continue

        summary.matched += 1

        cells_to_write.append(WriteCell(
            roster_name=match_result.best.roster_name,
            sheet_row=roster_row,
            col=session_info.session_col,
            old_value=current or None,
            new_value=gate.canonical_value,
        ))

        if session_info.type_col and session_type:
            type_current_data = ws.col_values(_col_idx(session_info.type_col))
            type_current = type_current_data[roster_row - 1] if roster_row <= len(type_current_data) else ""
            if not type_current.strip() or force:
                cells_to_write.append(WriteCell(
                    roster_name=match_result.best.roster_name,
                    sheet_row=roster_row,
                    col=session_info.type_col,
                    old_value=type_current or None,
                    new_value=session_type,
                ))

    if ocr_rows and all(not r.status_mark for r in ocr_rows):
        msg = "OCR returned no attendance marks for any row; defaulted every row to 'Present'. Verify the sheet."
        log.warning("all_marks_null", extra={"upload_id": upload_id})
        summary.errors.append(msg)

    write_result = batch_write(
        session_info,
        cells_to_write,
        upload_id=upload_id,
        dry_run=dry_run,
        retry_cfg=write_cfg.get("retry", {}),
    )

    summary.written = len(write_result.written)
    summary.unmatched = max(0, len(ocr_rows) - summary.matched - summary.flagged)

    if summary.flagged > 0:
        summary.review_url = str(review_queue._dir / f"{upload_id}.json")

    log.info(
        "pipeline_done",
        extra={
            "upload_id": upload_id,
            "matched": summary.matched,
            "written": summary.written,
            "flagged": summary.flagged,
        },
    )

    if summary.flagged > 0:
        review_queue.print_pending(upload_id)

    return summary


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _col_idx(col_letter: str) -> int:
    """Column letter → 1-based index."""
    result = 0
    for ch in col_letter.upper():
        result = result * 26 + (ord(ch) - ord("A") + 1)
    return result


def _merge_passes(pass1: list[OcrRow], pass2: list[OcrRow]) -> list[OcrRow]:
    """Merge two OCR passes.
    - If pass2 is empty, return pass1.
    - If names differ between passes for the same row index, downgrade confidence.
    - Otherwise average confidence.
    """
    if not pass2:
        return pass1

    p2_by_idx = {row.row_index: row for row in pass2}
    merged = []

    for row in pass1:
        p2 = p2_by_idx.get(row.row_index)
        if p2 is None:
            merged.append(row)
            continue

        # Compare normalised names
        from attendance.normalizer import normalize_for_matching
        n1 = normalize_for_matching(row.raw_name)
        n2 = normalize_for_matching(p2.raw_name)

        if n1 == n2:
            # Agreement: average confidence
            merged_conf = (row.confidence + p2.confidence) / 2
            # Roster match: keep it only if both passes agree (or only one offered
            # one); two different guesses on an agreed name is itself a signal of
            # ambiguity, so drop it and let the matcher's own gates handle it.
            if row.matched_roster_name and p2.matched_roster_name:
                n_m1 = normalize_for_matching(row.matched_roster_name)
                n_m2 = normalize_for_matching(p2.matched_roster_name)
                matched = row.matched_roster_name if n_m1 == n_m2 else None
            else:
                matched = row.matched_roster_name or p2.matched_roster_name
            merged.append(OcrRow(
                raw_name=row.raw_name,
                status_mark=row.status_mark,
                confidence=merged_conf,
                row_index=row.row_index,
                bounding_box=row.bounding_box or p2.bounding_box,
                matched_roster_name=matched,
            ))
        else:
            # Disagreement: downgrade confidence below min_confidence
            log.warning(
                "ocr_pass_disagree",
                extra={
                    "row": row.row_index,
                    "pass1": row.raw_name,
                    "pass2": p2.raw_name,
                },
            )
            merged.append(OcrRow(
                raw_name=row.raw_name,
                status_mark=row.status_mark,
                confidence=min(row.confidence, p2.confidence) * 0.5,  # force below threshold
                row_index=row.row_index,
                bounding_box=row.bounding_box or p2.bounding_box,
                matched_roster_name=None,
            ))

    return merged
