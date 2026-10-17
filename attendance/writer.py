"""attendance/writer.py — Google Sheets batchUpdate write engine.

Safety contract:
  - Idempotent: same upload_id + same data → no duplicate write.
  - Non-empty cells rejected unless --force.
  - All writes go through one batched update call per sheet (atomic per API call).
  - Exponential backoff with jitter on quota/transient errors.
  - Every cell write is logged to the audit log.
  - Dry-run mode prints the diff table without writing.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional


from attendance import health
from attendance._net import retry_call
from attendance.session_resolver import (
    SheetSession,
    _col_letter_to_index,
    _get_client,
    _open_worksheet,
    invalidate as invalidate_session_cache,
)
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
            col_idx = _col_letter_to_index(c_letter)
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
        col_idx = _col_letter_to_index(col) if isinstance(col, str) and not str(col).isdigit() else int(col)
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
    except Exception:
        pass

    try:
        all_vals = ws.get_all_values()
        for r in row_numbers:
            row_idx = int(r)
            if 1 <= row_idx <= len(all_vals):
                row_vals = all_vals[row_idx - 1]
                if any(v and str(v).strip() for v in row_vals):
                    return True
    except Exception:
        pass

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
) -> WriteResult:
    """Write (or diff) a list of cells to the Google Sheet.

    Args:
        session:    SheetSession from session_resolver.resolve()
        cells:      List of WriteCell objects to write.
        upload_id:  Unique identifier for this upload (for audit log).
        dry_run:    If True, print the diff but do not write anything.
        retry_cfg:  Optional dict with max_attempts, base_s, cap_s, jitter.
        store_dir:  Directory for review queue and fallback DB.

    Returns:
        WriteResult summarising what was (or would be) written.
    """
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

    updates: list[dict] = []

    for cell in cells:
        if cell.old_value and cell.old_value.strip() == cell.new_value:
            skipped.append(cell)
            continue  # idempotent no-op

        # A1 notation: e.g. "E7"
        a1 = f"{cell.col}{cell.sheet_row}"
        updates.append({"range": f"{a1}", "values": [[cell.new_value]]})
        written.append(cell)

    if updates:
        def _do_update():
            ws.batch_update(updates, value_input_option="USER_ENTERED")

        # Same payload re-sent on retry is idempotent (absolute cell values).
        try:
            _with_backoff(_do_update, op="sheets_batch_update", **backoff_kw)
        except Exception as exc:
            health.record("sheets_write_error")
            try:
                enqueue_failed_write(
                    upload_id=upload_id,
                    course=getattr(session, "course", "unknown"),
                    sheet_id=getattr(session, "sheet_id", "unknown"),
                    tab=getattr(session, "tab", "unknown"),
                    cells=written,
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
        marker = "=" if same else "←"
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
    rq = ReviewQueue(store_dir=store_dir)
    pending = rq.list_pending_writes()
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
        updates = [{"range": f"{c.col}{c.sheet_row}", "values": [[c.new_value]]} for c in cells]

        try:
            ws = _open_worksheet(gc, sheet_id, tab, retry_kw)
            ws.batch_update(updates, value_input_option="USER_ENTERED")
            rq.mark_write_success(write_id)
            invalidate_session_cache()
            succeeded += 1
            for c in cells:
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
            log.info("pending_write_recovered", extra={"write_id": write_id, "upload_id": item["upload_id"]})
        except Exception as exc:
            rq.mark_write_failure(write_id, str(exc))
            log.warning("pending_write_retry_failed", extra={"write_id": write_id, "error": str(exc)[:200]})

    return succeeded


def install(app, load_cfg: Callable[[], dict], interval: int = 300) -> bool:
    """Schedule recurring background retries for pending writes."""
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
    return True
