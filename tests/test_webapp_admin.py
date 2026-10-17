"""tests/test_webapp_admin.py — Tests for webapp admin page (W7).

Tests that the admin page stubs exist and are callable.
"""
from __future__ import annotations

import pytest


def test_page_admin_function_exists():
    """Test that _page_admin function exists and is callable."""
    from webapp.pages.admin import _page_admin

    assert callable(_page_admin)


def test_render_revert_stub_function_exists():
    """Test that _render_revert_stub function exists and is callable."""
    from webapp.pages.admin import _render_revert_stub

    assert callable(_render_revert_stub)


def test_render_absent_offer_stub_function_exists():
    """Test that _render_absent_offer_stub function exists and is callable."""
    from webapp.pages.admin import _render_absent_offer_stub

    assert callable(_render_absent_offer_stub)


def test_render_aliases_stub_function_exists():
    """Test that _render_aliases_stub function exists and is callable."""
    from webapp.pages.admin import _render_aliases_stub

    assert callable(_render_aliases_stub)


def test_render_health_stub_function_exists():
    """Test that _render_health_stub function exists and is callable."""
    from webapp.pages.admin import _render_health_stub

    assert callable(_render_health_stub)


def test_admin_page_has_four_stubs():
    """Test that the admin page has four stub functions."""
    import webapp.pages.admin as admin

    # Check that all stub functions are defined
    assert hasattr(admin, "_render_revert_stub")
    assert hasattr(admin, "_render_absent_offer_stub")
    assert hasattr(admin, "_render_aliases_stub")
    assert hasattr(admin, "_render_health_stub")
