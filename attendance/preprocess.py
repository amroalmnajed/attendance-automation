"""attendance/preprocess.py — Image pre-processing before OCR.

Steps:
  1. Load image, EXIF-rotate to upright
  2. Deskew using Hough line transform (detect dominant angle of ruled lines, capped at ±15°)
  3. Contrast normalization via CLAHE on L channel of LAB colour space
  4. Binarization: adaptive thresholding (opt-in)
  5. Resize downsampling to max_side_px
  6. Strip all EXIF metadata (GPS, device info)

Returns a JPEG bytes object ready to be sent to the OCR provider.
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Optional, Union

import cv2
import numpy as np
from PIL import Image, ImageOps

from core.logger import get_logger

log = get_logger("preprocess")


# ─── Public API ───────────────────────────────────────────────────────────────

def prepare(
    raw_bytes: Union[bytes, str, Path] = None,
    *,
    orient: bool = True,
    deskew: bool = True,
    contrast: str = "mild",     # "none" | "mild" | "clahe"
    adaptive_thresh: bool = False,
    max_side_px: int = 2000,
    strip_exif: bool = True,
    image_input: Union[bytes, str, Path] = None,
) -> bytes:
    """Pre-process an attendance sheet image and return clean JPEG bytes.

    Args:
        raw_bytes: Raw image bytes, or path (str/Path).
        orient: Whether to apply EXIF orientation fix.
        deskew: Whether to deskew using Hough line transform (capped at ±15°).
        contrast: Contrast mode ("none", "mild", "clahe").
        adaptive_thresh: Whether to apply adaptive thresholding.
        max_side_px: Maximum dimension for downsampling.
        strip_exif: Whether to remove metadata.
        image_input: Alias for raw_bytes for backward compatibility.

    Returns:
        Clean JPEG bytes ready for OCR.
    """
    input_data = raw_bytes if raw_bytes is not None else image_input
    if input_data is None:
        raise ValueError("raw_bytes or image_input must be provided")

    if isinstance(input_data, (str, Path)):
        img_bytes = Path(input_data).read_bytes()
    else:
        img_bytes = input_data

    pil_img = Image.open(io.BytesIO(img_bytes))

    # 1. EXIF orientation fix
    if orient:
        pil_img = _exif_transpose(pil_img)

    # Convert to OpenCV BGR for geometric corrections
    cv_img = _pil_to_cv(pil_img)

    # 2. Deskew
    if deskew:
        cv_img = _deskew(cv_img)

    # 3. Contrast normalisation via CLAHE in LAB colour space
    if contrast in ("mild", "clahe"):
        clip_limit = 3.0 if contrast == "clahe" else 2.0
        cv_img = _clahe_normalise(cv_img, clip_limit=clip_limit)

    # 4. Binarization: adaptive thresholding (opt-in)
    if adaptive_thresh:
        cv_img = _adaptive_threshold(cv_img)

    # 5. Resize
    cv_img = _resize(cv_img, max_side_px)

    # 6. Back to PIL, drop all metadata
    pil_out = _cv_to_pil(cv_img)
    if strip_exif:
        pil_out = _strip_exif(pil_out)

    # Encode as JPEG
    buf = io.BytesIO()
    pil_out.save(buf, format="JPEG", quality=92)
    result = buf.getvalue()
    log.info(
        "preprocess_done",
        extra={
            "input_bytes": len(img_bytes),
            "output_bytes": len(result),
            "orient": orient,
            "deskew": deskew,
            "contrast": contrast,
            "adaptive_thresh": adaptive_thresh,
        },
    )
    return result


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _exif_transpose(img: Image.Image) -> Image.Image:
    """Rotate image to upright orientation per EXIF tag."""
    try:
        return ImageOps.exif_transpose(img)
    except Exception:
        return img


def _pil_to_cv(img: Image.Image) -> np.ndarray:
    rgb = img.convert("RGB")
    return cv2.cvtColor(np.array(rgb), cv2.COLOR_RGB2BGR)


def _cv_to_pil(img: np.ndarray) -> Image.Image:
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    return Image.fromarray(rgb)


def _strip_exif(img: Image.Image) -> Image.Image:
    """Return a new PIL image with no EXIF/metadata."""
    data = img.tobytes()
    clean = Image.frombytes(img.mode, img.size, data)
    return clean


def _detect_hough_angle(gray: np.ndarray) -> Optional[float]:
    """Detect dominant angle of ruled lines on the sheet using Hough line transform."""
    h, w = gray.shape[:2]
    edges = cv2.Canny(gray, 50, 150, apertureSize=3)
    min_line_len = max(30, w // 8)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=60, minLineLength=min_line_len, maxLineGap=20)
    if lines is None or len(lines) == 0:
        return None

    angles = []
    for line in lines:
        # HoughLinesP returns shape (N,1,4) or (N,4) depending on OpenCV version
        seg = line[0] if line.ndim == 2 else line
        x1, y1, x2, y2 = int(seg[0]), int(seg[1]), int(seg[2]), int(seg[3])
        dx = x2 - x1
        dy = y2 - y1
        if dx == 0 and dy == 0:
            continue
        angle = np.degrees(np.arctan2(dy, dx))
        while angle > 90:
            angle -= 180
        while angle < -90:
            angle += 180
        if abs(angle) <= 45:
            angles.append(angle)

    if not angles:
        return None
    return float(np.median(angles))


def _detect_min_area_angle(gray: np.ndarray) -> Optional[float]:
    """Fallback deskew angle detection using minAreaRect on contours."""
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    coords = np.column_stack(np.where(thresh > 0))
    if len(coords) < 10:
        return None
    angle = cv2.minAreaRect(coords)[-1]
    if angle < -45:
        angle = -(90 + angle)
    else:
        angle = -angle
    return float(angle)


def _detect_deskew_angle(img: np.ndarray) -> Optional[float]:
    """Detect dominant deskew angle in degrees, using Hough lines with minAreaRect fallback."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if len(img.shape) == 3 else img
    hough = _detect_hough_angle(gray)
    if hough is not None:
        return hough
    return _detect_min_area_angle(gray)


