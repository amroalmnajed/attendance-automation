# Attendance Automation - Documentation Overview

## Purpose

This document provides a comprehensive overview of the attendance automation system and serves as a navigation hub for detailed documentation on specific topics.

## System Overview

The attendance automation system is a modern Python application that processes attendance photos from multiple sources (Telegram, Google Forms, Drive folders), performs OCR-based name recognition, matches names to course rosters, and writes attendance records to Google Sheets. The system features:

- **Local SQLite Database** with bidirectional synchronization to Google Sheets
- **ML-Powered Name Matching** using sentence-transformers for semantic similarity
- **Smart Suggestions** based on historical patterns
- **Multi-Layer Caching** for performance optimization
- **Concurrent Processing** for faster uploads
- **Enhanced Error Recovery** with dead-letter queue and automatic retries
- **Role-Based Access Control** (RBAC) for user management
- **Comprehensive Audit Logging** for compliance and debugging

## Documentation Structure

### Architecture Overview
**File**: [ARCHITECTURE.md](ARCHITECTURE.md)

Detailed system architecture including:
- Core modules and their responsibilities
- Data flow diagrams
- Concurrency model
- Error recovery mechanisms
- Security architecture
- Performance optimization strategies
- Deployment architecture
- Monitoring and backup strategies

**When to read**: If you need to understand how the system works at a high level, or if you're integrating new components.

### Database Schema
**File**: [SCHEMA.md](SCHEMA.md)

Complete database schema documentation including:
- Table definitions with column types and constraints
- Relationship diagrams
- Indexes and foreign keys
- Data integrity constraints
- Migration strategy
- Backup and recovery procedures

**When to read**: If you're working with the database directly, writing queries, or planning schema changes.

### ML Model Training Guide
**File**: [ML_TRAINING.md](ML_TRAINING.md)

Comprehensive guide for ML-based name matching including:
- Model selection criteria
- Training pipeline setup
- Fine-tuning procedures
- Model evaluation and benchmarking
- Performance tuning
- Troubleshooting ML issues
- Maintenance and monitoring

**When to read**: If you're improving name matching accuracy, training custom models, or debugging ML-related issues.

### Configuration Reference
**File**: [CONFIG_REFERENCE.md](CONFIG_REFERENCE.md)

Complete reference for all configuration options including:
- Core configuration (app, auth, notify, retention)
- Database configuration
- ML matching configuration
- Caching configuration
- RBAC configuration
- Attendance-specific settings (OCR, matching, write, etc.)
- Environment variables reference
- Migration notes

**When to read**: If you're configuring the system, tuning performance, or adding new configuration options.

### Migration Guide
**File**: [MIGRATION_GUIDE.md](MIGRATION_GUIDE.md)

Step-by-step guide for migrating from the original Google Sheets-only architecture to the modernized architecture including:
- Prerequisites and preparation
- Database initialization
- Data export from Google Sheets
- Import of learned aliases
- Data integrity verification
- Configuration updates
- Cache initialization
- ML matching testing
- Rollback procedures
- Post-migration checklist
- Troubleshooting migration issues

**When to read**: If you're migrating from the old architecture to the new one, or if you need to roll back changes.

### Troubleshooting Guide
**File**: [TROUBLESHOOTING.md](TROUBLESHOOTING.md)

Common issues and solutions including:
- Database issues (locks, migrations, orphaned records)
- OCR issues (timeouts, low confidence, rate limits)
- Matching issues (slow ML, low accuracy, alias problems)
- Sync issues (Google Sheets failures, conflicts)
- Cache issues (not working, too large)
- Concurrency issues (thread errors, deadlocks)
- Webapp issues (startup, authentication)
- Telegram bot issues (not responding, upload failures)
- Performance issues (slow uploads, high memory)
- Logging and debugging tips
- Common error messages

**When to read**: If you're encountering any issues with the system.

## Quick Start

### For New Users

