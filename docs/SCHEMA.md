# Database Schema Documentation

## Overview

The attendance automation system uses SQLite as the primary data store with SQLAlchemy ORM. The schema is designed to support courses, students, sessions, attendance records, audit logs, users, permissions, learned aliases, OCR corrections, and historical patterns.

## Schema Diagram

```
Course (1) ----< (N) Student
Course (1) ----< (N) Session
Course (1) ----< (N) Alias
Course (1) ----< (N) Pattern
Course (1) ----< (N) CoursePermission

Student (1) ----< (N) AttendanceRecord
Session (1) ----< (N) AttendanceRecord

User (1) ----< (N) CoursePermission
User (1) ----< (N) AuditLog

AttendanceRecord (N) ----< (1) Student
AttendanceRecord (N) ----< (1) Session
```

## Table Definitions

### courses

Stores course metadata and configuration.

| Column | Type | Description |
|--------|------|-------------|
| course_id | Integer (PK) | Primary key |
| course_key | String (unique) | Course identifier (e.g., "main", "crash", "g9", "g10") |
| name | String | Human-readable course name |
| sheet_id | String | Google Sheets ID |
| tab | String | Sheet tab name |
| layout | String | Sheet layout type ("single" or "typed_pair") |
| config_json | JSON | Additional course configuration |
| created_at | DateTime | Record creation timestamp |
| updated_at | DateTime | Last update timestamp |

**Indexes**:
- `ix_courses_course_key` (unique)
- `ix_courses_sheet_id`

### students

Stores student roster information.

| Column | Type | Description |
|--------|------|-------------|
| student_id | Integer (PK) | Primary key |
| course_id | Integer (FK) | Reference to courses.course_id |
| name | String | Student name |
| sheet_row | Integer | Row number in Google Sheets |
| is_active | Boolean | Whether student is active |
| created_at | DateTime | Record creation timestamp |
| updated_at | DateTime | Last update timestamp |

**Indexes**:
- `ix_students_course_id`
- `ix_students_name`
- `ix_students_course_id_name` (composite)

**Foreign Keys**:
- `course_id` → `courses.course_id`

### sessions

Stores session metadata.

| Column | Type | Description |
|--------|------|-------------|
| session_id | Integer (PK) | Primary key |
| course_id | Integer (FK) | Reference to courses.course_id |
| session_num | Integer | Session number (e.g., 1, 2, 3...) |
| session_type | String | Session type (e.g., "Saturday - Center") |
| date | String | Session date (text format) |
| session_col | String | Column letter for attendance values |
| type_col | String | Column letter for session type |
| created_at | DateTime | Record creation timestamp |
| updated_at | DateTime | Last update timestamp |

**Indexes**:
- `ix_sessions_course_id`
- `ix_sessions_course_id_session_num` (unique composite)

**Foreign Keys**:
- `course_id` → `courses.course_id`

### attendance_records

Stores attendance values for student-session pairs.

| Column | Type | Description |
|--------|------|-------------|
| record_id | Integer (PK) | Primary key |
| student_id | Integer (FK) | Reference to students.student_id |
| session_id | Integer (FK) | Reference to sessions.session_id |
| value | String | Attendance value (Present, Late, Absent, Not complete) |
| uploaded_at | DateTime | When the record was uploaded |
| uploaded_by | String | User who uploaded (or system) |
| created_at | DateTime | Record creation timestamp |
| updated_at | DateTime | Last update timestamp |

**Indexes**:
- `ix_attendance_records_student_id`
- `ix_attendance_records_session_id`
- `ix_attendance_records_student_id_session_id` (unique composite)

**Foreign Keys**:
- `student_id` → `students.student_id`
- `session_id` → `sessions.session_id`

### audit_log

Stores audit trail for all changes.

| Column | Type | Description |
|--------|------|-------------|
| log_id | Integer (PK) | Primary key |
| action | String | Action performed (e.g., "create", "update", "delete") |
| entity_type | String | Type of entity (e.g., "student", "attendance_record") |
| entity_id | Integer | ID of the affected entity |
| old_value | JSON | Previous value (for updates) |
| new_value | JSON | New value |
| user | String | User who performed the action |
| timestamp | DateTime | When the action occurred |
| details | JSON | Additional details |

**Indexes**:
- `ix_audit_log_entity_type`
- `ix_audit_log_entity_id`
- `ix_audit_log_user`
- `ix_audit_log_timestamp`

### users

Stores user accounts and roles.

