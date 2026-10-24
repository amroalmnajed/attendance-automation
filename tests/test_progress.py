"""T6 - staged progress messages: workflow stages."""
from attendance import image_workflow
from attendance.config_runtime import STAGE_TEXT
from tests.test_multi_photo_pipeline import FakeProvider, env, r  # noqa: F401  (fixture reuse)


def _provider():
    return FakeProvider({b"photo-one": [r("Ahmed Hassan Ali", "P", 1)], b"photo-two": [r("Sara Mohamed Nour", "L", 1)]})


# ─── workflow stages ──────────────────────────────────────────────────────────

def test_run_multi_reports_reading_matching_saving_in_order(env, monkeypatch):
    seen = []
    monkeypatch.setattr(image_workflow, "build_provider", lambda _c: _provider())
    monkeypatch.setattr(image_workflow, "prepare", lambda b, **kw: b)
    s = image_workflow.run_multi(image_paths=env.photos[:2], course="g10", session_num=3, dry_run=False,
                           config_path=env.cfg_path, review_store_dir=str(env.tmp / "rq"), progress=seen.append)
    assert s.written == 2
    assert seen == ["reading", "matching", "saving"]


def test_run_single_photo_reports_the_same_stages(env, monkeypatch):
    seen = []
    monkeypatch.setattr(image_workflow, "build_provider", lambda _c: _provider())
    monkeypatch.setattr(image_workflow, "prepare", lambda b, **kw: b)
    image_workflow.run(image_path=env.photos[0], course="g10", session_num=3, dry_run=False,
                 config_path=env.cfg_path, review_store_dir=str(env.tmp / "rq"), progress=seen.append)
    assert seen == ["reading", "matching", "saving"]


def test_progress_is_optional_and_a_broken_callback_never_breaks_the_run(env, monkeypatch):
    monkeypatch.setattr(image_workflow, "build_provider", lambda _c: _provider())
    monkeypatch.setattr(image_workflow, "prepare", lambda b, **kw: b)
    kw = dict(image_paths=env.photos[:2], course="g10", session_num=3, dry_run=False,
              config_path=env.cfg_path, review_store_dir=str(env.tmp / "rq"))

    def boom(stage):
        raise RuntimeError("telegram down")
    assert image_workflow.run_multi(progress=boom, **kw).written == 2


def test_dry_run_still_reports_saving(env, monkeypatch):
    seen = []
    monkeypatch.setattr(image_workflow, "build_provider", lambda _c: _provider())
    monkeypatch.setattr(image_workflow, "prepare", lambda b, **kw: b)
    image_workflow.run_multi(image_paths=env.photos[:2], course="g10", session_num=3, dry_run=True,
                       config_path=env.cfg_path, review_store_dir=str(env.tmp / "rq"), progress=seen.append)
    assert seen[-1] == "saving"


def test_texts_are_plain_language_without_jargon():
    assert STAGE_TEXT == {"reading": "📷 Reading the photo…", "matching": "🔎 Matching names…",
                          "saving": "✍️ Saving to the sheet…"}
    assert not any(w in " ".join(STAGE_TEXT.values()) for w in ("OpenCV", "Gemini", "OCR"))
