"""Feature 3 — "Mark missing students absent" (crash / main ONLY)."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from attendance import absent, pipeline, writer
from attendance.review_queue import ReviewQueue

ROSTER = [("Ahmed Hassan Ali", 2), ("Sara Mohamed Nour", 3), ("Omar Khaled Fathy", 4),
          ("Mona Adel Salem", 5), ("Youssef Tarek Ragab", 6)]
ALLOWED = ["Present", "Late", "Absent", "Not complete"]


class FakeWS:
    id = 7

    def __init__(self, col):
        self.col = list(col)
        self.updates = []

    def col_values(self, _idx):
        return list(self.col)

    def batch_update(self, updates, value_input_option=None):
        self.updates.extend(updates)
        for u in updates:                       # keep the fake sheet in sync like the real one
            row = int("".join(ch for ch in u["range"] if ch.isdigit()))
            while len(self.col) < row:
                self.col.append("")
            self.col[row - 1] = u["values"][0][0]


def fake_session(ws, course="crash", roster=None, allowed=None):
    return SimpleNamespace(
        course=course, session_num=3, session_col="E", type_col=None,
        roster=roster or ROSTER, allowed_attendance=allowed or ALLOWED,
        allowed_types=None, sheet_id="sheet123", tab="Tab", get_worksheet=lambda: ws,
    )


@pytest.fixture
def audit(monkeypatch):
    records = []
    monkeypatch.setattr(writer, "audit_write", lambda **kw: records.append(kw))
    return records


@pytest.fixture
def cfg(tmp_path):
    c = yaml.safe_load(Path("config/attendance.yaml").read_text(encoding="utf-8"))
    c["attendance"]["match"]["aliases_file"] = str(tmp_path / "aliases.json")
    c["attendance"]["match"]["learn_aliases"] = False
    c["attendance"]["review"]["local_dir"] = str(tmp_path / "rq")
    return c


def _pending(cfg, tmp_path, *, course="crash", dry_run=False, matched=(2, 3, 4), candidates=(), upload_id="up1234567890abcd"):
    return absent.record_pending(
        cfg=cfg, review_dir=tmp_path / "rq", upload_id=upload_id, course=course, session_num=3,
        session_type=None, dry_run=dry_run, roster=ROSTER, col_data=["", "", "", "", ""],
        matched_rows=set(matched), candidate_rows=set(candidates),
    )


# ─── Who is offered ───────────────────────────────────────────────────────────

def test_default_settings_offer_crash_and_main_only(cfg):
    assert absent.is_offered(cfg, "crash") and absent.is_offered(cfg, "main")
    assert not absent.is_offered(cfg, "g9") and not absent.is_offered(cfg, "g10")


def test_g9_and_g10_can_never_be_enabled_by_config(cfg):
    cfg["attendance"]["absent_button"]["courses"] = ["crash", "main", "g9", "g10"]
    assert not absent.is_offered(cfg, "g9") and not absent.is_offered(cfg, "g10")
    assert absent.is_offered(cfg, "crash")


def test_disabled_in_config(cfg):
    cfg["attendance"]["absent_button"]["enabled"] = False
    assert not absent.is_offered(cfg, "crash")


def test_no_offer_and_no_file_for_g10(cfg, tmp_path):
    assert _pending(cfg, tmp_path, course="g10") == (False, 0, 0)
    assert not (tmp_path / "rq" / "_absent").exists()


def test_offer_counts_students_not_on_the_sheet(cfg, tmp_path):
    offer, missing, held = _pending(cfg, tmp_path, matched=(2, 3, 4))
    assert (offer, missing, held) == (True, 2, 0)
    rec = json.loads((tmp_path / "rq" / "_absent" / "up1234567890abcd.json").read_text(encoding="utf-8"))
    assert "Ahmed" not in json.dumps(rec)                     # row numbers only, never names


def test_students_a_review_item_might_be_are_held_back(cfg, tmp_path):
    assert _pending(cfg, tmp_path, matched=(2, 3, 4), candidates=(5,)) == (True, 1, 1)


def test_no_offer_when_too_few_students_matched(cfg, tmp_path):
    assert _pending(cfg, tmp_path, matched=(2,))[0] is False   # garbled upload: never offer "mark 4 absent"


def test_no_offer_when_everyone_was_present(cfg, tmp_path):
    assert _pending(cfg, tmp_path, matched=(2, 3, 4, 5, 6))[0] is False


# ─── The button action ────────────────────────────────────────────────────────

def test_real_write_marks_only_missing_students_and_is_audited(cfg, tmp_path, audit):
    _pending(cfg, tmp_path, matched=(2, 3, 4))
    ws = FakeWS(["", "Present", "Present", "Present", "", ""])
    res = absent.mark_missing_absent("up1234567890abcd", cfg=cfg, review_dir=tmp_path / "rq",
                                     session_info=fake_session(ws))
    assert res.ok and res.marked == 2 and not res.dry_run
    assert sorted(u["range"] for u in ws.updates) == ["E5", "E6"]
    assert all(u["values"] == [["Absent"]] for u in ws.updates)
    # audited under its own id, so /revert can undo exactly these writes
    assert {r["upload_id"] for r in audit} == {res.abs_id} and res.abs_id == "aup1234567890abc"
    assert sorted(r["cell"] for r in audit) == ["E5", "E6"]


def test_overwrite_refuse_never_touches_a_filled_cell(cfg, tmp_path, audit):
    _pending(cfg, tmp_path, matched=(2, 3, 4))
    # Mona (row 5) got a value in the meantime (e.g. a reviewer filled it). Must stay as-is.
    ws = FakeWS(["", "Present", "Present", "Present", "Late", ""])
    res = absent.mark_missing_absent("up1234567890abcd", cfg=cfg, review_dir=tmp_path / "rq",
                                     session_info=fake_session(ws))
    assert res.marked == 1 and res.already_filled == 1
    assert [u["range"] for u in ws.updates] == ["E6"]
    assert ws.col[4] == "Late"


def test_tapping_twice_does_not_write_again(cfg, tmp_path, audit):
    _pending(cfg, tmp_path, matched=(2, 3, 4))
    ws = FakeWS(["", "Present", "Present", "Present", "", ""])
    kw = dict(cfg=cfg, review_dir=tmp_path / "rq", session_info=fake_session(ws))
    absent.mark_missing_absent("up1234567890abcd", **kw)
    n_updates = len(ws.updates)
    again = absent.mark_missing_absent("up1234567890abcd", **kw)
    assert not again.ok and again.reason == "nothing_to_mark" and len(ws.updates) == n_updates


def test_dry_run_upload_only_previews(cfg, tmp_path, audit):
    _pending(cfg, tmp_path, dry_run=True, matched=(2, 3, 4), candidates=(5,))
    ws = FakeWS(["", "", "", "", "", ""])       # a dry-run upload wrote nothing
    res = absent.mark_missing_absent("up1234567890abcd", cfg=cfg, review_dir=tmp_path / "rq",
                                     session_info=fake_session(ws))
    assert res.ok and res.dry_run and res.marked == 1       # candidate row 5 still held back
    assert ws.updates == [] and audit == []


def test_refused_while_review_items_are_pending(cfg, tmp_path, audit):
    _pending(cfg, tmp_path, matched=(2, 3, 4))
    q = ReviewQueue(store_dir=str(tmp_path / "rq"))
    from attendance.matcher import MatchDecision, MatchResult, ReasonCode
    from attendance.ocr import OcrRow
    q.enqueue(upload_id="up1234567890abcd", course="crash", session=3, image_ref="x.jpg",
              ocr_row=OcrRow("Mona", "P", 0.5, 4),
              match_result=MatchResult(MatchDecision.REVIEW, ReasonCode.MATCH_LOW_SCORE, None),
              raw_mark="P")
    ws = FakeWS(["", "Present", "Present", "Present", "", ""])
    res = absent.mark_missing_absent("up1234567890abcd", cfg=cfg, review_dir=tmp_path / "rq",
                                     session_info=fake_session(ws))
    assert not res.ok and res.reason == "pending_review" and res.pending_review == 1 and ws.updates == []


def test_expired_or_unknown_upload(cfg, tmp_path):
    res = absent.mark_missing_absent("nope", cfg=cfg, review_dir=tmp_path / "rq", session_info=fake_session(FakeWS([])))
    assert not res.ok and res.reason == "expired"


def test_turned_off_after_the_offer_was_made(cfg, tmp_path):
    _pending(cfg, tmp_path)
    cfg["attendance"]["absent_button"]["enabled"] = False
    res = absent.mark_missing_absent("up1234567890abcd", cfg=cfg, review_dir=tmp_path / "rq", session_info=fake_session(FakeWS([])))
    assert res.reason == "disabled"


def test_sheet_without_an_absent_option_writes_nothing(cfg, tmp_path, audit):
    _pending(cfg, tmp_path)
    ws = FakeWS(["", "Present", "Present", "Present", "", ""])
    res = absent.mark_missing_absent("up1234567890abcd", cfg=cfg, review_dir=tmp_path / "rq",
                                     session_info=fake_session(ws, allowed=["Present", "Late"]))
    assert res.reason == "value_not_allowed" and ws.updates == []


def test_uses_the_sheets_own_spelling_of_absent(cfg, tmp_path, audit):
    _pending(cfg, tmp_path, matched=(2, 3, 4))
    ws = FakeWS(["", "Present", "Present", "Present", "", ""])
    absent.mark_missing_absent("up1234567890abcd", cfg=cfg, review_dir=tmp_path / "rq",
                               session_info=fake_session(ws, allowed=["Present", "ABSENT"]))
    assert ws.updates[0]["values"] == [["ABSENT"]]


# ─── End to end through the real pipeline (fake sheet) ────────────────────────

def _e2e(monkeypatch, cfg, tmp_path, course, names, col):
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    ws = FakeWS(col)
    monkeypatch.setattr(pipeline, "resolve", lambda **_k: fake_session(ws, course=course))
    monkeypatch.setattr(writer, "audit_write", lambda **_k: None)
    summary = pipeline.run_text(names=names, course=course, session_num=3, dry_run=False,
                                config_path=str(cfg_path), review_store_dir=str(tmp_path / "rq"))
    return summary, ws


def test_pipeline_offers_the_button_for_crash(monkeypatch, cfg, tmp_path):
    s, ws = _e2e(monkeypatch, cfg, tmp_path, "crash",
                 ["Ahmed Hassan Ali", "Sara Mohamed Nour", "Omar Khaled Fathy"], [""] * 6)
    assert s.matched == 3 and s.written == 3
    assert s.absent_offer and s.missing_count == 2
    assert "Not on sheet" in s.text() and "Ahmed" not in json.dumps(s.__dict__)   # names never on the summary
    assert absent.load_pending(tmp_path / "rq", s.upload_id)["course"] == "crash"
    # offering the button must not itself write anything extra
    assert sorted(u["range"] for u in ws.updates) == ["E2", "E3", "E4"]


def test_pipeline_never_offers_the_button_for_g10(monkeypatch, cfg, tmp_path):
    s, _ = _e2e(monkeypatch, cfg, tmp_path, "g10",
                ["Ahmed Hassan Ali", "Sara Mohamed Nour", "Omar Khaled Fathy"], [""] * 6)
    assert s.matched == 3 and not s.absent_offer and s.missing_count == 0
    assert "Not on sheet" not in s.text()
    assert not (tmp_path / "rq" / "_absent").exists()
