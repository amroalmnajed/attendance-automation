"""tests/test_markdown_escaping.py — T2: Tests for Markdown escaping across bot message formatters."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from attendance.bot.cards import escape_markdown, format_review_card
from attendance.marks import format_preview, parse_lines
from tests.botutil import patch_bot


def test_escape_markdown_edge_cases():
    # Asterisks and parentheses: edge case specified in prompt
    assert escape_markdown("Al-*Hassan* (iPad)") == "Al-\\*Hassan\\* (iPad)"

    # All legacy markdown special characters: * _ ` [ ]
    all_specials = "prefix_*`[]_suffix"
    escaped = escape_markdown(all_specials)
    assert escaped == "prefix\\_\\*\\`\\[\\]\\_suffix"

    # Hyphens, dots, parentheses should remain intact
    plain = "Name-With-Hyphen (Parentheses).dot"
    assert escape_markdown(plain) == "Name-With-Hyphen (Parentheses).dot"

    # Non-string argument
    assert escape_markdown(42) == "42"


def test_format_review_card_escapes_edge_case_name():
    item = {
        "item_id": "test_item_1",
        "raw_name": "Al-*Hassan* (iPad)",
        "paper_row_index": 2,
        "ocr_confidence": 0.85,
        "reason_code": "MATCH_LOW_SCORE",
        "top3": [],
    }
    caption, _ = format_review_card(item, upload_id="up1234567890")
    assert "Paper name: Al-\\*Hassan\\* (iPad) (Row 2)" in caption


def test_format_preview_markdown_escapes_special_names():
    parsed = parse_lines(["Al-*Hassan* (iPad) | L", "Fatima_Zahra", "Omar [test] | zzz"])
    preview = format_preview(parsed, markdown=True)
    assert "1. Al-\\*Hassan\\* (iPad) → Late" in preview
    assert "2. Fatima\\_Zahra → Present (no mark → Present)" in preview
    assert "3. Omar \\[test\\] → ⚠️ “zzz” not recognised (will go to review)" in preview


def test_report_summary_escapes_absentee_names(monkeypatch):
    import attendance.bot.admin as admin_mod
    import attendance.reporter as reporter_mod

    cfg = {"attendance": {"authorized": {"telegram_ids": [1]}, "review": {"local_dir": "review_queue"}}}
    patch_bot(monkeypatch, "_load_cfg", lambda *a, **k: cfg)

    def _mock_report(course, session, cfg, review_dir):
        return {
            "present": 10,
            "total_students": 12,
            "attendance_rate": 83.3,
            "review_backlog": 0,
            "absentees": ["Al-*Hassan* (iPad)", "Sara_Smith [Tablet]"],
        }

    monkeypatch.setattr(reporter_mod, "generate_session_report", _mock_report)

    replies = []

    async def _reply(text, **kw):
        replies.append(text)

    msg = SimpleNamespace(reply_text=_reply, text="/report g10 3", chat_id=1, photo=[], document=None, caption=None)
    upd = SimpleNamespace(effective_user=SimpleNamespace(id=1), message=msg, callback_query=None, effective_chat=SimpleNamespace(id=1))
    ctx = SimpleNamespace(bot=SimpleNamespace(), user_data={}, args=[])

    asyncio.run(admin_mod.cmd_report(upd, ctx))

    assert len(replies) == 1
    assert "• Al-\\*Hassan\\* (iPad)" in replies[0]
    assert "• Sara\\_Smith \\[Tablet\\]" in replies[0]


def test_streaks_output_escapes_student_names(monkeypatch):
    import attendance.bot.admin as admin_mod
    import attendance.reporter as reporter_mod

    cfg = {"attendance": {"authorized": {"telegram_ids": [1]}, "review": {"local_dir": "review_queue"}}}
    patch_bot(monkeypatch, "_load_cfg", lambda *a, **k: cfg)

    def _mock_streaks(course, consec, cfg):
        return [
            {"row": 5, "name": "Al-*Hassan* (iPad)"},
            {"row": 8, "name": "Omar_Khattab `test`"},
        ]

    monkeypatch.setattr(reporter_mod, "generate_streaks_report", _mock_streaks)

    replies = []

    async def _reply(text, **kw):
        replies.append(text)

    msg = SimpleNamespace(reply_text=_reply, text="/streaks g10 3", chat_id=1, photo=[], document=None, caption=None)
    upd = SimpleNamespace(effective_user=SimpleNamespace(id=1), message=msg, callback_query=None, effective_chat=SimpleNamespace(id=1))
    ctx = SimpleNamespace(bot=SimpleNamespace(), user_data={}, args=[])

    asyncio.run(admin_mod.cmd_streaks(upd, ctx))

    assert len(replies) == 1
    assert "• Row 5: Al-\\*Hassan\\* (iPad)" in replies[0]
    assert "• Row 8: Omar\\_Khattab \\`test\\`" in replies[0]
