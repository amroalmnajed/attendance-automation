# Configuration Reference

## Overview

The attendance automation system is configured via `config/attendance.yaml`. This document provides a complete reference for all configuration options.

## Configuration Structure

```yaml
core:
  app: {...}
  auth: {...}
  notify: {...}
  retention: {...}
  database: {...}           # NEW
  ml_matching: {...}         # NEW
  caching: {...}             # NEW
  rbac: {...}                # NEW

attendance:
  ingest: {...}
  authorized: {...}
  reminders: {...}
  health: {...}
  summary_compare: {...}
  absent_button: {...}
  ocr: {...}
  match: {...}
  write: {...}
  dead_letter: {...}         # ENHANCED
  concurrency: {...}         # NEW
  review: {...}
  zoom_csv: {...}
  attendance_values: {...}
  courses: {...}
```

## Core Configuration

### core.app

Application-level settings.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| env | string | dev | Environment: dev or prod |
| timezone | string | Africa/Cairo | System timezone for scheduling |
| log_level | string | INFO | Logging level: DEBUG, INFO, WARNING, ERROR |
| dry_run_default | boolean | false | Default dry-run mode for uploads |

### core.auth

Authentication settings.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| credentials_env | string | GOOGLE_APPLICATION_CREDENTIALS | Environment variable for Google credentials |
| secrets_backend | string | env | Secrets backend: env or gcp_secret_manager |

### core.notify

Notification settings.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| channel | string | log | Notification channel: email, telegram, or log |
| recipients_env | string | NOTIFY_TO | Environment variable for notification recipients |
| include_student_names | boolean | false | Whether to include student names in notifications |

### core.retention

Data retention settings.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| image_days | integer | 30 | Days to keep uploaded images |
| flagged_image_days_after_resolve | integer | 30 | Days to keep flagged images after resolution |
| audit_log | string | keep | Audit log retention: keep or expire |

### core.database (NEW)

Database configuration.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| path | string | attendance.db | SQLite database file path |
| sync_interval_seconds | integer | 300 | Sync interval with Google Sheets (seconds) |
| backup_enabled | boolean | true | Enable automatic database backups |
| backup_dir | string | db_backups | Directory for database backups |
| backup_keep_days | integer | 30 | Days to keep backups |

### core.ml_matching (NEW)

ML matching configuration.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| enabled | boolean | true | Enable ML-based semantic matching |
| model | string | paraphrase-multilingual-MiniLM-L12-v2 | Sentence-transformers model name or path |
| cache_embeddings | boolean | true | Cache embeddings for performance |
| retrain_interval_days | integer | 7 | Days between model retraining |
| threshold | float | 0.75 | Cosine similarity threshold for ML matches |
| max_batch_size | integer | 100 | Maximum names to embed in one batch |

### core.caching (NEW)

Caching configuration.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| enabled | boolean | true | Enable caching layer |
| backend | string | sqlite | Cache backend: sqlite |
| path | string | cache.db | Cache database file path |
| roster_ttl | integer | 60 | Roster data TTL (seconds) |
| session_ttl | integer | 60 | Session info TTL (seconds) |
| embedding_ttl | integer | null | Embeddings TTL (null = no expiration) |
| correction_ttl | integer | null | OCR corrections TTL (null = no expiration) |
| permission_ttl | integer | 300 | Permission checks TTL (seconds) |
| max_size_mb | integer | 100 | Maximum cache size (MB) |

### core.rbac (NEW)

RBAC configuration.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| enabled | boolean | true | Enable role-based access control |
| default_role | string | viewer | Default role for new users |
| admin_telegram_ids | list | [] | Telegram IDs with admin access |
| permission_inheritance | boolean | true | Admin inherits all editor permissions |

## Attendance Configuration

### attendance.ingest

Ingestion settings for photos.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| channel | string | cli | Ingestion channel: google_form_drive, drive_folder, or cli |
| poll_interval_s | integer | 300 | Poll interval for automatic ingestion (seconds) |
| form_responses_sheet_id_env | string | INGEST_SHEET_ID | Environment variable for form responses sheet |
| inbox_folder_id_env | string | INBOX_FOLDER_ID | Environment variable for inbox folder |
| fields | object | {...} | Field mappings for form data |
| names_preview | string | on_issues | Names preview mode: always, on_issues, or never |
| multi_photo.enabled | boolean | true | Enable multi-photo uploads |
| multi_photo.max_photos | integer | 6 | Maximum photos per upload |
| multi_photo.append_window_s | integer | 120 | Window to append photos (seconds) |

### attendance.authorized

> Removed: the chat bot (and its `authorized.telegram_ids` allow-list) has been
> retired. Web access is controlled by the webapp's shared-password auth
> (`WEBAPP_PASSWORD`) — see `webapp/README.md`.

### attendance.reminders

Missing attendance reminders.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| enabled | boolean | true | Enable reminders |
| grace_minutes | integer | 60 | Grace period after class end (minutes) |
| window_hours | integer | 12 | Reminder window after class end (hours) |
| check_every_minutes | integer | 10 | Check interval (minutes) |
| recipients | string | both | Recipients: admin, teacher, or both |
| schedule | object | {...} | Per-course schedule |

