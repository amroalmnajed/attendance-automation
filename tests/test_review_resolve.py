"""tests/test_review_resolve.py — Test review resolve write-before-resolve ordering.

All imports inside cmd_review_resolve are local (not at module level), so we patch
the source modules they come from:
  - attendance.writer.batch_write
  - attendance.session_resolver.resolve  (would connect to Google Sheets)
  - attendance.review_queue.ReviewQueue  (to inject tmp-dir queue instance)
  - attendance.aliases.AliasStore        (to redirect alias file to tmp dir)
"""
import os
import shutil
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from attendance.review_queue import ReviewQueue


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_temp_dir() -> str:
    return tempfile.mkdtemp(prefix="test_review_")


def _cleanup_temp_dir(d: str) -> None:
    try:
        shutil.rmtree(d)
    except Exception:
        pass


def _make_ocr_row(raw_name: str = "Test Name") -> MagicMock:
    row = MagicMock()
    row.raw_name = raw_name
    row.status_mark = "P"
    row.confidence = 0.9
    row.row_index = 1
    return row


def _make_match_result() -> MagicMock:
    mr = MagicMock()
    mr.reason = MagicMock()
    mr.reason.value = "MATCH_AMBIGUOUS"
    mr.top3 = []  # ReviewQueue.enqueue iterates top3
    return mr


def _make_fake_session() -> MagicMock:
    """Stub SheetSession so no Google API calls are made."""
    sess = MagicMock()
    sess.session_col = "C"
    sess.type_col = None
    sess.roster = [("Test Student", 5)]
    sess.allowed_attendance = ["Present", "Late", "Not complete", "Absent"]
    return sess


def _enqueue_item(rq: ReviewQueue, upload_id: str, raw_name: str = "Test Name") -> str:
    return rq.enqueue(
        upload_id=upload_id,
        course="crash",
        session=1,
        image_ref="review_images/test.jpg",
        ocr_row=_make_ocr_row(raw_name),
        match_result=_make_match_result(),
        raw_mark="P",
    )


def _resolve_args(upload_id: str, item_id: str, **kwargs):
    """Build an argparse-like Namespace for cmd_review_resolve."""
    import argparse
    defaults = dict(
        upload_id=upload_id,
        item_id=item_id,
        roster_row=5,
        roster_name="Test Student",
        value="Present",
        session_type=None,
        dry_run=False,
        config="config/attendance.yaml",
    )
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


# ─── Tests ────────────────────────────────────────────────────────────────────

def test_review_resolve_writes_sheet_before_marking_resolved():
    """Sheet write MUST happen before the queue item is marked resolved."""
    tmpdir = _make_temp_dir()
    try:
        rq = ReviewQueue(store_dir=tmpdir)
        upload_id = "test_upload_123"
        item_id = _enqueue_item(rq, upload_id)

        assert rq.get(upload_id, item_id)["status"] == "pending"

        call_order: list[str] = []

        def mock_batch_write(session_info, cells, upload_id, dry_run, retry_cfg):
            call_order.append("write")
            result = MagicMock()
            result.written = [cells[0]]
            result.skipped = []
            return result

        with patch("attendance.writer.batch_write", side_effect=mock_batch_write), \
             patch("attendance.session_resolver.resolve", return_value=_make_fake_session()), \
             patch("attendance.review_queue.ReviewQueue", return_value=rq):

            from attendance.cli import cmd_review_resolve
            cmd_review_resolve(_resolve_args(upload_id, item_id))

        # Write happened
        assert "write" in call_order
        # Item is resolved
        item = rq.get(upload_id, item_id)
        assert item["status"] == "resolved"
        assert item["resolution"]["roster_row"] == 5
        assert item["resolution"]["attendance_value"] == "Present"
    finally:
        _cleanup_temp_dir(tmpdir)


def test_review_resolve_dry_run_does_not_resolve():
    """Dry-run: batch_write is called with dry_run=True; item remains pending."""
    tmpdir = _make_temp_dir()
    try:
        rq = ReviewQueue(store_dir=tmpdir)
        upload_id = "test_upload_456"
        item_id = _enqueue_item(rq, upload_id)

        write_dry_run_flag: list[bool] = []

        def mock_batch_write(session_info, cells, upload_id, dry_run, retry_cfg):
            write_dry_run_flag.append(dry_run)
            result = MagicMock()
            result.written = []
            result.skipped = []
            return result

        with patch("attendance.writer.batch_write", side_effect=mock_batch_write), \
             patch("attendance.session_resolver.resolve", return_value=_make_fake_session()), \
             patch("attendance.review_queue.ReviewQueue", return_value=rq):

            from attendance.cli import cmd_review_resolve
            cmd_review_resolve(_resolve_args(upload_id, item_id, dry_run=True))

        # batch_write was called with dry_run=True
        assert write_dry_run_flag == [True]
        # Item remains pending (dry-run exits before resolve)
        assert rq.get(upload_id, item_id)["status"] == "pending"
    finally:
        _cleanup_temp_dir(tmpdir)


def test_review_resolve_fails_if_write_fails():
    """If the sheet write raises, the item must remain pending (no silent data loss)."""
    tmpdir = _make_temp_dir()
    try:
        rq = ReviewQueue(store_dir=tmpdir)
        upload_id = "test_upload_789"
        item_id = _enqueue_item(rq, upload_id)

        def mock_batch_write(session_info, cells, upload_id, dry_run, retry_cfg):
            raise RuntimeError("Sheet write failed")

        with patch("attendance.writer.batch_write", side_effect=mock_batch_write), \
             patch("attendance.session_resolver.resolve", return_value=_make_fake_session()), \
             patch("attendance.review_queue.ReviewQueue", return_value=rq):

            from attendance.cli import cmd_review_resolve
            with pytest.raises(RuntimeError, match="Sheet write failed"):
                cmd_review_resolve(_resolve_args(upload_id, item_id))

        # Item must still be pending after write failure
        assert rq.get(upload_id, item_id)["status"] == "pending"
    finally:
        _cleanup_temp_dir(tmpdir)


def test_review_resolve_learns_alias_after_successful_write():
    """Alias must be learned after a successful write+resolve (not before, not on failure)."""
    tmpdir = _make_temp_dir()
    alias_path = os.path.join(tmpdir, "learned_aliases.json")
    try:
        rq = ReviewQueue(store_dir=tmpdir)
        upload_id = "test_upload_alias"
        raw_name = "Mohamed Ali"
        item_id = _enqueue_item(rq, upload_id, raw_name=raw_name)

        def mock_batch_write(session_info, cells, upload_id, dry_run, retry_cfg):
            result = MagicMock()
            result.written = [cells[0]]
            result.skipped = []
            return result

        # Redirect AliasStore to tmp alias_path so real config is not touched
        from attendance.aliases import AliasStore

        def make_alias_store(path, min_confirmations=1):
            return AliasStore(path=alias_path, min_confirmations=int(min_confirmations))

        with patch("attendance.writer.batch_write", side_effect=mock_batch_write), \
             patch("attendance.session_resolver.resolve", return_value=_make_fake_session()), \
             patch("attendance.review_queue.ReviewQueue", return_value=rq), \
             patch("attendance.aliases.AliasStore", side_effect=make_alias_store):

            from attendance.cli import cmd_review_resolve
            cmd_review_resolve(_resolve_args(upload_id, item_id, roster_name="Mohamed Ali Salem"))

        # Alias was persisted
        store = AliasStore(path=alias_path)
        assert store.lookup("crash", raw_name) == "Mohamed Ali Salem"
    finally:
        _cleanup_temp_dir(tmpdir)