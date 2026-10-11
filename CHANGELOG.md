# Changelog

Newest first. Phases follow the feature plan (Phase 1 cleanup, 2 input, 3 accuracy, 4 operations).
Test counts are `pytest -q` totals.

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
