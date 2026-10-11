"""attendance/config_check.py — fail fast on config mistakes that only bite at runtime.

Called once from the bot's ``main()``. A typed_pair course without session types would
otherwise show an empty keyboard, which Telegram rejects with BadRequest mid-conversation.
"""
from __future__ import annotations


class ConfigError(ValueError):
    """The attendance config is unusable; the message says what to fix (no secrets)."""


def _types_ok(types) -> bool:
    """True if ``types`` is a mapping with at least one non-blank string in some bucket."""
    if not isinstance(types, dict) or not types:
        return False
    return any(
        isinstance(bucket, (list, tuple)) and any(isinstance(t, str) and t.strip() for t in bucket)
        for bucket in types.values()
    )


def config_problems(cfg: dict) -> list[str]:
    """Human-readable problems (empty list = fine). Never raises."""
    try:
        courses = ((cfg or {}).get("attendance") or {}).get("courses") or {}
    except AttributeError:
        return ["attendance.courses is missing or malformed"]
    if not isinstance(courses, dict):
        return ["attendance.courses must be a mapping of course -> settings"]
    problems: list[str] = []
    for name, course in courses.items():
        if not isinstance(course, dict):
            problems.append(f"course '{name}': settings must be a mapping")
            continue
        if course.get("layout") == "typed_pair" and not _types_ok(course.get("types")):
            problems.append(
                f"course '{name}': layout is typed_pair but 'types' is missing or empty "
                "(add at least one session type under attendance.courses.%s.types)" % name
            )
    return problems


def validate_cfg(cfg: dict) -> None:
    """Raise ConfigError listing every problem found; return None when the config is usable."""
    problems = config_problems(cfg)
    if problems:
        raise ConfigError("Invalid config/attendance.yaml: " + "; ".join(problems))
