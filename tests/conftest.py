"""Shared pytest safety net.

Some tests (e.g. ``review resolve``) run against the real ``config/attendance.yaml``, whose
``aliases_file`` is the REAL ``config/learned_aliases.json``. Without this, every pytest run
teaches the production alias store a fake "Test Name -> Test Student" alias.

This fixture snapshots that file before each test and restores it afterwards, so running the
suite can never change real alias data.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

# Every real data file the app may write next to the config. Snapshot/restore (or delete if it did not
# exist before) so a test run can never change the owner's real alias dates or variant suggestions.
_REAL_FILES = [
    Path("config/learned_aliases.json"),
    Path("config/learned_aliases.meta.json"),      # Feature 7: alias dates (sidecar)
    Path("config/variant_suggestions.json"),       # Feature 15: pending name-variant suggestions
]


@pytest.fixture(autouse=True)
def _protect_real_alias_store():
    snaps = {p: (p.read_bytes() if p.exists() else None) for p in _REAL_FILES}
    try:
        yield
    finally:
        for p, snapshot in snaps.items():
            if snapshot is not None:
                if not p.exists() or p.read_bytes() != snapshot:
                    p.write_bytes(snapshot)
            elif p.exists():
                p.unlink()


@pytest.fixture(autouse=True)
def _clean_role_env():
    """Role allow-lists come from env; a developer's real .env must not change test outcomes."""
    names = ("ADMIN_TELEGRAM_IDS", "TEACHER_TELEGRAM_IDS")
    saved = {n: os.environ.pop(n, None) for n in names}
    try:
        yield
    finally:
        for n, v in saved.items():
            os.environ.pop(n, None)
            if v is not None:
                os.environ[n] = v


@pytest.fixture(autouse=True)
def _reset_health():
    """health.configure() is process-wide state; never let one test's tmp dir leak into the next."""
    from attendance import health
    health.reset()
    try:
        yield
    finally:
        health.reset()
