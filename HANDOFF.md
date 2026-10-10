# HANDOFF.md

## Objective
Add a `process-text` subcommand to the attendance-automation CLI. The feature allows the user to supply a plain-text file (one student name per line, already extracted from any source) instead of an attendance photo. The system interactively prompts for any missing parameters (course, session number, session type for g9/g10, and file path), then runs the full match → write pipeline normally.

---

## Completed Steps

### 1. `attendance/pipeline.py`
- Refactored `run()`: extracted all post-OCR logic into a new private function `_process_rows(...)`. The existing `run()` now handles OCR + preprocessing only, then delegates to `_process_rows`.
- Added `run_text(*, names, course, session_num, session_type, dry_run, force, config_path, review_store_dir)`:
  - Converts each name string into an `OcrRow(confidence=1.0, status_mark="Present")`.
  - Delegates straight to `_process_rows` — skipping OCR/image steps entirely.
  - Generates its `upload_id` from a SHA-256 of the names list + course + session.

### 2. `attendance/cli.py`
- Added `import os` to the imports block.
- Added `cmd_process_text(args)` handler (lines ~102–168):
  - Interactively prompts for: `course` (if not passed), `session` (if not passed), `session_type` (for g9/g10 only, if not passed), `text` file path (if not passed).
  - Validates the file exists; reads it line-by-line (blank lines skipped).
  - Calls `run_text(...)`, then prints the `RunSummary` and review prompt if needed.
- Registered `process-text` subparser in `main()` with arguments `--text`, `--course`, `--session`, `--type`, `--dry-run` / `--no-dry-run`, `--force`.
- Updated module docstring with new usage examples.

### 3. `PROJECT_CONTEXT.md` — Created at project root.

---

## Pending Steps
None. Feature is complete and syntax-verified.

---

## Technical Decisions & Caveats
- **`confidence=1.0` for text-input rows**: Since names come pre-extracted and are trusted, they bypass the OCR confidence gate and go straight to the fuzzy matcher. Any name that doesn't fuzzy-match at ≥90 score still goes to the review queue (same as image flow).
- **`status_mark="Present"` default**: The text input gives no attendance mark, so all names are treated as "Present" by default. The canonical gate normalises this. If you need to support mixed marks in the text file (e.g. `Name | P`), that's a future enhancement.
- **Session type prompt is g9/g10 only**: `crash` and `main` are `single`-layout courses; they never need a type column.
- **Dry-run defaults to `True`**: Consistent with the `process` command — safe by default.

---

## Resume Directive

```text
SYSTEM DIRECTIVE FOR INCOMING AI:
1. Read `PROJECT_CONTEXT.md` and `HANDOFF.md` completely.
2. Run `git status` or inspect the working tree to verify the current state.
3. Feature is COMPLETE. No remaining steps.
   If further work is requested, start from Step 1 of the new task.
```
