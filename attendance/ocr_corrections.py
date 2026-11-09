"""attendance/ocr_corrections.py — OCR error auto-correction learning system.

Learning System:
  - Track common OCR corrections from review resolutions
  - Build correction patterns (e.g., "0" → "O", "5" → "S", "rn" → "m")
  - Apply corrections before matching
  - Confidence-based application (only high-confidence corrections)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from attendance.cache import get_ocr_correction, set_ocr_correction
from attendance.db import SessionLocal, OcrCorrection


class OCRCorrectionEngine:
    """Engine for learning and applying OCR corrections."""

    def __init__(
        self,
        min_confidence: float = 0.8,
        min_frequency: int = 3,
        cache_corrections: bool = True,
    ):
        """Initialize the OCR correction engine.

        Args:
            min_confidence: Minimum confidence threshold for applying corrections
            min_frequency: Minimum number of occurrences to learn a correction
            cache_corrections: Whether to cache corrections
        """
        self.min_confidence = min_confidence
        self.min_frequency = min_frequency
        self.cache_corrections = cache_corrections

    def get_correction(self, raw_text: str) -> Optional[str]:
        """Get the corrected version of raw OCR text.

        Args:
            raw_text: Raw OCR text

        Returns:
            Corrected text, or None if no high-confidence correction exists
        """
        # Check cache first
        if self.cache_corrections:
            cached = get_ocr_correction(raw_text)
            if cached is not None:
                return cached

        # Query database
        session = SessionLocal()
        try:
            correction = session.query(OcrCorrection).filter(
                OcrCorrection.raw_text == raw_text,
                OcrCorrection.frequency >= self.min_frequency,
            ).first()

            if correction and correction.frequency >= self.min_frequency:
                # Calculate confidence based on frequency
                confidence = min(1.0, correction.frequency / 10.0)

                if confidence >= self.min_confidence:
                    # Cache the result
                    if self.cache_corrections:
                        set_ocr_correction(raw_text, correction.corrected_text)
                    return correction.corrected_text

            return None

        finally:
            session.close()

    def apply_corrections(self, text: str) -> str:
        """Apply all known corrections to a text.

        Args:
            text: Input text

        Returns:
            Text with corrections applied
        """
        corrected = text

        # Apply database-stored corrections
        session = SessionLocal()
        try:
            corrections = session.query(OcrCorrection).filter(
                OcrCorrection.frequency >= self.min_frequency,
            ).all()

            for correction in corrections:
                confidence = min(1.0, correction.frequency / 10.0)
                if confidence >= self.min_confidence:
                    corrected = corrected.replace(correction.raw_text, correction.corrected_text)

        finally:
            session.close()

        return corrected

    def record_correction(
        self,
        raw_text: str,
        corrected_text: str,
    ) -> None:
        """Record a correction for learning.

        Args:
            raw_text: Raw OCR text
            corrected_text: Corrected text
        """
        session = SessionLocal()
        try:
            # Check if correction already exists
            existing = session.query(OcrCorrection).filter(
                OcrCorrection.raw_text == raw_text,
                OcrCorrection.corrected_text == corrected_text,
            ).first()

            if existing:
                # Update frequency and last applied
                existing.frequency += 1
                existing.last_applied = datetime.now(timezone.utc)
            else:
                # Create new correction
                correction = OcrCorrection(
                    raw_text=raw_text,
                    corrected_text=corrected_text,
                    frequency=1,
                    last_applied=datetime.now(timezone.utc),
                )
                session.add(correction)

            session.commit()

            # Invalidate cache for this raw text
            if self.cache_corrections:
                from attendance.cache import get_cache
                cache = get_cache()
                cache.delete(f"ocr:{raw_text}", "ocr")

        except Exception:
            session.rollback()
        finally:
            session.close()

    def learn_from_diff(self, original: str, corrected: str) -> list[tuple[str, str]]:
        """Learn correction patterns from a diff between original and corrected text.

        Args:
            original: Original text
            corrected: Corrected text

        Returns:
            List of (raw, corrected) pattern pairs learned
        """
        # Simple diff-based learning
        patterns = []

        # Find character-level differences
        if len(original) == len(corrected):
            for i, (o, c) in enumerate(zip(original, corrected)):
                if o != c:
                    patterns.append((o, c))

        # Find substring-level differences
        orig_words = original.split()
        corr_words = corrected.split()

        if len(orig_words) == len(corr_words):
            for o, c in zip(orig_words, corr_words):
                if o != c:
                    patterns.append((o, c))

        # Record learned patterns
        for raw, corr in patterns:
            self.record_correction(raw, corr)

        return patterns

    def get_top_corrections(self, limit: int = 20) -> list[dict]:
        """Get the most frequent corrections.

        Args:
            limit: Maximum number of corrections to return

        Returns:
            List of correction dicts with raw_text, corrected_text, frequency
        """
        session = SessionLocal()
        try:
            corrections = session.query(OcrCorrection).order_by(
                OcrCorrection.frequency.desc()
            ).limit(limit).all()

            return [
                {
                    "raw_text": c.raw_text,
                    "corrected_text": c.corrected_text,
                    "frequency": c.frequency,
                    "last_applied": c.last_applied.isoformat() if c.last_applied else None,
                }
                for c in corrections
            ]

        finally:
            session.close()


# ─── Common OCR error patterns ────────────────────────────────────────────────

# Pre-defined common OCR errors (seed data)
COMMON_OCR_ERRORS = [
    # Number/letter confusions
    ("0", "O"),
    ("1", "I"),
    ("2", "Z"),
    ("3", "E"),
    ("4", "A"),
    ("5", "S"),
    ("8", "B"),
    # Character confusions
    ("rn", "m"),
    ("vv", "w"),
    ("cl", "d"),
    # Common typos
    ("teh", "the"),
    ("adn", "and"),
    ("wiht", "with"),
]


def seed_common_corrections(engine: OCRCorrectionEngine) -> int:
    """Seed the correction engine with common OCR errors.

    Args:
        engine: OCR correction engine instance

    Returns:
        Number of corrections seeded
    """
    count = 0
    for raw, corrected in COMMON_OCR_ERRORS:
        engine.record_correction(raw, corrected)
        count += 1
    return count


# ─── Global correction engine instance ─────────────────────────────────────────

_correction_engine: Optional[OCRCorrectionEngine] = None


def init_correction_engine(
    min_confidence: float = 0.8,
    min_frequency: int = 3,
    cache_corrections: bool = True,
) -> OCRCorrectionEngine:
    """Initialize the global OCR correction engine.

    Args:
        min_confidence: Minimum confidence threshold
        min_frequency: Minimum frequency threshold
        cache_corrections: Whether to cache corrections

    Returns:
        The correction engine instance
    """
    global _correction_engine
    if _correction_engine is None:
        _correction_engine = OCRCorrectionEngine(
            min_confidence=min_confidence,
            min_frequency=min_frequency,
            cache_corrections=cache_corrections,
        )
    return _correction_engine


def get_correction_engine() -> Optional[OCRCorrectionEngine]:
    """Get the global OCR correction engine instance."""
    return _correction_engine


# ─── Integration helper ───────────────────────────────────────────────────────

def correct_ocr_text(text: str) -> str:
    """Apply OCR corrections to text (convenience function).

    Args:
        text: Raw OCR text

    Returns:
        Corrected text
    """
    engine = init_correction_engine()
    return engine.apply_corrections(text)
