"""attendance/dead_letter.py — Enhanced dead-letter queue with categorization and automatic reprocessing.

Features:
  - Separate dead-letter queue for operations that fail after max retries
  - Categorize failures by type (API error, conflict, validation error)
  - Automatic reprocessing with exponential backoff
  - Manual retry interface in webapp
  - Dead-letter item expiration (configurable, default 30 days)
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

import yaml

from core.logger import get_logger
from core.paths import data_path

log = get_logger("dead_letter")


class FailureCategory(str, Enum):
    """Categories of failure types for dead-letter items."""
    API_ERROR = "api_error"
    CONFLICT = "conflict"
    VALIDATION_ERROR = "validation_error"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    UNKNOWN = "unknown"


class DeadLetterStatus(str, Enum):
    """Status of dead-letter items."""
    PENDING = "pending"
    PROCESSING = "processing"
    RETRY_SCHEDULED = "retry_scheduled"
    RETRYING = "retrying"
    SUCCEEDED = "succeeded"
    ABANDONED = "abandoned"
    EXPIRED = "expired"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(dt_str: str) -> datetime:
    """Parse ISO format datetime string, assuming UTC if naive."""
    dt = datetime.fromisoformat(dt_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _load_config() -> dict:
    """Load dead-letter queue configuration from attendance.yaml."""
    try:
        with open("config/attendance.yaml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        return cfg.get("attendance", {}).get("dead_letter", cfg.get("dead_letter", {}))
    except Exception:
        return {}


def _categorize_error(error: str, error_type: Optional[str] = None) -> FailureCategory:
    """Categorize an error based on error message and type.

    Args:
        error: Error message or exception string
        error_type: Optional error type name

    Returns:
        FailureCategory enum value
    """
    error_lower = error.lower() if error else ""
    type_lower = error_type.lower() if error_type else ""

    # Check for specific error patterns
    if "rate limit" in error_lower or "429" in error_lower or "quota" in error_lower:
        return FailureCategory.RATE_LIMIT
    if "timeout" in error_lower or "timed out" in error_lower:
        return FailureCategory.TIMEOUT
    if "conflict" in error_lower or "already exists" in error_lower or "409" in error_lower:
        return FailureCategory.CONFLICT
    if "validation" in error_lower or "invalid" in error_lower or "400" in error_lower:
        return FailureCategory.VALIDATION_ERROR
    if "api" in error_lower or "http" in error_lower or "500" in error_lower or "502" in error_lower or "503" in error_lower:
        return FailureCategory.API_ERROR

    # Check error type
    if "timeout" in type_lower:
        return FailureCategory.TIMEOUT
    if "validation" in type_lower:
        return FailureCategory.VALIDATION_ERROR

    return FailureCategory.UNKNOWN


def _calculate_backoff(retry_count: int, base_seconds: int = 2, max_seconds: int = 3600) -> int:
    """Calculate exponential backoff with jitter.

    Args:
        retry_count: Current retry count
        base_seconds: Base backoff in seconds
        max_seconds: Maximum backoff in seconds

    Returns:
        Backoff delay in seconds
    """
    # Exponential backoff: base * 2^retry_count
    backoff = base_seconds * (2 ** retry_count)
    # Cap at max
    backoff = min(backoff, max_seconds)
    # Add jitter (±25%)
    import random
    jitter = backoff * 0.25 * (random.random() * 2 - 1)
    return int(backoff + jitter)


class DeadLetterQueue:
    """Enhanced dead-letter queue with categorization and automatic reprocessing."""

    def __init__(self, db_path: Optional[Path] = None):
        """Initialize the dead-letter queue.

        Args:
            db_path: Path to the SQLite database (default: review_queue/dead_letter.db)
        """
        if db_path is None:
            db_path = data_path("dead_letter.db")
        self._db_path = db_path
        self._config = _load_config()
        self._init_db()

    def _init_db(self) -> None:
        """Create tables and enable WAL mode if not exists."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS dead_letter_items (
                    item_id TEXT PRIMARY KEY,
                    upload_id TEXT NOT NULL,
                    course TEXT NOT NULL,
                    sheet_id TEXT NOT NULL,
                    tab TEXT NOT NULL,
                    operation_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    failure_category TEXT NOT NULL,
                    error_message TEXT,
                    error_type TEXT,
                    retry_count INTEGER DEFAULT 0,
                    max_retries INTEGER DEFAULT 10,
                    next_retry_at TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT
                )
            """)
            # Indexes for common queries
            conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON dead_letter_items(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_category ON dead_letter_items(failure_category)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_next_retry ON dead_letter_items(next_retry_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_upload_id ON dead_letter_items(upload_id)")
            conn.commit()

    def enqueue(
        self,
        *,
        upload_id: str,
        course: str,
        sheet_id: str,
        tab: str,
        operation_type: str,
        payload: dict,
        error_message: str,
        error_type: Optional[str] = None,
        max_retries: Optional[int] = None,
        expires_after_days: Optional[int] = None,
    ) -> str:
        """Enqueue a failed operation to the dead-letter queue.

        Args:
            upload_id: Upload ID
            course: Course name
            sheet_id: Google Sheet ID
            tab: Sheet tab name
            operation_type: Type of operation (e.g., "write_attendance", "sync")
            payload: Operation payload as dict
            error_message: Error message
            error_type: Optional error type name
            max_retries: Maximum retry attempts (default from config)
            expires_after_days: Days until expiration (default from config)

        Returns:
            The item_id of the enqueued item
        """
        item_id = str(uuid.uuid4())[:12]
        now = _now_iso()

        # Categorize the error
        category = _categorize_error(error_message, error_type)

        # Get configuration defaults
        if max_retries is None:
            max_retries = int(self._config.get("max_retries", 10))
        if expires_after_days is None:
            expires_after_days = int(self._config.get("expires_after_days", 30))

        # Calculate expiration
        expiration = datetime.now(timezone.utc).timestamp() + (expires_after_days * 86400)
        expires_at = datetime.fromtimestamp(expiration, timezone.utc).isoformat()

        # Calculate initial backoff
        backoff_seconds = _calculate_backoff(0)
        next_retry = datetime.now(timezone.utc).timestamp() + backoff_seconds
        next_retry_at = datetime.fromtimestamp(next_retry, timezone.utc).isoformat()

        with sqlite3.connect(str(self._db_path)) as conn:
            conn.execute(
                """
                INSERT INTO dead_letter_items (
                    item_id, upload_id, course, sheet_id, tab, operation_type,
                    payload_json, failure_category, error_message, error_type,
                    retry_count, max_retries, next_retry_at, status,
                    created_at, updated_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 'retry_scheduled', ?, ?, ?)
                """,
                (
                    item_id,
                    upload_id,
                    course,
                    sheet_id,
                    tab,
                    operation_type,
                    json.dumps(payload, ensure_ascii=False),
                    category.value,
                    error_message[:1000],
                    error_type or "",
                    max_retries,
                    next_retry_at,
                    now,
                    now,
                    expires_at,
                ),
            )
            conn.commit()

        log.info(
            "dead_letter_enqueued",
            extra={
                "item_id": item_id,
                "upload_id": upload_id,
                "category": category.value,
                "operation": operation_type,
            },
        )
        return item_id

    def list_items(
        self,
        status: Optional[DeadLetterStatus] = None,
        category: Optional[FailureCategory] = None,
        upload_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        """List dead-letter items with optional filters.

        Args:
            status: Filter by status
            category: Filter by failure category
            upload_id: Filter by upload ID
            limit: Maximum number of items to return

        Returns:
            List of dead-letter item dicts
        """
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row

            query = "SELECT * FROM dead_letter_items WHERE 1=1"
            params = []

            if status:
                query += " AND status = ?"
                params.append(status.value)
            if category:
                query += " AND failure_category = ?"
                params.append(category.value)
            if upload_id:
                query += " AND upload_id = ?"
                params.append(upload_id)

            query += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)

            cur = conn.execute(query, params)
            rows = []
            for r in cur.fetchall():
                d = dict(r)
                d["payload"] = json.loads(d["payload_json"])
                rows.append(d)
            return rows

    def get_item(self, item_id: str) -> Optional[dict]:
        """Get a single dead-letter item by ID.

        Args:
            item_id: Item ID

        Returns:
            Item dict or None if not found
        """
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT * FROM dead_letter_items WHERE item_id = ?",
                (item_id,)
            )
            row = cur.fetchone()
            if row:
                d = dict(row)
                d["payload"] = json.loads(d["payload_json"])
                return d
            return None

    def claim_retryable_items(self, limit: int = 10) -> list[dict]:
        """Atomically claim items that are ready for retry.

        Args:
            limit: Maximum number of items to claim

        Returns:
            List of claimed item dicts
        """
        now = _now_iso()
        with sqlite3.connect(str(self._db_path), timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")

            # Find items ready for retry
            cur = conn.execute(
                """
                SELECT * FROM dead_letter_items
                WHERE status = 'retry_scheduled'
                  AND next_retry_at <= ?
                  AND retry_count < max_retries
                ORDER BY next_retry_at ASC
                LIMIT ?
                """,
                (now, limit),
            )
            rows = cur.fetchall()
            ids = [row["item_id"] for row in rows]

            # Mark as processing
            if ids:
                conn.executemany(
                    "UPDATE dead_letter_items SET status = 'retrying', updated_at = ? WHERE item_id = ?",
                    [(now, item_id) for item_id in ids],
                )
            conn.commit()

            return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in rows]

    def mark_success(self, item_id: str) -> None:
        """Mark an item as successfully retried.

        Args:
            item_id: Item ID
        """
        now = _now_iso()
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.execute(
                "UPDATE dead_letter_items SET status = 'succeeded', updated_at = ? WHERE item_id = ?",
                (now, item_id),
            )
            conn.commit()

        log.info("dead_letter_succeeded", extra={"item_id": item_id})

    def mark_failure(
        self,
        item_id: str,
        error_message: str,
        error_type: Optional[str] = None,
    ) -> None:
        """Mark an item as failed after a retry attempt.

        Args:
            item_id: Item ID
            error_message: Error message
            error_type: Optional error type name
        """
        now = _now_iso()
        with sqlite3.connect(str(self._db_path)) as conn:
            # Get current retry count
            cur = conn.execute(
                "SELECT retry_count, max_retries FROM dead_letter_items WHERE item_id = ?",
                (item_id,),
            )
            row = cur.fetchone()
            if not row:
                return

            retry_count, max_retries = row
            new_retry_count = retry_count + 1

            # Recategorize if needed
            category = _categorize_error(error_message, error_type)

            # Calculate next backoff
            if new_retry_count < max_retries:
                backoff_seconds = _calculate_backoff(new_retry_count)
                next_retry = datetime.now(timezone.utc).timestamp() + backoff_seconds
                next_retry_at = datetime.fromtimestamp(next_retry, timezone.utc).isoformat()
                new_status = "retry_scheduled"
            else:
                next_retry_at = None
                new_status = "abandoned"

            conn.execute(
                """
                UPDATE dead_letter_items
                SET retry_count = ?,
                    failure_category = ?,
                    error_message = ?,
                    error_type = ?,
                    next_retry_at = ?,
                    status = ?,
                    updated_at = ?
                WHERE item_id = ?
                """,
                (
                    new_retry_count,
                    category.value,
                    error_message[:1000],
                    error_type or "",
                    next_retry_at,
                    new_status,
                    now,
                    item_id,
                ),
            )
            conn.commit()

        log.info(
            "dead_letter_failed",
            extra={
                "item_id": item_id,
                "retry_count": new_retry_count,
                "status": new_status,
            },
        )

    def manual_retry(self, item_id: str) -> bool:
        """Manually trigger a retry for an item (e.g., from webapp).

        Args:
            item_id: Item ID

        Returns:
            True if item was queued for retry, False if not found or already exhausted
        """
        now = _now_iso()
        with sqlite3.connect(str(self._db_path)) as conn:
            # Check if item exists and can be retried
            cur = conn.execute(
                "SELECT retry_count, max_retries FROM dead_letter_items WHERE item_id = ?",
                (item_id,),
            )
            row = cur.fetchone()
            if not row:
                return False

            retry_count, max_retries = row
            if retry_count >= max_retries:
                return False

            # Reset to retrying immediately
            backoff_seconds = _calculate_backoff(retry_count)
            next_retry = datetime.now(timezone.utc).timestamp() + backoff_seconds
            next_retry_at = datetime.fromtimestamp(next_retry, timezone.utc).isoformat()

            conn.execute(
                """
                UPDATE dead_letter_items
                SET status = 'retry_scheduled',
                    next_retry_at = ?,
                    updated_at = ?
                WHERE item_id = ?
                """,
                (next_retry_at, now, item_id),
            )
            conn.commit()

        log.info("dead_letter_manual_retry", extra={"item_id": item_id})
        return True

    def delete_item(self, item_id: str) -> bool:
        """Delete a dead-letter item (e.g., after manual resolution).

        Args:
            item_id: Item ID

        Returns:
            True if item was deleted, False if not found
        """
        with sqlite3.connect(str(self._db_path)) as conn:
            cur = conn.execute(
                "DELETE FROM dead_letter_items WHERE item_id = ?",
                (item_id,),
            )
            conn.commit()
            deleted = cur.rowcount > 0

        if deleted:
            log.info("dead_letter_deleted", extra={"item_id": item_id})
        return deleted

    def expire_old_items(self) -> int:
        """Mark expired items as expired and return count.

        Returns:
            Number of items marked as expired
        """
        now = _now_iso()
        with sqlite3.connect(str(self._db_path)) as conn:
            cur = conn.execute(
                """
                UPDATE dead_letter_items
                SET status = 'expired', updated_at = ?
                WHERE status IN ('pending', 'retry_scheduled', 'retrying')
                  AND expires_at <= ?
                """,
                (now, now),
            )
            conn.commit()
            expired = cur.rowcount

        if expired > 0:
            log.info("dead_letter_expired", extra={"count": expired})
        return expired

    def get_stats(self) -> dict:
        """Get statistics about the dead-letter queue.

        Returns:
            Dict with counts by status and category
        """
        with sqlite3.connect(str(self._db_path)) as conn:
            # By status
            cur = conn.execute(
                "SELECT status, COUNT(*) FROM dead_letter_items GROUP BY status"
            )
            by_status = {row[0]: row[1] for row in cur.fetchall()}

            # By category
            cur = conn.execute(
                "SELECT failure_category, COUNT(*) FROM dead_letter_items GROUP BY failure_category"
            )
            by_category = {row[0]: row[1] for row in cur.fetchall()}

            # Total
            cur = conn.execute("SELECT COUNT(*) FROM dead_letter_items")
            total = cur.fetchone()[0]

            return {
                "total": total,
                "by_status": by_status,
                "by_category": by_category,
            }

    def cleanup_succeeded(self, older_than_days: int = 7) -> int:
        """Delete succeeded items older than the specified days.

        Args:
            older_than_days: Age threshold in days

        Returns:
            Number of items deleted
        """
        cutoff = datetime.now(timezone.utc).timestamp() - (older_than_days * 86400)
        cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()

        with sqlite3.connect(str(self._db_path)) as conn:
            cur = conn.execute(
                """
                DELETE FROM dead_letter_items
                WHERE status = 'succeeded'
                  AND updated_at <= ?
                """,
                (cutoff_iso,),
            )
            conn.commit()
            deleted = cur.rowcount

        if deleted > 0:
            log.info("dead_letter_cleanup_succeeded", extra={"count": deleted})
        return deleted

    def process_pending_retries(self, retry_handler: Callable[[dict], bool]) -> int:
        """Process items that are ready for retry using a provided handler.

        Args:
            retry_handler: Function that takes a dead-letter item dict and returns True if successful

        Returns:
            Number of items successfully retried
        """
        items = self.claim_retryable_items(limit=10)
        succeeded = 0

        for item in items:
            item_id = item["item_id"]
            try:
                success = retry_handler(item)
                if success:
                    self.mark_success(item_id)
                    succeeded += 1
                else:
                    self.mark_failure(
                        item_id,
                        error_message="Retry handler returned False",
                        error_type="RetryFailed",
                    )
            except Exception as exc:
                self.mark_failure(
                    item_id,
                    error_message=str(exc),
                    error_type=type(exc).__name__,
                )

        return succeeded
