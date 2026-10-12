"""Health and quota alerts (Feature 13).

* ``record(kind)`` is called from the existing failure paths (Gemini fallback, Sheets write failure). It is a
  one-liner that can NEVER raise and never waits on the network: alerts go out from a background thread.
* It does nothing until ``configure(cfg)`` ran (bot start / pipeline start), so unit tests and scripts that never
  configured it cannot touch real state files.
* Dry-run uploads record nothing (``dry_run_scope``).
* State is tiny and has NO names or error bodies: ``<review_dir>/_health/state.json`` =
  rolling event timestamps per kind + last-alert time per alert group.
* Alerts go to admins only (ADMIN_TELEGRAM_IDS / attendance.admin.telegram_ids) through ``core.notifier.send_text``,
  at most once per ``cooldown_minutes`` per alert group.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from attendance import roles
from attendance._fslock import atomic_write_json, file_lock
from core import notifier
from core.logger import get_logger

log = get_logger("health")

EVENT_KINDS = ("gemini_429", "gemini_error", "sheets_write_error")
GROUP_OF = {"gemini_429": "gemini", "gemini_error": "gemini", "sheets_write_error": "sheets"}
GROUPS = ("gemini", "sheets", "review_backlog")
KEEP_PER_KIND = 200

_THREADED = True                     # tests switch this off to send alerts inline
_dry = contextvars.ContextVar("health_dry_run", default=False)


@dataclass
class _Ctx:
    cfg: dict
    review_dir: str
    s: dict


_ctx: Optional[_Ctx] = None


# ─── Settings / configuration ─────────────────────────────────────────────────

def settings(cfg: dict) -> dict:
    sect = (((cfg or {}).get("attendance") or {}).get("health") or {})
    sect = sect if isinstance(sect, dict) else {}

    def _num(key, default, lo=1):
        try:
            return max(lo, int(sect.get(key, default)))
        except (TypeError, ValueError):
            return default
    return {
        "enabled": sect.get("enabled", True) is not False,
        "window_minutes": _num("window_minutes", 60),
        "gemini_threshold": _num("gemini_threshold", 5),
        "sheets_threshold": _num("sheets_threshold", 3),
        "review_max_age_days": _num("review_max_age_days", 3),
        "cooldown_minutes": _num("cooldown_minutes", 120),
    }


def review_dir_of(cfg: dict) -> str:
    return str((((cfg or {}).get("attendance") or {}).get("review") or {}).get("local_dir") or "review_queue")


def configure(cfg: dict) -> None:
    """Remember where to keep state and which thresholds apply. Never raises."""
    global _ctx
    try:
        _ctx = _Ctx(cfg or {}, review_dir_of(cfg), settings(cfg))
    except Exception:
        _ctx = None


def reset() -> None:
    """Forget the configuration (tests)."""
    global _ctx
    _ctx = None


@contextlib.contextmanager
def dry_run_scope(dry_run: bool):
    """Inside this scope ``record`` does nothing when ``dry_run`` is true."""
    token = _dry.set(bool(dry_run))
    try:
        yield
    finally:
        _dry.reset(token)


# ─── State file ───────────────────────────────────────────────────────────────

def _state_path(review_dir: str) -> Path:
    return Path(review_dir) / "_health" / "state.json"


def _empty() -> dict:
    return {"events": {k: [] for k in EVENT_KINDS}, "last_alert": {}}


def load_state(review_dir: str) -> dict:
    """Read the state; a missing or broken file simply means 'nothing recorded'."""
    try:
        data = json.loads(_state_path(review_dir).read_text(encoding="utf-8"))
        ev = data.get("events") if isinstance(data, dict) else None
        la = data.get("last_alert") if isinstance(data, dict) else None
        out = _empty()
        if isinstance(ev, dict):
            for k in EVENT_KINDS:
                out["events"][k] = [float(t) for t in ev.get(k, []) if isinstance(t, (int, float))]
        if isinstance(la, dict):
            out["last_alert"] = {k: float(v) for k, v in la.items() if isinstance(v, (int, float))}
        return out
    except (OSError, ValueError, TypeError, AttributeError):
        return _empty()


def _count(state: dict, group: str, now: float, window_s: float) -> int:
    return sum(1 for k in EVENT_KINDS if GROUP_OF[k] == group
               for t in state["events"].get(k, []) if now - t <= window_s)


def _prune(state: dict, now: float, keep_s: float) -> None:
    for k in EVENT_KINDS:
        state["events"][k] = [t for t in state["events"].get(k, []) if now - t <= keep_s][-KEEP_PER_KIND:]


def _cooldown_ok(state: dict, group: str, now: float, s: dict) -> bool:
    return now - state["last_alert"].get(group, 0.0) >= s["cooldown_minutes"] * 60


# ─── Alerts ───────────────────────────────────────────────────────────────────

def _text(group: str, n: int, s: dict, extra: str = "") -> str:
    if group == "gemini":
        return (f"⚠️ Gemini (the sheet-reading service) had {n} failed or rate-limited calls in the last "
                f"{s['window_minutes']} minutes. Uploads may fail or be slow. If it keeps happening, "
                f"check the Gemini quota or try again later.")
    if group == "sheets":
        return (f"⚠️ Google Sheets writes failed {n} times in the last {s['window_minutes']} minutes. "
                f"Attendance may not be saved. Check the sheet sharing / quota, then re-upload.")
    return extra


def _dispatch(text: str, ids: list[int]) -> None:
    def _go():
        try:
            notifier.send_text(text, ids)
        except Exception:
            pass
    if _THREADED:
        threading.Thread(target=_go, daemon=True).start()
    else:
        _go()


def _admins(cfg: dict) -> list[int]:
    return sorted(roles.admin_ids(cfg))


def record(kind: str, now: Optional[float] = None) -> None:
    """Note one failure event and alert admins if the threshold is crossed. NEVER raises."""
    try:
        ctx = _ctx
        if ctx is None or not ctx.s["enabled"] or _dry.get() or kind not in EVENT_KINDS:
            return
        now = time.time() if now is None else now
        s, path = ctx.s, _state_path(ctx.review_dir)
        group = GROUP_OF[kind]
        threshold = s["gemini_threshold"] if group == "gemini" else s["sheets_threshold"]
        send = None
        path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(path):
            state = load_state(ctx.review_dir)
            state["events"][kind].append(now)
            _prune(state, now, max(s["window_minutes"] * 60, 3600) * 2)
            n = _count(state, group, now, s["window_minutes"] * 60)
            ids = _admins(ctx.cfg)
            if n >= threshold and ids and _cooldown_ok(state, group, now, s):
                state["last_alert"][group] = now          # mark first: one alert per cooldown
                send = (_text(group, n, s), ids)
            atomic_write_json(path, state)
        if send:
            _dispatch(*send)
    except Exception:
        return


def record_exc(seen: set, exc: BaseException) -> None:
    """Gemini failure helper: at most one event of each kind per OCR read. NEVER raises."""
    try:
        from attendance._net import http_status
        kind = "gemini_429" if http_status(exc) == 429 else "gemini_error"
        if kind in seen:
            return
        seen.add(kind)
        record(kind)
    except Exception:
        return


# ─── Review queue age ─────────────────────────────────────────────────────────

def review_backlog(review_dir: str, now: Optional[datetime] = None) -> tuple[int, Optional[float]]:
    """(pending item count, age in days of the oldest upload that still has pending items)."""
    now = now or datetime.now(timezone.utc)
    count, oldest = 0, None
    try:
        from attendance.review_queue import ReviewQueue
        rq = ReviewQueue(store_dir=review_dir)
        upload_ids = rq._store.list_all_upload_ids()
    except Exception:
        return 0, None
    for upload_id in upload_ids:
        try:
            data = rq._load(upload_id)
            pending = sum(1 for i in data.get("items", []) if isinstance(i, dict) and i.get("status") == "pending")
            if not pending:
                continue
            created = datetime.fromisoformat(str(data.get("created_at")))
            created = created if created.tzinfo else created.replace(tzinfo=timezone.utc)
            age = (now - created).total_seconds() / 86400.0
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        count += pending
        oldest = age if oldest is None else max(oldest, age)
    return count, oldest


def check_review_age(cfg: dict, now: Optional[datetime] = None, send: Optional[Callable] = None) -> bool:
    """Alert admins if review items waited too long. True if an alert was sent. Never raises."""
    try:
        s = settings(cfg)
        if not s["enabled"]:
            return False
        rd = review_dir_of(cfg)
        now = now or datetime.now(timezone.utc)
        count, oldest = review_backlog(rd, now)
        ids = _admins(cfg)
        if not count or oldest is None or oldest < s["review_max_age_days"] or not ids:
            return False
        path = _state_path(rd)
        path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(path):
            state = load_state(rd)
            t = now.timestamp()
            if not _cooldown_ok(state, "review_backlog", t, s):
                return False
            state["last_alert"]["review_backlog"] = t
            atomic_write_json(path, state)
        text = (f"⏳ {count} review item(s) have been waiting for a decision; the oldest for "
                f"{int(oldest)} day(s) (limit {s['review_max_age_days']}). Please open /pending.")
        (send or notifier.send_text)(text, ids)
        return True
    except Exception:
        return False


# ─── /health report ───────────────────────────────────────────────────────────

def _ago(ts: Optional[float], tz_now: float) -> str:
    if not ts:
        return "never"
    mins = int((tz_now - ts) // 60)
    return f"{mins} min ago" if mins < 120 else f"{mins // 60} h ago"


def status_text(cfg: dict, scheduler: Optional[bool] = None, now: Optional[datetime] = None) -> str:
    """Plain-language status for the admin /health command. Counts only; no names."""
    from attendance import reminders as reminders_mod
    s, rd = settings(cfg), review_dir_of(cfg)
    now = now or datetime.now(timezone.utc)
    t = now.timestamp()
    state = load_state(rd)
    w = s["window_minutes"] * 60
    g, sh = _count(state, "gemini", t, w), _count(state, "sheets", t, w)
    count, oldest = review_backlog(rd, now)
    la = state["last_alert"]
    rem_on = reminders_mod.settings(cfg)["enabled"]
    if not rem_on:
        rem = "off"
    elif scheduler is False:
        rem = "ON, but the scheduler package is missing, so they cannot run"
    else:
        rem = "on"
    lines = [
        "🩺 System status" + ("" if s["enabled"] else " (alerts are switched off)"),
        f"• Gemini: {g} failed/rate-limited call(s) in the last {s['window_minutes']} min (alert at {s['gemini_threshold']}).",
        f"• Google Sheets: {sh} failed write(s) in the last {s['window_minutes']} min (alert at {s['sheets_threshold']}).",
        ("• Review queue: nothing waiting." if not count else
         f"• Review queue: {count} item(s) waiting, oldest {int(oldest or 0)} day(s) (alert after {s['review_max_age_days']})."),
        f"• Last alerts: Gemini {_ago(la.get('gemini'), t)}; Sheets {_ago(la.get('sheets'), t)}; "
        f"review {_ago(la.get('review_backlog'), t)}.",
        f"• Reminders: {rem}.",
    ]
    return "\n".join(lines)


# ─── Scheduler wiring (optional; the bot starts fine without it) ──────────────

def install(app, load_cfg: Callable[[], dict]) -> bool:
    """Run the review-age check every 30 minutes if the job queue exists."""
    try:
        if not settings(load_cfg())["enabled"]:
            return False
    except Exception:
        return False
    jq = getattr(app, "job_queue", None)
    if jq is None:
        log.warning("health_scheduler_unavailable",
                    extra={"reason": 'job-queue extra missing; /health still works, review-age alerts do not'})
        return False

    async def _job(context) -> None:
        try:
            import asyncio
            cfg = load_cfg()
            await asyncio.get_running_loop().run_in_executor(None, check_review_age, cfg)
        except Exception as exc:
            log.error("health_job_failed", extra={"error": type(exc).__name__})

    jq.run_repeating(_job, interval=1800, first=120, name="attendance_health")
    return True
