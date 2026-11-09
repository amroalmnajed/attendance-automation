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

from attendance.name_patterns import NamePatternResolver
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
    PHOTO_MARK_CONFLICT = "PHOTO_MARK_CONFLICT"            # same name on 2+ photos with different marks
    GUEST_UNMATCHED = "GUEST_UNMATCHED"                     # guest participant not matched to roster


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


# ─── T13: Phonetic matching helpers (Soundex + Metaphone) ──────────────────────

def phonetic_tokens_match(t1: str, t2: str) -> bool:
    """True if two name tokens sound identical under Soundex or Metaphone."""
    if not t1 or not t2:
        return False
    if t1.lower() == t2.lower():
        return True
    try:
        import jellyfish
        if jellyfish.soundex(t1) == jellyfish.soundex(t2):
            return True
        if jellyfish.metaphone(t1) == jellyfish.metaphone(t2):
            return True
    except Exception as exc:
        log.debug("matcher_swallowed", extra={"error": str(exc)})
    return False


def phonetic_similarity(name1: str, name2: str) -> float:
    """Compute phonetic similarity between two multi-word names (0.0 to 1.0).

    Returns the proportion of phonetically matching tokens over max(len(t1), len(t2)).
    """
    t1 = [w for w in name1.lower().split() if w]
    t2 = [w for w in name2.lower().split() if w]
    if not t1 or not t2:
        return 0.0

    max_len = max(len(t1), len(t2))
    matched = 0
    used_j = set()
    for w1 in t1:
        for j, w2 in enumerate(t2):
            if j not in used_j and phonetic_tokens_match(w1, w2):
                matched += 1
                used_j.add(j)
                break
    return matched / max_len


