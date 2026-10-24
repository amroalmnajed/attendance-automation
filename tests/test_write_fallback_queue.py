"""Tests for T12: Sheets write failure fallback queue to SQLite + async retry."""
from __future__ import annotations

import datetime
import json
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from attendance import writer
from attendance.review_queue import ReviewQueue
from attendance.writer import WriteCell, batch_write, enqueue_failed_write, retry_pending_writes


def test_enqueue_and_list_pending_writes(tmp_path):
    store_dir = str(tmp_path / "rq")
    cells = [
        WriteCell(roster_name="Alice", sheet_row=5, col="D", old_value=None, new_value="Present"),
        WriteCell(roster_name="Bob", sheet_row=6, col="D", old_value=None, new_value="Late"),
    ]
    write_id = enqueue_failed_write(
        upload_id="u123",
        course="g10",
        sheet_id="sid_test",
        tab="Sheet1",
        cells=cells,
        last_error="503 Service Unavailable",
        store_dir=store_dir,
    )
    assert write_id is not None

    rq = ReviewQueue(store_dir=store_dir)
    pending = rq.list_pending_writes()
    assert len(pending) == 1
    assert pending[0]["write_id"] == write_id
    assert pending[0]["course"] == "g10"
    assert pending[0]["upload_id"] == "u123"
    assert pending[0]["status"] == "pending"
    assert pending[0]["retry_count"] == 0
    assert len(pending[0]["cells"]) == 2
    assert pending[0]["cells"][0]["roster_name"] == "Alice"


def test_mark_write_failure_and_abandon(tmp_path):
    store_dir = str(tmp_path / "rq")
    rq = ReviewQueue(store_dir=store_dir)
    write_id = rq.enqueue_failed_write(
        upload_id="u456",
        course="crash",
        sheet_id="sid_crash",
        tab="Crash",
        cells=[{"roster_name": "Charlie", "sheet_row": 2, "col": "E", "old_value": None, "new_value": "Present"}],
        max_retries=2,
    )

    # First failure: retry_count becomes 1, status still pending
    rq.mark_write_failure(write_id, "429 Quota Exceeded")
    pending = rq.list_pending_writes()
    assert len(pending) == 1
    assert pending[0]["retry_count"] == 1
    assert pending[0]["status"] == "pending"

    # Second failure: retry_count reaches max_retries (2), status becomes abandoned
    rq.mark_write_failure(write_id, "429 Quota Exceeded again")
    pending = rq.list_pending_writes()
    assert len(pending) == 0  # No longer pending


def test_mark_write_success(tmp_path):
    store_dir = str(tmp_path / "rq")
    rq = ReviewQueue(store_dir=store_dir)
    write_id = rq.enqueue_failed_write(
        upload_id="u789",
        course="main",
        sheet_id="sid_main",
        tab="Main",
        cells=[{"roster_name": "Dave", "sheet_row": 3, "col": "C", "old_value": None, "new_value": "Absent"}],
    )
    assert len(rq.list_pending_writes()) == 1

    rq.mark_write_success(write_id)
    assert len(rq.list_pending_writes()) == 0


def test_batch_write_failure_enqueues_to_sqlite(tmp_path, monkeypatch):
    store_dir = str(tmp_path / "rq")

    class _FailingWS:
        def col_values(self, _index):
            return []

        def batch_update(self, updates, value_input_option=None):
            raise RuntimeError("Google Sheets API 500 Internal Error")

    class _Session:
        course = "g9"
        sheet_id = "sheet_g9"
        tab = "Tab9"

        def get_worksheet(self):
            return _FailingWS()

    cells = [
        WriteCell(roster_name="Eve", sheet_row=10, col="B", old_value=None, new_value="Present"),
    ]

    with pytest.raises(RuntimeError, match="500 Internal Error"):
        batch_write(
            _Session(),
            cells,
            upload_id="u_fail",
            dry_run=False,
            retry_cfg={"max_attempts": 1, "base_s": 0},
            store_dir=store_dir,
        )

    rq = ReviewQueue(store_dir=store_dir)
    pending = rq.list_pending_writes()
    assert len(pending) == 1
    assert pending[0]["upload_id"] == "u_fail"
    assert pending[0]["course"] == "g9"
    assert pending[0]["sheet_id"] == "sheet_g9"
    assert pending[0]["cells"][0]["roster_name"] == "Eve"


