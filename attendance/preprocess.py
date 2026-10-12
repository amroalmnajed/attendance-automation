"""attendance/preprocess.py — Image pre-processing before OCR.

Steps:
  1. Load image, EXIF-rotate to upright
  2. Deskew (find dominant text angle via Hough or minAreaRect on thresholded image)
  3. Mild contrast normalisation (CLAHE on L channel of LAB)
  4. Resize so the long side ≤ max_side_px (default 2000)
  5. Strip all EXIF metadata (GPS, device info)

Returns a JPEG bytes object ready to be sent to the OCR provider.
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Union

import cv2
import numpy as np
from PIL import Image, ImageOps

from core.logger import get_logger

log = get_logger("preprocess")


# ─── Public API ───────────────────────────────────────────────────────────────

def prepare(
    image_input: Union[str, Path, bytes],
    *,
    orient: bool = True,
    deskew: bool = True,
    contrast: str = "mild",   # "none" | "mild"
    max_side_px: int = 2000,
    strip_exif: bool = True,
) -> bytes:
    """Pre-process an attendance sheet image and return clean JPEG bytes."""
    # Load
    if isinstance(image_input, (str, Path)):
        img_bytes = Path(image_input).read_bytes()
    else:
        img_bytes = image_input

    pil_img = Image.open(io.BytesIO(img_bytes))

    # 1. EXIF orientation fix
    if orient:
        pil_img = _exif_transpose(pil_img)

    # Convert to OpenCV BGR for geometric corrections
    cv_img = _pil_to_cv(pil_img)

    # 2. Deskew
    if deskew:
        cv_img = _deskew(cv_img)

    # 3. Contrast normalisation
    if contrast == "mild":
        cv_img = _clahe_normalise(cv_img)

    # 4. Resize
    cv_img = _resize(cv_img, max_side_px)

    # 5. Back to PIL, drop all metadata
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


def _deskew(img: np.ndarray) -> np.ndarray:
    """Detect and correct skew angle using minAreaRect on thresholded contours."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    # Threshold to isolate dark ink
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    # Find all non-zero pixel coordinates
    coords = np.column_stack(np.where(thresh > 0))
    if len(coords) < 10:
        return img  # not enough content to estimate angle

    angle = cv2.minAreaRect(coords)[-1]
    # minAreaRect returns angles in [-90, 0); convert to a small deskew rotation
    if angle < -45:
        angle = -(90 + angle)
    else:
        angle = -angle

    # Only correct if the tilt is meaningful (> 0.3°) but not extreme (> 15°)
    if abs(angle) < 0.3 or abs(angle) > 15:
        return img

    h, w = img.shape[:2]
    center = (w // 2, h // 2)
    M = cv2.getRotationMatrix2D(center, angle, 1.0)
    rotated = cv2.warpAffine(
        img, M, (w, h),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )
    log.debug("deskew_applied", extra={"angle_deg": round(angle, 2)})
    return rotated


def _clahe_normalise(img: np.ndarray) -> np.ndarray:
    """Mild contrast enhancement via CLAHE on the L channel (LAB colour space)."""
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    l_ch, a_ch, b_ch = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    l_eq = clahe.apply(l_ch)
    lab_eq = cv2.merge([l_eq, a_ch, b_ch])
    return cv2.cvtColor(lab_eq, cv2.COLOR_LAB2BGR)


def _resize(img: np.ndarray, max_side: int) -> np.ndarray:
    h, w = img.shape[:2]
    longest = max(h, w)
    if longest <= max_side:
        return img
    scale = max_side / longest
    new_w, new_h = int(w * scale), int(h * scale)
    return cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
