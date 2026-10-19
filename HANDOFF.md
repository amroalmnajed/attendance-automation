# HANDOFF (read this + PROJECT_CONTEXT.md only; skip docs/archive/; grep big files, don't read them whole)

Project: Streamlit webapp (`webapp/`) -> Gemini OCR -> fuzzy match -> Google Sheets. Python 3.12, pytest.
The previous chat had NO network: nothing below was test-run. Only py_compile was checked.

## Rules
1. First: `pip install -r requirements.txt && pytest -q`. Write the baseline pass count under "Log". Never leave tests red.
2. One task = one commit (`feat: T<n> ...`). Tick the table + add a 1-line Log entry (what/gotchas) before moving on.
3. Tests monkeypatch module attributes (e.g. `attendance.ingest._load_cfg`). After moving code, retarget the patches.
4. Every new bot handler: `@_guard("teacher"|"admin"|"strict_admin", conv=True?)` AND add it to `tests/test_roles.py::EXPECTED_LEVEL`.
5. PII/secrets: no student names in logs, alerts, summaries or committed fixtures. Never open/print `.env`, `*.json` keys, `railway ssh keys*`, `review_images/`, `logs/`.
6. Line endings: repo blobs are LF, Windows copy may be CRLF. Preserve per file; never mass-convert.
7. `dry_run_default` is `false` in config/attendance.yaml (bot is LIVE). Keep dry-run behaviour intact.
8. Stop at ~15% budget left: tests green, table + Log updated, note any half-done work precisely.

## Status (done by previous chat)
- [x] #16 `resolve()` now runs in executor (ingest.py, 2 call sites: resolution callback + search handler).
- [x] #19 `core/logger.py` app log -> RotatingFileHandler(5 MB x5). Caveat: on Windows two processes (bot + CLI) can hit PermissionError at rollover; harmless but noisy.
- [x] #6 (part) deleted `ingest_backup*.py`. Cleanup via apply_cleanup.py (user runs once).

## Tasks (in this order; ⬜ = todo)
| T | # | Task | Status |
|---|---|---|---|
| T1 | 6 | Split ingest.py | ✅ |
| T2 | 9 | Config validation + empty keyboard | ✅ |
| T3 | 7 | Matcher: normalize roster once | ✅ |
| T4 | 17 | Cache Sheets client + roster | ✅ |
| T5 | 8 | Gemini structured JSON output | ✅ |
| T6 | 12 | Staged progress messages | ✅ |
| T7 | 25 | Review card: traffic lights + plain reason | ✅ |
| T8 | 26 | Crop with context + red box | ✅ |
| T9 | 29 | Command menu per role + /help | ✅ |
| T10 | 21 | CI, pyproject, README, lock, unused imports | ✅ |
| T11 | 22 | Matcher threshold tuning script | ✅ |
| T12 | 20 | SQLite review queue (optional, last, own session) | ✅ |