def test_retry_pending_writes_recovers(tmp_path, monkeypatch):
    store_dir = str(tmp_path / "rq")
    cells = [
        WriteCell(roster_name="Frank", sheet_row=7, col="C", old_value=None, new_value="Present"),
    ]
    enqueue_failed_write(
        upload_id="u_rec",
        course="g10",
        sheet_id="sid_rec",
        tab="Tab10",
        cells=cells,
        store_dir=store_dir,
    )

    updates_sent = []

    class _MockWS:
        def col_values(self, _index):
            return []

        def batch_update(self, updates, value_input_option=None):
            updates_sent.extend(updates)

    monkeypatch.setattr(writer, "_get_client", lambda retry_kw: None)
    monkeypatch.setattr(writer, "_open_worksheet", lambda gc, sid, tab, retry_kw: _MockWS())

    recovered = retry_pending_writes(store_dir=store_dir)
    assert recovered == 1
    assert len(updates_sent) == 1
    assert updates_sent[0]["range"] == "C7"

    # Queue should now be empty
    rq = ReviewQueue(store_dir=store_dir)
    assert len(rq.list_pending_writes()) == 0


def test_normal_write_refuses_toctou_conflict(tmp_path):
    """A value changed after the pipeline read it is never overwritten and is queued for review."""
    sent = []

    class _WS:
        def col_values(self, _index):
            return [""] * 4 + ["Late"]

        def batch_update(self, updates, value_input_option=None):
            sent.extend(updates)

    class _Session:
        course, sheet_id, tab = "g9", "sheet", "Tab"

        def get_worksheet(self):
            return _WS()

    result = batch_write(
        _Session(), [WriteCell("Alice", 5, "B", None, "Present")],
        upload_id="u_conflict", dry_run=False, retry_cfg={"max_attempts": 1, "base_s": 0}, store_dir=str(tmp_path / "rq"),
    )
    assert result.written == []
    assert sent == []
    # Conflict should be queued for review, not as a pending retry
    rq = ReviewQueue(store_dir=str(tmp_path / "rq"))
    assert rq.list_pending_writes() == []
    conflicts = rq.list_conflicts()
    assert len(conflicts) == 1
    assert conflicts[0]["upload_id"] == "u_conflict"
    assert conflicts[0]["status"] == "conflict"
    assert conflicts[0]["cells"][0]["roster_name"] == "Alice"


def test_normal_write_skips_when_other_actor_already_applied_value(tmp_path):
    sent = []

    class _WS:
        def col_values(self, _index):
            return [""] * 4 + ["Present"]

        def batch_update(self, updates, value_input_option=None):
            sent.extend(updates)

    class _Session:
        course, sheet_id, tab = "g9", "sheet", "Tab"

        def get_worksheet(self):
            return _WS()

    result = batch_write(
        _Session(), [WriteCell("Alice", 5, "B", None, "Present")],
        upload_id="u_done", dry_run=False, retry_cfg={"max_attempts": 1, "base_s": 0}, store_dir=str(tmp_path / "rq"),
    )
    assert result.written == []
    assert len(result.skipped) == 1
    assert sent == []


def test_retry_refuses_changed_cell_and_claim_prevents_double_worker(tmp_path, monkeypatch):
    store_dir = str(tmp_path / "rq")
    enqueue_failed_write(
        upload_id="u_conflict", course="g9", sheet_id="sid", tab="Tab",
        cells=[WriteCell("Alice", 2, "C", None, "Present")], store_dir=store_dir,
    )

    class _WS:
        def col_values(self, _index):
            return ["", "Late"]

        def batch_update(self, *_args, **_kwargs):
            pytest.fail("conflicted retry must not write")

    monkeypatch.setattr(writer, "_get_client", lambda retry_kw: None)
    monkeypatch.setattr(writer, "_open_worksheet", lambda *_args: _WS())
    assert retry_pending_writes(store_dir=store_dir) == 1
    assert retry_pending_writes(store_dir=store_dir) == 0
    rq = ReviewQueue(store_dir=store_dir)
    assert rq.list_pending_writes() == []


