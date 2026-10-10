"""Phase 2 — network / external API failure handling (gspread + Gemini)."""
import pytest

from attendance import _net, ocr, writer
from attendance import session_resolver as sr
from attendance.writer import WriteCell, batch_write


class _Resp:
    def __init__(self, status):
        self.status_code = status
        self.text = "err"

    def json(self):
        return {"error": {"code": self.status_code, "message": "boom", "status": "X"}}


def _api_error(status):
    from gspread.exceptions import APIError
    return APIError(_Resp(status))


class _GenaiError(Exception):
    def __init__(self, code, status):
        super().__init__(f"{code} {status}.")
        self.code = code


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(_net.time, "sleep", lambda s: None)


# ─── _net ─────────────────────────────────────────────────────────────────────

def test_is_transient_classification():
    assert _net.is_transient(_api_error(429))
    assert _net.is_transient(_api_error(503))
    assert not _net.is_transient(_api_error(403))
    assert not _net.is_transient(_api_error(404))
    assert _net.is_transient(TimeoutError("t"))
    assert _net.is_transient(ConnectionResetError("reset"))
    assert not _net.is_transient(ValueError("bad"))
    assert not _net.is_transient(_api_error(429), include_quota=False)


def test_retry_call_recovers_then_succeeds():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("slow")
        return "ok"

    assert _net.retry_call(fn, op="t", max_attempts=5) == "ok"
    assert calls["n"] == 3


def test_retry_call_does_not_retry_permanent_error():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise _api_error(403)

    with pytest.raises(Exception):
        _net.retry_call(fn, op="t", max_attempts=5)
    assert calls["n"] == 1


def test_retry_call_gives_up_after_max_attempts():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise _api_error(503)

    with pytest.raises(Exception):
        _net.retry_call(fn, op="t", max_attempts=3)
    assert calls["n"] == 3


# ─── writer (gspread) ─────────────────────────────────────────────────────────

class _WS:
    def __init__(self, failures):
        self.failures = list(failures)
        self.calls = 0

    def batch_update(self, updates, value_input_option=None):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)


class _Session:
    course, sheet_id, tab = "g10", "sid", "tab"

    def __init__(self, ws, open_failures=()):
        self.ws = ws
        self.open_failures = list(open_failures)

    def get_worksheet(self):
        if self.open_failures:
            raise self.open_failures.pop(0)
        return self.ws


def _cells():
    return [WriteCell("Ann", 5, "E", None, "Present")]


def test_batch_write_retries_timeout_and_quota(monkeypatch):
    monkeypatch.setattr(writer, "audit_write", lambda **kw: None)
    ws = _WS([TimeoutError("read timed out"), _api_error(429)])
    res = batch_write(_Session(ws), _cells(), upload_id="u1", dry_run=False)
    assert ws.calls == 3 and len(res.written) == 1


def test_batch_write_retries_worksheet_open(monkeypatch):
    monkeypatch.setattr(writer, "audit_write", lambda **kw: None)
    ws = _WS([])
    res = batch_write(_Session(ws, [ConnectionError("dns")]), _cells(), upload_id="u1", dry_run=False)
    assert ws.calls == 1 and len(res.written) == 1


def test_batch_write_exhausted_raises_and_skips_audit(monkeypatch):
    audited = []
    monkeypatch.setattr(writer, "audit_write", lambda **kw: audited.append(kw))
    ws = _WS([_api_error(503)] * 10)
    with pytest.raises(Exception):
        batch_write(_Session(ws), _cells(), upload_id="u1", dry_run=False,
                    retry_cfg={"max_attempts": 3})
    assert ws.calls == 3 and audited == []


def test_batch_write_permanent_error_not_retried(monkeypatch):
    monkeypatch.setattr(writer, "audit_write", lambda **kw: None)
    ws = _WS([_api_error(403)])
    with pytest.raises(Exception):
        batch_write(_Session(ws), _cells(), upload_id="u1", dry_run=False)
    assert ws.calls == 1


# ─── session_resolver (gspread) ───────────────────────────────────────────────

_CFG = {"attendance": {
    "match": {"skip_row_patterns": []},
    "write": {"retry": {"max_attempts": 3, "base_s": 0, "cap_s": 0}},
    "courses": {"g10": {
        "sheet_id_env": "SID", "tab": "T", "layout": "single", "header_row": 1,
        "first_student_row": 2, "session_header_pattern": r"^S(\d+)$",
    }},
}}


