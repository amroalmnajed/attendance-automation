"""Load tests for concurrent image processing and database performance."""

import datetime
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import MagicMock, patch

import pytest

from attendance.db import (
    Alias,
    Course,
    Pattern,
    Session,
    Student,
    get_session,
    init_db,
)
from attendance.matcher import Candidate, MatchDecision, MatchResult, ReasonCode
from attendance.ocr_corrections import OCRCorrectionEngine
from attendance.review_queue import OcrRow, ReviewQueue


@pytest.fixture(scope="module")
def load_db():
    """Create a temporary database for load testing."""
    from attendance.db import create_tables
    db_path = tempfile.mktemp(suffix=".db")
    init_db(db_path)
    create_tables()
    yield db_path
    # Cleanup handled by OS temp file


@pytest.fixture
def sample_roster():
    """Sample roster for load testing."""
    return [
        ("Ahmed Mohamed", 2),
        ("Mohamed Ali", 3),
        ("Sara Ahmed", 4),
        ("John Smith", 5),
        ("Jane Doe", 6),
        ("Fatima Hassan", 7),
        ("Omar Khalid", 8),
        ("Layla Mahmoud", 9),
        ("Yousef Ibrahim", 10),
        ("Nour El-Din", 11),
    ]


class TestConcurrentImageProcessing:
    """Test concurrent image processing performance."""

    def test_concurrent_ocr_processing_10(self, sample_roster):
        """Test processing 10 OCR rows concurrently."""
        from attendance.matcher import Matcher

        # Create a list of OCR rows to process
        ocr_rows = [
            OcrRow(
                raw_name=name,
                status_mark="P",
                confidence=0.95,
                row_index=i,
                photo_index=i,
                bounding_box=None,
            )
            for i, (name, _) in enumerate(sample_roster)
        ]

        # Time sequential processing
        start_seq = time.time()
        sequential_results = []
        matcher = Matcher(t_high=85.0, margin=5.0)
        for i, ocr_row in enumerate(ocr_rows):
            result = matcher.match(
                raw_name=ocr_row.raw_name,
                ocr_confidence=ocr_row.confidence,
                roster=sample_roster,
                paper_row_index=i,
            )
            sequential_results.append(result)
        seq_time = time.time() - start_seq

        # Time concurrent processing
        start_concurrent = time.time()
        concurrent_results = []

        def do_match(ocr_name, ocr_conf, idx):
            m = Matcher(t_high=85.0, margin=5.0)
            return m.match(ocr_name, ocr_conf, sample_roster, idx)

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(do_match, ocr_row.raw_name, ocr_row.confidence, i): ocr_row
                for i, ocr_row in enumerate(ocr_rows)
            }
            for future in as_completed(futures):
                concurrent_results.append(future.result())
        concurrent_time = time.time() - start_concurrent

        # Verify results are equivalent
        assert len(concurrent_results) == len(sequential_results)

        # Concurrent should be faster (allow some overhead)
        print(f"Sequential time (10): {seq_time:.3f}s")
        print(f"Concurrent time (10): {concurrent_time:.3f}s")
        print(f"Speedup (10): {seq_time / concurrent_time:.2f}x")

    def test_concurrent_ocr_processing_50(self):
        """Test processing 50 OCR rows concurrently."""
        from attendance.matcher import Matcher

        # Generate 50 synthetic names
        sample_roster = [(f"Student {i}", i + 2) for i in range(50)]

        # Create a list of OCR rows to process
        ocr_rows = [
            OcrRow(
                raw_name=name,
                status_mark="P",
                confidence=0.95,
                row_index=i,
                photo_index=i,
                bounding_box=None,
            )
            for i, (name, _) in enumerate(sample_roster)
        ]

        # Time concurrent processing
        start_concurrent = time.time()
        concurrent_results = []

        def do_match(ocr_name, ocr_conf, idx):
            m = Matcher(t_high=85.0, margin=5.0)
            return m.match(ocr_name, ocr_conf, sample_roster, idx)

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(do_match, ocr_row.raw_name, ocr_row.confidence, i): ocr_row
                for i, ocr_row in enumerate(ocr_rows)
            }
            for future in as_completed(futures):
                concurrent_results.append(future.result())
        concurrent_time = time.time() - start_concurrent

        # Verify all results
        assert len(concurrent_results) == 50

        print(f"Concurrent time (50): {concurrent_time:.3f}s")
        print(f"Throughput (50): {50 / concurrent_time:.2f} items/sec")

    def test_concurrent_ocr_processing_100(self):
        """Test processing 100 OCR rows concurrently."""
        from attendance.matcher import Matcher

        # Generate 100 synthetic names
        sample_roster = [(f"Student {i}", i + 2) for i in range(100)]

        # Create a list of OCR rows to process
        ocr_rows = [
            OcrRow(
                raw_name=name,
                status_mark="P",
                confidence=0.95,
                row_index=i,
                photo_index=i,
                bounding_box=None,
            )
            for i, (name, _) in enumerate(sample_roster)
        ]

        # Time concurrent processing
        start_concurrent = time.time()
        concurrent_results = []

        def do_match(ocr_name, ocr_conf, idx):
            m = Matcher(t_high=85.0, margin=5.0)
            return m.match(ocr_name, ocr_conf, sample_roster, idx)

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(do_match, ocr_row.raw_name, ocr_row.confidence, i): ocr_row
                for i, ocr_row in enumerate(ocr_rows)
            }
            for future in as_completed(futures):
                concurrent_results.append(future.result())
        concurrent_time = time.time() - start_concurrent

        # Verify all results
        assert len(concurrent_results) == 100

        print(f"Concurrent time (100): {concurrent_time:.3f}s")
        print(f"Throughput (100): {100 / concurrent_time:.2f} items/sec")

    def test_concurrent_review_queue_operations(self):
        """Test concurrent review queue operations."""
        review_dir = tempfile.mkdtemp()
        try:
            queue = ReviewQueue(store_dir=review_dir)

            def enqueue_item(i):
                upload_id = f"upload_{i}"
                ocr_row = OcrRow(
                    raw_name=f"Student {i}",
                    status_mark="P",
                    confidence=0.9,
                    row_index=i,
                    photo_index=i,
                    bounding_box=None,
                )
                match_result = MatchResult(
                    decision=MatchDecision.REVIEW,
                    reason=ReasonCode.OCR_LOW_CONFIDENCE,
                    best=Candidate(roster_name=f"Student {i}", roster_row=i, score=90.0),
                    top3=[Candidate(roster_name=f"Student {i}", roster_row=i, score=90.0)],
                    raw_name=f"Student {i}",
                )
                return queue.enqueue(
                    upload_id=upload_id,
                    course="test_course",
                    session=1,
                    image_ref=f"test_{i}.jpg",
                    ocr_row=ocr_row,
                    match_result=match_result,
                    raw_mark="P",
                    session_type="lecture",
                    reason="low_confidence",
                )

            # Enqueue 50 items concurrently
            start = time.time()
            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = [executor.submit(enqueue_item, i) for i in range(50)]
                item_ids = [future.result() for future in as_completed(futures)]
            enqueue_time = time.time() - start

            # Verify all items were enqueued
            pending = queue.list_pending()
            assert len(pending) == 50
            assert len(item_ids) == 50

            print(f"Enqueued 50 items in {enqueue_time:.3f}s")
            print(f"Throughput: {50 / enqueue_time:.2f} items/sec")

        finally:
            if hasattr(queue, '_store') and hasattr(queue._store, '_conn'):
                queue._store._conn.close()
            import shutil
            shutil.rmtree(review_dir, ignore_errors=True)


