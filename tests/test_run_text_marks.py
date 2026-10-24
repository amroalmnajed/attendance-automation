"""Feature 1 — run_text carries per-line marks into the workflow rows."""
from __future__ import annotations

from attendance import text_workflow
from attendance import row_processor


def _capture(monkeypatch):
    seen = {}
    monkeypatch.setattr(text_workflow, "load_config", lambda *_a, **_k: {"attendance": {}})

    def fake_process_rows(**kw):
        seen.update(kw)
        return kw["summary"]

    monkeypatch.setattr(text_workflow, "process_rows", fake_process_rows)
    return seen


def test_marks_become_status_marks(monkeypatch):
    seen = _capture(monkeypatch)
    text_workflow.run_text(names=["Ahmed", "Sara", "Omar"], marks=["Late", "Present", "Absent"],
                      course="crash", session_num=1)
    assert [r.status_mark for r in seen["ocr_rows"]] == ["Late", "Present", "Absent"]
    assert all(r.confidence == 1.0 for r in seen["ocr_rows"])


def test_unrecognised_mark_is_passed_through_for_the_gate_to_flag(monkeypatch):
    seen = _capture(monkeypatch)
    text_workflow.run_text(names=["Ahmed"], marks=["zzz"], course="crash", session_num=1)
    assert seen["ocr_rows"][0].status_mark == "zzz"


def test_no_marks_means_present_exactly_as_before(monkeypatch):
    seen = _capture(monkeypatch)
    text_workflow.run_text(names=["Ahmed", "Sara"], course="crash", session_num=1)
    assert [r.status_mark for r in seen["ocr_rows"]] == ["Present", "Present"]


def test_short_or_blank_marks_fall_back_to_present(monkeypatch):
    seen = _capture(monkeypatch)
    text_workflow.run_text(names=["Ahmed", "Sara", "Omar"], marks=["Late", None, "  "], course="crash", session_num=1)
    assert [r.status_mark for r in seen["ocr_rows"]] == ["Late", "Present", "Present"]


def test_blank_names_are_skipped_without_misaligning_marks(monkeypatch):
    seen = _capture(monkeypatch)
    text_workflow.run_text(names=["Ahmed", "  ", "Omar"], marks=["Late", "Absent", "Absent"], course="crash", session_num=1)
    assert [(r.raw_name, r.status_mark) for r in seen["ocr_rows"]] == [("Ahmed", "Late"), ("Omar", "Absent")]


def test_upload_id_is_unchanged_for_all_present_lists_and_differs_when_marks_differ(monkeypatch):
    seen = _capture(monkeypatch)
    text_workflow.run_text(names=["Ahmed", "Sara"], course="crash", session_num=1)
    plain = seen["upload_id"]
    text_workflow.run_text(names=["Ahmed", "Sara"], marks=["Present", "Present"], course="crash", session_num=1)
    assert seen["upload_id"] == plain                      # old ids stay stable
    text_workflow.run_text(names=["Ahmed", "Sara"], marks=["Present", "Absent"], course="crash", session_num=1)
    assert seen["upload_id"] != plain                      # different attendance = different upload
