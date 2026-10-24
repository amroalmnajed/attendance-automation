"""Zoom attendance workflow.

Flow:
  1. Load participants from CSV or Zoom API (zoom_csv.py, zoom_api.py)
  2. Detect teacher and exclude
  3. Calculate attendance duration (late, early leave)
  4. Convert to OcrRow instances
  5. Delegate to row_processor.process_rows() for matching, gating, writing
  6. Archive CSV and learn meeting ID mappings

Entry point:
  - run_zoom(): Process Zoom attendance from CSV or API
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Callable, Optional

import yaml

from attendance import health as health_mod
from attendance.ocr import OcrRow
from attendance.zoom_csv import determine_session_type
from attendance import row_processor
from attendance.zoom_csv import (
    archive_csv,
    calculate_attendance,
    clean_zoom_name,
    extract_date_from_filename,
    extract_meeting_id_from_filename,
    find_teacher,
    parse_zoom_csv,
    save_meeting_id_mapping,
    write_decision_log,
)
from attendance.zoom_api import fetch_past_meeting_participants
from attendance.utils.progress import ProgressFn, STAGE_READING, _emit
from core.logger import get_logger
from core.notifier import RunSummary

log = get_logger("zoom_workflow")


def load_config(path: str = "config/attendance.yaml") -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
    except FileNotFoundError:
        raise RuntimeError(f"Configuration file not found: {path}")
    from attendance.config_model import ConfigError, validate_cfg
    try:
        validate_cfg(cfg)
    except ConfigError as exc:
        raise RuntimeError(str(exc)) from exc
    return cfg


def _parse_manual_time(time_str: str, base_date: Optional[date] = None) -> Optional[datetime]:
    time_str = time_str.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %I:%M %p", "%Y-%m-%d %I:%M%p"):
        try:
            return datetime.strptime(time_str, fmt)
        except ValueError:
            pass
    parsed_time = None
    for fmt in ("%I:%M %p", "%I:%M%p", "%I:%M:%S %p", "%I:%M:%S%p", "%H:%M", "%H:%M:%S"):
        try:
            parsed_time = datetime.strptime(time_str, fmt).time()
            break
        except ValueError:
            pass
    if parsed_time is not None:
        d = base_date or datetime.now().date()
        return datetime.combine(d, parsed_time)
    return None


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
    manual_start_time: Optional[str | datetime] = None,
    progress: Optional[ProgressFn] = None,
) -> RunSummary:
    """Execute the attendance workflow for Zoom participant data.

    Source can be:
    - Local CSV file path (str)
    - Dict with meeting_id, instance_uuid (optional), date (from Zoom API)

    Returns RunSummary with teacher_not_found flag if teacher detection fails and no manual_start_time provided.
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
    log.info("workflow_zoom_start", extra={"upload_id": upload_id, "source": source, "dry_run": dry_run})

    if progress:
        progress(STAGE_READING)

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
            instance_uuid = source.get("instance_uuid")
            date_str = source.get("date")
            participants = fetch_past_meeting_participants(
                meeting_id=meeting_id,
                instance_uuid=instance_uuid,
                config=zoom_cfg.get("zoom_api", {}),
            )
            date = datetime.fromisoformat(date_str) if date_str else None
    except Exception as e:
        log.error("zoom_parse_error", extra={"error": str(e), "upload_id": upload_id})
        summary.errors.append(f"Failed to parse Zoom data: {e}")
        return summary

    # Find teacher (support configured teacher names with default fallback Yaman)
    candidate_names: list[str] = []
    cfg_teachers = zoom_cfg.get("teacher_names") or zoom_cfg.get("teacher_name")
    if isinstance(cfg_teachers, list):
        for t in cfg_teachers:
            if isinstance(t, str) and t.strip():
                candidate_names.append(t.strip())
    elif isinstance(cfg_teachers, str) and cfg_teachers.strip():
        candidate_names.append(cfg_teachers.strip())

    if not candidate_names:
        candidate_names.append("Yaman")
    elif not any(c.lower() == "yaman" for c in candidate_names):
        candidate_names.append("Yaman")

    fuzzy_threshold = zoom_cfg.get("teacher_fuzzy_match_threshold", 80)
    teacher = None
    matched_teacher_name = candidate_names[0] if candidate_names else "Yaman"
    for cand in candidate_names:
        teacher = find_teacher(participants, cand, fuzzy_threshold)
        if teacher is not None:
            matched_teacher_name = cand
            break

    meeting_start_time = None
    meeting_end_time = None

    if teacher is not None:
        meeting_start_time = teacher["join_time"]
        meeting_end_time = teacher["leave_time"]
    elif manual_start_time is not None:
        if isinstance(manual_start_time, datetime):
            meeting_start_time = manual_start_time
        elif isinstance(manual_start_time, str):
            base_d = date.date() if isinstance(date, datetime) else None
            if base_d is None and participants:
                for p in participants:
                    if p.get("join_time"):
                        base_d = p["join_time"].date()
                        break
            meeting_start_time = _parse_manual_time(manual_start_time, base_d)
            if meeting_start_time is None:
                summary.errors.append(f"Invalid manual start time format: {manual_start_time}")
                return summary
    else:
        summary.teacher_not_found = True
        log.warning("teacher_not_found_zoom", extra={"upload_id": upload_id})
        return summary

    # Auto-determine session type if not provided
    if not session_type and zoom_cfg.get("auto_session_type", True) and date:
        session_type = determine_session_type(date, course, cfg)
        if session_type == "ambiguous":
            summary.session_type_ambiguous = True
            log.warning("session_type_ambiguous", extra={"upload_id": upload_id})

    # Calculate attendance (exclude all candidate teachers so neither is treated as a student)
    attendance_results = calculate_attendance(
        participants=participants,
        config=zoom_cfg,
        meeting_start_time=meeting_start_time,
        meeting_end_time=meeting_end_time,
        teacher_name=candidate_names,
    )

    if attendance_results is None:
        summary.errors.append("Attendance calculation failed (teacher not found)")
        return summary

    # Convert to OcrRow format for the shared row processor.
    # Clean Zoom display-name artefacts (parenthetical suffixes, bilingual Arabic tails)
    # before fuzzy matching — raw Zoom names hurt scores dramatically.
    ocr_rows = []
    for name, mark, detail in attendance_results:
        ocr_rows.append(OcrRow(
            raw_name=clean_zoom_name(name),
            status_mark=mark,
            confidence=1.0,
            row_index=len(ocr_rows) + 1,
        ))

    # Build Zoom-specific matcher overrides: Zoom names are self-typed (reliable) but
    # are often partial (first + last only) so the raw fuzzy score ceiling is ~85–88
    # against a 3-4 token roster name.  We relax t_high and add a soft-accept lane.
    zoom_match_overrides: dict = zoom_cfg.get("match_overrides", {})

    # Process through shared row processor
    result = row_processor.process_rows(
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
        progress=progress,
        match_cfg_overrides=zoom_match_overrides,
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
