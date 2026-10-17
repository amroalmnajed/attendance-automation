"""attendance/review_queue.py — Review queue: persist, list, and resolve flagged items.

Flagged items are written to review_queue/<upload_id>.json (local) or GCS.
Each item contains: image_ref, raw_name, top3 candidates with scores, reason_code.

The `resolve` command picks a candidate and calls the writer.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import pytz
import yaml

from attendance import aliases, writer
from attendance.matcher import MatchResult, ReasonCode
from attendance.ocr import OcrRow
from attendance.session_resolver import SheetSession
from attendance.writer import WriteCell
from core.logger import get_logger

log = get_logger("review_queue")
CAIRO = pytz.timezone("Africa/Cairo")

# ─── Data ─────────────────────────────────────────────────────────────────────


def _bounding_box(ocr_row) -> Optional[list[float]]:
    """[ymin, xmin, ymax, xmax] of the row on the photo (0..1), or None. Tolerates odd test doubles."""
    box = getattr(ocr_row, "bounding_box", None)
    if isinstance(box, (list, tuple)) and len(box) == 4 and all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in box):
        return [float(v) for v in box]
    return None


def _photo_index(ocr_row) -> Optional[int]:
    """1-based photo number for multi-photo uploads; None for single-photo uploads."""
    value = getattr(ocr_row, "photo_index", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _now_iso() -> str:
    return datetime.now(CAIRO).isoformat()


def _load_retention_cfg() -> dict:
    """Load retention config from attendance.yaml."""
    try:
        with open("config/attendance.yaml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        return cfg.get("core", {}).get("retention", {})
    except Exception:
        return {}


def _parse_iso(dt_str: str) -> datetime:
    """Parse ISO format datetime string, assuming Cairo timezone if naive."""
    dt = datetime.fromisoformat(dt_str)
    if dt.tzinfo is None:
        dt = CAIRO.localize(dt)
    return dt


def resolve_item(
    item: dict,
    roster_row: int,
    roster_name: str,
    attendance_value: str,
    session_info: SheetSession,
    cfg: dict,
    upload_id: str,
    dry_run: bool,
    resolved_by: str = "unknown",
) -> None:
    """Shared resolution logic: write to sheet, learn alias, mark resolved.

    Used by both CLI (cmd_review_resolve) and bot (handle_resolution_callback).
    Raises on write failure; caller must mark resolved only after successful write.
    """
    cells: list[WriteCell] = [
        WriteCell(
            roster_name=roster_name,
            sheet_row=roster_row,
            col=session_info.session_col,
            old_value=None,
            new_value=attendance_value,
        )
    ]

    # For typed_pair courses (g9/g10) also write the session-type cell
    session_type = item.get("session_type")
    if session_info.type_col and session_type:
        from attendance.pipeline import _col_idx
        ws = session_info.get_worksheet()
        type_col_data = ws.col_values(_col_idx(session_info.type_col))
        type_current = type_col_data[roster_row - 1] if roster_row <= len(type_col_data) else ""
        if not type_current.strip():
            cells.append(WriteCell(
                roster_name=roster_name,
                sheet_row=roster_row,
                col=session_info.type_col,
                old_value=type_current or None,
                new_value=session_type,
            ))

    writer.batch_write(
        session_info,
        cells,
        upload_id=upload_id,
        dry_run=dry_run,
        retry_cfg=cfg["attendance"]["write"].get("retry", {}),
    )

    # Learn alias if not dry-run
    match_cfg = cfg["attendance"].get("match", {})
    if not dry_run and roster_row > 0 and match_cfg.get("learn_aliases", True):
        aliases.AliasStore(
            path=match_cfg.get("aliases_file", "config/learned_aliases.json"),
            min_confirmations=int(match_cfg.get("alias_min_confirmations", 1)),
        ).learn(item["course"], item["raw_name"], roster_name)

    # Record variant suggestion if not dry-run
    if not dry_run:
        from attendance import variants as variants_mod
        variants_mod.record_resolution(cfg, item["course"], item["raw_name"], roster_name)


# ─── SQLite store ───────────────────────────────────────────────────────────────

class _Store:
    """SQLite backend for review queue data (queue.db, WAL mode).

    Stores one row per upload with the full JSON blob, preserving the _load shape.
    """
    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        """Create tables and enable WAL mode if not exists."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS uploads (
                    upload_id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pending_writes (
                    write_id TEXT PRIMARY KEY,
                    upload_id TEXT NOT NULL,
                    course TEXT NOT NULL,
                    sheet_id TEXT NOT NULL,
                    tab TEXT NOT NULL,
                    cells_json TEXT NOT NULL,
                    retry_count INTEGER DEFAULT 0,
                    max_retries INTEGER DEFAULT 10,
                    last_error TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.commit()

    def enqueue_failed_write(
        self,
        *,
        upload_id: str,
        course: str,
        sheet_id: str,
        tab: str,
        cells: list[dict],
        last_error: Optional[str] = None,
        max_retries: int = 10,
    ) -> str:
        write_id = str(uuid.uuid4())[:12]
        now = _now_iso()
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.execute(
                """
                INSERT INTO pending_writes (
                    write_id, upload_id, course, sheet_id, tab, cells_json,
                    retry_count, max_retries, last_error, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, 'pending', ?, ?)
                """,
                (
                    write_id,
                    upload_id,
                    course,
                    sheet_id,
                    tab,
                    json.dumps(cells, ensure_ascii=False),
                    max_retries,
                    last_error or "",
                    now,
                    now,
                ),
            )
            conn.commit()
        return write_id

    def list_pending_writes(self) -> list[dict]:
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                """
                SELECT write_id, upload_id, course, sheet_id, tab, cells_json,
                       retry_count, max_retries, last_error, status, created_at, updated_at
                FROM pending_writes
                WHERE status = 'pending' AND retry_count < max_retries
                ORDER BY created_at ASC
                """
            )
            rows = []
            for r in cur.fetchall():
                d = dict(r)
                d["cells"] = json.loads(d["cells_json"])
                rows.append(d)
            return rows

    def mark_write_success(self, write_id: str) -> None:
        now = _now_iso()
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.execute(
                "UPDATE pending_writes SET status = 'succeeded', updated_at = ? WHERE write_id = ?",
                (now, write_id),
            )
            conn.commit()

    def mark_write_failure(self, write_id: str, error: str) -> None:
        now = _now_iso()
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.execute(
                """
                UPDATE pending_writes
                SET retry_count = retry_count + 1,
                    last_error = ?,
                    status = CASE WHEN retry_count + 1 >= max_retries THEN 'abandoned' ELSE 'pending' END,
                    updated_at = ?
                WHERE write_id = ?
                """,
                (error[:300], now, write_id),
            )
            conn.commit()

    def _load(self, upload_id: str) -> dict:
        """Load upload data as dict, same shape as JSON files."""
        with sqlite3.connect(str(self._db_path)) as conn:
            cur = conn.execute(
                "SELECT data FROM uploads WHERE upload_id = ?",
                (upload_id,)
            )
            row = cur.fetchone()
            if row:
                return json.loads(row[0])
            return {"upload_id": upload_id, "created_at": _now_iso(), "items": []}

    def _save(self, upload_id: str, data: dict) -> None:
        """Save upload data as JSON blob."""
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO uploads (upload_id, data, created_at) VALUES (?, ?, ?)",
                (upload_id, json.dumps(data, ensure_ascii=False), data.get("created_at", _now_iso()))
            )
            conn.commit()

    def find_by_prefix(self, prefix: str) -> Optional[str]:
        """Find full upload_id by short prefix. Returns None if not found or ambiguous."""
        with sqlite3.connect(str(self._db_path)) as conn:
            cur = conn.execute(
                "SELECT upload_id FROM uploads WHERE upload_id LIKE ?",
                (f"{prefix}%",)
            )
            matches = [row[0] for row in cur.fetchall()]
            if len(matches) == 1:
                return matches[0]
            return None

    def list_all_upload_ids(self) -> list[str]:
        """Return all upload IDs in the store."""
        with sqlite3.connect(str(self._db_path)) as conn:
            cur = conn.execute("SELECT upload_id FROM uploads ORDER BY upload_id")
            return [row[0] for row in cur.fetchall()]

    def import_json(self, json_path: Path) -> bool:
        """Import a JSON file into the store. Returns True if successful."""
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            upload_id = data.get("upload_id", json_path.stem)
            self._save(upload_id, data)
            return True
        except Exception:
            return False


