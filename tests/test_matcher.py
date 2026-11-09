"""tests/test_matcher.py — Unit tests for the RapidFuzz-based Matcher.

Covers the deliberate ambiguity cases from Phase 1 design + Arabic cross-script cases.
"""
import pytest
from attendance.matcher import Matcher, MatchDecision, ReasonCode


# Roster as the sheets have it (Latin names from Google Sheets)
ROSTER_MAHMOUD: list[tuple[str, int]] = [
    ("Mahmoud Ahmed Ali", 5),
    ("Mahmoud Ahmed Hassan", 6),
    ("Mohamed Ali Saber", 7),
    ("Yassin Ahmed Helmy El Kady", 8),
    ("Eyad Amr Mohamed Abbas", 9),
]

ROSTER_G10_PARTIAL: list[tuple[str, int]] = [
    ("Eyad Imran Abdel Hamid", 5),
    ("Eyad Abdel Latif", 6),
    ("Yassin Ahmed", 7),
    ("Yassin Wael", 8),
    ("Abdelrahman Hosny", 9),
    ("Abdelrahman Maher", 10),
    ("Hamza Mohamed Ramy", 11),
    ("Talia Ajami", 12),
]


def make_matcher(**kwargs) -> Matcher:
    return Matcher(
        t_high=kwargs.get("t_high", 85),
        margin=kwargs.get("margin", 10),
        review_floor=kwargs.get("review_floor", 60),
        min_tokens_for_auto=kwargs.get("min_tokens_for_auto", 2),
    )


# ─── Phase 1 design cases ─────────────────────────────────────────────────────

class TestPhase1Cases:
    def test_mahmoud_ahmed_ambiguous(self):
        """Mahmoud Ahmed on paper with both Ali and Hassan on roster → REVIEW."""
        m = make_matcher()
        result = m.match(
            raw_name="Mahmoud Ahmed",
            ocr_confidence=0.92,
            roster=ROSTER_MAHMOUD,
            paper_row_index=1,
        )
        # Margin between Mahmoud Ahmed Ali (93.3) and Mahmoud Ahmed Hassan (89.4) is ~3.9 < 10
        assert result.decision == MatchDecision.REVIEW
        assert result.reason == ReasonCode.MATCH_AMBIGUOUS

    def test_mohamed_ali_saber_accepts(self):
        """Mohamed Ali Sabr (spelling slip) should auto-accept."""
        m = make_matcher()
        result = m.match(
            raw_name="Mohamed Ali Sabr",
            ocr_confidence=0.95,
            roster=ROSTER_MAHMOUD,
            paper_row_index=2,
        )
        assert result.decision == MatchDecision.AUTO_ACCEPT
        assert result.reason == ReasonCode.OK
        assert result.best is not None
        assert "Saber" in result.best.roster_name

    def test_single_token_never_auto_accepts(self):
        """'Eyad' alone should never auto-accept regardless of score."""
        m = make_matcher()
        result = m.match(
            raw_name="Eyad",
            ocr_confidence=0.98,
            roster=ROSTER_MAHMOUD,
            paper_row_index=3,
        )
        assert result.decision == MatchDecision.REVIEW
        assert result.reason == ReasonCode.MATCH_AMBIGUOUS

    def test_low_ocr_confidence_routes_to_review(self):
        m = make_matcher()
        result = m.match(
            raw_name="Mohamed Ali Saber",
            ocr_confidence=0.50,  # below min_confidence
            roster=ROSTER_MAHMOUD,
            paper_row_index=4,
            min_confidence=0.75,
        )
        assert result.decision == MatchDecision.REVIEW
        assert result.reason == ReasonCode.OCR_LOW_CONFIDENCE

    def test_unreadable_routes_to_review(self):
        m = make_matcher()
        result = m.match(
            raw_name="[UNREADABLE]",
            ocr_confidence=0.30,
            roster=ROSTER_MAHMOUD,
            paper_row_index=5,
        )
        assert result.decision == MatchDecision.REVIEW
        assert result.reason == ReasonCode.OCR_UNREADABLE

    def test_roster_collision_on_second_claim(self):
        """Two paper lines resolving to the same roster student → collision on 2nd."""
        m = make_matcher()
        # First claim
        r1 = m.match(
            raw_name="Mohamed Ali Saber",
            ocr_confidence=0.95,
            roster=ROSTER_MAHMOUD,
            paper_row_index=1,
        )
        assert r1.decision == MatchDecision.AUTO_ACCEPT

        # Duplicate on paper (same student claimed again)
        r2 = m.match(
            raw_name="Mohamed Ali Saber",
            ocr_confidence=0.95,
            roster=ROSTER_MAHMOUD,
            paper_row_index=2,
        )
        assert r2.decision == MatchDecision.REVIEW
        assert r2.reason == ReasonCode.MATCH_ROSTER_COLLISION

    def test_review_match_does_not_claim_roster_row(self):
        """A weak pattern-match REVIEW must not claim the roster row.

        Regression: _try_pattern_match used to add best.roster_row to
        self._claimed before checking the auto-accept threshold, so a
        low-confidence REVIEW result blocked a later strong match for the
        same student with a spurious MATCH_ROSTER_COLLISION.
        """
        roster = [("Ahmad Hassan Ali", 5), ("Sara Mahmoud Adel", 6)]
        m = make_matcher()

        # Weak/ambiguous partial name → pattern-match REVIEW (score < t_high)
        r1 = m.match(
            raw_name="Sara M",
            ocr_confidence=0.90,
            roster=roster,
            paper_row_index=1,
        )
        assert r1.decision == MatchDecision.REVIEW
        assert r1.best is not None
        assert r1.best.roster_row == 6
        # The REVIEW result must not have claimed the row
        assert 6 not in m._claimed

        # Strong, correct match for the same student later in the batch
        r2 = m.match(
            raw_name="Sara Mahmoud Adel",
            ocr_confidence=0.95,
            roster=roster,
            paper_row_index=2,
        )
        assert r2.decision == MatchDecision.AUTO_ACCEPT
        assert r2.reason == ReasonCode.OK


# ─── Composite scoring ────────────────────────────────────────────────────────

class TestCompositeScoring:
    def test_score_pair_full_match(self):
        m = make_matcher()
        s = m.score_pair("mohamed ali saber", "mohamed ali saber")
        assert s == 100.0

    def test_score_pair_partial_subset(self):
        m = make_matcher()
        s = m.score_pair("mahmoud ahmed", "mahmoud ahmed ali")
        # token_set should be 100 (subset), token_sort ~86.7 → composite ~93.3
        assert s > 85

    def test_score_pair_unrelated(self):
        m = make_matcher()
        s = m.score_pair("john smith", "zainab khadiga")
        assert s < 40
