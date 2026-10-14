# Changelog

Newest first. Phases follow the feature plan (Phase 1 cleanup, 2 input, 3 accuracy, 4 operations).
Test counts are `pytest -q` totals.

## Zoom Attendance Automation (new feature)

### Added
- **Zoom API integration** (`attendance/zoom_api.py`): Server-to-Server OAuth token fetch with caching, participant report fetching, and instance listing for recurring meetings.
- **Zoom CSV parsing** (`attendance/zoom_csv.py`): CSV file parsing with UTF-8 BOM support, teacher detection with fuzzy matching and rejoin merging, attendance calculation based on join time/duration/teacher presence, session type auto-detection, CSV archiving, meeting ID mapping with file locking, and decision logging.
- **Zoom pipeline integration** (`attendance/pipeline.py`): `run_zoom()` function that processes Zoom data from either CSV files or Zoom API, integrates with existing matching and writing logic, and supports dry-run mode.
- **Telegram bot Zoom commands** (`attendance/bot/zoom.py`): `/zoom_fetch` (API fetch), `/zoom_upload` (CSV upload), `/zoom_reprocess` (reprocess archived CSV) with shared wizard flow for meeting ID lookup, instance selection, session type detection, teacher time handling, and confirmation.
- **CLI Zoom commands** (`attendance/cli.py`): `zoom-attend` interactive wizard for Zoom attendance, and `--from-archive` flag on `process-text` for reprocessing archived CSVs.
- **Guest participant handling**: All participants (including guests) are matched to roster using existing fuzzy matcher. Unmatched guests are sent to review queue with `GUEST_UNMATCHED` reason code.

### New config keys (`config/attendance.yaml`)
| Key | Default |
|---|---|
| `attendance.zoom_csv.enabled` | `true` |
| `attendance.zoom_csv.meeting_id_mappings` | `{}` (e.g., `81795786552: g10`) |
| `attendance.zoom_csv.teacher_name` | `"Yaman"` |
| `attendance.zoom_csv.teacher_fuzzy_match_threshold` | `80` |
| `attendance.zoom_csv.teacher_join_buffer_minutes` | `5` |
| `attendance.zoom_csv.late_threshold_minutes` | `15` |
| `attendance.zoom_csv.min_duration_minutes` | `45` |
| `attendance.zoom_csv.early_leave_threshold_minutes` | `20` |
| `attendance.zoom_csv.duplicate_handling` | `"sum_duration"` |
| `attendance.zoom_csv.auto_session_type` | `true` |
| `attendance.zoom_csv.auto_archive_csv` | `true` |
| `attendance.zoom_csv.archive_folder` | `"csv_archive"` |
| `attendance.zoom_csv.auto_learn_meeting_ids` | `true` |
| `attendance.zoom_csv.csv_strict_validation` | `true` |
| `attendance.zoom_csv.log_folder` | `"logs/zoom_csv"` |
| `attendance.zoom_csv.zoom_api.account_id_env` | `"ZOOM_ACCOUNT_ID"` |
| `attendance.zoom_csv.zoom_api.client_id_env` | `"ZOOM_CLIENT_ID"` |
| `attendance.zoom_csv.zoom_api.client_secret_env` | `"ZOOM_CLIENT_SECRET"` |
| `attendance.zoom_csv.zoom_api.token_url` | `"https://zoom.us/oauth/token"` |
| `attendance.zoom_csv.zoom_api.api_base_url` | `"https://api.zoom.us/v2"` |

### Env vars
- `ZOOM_ACCOUNT_ID` - Zoom account ID for Server-to-Server OAuth
- `ZOOM_CLIENT_ID` - OAuth app client ID
- `ZOOM_CLIENT_SECRET` - OAuth app client secret

### Dependency
- `requirements.txt`: `httpx>=0.25.0` - Modern HTTP client for Zoom API calls

