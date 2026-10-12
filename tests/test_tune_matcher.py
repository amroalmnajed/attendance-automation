"""T11 - tune_matcher: reads resolved items, counts only, never touches config."""
import hashlib
import json
from pathlib import Path

from tests import tune_matcher as tm


def _item(raw, expected, row, course="c", status="resolved", top=()):
    it = {"item_id": raw[:8], "status": status, "course": course, "raw_name": raw, "ocr_confidence": 0.9,
          "top3": [{"roster_name": n, "roster_row": i + 2, "score": 70} for i, n in enumerate(top)]}
    if expected is not None:
        it["resolution"] = {"roster_row": row, "roster_name": expected, "attendance_value": "Present"}
    return it


def _dir(tmp_path, items):
    d = tmp_path / "rq"
    d.mkdir()
    (d / "u1.json").write_text(json.dumps({"items": items}), encoding="utf-8")
    return str(d)


ROSTER = ["Zed Quarrington Smith", "Yara Bellweather Jones", "Xavier Ostrander Lee"]


def test_only_real_resolutions_become_cases(tmp_path):
    d = _dir(tmp_path, [
        _item("Zed Quarrington Smith", ROSTER[0], 5, top=ROSTER),
        _item("skipme", "Nobody Here", 0),                 # skipped (row 0)
        _item("absent one", "Nobody Here", 9, status="pending"),
        _item("no resolution", None, 0),
    ])
    cases, rosters = tm.load_cases(d)
    assert len(cases) == 1 and cases[0][3] == ROSTER[0]
    assert sorted(n for n, _ in rosters["c"]) == sorted(ROSTER)


def test_evaluate_counts_wrong_auto_accepts(tmp_path):
    d = _dir(tmp_path, [
        _item("Zed Quarrington Smith", ROSTER[0], 5, top=ROSTER),
        _item("Yara Bellweather Jones", ROSTER[0], 5, top=ROSTER),      # human said Zed: auto-accept would be WRONG
    ])
    cases, rosters = tm.load_cases(d)
    r = tm.evaluate(cases, rosters, t_high=80, margin=5, w_sort=0.5)
    assert r["auto"] == 2 and r["wrong"] == 1 and r["review"] == 0


def test_grid_sorts_fewest_wrong_first(tmp_path):
    d = _dir(tmp_path, [_item(n, n, 5, top=ROSTER) for n in ROSTER])
    cases, rosters = tm.load_cases(d)
    rows = tm.grid(cases, rosters)
    assert len(rows) == len(tm.T_HIGH) * len(tm.MARGIN) * len(tm.W_SORT)
    assert [(r["wrong"], -r["auto"]) for r in rows] == sorted((r["wrong"], -r["auto"]) for r in rows)


def test_report_prints_counts_only_no_names(tmp_path):
    d = _dir(tmp_path, [_item(n, n, 5, top=ROSTER) for n in ROSTER])
    text = tm.report(*tm.load_cases(d))
    for name in ROSTER:
        assert name.split()[0] not in text and name.split()[1] not in text
    assert "Resolved items usable as ground truth: 3" in text


def test_empty_directory_is_friendly(tmp_path):
    (tmp_path / "rq").mkdir()
    assert "Nothing to tune" in tm.report(*tm.load_cases(str(tmp_path / "rq")))


def test_corrupt_files_are_skipped_and_config_is_not_edited(tmp_path):
    d = tmp_path / "rq"
    d.mkdir()
    (d / "bad.json").write_text("{not json", encoding="utf-8")
    cfg = Path("config/attendance.yaml")
    before = hashlib.sha256(cfg.read_bytes()).hexdigest()
    assert tm.load_cases(str(d)) == ([], {})
    tm.main(["--dir", str(d)])
    assert hashlib.sha256(cfg.read_bytes()).hexdigest() == before
