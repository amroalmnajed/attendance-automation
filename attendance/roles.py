"""Role-based bot access (Feature 11). Pure decision logic plus one `guard` decorator.

Single role configured by Telegram user ID (env var and/or config, merged):
  * authorized - env AUTHORIZED_TELEGRAM_IDS + attendance.authorized.telegram_ids

Levels a handler can require:
  * "authorized"   - authorized users only

If NO ID is configured anywhere, "authorized" stays open to everyone (the old behaviour).
Fail-safe: if the role check itself fails, the user gets a friendly refusal, never access.
No student/course data ever appears in a refusal; only the user's own Telegram ID.
"""
from __future__ import annotations

import functools
import os
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

AUTHORIZED_ENV = "AUTHORIZED_TELEGRAM_IDS"

AUTHORIZED = "authorized"


def _parse(raw: Iterable) -> set[int]:
    out: set[int] = set()
    for item in raw:
        try:
            out.add(int(str(item).strip()))
        except ValueError:
            continue
    return out


def _collect(cfg: Optional[dict], env: Optional[dict], env_name: str, section: str) -> set[int]:
    env = os.environ if env is None else env
    raw: list = str(env.get(env_name, "")).replace(";", ",").split(",")
    sect = (((cfg or {}).get("attendance") or {}).get(section) or {})
    ids = sect.get("telegram_ids") if isinstance(sect, dict) else None
    raw += list(ids) if isinstance(ids, (list, tuple)) else []
    return _parse(raw)


def authorized_ids(cfg: Optional[dict], env: Optional[dict] = None) -> set[int]:
    return _collect(cfg, env, AUTHORIZED_ENV, "authorized")


def allowlist_configured(cfg: Optional[dict], env: Optional[dict] = None) -> bool:
    return bool(authorized_ids(cfg, env))


def role_of(uid: Optional[int], cfg: Optional[dict], env: Optional[dict] = None) -> Optional[str]:
    if uid is None:
        return None
    if uid in authorized_ids(cfg, env):
        return AUTHORIZED
    return None


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str      # open | ok | unknown | error


def decide(uid: Optional[int], level: str, cfg: Optional[dict], env: Optional[dict] = None) -> Decision:
    authorized = authorized_ids(cfg, env)
    configured = bool(authorized)
    if not configured:
        return Decision(True, "open")          # nothing configured: today's behaviour
    role = AUTHORIZED if uid in authorized else None
    if role is None:
        return Decision(False, "unknown")
    return Decision(True, "ok")


def startup_warnings(cfg: Optional[dict], env: Optional[dict] = None) -> list[str]:
    """Plain-language warnings for the log at bot start."""
    authorized = authorized_ids(cfg, env)
    if not authorized:
        return ["No bot allow-list is configured: EVERYONE who finds the bot can use it. "
                f"Set {AUTHORIZED_ENV} and restart to restrict access."]
    return []


# ─── Refusal wording (no course/student data, only the user's own ID) ─────────

def refusal_text(d: Decision, level: str, uid: Optional[int]) -> str:
    if d.reason == "error":
        return "❌ Couldn't verify your access right now. Please try again in a moment."
    who = f"Your Telegram ID is {uid}" if uid is not None else "Your Telegram ID could not be read"
    return (f"🔒 Sorry, you're not authorised to use this bot yet.\n"
            f"{who}. Please send it to an admin so they can add you.")


def alert_text(d: Decision, level: str, uid: Optional[int]) -> str:
    """Short text for a callback-query alert (Telegram limit: 200 chars)."""
    if d.reason == "error":
        return "❌ Couldn't verify your access. Try again."
    return f"🔒 Not authorised. Your Telegram ID: {uid}" if uid is not None else "🔒 Not authorised."


async def _refuse(update, context, d: Decision, level: str, uid: Optional[int]) -> None:
    try:
        query = getattr(update, "callback_query", None)
        if query is not None:
            await query.answer(alert_text(d, level, uid), show_alert=True)   # stops the spinner
            return
        msg = getattr(update, "message", None) or getattr(update, "effective_message", None)
        if msg is not None:
            await msg.reply_text(refusal_text(d, level, uid))
            return
        chat = getattr(update, "effective_chat", None)
        if chat is not None and getattr(context, "bot", None) is not None:
            await context.bot.send_message(chat.id, refusal_text(d, level, uid))
    except Exception:
        pass   # a failed refusal message must never become an access grant or a crash


def guard(level: str, load_cfg: Callable[[], dict], get_log: Optional[Callable[[], object]] = None,
          deny_return=None):
    """Decorator for Telegram handlers. `load_cfg` / `get_log` are called at request time."""
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(update, context, *a, **k):
            uid = None
            try:
                uid = getattr(getattr(update, "effective_user", None), "id", None)
                d = decide(uid, level, load_cfg())
            except Exception as exc:
                d = Decision(False, "error")
                try:
                    if get_log:
                        get_log().error("role_check_failed", extra={"error": type(exc).__name__})
                except Exception:
                    pass
            if d.allowed:
                return await fn(update, context, *a, **k)
            await _refuse(update, context, d, level, uid)
            return deny_return
        wrapper.required_level = level   # lets tests prove every handler is guarded
        return wrapper
    return deco
