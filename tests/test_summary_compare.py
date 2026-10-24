"""Feature 6 — post-write summary: Present/Late/Absent/Review counts + previous-session comparison."""
from __future__ import annotations

import json

import yaml

from attendance import history, row_processor, text_workflow, writer
from tests.test_absent import FakeWS, cfg, fake_session  # noqa: F401  (cfg is a fixture)

NAMES = ["Ahmed Hassan Ali", "Sara Mohamed Nour", "Omar Khaled Fathy"]


def _run(monkeypatch, cfg, tmp_path, marks, dry_run=False, session=3):
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    ws = FakeWS([""] * 6)
    monkeypatch.setattr(row_processor, "resolve", lambda **_k: fake_session(ws, course="crash"))
    monkeypatch.setattr(writer, "audit_write", lambda **_k: None)
    return text_workflow.run_text(names=NAMES, marks=marks, course="crash", session_num=session, dry_run=dry_run,
                             config_path=str(cfg_path), review_store_dir=str(tmp_path / "rq"))


def _seed(tmp_path, session, present, late=0, absent=0):
    history.record(str(tmp_path / "rq"), "crash", session, f"up{session}", 
                   {"present": present, "late": late, "absent": absent}, 0)


def test_counts_present_late_absent(monkeypatch, cfg, tmp_path):
    s = _run(monkeypatch, cfg, tmp_path, ["Present", "Late", "Absent"])
    assert (s.present_count, s.late_count, s.absent_count) == (1, 1, 1)
    assert "Present 1 · Late 1 · Absent 1 · Review 0" in s.text()
    assert s.prev_counts is None and "Previous" not in s.text()      # first session: nothing to compare


def test_previous_session_is_shown_and_a_big_drop_is_flagged(monkeypatch, cfg, tmp_path):
    _seed(tmp_path, 2, present=20, late=2)
    s = _run(monkeypatch, cfg, tmp_path, ["Present", "Present", "Late"])
    assert s.prev_session == "2" and s.prev_counts["present"] == 20
    assert "Previous (S2)" in s.text() and "down" in s.drop_warning and "22 → 3" in s.drop_warning


def test_small_change_or_tiny_previous_session_is_not_flagged(monkeypatch, cfg, tmp_path):
    _seed(tmp_path, 2, present=4)                      # below min_previous (5): too small to compare
    assert _run(monkeypatch, cfg, tmp_path, ["Present"] * 3).drop_warning == ""
    _seed(tmp_path, 1, present=4)
    cfg["attendance"]["summary_compare"]["min_previous"] = 1
    cfg["attendance"]["summary_compare"]["drop_threshold_pct"] = 90   # threshold is configurable
    assert _run(monkeypatch, cfg, tmp_path, ["Present", "Absent", "Absent"], session=9).drop_warning == ""


def test_dry_run_records_nothing_and_real_run_records_once_per_session(monkeypatch, cfg, tmp_path):
    _run(monkeypatch, cfg, tmp_path, ["Present"] * 3, dry_run=True)
    assert not (tmp_path / "rq" / "_history").exists()
    _run(monkeypatch, cfg, tmp_path, ["Present"] * 3)
    _run(monkeypatch, cfg, tmp_path, ["Present", "Late", "Late"])     # same session again replaces, not duplicates
    entries = json.loads((tmp_path / "rq" / "_history" / "crash.json").read_text(encoding="utf-8"))
    assert len(entries) == 1 and entries[0]["counts"]["late"] == 2


def test_history_and_summary_never_hold_student_names(monkeypatch, cfg, tmp_path):
    s = _run(monkeypatch, cfg, tmp_path, ["Present"] * 3)
    blob = (tmp_path / "rq" / "_history" / "crash.json").read_text(encoding="utf-8") + json.dumps(s.__dict__)
    assert "Ahmed" not in blob and "Sara" not in blob


def test_can_be_switched_off(monkeypatch, cfg, tmp_path):
    cfg["attendance"]["summary_compare"]["enabled"] = False
    s = _run(monkeypatch, cfg, tmp_path, ["Present"] * 3)
    assert s.present_count == 0 and "This session" not in s.text()
    assert not (tmp_path / "rq" / "_history").exists()
