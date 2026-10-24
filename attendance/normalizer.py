"""attendance/normalizer.py — Name and attendance-mark normalisation.

English-only workflow:
  1. Latin name normalisation (lowercase, strip accents/punctuation, collapse spaces).
  2. Variant folding via config/name_variants.yaml.
  3. Attendance mark → canonical attendance value.
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Optional

import yaml


# ─── Attendance mark map ──────────────────────────────────────────────────────

_MARK_MAP: dict[str, str] = {
    "p": "Present", "present": "Present", "✓": "Present", "√": "Present",
    "حاضر": "Present", "ح": "Present", "online": "Present",
    "a": "Absent",  "absent": "Absent",   "x": "Absent",  "×": "Absent",
    "غياب": "Absent", "غ": "Absent",
    "l": "Late",    "late": "Late", "متاخر": "Late", "متأخر": "Late",
    "nc": "Not complete", "n": "Not complete",
    "not complete": "Not complete", "not_complete": "Not complete",
    "free": "Not complete",
}

CANONICAL_ATTENDANCE = {"Present", "Late", "Not complete", "Absent"}


# ─── Variant table (loaded once) ─────────────────────────────────────────────

_variants_table: Optional[dict[str, str]] = None
_phrase_table: list[tuple[re.Pattern, str]] = []


def _load_variants(path: str = "config/name_variants.yaml") -> dict[str, str]:
    """Load variant tables once. Single-word variants -> token table; multi-word variants
    (e.g. "Abdel Aziz", "El Sharkawy") -> phrase table applied before token folding."""
    global _variants_table, _phrase_table
    if _variants_table is not None:
        return _variants_table
    table: dict[str, str] = {}
    phrases: list[tuple[list[str], str]] = []
    p = Path(path)
    if p.exists():
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        for section in data.values():
            if not isinstance(section, dict):
                continue
            for canonical, variants in section.items():
                if isinstance(variants, str):      # tolerate `Key: Value` instead of a list
                    variants = [variants]
                if not isinstance(variants, list):
                    continue
                canon = normalize_latin(str(canonical))
                for v in variants:
                    toks = normalize_latin(str(v)).split()
                    if not toks or " ".join(toks) == canon:
                        continue
                    if len(toks) == 1:
                        table[toks[0]] = canon
                    else:
                        phrases.append((toks, canon))
    phrases.sort(key=lambda tc: len(tc[0]), reverse=True)   # longest phrase first
    _phrase_table = [
        (re.compile(r"(?<!\S)" + re.escape(" ".join(t)) + r"(?!\S)"), c) for t, c in phrases
    ]
    _variants_table = table
    return table


# ─── Public functions ─────────────────────────────────────────────────────────

def normalize_latin(text: str) -> str:
    """Lowercase, strip accents, remove punctuation, collapse spaces.

    Special handling before general rules:
    - El-X / Al-X prefixes are collapsed to ElX / AlX so "El-Sharkawy" and
      "Elsharkawy" tokenise identically. This runs before the hyphen→space step.
    """
    # Collapse El-/Al- prefix-hyphen into a fused token (case-insensitive)
    text = re.sub(r'\b(El|Al)-(\w)', lambda m: m.group(1) + m.group(2), text, flags=re.IGNORECASE)
    nfd = unicodedata.normalize("NFD", text)
    stripped = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    lower = stripped.lower()
    cleaned = re.sub(r"[^\w\s-]", "", lower)
    return re.sub(r"[-\s]+", " ", cleaned).strip()


def normalize_arabic(text: str) -> str:
    """Normalize Arabic characters for better fuzzy matching."""
    text = re.sub(r'[\u0617-\u061A\u064B-\u0652]', '', text) # Remove tashkeel
    text = re.sub(r'\u0640', '', text)                      # Remove tatweel
    text = re.sub(r'[إأآا]', 'ا', text)                     # Normalize Alef
    text = re.sub(r'ة', 'ه', text)                          # Normalize Ta Marbuta
    text = re.sub(r'[يى]', 'ي', text)                       # Normalize Ya
    return text


def _contains_arabic(text: str) -> bool:
    """Return True if text contains any Arabic Unicode character (U+0600..U+06FF)."""
    return any('\u0600' <= ch <= '\u06ff' for ch in text)


# Egyptian Arabic syllable transliteration map — covers the most common name fragments.
# Applied token-by-token after normalize_arabic() so Alef/Ya/Ta-Marbuta are already unified.
_AR_TO_LATIN: dict[str, str] = {
    # Prefixes / particles
    "عبد": "abd", "عبده": "abdo", "ابو": "abu",
    "ال": "al", "بن": "bin", "ابن": "ibn",
    # Male given names
    "محمد": "mohamed", "مصطفى": "mostafa", "مصطفي": "mostafa",
    "احمد": "ahmed", "حمدي": "hamdi", "حمزه": "hamza", "حمزة": "hamza",
    "محمود": "mahmoud", "ياسين": "yassin", "يسين": "yassin",
    "الرحمن": "alrahman", "الرحمان": "alrahman",
    "عمر": "omar", "عمرو": "amr", "علي": "ali", "على": "ali",
    "حسن": "hassan", "حسين": "hussein", "خالد": "khaled", "كريم": "karim",
    "يوسف": "youssef", "اياد": "eyad", "عياد": "eyad",
    "يحيي": "yehia", "يحيى": "yehia",
    "رامي": "ramy", "صلاح": "salah", "هاني": "hani",
    "طارق": "tarek", "تامر": "tamer", "ياسر": "yasser",
    "نور": "nour", "نورالدين": "nour el din",
    "زياد": "ziad", "زيد": "zeid",
    "ابراهيم": "ibrahim", "عماد": "emad",
    "وليد": "walid", "ماهر": "maher", "منصور": "mansour",
    "سامي": "samy", "سامح": "sameh",
    "اسلام": "islam", "معاذ": "moaz", "مراد": "morad",
    "مليجي": "meligy", "جوهر": "gohar", "علام": "allam",
    "عصام": "essam", "ادهم": "adham", "ادم": "adam",
    "عادل": "adel", "كمال": "kamal", "جمال": "gamal",
    "فادي": "fady", "هيثم": "haitham", "بسام": "bassam",
    "شريف": "sherif", "رشاد": "rashad",
    "سيف": "seif", "صابر": "saber", "ناصر": "nasser",
    "فؤاد": "fouad", "رزق": "rizk", "رضا": "rida",
    "امين": "amin",
    # Female given names
    "مريم": "mariam", "فريده": "farida", "فريدة": "farida",
    "سهر": "sahar", "هانيا": "hania", "جني": "jana", "جنى": "jana",
    "حبيبه": "habiba", "حبيبة": "habiba", "تاليا": "talia",
    "ساره": "sara", "سارة": "sara",
    "نوره": "nour", "نورة": "nour",
    "رانيا": "rania", "منه": "menna", "منة": "menna",
    "دنيا": "donya", "لميس": "lamis", "ياسمين": "yasmine",
    "شيرين": "shireen", "غيداء": "gaidaa",
    "لمياء": "lamia", "رحمه": "rahma", "رحمة": "rahma",
    # Common Egyptian surnames / nisba
    "الطيف": "latif", "الحميد": "hamid", "المنعم": "moniem",
    "القادي": "el kady", "القاضي": "el kady",
    "الشرقاوي": "el sharkawy",
    "عباس": "abbas", "الدين": "el din",
    "فوزي": "fawzy", "بهاء": "bahaa",
    "سليم": "salim", "سالم": "salem",
    "حلمي": "helmy", "هلال": "helal",
    "صبري": "sabry", "سمير": "samir",
    "الصعيدي": "el saidy",
    "عجمي": "ajami",
}


def _transliterate_arabic(text: str) -> str:
    """Best-effort phonetic transliteration of Arabic name tokens into Latin script.

    Applies the _AR_TO_LATIN table token by token on already-normalised Arabic text.
    Unknown tokens are left as-is (they will produce near-zero fuzzy scores and go to review).
    """
    tokens = text.split()
    result = []
    for tok in tokens:
        result.append(_AR_TO_LATIN.get(tok, tok))
    return " ".join(result)


def normalize_for_matching(text: str) -> str:
    """Normalise an English or Arabic name for fuzzy comparison with variant folding.

    Pipeline:
    1. Strip Arabic diacritics / tatweel and unify letter forms (normalize_arabic).
    2. If the text is still Arabic-script after normalisation, transliterate to Latin.
    3. Normalise Latin (lowercase, strip accents/punctuation).
    4. Apply variant folding table (name_variants.yaml).
    """
    text = normalize_arabic(text)
    if _contains_arabic(text):
        text = _transliterate_arabic(text)
    latin = normalize_latin(text)
    variants = _load_variants()
    for pat, canon in _phrase_table:
        latin = pat.sub(canon, latin)
    tokens = latin.split()
    return " ".join(variants.get(tok, tok) for tok in tokens)


def normalize_attendance_mark(mark: Optional[str]) -> Optional[str]:
    """Map a raw attendance mark to a canonical value, or None if unrecognised."""
    if not mark or not mark.strip():
        return None
    return _MARK_MAP.get(mark.strip().lower())


def canonical_mark(mark: Optional[str], default: str = "Present") -> str:
    """Canonical attendance value for any raw mark ("P", "✓", None, ...); `default` if unrecognised/empty."""
    return normalize_attendance_mark(mark) or default


def suggest_variants_from_aliases(
    alias_path: str = "config/learned_aliases.json",
    min_confirmations: int = 2,
) -> list[dict]:
    """Scan the learned alias store and suggest new name_variants.yaml entries.

    For each confirmed alias (paper_spelling → roster_name), computes the
    normalized forms of both sides.  If they differ at the *token* level,
    the differing token pairs are candidates for name_variants.yaml.

    Returns a list of suggestion dicts:
      {
        "paper_token": <normalized paper token>,
        "roster_token": <normalized roster token>,
        "course": <course key>,
        "raw_paper": <original paper spelling>,
        "roster_name": <roster student name>,
        "confirmations": <vote count>,
      }

    Usage (CLI one-liner):
      python -c "from attendance.normalizer import suggest_variants_from_aliases; import json; print(json.dumps(suggest_variants_from_aliases(), indent=2, ensure_ascii=False))"
    """
    import json
    from pathlib import Path

    p = Path(alias_path)
    if not p.exists():
        return []

    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    suggestions: list[dict] = []

    for course, aliases in data.items():
        for raw_key, votes in aliases.items():
            # raw_key is already normalize_for_matching(paper_name)
            for roster_name, count in votes.items():
                if count < min_confirmations:
                    continue
                roster_norm = normalize_for_matching(roster_name)
                paper_tokens = raw_key.split()
                roster_tokens = roster_norm.split()
                if paper_tokens == roster_tokens:
                    continue
                # Pair tokens positionally; flag divergent ones
                for i, (pt, rt) in enumerate(zip(paper_tokens, roster_tokens)):
                    if pt != rt:
                        suggestions.append({
                            "paper_token": pt,
                            "roster_token": rt,
                            "course": course,
                            "raw_paper": raw_key,
                            "roster_name": roster_name,
                            "confirmations": count,
                        })

    # Deduplicate by (paper_token, roster_token) — keep highest confirmation count
    seen: dict[tuple[str, str], dict] = {}
    for s in suggestions:
        key = (s["paper_token"], s["roster_token"])
        if key not in seen or s["confirmations"] > seen[key]["confirmations"]:
            seen[key] = s
    return sorted(seen.values(), key=lambda x: -x["confirmations"])
