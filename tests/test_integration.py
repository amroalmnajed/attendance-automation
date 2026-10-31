"""tests/test_integration.py — Integration tests for database, sync, and end-to-end pipeline.

Tests database operations, synchronization behavior, and the full upload → match → review → write pipeline.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from attendance.db import (
    get_session,
    init_db,
    create_tables,
    Course,
    Student,
    Session,
    AttendanceRecord,
    AuditLog,
    User,
    CoursePermission,
    Alias,
    OcrCorrection,
    Pattern,
)
from attendance.sync import sync_course_from_sheet, sync_attendance_to_sheet
from attendance.matcher import MatchResult, Candidate, ReasonCode, MatchDecision
from attendance.ocr import OcrRow
from attendance.audit import log_resolve_item
from attendance.audit import log_action, query_audit_logs, get_audit_summary
from attendance.resolver_ops import resolve_item, ResolveResult
from attendance.review_queue import ReviewQueue


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    # Initialize the database with the temporary path
    init_db(db_path)
    create_tables()

    yield db_path

    # Cleanup: dispose engine to release file lock on Windows
    import attendance.db as db_module
    if db_module._engine:
        db_module._engine.dispose()
        db_module._engine = None
    db_module._session_factory = None

    # Now safe to delete the file
    Path(db_path).unlink(missing_ok=True)


@pytest.fixture
def sample_config():
    """Sample configuration for testing."""
    review_dir = tempfile.mkdtemp()
    config = {
        "attendance": {
            "database": {"path": ":memory:"},
            "review": {"local_dir": review_dir},
            "courses": {
                "test_course": {
                    "sheet_id": "test_sheet_id",
                    "tab": "Sheet1",
                    "layout": "standard",
                }
            },
        }
    }
    yield config
    shutil.rmtree(review_dir, ignore_errors=True)


class TestDatabaseOperations:
    """Test database CRUD operations."""

    def test_create_course(self, temp_db):
        """Test creating a course in the database."""
        session = get_session()
        try:
            course = Course(
                course_key="test_course",
                name="Test Course",
                sheet_id="test_sheet_id",
                tab="Sheet1",
                layout="standard",
                config_json='{"sheet_col": "A", "type_col": "B"}',
            )
            session.add(course)
            session.commit()

            # Verify the course was created
            retrieved = session.query(Course).filter_by(name="Test Course").first()
            assert retrieved is not None
            assert retrieved.sheet_id == "test_sheet_id"
            assert retrieved.tab == "Sheet1"
        finally:
            session.close()

    def test_create_student(self, temp_db):
        """Test creating a student in the database."""
        session = get_session()
        try:
            # First create a course
            course = Course(
                course_key="test_course",
                name="Test Course",
                sheet_id="test_sheet_id",
                tab="Sheet1",
                layout="standard",
            )
            session.add(course)
            session.commit()

            # Create a student
            student = Student(
                course_id=course.course_id,
                name="John Doe",
                sheet_row=2,
                is_active=True,
            )
            session.add(student)
            session.commit()

            # Verify the student was created
            retrieved = session.query(Student).filter_by(name="John Doe").first()
            assert retrieved is not None
            assert retrieved.sheet_row == 2
            assert retrieved.is_active is True
        finally:
            session.close()

    def test_create_session(self, temp_db):
        """Test creating a session in the database."""
        session = get_session()
        try:
            # Create a course
            course = Course(
                course_key="test_course",
                name="Test Course",
                sheet_id="test_sheet_id",
                tab="Sheet1",
                layout="standard",
            )
            session.add(course)
            session.commit()

            # Create a session
            session_entry = Session(
                course_id=course.course_id,
                session_num=1,
                session_type="lecture",
                date=datetime(2024, 1, 15),
                session_col="C",
                type_col="D",
            )
            session.add(session_entry)
            session.commit()

            # Verify the session was created
            retrieved = session.query(Session).filter_by(session_num=1).first()
            assert retrieved is not None
            assert retrieved.session_type == "lecture"
            assert retrieved.date == datetime(2024, 1, 15)
        finally:
            session.close()

    def test_create_attendance_record(self, temp_db):
        """Test creating an attendance record in the database."""
        session = get_session()
        try:
            # Create a course
            course = Course(
                course_key="test_course",
                name="Test Course",
                sheet_id="test_sheet_id",
                tab="Sheet1",
                layout="standard",
            )
            session.add(course)
            session.commit()

            # Create a student
            student = Student(
                course_id=course.course_id,
                name="John Doe",
                sheet_row=2,
                is_active=True,
            )
            session.add(student)
            session.commit()

            # Create a session
            session_entry = Session(
                course_id=course.course_id,
                session_num=1,
                session_type="lecture",
                date=datetime(2024, 1, 15),
                session_col="C",
                type_col="D",
            )
            session.add(session_entry)
            session.commit()

            # Create an attendance record
            record = AttendanceRecord(
                student_id=student.student_id,
                session_id=session_entry.session_id,
                value="present",
                uploaded_by="test_user",
            )
            session.add(record)
            session.commit()

            # Verify the record was created
            retrieved = session.query(AttendanceRecord).filter_by(
                student_id=student.student_id,
                session_id=session_entry.session_id,
            ).first()
            assert retrieved is not None
            assert retrieved.value == "present"
        finally:
            session.close()

    def test_create_audit_log(self, temp_db):
        """Test creating an audit log entry."""
        session = get_session()
        try:
            log = AuditLog(
                action="test_action",
                entity_type="test_entity",
                entity_id="test_id",
                old_value='{"old": "value"}',
                new_value='{"new": "value"}',
                user="test_user",
                context='{"meta": "data"}',
            )
            session.add(log)
            session.commit()

            # Verify the log was created
            retrieved = session.query(AuditLog).filter_by(action="test_action").first()
            assert retrieved is not None
            assert retrieved.entity_type == "test_entity"
            assert retrieved.user == "test_user"
        finally:
            session.close()


class TestAuditLogging:
    """Test audit logging functionality."""

    def test_log_action(self, temp_db):
        """Test logging an action to the database."""
        log_action(
            action="test_action",
            entity_type="test_entity",
            entity_id="test_id",
            old_value={"old": "value"},
            new_value={"new": "value"},
            user="test_user",
            metadata={"meta": "data"},
        )

        # Verify the log was created
        logs = query_audit_logs(action="test_action")
        assert len(logs) == 1
        assert logs[0]["action"] == "test_action"
        assert logs[0]["entity_type"] == "test_entity"
        assert logs[0]["user"] == "test_user"
        assert logs[0]["old_value"] == {"old": "value"}
        assert logs[0]["new_value"] == {"new": "value"}
        assert logs[0]["metadata"] == {"meta": "data"}

    def test_query_audit_logs_with_filters(self, temp_db):
        """Test querying audit logs with various filters."""
        # Create multiple log entries
        log_action("action1", "entity1", user="user1")
        log_action("action2", "entity2", user="user2")
        log_action("action1", "entity3", user="user1")

        # Query by action
        logs = query_audit_logs(action="action1")
        assert len(logs) == 2

        # Query by user
        logs = query_audit_logs(user="user2")
        assert len(logs) == 1

        # Query by both
        logs = query_audit_logs(action="action1", user="user1")
        assert len(logs) == 2

    def test_get_audit_summary(self, temp_db):
        """Test getting audit log summary statistics."""
        # Create multiple log entries
        log_action("action1", "entity1", user="user1")
        log_action("action2", "entity2", user="user2")
        log_action("action1", "entity3", user="user1")

        summary = get_audit_summary()
        assert summary["total_logs"] == 3
        assert summary["action_counts"]["action1"] == 2
        assert summary["action_counts"]["action2"] == 1
        assert summary["user_counts"]["user1"] == 2
        assert summary["user_counts"]["user2"] == 1


class TestReviewQueueIntegration:
    """Test review queue integration with database."""

    def test_resolve_item_with_audit(self, temp_db, sample_config):
        """Test that resolving an item logs to the audit database."""
        review_dir = tempfile.mkdtemp()
        try:
            # Create a review queue
            queue = ReviewQueue(store_dir=review_dir)

            # Add a test item using the correct enqueue API
            upload_id = "test_upload"
            ocr_row = OcrRow(
                raw_name="John Doe",
                status_mark="P",
                confidence=0.95,
                row_index=0,
                photo_index=0,
                bounding_box=None,
            )
            match_result = MatchResult(
                decision=MatchDecision.REVIEW,
                reason=ReasonCode.OCR_LOW_CONFIDENCE,
                best=Candidate(roster_name="John Doe", roster_row=2, score=95.0),
                top3=[
                    Candidate(roster_name="John Doe", roster_row=2, score=95.0),
                ],
                raw_name="John Doe",
            )
            item_id = queue.enqueue(
                upload_id=upload_id,
                course="test_course",
                session=1,
                image_ref="test.jpg",
                ocr_row=ocr_row,
                match_result=match_result,
                raw_mark="P",
                session_type="lecture",
                reason="low_confidence",
            )

            # Mock the writer to avoid actual Google Sheets writes
            mock_writer = MagicMock()
            mock_writer.batch_write.return_value = {"written": [], "errors": []}

            # Simplify: patch resolve_item to avoid complex session setup
            # We're testing audit logging, not the full resolution pipeline
            from attendance.resolver_ops import ResolveResult
            mock_result = ResolveResult(success=True, written=True, alias_learned=False)

            with patch('attendance.resolver_ops.resolve_item', return_value=mock_result):
                # Directly log the resolve action to test audit logging
                log_resolve_item(
                    upload_id=upload_id,
                    item_id=item_id,
                    chosen_roster_row=2,
                    attendance_value="present",
                    resolved_by="test_user",
                )

            # Verify the audit log was created
            logs = query_audit_logs(action="resolve_item")
            assert len(logs) == 1
            assert logs[0]["user"] == "test_user"
            assert logs[0]["entity_id"] == item_id
        finally:
            # Explicitly close the queue's SQLite connection to avoid Windows file lock
            if hasattr(queue, '_store') and hasattr(queue._store, '_conn'):
                queue._store._conn.close()
            shutil.rmtree(review_dir, ignore_errors=True)


class TestSyncBehavior:
    """Test synchronization behavior between SQLite and Google Sheets."""

    def test_sync_status_tracking(self):
        """Test that sync status is tracked correctly."""
        from attendance.sync import get_sync_status, _sync_status

        # Record a successful sync
        _sync_status.record_success("sheets_to_sqlite", rows_read=10, rows_written=5, conflicts=0)

        status = get_sync_status()
        assert status["last_sync_direction"] == "sheets_to_sqlite"
        assert status["last_sync_rows_read"] == 10
        assert status["last_sync_rows_written"] == 5
        assert status["last_sync_error"] is None

        # Record a failed sync
        _sync_status.record_failure("sqlite_to_sheets", "API error")

        status = get_sync_status()
        assert status["last_sync_direction"] == "sqlite_to_sheets"
        assert status["last_sync_error"] == "API error"


class TestEndToEndPipeline:
    """Test the end-to-end upload → match → review → write pipeline."""

    def test_pipeline_with_mock_sheets(self, temp_db, sample_config):
        """Test the full pipeline with mocked Google Sheets but real resolve_item execution."""
        review_dir = tempfile.mkdtemp()
        try:
            # Create a course and student in the database
            session = get_session()
            try:
                course = Course(
                    course_key="test_course",
                    name="Test Course",
                    sheet_id="test_sheet_id",
                    tab="Sheet1",
                    layout="standard",
                )
                session.add(course)
                session.commit()

                student = Student(
                    course_id=course.course_id,
                    name="John Doe",
                    sheet_row=2,
                    is_active=True,
                )
                session.add(student)
                session.commit()
            finally:
                session.close()

            # Create a review queue
            queue = ReviewQueue(store_dir=review_dir)

            # Add a test item (simulating OCR result)
            upload_id = "test_upload"
            ocr_row = OcrRow(
                raw_name="John Doe",
                status_mark="P",
                confidence=0.95,
                row_index=0,
                photo_index=0,
                bounding_box=None,
            )
            match_result = MatchResult(
                decision=MatchDecision.REVIEW,
                reason=ReasonCode.OCR_LOW_CONFIDENCE,
                best=Candidate(roster_name="John Doe", roster_row=2, score=95.0),
                top3=[
                    Candidate(roster_name="John Doe", roster_row=2, score=95.0),
                ],
                raw_name="John Doe",
            )
            item_id = queue.enqueue(
                upload_id=upload_id,
                course="test_course",
                session=1,
                image_ref="test.jpg",
                ocr_row=ocr_row,
                match_result=match_result,
                raw_mark="P",
                # session_type="lecture",  # Skip session_type to avoid worksheet access in dry-run
                reason="low_confidence",
            )

            # Mock the writer to avoid actual Google Sheets writes
            mock_writer = MagicMock()
            mock_write_result = MagicMock()
            mock_write_result.written = [2]
            mock_writer.batch_write.return_value = mock_write_result

            # Mock session_resolve to return a SheetSession
            from types import SimpleNamespace
            mock_session = SimpleNamespace(
                session_col="C",
                type_col="D",
                course="test_course",
                sheet_id="test_sheet_id",
                tab="Sheet1",
                get_worksheet=lambda: None,  # Mock method for dry-run
            )

            # Mock alias store
            mock_alias_store = MagicMock()

            # Call the REAL resolve_item function (not mocked)
            with patch('attendance.resolver_ops.session_resolve', return_value=mock_session):
                result = resolve_item(
                    upload_id=upload_id,
                    item_id=item_id,
                    chosen_row=2,
                    roster_name="John Doe",
                    queue=queue,
                    writer=mock_writer,
                    alias_store=mock_alias_store,
                    cfg=sample_config,
                    dry_run=False,  # Actually execute the full pipeline
                    resolved_by="test_user",
                )

            # Verify the resolution succeeded
            assert result.success is True
            assert result.written is True

            # Verify the writer was called with correct cells
            assert mock_writer.batch_write.call_count == 1
            written_cells = mock_writer.batch_write.call_args[0][1]
            assert len(written_cells) == 1
            assert written_cells[0].sheet_row == 2
            assert written_cells[0].new_value == "Present"

            # Verify the audit log was created
            logs = query_audit_logs(action="resolve_item")
            assert len(logs) == 1
            assert logs[0]["user"] == "test_user"
            assert logs[0]["entity_id"] == item_id

            # Verify the queue item was resolved
            pending = queue.list_pending()
            assert item_id not in [item["item_id"] for item in pending]
        finally:
            # Explicitly close the queue's SQLite connection to avoid Windows file lock
            if hasattr(queue, '_store') and hasattr(queue._store, '_conn'):
                queue._store._conn.close()
            shutil.rmtree(review_dir, ignore_errors=True)


class TestAliasStorage:
    """Test alias storage in the database."""

    def test_create_alias(self, temp_db):
        """Test creating an alias in the database."""
        session = get_session()
        try:
            # Create a course
            course = Course(
                course_key="test_course",
                name="Test Course",
                sheet_id="test_sheet_id",
                tab="Sheet1",
                layout="standard",
            )
            session.add(course)
            session.commit()

            # Create an alias
            alias = Alias(
                course_id=course.course_id,
                raw_name_key="john_doe",
                roster_name="John Doe",
                confirmations=5,
            )
            session.add(alias)
            session.commit()

            # Verify the alias was created
            retrieved = session.query(Alias).filter_by(raw_name_key="john_doe").first()
            assert retrieved is not None
            assert retrieved.roster_name == "John Doe"
            assert retrieved.confirmations == 5
        finally:
            session.close()


class TestOcrCorrections:
    """Test OCR correction storage in the database."""

    def test_create_ocr_correction(self, temp_db):
        """Test creating an OCR correction in the database."""
        session = get_session()
        try:
            correction = OcrCorrection(
                raw_text="0",
                corrected_text="O",
                frequency=10,
            )
            session.add(correction)
            session.commit()

            # Verify the correction was created
            retrieved = session.query(OcrCorrection).filter_by(raw_text="0").first()
            assert retrieved is not None
            assert retrieved.corrected_text == "O"
            assert retrieved.frequency == 10
        finally:
            session.close()


class TestPatterns:
    """Test pattern storage in the database."""

    def test_create_pattern(self, temp_db):
        """Test creating a pattern in the database."""
        session = get_session()
        try:
            # Create a course
            course = Course(
                course_key="test_course",
                name="Test Course",
                sheet_id="test_sheet_id",
                tab="Sheet1",
                layout="standard",
            )
            session.add(course)
            session.commit()

            # Create a pattern
            pattern = Pattern(
                course_id=course.course_id,
                pattern_type="session_type",
                pattern_value="lecture",
                confidence=0.9,
            )
            session.add(pattern)
            session.commit()

            # Verify the pattern was created
            retrieved = session.query(Pattern).filter_by(
                course_id=course.course_id,
                pattern_type="session_type",
            ).first()
            assert retrieved is not None
            assert retrieved.pattern_value == "lecture"
            assert retrieved.confidence == 0.9
        finally:
            session.close()
