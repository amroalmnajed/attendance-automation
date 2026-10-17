"""T14 — OcrProvider protocol + TesseractProvider + Gemini-to-Tesseract fallback.

Mocks pytesseract entirely so no Tesseract binary is needed to run the tests.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from attendance.ocr import (
    GeminiProvider,
    MockProvider,
    OcrProvider,
    OcrRow,
    OcrUnavailableError,
    build_provider,
)


# ─── 1. Protocol conformance ──────────────────────────────────────────────────

def test_gemini_provider_satisfies_protocol():
    """GeminiProvider must satisfy the OcrProvider runtime-checkable Protocol."""
    assert isinstance(GeminiProvider(), OcrProvider)


def test_mock_provider_satisfies_protocol():
    """MockProvider must satisfy the OcrProvider runtime-checkable Protocol."""
    assert isinstance(MockProvider(), OcrProvider)


def test_tesseract_provider_satisfies_protocol():
    """TesseractProvider must satisfy the OcrProvider runtime-checkable Protocol."""
    from attendance.ocr_tesseract import TesseractProvider
    assert isinstance(TesseractProvider(), OcrProvider)


def test_plain_duck_satisfies_protocol():
    """Any class with a matching read() signature satisfies the protocol structurally."""
    class MyProvider:
        def read(self, image_bytes, roster=None):
            return []

    assert isinstance(MyProvider(), OcrProvider)


# ─── 2. build_provider factory ───────────────────────────────────────────────

def test_build_provider_gemini():
    p = build_provider({"provider": "gemini"})
    assert isinstance(p, GeminiProvider)


def test_build_provider_mock():
    p = build_provider({"provider": "mock"})
    assert isinstance(p, MockProvider)


def test_build_provider_tesseract():
    from attendance.ocr_tesseract import TesseractProvider
    p = build_provider({"provider": "tesseract"})
    assert isinstance(p, TesseractProvider)


def test_build_provider_tesseract_lang():
    from attendance.ocr_tesseract import TesseractProvider
    p = build_provider({"provider": "tesseract", "tesseract_lang": "ara"})
    assert isinstance(p, TesseractProvider)
    assert p.lang == "ara"


def test_build_provider_unknown_raises():
    with pytest.raises(ValueError, match="Unknown OCR provider"):
        build_provider({"provider": "bogus"})


# ─── 3. TesseractProvider.read() ─────────────────────────────────────────────

def _make_pytesseract_stub(words: list[str], confs: list[int], *, img_w=100, img_h=200):
    """Build a minimal pytesseract.image_to_data return dict for given words."""
    n = len(words)
    data = {
        "text": words,
        "conf": confs,
        "page_num": [1] * n,
        "block_num": [1] * n,
        "par_num":   [1] * n,
        "line_num":  list(range(1, n + 1)),   # one word per line for simplicity
        "left":   [10] * n,
        "top":    [i * 20 for i in range(n)],
        "width":  [80] * n,
        "height": [15] * n,
    }
    return data


def _mock_pytesseract(monkeypatch, words, confs, img_w=100, img_h=200):
    """Patch pytesseract in ocr_tesseract's namespace."""
    data = _make_pytesseract_stub(words, confs, img_w=img_w, img_h=img_h)

    fake_pytesseract = MagicMock()
    fake_pytesseract.Output.DICT = "DICT"
    fake_pytesseract.image_to_data.return_value = data
    fake_pytesseract.pytesseract.TesseractNotFoundError = FileNotFoundError

    # Stub PIL.Image.open → returns an image of known size
    fake_pil_img = MagicMock()
    fake_pil_img.size = (img_w, img_h)
    fake_pil_img.convert.return_value = fake_pil_img

    fake_pil = SimpleNamespace(Image=SimpleNamespace(open=MagicMock(return_value=fake_pil_img)))

    monkeypatch.setitem(sys.modules, "pytesseract", fake_pytesseract)
    monkeypatch.setitem(sys.modules, "PIL", fake_pil)
    monkeypatch.setitem(sys.modules, "PIL.Image", SimpleNamespace(open=fake_pil.Image.open))

    return fake_pytesseract, fake_pil_img


def test_tesseract_read_returns_ocr_rows(monkeypatch):
    """TesseractProvider.read should return one OcrRow per non-empty line."""
    from attendance.ocr_tesseract import TesseractProvider

    words = ["Ahmed", "Mohamed", "Sara"]
    confs = [90, 80, 85]
    fake_pt, _ = _mock_pytesseract(monkeypatch, words, confs)

    p = TesseractProvider()
    rows = p.read(b"\xff\xd8\xff")   # fake JPEG bytes

    assert len(rows) == 3
    assert all(isinstance(r, OcrRow) for r in rows)
    assert rows[0].raw_name == "Ahmed"
    assert rows[0].status_mark is None          # Tesseract never sets status_mark
    assert 0.0 <= rows[0].confidence <= 1.0


