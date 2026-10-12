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

from attendance.matcher import MatchResult, ReasonCode
from attendance.ocr import OcrRow
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


# ─── SQLite store ───────────────────────────────────────────────────────────────

class _Store:
    """SQLite backend for review queue data (queue.db, WAL mode).

    Stores one row per upload with the full JSON blob, preserving the _load shape.
    """
    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        """Create table and enable WAL mode if not exists."""
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
                "reason": match_result.reason.value,
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