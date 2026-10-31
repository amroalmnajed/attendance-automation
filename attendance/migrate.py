"""attendance/migrate.py — Data migration utilities for modernization.

Provides utilities to:
- Export existing Google Sheets data to SQLite
- Import learned_aliases.json into the database
- Backfill audit logs from existing log files
- Verify data integrity
- Support rollback procedures
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from attendance.db import (
    Alias,
    AttendanceRecord,
    AuditLog,
    Course,
    Session as DBSession,
    Student,
    create_tables,
    get_session,
    SessionLocal,
)
from attendance.audit import log_action
from attendance.session_resolver import resolve

logger = logging.getLogger(__name__)


class DataMigrator:
    """Handles data migration from Google Sheets and learned_aliases.json to SQLite."""

    def __init__(self, db_path: Optional[str] = None):
        """Initialize the migrator.

        Args:
            db_path: Optional custom database path
        """
        self.db_path = db_path
        self.session = SessionLocal()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.session.close()

    def export_course_from_sheets(
        self,
        course_key: str,
        sheet_id: str,
        tab: str,
        layout: str = "single",
    ) -> dict:
        """Export course data from Google Sheets to SQLite.

        Args:
            course_key: Course identifier (e.g., "main", "crash", "g9", "g10")
            sheet_id: Google Sheets ID
            tab: Sheet tab name
            layout: Sheet layout type (single or typed_pair)

        Returns:
            Summary dict with counts of exported records
        """
        logger.info(f"Exporting course {course_key} from sheet {sheet_id}/{tab}")

        # Load config to use resolve function
        import yaml
        import os
        with open("config/attendance.yaml", "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        
        # Use resolve to fetch roster data (session_num 1, no type for single layout)
        sheet_session = resolve(course_key, session_num=1, session_type=None, cfg=config)
        roster = sheet_session.roster

        # Create or update course record
        course = self.session.query(Course).filter_by(course_key=course_key).first()
        if not course:
            course = Course(
                course_key=course_key,
                name=course_key.capitalize(),
                sheet_id=sheet_id,
                tab=tab,
                layout=layout,
                config_json=json.dumps({"layout": layout}),
            )
            self.session.add(course)
            self.session.commit()
            logger.info(f"Created course record for {course_key}")
        else:
            logger.info(f"Using existing course record for {course_key}")

        # Import students
        students_added = 0
        students_updated = 0
        for name, sheet_row in roster:
            student = self.session.query(Student).filter_by(
                course_id=course.course_id,
                name=name,
            ).first()

            if not student:
                student = Student(
                    course_id=course.course_id,
                    name=name,
                    sheet_row=sheet_row,
                    is_active=True,
                )
                self.session.add(student)
                students_added += 1
            else:
                # Update sheet row if changed
                if student.sheet_row != sheet_row:
                    student.sheet_row = sheet_row
                    students_updated += 1

        self.session.commit()

        summary = {
            "course_key": course_key,
            "students_added": students_added,
            "students_updated": students_updated,
            "total_students": len(roster),
        }

        logger.info(f"Export summary for {course_key}: {summary}")
        return summary

    def import_learned_aliases(self, aliases_file: str = "config/learned_aliases.json") -> dict:
        """Import learned aliases from JSON file into database.

        Args:
            aliases_file: Path to learned_aliases.json

        Returns:
            Summary dict with counts of imported aliases
        """
        logger.info(f"Importing learned aliases from {aliases_file}")

        aliases_path = Path(aliases_file)
        if not aliases_path.exists():
            logger.warning(f"Aliases file not found: {aliases_file}")
            return {"aliases_imported": 0, "courses": {}}

        with open(aliases_path, "r", encoding="utf-8") as f:
            aliases_data = json.load(f)

        total_imported = 0
        course_summaries = {}

        for course_key, aliases in aliases_data.items():
            # Get course ID
            course = self.session.query(Course).filter_by(course_key=course_key).first()
            if not course:
                logger.warning(f"Course {course_key} not found, skipping aliases")
                continue

            imported = 0
            for raw_name, alias_info in aliases.items():
                if isinstance(alias_info, dict):
                    roster_name = alias_info.get("roster_name", raw_name)
                    confirmations = alias_info.get("confirmations", 1)
                    first_seen = alias_info.get("first_seen")
                    last_used = alias_info.get("last_used")
                else:
                    # Legacy format: raw_name -> roster_name
                    roster_name = alias_info
                    confirmations = 1
                    first_seen = None
                    last_used = None

                # Check if alias already exists
                existing = self.session.query(Alias).filter_by(
                    course_id=course.course_id,
                    raw_name_key=raw_name,
                    roster_name=roster_name,
                ).first()

                if not existing:
                    alias = Alias(
                        course_id=course.course_id,
                        raw_name_key=raw_name,
                        roster_name=roster_name,
                        confirmations=confirmations,
                        first_seen=datetime.fromisoformat(first_seen) if first_seen else datetime.now(timezone.utc),
                        last_used=datetime.fromisoformat(last_used) if last_used else datetime.now(timezone.utc),
                    )
                    self.session.add(alias)
                    imported += 1
                else:
                    # Update confirmations if higher
                    if confirmations > existing.confirmations:
                        existing.confirmations = confirmations
                        if last_used:
                            existing.last_used = datetime.fromisoformat(last_used)

            course_summaries[course_key] = imported
            total_imported += imported

        self.session.commit()

        summary = {
            "aliases_imported": total_imported,
            "courses": course_summaries,
        }

        logger.info(f"Imported {total_imported} aliases across {len(course_summaries)} courses")
        return summary

    def export_sessions_from_sheets(
        self,
        course_key: str,
    ) -> dict:
        """Export session metadata from Google Sheets to SQLite.

        Args:
            course_key: Course identifier

        Returns:
            Summary dict with counts of exported sessions
        """
        logger.info(f"Exporting sessions for course {course_key}")

        # Get course
        course = self.session.query(Course).filter_by(course_key=course_key).first()
        if not course:
            logger.error(f"Course {course_key} not found")
            return {"sessions_added": 0}

        # Load config to use session_resolver
        import yaml
        import os
        import re
        from attendance.utils.column_utils import index_to_col_letter

        with open("config/attendance.yaml", "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)

        course_cfg = config["attendance"]["courses"][course_key]
        sheet_id_env = course_cfg["sheet_id_env"]
        sheet_id = os.environ.get(sheet_id_env)
        tab = course_cfg["tab"]
        layout = course_cfg.get("layout", "single")
        header_row = int(course_cfg["header_row"])

        if not sheet_id:
            logger.error(f"Environment variable {sheet_id_env} not set for course {course_key}")
            return {"sessions_added": 0}

        # Use session_resolver to get worksheet access
        try:
            sheet_session = resolve(course_key, session_num=1, session_type=None, cfg=config)
            ws = sheet_session.get_worksheet()
        except Exception as e:
            logger.error(f"Failed to access sheet for session discovery: {e}")
            return {"sessions_added": 0}

        # Read header row to discover all session columns
        all_values = ws.get_all_values()
        if len(all_values) < header_row:
            logger.error(f"Sheet has fewer rows than expected header row {header_row}")
            return {"sessions_added": 0}

        header = all_values[header_row - 1]  # 0-based index

        # Discover sessions based on layout
        sessions = {}
        session_pattern = re.compile(course_cfg["session_header_pattern"])

        if layout == "single":
            # Find all columns matching session pattern
            for col_idx, cell in enumerate(header):
                m = session_pattern.match(cell.strip())
                if m:
                    session_num = int(m.group(1))
                    session_col = index_to_col_letter(col_idx + 1)  # 1-based
                    sessions[session_num] = {
                        "col": session_col,
                        "type_col": None,
                    }

        elif layout == "typed_pair":
            att_pattern_str = course_cfg.get("attendance_header_pattern", r"^S(\d+) Attendance$")
            att_pattern = re.compile(att_pattern_str)
            # Find all "S{n} Attendance" columns
            for col_idx, cell in enumerate(header):
                m = att_pattern.match(cell.strip())
                if m:
                    session_num = int(m.group(1))
                    session_col = index_to_col_letter(col_idx + 1)  # 1-based
                    type_col = index_to_col_letter(col_idx)  # 0-based → 1-based = col_idx
                    sessions[session_num] = {
                        "col": session_col,
                        "type_col": type_col,
                    }

        sessions_added = 0
        for session_num, session_info in sessions.items():
            existing = self.session.query(DBSession).filter_by(
                course_id=course.course_id,
                session_num=session_num,
            ).first()

            if not existing:
                db_session = DBSession(
                    course_id=course.course_id,
                    session_num=session_num,
                    session_type=session_info.get("type"),
                    date=session_info.get("date"),
                    session_col=session_info.get("col"),
                    type_col=session_info.get("type_col"),
                )
                self.session.add(db_session)
                sessions_added += 1

        self.session.commit()

        summary = {
            "course_key": course_key,
            "sessions_added": sessions_added,
            "total_sessions": len(sessions),
        }

        logger.info(f"Exported {sessions_added} sessions for {course_key}")
        return summary

    def backfill_audit_logs(self, logs_dir: str = "logs") -> dict:
        """Backfill audit logs from existing log files into the database.

        Args:
            logs_dir: Directory containing log files

        Returns:
            Summary dict with counts of backfilled log entries
        """
        logger.info(f"Backfilling audit logs from {logs_dir}")

        logs_path = Path(logs_dir)
        if not logs_path.exists():
            logger.warning(f"Logs directory not found: {logs_dir}")
            return {"entries_backfilled": 0, "files_processed": 0}

        log_files = list(logs_path.glob("*.log"))
        if not log_files:
            logger.warning(f"No log files found in {logs_dir}")
            return {"entries_backfilled": 0, "files_processed": 0}

        total_backfilled = 0
        files_processed = 0

        # Simple log parsing - assumes log format with timestamp, level, message
        # This is a basic implementation; adjust based on actual log format
        import re
        log_pattern = re.compile(
            r'(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[.\d]*[Z]?).*?'
            r'\[(\w+)\].*?'
            r'action=(\w+).*?'
            r'user=(\S+).*?'
            r'(?:item_id=(\S+))?'
        )

        for log_file in log_files:
            logger.info(f"Processing log file: {log_file}")
            try:
                with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue

                        match = log_pattern.search(line)
                        if match:
                            timestamp_str, level, action, user, item_id = match.groups()

                            try:
                                # Parse timestamp
                                if "T" in timestamp_str:
                                    if timestamp_str.endswith("Z"):
                                        timestamp_str = timestamp_str[:-1]
                                    timestamp = datetime.fromisoformat(timestamp_str).replace(tzinfo=timezone.utc)
                                else:
                                    # Assume local time without timezone
                                    timestamp = datetime.fromisoformat(timestamp_str)

                                # Check if this log entry already exists
                                existing = self.session.query(AuditLog).filter_by(
                                    action=action,
                                    user=user,
                                    timestamp=timestamp,
                                ).first()

                                if not existing:
                                    audit_entry = AuditLog(
                                        action=action,
                                        user=user,
                                        item_id=item_id,
                                        details=line[:1000],  # Truncate if too long
                                        timestamp=timestamp,
                                    )
                                    self.session.add(audit_entry)
                                    total_backfilled += 1

                            except (ValueError, TypeError) as e:
                                logger.debug(f"Failed to parse log line: {e}")
                                continue

                files_processed += 1
                # Commit per file to avoid large transactions
                self.session.commit()

            except Exception as e:
                logger.error(f"Error processing log file {log_file}: {e}")
                self.session.rollback()
                continue

        summary = {
            "entries_backfilled": total_backfilled,
            "files_processed": files_processed,
        }

        logger.info(f"Backfilled {total_backfilled} audit log entries from {files_processed} files")
        return summary

    def verify_integrity(self) -> dict:
        """Verify data integrity after migration.

        Returns:
            Summary dict with integrity check results
        """
        logger.info("Running integrity checks")

        checks = {
            "courses": self.session.query(Course).count(),
            "students": self.session.query(Student).count(),
            "sessions": self.session.query(DBSession).count(),
            "aliases": self.session.query(Alias).count(),
            "attendance_records": self.session.query(AttendanceRecord).count(),
        }

        # Check for orphaned students
        orphaned_students = self.session.query(Student).filter(
            ~Student.course_id.in_(self.session.query(Course.course_id))
        ).count()

        # Check for orphaned sessions
        orphaned_sessions = self.session.query(DBSession).filter(
            ~DBSession.course_id.in_(self.session.query(Course.course_id))
        ).count()

        # Check for orphaned aliases
        orphaned_aliases = self.session.query(Alias).filter(
            ~Alias.course_id.in_(self.session.query(Course.course_id))
        ).count()

        # Check for orphaned attendance records (orphaned by student)
        orphaned_attendance_student = self.session.query(AttendanceRecord).filter(
            ~AttendanceRecord.student_id.in_(self.session.query(Student.student_id))
        ).count()

        # Check for orphaned attendance records (orphaned by session)
        orphaned_attendance_session = self.session.query(AttendanceRecord).filter(
            ~AttendanceRecord.session_id.in_(self.session.query(DBSession.session_id))
        ).count()

        checks["orphaned_students"] = orphaned_students
        checks["orphaned_sessions"] = orphaned_sessions
        checks["orphaned_aliases"] = orphaned_aliases
        checks["orphaned_attendance_student"] = orphaned_attendance_student
        checks["orphaned_attendance_session"] = orphaned_attendance_session

        is_healthy = (
            orphaned_students == 0
            and orphaned_sessions == 0
            and orphaned_aliases == 0
            and orphaned_attendance_student == 0
            and orphaned_attendance_session == 0
        )

        logger.info(f"Integrity check results: {checks}")
        return {
            "healthy": is_healthy,
            "checks": checks,
        }

    def rollback_course(self, course_key: str) -> dict:
        """Rollback migration for a specific course.

        Args:
            course_key: Course identifier to rollback

        Returns:
            Summary dict with counts of deleted records
        """
        logger.warning(f"Rolling back course {course_key}")

        course = self.session.query(Course).filter_by(course_key=course_key).first()
        if not course:
            logger.error(f"Course {course_key} not found")
            return {"deleted": 0}

        course_id = course.course_id

        # Delete attendance records first (child records)
        # Delete via student_id
        attendance_by_student = self.session.query(AttendanceRecord).filter(
            AttendanceRecord.student_id.in_(
                self.session.query(Student.student_id).filter_by(course_id=course_id)
            )
        ).count()
        self.session.query(AttendanceRecord).filter(
            AttendanceRecord.student_id.in_(
                self.session.query(Student.student_id).filter_by(course_id=course_id)
            )
        ).delete(synchronize_session=False)

        # Delete via session_id
        attendance_by_session = self.session.query(AttendanceRecord).filter(
            AttendanceRecord.session_id.in_(
                self.session.query(DBSession.session_id).filter_by(course_id=course_id)
            )
        ).count()
        self.session.query(AttendanceRecord).filter(
            AttendanceRecord.session_id.in_(
                self.session.query(DBSession.session_id).filter_by(course_id=course_id)
            )
        ).delete(synchronize_session=False)

        total_attendance_deleted = attendance_by_student + attendance_by_session

        # Delete aliases
        aliases_deleted = self.session.query(Alias).filter_by(course_id=course_id).count()
        self.session.query(Alias).filter_by(course_id=course_id).delete()

        # Delete students
        students_deleted = self.session.query(Student).filter_by(course_id=course_id).count()
        self.session.query(Student).filter_by(course_id=course_id).delete()

        # Delete sessions
        sessions_deleted = self.session.query(DBSession).filter_by(course_id=course_id).count()
        self.session.query(DBSession).filter_by(course_id=course_id).delete()

        # Delete course
        self.session.query(Course).filter_by(course_id=course_id).delete()

        self.session.commit()

        summary = {
            "course_key": course_key,
            "attendance_records_deleted": total_attendance_deleted,
            "aliases_deleted": aliases_deleted,
            "students_deleted": students_deleted,
            "sessions_deleted": sessions_deleted,
        }

        logger.warning(f"Rollback complete for {course_key}: {summary}")
        return summary


def migrate_all_courses(config_file: str = "config/attendance.yaml") -> dict:
    """Run full migration for all configured courses.

    Args:
        config_file: Path to attendance.yaml config

    Returns:
        Summary dict with migration results
    """
    import yaml

    logger.info("Starting full migration for all courses")

    with open(config_file, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    courses_config = config.get("attendance", {}).get("courses", {})

    with DataMigrator() as migrator:
        results = {}

        # Export courses and students
        for course_key, course_config in courses_config.items():
            sheet_id = course_config.get("sheet_id_env")
            tab = course_config.get("tab")
            layout = course_config.get("layout", "single")

            if not sheet_id:
                logger.warning(f"No sheet_id_env for course {course_key}, skipping")
                continue

            # Get actual sheet ID from environment or config
            import os
            sheet_id_actual = os.environ.get(sheet_id)
            if not sheet_id_actual:
                logger.warning(f"Environment variable {sheet_id} not set for course {course_key}, skipping")
                continue

            try:
                result = migrator.export_course_from_sheets(
                    course_key=course_key,
                    sheet_id=sheet_id_actual,
                    tab=tab,
                    layout=layout,
                )
                results[f"{course_key}_course"] = result

                # Export sessions
                session_result = migrator.export_sessions_from_sheets(course_key)
                results[f"{course_key}_sessions"] = session_result

            except Exception as e:
                logger.error(f"Failed to migrate course {course_key}: {e}")
                results[f"{course_key}_error"] = str(e)

        # Import learned aliases
        alias_result = migrator.import_learned_aliases()
        results["aliases"] = alias_result

        # Backfill audit logs from log files
        audit_backfill = migrator.backfill_audit_logs()
        results["audit_backfill"] = audit_backfill

        # Verify integrity
        integrity = migrator.verify_integrity()
        results["integrity"] = integrity

    logger.info(f"Migration complete: {results}")
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Initialize database tables
    create_tables()

    # Run migration
    results = migrate_all_courses()

    print("\n=== Migration Results ===")
    print(json.dumps(results, indent=2, default=str))
