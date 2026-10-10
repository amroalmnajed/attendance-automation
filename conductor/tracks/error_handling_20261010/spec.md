# Track Specification: System-Wide Error Handling Audit

## Overview
This track focuses on auditing the entire Attendance Automation System to identify and fix missing or weak error handling. We will use LSP diagnostics to find unhandled exceptions and risky operations. The goal is to implement robust, industry-standard exception management across all critical sections without altering core business logic. 

**Note on Risk:** As requested, the user will be explicitly warned before any risky or extensive code modifications are executed, allowing for a manual backup update.

## Functional Requirements
- **I/O Operations:** Ensure all file reads/writes (e.g., config loading, image saving) are wrapped in `try/except` blocks with appropriate fallback/error logging.
- **Network Requests:** Apply robust error handling (e.g., timeouts, retries, quota limit catching) to Google Sheets (`gspread`) and Gemini OCR API calls.
- **Bot Handlers:** Update Telegram bot endpoints in `ingest.py` to gracefully catch underlying pipeline errors and notify the user instead of silently failing or crashing.
- **Logging:** Improve error context across the board (e.g., logging stack traces and context IDs like `upload_id`).

## Acceptance Criteria
- All identified missing `try/except` blocks around I/O, network, and database operations are resolved.
- Exceptions yield descriptive error messages to both the application logs and the user interface (Telegram).
- The application recovers gracefully or fails fast safely upon encountering errors, without leaving hanging state.
- All modifications are presented as unified diffs for review prior to execution.

## Out of Scope
- Adding new product features or changing existing business logic.
- Complete architectural redesign of the OCR or Sheets integration.
