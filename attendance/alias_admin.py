"""Admin-only alias management for the bot (Feature 7). Pure functions so they are testable
without Telegram. Student names appear only in the reply to an admin, never in logs."""
from __future__ import annotations

import os
from typing import Optional

from attendance import roles
from attendance.aliases import AliasStore, store_from_cfg

MAX_LINES = 25


def admin_ids(cfg: dict, env: Optional[dict] = None) -> set[int]:
    """Admin Telegram IDs (thin wrapper; the real logic lives in attendance/roles.py)."""
    return roles.admin_ids(cfg, env)


def stale_days(cfg: dict) -> int:
    m = (((cfg or {}).get("attendance") or {}).get("match") or {})
    try:
        return int(m.get("alias_stale_days", 90))
    except (TypeError, ValueError):
        return 90


def _line(e: dict) -> str:
    flags = f"  ⚠ {', '.join(e['flags'])}" if e["flags"] else ""
    last = e["last_used"] or e["last_confirmed"] or "no date"
    return f"[{e['course']}] {e['key']} → {e['roster_name']} (x{e['confirmations']}, {last}){flags}"


def handle(args: list[str], cfg: dict, store: Optional[AliasStore] = None) -> str:
    """Run ``/aliases [list|flagged|remove] ...`` and return the reply text."""
    store = store or store_from_cfg(cfg)
    sub = (args[0].lower() if args else "flagged")
    rows = store.entries(stale_days=stale_days(cfg))
    if sub in ("flagged", "list"):
        course = args[1].lower() if len(args) > 1 else None
        if course:
            rows = [r for r in rows if r["course"].lower() == course]
        if sub == "flagged":
            rows = [r for r in rows if set(r["flags"]) - {"undated"}]
        if not rows:
            return "No flagged aliases." if sub == "flagged" else "No aliases found."
        rows.sort(key=lambda r: (r["course"], r["key"]))
        shown = [_line(r) for r in rows[:MAX_LINES]]
        more = f"\n…and {len(rows) - MAX_LINES} more (use the CLI: aliases export)." if len(rows) > MAX_LINES else ""
        head = "Flagged aliases" if sub == "flagged" else "Learned aliases"
        return f"{head} ({len(rows)}):\n" + "\n".join(shown) + more + "\n\nRemove: /aliases remove <course> <paper name>"
    if sub == "remove":
        if len(args) < 3:
            return "Usage: /aliases remove <course> <paper name as shown above>"
        course, name = args[1].lower(), " ".join(args[2:])
        n = store.remove(course, name)
        return (f"Removed {n} learned alias entr{'y' if n == 1 else 'ies'} for “{name}” in {course}."
                if n else f"No alias found for “{name}” in {course}.")
    return "Usage: /aliases [list [course] | flagged | remove <course> <paper name>]"
