import json
from pathlib import Path
import pytest
import tempfile
import shutil

from attendance.reporter import (
    AuditRecord,
    read_audit_log,
    plan_revert,
    RevertPlanCell
)

def test_read_audit_log():
    temp_dir = tempfile.mkdtemp()
    try:
        log_file = Path(temp_dir) / "audit.jsonl"
        records = [
            {"ts": "2026-10-09T00:00:00", "upload_id": "up1", "course": "g10", "sheet_id": "sid", "tab": "tab", "cell": "C5", "old_value": None, "new_value": "Present", "dry_run": False},
            {"ts": "2026-10-09T00:01:00", "upload_id": "up2", "course": "g10", "sheet_id": "sid", "tab": "tab", "cell": "C5", "old_value": "Present", "new_value": "Absent", "dry_run": False},
        ]
        with log_file.open("w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
                
        parsed = read_audit_log(log_file)
        assert len(parsed) == 2
        assert parsed[0].cell == "C5"
        assert parsed[0].col == "C"
        assert parsed[0].row == 5
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

def test_plan_revert():
    records = [
        AuditRecord("ts", "up1", "g10", "sid", "tab", "C5", None, "Present", False),
        AuditRecord("ts", "up1", "g10", "sid", "tab", "C6", None, "Present", False),
        AuditRecord("ts", "up2", "g10", "sid", "tab", "C5", "Present", "Absent", False),
    ]
    
    # Revert up1
    plan1 = plan_revert("up1", records)
    assert len(plan1) == 2
    c5_plan = next(p for p in plan1 if p.cell == "C5")
    c6_plan = next(p for p in plan1 if p.cell == "C6")
    
    # C5 was touched by up2 since up1
    assert c5_plan.revert_to_value is None
    assert c5_plan.current_in_audit == "Absent"
    assert c5_plan.overwritten_since is True
    
    # C6 was NOT touched by up2
    assert c6_plan.revert_to_value is None
    assert c6_plan.current_in_audit == "Present"
    assert c6_plan.overwritten_since is False
    
    # Revert up2
    plan2 = plan_revert("up2", records)
    assert len(plan2) == 1
    c5_plan2 = plan2[0]
    assert c5_plan2.revert_to_value == "Present"
    assert c5_plan2.current_in_audit == "Absent"
    assert c5_plan2.overwritten_since is False
