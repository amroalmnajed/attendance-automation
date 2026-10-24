"""Text-input attendance workflow.

Flow:
  1. Parse names and optional marks (marks.py)
  2. Create OcrRow instances from text
  3. Delegate to row_processor.process_rows() for matching, gating, writing

Entry point:
  - run_text(): Process list of names with optional marks
"""
from __future__ import annotations

import hashlib
from typing import Optional

import yaml

from attendance import health as health_mod
from attendance.ocr import OcrRow
from attendance.row_processor import process_rows
from core.logger import get_logger
from core.notifier import RunSummary

log = get_logger("text_workflow")


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
    """Execute the attendance workflow using a provided list of names.

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
    log.info("workflow_text_start", extra={"upload_id": upload_id, "names_count": len(names), "dry_run": dry_run})

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

    return process_rows(
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