def test_tesseract_read_confidence_normalised(monkeypatch):
    """Tesseract 0-100 conf must be divided to 0.0-1.0."""
    from attendance.ocr_tesseract import TesseractProvider

    fake_pt, _ = _mock_pytesseract(monkeypatch, ["Alice"], [80])
    rows = TesseractProvider().read(b"\xff\xd8\xff")
    assert abs(rows[0].confidence - 0.80) < 1e-9


def test_tesseract_read_bounding_box_normalised(monkeypatch):
    """Bounding box must be normalised to [0, 1] relative to image dimensions."""
    from attendance.ocr_tesseract import TesseractProvider

    fake_pt, _ = _mock_pytesseract(monkeypatch, ["Bob"], [70], img_w=200, img_h=400)
    rows = TesseractProvider().read(b"\xff\xd8\xff")
    bbox = rows[0].bounding_box
    assert bbox is not None
    assert len(bbox) == 4
    assert all(0.0 <= v <= 1.0 for v in bbox), f"bbox out of range: {bbox}"


def test_tesseract_empty_words_skipped(monkeypatch):
    """Entries with empty/whitespace text must be silently skipped."""
    from attendance.ocr_tesseract import TesseractProvider

    fake_pt, _ = _mock_pytesseract(monkeypatch, ["", "  ", "Ziad"], [90, 90, 85])
    rows = TesseractProvider().read(b"\xff\xd8\xff")
    # Only "Ziad" should appear (the first two words are blank)
    assert len(rows) == 1
    assert rows[0].raw_name == "Ziad"


def test_tesseract_not_found_raises_unavailable(monkeypatch):
    """Missing Tesseract binary must raise OcrUnavailableError (not TesseractNotFoundError)."""
    from attendance.ocr_tesseract import TesseractProvider

    fake_pt = MagicMock()
    fake_pt.Output.DICT = "DICT"
    TesseractNotFoundError = type("TesseractNotFoundError", (FileNotFoundError,), {})
    fake_pt.pytesseract.TesseractNotFoundError = TesseractNotFoundError
    fake_pt.image_to_data.side_effect = TesseractNotFoundError("tesseract not found")

    fake_pil_img = MagicMock()
    fake_pil_img.size = (100, 200)
    fake_pil_img.convert.return_value = fake_pil_img
    fake_pil = SimpleNamespace(Image=SimpleNamespace(open=MagicMock(return_value=fake_pil_img)))

    monkeypatch.setitem(sys.modules, "pytesseract", fake_pt)
    monkeypatch.setitem(sys.modules, "PIL", fake_pil)
    monkeypatch.setitem(sys.modules, "PIL.Image", SimpleNamespace(open=fake_pil.Image.open))

    with pytest.raises(OcrUnavailableError, match="Tesseract binary not found"):
        TesseractProvider().read(b"\xff\xd8\xff")


# ─── 4. Gemini → Tesseract pipeline fallback ─────────────────────────────────

def test_pipeline_fallback_to_tesseract_when_gemini_raises(monkeypatch):
    """When Gemini raises OcrUnavailableError and fallback_to_tesseract=True,
    _read_sheet should retry with TesseractProvider and return its rows."""
    from attendance import pipeline
    from attendance.ocr_tesseract import TesseractProvider

    fallback_rows = [OcrRow(raw_name="Fallback", status_mark=None, confidence=0.6, row_index=1)]

    class _GeminiFailProvider:
        def read(self, image_bytes, roster=None):
            raise OcrUnavailableError("Gemini down")

    class _TesseractOkProvider:
        def read(self, image_bytes, roster=None):
            return fallback_rows

    # Patch TesseractProvider constructor in pipeline's import scope
    monkeypatch.setattr(
        "attendance.ocr_tesseract.TesseractProvider",
        lambda **kw: _TesseractOkProvider(),
    )

    ocr_cfg = {
        "passes": 1,
        "retry_preprocessing": False,
        "min_confidence": 0.75,
        "fallback_to_tesseract": True,
    }

    rows = pipeline._read_sheet(
        image_bytes=b"img",
        provider=_GeminiFailProvider(),
        roster_names=None,
        ocr_cfg=ocr_cfg,
        min_conf=0.75,
        upload_id="u_fallback",
        raw_bytes=b"raw",
        preprocess_cfg={},
    )
    assert rows == fallback_rows


def test_pipeline_no_fallback_when_flag_is_false(monkeypatch):
    """When fallback_to_tesseract=False, OcrUnavailableError must propagate."""
    from attendance import pipeline

    class _GeminiFailProvider:
        def read(self, image_bytes, roster=None):
            raise OcrUnavailableError("Gemini down")

    ocr_cfg = {
        "passes": 1,
        "retry_preprocessing": False,
        "min_confidence": 0.75,
        "fallback_to_tesseract": False,
    }

    with pytest.raises(OcrUnavailableError):
        pipeline._read_sheet(
            image_bytes=b"img",
            provider=_GeminiFailProvider(),
            roster_names=None,
            ocr_cfg=ocr_cfg,
            min_conf=0.75,
            upload_id="u_no_fallback",
            raw_bytes=b"raw",
            preprocess_cfg={},
        )
