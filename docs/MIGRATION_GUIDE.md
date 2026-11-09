# Migration Guide

## Overview

This guide covers migrating from the original Google Sheets-only architecture to the modernized architecture with SQLite database, ML matching, caching, and enhanced features.

## Prerequisites

Before starting the migration:

1. **Backup Existing Data**
   - Export all Google Sheets to CSV/Excel
   - Backup `config/learned_aliases.json`
   - Backup `config/name_variants.yaml`
   - Backup `config/variant_suggestions.json` (if exists)

2. **Install Dependencies**
   ```bash
   pip install -r requirements.txt
   ```

3. **Set Environment Variables**
   - Ensure all sheet ID environment variables are set
   - Set `HF_TOKEN` for Hugging Face model downloads (optional but recommended)

4. **Stop Running Services**
   - Stop any running webapp instance
   - Ensure no active uploads are in progress

## Migration Steps

### Step 1: Initialize Database

Create the SQLite database with all tables:

```bash
python -c "from attendance.db import create_all_tables; create_all_tables()"
```

This creates `attendance.db` with all required tables.

### Step 2: Run Alembic Migrations

Initialize Alembic and create the initial migration:

```bash
# Initialize Alembic (if not already done)
alembic init alembic

# Generate initial migration
alembic revision --autogenerate -m "Initial schema"

# Apply migration
alembic upgrade head
```

### Step 3: Export Course Data

Export course rosters and session metadata from Google Sheets to SQLite:

```python
from attendance.migrate import DataMigrator

with DataMigrator() as migrator:
    # Export each course
    for course_key in ["main", "crash", "g9", "g10"]:
        # Get sheet ID from environment
        import os
        sheet_id = os.environ.get(f"{course_key.upper()}_SHEET_ID")
        if sheet_id:
            result = migrator.export_course_from_sheets(
                course_key=course_key,
                sheet_id=sheet_id,
                tab="Main-Attendance" if course_key != "crash" else "Crash-Attendance",
                layout="typed_pair" if course_key in ["g9", "g10"] else "single",
            )
            print(f"Exported {course_key}: {result}")

            # Export sessions
            session_result = migrator.export_sessions_from_sheets(course_key)
            print(f"Sessions for {course_key}: {session_result}")
```

Or use the convenience function:

```bash
python -c "from attendance.migrate import migrate_all_courses; import json; print(json.dumps(migrate_all_courses(), indent=2, default=str))"
```

### Step 4: Import Learned Aliases

Import learned aliases from JSON to database:

```python
from attendance.migrate import DataMigrator

with DataMigrator() as migrator:
    result = migrator.import_learned_aliases("config/learned_aliases.json")
    print(f"Imported aliases: {result}")
```

### Step 5: Verify Data Integrity

Verify that the migration completed successfully:

```python
from attendance.migrate import DataMigrator

with DataMigrator() as migrator:
    integrity = migrator.verify_integrity()
    print(f"Integrity check: {integrity}")

    if not integrity["healthy"]:
        print("WARNING: Integrity check failed!")
        print(integrity["checks"])
```

### Step 6: Update Configuration

Update `config/attendance.yaml` with new sections:

```yaml
core:
  database:
    path: attendance.db
    sync_interval_seconds: 300
    backup_enabled: true
    backup_dir: db_backups
    backup_keep_days: 30

  ml_matching:
    enabled: true
    model: paraphrase-multilingual-MinaryLM-L12-v2
    cache_embeddings: true
    retrain_interval_days: 7
    threshold: 0.75

  caching:
    enabled: true
    backend: sqlite
    path: cache.db
    roster_ttl: 60
    session_ttl: 60

  rbac:
    enabled: true
    default_role: viewer
```

### Step 7: Initialize Cache

Initialize the caching layer:

```python
from attendance.cache import CacheManager

cache = CacheManager()
cache.warm_all()
```

### Step 8: Test ML Matching

Test ML matching with a sample roster:

```python
from attendance.ml_matcher import MLMatcher
from attendance.session_resolver import SessionResolver

# Initialize matcher
matcher = MLMatcher(
    model_name="paraphrase-multilingual-MiniLM-L12-v2",
    cache_embeddings=True,
    threshold=0.75,
)

# Get roster
resolver = SessionResolver(course_key="main")
roster = resolver.get_roster()
roster_names = [name for name, _ in roster]

# Test matching
ocr_name = "Ahmed Mohameed"
match, score = matcher.match(ocr_name, roster_names)
print(f"Match: {match}, Score: {score}")
```

### Step 9: Run Benchmark Tests

Verify OCR accuracy and performance:

```bash
pytest tests/test_ocr_benchmark.py -v
```

### Step 10: Update Review Queue

