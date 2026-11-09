# Troubleshooting Guide

## Overview

This guide covers common issues and their solutions for the attendance automation system.

## Database Issues

### Issue: Database Locked Error

**Symptoms**:
- `sqlite3.OperationalError: database is locked`
- Operations hang or timeout

**Causes**:
- Multiple processes writing to database simultaneously
- Long-running transaction holding lock
- WAL mode not enabled

**Solutions**:
1. Enable WAL mode:
   ```python
   from attendance.db import engine
   with engine.connect() as conn:
       conn.execute("PRAGMA journal_mode=WAL")
   ```
2. Check for long-running transactions:
   ```python
   from attendance.db import SessionLocal
   session = SessionLocal()
   # Ensure sessions are closed promptly
   session.close()
   ```
3. Increase timeout in SQLAlchemy:
   ```python
   engine = create_engine("sqlite:///attendance.db", connect_args={"timeout": 30})
   ```

### Issue: Migration Fails

**Symptoms**:
- `alembic.util.exc.CommandError: Target database is not up to date`
- Migration scripts fail to apply

**Solutions**:
1. Check current migration version:
   ```bash
   alembic current
   ```
2. View migration history:
   ```bash
   alembic history
   ```
3. Stamp current state if needed:
   ```bash
   alembic stamp head
   ```
4. Rollback and retry:
   ```bash
   alembic downgrade base
   alembic upgrade head
   ```

### Issue: Orphaned Records

**Symptoms**:
- Integrity check fails
- Students without courses
- Sessions without courses

**Solutions**:
1. Run integrity check:
   ```python
   from attendance.migrate import DataMigrator
   with DataMigrator() as migrator:
       integrity = migrator.verify_integrity()
       print(integrity)
   ```
2. Clean up orphaned records:
   ```python
   from attendance.db import SessionLocal, Student, Course
   session = SessionLocal()
   # Delete students without courses
   session.query(Student).filter(
       ~Student.course_id.in_(session.query(Course.course_id))
   ).delete(synchronize_session=False)
   session.commit()
   ```

## OCR Issues

### Issue: OCR Fails with Timeout

**Symptoms**:
- `TimeoutError: Request timed out`
- OCR provider unreachable

**Solutions**:
1. Increase timeout in config:
   ```yaml
   attendance:
     ocr:
       timeout_s: 120  # Increase from 60
   ```
2. Check network connectivity to OCR provider
3. Switch provider:
   ```yaml
   attendance:
     ocr:
       provider: gemini  # Switch from freeai
   ```
4. Enable fallback:
   ```yaml
   attendance:
     ocr:
       fallback_to_gemini: true
   ```

### Issue: Low OCR Confidence

**Symptoms**:
- Many rows flagged as OCR_LOW_CONFIDENCE
- Names not recognized correctly

**Solutions**:
1. Enable retry preprocessing:
   ```yaml
   attendance:
     ocr:
       retry_preprocessing: true
   ```
2. Adjust confidence threshold:
   ```yaml
   attendance:
     ocr:
       min_confidence: 0.70  # Lower from 0.75
   ```
3. Improve image quality:
   - Ensure images are well-lit
   - Check camera focus
   - Verify sheet orientation

### Issue: FreeAI Token Limit

**Symptoms**:
- `RateLimitError: Daily token limit exceeded`
- OCR requests fail

**Solutions**:
1. Enable Gemini fallback:
   ```yaml
   attendance:
     ocr:
       fallback_to_gemini: true
   ```
2. Use Gemini as primary:
   ```yaml
   attendance:
     ocr:
       provider: gemini
   ```
3. Wait for token reset (daily)

## Matching Issues

### Issue: ML Matching Slow

**Symptoms**:
- ML matching takes > 500ms per name
- Upload processing is slow

**Solutions**:
1. Ensure caching is enabled:
   ```yaml
   core:
     ml_matching:
       cache_embeddings: true
   ```
2. Warm cache on startup:
   ```python
   from attendance.ml_matcher import MLMatcher
   from attendance.session_resolver import SessionResolver

   matcher = MLMatcher()
   resolver = SessionResolver(course_key="main")
   roster = resolver.get_roster()
   for name, _ in roster:
       matcher.get_embedding(name)
   ```
3. Use smaller model:
   ```yaml
   core:
     ml_matching:
       model: all-MiniLM-L6-v2  # Smaller, faster
   ```

