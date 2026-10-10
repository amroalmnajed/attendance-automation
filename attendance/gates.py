"""attendance/gates.py — Row-level write gate checks and reason codes.

Each gate check returns (passed: bool, reason_code: str | None).
A row auto-accepts only if ALL gates pass.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from attendance.matcher import MatchDecision, MatchResult, ReasonCode
from attendance.normalizer import normalize_attendance_mark
from core.logger import get_logger

log = get_logger("gates")


class GateResult:
    def __init__(
        self,
        passed: bool,
        reason: ReasonCode,
        canonical_value: Optional[str] = None,
    ):
        self.passed = passed
        self.reason = reason
        self.canonical_value = canonical_value  # The normalised attendance value to write


def check_all_gates(
    match_result: MatchResult,
    raw_mark: Optional[str],
    allowed_attendance: list[str],
    current_cell_value: Optional[str],
    force: bool = False,
) -> GateResult:
    """Run all per-row gates in order. Returns the first failure or OK.

    Gates (in order):
    1. Match decision must be AUTO_ACCEPT
    2. Mark must normalise to a recognised attendance value
    3. Canonical value must be in the sheet's allowed dropdown list
    4. Target cell must be empty (or already holds the same value), unless --force
    """
    # Gate 1 — match gate
    if match_result.decision != MatchDecision.AUTO_ACCEPT:
        return GateResult(passed=False, reason=match_result.reason)

    # Gate 2 — attendance mark recognition
    canonical = normalize_attendance_mark(raw_mark)
    if canonical is None:
        log.debug("gate_mark_unrecognized", extra={"raw_mark": raw_mark})
        return GateResult(passed=False, reason=ReasonCode.MARK_UNRECOGNIZED)

    # Gate 3 — value allowed by sheet dropdown
    # Case-insensitive comparison (sheet may vary: "Not complete" vs "Not Complete")
    allowed_lower = {v.lower() for v in allowed_attendance}
    if canonical.lower() not in allowed_lower:
        log.error(
            "gate_value_not_allowed",
            extra={"canonical": canonical, "allowed": allowed_attendance},
        )
        return GateResult(passed=False, reason=ReasonCode.VALUE_NOT_ALLOWED)

    # Resolve exact case from the allowed list (use sheet's exact string)
    for allowed_val in allowed_attendance:
        if allowed_val.lower() == canonical.lower():
            canonical = allowed_val
            break

    # Gate 4 — cell overwrite protection
    if current_cell_value and current_cell_value.strip():
        if current_cell_value.strip().lower() == canonical.lower():
            # Same value already written — idempotent, no-op is fine but we still report
            return GateResult(passed=True, reason=ReasonCode.OK, canonical_value=canonical)
        if not force:
            log.warning(
                "gate_cell_nonempty",
                extra={"current": current_cell_value, "new": canonical},
            )
            return GateResult(passed=False, reason=ReasonCode.CELL_NONEMPTY)

    return GateResult(passed=True, reason=ReasonCode.OK, canonical_value=canonical)


# Re-export for convenience
__all__ = ["GateResult", "check_all_gates", "ReasonCode"]
