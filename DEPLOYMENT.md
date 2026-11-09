# Deployment Guide — Attendance Automation Sub-System 1

Phase 4: Step-by-step setup, environment configuration, scheduling, and go-live checklist.

---

## 1. Prerequisites

| Requirement | Minimum version | Check |
|---|---|---|
| Python | 3.11+ (3.9 works but is EOL) | `python --version` |
| pip | latest | `pip --version` |
| Google Cloud project | Any project with billing | console.cloud.google.com |
| Free.ai API key | free.ai dashboard | free.ai |
| Gemini API key (fallback) | Free tier or paid | aistudio.google.com |

> **Upgrade Python first.**
> `google-auth` already prints EOL warnings on Python 3.9. Run on 3.11+:
> `winget install Python.Python.3.11` (Windows) or `sudo apt install python3.11` (Ubuntu).

---

## 2. Service Account Setup

### 2a. Enable APIs
In your Google Cloud project, enable:
- Google Sheets API
- Google Drive API

```
https://console.cloud.google.com/apis/library
```

### 2b. Create a service account
Create a service account in your Google Cloud project (e.g., with suffix `-sheet-bot`).
Download the JSON key file and store it securely (see `.env.example`).

Least-privilege IAM roles needed (do **not** grant broader roles):
- No project-level IAM role needed — the SA gets access only via sheet sharing (step 2c).

### 2c. Share sheets with the service account
All four sheets must be shared with the service account as **Editor**:

```
Share → Add: <your-service-account-email> → Editor
```

Replace `<your-service-account-email>` with the email from your service account (format: `service-account-name@project-id.iam.gserviceaccount.com`).

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

Copy `.env.example` to `.env` and fill in your actual values:

```dotenv
# .env  (copy from .env.example and fill in real values)
GOOGLE_APPLICATION_CREDENTIALS=your-service-account-key.json

CRASH_SHEET_ID=<your-crash-sheet-id>
MAIN_SHEET_ID=<your-main-sheet-id>
G10_SHEET_ID=<your-g10-sheet-id>
G9_SHEET_ID=<your-g9-sheet-id>

GEMINI_API_KEY=<your-key-from-aistudio.google.com>

# Optional — email notifications
SMTP_HOST=smtp.gmail.com              # only needed for email notifications
SMTP_PORT=587
SMTP_USER=you@gmail.com
SMTP_PASS=<app-password>
NOTIFY_TO=you@gmail.com
```

### Verify env is loaded correctly:
```powershell
python -c "from dotenv import load_dotenv; import os; load_dotenv(override=True); print(os.getenv('CRASH_SHEET_ID'))"
# Expected: your crash sheet ID
```

---

## 5. Verify Sheet Discovery

Run the discovery script to confirm the service account has access and headers are as expected:

