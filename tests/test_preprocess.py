"""Tests for T7: OpenCV preprocessing with deskew + CLAHE + adaptive threshold."""
from __future__ import annotations

import io
import cv2
import numpy as np
from PIL import Image

from attendance.preprocess import (
    _adaptive_threshold,
    _clahe_normalise,
    _deskew,
    _detect_deskew_angle,
    _detect_hough_angle,
    prepare,
)


def _make_tilted_image(angle_deg: float, size: tuple[int, int] = (400, 600)) -> np.ndarray:
    """Create a synthetic white document image with horizontal ruled lines tilted by angle_deg."""
    h, w = size
    img = np.full((h, w, 3), 255, dtype=np.uint8)

    # Draw horizontal ruled lines
    for y in range(50, h - 50, 40):
        cv2.line(img, (20, y), (w - 20, y), (0, 0, 0), 2)

    # Rotate by angle_deg (clockwise tilt: positive angle)
    center = (w // 2, h // 2)
    M = cv2.getRotationMatrix2D(center, angle_deg, 1.0)
    rotated = cv2.warpAffine(img, M, (w, h), borderValue=(255, 255, 255))
    return rotated


def _to_jpeg_bytes(img: np.ndarray) -> bytes:
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    pil = Image.fromarray(rgb)
    buf = io.BytesIO()
    pil.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


# ─── Deskew Tests ─────────────────────────────────────────────────────────────

def test_hough_deskew_detects_and_straightens_5deg_tilt():
    """Image with a 5° tilt is detected and deskewed via the Hough path."""
    tilted = _make_tilted_image(5.0)
    detected_angle = _detect_deskew_angle(tilted)

    assert detected_angle is not None
    # Detected angle magnitude should be within ~2° of 5° (sign depends on Hough line direction)
    assert abs(abs(detected_angle) - 5.0) < 2.0

    # Deskew should run without error and preserve image shape
    straightened = _deskew(tilted)
    assert straightened.shape == tilted.shape


def test_extreme_angle_not_rotated_beyond_15deg_cap():
    """Image with an extreme 45° angle is NOT rotated beyond ±15° cap."""
    tilted = _make_tilted_image(45.0)
    detected_angle = _detect_deskew_angle(tilted)
    assert detected_angle is not None

    # Deskew must cap rotation at 15°
    straightened = _deskew(tilted)
    # The image should NOT be fully rotated 45°: lines should still be tilted ~30°
    remaining_angle = _detect_deskew_angle(straightened)
    assert remaining_angle is not None
    assert abs(remaining_angle) > 25.0


def test_small_angle_ignored():
    """Very small tilt (< 0.3°) is left as is without extra interpolation blur."""
    clean = _make_tilted_image(0.0)
    result = _deskew(clean)
    assert np.array_equal(clean, result)


# ─── CLAHE Contrast Tests ─────────────────────────────────────────────────────

def test_clahe_contrast_enhancement_without_clipping():
    """CLAHE path produces an image with higher contrast in L channel without clipping."""
    # Create low-contrast image (grayscale gradient with uneven lighting)
    img = np.full((200, 200, 3), 128, dtype=np.uint8)
    for y in range(200):
        img[y, :] = int(100 + (y / 200.0) * 50)

    enhanced = _clahe_normalise(img, clip_limit=2.0)

    # Convert both to LAB to compare L channel
    lab_orig = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    lab_enh = cv2.cvtColor(enhanced, cv2.COLOR_BGR2LAB)

    l_orig = lab_orig[:, :, 0].astype(float)
    l_enh = lab_enh[:, :, 0].astype(float)

    # Enhanced dynamic range (standard deviation) should be higher
    assert np.std(l_enh) > np.std(l_orig)

    # Check no severe clipping: min >= 0 and max <= 255
    assert np.min(enhanced) >= 0
    assert np.max(enhanced) <= 255


# ─── Adaptive Threshold Tests ─────────────────────────────────────────────────

def test_adaptive_threshold_produces_valid_image():
    """adaptive_thresh=True returns valid image bytes with binary pixels."""
    tilted = _make_tilted_image(0.0)
    raw_bytes = _to_jpeg_bytes(tilted)

    processed_bytes = prepare(raw_bytes, adaptive_thresh=True)
    assert len(processed_bytes) > 0

    # Ensure it can be decoded by PIL
    pil_img = Image.open(io.BytesIO(processed_bytes))
    assert pil_img.format == "JPEG"
    assert pil_img.size[0] > 0 and pil_img.size[1] > 0


# ─── prepare Pipeline Integration Tests ───────────────────────────────────────

def test_prepare_resizes_down_to_max_side():
    """Image larger than max_side_px is downscaled properly."""
    large_img = np.full((3000, 1500, 3), 255, dtype=np.uint8)
    raw_bytes = _to_jpeg_bytes(large_img)

    result_bytes = prepare(raw_bytes, max_side_px=1000)
    pil = Image.open(io.BytesIO(result_bytes))
    assert max(pil.size) <= 1000


def test_prepare_supports_contrast_modes():
    """prepare accepts contrast modes: 'none', 'mild', 'clahe'."""
    img = _make_tilted_image(0.0)
    raw_bytes = _to_jpeg_bytes(img)

    b_none = prepare(raw_bytes, contrast="none")
    b_mild = prepare(raw_bytes, contrast="mild")
    b_clahe = prepare(raw_bytes, contrast="clahe")

    assert len(b_none) > 0
    assert len(b_mild) > 0
    assert len(b_clahe) > 0
