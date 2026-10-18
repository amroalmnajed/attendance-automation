"""tests/test_markdown_escaping.py — Tests for Markdown escaping across message formatters."""
from __future__ import annotations

import pytest

from attendance.review_cards import escape_markdown, format_review_card
from attendance.marks import format_preview, parse_lines


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