class _SheetWS:
    def __init__(self, failures):
        self.failures = list(failures)
        self.calls = 0

    def get_all_values(self):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return [["Name", "S1"], ["Ann", ""]]


class _SH:
    def __init__(self, ws, tab_exc=None):
        self.ws, self.tab_exc = ws, tab_exc

    def worksheet(self, tab):
        if self.tab_exc:
            raise self.tab_exc
        return self.ws


class _GC:
    def __init__(self, sh, open_failures=()):
        self.sh, self.open_failures = sh, list(open_failures)

    def open_by_key(self, key):
        if self.open_failures:
            raise self.open_failures.pop(0)
        return self.sh


def _patch_resolver(monkeypatch, gc):
    monkeypatch.setenv("SID", "sheet123")
    monkeypatch.setattr(sr, "get_credentials", lambda: object())
    monkeypatch.setattr(sr.gspread, "authorize", lambda creds: gc)


def test_resolve_retries_transient_open_and_read(monkeypatch):
    ws = _SheetWS([_api_error(503)])
    _patch_resolver(monkeypatch, _GC(_SH(ws), [TimeoutError("t")]))
    s = sr.resolve("g10", 1, None, _CFG)
    assert s.session_col == "B" and s.roster == [("Ann", 2)] and ws.calls == 2


def test_resolve_gives_up_after_retries(monkeypatch):
    ws = _SheetWS([_api_error(429)] * 10)
    _patch_resolver(monkeypatch, _GC(_SH(ws)))
    with pytest.raises(Exception):
        sr.resolve("g10", 1, None, _CFG)
    assert ws.calls == 3


def test_resolve_missing_tab_is_descriptive_not_retried(monkeypatch):
    from gspread.exceptions import WorksheetNotFound
    _patch_resolver(monkeypatch, _GC(_SH(None, WorksheetNotFound("T"))))
    with pytest.raises(sr.ConfigSheetDrift, match="T"):
        sr.resolve("g10", 1, None, _CFG)


# ─── ocr (Gemini) ─────────────────────────────────────────────────────────────

_GOOD = '[{"raw_name": "Ann", "status_mark": "P", "confidence": 0.9, "row_index": 1}]'


class _Models:
    def __init__(self, script):
        self.script, self.seen = list(script), []

    def generate_content(self, model, contents, config):
        self.seen.append(model)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return type("R", (), {"text": item})()


def _provider(script, **kw):
    p = ocr.GeminiProvider(model="m1", **kw)
    models = _Models(script)
    p._client = type("C", (), {"models": models})()
    return p, models


def test_gemini_retries_transient_on_same_model():
    p, m = _provider([_GenaiError(503, "UNAVAILABLE"), TimeoutError("t"), _GOOD])
    rows = p.read(b"\xff\xd8\xff")
    assert rows[0].raw_name == "Ann" and m.seen == ["m1", "m1", "m1"]


def test_gemini_quota_falls_back_to_next_model_without_same_model_retry():
    p, m = _provider([_GenaiError(429, "RESOURCE_EXHAUSTED"), _GOOD])
    rows = p.read(b"\xff\xd8\xff")
    assert rows and len(m.seen) == 2 and m.seen[0] == "m1" and m.seen[1] != "m1"


def test_gemini_permanent_error_raises_immediately():
    p, m = _provider([_GenaiError(400, "INVALID_ARGUMENT")])
    with pytest.raises(_GenaiError):
        p.read(b"\xff\xd8\xff")
    assert len(m.seen) == 1


def test_gemini_all_models_exhausted_raises_descriptive():
    p, m = _provider([_GenaiError(429, "RESOURCE_EXHAUSTED")] * 20)
    with pytest.raises(ocr.OcrUnavailableError, match="quota|unavailable|failed"):
        p.read(b"\xff\xd8\xff")


def test_gemini_empty_response_tries_next_model_then_raises():
    p, m = _provider([None] * 20)
    with pytest.raises(ocr.OcrUnavailableError):
        p.read(b"\xff\xd8\xff")


@pytest.mark.parametrize("raw", ['{"a": 1}', '[{"raw_name": "A", "confidence": "high"}]', "null"])
def test_parse_malformed_payload_does_not_crash(raw):
    rows = ocr._parse_gemini_response(raw)
    assert isinstance(rows, list) and rows
