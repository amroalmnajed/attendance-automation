"""Feature 9 — second-pass OCR only on low-confidence rows (config option; 'full' stays the default)."""
from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest

from attendance import pipeline
from attendance.ocr import OcrRow


def row(i, name, conf=0.95, matched="M"):
    return OcrRow(raw_name=name, status_mark="Present", confidence=conf, row_index=i, matched_roster_name=matched)


class FakeProvider:
    def __init__(self, *passes):
        self.passes, self.calls = list(passes), 0

    def read(self, image_bytes, roster=None):
        out = self.passes[min(self.calls, len(self.passes) - 1)]
        self.calls += 1
        if isinstance(out, Exception):
            raise out
        return out


def read(provider, ocr_cfg, roster=("M",)):
    cfg = {"min_confidence": 0.75, **ocr_cfg}
    return pipeline._read_sheet(image_bytes=b"x", provider=provider, roster_names=list(roster) if roster else None,
                                ocr_cfg=cfg, min_conf=0.75, upload_id="up1")


LOW = {"passes": 2, "second_pass": {"mode": "low_confidence"}}


def test_default_is_unchanged_two_full_passes():
    p = FakeProvider([row(1, "Ahmed"), row(2, "Sara")])
    read(p, {"passes": 2})                                  # no second_pass key at all
    assert p.calls == 2
    p = FakeProvider([row(1, "Ahmed")])
    read(p, {"passes": 2, "second_pass": {"mode": "typo"}})  # unknown value falls back to full
    assert p.calls == 2


def test_single_pass_setting_always_wins():
    p = FakeProvider([row(1, "Ahmed", conf=0.1)])
    read(p, {"passes": 1, "second_pass": {"mode": "low_confidence"}})
    assert p.calls == 1


def test_clean_sheet_costs_one_call():
    p = FakeProvider([row(1, "Ahmed"), row(2, "Sara")])
    assert len(read(p, LOW)) == 2 and p.calls == 1


def test_only_the_low_confidence_row_is_merged():
    first = [row(1, "Ahmed"), row(2, "Sarra", conf=0.5), row(3, "Omar")]
    second = [row(1, "CHANGED"), row(2, "Sara", conf=0.9), row(3, "CHANGED")]   # pass 2 disagrees everywhere
    p = FakeProvider(first, second)
    out = read(p, LOW)
    assert p.calls == 2
    assert out[0] is first[0] and out[2] is first[2]          # confident rows untouched, even though pass 2 differs
    assert out[1].raw_name == "Sarra" and out[1].confidence < 0.5 and out[1].matched_roster_name is None  # disagreement downgrades


def test_agreement_on_the_low_row_averages_confidence():
    p = FakeProvider([row(1, "Sara", conf=0.5)], [row(1, "Sara", conf=0.9)])
    assert abs(read(p, LOW)[0].confidence - 0.7) < 1e-9


def test_unmatched_rows_are_rechecked_only_when_roster_aware():
    p = FakeProvider([row(1, "Ahmed", matched=None)])
    read(p, LOW, roster=("M",))
    assert p.calls == 2
    p = FakeProvider([row(1, "Ahmed", matched=None)])
    read(p, LOW, roster=None)
    assert p.calls == 1
    p = FakeProvider([row(1, "Ahmed", matched=None)])
    read(p, {"passes": 2, "second_pass": {"mode": "low_confidence", "include_unmatched": False}})
    assert p.calls == 1


def test_different_model_or_provider_for_the_extra_read(monkeypatch):
    main = FakeProvider([row(1, "Sara", conf=0.4)])
    other = FakeProvider([row(1, "Sara", conf=0.9)])
    built = []
    monkeypatch.setattr(pipeline, "build_provider", lambda c: built.append(c) or other)
    out = read(main, {"passes": 2, "model": "m1", "second_pass": {"mode": "low_confidence", "model": "m2", "provider": "mock"}})
    assert (main.calls, other.calls) == (1, 1)
    assert built[0]["model"] == "m2" and built[0]["provider"] == "mock"
    assert abs(out[0].confidence - 0.65) < 1e-9


def test_failed_second_pass_keeps_pass_one_but_failed_first_pass_raises():
    first = [row(1, "Sara", conf=0.4)]
    p = FakeProvider(first, RuntimeError("429"))
    assert read(p, LOW) == first
    with pytest.raises(RuntimeError):
        read(FakeProvider(RuntimeError("down")), LOW)


def test_mock_rows_from_other_tests_do_not_break_row_selection():
    assert pipeline._needs_second_look(MagicMock(), 0.75, True, False) is False


def test_run_and_run_multi_share_the_one_helper():
    assert "_read_sheet(" in inspect.getsource(pipeline.run)
    assert "_read_sheet(" in inspect.getsource(pipeline.run_multi)
    assert "provider.read(" not in inspect.getsource(pipeline.run)
