# HANDOFF (read this + PROJECT_CONTEXT.md only; skip docs/archive/; grep big files, don't read them whole)

Project: Telegram bot (`attendance/ingest.py`) -> Gemini OCR -> fuzzy match -> Google Sheets. Python 3.12, pytest.
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
| T5 | 8 | Gemini structured JSON output | ⬜ |
| T6 | 12 | Staged progress messages | ⬜ |
| T7 | 25 | Review card: traffic lights + plain reason | ⬜ |
| T8 | 26 | Crop with context + red box | ⬜ |
| T9 | 29 | Command menu per role + /help | ⬜ |
| T10 | 21 | CI, pyproject, README, lock, unused imports | ⬜ |
| T11 | 22 | Matcher threshold tuning script | ⬜ |
| T12 | 20 | SQLite review queue (optional, last, own session) | ⬜ |

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

## Log
- Baseline pytest: 352 passed. Env note: cloud session had no network, no pytest and no .venv, so tests ran on a scratch pytest-compatible runner + stand-ins for rapidfuzz/gspread/telegram/pytz (kept OUTSIDE the repo). Real PTB/gspread/Gemini behaviour is UNVERIFIED here: run `pip install -r requirements.txt && pytest -q` on a real venv first.
- T1 done: ingest.py -> attendance/bot/{common,upload,names,review,admin,app}.py (all <400 lines); ingest.py is a 4-line shim; get_running_loop everywhere. Gotcha: handlers import helpers by name, so tests patch via `tests/botutil.py::patch_bot` (sets the attr on every bot module that has it) and find handlers via `botutil.handler(name)`. `_send_summary`/`_deliver_summary` live in review.py. 352 passed.
- T2 done: attendance/config_check.py (config_problems/validate_cfg/ConfigError); bot main() exits 1 on a typed_pair course without usable types (unreadable config still tolerated as before); empty type list in the paste-names flow -> friendly message + END, never an empty keyboard; _course_type_list ignores blank/non-list buckets. +18 tests (370 passed).
- T3 done: Matcher._norm() caches normalize_for_matching per instance (roster, OCR-hint and alias paths; cleared in reset(), capped at 5000). tests/eval_matcher.py output byte-identical before/after (run with stand-in rapidfuzz). +5 tests (375 passed).
- T4 done: session_resolver caches the gspread client (dropped on 401/403) + resolved sessions for 60 s keyed (sheet_id, tab, course, session, type); callers get copies; `invalidate(client=False)` runs after every successful non-dry-run batch_write (all writers use it). No admin 'reload' command exists, so nothing else calls it. conftest autouse fixture clears the cache; test_network_errors unchanged and still exercises retries. +11 tests (386 passed).