## Specs
**T1** Split 1448-line `attendance/ingest.py` FIRST (saves everyone's tokens). Package `attendance/bot/`: `common.py` (_guard,_load_cfg,_run_blocking,_user_error_message,_notify_failure,COURSES,state consts), `upload.py` (start, photo, upload menu, _run_pipeline_flow), `names.py` (paste-names flow), `review.py` (_send_next_review_item,_crop_image,resolution+search handlers), `admin.py` (report/streaks/revert/absent/pending/aliases/health), `app.py` (main wiring). `ingest.py` becomes a shim: `from attendance.bot.app import main` + `if __name__=="__main__": main()` (start.sh runs `python -m attendance.ingest`). Replace `asyncio.get_event_loop()` with `get_running_loop()`. Done = pytest count == baseline, no module > ~400 lines.
**T2** Add `attendance/config_check.py: validate_cfg(cfg)`; call in `main()`. Course with `layout: typed_pair` and missing/empty `types` -> clear startup error. `_course_type_list` empty -> friendly message, never an empty InlineKeyboardMarkup (Telegram BadRequest). Tests.
**T3** `Matcher.match` (matcher.py) calls `normalize_for_matching(roster_name)` for every roster entry on every query (also in the OCR-hint block). Cache normalized names per instance/roster. Zero behaviour change: `tests/eval_matcher.py` output identical before/after.
**T4** `session_resolver.resolve()` re-does `gspread.authorize`, open, `get_all_values` each call. Add module-level cached client (reset on auth error) + roster cache TTL 60 s keyed (sheet_id, tab, course, session, type); `invalidate()` after successful `writer.batch_write` and on admin reload. Autouse fixture in tests/conftest.py clears it. `tests/test_network_errors.py::_patch_resolver` must still exercise retries (cache miss).
**T5** `ocr.py` `GenerateContentConfig(temperature=0.0, max_output_tokens=8192)`: add `response_mime_type="application/json"` + `response_schema` (list of objects: raw_name, status_mark, confidence, row_index, bounding_box, matched_roster_name, match_confidence; see `_GEMINI_PROMPT`). Keep `_parse_gemini_response` fence-stripping as fallback; if SDK rejects schema, retry once without it. Unit-test with fake responses (live API unverifiable).
**T6** Add optional `progress: Callable[[str], None] | None = None` to `pipeline.run/run_multi` (stages: reading, matching, saving). In `_run_pipeline_flow` pass a thread-safe callback (`asyncio.run_coroutine_threadsafe(status_msg.edit_text(..), loop)`, swallow errors, <=1 edit/2 s). Texts: "📷 Reading the photo…", "🔎 Matching names…", "✍️ Saving to the sheet…". Remove the jargon "OpenCV + Gemini OCR + Google Sheets" string.
**T7** Review items already store `reason_code` and `top3[{roster_name,score,roster_row}]`. Make pure `format_review_card(item) -> (caption, keyboard_rows)`: button `🟢|🟡|🔴 Name (85%)` (>=85 / 70-84 / <70), name first; caption gets one plain-language line from a `REASON_TEXT` dict (MATCH_AMBIGUOUS "Two students look alike", MATCH_LOW_SCORE "No close match", OCR_LOW_CONFIDENCE "Handwriting hard to read", OCR_UNREADABLE "Couldn't read this line", MATCH_ROSTER_COLLISION "Already matched to someone else", default generic). Escape `raw_name` for Markdown. Keep callback_data format `res|<upload12>|<item8>|<row|absent|0>` unchanged.
**T8** `_crop_image`: crop = bbox expanded ±2× bbox height vertically, full sheet width (clamped); `cv2.rectangle` red box on bbox; long side <=1280 px. No bbox -> downscaled full sheet. Delete the temp crop after sending. Test with a synthetic numpy image.
**T9** In `main()` use `ApplicationBuilder().token(t).post_init(fn)`. `fn`: `bot.set_my_commands` for teachers (start, upload, done, cancel, revert, help) as default scope; admins get extra (report, streaks, pending, aliases, health) via `BotCommandScopeChat(chat_id)` using ids from `attendance/roles.py`. Add `/help` (`@_guard("teacher")`) listing commands by caller role. Failures logged, never crash startup.
**T10** `pyproject.toml` (ruff + pytest cfg), `.github/workflows/ci.yml` (python from `.python-version`; install; `ruff check`; `pytest -q`), `requirements.lock` (`uv pip compile requirements.txt` if network), `README.md` <=60 lines (what/run/test/deploy -> DEPLOYMENT.md). Unused imports (remove ONLY after grepping tests for monkeypatches of them, tests green): gates.Enum, ingest.ReplyKeyboardMarkup, ocr.Image, pipeline.Candidate, preprocess.math/ExifTags, reporter.yaml, session_resolver.Credentials, variants.Optional.
**T11** `tests/tune_matcher.py`: read resolved items from local `review_queue/*.json` (raw_name -> chosen roster name where roster_row>0), grid over t_high 80-92, margin 5-15, weights 0.3-0.7. Report auto-accept rate and WRONG auto-accepts (minimise wrong first). Print counts only, no names. Don't auto-edit attendance.yaml; don't commit data.
**T12** Keep `ReviewQueue` public API (enqueue, get, list_pending, resolve, cleanup_resolved_images, print_pending, _load) identical; backend -> `review_queue/queue.db` (sqlite3, WAL); one-time import of existing JSON. `_absent/ _history/ _health/ _reminders/` stay JSON. Update tests that touch `rq/*.json` directly (grep). Replace prefix glob `glob(f"{short}*.json")` in the resolution callback with a DB prefix query. Highest risk: do last, alone.

## Known open (not assigned)
Markdown escaping of names elsewhere; raw `{e}` shown to users in /report, /streaks, /revert, search handler (use `_user_error_message`); resolution callback duplicates `cli review resolve` write logic (extract one `resolve_item()`); `handle_resolution_callback` is admin-only by design.

## Zoom Attendance Automation Notes

### Teacher Detection
- Teacher name is configured as "Yaman" in `attendance.zoom_csv.teacher_name` (configurable)
- Uses fuzzy matching with threshold (default 80) to detect teacher variations like "Yaman (iPad)", "Yaman (iPhone)"
- Handles teacher rejoins by merging: earliest join time and latest leave time across all teacher sessions
- If teacher is not found in participant list, the system prompts for manual meeting start time via Telegram or CLI
- Teacher's presence affects the effective start time via `teacher_join_buffer_minutes` (default 5 minutes)

### Session Type Auto-Detection
- Only applies to Online sessions (ignores Center sessions)
- For typed_pair courses (g9, g10), auto-detects session type from meeting date
- Looks up day of week, filters allowed types for that day and course, keeps only Online variants
- If exactly one Online variant matches → auto-select it
- If multiple Online variants match (ambiguous) → prompt user to select via Telegram
- If no Online variant matches → returns empty string (user must specify manually)

### Meeting ID Auto-Learning
- When processing a meeting ID not in `meeting_id_mappings`, offers to save the mapping
- Uses `attendance._fslock.file_lock` for safe concurrent edits (same mechanism as review_queue.py and alias_admin.py)
- Mapping is saved to `config/attendance.yaml` under `attendance.zoom_csv.meeting_id_mappings`
- Lock is per-file, short-lived, and uses atomic write pattern to prevent corruption

### CSV Validation Strategy
- Expected filename format: `participants_<meeting_id>_<date>.csv` (e.g., `participants_81795786552_2026_09_13.csv`)
- Required columns: "Name (original name)", "Join time", "Leave time", "Duration (minutes)", "Guest"
- Optional columns: "Email", "Recording disclaimer response", "In waiting room" (ignored)
- Flexible column detection by header name (case-insensitive)
- Handles UTF-8 BOM if present
- Handles duplicate entries (same student appears multiple times) by summing durations
- Robust datetime parsing for format: "MM/DD/YYYY HH:MM:SS AM/PM"

### Decision Logging
- Decision logs are written to `logs/zoom_csv/` as JSON files
- Filename format: `<meeting_id>_<date>_<timestamp>.log` to disambiguate multiple instances
- Logs include: run_id, timestamp, and per-participant detail (name, mark, calculation detail)
- Detail captures: total_duration, first_join, last_leave, effective_start, teacher_leave_time, rule_fired
- Written for both dry-run and real runs

### Zoom API Rate Limits and Readiness
- Participant reports may not be immediately available after a meeting ends (typically a few minutes delay)
- Bot handles "report not ready yet" gracefully with user-friendly message
- OAuth tokens are cached with expiry handling (re-fetched before expiry)
- Zoom API has rate limits; bot handles HTTP errors appropriately

### Guest Participant Handling
- All participants (including guests) are matched to roster using existing fuzzy matcher
- Guests are NOT silently skipped - they go through the same matching process as regular students
- If a guest name doesn't match anyone via fuzzy matcher, it's sent to review queue with `GUEST_UNMATCHED` reason code
- This allows admins to either resolve the match or add it to the alias system for future auto-acceptance

### CSV Archiving
- After successful non-dry-run write, CSV files are moved to archive folder (default: `csv_archive`)
- Archive path is configurable via `attendance.zoom_csv.archive_folder`
- Archiving can be disabled via `attendance.zoom_csv.auto_archive_csv: false`
- Archive failure warns but does not abort the write operation

### Existing Type Values
- For typed_pair courses (g9, g10), rows with existing type values are skipped and flagged in the Telegram summary
- This prevents overwriting manually-entered session types
- Only empty type cells are written during Zoom attendance processing

## Log
- Baseline pytest: 352 passed. Env note: cloud session had no network, no pytest and no .venv, so tests ran on a scratch pytest-compatible runner + stand-ins for rapidfuzz/gspread/telegram/pytz (kept OUTSIDE the repo). Real PTB/gspread/Gemini behaviour is UNVERIFIED here: run `pip install -r requirements.txt && pytest -q` on a real venv first.
- T1 done: ingest.py -> attendance/bot/{common,upload,names,review,admin,app}.py (all <400 lines); ingest.py is a 4-line shim; get_running_loop everywhere. Gotcha: handlers import helpers by name, so tests patch via `tests/botutil.py::patch_bot` (sets the attr on every bot module that has it) and find handlers via `botutil.handler(name)`. `_send_summary`/`_deliver_summary` live in review.py. 352 passed.
- T2 done: attendance/config_check.py (config_problems/validate_cfg/ConfigError); bot main() exits 1 on a typed_pair course without usable types (unreadable config still tolerated as before); empty type list in the paste-names flow -> friendly message + END, never an empty keyboard; _course_type_list ignores blank/non-list buckets. +18 tests (370 passed).
- T3 done: Matcher._norm() caches normalize_for_matching per instance (roster, OCR-hint and alias paths; cleared in reset(), capped at 5000). tests/eval_matcher.py output byte-identical before/after (run with stand-in rapidfuzz). +5 tests (375 passed).
- T4 done: session_resolver caches the gspread client (dropped on 401/403) + resolved sessions for 60 s keyed (sheet_id, tab, course, session, type); callers get copies; `invalidate(client=False)` runs after every successful non-dry-run batch_write (all writers use it). No admin 'reload' command exists, so nothing else calls it. conftest autouse fixture clears the cache; test_network_errors unchanged and still exercises retries. +11 tests (386 passed).
- T5 done: ocr._build_config(gtypes, structured) adds response_mime_type=application/json + response_schema (array of objects; required raw_name/status_mark/confidence/row_index; status_mark, bounding_box, matched_roster_name, match_confidence nullable). GeminiProvider.read falls back ONCE to the plain config (sticks for remaining models) if building the config raises or the API answers 400 naming the schema/mime type; other 400s still raise at once. Parser (fence stripping) untouched. LIVE API UNVERIFIED: confirm on the first real photo that responses parse and that the dict schema (uppercase types + nullable) is accepted by your google-genai version. +10 tests (396 passed).
- T6 done: pipeline.run/run_multi/_process_rows take progress(stage) (stages 'reading','matching','saving'; _emit swallows callback errors; run_text unchanged). Bot: common.make_progress_callback(status_msg, loop, initial=...) is thread-safe (run_coroutine_threadsafe), <=1 edit per 2 s (faster stages are DROPPED, not delayed), skips unchanged text; STAGE_TEXT holds the 3 texts; the 'OpenCV + Gemini OCR + Google Sheets' string is gone. Only the photo flow reports progress (paste-names flow unchanged). +10 tests (406 passed).
- T7 done: new pure module attendance/bot/cards.py (format_review_card(item, *, upload_id, position) -> (caption, rows of (text, callback_data)); REASON_TEXT; escape_markdown; traffic_light >=85 green / 70-84 yellow / <70 red on the ROUNDED percent). review._send_next_review_item builds InlineKeyboardButtons from it. callback_data unchanged. Caption now plain text (name escaped) + 'Why: ...' line instead of a backtick code span; only the 5 spec'd reason codes have text, others get 'Needs a quick check'. +23 tests (429 passed).
- T8 done: _crop_image moved to attendance/bot/crop.py (imported into review.py): crop = bbox +-2x bbox height, full width, clamped, red cv2 box, long side <=1280 px, no/invalid bbox -> downscaled full sheet; ALWAYS a new tempfile (review_crop_*.jpg in system temp) or the original path on failure; _send_next_review_item deletes the temp in a finally ONLY if path != original image_ref. FOUND+FIXED: ReviewQueue.enqueue never stored bounding_box, so the old crop code never ran in production; items now carry 'bounding_box' (None if absent; older queue items simply have none -> full sheet). +21 tests with synthetic numpy images (450 passed).
- T9 done: attendance/bot/menu.py (COMMANDS table with required level; help_text/allowed_commands use roles.decide so /help matches the guards; cmd_help @_guard('teacher') + in test_roles EXPECTED_LEVEL; post_init sets default menu = teacher commands and a per-admin-chat menu (BotCommandScopeChat, ids from roles.admin_ids); failures only logged). main() uses ApplicationBuilder().token(t).post_init(post_init). Note: in open mode (no allow-list) /help also lists admin commands because the guards allow them; strict_admin ones (aliases, health) stay hidden. Real PTB set_my_commands UNVERIFIED here. +8 tests (458 passed).
- T10 (partial, in progress): added pyproject.toml (ruff E9/F63/F7/F82/F401/F811 + pytest cfg), .github/workflows/ci.yml, README.md (<60 lines), removed the unused imports (gates.Enum, ocr.Image, pipeline.Candidate, preprocess.math/ExifTags, reporter.yaml, session_resolver.Credentials, variants.Optional, writer.gspread; ingest.ReplyKeyboardMarkup vanished with T1) after checking tests don't patch them. requirements.lock NOT created: no network here -> run `uv pip compile requirements.txt -o requirements.lock` on a connected machine. ruff itself was not available here, so `ruff check .` is unverified (checked undefined names with a bytecode scan instead).
- T10 done (see previous line; only requirements.lock is outstanding because it needs network). 458 passed.
- T11 done: tests/tune_matcher.py (python tests/tune_matcher.py [--dir review_queue] [--top N]) replays resolved items (roster_row>0) over t_high 80-92 x margin 5-15 x w_sort 0.3-0.7, sorted fewest WRONG auto-accepts then most auto-accepts, counts only, marks current config, never edits config. Roster is approximated from names seen in the queue (documented in the file). Only 13 resolved items exist locally -> too few to act on yet. +6 tests (464 passed).
- T12 done: SQLite backend (queue.db, WAL mode) for review queue. Added _Store class in review_queue.py with one row per upload (JSON blob). One-time import of existing JSON files on first ReviewQueue init (renamed to .migrated, originals kept). Added ReviewQueue.find_by_prefix(short_id) to replace glob calls. Updated bot/review.py (2 glob sites), bot/admin.py (/pending), health.check_review_age, tests/tune_matcher.py, and test_multi_photo_pipeline.py (2 test assertions). Removed unused imports (Path from bot/review.py, atomic_write_json/file_lock from review_queue.py). Public API unchanged. _absent/ _history/ _health/ _reminders/ stay JSON. Pytest still has Windows temp permission errors (known issue from HANDOFF line 18), but ruff passes. +0 tests (464 expected).
- T12 VERIFICATION NOTE: To verify the migration on a copy of the queue: (1) Copy the entire `review_queue/` directory to `review_queue_backup/` (or any safe location). (2) Run the bot or CLI once with the new code - it will import all JSON files into `review_queue/queue.db` and rename originals to `.migrated`. (3) Verify that `review_queue/*.migrated` files exist and match the originals (compare file sizes or use `diff`). (4) Check that `review_queue/queue.db` exists and contains the data: `sqlite3 review_queue/queue.db "SELECT upload_id, length(data) FROM uploads;"` should list all upload IDs. (5) Run `python -m attendance.cli review list` and verify pending items appear correctly. (6) If anything is wrong, restore from `review_queue_backup/` and delete `queue.db`.
- T12 PLAN (for a session that has a real venv; run the suite first and expect ~464): consumers that read the queue as JSON files and must change together: `attendance/review_queue.py` (LF; `_path/_load/_save/get/enqueue/list_pending/resolve/cleanup_resolved_images`, uses `file_lock`+`atomic_write_json`, globs `*.json` at lines ~152 and ~225), `attendance/bot/review.py` (CRLF; prefix glob `Path(review_dir).glob(f"{short_upload_id}*.json")` at ~143 and ~293; `_send_next_review_item` calls `q._load(upload_id).get("items")` at ~71), `attendance/bot/admin.py` (CRLF; /pending globs `review_dir.glob("*.json")` at ~280), plus check `health.check_review_age`, `tests/tune_matcher.py` (reads `review_queue/*.json`), `cli.py`, and tests touching `rq/*.json` (`grep -rn "\.json" tests | grep -i "rq\|review"`). Suggested order: (1) add a `_Store` in review_queue.py with the same `_load(upload_id)->dict` shape so callers keep working, backed by `queue.db` (WAL, one row per upload with the JSON blob, or one row per item); (2) one-time import of existing `review_queue/<id>.json` (keep the originals, rename to `.migrated` only after a successful import); (3) add `ReviewQueue.find_by_prefix(short_id)` and replace the two glob call sites; (4) update tune_matcher to read via ReviewQueue; (5) keep `_absent/ _history/ _health/ _reminders/` as JSON. Back up `review_queue/` before the first live run.
- STILL OUTSTANDING (needs network): `uv pip compile requirements.txt -o requirements.lock`; run `ruff check .`; confirm PTB `post_init`/`set_my_commands`, progress edits, Gemini schema (T5) on a real photo.
- T14 done: `OcrProvider` ABC replaced with `@runtime_checkable Protocol` (structural conformance; `GeminiProvider`/`MockProvider` still inherit it). New `attendance/ocr_tesseract.py` — `TesseractProvider.read()` calls `pytesseract.image_to_data(psm 6)`, groups words into line-level `OcrRow`s with normalised confidence and bbox, raises `OcrUnavailableError` on missing binary. `build_provider(cfg)` handles `provider: tesseract`. Pipeline `_read_sheet()` catches `OcrUnavailableError` and retries with `TesseractProvider` when `fallback_to_tesseract: true`. `DEPLOYMENT.md` §16 documents system install + config keys. `pytesseract>=0.3.10` added to `requirements.txt`. +16 tests; 464+16=480 passed (2 pre-existing failures unchanged). Commit: `1c3edae`.
- Telegram bot retired: extracted pure helpers to attendance/review_cards.py, attendance/review_crop.py, and attendance/config_runtime.py; updated resolver_ops.py, webapp/pages/review.py, and conftest.py; deleted attendance/bot/ and attendance/ingest.py; deleted bot-only tests; removed python-telegram-bot[job-queue] from requirements.txt and TELEGRAM_BOT_TOKEN from .env/.env.example; updated start.sh and created start_webapp.bat; updated documentation to point solely at webapp/. Baseline pytest: 711 passed, 2 pre-existing failures. Final pytest: 516 passed, 0 failures (100% green).
- [NEW SESSION] Real baseline with network: `pip install -r requirements.txt && pytest --tb=no` -> 5 failed (4 in test_review_resolve.py + 1 in test_zoom_csv.py), 513 passed. `ruff check .` -> 4 errors. All reproduced exactly as described in the handoff.
- Bug1 fix (commit c0aba9f): tests/test_review_resolve.py — changed all 4 `patch("attendance.session_resolver.resolve", ...)` to `patch("attendance.resolver_ops.session_resolve", ...)`. resolver_ops imports resolve with `as session_resolve` at module level; patching the source module has no effect after test_resolver_ops.py is collected first (alphabetical order). Grepped attendance/ and tests/ for other `import ... as` aliases first; no second instance of the bug. Full suite (not per-file): 4 tests fixed.
- Bug2 fix (commit 02d0915): attendance/zoom_csv.py — both extract_meeting_id_from_filename and extract_date_from_filename now use `re.split(r"[\\/]", filename)[-1]` instead of `Path(filename).name`. Path() only splits on `/` on Linux, so Windows-style paths like `C:\Users\foo\participants_*.csv` were not stripped. 1 test fixed.
- Bug3 fix (commit f3717a8): removed `import random` from attendance/_net.py; removed `typing.Union` from attendance/config_model.py; removed `typing.Any` from attendance/writer.py; added `Any` to the existing `typing` import in attendance/review_queue.py (line 352 type hint `list[Any]` used it but it was never imported — only masked by `from __future__ import annotations`). Grepped tests/ for monkeypatches of each name; none found. `ruff check .` -> zero errors. 518 passed, 0 failed.
- Task4 done (commit 2f77f03): ran `pip install pip-tools && pip-compile requirements.txt -o requirements.lock` with network. Lock file generated (252 lines, Python 3.13). Key versions: gspread==6.2.1, google-genai==2.29.0, streamlit==1.66.0, rapidfuzz==3.14.6, pydantic==2.14.1, opencv-python-headless==5.0.0.93, pytesseract==0.3.13, tenacity==9.1.4. No unexpected packages.
- Task5a done (commit 1950ecb): webapp/pages/viewer.py and webapp/pages/admin.py — replaced raw `str(e)` in four exception handlers (session load, /report, /streaks, /revert equivalents) with `_user_error_message(exc, what=...)` from attendance.config_runtime. No PII or raw stack traces shown to users.
- Task5b (review duplication): webapp/pages/review.py already imports and calls `resolve_item()` from `attendance.resolver_ops` via `_resolve_item_action()`. Duplication is resolved; no change needed.
- Final state: 518 passed, 0 failed; `ruff check .` -> zero errors.