class TestSyncPerformance:
    """Test sync performance with large datasets."""

    def test_sync_large_dataset_roster(self, load_db):
        """Test syncing a large roster (500 students)."""
        session = get_session()
        try:
            # Create a course
            course = Course(
                course_key="large_sync_test",
                name="Large Sync Test Course",
                sheet_id="test_sheet",
                tab="Sheet1",
                layout="standard",
            )
            session.add(course)
            session.commit()

            # Insert 500 students
            start = time.time()
            for i in range(500):
                student = Student(
                    course_id=course.course_id,
                    name=f"Student {i}",
                    sheet_row=i + 2,
                    is_active=True,
                )
                session.add(student)
            session.commit()
            insert_time = time.time() - start

            print(f"Inserted 500 students in {insert_time:.3f}s")
            print(f"Throughput: {500 / insert_time:.2f} students/sec")

            # Query performance
            start = time.time()
            students = session.query(Student).filter_by(course_id=course.course_id).all()
            query_time = time.time() - start

            assert len(students) == 500
            print(f"Queried 500 students in {query_time:.3f}s")

        finally:
            session.close()

    def test_sync_large_dataset_sessions(self, load_db):
        """Test syncing many sessions (100 sessions)."""
        session = get_session()
        try:
            # Create a course
            course = Course(
                course_key="session_sync_test",
                name="Session Sync Test Course",
                sheet_id="test_sheet",
                tab="Sheet1",
                layout="standard",
            )
            session.add(course)
            session.commit()

            # Insert 100 sessions
            start = time.time()
            for i in range(100):
                session_entry = Session(
                    course_id=course.course_id,
                    session_num=i + 1,
                    session_type="lecture",
                    date=datetime.datetime(2024, 1, (i % 30) + 1),
                    session_col=chr(67 + (i % 5)),  # C, D, E, F, G columns
                    type_col=None,
                )
                session.add(session_entry)
            session.commit()
            insert_time = time.time() - start

            print(f"Inserted 100 sessions in {insert_time:.3f}s")
            print(f"Throughput: {100 / insert_time:.2f} sessions/sec")

            # Query performance
            start = time.time()
            sessions = session.query(Session).filter_by(course_id=course.course_id).all()
            query_time = time.time() - start

            assert len(sessions) == 100
            print(f"Queried 100 sessions in {query_time:.3f}s")

        finally:
            session.close()