def phonetic_match(name1: str, name2: str, threshold: float = 0.8) -> bool:
    """True if name1 and name2 are phonetically consistent (similarity >= threshold)."""
    return phonetic_similarity(name1, name2) >= threshold


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
        use_phonetic: bool = True,
        use_ml: bool = False,
        ml_threshold: float = 0.75,
        ml_margin: float = 0.05,
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
        self.use_phonetic = use_phonetic
        # ML semantic matching
        self.use_ml = use_ml
        self.ml_threshold = ml_threshold
        self.ml_margin = ml_margin
        self._ml_matcher = None
        # Name pattern resolver for partial matching and nicknames
        self._pattern_resolver: Optional[NamePatternResolver] = None

        # Set of roster rows already claimed in this upload (prevents collisions)
        # Can be replaced with a shared set for thread-safe concurrent processing
        self._claimed: set[int] = set()
        # Optional lock for thread-safe collision tracking
        self._claimed_lock: Optional[threading.Lock] = None
        # name -> normalize_for_matching(name). Normalisation is pure per name, so each roster
        # entry is normalised once per matcher instead of once per query.
        self._norm_cache: dict[str, str] = {}

    def reset(self) -> None:
        """Reset collision tracker (and the normalisation cache) between uploads."""
        if self._claimed_lock:
            with self._claimed_lock:
                self._claimed.clear()
        else:
            self._claimed = set()
        self._norm_cache = {}

    def _norm(self, name: str) -> str:
        """Cached ``normalize_for_matching`` for roster names and OCR hints."""
        hit = self._norm_cache.get(name)
        if hit is None:
            if len(self._norm_cache) >= 5000:      # bounded: a roster is a few hundred names
                self._norm_cache.clear()
            hit = self._norm_cache[name] = normalize_for_matching(name)
        return hit

    def _get_pattern_resolver(self) -> NamePatternResolver:
        """Lazy-load NamePatternResolver."""
        if self._pattern_resolver is None:
            self._pattern_resolver = NamePatternResolver(
                use_nicknames=True,
                use_transliteration=True,
                use_partial_matching=True,
                partial_threshold=0.9,
            )
        return self._pattern_resolver

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

        # Phase 1: Learned alias (human-confirmed exact spelling → roster student)
        alias_hit = self._try_alias(raw_name, query, roster, paper_row_index)
        if alias_hit is not None:
            return alias_hit

        # Phase 1.5: Name pattern matching (partial names, nicknames)
        pattern_hit = self._try_pattern_match(raw_name, query, roster, paper_row_index)
        if pattern_hit is not None:
            return pattern_hit

        # Phase 2: ML semantic matching (BERT-based semantic similarity)
        ml_hit = self._try_ml_match(raw_name, query, roster, paper_row_index, ocr_confidence, min_confidence)
        if ml_hit is not None:
            return ml_hit

        # Phase 3: Fall back to RapidFuzz fuzzy matching
        scored: list[Candidate] = []
        for roster_name, sheet_row in roster:
            norm_roster = self._norm(roster_name)
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
            target = self._norm(ocr_suggested_name)
            suggested = next((c for c in scored if self._norm(c.roster_name) == target), None)
            if suggested is None:
                # Model may not have copied the roster string verbatim — resolve it
                # to the closest actual roster entry rather than discarding the hint.
                resolved = max(scored, key=lambda c: self.score_pair(target, self._norm(c.roster_name)))
                if self.score_pair(target, self._norm(resolved.roster_name)) >= 90.0:
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
        # T13: Phonetic fallback lane for borderline fuzzy match scores
        phonetic_lane = (
            self.use_phonetic
            and lead >= self.margin
            and best.score >= max(self.review_floor, self.t_high - 15.0)
            and phonetic_match(query, self._norm(best.roster_name))
        )
        if not strong_lane and not phonetic_lane:
            if best.score < self.t_high or lead < self.margin:
                return make_result(MatchDecision.REVIEW, ReasonCode.MATCH_AMBIGUOUS)

        # Gate 6: roster collision
        if self._claimed_lock:
            with self._claimed_lock:
                collision = best.roster_row in self._claimed
                if not collision:
                    self._claimed.add(best.roster_row)
        else:
            collision = best.roster_row in self._claimed
            if not collision:
                self._claimed.add(best.roster_row)

        if collision:
            return make_result(MatchDecision.REVIEW, ReasonCode.MATCH_ROSTER_COLLISION)
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
        target = self._norm(learned)
        hit = next(((n, r) for n, r in roster if self._norm(n) == target), None)
        if hit is None:
            return None  # student no longer on roster
        cand = Candidate(roster_name=hit[0], roster_row=hit[1], score=100.0)
        if self._claimed_lock:
            with self._claimed_lock:
                collision = hit[1] in self._claimed
                if not collision:
                    self._claimed.add(hit[1])
        else:
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

    def _try_pattern_match(
        self,
        raw_name: str,
        query: str,
        roster: list[tuple[str, int]],
        paper_row_index: int,
    ) -> Optional[MatchResult]:
        """Resolve via name pattern matching (partial names, nicknames). None → fall through."""
        resolver = self._get_pattern_resolver()

        # Try partial name matching for each roster entry
        scored: list[Candidate] = []
        for roster_name, sheet_row in roster:
            matched, match_type = resolver.match_partial(query, roster_name)
            if matched:
                # Boost score based on match type (first/last name matches are strong signals)
                boost = 15.0 if match_type in ("first", "last") else 10.0
                norm_roster = self._norm(roster_name)
                s = self.score_pair(query, norm_roster) + boost
                scored.append(Candidate(roster_name=roster_name, roster_row=sheet_row, score=min(100.0, s)))

        if not scored:
            return None

        # Sort and check for collision
        scored.sort(key=lambda c: c.score, reverse=True)
        best = scored[0]
        top3 = scored[:3]

        # Check collision
        if self._claimed_lock:
            with self._claimed_lock:
                collision = best.roster_row in self._claimed
                if not collision:
                    self._claimed.add(best.roster_row)
        else:
            collision = best.roster_row in self._claimed
            if not collision:
                self._claimed.add(best.roster_row)

        # Only auto-accept if score is high enough and no collision
        if best.score >= self.t_high and not collision:
            return MatchResult(
                decision=MatchDecision.AUTO_ACCEPT,
                reason=ReasonCode.OK,
                best=best,
                top3=top3,
                raw_name=raw_name,
                normalized_query=query,
                paper_row_index=paper_row_index,
            )
        # Otherwise return REVIEW result
        return MatchResult(
            decision=MatchDecision.REVIEW,
            reason=ReasonCode.MATCH_ROSTER_COLLISION if collision else ReasonCode.MATCH_LOW_SCORE,
            best=best,
            top3=top3,
            raw_name=raw_name,
            normalized_query=query,
            paper_row_index=paper_row_index,
        )

    def _try_ml_match(
        self,
        raw_name: str,
        query: str,
        roster: list[tuple[str, int]],
        paper_row_index: int,
        ocr_confidence: float,
        min_confidence: float,
    ) -> Optional[MatchResult]:
        """Resolve via ML semantic matching. None → fall through to RapidFuzz."""
        if not self.use_ml:
            return None

        # Gate 1: OCR confidence check (enforced before ML matching)
        if ocr_confidence < min_confidence:
            return None

        # Gate 4: Token count check
        if len(query.split()) < self.min_tokens_for_auto:
            return None

        # Lazy-load ML matcher
        if self._ml_matcher is None:
            try:
                from attendance.ml_matcher import init_ml_matcher
                self._ml_matcher = init_ml_matcher(threshold=self.ml_threshold)
            except ImportError:
                log.warning("ML matching requested but sentence-transformers not available")
                self.use_ml = False
                return None

        roster_names = [name for name, _ in roster]
        matched_name, similarity, top3 = self._ml_matcher.match(raw_name, roster_names, self.ml_threshold)

        if matched_name is None:
            return None

        # Gate 5: Ambiguity margin check
        if len(top3) >= 2:
            top1_score = top3[0][1]
            top2_score = top3[1][1]
            margin = top1_score - top2_score
            if margin < self.ml_margin:
                return None

        # Find the roster entry for the matched name
        hit = next(((n, r) for n, r in roster if n == matched_name), None)
        if hit is None:
            return None

        # Convert similarity (0-1) to score (0-100)
        score = similarity * 100.0
        cand = Candidate(roster_name=hit[0], roster_row=hit[1], score=score)
        if self._claimed_lock:
            with self._claimed_lock:
                collision = hit[1] in self._claimed
                if not collision:
                    self._claimed.add(hit[1])
        else:
            collision = hit[1] in self._claimed
            if not collision:
                self._claimed.add(hit[1])

        # Build top3 candidates with roster rows
        top3_candidates = []
        for name, sim in top3:
            roster_entry = next(((n, r) for n, r in roster if n == name), None)
            if roster_entry:
                top3_candidates.append(
                    Candidate(roster_name=roster_entry[0], roster_row=roster_entry[1], score=sim * 100.0)
                )

        return MatchResult(
            decision=MatchDecision.REVIEW if collision else MatchDecision.AUTO_ACCEPT,
            reason=ReasonCode.MATCH_ROSTER_COLLISION if collision else ReasonCode.OK,
            best=cand,
            top3=top3_candidates[:3],
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
        use_phonetic=bool(cfg.get("use_phonetic", cfg.get("phonetic", True))),
        use_ml=bool(cfg.get("use_ml", False)),
        ml_threshold=float(cfg.get("ml_threshold", 0.75)),
        ml_margin=float(cfg.get("ml_margin", 0.05)),
    )


# Compatibility alias for existing code that expects NameMatcher
NameMatcher = Matcher
