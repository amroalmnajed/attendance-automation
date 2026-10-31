"""attendance/session_patterns.py — Session type recommendations based on patterns.

Pattern Detection:
  - Analyze date/time patterns for session types (odd/even parity)
  - Learn from historical session type assignments
  - Recommend session type based on time of day, day of week
  - Course-specific pattern learning
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

from attendance.cache import get_pattern_cache, set_pattern_cache
from attendance.db import SessionLocal, Pattern, Session


class SessionPatternEngine:
    """Engine for learning and recommending session types based on patterns."""

    def __init__(
        self,
        min_confidence: float = 0.7,
        min_samples: int = 3,
        cache_patterns: bool = True,
    ):
        """Initialize the session pattern engine.

        Args:
            min_confidence: Minimum confidence threshold for recommendations
            min_samples: Minimum number of samples to learn a pattern
            cache_patterns: Whether to cache pattern predictions
        """
        self.min_confidence = min_confidence
        self.min_samples = min_samples
        self.cache_patterns = cache_patterns

    def recommend_session_type(
        self,
        course_id: int,
        session_date: datetime,
        session_num: Optional[int] = None,
    ) -> Optional[str]:
        """Recommend a session type based on historical patterns.

        Args:
            course_id: Course ID
            session_date: Session date/time
            session_num: Session number (if available)

        Returns:
            Recommended session type, or None if no confident pattern exists
        """
        # Check cache first
        cache_key = f"session_type:{course_id}:{session_date.date()}:{session_num}"
        if self.cache_patterns:
            cached = get_pattern_cache(cache_key)
            if cached is not None:
                return cached

        recommendation = None
        best_confidence = 0.0

        session = SessionLocal()
        try:
            # Analyze multiple pattern types
            patterns = {
                "odd_even": self._analyze_odd_even_pattern(session, course_id, session_num),
                "day_of_week": self._analyze_day_of_week_pattern(session, course_id, session_date),
                "time_of_day": self._analyze_time_of_day_pattern(session, course_id, session_date),
            }

            # Combine patterns
            for pattern_type, (type_name, confidence) in patterns.items():
                if confidence >= self.min_confidence and confidence > best_confidence:
                    recommendation = type_name
                    best_confidence = confidence

            # Cache the result
            if self.cache_patterns and recommendation:
                set_pattern_cache(cache_key, recommendation)

            return recommendation

        finally:
            session.close()

    def _analyze_odd_even_pattern(
        self,
        session,
        course_id: int,
        session_num: Optional[int],
    ) -> tuple[Optional[str], float]:
        """Analyze odd/even session number pattern.

        Args:
            session: Database session
            course_id: Course ID
            session_num: Session number

        Returns:
            (session_type, confidence) tuple
        """
        if session_num is None:
            return None, 0.0

        is_odd = session_num % 2 == 1
        parity = "odd" if is_odd else "even"

        # Query patterns for odd_even type
        patterns = session.query(Pattern).filter(
            Pattern.course_id == course_id,
            Pattern.pattern_type == "odd_even",
            Pattern.is_active == True,
        ).all()

        if len(patterns) < self.min_samples:
            return None, 0.0

        # Filter patterns matching current parity and extract session_type
        import json
        session_types = []
        for p in patterns:
            try:
                data = json.loads(p.pattern_value)
                if data.get("trigger") == parity:
                    session_types.append((data.get("session_type"), p.confidence))
            except (json.JSONDecodeError, KeyError):
                continue

        if not session_types:
            return None, 0.0

        # Find highest confidence session type
        session_types.sort(key=lambda x: x[1], reverse=True)
        best_type, best_confidence = session_types[0]

        return best_type, best_confidence

    def _analyze_day_of_week_pattern(
        self,
        session,
        course_id: int,
        session_date: datetime,
    ) -> tuple[Optional[str], float]:
        """Analyze day-of-week pattern.

        Args:
            session: Database session
            course_id: Course ID
            session_date: Session date

        Returns:
            (session_type, confidence) tuple
        """
        day_of_week = str(session_date.weekday())  # 0 = Monday, 6 = Sunday

        # Query patterns for day_of_week type
        patterns = session.query(Pattern).filter(
            Pattern.course_id == course_id,
            Pattern.pattern_type == "day_of_week",
            Pattern.is_active == True,
        ).all()

        if len(patterns) < self.min_samples:
            return None, 0.0

        # Filter patterns matching current day and extract session_type
        import json
        session_types = []
        for p in patterns:
            try:
                data = json.loads(p.pattern_value)
                if data.get("trigger") == day_of_week:
                    session_types.append((data.get("session_type"), p.confidence))
            except (json.JSONDecodeError, KeyError):
                continue

        if not session_types:
            return None, 0.0

        # Find highest confidence session type
        session_types.sort(key=lambda x: x[1], reverse=True)
        best_type, best_confidence = session_types[0]

        return best_type, best_confidence

    def _analyze_time_of_day_pattern(
        self,
        session,
        course_id: int,
        session_date: datetime,
    ) -> tuple[Optional[str], float]:
        """Analyze time-of-day pattern.

        Args:
            session: Database session
            course_id: Course ID
            session_date: Session date

        Returns:
            (session_type, confidence) tuple
        """
        hour = session_date.hour

        # Define time slots
        time_slot = self._get_time_slot(hour)

        # Query patterns for time_of_day type
        patterns = session.query(Pattern).filter(
            Pattern.course_id == course_id,
            Pattern.pattern_type == "time_of_day",
            Pattern.is_active == True,
        ).all()

        if len(patterns) < self.min_samples:
            return None, 0.0

        # Filter patterns matching current time slot and extract session_type
        import json
        session_types = []
        for p in patterns:
            try:
                data = json.loads(p.pattern_value)
                if data.get("trigger") == time_slot:
                    session_types.append((data.get("session_type"), p.confidence))
            except (json.JSONDecodeError, KeyError):
                continue

        if not session_types:
            return None, 0.0

        # Find highest confidence session type
        session_types.sort(key=lambda x: x[1], reverse=True)
        best_type, best_confidence = session_types[0]

        return best_type, best_confidence

    def _get_time_slot(self, hour: int) -> str:
        """Get time slot for a given hour.

        Args:
            hour: Hour (0-23)

        Returns:
            Time slot identifier
        """
        if 6 <= hour < 12:
            return "morning"
        elif 12 <= hour < 17:
            return "afternoon"
        elif 17 <= hour < 21:
            return "evening"
        else:
            return "night"

    def record_session_assignment(
        self,
        course_id: int,
        session_date: datetime,
        session_num: Optional[int],
        session_type: str,
    ) -> None:
        """Record a session type assignment for learning.

        Args:
            course_id: Course ID
            session_date: Session date
            session_num: Session number
            session_type: Assigned session type
        """
        session = SessionLocal()
        try:
            # Record patterns as database entries
            # Odd/even pattern
            if session_num is not None:
                parity = "odd" if session_num % 2 == 1 else "even"
                self._store_pattern(
                    session,
                    course_id,
                    "odd_even",
                    f"{parity}",
                    session_type,
                )

            # Day of week pattern
            day_of_week = session_date.weekday()
            self._store_pattern(
                session,
                course_id,
                "day_of_week",
                str(day_of_week),
                session_type,
            )

            # Time of day pattern
            time_slot = self._get_time_slot(session_date.hour)
            self._store_pattern(
                session,
                course_id,
                "time_of_day",
                time_slot,
                session_type,
            )

            session.commit()

        except Exception:
            session.rollback()
        finally:
            session.close()

    def _store_pattern(
        self,
        session,
        course_id: int,
        pattern_type: str,
        pattern_value: str,
        session_type: str,
    ) -> None:
        """Store a pattern in the database.

        Args:
            session: Database session
            course_id: Course ID
            pattern_type: Pattern type (odd_even, day_of_week, time_of_day)
            pattern_value: Pattern value
            session_type: Session type
        """
        import json

        # Serialize pattern_value and session_type as JSON
        pattern_data = json.dumps({
            "trigger": pattern_value,
            "session_type": session_type,
        })

        # Check if pattern already exists (match on both trigger and session_type)
        existing = session.query(Pattern).filter(
            Pattern.course_id == course_id,
            Pattern.pattern_type == pattern_type,
            Pattern.pattern_value == pattern_data,
        ).first()

        if existing:
            # Update confidence (increment based on recurrence)
            existing.confidence = min(1.0, existing.confidence + 0.1)
            existing.count += 1
        else:
            # Create new pattern
            pattern = Pattern(
                course_id=course_id,
                pattern_type=pattern_type,
                pattern_value=pattern_data,
                confidence=0.5,  # Initial confidence
                count=1,
            )
            session.add(pattern)

    def get_course_patterns(self, course_id: int) -> dict:
        """Get all learned patterns for a course.

        Args:
            course_id: Course ID

        Returns:
            Dictionary of patterns by type
        """
        import json

        session = SessionLocal()
        try:
            patterns = session.query(Pattern).filter(
                Pattern.course_id == course_id,
            ).all()

            result = defaultdict(list)
            for p in patterns:
                try:
                    data = json.loads(p.pattern_value)
                    result[p.pattern_type].append({
                        "trigger": data.get("trigger"),
                        "session_type": data.get("session_type"),
                        "confidence": p.confidence,
                        "count": p.count,
                    })
                except (json.JSONDecodeError, KeyError):
                    # Fallback for legacy non-JSON patterns
                    result[p.pattern_type].append({
                        "value": p.pattern_value,
                        "confidence": p.confidence,
                        "count": p.count,
                    })

            return dict(result)

        finally:
            session.close()


# ─── Global pattern engine instance ─────────────────────────────────────────────

_pattern_engine: Optional[SessionPatternEngine] = None


def init_pattern_engine(
    min_confidence: float = 0.7,
    min_samples: int = 3,
    cache_patterns: bool = True,
) -> SessionPatternEngine:
    """Initialize the global session pattern engine.

    Args:
        min_confidence: Minimum confidence threshold
        min_samples: Minimum sample threshold
        cache_patterns: Whether to cache patterns

    Returns:
        The pattern engine instance
    """
    global _pattern_engine
    if _pattern_engine is None:
        _pattern_engine = SessionPatternEngine(
            min_confidence=min_confidence,
            min_samples=min_samples,
            cache_patterns=cache_patterns,
        )
    return _pattern_engine


def get_pattern_engine() -> Optional[SessionPatternEngine]:
    """Get the global session pattern engine instance."""
    return _pattern_engine


# ─── Integration helper ───────────────────────────────────────────────────────

def recommend_session_type(
    course_id: int,
    session_date: datetime,
    session_num: Optional[int] = None,
) -> Optional[str]:
    """Recommend a session type (convenience function).

    Args:
        course_id: Course ID
        session_date: Session date
        session_num: Session number

    Returns:
        Recommended session type
    """
    engine = init_pattern_engine()
    return engine.recommend_session_type(course_id, session_date, session_num)


def recommend_session_type_by_key(
    course_key: str,
    session_date: datetime,
    session_num: Optional[int] = None,
) -> Optional[str]:
    """Recommend a session type by course key (webapp-friendly wrapper).

    Args:
        course_key: Course key (e.g., "g9", "g10")
        session_date: Session date
        session_num: Session number

    Returns:
        Recommended session type, or None if no confident pattern exists
    """
    from attendance.db import get_course_by_key, SessionLocal

    session = SessionLocal()
    try:
        course = get_course_by_key(session, course_key)
        if course is None:
            return None
        return recommend_session_type(course.course_id, session_date, session_num)
    finally:
        session.close()
