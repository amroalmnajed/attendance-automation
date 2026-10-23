# Project Overview & Tech Stack
- **Purpose**: Automates attendance tracking by extracting student names from photos of paper attendance sheets via OCR, matching them against roster names, and writing attendance records to Google Sheets.
- **Tech Stack**: Python 3.x.
- **Key Dependencies**: `gspread` (Google Sheets API), `openai` (Free.ai GOT-OCR 2.0 — primary OCR), `google-genai` (Gemini API — fallback OCR), `PyYAML` (Configuration).
- **OCR Strategy**: Free.ai GOT-OCR 2.0 is the primary provider (optimised for Arabic handwriting). Gemini is the secondary fallback (triggered when free.ai hits its daily 6,000 token limit). Tesseract has been fully removed.

# Directory & Component Map
- `attendance/`: Core domain logic.
  - `cli.py`: Command-line interface entry points.
  - `pipeline.py`: Orchestrates the main flow (OCR -> Normalize -> Match -> Write).
  - `ocr.py`: Interfaces with OCR providers (`FreeAIProvider`, `GeminiProvider`, `MockProvider`) to extract names and marks.
  - `matcher.py`: Fuzzy matching logic comparing OCR names to canonical roster names.
  - `writer.py`: Handles Google Sheets API writes and batching.
  - `session_resolver.py`: Resolves course, session, and column metadata from Sheets.
  - `review_queue.py`: Manages uncertain matches for human review.
  - `review_cards.py`: Formats review cards and traffic-light scores.
  - `review_crop.py`: Generates context crop images for flagged items.
  - `config_runtime.py`: Pure runtime configuration and user-safe error messages.
- `webapp/`: Streamlit web application (primary graphical interface).
- `core/`: Shared utilities (`auth.py`, `logger.py`, `notifier.py`).
- `config/`: Configuration files (`attendance.yaml`, `learned_aliases.json`, `name_variants.yaml`).
- `tests/`: Unit and evaluation tests (pytest).

# Data Flow & Architecture
1. **Input**: Image path, course, session provided via CLI.
2. **Setup**: `session_resolver` pulls the current roster from Google Sheets.
3. **Extraction**: `ocr` extracts `(raw_name, status_mark)` pairs from the image (FreeAI primary, Gemini fallback).
4. **Normalization & Matching**: `normalizer` cleans strings; `matcher` fuzzy-matches them to the roster.
5. **Writing**: Exact/high-confidence matches are written via `writer`. Low-confidence matches go to `review_queue`.
6. **Review**: Humans resolve flagged items via `cli review resolve`, which completes the write and learns the alias.

# Development Conventions
- **Code Style**: Standard Python PEP-8, use type hints (`from __future__ import annotations`).
- **Configuration**: Driven by `config/attendance.yaml`. Secrets loaded via `.env` or GCP Secret Manager.
- **Dry-run by default**: Critical operations default to dry-run mode to prevent accidental Sheet modifications.

# Environment & Configuration
- **Env Vars Required**: `GOOGLE_APPLICATION_CREDENTIALS` (Service Account JSON), `FREEAI_API_KEY` (primary OCR), `CRASH_SHEET_ID`, `MAIN_SHEET_ID`, `G10_SHEET_ID`, `G9_SHEET_ID`.
- **Env Vars Optional**: `GEMINI_API_KEY` (fallback OCR — needed when `fallback_to_gemini: true`), `OPS_SHEET_ID`.
- **Setup**: `pip install -r requirements.txt`.

# Phase 4 additions (see CHANGELOG.md for config keys)
- `attendance/roles.py` — user ID sets (env + config), `decide(uid, level, cfg)`, `guard(level, load_cfg, get_log, deny_return)` decorator.
- `attendance/reminders.py` — pure `due_reminders(now, cfg, history_reader, sent)`; `run_check` (send + remember); `install(app, load_cfg)` registers the job only if enabled AND `app.job_queue` exists.
- `attendance/health.py` — `configure(cfg)` (no-op until called), `record(kind)` / `record_exc(seen, exc)` (never raise), `dry_run_scope`, `check_review_age`, `status_text`, `install`. State: `<review_dir>/_health/state.json`. Alerts via `core.notifier.send_text` to admins only.
- Gotchas: health state is process-wide, so `tests/conftest.py` resets it per test; set `health._THREADED = False` in tests that check alerts. Reminders use `pytz` (no tzdata needed on Windows). Alert/reminder text must stay name-free.

# Phase 3 additions (see CHANGELOG.md for config keys)
- `attendance/history.py` — per-course counts for the summary comparison (`<review_dir>/_history/<course>.json`, numbers only).
- `attendance/aliases.py` — `AliasStore` + dates in sidecar `learned_aliases.meta.json`; `entries()`, `remove()`, `flush_usage()`. `attendance/alias_admin.py` — pure functions behind `/aliases` (`admin_ids(cfg, env)` reads `ADMIN_TELEGRAM_IDS` + `attendance.admin.telegram_ids`).
- `pipeline._read_sheet()` — the ONE OCR helper used by `run()` and `run_multi()` (modes `full` / `low_confidence`).
- `attendance/variants.py` — name-variant suggestions (`config/variant_suggestions.json`) and approval into `learned_variants:` of `config/name_variants.yaml`. CLI: `aliases variants list|review`.
- Gotchas: `tests/conftest.py` snapshots/restores the real alias, alias-meta and variant-suggestion files. `/aliases` currently does its own admin check; Phase 4 replaces it with real roles.

# Phase 1–2 additions (see CHANGELOG.md for config keys)
- `attendance/marks.py` — parses `name | mark` lines (text/paste mode). `parse_lines`, `split_names_and_marks`, `format_preview`.
- `attendance/photos.py` — `merge_photo_rows` merges OCR rows from several photos before matching.
- `attendance/absent.py` — "Mark N absent?" (crash/main only). Offer is recorded after each upload in `<review_dir>/_absent/<upload_id>.json` (row numbers only); the button re-reads the live sheet and writes only empty cells via `batch_write`.
- `pipeline.run_multi()` = several photos, one `upload_id`. `pipeline.run_text(..., marks=[...])` carries per-line marks.
- Web app: paste-names screen shows a parse preview; unrecognised marks prompt confirmation before processing. Photo uploader supports multiple photos per upload.
- Gotchas: `RunSummary` is logged whole by the notifier — never put student names on it. Several tests pass `MagicMock` rows, so new fields read from rows must tolerate non-int/None values. `tests/conftest.py` protects the real alias file.
- Line endings: committed blobs are LF; the Windows working copy may show CRLF. Edit with tools that preserve each file's endings; never mass-convert. Work plan + rules: see HANDOFF.md.
