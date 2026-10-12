"""tests/tune_matcher.py - suggest matcher thresholds from your own resolved review items.

Reads ``review_queue/*.json`` (local, never committed), takes every item a human resolved to a real
student (roster_row > 0) as ground truth (paper name -> chosen roster name) and replays the matcher
over a grid of t_high / margin / token weights.

  python tests/tune_matcher.py [--dir review_queue] [--top 10]

Output is COUNTS ONLY (no student names). The current config values are marked ``<- current``.
It never edits config/attendance.yaml: copy the numbers you like yourself.

Limits: the full roster is not stored with review items, so each course's roster is approximated by
the names seen in that course's review items (candidates + chosen). That is harder than the real
roster in some ways and easier in others: treat the result as a hint, not a guarantee. Items that
the live matcher auto-accepted are never in the queue, so the sample is biased towards hard cases.
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from attendance.matcher import MatchDecision, Matcher   # noqa: E402

T_HIGH = [80, 82, 84, 86, 88, 90, 92]
MARGIN = [5, 8, 10, 12, 15]
W_SORT = [0.3, 0.4, 0.5, 0.6, 0.7]


def load_cases(review_dir: str):
    """-> (cases, rosters). case = (course, raw_name, ocr_confidence, expected_name)."""
    from attendance.review_queue import ReviewQueue
    cases, names = [], defaultdict(set)
    rq = ReviewQueue(store_dir=review_dir)
    for upload_id in rq._store.list_all_upload_ids():
        try:
            data = rq._load(upload_id)
            items = data.get("items", [])
        except Exception:
            continue
        for it in items:
            course = it.get("course", "?")
            for c in it.get("top3") or []:
                if c.get("roster_name"):
                    names[course].add(c["roster_name"])
            res = it.get("resolution") or {}
            if it.get("status") == "pending" or not res.get("roster_name") or not (res.get("roster_row") or 0) > 0:
                continue
            names[course].add(res["roster_name"])
            cases.append((course, it.get("raw_name", ""), float(it.get("ocr_confidence") or 0.0), res["roster_name"]))
    rosters = {c: [(n, i + 2) for i, n in enumerate(sorted(v))] for c, v in names.items()}
    return cases, rosters


def evaluate(cases, rosters, *, t_high, margin, w_sort):
    """-> dict(auto, wrong, review) counts for one parameter set."""
    auto = wrong = review = 0
    for i, (course, raw, conf, expected) in enumerate(cases):
        m = Matcher(t_high=float(t_high), margin=float(margin), weight_sort=w_sort, weight_set=round(1 - w_sort, 6))
        res = m.match(raw, conf, rosters[course], i, min_confidence=0.0)
        if res.decision == MatchDecision.AUTO_ACCEPT:
            auto += 1
            wrong += 0 if res.best and res.best.roster_name == expected else 1
        else:
            review += 1
    return {"auto": auto, "wrong": wrong, "review": review}


def grid(cases, rosters):
    rows = []
    for t, mg, w in itertools.product(T_HIGH, MARGIN, W_SORT):
        r = evaluate(cases, rosters, t_high=t, margin=mg, w_sort=w)
        rows.append({"t_high": t, "margin": mg, "w_sort": w, **r})
    # fewest WRONG auto-accepts first, then most auto-accepts
    rows.sort(key=lambda r: (r["wrong"], -r["auto"], r["t_high"], r["margin"]))
    return rows


def _current():
    try:
        import yaml
        m = yaml.safe_load(Path("config/attendance.yaml").read_text(encoding="utf-8"))["attendance"]["match"]
        return float(m.get("t_high", 85)), float(m.get("margin", 10)), float((m.get("weights") or {}).get("token_sort", 0.5))
    except Exception:
        return None


def report(cases, rosters, top: int = 10) -> str:
    n = len(cases)
    out = [f"Resolved items usable as ground truth: {n}"]
    if n == 0:
        out.append("Nothing to tune yet: resolve some review items first.")
        return "\n".join(out)
    cur = _current()
    rows = grid(cases, rosters)
    out.append(f"Parameter sets tried: {len(rows)}  (WRONG = auto-accepted the wrong student)")
    out.append(f"{'t_high':>6} {'margin':>6} {'w_sort':>6} {'auto':>5} {'wrong':>5} {'review':>6} {'auto%':>6}")

    def line(r, tag=""):
        return (f"{r['t_high']:>6} {r['margin']:>6} {r['w_sort']:>6} {r['auto']:>5} {r['wrong']:>5} "
                f"{r['review']:>6} {100 * r['auto'] / n:>5.0f}%{tag}")
    for r in rows[:top]:
        is_cur = cur and (r["t_high"], r["margin"], r["w_sort"]) == cur
        out.append(line(r, "  <- current" if is_cur else ""))
    if cur:
        r = evaluate(cases, rosters, t_high=cur[0], margin=cur[1], w_sort=cur[2])
        out.append("Current config: " + f"t_high={cur[0]:g} margin={cur[1]:g} w_sort={cur[2]:g} -> "
                   f"auto {r['auto']}, wrong {r['wrong']}, review {r['review']}")
    out.append("Prefer the row with the fewest wrong auto-accepts; only then more auto-accepts. "
               "This script never edits config/attendance.yaml.")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dir", default="review_queue")
    ap.add_argument("--top", type=int, default=10)
    a = ap.parse_args(argv)
    cases, rosters = load_cases(a.dir)
    print(report(cases, rosters, a.top))
    return 0


if __name__ == "__main__":
    sys.exit(main())
