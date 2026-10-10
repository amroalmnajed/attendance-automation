"""tests/test_gates.py — Unit tests for gates.py."""
import pytest
from attendance.gates import check_all_gates
from attendance.matcher import MatchDecision, MatchResult, ReasonCode, Candidate


ALLOWED = ["Present", "Late", "Not complete", "Absent"]


def make_accept_result(roster_row: int = 5, roster_name: str = "Test Student") -> MatchResult:
    best = Candidate(roster_name=roster_name, roster_row=roster_row, score=95.0)
    return MatchResult(
        decision=MatchDecision.AUTO_ACCEPT,
        reason=ReasonCode.OK,
        best=best,
        top3=[best],
        raw_name="Test",
        normalized_query="test",
        paper_row_index=1,
    )


def make_review_result(reason: ReasonCode = ReasonCode.MATCH_AMBIGUOUS) -> MatchResult:
    return MatchResult(
        decision=MatchDecision.REVIEW,
        reason=reason,
        best=None,
        top3=[],
        raw_name="Test",
        normalized_query="test",
        paper_row_index=1,
    )


class TestCheckAllGates:
    def test_all_pass(self):
        gate = check_all_gates(
            match_result=make_accept_result(),
            raw_mark="P",
            allowed_attendance=ALLOWED,
            current_cell_value="",
            force=False,
        )
        assert gate.passed
        assert gate.reason == ReasonCode.OK
        assert gate.canonical_value == "Present"

    def test_match_review_fails_gate1(self):
        gate = check_all_gates(
            match_result=make_review_result(ReasonCode.MATCH_AMBIGUOUS),
            raw_mark="P",
            allowed_attendance=ALLOWED,
            current_cell_value="",
        )
        assert not gate.passed
        assert gate.reason == ReasonCode.MATCH_AMBIGUOUS

    def test_unrecognized_mark_fails_gate2(self):
        gate = check_all_gates(
            match_result=make_accept_result(),
            raw_mark="XYZ",
            allowed_attendance=ALLOWED,
            current_cell_value="",
        )
        assert not gate.passed
        assert gate.reason == ReasonCode.MARK_UNRECOGNIZED

    def test_none_mark_fails_gate2(self):
        gate = check_all_gates(
            match_result=make_accept_result(),
            raw_mark=None,
            allowed_attendance=ALLOWED,
            current_cell_value="",
        )
        assert not gate.passed
        assert gate.reason == ReasonCode.MARK_UNRECOGNIZED

    def test_nonempty_cell_fails_gate4_without_force(self):
        gate = check_all_gates(
            match_result=make_accept_result(),
            raw_mark="P",
            allowed_attendance=ALLOWED,
            current_cell_value="Absent",  # different value already written
            force=False,
        )
        assert not gate.passed
        assert gate.reason == ReasonCode.CELL_NONEMPTY

    def test_nonempty_cell_passes_with_force(self):
        gate = check_all_gates(
            match_result=make_accept_result(),
            raw_mark="P",
            allowed_attendance=ALLOWED,
            current_cell_value="Absent",
            force=True,
        )
        assert gate.passed
        assert gate.canonical_value == "Present"

    def test_same_value_idempotent(self):
        gate = check_all_gates(
            match_result=make_accept_result(),
            raw_mark="P",
            allowed_attendance=ALLOWED,
            current_cell_value="Present",  # same value
            force=False,
        )
        assert gate.passed  # idempotent
        assert gate.canonical_value == "Present"


    def test_case_insensitive_not_complete(self):
        # G9 column E has "Not Complete" (capital C) vs config "Not complete"
        gate = check_all_gates(
            match_result=make_accept_result(),
            raw_mark="NC",
            allowed_attendance=["Present", "Late", "Not Complete", "Absent"],
            current_cell_value="",
        )
        assert gate.passed
        assert gate.canonical_value == "Not Complete"  # uses the sheet's exact string
