"""attendance/matcher.py — RapidFuzz-based student name matcher.

Matching strategy:
  composite_score = 0.5 * token_sort_ratio + 0.5 * token_set_ratio

Auto-accept only when:
  1. composite_score >= T_HIGH  (default 85)
  2. lead over 2nd candidate   >= MARGIN (default 10)
  3. paper name has >= min_tokens_for_auto tokens (default 2)
  4. No other paper line already claimed the same roster student.

Otherwise → review queue with reason code.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from rapidfuzz import fuzz

from attendance.normalizer import normalize_for_matching
from core.logger import get_logger

log = get_logger("matcher")


# ─── Enums & data classes ─────────────────────────────────────────────────────

class MatchDecision(str, Enum):
    AUTO_ACCEPT = "AUTO_ACCEPT"
    REVIEW = "REVIEW"


class ReasonCode(str, Enum):
    OK = "OK"
    MATCH_LOW_SCORE = "MATCH_LOW_SCORE"
    MATCH_AMBIGUOUS = "MATCH_AMBIGUOUS"
    MATCH_DUPLICATE_PAPER = "MATCH_DUPLICATE_PAPER"
    MATCH_ROSTER_COLLISION = "MATCH_ROSTER_COLLISION"
    OCR_LOW_CONFIDENCE = "OCR_LOW_CONFIDENCE"
    OCR_UNREADABLE = "OCR_UNREADABLE"
    MARK_UNRECOGNIZED = "MARK_UNRECOGNIZED"
    CELL_NONEMPTY = "CELL_NONEMPTY"
    VALUE_NOT_ALLOWED = "VALUE_NOT_ALLOWED"
    SESSION_COLUMN_MISMATCH = "SESSION_COLUMN_MISMATCH"   # declared session ≠ live header
    INVALID_TYPE = "INVALID_TYPE"                          # session type not valid for parity
    API_ERROR = "API_ERROR"                                # Sheets or OCR API call failed


@dataclass
class Candidate:
    roster_name: str           # Original name as it appears in the roster
    roster_row: int            # 1-based row index in the sheet (for writing)
    score: float               # Composite score 0–100


@dataclass
class MatchResult:
    decision: MatchDecision
    reason: ReasonCode
    best: Optional[Candidate]
    top3: list[Candidate] = field(default_factory=list)
    raw_name: str = ""
    normalized_query: str = ""
    paper_row_index: int = 0


# ─── Matcher class ────────────────────────────────────────────────────────────

class Matcher:
    def __init__(
        self,
        *,
        t_high: float = 85.0,
        margin: float = 10.0,
        review_floor: float = 60.0,
        min_tokens_for_auto: int = 2,
        weight_sort: float = 0.5,
        weight_set: float = 0.5,
        t_soft: Optional[float] = None,
        margin_strong: float = 20.0,
        alias_lookup: Optional[Callable[[str], Optional[str]]] = None,
    ):
        self.t_high = t_high
        self.margin = margin
        self.review_floor = review_floor
        self.min_tokens_for_auto = min_tokens_for_auto
        self.weight_sort = weight_sort
        self.weight_set = weight_set
        # Optional second accept lane: score >= t_soft AND lead >= margin_strong (disabled when None).
        self.t_soft = t_soft
        self.margin_strong = margin_strong
        # raw paper name -> learned roster name (from human review decisions), or None.
        self.alias_lookup = alias_lookup

        # Set of roster rows already claimed in this upload (prevents collisions)
        self._claimed: set[int] = set()

    def reset(self) -> None:
        """Reset collision tracker between uploads."""
        self._claimed = set()

    def score_pair(self, query: str, candidate: str) -> float:
        """Composite score: 0.5 * token_sort_ratio + 0.5 * token_set_ratio."""
        sort_score = fuzz.token_sort_ratio(query, candidate)
        set_score = fuzz.token_set_ratio(query, candidate)
        return self.weight_sort * sort_score + self.weight_set * set_score

    def match(
        self,
        raw_name: str,
        ocr_confidence: float,
        roster: list[tuple[str, int]],   # (roster_name, sheet_row_1based)
        paper_row_index: int,
        min_confidence: float = 0.75,
        ocr_suggested_name: Optional[str] = None,
        ocr_suggested_confidence: float = 0.0,
    ) -> MatchResult:
        """Match a single OCR'd name against the roster."""
        # Gate 1: unreadable
        if raw_name == "[UNREADABLE]" or not raw_name.strip():
            return MatchResult(
                decision=MatchDecision.REVIEW,
                reason=ReasonCode.OCR_UNREADABLE,
                best=None,
                raw_name=raw_name,
                paper_row_index=paper_row_index,
            )

        # Gate 2: low OCR confidence
        if ocr_confidence < min_confidence:
            return MatchResult(
                decision=MatchDecision.REVIEW,
                reason=ReasonCode.OCR_LOW_CONFIDENCE,
                best=None,
                raw_name=raw_name,
                paper_row_index=paper_row_index,
            )

        query = normalize_for_matching(raw_name)

        # Learned alias: a human already confirmed this exact paper spelling → roster student.
        alias_hit = self._try_alias(raw_name, query, roster, paper_row_index)
        if alias_hit is not None:
            return alias_hit

        scored: list[Candidate] = []
        for roster_name, sheet_row in roster:
            norm_roster = normalize_for_matching(roster_name)
            s = self.score_pair(query, norm_roster)
            scored.append(Candidate(roster_name=roster_name, roster_row=sheet_row, score=s))

        # If the OCR step itself (given the full roster) proposed a specific student,
        # boost that one candidate. Plain string-distance alone can't disambiguate a
        # partial/nicknamed paper name against several similar roster names — it has
        # no way to know which one is *actually* right, only which looks closest.
        # Reasoning-based roster matching can, so give it real weight, but never let
        # it single-handedly force an accept: it only nudges the score used by the
        # same margin/collision/review-floor gates below, it doesn't bypass them.
        if ocr_suggested_name and scored:
            target = normalize_for_matching(ocr_suggested_name)
            suggested = next((c for c in scored if normalize_for_matching(c.roster_name) == target), None)
            if suggested is None:
                # Model may not have copied the roster string verbatim — resolve it
                # to the closest actual roster entry rather than discarding the hint.
                resolved = max(scored, key=lambda c: self.score_pair(target, normalize_for_matching(c.roster_name)))
                if self.score_pair(target, normalize_for_matching(resolved.roster_name)) >= 90.0:
                    suggested = resolved
            if suggested is not None:
                boost = 10.0 + 10.0 * max(0.0, min(1.0, ocr_suggested_confidence))  # +10..+20
                suggested.score = min(100.0, suggested.score + boost)

        # Sort descending
        scored.sort(key=lambda c: c.score, reverse=True)
        top3 = scored[:3]
        best = top3[0] if top3 else None

        def make_result(decision: MatchDecision, reason: ReasonCode) -> MatchResult:
            return MatchResult(
                decision=decision,
                reason=reason,
                best=best,
                top3=top3,
                raw_name=raw_name,
                normalized_query=query,
                paper_row_index=paper_row_index,
            )

        if best is None:
            return make_result(MatchDecision.REVIEW, ReasonCode.MATCH_LOW_SCORE)

        # Gate 3: below review floor
        if best.score < self.review_floor:
            return make_result(MatchDecision.REVIEW, ReasonCode.MATCH_LOW_SCORE)

        # Gate 4: token count (single-token names never auto-accept)
        token_count = len(query.split())
        if token_count < self.min_tokens_for_auto:
            return make_result(MatchDecision.REVIEW, ReasonCode.MATCH_AMBIGUOUS)

        # Gate 5: ambiguity (insufficient score or lead)
        second_score = top3[1].score if len(top3) > 1 else 0.0
        lead = best.score - second_score
        strong_lane = (
            self.t_soft is not None
            and best.score >= self.t_soft
            and lead >= self.margin_strong
        )
        if not strong_lane:
            if best.score < self.t_high or lead < self.margin:
                return make_result(MatchDecision.REVIEW, ReasonCode.MATCH_AMBIGUOUS)

        # Gate 6: roster collision
        if best.roster_row in self._claimed:
            return make_result(MatchDecision.REVIEW, ReasonCode.MATCH_ROSTER_COLLISION)

        # All gates passed
        self._claimed.add(best.roster_row)
        log.debug(
            "match_accept",
            extra={
                "raw_name": raw_name,
                "matched_to": best.roster_name,
                "score": round(best.score, 1),
                "margin": round(best.score - second_score, 1),
            },
        )
        return make_result(MatchDecision.AUTO_ACCEPT, ReasonCode.OK)

    def _try_alias(
        self,
        raw_name: str,
        query: str,
        roster: list[tuple[str, int]],
        paper_row_index: int,
    ) -> Optional[MatchResult]:
        """Resolve via learned alias. None → fall through to fuzzy matching."""
        if self.alias_lookup is None:
            return None
        learned = self.alias_lookup(raw_name)
        if not learned:
            return None
        target = normalize_for_matching(learned)
        hit = next(((n, r) for n, r in roster if normalize_for_matching(n) == target), None)
        if hit is None:
            return None  # student no longer on roster
        cand = Candidate(roster_name=hit[0], roster_row=hit[1], score=100.0)
        collision = hit[1] in self._claimed
        if not collision:
            self._claimed.add(hit[1])
        return MatchResult(
            decision=MatchDecision.REVIEW if collision else MatchDecision.AUTO_ACCEPT,
            reason=ReasonCode.MATCH_ROSTER_COLLISION if collision else ReasonCode.OK,
            best=cand,
            top3=[cand],
            raw_name=raw_name,
            normalized_query=query,
            paper_row_index=paper_row_index,
        )


def build_matcher(cfg: dict, alias_lookup: Optional[Callable[[str], Optional[str]]] = None) -> Matcher:
    """Construct a Matcher from the config dict (attendance.match section)."""
    t_soft = cfg.get("t_soft")
    return Matcher(
        t_soft=float(t_soft) if t_soft is not None else None,
        margin_strong=float(cfg.get("margin_strong", 20)),
        alias_lookup=alias_lookup,
        t_high=float(cfg.get("t_high", 85)),
        margin=float(cfg.get("margin", 10)),
        review_floor=float(cfg.get("review_floor", 60)),
        min_tokens_for_auto=int(cfg.get("min_tokens_for_auto", 2)),
        weight_sort=float(cfg.get("weights", {}).get("token_sort", 0.5)),
        weight_set=float(cfg.get("weights", {}).get("token_set", 0.5)),
    )
