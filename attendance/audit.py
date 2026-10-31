"""attendance/audit.py — Comprehensive audit logging system.

Logs all data changes, user actions, and system events to the database.
Provides query interface for audit log viewer in webapp.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from attendance.db import get_session, AuditLog


def log_action(
    action: str,
    entity_type: str,
    entity_id: Optional[str] = None,
    old_value: Optional[Any] = None,
    new_value: Optional[Any] = None,
    user: Optional[str] = None,
    metadata: Optional[dict] = None,
    db_path: Optional[str] = None,
) -> None:
    """Log an action to the audit database.

    Args:
        action: The action performed (e.g., "resolve_item", "write_attendance", "edit_roster").
        entity_type: Type of entity affected (e.g., "attendance_record", "student", "course").
        entity_id: ID of the affected entity.
        old_value: Previous value (for change tracking).
        new_value: New value (for change tracking).
        user: User who performed the action (username or identifier).
        metadata: Additional context as a dictionary.
        db_path: Optional database path to ensure consistency across contexts.
    """
    try:
        # Only initialize engine if a specific db_path is provided
        # Otherwise, get_session() will handle lazy initialization
        if db_path:
            from attendance.db import init_db
            init_db(db_path)
        session = get_session()
        try:
            # Serialize complex values to JSON
            old_json = json.dumps(old_value) if old_value is not None else None
            new_json = json.dumps(new_value) if new_value is not None else None
            meta_json = json.dumps(metadata) if metadata else None

            log_entry = AuditLog(
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                old_value=old_json,
                new_value=new_json,
                user=user,
                context=meta_json,
                timestamp=datetime.now(timezone.utc),
            )
            session.add(log_entry)
            session.commit()
        finally:
            session.close()
    except Exception as exc:
        # Audit logging failures should not break the main flow
        # Log to stderr as fallback
        import sys
        print(f"Audit logging failed: {exc}", file=sys.stderr)


def log_resolve_item(
    upload_id: str,
    item_id: str,
    chosen_roster_row: int,
    attendance_value: str,
    resolved_by: str,
    course: Optional[str] = None,
    session_num: Optional[int] = None,
    db_path: Optional[str] = None,
) -> None:
    """Log a review item resolution."""
    log_action(
        action="resolve_item",
        entity_type="review_item",
        entity_id=item_id,
        old_value=None,
        new_value={
            "upload_id": upload_id,
            "roster_row": chosen_roster_row,
            "attendance_value": attendance_value,
        },
        user=resolved_by,
        metadata={
            "course": course,
            "session_num": session_num,
        },
        db_path=db_path,
    )


def log_write_attendance(
    upload_id: str,
    course: str,
    sheet_id: str,
    tab: str,
    cell: str,
    old_value: Optional[str],
    new_value: str,
    user: Optional[str] = None,
    db_path: Optional[str] = None,
) -> None:
    """Log an attendance write to Google Sheets."""
    log_action(
        action="write_attendance",
        entity_type="attendance_record",
        entity_id=f"{sheet_id}/{tab}/{cell}",
        old_value=old_value,
        new_value=new_value,
        user=user,
        metadata={
            "upload_id": upload_id,
            "course": course,
            "sheet_id": sheet_id,
            "tab": tab,
            "cell": cell,
        },
        db_path=db_path,
    )


def log_edit_roster(
    course: str,
    student_id: str,
    old_name: Optional[str],
    new_name: str,
    user: Optional[str] = None,
    db_path: Optional[str] = None,
) -> None:
    """Log a roster edit."""
    log_action(
        action="edit_roster",
        entity_type="student",
        entity_id=student_id,
        old_value=old_name,
        new_value=new_name,
        user=user,
        metadata={"course": course},
        db_path=db_path,
    )


def log_user_action(
    action: str,
    user: str,
    metadata: Optional[dict] = None,
    db_path: Optional[str] = None,
) -> None:
    """Log a general user action (e.g., login, view, search)."""
    log_action(
        action=action,
        entity_type="user_action",
        entity_id=None,
        old_value=None,
        new_value=None,
        user=user,
        metadata=metadata,
        db_path=db_path,
    )


def log_system_event(
    event: str,
    metadata: Optional[dict] = None,
    db_path: Optional[str] = None,
) -> None:
    """Log a system event (e.g., sync, error, retry)."""
    log_action(
        action=event,
        entity_type="system_event",
        entity_id=None,
        old_value=None,
        new_value=None,
        user="system",
        metadata=metadata,
        db_path=db_path,
    )


def query_audit_logs(
    action: Optional[str] = None,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    user: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """Query audit logs with optional filters.

    Args:
        action: Filter by action type.
        entity_type: Filter by entity type.
        entity_id: Filter by entity ID.
        user: Filter by user.
        start_date: Filter logs after this date.
        end_date: Filter logs before this date.
        limit: Maximum number of results.
        offset: Offset for pagination.

    Returns:
        List of audit log entries as dictionaries.
    """
    try:
        session = get_session()
        try:
            query = session.query(AuditLog)

            if action:
                query = query.filter(AuditLog.action == action)
            if entity_type:
                query = query.filter(AuditLog.entity_type == entity_type)
            if entity_id:
                query = query.filter(AuditLog.entity_id == entity_id)
            if user:
                query = query.filter(AuditLog.user == user)
            if start_date:
                query = query.filter(AuditLog.timestamp >= start_date)
            if end_date:
                query = query.filter(AuditLog.timestamp <= end_date)

            query = query.order_by(AuditLog.timestamp.desc())
            query = query.limit(limit).offset(offset)

            results = []
            for log in query.all():
                result = {
                    "log_id": log.log_id,
                    "action": log.action,
                    "entity_type": log.entity_type,
                    "entity_id": log.entity_id,
                    "old_value": json.loads(log.old_value) if log.old_value else None,
                    "new_value": json.loads(log.new_value) if log.new_value else None,
                    "user": log.user,
                    "metadata": json.loads(log.context) if log.context else None,
                    "timestamp": log.timestamp.isoformat() if log.timestamp else None,
                }
                results.append(result)

            return results
        finally:
            session.close()
    except Exception as exc:
        import sys
        print(f"Audit query failed: {exc}", file=sys.stderr)
        return []


def get_audit_summary(
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
) -> dict:
    """Get a summary of audit log statistics.

    Args:
        start_date: Start date for summary.
        end_date: End date for summary.

    Returns:
        Dictionary with summary statistics.
    """
    try:
        session = get_session()
        try:
            query = session.query(AuditLog)

            if start_date:
                query = query.filter(AuditLog.timestamp >= start_date)
            if end_date:
                query = query.filter(AuditLog.timestamp <= end_date)

            total_logs = query.count()

            # Count by action and user in a single pass
            action_counts = {}
            user_counts = {}
            for log in query.all():
                action_counts[log.action] = action_counts.get(log.action, 0) + 1
                user_counts[log.user] = user_counts.get(log.user, 0) + 1

            return {
                "total_logs": total_logs,
                "action_counts": action_counts,
                "user_counts": user_counts,
            }
        finally:
            session.close()
    except Exception as exc:
        import sys
        print(f"Audit summary failed: {exc}", file=sys.stderr)
        return {
            "total_logs": 0,
            "action_counts": {},
            "user_counts": {},
        }


def cleanup_old_logs(retention_days: int = 365) -> int:
    """Delete audit logs older than the retention period.

    Args:
        retention_days: Number of days to retain logs.

    Returns:
        Number of logs deleted.
    """
    try:
        from datetime import timedelta

        cutoff_date = datetime.now(timezone.utc) - timedelta(days=retention_days)

        session = get_session()
        try:
            deleted = session.query(AuditLog).filter(
                AuditLog.timestamp < cutoff_date
            ).delete()
            session.commit()
            return deleted
        finally:
            session.close()
    except Exception as exc:
        import sys
        print(f"Audit cleanup failed: {exc}", file=sys.stderr)
        return 0
