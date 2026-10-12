import json
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional, Any
from pathlib import Path
import os

from core.logger import _AUDIT_PATH, get_logger
from core.auth import get_credentials
import gspread

log = get_logger("reporter")

@dataclass
class AuditRecord:
    ts: str
    upload_id: str
    course: str
    sheet_id: str
    tab: str
    cell: str
    old_value: Optional[str]
    new_value: str
    dry_run: bool

    @property
    def col(self) -> str:
        match = re.match(r"([A-Z]+)(\d+)", self.cell)
        return match.group(1) if match else ""
    
    @property
    def row(self) -> int:
        match = re.match(r"([A-Z]+)(\d+)", self.cell)
        return int(match.group(2)) if match else 0

def read_audit_log(path: Optional[Path] = None) -> list[AuditRecord]:
    log_path = path or _AUDIT_PATH
    if not log_path.exists():
        return []
    records = []
    with log_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                records.append(AuditRecord(**data))
            except json.JSONDecodeError:
                continue
    return records

@dataclass
class RevertPlanCell:
    cell: str
    sheet_row: int
    col: str
    revert_to_value: Optional[str]
    current_in_audit: str
    overwritten_since: bool

def plan_revert(upload_id: str, audit_records: list[AuditRecord]) -> list[RevertPlanCell]:
    history_by_cell: dict[str, list[AuditRecord]] = defaultdict(list)
    for r in audit_records:
        if not r.dry_run:
            history_by_cell[r.cell].append(r)
            
    plan = []
    for cell, history in history_by_cell.items():
        writes_by_target = [r for r in history if r.upload_id == upload_id]
        if not writes_by_target:
            continue
            
        revert_to = writes_by_target[0].old_value
        last_write_idx = history.index(writes_by_target[-1])
        overwritten = last_write_idx < len(history) - 1
        current_val = history[-1].new_value
        
        plan.append(RevertPlanCell(
            cell=cell,
            sheet_row=writes_by_target[0].row,
            col=writes_by_target[0].col,
            revert_to_value=revert_to,
            current_in_audit=current_val,
            overwritten_since=overwritten
        ))
    return plan

def get_course_sheet(course: str, cfg: dict) -> gspread.Worksheet:
    course_cfg = cfg["attendance"]["courses"][course]
    sheet_id_env = course_cfg["sheet_id_env"]
    sheet_id = os.environ[sheet_id_env]
    tab = course_cfg["tab"]
    
    creds = get_credentials()
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id)
    return sh.worksheet(tab)

def generate_session_report(course: str, session: int, cfg: dict, review_store_dir: Path) -> dict[str, Any]:
    from attendance.session_resolver import resolve
    session_info = resolve(course, session, None, cfg)
    ws = session_info.get_worksheet()
    
    # 1. Absentees
    col_idx = 0
    for ch in session_info.session_col.upper():
        col_idx = col_idx * 26 + (ord(ch) - ord("A") + 1)
    
    col_data = ws.col_values(col_idx)
    absentees = []
    present_count = 0
    for name, row in session_info.roster:
        val = col_data[row - 1] if row <= len(col_data) else ""
        if val.lower() == "absent" or not val.strip():
            absentees.append(name)
        elif val.strip():
            present_count += 1
            
    # 2. Review backlog
    pending_items = 0
    if review_store_dir.exists():
        for f in review_store_dir.glob("*.json"):
            try:
                with f.open("r", encoding="utf-8") as fh:
                    data = json.load(fh)
                    if data.get("course") == course and data.get("session") == session:
                        pending_items += len([i for i in data.get("items", []) if i.get("status") == "pending"])
            except Exception:
                pass

    return {
        "course": course,
        "session": session,
        "total_students": len(session_info.roster),
        "present": present_count,
        "absentees": absentees,
        "attendance_rate": (present_count / len(session_info.roster)) * 100 if session_info.roster else 0,
        "review_backlog": pending_items
    }

def generate_streaks_report(course: str, consecutive_absences: int, cfg: dict) -> list[dict[str, Any]]:
    # To find the last N sessions, we need to read the sheet header
    course_cfg = cfg["attendance"]["courses"][course]
    header_row = int(course_cfg["header_row"])
    ws = get_course_sheet(course, cfg)
    all_values = ws.get_all_values()
    if len(all_values) < header_row:
        return []
    
    header = all_values[header_row - 1]
    layout = course_cfg.get("layout", "single")
    session_pattern_str = course_cfg["session_header_pattern"] if layout == "single" else course_cfg.get("attendance_header_pattern", r"^S(\d+) Attendance$")
    session_pattern = re.compile(session_pattern_str)
    
    sessions = []
    for col_idx, cell in enumerate(header):
        m = session_pattern.match(cell.strip())
        if m:
            sessions.append((int(m.group(1)), col_idx))
    
    sessions.sort(key=lambda x: x[0])
    last_sessions = sessions[-consecutive_absences:]
    if len(last_sessions) < consecutive_absences:
        return []
    
    # Resolve roster
    from attendance.session_resolver import resolve
    session_info = resolve(course, last_sessions[-1][0], None, cfg)
    
    streaks = []
    for name, row in session_info.roster:
        missed = 0
        for s_num, c_idx in last_sessions:
            val = all_values[row - 1][c_idx] if row <= len(all_values) and c_idx < len(all_values[row - 1]) else ""
            if val.lower() == "absent" or not val.strip():
                missed += 1
            else:
                break
        if missed == consecutive_absences:
            streaks.append({"name": name, "row": row, "missed": missed})
            
    return streaks
