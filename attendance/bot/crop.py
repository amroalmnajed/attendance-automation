"""attendance/bot/crop.py — the picture shown on a review card.

The flagged row is cut out with context (two row-heights above and below, full sheet width),
outlined with a red box and downscaled so Telegram shows it quickly. Without a usable bounding
box the whole sheet is sent, downscaled. The result is always a NEW temp file (the stored review
image is never touched) or the original path if anything fails, so callers delete the file only
when it differs from the path they passed in.
"""
from __future__ import annotations

import math
import os
import tempfile

import cv2

MAX_SIDE_PX = 1280
CONTEXT_ROW_HEIGHTS = 2.0          # extra rows of context above and below the flagged row
RED = (0, 0, 255)                  # BGR


def _valid_bbox(bbox) -> list[float] | None:
    """[ymin, xmin, ymax, xmax] clamped to 0..1, or None if unusable."""
    try:
        vals = [float(v) for v in bbox]
    except (TypeError, ValueError):
        return None
    if len(vals) != 4 or not all(math.isfinite(v) for v in vals):
        return None
    ymin, xmin, ymax, xmax = (min(1.0, max(0.0, v)) for v in vals)
    return [ymin, xmin, ymax, xmax] if ymax > ymin and xmax > xmin else None


def _scale_for(h: int, w: int) -> float:
    return min(1.0, MAX_SIDE_PX / max(h, w, 1))


def _resize(img, scale: float):
    if scale >= 1.0:
        return img
    h, w = img.shape[:2]
    return cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA)


def _crop_image(img_path: str, bbox) -> str:
    """Path of the picture to send for this review item (a new temp file), or ``img_path`` on failure."""
    try:
        img = cv2.imread(img_path)
        if img is None:
            return img_path
        h, w = img.shape[:2]
        box = _valid_bbox(bbox)
        if box is None:
            out = _resize(img, _scale_for(h, w))               # no usable box: whole sheet, smaller
        else:
            ymin, xmin, ymax, xmax = box
            pad = (ymax - ymin) * CONTEXT_ROW_HEIGHTS
            y1 = max(0, int((ymin - pad) * h))
            y2 = min(h, int(math.ceil((ymax + pad) * h)))
            crop = img[y1:y2, :].copy()                        # full sheet width
            ch, cw = crop.shape[:2]
            if ch < 1 or cw < 1:
                return img_path
            scale = _scale_for(ch, cw)
            thickness = max(2, int(math.ceil(3 / scale)))      # still >= 2 px after downscaling
            top_left = (int(xmin * w), int(ymin * h) - y1)
            bottom_right = (min(w - 1, int(xmax * w)), min(ch - 1, int(ymax * h) - y1))
            cv2.rectangle(crop, top_left, bottom_right, RED, thickness)
            out = _resize(crop, scale)
        fd, tmp = tempfile.mkstemp(prefix="review_crop_", suffix=".jpg")
        os.close(fd)
        if not cv2.imwrite(tmp, out):
            os.remove(tmp)
            return img_path
        return tmp
    except Exception:
        return img_path
