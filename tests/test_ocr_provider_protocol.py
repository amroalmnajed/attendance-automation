"""T14 — OcrProvider protocol + FreeAIProvider + FreeAI-to-Gemini fallback.

Mocks the OpenAI SDK entirely so no free.ai API key is needed to run the tests.
"""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from attendance.ocr import (
    FreeAIProvider,
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


def test_freeai_provider_satisfies_protocol():
    """FreeAIProvider must satisfy the OcrProvider runtime-checkable Protocol."""
    assert isinstance(FreeAIProvider(), OcrProvider)


def test_plain_duck_satisfies_protocol():
    """Any class with a matching read() signature satisfies the protocol structurally."""
    class MyProvider:
        def read(self, image_bytes, roster=None):
            return []

    assert isinstance(MyProvider(), OcrProvider)


# ─── 2. build_provider factory ───────────────────────────────────────────────

def test_build_provider_freeai():
    p = build_provider({"provider": "freeai"})
    assert isinstance(p, FreeAIProvider)


def test_build_provider_freeai_default():
    """Default provider (no key) should be freeai."""
    p = build_provider({})
    assert isinstance(p, FreeAIProvider)


def test_build_provider_gemini():
    p = build_provider({"provider": "gemini"})
    assert isinstance(p, GeminiProvider)


def test_build_provider_mock():
    p = build_provider({"provider": "mock"})
    assert isinstance(p, MockProvider)


def test_build_provider_unknown_raises():
    with pytest.raises(ValueError, match="Unknown OCR provider"):
        build_provider({"provider": "bogus"})


# ─── 3. FreeAIProvider ──────────────────────────────────────────────────────

def _make_openai_response(content: str):
    """Build a minimal OpenAI-compatible chat completion response."""
    msg = SimpleNamespace(content=content, role="assistant")
    choice = SimpleNamespace(message=msg, index=0, finish_reason="stop")
    return SimpleNamespace(choices=[choice], usage=SimpleNamespace(total_tokens=100))


def test_freeai_read_returns_ocr_rows(monkeypatch):
    """FreeAIProvider.read should return OcrRow objects from the API response."""
    monkeypatch.setenv("FREEAI_API_KEY", "test-key-123")

    response_json = json.dumps([
        {"raw_name": "Ahmed Hassan", "status_mark": "P", "confidence": 0.95, "row_index": 1,
         "bounding_box": [0.1, 0.05, 0.15, 0.95]},
        {"raw_name": "Sara Mohamed", "status_mark": None, "confidence": 0.90, "row_index": 2,
         "bounding_box": [0.16, 0.05, 0.19, 0.95]},
    ])

    fake_response = _make_openai_response(response_json)
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = fake_response

    fake_openai_mod = MagicMock()
    fake_openai_mod.OpenAI.return_value = fake_client
    monkeypatch.setitem(sys.modules, "openai", fake_openai_mod)

    p = FreeAIProvider()
    p._client = None  # force re-init
    rows = p.read(b"\xff\xd8\xff")  # fake JPEG bytes

    assert len(rows) == 2
    assert all(isinstance(r, OcrRow) for r in rows)
    assert rows[0].raw_name == "Ahmed Hassan"
    assert rows[0].status_mark == "P"
    assert 0.0 <= rows[0].confidence <= 1.0


def test_freeai_token_limit_raises_unavailable(monkeypatch):
    """When free.ai returns 429, FreeAIProvider must raise OcrUnavailableError."""
    monkeypatch.setenv("FREEAI_API_KEY", "test-key-123")

    # Create an exception that looks like a 429
    exc_429 = Exception("Error code: 429 - rate limit exceeded, token quota exhausted")
    exc_429.status_code = 429

    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = exc_429

    fake_openai_mod = MagicMock()
    fake_openai_mod.OpenAI.return_value = fake_client
    monkeypatch.setitem(sys.modules, "openai", fake_openai_mod)

    p = FreeAIProvider()
    p._client = None
    with pytest.raises(OcrUnavailableError, match="token limit"):
        p.read(b"\xff\xd8\xff")


def test_freeai_empty_response_raises_unavailable(monkeypatch):
    """Empty response from free.ai must raise OcrUnavailableError."""
    monkeypatch.setenv("FREEAI_API_KEY", "test-key-123")

    fake_response = _make_openai_response("")
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = fake_response

    fake_openai_mod = MagicMock()
    fake_openai_mod.OpenAI.return_value = fake_client
    monkeypatch.setitem(sys.modules, "openai", fake_openai_mod)

    p = FreeAIProvider()
    p._client = None
    with pytest.raises(OcrUnavailableError, match="empty response"):
        p.read(b"\xff\xd8\xff")


# ─── 4. FreeAI → Gemini pipeline fallback ────────────────────────────────────

def test_pipeline_fallback_to_gemini_when_freeai_raises(monkeypatch):
    """When FreeAI raises OcrUnavailableError and fallback_to_gemini=True,
    _read_sheet should retry with GeminiProvider and return its rows."""
    from attendance import pipeline

    fallback_rows = [OcrRow(raw_name="Fallback", status_mark=None, confidence=0.6, row_index=1)]

    class _FreeAIFailProvider:
        def read(self, image_bytes, roster=None):
            raise OcrUnavailableError("FreeAI token limit")

    class _GeminiOkProvider:
        def read(self, image_bytes, roster=None):
            return fallback_rows

    # Patch GeminiProvider constructor in ocr module scope
    monkeypatch.setattr(
        "attendance.ocr.GeminiProvider",
        lambda **kw: _GeminiOkProvider(),
    )

    ocr_cfg = {
        "passes": 1,
        "retry_preprocessing": False,
        "min_confidence": 0.75,
        "fallback_to_gemini": True,
    }

    rows = pipeline._read_sheet(
        image_bytes=b"img",
        provider=_FreeAIFailProvider(),
        roster_names=None,
        ocr_cfg=ocr_cfg,
        min_conf=0.75,
        upload_id="u_fallback",
        raw_bytes=b"raw",
        preprocess_cfg={},
    )
    assert rows == fallback_rows


def test_pipeline_no_fallback_when_flag_is_false(monkeypatch):
    """When fallback_to_gemini=False, OcrUnavailableError must propagate."""
    from attendance import pipeline

    class _FreeAIFailProvider:
        def read(self, image_bytes, roster=None):
            raise OcrUnavailableError("FreeAI down")

    ocr_cfg = {
        "passes": 1,
        "retry_preprocessing": False,
        "min_confidence": 0.75,
        "fallback_to_gemini": False,
    }

    with pytest.raises(OcrUnavailableError):
        pipeline._read_sheet(
            image_bytes=b"img",
            provider=_FreeAIFailProvider(),
            roster_names=None,
            ocr_cfg=ocr_cfg,
            min_conf=0.75,
            upload_id="u_no_fallback",
            raw_bytes=b"raw",
            preprocess_cfg={},
        )
