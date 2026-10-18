"""tests/test_resolver_ops.py — Unit tests for shared resolve_item in attendance.resolver_ops."""
from __future__ import annotations

import shutil
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from attendance.aliases import AliasStore
from attendance.resolver_ops import ResolveResult, UndoResult, resolve_item, undo_resolution
from attendance.review_queue import ReviewQueue


@pytest.fixture
def tmp_dir():
    d = tempfile.mkdtemp(prefix="test_resolver_ops_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _make_queue(tmp_dir: str) -> ReviewQueue:
    return ReviewQueue(store_dir=tmp_dir)


def _enqueue_sample(rq: ReviewQueue, upload_id: str, raw_name: str = "Test Student") -> str:
    ocr_row = MagicMock()
    ocr_row.raw_name = raw_name
    ocr_row.status_mark = "P"
    ocr_row.confidence = 0.95
    ocr_row.row_index = 2

    match_res = MagicMock()
    match_res.reason.value = "MATCH_LOW_SCORE"
    c1 = MagicMock()
    c1.roster_name = "Candidate One"
    c1.roster_row = 4
    c1.score = 75.0
    match_res.top3 = [c1]

    return rq.enqueue(
        upload_id=upload_id,
        course="crash",
        session=1,
        image_ref="test.jpg",
        ocr_row=ocr_row,
        match_result=match_res,
        raw_mark="P",
    )


def _make_fake_session():
    sess = MagicMock()
    sess.session_col = "D"
    sess.type_col = None
    sess.course = "crash"
    sess.sheet_id = "sheet1"
    sess.tab = "tab1"
    return sess


def test_resolve_item_missing_returns_error(tmp_dir):
    rq = _make_queue(tmp_dir)
    res = resolve_item(
        upload_id="u1",
        item_id="nonexistent",
        chosen_row=1,
        queue=rq,
        writer=MagicMock(),
        alias_store=MagicMock(),
        cfg={"attendance": {}},
    )
    assert not res.success
    assert not res.written
    assert "not found" in (res.error or "")


def test_resolve_item_already_resolved_returns_error(tmp_dir):
    rq = _make_queue(tmp_dir)
    upload_id = "u_done"
    item_id = _enqueue_sample(rq, upload_id)
    rq.resolve(upload_id, item_id, chosen_roster_row=4, chosen_roster_name="Candidate One", attendance_value="Present")

    res = resolve_item(
        upload_id=upload_id,
        item_id=item_id,
        chosen_row=4,
        queue=rq,
        writer=MagicMock(),
        alias_store=MagicMock(),
        cfg={"attendance": {}},
    )
    assert not res.success
    assert "already resolved" in (res.error or "")


def test_resolve_item_skip_row_zero(tmp_dir):
    rq = _make_queue(tmp_dir)
    upload_id = "u_zero"
    item_id = _enqueue_sample(rq, upload_id)

    mock_writer = MagicMock()
    mock_alias = MagicMock()

    res = resolve_item(
        upload_id=upload_id,
        item_id=item_id,
        chosen_row=0,
        queue=rq,
        writer=mock_writer,
        alias_store=mock_alias,
        cfg={"attendance": {}},
    )
    assert res.success
    assert not res.written
    assert not res.alias_learned
    assert mock_writer.batch_write.call_count == 0
    assert mock_alias.learn.call_count == 0

    item = rq.get(upload_id, item_id)
    assert item["status"] == "resolved"
    assert item["resolution"]["roster_row"] == 0


def test_resolve_item_dry_run_leaves_item_pending(tmp_dir):
    rq = _make_queue(tmp_dir)
    upload_id = "u_dry"
    item_id = _enqueue_sample(rq, upload_id)

    mock_writer = MagicMock()
    mock_alias = MagicMock()

    with patch("attendance.resolver_ops.session_resolve", return_value=_make_fake_session()):
        res = resolve_item(
            upload_id=upload_id,
            item_id=item_id,
            chosen_row=4,
            queue=rq,
            writer=mock_writer,
            alias_store=mock_alias,
            cfg={"attendance": {}},
            dry_run=True,
        )

    assert res.success
    assert not res.written
    assert not res.alias_learned
    assert mock_writer.batch_write.call_count == 1
    # Item must remain pending in dry-run
    assert rq.get(upload_id, item_id)["status"] == "pending"


def test_resolve_item_write_failure_returns_error(tmp_dir):
    rq = _make_queue(tmp_dir)
    upload_id = "u_fail"
    item_id = _enqueue_sample(rq, upload_id)

    mock_writer = MagicMock()
    mock_writer.batch_write.side_effect = RuntimeError("Sheets API down")

    with patch("attendance.resolver_ops.session_resolve", return_value=_make_fake_session()):
        res = resolve_item(
            upload_id=upload_id,
            item_id=item_id,
            chosen_row=4,
            queue=rq,
            writer=mock_writer,
            alias_store=MagicMock(),
            cfg={"attendance": {}},
        )

    assert not res.success
    assert not res.written
    assert "Sheets API down" in (res.error or "")
    assert rq.get(upload_id, item_id)["status"] == "pending"


def test_resolve_item_success_flow(tmp_dir):
    rq = _make_queue(tmp_dir)
    upload_id = "u_ok"
    item_id = _enqueue_sample(rq, upload_id, raw_name="Paper Raw")

    mock_writer = MagicMock()
    mock_write_res = MagicMock()
    mock_write_res.written = [1]
    mock_writer.batch_write.return_value = mock_write_res

    mock_alias = MagicMock()

    with patch("attendance.resolver_ops.session_resolve", return_value=_make_fake_session()), \
         patch("attendance.variants.record_resolution"):
        res = resolve_item(
            upload_id=upload_id,
            item_id=item_id,
            chosen_row=4,
            queue=rq,
            writer=mock_writer,
            alias_store=mock_alias,
            cfg={"attendance": {"match": {"learn_aliases": True}}},
            resolved_by="cli",
        )

    assert res.success
    assert res.written
    assert res.alias_learned
    assert res.error is None

    # Check alias learned
    mock_alias.learn.assert_called_once_with("crash", "Paper Raw", "Candidate One")

    # Check item is resolved in queue
    item = rq.get(upload_id, item_id)
    assert item["status"] == "resolved"
    assert item["resolution"]["roster_row"] == 4
    assert item["resolution"]["roster_name"] == "Candidate One"
    assert item["resolution"]["attendance_value"] == "Present"


# ─── Unit Tests for undo_resolution ───────────────────────────────────────────

def test_undo_resolution_clears_cell_and_reenqueues():
    mock_queue = MagicMock()
    mock_writer = MagicMock()

    item = {
        "item_id": "item123",
        "course": "g10",
        "session": 1,
        "image_ref": "img.jpg",
        "raw_name": "Ahmed Ali",
        "raw_mark": "Present",
        "status": "resolved",
        "resolution": {
            "roster_row": 5,
            "roster_name": "Ahmed Ali",
            "attendance_value": "Present",
        },
        "top3": [{"roster_name": "Ahmed Ali", "roster_row": 5, "score": 95.0}],
    }
    mock_queue.get.return_value = item

    cfg = {
        "attendance": {
            "courses": {
                "g10": {
                    "sheet_id_env": "SHEET_G10",
                    "tab": "Att",
                    "first_student_row": 5,
                    "header_row": 3,
                    "session_header_pattern": r"^S(\d+)$",
                }
            },
            "write": {"retry": {}},
        }
    }

    mock_session = SimpleNamespace(
        session_col="E",
        type_col=None,
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("attendance.resolver_ops.session_resolve", lambda *a, **k: mock_session)
        res = undo_resolution("u1", "item123", mock_queue, mock_writer, cfg)

    assert res.success is True
    assert res.cleared is True
    assert res.re_enqueued is True

    # Check cell write cleared cell
    mock_writer.batch_write.assert_called_once()
    written_cells = mock_writer.batch_write.call_args[0][1]
    assert len(written_cells) == 1
    assert written_cells[0].sheet_row == 5
    assert written_cells[0].new_value == ""

    # Check re-enqueue called
    mock_queue.enqueue.assert_called_once()
    eq_kw = mock_queue.enqueue.call_args[1]
    assert eq_kw["upload_id"] == "u1"
    assert eq_kw["course"] == "g10"
    assert eq_kw["ocr_row"].raw_name == "Ahmed Ali"


def test_undo_resolution_item_not_found():
    mock_queue = MagicMock()
    mock_queue.get.return_value = None
    mock_queue._load.return_value = {"items": []}

    res = undo_resolution("u1", "missing", mock_queue)
    assert res.success is False
    assert "not found" in res.error


def test_undo_resolution_ignored_item_does_not_call_batch_write():
    mock_queue = MagicMock()
    mock_writer = MagicMock()

    item = {
        "item_id": "item123",
        "course": "g10",
        "session": 1,
        "image_ref": "img.jpg",
        "raw_name": "Noise",
        "status": "resolved",
        "resolution": {
            "roster_row": 0,
            "roster_name": "IGNORED",
            "attendance_value": "Absent",
        },
        "top3": [],
    }
    mock_queue.get.return_value = item

    res = undo_resolution("u1", "item123", mock_queue, mock_writer)
    assert res.success is True
    assert res.cleared is False
    assert res.re_enqueued is True
    mock_writer.batch_write.assert_not_called()

