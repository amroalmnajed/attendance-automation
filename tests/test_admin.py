"""tests/test_admin.py — T1: error-path tests for /report, /streaks, /revert, search handler.

All handlers under test must reply with a user-safe string (from _user_error_message)
and must NOT expose the raw exception message when an underlying call raises.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tests.botutil import patch_bot

CFG = {
    "attendance": {
        "authorized": {"telegram_ids": [1]},
        "review": {"local_dir": "review_queue"},
        "write": {"retry": {}},
    },
    "core": {"app": {"dry_run_default": False}},
}
AUTHORIZED_ID = 1


# ─── Shared helpers ───────────────────────────────────────────────────────────

def _make_update(text: str, user_id: int = AUTHORIZED_ID):
    """Build a minimal Update-like object for a command message."""
    replies: list[str] = []

    async def reply_text(msg: str, **_kw):
        replies.append(msg)

    msg = SimpleNamespace(
        reply_text=reply_text,
        text=text,
        chat_id=5,
        photo=[],
        document=None,
        caption=None,
    )
    upd = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        message=msg,
        callback_query=None,
        effective_chat=SimpleNamespace(id=5),
    )
    return upd, replies


def _make_search_update(text: str, user_id: int = AUTHORIZED_ID):
    """Build an Update-like object for the search_input_handler (text message)."""
    replies: list[str] = []

    async def reply_text(msg: str, **_kw):
        replies.append(msg)

    msg = SimpleNamespace(
        reply_text=reply_text,
        text=text,
        chat_id=5,
        photo=[],
        document=None,
        caption=None,
    )
    upd = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        message=msg,
        callback_query=None,
        effective_chat=SimpleNamespace(id=5),
    )
    return upd, replies


def _ctx(user_data: dict | None = None):
    return SimpleNamespace(
        bot=SimpleNamespace(),
        user_data=user_data or {},
        args=[],
    )


# ─── T1: /report error path ───────────────────────────────────────────────────

@pytest.mark.parametrize("text", ["/report g10 3"])
def test_report_error_shows_user_safe_message(monkeypatch, text):
    """When generate_session_report raises, the user must NOT see raw 'boom'."""
    patch_bot(monkeypatch, "_load_cfg", lambda *a, **kw: CFG)

    import attendance.bot.admin as admin_mod

    def _boom(*a, **kw):
        raise Exception("boom")

    monkeypatch.setattr(
        "attendance.reporter.generate_session_report", _boom, raising=False
    )
    # Also patch the import inside the handler
    import attendance.reporter as reporter_mod
    monkeypatch.setattr(reporter_mod, "generate_session_report", _boom)

    upd, replies = _make_update(text)
    asyncio.run(admin_mod.cmd_report(upd, _ctx()))

    assert replies, "no reply sent"
    msg = replies[-1]
    assert "boom" not in msg
    assert "Exception" not in msg
    assert "❌" in msg or "⚠️" in msg


# ─── T1: /streaks error path ──────────────────────────────────────────────────

@pytest.mark.parametrize("text", ["/streaks g10", "/streaks g10 4"])
def test_streaks_error_shows_user_safe_message(monkeypatch, text):
    """When generate_streaks_report raises, user must NOT see raw exception."""
    patch_bot(monkeypatch, "_load_cfg", lambda *a, **kw: CFG)

    import attendance.bot.admin as admin_mod

    def _boom(*a, **kw):
        raise Exception("boom")

    import attendance.reporter as reporter_mod
    monkeypatch.setattr(reporter_mod, "generate_streaks_report", _boom)

    upd, replies = _make_update(text)
    asyncio.run(admin_mod.cmd_streaks(upd, _ctx()))

    assert replies, "no reply sent"
    msg = replies[-1]
    assert "boom" not in msg
    assert "Exception" not in msg
    assert "❌" in msg or "⚠️" in msg


# ─── T1: /revert error path ───────────────────────────────────────────────────

def test_revert_error_shows_user_safe_message(monkeypatch):
    """When the revert logic raises, user must NOT see raw exception."""
    patch_bot(monkeypatch, "_load_cfg", lambda *a, **kw: CFG)

    import attendance.bot.admin as admin_mod

    def _boom(*a, **kw):
        raise Exception("boom")

    import attendance.reporter as reporter_mod
    monkeypatch.setattr(reporter_mod, "read_audit_log", _boom)

    upd, replies = _make_update("/revert abc12345")
    asyncio.run(admin_mod.cmd_revert(upd, _ctx()))

    assert replies, "no reply sent"
    msg = replies[-1]
    assert "boom" not in msg
    assert "Exception" not in msg
    assert "❌" in msg or "⚠️" in msg


# ─── T1: search_input_handler error path ─────────────────────────────────────

def test_search_handler_error_shows_user_safe_message(monkeypatch):
    """When the roster resolve raises, search_input_handler must reply safely."""
    patch_bot(monkeypatch, "_load_cfg", lambda *a, **kw: CFG)

    import attendance.bot.review as review_mod

    def _boom(*a, **kw):
        raise Exception("boom")

    # Make find_by_prefix succeed then resolve raise
    class _FakeQueue:
        def find_by_prefix(self, _):
            return "full_upload_id_123"

        def list_pending(self, _):
            return [
                {
                    "item_id": "item001",
                    "course": "g10",
                    "session": 1,
                    "session_type": None,
                    "raw_name": "Test",
                    "raw_mark": "P",
                    "top3": [],
                    "reason_code": "MATCH_LOW_SCORE",
                    "ocr_confidence": 0.5,
                    "paper_row_index": 2,
                }
            ]

    monkeypatch.setattr(review_mod, "ReviewQueue", lambda **kw: _FakeQueue())
    monkeypatch.setattr(review_mod, "resolve", _boom)

    upd, replies = _make_search_update("ali")
    user_data = {"search_upload_id": "full_uplo", "search_item_id": "item001"}
    asyncio.run(review_mod.search_input_handler(upd, _ctx(user_data)))

    assert replies, "no reply sent"
    msg = replies[-1]
    assert "boom" not in msg
    assert "Exception" not in msg
    assert "❌" in msg or "⚠️" in msg


def test_search_handler_queue_error_shows_user_safe_message(monkeypatch):
    """When the queue lookup itself raises, search_input_handler must reply safely."""
    patch_bot(monkeypatch, "_load_cfg", lambda *a, **kw: CFG)

    import attendance.bot.review as review_mod

    class _BoomQueue:
        def find_by_prefix(self, _):
            raise Exception("boom")

    monkeypatch.setattr(review_mod, "ReviewQueue", lambda **kw: _BoomQueue())

    upd, replies = _make_search_update("ali")
    user_data = {"search_upload_id": "full_uplo", "search_item_id": "item001"}
    asyncio.run(review_mod.search_input_handler(upd, _ctx(user_data)))

    assert replies, "no reply sent"
    msg = replies[-1]
    assert "boom" not in msg
    assert "Exception" not in msg
    assert "❌" in msg or "⚠️" in msg

# ─── T4: /reload_config tests ────────────────────────────────────────────────

def test_reload_config_valid_updates_courses_and_replies_success(monkeypatch):
    import attendance.bot.admin as admin_mod
    from attendance.bot import common

    valid_cfg = {
        "attendance": {
            "authorized": {"telegram_ids": [1]},
            "courses": {
                "course_a": {"layout": "single"},
                "course_b": {"layout": "single"},
                "course_c": {"layout": "single"},
            },
        }
    }
    patch_bot(monkeypatch, "_load_cfg", lambda *a, **k: valid_cfg)

    # Set initial COURSES
    monkeypatch.setattr(common, "COURSES", ["old1", "old2"])

    upd, replies = _make_update("/reload_config")
    asyncio.run(admin_mod.cmd_reload_config(upd, _ctx()))

    assert len(replies) == 1
    assert "✅ Config reloaded — 3 courses loaded." in replies[0]
    assert common.COURSES == ["course_a", "course_b", "course_c"]


def test_reload_config_invalid_rejects_and_does_not_update_courses(monkeypatch):
    import attendance.bot.admin as admin_mod
    from attendance.bot import common

    invalid_cfg = {
        "attendance": {
            "authorized": {"telegram_ids": [1]},
            "courses": {
                "g9": {"layout": "typed_pair", "types": {}},
            },
        }
    }
    patch_bot(monkeypatch, "_load_cfg", lambda *a, **k: invalid_cfg)

    initial_courses = ["crash", "main", "g10", "g9"]
    monkeypatch.setattr(common, "COURSES", list(initial_courses))

    upd, replies = _make_update("/reload_config")
    asyncio.run(admin_mod.cmd_reload_config(upd, _ctx()))

    assert len(replies) == 1
    msg = replies[0]
    assert "layout is typed_pair but 'types' is missing or empty" in msg
    assert common.COURSES == initial_courses


def test_reload_config_unexpected_error_shows_user_safe_message(monkeypatch):
    import attendance.bot.admin as admin_mod

    # We want guard to pass with valid cfg, but handler's _load_cfg to fail
    valid_cfg = {"attendance": {"authorized": {"telegram_ids": [1]}}}
    patch_bot(monkeypatch, "_load_cfg", lambda *a, **k: valid_cfg)

    def _boom(*a, **kw):
        raise RuntimeError("disk broken")

    monkeypatch.setattr("attendance.config_check.validate_cfg", _boom)

    upd, replies = _make_update("/reload_config")
    asyncio.run(admin_mod.cmd_reload_config(upd, _ctx()))

    assert len(replies) == 1
    msg = replies[0]
    assert "disk broken" not in msg
    assert "RuntimeError" not in msg
    assert "❌" in msg or "⚠️" in msg


def test_reload_config_updates_roles(monkeypatch):
    import attendance.bot.admin as admin_mod
    from attendance import roles

    cfg_with_ids = {
        "attendance": {
            "authorized": {"telegram_ids": [1, 10, 20]},
            "courses": {"c1": {"layout": "single"}},
        }
    }
    patch_bot(monkeypatch, "_load_cfg", lambda *a, **k: cfg_with_ids)

    upd, replies = _make_update("/reload_config")
    asyncio.run(admin_mod.cmd_reload_config(upd, _ctx()))

    assert roles.AUTHORIZED_IDS == {1, 10, 20}
