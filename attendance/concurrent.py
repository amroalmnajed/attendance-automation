"""attendance/concurrent.py — Parallel processing utilities for image and review operations.

Implementation:
  - Use concurrent.futures.ThreadPoolExecutor for image processing
  - Process multiple OCR rows concurrently
  - Process multiple review items concurrently
  - Thread-safe matcher with per-thread collision tracking
  - Configurable worker pool size (default: 4 workers)
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional, TypeVar

from attendance.matcher import MatchResult, build_matcher
from attendance.ocr import OcrRow
from core.logger import get_logger

log = get_logger("concurrent")

T = TypeVar("T")
R = TypeVar("R")


class ThreadSafeMatcher:
    """Thread-safe wrapper for the matcher with shared cross-thread collision tracking."""

    def __init__(self, match_cfg: dict, alias_lookup: Optional[Callable] = None):
        """Initialize a thread-safe matcher.

        Args:
            match_cfg: Matcher configuration
            alias_lookup: Alias lookup function
        """
        self.match_cfg = match_cfg
        self.alias_lookup = alias_lookup
        self._local = threading.local()
        # Shared claimed set for cross-thread collision detection
        self._claimed: set[int] = set()
        self._claimed_lock = threading.Lock()

    def reset(self) -> None:
        """Reset the shared collision tracker between uploads."""
        with self._claimed_lock:
            self._claimed.clear()
        # Reset thread-local matchers
        if hasattr(self._local, "matcher"):
            self._local.matcher.reset()

    def _get_matcher(self) -> any:
        """Get a thread-local matcher instance with shared collision tracking."""
        if not hasattr(self._local, "matcher"):
            self._local.matcher = build_matcher(
                self.match_cfg,
                alias_lookup=self.alias_lookup,
            )
            # Replace the matcher's per-instance _claimed with our shared set
            self._local.matcher._claimed = self._claimed
            self._local.matcher._claimed_lock = self._claimed_lock
        return self._local.matcher

    def match(
        self,
        raw_name: str,
        ocr_confidence: float,
        roster: list[tuple[str, int]],
        paper_row_index: int,
        min_confidence: float,
        ocr_suggested_name: Optional[str] = None,
        ocr_suggested_confidence: Optional[float] = None,
    ) -> MatchResult:
        """Perform a match in a thread-safe manner.

        Args:
            raw_name: Raw OCR name
            ocr_confidence: OCR confidence score
            roster: Roster list of (name, row) tuples
            paper_row_index: Row index in the paper
            min_confidence: Minimum confidence threshold
            ocr_suggested_name: Optional OCR-suggested name
            ocr_suggested_confidence: Optional OCR-suggested confidence

        Returns:
            MatchResult
        """
        matcher = self._get_matcher()
        return matcher.match(
            raw_name=raw_name,
            ocr_confidence=ocr_confidence,
            roster=roster,
            paper_row_index=paper_row_index,
            min_confidence=min_confidence,
            ocr_suggested_name=ocr_suggested_name,
            ocr_suggested_confidence=ocr_suggested_confidence,
        )


class ConcurrentProcessor:
    """Concurrent processor for batch operations."""

    def __init__(
        self,
        max_workers: int = 4,
        timeout: Optional[float] = None,
    ):
        """Initialize the concurrent processor.

        Args:
            max_workers: Maximum number of worker threads
            timeout: Optional timeout for individual tasks
        """
        self.max_workers = max_workers
        self.timeout = timeout

    def process_items(
        self,
        items: list[T],
        process_fn: Callable[[T], R],
        desc: str = "processing",
    ) -> list[tuple[T, Optional[R], Optional[Exception]]]:
        """Process items concurrently.

        Args:
            items: List of items to process
            process_fn: Function to process each item
            desc: Description for logging

        Returns:
            List of (item, result, error) tuples in original submission order
        """
        results = []

        if not items:
            return results

        log.info(
            "concurrent_start",
            extra={
                "desc": desc,
                "items": len(items),
                "workers": self.max_workers,
            },
        )

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # Submit all tasks
            futures = [
                executor.submit(process_fn, item)
                for item in items
            ]

            # Collect results in submission order to preserve row ordering
            for i, future in enumerate(futures):
                try:
                    result = future.result(timeout=self.timeout)
                    results.append((items[i], result, None))
                except Exception as exc:
                    log.error(
                        "concurrent_item_failed",
                        extra={"desc": desc, "item": str(items[i]), "error": str(exc)},
                    )
                    results.append((items[i], None, exc))

        success_count = sum(1 for _, r, e in results if r is not None and e is None)
        log.info(
            "concomplete",
            extra={
                "desc": desc,
                "total": len(items),
                "success": success_count,
                "failed": len(items) - success_count,
            },
        )

        return results

    def process_ocr_rows(
        self,
        ocr_rows: list[OcrRow],
        match_fn: Callable[[OcrRow], any],
    ) -> list[tuple[OcrRow, Optional[any], Optional[Exception]]]:
        """Process OCR rows concurrently with matching.

        Args:
            ocr_rows: List of OCR rows
            match_fn: Function to match each row

        Returns:
            List of (row, match_result, error) tuples
        """
        return self.process_items(ocr_rows, match_fn, desc="ocr_matching")

    def process_review_items(
        self,
        review_items: list[dict],
        process_fn: Callable[[dict], any],
    ) -> list[tuple[dict, Optional[any], Optional[Exception]]]:
        """Process review queue items concurrently.

        Args:
            review_items: List of review queue items
            process_fn: Function to process each item

        Returns:
            List of (item, result, error) tuples
        """
        return self.process_items(review_items, process_fn, desc="review_processing")


# ─── Configuration helpers ───────────────────────────────────────────────────

def get_max_workers(cfg: dict) -> int:
    """Get the configured max workers from config.

    Args:
        cfg: Configuration dictionary

    Returns:
        Max workers (default: 4)
    """
    concurrency_cfg = cfg.get("attendance", {}).get("concurrency", cfg.get("concurrency", {}))
    return int(concurrency_cfg.get("max_workers", 4))


def create_processor(cfg: dict) -> ConcurrentProcessor:
    """Create a ConcurrentProcessor from configuration.

    Args:
        cfg: Configuration dictionary

    Returns:
        ConcurrentProcessor instance
    """
    concurrency_cfg = cfg.get("attendance", {}).get("concurrency", cfg.get("concurrency", {}))
    return ConcurrentProcessor(
        max_workers=get_max_workers(cfg),
        timeout=concurrency_cfg.get("timeout"),
    )


def create_thread_safe_matcher(
    match_cfg: dict,
    alias_lookup: Optional[Callable] = None,
) -> ThreadSafeMatcher:
    """Create a ThreadSafeMatcher.

    Args:
        match_cfg: Matcher configuration
        alias_lookup: Alias lookup function

    Returns:
        ThreadSafeMatcher instance
    """
    return ThreadSafeMatcher(match_cfg, alias_lookup)


# ─── Batch processing helpers ─────────────────────────────────────────────────

def batch_concurrent(
    items: list[T],
    process_fn: Callable[[T], R],
    batch_size: int = 10,
    max_workers: int = 4,
) -> list[R]:
    """Process items in batches concurrently.

    Args:
        items: List of items to process
        process_fn: Function to process each item
        batch_size: Number of items per batch
        max_workers: Maximum number of workers

    Returns:
        List of results
    """
    results = []

    # Split into batches
    batches = [
        items[i:i + batch_size]
        for i in range(0, len(items), batch_size)
    ]

    def process_batch(batch: list[T]) -> list[R]:
        return [process_fn(item) for item in batch]

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_batch = {
            executor.submit(process_batch, batch): batch
            for batch in batches
        }

        for future in as_completed(future_to_batch):
            try:
                batch_results = future.result()
                results.extend(batch_results)
            except Exception as exc:
                log.error(
                    "batch_failed",
                    extra={"error": str(exc)},
                )

    return results
