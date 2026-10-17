"""attendance/sheet_review_queue.py — Sheet-backed review queue for dashboard visibility.

Mirrors the dashboard's ReviewQueue schema to enable the bot to write flagged items
directly to Google Sheets, making them visible in the Streamlit dashboard.

This is an optional mirror to the local JSON/SQLite queue; items are written to both
for redundancy and dashboard visibility.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

import gspread
import pytz
import yaml

from attendance._net import retry_call
from attendance.session_resolver import _open_worksheet
from core.auth import get_credentials
from core.logger import get_logger

log = get_logger("sheet_review_queue")
CAIRO = pytz.timezone("Africa/Cairo")


def _load_config() -> dict:
    """Load attendance config."""
    try:
        with open("config/attendance.yaml", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception:
        return {}


def _now_iso() -> str:
    return datetime.now(CAIRO).isoformat()


def _get_sheet_id(course_key: str) -> Optional[str]:
    """Get sheet ID for a course from config and env."""
    cfg = _load_config()
    course_cfg = cfg.get("attendance", {}).get("courses", {}).get(course_key)
    if not course_cfg:
        return None
    sheet_id_env = course_cfg.get("sheet_id_env")
    if not sheet_id_env:
        return None
    return os.getenv(sheet_id_env)


def _get_review_tab(course_key: str) -> str:
    """Get review tab name for a course."""
    cfg = _load_config()
    review_cfg = cfg.get("attendance", {}).get("review", {})
    return review_cfg.get("tab", "ReviewQueue")


def _format_bbox(bbox: Optional[list[float]]) -> str:
    """Format bounding box as string for sheet storage."""
    if not bbox:
        return ""
    return ",".join(str(v) for v in bbox)


def _parse_bbox(bbox_str: str) -> Optional[list[float]]:
    """Parse bounding box from string."""
    if not bbox_str:
        return None
    try:
        return [float(v) for v in bbox_str.split(",")]
    except Exception:
        return None


class SheetReviewQueue:
    """Writer for review items to Google Sheets ReviewQueue tab."""

    def __init__(self, retry_cfg: Optional[dict] = None):
        self._retry_cfg = retry_cfg or {}

    def enqueue(
        self,
        *,
        upload_id: str,
        course: str,
        session: int,
        session_column: str,
        session_type: Optional[str],
        image_ref: str,
        ocr_row,  # OcrRow from attendance.ocr
        match_result,  # MatchResult from attendance.matcher
        raw_mark: Optional[str],
        reason: Optional[str],
    ) -> Optional[str]:
        """Append a review item to the course's ReviewQueue tab.

        Returns the row number if successful, None on failure (non-blocking).
        """
        try:
            sheet_id = _get_sheet_id(course)
            if not sheet_id:
                log.warning("sheet_review_queue_no_sheet_id", extra={"course": course})
                return None

            review_tab = _get_review_tab(course)

            # Authorize and open worksheet
            creds = retry_call(get_credentials, op="gcp_credentials", **self._retry_cfg)
            gc = gspread.authorize(creds)
            ws = _open_worksheet(gc, sheet_id, review_tab, self._retry_cfg)

            # Extract item data
            item_id = f"{upload_id[:8]}_{session}_{ocr_row.row_index}"

            # Format top3 candidates
            top3 = match_result.top3 if match_result else []
            candidates = {
                "candidate_1": top3[0].roster_name if len(top3) > 0 else "",
                "candidate_1_score": round(top3[0].score, 2) if len(top3) > 0 else 0,
                "candidate_2": top3[1].roster_name if len(top3) > 1 else "",
                "candidate_2_score": round(top3[1].score, 2) if len(top3) > 1 else 0,
                "candidate_3": top3[2].roster_name if len(top3) > 2 else "",
                "candidate_3_score": round(top3[2].score, 2) if len(top3) > 2 else 0,
            }

            # Extract bounding box
            bbox = getattr(ocr_row, "bounding_box", None)
            bbox_str = _format_bbox(bbox)

            # Extract photo index
            photo_idx = getattr(ocr_row, "photo_index", None)
            photo_idx_str = str(photo_idx) if photo_idx is not None else ""

            # Build row matching dashboard schema
            row = [
                item_id,  # id
                upload_id,  # upload_id
                course,  # course
                str(session),  # session_num
                session_column,  # session_column (exact column header)
                session_type or "",  # session_type
                ocr_row.raw_name,  # raw_name
                image_ref,  # image_ref
                bbox_str,  # bounding_box
                str(ocr_row.row_index),  # paper_row_index
                photo_idx_str,  # photo_index
                candidates["candidate_1"],  # candidate_1
                candidates["candidate_1_score"],  # candidate_1_score
                candidates["candidate_2"],  # candidate_2
                candidates["candidate_2_score"],  # candidate_2_score
                candidates["candidate_3"],  # candidate_3
                candidates["candidate_3_score"],  # candidate_3_score
                reason or "",  # reason_code
                "pending",  # status
                "",  # resolved_name
                "",  # resolved_value
                "",  # resolved_by
                "",  # resolved_at
                _now_iso(),  # created_at
            ]

            # Append row to sheet
            ws.append_row(row)
            log.info(
                "sheet_review_enqueued",
                extra={
                    "upload_id": upload_id,
                    "item_id": item_id,
                    "course": course,
                    "session": session,
                },
            )
            return str(len(ws.get_all_values()))  # Return new row number

        except Exception as e:
            log.warning(
                "sheet_review_enqueue_failed",
                extra={
                    "upload_id": upload_id,
                    "course": course,
                    "error": str(e),
                },
            )
            # Non-blocking: local queue still works
            return None

    def resolve(
        self,
        course: str,
        item_id: str,
        resolved_name: str,
        resolved_value: str,
        resolved_by: str,
    ) -> bool:
        """Mark a review item as resolved in the sheet.

        Returns True if successful, False on failure.
        """
        try:
            sheet_id = _get_sheet_id(course)
            if not sheet_id:
                return False

            review_tab = _get_review_tab(course)

            # Authorize and open worksheet
            creds = retry_call(get_credentials, op="gcp_credentials", **self._retry_cfg)
            gc = gspread.authorize(creds)
            ws = _open_worksheet(gc, sheet_id, review_tab, self._retry_cfg)

            # Find the item row by id
            values = ws.get_all_values()
            if not values:
                return False

            headers = values[0]
            id_idx = headers.index("id") if "id" in headers else -1
            if id_idx == -1:
                return False

            for row_idx, row in enumerate(values[1:], start=2):
                if len(row) > id_idx and row[id_idx] == item_id:
                    # Update the row
                    now = _now_iso()

                    # Find column indices
                    status_idx = headers.index("status") if "status" in headers else -1
                    resolved_name_idx = headers.index("resolved_name") if "resolved_name" in headers else -1
                    resolved_value_idx = headers.index("resolved_value") if "resolved_value" in headers else -1
                    resolved_by_idx = headers.index("resolved_by") if "resolved_by" in headers else -1
                    resolved_at_idx = headers.index("resolved_at") if "resolved_at" in headers else -1

                    updates = []
                    if status_idx >= 0:
                        updates.append((row_idx, status_idx + 1, "approved"))
                    if resolved_name_idx >= 0:
                        updates.append((row_idx, resolved_name_idx + 1, resolved_name))
                    if resolved_value_idx >= 0:
                        updates.append((row_idx, resolved_value_idx + 1, resolved_value))
                    if resolved_by_idx >= 0:
                        updates.append((row_idx, resolved_by_idx + 1, resolved_by))
                    if resolved_at_idx >= 0:
                        updates.append((row_idx, resolved_at_idx + 1, now))

                    if updates:
                        from gspread.utils import rowcol_to_a1
                        formatted_updates = [
                            {"range": rowcol_to_a1(row, col), "values": [[value]]}
                            for row, col, value in updates
                        ]
                        ws.batch_update(formatted_updates)

                    log.info(
                        "sheet_review_resolved",
                        extra={"item_id": item_id, "resolved_by": resolved_by},
                    )
                    return True

            return False
        except Exception as e:
            log.warning(
                "sheet_review_resolve_failed",
                extra={"item_id": item_id, "error": str(e)},
            )
            return False