def _deskew(img: np.ndarray) -> np.ndarray:
    """Detect dominant angle of ruled lines and rotate to align horizontally.

    Rotation is strictly capped at ±15° so an upside-down or portrait photo
    is never spun onto its side.
    """
    angle = _detect_deskew_angle(img)
    if angle is None:
        return img

    # Cap rotation at ±15°
    capped_angle = float(np.clip(angle, -15.0, 15.0))

    # Only correct if tilt is meaningful (> 0.3°)
    if abs(capped_angle) < 0.3:
        return img

    h, w = img.shape[:2]
    center = (w // 2, h // 2)
    # Negative angle rotates to straighten
    M = cv2.getRotationMatrix2D(center, -capped_angle, 1.0)
    rotated = cv2.warpAffine(
        img, M, (w, h),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )
    log.debug("deskew_applied", extra={"detected_angle": round(angle, 2), "applied_rotation": round(-capped_angle, 2)})
    return rotated


def _clahe_normalise(img: np.ndarray, clip_limit: float = 2.0) -> np.ndarray:
    """Apply CLAHE on the L channel of LAB colour space to handle uneven shadows."""
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    l_ch, a_ch, b_ch = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=clip_limit, tileGridSize=(8, 8))
    l_eq = clahe.apply(l_ch)
    lab_eq = cv2.merge([l_eq, a_ch, b_ch])
    return cv2.cvtColor(lab_eq, cv2.COLOR_LAB2BGR)


def _adaptive_threshold(img: np.ndarray) -> np.ndarray:
    """Apply Gaussian adaptive thresholding for high-contrast binarization."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if len(img.shape) == 3 else img
    thresh = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 25, 10
    )
    return cv2.cvtColor(thresh, cv2.COLOR_GRAY2BGR)


def _resize(img: np.ndarray, max_side: int) -> np.ndarray:
    h, w = img.shape[:2]
    longest = max(h, w)
    if longest <= max_side:
        return img
    scale = max_side / longest
    new_w, new_h = int(w * scale), int(h * scale)
    return cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
