"""OCR accuracy benchmarks with before/after ML comparison."""

import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

from attendance.matcher import Matcher
from attendance.ml_matcher import MLMatcher
from attendance.ocr_corrections import OCRCorrectionEngine
from attendance.db import init_db, create_tables, get_session, Student, Course


class OCRBenchmarkResult:
    """Container for benchmark results."""

    def __init__(self):
        self.total_names = 0
        self.correct_matches = 0
        self.ml_matches = 0
        self.rapidfuzz_matches = 0
        self.alias_matches = 0
        self.no_matches = 0
        self.ml_match_latency_total = 0.0
        self.rapidfuzz_latency_total = 0.0


@pytest.fixture(scope="module")
def benchmark_db():
    """Create a temporary database for benchmarking."""
    from attendance.db import create_tables
    db_path = tempfile.mktemp(suffix=".db")
    init_db(db_path)
    create_tables()
    yield db_path


@pytest.fixture
def sample_roster(benchmark_db):
    """Create a sample roster for benchmarking."""
    import uuid
    session = get_session()
    try:
        # Create a course with unique key
        course_key = f"benchmark_course_{uuid.uuid4().hex[:8]}"
        course = Course(
            course_key=course_key,
            name="Benchmark Course",
            sheet_id="test_sheet",
            tab="Sheet1",
            layout="standard",
        )
        session.add(course)
        session.commit()
        course_id = course.course_id

        # Create sample students with realistic names
        students = [
            ("Ahmed Mohamed", 1),
            ("Mohamed Ali", 2),
            ("Sara Hassan", 3),
            ("Omar Khalid", 4),
            ("Fatima Ahmed", 5),
            ("Youssef Ibrahim", 6),
            ("Nour Ahmed", 7),
            ("Karim Mahmoud", 8),
            ("Laila Mohamed", 9),
            ("Hassan Ali", 10),
        ]

        for name, row in students:
            student = Student(
                course_id=course_id,
                name=name,
                sheet_row=row,
                is_active=True,
            )
            session.add(student)

        session.commit()

        # Return as list of (name, sheet_row) tuples for matcher
        roster = [(name, row) for name, row in students]
        return roster
    finally:
        session.close()