### Issue: Low Match Accuracy

**Symptoms**:
- Many names require manual review
- Incorrect matches

**Solutions**:
1. Adjust matching thresholds:
   ```yaml
   attendance:
     match:
       t_high: 85  # Lower from 90
       margin: 8   # Lower from 10
   ```
2. Enable soft-accept lane:
   ```yaml
   attendance:
     match:
       t_soft: 75
       margin_strong: 20
   ```
3. Train ML model on more data:
   - Collect more confirmed aliases
   - Run fine-tuning (see ML_TRAINING.md)
4. Review and fix name variants:
   ```bash
   python -m attendance.cli aliases variants review
   ```

### Issue: Alias Not Working

**Symptoms**:
- Learned alias not auto-accepting
- Same name repeatedly in review queue

**Solutions**:
1. Check alias confirmations:
   ```python
   from attendance.db import SessionLocal, Alias
   session = SessionLocal()
   alias = session.query(Alias).filter_by(raw_name_key="...").first()
   print(f"Confirmations: {alias.confirmations}")
   ```
2. Lower confirmation threshold:
   ```yaml
   attendance:
     match:
       alias_min_confirmations: 1  # Lower from 2
   ```
3. Check alias is not stale:
   ```yaml
   attendance:
     match:
       alias_stale_days: 180  # Increase from 90
   ```

## Sync Issues

### Issue: Sync to Google Sheets Fails

**Symptoms**:
- `gspread.exceptions.APIError`
- Sync errors in logs
- Data not appearing in Sheets

**Solutions**:
1. Check credentials:
   ```bash
   echo $GOOGLE_APPLICATION_CREDENTIALS
   ```
2. Verify sheet ID is correct
3. Check API quotas:
   - Visit Google Cloud Console
   - Check Sheets API usage
4. Enable retry:
   ```yaml
   attendance:
     write:
       retry:
         max_attempts: 10  # Increase from 6
   ```
5. Check for rate limiting:
   - Sync interval may be too short
   - Increase `sync_interval_seconds`

### Issue: Conflict in Sync

**Symptoms**:
- Conflict warnings in logs
- Data overwritten unexpectedly

**Solutions**:
1. Review audit log for conflicts
2. Understand conflict resolution (last-write-wins)
3. Ensure only one source modifies data at a time
4. Check time synchronization between systems

## Cache Issues

### Issue: Cache Not Working

**Symptoms**:
- Cache hit rate is 0%
- Performance not improved

**Solutions**:
1. Verify cache is enabled:
   ```yaml
   core:
     caching:
       enabled: true
   ```
2. Check cache database exists:
   ```bash
   ls cache.db
   ```
3. Reinitialize cache:
   ```python
   from attendance.cache import CacheManager
   cache = CacheManager()
   cache.clear()
   cache.warm_all()
   ```
4. Check cache stats:
   ```python
   cache = CacheManager()
   stats = cache.get_stats()
   print(stats)
   ```

### Issue: Cache Too Large

**Symptoms**:
- Cache database file is very large
- Disk space issues

**Solutions**:
1. Set max size:
   ```yaml
   core:
     caching:
       max_size_mb: 50  # Reduce from 100
   ```
2. Clear old cache entries:
   ```python
   from attendance.cache import CacheManager
   cache = CacheManager()
   cache.clear()
   ```
3. Set TTLs for persistent data:
   ```yaml
   core:
     caching:
       embedding_ttl: 604800  # 7 days instead of null
   ```

## Concurrency Issues

### Issue: Thread Errors

**Symptoms**:
- `RuntimeError: SQLite objects created in a thread can only be used in that same thread`
- Concurrent operations fail

**Solutions**:
1. Use thread-local sessions:
   ```python
   from attendance.db import SessionLocal
   session = SessionLocal()
   # Create session per thread
   ```
2. Ensure connection pooling is configured:
   ```python
   from sqlalchemy import pool
   engine = create_engine(
       "sqlite:///attendance.db",
       connect_args={"check_same_thread": False},
       poolclass=pool.StaticPool,
   )
   ```
3. Reduce concurrency:
   ```yaml
   attendance:
     concurrency:
       max_workers: 2  # Reduce from 4
   ```

### Issue: Deadlock

**Symptoms**:
- Operations hang indefinitely
- Database locks not released

