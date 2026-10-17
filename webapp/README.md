# Attendance Webapp

A Streamlit web application that replaces the Telegram bot as the primary interface for attendance automation. Upload photos, paste names, process Zoom data, resolve review items, and view reports — all in the browser.

## Setup

### 1. Install Dependencies

The webapp shares the root `requirements.txt`. Install from the repo root:

```bash
pip install -r requirements.txt
```

### 2. Configure Environment

Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and fill in your values:

```bash
cp webapp/.streamlit/secrets.toml.example webapp/.streamlit/secrets.toml
```

Edit `webapp/.streamlit/secrets.toml`:
- `WEBAPP_PASSWORD`: Password for the webapp (required)
- `GOOGLE_CREDENTIALS_B64`: Base64-encoded service account JSON
- `CRASH_SHEET_ID`, `MAIN_SHEET_ID`, `G9_SHEET_ID`, `G10_SHEET_ID`: Your Google Sheet IDs
- `GEMINI_API_KEY`: Gemini API key for OCR
- `ZOOM_ACCOUNT_ID`, `ZOOM_CLIENT_ID`, `ZOOM_CLIENT_SECRET`: Zoom API credentials (optional, for Zoom ingestion)

### 3. Verify Configuration

Ensure `config/attendance.yaml` exists and has your course configurations:

```bash
cat config/attendance.yaml
```

## Running the Webapp

From the repo root:

```bash
streamlit run webapp/app.py
```

Then open http://localhost:8501 in your browser.

### First Run

On the first run, Streamlit may ask for your email. You can:
- Enter your email (optional)
- Or press Enter to skip

Enter your name (for audit trails) and the password from `WEBAPP_PASSWORD` to access the webapp.

## Features

### Upload Photos (W2)
- Upload one or more attendance sheet photos
- Select course, session number, and optional session type
- Dry-run toggle (default ON for safety)
- View summary with matched/flagged/written counts
- Link to review queue for flagged items

### Paste Names (W3)
- Paste a list of names with optional marks (e.g., "Ahmed | L")
- Preview before processing
- Automatic mark detection (Present, Late, Absent, Not complete)
- Same preview-before-submit UX as the bot's `/done` command

### Zoom Ingestion (W4)
- Two paths: Meeting ID + date (Zoom API fetch) or CSV file upload
- Handles teacher detection failures with manual start time input
- Handles session type ambiguity with explicit type selection
- Gates behind `attendance.zoom_csv.enabled` config

### Review Queue (W5)
- Reads the real SQLite queue (not the sheet mirror)
- Displays review cards with traffic-light scores
- Shows cropped images of flagged rows
- Resolve items with candidate matches, absent, or skip
- Roster search when no candidate matches
- Dry-run toggle per resolution

### Attendance Viewer (W6)
- Session selector with history from `attendance.history`
- Search by student name
- Summary metrics (Present, Late, Absent, Total)
- Review backlog count
- Reports: Session Report (equivalent to `/report`) and Streaks Report (equivalent to `/streaks`)

### Admin Tools (W7)
- Stub screens for revert, absent-offer, alias management, and health status
- Clearly labeled as "not yet ported" with CLI/bot alternatives

## Auth & Audit Trail

The webapp uses a shared password model (matching the dashboard deployment pattern):
- Password stored in `WEBAPP_PASSWORD` (Streamlit secrets or env var)
- User name stored in session state for audit trails
- `resolved_by` field populated with user name on all write actions (review resolutions, reverts, absent-marking)

**Note:** This is not full access control with user accounts — it's a simple shared password for single-teacher/small-team deployments.

## File Structure

```
webapp/
├── app.py                 # Streamlit entrypoint
├── config.py              # Course and secret configuration
├── pages/
│   ├── __init__.py
│   ├── upload.py           # Photo upload & processing (W2)
│   ├── names.py            # Paste names (W3)
│   ├── zoom.py             # Zoom ingestion (W4)
│   ├── review.py           # Review queue (W5)
│   ├── viewer.py           # Attendance viewer & reports (W6)
│   └── admin.py            # Admin tools stubs (W7)
├── requirements.txt       # References root requirements.txt
├── .env.example           # Environment template
├── .streamlit/
│   ├── config.toml        # Streamlit config
│   ├── secrets.toml.example  # Secrets template
│   └── secrets.toml       # Your secrets (create this)
└── README.md              # This file
```

## Architecture

The webapp calls the same pipeline functions the bot and CLI use:
- `attendance.pipeline.run()` / `run_multi()` / `run_text()` / `run_zoom()`
- `attendance.review_queue.ReviewQueue` (SQLite, not the sheet mirror)
- `attendance.resolver_ops.resolve_item()` for resolutions
- `attendance.history.count_marks()` for viewer metrics
- `attendance.reporter.generate_session_report()` / `generate_streaks_report()` for reports

This ensures consistency between the webapp, bot, and CLI.

## Current Status

**Completed:**
- ✅ W1: Scaffold webapp/ + config + auth gate
- ✅ W2: Photo upload & process screen
- ✅ W3: Paste-names screen
- ✅ W4: Zoom ingestion screen
- ✅ W5: Review queue screen
- ✅ W6: Attendance viewer & reporting
- ✅ W7: Admin tools stubs
- ✅ W8: Progress feedback wiring (integrated in W2-W4)
- ✅ W9: Auth & audit trail design (shared password + user name)

**Pending:**
- ⬜ W10: Deployment docs + secrets template (in progress)
- ⬜ W11: Parity verification + delete dashboard/

## Limitations

- Admin tools (revert, absent-offer, alias management, health) are stubs only — use CLI/bot equivalents
- No real user accounts or role-based access control
- Zoom ingestion requires valid Zoom API credentials
- Requires Google Sheets to be shared with the service account

## Security Notes

- Never commit `.streamlit/secrets.toml` or `.env` files
- Use strong passwords for `WEBAPP_PASSWORD`
- Google credentials should have minimal required permissions
- Review queue contains student names — keep access restricted
