"""attendance/ocr_tesseract.py — Tesseract-based OCR provider (offline fallback).

Used when Gemini is unavailable (quota exhausted, network down) and
``attendance.ocr.fallback_to_tesseract: true`` is set in the config.

Accuracy on handwritten Arabic/English names is significantly lower than Gemini.
A clear warning is logged whenever this provider is called.

System requirement: ``tesseract-ocr`` must be installed.
  Ubuntu/Debian : sudo apt-get install -y tesseract-ocr
  macOS         : brew install tesseract
  Windows       : https://github.com/UB-Mannheim/tesseract/wiki (add to PATH)
  Railway       : add ``apt-get install -y tesseract-ocr`` to your Dockerfile / nixpacks.toml
"""
from __future__ import annotations

import io
from typing import Optional

from attendance.ocr import OcrRow, OcrUnavailableError
from core.logger import get_logger

log = get_logger("ocr_tesseract")


class TesseractProvider:
    """OCR via Tesseract (offline, CPU-based).

    Wraps ``pytesseract.image_to_data`` word-level output into lines, then
    constructs one :class:`~attendance.ocr.OcrRow` per line.

    Notes
    -----
    - ``status_mark`` is always ``None`` — Tesseract can't reliably distinguish
      the attendance-mark column from the name column on these sheets; the
      matcher and review queue handle it.
    - ``bounding_box`` is normalised [ymin, xmin, ymax, xmax] (0.0–1.0).
    - ``confidence`` is Tesseract's word-level ``conf`` averaged across the
      line, then divided by 100 to produce a 0.0–1.0 float.
    - The roster list is accepted but ignored: Tesseract has no concept of a
      constrained vocabulary for handwritten text.
    """

    # Tesseract page-segmentation mode 6 = "assume a single uniform block of text"
    # (good for a tabular attendance sheet read top-to-bottom).
    _DEFAULT_CONFIG = "--psm 6"

    def __init__(
        self,
        lang: str = "eng",
        tesseract_cmd: Optional[str] = None,
        config: str = _DEFAULT_CONFIG,
    ) -> None:
        self.lang = lang
        self.tesseract_cmd = tesseract_cmd
        self.config = config

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def read(
        self,
        image_bytes: bytes,
        roster: Optional[list[str]] = None,
    ) -> list[OcrRow]:
        """Convert *image_bytes* to a list of :class:`~attendance.ocr.OcrRow`.

        Parameters
        ----------
        image_bytes:
            Raw bytes of the attendance sheet photo (JPEG / PNG / WEBP).
        roster:
            Accepted for API compatibility; not used by Tesseract.

        Raises
        ------
        OcrUnavailableError
            When the Tesseract binary cannot be found.
        """
        log.warning(
            "tesseract_fallback_active",
            extra={"detail": "Using Tesseract fallback — expect more review-queue items."},
        )

        try:
            import pytesseract  # type: ignore[import]
            from PIL import Image  # type: ignore[import]
        except ImportError as exc:
            raise OcrUnavailableError(
                "pytesseract or Pillow not installed. "
                "Run: pip install pytesseract Pillow"
            ) from exc

        if self.tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = self.tesseract_cmd

        # Convert raw bytes → PIL Image
        try:
            pil_img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        except Exception as exc:
            raise OcrUnavailableError(f"Failed to decode image for Tesseract: {exc}") from exc

        # Run Tesseract
        try:
            data = pytesseract.image_to_data(
                pil_img,
                lang=self.lang,
                config=self.config,
                output_type=pytesseract.Output.DICT,
            )
        except pytesseract.pytesseract.TesseractNotFoundError as exc:
            raise OcrUnavailableError(
                "Tesseract binary not found. Install tesseract-ocr and ensure it is on PATH. "
                "See DEPLOYMENT.md § OCR Fallback."
            ) from exc
        except Exception as exc:
            raise OcrUnavailableError(f"Tesseract OCR failed: {exc}") from exc

        img_w, img_h = pil_img.size
        rows = _group_into_rows(data, img_w, img_h)
        log.info("tesseract_ocr_done", extra={"rows_parsed": len(rows)})
        return rows


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────

def _group_into_rows(data: dict, img_w: int, img_h: int) -> list[OcrRow]:
    """Group word-level pytesseract output into line-level OcrRow objects.

    The strategy: group words by ``(page_num, block_num, par_num, line_num)``,
    concatenate word texts, average confidences, and compute a bounding box
    spanning all words on the line.  Lines whose combined text is blank (or
    only whitespace) are skipped.
    """
    from collections import defaultdict

    # Build a stable sort key for lines so we preserve reading order (top-to-bottom).
    line_words: dict[tuple, list[int]] = defaultdict(list)
    n = len(data["text"])
    for i in range(n):
        # Tesseract uses -1 as a sentinel confidence for non-word entries (block/line dividers)
        if int(data["conf"][i]) < 0:
            continue
        key = (
            int(data["page_num"][i]),
            int(data["block_num"][i]),
            int(data["par_num"][i]),
            int(data["line_num"][i]),
        )
        line_words[key].append(i)

    # Sort lines by their top-left corner (top then left) to assign row indices
    def _line_top(indices: list[int]) -> float:
        return min(int(data["top"][i]) for i in indices)

    sorted_lines = sorted(line_words.items(), key=lambda kv: _line_top(kv[1]))

    rows: list[OcrRow] = []
    for row_index, (_, indices) in enumerate(sorted_lines, start=1):
        texts = [str(data["text"][i]).strip() for i in indices if str(data["text"][i]).strip()]
        if not texts:
            continue

        combined = " ".join(texts)

        confs = [float(data["conf"][i]) for i in indices if int(data["conf"][i]) >= 0]
        avg_conf = (sum(confs) / len(confs) / 100.0) if confs else 0.0
        avg_conf = min(max(avg_conf, 0.0), 1.0)

        # Bounding box (normalised [ymin, xmin, ymax, xmax])
        lefts  = [int(data["left"][i])                        for i in indices]
        tops   = [int(data["top"][i])                         for i in indices]
        rights = [int(data["left"][i]) + int(data["width"][i]) for i in indices]
        bots   = [int(data["top"][i]) + int(data["height"][i]) for i in indices]

        xmin = min(lefts)  / max(img_w, 1)
        ymin = min(tops)   / max(img_h, 1)
        xmax = max(rights) / max(img_w, 1)
        ymax = max(bots)   / max(img_h, 1)
        bbox = [ymin, xmin, ymax, xmax]

        rows.append(OcrRow(
            raw_name=combined,
            status_mark=None,
            confidence=avg_conf,
            row_index=row_index,
            bounding_box=bbox,
        ))

    return rows