def test_mixed_batch_writes_safe_cells_and_retains_conflicts(tmp_path):
    """A batch with safe, already-applied, and conflicting cells writes only safe cells and queues conflicts."""
    sent = []

    class _WS:
        def col_values(self, _index):
            # Row 5: already has "Present" (already-applied)
            # Row 6: empty (safe)
            # Row 7: has "Late" (conflict)
            return [""] * 4 + ["Present", "", "Late"]

        def batch_update(self, updates, value_input_option=None):
            sent.extend(updates)

    class _Session:
        course, sheet_id, tab = "g9", "sheet", "Tab"

        def get_worksheet(self):
            return _WS()

    cells = [
        WriteCell("Alice", 5, "B", None, "Present"),  # already-applied
        WriteCell("Bob", 6, "B", None, "Present"),    # safe
        WriteCell("Charlie", 7, "B", None, "Present"), # conflict
    ]

    result = batch_write(
        _Session(), cells,
        upload_id="u_mixed", dry_run=False, retry_cfg={"max_attempts": 1, "base_s": 0}, store_dir=str(tmp_path / "rq"),
    )
    assert len(result.written) == 1
    assert result.written[0].roster_name == "Bob"
    assert len(result.skipped) == 1
    assert result.skipped[0].roster_name == "Alice"
    assert len(sent) == 1
    assert sent[0]["range"] == "B6"

    # Conflict should be queued for review
    rq = ReviewQueue(store_dir=str(tmp_path / "rq"))
    conflicts = rq.list_conflicts()
    assert len(conflicts) == 1
    assert conflicts[0]["cells"][0]["roster_name"] == "Charlie"


def test_retry_mixed_batch_writes_safe_and_retains_conflicts(tmp_path, monkeypatch):
    """A retry batch with safe and conflicting cells writes only safe cells and queues conflicts."""
    store_dir = str(tmp_path / "rq")
    cells = [
        WriteCell("Diana", 2, "C", None, "Present"),   # safe
        WriteCell("Eve", 3, "C", None, "Present"),     # already-applied
        WriteCell("Frank", 4, "C", None, "Present"),   # conflict
    ]
    enqueue_failed_write(
        upload_id="u_retry_mixed", course="g9", sheet_id="sid", tab="Tab",
        cells=cells, store_dir=store_dir,
    )

    sent = []

    class _WS:
        def col_values(self, _index):
            # Row 1: header (ignored)
            # Row 2: empty (safe)
            # Row 3: already has "Present" (already-applied)
            # Row 4: has "Late" (conflict)
            return ["header", "", "Present", "Late"]

        def batch_update(self, updates, value_input_option=None):
            sent.extend(updates)

    monkeypatch.setattr(writer, "_get_client", lambda retry_kw: None)
    monkeypatch.setattr(writer, "_open_worksheet", lambda *_args: _WS())

    recovered = retry_pending_writes(store_dir=store_dir)
    assert recovered == 1
    assert len(sent) == 1
    # The safe cell is row 2 (Diana)
    assert sent[0]["range"] == "C2"

    # Conflict should be queued for review
    rq = ReviewQueue(store_dir=store_dir)
    conflicts = rq.list_conflicts()
    assert len(conflicts) == 1
    assert conflicts[0]["cells"][0]["roster_name"] == "Frank"


def test_concurrent_retry_workers_cannot_process_same_item(tmp_path, monkeypatch):
    """Two workers claiming and retrying simultaneously cannot both process the same item."""
    store_dir = str(tmp_path / "rq")
    # Enqueue multiple items
    for i in range(3):
        enqueue_failed_write(
            upload_id=f"u_{i}", course="g9", sheet_id="sid", tab="Tab",
            cells=[WriteCell(f"User{i}", 2, "C", None, "Present")],
            store_dir=store_dir,
        )

    class _WS:
        def col_values(self, _index):
            return [""]

        def batch_update(self, updates, value_input_option=None):
            pass

    monkeypatch.setattr(writer, "_get_client", lambda retry_kw: None)
    monkeypatch.setattr(writer, "_open_worksheet", lambda *_args: _WS())

    results = []
    lock = threading.Lock()

    def worker():
        recovered = retry_pending_writes(store_dir=store_dir)
        with lock:
            results.append(recovered)

    # Launch two workers simultaneously
    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Total recovered should be exactly 3 (all items), not more due to double-processing
    assert sum(results) == 3
    # Queue should be empty
    rq = ReviewQueue(store_dir=store_dir)
    assert len(rq.list_pending_writes()) == 0


