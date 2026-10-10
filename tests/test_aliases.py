"""tests/test_aliases.py — Learned alias store + matcher alias fast-path + mark canonicalisation."""
from attendance.aliases import AliasStore
from attendance.matcher import Matcher, MatchDecision, ReasonCode
from attendance.normalizer import canonical_mark

ROSTER = [("Baraa Ahmed Salem", 5), ("Baraa Ahmed Nour", 6), ("Yassin Wael", 7)]


import tempfile
import shutil
from pathlib import Path

def test_learn_then_lookup():
    d = tempfile.mkdtemp()
    try:
        s = AliasStore(str(Path(d) / "a.json"))
        assert s.lookup("g9", "Baraa Ahmed") is None
        s.learn("g9", "Baraa Ahmed", "Baraa Ahmed Nour")
        assert s.lookup("g9", "baraa  ahmed") == "Baraa Ahmed Nour"   # normalised key
        assert s.lookup("g10", "Baraa Ahmed") is None                  # per-course
    finally:
        shutil.rmtree(d, ignore_errors=True)

def test_ignore_never_learned():
    d = tempfile.mkdtemp()
    try:
        s = AliasStore(str(Path(d) / "a.json"))
        s.learn("g9", "Noise", "IGNORED")
        assert s.lookup("g9", "Noise") is None
    finally:
        shutil.rmtree(d, ignore_errors=True)

def test_conflicting_votes_need_majority():
    d = tempfile.mkdtemp()
    try:
        s = AliasStore(str(Path(d) / "a.json"))
        s.learn("g9", "Baraa Ahmed", "Baraa Ahmed Salem")
        s.learn("g9", "Baraa Ahmed", "Baraa Ahmed Nour")
        assert s.lookup("g9", "Baraa Ahmed") is None                   # tie → no auto-accept
        s.learn("g9", "Baraa Ahmed", "Baraa Ahmed Nour")
        assert s.lookup("g9", "Baraa Ahmed") == "Baraa Ahmed Nour"
    finally:
        shutil.rmtree(d, ignore_errors=True)

def test_min_confirmations():
    d = tempfile.mkdtemp()
    try:
        s = AliasStore(str(Path(d) / "a.json"), min_confirmations=2)
        s.learn("g9", "Baraa Ahmed", "Baraa Ahmed Nour")
        assert s.lookup("g9", "Baraa Ahmed") is None
        s.learn("g9", "Baraa Ahmed", "Baraa Ahmed Nour")
        assert s.lookup("g9", "Baraa Ahmed") == "Baraa Ahmed Nour"
    finally:
        shutil.rmtree(d, ignore_errors=True)

def test_matcher_alias_resolves_ambiguous_name():
    m = Matcher(alias_lookup=lambda n: "Baraa Ahmed Nour" if n == "Baraa Ahmed" else None)
    r = m.match("Baraa Ahmed", 0.9, ROSTER, 1)
    assert r.decision == MatchDecision.AUTO_ACCEPT and r.best.roster_row == 6


def test_matcher_alias_collision_goes_to_review():
    m = Matcher(alias_lookup=lambda n: "Yassin Wael")
    assert m.match("Yasin W", 0.9, ROSTER, 1).decision == MatchDecision.AUTO_ACCEPT
    r = m.match("Yasin Wael", 0.9, ROSTER, 2)
    assert r.reason == ReasonCode.MATCH_ROSTER_COLLISION


def test_matcher_alias_respects_low_ocr_confidence():
    m = Matcher(alias_lookup=lambda n: "Yassin Wael")
    assert m.match("Yassin", 0.2, ROSTER, 1).reason == ReasonCode.OCR_LOW_CONFIDENCE


def test_matcher_alias_for_removed_student_falls_through():
    m = Matcher(alias_lookup=lambda n: "Gone Student")
    assert m._try_alias("x", "x", ROSTER, 1) is None


def test_soft_lane_disabled_by_default():
    assert Matcher().t_soft is None


def test_canonical_mark():
    assert canonical_mark("P") == "Present"
    assert canonical_mark("✓") == "Present"
    assert canonical_mark("a") == "Absent"
    assert canonical_mark(None) == "Present"
    assert canonical_mark("???", default="Absent") == "Absent"
