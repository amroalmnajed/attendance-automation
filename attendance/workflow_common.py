"""Shared helpers for the attendance ingestion workflows.

Extracted from image_workflow / text_workflow / zoom_workflow, which each
reimplemented the same config loading and (for image) early roster
resolution around the shared ``row_processor.process_rows`` core.
"""
from __future__ import annotations

from typing import Optional

import yaml

from attendance import health as health_mod
from attendance.session_resolver import SheetSession, resolve
from core.logger import get_logger

log = get_logger("workflow_common")


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


def configure_health(cfg: dict) -> None:
    health_mod.configure(cfg)


def resolve_roster_for_ocr(
    *,
    cfg: dict,
    course: str,
    session_num: int,
    session_type: Optional[str],
    upload_id: str,
) -> tuple[Optional[SheetSession], Optional[list[str]]]:
    """Resolve session/roster up front when the OCR provider may see the roster.

    Returns ``(session_info, roster_names)``. On failure returns ``(None, None)``
    so the caller can fall back to resolving inside ``process_rows``.
    """
    try:
        session_info = resolve(course_key=course, session_num=session_num, session_type=session_type, cfg=cfg)
        return session_info, [name for name, _ in session_info.roster]
    except Exception as exc:
        log.warning("early_session_resolve_failed", extra={"error": str(exc), "upload_id": upload_id})
        return None, None