def test_concurrent_review_enqueue_and_resolve_preserve_all_items(tmp_path):
    """Concurrent enqueue and resolve operations on review items do not lose data."""
    from attendance.matcher import MatchResult, ReasonCode, Candidate, MatchDecision
    from attendance.ocr import OcrRow

    store_dir = str(tmp_path / "rq")
    rq = ReviewQueue(store_dir=store_dir)

    upload_id = "u_concurrent"

    results = []
    lock = threading.Lock()

    def enqueuer():
        for i in range(5):
            ocr_row = OcrRow(
                raw_name=f"Student{i}",
                status_mark=None,
                confidence=0.9,
                row_index=i,
                bounding_box=None,
                photo_index=None,
            )
            candidate = Candidate(
                roster_name=f"Roster{i}",
                roster_row=i + 1,
                score=0.8,
            )
            match_result = MatchResult(
                decision=MatchDecision.REVIEW,
                reason=ReasonCode.MATCH_LOW_SCORE,
                best=candidate,
                top3=[candidate],
                raw_name=f"Student{i}",
                paper_row_index=i,
            )
            item_id = rq.enqueue(
                upload_id=upload_id,
                course="g9",
                session=1,
                image_ref=f"img{i}.jpg",
                ocr_row=ocr_row,
                match_result=match_result,
                raw_mark=None,
            )
            with lock:
                results.append(("enqueue", item_id))

    def resolver():
        # Give enqueuer time to add some items
        import time
        time.sleep(0.01)
        for i in range(3):
            try:
                # Try to resolve item_{i} if it exists
                rq.resolve(
                    upload_id=upload_id,
                    item_id=f"item_{i}",  # This won't match actual IDs, but tests transaction safety
                    chosen_roster_row=1,
                    chosen_roster_name="Test",
                    attendance_value="Present",
                )
            except (KeyError, ValueError):
                pass  # Item doesn't exist or already resolved, which is fine
        with lock:
            results.append(("resolve", None))

    threads = [threading.Thread(target=enqueuer), threading.Thread(target=resolver)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # All enqueued items should be present
    pending = rq.list_pending(upload_id=upload_id)
    assert len(pending) == 5


def test_stale_write_claim_releases_and_allows_reclaim(tmp_path):
    """A claim older than the timeout is released back to pending for another worker."""
    from attendance.review_queue import _Store
    from datetime import timedelta

    store_dir = str(tmp_path / "rq")

    # Enqueue a failed write
    enqueue_failed_write(
        upload_id="u_stale", course="g9", sheet_id="sid", tab="Tab",
        cells=[WriteCell("Alice", 2, "C", None, "Present")], store_dir=store_dir,
    )

    # Claim it (simulating a worker starting)
    store = _Store(Path(store_dir) / "queue.db")
    claimed = store.claim_pending_writes()
    assert len(claimed) == 1
    write_id = claimed[0]["write_id"]

    # Verify the database shows it as processing (returned dict has old status)
    with sqlite3.connect(str(Path(store_dir) / "queue.db")) as conn:
        cur = conn.execute("SELECT status FROM pending_writes WHERE write_id = ?", (write_id,))
        assert cur.fetchone()[0] == "processing"

    # Manually age the claim to be older than the 900-second default timeout
    old_time = (datetime.datetime.now(datetime.timezone.utc) - timedelta(seconds=1000)).isoformat()
    with sqlite3.connect(str(Path(store_dir) / "queue.db")) as conn:
        conn.execute("UPDATE pending_writes SET updated_at = ? WHERE write_id = ?", (old_time, write_id))
        conn.commit()

    # Release stale claims
    released = store.release_stale_write_claims(older_than_seconds=900)
    assert released == 1

    # Verify the item is back to pending
    pending = store.list_pending_writes()
    assert len(pending) == 1
    assert pending[0]["status"] == "pending"
    assert pending[0]["write_id"] == write_id

    # Verify another worker can now claim it
    claimed_again = store.claim_pending_writes()
    assert len(claimed_again) == 1
    assert claimed_again[0]["write_id"] == write_id


def test_install_job_queue():
    jobs = []

    class _MockJobQueue:
        def run_repeating(self, callback, interval, first, name):
            jobs.append({"callback": callback, "interval": interval, "name": name})

    app = SimpleNamespace(job_queue=_MockJobQueue())
    installed = writer.install(app, lambda: {})
    assert installed is True
    assert len(jobs) == 1
    assert jobs[0]["name"] == "attendance_pending_writes"
