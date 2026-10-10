# Deployment Guide — Attendance Automation Sub-System 1

Phase 4: Step-by-step setup, environment configuration, scheduling, and go-live checklist.

---

## 1. Prerequisites

| Requirement | Minimum version | Check |
|---|---|---|
| Python | 3.11+ (3.9 works but is EOL) | `python --version` |
| pip | latest | `pip --version` |
| Google Cloud project | Any project with billing | console.cloud.google.com |
| Gemini API key | Free tier or paid | aistudio.google.com |

> **Upgrade Python first.**  
> `google-auth` already prints EOL warnings on Python 3.9. Run on 3.11+:  
> `winget install Python.Python.3.11` (Windows) or `sudo apt install python3.11` (Ubuntu).

---

## 2. Service Account Setup

### 2a. Enable APIs
In your Google Cloud project (`attendance-511005`), enable:
- Google Sheets API
- Google Drive API

```
https://console.cloud.google.com/apis/library
```

### 2b. Service account is already created
The service account `attendance-sheet-bot@attendance-511005.iam.gserviceaccount.com`
exists with key file `attendance-511005-781e52a31316.json`.

Least-privilege IAM roles needed (do **not** grant broader roles):
- No project-level IAM role needed — the SA gets access only via sheet sharing (step 2c).

### 2c. Share sheets with the service account
All four sheets must be shared with the service account as **Editor**:

```
Share → Add: attendance-sheet-bot@attendance-511005.iam.gserviceaccount.com → Editor
```

Sheets:
- `1nOTft1CH6OTh3cVkc2c1ZOqD-m1v7jCYM1gxIb7oRnM` (Crash)
- `1pyrm_USZr66t_V7y6R-F-JJ8wJ8kdOzrsJ-bbDX11BY` (Main)
- `1vuOBu3JJqWUa0uCY6rmdwJbkZeqJ6QPhx6519gvQS10` (G10)
- `1CcudM6UbwRQK8Ro3wHg6j9aHhvYGyCqt3gzl4JSTBJU` (G9)

✓ Already done per project setup.

---

## 3. Local Installation

```powershell
# 1. Clone / unzip the project
cd C:\Users\lenovo\Desktop\attendance-automation

# 2. Create a virtual environment (strongly recommended)
python -m venv .venv
.venv\Scripts\Activate.ps1

# 3. Install dependencies
pip install -r requirements.txt

# 4. Verify key packages
python -c "import gspread, rapidfuzz, cv2, google.generativeai; print('OK')"
```

---

## 4. Environment Variables

All secrets live in `.env` at the project root. **Never commit `.env` to git.**

```dotenv
# .env  (already populated — do not share this file)
GOOGLE_APPLICATION_CREDENTIALS=attendance-511005-781e52a31316.json

CRASH_SHEET_ID=1nOTft1CH6OTh3cVkc2c1ZOqD-m1v7jCYM1gxIb7oRnM
MAIN_SHEET_ID=1pyrm_USZr66t_V7y6R-F-JJ8wJ8kdOzrsJ-bbDX11BY
G10_SHEET_ID=1vuOBu3JJqWUa0uCY6rmdwJbkZeqJ6QPhx6519gvQS10
G9_SHEET_ID=1CcudM6UbwRQK8Ro3wHg6j9aHhvYGyCqt3gzl4JSTBJU

GEMINI_API_KEY=<your-key-from-aistudio.google.com>

# Optional — notifications
TELEGRAM_BOT_TOKEN=<bot-token>        # only needed if running the Telegram ingest bot
TELEGRAM_CHAT_ID=<your-chat-id>       # your personal chat ID for summary notifications
SMTP_HOST=smtp.gmail.com              # only needed for email notifications
SMTP_PORT=587
SMTP_USER=you@gmail.com
SMTP_PASS=<app-password>
NOTIFY_TO=you@gmail.com
```

