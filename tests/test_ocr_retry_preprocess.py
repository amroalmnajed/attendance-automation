"""Tests for T8: OCR retry with alternative preprocessing on low confidence."""
from __future__ import annotations

import pytest

from attendance.ocr import OcrRow
from attendance import image_workflow


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _row(name="Alice", conf=0.9, idx=1):
    return OcrRow(raw_name=name, status_mark="Present", confidence=conf, row_index=idx)


def _make_provider(calls_and_rows: list[list[OcrRow]]):
    """Provider that returns successive lists of OcrRow on each .read() call."""
    class _Provider:
        def __init__(self):
            self._seq = iter(calls_and_rows)
            self.calls = []

        def read(self, image_bytes, roster=None):
            self.calls.append(image_bytes)
            return next(self._seq)

    return _Provider()


# ─── _avg_confidence ──────────────────────────────────────────────────────────

def test_avg_confidence_empty():
    assert image_workflow._avg_confidence([]) == 0.0


def test_avg_confidence_single():
    assert abs(image_workflow._avg_confidence([_row(conf=0.8)]) - 0.8) < 1e-9


def test_avg_confidence_multiple():
    rows = [_row(conf=0.6), _row(conf=0.4)]
    assert abs(image_workflow._avg_confidence(rows) - 0.5) < 1e-9


# ─── retry_preprocessing NOT triggered (disabled) ─────────────────────────────

def test_retry_preprocess_disabled_no_extra_call(monkeypatch):
    """When retry_preprocessing=False, provider is called for passes only, NOT for alt-preprocess."""
    low_conf_rows = [_row(conf=0.3)]
    provider = _make_provider([low_conf_rows])  # one call for pass 1

    monkeypatch.setattr(image_workflow, "prepare", lambda b, **kw: b)

    ocr_cfg = {"passes": 1, "retry_preprocessing": False, "min_confidence": 0.75}
    rows = image_workflow._read_sheet_inner(
        image_bytes=b"img",
        provider=provider,
        roster_names=None,
        ocr_cfg=ocr_cfg,
        min_conf=0.75,
        upload_id="u1",
        raw_bytes=b"raw",
        preprocess_cfg={},
    )
    assert len(provider.calls) == 1
    assert rows == low_conf_rows


# ─── retry_preprocessing NOT triggered (confidence already OK) ────────────────

def test_retry_preprocess_skipped_when_conf_ok(monkeypatch):
    """retry_preprocessing=True but avg conf >= min_conf → no extra OCR call."""
    high_conf_rows = [_row(conf=0.9)]
    provider = _make_provider([high_conf_rows])

    monkeypatch.setattr(image_workflow, "prepare", lambda b, **kw: b)

    ocr_cfg = {"passes": 1, "retry_preprocessing": True, "min_confidence": 0.75}
    rows = image_workflow._read_sheet_inner(
        image_bytes=b"img",
        provider=provider,
        roster_names=None,
        ocr_cfg=ocr_cfg,
        min_conf=0.75,
        upload_id="u2",
        raw_bytes=b"raw",
        preprocess_cfg={},
    )
    assert len(provider.calls) == 1
    assert rows == high_conf_rows


# ─── retry_preprocessing triggered, alt result is BETTER ─────────────────────

def test_retry_preprocess_accepted_when_alt_higher(monkeypatch):
    """When retry fires and alt avg > orig avg, alt rows are returned."""
    low_rows = [_row(conf=0.3, name="A")]
    high_rows = [_row(conf=0.9, name="B")]
    provider = _make_provider([low_rows, high_rows])

    prepare_calls = []

    def fake_prepare(b, **kw):
        prepare_calls.append((b, kw))
        return b"alt_img"

    monkeypatch.setattr(image_workflow, "prepare", fake_prepare)

    ocr_cfg = {"passes": 1, "retry_preprocessing": True, "min_confidence": 0.75}
    rows = image_workflow._read_sheet_inner(
        image_bytes=b"orig_img",
        provider=provider,
        roster_names=None,
        ocr_cfg=ocr_cfg,
        min_conf=0.75,
        upload_id="u3",
        raw_bytes=b"raw",
        preprocess_cfg={"orient": True, "deskew": True, "max_side_px": 2000, "strip_exif": True},
    )
    # prepare should have been called with clahe + adaptive_thresh
    assert len(prepare_calls) == 1
    _, kw = prepare_calls[0]
    assert kw.get("contrast") == "clahe"
    assert kw.get("adaptive_thresh") is True
    # Alt rows (higher conf) should win
    assert rows == high_rows
    # Provider called twice: pass 1 + alt retry
    assert len(provider.calls) == 2
    assert provider.calls[1] == b"alt_img"


# ─── retry_preprocessing triggered, alt result is WORSE ──────────────────────

def test_retry_preprocess_rejected_when_alt_lower(monkeypatch):
    """When alt avg <= orig avg, original rows are kept."""
    orig_rows = [_row(conf=0.4, name="A")]
    alt_rows = [_row(conf=0.2, name="B")]
    provider = _make_provider([orig_rows, alt_rows])

    monkeypatch.setattr(image_workflow, "prepare", lambda b, **kw: b"alt_img")

    ocr_cfg = {"passes": 1, "retry_preprocessing": True, "min_confidence": 0.75}
    rows = image_workflow._read_sheet_inner(
        image_bytes=b"orig_img",
        provider=provider,
        roster_names=None,
        ocr_cfg=ocr_cfg,
        min_conf=0.75,
        upload_id="u4",
        raw_bytes=b"raw",
        preprocess_cfg={},
    )
    # Original rows retained (lower alt)
    assert rows == orig_rows


# ─── retry_preprocessing: no raw_bytes → skip silently ───────────────────────

def test_retry_preprocess_skipped_without_raw_bytes(monkeypatch):
    """Without raw_bytes, retry is silently skipped even if config says true."""
    low_rows = [_row(conf=0.3)]
    provider = _make_provider([low_rows])

    monkeypatch.setattr(image_workflow, "prepare", lambda b, **kw: b)

    ocr_cfg = {"passes": 1, "retry_preprocessing": True, "min_confidence": 0.75}
    rows = image_workflow._read_sheet_inner(
        image_bytes=b"img",
        provider=provider,
        roster_names=None,
        ocr_cfg=ocr_cfg,
        min_conf=0.75,
        upload_id="u5",
        raw_bytes=None,       # key: no raw bytes
        preprocess_cfg={},
    )
    assert len(provider.calls) == 1
    assert rows == low_rows


# ─── retry_preprocessing: provider failure is swallowed ──────────────────────

def test_retry_preprocess_provider_error_falls_back(monkeypatch):
    """If alt OCR call raises, original rows are returned (error is swallowed)."""
    low_rows = [_row(conf=0.3, name="A")]

    class _FailingProvider:
        calls = 0

        def read(self, image_bytes, roster=None):
            self.calls += 1
            if self.calls == 1:
                return low_rows
            raise RuntimeError("quota exhausted")

    provider = _FailingProvider()
    monkeypatch.setattr(image_workflow, "prepare", lambda b, **kw: b"alt_img")

    ocr_cfg = {"passes": 1, "retry_preprocessing": True, "min_confidence": 0.75}
    rows = image_workflow._read_sheet_inner(
        image_bytes=b"orig_img",
        provider=provider,
        roster_names=None,
        ocr_cfg=ocr_cfg,
        min_conf=0.75,
        upload_id="u6",
        raw_bytes=b"raw",
        preprocess_cfg={},
    )
    assert rows == low_rows   # fallback to original
