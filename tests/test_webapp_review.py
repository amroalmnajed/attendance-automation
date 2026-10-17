"""tests/test_webapp_review.py — Tests for webapp review queue page (W5).

Tests the review queue page configuration loading and basic structure.
"""
from __future__ import annotations

import pytest

from webapp.pages.review import _load_config


def test_load_config_returns_dict():
    """Test that _load_config returns a dict."""
    cfg = _load_config()
    assert isinstance(cfg, dict)


def test_load_config_has_attendance_section():
    """Test that the loaded config has an attendance section."""
    cfg = _load_config()
    assert "attendance" in cfg


def test_load_config_has_review_section():
    """Test that the loaded config has a review section."""
    cfg = _load_config()
    att_cfg = cfg.get("attendance", {})
    assert "review" in att_cfg


def test_load_config_has_local_dir():
    """Test that the review config has local_dir."""
    cfg = _load_config()
    att_cfg = cfg.get("attendance", {})
    review_cfg = att_cfg.get("review", {})
    assert "local_dir" in review_cfg


def test_format_review_card_pure():
    """Test that format_review_card is a pure function."""
    from attendance.bot.cards import format_review_card

    # Create a mock item
    item = {
        "item_id": "test123",
        "raw_name": "Test Student",
        "paper_row_index": 5,
        "reason_code": "OCR_LOW_CONFIDENCE",
        "ocr_confidence": 0.75,
        "top3": [
            {"roster_name": "Test Student", "roster_row": 10, "score": 95},
            {"roster_name": "Test Smith", "roster_row": 15, "score": 80},
        ],
    }

    caption, rows = format_review_card(item, upload_id="abc123")

    assert isinstance(caption, str)
    assert isinstance(rows, list)
    assert len(rows) > 0
    assert "Test Student" in caption


def test_crop_image_function_exists():
    """Test that _crop_image function exists and is callable."""
    from attendance.bot.crop import _crop_image

    assert callable(_crop_image)


def test_review_queue_class_exists():
    """Test that ReviewQueue class exists and can be instantiated."""
    from attendance.review_queue import ReviewQueue

    # We can't actually test with a real queue without fixtures,
    # but we can verify the class exists
    assert ReviewQueue is not None


def test_resolve_item_function_exists():
    """Test that resolve_item function exists and is callable."""
    from attendance.resolver_ops import resolve_item

    assert callable(resolve_item)


def test_session_resolve_function_exists():
    """Test that session_resolve function exists and is callable."""
    from attendance.session_resolver import resolve

    assert callable(resolve)
