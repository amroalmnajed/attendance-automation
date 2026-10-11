"""Arabic/English name-variant learning (Feature 15) — kept SEPARATE from the learned-alias store.

When a reviewer resolves a flagged name, the token-level spelling differences between what was
written on paper and the roster name (e.g. "Gidaa" vs "Gaidaa") are recorded here as *suggestions*
(``config/variant_suggestions.json``). Nothing changes matching until a human approves a suggestion
with ``aliases variants review``; approval then writes it to the machine-managed ``learned_variants``
section of ``config/name_variants.yaml`` (the hand-written part of that file is never touched).
The bot loads name_variants.yaml once, so approved variants take effect after a bot restart.
"""
from __future__ import annotations

import difflib
import json
import shutil
from datetime import date
from pathlib import Path
from typing import Optional

import yaml

from attendance._fslock import atomic_write_json, file_lock
from attendance.normalizer import normalize_for_matching, normalize_latin
from core.logger import get_logger

log = get_logger("variants")

LEARNED_SECTION = "learned_variants"
HEADER = f"{LEARNED_SECTION}:   # machine-managed by `aliases variants review` (restart the bot to apply); safe to edit by hand"
PLACEHOLDERS = {"", "unknown", "ignored", "unmatched"}


def settings(cfg: dict) -> dict:
    m = (((cfg or {}).get("attendance") or {}).get("match") or {})
    sect = m.get("variant_learning") or {}
    if not isinstance(sect, dict):
        sect = {}
    try:
        sim = float(sect.get("min_similarity", 0.7))
    except (TypeError, ValueError):
        sim = 0.7
    return {
        "enabled": bool(sect.get("enabled", True)),
        "file": str(sect.get("suggestions_file", "config/variant_suggestions.json")),
        "variants_file": str(m.get("variants_file", "config/name_variants.yaml")),
        "min_similarity": sim,
    }


def _sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def _ok_token(t: str) -> bool:
    return len(t) >= 3 and t.isalpha()


def token_pairs(raw_name: str, roster_name: str, min_similarity: float = 0.7) -> list[tuple[str, str]]:
    """(paper_token, roster_token) pairs that look like spelling variants of the same name.

    Tokens are compared after the normal normalisation (so already-known variants and Arabic
    transliteration are folded first). Pairing is order-independent and each roster token is used
    once; clearly different names (e.g. "Ahmed" vs "Ayman") are NOT paired.
    """
    if str(roster_name or "").strip().lower() in PLACEHOLDERS:
        return []
    paper = normalize_for_matching(raw_name or "").split()
    roster = normalize_for_matching(roster_name or "").split()
    free_roster = [t for t in roster if t not in paper]
    pairs: list[tuple[str, str]] = []
    for pt in (t for t in paper if t not in roster):
        if not _ok_token(pt):
            continue
        best, best_s = None, min_similarity
        for rt in free_roster:
            if _ok_token(rt):
                s = _sim(pt, rt)
                if s >= best_s:
                    best, best_s = rt, s
        if best is not None:
            pairs.append((pt, best))
            free_roster.remove(best)
    return pairs


