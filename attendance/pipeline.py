"""attendance/pipeline.py — Top-level orchestrator for Sub-System 1.

Ties together: preprocess → OCR (2 passes) → normalise → match → gate → write/review.
Returns a RunSummary suitable for the notifier.
"""
from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import yaml

from attendance import absent as absent_mod
from attendance import health as health_mod
from attendance import history as history_mod
from attendance.gates import check_all_gates
from attendance.aliases import AliasStore
from attendance.matcher import MatchDecision, MatchResult, ReasonCode, build_matcher
from attendance.normalizer import normalize_attendance_mark
from attendance.ocr import OcrRow, build_provider
from attendance.photos import merge_photo_rows
from attendance.preprocess import prepare
from attendance.review_queue import ReviewQueue
from attendance.session_resolver import SheetSession, resolve
from attendance.writer import WriteCell, batch_write
from attendance.zoom_api import fetch_past_meeting_participants
from attendance.zoom_csv import (
    archive_csv,
    calculate_attendance,
    determine_session_type,
    extract_date_from_filename,
    extract_meeting_id_from_filename,
    find_teacher,
    parse_zoom_csv,
    save_meeting_id_mapping,
    write_decision_log,
)
from core.logger import get_logger
from core.notifier import RunSummary

log = get_logger("pipeline")

# Progress stages reported through the optional ``progress`` callback (the bot maps them to text).
STAGE_READING, STAGE_MATCHING, STAGE_SAVING = "reading", "matching", "saving"
ProgressFn = Callable[[str], None]


def _emit(progress: Optional[ProgressFn], stage: str) -> None:
    """Report a stage; a broken callback must never break the pipeline."""
    if progress is None:
        return
    try:
        progress(stage)
    except Exception:
        log.warning("progress_callback_failed", extra={"stage": stage})


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
    progress: Optional[ProgressFn] = None,
) -> RunSummary:
    """Execute the full attendance pipeline for a single image."""
    cfg = load_config(config_path)
    health_mod.configure(cfg)
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
    _emit(progress, STAGE_READING)

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

    try:
        ocr_rows = _read_sheet(image_bytes=image_bytes, provider=provider, roster_names=roster_names,
                               ocr_cfg=ocr_cfg, min_conf=min_conf, upload_id=upload_id, dry_run=dry_run)
    except Exception as exc:
        log.error("ocr_api_error", extra={"error": str(exc), "upload_id": upload_id})
        summary.errors.append(f"API_ERROR: OCR pass 1 failed: {exc}")
        return summary

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
        progress=progress,
    )


