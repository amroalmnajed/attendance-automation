# Attendance Automation

Telegram bot that turns a photo of a paper attendance sheet into attendance marks in Google Sheets.

Photo -> OpenCV clean-up -> Gemini OCR -> fuzzy match against the roster -> Google Sheets.
Uncertain matches go to a review queue that an admin resolves from Telegram (or the CLI).

## Run

1. Python 3.12 (see `.python-version`), then `pip install -r requirements.txt`.
2. Create `.env` with `TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`, the `*_SHEET_ID` values and Google
   credentials (`GOOGLE_APPLICATION_CREDENTIALS`, or `GOOGLE_CREDENTIALS_B64` on Railway).
3. Edit `config/attendance.yaml` (courses, thresholds, `dry_run_default`, admin/teacher Telegram IDs).
4. Start the bot: `python -m attendance.ingest`

`dry_run_default: false` means the bot writes to the real sheets. Set it to `true` to rehearse.

## Layout

- `attendance/bot/` - Telegram handlers (`app.py` wiring, `upload`, `names`, `review`, `admin`, `menu`).
- `attendance/pipeline.py` - OCR -> match -> write. `ocr.py`, `matcher.py`, `writer.py`, `session_resolver.py`.
- `attendance/cli.py` - command-line tools. `core/` - auth, logging, notifier.
- `HANDOFF.md` / `PROJECT_CONTEXT.md` - working notes for whoever continues the project.

## Test

    pip install -r requirements.txt
    pytest -q
    ruff check .

Tests never touch the network or the real sheets. `tests/tune_matcher.py` (optional) suggests
matcher thresholds from resolved review items.

## Deploy

See `DEPLOYMENT.md` (Railway and Windows service instructions).

## Zoom Attendance Automation

The bot supports automated attendance processing for Zoom meetings via two methods:

### 1. Zoom API Fetch
Fetch participant reports directly from Zoom using the Server-to-Server OAuth app.

**Zoom App Setup:**
1. Go to [Zoom Marketplace](https://marketplace.zoom.us/) and create a Server-to-Server OAuth app
2. Required scope: `meeting:read:list_past_participants` or `meeting:read:list_past_participants:admin` (for accessing participant reports)
   - **Important:** Make sure to add this scope in the Zoom app settings under "Scopes" → "Add Scopes" → "Meeting" → check the participant read scope
3. Copy the Account ID, Client ID, and Client Secret from the app credentials
4. Store these as Railway environment variables:
   - `ZOOM_ACCOUNT_ID` - Your Zoom account ID
   - `ZOOM_CLIENT_ID` - OAuth app client ID
   - `ZOOM_CLIENT_SECRET` - OAuth app client secret

**Important Notes:**
- Participant reports may not be immediately available after a meeting ends (typically a few minutes delay)
- Zoom API has rate limits; the bot handles "report not ready yet" gracefully
- Credentials are stored as environment variables, never committed to the repository

### 2. Manual CSV Upload
Upload Zoom participant CSV files manually via Telegram.

**CSV Format:**
- Filename: `participants_<meeting_id>_<date>.csv` (e.g., `participants_81795786552_2026_09_13.csv`)
- Required columns: "Name (original name)", "Join time", "Leave time", "Duration (minutes)", "Guest"
- Optional columns: "Email", "Recording disclaimer response", "In waiting room" (ignored)
- The system calculates attendance based on join time, duration, and teacher presence

**Telegram Commands:**
- `/zoom_fetch` - Fetch participant report from Zoom API by meeting ID
- `/zoom_upload` - Upload a CSV file manually
- `/zoom_reprocess` - Reprocess an archived CSV file

**Teacher Detection:**
- Teacher name is configured as "Yaman" (configurable in `attendance.yaml`)
- Uses fuzzy matching to detect teacher variations (e.g., "Yaman (iPad)", "Yaman (iPhone)")
- If teacher is not found, the system prompts for manual meeting start time
- Teacher's presence affects the effective start time (configured buffer of 5 minutes)