### New bot commands
- `/zoom_fetch` - Fetch participant report from Zoom API by meeting ID
- `/zoom_upload` - Upload a CSV file manually
- `/zoom_reprocess` - Reprocess an archived CSV file

### New CLI commands
- `zoom-attend` - Interactive wizard for Zoom attendance (CSV upload / API fetch / reprocess archive)
- `process-text --from-archive` - Reprocess an archived Zoom CSV

### New files
- `attendance/zoom_api.py` - Zoom API integration
- `attendance/zoom_csv.py` - CSV parsing and attendance calculation
- `attendance/bot/zoom.py` - Telegram handlers for Zoom flows
- `tests/test_zoom_csv.py` - Tests for parsing/calculation
- `tests/test_zoom_api.py` - Tests for Zoom API (mocked)

### Modified files
- `attendance/matcher.py` - Added `GUEST_UNMATCHED` reason code
- `attendance/pipeline.py` - Added `run_zoom()` function
- `attendance/cli.py` - Added `zoom-attend` subcommand and `--from-archive` flag
- `attendance/bot/common.py` - Added Zoom conversation states
- `attendance/bot/app.py` - Wired up Zoom commands and handlers
- `attendance/bot/menu.py` - Added Zoom commands to menu
- `core/notifier.py` - Added Zoom-specific flags to RunSummary

### Tests
- +24 tests for Zoom CSV parsing, attendance calculation, teacher detection, filename parsing, session type detection, archiving, meeting ID mapping, and decision logging
- +9 tests for Zoom API OAuth token handling, participant fetching, and instance listing (all mocked)

## Phase 4 — Operations and security  (tests: 262 -> 352)

### Added
- **Role-based bot access** (`attendance/roles.py`): two roles by Telegram user ID. *Teacher*: upload (photo/album), paste names,
  tap "Mark N absent?", undo with `/revert` or the `rev|` button. *Admin* (implicitly also a teacher): everything, including review
  (`res|`, `srch|`), `/report`, `/streaks`, `/pending`, `/aliases`, `/health`. Every handler and callback is guarded. A stranger gets a polite
  refusal containing only their own Telegram ID; a forbidden button press gets a short pop-up alert (spinner stops). If the role check itself
  fails, the user is refused. If NO teacher and NO admin is configured, everything stays open as before (clear warning in the log at start),
  except `/aliases` and `/health`, which stay closed until an admin is configured.
- **Missing-attendance reminders** (`attendance/reminders.py`, OFF by default): once per expected class, if no upload is recorded for that
  course on that date. Text = course + date/time only. Remembered in `<review_dir>/_reminders/sent.json` so a restart never repeats one.
- **Health and quota alerts** (`attendance/health.py`): admins are alerted (with a cooldown) when Gemini keeps failing or hitting quota,
  when Sheets writes keep failing, or when review items wait too long. State: `<review_dir>/_health/state.json` (timestamps only).
  Recording is one-line and can never raise or wait on the network (alerts are sent from a background thread). Dry runs record nothing.
- **`/health`** (admin only): Gemini/Sheets failure counts, oldest waiting review item, last alerts, whether reminders are on and can run.
- `core/notifier.py::send_text(text, chat_ids)`: plain-text Telegram sender (never raises, never logs the text).

### New config keys (`config/attendance.yaml`)
| Key | Default |
|---|---|
| `attendance.teacher.telegram_ids` | `[]` |
| `attendance.reminders.enabled` | `false` |
| `attendance.reminders.grace_minutes` / `.window_hours` / `.check_every_minutes` | `60` / `12` / `10` |
| `attendance.reminders.recipients` | `both` (`admin` \| `teacher` \| `both`) |
| `attendance.reminders.schedule` | `{}` e.g. `crash: [{day: mon, time: "18:00"}]` (uses `core.app.timezone`) |
| `attendance.health.enabled` | `true` |
| `attendance.health.window_minutes` | `60` |
| `attendance.health.gemini_threshold` / `.sheets_threshold` | `5` / `3` |
| `attendance.health.review_max_age_days` | `3` |
| `attendance.health.cooldown_minutes` | `120` |

