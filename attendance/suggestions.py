"""attendance/suggestions.py — Historical pattern matching for smart suggestions.

Implementation:
  - Track which roster students are commonly matched to specific OCR patterns
  - Suggest top 3 candidates for review items based on history
  - Confidence scores based on frequency and recency
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from attendance.db import SessionLocal, Pattern
from attendance.normalizer import normalize_for_matching
from core.paths import data_path


class SuggestionEngine:
    """Engine for generating smart suggestions based on historical patterns."""

    def __init__(
        self,
        max_suggestions: int = 3,
        min_confidence: float = 0.3,
        recency_weight: float = 0.3,
        frequency_weight: float = 0.7,
    ):
        """Initialize the suggestion engine.

        Args:
            max_suggestions: Maximum number of suggestions to return
            min_confidence: Minimum confidence threshold for suggestions
            recency_weight: Weight for recency in confidence calculation
            frequency_weight: Weight for frequency in confidence calculation
        """
        self.max_suggestions = max_suggestions
        self.min_confidence = min_confidence
        self.recency_weight = recency_weight
        self.frequency_weight = frequency_weight

    def get_suggestions(
        self,
        raw_name: str,
        course_id: int,
        roster: list[tuple[str, int]],
    ) -> list[tuple[str, float]]:
        """Get suggestions for matching a raw name to roster students.

        Args:
            raw_name: Raw OCR name
            course_id: Course ID
            roster: List of (roster_name, sheet_row) tuples

        Returns:
            List of (roster_name, confidence) tuples, sorted by confidence
        """
        norm_name = normalize_for_matching(raw_name)

        # Query historical patterns from database
        session = SessionLocal()
        try:
            patterns = session.query(Pattern).filter(
                Pattern.course_id == course_id,
                Pattern.pattern_type == "name_match",
            ).all()

            # Build frequency map: roster_name -> count
            freq_map = defaultdict(int)
            recency_map = defaultdict(float)

            now = datetime.now(timezone.utc)

            for pattern in patterns:
                try:
                    pattern_value = json.loads(pattern.pattern_value)
                except json.JSONDecodeError:
                    continue
                if pattern_value.get("raw_norm") == norm_name:
                    roster_name = pattern_value.get("roster_name")
                    if roster_name:
                        freq_map[roster_name] += 1

                        # Calculate recency score (1.0 for recent, decays over time)
                        days_ago = (now - pattern.last_observed).days
                        recency_score = max(0.0, 1.0 - days_ago / 365.0)  # Decay over 1 year
                        recency_map[roster_name] = max(recency_map[roster_name], recency_score)

            # Calculate confidence scores
            suggestions = []
            total_freq = sum(freq_map.values()) if freq_map else 1

            for roster_name, freq in freq_map.items():
                freq_score = freq / total_freq
                recency_score = recency_map.get(roster_name, 0.0)

                # Combined confidence score
                confidence = (
                    self.frequency_weight * freq_score +
                    self.recency_weight * recency_score
                )

                if confidence >= self.min_confidence:
                    suggestions.append((roster_name, confidence))

            # Sort by confidence and return top N
            suggestions.sort(key=lambda x: x[1], reverse=True)
            return suggestions[:self.max_suggestions]

        finally:
            session.close()

    def record_match(
        self,
        raw_name: str,
        roster_name: str,
        course_id: int,
        confidence: float = 1.0,
    ) -> None:
        """Record a successful match for future learning.

        Args:
            raw_name: Raw OCR name
            roster_name: Matched roster name
            course_id: Course ID
            confidence: Match confidence (0-1)
        """
        norm_name = normalize_for_matching(raw_name)
        pattern_value = json.dumps({
            "raw_norm": norm_name,
            "roster_name": roster_name,
        })

        session = SessionLocal()
        try:
            # Check if pattern already exists
            existing = session.query(Pattern).filter(
                Pattern.course_id == course_id,
                Pattern.pattern_type == "name_match",
                Pattern.pattern_value == pattern_value,
            ).first()

            if existing:
                # Update existing pattern
                existing.confidence = max(existing.confidence, confidence)
                existing.last_observed = datetime.now(timezone.utc)
            else:
                # Create new pattern
                pattern = Pattern(
                    course_id=course_id,
                    pattern_type="name_match",
                    pattern_value=pattern_value,
                    confidence=confidence,
                    last_observed=datetime.now(timezone.utc),
                )
                session.add(pattern)

            session.commit()

        except Exception:
            session.rollback()
        finally:
            session.close()

    def record_correction(
        self,
        raw_name: str,
        old_roster_name: str,
        new_roster_name: str,
        course_id: int,
    ) -> None:
        """Record a correction to improve future suggestions.

        Args:
            raw_name: Raw OCR name
            old_roster_name: Previously matched (incorrect) roster name
            new_roster_name: Correct roster name
            course_id: Course ID
        """
        # Decrease confidence for the incorrect match
        norm_name = normalize_for_matching(raw_name)
        pattern_value = json.dumps({
            "raw_norm": norm_name,
            "roster_name": old_roster_name,
        })

        session = SessionLocal()
        try:
            existing = session.query(Pattern).filter(
                Pattern.course_id == course_id,
                Pattern.pattern_type == "name_match",
                Pattern.pattern_value == pattern_value,
            ).first()

            if existing:
                existing.confidence = max(0.0, existing.confidence - 0.2)  # Penalty

            # Record the correct match with high confidence
            self.record_match(raw_name, new_roster_name, course_id, confidence=1.0)

            session.commit()

        except Exception:
            session.rollback()
        finally:
            session.close()


# ─── Global suggestion engine instance ─────────────────────────────────────────

_suggestion_engine: Optional[SuggestionEngine] = None


def init_suggestion_engine(
    max_suggestions: int = 3,
    min_confidence: float = 0.3,
    recency_weight: float = 0.3,
    frequency_weight: float = 0.7,
) -> SuggestionEngine:
    """Initialize the global suggestion engine.

    Args:
        max_suggestions: Maximum number of suggestions to return
        min_confidence: Minimum confidence threshold
        recency_weight: Weight for recency in confidence calculation
        frequency_weight: Weight for frequency in confidence calculation

    Returns:
        The suggestion engine instance
    """
    global _suggestion_engine
    if _suggestion_engine is None:
        _suggestion_engine = SuggestionEngine(
            max_suggestions=max_suggestions,
            min_confidence=min_confidence,
            recency_weight=recency_weight,
            frequency_weight=frequency_weight,
        )
    return _suggestion_engine


def get_suggestion_engine() -> Optional[SuggestionEngine]:
    """Get the global suggestion engine instance."""
    return _suggestion_engine


# ─── Learning from learned_aliases.json ─────────────────────────────────────────

def import_aliases_to_patterns(aliases_path: str | Path, course_id: int) -> int:
    """Import learned aliases into the patterns database.

    Args:
        aliases_path: Path to learned_aliases.json
        course_id: Course ID to associate patterns with

    Returns:
        Number of patterns imported
    """
    import json
    from attendance.db import get_session, Course

    aliases_path = data_path(aliases_path)
    if not aliases_path.exists():
        return 0

    with open(aliases_path, "r", encoding="utf-8") as f:
        aliases_data = json.load(f)

    # Get course_key from database using course_id
    session = get_session()
    try:
        course = session.query(Course).filter(Course.course_id == course_id).first()
        if not course:
            return 0
        course_key = course.course_key
    finally:
        session.close()

    engine = init_suggestion_engine()
    count = 0

    for raw_name, roster_name in aliases_data.get(course_key, {}).items():
        engine.record_match(raw_name, roster_name, course_id, confidence=1.0)
        count += 1

    return count