### Verify env is loaded correctly:
```powershell
python -c "from dotenv import load_dotenv; import os; load_dotenv(override=True); print(os.getenv('CRASH_SHEET_ID'))"
# Expected: 1nOTft1CH6OTh3cVkc2c1ZOqD-m1v7jCYM1gxIb7oRnM
```

---

## 5. Verify Sheet Discovery

Run the discovery script to confirm the service account has access and headers are as expected:

```powershell
# Set env vars in PowerShell (already in .env but useful for one-off checks)
$env:GOOGLE_APPLICATION_CREDENTIALS="attendance-511005-781e52a31316.json"
$env:CRASH_SHEET_ID="1nOTft1CH6OTh3cVkc2c1ZOqD-m1v7jCYM1gxIb7oRnM"
$env:MAIN_SHEET_ID="1pyrm_USZr66t_V7y6R-F-JJ8wJ8kdOzrsJ-bbDX11BY"
$env:G10_SHEET_ID="1vuOBu3JJqWUa0uCY6rmdwJbkZeqJ6QPhx6519gvQS10"
$env:G9_SHEET_ID="1CcudM6UbwRQK8Ro3wHg6j9aHhvYGyCqt3gzl4JSTBJU"

python discover_sheets.py
```

Expected: output shows all 4 sheets with student rows starting at row 5.

---

## 6. Run Tests

```powershell
# Unit tests (no internet connection needed)
pytest tests/ -v

# Name-matcher evaluation (measures accuracy + false-accept rate)
python tests/eval_matcher.py

# With custom thresholds for comparison:
python tests/eval_matcher.py --t-high 85 --margin 8
python tests/eval_matcher.py --t-high 90 --margin 10
```

All tests should pass and false-accept rate should be 0%.

---

## 7. OCR Test (sanity-check before going live)

Test OCR on the sample photos **without touching any sheet**:

```powershell
python -m attendance.cli ocr-test --folder "C:\Users\lenovo\Desktop\agy-project-help"
```

Expected: each image prints extracted names and marks. Confidence should be ≥ 0.75 for most rows.

If you get a `quota exceeded` error on the Gemini API, wait 60 seconds (free-tier rate limit) and retry.

---

## 8. First Dry Run (no writes)

Process a real attendance photo in dry-run mode:

```powershell
# Crash course, session 1
python -m attendance.cli process `
  --image "C:\Users\lenovo\Desktop\agy-project-help\actualattendancephoto1.jpeg" `
  --course crash `
  --session 1 `
  --dry-run

# G10 (requires session type)
python -m attendance.cli process `
  --image "C:\Users\lenovo\Desktop\agy-project-help\actualattendancephoto2.jpeg" `
  --course g10 `
  --session 1 `
  --type "Saturday - Center" `
  --dry-run
```

Review the diff table printed to console. Check:
- Student names matched correctly
- Attendance values are valid (`Present | Late | Not complete | Absent`)
- Flagged items listed at the bottom with reason codes

---

## 9. First Live Write

Only after the dry run looks correct:

```powershell
python -m attendance.cli process `
  --image "C:\Users\lenovo\Desktop\agy-project-help\actualattendancephoto1.jpeg" `
  --course crash `
  --session 1 `
  --no-dry-run
```

Open the sheet in the browser and verify the cells were written.

---

## 10. Resolve Flagged Items

After any run that produces review items:

```powershell
# List all pending items
python -m attendance.cli review list

# Resolve one item (pick the correct roster row from the top3 list)
python -m attendance.cli review resolve `
  --upload-id <upload_id from list> `
  --item-id   <item_id from list> `
  --roster-row 7 `
  --roster-name "Mohamed Ali Saber" `
  --value Present `
  --no-dry-run
