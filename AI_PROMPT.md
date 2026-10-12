You are continuing a Python project (Telegram bot: attendance-sheet photo -> Gemini OCR -> fuzzy match -> Google Sheets). Previous AIs ran out of budget; you continue.

READ ONLY: `HANDOFF.md`, then `PROJECT_CONTEXT.md`. Skip `docs/archive/`. Never open big files whole (ingest.py is ~1450 lines): grep for the function you need and view small ranges.

DO:
1. `pip install -r requirements.txt && pytest -q`. Put the pass count in HANDOFF.md "Log".
2. Take the first task in the HANDOFF.md table that is not ✅.
3. Implement it exactly per its spec, add/adjust tests, run the full suite, fix regressions.
4. Tick the table, add a 1-line Log entry (what changed, gotchas), commit `feat: T<n> <title>`.
5. Repeat. When ~15% of your budget remains: stop starting new tasks, leave tests green, and make HANDOFF.md accurate (done / half-done / gotchas) for the next AI.

RULES: never print or commit secrets (`.env`, key JSONs, ssh keys) or student names; preserve each file's line endings; new bot handlers need `@_guard` + `tests/test_roles.py::EXPECTED_LEVEL`; tests monkeypatch module attributes, so retarget patches when moving code; bot is LIVE (`dry_run_default: false`), keep dry-run semantics. Don't refactor beyond the task.
Stop at ~15% budget left: note any half-done work precisely and output only the changed files you did.

OUTPUT: a completely full new zip with the updates you made.
