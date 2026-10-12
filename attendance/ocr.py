"""attendance/ocr.py — OCR provider interface + Gemini Flash implementation.

The OcrProvider ABC lets you swap providers via one config line.
The GeminiProvider sends the preprocessed image to Gemini with a strict
JSON-schema prompt and returns:
  [{raw_name: str, status_mark: str | null, confidence: float}]
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


from attendance import health
from attendance._net import http_status, is_transient, retry_call
from core.logger import get_logger

log = get_logger("ocr")


class OcrUnavailableError(RuntimeError):
    """Gemini could not produce a usable response (quota, outage, timeout, empty reply)."""


# ─── Data model ───────────────────────────────────────────────────────────────

@dataclass
class OcrRow:
    raw_name: str            # As seen on paper (English)
    status_mark: Optional[str]  # Raw mark string or None if blank/unreadable
    confidence: float        # Provider-reported confidence [0, 1]
    row_index: int           # 1-based row index on the paper sheet
    bounding_box: Optional[list[float]] = None # [ymin, xmin, ymax, xmax] normalized 0.0-1.0
    matched_roster_name: Optional[str] = None  # Model's own best guess at the roster match,
                                                # when the roster was given to it (see `roster=`).
                                                # None when roster-aware mode is off/unavailable.
    source_image: Optional[str] = None          # Which photo this row came from (multi-photo uploads).
    photo_index: Optional[int] = None           # 1-based photo number within the upload.
    conflict_marks: Optional[list[str]] = None  # Set when the same name had different marks on 2+ photos.


# ─── Abstract interface ───────────────────────────────────────────────────────

class OcrProvider(ABC):
    @abstractmethod
    def read(self, image_bytes: bytes, roster: Optional[list[str]] = None) -> list[OcrRow]:
        """Read the attendance sheet image and return structured rows.

        `roster`, when given, is the full list of canonical student names for
        this course/session. Passing it lets the model match handwriting to a
        specific real name instead of guessing a transliteration in isolation —
        this is what actually resolves ambiguous/partial names; plain string
        similarity on a blind transliteration can't.
        """
        ...


# ─── Gemini provider ─────────────────────────────────────────────────────────

_GEMINI_PROMPT = """\
You are an expert OCR system reading a handwritten attendance sheet photo for an IGCSE tutoring center in Egypt.

The sheet has rows. Each row contains:
- A student name (written in English OR Arabic handwriting)
- An attendance mark: checkmark (✓), "P", "A", "L", "NC", "حاضر", "غياب", a cross (✗/X), or blank

Instructions:
1. Read EVERY row that has a student name.
2. For EACH row return a JSON object with:
   - raw_name: the student name in Latin script (English letters).
     * If the name is already in English, write it exactly as written.
     * If the name is written in Arabic, transliterate it into Latin script using Egyptian transliteration conventions:
       محمد→Mohamed, أحمد→Ahmed, إياد→Eyad, يوسف→Youssef, ياسين→Yassin, يحيى→Yehia,
       حمزة→Hamza, سهر→Sahar, علي→Ali, حسن→Hassan, عمر→Omar, عمرو→Amr,
       عبدالرحمن→Abdelrahman, عبداللطيف→Abdel Latif, عبدالحميد→Abdel Hamid, عبد المنعم→Abdelmoniem,
       محمود→Mahmoud, رامي→Ramy, مليجي→Meligy, جوهر→Gohar, صلاح→Salah, مصطفى→Mostafa, علام→Allam,
       مريم→Mariam, معاذ→Moaz, فريدة→Farida, تاليا→Talia, جنى→Jana, حبيبة→Habiba, هانيا→Hania
   - status_mark: the attendance mark exactly as written ("P", "A", "L", "NC", "✓", "✗"), or null if blank/unreadable.
   - confidence: 0.0–1.0 for how certain you are of both the name and mark reading.
   - row_index: 1-based integer for the student row on the paper sheet.
   - bounding_box: [ymin, xmin, ymax, xmax] normalized coordinates between 0.0 and 1.0 representing the bounding box of the entire row.
3. Skip table headers and empty rows.
4. If a name is unreadable, set confidence below 0.5 and raw_name="[UNREADABLE]".

Output ONLY a valid JSON array. No markdown, no explanation, no code fences.
Example:
[
  {"raw_name": "Ahmed Hassan Ali", "status_mark": "P", "confidence": 0.95, "row_index": 1, "bounding_box": [0.12, 0.05, 0.15, 0.95]},
  {"raw_name": "Sara Mohamed", "status_mark": null, "confidence": 0.90, "row_index": 2, "bounding_box": [0.16, 0.05, 0.19, 0.95]}
]
"""

_ROSTER_MATCH_INSTRUCTIONS = """\