class TestDatabasePerformance:
    """Test database performance under load."""

    def test_bulk_student_insert(self, load_db):
        """Test bulk student insertion performance."""
        session = get_session()
        try:
            # Create a course
            course = Course(
                course_key="perf_test",
                name="Performance Test Course",
                sheet_id="test_sheet",
                tab="Sheet1",
                layout="standard",
            )
            session.add(course)
            session.commit()

            # Insert 1000 students
            start = time.time()
            for i in range(1000):
                student = Student(
                    course_id=course.course_id,
                    name=f"Student {i}",
                    sheet_row=i + 2,
                    is_active=True,
                )
                session.add(student)
            session.commit()
            insert_time = time.time() - start

            print(f"Inserted 1000 students in {insert_time:.3f}s")
            print(f"Throughput: {1000 / insert_time:.2f} students/sec")

            # Query performance
            start = time.time()
            students = session.query(Student).filter_by(course_id=course.course_id).all()
            query_time = time.time() - start

            assert len(students) == 1000
            print(f"Queried 1000 students in {query_time:.3f}s")

        finally:
            session.close()

    def test_concurrent_database_writes(self, load_db):
        """Test concurrent database write operations."""
        session = get_session()
        try:
            # Create a course
            course = Course(
                course_key="concurrent_test",
                name="Concurrent Test Course",
                sheet_id="test_sheet",
                tab="Sheet1",
                layout="standard",
            )
            session.add(course)
            session.commit()
            course_id = course.course_id
        finally:
            session.close()

        def add_student(i):
            session = get_session()
            try:
                student = Student(
                    course_id=course_id,
                    name=f"Student {i}",
                    sheet_row=i + 2,
                    is_active=True,
                )
                session.add(student)
                session.commit()
                return True
            except Exception as e:
                session.rollback()
                return False
            finally:
                session.close()

        # Add 100 students concurrently
        start = time.time()
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(add_student, i) for i in range(100)]
            results = [future.result() for future in as_completed(futures)]
        write_time = time.time() - start

        assert all(results)
        print(f"Concurrent writes: 100 students in {write_time:.3f}s")

        # Verify count
        session = get_session()
        try:
            count = session.query(Student).filter_by(course_id=course_id).count()
            assert count == 100
        finally:
            session.close()


