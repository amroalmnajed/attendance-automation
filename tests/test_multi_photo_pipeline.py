"""Feature 10 — run_multi: several photos, one upload."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from attendance import pipeline, writer
from attendance.ocr import OcrRow
from tests.test_absent import ALLOWED, ROSTER, FakeWS, fake_session


class FakeProvider:
    """Returns canned rows keyed by the (identity-preprocessed) image bytes."""
    def __init__(self, by_bytes, fail_on=None):
        self.by_bytes, self.fail_on, self.calls = by_bytes, fail_on, 0

    def read(self, image_bytes, roster=None):
        self.calls += 1
        if image_bytes == self.fail_on:
            raise RuntimeError("quota exhausted")
        return [OcrRow(**r) for r in self.by_bytes[image_bytes]]


def r(name, mark, idx, conf=0.95):
    return dict(raw_name=name, status_mark=mark, confidence=conf, row_index=idx)


@pytest.fixture
def env(monkeypatch, tmp_path):
    cfg = yaml.safe_load(Path("config/attendance.yaml").read_text(encoding="utf-8"))
    cfg["attendance"]["match"]["aliases_file"] = str(tmp_path / "aliases.json")
    cfg["attendance"]["match"]["learn_aliases"] = False
    cfg["attendance"]["ocr"]["passes"] = 1
    cfg["attendance"]["ocr"]["send_roster_to_provider"] = False
    cfg["attendance"]["review"]["local_dir"] = str(tmp_path / "rq")
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")

    ws = FakeWS([""] * 6)
    monkeypatch.setattr(pipeline, "resolve", lambda **_k: fake_session(ws, course="g10"))
    monkeypatch.setattr(pipeline, "prepare", lambda b, **_k: b)
    monkeypatch.setattr(writer, "audit_write", lambda **_k: None)

    photos = []
    for n, content in enumerate([b"photo-one", b"photo-two", b"photo-three"], start=1):
        p = tmp_path / f"p{n}.jpg"
        p.write_bytes(content)
        photos.append(str(p))
    return SimpleNamespace(cfg_path=str(cfg_path), ws=ws, photos=photos, tmp=tmp_path)


def go(env, monkeypatch, provider, paths):
    monkeypatch.setattr(pipeline, "build_provider", lambda _cfg: provider)
    return pipeline.run_multi(image_paths=paths, course="g10", session_num=3, dry_run=False,
                              config_path=env.cfg_path, review_store_dir=str(env.tmp / "rq"))


def test_rows_from_all_photos_are_written_in_one_upload(env, monkeypatch):
    prov = FakeProvider({
        b"photo-one": [r("Ahmed Hassan Ali", "P", 1), r("Sara Mohamed Nour", "L", 2)],
        b"photo-two": [r("Omar Khaled Fathy", "P", 1)],
    })
    s = go(env, monkeypatch, prov, env.photos[:2])
    assert s.photo_count == 2 and s.matched == 3 and s.written == 3 and s.flagged == 0
    assert {u["range"]: u["values"][0][0] for u in env.ws.updates} == {"E2": "Present", "E3": "Late", "E4": "Present"}
    assert "Photos:" in s.text()


def test_same_student_on_two_photos_is_written_once(env, monkeypatch):
    prov = FakeProvider({
        b"photo-one": [r("Ahmed Hassan Ali", "P", 1)],
        b"photo-two": [r("Ahmed Hassan Ali", "P", 5), r("Sara Mohamed Nour", "P", 6)],
    })
    s = go(env, monkeypatch, prov, env.photos[:2])
    assert s.matched == 2 and s.flagged == 0
    assert sorted(u["range"] for u in env.ws.updates) == ["E2", "E3"]


def test_conflicting_marks_go_to_review_and_are_not_written(env, monkeypatch):
    prov = FakeProvider({
        b"photo-one": [r("Ahmed Hassan Ali", "P", 1), r("Sara Mohamed Nour", "P", 2)],
        b"photo-two": [r("Ahmed Hassan Ali", "A", 3)],
    })
    s = go(env, monkeypatch, prov, env.photos[:2])
    assert s.flagged == 1 and s.matched == 1
    assert [u["range"] for u in env.ws.updates] == ["E3"]                       # Ahmed not written
    data = json.loads((env.tmp / "rq" / f"{s.upload_id}.json").read_text(encoding="utf-8"))
    item = data["items"][0]
    assert item["reason_code"] == "PHOTO_MARK_CONFLICT"
    assert item["raw_mark"] == "Present / Absent"
    assert data["upload_id"] == s.upload_id                                      # one upload id for all photos


def test_review_items_point_at_their_own_photo(env, monkeypatch):
    prov = FakeProvider({
        b"photo-one": [r("Ahmed Hassan Ali", "P", 1)],
        b"photo-two": [r("Zzzz Unknownperson", "P", 1)],
    })
    s = go(env, monkeypatch, prov, env.photos[:2])
    item = json.loads((env.tmp / "rq" / f"{s.upload_id}.json").read_text(encoding="utf-8"))["items"][0]
    assert item["image_ref"] == env.photos[1] and item["photo_index"] == 2


def test_one_unreadable_photo_aborts_everything(env, monkeypatch):
    prov = FakeProvider({b"photo-one": [r("Ahmed Hassan Ali", "P", 1)], b"photo-two": []}, fail_on=b"photo-two")
    s = go(env, monkeypatch, prov, env.photos[:2])
    assert s.errors and "photo 2 of 2" in s.errors[0]
    assert env.ws.updates == []          # nothing written: a missing photo would make its students look absent
    assert not s.absent_offer


def test_missing_file_is_reported_not_raised(env, monkeypatch):
    s = go(env, monkeypatch, FakeProvider({}), [env.photos[0], str(env.tmp / "gone.jpg")])
    assert s.errors and "not found" in s.errors[0]


def test_a_single_photo_behaves_exactly_like_run(env, monkeypatch):
    called = {}
    monkeypatch.setattr(pipeline, "run", lambda **kw: called.update(kw) or "single")
    assert pipeline.run_multi(image_paths=[env.photos[0]], course="g10", session_num=3) == "single"
    assert called["image_path"] == env.photos[0]


def test_empty_list_is_rejected():
    with pytest.raises(ValueError):
        pipeline.run_multi(image_paths=[], course="g10", session_num=3)
