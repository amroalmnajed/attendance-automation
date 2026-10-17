"""attendance/resolver_ops.py — Shared resolution operations for ReviewQueue items.

Used by both the Telegram bot callback (attendance.bot.review) and the CLI
(attendance.cli review resolve).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol

from attendance import variants
from attendance.aliases import AliasStore
from attendance.normalizer import canonical_mark
from attendance.review_queue import ReviewQueue
from attendance.session_resolver import SheetSession, resolve as session_resolve
from attendance.writer import WriteCell, batch_write
from core.logger import get_logger

log = get_logger("resolver_ops")


class Writer(Protocol):
    def batch_write(
        self,
        session: SheetSession,
        cells: list[WriteCell],
        *,
        upload_id: str,
        dry_run: bool = True,
        retry_cfg: Optional[dict] = None,
    ) -> Any:
        ...


@dataclass
class ResolveResult:
    success: bool
    written: bool
    alias_learned: bool
    error: Optional[str] = None


def resolve_item(
    upload_id: str,
    item_id: str,
    chosen_row: int,
    queue: ReviewQueue,
    writer: Any = None,
    alias_store: Optional[AliasStore] = None,
    cfg: Optional[dict] = None,
    dry_run: bool = False,
    *,
    attendance_value: Optional[str] = None,
    roster_name: Optional[str] = None,
    resolved_by: str = "unknown",
) -> ResolveResult:
    """Resolve a review queue item: write to sheet, learn alias, mark resolved.

    Args:
        upload_id: Unique upload ID.
        item_id: Item ID (full or prefix).
        chosen_row: Roster row number (0 = skip/ignore/absent without sheet write).
        queue: ReviewQueue instance.
        writer: Writer module or object with batch_write.
        alias_store: AliasStore instance.
        cfg: Attendance config dict.
        dry_run: If True, simulate sheet write without modifying sheet/aliases/queue.
        attendance_value: Canonical attendance value (e.g. "Present"). If omitted,
            derived from item's raw_mark.
        roster_name: Roster name to write/learn. If omitted, looked up in item's top3.
        resolved_by: Identifier ("cli" or "telegram").

    Returns:
        ResolveResult indicating success, written, alias_learned, and optional error.
    """
    if cfg is None:
        from attendance.bot.common import _load_cfg
        cfg = _load_cfg()

    # 1. Fetch item from queue
    item = queue.get(upload_id, item_id)
    if item is None:
        # Check prefix match in pending or all items
        for cand in queue.list_pending(upload_id) or queue._load(upload_id).get("items", []):
            if cand.get("item_id", "").startswith(item_id):
                item = cand
                break

    if item is None:
        return ResolveResult(
            success=False,
            written=False,
            alias_learned=False,
            error=f"Item {item_id} not found in upload {upload_id}",
        )

    if item.get("status") == "resolved":
        return ResolveResult(
            success=False,
            written=False,
            alias_learned=False,
            error=f"Item {item_id} already resolved",
        )

    # 2. Determine roster_name and attendance_value
    if chosen_row > 0:
        if not roster_name:
            top3 = item.get("top3", [])
            matched = next((m.get("roster_name") for m in top3 if m.get("roster_row") == chosen_row), None)
            roster_name = matched or f"row {chosen_row}"
        if not attendance_value:
            attendance_value = canonical_mark(item.get("raw_mark")) or "Present"
    else:
        roster_name = roster_name or "IGNORED"
        attendance_value = attendance_value or "Absent"

    written = False
    alias_learned = False

    # 3. Write to sheet if chosen_row > 0
    if chosen_row > 0:
        course = item["course"]
        session_num = item["session"]
        session_type = item.get("session_type")

        session_info = session_resolve(
            course_key=course,
            session_num=session_num,
            session_type=session_type,
            cfg=cfg,
        )

        cells = [
            WriteCell(
                roster_name=roster_name,
                sheet_row=chosen_row,
                col=session_info.session_col,
                old_value=None,
                new_value=attendance_value,
            )
        ]

        if session_info.type_col and session_type:
            from attendance.pipeline import _col_idx
            ws = session_info.get_worksheet()
            type_col_data = ws.col_values(_col_idx(session_info.type_col))
            type_current = type_col_data[chosen_row - 1] if chosen_row <= len(type_col_data) else ""
            if not type_current.strip():
                cells.append(WriteCell(
                    roster_name=roster_name,
                    sheet_row=chosen_row,
                    col=session_info.type_col,
                    old_value=type_current or None,
                    new_value=session_type,
                ))

        batch_fn = getattr(writer, "batch_write", None) or batch_write
        retry_cfg = (cfg.get("attendance", {}).get("write", {}) or {}).get("retry", {})
        try:
            write_res = batch_fn(
                session_info,
                cells,
                upload_id=upload_id,
                dry_run=dry_run,
                retry_cfg=retry_cfg,
            )
        except Exception as exc:
            log.error("resolve_item_write_failed", extra={"upload_id": upload_id, "item_id": item_id, "error": str(exc)})
            return ResolveResult(
                success=False,
                written=False,
                alias_learned=False,
                error=str(exc),
            )

        if dry_run:
            return ResolveResult(success=True, written=False, alias_learned=False)

        written = bool(getattr(write_res, "written", True))

    # In dry-run mode, do not learn alias or mark resolved
    if dry_run:
        return ResolveResult(success=True, written=False, alias_learned=False)

    # 4. Learn alias and record variant
    if chosen_row > 0:
        match_cfg = cfg.get("attendance", {}).get("match", {})
        if match_cfg.get("learn_aliases", True):
            if alias_store is None:
                alias_store = AliasStore(
                    path=match_cfg.get("aliases_file", "config/learned_aliases.json"),
                    min_confirmations=int(match_cfg.get("alias_min_confirmations", 1)),
                )
            learn_fn = getattr(alias_store, "learn", getattr(alias_store, "add", None))
            if learn_fn:
                learn_fn(item["course"], item["raw_name"], roster_name)
                alias_learned = True

        variants.record_resolution(cfg, item["course"], item["raw_name"], roster_name)

    # 5. Mark resolved in queue
    actual_item_id = item["item_id"]
    queue.resolve(
        upload_id,
        actual_item_id,
        chosen_roster_row=chosen_row,
        chosen_roster_name=roster_name,
        attendance_value=attendance_value,
        resolved_by=resolved_by,
    )

    return ResolveResult(
        success=True,
        written=written,
        alias_learned=alias_learned,
        error=None,
    )
