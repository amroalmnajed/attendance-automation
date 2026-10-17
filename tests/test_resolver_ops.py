"""tests/test_resolver_ops.py — Unit tests for shared resolve_item in attendance.resolver_ops."""
from __future__ import annotations

import shutil
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from attendance.aliases import AliasStore
from attendance.resolver_ops import ResolveResult, resolve_item
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
