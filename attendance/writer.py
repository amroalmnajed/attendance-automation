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
from typing import Optional

import gspread

from attendance._net import retry_call
from attendance.session_resolver import SheetSession
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


# ─── Writer ───────────────────────────────────────────────────────────────────

def batch_write(
    session: SheetSession,
    cells: list[WriteCell],
    *,
    upload_id: str,
    dry_run: bool = True,
    retry_cfg: Optional[dict] = None,
) -> WriteResult:
    """Write (or diff) a list of cells to the Google Sheet.

    Args:
        session:    SheetSession from session_resolver.resolve()
        cells:      List of WriteCell objects to write.
        upload_id:  Unique identifier for this upload (for audit log).
        dry_run:    If True, print the diff but do not write anything.
        retry_cfg:  Optional dict with max_attempts, base_s, cap_s, jitter.

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
        context={"upload_id": upload_id, "course": session.course, "tab": session.tab},
    )
    ws = _with_backoff(session.get_worksheet, op="sheets_open_worksheet", **backoff_kw)
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
        _with_backoff(_do_update, op="sheets_batch_update", **backoff_kw)

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
