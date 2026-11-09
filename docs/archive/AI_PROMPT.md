You are continuing a Python project (Streamlit webapp: attendance-sheet photo -> Gemini OCR -> fuzzy match -> Google Sheets). Previous AIs ran out of budget; you continue.

READ ONLY: `HANDOFF.md`, then `PROJECT_CONTEXT.md`, then `HANDOFF_WEBAPP.md`. Skip `docs/archive/`. Never open big files whole: grep for what you need and view small ranges.

DO:
1. `pip install -r requirements.txt && pytest -q`. Put the pass count in HANDOFF.md "Log".
2. Take the first task in the HANDOFF.md or HANDOFF_WEBAPP.md table that is not ✅.
3. Implement it exactly per its spec, add/adjust tests, run the full suite, fix regressions.
4. Tick the table, add a 1-line Log entry (what changed, gotchas), commit `feat: <n> <title>`.
5. Repeat. When ~15% of your budget remains: stop starting new tasks, leave tests green, and make HANDOFF.md accurate (done / half-done / gotchas) for the next AI.

RULES: never print or commit secrets (`.env`, key JSONs, ssh keys) or student names; preserve each file's line endings; tests monkeypatch module attributes, so retarget patches when moving code; webapp defaults new uploads to dry run, keep dry-run semantics. Don't refactor beyond the task.
Stop at ~15% budget left: note any half-done work precisely and output only the changed files you did.

OUTPUT: a completely full new zip with the updates you made.
