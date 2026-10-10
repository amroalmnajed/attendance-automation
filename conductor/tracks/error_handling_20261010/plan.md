# Implementation Plan

## Phase 1: I/O and File Processing Error Handling
- [ ] Task: Write Tests for I/O Failures
  - [ ] Create or update unit tests mocking file read/write errors (e.g., missing config, missing images).
- [ ] Task: Implement I/O Error Handling
  - [ ] Add `try/except` around `load_config` and file read/write operations with proper fallback/logging.
  - [ ] Ensure image saving/reading in `ingest.py` and `pipeline.py` fails gracefully without crashing.
- [ ] Task: Phase Verification & Checkpoint (Refer to workflow.md)

## Phase 2: Network & External API Error Handling
- [ ] Task: Write Tests for API Failures
  - [ ] Mock `gspread` and `google-genai` network errors (e.g., timeouts, quota exceeded).
- [ ] Task: Implement Network Error Handling
  - [ ] Wrap Gemini API calls in `ocr.py` with robust exception handlers and retry logic for transient errors.
  - [ ] Review and strengthen `gspread` exception handling and exponential backoff in `writer.py` and `session_resolver.py`.
- [ ] Task: Phase Verification & Checkpoint (Refer to workflow.md)

## Phase 3: Bot Handler Resilience & Graceful Degradation
- [ ] Task: Write Tests for Bot Resilience
  - [ ] Mock underlying pipeline exceptions to verify the Telegram bot returns user-friendly error messages.
- [ ] Task: Implement Bot Error Handling
  - [ ] Update `ingest.py` handlers (e.g., `_run_pipeline_flow`, `handle_names_done`) to catch all unexpected exceptions.
  - [ ] Ensure errors are safely logged and communicated to the user, averting hanging states.
- [ ] Task: Phase Verification & Checkpoint (Refer to workflow.md)
