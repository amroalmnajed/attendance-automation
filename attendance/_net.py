"""attendance/_net.py — shared transient-error classification and retry helper.

Used by ocr.py (Gemini), writer.py and session_resolver.py (gspread) so all
network calls share one definition of "worth retrying" and one backoff policy.
"""
from __future__ import annotations

import random
import time
from typing import Any, Callable, Optional, TypeVar

from core.logger import get_logger

log = get_logger("net")

T = TypeVar("T")

TRANSIENT_STATUS = frozenset({408, 500, 502, 503, 504})
QUOTA_STATUS = 429
# Matched by class name so we don't hard-depend on requests/httpx/google-auth.
_NETWORK_EXC_NAMES = frozenset({
    "Timeout", "ReadTimeout", "ConnectTimeout", "ConnectionError", "ChunkedEncodingError",
    "TimeoutException", "TransportError", "ConnectError", "ReadError", "RemoteProtocolError",
})
_STATUS_TOKENS = {
    "RESOURCE_EXHAUSTED": 429, "UNAVAILABLE": 503, "DEADLINE_EXCEEDED": 504,
    "INTERNAL": 500, "NOT_FOUND": 404,
}


def http_status(exc: BaseException) -> Optional[int]:
    """Best-effort HTTP status from gspread APIError / google-genai errors."""
    for obj, attr in ((getattr(exc, "response", None), "status_code"),
                      (exc, "code"), (exc, "status_code")):
        val = getattr(obj, attr, None) if obj is not None else None
        if isinstance(val, int) and 100 <= val < 600:
            return val
    msg = str(exc)
    for token, code in _STATUS_TOKENS.items():
        if token in msg:
            return code
    for code in (429, 503, 504, 500, 404):
        if str(code) in msg:
            return code
    return None


def is_network_error(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    return any(c.__name__ in _NETWORK_EXC_NAMES for c in type(exc).__mro__)


def is_transient(exc: BaseException, *, include_quota: bool = True) -> bool:
    """True if retrying the same call may succeed."""
    if is_network_error(exc):
        return True
    status = http_status(exc)
    if status == QUOTA_STATUS:
        return include_quota
    return status in TRANSIENT_STATUS


def retry_call(
    fn: Callable[[], T],
    *,
    op: str,
    max_attempts: int = 6,
    base_s: float = 1.0,
    cap_s: float = 32.0,
    jitter: bool = True,
    include_quota: bool = True,
    context: Optional[dict[str, Any]] = None,
) -> T:
    """Call fn(), retrying transient errors with exponential backoff (+ jitter).

    Non-transient errors, and the final transient error, are re-raised unchanged.
    """
    max_attempts = max(1, int(max_attempts))
    ctx = dict(context or {})
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as exc:
            transient = is_transient(exc, include_quota=include_quota)
            if not transient or attempt >= max_attempts:
                log.error(
                    "net_call_failed",
                    extra={**ctx, "op": op, "attempt": attempt, "transient": transient,
                           "status": http_status(exc), "error": str(exc)[:200]},
                )
                raise
            delay = min(base_s * (2 ** (attempt - 1)), cap_s)
            if jitter:
                delay *= 0.5 + random.random() * 0.5
            log.warning(
                "net_call_retry",
                extra={**ctx, "op": op, "attempt": attempt, "status": http_status(exc),
                       "delay_s": round(delay, 2), "error": str(exc)[:200]},
            )
            time.sleep(delay)
    raise RuntimeError(f"{op}: exhausted retries")  # unreachable
