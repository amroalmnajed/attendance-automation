"""attendance/db.py — SQLAlchemy ORM models for the attendance automation system.

SQLite with WAL mode for concurrent access. Migrations managed by Alembic.

Schema:
  - courses: course configuration (sheet_id, tab, layout, config_json)
  - students: roster students per course (name, sheet_row, is_active)
  - sessions: session metadata per course (session_num, session_type, date, session_col, type_col)
  - attendance_records: attendance marks (student_id, session_id, value, uploaded_at, uploaded_by)
  - audit_log: comprehensive audit trail (action, entity_type, entity_id, old_value, new_value, user, timestamp)
  - users: user accounts (username, telegram_id, role, permissions_json)
  - course_permissions: per-course access control (user_id, course_id, permission_level)
  - aliases: learned name aliases (course_id, raw_name_key, roster_name, confirmations, first_seen, last_used)
  - ocr_corrections: learned OCR corrections (raw_text, corrected_text, frequency, last_applied)
  - patterns: historical patterns for suggestions (course_id, pattern_type, pattern_value, confidence)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    event,
    func,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

from core.paths import data_path


class Base(DeclarativeBase):
    """Base class for all ORM models."""
    pass


class Course(Base):
    """Course configuration and metadata."""
    __tablename__ = "courses"

    course_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_key: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)  # e.g., "crash", "main", "g10", "g9"
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    sheet_id: Mapped[str] = mapped_column(String(100), nullable=False)  # Environment variable key or actual ID
    tab: Mapped[str] = mapped_column(String(100), nullable=False)
    layout: Mapped[str] = mapped_column(String(20), nullable=False, default="single")  # single | typed_pair
    config_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # Additional course-specific config
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    # Relationships
    students: Mapped[list["Student"]] = relationship(back_populates="course", cascade="all, delete-orphan")
    sessions: Mapped[list["Session"]] = relationship(back_populates="course", cascade="all, delete-orphan")
    permissions: Mapped[list["CoursePermission"]] = relationship(back_populates="course", cascade="all, delete-orphan")
    aliases: Mapped[list["Alias"]] = relationship(back_populates="course", cascade="all, delete-orphan")
    patterns: Mapped[list["Pattern"]] = relationship(back_populates="course", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_course_key_active", "course_key", "is_active"),
    )


class Student(Base):
    """Roster student per course."""
    __tablename__ = "students"

    student_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.course_id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    sheet_row: Mapped[int] = mapped_column(Integer, nullable=False)  # 1-based row in Google Sheets
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    # Relationships
    course: Mapped["Course"] = relationship(back_populates="students")
    attendance_records: Mapped[list["AttendanceRecord"]] = relationship(back_populates="student", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_course_student", "course_id", "name"),
        Index("idx_course_sheet_row", "course_id", "sheet_row"),
    )


class Session(Base):
    """Session metadata per course."""
    __tablename__ = "sessions"

    session_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.course_id"), nullable=False, index=True)
    session_num: Mapped[int] = mapped_column(Integer, nullable=False)
    session_type: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)  # e.g., "Saturday - Center"
    date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    session_col: Mapped[str] = mapped_column(String(10), nullable=False)  # Column letter for attendance
    type_col: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)  # Column letter for type (typed_pair)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    # Relationships
    course: Mapped["Course"] = relationship(back_populates="sessions")
    attendance_records: Mapped[list["AttendanceRecord"]] = relationship(back_populates="session", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_course_session_num", "course_id", "session_num", unique=True),
    )


class AttendanceRecord(Base):
    """Attendance mark for a student in a session."""
    __tablename__ = "attendance_records"

    record_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.student_id"), nullable=False, index=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.session_id"), nullable=False, index=True)
    value: Mapped[str] = mapped_column(String(50), nullable=False)  # Present, Late, Not complete, Absent
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    uploaded_by: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)  # Username or "system"
    upload_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)  # Reference to upload batch
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    # Relationships
    student: Mapped["Student"] = relationship(back_populates="attendance_records")
    session: Mapped["Session"] = relationship(back_populates="attendance_records")

    __table_args__ = (
        Index("idx_session_student", "session_id", "student_id", unique=True),
        Index("idx_upload_id", "upload_id"),
    )


class AuditLog(Base):
    """Comprehensive audit trail for all system changes."""
    __tablename__ = "audit_log"

    log_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)  # e.g., "attendance_write", "alias_learn"
    entity_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)  # e.g., "attendance_record", "alias"
    entity_id: Mapped[Optional[str]] = mapped_column(String(200), nullable=True, index=True)  # ID of affected entity
    old_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    new_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    user: Mapped[Optional[str]] = mapped_column(String(200), nullable=True, index=True)  # Username or "system"
    context: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # Additional context (JSON)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)

    __table_args__ = (
        Index("idx_action_timestamp", "action", "timestamp"),
        Index("idx_entity_timestamp", "entity_type", "entity_id", "timestamp"),
    )


class User(Base):
    """User accounts for RBAC."""
    __tablename__ = "users"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    telegram_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, unique=True, index=True)
    role: Mapped[str] = mapped_column(String(50), nullable=False, default="viewer")  # viewer, editor, admin
    permissions_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # Additional permissions (JSON)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    # Relationships
    course_permissions: Mapped[list["CoursePermission"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class CoursePermission(Base):
    """Per-course access control."""
    __tablename__ = "course_permissions"

    permission_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.user_id"), nullable=False, index=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.course_id"), nullable=False, index=True)
    permission_level: Mapped[str] = mapped_column(String(50), nullable=False)  # view, edit, admin
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    # Relationships
    user: Mapped["User"] = relationship(back_populates="course_permissions")
    course: Mapped["Course"] = relationship(back_populates="permissions")

    __table_args__ = (
        Index("idx_user_course", "user_id", "course_id", unique=True),
    )


class Alias(Base):
    """Learned name aliases (migrated from learned_aliases.json)."""
    __tablename__ = "aliases"

    alias_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.course_id"), nullable=False, index=True)
    raw_name_key: Mapped[str] = mapped_column(String(200), nullable=False, index=True)  # Normalized paper name
    roster_name: Mapped[str] = mapped_column(String(200), nullable=False)  # Actual roster name
    confirmations: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=datetime.now(timezone.utc), nullable=False)
    last_confirmed: Mapped[datetime] = mapped_column(DateTime, default=datetime.now(timezone.utc), nullable=False)
    last_used: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    # Relationships
    course: Mapped["Course"] = relationship(back_populates="aliases")

    __table_args__ = (
        Index("idx_course_raw_name", "course_id", "raw_name_key"),
        Index("idx_last_used", "last_used"),
    )


class OcrCorrection(Base):
    """Learned OCR corrections."""
    __tablename__ = "ocr_corrections"

    correction_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    raw_text: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    corrected_text: Mapped[str] = mapped_column(String(500), nullable=False)
    frequency: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)  # 0.0 to 1.0
    last_applied: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    __table_args__ = (
        Index("idx_raw_text_active", "raw_text", "is_active"),
    )


class Pattern(Base):
    """Historical patterns for smart suggestions."""
    __tablename__ = "patterns"

    pattern_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.course_id"), nullable=False, index=True)
    pattern_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)  # e.g., "session_type", "name_match"
    pattern_value: Mapped[str] = mapped_column(String(500), nullable=False)  # The pattern data (JSON if complex)
    confidence: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)  # 0.0 to 1.0
    count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)  # How many times observed
    last_observed: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    # Relationships
    course: Mapped["Course"] = relationship(back_populates="patterns")

    __table_args__ = (
        Index("idx_course_pattern_type", "course_id", "pattern_type"),
    )


# ── Database connection management ─────────────────────────────────────────────

_engine = None
_session_factory = None


def _get_default_db_path() -> Path:
    """Get the default database path from configuration or fall back to repo root.

    Returns:
        Absolute path to the database file.
    """
    try:
        import yaml
        from pathlib import Path

        # Try to resolve from repo root relative to this file
        repo_root = Path(__file__).resolve().parents[1]
        config_path = repo_root / "config" / "attendance.yaml"

        if config_path.exists():
            with config_path.open(encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
                db_path = cfg.get("attendance", {}).get("database", {}).get("path", "attendance.db")
                return data_path(db_path)
    except Exception:
        pass

    # Fallback to attendance.db in the data directory
    return data_path("attendance.db")


def init_db(db_path: str | Path | None = None, echo: bool = False) -> None:
    """Initialize the database engine and session factory.

    Args:
        db_path: Path to SQLite database file. If None, uses configuration or default.
        echo: If True, log all SQL statements (for debugging)
    """
    global _engine, _session_factory

    if db_path is None:
        db_path = _get_default_db_path()
    db_path = data_path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Dispose existing engine to prevent connection pool leaks
    if _engine is not None:
        _engine.dispose()

    # SQLite with WAL mode for better concurrency
    db_url = f"sqlite:///{db_path}"
    _engine = create_engine(db_url, echo=echo, connect_args={"check_same_thread": False})

    # Enable WAL mode and other optimizations
    @event.listens_for(_engine, "connect")
    def set_sqlite_pragma(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA cache_size=-10000")  # 10MB cache
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    _session_factory = sessionmaker(bind=_engine)


def get_session() -> Session:
    """Get a new database session."""
    if _session_factory is None:
        init_db()
    return _session_factory()


# Session factory for compatibility with SQLAlchemy patterns
class _SessionLocalFactory:
    """Lazy-initializing SessionLocal factory."""
    def __call__(self):
        if _session_factory is None:
            init_db()
        return _session_factory()

SessionLocal = _SessionLocalFactory()


def create_tables() -> None:
    """Create all tables in the database."""
    if _engine is None:
        init_db()
    Base.metadata.create_all(_engine)


def drop_tables() -> None:
    """Drop all tables from the database (DESTRUCTIVE - use with caution)."""
    if _engine is None:
        init_db()
    Base.metadata.drop_all(_engine)


# ── Helper functions ───────────────────────────────────────────────────────────

def audit_log_action(
    session: Session,
    action: str,
    entity_type: str,
    entity_id: Optional[str] = None,
    old_value: Optional[str] = None,
    new_value: Optional[str] = None,
    user: Optional[str] = None,
    context: Optional[dict] = None,
) -> AuditLog:
    """Create an audit log entry.

    Args:
        session: Database session
        action: Action performed (e.g., "attendance_write", "alias_learn")
        entity_type: Type of entity affected (e.g., "attendance_record", "alias")
        entity_id: ID of affected entity
        old_value: Previous value (for updates)
        new_value: New value
        user: Username who performed the action
        context: Additional context (dict, will be JSON serialized)

    Returns:
        The created AuditLog entry
    """
    context_json = json.dumps(context) if context else None
    log_entry = AuditLog(
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        old_value=old_value,
        new_value=new_value,
        user=user or "system",
        context=context_json,
    )
    session.add(log_entry)
    return log_entry


def get_course_by_key(session: Session, course_key: str) -> Optional[Course]:
    """Get a course by its key (e.g., 'crash', 'main')."""
    return session.query(Course).filter(Course.course_key == course_key, Course.is_active == True).first()


def get_or_create_course(session: Session, course_key: str, name: str, sheet_id: str, tab: str, layout: str = "single") -> Course:
    """Get an existing course or create a new one."""
    course = get_course_by_key(session, course_key)
    if course is None:
        course = Course(
            course_key=course_key,
            name=name,
            sheet_id=sheet_id,
            tab=tab,
            layout=layout,
        )
        session.add(course)
        session.flush()
    return course


def get_user_by_telegram_id(session: Session, telegram_id: int) -> Optional[User]:
    """Get a user by Telegram ID."""
    return session.query(User).filter(User.telegram_id == telegram_id, User.is_active == True).first()


def get_or_create_user(session: Session, username: str, telegram_id: Optional[int] = None, role: str = "viewer") -> User:
    """Get an existing user or create a new one."""
    if telegram_id:
        user = get_user_by_telegram_id(session, telegram_id)
        if user:
            return user
    user = session.query(User).filter(User.username == username).first()
    if user is None:
        user = User(username=username, telegram_id=telegram_id, role=role)
        session.add(user)
        session.flush()
    return user
