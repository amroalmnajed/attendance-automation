# Project Overview & Tech Stack
- **Purpose**: Automates attendance tracking by extracting student names from photos of paper attendance sheets via OCR, matching them against roster names, and writing attendance records to Google Sheets.
- **Tech Stack**: Python 3.x.
- **Key Dependencies**: `gspread` (Google Sheets API), `google-genai` (Gemini API for OCR), `PyYAML` (Configuration).

# Directory & Component Map
- `attendance/`: Core domain logic.
  - `cli.py`: Command-line interface entry points.
  - `pipeline.py`: Orchestrates the main flow (OCR -> Normalize -> Match -> Write).
  - `ocr.py`: Interfaces with OCR providers (e.g., Gemini) to extract names and marks.
  - `matcher.py`: Fuzzy matching logic comparing OCR names to canonical roster names.
  - `writer.py`: Handles Google Sheets API writes and batching.
  - `session_resolver.py`: Resolves course, session, and column metadata from Sheets.
  - `review_queue.py`: Manages uncertain matches for human review.
- `core/`: Shared utilities (`auth.py`, `logger.py`, `notifier.py`).
- `config/`: Configuration files (`attendance.yaml`, `learned_aliases.json`, `name_variants.yaml`).
- `tests/`: Unit and evaluation tests (pytest).

# Data Flow & Architecture
1. **Input**: Image path, course, session provided via CLI.
2. **Setup**: `session_resolver` pulls the current roster from Google Sheets.
3. **Extraction**: `ocr` extracts `(raw_name, status_mark)` pairs from the image.
4. **Normalization & Matching**: `normalizer` cleans strings; `matcher` fuzzy-matches them to the roster.
5. **Writing**: Exact/high-confidence matches are written via `writer`. Low-confidence matches go to `review_queue`.
6. **Review**: Humans resolve flagged items via `cli review resolve`, which completes the write and learns the alias.

# Development Conventions
- **Code Style**: Standard Python PEP-8, use type hints (`from __future__ import annotations`).
- **Configuration**: Driven by `config/attendance.yaml`. Secrets loaded via `.env` or GCP Secret Manager.
- **Dry-run by default**: Critical operations default to dry-run mode to prevent accidental Sheet modifications.

# Environment & Configuration
- **Env Vars Required**: `GOOGLE_APPLICATION_CREDENTIALS` (Service Account JSON), `OPS_SHEET_ID`, `CRASH_SHEET_ID`, `MAIN_SHEET_ID`, `G10_SHEET_ID`, `G9_SHEET_ID`, `GEMINI_API_KEY`.
- **Setup**: `pip install -r requirements.txt`.
