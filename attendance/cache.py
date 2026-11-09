"""attendance/cache.py — Caching layer with file-based SQLite backend.

Cache types:
  - Roster data: TTL 60 seconds (matches existing session_resolver cache)
  - Session info: TTL 60 seconds
  - ML model embeddings: Persistent cache (no expiration)
  - OCR corrections: Persistent cache
  - Permission checks: TTL 300 seconds

Implementation:
  - File-based caching using SQLite for simplicity
  - Cache invalidation on writes
  - Cache warming on startup
  - Metrics for cache hit/miss rates
"""
from __future__ import annotations

import json
import pickle
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import Boolean, DateTime, Float, Integer, LargeBinary, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from core.paths import data_path


class Base(DeclarativeBase):
    """Base class for cache models."""
    pass


class CacheEntry(Base):
    """Generic cache entry."""
    __tablename__ = "cache_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cache_key: Mapped[str] = mapped_column(String(500), unique=True, nullable=False, index=True)
    cache_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)  # roster, session, embedding, ocr, permission
    value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON-encoded value
    value_bytes: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)  # Pickle-encoded for complex objects
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now(timezone.utc), nullable=False)
    hit_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    miss_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


# ─── Cache backend ───────────────────────────────────────────────────────────

class CacheBackend:
    """File-based SQLite cache backend."""

    def __init__(self, cache_path: str | Path = "cache.db"):
        """Initialize the cache backend.

        Args:
            cache_path: Path to the cache database file
        """
        cache_path = data_path(cache_path)
        cache_path.parent.mkdir(parents=True, exist_ok=True)

        self._engine = create_engine(f"sqlite:///{cache_path}", connect_args={"check_same_thread": False})
        Base.metadata.create_all(self._engine)
        self._session_factory = sessionmaker(bind=self._engine)

        # In-memory metrics for hit/miss tracking
        self._hit_count = 0
        self._miss_count = 0

    def _get_session(self) -> Session:
        """Get a new database session."""
        return self._session_factory()

    def get(self, key: str, cache_type: str = "default") -> Optional[Any]:
        """Get a value from the cache.

        Args:
            key: Cache key
            cache_type: Type of cache entry

        Returns:
            Cached value, or None if not found or expired
        """
        session = self._get_session()
        try:
            entry = session.query(CacheEntry).filter(
                CacheEntry.cache_key == key,
                CacheEntry.cache_type == cache_type,
            ).first()

            if entry is None:
                # Cache miss - track in memory
                self._miss_count += 1
                return None

            # Check expiration
            if entry.expires_at and entry.expires_at < datetime.now(timezone.utc):
                return None

            # Record hit
            entry.hit_count += 1
            self._hit_count += 1
            session.commit()

            # Deserialize value
            if entry.value_bytes:
                try:
                    return pickle.loads(entry.value_bytes)
                except Exception:
                    pass
            if entry.value:
                try:
                    return json.loads(entry.value)
                except Exception:
                    return entry.value

            return None

        except Exception:
            session.rollback()
            return None
        finally:
            session.close()

    def set(self, key: str, value: Any, cache_type: str = "default", ttl_seconds: Optional[int] = None) -> None:
        """Set a value in the cache.

        Args:
            key: Cache key
            value: Value to cache
            cache_type: Type of cache entry
            ttl_seconds: Time-to-live in seconds (None for no expiration)
        """
        session = self._get_session()
        try:
            expires_at = None
            if ttl_seconds is not None:
                expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)

            # Serialize value
            value_str = None
            value_bytes = None

            # Try JSON first for simple types
            try:
                value_str = json.dumps(value)
            except Exception:
                # Fall back to pickle for complex objects
                value_bytes = pickle.dumps(value)

            entry = session.query(CacheEntry).filter(
                CacheEntry.cache_key == key,
                CacheEntry.cache_type == cache_type,
            ).first()

            if entry:
                entry.value = value_str
                entry.value_bytes = value_bytes
                entry.expires_at = expires_at
                entry.created_at = datetime.now(timezone.utc)
            else:
                entry = CacheEntry(
                    cache_key=key,
                    cache_type=cache_type,
                    value=value_str,
                    value_bytes=value_bytes,
                    expires_at=expires_at,
                )
                session.add(entry)

            session.commit()

        except Exception:
            session.rollback()
        finally:
            session.close()

    def delete(self, key: str, cache_type: str = "default") -> bool:
        """Delete a value from the cache.

        Args:
            key: Cache key
            cache_type: Type of cache entry

        Returns:
            True if deleted, False if not found
        """
        session = self._get_session()
        try:
            entry = session.query(CacheEntry).filter(
                CacheEntry.cache_key == key,
                CacheEntry.cache_type == cache_type,
            ).first()

            if entry:
                session.delete(entry)
                session.commit()
                return True

            return False

        except Exception:
            session.rollback()
            return False
        finally:
            session.close()

    def invalidate_type(self, cache_type: str) -> int:
        """Invalidate all entries of a given type.

        Args:
            cache_type: Type of cache entry to invalidate

        Returns:
            Number of entries invalidated
        """
        session = self._get_session()
        try:
            count = session.query(CacheEntry).filter(CacheEntry.cache_type == cache_type).delete()
            session.commit()
            return count

        except Exception:
            session.rollback()
            return 0
        finally:
            session.close()

    def cleanup_expired(self) -> int:
        """Remove expired entries from the cache.

        Returns:
            Number of entries removed
        """
        session = self._get_session()
        try:
            count = session.query(CacheEntry).filter(
                CacheEntry.expires_at < datetime.now(timezone.utc)
            ).delete()
            session.commit()
            return count

        except Exception:
            session.rollback()
            return 0
        finally:
            session.close()

    def get_stats(self) -> dict:
        """Get cache statistics.

        Returns:
            Dict with hit rate, miss rate, and entry counts
        """
        session = self._get_session()
        try:
            total_hits = session.query(CacheEntry).with_entities(
                CacheEntry.hit_count
            ).all()
            total_hits = sum(row[0] for row in total_hits)

            # Use in-memory miss counter
            total_misses = self._miss_count

            total_requests = total_hits + total_misses
            hit_rate = total_hits / total_requests if total_requests > 0 else 0.0

            entry_counts = {}
            for cache_type in ["roster", "session", "embedding", "ocr", "permission"]:
                count = session.query(CacheEntry).filter(
                    CacheEntry.cache_type == cache_type
                ).count()
                entry_counts[cache_type] = count

            return {
                "hit_rate": hit_rate,
                "total_hits": total_hits,
                "total_misses": total_misses,
                "total_requests": total_requests,
                "entry_counts": entry_counts,
            }

        except Exception:
            return {}
        finally:
            session.close()


