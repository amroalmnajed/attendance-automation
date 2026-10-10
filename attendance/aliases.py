"""attendance/aliases.py — Learned name aliases: human review decisions become future auto-accepts.

Each time a reviewer resolves a flagged item to a roster student, the normalized paper name is
remembered per course. Next upload, the same handwriting/spelling resolves instantly.

Safety: an alias is only honoured when it has >= min_confirmations and strictly outvotes any
conflicting roster name recorded for the same key. "Ignore" resolutions are never learned.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from attendance._fslock import atomic_write_json, file_lock
from attendance.normalizer import normalize_for_matching


class AliasStore:
    def __init__(self, path: str = "config/learned_aliases.json", min_confirmations: int = 1):
        self._path = Path(path)
        self.min_confirmations = max(1, int(min_confirmations))

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