```

---

## 11. Telegram Bot (Upload Channel)

The Telegram bot is the recommended production upload channel: send a photo, answer
course/session/type questions, receive a summary reply.

### 11a. Create a bot
1. Message `@BotFather` on Telegram → `/newbot`
2. Copy the token → set `TELEGRAM_BOT_TOKEN` in `.env`
3. Send yourself a message, then get your chat ID:
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
   Copy `chat.id` → set `TELEGRAM_CHAT_ID` in `.env`

### 11b. Start the bot
```powershell
python -m attendance.ingest
```

Send the bot a photo. It will ask: course → session → type (for G9/G10), then run the pipeline and reply with the summary.

### 11c. Keep it running (Windows Task Scheduler)
1. Open Task Scheduler → Create Basic Task
2. Trigger: At system startup (or daily at the start of teaching day)
3. Action: Start a program
   - Program: `C:\Users\lenovo\Desktop\attendance-automation\.venv\Scripts\python.exe`
   - Arguments: `-m attendance.ingest`
   - Start in: `C:\Users\lenovo\Desktop\attendance-automation`

---

## 12. Monitoring & Logs

| Log file | Content |
|---|---|
| `logs/app.jsonl` | Structured JSON log (every pipeline event) |
| `logs/audit.jsonl` | Append-only cell-write audit trail (who/what/when/old→new) |
| `review_queue/*.json` | Flagged items with top-3 candidates and reason codes |

View the last 20 log events:
```powershell
Get-Content logs\app.jsonl -Tail 20
```

View audit trail for a specific upload:
```powershell
Get-Content logs\audit.jsonl | Select-String "upload_id_here"
```

---

## 13. Image Retention

Per `config/attendance.yaml`:
```yaml
retention:
  image_days: 30
  flagged_image_days_after_resolve: 30
  audit_log: keep
```

The Telegram bot deletes temp images immediately after processing (`os.remove(img_path)`).
For CLI-processed images, delete manually after the session, or add a cron/scheduler task.

Student names and images are **never sent to third parties** except the Gemini API for OCR.
To opt out of Gemini data use, set `GEMINI_API_KEY` to a paid key and enable
[data opt-out in AI Studio settings](https://aistudio.google.com/app/settings).

---

## 14. Go-Live Checklist

```
PRE-LAUNCH
[ ] Python 3.11+ installed and active in virtual environment
[ ] pip install -r requirements.txt succeeded (no errors)
[ ] .env populated with all 4 sheet IDs, GOOGLE_APPLICATION_CREDENTIALS, GEMINI_API_KEY
[ ] Service account JSON file present at path in GOOGLE_APPLICATION_CREDENTIALS
[ ] All 4 sheets shared as Editor with attendance-sheet-bot@attendance-511005.iam.gserviceaccount.com
[ ] python discover_sheets.py runs cleanly (no auth errors, all 4 sheets discovered)
[ ] pytest tests/ → all tests pass
[ ] python tests/eval_matcher.py → false-accept rate = 0%
[ ] python -m attendance.cli ocr-test --folder <photos_dir> → names extracted correctly
[ ] Dry-run on one real photo per course → diff table looks correct
[ ] At least one session resolved manually (no --force, no surprises)

FIRST LIVE RUN
[ ] Run with --no-dry-run on one Crash session (safest: no type col)
[ ] Open sheet in browser → verify exactly the right cells are written
[ ] Run python -m attendance.cli review list → resolve any flagged items
[ ] Check logs/audit.jsonl → audit trail has correct old→new values
[ ] Confirm no stray writes (re-run same image with dry-run → should show all "=" same-value rows)

OPERATIONS
[ ] Telegram bot started and tested end-to-end with a real photo
[ ] Notification channel configured (TELEGRAM_CHAT_ID or SMTP_*) and tested
[ ] Task Scheduler entry created to auto-start the bot on boot
[ ] Team briefed: how to send a photo, how to resolve review queue items
[ ] Image retention policy agreed and enforced (delete after each session or after 30 days)
[ ] .env and service account JSON excluded from any git push (.gitignore ✓)
```

---

## 15. Rollback

If a write goes wrong, the audit log at `logs/audit.jsonl` records every cell's old value.
To revert:
1. Find the relevant `upload_id` in the audit log
2. For each written cell, manually set the cell back to `old_value` in the sheet
3. Or use the API: `python -m attendance.cli review resolve ... --value <old_value> --force`

There is no automatic rollback command. Add one if needed for production.
