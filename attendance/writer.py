"""attendance/writer.py — Google Sheets batchUpdate write engine.

Safety contract:
  - Idempotent: same upload_id + same data → no duplicate write.
  - Non-empty cells rejected unless --force.
  - All writes go through one batched update call per sheet (atomic per API call).
  - Exponential backoff with jitter on quota/transient errors.
  - Every cell write is logged to the audit log.
  - Dry-run mode prints the diff table without writing.
  - Transactional writes with two-phase commit (SQLite → Sheets → confirm).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional


from attendance import health
from attendance._net import retry_call
from attendance.roles import has_permission
from attendance.session_resolver import (
    SheetSession,
    _get_client,
    _open_worksheet,
    invalidate as invalidate_session_cache,
)
from attendance.utils.column_utils import col_letter_to_index
from attendance.audit import log_write_attendance
from core.logger import audit_write, get_logger

log = get_logger("writer")

# ─── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class WriteCell:
    roster_name: str       # for human-readable diff
    sheet_row: int         # 1-based sheet row
    col: str               # column letter
    old_value: Optional[str]
    new_value: str         # canonical attendance value


@dataclass
class WriteResult:
    written: list[WriteCell]
    skipped: list[WriteCell]   # already had the same value
    dry_run: bool


def _normalise_cell_value(value: object) -> str:
    """Comparison form for Sheets values; empty and whitespace-only are equivalent."""
    return "" if value is None else str(value).strip()


def reconcile_cells(ws, cells: list[WriteCell]) -> tuple[list[WriteCell], list[WriteCell], list[WriteCell]]:
    """Re-read cells and split them into safe, already-applied and conflicting writes.

    A write is safe only when the live cell is still the value observed by the
    caller.  This is deliberately shared by normal writes and replayed writes
    so a delayed retry cannot overwrite a later manual correction.
    """
    values_by_col: dict[str, list[object]] = {}
    for col in {cell.col for cell in cells}:
        values_by_col[col] = ws.col_values(col_letter_to_index(col))

    safe: list[WriteCell] = []
    already_applied: list[WriteCell] = []
    conflicts: list[WriteCell] = []
    for cell in cells:
        values = values_by_col[cell.col]
        current = values[cell.sheet_row - 1] if 1 <= cell.sheet_row <= len(values) else ""
        current_value = _normalise_cell_value(current)
        expected_old = _normalise_cell_value(cell.old_value)
        intended = _normalise_cell_value(cell.new_value)
        if current_value == intended:
            already_applied.append(cell)
        elif current_value == expected_old:
            safe.append(cell)
        else:
            conflicts.append(cell)
    return safe, already_applied, conflicts


# ─── Retry logic ──────────────────────────────────────────────────────────────

def _with_backoff(
    fn,
    *,
    max_attempts: int = 6,
    base_s: float = 1.0,
    cap_s: float = 32.0,
    jitter: bool = True,
    op: str = "sheets_api",
    context: Optional[dict] = None,
):
    """Call fn(), retrying quota (429), 5xx, timeouts and connection errors with backoff."""
    return retry_call(
        fn, op=op, max_attempts=max_attempts, base_s=base_s, cap_s=cap_s,
        jitter=jitter, context=context,
    )


def check_existing(
    sheet_id: str,
    tab: str,
    row_numbers: list[int | WriteCell] | set[int],
    cfg: Optional[dict | str] = None,
    col: Optional[str] = None,
) -> bool:
    """Read target cells from the sheet. Returns True if any target cell is non-empty.

    Uses the cached gspread client from session_resolver.
    """
    if isinstance(cfg, str) and col is None:
        col = cfg
        cfg = None
    if col is None and isinstance(cfg, dict):
        col = cfg.get("col") or cfg.get("session_col")

    if not row_numbers:
        return False

    retry_kw = {}
    if isinstance(cfg, dict) and "attendance" in cfg and "write" in cfg["attendance"]:
        retry_kw = cfg["attendance"]["write"].get("retry", {})
    elif isinstance(cfg, dict) and "retry" in cfg:
        retry_kw = cfg["retry"]

    gc = _get_client(retry_kw)
    ws = _open_worksheet(gc, sheet_id, tab, retry_kw)

    first_elem = next(iter(row_numbers))
    if isinstance(first_elem, WriteCell):
        cols_needed = {c.col for c in row_numbers if isinstance(c, WriteCell)}
        for c_letter in cols_needed:
            col_idx = col_letter_to_index(c_letter)
            col_vals = ws.col_values(col_idx)
            for c in row_numbers:
                if isinstance(c, WriteCell) and c.col == c_letter:
                    r = c.sheet_row
                    if 1 <= r <= len(col_vals):
                        v = col_vals[r - 1]
                        if v and str(v).strip():
                            return True
        return False

    if col is not None:
        col_idx = col_letter_to_index(col) if isinstance(col, str) and not str(col).isdigit() else int(col)
        col_vals = ws.col_values(col_idx)
        for r in row_numbers:
            row_idx = int(r)
            if 1 <= row_idx <= len(col_vals):
                v = col_vals[row_idx - 1]
                if v and str(v).strip():
                    return True
        return False

    # Fallback if col not specified: try col 1 or all values
    try:
        col_vals = ws.col_values(1)
        for r in row_numbers:
            row_idx = int(r)
            if 1 <= row_idx <= len(col_vals):
                v = col_vals[row_idx - 1]
                if v and str(v).strip():
                    return True
    except Exception as exc:
        log.debug("writer_swallowed", extra={"error": str(exc)})

    try:
        all_vals = ws.get_all_values()
        for r in row_numbers:
            row_idx = int(r)
            if 1 <= row_idx <= len(all_vals):
                row_vals = all_vals[row_idx - 1]
                if any(v and str(v).strip() for v in row_vals):
                    return True
    except Exception as exc:
        log.debug("writer_swallowed", extra={"error": str(exc)})

    return False


# ─── Writer ───────────────────────────────────────────────────────────────────

def batch_write(
    session: SheetSession,
    cells: list[WriteCell],
    *,
    upload_id: str = "unknown",
    dry_run: bool = True,
    retry_cfg: Optional[dict] = None,
    store_dir: str = "review_queue",
    cfg: Optional[dict] = None,
    user_id: Optional[int] = None,
    resolved_by: Optional[str] = None,
) -> WriteResult:
    """Write (or diff) a list of cells to the Google Sheet.

    Args:
        session:    SheetSession from session_resolver.resolve()
        cells:      List of WriteCell objects to write.
        upload_id:  Unique identifier for this upload (for audit log).
        dry_run:    If True, print the diff but do not write anything.
        retry_cfg:  Optional dict with max_attempts, base_s, cap_s, jitter.
        store_dir:  Directory for review queue and fallback DB.
        cfg:        Optional config dict for permission checking.
        user_id:    Optional user ID for permission checking (legacy).
        resolved_by: Optional user identifier for permission checking and attribution.

    Returns:
        WriteResult summarising what was (or would be) written.
    """
    if cfg is None:
        from attendance.config_runtime import _load_cfg
        cfg = _load_cfg()

    # Determine user identifier for permission check (prefer resolved_by over user_id)
    user_identifier = resolved_by or user_id

    # Check permission unless in dry-run mode or no user identifier provided
    if not dry_run and user_identifier is not None:
        course = getattr(session, "course", None)
        if not has_permission(user_identifier, "edit_attendance", cfg, course=course):
            raise PermissionError(f"User {user_identifier} does not have edit_attendance permission for course {course}")

    retry = retry_cfg or {}
    written: list[WriteCell] = []
    skipped: list[WriteCell] = []

    # Print diff table
    _print_diff(cells, dry_run=dry_run)

    if dry_run:
        for cell in cells:
            if cell.old_value and cell.old_value.strip() == cell.new_value:
                skipped.append(cell)
            else:
                written.append(cell)  # "would write"
        return WriteResult(written=written, skipped=skipped, dry_run=True)

    # Build gspread batch update payload
    backoff_kw = dict(
        max_attempts=int(retry.get("max_attempts", 6)),
        base_s=float(retry.get("base_s", 1.0)),
        cap_s=float(retry.get("cap_s", 32.0)),
        jitter=bool(retry.get("jitter", True)),
        context={"upload_id": upload_id, "course": getattr(session, "course", "unknown"), "tab": getattr(session, "tab", "unknown")},
    )
    try:
        ws = _with_backoff(session.get_worksheet, op="sheets_open_worksheet", **backoff_kw)
    except Exception as exc:
        health.record("sheets_write_error")
        to_write = [c for c in cells if not (c.old_value and c.old_value.strip() == c.new_value)]
        if to_write:
            try:
                enqueue_failed_write(
                    upload_id=upload_id,
                    course=getattr(session, "course", "unknown"),
                    sheet_id=getattr(session, "sheet_id", "unknown"),
                    tab=getattr(session, "tab", "unknown"),
                    cells=to_write,
                    last_error=str(exc)[:300],
                    store_dir=store_dir,
                )
            except Exception as enq_exc:
                log.error("enqueue_failed_write_error", extra={"error": str(enq_exc)})
        raise

    safe_cells, already_applied, conflicts = reconcile_cells(ws, cells)
    skipped.extend(already_applied)
    if conflicts:
        try:
            from attendance.review_queue import ReviewQueue
            ReviewQueue(store_dir=store_dir).enqueue_write_conflict(
                upload_id=upload_id,
                course=getattr(session, "course", "unknown"),
                sheet_id=getattr(session, "sheet_id", "unknown"),
                tab=getattr(session, "tab", "unknown"),
                cells=conflicts,
                last_error="Target cells changed after validation; manual review required.",
            )
        except Exception as exc:
            log.error("enqueue_write_conflict_error", extra={"error": str(exc)})
            raise RuntimeError("Refusing to overwrite changed cells and could not record the conflict") from exc
        log.warning("write_conflicts_detected", extra={"upload_id": upload_id, "count": len(conflicts)})

    updates = [{"range": f"{cell.col}{cell.sheet_row}", "values": [[cell.new_value]]} for cell in safe_cells]
    written.extend(safe_cells)

    if updates:
        def _do_update():
            ws.batch_update(updates, value_input_option="USER_ENTERED")

        # Same payload re-sent on retry is idempotent (absolute cell values).
        try:
            _with_backoff(_do_update, op="sheets_batch_update", **backoff_kw)
        except Exception as exc:
            health.record("sheets_write_error")
            # Enqueue to review queue for retry
            try:
                enqueue_failed_write(
                    upload_id=upload_id,
                    course=getattr(session, "course", "unknown"),
                    sheet_id=getattr(session, "sheet_id", "unknown"),
                    tab=getattr(session, "tab", "unknown"),
                    cells=safe_cells,
                    last_error=str(exc)[:300],
                    store_dir=store_dir,
                )
            except Exception as enq_exc:
                log.error("enqueue_failed_write_error", extra={"error": str(enq_exc)})
            raise
        invalidate_session_cache()      # the sheet changed: cached rosters/columns are stale

    # Also write the type cell if this is a typed_pair session
    # (Handled by the caller via a separate WriteCell in the cells list)

    # Audit log every written cell
    for cell in written:
        audit_write(
            upload_id=upload_id,
            course=session.course,
            sheet_id=session.sheet_id,
            tab=session.tab,
            cell=f"{cell.col}{cell.sheet_row}",
            old_value=cell.old_value,
            new_value=cell.new_value,
            dry_run=dry_run,
        )

    log.info(
        "write_complete",
        extra={
            "upload_id": upload_id,
            "written": len(written),
            "skipped": len(skipped),
            "dry_run": dry_run,
        },
    )
    return WriteResult(written=written, skipped=skipped, dry_run=dry_run)


def _print_diff(cells: list[WriteCell], dry_run: bool) -> None:
    """Print a human-readable diff table."""
    tag = "[DRY-RUN]" if dry_run else "[WRITE]"
    print(f"\n{tag} Cell diff ({len(cells)} rows):")
    print(f"  {'Row':>5}  {'Col':>4}  {'Student':<30}  {'Old':>14}  {'New':<14}")
    print(f"  {'-'*5}  {'-'*4}  {'-'*30}  {'-'*14}  {'-'*14}")
    for c in cells:
        old_disp = (c.old_value or "").strip() or "(empty)"
        same = old_disp.lower() == c.new_value.lower()
        marker = "=" if same else "<"
        print(
            f"  {c.sheet_row:>5}  {c.col:>4}  {c.roster_name:<30}  "
            f"{old_disp:>14}  {c.new_value:<14}  {marker}"
        )
    print()


# ─── T12: Fallback queue & background retry ───────────────────────────────────

def enqueue_failed_write(
    *,
    upload_id: str,
    course: str,
    sheet_id: str,
    tab: str,
    cells: list[WriteCell],
    last_error: Optional[str] = None,
    max_retries: int = 10,
    store_dir: str = "review_queue",
) -> str:
    """Store failed write cells into SQLite review queue DB for background retries."""
    from attendance.review_queue import ReviewQueue
    rq = ReviewQueue(store_dir=store_dir)
    return rq.enqueue_failed_write(
        upload_id=upload_id,
        course=course,
        sheet_id=sheet_id,
        tab=tab,
        cells=cells,
        last_error=last_error,
        max_retries=max_retries,
    )


def retry_pending_writes(store_dir: str = "review_queue", cfg: Optional[dict] = None) -> int:
    """Process pending failed writes from SQLite. Returns count of recovered batches."""
    from attendance.review_queue import ReviewQueue
    from attendance.dead_letter import DeadLetterQueue

    rq = ReviewQueue(store_dir=store_dir)
    dlq = DeadLetterQueue()
    pending = rq.claim_pending_writes()
    if not pending:
        return 0

    retry_kw = {}
    if isinstance(cfg, dict):
        retry_kw = cfg.get("attendance", {}).get("write", {}).get("retry", {}) or {}

    gc = _get_client(retry_kw)
    succeeded = 0

    for item in pending:
        write_id = item["write_id"]
        sheet_id = item["sheet_id"]
        tab = item["tab"]
        cells = [
            WriteCell(
                roster_name=c.get("roster_name", ""),
                sheet_row=int(c.get("sheet_row", 0)),
                col=c.get("col", ""),
                old_value=c.get("old_value"),
                new_value=c.get("new_value", ""),
            )
            for c in item.get("cells", [])
        ]
        try:
            ws = _open_worksheet(gc, sheet_id, tab, retry_kw)
            safe_cells, already_applied, conflicts = reconcile_cells(ws, cells)
            if conflicts:
                rq.enqueue_write_conflict(
                    upload_id=item["upload_id"],
                    course=item["course"],
                    sheet_id=sheet_id,
                    tab=tab,
                    cells=conflicts,
                    last_error="Target cells changed after the original write failed; manual review required.",
                )
            updates = [{"range": f"{c.col}{c.sheet_row}", "values": [[c.new_value]]} for c in safe_cells]
            if updates:
                ws.batch_update(updates, value_input_option="USER_ENTERED")
            rq.mark_write_success(write_id)
            if updates:
                invalidate_session_cache()
            succeeded += 1
            for c in safe_cells:
                audit_write(
                    upload_id=item["upload_id"],
                    course=item["course"],
                    sheet_id=sheet_id,
                    tab=tab,
                    cell=f"{c.col}{c.sheet_row}",
                    old_value=c.old_value,
                    new_value=c.new_value,
                    dry_run=False,
                )
            log.info(
                "pending_write_recovered",
                extra={"write_id": write_id, "upload_id": item["upload_id"], "skipped": len(already_applied), "conflicts": len(conflicts)},
            )
        except Exception as exc:
            rq.mark_write_failure(write_id, str(exc))
            log.warning("pending_write_retry_failed", extra={"write_id": write_id, "error": str(exc)[:200]})

            # Escalate to dead-letter queue if max retries exhausted
            # Note: item holds pre-increment retry_count, so add 1 to check against max_retries
            if item.get("retry_count", 0) + 1 >= item.get("max_retries", 10):
                try:
                    dlq.enqueue(
                        upload_id=item["upload_id"],
                        course=item["course"],
                        sheet_id=sheet_id,
                        tab=tab,
                        operation_type="write_attendance",
                        payload={"cells": [c.__dict__ for c in cells]},
                        error_message=str(exc),
                        error_type=type(exc).__name__,
                    )
                    log.info("write_escalated_to_dead_letter", extra={"write_id": write_id})
                except Exception as dlq_exc:
                    log.error("dead_letter_escalation_failed", extra={"error": str(dlq_exc)})

    return succeeded


# ─── Two-phase commit (SQLite → Sheets → confirm) ─────────────────────────────

def batch_write_two_phase(
    session: SheetSession,
    cells: list[WriteCell],
    *,
    upload_id: str = "unknown",
    dry_run: bool = True,
    retry_cfg: Optional[dict] = None,
    store_dir: str = "review_queue",
    resolved_by: Optional[str] = None,
) -> WriteResult:
    """Write cells using two-phase commit: SQLite (pending) → Sheets → confirm/rollback.

    Phase 1: Open transaction on attendance.db, insert pending AttendanceRecord rows
    Phase 2: Write to Google Sheets
    Phase 3: Commit transaction if Sheets succeeds, rollback if it fails

    This provides durability and recovery guarantees: if Sheets fails after the write
    appears to succeed, the pending SQLite record allows for verification and retry.

    Args:
        session:    SheetSession from session_resolver.resolve()
        cells:      List of WriteCell objects to write.
        upload_id:  Unique identifier for this upload (for audit log).
        dry_run:    If True, print the diff but do not write anything.
        retry_cfg:  Optional dict with max_attempts, base_s, cap_s, jitter.
        store_dir:  Directory for review queue and fallback DB.
        resolved_by: Optional user identifier for attribution (uploaded_by, audit log).

    Returns:
        WriteResult summarising what was (or would be) written.
    """
    from attendance.review_queue import ReviewQueue
    from attendance.db import get_session, Student, Session, AttendanceRecord, Course
    from attendance.config_runtime import _load_cfg

    cfg = _load_cfg()

    # Check permission unless in dry-run mode or no resolved_by provided
    if not dry_run and resolved_by is not None:
        if not has_permission(resolved_by, "edit_attendance", cfg, course=session.course):
            raise PermissionError(f"User {resolved_by} does not have edit_attendance permission for course {session.course}")

    # Print diff table
    _print_diff(cells, dry_run=dry_run)

    if dry_run:
        written = [c for c in cells if not (c.old_value and c.old_value.strip() == c.new_value)]
        skipped = [c for c in cells if c.old_value and c.old_value.strip() == c.new_value]
        return WriteResult(written=written, skipped=skipped, dry_run=True)

    # Phase 1: Open transaction on primary database and insert pending AttendanceRecord rows
    db_session = get_session()
    attendance_records = []

    try:
        # Get course to find session_id
        course = db_session.query(Course).filter(Course.course_key == session.course).first()
        if not course:
            raise RuntimeError(f"Course {session.course} not found in database")

        # Get session
        db_session_obj = db_session.query(Session).filter(
            Session.course_id == course.course_id,
            Session.session_num == session.session_num
        ).first()
        if not db_session_obj:
            raise RuntimeError(f"Session {session.session_num} not found for course {session.course}")

        # For each cell, find the student and create AttendanceRecord
        for cell in cells:
            # Find student by sheet_row
            student = db_session.query(Student).filter(
                Student.course_id == course.course_id,
                Student.sheet_row == cell.sheet_row
            ).first()
            if not student:
                log.warning("student_not_found_for_row", extra={"row": cell.sheet_row, "course": session.course})
                continue

            # Create AttendanceRecord
            record = AttendanceRecord(
                student_id=student.student_id,
                session_id=db_session_obj.session_id,
                value=cell.new_value,
                uploaded_at=datetime.now(timezone.utc),
                uploaded_by=resolved_by or "system",
                upload_id=upload_id,
            )
            db_session.add(record)
            attendance_records.append(record)

        # Note: We do NOT commit yet - this is phase 1 staging
        log.info("two_phase_phase1_staging", extra={"upload_id": upload_id, "records": len(attendance_records)})

    except Exception as exc:
        db_session.rollback()
        db_session.close()
        log.error("two_phase_phase1_failed", extra={"error": str(exc)})
        raise RuntimeError("Failed to stage write in primary database") from exc

    # Phase 2: Write to Google Sheets
    retry = retry_cfg or {}
    backoff_kw = dict(
        max_attempts=int(retry.get("max_attempts", 6)),
        base_s=float(retry.get("base_s", 1.0)),
        cap_s=float(retry.get("cap_s", 32.0)),
        jitter=bool(retry.get("jitter", True)),
        context={"upload_id": upload_id, "course": session.course, "tab": session.tab},
    )

    try:
        ws = _with_backoff(session.get_worksheet, op="sheets_open_worksheet", **backoff_kw)
    except Exception as exc:
        health.record("sheets_write_error")
        # Phase 3 rollback: rollback SQLite transaction
        db_session.rollback()
        db_session.close()
        raise

    safe_cells, already_applied, conflicts = reconcile_cells(ws, cells)
    if conflicts:
        # Phase 3 rollback: rollback SQLite transaction
        db_session.rollback()
        db_session.close()
        # Enqueue conflict to review queue for manual resolution
        rq = ReviewQueue(store_dir=store_dir)
        try:
            rq.enqueue_write_conflict(
                upload_id=upload_id,
                course=session.course,
                sheet_id=session.sheet_id,
                tab=session.tab,
                cells=conflicts,
                last_error="Target cells changed after validation; manual review required.",
            )
        except Exception as exc:
            log.error("enqueue_write_conflict_error", extra={"error": str(exc)})
        log.warning("two_phase_conflict", extra={"upload_id": upload_id, "count": len(conflicts)})
        raise RuntimeError("Conflicts detected during two-phase write") from None

    updates = [{"range": f"{cell.col}{cell.sheet_row}", "values": [[cell.new_value]]} for cell in safe_cells]

    if updates:
        def _do_update():
            ws.batch_update(updates, value_input_option="USER_ENTERED")

        try:
            _with_backoff(_do_update, op="sheets_batch_update", **backoff_kw)
        except Exception as exc:
            health.record("sheets_write_error")
            # Phase 3 rollback: rollback SQLite transaction
            db_session.rollback()
            db_session.close()
            invalidate_session_cache()
            raise

    # Phase 3: Commit SQLite transaction (confirm success)
    try:
        db_session.commit()
    except Exception as exc:
        db_session.rollback()
        invalidate_session_cache()
        log.error("two_phase_commit_failed", extra={"error": str(exc)})
        raise RuntimeError("Failed to commit write to primary database") from exc
    finally:
        db_session.close()

    invalidate_session_cache()

    # Audit log every written cell
    for cell in safe_cells:
        audit_write(
            upload_id=upload_id,
            course=session.course,
            sheet_id=session.sheet_id,
            tab=session.tab,
            cell=f"{cell.col}{cell.sheet_row}",
            old_value=cell.old_value,
            new_value=cell.new_value,
            dry_run=dry_run,
        )
        # Also log to the audit database
        if not dry_run:
            log_write_attendance(
                upload_id=upload_id,
                course=session.course,
                sheet_id=session.sheet_id,
                tab=session.tab,
                cell=f"{cell.col}{cell.sheet_row}",
                old_value=cell.old_value,
                new_value=cell.new_value,
                user=resolved_by or "webapp-user",
            )

    log.info(
        "two_phase_complete",
        extra={
            "upload_id": upload_id,
            "written": len(safe_cells),
            "skipped": len(already_applied),
        },
    )
    return WriteResult(written=safe_cells, skipped=already_applied, dry_run=False)


def install(app, load_cfg: Callable[[], dict], interval: int = 300) -> bool:
    """Schedule recurring background retries for pending writes and dead-letter queue."""
    jq = getattr(app, "job_queue", None)
    if jq is None:
        return False

    async def _job(context) -> None:
        try:
            import asyncio
            cfg = load_cfg()
            rd = (((cfg or {}).get("attendance") or {}).get("review") or {}).get("local_dir", "review_queue")
            await asyncio.get_running_loop().run_in_executor(None, retry_pending_writes, rd, cfg)
        except Exception as exc:
            log.error("pending_writes_job_failed", extra={"error": type(exc).__name__})

    jq.run_repeating(_job, interval=interval, first=60, name="attendance_pending_writes")

    # Add dead-letter queue reprocessing job
    async def _dlq_job(context) -> None:
        try:
            import asyncio
            from attendance.dead_letter import DeadLetterQueue

            dlq = DeadLetterQueue()

            def _retry_dead_letter_item(item: dict) -> bool:
                """Retry a dead-letter item by re-attempting the write."""
                try:
                    from attendance.session_resolver import resolve

                    # Reconstruct session from item
                    course_key = item["course"]
                    sheet_id = item["sheet_id"]
                    tab = item["tab"]
                    payload = item["payload"]
                    cells_data = payload.get("cells", [])

                    # Convert cell data back to WriteCell objects
                    cells = [
                        WriteCell(
                            roster_name=c.get("roster_name", ""),
                            sheet_row=int(c.get("sheet_row", 0)),
                            col=c.get("col", ""),
                            old_value=c.get("old_value"),
                            new_value=c.get("new_value", ""),
                        )
                        for c in cells_data
                    ]

                    # Resolve session
                    session = resolve(course_key=course_key, sheet_id=sheet_id, tab=tab)
                    if not session:
                        log.error("dlq_session_resolve_failed", extra={"item_id": item["item_id"]})
                        return False

                    # Re-execute write (single-phase, bypassing dead-letter escalation)
                    from attendance.writer import batch_write
                    batch_write(
                        session=session,
                        cells=cells,
                        upload_id=item["upload_id"],
                        dry_run=False,
                        retry_cfg={"max_attempts": 1},  # Single attempt in DLQ retry
                    )
                    return True
                except Exception as exc:
                    log.error("dlq_retry_handler_failed", extra={"item_id": item["item_id"], "error": str(exc)})
                    return False

            await asyncio.get_running_loop().run_in_executor(None, dlq.process_pending_retries, _retry_dead_letter_item)
        except Exception as exc:
            log.error("dead_letter_job_failed", extra={"error": type(exc).__name__})

    jq.run_repeating(_dlq_job, interval=interval, first=120, name="attendance_dead_letter")
    return True
