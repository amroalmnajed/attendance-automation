"""Tests for T12: Sheets write failure fallback queue to SQLite + async retry."""
from __future__ import annotations

import json
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
