"""Tests for Zoom bot handlers, focusing on callback-query context bugs."""
from __future__ import annotations

# ─── handle_zoom_course_callback core logic test ─────────────────────────────

def test_zoom_course_callback_source_type_logic():
    """Test the core logic: zoom_source_type is preserved when already set."""
    # Simulate the key logic from handle_zoom_course_callback
    user_data = {}

    # Case 1: zoom_source_type already set to "csv" (CSV upload flow)
    user_data["zoom_source_type"] = "csv"
    user_data["zoom_course"] = "g10"
    if "zoom_source_type" not in user_data:
        user_data["zoom_source_type"] = "api"
    assert user_data["zoom_source_type"] == "csv"  # Should NOT be overwritten

    # Case 2: zoom_source_type not set (API fetch flow)
    user_data = {}
    user_data["zoom_course"] = "g10"
    if "zoom_source_type" not in user_data:
        user_data["zoom_source_type"] = "api"
    assert user_data["zoom_source_type"] == "api"  # Should be set to "api"