### Env vars
- `TEACHER_TELEGRAM_IDS` (optional, comma-separated), merged with `attendance.teacher.telegram_ids`. `ADMIN_TELEGRAM_IDS` as in Phase 3. Restart the bot after changing either.

### Dependency
- `requirements.txt`: `python-telegram-bot>=20.0` became `python-telegram-bot[job-queue]>=20.0` (adds APScheduler). Needed only for reminders and the review-age alert; without it the bot still starts and logs a warning.

### New bot commands
- `/health` (admin only).

### Changed internals (backward compatible)
- `alias_admin.admin_ids` is now a thin wrapper over `roles.admin_ids`. `pipeline._read_sheet` accepts `dry_run` (wrapper around `_read_sheet_inner`).
  `history.entries()` added. `ocr.py` / `writer.py` call `health.record...` on their failure paths.
- Two existing tests changed on purpose: when the config cannot be loaded, the role check now runs first and answers with a friendly refusal
  (fail-safe) instead of the handler's own error message.
- `tests/conftest.py`: autouse fixtures clear role env vars and reset the health context between tests.

### Not verified live
- The real scheduler (APScheduler is not installed in the test venv), real Telegram button presses and real sends. Covered by mocks only.

## Phase 3 — Accuracy and matching  (tests: 227 -> 262)

### Added
- **Summary with comparison** (`attendance/history.py`): every upload card shows Present / Late / Absent / Review for this session
  and the previous session of the same course; warns when Present+Late fell sharply. History = counts only, in
  `<review_dir>/_history/<course>.json`. Dry runs record nothing; re-uploading a session replaces its entry.
- **Alias management** (`attendance/aliases.py`, `attendance/alias_admin.py`): confirmation count (already the vote count) plus
  dates (`first_seen`, `last_confirmed`, `last_used`) kept in a SIDECAR `config/learned_aliases.meta.json`; `learned_aliases.json`
  keeps its exact old format. Flags: `old`, `unused`, `undated` (learned before dates existed), `conflict`, `low`, `unknown-name`.
- **Second-pass OCR on low-confidence rows** (`pipeline._read_sheet`, one helper shared by `run` and `run_multi`).
- **Name-variant learning** (`attendance/variants.py`): reviewer decisions create *suggestions* only, separate from the alias store.
  Approved ones go to a machine-managed `learned_variants:` section at the end of `config/name_variants.yaml` (hand-written part untouched, `.bak` kept).
- `tests/conftest.py` now also protects `config/learned_aliases.meta.json` and `config/variant_suggestions.json`.

