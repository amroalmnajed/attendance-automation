# Attendance Automation Architecture

## Overview

The attendance automation system is a modern Python application that processes attendance photos from multiple sources (Telegram, Google Forms, Drive folders), performs OCR-based name recognition, matches names to course rosters, and writes attendance records to Google Sheets. The system now includes a local SQLite database with bidirectional synchronization, ML-powered name matching, smart suggestions, caching, concurrency, error recovery, and role-based access control.

## System Components

### Core Modules

#### attendance/db.py
- **Purpose**: SQLAlchemy ORM models and database operations
- **Database**: SQLite with WAL mode for concurrent access
- **Models**:
  - `Course`: Course metadata (sheet_id, tab, layout, config)
  - `Student`: Student roster (name, sheet_row, is_active)
  - `Session`: Session metadata (session_num, type, date, columns)
  - `AttendanceRecord`: Attendance values (student_id, session_id, value)
  - `AuditLog`: Audit trail for all changes
  - `User`: User accounts with roles
  - `CoursePermission`: Per-course access control
  - `Alias`: Learned name aliases from review decisions
  - `OcrCorrection`: Learned OCR error corrections
  - `Pattern`: Historical patterns for smart suggestions

#### attendance/sync.py
- **Purpose**: Bidirectional synchronization between SQLite and Google Sheets
- **Strategy**: SQLite is primary, Sheets is synchronized representation
- **Sync Direction**:
  - Read: Sheet → SQLite on session resolution
  - Write: SQLite → Sheet batched with conflict detection
- **Conflict Resolution**: Last-write-wins with audit trail
- **Sync Interval**: Every 5 minutes or on-demand

#### attendance/cache.py
- **Purpose**: Caching layer for performance optimization
- **Backend**: File-based SQLite
- **Cache Types**:
  - Roster data (TTL: 60s)
  - Session info (TTL: 60s)
  - ML embeddings (persistent)
  - OCR corrections (persistent)
  - Permission checks (TTL: 300s)
- **Features**: Cache warming, metrics tracking, invalidation on writes

#### attendance/matcher.py
- **Purpose**: Name matching with hybrid strategy
- **Matching Pipeline**:
  1. Learned alias lookup
  2. ML semantic matching (if enabled)
  3. RapidFuzz fuzzy matching
  4. Phonetic matching (fallback)
- **Scoring**: Composite fuzzy score (token-sort + token-set ratios)
- **Decisions**: AUTO_ACCEPT, REVIEW_REQUIRED, NO_MATCH, MATCH_AMBIGUOUS

#### attendance/ml_matcher.py
- **Purpose**: BERT-based semantic name matching
- **Model**: sentence-transformers with multilingual model
- **Operation**:
  - Generate embeddings for roster names (cached)
  - Generate embedding for OCR result at runtime
  - Cosine similarity for matching
- **Training**: Fine-tune on learned_aliases.json weekly

#### attendance/name_patterns.py
- **Purpose**: Nickname and transliteration handling
- **Features**:
  - Nickname dictionary (Ahmed → Ahmad, Mohamed → Muhammad)
  - Transliteration patterns (Arabic script variations)
  - Partial name matching (first/last name only)
  - Learned from historical review decisions

#### attendance/suggestions.py
- **Purpose**: Smart attendance suggestions
- **Features**:
  - Historical pattern matching
  - Suggest top 3 candidates based on history
  - Confidence scores based on frequency and recency

#### attendance/ocr_corrections.py
- **Purpose**: OCR error auto-correction learning
- **Features**:
  - Track common corrections from review resolutions
  - Build correction patterns (0 → O, 5 → S, rn → m)
  - Apply corrections before matching
  - Confidence-based application

#### attendance/session_patterns.py
- **Purpose**: Session type recommendations
- **Features**:
  - Analyze date/time patterns for session types
  - Learn from historical session type assignments
  - Recommend session type based on time of day

#### attendance/concurrent.py
- **Purpose**: Parallel processing utilities
- **Implementation**: ThreadPoolExecutor
- **Use Cases**:
  - Concurrent image processing
  - Concurrent review operations
- **Configuration**: max_workers (default: 4)

#### attendance/dead_letter.py
- **Purpose**: Enhanced dead-letter queue
- **Features**:
  - Categorize failures by type
  - Automatic reprocessing with exponential backoff
  - Manual retry interface in webapp
  - Dead-letter item expiration (30 days)

#### attendance/writer.py
- **Purpose**: Transactional writes with two-phase commit
- **Strategy**:
  1. Write to SQLite
  2. Write to Google Sheets
  3. Confirm in SQLite
- **Features**: Rollback on failure, retry with idempotency, transaction logging

#### attendance/audit.py
- **Purpose**: Comprehensive audit logging
- **Scope**: All data changes, user actions, system events
- **Retention**: Configurable (default: 1 year)
- **Viewer**: Available in webapp

