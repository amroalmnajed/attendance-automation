"""T6 - staged progress messages: pipeline stages + the thread-safe bot callback."""
import asyncio
import threading
from types import SimpleNamespace

from attendance import pipeline
from attendance.bot.common import STAGE_TEXT, make_progress_callback
from tests.test_multi_photo_pipeline import FakeProvider, env, r  # noqa: F401  (fixture reuse)


def _provider():
    return FakeProvider({b"photo-one": [r("Ahmed Hassan Ali", "P", 1)], b"photo-two": [r("Sara Mohamed Nour", "L", 1)]})


# ─── pipeline stages ──────────────────────────────────────────────────────────

def test_run_multi_reports_reading_matching_saving_in_order(env, monkeypatch):
    seen = []
    monkeypatch.setattr(pipeline, "build_provider", lambda _c: _provider())
    s = pipeline.run_multi(image_paths=env.photos[:2], course="g10", session_num=3, dry_run=False,
                           config_path=env.cfg_path, review_store_dir=str(env.tmp / "rq"), progress=seen.append)
    assert s.written == 2
    assert seen == ["reading", "matching", "saving"]


def test_run_single_photo_reports_the_same_stages(env, monkeypatch):
    seen = []
    monkeypatch.setattr(pipeline, "build_provider", lambda _c: _provider())
    pipeline.run(image_path=env.photos[0], course="g10", session_num=3, dry_run=False,
                 config_path=env.cfg_path, review_store_dir=str(env.tmp / "rq"), progress=seen.append)
    assert seen == ["reading", "matching", "saving"]


def test_progress_is_optional_and_a_broken_callback_never_breaks_the_run(env, monkeypatch):
    monkeypatch.setattr(pipeline, "build_provider", lambda _c: _provider())
    kw = dict(image_paths=env.photos[:2], course="g10", session_num=3, dry_run=False,
              config_path=env.cfg_path, review_store_dir=str(env.tmp / "rq"))

    def boom(stage):
        raise RuntimeError("telegram down")
    assert pipeline.run_multi(progress=boom, **kw).written == 2


def test_dry_run_still_reports_saving(env, monkeypatch):
    seen = []
    monkeypatch.setattr(pipeline, "build_provider", lambda _c: _provider())
    pipeline.run_multi(image_paths=env.photos[:2], course="g10", session_num=3, dry_run=True,
                       config_path=env.cfg_path, review_store_dir=str(env.tmp / "rq"), progress=seen.append)
    assert seen[-1] == "saving"


# ─── bot callback ─────────────────────────────────────────────────────────────

class _Msg:
    def __init__(self, fail=False):
        self.edits, self.fail = [], fail

    async def edit_text(self, text, **kw):
        if self.fail:
            raise RuntimeError("message is not modified")
        self.edits.append(text)


class _Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def _drive(msg, steps, *, initial="reading", interval=2.0):
    """Run ``steps`` = [(seconds_to_advance, stage)] from a worker thread, like pipeline.run."""
    async def main():
        loop = asyncio.get_running_loop()
        clock = _Clock()
        cb = make_progress_callback(msg, loop, initial=initial, min_interval_s=interval, clock=clock)

        def worker():
            for dt, stage in steps:
                clock.t += dt
                cb(stage)
        t = threading.Thread(target=worker)
        t.start()
        while t.is_alive():
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)                       # let scheduled edits run
    asyncio.run(main())


def test_texts_are_plain_language_without_jargon():
    assert STAGE_TEXT == {"reading": "📷 Reading the photo…", "matching": "🔎 Matching names…",
                          "saving": "✍️ Saving to the sheet…"}
    assert not any(w in " ".join(STAGE_TEXT.values()) for w in ("OpenCV", "Gemini", "OCR"))


def test_edits_happen_on_the_event_loop_in_order():
    msg = _Msg()
    _drive(msg, [(5, "matching"), (5, "saving")])
    assert msg.edits == [STAGE_TEXT["matching"], STAGE_TEXT["saving"]]


def test_initial_stage_text_is_not_resent():
    msg = _Msg()
    _drive(msg, [(5, "reading"), (5, "reading")])
    assert msg.edits == []


def test_stages_faster_than_two_seconds_are_dropped_not_delayed():
    msg = _Msg()
    _drive(msg, [(0.5, "matching"), (0.5, "saving"), (3.0, "saving")])
    assert msg.edits == [STAGE_TEXT["saving"]]            # first two were inside the 2 s window


def test_unknown_stage_and_failing_edit_are_harmless():
    msg = _Msg(fail=True)
    _drive(msg, [(5, "bogus"), (5, "matching")])
    assert msg.edits == []


def test_closed_loop_is_swallowed():
    loop = asyncio.new_event_loop()
    loop.close()
    cb = make_progress_callback(SimpleNamespace(), loop, initial="reading", min_interval_s=0)
    cb("matching")                                           # must not raise