class TestOCRAccuracyBenchmark:
    """Benchmark OCR accuracy with and without ML matching."""

    def test_benchmark_with_mock_images(self, sample_roster):
        """Benchmark with mock image data simulating real OCR pipeline."""
        from PIL import Image
        import io

        # Create mock image data (simulating real attendance sheet crops)
        mock_images = []
        for i, (name, _) in enumerate(sample_roster):
            # Create a simple white image with text
            img = Image.new('RGB', (200, 50), color='white')
            mock_images.append(img)

        matcher = Matcher()

        # Simulate OCR results with typical errors
        ocr_results = [
            ("Ahmed Mohameed", "Ahmed Mohamed", 0.85),
            ("Mohmmed Ali", "Mohamed Ali", 0.80),
            ("Sara Hassann", "Sara Hassan", 0.82),
            ("Omar Khalidd", "Omar Khalid", 0.84),
            ("Fatima Ahmmed", "Fatima Ahmed", 0.81),
            ("Youssef Ibrahiim", "Youssef Ibrahim", 0.79),
            ("Nour Ahhmed", "Nour Ahmed", 0.83),
            ("Karim Mahmood", "Karim Mahmoud", 0.80),
            ("Laila Mohamad", "Laila Mohamed", 0.82),
            ("Hassan Alli", "Hassan Ali", 0.81),
        ]

        result = OCRBenchmarkResult()
        result.total_names = len(ocr_results)

        start = time.time()
        for i, (raw_name, expected_name, confidence) in enumerate(ocr_results):
            # Simulate preprocessing time on mock image
            img = mock_images[i]
            # In real scenario, this would run OCR on the image
            # For now, we use the pre-defined OCR results

            match_result = matcher.match(
                raw_name=raw_name,
                ocr_confidence=confidence,
                roster=sample_roster,
                paper_row_index=0,
                min_confidence=0.75,
            )

            if match_result.best and match_result.best.roster_name == expected_name:
                result.correct_matches += 1
                result.rapidfuzz_matches += 1
            elif not match_result.best:
                result.no_matches += 1

            result.rapidfuzz_latency_total += time.time() - start
            start = time.time()

        accuracy = (result.correct_matches / result.total_names) * 100
        avg_latency = result.rapidfuzz_latency_total / result.total_names

        print(f"\nMock Image Benchmark Results:")
        print(f"  Total names: {result.total_names}")
        print(f"  Correct matches: {result.correct_matches}")
        print(f"  Accuracy: {accuracy:.2f}%")
        print(f"  Average latency: {avg_latency*1000:.2f}ms")
        print(f"  Note: Using mock images - replace with real labeled test images for production benchmarking")

        # Assert baseline accuracy is reasonable
        assert accuracy >= 50, "Baseline accuracy should be at least 50%"

    def test_rapidfuzz_only_accuracy(self, sample_roster):
        """Benchmark accuracy using only RapidFuzz matching."""
        matcher = Matcher()

        # Simulate OCR results with typical errors
        ocr_results = [
            ("Ahmed Mohameed", "Ahmed Mohamed", 0.85),
            ("Mohmmed Ali", "Mohamed Ali", 0.80),
            ("Sara Hassann", "Sara Hassan", 0.82),
            ("Omar Khalidd", "Omar Khalid", 0.84),
            ("Fatima Ahmmed", "Fatima Ahmed", 0.81),
            ("Youssef Ibrahiim", "Youssef Ibrahim", 0.79),
            ("Nour Ahhmed", "Nour Ahmed", 0.83),
            ("Karim Mahmood", "Karim Mahmoud", 0.80),
            ("Laila Mohamad", "Laila Mohamed", 0.82),
            ("Hassan Alli", "Hassan Ali", 0.81),
        ]

        result = OCRBenchmarkResult()
        result.total_names = len(ocr_results)

        start = time.time()
        for raw_name, expected_name, confidence in ocr_results:
            match_result = matcher.match(
                raw_name=raw_name,
                ocr_confidence=confidence,
                roster=sample_roster,
                paper_row_index=0,
                min_confidence=0.75,
            )

            if match_result.best and match_result.best.roster_name == expected_name:
                result.correct_matches += 1
                result.rapidfuzz_matches += 1
            elif not match_result.best:
                result.no_matches += 1

            result.rapidfuzz_latency_total += time.time() - start
            start = time.time()

        accuracy = (result.correct_matches / result.total_names) * 100
        avg_latency = result.rapidfuzz_latency_total / result.total_names

        print(f"\nRapidFuzz Only Results:")
        print(f"  Total names: {result.total_names}")
        print(f"  Correct matches: {result.correct_matches}")
        print(f"  Accuracy: {accuracy:.2f}%")
        print(f"  Average latency: {avg_latency*1000:.2f}ms")

        # Assert baseline accuracy is reasonable
        assert accuracy >= 50, "RapidFuzz baseline accuracy should be at least 50%"

    def test_hybrid_matching_accuracy(self, sample_roster):
        """Benchmark accuracy using ML matching directly."""
        ml_matcher = MLMatcher()

        # Simulate OCR results with typical errors
        ocr_results = [
            ("Ahmed Mohameed", "Ahmed Mohamed", 0.85),
            ("Mohmmed Ali", "Mohamed Ali", 0.80),
            ("Sara Hassann", "Sara Hassan", 0.82),
            ("Omar Khalidd", "Omar Khalid", 0.84),
            ("Fatima Ahmmed", "Fatima Ahmed", 0.81),
            ("Youssef Ibrahiim", "Youssef Ibrahim", 0.79),
            ("Nour Ahhmed", "Nour Ahmed", 0.83),
            ("Karim Mahmood", "Karim Mahmoud", 0.80),
            ("Laila Mohamad", "Laila Mohamed", 0.82),
            ("Hassan Alli", "Hassan Ali", 0.81),
        ]

        result = OCRBenchmarkResult()
        result.total_names = len(ocr_results)

        # Extract roster names for ML matcher
        roster_names = [name for name, _ in sample_roster]

        start = time.time()
        for raw_name, expected_name, confidence in ocr_results:
            # Try ML matching
            matched_name, score, _ = ml_matcher.match(raw_name, roster_names)

            if matched_name and matched_name == expected_name:
                result.correct_matches += 1
                result.ml_matches += 1
            elif not matched_name:
                result.no_matches += 1

            result.ml_match_latency_total += time.time() - start
            start = time.time()

        accuracy = (result.correct_matches / result.total_names) * 100
        avg_latency = result.ml_match_latency_total / result.total_names

        print(f"\nML Matching Results:")
        print(f"  Total names: {result.total_names}")
        print(f"  Correct matches: {result.correct_matches}")
        print(f"  ML matches: {result.ml_matches}")
        print(f"  No matches: {result.no_matches}")
        print(f"  Accuracy: {accuracy:.2f}%")
        print(f"  Average latency: {avg_latency*1000:.2f}ms")

        # ML matching may have lower accuracy without fine-tuning
        # This is more of a performance test than accuracy test
        print(f"  Note: ML accuracy baseline without fine-tuning")

    def test_ocr_correction_effectiveness(self, sample_roster):
        """Benchmark OCR correction effectiveness."""
        correction_engine = OCRCorrectionEngine(
            min_confidence=0.8,
            min_frequency=3,
            cache_corrections=True,
        )

        # Teach the engine common corrections (lowercase for substring matching)
        corrections = [
            ("ahmmed", "ahmed"),
            ("mohmmed", "mohamed"),
            ("hassann", "hassan"),
            ("khalidd", "khalid"),
            ("ibrahiim", "ibrahim"),
            ("mahmood", "mahmoud"),
            ("mohamad", "mohamed"),
            ("alli", "ali"),
        ]

        for raw, corrected in corrections:
            for _ in range(10):  # Build frequency
                correction_engine.record_correction(raw, corrected)

        # Test correction application with lowercase to match recorded corrections
        test_cases = [
            ("ahmed ahmmed", "ahmed ahmed"),
            ("mohmmed ali", "mohamed ali"),
            ("sara hassann", "sara hassan"),
            ("omar khalidd", "omar khalid"),
            ("youssef ibrahiim", "youssef ibrahim"),
        ]

        corrected_count = 0
        for raw, expected in test_cases:
            corrected = correction_engine.apply_corrections(raw)
            if corrected == expected:
                corrected_count += 1

        effectiveness = (corrected_count / len(test_cases)) * 100

        print(f"\nOCR Correction Results:")
        print(f"  Total test cases: {len(test_cases)}")
        print(f"  Correctly corrected: {corrected_count}")
        print(f"  Effectiveness: {effectiveness:.2f}%")

        assert effectiveness >= 80, "OCR correction effectiveness should be at least 80%"

    def test_performance_comparison(self, sample_roster):
        """Compare performance between RapidFuzz and ML matching."""
        matcher = Matcher()
        ml_matcher = MLMatcher()

        # Pre-load ML model to avoid cold-start bias
        roster_names = [name for name, _ in sample_roster]
        ml_matcher._load_model()

        # Generate embeddings once (caching behavior)
        for name in roster_names:
            ml_matcher.get_embedding(name)

        # Test RapidFuzz only
        ocr_results = [
            ("Ahmed Mohameed", "Ahmed Mohamed", 0.85),
            ("Mohmmed Ali", "Mohamed Ali", 0.80),
            ("Sara Hassann", "Sara Hassan", 0.82),
            ("Omar Khalidd", "Omar Khalid", 0.84),
            ("Fatima Ahmmed", "Fatima Ahmed", 0.81),
            ("Youssef Ibrahiim", "Youssef Ibrahim", 0.79),
            ("Nour Ahhmed", "Nour Ahmed", 0.83),
            ("Karim Mahmood", "Karim Mahmoud", 0.80),
            ("Laila Mohamad", "Laila Mohamed", 0.82),
            ("Hassan Alli", "Hassan Ali", 0.81),
        ] * 10  # Repeat for larger sample

        rapidfuzz_times = []
        for raw_name, _, confidence in ocr_results:
            start = time.time()
            matcher.match(
                raw_name=raw_name,
                ocr_confidence=confidence,
                roster=sample_roster,
                paper_row_index=0,
                min_confidence=0.75,
            )
            rapidfuzz_times.append(time.time() - start)

        # Test ML matching (with cached embeddings)
        ml_times = []
        for raw_name, _, confidence in ocr_results:
            start = time.time()
            ml_matcher.match(raw_name, roster_names)
            ml_times.append(time.time() - start)

        avg_rapidfuzz = sum(rapidfuzz_times) / len(rapidfuzz_times)
        avg_ml = sum(ml_times) / len(ml_times)
        slowdown = (avg_ml / avg_rapidfuzz) * 100

        print(f"\nPerformance Comparison (with caching):")
        print(f"  RapidFuzz average: {avg_rapidfuzz*1000:.2f}ms")
        print(f"  ML average: {avg_ml*1000:.2f}ms")
        print(f"  Slowdown: {slowdown:.1f}%")

        # ML with caching should be reasonably competitive
        # The test is informational - ML is slower but provides semantic matching
        print(f"  Note: ML provides semantic matching benefits beyond pure speed")
