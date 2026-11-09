"""attendance/name_patterns.py — Nickname and transliteration handling.

Features:
  - Nickname dictionary (e.g., "Ahmed" → "Ahmad", "Mohamed" → "Muhammad")
  - Transliteration patterns (Arabic script variations)
  - Partial name matching (first name only, last name only)
  - Learned from historical review decisions
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from attendance.normalizer import normalize_for_matching
from core.paths import data_path


# ─── Nickname dictionary ───────────────────────────────────────────────────────

# Common Arabic name variations (expandable via config or learned from history)
NICKNAME_MAP = {
    # Ahmed variants
    "ahmed": "ahmad",
    "ahmad": "ahmed",
    # Mohamed variants
    "mohamed": "muhammad",
    "muhammad": "mohamed",
    "mohammed": "muhammad",
    "mohammad": "muhammad",
    # Ali variants
    "ali": "ali",
    # Hassan variants
    "hassan": "hasan",
    "hasan": "hassan",
    # Hussein variants
    "hussein": "husayn",
    "husayn": "hussein",
    # Omar variants
    "omar": "umar",
    "umar": "omar",
    # Youssef variants
    "youssef": "yusuf",
    "yusuf": "youssef",
    # Khalid variants
    "khalid": "khalid",
    "khalid": "khaled",
    "khaled": "khalid",
    # Abdullah variants
    "abdullah": "abdullah",
    # Mohammed variants (middle name)
    "mohd": "muhammad",
    "muhammad": "mohd",
}


# ─── Transliteration patterns ───────────────────────────────────────────────────

# Common OCR/transliteration errors and corrections
TRANSLITERATION_PATTERNS = [
    # Number/letter confusions
    (r"0", "O"),
    (r"1", "I"),
    (r"2", "Z"),
    (r"3", "E"),
    (r"4", "A"),
    (r"5", "S"),
    (r"8", "B"),
    # Character confusions
    (r"rn", "m"),
    (r"vv", "w"),
    (r"cl", "d"),
    # Arabic to Latin variations
    (r"gh", "g"),
    (r"kh", "k"),
    (r"sh", "s"),
    (r"th", "t"),
    (r"dh", "d"),
    (r"aa", "a"),
    (r"ee", "i"),
    (r"oo", "u"),
]


def apply_transliteration_corrections(name: str) -> str:
    """Apply transliteration corrections to a name.

    Args:
        name: Input name to correct

    Returns:
        Corrected name
    """
    corrected = name
    for pattern, replacement in TRANSLITERATION_PATTERNS:
        corrected = re.sub(pattern, replacement, corrected, flags=re.IGNORECASE)
    return corrected


# ─── Partial name matching ─────────────────────────────────────────────────────

def extract_first_last(name: str) -> tuple[Optional[str], Optional[str]]:
    """Extract first and last name from a full name.

    Args:
        name: Full name

    Returns:
        Tuple of (first_name, last_name) or (None, None) if parsing fails
    """
    parts = name.strip().split()
    if len(parts) == 0:
        return None, None
    if len(parts) == 1:
        return parts[0], None
    # Assume first word is first name, last word is last name
    return parts[0], parts[-1]


def match_by_first_name(
    query_first: str,
    roster_name: str,
    threshold: float = 0.9,
) -> bool:
    """Check if first names match (for partial matching).

    Args:
        query_first: First name from query
        roster_name: Full roster name
        threshold: Similarity threshold

    Returns:
        True if first names match above threshold
    """
    from rapidfuzz import fuzz

    roster_first, _ = extract_first_last(roster_name)
    if roster_first is None:
        return False

    score = fuzz.ratio(query_first.lower(), roster_first.lower())
    return score >= (threshold * 100)


def match_by_last_name(
    query_last: str,
    roster_name: str,
    threshold: float = 0.9,
) -> bool:
    """Check if last names match (for partial matching).

    Args:
        query_last: Last name from query
        roster_name: Full roster name
        threshold: Similarity threshold

    Returns:
        True if last names match above threshold
    """
    from rapidfuzz import fuzz

    _, roster_last = extract_first_last(roster_name)
    if roster_last is None:
        return False

    score = fuzz.ratio(query_last.lower(), roster_last.lower())
    return score >= (threshold * 100)


# ─── Nickname resolution ───────────────────────────────────────────────────────

def resolve_nickname(name: str) -> str:
    """Resolve a name to its canonical form using nickname map.

    Args:
        name: Input name

    Returns:
        Canonical name (or original if no mapping found)
    """
    normalized = normalize_for_matching(name).lower()
    if normalized in NICKNAME_MAP:
        return NICKNAME_MAP[normalized]
    return name


def expand_nicknames(name: str) -> list[str]:
    """Expand a name to all possible variants using nickname map.

    Args:
        name: Input name

    Returns:
        List of possible variants (including original)
    """
    variants = [name]
    normalized = normalize_for_matching(name).lower()

    # Check if this is a nickname that maps to something else
    if normalized in NICKNAME_MAP:
        canonical = NICKNAME_MAP[normalized]
        variants.append(canonical)

    # Check if this is a canonical name that has nicknames
    for nickname, canonical in NICKNAME_MAP.items():
        if canonical == normalized:
            variants.append(nickname)

    return list(set(variants))


# ─── Comprehensive name pattern resolution ────────────────────────────────────

class NamePatternResolver:
    """Resolver for nickname, transliteration, and partial name patterns."""

    def __init__(
        self,
        use_nicknames: bool = True,
        use_transliteration: bool = True,
        use_partial_matching: bool = True,
        partial_threshold: float = 0.9,
    ):
        """Initialize the name pattern resolver.

        Args:
            use_nicknames: Whether to use nickname resolution
            use_transliteration: Whether to apply transliteration corrections
            use_partial_matching: Whether to use partial name matching
            partial_threshold: Threshold for partial name matching
        """
        self.use_nicknames = use_nicknames
        self.use_transliteration = use_transliteration
        self.use_partial_matching = use_partial_matching
        self.partial_threshold = partial_threshold

    def normalize(self, name: str) -> str:
        """Apply all normalization steps to a name.

        Args:
            name: Input name

        Returns:
            Normalized name
        """
        result = name

        # Apply transliteration corrections
        if self.use_transliteration:
            result = apply_transliteration_corrections(result)

        # Resolve nickname to canonical form
        if self.use_nicknames:
            result = resolve_nickname(result)

        # Standard normalization
        result = normalize_for_matching(result)

        return result

    def get_variants(self, name: str) -> list[str]:
        """Get all possible variants of a name.

        Args:
            name: Input name

        Returns:
            List of name variants
        """
        variants = [name]

        # Add nickname variants
        if self.use_nicknames:
            variants.extend(expand_nicknames(name))

        # Add transliteration-corrected variant
        if self.use_transliteration:
            corrected = apply_transliteration_corrections(name)
            if corrected != name:
                variants.append(corrected)

        # Normalize all variants
        normalized_variants = [self.normalize(v) for v in variants]

        return list(set(normalized_variants))

    def match_partial(
        self,
        query: str,
        roster_name: str,
    ) -> tuple[bool, str]:
        """Try partial name matching (first or last name only).

        Args:
            query: Query name
            roster_name: Roster name to match against

        Returns:
            Tuple of (matched, match_type) where match_type is "first", "last", or "none"
        """
        if not self.use_partial_matching:
            return False, "none"

        query_first, query_last = extract_first_last(query)

        if query_first is not None and match_by_first_name(query_first, roster_name, self.partial_threshold):
            return True, "first"

        if query_last is not None and match_by_last_name(query_last, roster_name, self.partial_threshold):
            return True, "last"

        return False, "none"


# ─── Learning from historical decisions ───────────────────────────────────────

def learn_from_aliases(aliases_path: str | Path) -> dict[str, str]:
    """Learn nickname patterns from learned_aliases.json.

    Args:
        aliases_path: Path to learned_aliases.json

    Returns:
        Dictionary of nickname → canonical mappings
    """
    import json

    aliases_path = data_path(aliases_path)
    if not aliases_path.exists():
        return {}

    with open(aliases_path, "r", encoding="utf-8") as f:
        aliases_data = json.load(f)

    learned = {}
    for course_aliases in aliases_data.values():
        for raw_name, roster_name in course_aliases.items():
            # Extract potential nickname patterns
            raw_norm = normalize_for_matching(raw_name).lower()
            roster_norm = normalize_for_matching(roster_name).lower()

            if raw_norm != roster_norm:
                learned[raw_norm] = roster_norm

    return learned


def update_nickname_map(learned_nicknames: dict[str, str]) -> None:
    """Update the global nickname map with learned patterns.

    Args:
        learned_nicknames: Dictionary of learned nickname → canonical mappings
    """
    global NICKNAME_MAP
    NICKNAME_MAP.update(learned_nicknames)


# ─── Global resolver instance ─────────────────────────────────────────────────

_resolver: Optional[NamePatternResolver] = None


def init_resolver(
    use_nicknames: bool = True,
    use_transliteration: bool = True,
    use_partial_matching: bool = True,
    partial_threshold: float = 0.9,
) -> NamePatternResolver:
    """Initialize the global name pattern resolver.

    Args:
        use_nicknames: Whether to use nickname resolution
        use_transliteration: Whether to apply transliteration corrections
        use_partial_matching: Whether to use partial name matching
        partial_threshold: Threshold for partial name matching

    Returns:
        The resolver instance
    """
    global _resolver
    if _resolver is None:
        _resolver = NamePatternResolver(
            use_nicknames=use_nicknames,
            use_transliteration=use_transliteration,
            use_partial_matching=use_partial_matching,
            partial_threshold=partial_threshold,
        )
    return _resolver


def get_resolver() -> Optional[NamePatternResolver]:
    """Get the global name pattern resolver instance."""
    return _resolver
