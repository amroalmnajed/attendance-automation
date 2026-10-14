"""Missing-attendance reminders (Feature 12). OPT-IN: ``attendance.reminders.enabled`` is false by default.

``due_reminders`` is pure (no Telegram, no scheduler, no files): given "now", the config and a function
that returns a course's history entries, it says which (course, label) reminders are due. Everything else
here is thin wiring. A reminder says course + date/time only, never a student name.

Known limit: dry-run uploads are not recorded in history, so a deployment that only does dry runs
will keep getting reminded.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

import pytz

from attendance import history as history_mod
from attendance import roles
from attendance._fslock import atomic_write_json, file_lock
from core import notifier
from core.logger import get_logger

log = get_logger("reminders")

DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
KEEP_SENT = 300
_warned_no_recipients = False


def settings(cfg: dict) -> dict:
    sect = (((cfg or {}).get("attendance") or {}).get("reminders") or {})
    sect = sect if isinstance(sect, dict) else {}

    def _int(key, default, lo):
        try:
            return max(lo, int(sect.get(key, default)))
        except (TypeError, ValueError):
            return default
    rec = str(sect.get("recipients", "both")).strip().lower()
    return {
        "enabled": sect.get("enabled", False) is True,
        "grace_minutes": _int("grace_minutes", 60, 0),
        "window_hours": _int("window_hours", 12, 1),
        "check_every_minutes": _int("check_every_minutes", 10, 1),
        "recipients": rec if rec in ("admin", "teacher", "both") else "both",
        "schedule": sect.get("schedule") if isinstance(sect.get("schedule"), dict) else {},
    }


def _tz(cfg: dict):
    name = ((((cfg or {}).get("core") or {}).get("app") or {}).get("timezone")) or "UTC"
    try:
        return pytz.timezone(str(name))
    except Exception:
        log.warning("reminder_bad_timezone", extra={"timezone": str(name)})
        return pytz.UTC


def _slots(schedule: dict) -> list[tuple[str, int, int, int]]:
    """(course, weekday 0-6, hour, minute) for every valid schedule entry; junk is skipped."""
    out = []
    for course, entries in (schedule or {}).items():
        for e in entries if isinstance(entries, (list, tuple)) else []:
            try:
                wd = DAYS[str(e["day"]).strip().lower()[:3]]
                hh, mm = str(e["time"]).strip().split(":")
                hh, mm = int(hh), int(mm)
                if not (0 <= hh < 24 and 0 <= mm < 60):
                    continue
                out.append((str(course).strip().lower(), wd, hh, mm))
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
    return out


def reminder_key(course: str, label: str) -> str:
    return f"{course}|{label}"


def _parse_ts(value) -> Optional[datetime]:
    try:
        ts = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _submitted(entries: Iterable[dict], end, day, tz) -> bool:
    """Attendance exists if an upload landed on the class date, or any time after the class ended."""
    for e in entries or []:
        ts = _parse_ts(e.get("ts")) if isinstance(e, dict) else None
        if ts is None:
            continue
        local = ts.astimezone(tz)
        if local.date() == day or local >= end:
            return True
    return False


def due_reminders(now: datetime, cfg: dict, history_reader: Callable[[str], list],
                  sent: Iterable[str] = ()) -> list[tuple[str, str]]:
    """Reminders that are due at ``now``: [(course, label)], label like 'Mon 2026-10-12 18:00'."""
    rem = settings(cfg)
    if not rem["enabled"]:
        return []
    tz = _tz(cfg)
    local_now = tz.localize(now) if now.tzinfo is None else now.astimezone(tz)
    sent = set(sent or ())
    slots = _slots(rem["schedule"])
    due: list[tuple[str, str]] = []
    for back in (0, 1):                      # today and yesterday (a late class can be due after midnight)
        day = (local_now - timedelta(days=back)).date()
        for course, wd, hh, mm in slots:
            if day.weekday() != wd:
                continue
            end = tz.localize(datetime(day.year, day.month, day.day, hh, mm))
            if not (end + timedelta(minutes=rem["grace_minutes"]) <= local_now
                    < end + timedelta(hours=rem["window_hours"])):
                continue
            label = f"{day:%a} {day.isoformat()} {hh:02d}:{mm:02d}"
            if reminder_key(course, label) in sent:
                continue
            try:
                if _submitted(history_reader(course), end, day, tz):
                    continue
            except Exception as exc:         # can't tell -> don't raise a false alarm
                log.warning("reminder_history_unreadable", extra={"course": course, "error": type(exc).__name__})
                continue
            due.append((course, label))
    return sorted(set(due), key=lambda x: (x[1], x[0]))


def message(course: str, label: str) -> str:
    dow, date, hhmm = (label.split(" ") + ["", "", ""])[:3]
    return (f"⏰ Reminder: no attendance upload found yet for {course} — "
            f"class on {dow} {date} ended at {hhmm}. Please send the sheet photo or paste the names "
            f"when you can.")


# ─── Tiny "already reminded" store, so a restart never repeats a reminder ──────

def _review_dir(cfg: dict) -> str:
    return str((((cfg or {}).get("attendance") or {}).get("review") or {}).get("local_dir") or "review_queue")


def _sent_path(review_dir: str) -> Path:
    return Path(review_dir) / "_reminders" / "sent.json"


def load_sent(review_dir: str) -> set[str]:
    try:
        data = json.loads(_sent_path(review_dir).read_text(encoding="utf-8"))
        return {str(k) for k in data} if isinstance(data, list) else set()
    except (OSError, ValueError):
        return set()


def mark_sent(review_dir: str, keys: Iterable[str]) -> None:
    keys = list(keys)
    if not keys:
        return
    path = _sent_path(review_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path):
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            old = [str(k) for k in old] if isinstance(old, list) else []
        except (OSError, ValueError):
            old = []
        atomic_write_json(path, (old + [k for k in keys if k not in old])[-KEEP_SENT:])


def recipients(cfg: dict, env: Optional[dict] = None) -> list[int]:
    mode = settings(cfg)["recipients"]
    admins = roles.authorized_ids(cfg, env)
    # Note: teacher_ids no longer exists in the simplified roles module
    # Use authorized IDs for all modes
    chosen = admins
    return sorted(chosen)


def run_check(cfg: dict, now: Optional[datetime] = None, send: Callable = None,
              env: Optional[dict] = None) -> list[tuple[str, str]]:
    """One scheduler tick: send what is due, remember it. Returns what was delivered."""
    global _warned_no_recipients
    if not settings(cfg)["enabled"]:
        return []
    send = send or notifier.send_text
    rd = _review_dir(cfg)
    due = due_reminders(now or datetime.now(timezone.utc), cfg,
                        lambda c: history_mod.entries(rd, c), load_sent(rd))
    if not due:
        return []
    ids = recipients(cfg, env)
    if not ids:
        if not _warned_no_recipients:
            _warned_no_recipients = True
            log.warning("reminder_no_recipients",
                        extra={"hint": "set TEACHER_TELEGRAM_IDS / ADMIN_TELEGRAM_IDS"})
        return []
    delivered = []
    for course, label in due:
        if send(message(course, label), ids) > 0:
            delivered.append((course, label))
    mark_sent(rd, [reminder_key(c, l) for c, l in delivered])
    return delivered


def install(app, load_cfg: Callable[[], dict]) -> bool:
    """Register the repeating check on the bot's job queue. Never stops the bot from starting."""
    try:
        rem = settings(load_cfg())
    except Exception as exc:
        log.warning("reminders_config_unreadable", extra={"error": type(exc).__name__})
        return False
    if not rem["enabled"]:
        log.info("reminders_disabled")
        return False
    jq = getattr(app, "job_queue", None)
    if jq is None:
        log.warning("reminders_unavailable", extra={
            "reason": 'job-queue extra missing; run: pip install "python-telegram-bot[job-queue]"'})
        return False

    async def _job(context) -> None:
        try:
            cfg = load_cfg()
            await asyncio.get_running_loop().run_in_executor(None, run_check, cfg)
        except Exception as exc:
            log.error("reminder_job_failed", extra={"error": type(exc).__name__})

    jq.run_repeating(_job, interval=rem["check_every_minutes"] * 60, first=30, name="attendance_reminders")
    log.info("reminders_scheduled", extra={"every_minutes": rem["check_every_minutes"]})
    return True
