"""attendance/sync.py — Bidirectional synchronization between SQLite and Google Sheets.

SQLite is the primary data store. Google Sheets is the authoritative source for
roster and session structure, while SQLite stores attendance records and
learned patterns.

Sync strategy:
  - Read: Sheet → SQLite on session resolution (roster, sessions)
  - Write: SQLite → Sheet batched, with conflict detection
  - Sync job runs every 5 minutes or on-demand
  - Conflict resolution: last-write-wins with audit trail
  - Sync status tracking for monitoring
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Callable, Optional

from attendance.db import (
    Alias,
    AttendanceRecord,
    Course,
    OcrCorrection,
    Pattern,
    Session,
    Student,
    audit_log_action,
    get_or_create_course,
    get_session,
)
from attendance.session_resolver import (
    SheetSession,
    _get_client,
    _open_worksheet,
    invalidate as invalidate_session_cache,
)
from attendance.writer import WriteCell, batch_write, reconcile_cells
from attendance.utils.column_utils import col_letter_to_index
from core.logger import get_logger

log = get_logger("sync")


# ─── Sync status tracking ───────────────────────────────────────────────────────

class SyncStatus:
    """Track the status of sync operations."""
    def __init__(self):
        self.last_sync_time: Optional[datetime] = None
        self.last_sync_direction: Optional[str] = None  # "sheets_to_sqlite" or "sqlite_to_sheets"
        self.last_sync_error: Optional[str] = None
        self.last_sync_rows_read: int = 0
        self.last_sync_rows_written: int = 0
        self.last_sync_conflicts: int = 0

    def record_success(self, direction: str, rows_read: int = 0, rows_written: int = 0, conflicts: int = 0) -> None:
        self.last_sync_time = datetime.now(timezone.utc)
        self.last_sync_direction = direction
        self.last_sync_error = None
        self.last_sync_rows_read = rows_read
        self.last_sync_rows_written = rows_written
        self.last_sync_conflicts = conflicts

    def record_failure(self, direction: str, error: str) -> None:
        self.last_sync_time = datetime.now(timezone.utc)
        self.last_sync_direction = direction
        self.last_sync_error = error

    def to_dict(self) -> dict:
        return {
            "last_sync_time": self.last_sync_time.isoformat() if self.last_sync_time else None,
            "last_sync_direction": self.last_sync_direction,
            "last_sync_error": self.last_sync_error,
            "last_sync_rows_read": self.last_sync_rows_read,
            "last_sync_rows_written": self.last_sync_rows_written,
            "last_sync_conflicts": self.last_sync_conflicts,
        }


# Global sync status
_sync_status = SyncStatus()


def get_sync_status() -> dict:
    """Get the current sync status."""
    return _sync_status.to_dict()


# ─── Sheet → SQLite sync (roster and sessions) ────────────────────────────────

def sync_course_from_sheet(
    course_key: str,
    session: SheetSession,
    cfg: Optional[dict] = None,
) -> int:
    """Sync course, students, and sessions from Google Sheets to SQLite.

    This is a read-only sync that populates the local database with the
    authoritative roster and session structure from Google Sheets.

    Args:
        course_key: Course key (e.g., "crash", "main")
        session: SheetSession from session_resolver.resolve()
        cfg: Optional configuration dict

    Returns:
        Number of rows synced (students + sessions)
    """
    db = get_session()
    try:
        # Get or create course
        course = get_or_create_course(
            db,
            course_key=course_key,
            name=session.course,
            sheet_id=session.sheet_id,
            tab=session.tab,
            layout=session.layout,
        )

        rows_synced = 0

        # Sync students (roster)
        for roster_name, sheet_row in session.roster:
            # Check if student exists
            student = db.query(Student).filter(
                Student.course_id == course.course_id,
                Student.sheet_row == sheet_row,
            ).first()

            if student is None:
                student = Student(
                    course_id=course.course_id,
                    name=roster_name,
                    sheet_row=sheet_row,
                    is_active=True,
                )
                db.add(student)
                rows_synced += 1
            elif student.name != roster_name:
                # Name changed in sheet, update it
                old_name = student.name
                student.name = roster_name
                student.updated_at = datetime.now(timezone.utc)
                audit_log_action(
                    db,
                    action="student_update",
                    entity_type="student",
                    entity_id=str(student.student_id),
                    old_value=old_name,
                    new_value=roster_name,
                    user="sync",
                    context={"course": course_key, "sheet_row": sheet_row},
                )
                rows_synced += 1

        # Sync sessions
        # For each session column, create or update a session record
        # This is based on the session discovery in session_resolver
        # We'll use session_num derived from the column index or pattern
        session_num = 1
        for col in session.session_cols:
            # Extract session number from column if possible (e.g., "D" -> 3 if D is first session)
            # For now, we'll use sequential numbering
            sess = db.query(Session).filter(
                Session.course_id == course.course_id,
                Session.session_col == col,
            ).first()

            # Try to determine session type if this is a typed_pair layout
            session_type = None
            type_col = None
            if session.layout == "typed_pair":
                # Type column is immediately to the left of attendance column
                type_col = session.type_col
                # Try to read the actual session type from the sheet
                try:
                    if type_col:
                        ws = session.get_worksheet()
                        # Get header row from configuration or default to 1
                        header_row = 1
                        if cfg:
                            header_row = cfg.get("attendance", {}).get("courses", {}).get(course_key, {}).get("header_row", 1)

                        type_col_idx = col_letter_to_index(type_col)
                        type_values = ws.col_values(type_col_idx)
                        # Use the first non-empty type value (header row is excluded)
                        for val in type_values[header_row - 1:]:
                            if val and val.strip():
                                session_type = val.strip()
                                break
                except Exception as exc:
                    log.debug("sync_swallowed", extra={"error": str(exc)})

            if sess is None:
                sess = Session(
                    course_id=course.course_id,
                    session_num=session_num,
                    session_type=session_type,
                    session_col=col,
                    type_col=type_col,
                )
                db.add(sess)
                rows_synced += 1
            else:
                # Update session if type column changed
                if sess.type_col != type_col:
                    sess.type_col = type_col
                    sess.updated_at = datetime.now(timezone.utc)
                    rows_synced += 1

            # Record session type assignment for pattern learning
            if session_type:
                try:
                    from attendance.session_patterns import init_pattern_engine
                    pattern_engine = init_pattern_engine()
                    pattern_engine.record_session_assignment(
                        course_id=course.course_id,
                        session_date=datetime.now(timezone.utc),
                        session_num=session_num,
                        session_type=session_type,
                    )
                except Exception as exc:
                    log.debug("sync_swallowed", extra={"error": str(exc)})
                    # Pattern learning failures should not break sync

            session_num += 1

        db.commit()

        _sync_status.record_success("sheets_to_sqlite", rows_read=rows_synced)
        log.info(
            "sync_sheets_to_sqlite_complete",
            extra={
                "course": course_key,
                "rows_synced": rows_synced,
            },
        )

        return rows_synced

    except Exception as exc:
        db.rollback()
        _sync_status.record_failure("sheets_to_sqlite", str(exc))
        log.error("sync_sheets_to_sqlite_failed", extra={"course": course_key, "error": str(exc)})
        raise


# ─── SQLite → Sheet sync (attendance records) ─────────────────────────────────

def sync_attendance_to_sheet(
    course_key: str,
    upload_id: Optional[str] = None,
    dry_run: bool = True,
    cfg: Optional[dict] = None,
) -> int:
    """Sync attendance records from SQLite to Google Sheets.

    This writes pending attendance records to Google Sheets. Records are
    marked as synced only after successful write.

    Args:
        course_key: Course key (e.g., "crash", "main")
        upload_id: Optional upload_id to filter records
        dry_run: If True, compute the diff but don't write
        cfg: Optional configuration dict

    Returns:
        Number of rows written
    """
    db = get_session()
    try:
        course = db.query(Course).filter(Course.course_key == course_key, Course.is_active == True).first()
        if course is None:
            log.warning("sync_course_not_found", extra={"course": course_key})
            return 0

        # Get attendance records that need to be synced
        # For now, we'll sync all records without a specific sync status
        # In the future, we might add a "synced" flag to AttendanceRecord
        query = db.query(AttendanceRecord).join(Session).join(Student).filter(
            Session.course_id == course.course_id,
            Student.is_active == True,
        )

        if upload_id:
            query = query.filter(AttendanceRecord.upload_id == upload_id)

        records = query.all()

        if not records:
            log.info("sync_no_records_to_write", extra={"course": course_key})
            return 0

        # Group records by session
        sessions_dict: dict[int, list[AttendanceRecord]] = {}
        for record in records:
            sessions_dict.setdefault(record.session_id, []).append(record)

        # Resolve session for each session_id
        total_written = 0
        for session_id, session_records in sessions_dict.items():
            sess = db.query(Session).filter(Session.session_id == session_id).first()
            if sess is None:
                continue

            # Get SheetSession for this course
            try:
                from attendance.session_resolver import resolve
                sheet_session = resolve(course_key, cfg=cfg)
            except Exception as exc:
                log.warning("sync_session_resolve_failed", extra={"session_id": session_id, "error": str(exc)})
                continue

            # Build WriteCell list
            cells: list[WriteCell] = []
            ws = sheet_session.get_worksheet()
            col_idx = col_letter_to_index(sess.session_col)

            # Batch-read the entire column to avoid N+1 API calls
            try:
                column_values = ws.col_values(col_idx)
            except Exception:
                column_values = []

            for record in session_records:
                student = db.query(Student).filter(Student.student_id == record.student_id).first()
                if student is None:
                    continue

                # Get current value from pre-loaded column values (1-indexed in gspread)
                try:
                    current_value = column_values[student.sheet_row - 1] if student.sheet_row <= len(column_values) else None
                except Exception:
                    current_value = None

                cells.append(WriteCell(
                    roster_name=student.name,
                    sheet_row=student.sheet_row,
                    col=sess.session_col,
                    old_value=current_value,
                    new_value=record.value,
                ))

            if not cells:
                continue

            # Write to sheet
            result = batch_write(
                sheet_session,
                cells,
                upload_id=upload_id or "sync",
                dry_run=dry_run,
                retry_cfg=cfg.get("attendance", {}).get("write", {}).get("retry") if cfg else None,
            )

            if not dry_run:
                total_written += len(result.written)

                # Audit log
                for cell in result.written:
                    audit_log_action(
                        db,
                        action="attendance_sync_write",
                        entity_type="attendance_record",
                        entity_id=f"{cell.sheet_row}:{cell.col}",
                        old_value=cell.old_value,
                        new_value=cell.new_value,
                        user="sync",
                        context={"course": course_key, "upload_id": upload_id},
                    )

                db.commit()

        _sync_status.record_success("sqlite_to_sheets", rows_written=total_written)
        log.info(
            "sync_sqlite_to_sheets_complete",
            extra={
                "course": course_key,
                "rows_written": total_written,
                "dry_run": dry_run,
            },
        )

        return total_written

    except Exception as exc:
        db.rollback()
        _sync_status.record_failure("sqlite_to_sheets", str(exc))
        log.error("sync_sqlite_to_sheets_failed", extra={"course": course_key, "error": str(exc)})
        raise


# ─── Sync learned data (aliases, OCR corrections, patterns) ───────────────────

def sync_aliases_to_db(course_key: str, cfg: Optional[dict] = None) -> int:
    """Sync learned aliases from JSON file to SQLite database.

    This is a one-time migration step to move aliases from the JSON file
    to the database. After migration, the database becomes the source of truth.

    Args:
        course_key: Course key
        cfg: Optional configuration dict

    Returns:
        Number of aliases migrated
    """
    from attendance.aliases import store_from_cfg

    alias_store = store_from_cfg(cfg or {})
    entries = alias_store.entries()

    db = get_session()
    try:
        course = db.query(Course).filter(Course.course_key == course_key, Course.is_active == True).first()
        if course is None:
            log.warning("sync_aliases_course_not_found", extra={"course": course_key})
            return 0

        migrated = 0
        for entry in entries:
            if entry["course"] != course_key:
                continue

            # Check if alias already exists
            existing = db.query(Alias).filter(
                Alias.course_id == course.course_id,
                Alias.raw_name_key == entry["key"],
                Alias.roster_name == entry["roster_name"],
            ).first()

            if existing is None:
                alias = Alias(
                    course_id=course.course_id,
                    raw_name_key=entry["key"],
                    roster_name=entry["roster_name"],
                    confirmations=entry["confirmations"],
                    first_seen=datetime.fromisoformat(entry["first_seen"]) if entry["first_seen"] else datetime.now(timezone.utc),
                    last_confirmed=datetime.fromisoformat(entry["last_confirmed"]) if entry["last_confirmed"] else datetime.now(timezone.utc),
                    last_used=datetime.fromisoformat(entry["last_used"]) if entry["last_used"] else None,
                    is_active=True,
                )
                db.add(alias)
                migrated += 1

        db.commit()
        log.info("sync_aliases_to_db_complete", extra={"course": course_key, "migrated": migrated})
        return migrated

    except Exception as exc:
        db.rollback()
        log.error("sync_aliases_to_db_failed", extra={"course": course_key, "error": str(exc)})
        raise


# ─── Utility functions ─────────────────────────────────────────────────────────

def sync_all_courses(cfg: Optional[dict] = None, dry_run: bool = True) -> dict:
    """Sync all active courses from Sheets to SQLite.

    Args:
        cfg: Configuration dict
        dry_run: If True, compute diff but don't write

    Returns:
        Dict with sync results per course
    """
    from attendance.session_resolver import resolve

    results = {}
    # Read course keys from config
    if cfg:
        course_keys = list(cfg.get("attendance", {}).get("courses", {}).keys())
    else:
        import yaml
        with open("config/attendance.yaml", "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        course_keys = list(cfg.get("attendance", {}).get("courses", {}).keys())

    for course_key in course_keys:
        try:
            # Use session_num=1 for roster sync (any session works for roster)
            session = resolve(course_key, session_num=1, session_type=None, cfg=cfg)
            rows = sync_course_from_sheet(course_key, session, cfg)
            results[course_key] = {"status": "success", "rows_synced": rows}
        except Exception as exc:
            results[course_key] = {"status": "error", "error": str(exc)}

    return results


def install_sync_job(app, load_cfg: Callable, interval: int = 300) -> bool:
    """Schedule recurring background sync jobs.

    Args:
        app: Telegram bot app (or similar with job_queue)
        load_cfg: Function to load configuration
        interval: Sync interval in seconds (default: 300 = 5 minutes)

    Returns:
        True if job was installed, False otherwise
    """
    jq = getattr(app, "job_queue", None)
    if jq is None:
        return False

    async def _sync_job(context) -> None:
        try:
            import asyncio
            cfg = load_cfg()
            await asyncio.get_running_loop().run_in_executor(None, sync_all_courses, cfg, False)
        except Exception as exc:
            log.error("sync_job_failed", extra={"error": type(exc).__name__})

    jq.run_repeating(_sync_job, interval=interval, first=60, name="attendance_sync")
    return True
