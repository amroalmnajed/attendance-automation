"""Tests for T11: Tenacity retry + exponential backoff for Gemini and Sheets calls."""
from __future__ import annotations

import pytest
from tenacity import RetryError

from attendance import _net, ocr


class _MockApiError(Exception):
    def __init__(self, code: int):
        super().__init__(f"HTTP {code}")
        self.code = code


def test_tenacity_retry_recovers():
    calls = {"n": 0}

    @_net.make_tenacity_retry(max_attempts=4, base_s=0.01)
    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("slow")
        return "success"

    assert flaky() == "success"
    assert calls["n"] == 3


def test_tenacity_retry_does_not_retry_permanent_error():
    calls = {"n": 0}

    @_net.make_tenacity_retry(max_attempts=4, base_s=0.01)
    def fatal():
        calls["n"] += 1
        raise _MockApiError(403)

    with pytest.raises(_MockApiError):
        fatal()
    assert calls["n"] == 1


def test_tenacity_retry_exhaustion_reraises():
    calls = {"n": 0}

    @_net.make_tenacity_retry(max_attempts=3, base_s=0.01)
    def always_fail():
        calls["n"] += 1
        raise _MockApiError(503)

    with pytest.raises(_MockApiError):
        always_fail()
    assert calls["n"] == 3


def test_retry_call_uses_tenacity_backoff():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _MockApiError(500)
        return "ok"

    res = _net.retry_call(fn, op="test_op", max_attempts=5, base_s=0.01, cap_s=0.05)
    assert res == "ok"
    assert calls["n"] == 3


def test_retry_call_quota_policy():
    # include_quota=False: 429 is not transient
    calls = {"n": 0}

    def fn_quota():
        calls["n"] += 1
        raise _MockApiError(429)

    with pytest.raises(_MockApiError):
        _net.retry_call(fn_quota, op="gemini_op", max_attempts=5, include_quota=False)
    assert calls["n"] == 1

    # include_quota=True: 429 is transient (e.g. for sheets)
    calls["n"] = 0

    def fn_quota_retry():
        calls["n"] += 1
        if calls["n"] < 2:
            raise _MockApiError(429)
        return "recovered"

    assert _net.retry_call(fn_quota_retry, op="sheets_op", max_attempts=3, base_s=0.01, include_quota=True) == "recovered"
    assert calls["n"] == 2