### attendance.health

Health alerts for admins.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| enabled | boolean | true | Enable health alerts |
| window_minutes | integer | 60 | Failure counting window (minutes) |
| gemini_threshold | integer | 5 | Gemini failure threshold |
| sheets_threshold | integer | 3 | Sheets write failure threshold |
| review_max_age_days | integer | 3 | Alert on stale review items (days) |
| cooldown_minutes | integer | 120 | Minimum time between alerts (minutes) |

### attendance.summary_compare

Post-write summary comparison.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| enabled | boolean | true | Enable summary comparison |
| drop_threshold_pct | integer | 30 | Alert on attendance drop threshold (%) |
| min_previous | integer | 5 | Minimum previous attendees for comparison |

### attendance.absent_button

"Mark N absent" button settings.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| enabled | boolean | true | Enable absent button |
| courses | list | [crash, main] | Courses to offer button for |
| min_matched | integer | 3 | Minimum matched names to offer button |
| block_when_pending_review | boolean | true | Block button when review items pending |

### attendance.ocr

OCR configuration.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| provider | string | freeai | OCR provider: freeai, gemini, or mock |
| model | string | got-ocr2 | Model name |
| freeai_api_key_env | string | FREEAI_API_KEY | Environment variable for API key |
| fallback_to_gemini | boolean | true | Fallback to Gemini on token limit |
| passes | integer | 2 | Number of OCR passes |
| timeout_s | integer | 60 | Request timeout (seconds) |
| max_attempts | integer | 3 | Maximum retry attempts |
| second_provider | string | null | Second provider for second pass |
| second_pass.mode | string | full | Second pass mode: full or low_confidence |
| second_pass.model | string | null | Second pass model (null = same) |
| second_pass.provider | string | null | Second pass provider |
| second_pass.include_unmatched | boolean | true | Include unmatched rows in second pass |
| min_confidence | float | 0.75 | Minimum confidence threshold |
| send_roster_to_provider | boolean | true | Send roster to provider for matching |
| preprocess.orient | boolean | true | Auto-orient image |
| preprocess.deskew | boolean | true | Deskew image |
| preprocess.contrast | string | mild | Contrast enhancement: mild, strong, or none |
| preprocess.max_side_px | integer | 2000 | Maximum image dimension (pixels) |
| preprocess.strip_exif | boolean | true | Strip EXIF metadata |
| retry_preprocessing | boolean | false | Retry with different preprocessing on low confidence |

### attendance.match

Name matching configuration.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| t_high | integer | 90 | Auto-accept threshold |
| margin | integer | 10 | Score margin for auto-accept |
| review_floor | integer | 60 | Minimum score for review (below = NO_MATCH) |
| min_tokens_for_auto | integer | 1 | Minimum tokens for auto-accept |
| t_soft | integer | null | Soft-accept threshold (disabled by default) |
| margin_strong | integer | 20 | Score margin for soft-accept |
| learn_aliases | boolean | true | Learn aliases from review decisions |
| aliases_file | string | config/learned_aliases.json | Aliases file path |
| alias_min_confirmations | integer | 1 | Minimum confirmations for alias |
| variant_learning.enabled | boolean | true | Enable variant learning |
| variant_learning.suggestions_file | string | config/variant_suggestions.json | Suggestions file path |
| variant_learning.min_similarity | float | 0.7 | Minimum similarity for suggestions |
| alias_stale_days | integer | 90 | Days before alias is considered stale |
| weights.token_sort | float | 0.5 | Token-sort ratio weight |
| weights.token_set | float | 0.5 | Token-set ratio weight |
| variants_file | string | config/name_variants.yaml | Name variants file path |
| hidden_rows | string | exclude | Hidden rows handling: exclude or include |
| skip_row_patterns | list | [...] | Regex patterns to skip |
| missing_student_policy | string | report | Policy for missing students: ignore, report, or mark_absent |

### attendance.write

Write configuration.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| session_check | string | strict | Session check mode: strict or lenient |
| overwrite | string | refuse | Overwrite mode: refuse or force |
| values_source | string | sheet | Values source: sheet or config |
| retry.max_attempts | integer | 6 | Maximum retry attempts |
| retry.base_s | float | 1.0 | Base retry delay (seconds) |
| retry.cap_s | float | 32.0 | Maximum retry delay (seconds) |
| retry.jitter | boolean | true | Add jitter to retry delays |

### attendance.dead_letter (ENHANCED)

Dead-letter queue configuration.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| max_retries | integer | 10 | Maximum retry attempts for failed operations |
| expires_after_days | integer | 30 | Days until dead-letter items expire |
| retry_base_seconds | integer | 2 | Base for exponential backoff (seconds) |
| retry_max_seconds | integer | 3600 | Maximum backoff delay (seconds) |
| cleanup_succeeded_days | integer | 7 | Delete succeeded items after (days) |

### attendance.concurrency (NEW)

