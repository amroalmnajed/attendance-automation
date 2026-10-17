"""Tests for T5: duplicate sheet upload guard before batch_write."""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from attendance.bot import upload
from attendance.writer import WriteCell, check_existing
from tests.botutil import patch_bot


# ─── Fakes & Helpers ──────────────────────────────────────────────────────────

class FakeBot:
    def __init__(self):
        self.sent = []          # (text, kwargs)

    async def send_message(self, chat_id, text, **kw):
        msg = SimpleNamespace(
            text=text,
            chat_id=chat_id,
            edit_text=self._noop,
            delete=self._noop,
            reply_markup=kw.get("reply_markup"),
        )
        self.sent.append((text, kw, msg))
        return msg

    async def send_chat_action(self, **kw):
        pass

    async def _noop(self, *a, **k):
        pass

    @property
    def texts(self):
        return [t for t, _, _ in self.sent]


def ctx(bot=None, **user_data):
    return SimpleNamespace(bot=bot or FakeBot(), user_data=dict(user_data))


def callback_update(data, chat_id=42):
    edits = []
    deleted = []

    async def answer():
        pass

    async def edit_message_text(text, **kw):
        edits.append(text)

    async def delete():
        deleted.append(True)

    q = SimpleNamespace(
        data=data,
        answer=answer,
        edit_message_text=edit_message_text,
        delete=delete,
        message=SimpleNamespace(chat_id=chat_id, edit_message_text=edit_message_text, delete=delete),
    )
    return SimpleNamespace(callback_query=q), edits, deleted


@pytest.fixture
def mock_sheets(monkeypatch):
    mock_gc = MagicMock()
    mock_ws = MagicMock()
    monkeypatch.setattr("attendance.writer._get_client", lambda *a, **k: mock_gc)
    monkeypatch.setattr("attendance.writer._open_worksheet", lambda *a, **k: mock_ws)
    return mock_ws


# ─── check_existing unit tests ────────────────────────────────────────────────

def test_check_existing_returns_true_when_cell_non_empty(mock_sheets):
    mock_sheets.col_values.return_value = ["", "", "Present", ""]
    # Rows 1 to 4; row 3 is non-empty
    result = check_existing("sheet1", "Tab1", [1, 2, 3, 4], col="E")
    assert result is True
    mock_sheets.col_values.assert_called_once_with(5)


def test_check_existing_returns_false_when_cells_empty(mock_sheets):
    mock_sheets.col_values.return_value = ["", "  ", "", ""]
    result = check_existing("sheet1", "Tab1", [1, 2, 3, 4], col="E")
    assert result is False


def test_check_existing_with_write_cell_objects(mock_sheets):
    mock_sheets.col_values.return_value = ["", "Late"]
    cells = [
        WriteCell("Student A", 1, "E", "", "Present"),
        WriteCell("Student B", 2, "E", "", "Present"),
    ]
    result = check_existing("sheet1", "Tab1", cells)
    assert result is True


def test_check_existing_empty_rows(mock_sheets):
    assert check_existing("sheet1", "Tab1", []) is False


# ─── Bot upload guard tests ───────────────────────────────────────────────────

def test_duplicate_upload_shows_warning_and_skips_write(monkeypatch, tmp_path):
    img = tmp_path / "sheet.jpg"
    img.write_bytes(b"image_content")

    cfg = {
        "core": {"app": {"dry_run_default": False}},
        "attendance": {
            "review": {"local_dir": str(tmp_path / "rq")},
            "courses": {
                "g10": {
                    "sheet_id_env": "SHEET_ID_G10",
                    "tab": "Attendance",
                    "first_student_row": 5,
                    "header_row": 3,
                    "session_header_pattern": r"^S(\d+)$",
                }
            },
        },
    }
    patch_bot(monkeypatch, "_load_cfg", lambda *a, **k: cfg)
    patch_bot(monkeypatch, "resolve", lambda *a, **k: SimpleNamespace(
        sheet_id="test_sheet",
        tab="Attendance",
        session_col="E",
        roster=[("Student A", 5), ("Student B", 6)],
    ))
    # Mock cell-read to return non-empty
    patch_bot(monkeypatch, "check_existing", lambda *a, **k: True)

    write_called = False
    def fail_run(**kw):
        nonlocal write_called
        write_called = True
        return SimpleNamespace(upload_id="u", flagged=0, sheet_url=None)
    patch_bot(monkeypatch, "run", fail_run)

    bot = FakeBot()
    c = ctx(bot, course="g10", session=1, type="Not Set", image_path=str(img), image_paths=[str(img)])

    asyncio.run(upload._run_pipeline_flow(SimpleNamespace(), c, 42, force=False))

    # batch_write / run must NOT be called
    assert write_called is False

    # Warning message sent with inline keyboard
    assert len(bot.sent) == 1
    warn_text, kw, _ = bot.sent[0]
    assert "Attendance already exists for G10 Session 1 on" in warn_text
    markup = kw["reply_markup"]
    buttons = [b for row in markup.inline_keyboard for b in row]
    assert len(buttons) == 2
    assert "Overwrite" in buttons[0].text and buttons[0].callback_data == "up|overwrite"
    assert "Cancel" in buttons[1].text and buttons[1].callback_data == "up|cancel"

    # User data and file must be kept for overwrite/cancel
    assert c.user_data["course"] == "g10"
    assert img.exists()


def test_duplicate_overwrite_proceeds_with_write(monkeypatch, tmp_path):
    img = tmp_path / "sheet.jpg"
    img.write_bytes(b"image_content")

    cfg = {
        "core": {"app": {"dry_run_default": False}},
        "attendance": {"review": {"local_dir": str(tmp_path / "rq")}},
    }
    patch_bot(monkeypatch, "_load_cfg", lambda *a, **k: cfg)

    seen = {}
    patch_bot(monkeypatch, "run", lambda **kw: seen.update(kw) or SimpleNamespace(
        upload_id="u", flagged=0, sheet_url=None, errors=[],
    ))
    async def ok_deliver(*a, **k):
        pass
    patch_bot(monkeypatch, "_deliver_summary", ok_deliver)

    bot = FakeBot()
    c = ctx(bot, course="g10", session=1, type="Not Set", image_path=str(img), image_paths=[str(img)])

    upd, edits, deleted = callback_update("up|overwrite")

    async def _test():
        res = await upload.handle_upload_callback(upd, c)
        assert res == upload.ConversationHandler.END
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if tasks:
            await asyncio.gather(*tasks)

    asyncio.run(_test())

    assert seen.get("force") is True
    assert seen.get("course") == "g10"


def test_duplicate_cancel_aborts_and_cleans_up(monkeypatch, tmp_path):
    img = tmp_path / "sheet.jpg"
    img.write_bytes(b"image_content")

    run_called = False
    patch_bot(monkeypatch, "run", lambda **kw: setattr(run_called, True))

    bot = FakeBot()
    c = ctx(bot, course="g10", session=1, type="Not Set", image_path=str(img), image_paths=[str(img)])

    upd, edits, deleted = callback_update("up|cancel")
    res = asyncio.run(upload.handle_upload_callback(upd, c))

    assert res == upload.ConversationHandler.END
    assert run_called is False
    assert len(edits) == 1
    assert "Cancelled. No changes made." in edits[0]
    assert c.user_data == {}
    assert not img.exists()
