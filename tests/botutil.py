"""Helpers for tests of the split bot package (attendance/bot/*)."""
from __future__ import annotations

import importlib

_MODULES = ("common", "upload", "names", "review", "admin", "app")


def bot_modules():
    return [importlib.import_module(f"attendance.bot.{m}") for m in _MODULES]


def patch_bot(monkeypatch, name: str, value) -> None:
    """Patch ``name`` in every bot module that has it.

    Handlers import shared helpers by name (``from attendance.bot.common import _load_cfg``),
    so one patch must reach each module's own reference.
    """
    hit = False
    for mod in bot_modules():
        if hasattr(mod, name):
            monkeypatch.setattr(mod, name, value)
            hit = True
    assert hit, f"no bot module defines {name}"


def handler(name: str):
    """Find a bot handler/helper by name across the bot modules (defining module wins)."""
    for mod in bot_modules():
        obj = mod.__dict__.get(name)
        if obj is not None and getattr(obj, "__module__", mod.__name__) == mod.__name__:
            return obj
    raise AttributeError(name)