If using local JSON review queue, migrate to database:

```python
# Review queue already uses database via attendance/review_queue.py
# No manual migration needed if using the new architecture
```

### Step 11: Start Services

Start the webapp:

```bash
streamlit run webapp/app.py
```

### Step 12: Monitor Initial Operations

Monitor the first few uploads to ensure:

- OCR processing works correctly
- ML matching is functioning
- Sync to Google Sheets succeeds
- Review queue items are created appropriately
- Audit logs are being written

## Rollback Procedures

If migration fails or issues arise:

### Rollback a Single Course

```python
from attendance.migrate import DataMigrator

with DataMigrator() as migrator:
    result = migrator.rollback_course("main")
    print(f"Rolled back: {result}")
```

### Full Database Rollback

To completely revert to the old architecture:

1. Stop all services
2. Delete `attendance.db`
3. Delete `cache.db`
4. Restore original `config/attendance.yaml` (remove new sections)
5. Restart services with old architecture

### Alembic Rollback

To roll back a specific migration:

```bash
# Roll back one step
alembic downgrade -1

# Roll back to specific version
alembic downgrade <revision_id>
```

## Post-Migration Checklist

- [ ] All courses exported to database
- [ ] All students imported correctly
- [ ] All sessions imported correctly
- [ ] Learned aliases imported
- [ ] Integrity check passes
- [ ] Configuration updated
- [ ] Cache initialized
- [ ] ML matching tested
- [ ] Benchmark tests pass
- [ ] Services started successfully
- [ ] First upload processed correctly
- [ ] Sync to Google Sheets working
- [ ] Review queue functional
- [ ] Audit logs being written

## Troubleshooting

### Issue: Database Already Exists

**Error**: `sqlite3.OperationalError: table already exists`

**Solution**:
- If it's a fresh migration, delete `attendance.db` and start over
- If it has data, verify integrity and proceed with existing data

### Issue: Sheet ID Not Found

**Error**: `Environment variable not set`

**Solution**:
- Set the environment variable for the course's sheet ID
- Example: `export MAIN_SHEET_ID=1abc...`

### Issue: ML Model Download Fails

**Error**: `Failed to download model`

**Solution**:
- Check internet connection
- Set `HF_TOKEN` environment variable
- Download model manually and set `model` path in config

### Issue: Migration Hangs

**Error**: Migration appears to hang

**Solution**:
- Check Google Sheets API rate limits
- Verify credentials are valid
- Try exporting courses one at a time

### Issue: Integrity Check Fails

**Error**: Orphaned records detected

**Solution**:
- Check which records are orphaned
- Manually clean up or re-run migration
- Verify course IDs are correct

## Data Validation

After migration, validate data quality:

### Validate Student Counts

```python
from attendance.db import SessionLocal, Student, Course

session = SessionLocal()
for course in session.query(Course).all():
    student_count = session.query(Student).filter_by(course_id=course.course_id).count()
    print(f"{course.course_key}: {student_count} students")
```

### Validate Session Counts

```python
from attendance.db import SessionLocal, Session as DBSession, Course

session = SessionLocal()
for course in session.query(Course).all():
    session_count = session.query(DBSession).filter_by(course_id=course.course_id).count()
    print(f"{course.course_key}: {session_count} sessions")
```

### Validate Alias Counts

```python
from attendance.db import SessionLocal, Alias, Course

session = SessionLocal()
for course in session.query(Course).all():
    alias_count = session.query(Alias).filter_by(course_id=course.course_id).count()
    print(f"{course.course_key}: {alias_count} aliases")
```

## Performance Tuning

After migration, tune performance:

### Adjust Cache TTLs

If cache hit rates are low, increase TTLs:

```yaml
core:
  caching:
    roster_ttl: 300  # Increase from 60
    session_ttl: 300
```

### Adjust ML Threshold

If ML accuracy is low, adjust threshold:

```yaml
core:
  ml_matching:
    threshold: 0.70  # Lower from 0.75
```

### Adjust Concurrency

If processing is slow, increase workers:

```yaml
attendance:
  concurrency:
    max_workers: 8  # Increase from 4
```

## Monitoring

After migration, monitor:

### Cache Hit Rates

```python
from attendance.cache import CacheManager

cache = CacheManager()
stats = cache.get_stats()
print(f"Cache hit rate: {stats['hit_rate']:.2%}")
```

### Sync Status

Check sync logs for errors or delays.

### Audit Logs

Review audit logs to ensure all operations are logged.

## Support

If issues persist:

1. Check this guide's troubleshooting section
2. Review `docs/TROUBLESHOOTING.md`
3. Check logs in `logs/` directory
4. Review audit logs in webapp
5. Contact support with error messages and logs