| Column | Type | Description |
|--------|------|-------------|
| user_id | Integer (PK) | Primary key |
| username | String (unique) | Username |
| telegram_id | String | Telegram user ID |
| role | String | User role (viewer, editor, admin) |
| permissions_json | JSON | Additional permissions |
| created_at | DateTime | Record creation timestamp |
| updated_at | DateTime | Last update timestamp |

**Indexes**:
- `ix_users_username` (unique)
- `ix_users_telegram_id`

### course_permissions

Stores per-course access control.

| Column | Type | Description |
|--------|------|-------------|
| permission_id | Integer (PK) | Primary key |
| user_id | Integer (FK) | Reference to users.user_id |
| course_id | Integer (FK) | Reference to courses.course_id |
| permission_level | String | Permission level (view, edit, admin) |
| created_at | DateTime | Record creation timestamp |
| updated_at | DateTime | Last update timestamp |

**Indexes**:
- `ix_course_permissions_user_id`
- `ix_course_permissions_course_id`
- `ix_course_permissions_user_id_course_id` (unique composite)

**Foreign Keys**:
- `user_id` → `users.user_id`
- `course_id` → `courses.course_id`

### aliases

Stores learned name aliases from review decisions.

| Column | Type | Description |
|--------|------|-------------|
| alias_id | Integer (PK) | Primary key |
| course_id | Integer (FK) | Reference to courses.course_id |
| raw_name_key | String | Raw OCR name as key |
| roster_name | String | Correct roster name |
| confirmations | Integer | Number of confirmations |
| first_seen | DateTime | When this alias was first seen |
| last_used | DateTime | When this alias was last used |
| created_at | DateTime | Record creation timestamp |
| updated_at | DateTime | Last update timestamp |

**Indexes**:
- `ix_aliases_course_id`
- `ix_aliases_raw_name_key`
- `ix_aliases_course_id_raw_name_key` (unique composite)

**Foreign Keys**:
- `course_id` → `courses.course_id`

### ocr_corrections

Stores learned OCR error corrections.

| Column | Type | Description |
|--------|------|-------------|
| correction_id | Integer (PK) | Primary key |
| raw_text | String | Raw OCR text to correct |
| corrected_text | String | Corrected text |
| frequency | Integer | How often this correction was applied |
| last_applied | DateTime | When this correction was last applied |
| created_at | DateTime | Record creation timestamp |
| updated_at | DateTime | Last update timestamp |

**Indexes**:
- `ix_ocr_corrections_raw_text`
- `ix_ocr_corrections_frequency`

### patterns

Stores historical patterns for smart suggestions.

| Column | Type | Description |
|--------|------|-------------|
| pattern_id | Integer (PK) | Primary key |
| course_id | Integer (FK) | Reference to courses.course_id |
| pattern_type | String | Type of pattern (e.g., "session_type", "name_variant") |
| pattern_value | String | Pattern value |
| confidence | Float | Confidence score (0-1) |
| created_at | DateTime | Record creation timestamp |
| updated_at | DateTime | Last update timestamp |

**Indexes**:
- `ix_patterns_course_id`
- `ix_patterns_pattern_type`
- `ix_patterns_course_id_pattern_type` (composite)

**Foreign Keys**:
- `course_id` → `courses.course_id`

## Relationships

### Course Relationships
- A course has many students
- A course has many sessions
- A course has many aliases
- A course has many patterns
- A course has many course permissions

### Student Relationships
- A student belongs to one course
- A student has many attendance records

### Session Relationships
- A session belongs to one course
- A session has many attendance records

### Attendance Record Relationships
- An attendance record belongs to one student
- An attendance record belongs to one session

### User Relationships
- A user has many course permissions
- A user has many audit log entries

### Course Permission Relationships
- A course permission belongs to one user
- A course permission belongs to one course

## Data Integrity

### Constraints
- All foreign key relationships are enforced
- Unique constraints on course_key, username, and composite keys
- Not null constraints on required fields

### Cascading Deletes
- Deleting a course cascades to students, sessions, aliases, patterns, and course permissions
- Deleting a student cascades to attendance records
- Deleting a session cascades to attendance records
- Deleting a user cascades to course permissions (but not audit logs for historical preservation)

## Migration Strategy

The schema is managed using Alembic migrations:
- All schema changes go through migration scripts
- Migrations are versioned and reversible
- Data migration utilities (attendance/migrate.py) handle data import from Google Sheets

## Backup and Recovery

- Database is backed up automatically (configurable interval)
- Backups are stored in `db_backups/` directory
- 30-day retention policy for backups
- Course-level rollback available via migration utilities