class VariantSuggestions:
    """Pending / approved / rejected variant suggestions (JSON, names tokens only + one example)."""

    def __init__(self, path: str = "config/variant_suggestions.json"):
        self._path = Path(path)

    def _load(self) -> list[dict]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            items = data.get("items") if isinstance(data, dict) else None
            return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []
        except (OSError, ValueError):
            return []

    def _save(self, items: list[dict]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self._path, {"version": 1, "items": items})

    def record(self, course: str, raw_name: str, roster_name: str, min_similarity: float = 0.7) -> int:
        """Count one reviewer decision. Returns how many token pairs were recorded/updated."""
        pairs = token_pairs(raw_name, roster_name, min_similarity)
        if not pairs:
            return 0
        self._upsert([(p, r, 1, course, f"{raw_name} → {roster_name}") for p, r in pairs])
        return len(pairs)

    def import_pairs(self, rows: list[dict], min_similarity: float = 0.7) -> int:
        """Seed from the existing alias store's suggestions (``suggest_variants_from_aliases``)."""
        ups = []
        for s in rows:
            pt, rt = s.get("paper_token", ""), s.get("roster_token", "")
            if _ok_token(pt) and _ok_token(rt) and _sim(pt, rt) >= min_similarity:
                ups.append((pt, rt, int(s.get("confirmations", 1)), s.get("course", ""),
                            f"{s.get('raw_paper', '')} → {s.get('roster_name', '')}"))
        return self._upsert(ups, absolute=True)

    def _upsert(self, ups: list[tuple], absolute: bool = False) -> int:
        today = date.today().isoformat()
        n = 0
        with file_lock(self._path):
            items = self._load()
            by_key = {(i.get("paper_token"), i.get("roster_token")): i for i in items}
            for pt, rt, count, course, example in ups:
                it = by_key.get((pt, rt))
                if it is None:
                    it = {"paper_token": pt, "roster_token": rt, "count": 0, "courses": [],
                          "first_seen": today, "status": "pending"}
                    items.append(it)
                    by_key[(pt, rt)] = it
                it["count"] = max(int(it.get("count", 0)), count) if absolute else int(it.get("count", 0)) + count
                if course and course not in it.setdefault("courses", []):
                    it["courses"].append(course)
                it["last_seen"], it["example"] = today, example
                n += 1
            self._save(items)
        return n

    def pending(self, min_count: int = 1) -> list[dict]:
        rows = [i for i in self._load() if i.get("status", "pending") == "pending" and int(i.get("count", 0)) >= min_count]
        return sorted(rows, key=lambda i: (-int(i.get("count", 0)), i.get("paper_token", "")))

    def set_status(self, paper_token: str, roster_token: str, status: str) -> bool:
        with file_lock(self._path):
            items = self._load()
            for it in items:
                if (it.get("paper_token"), it.get("roster_token")) == (paper_token, roster_token):
                    it["status"] = status
                    self._save(items)
                    return True
        return False


def existing_variants(variants_path: str) -> dict[str, str]:
    """{normalised variant token -> normalised canonical token} already in name_variants.yaml (all sections)."""
    out: dict[str, str] = {}
    p = Path(variants_path)
    if not p.exists():
        return out
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    for section in data.values():
        if not isinstance(section, dict):
            continue
        for canonical, variants in section.items():
            for v in ([variants] if isinstance(variants, str) else variants or []):
                t = normalize_latin(str(v))
                if t and " " not in t:
                    out[t] = normalize_latin(str(canonical))
    return out


def approve(paper_token: str, roster_token: str, variants_path: str) -> str:
    """Write one approved variant into ``learned_variants`` of name_variants.yaml.

    Returns "added", "exists" (already mapped to this name) or "conflict:<other>" (that spelling is
    already mapped to a DIFFERENT name; nothing written). A ``.bak`` copy is kept before writing.
    """
    p = Path(variants_path)
    known = existing_variants(variants_path).get(paper_token)
    if known == roster_token:
        return "exists"
    if known:
        return f"conflict:{known}"
    raw = p.read_bytes().decode("utf-8") if p.exists() else ""
    nl = "\r\n" if "\r\n" in raw else "\n"
    text = raw.replace("\r\n", "\n")
    data = yaml.safe_load(text) or {}
    learned = {str(k): list(v or []) for k, v in (data.get(LEARNED_SECTION) or {}).items()} if isinstance(data.get(LEARNED_SECTION), dict) else {}
    canon = next((k for k in learned if normalize_latin(k) == roster_token), roster_token.title())
    learned.setdefault(canon, []).append(paper_token.title())
    block = [HEADER] + [line for c, vs in learned.items() for line in [f"  {c}:"] + [f"    - {v}" for v in vs]]

    lines = text.split("\n") if text else []
    start = next((i for i, l in enumerate(lines) if l.startswith(f"{LEARNED_SECTION}:")), None)
    if start is None:
        while lines and lines[-1] == "":
            lines.pop()
        new_lines = lines + ([""] if lines else []) + block + [""]
    else:
        end = next((j for j in range(start + 1, len(lines)) if lines[j] and not lines[j][0].isspace() and not lines[j].startswith("#")), len(lines))
        new_lines = lines[:start] + block + [""] + lines[end:]
    new_text = nl.join(new_lines)
    yaml.safe_load(new_text)                       # must still parse before we touch the real file
    if p.exists():
        shutil.copy2(p, p.with_name(p.name + ".bak"))
    p.write_text(new_text, encoding="utf-8", newline="")
    return "added"


def record_resolution(cfg: dict, course: str, raw_name: str, roster_name: str) -> int:
    """Called after a reviewer resolves a name. Never raises; records suggestions only (no effect on matching)."""
    try:
        s = settings(cfg)
        if not s["enabled"] or not raw_name or not roster_name:
            return 0
        return VariantSuggestions(s["file"]).record(course, raw_name, roster_name, s["min_similarity"])
    except Exception as exc:
        log.warning("variant_record_failed", extra={"error": str(exc)})
        return 0