def run_multi(
    *,
    image_paths: list[str],
    course: str,
    session_num: int,
    session_type: Optional[str] = None,
    dry_run: bool = True,
    force: bool = False,
    config_path: str = "config/attendance.yaml",
    review_store_dir: str = "review_queue",
    progress: Optional[ProgressFn] = None,
) -> RunSummary:
    """Run the pipeline over SEVERAL photos of one attendance sheet as ONE upload.

    Each photo is OCR'd on its own, the rows are merged (duplicates across photos
    collapsed; conflicting marks sent to review), then everything is matched and
    written as a single session under one ``upload_id``.

    All-or-nothing on OCR: if any photo cannot be read, nothing is written. A missing
    photo would make its students look absent.
    """
    if not image_paths:
        raise ValueError("run_multi needs at least one image path")
    if len(image_paths) == 1:
        return run(
            image_path=image_paths[0], course=course, session_num=session_num,
            session_type=session_type, dry_run=dry_run, force=force,
            config_path=config_path, review_store_dir=review_store_dir,
        )

    cfg = load_config(config_path)
    health_mod.configure(cfg)
    att_cfg = cfg.get("attendance", {})
    ocr_cfg = att_cfg.get("ocr", {})
    preprocess_cfg = ocr_cfg.get("preprocess", {})

    raw_list: list[bytes] = []
    for path in image_paths:
        try:
            raw_list.append(Path(path).read_bytes())
        except FileNotFoundError:
            summary = RunSummary(upload_id="error_missing_file", course=course, session=str(session_num), dry_run=dry_run)
            summary.errors.append(f"Image file not found: {path}")
            return summary
        except Exception as e:
            summary = RunSummary(upload_id="error_reading_file", course=course, session=str(session_num), dry_run=dry_run)
            summary.errors.append(f"Failed to read image file: {str(e)}")
            return summary

    upload_id = hashlib.sha256(b"".join(raw_list) + course.encode() + str(session_num).encode()).hexdigest()[:16]
    log.info("pipeline_multi_start", extra={"upload_id": upload_id, "photos": len(image_paths), "dry_run": dry_run})
    _emit(progress, STAGE_READING)

    summary = RunSummary(
        upload_id=upload_id, course=course, session=str(session_num),
        dry_run=dry_run, photo_count=len(image_paths),
    )

    provider = build_provider(ocr_cfg)
    min_conf = float(ocr_cfg.get("min_confidence", 0.75))

    session_info: Optional[SheetSession] = None
    roster_names: Optional[list[str]] = None
    if ocr_cfg.get("send_roster_to_provider", False):
        try:
            session_info = resolve(course_key=course, session_num=session_num, session_type=session_type, cfg=cfg)
            roster_names = [name for name, _ in session_info.roster]
        except Exception as exc:
            log.warning("early_session_resolve_failed", extra={"error": str(exc), "upload_id": upload_id})
            session_info = None
            roster_names = None

    per_photo: list[list[OcrRow]] = []

    def _process_one_photo(n: int, raw_bytes: bytes) -> Optional[list[OcrRow]]:
        """Process a single photo (preprocess + OCR). Called in parallel."""
        image_bytes = prepare(
            raw_bytes,
            orient=preprocess_cfg.get("orient", True),
            deskew=preprocess_cfg.get("deskew", True),
            contrast=preprocess_cfg.get("contrast", "mild"),
            max_side_px=int(preprocess_cfg.get("max_side_px", 2000)),
            strip_exif=preprocess_cfg.get("strip_exif", True),
        )
        log.info("ocr_photo", extra={"upload_id": upload_id, "photo": n, "of": len(raw_list)})
        try:
            return _read_sheet(image_bytes=image_bytes, provider=provider, roster_names=roster_names,
                               ocr_cfg=ocr_cfg, min_conf=min_conf, upload_id=upload_id, photo=n,
                               dry_run=dry_run)
        except Exception as exc:
            log.error("ocr_api_error", extra={"error": str(exc), "upload_id": upload_id, "photo": n})
            raise exc

    # Process photos concurrently (up to 4 at a time to avoid overwhelming the OCR API)
    max_workers = min(4, len(raw_list))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_idx = {
            executor.submit(_process_one_photo, n, raw): n
            for n, raw in enumerate(raw_list, start=1)
        }
        for future in as_completed(future_to_idx):
            n = future_to_idx[future]
            try:
                result = future.result()
                per_photo.append((n, result))
            except Exception as exc:
                summary.errors.append(f"API_ERROR: OCR failed on photo {n} of {len(raw_list)}: {exc}")
                return summary

    # Sort by original photo order
    per_photo.sort(key=lambda x: x[0])
    per_photo = [rows for _, rows in per_photo]

    ocr_rows = merge_photo_rows(per_photo, list(image_paths))

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
        image_path=image_paths[0],
        review_store_dir=review_store_dir,
        summary=summary,
        session_info=session_info,
        progress=progress,
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
    marks: Optional[list[Optional[str]]] = None,
) -> RunSummary:
    """Execute the attendance pipeline using a provided list of names.

    ``marks`` is an optional list parallel to ``names`` (e.g. from
    ``attendance.marks.split_names_and_marks``). A missing or empty mark means
    Present, as before. A mark the gates do not recognise goes to review.
    """
    cfg = load_config(config_path)
    health_mod.configure(cfg)

    id_basis = str(names)
    if marks and any((m or "Present") != "Present" for m in marks):
        id_basis += str(list(marks))   # unchanged for all-Present lists, so old ids stay stable
    upload_id = hashlib.sha256(id_basis.encode() + course.encode() + str(session_num).encode()).hexdigest()[:16]
    log.info("pipeline_text_start", extra={"upload_id": upload_id, "names_count": len(names), "dry_run": dry_run})

    summary = RunSummary(
        upload_id=upload_id,
        course=course,
        session=str(session_num),
        dry_run=dry_run,
    )

    def _mark_for(i: int) -> str:
        if marks and i < len(marks) and marks[i] and str(marks[i]).strip():
            return str(marks[i]).strip()
        return "Present"

    ocr_rows = [
        OcrRow(raw_name=name.strip(), status_mark=_mark_for(i), confidence=1.0, row_index=i+1)
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


def run_zoom(
    *,
    source: str | dict[str, str],
    course: str,
    session_num: int,
    session_type: Optional[str] = None,
    dry_run: bool = True,
    force: bool = False,
    config_path: str = "config/attendance.yaml",
    review_store_dir: str = "review_queue",
) -> RunSummary:
    """Execute the attendance pipeline for Zoom participant data.

    Source can be:
    - Local CSV file path (str)
    - Dict with meeting_id, date (from Zoom API) - instance_uuid is not used

    Returns RunSummary with teacher_not_found flag if teacher detection fails.
    """
    cfg = load_config(config_path)
    health_mod.configure(cfg)
    att_cfg = cfg.get("attendance", {})
    zoom_cfg = att_cfg.get("zoom_csv", {})

    if not zoom_cfg.get("enabled", False):
        summary = RunSummary(upload_id="zoom_disabled", course=course, session=str(session_num), dry_run=dry_run)
        summary.errors.append("Zoom CSV integration is disabled in config")
        return summary

    upload_id = f"zoom_{course}_{session_num}"
    log.info("pipeline_zoom_start", extra={"upload_id": upload_id, "source": source, "dry_run": dry_run})

    summary = RunSummary(
        upload_id=upload_id,
        course=course,
        session=str(session_num),
        dry_run=dry_run,
    )

    # Parse participants from source
    try:
        if isinstance(source, str):
            # CSV file path
            participants = parse_zoom_csv(source)
            meeting_id = extract_meeting_id_from_filename(Path(source).name)
            date = extract_date_from_filename(Path(source).name)
        else:
            # Zoom API data
            meeting_id = source.get("meeting_id")
            date_str = source.get("date")
            participants = fetch_past_meeting_participants(
                meeting_id=meeting_id,
                instance_uuid=None,  # Not used - Zoom API doesn't support instance-specific endpoint
                config=zoom_cfg.get("zoom_api", {}),
            )
            date = datetime.fromisoformat(date_str) if date_str else None
    except Exception as e:
        log.error("zoom_parse_error", extra={"error": str(e), "upload_id": upload_id})
        summary.errors.append(f"Failed to parse Zoom data: {e}")
        return summary

    # Find teacher
    teacher_name = zoom_cfg.get("teacher_name", "Yaman")
    fuzzy_threshold = zoom_cfg.get("teacher_fuzzy_match_threshold", 80)
    teacher = find_teacher(participants, teacher_name, fuzzy_threshold)

    if teacher is None:
        summary.teacher_not_found = True
        log.warning("teacher_not_found_zoom", extra={"upload_id": upload_id})
        return summary

    meeting_start_time = teacher["join_time"]
    meeting_end_time = teacher["leave_time"]

    # Auto-determine session type if not provided
    if not session_type and zoom_cfg.get("auto_session_type", True) and date:
        session_type = determine_session_type(date, course, cfg)
        if session_type == "ambiguous":
            summary.session_type_ambiguous = True
            log.warning("session_type_ambiguous", extra={"upload_id": upload_id})

    # Calculate attendance
    attendance_results = calculate_attendance(
        participants=participants,
        config=zoom_cfg,
        meeting_start_time=meeting_start_time,
        meeting_end_time=meeting_end_time,
    )

    if attendance_results is None:
        summary.errors.append("Attendance calculation failed (teacher not found)")
        return summary

    # Convert to OcrRow format for existing pipeline
    ocr_rows = []
    for name, mark, detail in attendance_results:
        ocr_rows.append(OcrRow(
            raw_name=name,
            status_mark=mark,
            confidence=1.0,
            row_index=len(ocr_rows) + 1,
        ))

    # Process through existing pipeline
    result = _process_rows(
        ocr_rows=ocr_rows,
        course=course,
        session_num=session_num,
        session_type=session_type,
        dry_run=dry_run,
        force=force,
        cfg=cfg,
        min_conf=1.0,
        upload_id=upload_id,
        image_path="zoom_source",
        review_store_dir=review_store_dir,
        summary=summary,
    )

    # Post-processing for non-dry-run
    if not dry_run:
        # Archive CSV if it was a local file
        if isinstance(source, str) and zoom_cfg.get("auto_archive_csv", True):
            try:
                archive_csv(source, zoom_cfg.get("archive_folder", "csv_archive"))
            except Exception as e:
                log.warning("csv_archive_failed", extra={"error": str(e), "upload_id": upload_id})

        # Save meeting ID mapping if auto-learn enabled
        if meeting_id and zoom_cfg.get("auto_learn_meeting_ids", True):
            try:
                save_meeting_id_mapping(meeting_id, course, config_path)
            except Exception as e:
                log.warning("meeting_id_save_failed", extra={"error": str(e), "upload_id": upload_id})

        # Write decision log
        try:
            participants_detail = [
                {"name": name, "mark": mark, "detail": detail}
                for name, mark, detail in attendance_results
            ]
            write_decision_log(upload_id, participants_detail, zoom_cfg)
        except Exception as e:
            log.warning("decision_log_failed", extra={"error": str(e), "upload_id": upload_id})

    return result


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
    progress: Optional[ProgressFn] = None,
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

def _note_candidates(match_result: MatchResult, candidate_rows: set[int]) -> None:
    """Remember roster rows a flagged item might refer to (so they are not called 'missing')."""
    if match_result.best is not None and match_result.best.roster_row > 0:
        candidate_rows.add(match_result.best.roster_row)
    for cand in match_result.top3:
        if cand.roster_row > 0:
            candidate_rows.add(cand.roster_row)


def _col_idx(col_letter: str) -> int:
    """Column letter → 1-based index."""
    result = 0
    for ch in col_letter.upper():
        result = result * 26 + (ord(ch) - ord("A") + 1)
    return result


def _second_pass_settings(ocr_cfg: dict) -> dict:
    """Effective ``attendance.ocr.second_pass`` settings. Unknown/missing -> today's behaviour (full)."""
    sp = (ocr_cfg or {}).get("second_pass") or {}
    if not isinstance(sp, dict):
        sp = {}
    mode = str(sp.get("mode", "full")).strip().lower()
    return {
        "mode": mode if mode in ("full", "low_confidence") else "full",
        "model": sp.get("model") or None,          # different Gemini model for the extra read
        "provider": sp.get("provider") or None,    # or a different provider (gemini | mock)
        "include_unmatched": bool(sp.get("include_unmatched", True)),
    }


def _needs_second_look(row, min_conf: float, roster_aware: bool, include_unmatched: bool) -> bool:
    """Row is worth a second read: confidence below min_confidence, or (roster-aware OCR) no roster match."""
    conf = getattr(row, "confidence", None)
    if isinstance(conf, (int, float)) and conf < min_conf:
        return True
    if roster_aware and include_unmatched and not getattr(row, "matched_roster_name", None):
        return True
    return False


def _read_sheet(*, dry_run: bool = False, **kwargs) -> list[OcrRow]:
    """OCR one sheet. Dry runs record no health events (see attendance/health.py)."""
    with health_mod.dry_run_scope(dry_run):
        return _read_sheet_inner(**kwargs)


def _read_sheet_inner(
    *,
    image_bytes: bytes,
    provider,
    roster_names: Optional[list[str]],
    ocr_cfg: dict,
    min_conf: float,
    upload_id: str,
    photo: Optional[int] = None,
) -> list[OcrRow]:
    """OCR one sheet image (shared by run() and run_multi()).

    ``passes: 1``                      -> one read.
    ``passes: 2`` + ``mode: full``     -> every row read twice and merged (today's behaviour, the default).
    ``passes: 2`` + ``mode: low_confidence`` -> pass 1 on everything; a second read ONLY when some rows are
        low-confidence / unmatched, merged into just those rows (optionally with another model/provider).
    Pass-1 failure raises (caller reports it); a failed pass 2 is logged and pass 1 is used as-is.
    Retries and the 429 model fallback live inside ``provider.read`` (ocr.py / _net.py).
    """
    extra = {"upload_id": upload_id} if photo is None else {"upload_id": upload_id, "photo": photo}
    log.info("ocr_pass_1", extra=extra)
    rows1 = provider.read(image_bytes, roster=roster_names)

    if int(ocr_cfg.get("passes", 2)) < 2:
        return rows1
    sp = _second_pass_settings(ocr_cfg)

    if sp["mode"] == "full":
        rows2: list[OcrRow] = []
        log.info("ocr_pass_2", extra=extra)
        try:
            rows2 = provider.read(image_bytes, roster=roster_names)
        except Exception as exc:
            log.warning("ocr_pass2_failed", extra={**extra, "error": str(exc)})
        return _merge_passes(rows1, rows2)

    # mode == "low_confidence"
    roster_aware = roster_names is not None
    flagged = [_needs_second_look(r, min_conf, roster_aware, sp["include_unmatched"]) for r in rows1]
    if not any(flagged):
        log.info("ocr_pass_2_skipped", extra={**extra, "rows": len(rows1)})
        return rows1
    log.info("ocr_pass_2_targeted", extra={**extra, "rows": len(rows1), "rechecked": sum(flagged)})
    try:
        second = provider
        if sp["model"] or sp["provider"]:
            second_cfg = dict(ocr_cfg)
            if sp["model"]:
                second_cfg["model"] = sp["model"]
            if sp["provider"]:
                second_cfg["provider"] = sp["provider"]
            second = build_provider(second_cfg)
        rows2 = second.read(image_bytes, roster=roster_names)
    except Exception as exc:
        log.warning("ocr_pass2_failed", extra={**extra, "error": str(exc)})
        return rows1
    return [_merge_passes([r], rows2)[0] if hit else r for r, hit in zip(rows1, flagged)]


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