You are ALSO given the real class roster below — the actual full names of every
student who could appear on this sheet. The handwriting is often a first name
only, a nickname, a misspelling, or a partial name, and a simple character
comparison cannot reliably tell two students with similar names apart. You can,
because you can read the roster as a whole and reason about which single real
student each handwritten row most plausibly refers to — the same way a teacher
who knows the class would.

For EACH row, in addition to raw_name/status_mark/confidence/row_index/bounding_box, also return:
   - matched_roster_name: the roster name (copied EXACTLY, character-for-character,
     from the roster list below) that this row most likely refers to, or null if
     you are not reasonably confident it is any specific student on the roster.
   - match_confidence: 0.0–1.0, how sure you are that matched_roster_name is the
     correct real student (independent of how clearly the handwriting itself reads).

Rules for matching:
- Never invent or alter a roster name — matched_roster_name must be copied verbatim from the roster, or null.
- If two or more roster names are plausible and you cannot tell which one from the handwriting, name order, or context, set matched_roster_name to null rather than guessing — a human will resolve it.
- Do not assign the same roster student to two different rows. If two rows seem to refer to the same student, pick the better match for one and set the other to null.
- A short/partial paper name (e.g. one or two words) can still confidently match a longer roster name if only one roster student plausibly fits it.

ROSTER ({roster_count} students):
{roster_block}