1. Read **Architecture Overview** to understand the system
2. Read **Configuration Reference** to set up your environment
3. Follow **Migration Guide** if migrating from the old architecture
4. Use **Troubleshooting Guide** if you encounter issues

### For Developers

1. Read **Architecture Overview** for system design
2. Read **Database Schema** for data model understanding
3. Read **ML Training Guide** if working on name matching
4. Refer to **Configuration Reference** for adding new features
5. Use **Troubleshooting Guide** for debugging

### For Operators/Administrators

1. Read **Architecture Overview** for high-level understanding
2. Read **Configuration Reference** for system tuning
3. Read **Troubleshooting Guide** for issue resolution
4. Refer to **Migration Guide** for major updates

## Key Concepts

### Data Flow

1. **Ingestion**: Photos received from Telegram/Drive/CLI
2. **OCR Processing**: Text extraction using FreeAI/Gemini
3. **Name Matching**: Hybrid strategy (ML + RapidFuzz + Phonetic)
4. **Review Queue**: Low-confidence matches flagged for review
5. **Transaction**: Write to SQLite → Google Sheets → confirm
6. **Sync**: Background sync ensures consistency

### Synchronization Strategy

- **Primary Data Store**: SQLite database
- **Secondary Representation**: Google Sheets (synchronized)
- **Sync Direction**: Bidirectional with conflict detection
- **Conflict Resolution**: Last-write-wins with audit trail
- **Sync Interval**: Every 5 minutes or on-demand

### Name Matching Pipeline

1. Learned alias lookup
2. ML semantic matching (if enabled)
3. RapidFuzz fuzzy matching
4. Phonetic matching (fallback)
5. Confidence scoring and decision

### Caching Strategy

- **Backend**: File-based SQLite
- **Cache Types**: Roster data, session info, ML embeddings, OCR corrections, permission checks
- **TTL**: Configurable per cache type (60s default for dynamic data, persistent for embeddings)
- **Features**: Cache warming, metrics tracking, invalidation on writes

## System Requirements

- **Python**: 3.14+
- **Database**: SQLite (included with Python)
- **External Services**: Google Sheets API, FreeAI/Gemini OCR
- **Hardware**: Minimum 2GB RAM, 4GB recommended for ML matching
- **OS**: Linux, macOS, or Windows (tested on all)

## Installation

```bash
# Clone repository
git clone <repository-url>
cd attendance-automation

# Install dependencies
pip install -r requirements.txt

# Set environment variables
cp .env.example .env
# Edit .env with your configuration

# Initialize database
python -c "from attendance.db import create_all_tables; create_all_tables()"

# Run migrations
alembic upgrade head
```

## Running the System

### Telegram Bot

```bash
python -m attendance.bot
```

### Web Application

```bash
streamlit run webapp/app.py
```

### CLI Upload

```bash
python -m attendance.cli upload --photo <path> --course <course_key>
```

## Support

For issues not covered in the documentation:

1. Check the **Troubleshooting Guide** first
2. Review relevant logs in `logs/` directory
3. Check audit logs in the webapp
4. Contact support with error messages and configuration details

## Documentation Updates

This documentation is updated alongside system changes. When new features are added:

1. Update **Architecture Overview** with new components
2. Update **Database Schema** with new tables/fields
3. Update **Configuration Reference** with new options
4. Update **Migration Guide** if data migration is needed
5. Update **Troubleshooting Guide** with new issues

## Version History

- **v2.0**: Modernized architecture with SQLite, ML matching, caching, RBAC
- **v1.0**: Original Google Sheets-only architecture

## Additional Resources

- [README.md](../README.md) - Project overview and quick start
- [CHANGELOG.md](../CHANGELOG.md) - Version history and changes
- [DEPLOYMENT.md](../DEPLOYMENT.md) - Deployment instructions
- [PROJECT_CONTEXT.md](../PROJECT_CONTEXT.md) - Project background and goals
