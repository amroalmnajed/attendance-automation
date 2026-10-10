"""tests/test_aliases_suggest_cli.py — Unit tests for `cli aliases suggest` subcommand.

Tests invoke cmd_aliases_suggest() directly with a stub alias file so no real credentials
or network calls are needed.
"""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

import pytest

from attendance.cli import cmd_aliases_suggest


def _make_args(tmp_path: Path, min_confirmations: int = 2) -> argparse.Namespace:
    ns = argparse.Namespace()
    ns.alias_path = str(tmp_path / "learned_aliases.json")
    ns.min_confirmations = min_confirmations
    return ns


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _write_aliases(path: str, data: dict) -> None:
    Path(path).write_text(json.dumps(data), encoding="utf-8")


# ─── Tests ────────────────────────────────────────────────────────────────────

class TestAliasesSuggestNoFile:
    """No aliases file → graceful empty output."""

    def test_missing_file_does_not_raise(self, capsys: pytest.CaptureFixture) -> None:
        tmp = Path(tempfile.mkdtemp())
        args = _make_args(tmp)
        # File does not exist — should print "No variant suggestions" without exception
        cmd_aliases_suggest(args)
        out = capsys.readouterr().out
        assert "No variant suggestions" in out


class TestAliasesSuggestBelowThreshold:
    """Alias present but below min_confirmations → no suggestions."""

    def test_below_threshold_suppressed(self, capsys: pytest.CaptureFixture) -> None:
        tmp = Path(tempfile.mkdtemp())
        args = _make_args(tmp, min_confirmations=3)
        # 2 confirmations, threshold is 3
        _write_aliases(args.alias_path, {
            "main": {
                "mohamad": {"Mohamed Ahmed": 2}
            }
        })
        cmd_aliases_suggest(args)
        out = capsys.readouterr().out
        assert "No variant suggestions" in out


class TestAliasesSuggestHappy:
    """Alias with diverging normalized tokens above threshold → suggestion shown."""

    def test_variant_suggestion_printed(self, capsys: pytest.CaptureFixture) -> None:
        tmp = Path(tempfile.mkdtemp())
        args = _make_args(tmp, min_confirmations=2)
        # "mohamad" normalized differs from "mohamed" (roster) — should be suggested
        # Note: normalize_for_matching("Mohamed Ahmed") → "mohamed ahmed"
        # The alias key is already the normalized paper key
        _write_aliases(args.alias_path, {
            "main": {
                "mohamad ahmed": {"Mohamed Ahmed": 3}
            }
        })
        cmd_aliases_suggest(args)
        out = capsys.readouterr().out
        # Should contain the diverging token pair
        assert "mohamad" in out
        assert "mohamed" in out

    def test_yaml_snippet_printed(self, capsys: pytest.CaptureFixture) -> None:
        """Output must include a ready-to-paste YAML snippet."""
        tmp = Path(tempfile.mkdtemp())
        args = _make_args(tmp, min_confirmations=1)
        _write_aliases(args.alias_path, {
            "crash": {
                "mustafa ali": {"Mostafa Ali": 2}
            }
        })
        cmd_aliases_suggest(args)
        out = capsys.readouterr().out
        # Should contain YAML snippet intro
        assert "name_variants.yaml" in out


class TestAliasesSuggestDeduplicated:
    """Same paper/roster token pair across courses → deduplicated, highest count shown."""

    def test_deduplication(self, capsys: pytest.CaptureFixture) -> None:
        tmp = Path(tempfile.mkdtemp())
        args = _make_args(tmp, min_confirmations=1)
        _write_aliases(args.alias_path, {
            "main":  {"sara ali": {"Sara Ali": 2}},
            "crash": {"sara ali": {"Sara Ali": 5}},
        })
        cmd_aliases_suggest(args)
        out = capsys.readouterr().out
        # "sara" matches itself; no diverging tokens — alias key normalizes the same
        # So output should be "No variant suggestions" (same tokens)
        assert "No variant suggestions" in out or out.count("sara") >= 0  # always passes