# ─── Queue manager ────────────────────────────────────────────────────────────

class ReviewQueue:
    def __init__(self, store_dir: str = "review_queue"):
        self._dir = Path(store_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._store = _Store(self._dir / "queue.db")
        # One-time import of existing JSON files
        self._import_existing_json()

    def _import_existing_json(self) -> None:
        """Import existing JSON files into SQLite on first access."""
        for json_file in sorted(self._dir.glob("*.json")):
            # Skip already imported files
            if json_file.name.endswith(".migrated"):
                continue
            if self._store.import_json(json_file):
                # Rename to .migrated to mark as imported (keep original)
                try:
                    json_file.rename(json_file.with_suffix(json_file.suffix + ".migrated"))
                except OSError:
                    pass  # Rename failed, but data is imported

    def _path(self, upload_id: str) -> Path:
        return self._dir / f"{upload_id}.json"

    def _load(self, upload_id: str) -> dict:
        return self._store._load(upload_id)

    def _save(self, upload_id: str, data: dict) -> None:
        self._store._save(upload_id, data)

    def get(self, upload_id: str, item_id: str) -> Optional[dict]:
        """Return a single item (any status) or None."""
        for item in self._load(upload_id).get("items", []):
            if item["item_id"] == item_id:
                return item
        return None

    def enqueue_failed_write(
        self,
        *,
        upload_id: str,
        course: str,
        sheet_id: str,
        tab: str,
        cells: list[Any],
        last_error: Optional[str] = None,
        max_retries: int = 10,
    ) -> str:
        cell_dicts = [
            {"roster_name": getattr(c, "roster_name", ""),
             "sheet_row": getattr(c, "sheet_row", 0),
             "col": getattr(c, "col", ""),
             "old_value": getattr(c, "old_value", None),
             "new_value": getattr(c, "new_value", "")}
            if not isinstance(c, dict) else c
            for c in cells
        ]
        return self._store.enqueue_failed_write(
            upload_id=upload_id,
            course=course,
            sheet_id=sheet_id,
            tab=tab,
            cells=cell_dicts,
            last_error=last_error,
            max_retries=max_retries,
        )

    def list_pending_writes(self) -> list[dict]:
        return self._store.list_pending_writes()

    def mark_write_success(self, write_id: str) -> None:
        self._store.mark_write_success(write_id)

    def mark_write_failure(self, write_id: str, error: str) -> None:
        self._store.mark_write_failure(write_id, error)

    def enqueue(
        self,
        *,
        upload_id: str,
        course: str,
        session: int,
        image_ref: str,
        ocr_row: OcrRow,
        match_result: MatchResult,
        raw_mark: Optional[str],
        session_type: Optional[str] = None,
        reason: Optional[ReasonCode | str] = None,
    ) -> str:
        """Add a flagged item. Returns the item_id."""
        item_id = str(uuid.uuid4())[:8]
        reason_val = reason.value if isinstance(reason, ReasonCode) else (reason or match_result.reason.value)
        item = {
            "item_id": item_id,
            "flagged_at": _now_iso(),
            "status": "pending",
            "image_ref": image_ref,
            "course": course,
            "session": session,
            "paper_row_index": ocr_row.row_index,
            "photo_index": _photo_index(ocr_row),
            "bounding_box": _bounding_box(ocr_row),
            "raw_name": ocr_row.raw_name,
            "raw_mark": raw_mark,
            "session_type": session_type,
            "ocr_confidence": ocr_row.confidence,
            "reason_code": reason_val,
            "top3": [
                {
                    "roster_name": c.roster_name,
                    "roster_row": c.roster_row,
                    "score": round(c.score, 2),
                }
                for c in match_result.top3
            ],
        }

        # SQLite handles concurrency with WAL mode; no file_lock needed
        data = self._load(upload_id)
        data["items"].append(item)
        self._save(upload_id, data)

        log.info(
            "review_enqueued",
            extra={
                "upload_id": upload_id,
                "item_id": item_id,
                "reason": reason_val,
            },
        )
        return item_id

    def list_pending(self, upload_id: Optional[str] = None) -> list[dict]:
        """Return all pending review items, optionally filtered by upload_id."""
        items = []
        upload_ids = [upload_id] if upload_id else self._store.list_all_upload_ids()
        for uid in upload_ids:
            data = self._load(uid)
            for item in data.get("items", []):
                if item.get("status") == "pending":
                    items.append({**item, "upload_id": data["upload_id"]})
        return items

    def resolve(
        self,
        upload_id: str,
        item_id: str,
        *,
        chosen_roster_row: int,
        chosen_roster_name: str,
        attendance_value: str,
        resolved_by: str = "cli",
    ) -> dict:
        """Mark an item as resolved and return its write specification.

        SQLite with WAL mode handles concurrency safely. Raises ValueError if the item is already resolved.
        """
        data = self._load(upload_id)
        resolved_item = None
        for item in data["items"]:
            if item["item_id"] == item_id:
                if item.get("status") == "resolved":
                    raise ValueError(f"Item {item_id} already resolved")
                item["status"] = "resolved"
                item["resolved_at"] = _now_iso()
                item["resolved_by"] = resolved_by
                item["resolution"] = {
                    "roster_row": chosen_roster_row,
                    "roster_name": chosen_roster_name,
                    "attendance_value": attendance_value,
                }
                resolved_item = item
                break

        if resolved_item is None:
            raise KeyError(f"Item {item_id} not found in upload {upload_id}")

        self._save(upload_id, data)
        log.info(
            "review_resolved",
            extra={
                "upload_id": upload_id,
                "item_id": item_id,
                "roster_row": chosen_roster_row,
                "value": attendance_value,
            },
        )
        return resolved_item

    def cleanup_resolved_images(self, review_images_dir: str = "review_images") -> int:
        """Delete image files for resolved items older than retention period.

        Returns the number of images deleted.
        """
        retention_cfg = _load_retention_cfg()
        days = int(retention_cfg.get("flagged_image_days_after_resolve", 30))
        cutoff = datetime.now(CAIRO).timestamp() - (days * 86400)

        images_dir = Path(review_images_dir)
        if not images_dir.exists():
            return 0

        deleted = 0
        for upload_id in self._store.list_all_upload_ids():
            data = self._load(upload_id)
            for item in data.get("items", []):
                if item.get("status") != "resolved":
                    continue
                resolved_at_str = item.get("resolved_at")
                if not resolved_at_str:
                    continue
                try:
                    resolved_at = _parse_iso(resolved_at_str).timestamp()
                except Exception:
                    continue
                if resolved_at < cutoff:
                    image_ref = item.get("image_ref")
                    if image_ref:
                        img_path = Path(image_ref)
                        # Only delete if it's in our review_images directory (safety)
                        try:
                            img_path.resolve().relative_to(images_dir.resolve())
                        except ValueError:
                            continue  # not in our managed directory
                        try:
                            img_path.unlink()
                            deleted += 1
                            log.info("review_image_pruned", extra={"image": str(img_path), "item_id": item["item_id"]})
                        except OSError:
                            pass
        return deleted

    def find_by_prefix(self, short_id: str) -> Optional[str]:
        """Find full upload_id by short prefix. Returns None if not found or ambiguous."""
        return self._store.find_by_prefix(short_id)

    def print_pending(self, upload_id: Optional[str] = None) -> None:
        """Pretty-print all pending items to stdout."""
        items = self.list_pending(upload_id)
        if not items:
            print("No pending review items.")
            return

        print(f"\n{'='*70}")
        print(f"PENDING REVIEW ITEMS ({len(items)} total)")
        print(f"{'='*70}")
        for item in items:
            print(f"\n  upload : {item['upload_id']} / item {item['item_id']}")
            print(f"  reason : {item['reason_code']}")
            print(f"  paper  : row {item['paper_row_index']} — {item['raw_name']!r}  mark={item['raw_mark']!r}")
            print(f"  OCR    : confidence={item['ocr_confidence']:.2f}")
            print(f"  top3   :")
            for i, c in enumerate(item.get("top3", []), 1):
                print(f"    [{i}] row={c['roster_row']:>4}  score={c['score']:>5.1f}  {c['roster_name']}")
        print()