Concurrency configuration.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| max_workers | integer | 4 | Maximum worker threads |
| timeout | integer | null | Optional timeout for tasks (seconds) |

### attendance.review

Review queue configuration.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| store | string | local_json | Review queue storage: gcs_json or local_json |
| local_dir | string | review_queue | Local directory for review queue |
| mirror_sheet_id_env | string | OPS_SHEET_ID | Environment variable for mirror sheet |
| tab | string | Review | Sheet tab for mirror |

### attendance.zoom_csv

Zoom CSV integration configuration.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| enabled | boolean | true | Enable Zoom CSV integration |
| meeting_id_mappings | object | {...} | Meeting ID to course key mappings |
| teacher_name | string | Yaman | Teacher name for fuzzy matching |
| teacher_fuzzy_match_threshold | integer | 80 | Fuzzy match threshold for teacher |
| teacher_join_buffer_minutes | integer | 5 | Buffer around teacher join time (minutes) |
| late_threshold_minutes | integer | 15 | Late threshold (minutes) |
| min_duration_minutes | integer | 45 | Minimum duration (minutes) |
| early_leave_threshold_minutes | integer | 20 | Early leave threshold (minutes) |
| duplicate_handling | string | sum_duration | Duplicate handling: sum_duration or use_latest |
| auto_session_type | boolean | true | Auto-detect session type |
| auto_archive_csv | boolean | true | Auto-archive processed CSVs |
| archive_folder | string | csv_archive | Archive folder path |
| auto_learn_meeting_ids | boolean | true | Auto-learn meeting IDs |
| csv_strict_validation | boolean | true | Strict CSV validation |
| log_folder | string | logs/zoom_csv | Log folder path |
| zoom_api.account_id_env | string | ZOOM_ACCOUNT_ID | Environment variable for account ID |
| zoom_api.client_id_env | string | ZOOM_CLIENT_ID | Environment variable for client ID |
| zoom_api.client_secret_env | string | ZOOM_CLIENT_SECRET | Environment variable for client secret |
| zoom_api.token_url | string | https://zoom.us/oauth/token | OAuth token URL |
| zoom_api.api_base_url | string | https://api.zoom.us/v2 | API base URL |
| match_overrides.t_high | integer | 76 | Zoom-specific t_high |
| match_overrides.margin | integer | 8 | Zoom-specific margin |
| match_overrides.t_soft | integer | 68 | Zoom-specific t_soft |
| match_overrides.margin_strong | integer | 20 | Zoom-specific margin_strong |

### attendance.attendance_values

Canonical attendance values.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| Present | string | Present | Value for Present |
| Late | string | Late | Value for Late |
| Not complete | string | Not complete | Value for Not complete |
| Absent | string | Absent | Value for Absent |

### attendance.courses

Per-course configuration.

Each course has the following options:

| Option | Type | Description |
|--------|------|-------------|
| sheet_id_env | string | Environment variable for sheet ID |
| tab | string | Sheet tab name |
| layout | string | Layout type: single or typed_pair |
| name_col | string | Column for student names |
| header_row | integer | Row number for session headers |
| session_header_pattern | string | Regex pattern for session headers |
| date_row | integer | Row number for date |
| first_student_row | integer | First row for student data |
| hidden_rows | list | Rows to hide (e.g., [9, 19]) |
| hidden_cols | list | Columns to hide (e.g., [B, C]) |
| allowed_values | list | Allowed attendance values |
| types | object | Session type mappings (for typed_pair layout) |

## Environment Variables

The following environment variables are referenced in the configuration:

| Variable | Purpose |
|----------|---------|
| GOOGLE_APPLICATION_CREDENTIALS | Google Cloud credentials path |
| NOTIFY_TO | Notification recipients (email) |
| INGEST_SHEET_ID | Google Form responses sheet ID |
| INBOX_FOLDER_ID | Drive inbox folder ID |
| FREEAI_API_KEY | FreeAI API key for OCR |
| OPS_SHEET_ID | Operations/mirror sheet ID |
| CRASH_SHEET_ID | Crash course sheet ID |
| MAIN_SHEET_ID | Main course sheet ID |
| G10_SHEET_ID | G10 course sheet ID |
| G9_SHEET_ID | G9 course sheet ID |
| ZOOM_ACCOUNT_ID | Zoom account ID |
| ZOOM_CLIENT_ID | Zoom OAuth client ID |
| ZOOM_CLIENT_SECRET | Zoom OAuth client secret |
| HF_TOKEN | Hugging Face token for model downloads |

## Migration Notes

### New Configuration Sections

The following sections were added in the modernization:

1. `core.database` - SQLite database configuration
2. `core.ml_matching` - ML matching configuration
3. `core.caching` - Caching layer configuration
4. `core.rbac` - RBAC configuration
5. `attendance.concurrency` - Concurrency configuration
6. `attendance.dead_letter` - Enhanced dead-letter queue

### Deprecated Options

No options have been deprecated. All existing configuration remains compatible.

### Default Values

All new configuration sections have sensible defaults. The system will work without explicit configuration of new sections.
