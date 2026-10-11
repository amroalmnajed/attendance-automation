"""T8 - review crop: context rows, red box, <=1280 px, temp file cleanup, bbox stored in the queue."""
import asyncio
import hashlib
import os
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from attendance.bot import review
from attendance.bot.crop import MAX_SIDE_PX, _crop_image
from attendance.review_queue import ReviewQueue

UP = "abcdef0123456789abcd"


def _sheet(tmp_path, h=1000, w=2000, name="sheet.jpg"):
    p = tmp_path / name
    cv2.imwrite(str(p), np.full((h, w, 3), 255, np.uint8))
    return str(p)


def _red(img):
    b, g, r = (img[..., i].astype(int) for i in range(3))
    return (r > 200) & (g < 90) & (b < 90)


def _digest(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def test_crop_has_context_full_width_and_a_red_box(tmp_path):
    src = _sheet(tmp_path)
    out = _crop_image(src, [0.50, 0.10, 0.55, 0.90])
    try:
        assert out != src and os.path.exists(out)
        img = cv2.imread(out)
        h, w = img.shape[:2]
        assert max(h, w) <= MAX_SIDE_PX
        # full width kept (2000 -> 1280); height = bbox 50 px + 2x50 above + 2x50 below = 250 px -> 160
        assert w == 1280 and abs(h - 160) <= 3
        mask = _red(img)
        assert mask.sum() > 100
        ys, xs = np.nonzero(mask)
        assert abs(ys.min() - 64) <= 6 and abs(ys.max() - 96) <= 6            # box sits in the middle band
        assert abs(xs.min() - 128) <= 6 and abs(xs.max() - 1152) <= 6          # 0.10 / 0.90 of the width
    finally:
        os.remove(out)


def test_small_images_are_not_upscaled(tmp_path):
    src = _sheet(tmp_path, h=300, w=600)
    out = _crop_image(src, [0.4, 0.1, 0.6, 0.9])
    try:
        assert cv2.imread(out).shape[1] == 600
    finally:
        os.remove(out)


def test_context_is_clamped_at_the_sheet_edges(tmp_path):
    src = _sheet(tmp_path)
    out = _crop_image(src, [0.0, 0.0, 0.05, 1.0])
    try:
        h, w = cv2.imread(out).shape[:2]
        assert h > 0 and w == 1280 and _red(cv2.imread(out)).sum() > 0
    finally:
        os.remove(out)


@pytest.mark.parametrize("bbox", [None, [], [0.1, 0.2, 0.3], [0.5, 0.1, 0.5, 0.9], [0.6, 0.1, 0.4, 0.9],
                                  ["a", "b", "c", "d"], [float("nan"), 0, 1, 1]])
def test_no_or_invalid_bbox_gives_the_whole_sheet_downscaled(tmp_path, bbox):
    src = _sheet(tmp_path, h=1000, w=2000)
    out = _crop_image(src, bbox)
    try:
        assert out != src
        h, w = cv2.imread(out).shape[:2]
        assert (w, h) == (1280, 640) and _red(cv2.imread(out)).sum() == 0
    finally:
        os.remove(out)


def test_original_is_never_modified(tmp_path):
    src = _sheet(tmp_path)
    before = _digest(src)
    os.remove(_crop_image(src, [0.2, 0.1, 0.25, 0.9]))
    assert _digest(src) == before and sorted(p.name for p in tmp_path.iterdir()) == ["sheet.jpg"]


def test_unreadable_or_missing_file_returns_the_original_path(tmp_path):
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not an image")
    assert _crop_image(str(bad), [0.1, 0.1, 0.2, 0.2]) == str(bad)
    assert _crop_image(str(tmp_path / "missing.jpg"), [0.1, 0.1, 0.2, 0.2]) == str(tmp_path / "missing.jpg")


# ─── sender ───────────────────────────────────────────────────────────────────

class _Bot:
    def __init__(self, photo_fails=False):
        self.sent, self.photo_fails = [], photo_fails

    async def send_message(self, **kw):
        self.sent.append(("message", kw))

    async def send_photo(self, **kw):
        if self.photo_fails:
            raise RuntimeError("telegram said no")
        self.sent.append(("photo", kw))


def _run_sender(monkeypatch, image_ref, bbox, bot):
    item = {"item_id": "i1234567890", "raw_name": "Sara", "paper_row_index": 1, "ocr_confidence": 0.8,
            "reason_code": "MATCH_AMBIGUOUS", "image_ref": image_ref, "bounding_box": bbox,
            "top3": [{"roster_name": "Sara Mohamed", "roster_row": 7, "score": 90}]}

    class FakeQueue:
        def __init__(self, store_dir):
            pass

        def list_pending(self, upload_id):
            return [item]

        def _load(self, upload_id):
            return {"items": [item]}
    monkeypatch.setattr(review, "ReviewQueue", FakeQueue)
    made = []
    real = review._crop_image
    monkeypatch.setattr(review, "_crop_image", lambda p, b: made.append(real(p, b)) or made[-1])
    asyncio.run(review._send_next_review_item(5, bot, UP, "rq"))
    return made


def test_temp_crop_is_sent_then_deleted_and_original_kept(monkeypatch, tmp_path):
    src = _sheet(tmp_path)
    bot = _Bot()
    made = _run_sender(monkeypatch, src, [0.5, 0.1, 0.55, 0.9], bot)
    assert bot.sent[0][0] == "photo" and made[0] != src
    assert not os.path.exists(made[0]) and os.path.exists(src)


def test_temp_crop_is_deleted_even_when_sending_the_photo_fails(monkeypatch, tmp_path):
    src = _sheet(tmp_path)
    bot = _Bot(photo_fails=True)
    made = _run_sender(monkeypatch, src, [0.5, 0.1, 0.55, 0.9], bot)
    assert bot.sent[0][0] == "message" and not os.path.exists(made[0]) and os.path.exists(src)


def test_missing_image_falls_back_to_text_only(monkeypatch, tmp_path):
    bot = _Bot()
    _run_sender(monkeypatch, str(tmp_path / "gone.jpg"), [0.5, 0.1, 0.55, 0.9], bot)
    assert bot.sent[0][0] == "message"


# ─── bounding box reaches the queue ───────────────────────────────────────────

def _enqueue(tmp_path, ocr_row):
    from attendance.matcher import MatchDecision, MatchResult, ReasonCode
    q = ReviewQueue(store_dir=str(tmp_path / "rq"))
    mr = MatchResult(decision=MatchDecision.REVIEW, reason=ReasonCode.MATCH_AMBIGUOUS, best=None)
    q.enqueue(upload_id="u1", course="c", session=1, image_ref="x.jpg", ocr_row=ocr_row,
              match_result=mr, raw_mark="P")
    return q.list_pending("u1")[0]


def test_enqueue_stores_the_bounding_box(tmp_path):
    row = SimpleNamespace(row_index=1, raw_name="A", confidence=0.9, bounding_box=[0.1, 0.2, 0.3, 0.4])
    assert _enqueue(tmp_path, row)["bounding_box"] == [0.1, 0.2, 0.3, 0.4]


@pytest.mark.parametrize("box", [None, [0.1], "abc", [0.1, 0.2, 0.3, "x"], object()])
def test_enqueue_tolerates_missing_or_odd_boxes(tmp_path, box):
    row = SimpleNamespace(row_index=1, raw_name="A", confidence=0.9, bounding_box=box)
    assert _enqueue(tmp_path, row)["bounding_box"] is None