#### attendance/roles.py
- **Purpose**: Enhanced RBAC with granular permissions
- **Roles**: viewer, editor, admin
- **Permissions**:
  - view_attendance
  - edit_attendance
  - manage_roster
  - manage_users
  - view_audit_log
- **Features**: Per-course access control, permission inheritance

#### attendance/resolver_ops.py
- **Purpose**: Session resolution operations
- **Integration**: Uses database cache, integrates with sync

#### attendance/review_queue.py
- **Purpose**: Review queue management
- **Integration**: Uses database for persistence, supports bulk operations

#### attendance/review_cards.py
- **Purpose**: Review card formatting
- **Features**: Image previews, OCR text display, inline editing

#### attendance/session_resolver.py
- **Purpose**: Session metadata resolution
- **Integration**: Uses database cache, supports session patterns

#### attendance/migrate.py
- **Purpose**: Data migration utilities
- **Features**:
  - Export Google Sheets to SQLite
  - Import learned_aliases.json
  - Integrity verification
  - Rollback procedures

### Web Application

#### webapp/app.py
- **Purpose**: Streamlit web application entry point
- **Features**: Multi-page navigation, authentication, layout

#### webapp/auth.py
- **Purpose**: Authentication and authorization
- **Features**: Telegram bot authentication, RBAC enforcement

#### webapp/pages/dashboard.py
- **Purpose**: Dashboard with attendance overview
- **Features**: Attendance summary, recent uploads, health status

#### webapp/pages/review.py
- **Purpose**: Review interface
- **Features**:
  - Side-by-side image and OCR text display
  - Inline editing of names and values
  - Bulk actions (select, resolve, mark absent, apply session type)
  - Keyboard-oriented workflows

#### webapp/pages/audit.py
- **Purpose**: Audit log viewer
- **Features**: Filterable audit log, export, search

## Data Flow

### Upload Processing Flow

1. **Ingestion**: Photo received from Telegram/Drive/CLI
2. **OCR Processing**: Extract text from image using FreeAI/Gemini
3. **Name Matching**: Match OCR names to roster using hybrid strategy
4. **Review Queue**: Flag low-confidence matches for review
5. **Transaction**: Write to SQLite → Google Sheets → confirm
6. **Sync**: Background sync ensures consistency

### Sync Flow

1. **Read Sync**: Sheet → SQLite on session resolution
2. **Write Sync**: SQLite → Sheet batched every 5 minutes
3. **Conflict Detection**: Compare versions, use last-write-wins
4. **Audit Trail**: Log all sync operations

### ML Matching Flow

1. **Embedding Generation**: Generate embeddings for roster names (cached)
2. **Query Embedding**: Generate embedding for OCR result
3. **Similarity Calculation**: Cosine similarity with roster embeddings
4. **Threshold Check**: Accept if similarity >= threshold
5. **Fallback**: Use RapidFuzz if ML match fails

## Concurrency Model

- **Image Processing**: ThreadPoolExecutor with configurable workers
- **Review Operations**: Concurrent resolution of review items
- **Database Access**: Connection pooling, WAL mode for reads
- **Cache Access**: Thread-safe SQLite cache backend

## Error Recovery

- **Retry Logic**: Exponential backoff with jitter
- **Dead-Letter Queue**: Failed operations categorized and reprocessed
- **Rollback**: Transactional writes with rollback on failure
- **Idempotency**: Retry-safe operations with deduplication

## Security

- **Authentication**: Telegram bot with authorized user list
- **Authorization**: RBAC with per-course permissions
- **Audit Logging**: All changes logged with user and timestamp
- **Data Privacy**: Local SQLite storage, configurable retention

## Performance Optimization

- **Caching**: Multi-layer cache for roster, sessions, embeddings, corrections
- **Concurrent Processing**: Parallel image and review operations
- **Lazy Loading**: ML model loaded on first use
- **Batch Operations**: Batched database writes and Sheet sync
- **Connection Pooling**: Database connection reuse

## Deployment Architecture

- **Environment**: Python 3.14+ on local machine or Cloud Run
- **Database**: SQLite with local file storage
- **External Services**: Google Sheets API, FreeAI/Gemini OCR
- **Web Application**: Streamlit with local deployment
- **Scheduler**: Built-in for reminders and health checks

## Monitoring

- **Health Checks**: Gemini/Sheets failure tracking, review queue age
- **Metrics**: Cache hit rates, match accuracy, processing latency
- **Logging**: Structured logging with configurable levels
- **Audit Trail**: Complete audit log for compliance

## Backup and Recovery

- **Database Backups**: Automatic daily backups, 30-day retention
- **Rollback Procedures**: Course-level rollback capability
- **Data Export**: Full export to Google Sheets available
- **Import/Export**: Migration utilities for data portability
