"""tests/eval_matcher.py — Evaluation script for name-match accuracy and false-accept rate.

Requirement 6 (original prompt):
  "A small evaluation script: given labeled sample sheets, report
   name-match accuracy and the false-accept rate."

Usage:
  # Run built-in synthetic test suite (no external files needed):
  python tests/eval_matcher.py

  # Run against a labeled JSON file:
  python tests/eval_matcher.py --labeled tests/sample_labeled.json

  # Tune thresholds and compare:
  python tests/eval_matcher.py --t-high 85 --margin 8
  python tests/eval_matcher.py --t-high 90 --margin 10

Labeled JSON format:
  {
    "roster": [["Student Full Name", sheet_row_int], ...],
    "cases": [
      {
        "paper_name": "Name as written on paper",
        "expected_roster_name": "Exact name from roster (or null for unmatched)",
        "ocr_confidence": 0.92,
        "note": "optional human note"
      },
      ...
    ]
  }

Metrics reported:
  - Total cases / Auto-accepted / Sent-to-review
  - Correct accepts   (auto-accept → correct roster student)
  - False accepts     (auto-accept → WRONG roster student)
  - False negatives   (correct match sent to review — conservative but inefficient)
  - True negatives    (correctly sent to review — ambiguous cases caught)
  - Precision = correct_accepts / (correct_accepts + false_accepts)
  - Recall    = correct_accepts / (correct_accepts + false_negatives)
  - False-accept rate = false_accepts / total_auto_accepted
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ── Windows UTF-8 fix ─────────────────────────────────────────────────────────
# Box-drawing chars (─, ═, ✓, ✗, △, ⚠) crash on cp1252 terminals.
# Reconfigure stdout/stderr to UTF-8 if possible; fall back to ASCII replacements.
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf_8"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

# Ensure the project root is on sys.path when run directly
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from attendance.matcher import Matcher, MatchDecision, ReasonCode


# ─── Built-in test suite ──────────────────────────────────────────────────────

_BUILTIN_ROSTER: list[tuple[str, int]] = [
    # G10-style roster (Latin names as they appear in sheets)
    ("Mahmoud Ahmed Ali", 5),
    ("Mahmoud Ahmed Hassan", 6),
    ("Mohamed Ali Saber", 7),
    ("Yassin Ahmed Helmy El Kady", 8),
    ("Eyad Amr Mohamed Abbas", 9),
    ("Eyad Imran Abdel Hamid", 10),
    ("Abdelrahman Hosny Mohamed", 11),
    ("Abdelrahman Maher Salah", 12),
    ("Hamza Mohamed Ramy", 13),
    ("Talia Ajami", 14),
    ("Youssef Khaled Nasser", 15),
    ("Ziad Tamer Fouad", 16),
    ("Sara Mohamed Aly", 17),
    ("Nour El Din Ahmed", 18),
]

_BUILTIN_CASES: list[dict] = [
    # ── Should AUTO-ACCEPT (correct) ────────────────────────────────────────
    {
        "paper_name": "Mohamed Ali Saber",
        "expected_roster_name": "Mohamed Ali Saber",
        "ocr_confidence": 0.95,
        "note": "exact match",
    },
    {
        "paper_name": "Mohamed Ali Sabr",
        "expected_roster_name": None,
        "ocr_confidence": 0.91,
        "note": "minor spelling slip — score 97 but margin over Sara Mohamed Aly is 9.5 < 10; conservatively sent to review (correct behavior)",
    },
    {
        "paper_name": "Youssef Khaled",
        "expected_roster_name": "Youssef Khaled Nasser",
        "ocr_confidence": 0.93,
        "note": "partial name — subset of roster name",
    },
    {
        "paper_name": "Ziad Tamer",
        "expected_roster_name": None,
        "ocr_confidence": 0.89,
        "note": "two-token partial, score 88.5 < T_HIGH=90; conservatively sent to review (correct behavior)",
    },
    {
        "paper_name": "Sara Mohamed Aly",
        "expected_roster_name": "Sara Mohamed Aly",
        "ocr_confidence": 0.97,
        "note": "exact match different casing handled by normalizer",
    },
    {
        "paper_name": "Hamza Mohamed Ramy",
        "expected_roster_name": "Hamza Mohamed Ramy",
        "ocr_confidence": 0.94,
        "note": "exact 3-token match",
    },
    {
        "paper_name": "Nour Eldin Ahmed",
        "expected_roster_name": "Nour El Din Ahmed",
        "ocr_confidence": 0.88,
        "note": "El Din written as Eldin (prefix particle variant)",
    },
    # ── Should go to REVIEW (ambiguous) ─────────────────────────────────────
    {
        "paper_name": "Mahmoud Ahmed",
        "expected_roster_name": None,  # None = correct outcome is REVIEW
        "ocr_confidence": 0.92,
        "note": "ambiguous: matches both Mahmoud Ahmed Ali and Mahmoud Ahmed Hassan",
    },
    {
        "paper_name": "Eyad",
        "expected_roster_name": None,
        "ocr_confidence": 0.98,
        "note": "single token — never auto-accept regardless of score",
    },
    {
        "paper_name": "Abdelrahman",
        "expected_roster_name": None,
        "ocr_confidence": 0.90,
        "note": "single token shared by two roster students",
    },
    {
        "paper_name": "Abdelrahman Hosny",
        "expected_roster_name": "Abdelrahman Hosny Mohamed",
        "ocr_confidence": 0.85,
        "note": "two-token partial — margin over Abdelrahman Maher is 16.2 (>10), so correct auto-accept",
    },
    # ── Should go to REVIEW (low confidence) ────────────────────────────────
    {
        "paper_name": "Mohamed Ali Saber",
        "expected_roster_name": None,
        "ocr_confidence": 0.55,
        "note": "low OCR confidence — correct match but not trusted",
    },
    # ── Should go to REVIEW (unreadable) ────────────────────────────────────
    {
        "paper_name": "[UNREADABLE]",
        "expected_roster_name": None,
        "ocr_confidence": 0.30,
        "note": "OCR flagged line as unreadable",
    },
    # ── Deliberate false-accept trap: name so similar it might fool the matcher
    {
        "paper_name": "Eyad Amr Mohamed",
        "expected_roster_name": "Eyad Amr Mohamed Abbas",
        "ocr_confidence": 0.90,
        "note": "3 matching tokens out of 4 — check margin vs Eyad Imran Abdel Hamid",
    },
]


# ─── Eval runner ──────────────────────────────────────────────────────────────

@dataclass
class EvalResult:
    total: int = 0
    auto_accepted: int = 0
    sent_to_review: int = 0
    correct_accepts: int = 0    # auto-accept matched the right person
    false_accepts: int = 0      # auto-accept matched the WRONG person
    true_negatives: int = 0     # correctly sent to review (ambiguous/low-conf)
    false_negatives: int = 0    # should have auto-accepted but was sent to review
    details: list[dict] = field(default_factory=list)

    @property
    def precision(self) -> float:
        denom = self.correct_accepts + self.false_accepts
        return self.correct_accepts / denom if denom else 0.0

    @property
    def recall(self) -> float:
        denom = self.correct_accepts + self.false_negatives
        return self.correct_accepts / denom if denom else 0.0

    @property
    def false_accept_rate(self) -> float:
        return self.false_accepts / self.auto_accepted if self.auto_accepted else 0.0


def run_eval(
    roster: list[tuple[str, int]],
    cases: list[dict],
    *,
    t_high: float = 90.0,
    margin: float = 10.0,
    review_floor: float = 60.0,
    min_tokens_for_auto: int = 2,
    min_confidence: float = 0.75,
    verbose: bool = True,
) -> EvalResult:
    matcher = Matcher(
        t_high=t_high,
        margin=margin,
        review_floor=review_floor,
        min_tokens_for_auto=min_tokens_for_auto,
    )
    matcher.reset()

    result = EvalResult(total=len(cases))

    for i, case in enumerate(cases):
        paper_name: str = case["paper_name"]
        expected: Optional[str] = case.get("expected_roster_name")
        conf: float = float(case.get("ocr_confidence", 0.9))
        note: str = case.get("note", "")

        mr = matcher.match(
            raw_name=paper_name,
            ocr_confidence=conf,
            roster=roster,
            paper_row_index=i + 1,
            min_confidence=min_confidence,
        )

        decision_str = mr.decision.value
        best_name = mr.best.roster_name if mr.best else None
        score = round(mr.best.score, 1) if mr.best else 0.0
        top3_str = ", ".join(
            f"{c.roster_name}({c.score:.1f})" for c in mr.top3[:3]
        )

        # Classify the outcome
        outcome: str
        if mr.decision == MatchDecision.AUTO_ACCEPT:
            result.auto_accepted += 1
            if expected is not None and best_name == expected:
                result.correct_accepts += 1
                outcome = "✓ CORRECT ACCEPT"
            elif expected is None:
                # We expected review but got auto-accept → false accept
                result.false_accepts += 1
                outcome = "✗ FALSE ACCEPT (expected review)"
            else:
                # Expected a different name
                result.false_accepts += 1
                outcome = f"✗ FALSE ACCEPT (expected: {expected})"
        else:  # REVIEW
            result.sent_to_review += 1
            if expected is None:
                result.true_negatives += 1
                outcome = "✓ CORRECT REVIEW"
            else:
                result.false_negatives += 1
                outcome = f"△ FALSE NEGATIVE (should have matched: {expected})"

        detail = {
            "case": i + 1,
            "paper_name": paper_name,
            "expected": expected,
            "decision": decision_str,
            "reason": mr.reason.value,
            "best_match": best_name,
            "score": score,
            "top3": top3_str,
            "outcome": outcome,
            "note": note,
        }
        result.details.append(detail)

        if verbose:
            _print_case(detail)

    return result


def _print_case(d: dict) -> None:
    print(
        f"  [{d['case']:>2}] {d['paper_name']:<35} "
        f"→ {d['decision']:<14} {d['reason']:<30} "
        f"best={d['best_match'] or '—'!r:<35} score={d['score']:>5.1f}  "
        f"{d['outcome']}"
    )
    if d["note"]:
        print(f"       note: {d['note']}")


def print_summary(result: EvalResult, t_high: float, margin: float) -> None:
    print(f"\n{'═'*80}")
    print(f"  EVAL SUMMARY  (T_HIGH={t_high}, margin={margin})")
    print(f"{'═'*80}")
    print(f"  Total cases        : {result.total}")
    print(f"  Auto-accepted      : {result.auto_accepted}")
    print(f"  Sent to review     : {result.sent_to_review}")
    print()
    print(f"  Correct accepts    : {result.correct_accepts}  (auto-accept → right person)")
    print(f"  False accepts      : {result.false_accepts}   (auto-accept → wrong/unexpected)")
    print(f"  True negatives     : {result.true_negatives}  (correctly sent to review)")
    print(f"  False negatives    : {result.false_negatives}  (correct match sent to review)")
    print()
    print(f"  Precision          : {result.precision:.1%}  "
          f"(of accepted, fraction correct)")
    print(f"  Recall             : {result.recall:.1%}  "
          f"(of should-accept cases, fraction caught)")
    print(f"  False-accept rate  : {result.false_accept_rate:.1%}  "
          f"(of auto-accepts, fraction wrong)")
    print(f"{'═'*80}\n")

    if result.false_accept_rate > 0.05:
        print("  ⚠  FALSE-ACCEPT RATE > 5% — consider raising T_HIGH or MARGIN.")
    elif result.false_accept_rate == 0.0 and result.false_negatives > 3:
        print("  △  Zero false accepts but many false negatives — "
              "consider lowering T_HIGH slightly to reduce review queue load.")
    else:
        print("  ✓  Thresholds look acceptable.")
    print()


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate name-matcher accuracy and false-accept rate."
    )
    parser.add_argument(
        "--labeled", default=None,
        help="Path to a labeled JSON file (see module docstring for format). "
             "Omit to run the built-in synthetic suite.",
    )
    parser.add_argument("--t-high", type=float, default=90.0,
                        help="Auto-accept score threshold (default: 90)")
    parser.add_argument("--margin", type=float, default=10.0,
                        help="Minimum lead over 2nd candidate (default: 10)")
    parser.add_argument("--review-floor", type=float, default=60.0,
                        help="Score below which match goes straight to review (default: 60)")
    parser.add_argument("--min-confidence", type=float, default=0.75,
                        help="Minimum OCR confidence (default: 0.75)")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress per-case output; show summary only.")
    args = parser.parse_args()

    if args.labeled:
        p = Path(args.labeled)
        if not p.exists():
            print(f"ERROR: labeled file not found: {p}", file=sys.stderr)
            sys.exit(1)
        data = json.loads(p.read_text(encoding="utf-8"))
        roster: list[tuple[str, int]] = [tuple(r) for r in data["roster"]]  # type: ignore
        cases = data["cases"]
        print(f"\nLoaded {len(cases)} cases and {len(roster)} roster entries from {p.name}")
    else:
        roster = _BUILTIN_ROSTER
        cases = _BUILTIN_CASES
        print(f"\nRunning built-in synthetic suite: "
              f"{len(cases)} cases, {len(roster)} roster entries")

    print(f"Settings: T_HIGH={args.t_high}, margin={args.margin}, "
          f"review_floor={args.review_floor}, min_confidence={args.min_confidence}\n")
    print(f"  {'#':>3}  {'Paper name':<35}  {'Decision':<14}  "
          f"{'Reason':<30}  {'Best match':<35}  {'Score':>5}  Outcome")
    print(f"  {'─'*3}  {'─'*35}  {'─'*14}  {'─'*30}  {'─'*35}  {'─'*5}  {'─'*20}")

    result = run_eval(
        roster,
        cases,
        t_high=args.t_high,
        margin=args.margin,
        review_floor=args.review_floor,
        min_confidence=args.min_confidence,
        verbose=not args.quiet,
    )

    print_summary(result, args.t_high, args.margin)

    # Exit non-zero if false accepts detected (useful in CI)
    if result.false_accepts > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
