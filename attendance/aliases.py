"""attendance/aliases.py — Learned name aliases: human review decisions become future auto-accepts.

Each time a reviewer resolves a flagged item to a roster student, the normalized paper name is
remembered per course. Next upload, the same handwriting/spelling resolves instantly.

Safety: an alias is only honoured when it has >= min_confirmations and strictly outvotes any
conflicting roster name recorded for the same key. "Ignore" resolutions are never learned.
"""
from __future__ import annotations

import json
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from attendance._fslock import atomic_write_json, file_lock
from attendance.normalizer import normalize_for_matching


class AliasStore:
    def __init__(self, path: str = "config/learned_aliases.json", min_confirmations: int = 1):
        self._path = Path(path)
        self.min_confirmations = max(1, int(min_confirmations))
        # Feature 7: dates live in a SIDECAR file (<name>.meta.json) so learned_aliases.json keeps
        # its exact old format ({course: {key: {roster_name: votes}}}) and old code still reads it.
        self._meta_path = self._path.with_name(self._path.stem + ".meta.json")
        self._used: set[tuple[str, str]] = set()

    def _load(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def lookup(self, course: str, raw_name: str) -> Optional[str]:
        """Return the learned roster name for this paper name, or None."""
        key = normalize_for_matching(raw_name)
        if not key:
            return None
        votes: dict[str, int] = self._load().get(course, {}).get(key, {})
        if not votes:
            return None
        ranked = sorted(votes.items(), key=lambda kv: kv[1], reverse=True)
        top_name, top_n = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else 0
        if top_n >= self.min_confirmations and top_n > runner_up:
            self._used.add((course, key))
            return top_name
        return None

    def learn(self, course: str, raw_name: str, roster_name: str) -> None:
        """Record one human confirmation (cross-process safe)."""
        key = normalize_for_matching(raw_name)
        if not key or not roster_name or roster_name == "IGNORED":
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(self._path):
            data = self._load()
            votes = data.setdefault(course, {}).setdefault(key, {})
            votes[roster_name] = votes.get(roster_name, 0) + 1
            atomic_write_json(self._path, data)
            self._touch_meta({(course, key)}, "last_confirmed")

    # ── Feature 7: dates, listing, removal, export ────────────────────────────

    def _load_meta(self) -> dict:
        try:
            data = json.loads(self._meta_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _touch_meta(self, keys: set, field: str, today: Optional[date] = None) -> None:
        stamp = (today or date.today()).isoformat()
        with file_lock(self._meta_path):
            meta = self._load_meta()
            for course, key in keys:
                m = meta.setdefault(course, {}).setdefault(key, {})
                m.setdefault("first_seen", stamp)
                m[field] = stamp
            atomic_write_json(self._meta_path, meta)

    def flush_usage(self, today: Optional[date] = None) -> None:
        """Stamp ``last_used`` for aliases that lookup() applied since the last flush (one write)."""
        used, self._used = self._used, set()
        if used:
            self._touch_meta(used, "last_used", today)

    def entries(self, stale_days: int = 90, today: Optional[date] = None) -> list[dict]:
        """One row per (course, paper key): winning roster name, votes, dates and flags.

        Flags: ``old`` (last used long ago), ``unused`` (never applied since dates were tracked),
        ``undated`` (legacy entry, no dates yet), ``conflict`` / ``low`` (not currently honoured),
        ``unknown-name`` (points at a placeholder such as "Unknown").
        """
        today = today or date.today()
        data, meta = self._load(), self._load_meta()
        rows: list[dict] = []
        for course, keys in (data.items() if isinstance(data, dict) else []):
            if not isinstance(keys, dict):
                continue
            for key, votes in keys.items():
                if not isinstance(votes, dict) or not votes:
                    continue
                ranked = sorted(votes.items(), key=lambda kv: kv[1], reverse=True)
                top_name, top_n = ranked[0]
                runner = ranked[1][1] if len(ranked) > 1 else 0
                m = (meta.get(course, {}) or {}).get(key, {}) or {}
                flags = []
                if top_n <= runner:
                    flags.append("conflict")
                elif top_n < self.min_confirmations:
                    flags.append("low")
                if str(top_name).strip().lower() in ("", "unknown", "ignored", "unmatched"):
                    flags.append("unknown-name")
                if not m:
                    flags.append("undated")
                elif m.get("last_used"):
                    if self._age_days(m["last_used"], today) > stale_days:
                        flags.append("old")
                else:
                    seen = m.get("last_confirmed") or m.get("first_seen")
                    if seen and self._age_days(seen, today) > stale_days:
                        flags.append("unused")
                rows.append({
                    "course": course, "key": key, "roster_name": top_name, "confirmations": top_n,
                    "other_names": {n: v for n, v in ranked[1:]},
                    "first_seen": m.get("first_seen", ""), "last_confirmed": m.get("last_confirmed", ""),
                    "last_used": m.get("last_used", ""), "flags": flags,
                })
        return rows

    @staticmethod
    def _age_days(iso: str, today: date) -> int:
        try:
            return (today - datetime.strptime(str(iso)[:10], "%Y-%m-%d").date()).days
        except ValueError:
            return 0

    def remove(self, course: str, raw_name: str, roster_name: Optional[str] = None) -> int:
        """Delete a learned alias (all roster names for that paper spelling, or just ``roster_name``).

        Returns how many roster-name entries were removed. A ``.bak`` copy of the alias file is
        kept first so a mistaken removal can be undone by copying it back.
        """
        key = normalize_for_matching(raw_name)
        if not key or not self._path.exists():
            return 0
        with file_lock(self._path):
            data = self._load()
            votes = (data.get(course) or {}).get(key)
            if not isinstance(votes, dict) or not votes:
                return 0
            if roster_name is not None and roster_name not in votes:
                return 0
            shutil.copy2(self._path, self._path.with_name(self._path.name + ".bak"))
            removed = 1 if roster_name is not None else len(votes)
            if roster_name is not None:
                del votes[roster_name]
            else:
                votes.clear()
            if not votes:
                del data[course][key]
                if not data[course]:
                    del data[course]
            atomic_write_json(self._path, data)
        if key not in (data.get(course) or {}):
            with file_lock(self._meta_path):
                meta = self._load_meta()
                if (meta.get(course) or {}).pop(key, None) is not None:
                    atomic_write_json(self._meta_path, meta)
        return removed


def store_from_cfg(cfg: dict) -> "AliasStore":
    """AliasStore configured from ``attendance.match`` (tolerates a minimal/fake config)."""
    match_cfg = (((cfg or {}).get("attendance") or {}).get("match") or {})
    return AliasStore(
        path=match_cfg.get("aliases_file", "config/learned_aliases.json"),
        min_confirmations=int(match_cfg.get("alias_min_confirmations", 1)),
    )
