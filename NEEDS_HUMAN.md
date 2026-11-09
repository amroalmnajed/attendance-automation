core.app.env is 'dev' while dry_run_default is false: human must decide env value.
requirements.lock was not regenerated after removing sentence-transformers/torch/scikit-learn (pip-compile unavailable); run `pip-compile --output-file=requirements.lock requirements.txt` to refresh it.
roles.py is still keyed on Telegram IDs; webapp auth treats everyone as allowed. Needs a redesign (Part 2).

## Phase 6 — dead modules audit

| module | imported by (non-test files) | verdict |
|---|---|---|
| attendance/dead_letter.py | attendance/writer.py | used |
| attendance/session_patterns.py | webapp/pages/review.py, webapp/pages/upload.py (attendance/sync.py also imported it, but sync.py was deleted in this phase) | used |
| attendance/name_patterns.py | attendance/matcher.py | used |
| attendance/suggestions.py | webapp/pages/review.py | used |
| attendance/ocr_corrections.py | attendance/ocr.py, attendance/resolver_ops.py | used |

Deleted in this phase (only referenced by their own file, tests, and docs): attendance/sync.py, attendance/migrate.py.

## Phase 7 — ruff

Added flake8-bugbear (`B`) and E722 to ruff select. 30 new violations exceeded the 25-error fix budget, so these rule codes are ignored in pyproject.toml pending Part 2 cleanup:

- B007 unused-loop-control-variable (8)
- B017 assert-raises-exception (9)
- B023 function-uses-loop-variable (1)
- B904 raise-without-from-inside-except (6)
- B905 zip-without-explicit-strict (6)