### New config keys (`config/attendance.yaml`)
| Key | Default |
|---|---|
| `attendance.summary_compare.enabled` | `true` |
| `attendance.summary_compare.drop_threshold_pct` | `30` |
| `attendance.summary_compare.min_previous` | `5` (ignore comparison when the previous session had fewer attendees) |
| `attendance.ocr.second_pass.mode` | `full` (= today's behaviour; `low_confidence` = re-read only weak rows) |
| `attendance.ocr.second_pass.model` / `.provider` | `null` / `null` (optional different model/provider for the extra read) |
| `attendance.ocr.second_pass.include_unmatched` | `true` |
| `attendance.match.alias_stale_days` | `90` |
| `attendance.match.variant_learning.enabled` | `true` (suggestions only, never applied automatically) |
| `attendance.match.variant_learning.suggestions_file` | `config/variant_suggestions.json` |
| `attendance.match.variant_learning.min_similarity` | `0.7` |
| `attendance.admin.telegram_ids` | `[]` |

### Env vars
- `ADMIN_TELEGRAM_IDS` (optional, comma-separated Telegram user IDs). Merged with `attendance.admin.telegram_ids`. With no admin configured `/aliases` stays unavailable.

### New CLI commands
- `aliases list [--course X] [--flagged-only] [--stale-days N]`, `aliases remove <course> <name> [--roster-name X] [--yes]` (keeps `learned_aliases.json.bak`), `aliases export <file.csv|.json>`.
- `aliases variants list|review [--from-aliases] [--min-count N]` (review = approve / reject one by one; restart the bot afterwards).

### New bot commands
- `/aliases` (admin only): `/aliases` (flagged), `/aliases list [course]`, `/aliases remove <course> <paper name>`.

### Changed internals (backward compatible)
- `RunSummary` gained `present_count`, `late_count`, `absent_count`, `prev_counts`, `prev_session`, `drop_warning` (numbers only, never names).
- `AliasStore` gained `flush_usage()`, `entries()`, `remove()`; `lookup()` is unchanged but remembers which aliases it applied. `store_from_cfg()` helper.
- `passes: 1` still means one read, whatever `second_pass.mode` says.

## Phase 2 — Input features  (tests: 114 -> 227)

### Added
- **Mixed marks in text/paste mode** (`attendance/marks.py`): `Ahmed Ali | L`, `Sara - absent`, `Omar, Late`.
  P/L/A/NC + English/Arabic words, case-insensitive. No mark = Present. Unknown mark = never guessed, goes to review.
  Used by `process-text`, the `attend` wizard and the bot paste flow, each with a parse preview.
- **"Mark N absent?" button** (`attendance/absent.py`), crash and main ONLY (g9/g10 hard-blocked in code).
  Writes only EMPTY cells through `batch_write`, audited under its own id (`rev|<abs_id>` undoes it), dry-run only previews.
- **Multi-photo uploads** (`attendance/photos.py`, `pipeline.run_multi`): Telegram albums or photos sent within
  `append_window_s`. Same name on 2+ photos merged; conflicting marks -> review (`PHOTO_MARK_CONFLICT`).
  All-or-nothing OCR. One `upload_id`; each review item keeps its own `image_ref` + `photo_index`.
- `tests/conftest.py`: protects the real `config/learned_aliases.json` from being modified by the test suite.

### New config keys (`config/attendance.yaml`)
| Key | Default |
|---|---|
| `attendance.ingest.names_preview` | `on_issues` (`always` / `on_issues` / `never`) |
| `attendance.ingest.multi_photo.enabled` | `true` |
| `attendance.ingest.multi_photo.max_photos` | `6` |
| `attendance.ingest.multi_photo.append_window_s` | `120` |
| `attendance.absent_button.enabled` | `true` |
| `attendance.absent_button.courses` | `[crash, main]` (g9/g10 ignored even if listed) |
| `attendance.absent_button.min_matched` | `3` |
| `attendance.absent_button.block_when_pending_review` | `true` |

### New CLI options / bot behaviour
- CLI: `process-text --yes/-y` (skip the confirm prompt when a mark is unrecognised).
- Bot: preview message after `/done`; `pv|go|fix|cancel` buttons (new state `NAMES_CONFIRM`); `abs|<upload_id>` button/callback.
- Env vars: none added.

### Changed internals (all backward compatible)
- `ReasonCode.PHOTO_MARK_CONFLICT`; `OcrRow` gained optional `source_image`, `photo_index`, `conflict_marks`.
- `RunSummary` gained `absent_offer`, `missing_count`, `missing_held_back`, `photo_count` (counts only, never names).
- `pipeline.run_text(..., marks=None)`; new `pipeline.run_multi(...)`; review items gained optional `photo_index`.
- `_run_blocking(fn, timeout_s=None)`.

## Phase 1 — Cleanup
- Deleted `attendance/ingest_backup.py`, `attendance/ingest_backup2.py` (unreferenced).
- Removed `.pytest_tmp/`, `__pycache__/`; `.gitignore` now also covers test artifacts, key files, `review_images/`.
- `review_images/` untracked from git (student sheet photos; files kept on disk).
