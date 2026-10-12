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
