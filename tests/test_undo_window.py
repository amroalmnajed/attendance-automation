"""Tests for T6: undo window on resolution."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from attendance.bot import review
from attendance.resolver_ops import UndoResult, undo_resolution
from tests.botutil import patch_bot


# ─── Fakes & Helpers ──────────────────────────────────────────────────────────

class FakeJob:
    def __init__(self, data, name):
        self.data = data
        self.name = name
        self.removed = False

    def schedule_removal(self):
        self.removed = True


class FakeJobQueue:
    def __init__(self):
        self.jobs = []          # (callback, when, data, name, job)

    def run_once(self, callback, when, data=None, name=None):
        job = FakeJob(data, name)
        self.jobs.append((callback, when, data, name, job))
        return job

    def get_jobs_by_name(self, name):
        return [job for _, _, _, n, job in self.jobs if n == name]


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edited = []

    async def send_message(self, chat_id, text, **kw):
        msg = SimpleNamespace(
            message_id=len(self.sent) + 1,
            chat_id=chat_id,
            text=text,
            reply_markup=kw.get("reply_markup"),
        )
        self.sent.append(msg)
        return msg

    async def edit_message_text(self, chat_id, message_id, text, **kw):
        self.edited.append((chat_id, message_id, text, kw))

    async def send_photo(self, **kw):
        pass


def callback_update(data, chat_id=42, message_id=10):
    edits = []

    async def answer():
        pass

    async def edit_message_text(text, **kw):
        edits.append((text, kw))

    async def delete():
        pass

    q = SimpleNamespace(
        data=data,
        answer=answer,
        edit_message_text=edit_message_text,
        delete=delete,
        message=SimpleNamespace(
            chat_id=chat_id,
            message_id=message_id,
            edit_message_text=edit_message_text,
            delete=delete,
        ),
    )
    return SimpleNamespace(callback_query=q), edits


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
    _, kwargs = mock_writer.batch_write.call_args
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
    mock_queue.enqueue.assert_called_once()


# ─── Bot Review Callback & Undo Tests ─────────────────────────────────────────

def test_handle_resolution_sends_undo_button_and_schedules_timeout(monkeypatch):
    bot = FakeBot()
    jq = FakeJobQueue()
    ctx = SimpleNamespace(bot=bot, job_queue=jq)

    item = {
        "item_id": "item999",
        "course": "g10",
        "session": 1,
        "raw_name": "Student A",
        "raw_mark": "Present",
        "top3": [{"roster_name": "Student A", "roster_row": 5, "score": 90.0}],
    }

    mock_q = MagicMock()
    mock_q.find_by_prefix.return_value = "full_upload_123"
    mock_q.list_pending.return_value = [item]

    patch_bot(monkeypatch, "ReviewQueue", lambda *a, **k: mock_q)
    patch_bot(monkeypatch, "resolve_item", lambda *a, **k: SimpleNamespace(success=True))
    patch_bot(monkeypatch, "_send_next_review_item", lambda *a, **k: asyncio.sleep(0))
    patch_bot(monkeypatch, "_load_cfg", lambda *a, **k: {"attendance": {"review": {}}, "core": {"app": {}}})

    upd, _ = callback_update("res|short_u|item999|5")

    asyncio.run(review.handle_resolution_callback(upd, ctx))

    # Follow-up message sent
    assert len(bot.sent) == 1
    undo_msg = bot.sent[0]
    assert "Resolution recorded" in undo_msg.text
    buttons = [b for row in undo_msg.reply_markup.inline_keyboard for b in row]
    assert len(buttons) == 1
    assert "Undo (30 s)" in buttons[0].text
    # No PII in callback data!
    assert buttons[0].callback_data == "undo|full_upload_123|item999"
    assert "Student" not in buttons[0].callback_data

    # Job scheduled
    assert len(jq.jobs) == 1
    callback, when, data, name, job = jq.jobs[0]
    assert when == 30
    assert name == "undo_full_upload_123_item999"
    assert data["upload_id"] == "full_upload_123"
    assert data["item_id"] == "item999"
    # No PII in job metadata!
    assert "Student" not in str(data)


def test_handle_undo_callback_cancels_job_and_edits_message(monkeypatch):
    bot = FakeBot()
    jq = FakeJobQueue()
    # Pre-populate active job in queue
    job = jq.run_once(lambda *a: None, 30, name="undo_u1_it1")
    ctx = SimpleNamespace(bot=bot, job_queue=jq)

    undo_called = False
    def mock_undo(**kw):
        nonlocal undo_called
        undo_called = True
        return UndoResult(success=True, cleared=True, re_enqueued=True)

    patch_bot(monkeypatch, "undo_resolution", mock_undo)
    patch_bot(monkeypatch, "_load_cfg", lambda *a, **k: {"attendance": {"review": {}}})

    upd, edits = callback_update("undo|u1|it1")

    asyncio.run(review.handle_undo_callback(upd, ctx))

    assert undo_called is True
    assert job.removed is True
    assert len(edits) == 1
    assert "Undone — item returned to review queue." in edits[0][0]
    assert edits[0][1].get("reply_markup") is None


def test_undo_timeout_job_disables_button():
    bot = FakeBot()
    data = {"chat_id": 42, "message_id": 10, "upload_id": "u1", "item_id": "it1"}
    job = FakeJob(data, "undo_u1_it1")
    ctx = SimpleNamespace(bot=bot, job=job)

    asyncio.run(review._undo_timeout_job(ctx))

    assert len(bot.edited) == 1
    chat_id, message_id, text, kw = bot.edited[0]
    assert chat_id == 42
    assert message_id == 10
    assert "Undo window closed." in text
    assert kw.get("reply_markup") is None


def test_handle_undo_callback_error_handling(monkeypatch):
    bot = FakeBot()
    ctx = SimpleNamespace(bot=bot, job_queue=None)

    def mock_undo(**kw):
        raise RuntimeError("boom_sheet_down")

    patch_bot(monkeypatch, "undo_resolution", mock_undo)
    patch_bot(monkeypatch, "_load_cfg", lambda *a, **k: {"attendance": {"review": {}}})

    upd, edits = callback_update("undo|u1|it1")

    asyncio.run(review.handle_undo_callback(upd, ctx))

    assert len(edits) == 1
    err_text = edits[0][0]
    # Generic clean error, no raw exception name or boom_sheet_down
    assert "boom_sheet_down" not in err_text
    assert "RuntimeError" not in err_text
