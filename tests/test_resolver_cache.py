"""T4 - cached Sheets client + 60 s resolved-session cache."""
import pytest

from attendance import session_resolver as sr
from attendance.writer import WriteCell, batch_write

CFG = {"attendance": {
    "match": {"skip_row_patterns": []},
    "write": {"retry": {"max_attempts": 2, "base_s": 0, "cap_s": 0}},
    "courses": {"c": {
        "sheet_id_env": "SID", "tab": "T", "layout": "single", "header_row": 1,
        "first_student_row": 2, "session_header_pattern": r"^S(\d+)$",
    }},
}}


class _WS:
    def __init__(self, fail=None):
        self.reads, self.fail, self.updates = 0, fail, []

    def col_values(self, col_idx):
        return [""] * 100  # Empty column to allow reconciliation

    def get_all_values(self):
        self.reads += 1
        if self.fail:
            raise self.fail
        return [["Name", "S1", "S2"], ["Ann", "", ""], ["Bob", "", ""]]

    def batch_update(self, updates, value_input_option=None):
        self.updates.append(updates)


class _GC:
    def __init__(self, ws):
        self.ws = ws
        self.opens = 0
        gc = self

        class SH:
            def worksheet(self, tab):
                return gc.ws
        self.sh = SH()

    def open_by_key(self, key):
        self.opens += 1
        return self.sh


class _Env:
    pass


@pytest.fixture
def env(monkeypatch):
    e = _Env()
    e.ws = _WS()
    e.gc = _GC(e.ws)
    e.auth_calls = 0
    e.clock = [1000.0]
    monkeypatch.setenv("SID", "sheet123")

    def authorize(creds):
        e.auth_calls += 1
        return e.gc
    monkeypatch.setattr(sr, "get_credentials", lambda: object())
    monkeypatch.setattr(sr.gspread, "authorize", authorize)
    monkeypatch.setattr(sr, "_now", lambda: e.clock[0])
    return e


def test_second_resolve_is_served_from_cache(env):
    a = sr.resolve("c", 1, None, CFG)
    b = sr.resolve("c", 1, None, CFG)
    assert env.ws.reads == 1 and env.auth_calls == 1
    assert a.roster == b.roster == [("Ann", 2), ("Bob", 3)] and a.session_col == "B"


def test_cache_expires_after_ttl(env):
    sr.resolve("c", 1, None, CFG)
    env.clock[0] += sr.ROSTER_CACHE_TTL_S - 1
    sr.resolve("c", 1, None, CFG)
    assert env.ws.reads == 1
    env.clock[0] += 2
    sr.resolve("c", 1, None, CFG)
    assert env.ws.reads == 2 and env.auth_calls == 1          # client is reused across expiry


def test_different_session_is_a_different_key(env):
    sr.resolve("c", 1, None, CFG)
    s2 = sr.resolve("c", 2, None, CFG)
    assert env.ws.reads == 2 and s2.session_col == "C"


def test_callers_get_copies(env):
    a = sr.resolve("c", 1, None, CFG)
    a.roster.append(("Mallory", 99))
    assert sr.resolve("c", 1, None, CFG).roster == [("Ann", 2), ("Bob", 3)]


def test_invalidate_forces_a_reread_but_keeps_client(env):
    sr.resolve("c", 1, None, CFG)
    sr.invalidate()
    sr.resolve("c", 1, None, CFG)
    assert env.ws.reads == 2 and env.auth_calls == 1
    sr.invalidate(client=True)
    sr.resolve("c", 1, None, CFG)
    assert env.auth_calls == 2


def test_real_write_invalidates_but_dry_run_does_not(env):
    s = sr.resolve("c", 1, None, CFG)
    cell = WriteCell(roster_name="Ann", sheet_row=2, col="B", new_value="Present", old_value="")
    batch_write(s, [cell], upload_id="u1", dry_run=True, retry_cfg={})
    sr.resolve("c", 1, None, CFG)
    assert env.ws.reads == 1                                   # dry run: cache kept
    batch_write(s, [cell], upload_id="u2", dry_run=False, retry_cfg={"max_attempts": 1})
    assert env.ws.updates                                      # the write really happened
    sr.resolve("c", 1, None, CFG)
    assert env.ws.reads == 2                                   # real write: cache dropped


class _Auth(Exception):
    def __init__(self, code):
        super().__init__(f"HTTP {code}")
        self.code = code


@pytest.mark.parametrize("code,reset", [(401, True), (403, True), (500, False), (404, False)])
def test_client_reset_only_on_auth_errors(env, code, reset):
    env.ws.fail = _Auth(code)
    with pytest.raises(Exception):
        sr.resolve("c", 1, None, CFG)
    env.ws.fail = None
    sr.resolve("c", 1, None, CFG)
    assert env.auth_calls == (2 if reset else 1)


def test_failed_resolve_is_not_cached(env):
    env.ws.fail = _Auth(500)
    with pytest.raises(Exception):
        sr.resolve("c", 1, None, CFG)
    env.ws.fail = None
    assert sr.resolve("c", 1, None, CFG).roster
