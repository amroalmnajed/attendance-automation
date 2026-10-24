"""Shared post-OCR row processing logic.

This module contains the workflow-agnostic logic that runs after OCR results
are available. It handles:
  - Session resolution
  - Matching against roster with alias learning
  - Gate checks (cell empty, value allowed, etc.)
  - Review queue enqueuing for uncertain matches
  - Batch writing to Google Sheets
  - History tracking
  - Absent-student offer
  - Alias usage flushing
  - Summary completion

Used by image, text, and Zoom workflows.

Entry point:
  - process_rows(): Main function taking OCR rows and returning RunSummary
"""
from __future__ import annotations

from typing import Optional

from attendance import aliases as aliases_mod
from attendance import absent as absent_mod
from attendance import history as history_mod
from attendance.aliases import AliasStore
from attendance.gates import check_all_gates
from attendance.matcher import MatchDecision, MatchResult, ReasonCode, build_matcher
from attendance.ocr import OcrRow
from attendance.session_resolver import resolve
from attendance.review_queue import ReviewQueue
from attendance.session_resolver import SheetSession, resolve
from attendance.utils.column_utils import col_letter_to_index
from attendance.utils.progress import ProgressFn, STAGE_MATCHING, STAGE_SAVING, _emit
from attendance.writer import WriteCell, batch_write
from attendance.marks import normalize_attendance_mark
from core.logger import get_logger

log = get_logger("row_processor")


def _note_candidates(match_result: MatchResult, candidate_rows: set[int]) -> None:
    """Remember roster rows a flagged item might refer to (so they are not called 'missing')."""
    if match_result.best is not None and match_result.best.roster_row > 0:
        candidate_rows.add(match_result.best.roster_row)
    for cand in match_result.top3:
        if cand.roster_row > 0:
            candidate_rows.add(cand.roster_row)


def process_rows(
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
    summary,
    session_info: Optional[SheetSession] = None,
    progress: Optional[ProgressFn] = None,
    match_cfg_overrides: Optional[dict] = None,
):
    """Process OCR rows: match, gate, write, and track attendance.

    This is the shared post-OCR logic used by image, text, and Zoom workflows.
    """
    att_cfg = cfg.get("attendance", {})
    # Allow callers (e.g. the Zoom path) to override specific matcher knobs without
    # touching the global config.  Overrides are merged on top of the base config.
    match_cfg = {**att_cfg.get("match", {}), **(match_cfg_overrides or {})}
    write_cfg = att_cfg.get("write", {})

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
    col_data = ws.col_values(col_letter_to_index(session_info.session_col))

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
    matched_rows: set[int] = set()       # roster rows accepted from this upload
    candidate_rows: set[int] = set()     # roster rows that flagged items might refer to
    NOISE_WORDS = {"name", "student", "student name", "الاسم", "اسم", "اسم الطالب", "م"}
    MIN_CHARS = 2

    _emit(progress, STAGE_MATCHING)
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

        if ocr_row.conflict_marks:
            # Same name on 2+ photos with different marks: never guess, a human decides.
            _note_candidates(match_result, candidate_rows)
            review_queue.enqueue(
                upload_id=upload_id,
                course=course,
                session=session_num,
                image_ref=ocr_row.source_image or image_path,
                ocr_row=ocr_row,
                match_result=match_result,
                raw_mark=" / ".join(ocr_row.conflict_marks),
                session_type=session_type,
                reason=ReasonCode.PHOTO_MARK_CONFLICT,
            )
            summary.flagged += 1
            continue

        if match_result.decision != MatchDecision.AUTO_ACCEPT:
            _note_candidates(match_result, candidate_rows)
            review_queue.enqueue(
                upload_id=upload_id,
                course=course,
                session=session_num,
                image_ref=ocr_row.source_image or image_path,
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
            _note_candidates(match_result, candidate_rows)
            review_queue.enqueue(
                upload_id=upload_id,
                course=course,
                session=session_num,
                image_ref=ocr_row.source_image or image_path,
                ocr_row=ocr_row,
                match_result=match_result,
                raw_mark=raw_mark,
                session_type=session_type,
                reason=gate.reason,
            )
            summary.flagged += 1
            continue

        summary.matched += 1
        matched_rows.add(roster_row)

        cells_to_write.append(WriteCell(
            roster_name=match_result.best.roster_name,
            sheet_row=roster_row,
            col=session_info.session_col,
            old_value=current or None,
            new_value=gate.canonical_value,
        ))

        if session_info.type_col and session_type:
            type_current_data = ws.col_values(col_letter_to_index(session_info.type_col))
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

    _emit(progress, STAGE_SAVING)
    write_result = batch_write(
        session_info,
        cells_to_write,
        upload_id=upload_id,
        dry_run=dry_run,
        retry_cfg=write_cfg.get("retry", {}),
    )

    summary.written = len(write_result.written)
    summary.unmatched = max(0, len(ocr_rows) - summary.matched - summary.flagged)

    # Feature 6: Present/Late/Absent counts + comparison with the previous session (counts only).
    try:
        hs = history_mod.settings(cfg)
        if hs["enabled"]:
            counts = history_mod.count_marks(
                c.new_value for c in cells_to_write if c.col == session_info.session_col
            )
            summary.present_count, summary.late_count, summary.absent_count = (
                counts["present"], counts["late"], counts["absent"])
            prev = history_mod.previous(review_store_dir, course, session_num)
            if prev:
                summary.prev_counts = dict(prev.get("counts") or {})
                summary.prev_session = str(prev.get("session", ""))
                summary.drop_warning = history_mod.drop_warning(counts, prev, hs)
            if not dry_run and sum(counts.values()) > 0:
                history_mod.record(review_store_dir, course, session_num, upload_id, counts, summary.flagged)
    except Exception as exc:   # an optional extra must never fail an upload that already succeeded
        log.warning("summary_compare_failed", extra={"upload_id": upload_id, "error": str(exc)})

    # "Mark missing students absent?" offer: crash/main only, never changes the sheet here.
    try:
        offer, missing_n, held_n = absent_mod.record_pending(
            cfg=cfg,
            review_dir=review_store_dir,
            upload_id=upload_id,
            course=course,
            session_num=session_num,
            session_type=session_type,
            dry_run=dry_run,
            roster=session_info.roster,
            col_data=col_data,
            matched_rows=matched_rows,
            candidate_rows=candidate_rows,
        )
        summary.absent_offer, summary.missing_count, summary.missing_held_back = offer, missing_n, held_n
    except Exception as exc:   # an optional extra must never fail an upload that already succeeded
        log.warning("absent_offer_failed", extra={"upload_id": upload_id, "error": str(exc)})

    if alias_store is not None and not dry_run:
        try:
            alias_store.flush_usage()       # Feature 7: remember when learned aliases were last applied
        except Exception as exc:
            log.warning("alias_usage_flush_failed", extra={"upload_id": upload_id, "error": str(exc)})

    if summary.flagged > 0:
        summary.review_url = str(review_queue._dir / f"{upload_id}.json")

    log.info(
        "workflow_done",
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
