"""Feature 1 — CLI: process-text / attend wizard parse marks and show a preview."""
from __future__ import annotations

import pytest

from attendance import cli


def test_preview_is_printed_and_marks_returned(capsys):
    names, marks = cli._parse_marks_with_preview(["Ahmed Ali | L", "Sara - absent", "Omar"],
                                                 always_confirm=False, assume_yes=True)
    out = capsys.readouterr().out
    assert names == ["Ahmed Ali", "Sara", "Omar"] and marks == ["Late", "Absent", "Present"]
    assert "Ahmed Ali → Late" in out and "Omar → Present (no mark → Present)" in out


def test_non_interactive_runs_never_block_on_a_prompt(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("must not prompt without a TTY"))
    names, marks = cli._parse_marks_with_preview(["Omar | zzz"], always_confirm=True, assume_yes=False)
    assert marks == ["zzz"]            # passed through so the workflow sends it to review


def test_declining_the_confirmation_stops_without_processing(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    with pytest.raises(SystemExit) as e:
        cli._parse_marks_with_preview(["Omar | zzz"], always_confirm=False, assume_yes=False)
    assert e.value.code == 0 and "Nothing was processed" in capsys.readouterr().out


def test_clean_list_does_not_prompt_unless_asked(monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("clean list needs no confirmation"))
    cli._parse_marks_with_preview(["Ahmed", "Sara | A"], always_confirm=False, assume_yes=False)


def test_wizard_always_confirms(monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
    asked = []
    monkeypatch.setattr("builtins.input", lambda p="": asked.append(p) or "y")
    cli._parse_marks_with_preview(["Ahmed"], always_confirm=True, assume_yes=False)
    assert asked and "look right" in asked[0]
