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

## Item 5 — roles.py architectural decision (documented, code kept)

`attendance/roles.py` implements Telegram-ID-based RBAC, but it is NOT dead code:
it is imported by live paths — `attendance/row_processor.py`, `attendance/writer.py`,
`attendance/resolver_ops.py` (all call `has_permission(...)` for `edit_attendance`),
`attendance/health.py` and `attendance/reminders.py` (`authorized_ids`),
`attendance/alias_admin.py` (wrapper), and `webapp/auth.py` (`allowlist_configured`,
`ROLE_PERMISSIONS`). Deleting it would break write-permission checks in the live
review-resolve and sheet-write paths.

Decision: **(b) keep roles.py as the single source of truth for permission→role
mapping**, shared by the webapp (`check_permission`) and any future bot. The
Telegram-ID *identifier* plumbing is legacy-shaped (uids are ints), but the
`ROLE_PERMISSIONS` table and `has_permission` signature are identifier-agnostic
enough (`user_identifier` params in writer/resolver_ops) to serve webapp users via
`rbac.webapp_users` name→role mapping. No code was deleted; the follow-up work is
Item 6 (default-deny in `webapp/auth.py`) which makes the webapp actually consult
this mapping instead of allowing everything.