```powershell
# Set env vars in PowerShell (already in .env but useful for one-off checks)
$env:GOOGLE_APPLICATION_CREDENTIALS="your-service-account-key.json"
$env:CRASH_SHEET_ID="<your-crash-sheet-id>"
$env:MAIN_SHEET_ID="<your-main-sheet-id>"
$env:G10_SHEET_ID="<your-g10-sheet-id>"
$env:G9_SHEET_ID="<your-g9-sheet-id>"

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

## 11. Webapp (Upload Channel)

The Streamlit webapp is the production upload channel: open it in the browser,
upload a photo or paste names, answer course/session/type questions, and review
the results.

### 11a. Configure secrets

Copy `webapp/.streamlit/secrets.toml.example` to `webapp/.streamlit/secrets.toml`
(or use `webapp/.env.example` / `.env`) and fill in `WEBAPP_PASSWORD`, the Google
credentials, and the course sheet IDs. See `webapp/README.md` and `webapp/DEPLOY.md`.

### 11b. Start the webapp
```powershell
streamlit run webapp/app.py
```

Open http://localhost:8501, log in, then upload a photo (course → session → type
for G9/G10) or paste a list of names.

### 11c. Keep it running (Windows Task Scheduler)
1. Open Task Scheduler → Create Basic Task
2. Trigger: At system startup (or daily at the start of teaching day)
3. Action: Start a program
   - Program: `C:\Users\lenovo\Desktop\attendance-automation\.venv\Scripts\streamlit.exe`
   - Arguments: `run webapp/app.py --server.port 8501`
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

Temp images are deleted immediately after processing (`os.remove(img_path)`).
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
[ ] All 4 sheets shared as Editor with your service account email
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
[ ] Webapp started and tested end-to-end with a real photo upload
[ ] Notification channel configured (SMTP_* for email, or the log channel) and tested
[ ] Task Scheduler entry created to auto-start the webapp on boot
[ ] Team briefed: how to use the webapp upload screen and how to resolve review queue items
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

## Release notes: Phase 1–2
- No new environment variables and no new dependencies.
- New optional settings live in `config/attendance.yaml` (`attendance.ingest.names_preview`, `attendance.ingest.multi_photo.*`, `attendance.absent_button.*`); every one has a safe default, see `CHANGELOG.md`.
- `review_queue/_absent/` (small JSON files, row numbers only) is safe to delete old files from.
- `review_images/` is no longer tracked by git and is git-ignored. It holds photos of student sheets: keep it out of any public repo.

## Release notes: Phase 3
- New small data files next to the old ones in `config/`: `learned_aliases.meta.json` (alias dates) and `variant_suggestions.json` (pending name-variant suggestions). Both are git-ignored like `learned_aliases.json`; back them up together. `review_queue/_history/` holds counts only.
- Approving a name variant (`python -m attendance.cli aliases variants review`) edits `config/name_variants.yaml` (backup `.bak`). Restart the webapp afterwards: that file is read once at start-up.
- `aliases remove` keeps `config/learned_aliases.json.bak` for one undo.
- To save Gemini calls, set `attendance.ocr.second_pass.mode: low_confidence` (default `full` = unchanged behaviour).

## Release notes: Phase 4 (operations and security)
- Reminders and health alerts are configured in `config/attendance.yaml` (`attendance.reminders.*`, `attendance.health.*`). Alerts go to admins only; reminder recipients are `admin | teacher | both`. Dry-run uploads are not counted, so a dry-run-only setup keeps reminding.
- If the original zip with `.env`, the service-account key or the SSH keys was ever shared or committed, rotate those secrets now.

### Go-live checklist: reminders, alerts
1. Run `pip install -r requirements.txt`, start the webapp, and confirm `/health` (webapp admin screen) shows reminder/alert status.
2. Reminders (optional): in `config/attendance.yaml` set `attendance.reminders.enabled: true`, fill `schedule` per course (weekday + class end time), choose `recipients` (default `both`). Reminders only count REAL uploads.
3. Alerts: default thresholds are on. Alerts go only to admins. Lower `gemini_threshold` / `sheets_threshold` if you want earlier warnings.

## 16. Free.ai OCR Setup

The system uses **Free.ai GOT-OCR 2.0** as its primary OCR provider, optimised
for Arabic handwriting recognition. When the free.ai daily token limit is reached
(6,000/day free, 30,000/day with account), the system automatically falls back to
**Gemini** as a secondary provider.

### 16a. Get an API key

1. Sign up at [free.ai](https://www.free.ai/)
2. Generate an API key from the dashboard
3. Set the environment variable:

```bash
export FREEAI_API_KEY="your-key-here"
# Also keep GEMINI_API_KEY for fallback:
export GEMINI_API_KEY="your-gemini-key"
```

### 16b. Configure in attendance.yaml

```yaml
# config/attendance.yaml
attendance:
  ocr:
    provider: freeai               # freeai | gemini | mock
    model: got-ocr2                # free.ai GOT-OCR 2.0 model
    freeai_api_key_env: FREEAI_API_KEY
    fallback_to_gemini: true       # fall back to Gemini when free.ai hits token limit
```

You can also set `provider: gemini` to use Gemini as the primary provider
(e.g., when free.ai is down):

```yaml
attendance:
  ocr:
    provider: gemini
    model: gemini-3.1-flash-lite
```

### 16c. Token limits

| Tier | Daily limit |
|---|---|
| Free | 6,000 tokens/day |
| Registered | 30,000 tokens/day |

When the limit is reached, `OcrUnavailableError` is raised and the pipeline
automatically retries with Gemini (if `fallback_to_gemini: true`).

### 16d. Verify

```bash
python -c "from attendance.ocr import FreeAIProvider; print('FreeAIProvider OK')"
python -c "import openai; print('openai SDK OK')"
```