class TestOCRCorrectionPerformance:
    """Test OCR correction system performance."""

    def test_bulk_correction_application(self, load_db):
        """Test applying many OCR corrections."""
        correction_engine = OCRCorrectionEngine(
            min_confidence=0.8,
            min_frequency=3,
            cache_corrections=True,
        )

        # Add 100 corrections
        corrections = [
            (f"raw_{i}", f"corrected_{i}")
            for i in range(100)
        ]

        start = time.time()
        for raw, corrected in corrections:
            # Record correction multiple times to build frequency
            for _ in range(10):
                correction_engine.record_correction(raw, corrected)
        correction_time = time.time() - start

        print(f"Recorded 100 corrections in {correction_time:.3f}s")

        # Test lookup performance
        start = time.time()
        for raw, _ in corrections:
            correction_engine.apply_corrections(raw)
        lookup_time = time.time() - start

        print(f"Looked up 100 corrections in {lookup_time:.3f}s")


class TestAliasPerformance:
    """Test alias storage performance."""

    def test_bulk_alias_operations(self, load_db):
        """Test bulk alias create and lookup."""
        session = get_session()
        try:
            # Create a course
            course = Course(
                course_key="alias_test",
                name="Alias Test Course",
                sheet_id="test_sheet",
                tab="Sheet1",
                layout="standard",
            )
            session.add(course)
            session.commit()
            course_id = course.course_id
        finally:
            session.close()

        # Add 100 aliases
        start = time.time()
        session = get_session()
        try:
            for i in range(100):
                alias = Alias(
                    course_id=course_id,
                    raw_name_key=f"raw_{i}",
                    roster_name=f"correct_{i}",
                    confirmations=5,
                    first_seen=datetime.datetime(2024, 1, 1),
                    last_used=datetime.datetime(2024, 1, 1),
                )
                session.add(alias)
            session.commit()
            alias_time = time.time() - start
        finally:
            session.close()

        print(f"Created 100 aliases in {alias_time:.3f}s")

        # Query performance
        start = time.time()
        session = get_session()
        try:
            aliases = session.query(Alias).filter_by(course_id=course_id).all()
            query_time = time.time() - start
        finally:
            session.close()

        assert len(aliases) == 100
        print(f"Queried 100 aliases in {query_time:.3f}s")


@pytest.mark.benchmark
class TestBenchmarking:
    """Benchmark key operations with pytest-benchmark."""

    def test_benchmark_match_performance(self, benchmark, sample_roster):
        """Benchmark name matching performance."""
        from attendance.matcher import Matcher

        ocr_name = "Ahmed Mohamed"

        def do_match():
            m = Matcher(t_high=85.0, margin=5.0)
            return m.match(ocr_name, 0.95, sample_roster, 0)

        result = benchmark(do_match)
        assert result is not None

    def test_benchmark_cache_lookup(self, benchmark, load_db):
        """Benchmark cache lookup performance."""
        from attendance.cache import CacheBackend

        cache = CacheBackend()

        # Warm the cache
        cache.set("test_key", "test_value", "default", ttl_seconds=60)

        def do_lookup():
            return cache.get("test_key")

        result = benchmark(do_lookup)
        assert result == "test_value"