# ─── Global cache instance ─────────────────────────────────────────────────────

_cache_backend: Optional[CacheBackend] = None


def init_cache(cache_path: str | Path = "cache.db") -> CacheBackend:
    """Initialize the global cache backend.

    Args:
        cache_path: Path to the cache database file

    Returns:
        The cache backend instance
    """
    global _cache_backend
    if _cache_backend is None:
        _cache_backend = CacheBackend(cache_path)
    return _cache_backend


def get_cache() -> CacheBackend:
    """Get the global cache backend instance."""
    if _cache_backend is None:
        init_cache()
    return _cache_backend


# ─── Typed cache helpers ───────────────────────────────────────────────────────

def get_roster(course_key: str) -> Optional[dict]:
    """Get roster data from cache (TTL: 60 seconds)."""
    return get_cache().get(f"roster:{course_key}", "roster")


def set_roster(course_key: str, roster: dict) -> None:
    """Set roster data in cache (TTL: 60 seconds)."""
    get_cache().set(f"roster:{course_key}", roster, "roster", ttl_seconds=60)


def invalidate_roster(course_key: str) -> None:
    """Invalidate roster cache for a course."""
    get_cache().delete(f"roster:{course_key}", "roster")


def get_session_info(course_key: str, session_num: int) -> Optional[dict]:
    """Get session info from cache (TTL: 60 seconds)."""
    return get_cache().get(f"session:{course_key}:{session_num}", "session")