**Solutions**:
1. Ensure WAL mode is enabled
2. Keep transactions short
3. Avoid nested transactions
4. Use retry logic with exponential backoff

## Webapp Issues

### Issue: Webapp Won't Start

**Symptoms**:
- Streamlit fails to start
- Port already in use

**Solutions**:
1. Check port availability:
   ```bash
   netstat -ano | findstr :8501
   ```
2. Use different port:
   ```bash
   streamlit run webapp/app.py --server.port 8502
   ```
3. Check dependencies:
   ```bash
   pip install streamlit
   ```

### Issue: Authentication Fails

**Symptoms**:
- Login not working
- Access denied errors

**Solutions**:
1. Verify `WEBAPP_PASSWORD` is set in `webapp/.streamlit/secrets.toml` or `.env`
2. Check RBAC configuration:
   ```yaml
   core:
     rbac:
       enabled: true
       default_role: viewer
   ```

## Upload Issues

### Issue: Photo Upload Fails

**Symptoms**:
- Photo upload fails in the webapp
- Processing errors or timeouts

**Solutions**:
1. Check file size limits
2. Verify photo format (JPG, PNG)
3. Check OCR provider status
4. Review `logs/app.jsonl` for errors

## Performance Issues

### Issue: Slow Upload Processing

**Symptoms**:
- Processing takes > 60 seconds
- Upload times out

**Solutions**:
1. Enable concurrency:
   ```yaml
   attendance:
     concurrency:
       max_workers: 8  # Increase from 4
   ```
2. Optimize OCR (see OCR section)
3. Enable caching
4. Use ML matching with cache warming

### Issue: High Memory Usage

**Symptoms**:
- Process uses > 1GB RAM
- System slows down

**Solutions**:
1. Clear cache:
   ```python
   from attendance.cache import CacheManager
   cache = CacheManager()
   cache.clear()
   ```
2. Reduce batch sizes:
   ```yaml
   core:
     ml_matching:
       max_batch_size: 50  # Reduce from 100
   ```
3. Use smaller ML model
4. Limit concurrent workers

## Logging and Debugging

### Enable Debug Logging

```yaml
core:
  app:
    log_level: DEBUG
```

### View Logs

```bash
# Main application logs
tail -f logs/attendance.log

# Sync logs
tail -f logs/sync.log

# Zoom CSV logs
tail -f logs/zoom_csv/*.log
```

### Enable SQL Logging

```python
import logging
logging.getLogger('sqlalchemy.engine').setLevel(logging.INFO)
```

## Getting Help

If issues persist after trying these solutions:

1. Collect diagnostic information:
   - Full error messages
   - Relevant log excerpts
   - Configuration sections
   - Database schema version (`alembic current`)

2. Check documentation:
   - `docs/ARCHITECTURE.md`
   - `docs/SCHEMA.md`
   - `docs/ML_TRAINING.md`
   - `docs/CONFIG_REFERENCE.md`
   - `docs/MIGRATION_GUIDE.md`

3. Review audit logs in webapp for context

4. Contact support with the collected information

## Common Error Messages

### `sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) no such table`

**Cause**: Database tables not created
**Solution**: Run `alembic upgrade head`

### `gspread.exceptions.APIError: Quota exceeded`

**Cause**: Google Sheets API quota exceeded
**Solution**: Wait for quota reset or increase quota in Google Cloud Console

### `sentence_transformers.util.Loader: Model not found`

**Cause**: Model name incorrect or not downloaded
**Solution**: Check model name in config, ensure internet connection for first download

### `KeyError: 'course_key'`

**Cause**: Course not found in configuration
**Solution**: Add course to `attendance.courses` in config

### `PermissionError: [Errno 13] Permission denied`

**Cause**: File access denied
**Solution**: Check file permissions, ensure process has write access

## System Health Checks

Run these checks periodically:

```python
from attendance.db import SessionLocal, Course, Student, Session as DBSession
from attendance.cache import CacheManager
from attendance.migrate import DataMigrator

# Database health
session = SessionLocal()
print(f"Courses: {session.query(Course).count()}")
print(f"Students: {session.query(Student).count()}")
print(f"Sessions: {session.query(DBSession).count()}")
session.close()

# Cache health
cache = CacheManager()
print(f"Cache stats: {cache.get_stats()}")

# Integrity check
with DataMigrator() as migrator:
    print(f"Integrity: {migrator.verify_integrity()}")
```
