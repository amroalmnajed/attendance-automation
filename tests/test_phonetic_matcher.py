"""Tests for T13: Phonetic matching layer using Soundex and Metaphone (jellyfish)."""
from __future__ import annotations

import pytest

from attendance.matcher import (
    MatchDecision,
    Matcher,
    ReasonCode,
    phonetic_match,
    phonetic_similarity,
    phonetic_tokens_match,
)


def test_phonetic_tokens_match():
    # Identical
    assert phonetic_tokens_match("Ahmed", "Ahmed") is True

    # Alternate transliterations
    assert phonetic_tokens_match("Mohamed", "Muhammed") is True
    assert phonetic_tokens_match("Youssef", "Yousef") is True
    assert phonetic_tokens_match("Tareq", "Tarek") is True
    assert phonetic_tokens_match("Smith", "Smyth") is True

    # Completely different names
    assert phonetic_tokens_match("Ahmed", "Khaled") is False
    assert phonetic_tokens_match("Ali", "Hassan") is False
    assert phonetic_tokens_match("", "Ahmed") is False


def test_phonetic_similarity():
    # Exact phonetic equivalent
    assert phonetic_similarity("Mohamed Ahmed", "Muhammed Ahmet") == 1.0
    assert phonetic_similarity("Tareq Yousef", "Tarek Youssef") == 1.0

    # Partial phonetic overlap
    sim = phonetic_similarity("Mohamed Ali", "Muhammed Ahmed")
    assert abs(sim - 0.5) < 1e-6

    # Partial name length mismatch
    sim_partial = phonetic_similarity("Ziad Tamer", "Ziad Tamer Fouad")
    assert abs(sim_partial - (2 / 3)) < 1e-4


def test_phonetic_match():
    assert phonetic_match("Tareq Yousef", "Tarek Youssef") is True
    assert phonetic_match("Ziad Tamer", "Ziad Tamer Fouad", threshold=0.8) is False
    assert phonetic_match("Omar", "Khaled") is False


def test_matcher_phonetic_fallback_auto_accepts_borderline_score():
    """A name with borderline score (e.g. 80-84 with t_high=85) but strong phonetic match and decisive margin auto-accepts."""
    # Roster has 'Tarek Youssef', paper has 'Tareq Yousef'
    roster = [
        ("Tarek Youssef", 5),
        ("Khaled Omar", 6),
    ]
    matcher = Matcher(t_high=85.0, margin=10.0, review_floor=60.0, use_phonetic=True)

    result = matcher.match(
        raw_name="Tareq Yousef",
        ocr_confidence=0.95,
        roster=roster,
        paper_row_index=1,
    )
    # The phonetic lane triggers and accepts
    assert result.decision == MatchDecision.AUTO_ACCEPT
    assert result.reason == ReasonCode.OK
    assert result.best.roster_name == "Tarek Youssef"


def test_matcher_phonetic_fallback_disabled_goes_to_review():
    """When use_phonetic=False, borderline scores below t_high still go to review."""
    roster = [
        ("Tarek Youssef", 5),
        ("Khaled Omar", 6),
    ]
    # Set t_high high so fuzzy score alone falls short
    matcher = Matcher(t_high=95.0, margin=10.0, review_floor=60.0, use_phonetic=False)

    result = matcher.match(
        raw_name="Tareq Yousef",
        ocr_confidence=0.95,
        roster=roster,
        paper_row_index=1,
    )
    assert result.decision == MatchDecision.REVIEW
    assert result.reason == ReasonCode.MATCH_AMBIGUOUS


def test_matcher_phonetic_fallback_respects_ambiguity_margin():
    """If two roster entries have close scores (lead < margin), phonetic match does not force accept."""
    roster = [
        ("Tarek Youssef Ali", 5),
        ("Tarek Youssef Hassan", 6),
    ]
    matcher = Matcher(t_high=85.0, margin=10.0, review_floor=60.0, use_phonetic=True)

    result = matcher.match(
        raw_name="Tareq Yousef",
        ocr_confidence=0.95,
        roster=roster,
        paper_row_index=1,
    )
    # Close scores between both candidates -> must go to REVIEW
    assert result.decision == MatchDecision.REVIEW
    assert result.reason == ReasonCode.MATCH_AMBIGUOUS