def set_session_info(course_key: str, session_num: int, session_info: dict) -> None:
    """Set session info in cache (TTL: 60 seconds)."""
    get_cache().set(f"session:{course_key}:{session_num}", session_info, "session", ttl_seconds=60)


def invalidate_session(course_key: str, session_num: Optional[int] = None) -> None:
    """Invalidate session cache for a course or specific session."""
    if session_num is None:
        get_cache().invalidate_type("session")
    else:
        get_cache().delete(f"session:{course_key}:{session_num}", "session")


def get_embedding(name: str) -> Optional[Any]:
    """Get ML embedding from cache (persistent, no expiration)."""
    return get_cache().get(f"embedding:{name}", "embedding")


def set_embedding(name: str, embedding: Any) -> None:
    """Set ML embedding in cache (persistent, no expiration)."""
    get_cache().set(f"embedding:{name}", embedding, "embedding", ttl_seconds=None)


def get_ocr_correction(raw_text: str) -> Optional[str]:
    """Get OCR correction from cache (persistent, no expiration)."""
    return get_cache().get(f"ocr:{raw_text}", "ocr")


def set_ocr_correction(raw_text: str, corrected_text: str) -> None:
    """Set OCR correction in cache (persistent, no expiration)."""
    get_cache().set(f"ocr:{raw_text}", corrected_text, "ocr", ttl_seconds=None)


def get_permission(user_id: int, course_key: str) -> Optional[str]:
    """Get permission level from cache (TTL: 300 seconds)."""
    return get_cache().get(f"permission:{user_id}:{course_key}", "permission")


def set_permission(user_id: int, course_key: str, permission_level: str) -> None:
    """Set permission level in cache (TTL: 300 seconds)."""
    get_cache().set(f"permission:{user_id}:{course_key}", permission_level, "permission", ttl_seconds=300)


def invalidate_permissions(user_id: Optional[int] = None) -> None:
    """Invalidate permission cache for a user or all users."""
    if user_id is None:
        get_cache().invalidate_type("permission")
    else:
        # Delete all permission entries for this user
        session = get_cache()._get_session()
        try:
            session.query(CacheEntry).filter(
                CacheEntry.cache_type == "permission",
                CacheEntry.cache_key.like(f"permission:{user_id}:%")
            ).delete()
            session.commit()
        except Exception:
            session.rollback()
        finally:
            session.close()


def get_pattern_cache(cache_key: str) -> Optional[str]:
    """Get cached pattern recommendation."""
    entry = get_cache().get(cache_key, "pattern")
    if entry:
        return entry
    return None


def set_pattern_cache(cache_key: str, value: str, ttl: int = None) -> None:
    """Set cached pattern recommendation."""
    if ttl is None:
        ttl = 3600  # Default 1 hour for patterns
    get_cache().set(cache_key, value, "pattern", ttl_seconds=ttl)


def warm_cache(course_keys: list[str]) -> None:
    """Warm cache by loading roster and session data for courses.

    Args:
        course_keys: List of course keys to warm
    """
    from attendance.session_resolver import resolve

    for course_key in course_keys:
        try:
            session = resolve(course_key)
            # Cache roster
            set_roster(course_key, session.roster)
            # Cache session info for all sessions
            for session_num, col in enumerate(session.session_cols, start=1):
                set_session_info(course_key, session_num, {
                    "session_col": col,
                    "type_col": session.type_col,
                    "layout": session.layout,
                })
        except Exception:
            # Silently skip courses that fail to resolve
            pass


def get_cache_stats() -> dict:
    """Get cache statistics."""
    return get_cache().get_stats()
