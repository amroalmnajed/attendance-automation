"""Tests for Zoom bot handlers, focusing on callback-query context, input validation, and flow states."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.ext import ConversationHandler

from attendance.bot.common import ZOOM_CONFIRM, ZOOM_MEETING_ID, ZOOM_SESSION_NUM, ZOOM_TEACHER_TIME
from attendance.bot.zoom import (
    cmd_zoom_fetch,
    cmd_zoom_upload,
    handle_zoom_confirm_text,
    handle_zoom_course_callback,
    handle_zoom_session_num_text,
    handle_zoom_teacher_time_text,
)


AUTH_USER_ID = 5674439345


def _make_context(**user_data):
    bot = SimpleNamespace(
        send_message=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    return SimpleNamespace(bot=bot, user_data=dict(user_data))


def _make_callback_update(data: str, user_id: int = AUTH_USER_ID):
    query = SimpleNamespace(
        data=data,
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        message=SimpleNamespace(chat_id=42, reply_text=AsyncMock()),
    )
    user = SimpleNamespace(id=user_id)
    return SimpleNamespace(callback_query=query, effective_message=query.message, effective_user=user)


def _make_text_update(text: str, user_id: int = AUTH_USER_ID):
    replies = []

    async def reply_text(msg_text, **kw):
        replies.append((msg_text, kw))
        return SimpleNamespace(edit_text=AsyncMock())

    msg = SimpleNamespace(chat_id=42, text=text, reply_text=reply_text)
    user = SimpleNamespace(id=user_id)
    return SimpleNamespace(message=msg, effective_message=msg, effective_user=user, replies=replies)


# ─── handle_zoom_course_callback tests ───────────────────────────────────────

def test_zoom_course_callback_preserves_csv_source_type():
    """Test handle_zoom_course_callback preserves zoom_source_type='csv' when set by CSV upload flow."""
    update = _make_callback_update("zoom|course|g10")
    context = _make_context(zoom_meeting_id="81795786552", zoom_source_type="csv")

    with patch("attendance.bot.zoom._handle_instances_or_date", new=AsyncMock(return_value=123)) as mock_next:
        res = asyncio.run(handle_zoom_course_callback(update, context))

    assert res == 123
    assert context.user_data["zoom_course"] == "g10"
    assert context.user_data["zoom_source_type"] == "csv"
    mock_next.assert_awaited_once()


def test_zoom_course_callback_defaults_to_api():
    """Test handle_zoom_course_callback sets zoom_source_type='api' when not already set."""
    update = _make_callback_update("zoom|course|main")
    context = _make_context(zoom_meeting_id="81795786552")

    with patch("attendance.bot.zoom._handle_instances_or_date", new=AsyncMock(return_value=456)) as mock_next:
        res = asyncio.run(handle_zoom_course_callback(update, context))

    assert res == 456
    assert context.user_data["zoom_course"] == "main"
    assert context.user_data["zoom_source_type"] == "api"
    mock_next.assert_awaited_once()


# ─── handle_zoom_session_num_text tests ───────────────────────────────────────

def test_handle_zoom_session_num_text_valid():
    """Valid positive integer saves session_num and continues to attendance processing."""
    update = _make_text_update("3")
    context = _make_context(zoom_course="g10")

    with patch("attendance.bot.zoom._process_zoom_attendance", new=AsyncMock(return_value=789)) as mock_proc:
        res = asyncio.run(handle_zoom_session_num_text(update, context))

    assert res == 789
    assert context.user_data["zoom_session_num"] == 3
    mock_proc.assert_awaited_once()


def test_handle_zoom_session_num_text_invalid():
    """Non-numeric or negative input sends error and stays in ZOOM_SESSION_NUM state."""
    update = _make_text_update("abc")
    context = _make_context(zoom_course="g10")

    res = asyncio.run(handle_zoom_session_num_text(update, context))
    assert res == ZOOM_SESSION_NUM
    assert "zoom_session_num" not in context.user_data
    assert len(update.replies) == 1
    assert "valid session number" in update.replies[0][0]


# ─── handle_zoom_confirm_text tests ──────────────────────────────────────────

def test_handle_zoom_confirm_text_cancel():
    """Replying CANCEL discards and ends conversation."""
    update = _make_text_update("cancel")
    context = _make_context(zoom_course="g10", zoom_summary=SimpleNamespace())

    res = asyncio.run(handle_zoom_confirm_text(update, context))
    assert res == ConversationHandler.END
    assert len(context.user_data) == 0
    assert len(update.replies) == 1
    assert "Cancelled" in update.replies[0][0]


def test_handle_zoom_confirm_text_invalid():
    """Replying with anything other than CONFIRM or CANCEL prompts user again."""
    update = _make_text_update("maybe")
    context = _make_context(zoom_course="g10", zoom_summary=SimpleNamespace())

    res = asyncio.run(handle_zoom_confirm_text(update, context))
    assert res == ZOOM_CONFIRM
    assert len(update.replies) == 1
    assert "CONFIRM" in update.replies[0][0]


def test_handle_zoom_confirm_text_confirm():
    """Replying CONFIRM executes run_zoom write and clears user_data."""
    update = _make_text_update("confirm")
    fake_summary = SimpleNamespace(text=lambda: "Written 5 marks successfully.")
    context = _make_context(
        zoom_course="g10",
        zoom_source={"meeting_id": "123"},
        zoom_session_num=2,
        zoom_session_type=None,
        zoom_summary=fake_summary,
        zoom_manual_start_time="10:30 AM",
    )

    with patch("attendance.bot.zoom.run_zoom", return_value=fake_summary) as mock_run_zoom, \
         patch("attendance.bot.zoom._run_blocking", new=AsyncMock(return_value=fake_summary)):
        res = asyncio.run(handle_zoom_confirm_text(update, context))

    assert res == ConversationHandler.END
    assert len(context.user_data) == 0


def test_zoom_unauthorized_user_refusal():
    """Unauthorized user is blocked by guard and returns ConversationHandler.END."""
    update = _make_callback_update("zoom|course|g10", user_id=999999999)
    context = _make_context(zoom_meeting_id="81795786552")

    res = asyncio.run(handle_zoom_course_callback(update, context))
    assert res == ConversationHandler.END
    update.callback_query.answer.assert_awaited_once()


def test_cmd_zoom_fetch_clears_stale_data():
    """cmd_zoom_fetch clears user_data from prior runs and returns ZOOM_MEETING_ID."""
    update = _make_text_update("/zoom_fetch")
    context = _make_context(zoom_course="old_course", zoom_manual_start_time="10:00")

    res = asyncio.run(cmd_zoom_fetch(update, context))
    assert res == ZOOM_MEETING_ID
    assert len(context.user_data) == 0
    assert len(update.replies) == 1
    assert "Zoom Attendance" in update.replies[0][0]


def test_cmd_zoom_upload_clears_stale_data():
    """cmd_zoom_upload clears user_data and prompts for CSV."""
    update = _make_text_update("/zoom_upload")
    context = _make_context(zoom_course="old_course")

    res = asyncio.run(cmd_zoom_upload(update, context))
    assert res == ZOOM_MEETING_ID
    assert len(context.user_data) == 0
    assert "upload the Zoom participant CSV" in update.replies[0][0]


# ─── handle_zoom_teacher_time_text tests ─────────────────────────────────────

def test_handle_zoom_teacher_time_text_manual_time():
    """Entering a time string stores zoom_manual_start_time and processes attendance."""
    update = _make_text_update("10:30 AM")
    context = _make_context(zoom_course="g10")

    with patch("attendance.bot.zoom._process_zoom_attendance", new=AsyncMock(return_value=999)) as mock_proc:
        res = asyncio.run(handle_zoom_teacher_time_text(update, context))

    assert res == 999
    assert context.user_data["zoom_manual_start_time"] == "10:30 AM"
    mock_proc.assert_awaited_once()


def test_handle_zoom_teacher_time_text_valid_date():
    """Entering a date string stores zoom_date and continues to session type handling."""
    update = _make_text_update("2026-09-13")
    context = _make_context(zoom_course="g10")

    with patch("attendance.bot.zoom._handle_session_type", new=AsyncMock(return_value=888)) as mock_handle_type:
        res = asyncio.run(handle_zoom_teacher_time_text(update, context))

    assert res == 888
    assert "2026-09-13" in context.user_data["zoom_date"]
    mock_handle_type.assert_awaited_once()


def test_handle_zoom_teacher_time_text_invalid_format():
    """Entering garbage format prompts user again and stays in ZOOM_TEACHER_TIME."""
    update = _make_text_update("not_a_date_or_time")
    context = _make_context(zoom_course="g10")

    res = asyncio.run(handle_zoom_teacher_time_text(update, context))
    assert res == ZOOM_TEACHER_TIME
    assert len(update.replies) == 1
    assert "Invalid format" in update.replies[0][0]
    assert "zoom_manual_start_time" not in context.user_data
    assert "zoom_date" not in context.user_data
