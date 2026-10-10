# Implementation Plan

## Phase 1: I/O and File Processing Error Handling
- [x] Task: Write Tests for I/O Failures 183afc8
  - [x] Create or update unit tests mocking file read/write errors (e.g., missing config, missing images).
- [x] Task: Implement I/O Error Handling 183afc8
  - [x] Add `try/except` around `load_config` and file read/write operations with proper fallback/logging.
  - [x] Ensure image saving/reading in `ingest.py` and `pipeline.py` fails gracefully without crashing.
- [x] Task: Phase Verification & Checkpoint (Refer to workflow.md) [checkpoint: 183afc8]

## Phase 2: Network & External API Error Handling
- [x] Task: Write Tests for API Failures
  - [x] Mock `gspread` and `google-genai` network errors (timeouts, quota exceeded). -> Added `tests/test_network_errors.py`
- [x] Task: Implement Network Error Handling
  - [x] Wrap Gemini API calls in `ocr.py` with robust exception handlers and retry logic. -> Added `attendance/_net.py` & updated `ocr.py`
  - [x] Strengthen `gspread` exception handling and exponential backoff in `writer.py` and `session_resolver.py`.

## Phase 3: Bot Handler Resilience & Graceful Degradation
- [ ] Task: Write Tests for Bot Resilience
  - [ ] Mock underlying pipeline exceptions to verify the Telegram bot returns user-friendly error messages.
- [ ] Task: Implement Bot Error Handling
  - [ ] Update `ingest.py` handlers (e.g., `_run_pipeline_flow`, `handle_names_done`) to catch all unexpected exceptions.
  - [ ] Ensure errors are safely logged and communicated to the user, averting hanging states.
- [ ] Task: Phase Verification & Checkpoint (Refer to workflow.md)