Output ONLY a valid JSON array, each object with ALL of: raw_name, status_mark, confidence, row_index, bounding_box, matched_roster_name, match_confidence.
"""


def _build_prompt(roster: Optional[list[str]]) -> str:
    if not roster:
        return _GEMINI_PROMPT
    roster_block = "\n".join(f"{i+1}. {name}" for i, name in enumerate(roster))
    return _GEMINI_PROMPT + _ROSTER_MATCH_INSTRUCTIONS.format(
        roster_count=len(roster), roster_block=roster_block
    )


def _response_schema() -> dict:
    """JSON schema for Gemini structured output: an array of row objects.

    The roster-match fields are always declared (nullable) so one schema serves both the plain
    and the roster-aware prompt. ``status_mark`` is required but may be null (blank cell).
    """
    return {
        "type": "ARRAY",
        "items": {
            "type": "OBJECT",
            "properties": {
                "raw_name": {"type": "STRING"},
                "status_mark": {"type": "STRING", "nullable": True},
                "confidence": {"type": "NUMBER"},
                "row_index": {"type": "INTEGER"},
                "bounding_box": {"type": "ARRAY", "items": {"type": "NUMBER"}, "nullable": True},
                "matched_roster_name": {"type": "STRING", "nullable": True},
                "match_confidence": {"type": "NUMBER", "nullable": True},
            },
            "required": ["raw_name", "status_mark", "confidence", "row_index"],
        },
    }


def _build_config(gtypes, structured: bool = True):
    """GenerateContentConfig for OCR. ``structured`` asks Gemini for schema-constrained JSON."""
    kwargs = {"temperature": 0.0, "max_output_tokens": 8192}   # deterministic output
    if structured:
        kwargs["response_mime_type"] = "application/json"
        kwargs["response_schema"] = _response_schema()
    return gtypes.GenerateContentConfig(**kwargs)


_SCHEMA_WORDS = ("schema", "response_mime_type", "mime type", "responsemimetype")


def _is_schema_rejection(exc: BaseException) -> bool:
    """True for a 400 that blames the response schema / mime type (an SDK or model that can't do it)."""
    msg = str(exc).lower()
    is_400 = http_status(exc) == 400 or "invalid_argument" in msg
    return is_400 and any(w in msg for w in _SCHEMA_WORDS)


class GeminiProvider(OcrProvider):
    """OCR via Google Gemini Flash (multimodal) using google-genai SDK."""

    def __init__(
        self,
        model: str = "gemini-3.1-flash-lite",
        min_confidence: float = 0.75,
        timeout_s: float = 60.0,
        max_attempts: int = 3,
        base_s: float = 2.0,
        cap_s: float = 16.0,
    ):
        self.model_name = model
        self.min_confidence = min_confidence
        self.timeout_s = timeout_s
        self.max_attempts = max_attempts
        self.base_s = base_s
        self.cap_s = cap_s
        self._client = None  # lazy init

    def _get_client(self):
        if self._client is None:
            try:
                from google import genai
                api_key = os.environ.get("GEMINI_API_KEY", "")
                if not api_key:
                    raise ValueError(
                        "GEMINI_API_KEY env var is not set. "
                        "Get a key from https://aistudio.google.com/app/apikey"
                    )
                from google.genai import types as gtypes
                self._client = genai.Client(
                    api_key=api_key,
                    http_options=gtypes.HttpOptions(timeout=int(self.timeout_s * 1000)),
                )
            except ImportError:
                raise ImportError(
                    "google-genai package not installed. Run: pip install google-genai"
                )
        return self._client

    def read(self, image_bytes: bytes, roster: Optional[list[str]] = None) -> list[OcrRow]:
        """Send image to Gemini and parse the structured JSON response."""
        from google.genai import types as gtypes

        client = self._get_client()

        # Encode image as base64 for the API
        image_b64 = base64.b64encode(image_bytes).decode("ascii")

        # Determine MIME type from image bytes magic bytes
        mime_type = _detect_mime(image_bytes)

        log.info(
            "gemini_ocr_request",
            extra={"model": self.model_name, "mime": mime_type, "roster_aware": bool(roster)},
        )

        prompt = _build_prompt(roster)

        # Build the content parts: image + text prompt
        contents = [
            gtypes.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            gtypes.Part.from_text(text=prompt),
        ]

        models_to_try = [self.model_name]
        # Fallback candidates if primary model hits quota (429), high demand (503), or 404
        fallbacks = ["gemini-3.1-flash-lite", "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.5-flash"]
        for fb in fallbacks:
            if fb not in models_to_try:
                models_to_try.append(fb)

        response = None
        last_error: Optional[BaseException] = None
        health_seen: set = set()   # at most one health event per kind per read

        # Structured JSON output (schema) first; ONE fallback to the plain config if building it
        # fails or the API rejects the schema. The fallback sticks for the remaining models/retries.
        structured = {"on": True}

        def _config():
            if structured["on"]:
                try:
                    return _build_config(gtypes, True)
                except Exception as exc:
                    structured["on"] = False
                    log.warning("gemini_schema_unavailable", extra={"error": str(exc)[:120]})
            return _build_config(gtypes, False)

        def _call(model_name: str):
            try:
                return client.models.generate_content(model=model_name, contents=contents, config=_config())
            except Exception as exc:
                if not (structured["on"] and _is_schema_rejection(exc)):
                    raise
                structured["on"] = False
                log.warning("gemini_schema_rejected", extra={"model": model_name, "error": str(exc)[:120]})
                return client.models.generate_content(
                    model=model_name, contents=contents, config=_build_config(gtypes, False))

        for model_candidate in models_to_try:
            try:
                log.info("gemini_generating", extra={"model": model_candidate})
                # Retry timeouts / 5xx / connection errors on the same model.
                # 429 is NOT retried here: quota is per-model, so fall through to the next one.
                response = retry_call(
                    lambda: _call(model_candidate),
                    op="gemini_generate",
                    max_attempts=self.max_attempts,
                    base_s=self.base_s,
                    cap_s=self.cap_s,
                    include_quota=False,
                    context={"model": model_candidate},
                )
            except Exception as exc:
                last_error = exc
                health.record_exc(health_seen, exc)
                if is_transient(exc) or http_status(exc) in (404,):
                    log.warning(
                        "gemini_model_fallback",
                        extra={"failed_model": model_candidate, "status": http_status(exc),
                               "error": str(exc)[:120]},
                    )
                    continue
                log.error("gemini_api_error", extra={"model": model_candidate, "error": str(exc)[:300]})
                raise
            if response is not None and getattr(response, "text", None):
                break
            # Empty / blocked reply (e.g. safety filter): try the next model.
            log.warning("gemini_empty_response", extra={"model": model_candidate})
            response = None

        if response is None or not response.text:
            log.error("gemini_all_models_failed", extra={"last_error": str(last_error)[:300]})
            reason = (
                f"last error: {last_error}" if last_error is not None
                else "every model returned an empty response"
            )
            raise OcrUnavailableError(
                f"Gemini OCR failed on all models ({', '.join(models_to_try)}) — "
                f"quota exhausted or service unavailable; {reason}"
            ) from last_error

        raw_text = response.text.strip()
        log.debug("gemini_raw_response", extra={"chars": len(raw_text)})

        return _parse_gemini_response(raw_text)


def _detect_mime(image_bytes: bytes) -> str:
    """Detect MIME type from magic bytes."""
    if image_bytes[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if image_bytes[:4] in (b"RIFF", b"WEBP"):
        return "image/webp"
    return "image/jpeg"  # safe default


def _parse_gemini_response(raw: str) -> list[OcrRow]:
    """Parse the JSON array from Gemini's response text."""
    # Strip markdown fences if present (model sometimes ignores the instruction)
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
    cleaned = re.sub(r"\s*```$", "", cleaned, flags=re.MULTILINE).strip()

    try:
        items = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        log.error("gemini_json_parse_error", extra={"error": str(exc), "raw": raw[:500]})
        # Return a single unreadable row rather than crashing the pipeline
        return [OcrRow(raw_name="[UNREADABLE]", status_mark=None, confidence=0.0, row_index=1)]

    if not isinstance(items, list):
        log.error("gemini_json_not_list", extra={"type": type(items).__name__, "raw": raw[:500]})
        return [OcrRow(raw_name="[UNREADABLE]", status_mark=None, confidence=0.0, row_index=1)]

    rows: list[OcrRow] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        raw_name = str(item.get("raw_name", "")).strip()
        if not raw_name:
            continue
        mark = item.get("status_mark")
        if mark is not None:
            mark = str(mark).strip() or None
        try:
            conf = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.0  # unparseable confidence → force human review
        try:
            row_idx = int(item.get("row_index", i + 1))
        except (TypeError, ValueError):
            row_idx = i + 1
        bounding_box = item.get("bounding_box")

        matched_roster_name = item.get("matched_roster_name")
        if matched_roster_name is not None:
            matched_roster_name = str(matched_roster_name).strip() or None
        if matched_roster_name is not None:
            # match_confidence (how sure it is of the student) overrides the plain
            # OCR confidence (how sure it is of the handwriting) once a roster match
            # is offered — that's the number the matcher actually cares about.
            match_conf = item.get("match_confidence")
            if match_conf is not None:
                try:
                    conf = min(max(float(match_conf), 0.0), 1.0)
                except (TypeError, ValueError):
                    pass

        rows.append(OcrRow(
            raw_name=raw_name,
            status_mark=mark,
            confidence=min(max(conf, 0.0), 1.0),
            row_index=row_idx,
            bounding_box=bounding_box,
            matched_roster_name=matched_roster_name,
        ))

    log.info("gemini_ocr_done", extra={"rows_parsed": len(rows)})
    return rows


# ─── Mock provider (for testing without API calls) ────────────────────────────

class MockProvider(OcrProvider):
    """Returns a fixed list of rows for unit tests and dry-run demos."""

    DEFAULT_ROWS: list[OcrRow] = [
        OcrRow(raw_name="Mohamed Ali Saber",      status_mark="P",  confidence=0.95, row_index=1, bounding_box=None),
        OcrRow(raw_name="Mahmoud Ahmed Ali",       status_mark="A",  confidence=0.90, row_index=2, bounding_box=None),
        OcrRow(raw_name="Yassin Ahmed Helmy",      status_mark="L",  confidence=0.88, row_index=3, bounding_box=None),
        OcrRow(raw_name="Eyad Amr Mohamed",        status_mark="NC", confidence=0.85, row_index=4, bounding_box=None),
        OcrRow(raw_name="Hamza Mohamed Ramy",      status_mark="P",  confidence=0.92, row_index=5, bounding_box=None),
    ]

    def __init__(self, rows: Optional[list[OcrRow]] = None):
        self._rows = rows or self.DEFAULT_ROWS

    def read(self, image_bytes: bytes, roster: Optional[list[str]] = None) -> list[OcrRow]:
        log.info("mock_ocr_read", extra={"rows": len(self._rows), "roster_aware": bool(roster)})
        return list(self._rows)


# ─── Factory ──────────────────────────────────────────────────────────────────

def build_provider(cfg: dict) -> OcrProvider:
    """Build the OCR provider from the config dict (attendance.ocr section)."""
    provider_name = cfg.get("provider", "gemini")
    model = cfg.get("model", "gemini-3.1-flash-lite")
    min_conf = float(cfg.get("min_confidence", 0.75))

    if provider_name == "gemini":
        return GeminiProvider(
            model=model,
            min_confidence=min_conf,
            timeout_s=float(cfg.get("timeout_s", 60)),
            max_attempts=int(cfg.get("max_attempts", 3)),
        )
    elif provider_name == "mock":
        return MockProvider()

    raise ValueError(f"Unknown OCR provider: {provider_name!r}. Supported: gemini, mock")