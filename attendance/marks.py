"""attendance/marks.py — Parse "name + attendance mark" lines (text / paste mode).

Accepted shapes (one student per line):

    Ahmed Ali | L          Sara - absent          Omar, Late
    Mona: P                Youssef                 (no mark -> Present)

Rules
-----
* The mark is whatever follows the LAST separator (``|``  ``,``  ``،``  ``:``  tab,
  or a hyphen/dash surrounded by spaces).
* A mark is recognised case-insensitively, in English or common Arabic forms, and is
  mapped to the canonical sheet value (Present / Late / Absent / Not complete).
* A line with no separator, or whose tail is clearly part of a name, is a plain name
  and defaults to Present (today's behaviour).
* A short single-word tail that is NOT a known mark (e.g. a typo such as "abcent")
  is **never guessed**: the line is flagged ``unrecognized`` and the raw token is
  passed on as the mark, so the workflow's mark gate sends it to human review.
* Hyphens without surrounding spaces are never separators ("Abdel-Aziz" stays intact),
  unless the tail is a known mark ("Sara-absent").
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from attendance.normalizer import normalize_attendance_mark

# ─── Mark vocabulary (text mode) ──────────────────────────────────────────────
# Extends the OCR mark map with spelled-out words and more Arabic forms. Kept here
# (not in normalizer._MARK_MAP) so OCR behaviour is unchanged.

_ARABIC_DIACRITICS = re.compile(r"[\u064B-\u0652\u0640]")


def _fold(text: str) -> str:
    """Lower-case, strip Arabic diacritics/tatweel, and unify common letter variants."""
    t = _ARABIC_DIACRITICS.sub("", text.strip().lower())
    for src, dst in (("أ", "ا"), ("إ", "ا"), ("آ", "ا"), ("ة", "ه"), ("ى", "ي")):
        t = t.replace(src, dst)
    return re.sub(r"\s+", " ", t)


_EXTRA_MARKS: dict[str, str] = {
    # Present
    "حاضر": "Present", "حاضره": "Present", "حضور": "Present", "حضر": "Present",
    "here": "Present", "yes": "Present", "y": "Present",
    # Late
    "متاخر": "Late", "متاخره": "Late", "تاخير": "Late", "تاخر": "Late",
    # Absent
    "غائب": "Absent", "غايب": "Absent", "غائبه": "Absent", "غايبه": "Absent",
    "غياب": "Absent", "غاب": "Absent", "no": "Absent",
    # Not complete
    "غير مكتمل": "Not complete", "غير مكتمله": "Not complete",
    "غير كامل": "Not complete", "ناقص": "Not complete", "نص": "Not complete",
    "not-complete": "Not complete", "notcomplete": "Not complete",
    "incomplete": "Not complete",
}
_EXTRA_FOLDED = {_fold(k): v for k, v in _EXTRA_MARKS.items()}


def parse_mark(token: Optional[str]) -> Optional[str]:
    """Return the canonical value for a mark token, or None if not recognised."""
    if not token or not token.strip():
        return None
    return normalize_attendance_mark(token) or _EXTRA_FOLDED.get(_fold(token))


# ─── Line parsing ─────────────────────────────────────────────────────────────

# Separators that always split (comma incl. Arabic comma, pipe, colon, tab).
_HARD_SEP = re.compile(r"\s*[|,\u060C:\t]\s*")
# A dash only counts as a separator when surrounded by whitespace.
_SPACED_DASH = re.compile(r"\s+[-\u2013\u2014]\s+")
# Dash with no spaces, used only to peel off an obviously-valid trailing mark.
_TIGHT_DASH = re.compile(r"[-\u2013\u2014]")

# A tail longer than this (or with spaces) is treated as part of a name, not a mark.
_MAX_MARK_LEN = 14

DEFAULT_MARK = "Present"

KIND_DEFAULT = "default"            # no mark on the line -> Present
KIND_MARKED = "marked"              # explicit, recognised mark
KIND_UNRECOGNIZED = "unrecognized"  # looks like a mark but isn't known -> review


@dataclass(frozen=True)
class ParsedLine:
    index: int                 # 1-based line number among non-empty lines
    raw: str                   # the line as typed
    name: str                  # name part (separator + mark removed)
    mark_raw: Optional[str]    # mark text as typed, if any
    canonical: Optional[str]   # canonical value; None when unrecognized
    kind: str                  # KIND_*

    @property
    def effective_mark(self) -> str:
        """Value to hand to the workflow as ``status_mark``.

        Unrecognized marks are passed through verbatim so the mark gate flags them.
        """
        if self.kind == KIND_UNRECOGNIZED:
            return self.mark_raw or ""
        return self.canonical or DEFAULT_MARK


def _split_tail(line: str) -> tuple[str, Optional[str], bool]:
    """Split ``line`` into (name, tail, from_spaced_or_hard_sep).

    Returns tail=None when no separator is present.
    """
    # Hard separators first (last occurrence wins).
    hard = list(_HARD_SEP.finditer(line))
    dash = list(_SPACED_DASH.finditer(line))
    candidates = hard + dash
    if candidates:
        m = max(candidates, key=lambda x: x.start())
        return line[: m.start()].strip(), line[m.end():].strip(), True

    # Unspaced dash: only honour it when the tail is a *known* mark ("Sara-absent").
    tight = list(_TIGHT_DASH.finditer(line))
    if tight:
        m = tight[-1]
        tail = line[m.end():].strip()
        if tail and parse_mark(tail) is not None:
            return line[: m.start()].strip(), tail, False
    return line.strip(), None, False


def parse_line(line: str, index: int = 1) -> Optional[ParsedLine]:
    """Parse one line. Returns None for blank lines."""
    raw = line.strip()
    if not raw:
        return None

    name, tail, explicit_sep = _split_tail(raw)

    if tail is None or not name:
        # No separator (or nothing before it): whole line is a name -> Present.
        return ParsedLine(index, raw, raw if not name or tail is None else name,
                          None, DEFAULT_MARK, KIND_DEFAULT)

    canonical = parse_mark(tail)
    if canonical is not None:
        return ParsedLine(index, raw, name, tail, canonical, KIND_MARKED)

    if not tail:
        # "Sara -" / "Sara |" with nothing after the separator: just the name.
        return ParsedLine(index, raw, name, None, DEFAULT_MARK, KIND_DEFAULT)

    if explicit_sep and " " not in tail and len(tail) <= _MAX_MARK_LEN:
        # A short single token after a separator that we do not know: never guess.
        return ParsedLine(index, raw, name, tail, None, KIND_UNRECOGNIZED)

    # Long / multi-word tail after a separator: treat the whole line as a name.
    return ParsedLine(index, raw, raw, None, DEFAULT_MARK, KIND_DEFAULT)


def parse_lines(lines: list[str]) -> list[ParsedLine]:
    """Parse many lines; blank lines are dropped and indices are 1-based over the rest."""
    out: list[ParsedLine] = []
    for line in lines:
        p = parse_line(line, index=len(out) + 1)
        if p is not None:
            out.append(p)
    return out


def split_names_and_marks(parsed: list[ParsedLine]) -> tuple[list[str], list[str]]:
    """Parallel (names, marks) lists ready for ``text_workflow.run_text``."""
    return [p.name for p in parsed], [p.effective_mark for p in parsed]


def has_issues(parsed: list[ParsedLine]) -> bool:
    return any(p.kind == KIND_UNRECOGNIZED for p in parsed)


# ─── Preview ──────────────────────────────────────────────────────────────────

def format_preview(parsed: list[ParsedLine], *, max_lines: int = 40, markdown: bool = False) -> str:
    """Short, human-readable "how I read your list" preview.

    Plain text by default; when ``markdown=True``, names are escaped so Markdown-reserved
    characters do not break parsing.
    """
    counts = {"Present": 0, "Late": 0, "Absent": 0, "Not complete": 0}
    unrecognized = 0
    rows: list[str] = []
    for p in parsed:
        name_disp = re.sub(r"([_*`\[\]])", r"\\\1", p.name) if markdown else p.name
        if p.kind == KIND_UNRECOGNIZED:
            unrecognized += 1
            rows.append(f"{p.index}. {name_disp} → ⚠️ “{p.mark_raw}” not recognised (will go to review)")
            continue
        counts[p.canonical or DEFAULT_MARK] += 1
        suffix = " (no mark → Present)" if p.kind == KIND_DEFAULT else ""
        rows.append(f"{p.index}. {name_disp} → {p.canonical}{suffix}")

    shown = rows[:max_lines]
    if len(rows) > max_lines:
        shown.append(f"… and {len(rows) - max_lines} more")

    tally = " · ".join(f"{k} {v}" for k, v in counts.items() if v)
    if unrecognized:
        tally += (" · " if tally else "") + f"Needs review {unrecognized}"
    header = f"🔎 *How I read your list* ({len(parsed)} line{'s' if len(parsed) != 1 else ''})" if markdown else f"🔎 How I read your list ({len(parsed)} line{'s' if len(parsed) != 1 else ''})"
    return "\n".join([header, tally, "", *shown]).rstrip()
