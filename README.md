# Attendance Automation

Streamlit web app and CLI that turn a photo of a paper attendance sheet into attendance marks in Google Sheets.

Photo -> OpenCV clean-up -> Free.ai GOT-OCR 2.0 (Gemini fallback) -> fuzzy match against the roster -> Google Sheets.
Uncertain matches go to a review queue that an admin resolves from the web app or the CLI.

## Interfaces

1. **Web app** (`webapp/`) - Sole graphical interface for photo uploads, Zoom ingestion, review queue resolution, and attendance reporting. See `webapp/README.md`.
2. **CLI** (`attendance/cli.py`) - Command-line tools for processing and review.

## Run

### Web App (Primary Interface)

1. Python 3.12 (see `.python-version`), then `pip install -r requirements.txt`.
2. Copy `.env.example` to `.env` and fill in your actual values (`FREEAI_API_KEY`,
   `GEMINI_API_KEY` (fallback),
   the `*_SHEET_ID` values and Google credentials (`GOOGLE_APPLICATION_CREDENTIALS`, or
   `GOOGLE_CREDENTIALS_B64` on Railway)).
3. Edit `config/attendance.yaml` (courses, thresholds, `dry_run_default`,
   Zoom meeting IDs, teacher name).
4. Start the web app:

```bash
streamlit run webapp/app.py
```

On Windows, you can also double-click `start_webapp.bat`.

See `webapp/README.md` for detailed usage and `webapp/DEPLOY.md` for deployment.

### CLI

Command-line tools for processing and review:

```bash
python -m attendance.cli --help
```

## Layout

- `webapp/` - Streamlit web app (photo upload, paste names, Zoom, review queue, viewer/reports, admin).
- `attendance/pipeline.py` - OCR -> match -> write pipeline. `ocr.py`, `matcher.py`, `writer.py`, `session_resolver.py`.
- `attendance/review_cards.py` - Review card formatting and traffic-light scoring.
- `attendance/review_crop.py` - Context crop image generation for flagged items.
- `attendance/config_runtime.py` - Pure runtime configuration and error messaging.
- `attendance/cli.py` - Command-line interface.
- `core/` - Auth, logging, notifier.
- `HANDOFF.md` / `PROJECT_CONTEXT.md` - Working notes for whoever continues the project.

## Test

```bash
pip install -r requirements.txt
pytest -q
ruff check .
```

Tests never touch the network or the real sheets. `tests/tune_matcher.py` (optional) suggests
matcher thresholds from resolved review items.

## Deploy

See `DEPLOYMENT.md` and `webapp/DEPLOY.md`.

## Zoom Attendance Automation

The system supports automated attendance processing for Zoom meetings via two methods in `webapp/pages/zoom.py`:

### 1. Zoom API Fetch
Fetch participant reports directly from Zoom using the Server-to-Server OAuth app.

**Zoom App Setup:**
1. Go to [Zoom Marketplace](https://marketplace.zoom.us/) and create a Server-to-Server OAuth app
2. Required scope: `meeting:read:list_past_participants` or `meeting:read:list_past_participants:admin`
3. Set environment variables: `ZOOM_ACCOUNT_ID`, `ZOOM_CLIENT_ID`, `ZOOM_CLIENT_SECRET`

### 2. Manual CSV Upload
Upload Zoom participant CSV files via the web app.

**CSV Format:**
- Filename: `participants_<meeting_id>_<date>.csv` (e.g., `participants_81795786552_2026_09_13.csv`)
- Required columns: "Name (original name)", "Join time", "Leave time", "Duration (minutes)", "Guest"

**Teacher Detection:**
- Teacher name is configured in `config/attendance.yaml` (`teacher_name`)
- Uses fuzzy matching to detect teacher variations
- If teacher is not found, the system prompts for manual meeting start time
