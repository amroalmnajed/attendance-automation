"""Feature 12 - missing-attendance reminders. One rule per test; no Telegram, no scheduler."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytz

from attendance import history as history_mod
from attendance import reminders as rem

CAIRO = pytz.timezone("Africa/Cairo")
MON_END = "Mon 2026-10-12 18:00"          # 2026-10-12 is a Monday


def cfg_for(tmp_path, **over):
    r = {"enabled": True, "grace_minutes": 60, "recipients": "both",
         "schedule": {"crash": [{"day": "mon", "time": "18:00"}]}}
    r.update(over)
    return {"core": {"app": {"timezone": "Africa/Cairo"}},
            "attendance": {"review": {"local_dir": str(tmp_path / "rq")}, "reminders": r,
                           "authorized": {"telegram_ids": [1, 2]}}}


def at(y, mo, d, h, mi):
    return CAIRO.localize(datetime(y, mo, d, h, mi))


def no_history(_course):
    return []


def test_disabled_never_reminds(tmp_path):
    c = cfg_for(tmp_path, enabled=False)
    assert rem.due_reminders(at(2026, 10, 12, 19, 30), c, no_history) == []


def test_enabled_must_be_a_real_true(tmp_path):
    assert rem.due_reminders(at(2026, 10, 12, 19, 30), cfg_for(tmp_path, enabled="yes"), no_history) == []


def test_default_config_is_off():
    assert rem.settings({})["enabled"] is False


def test_due_after_grace_but_not_before(tmp_path):
    c = cfg_for(tmp_path)
    assert rem.due_reminders(at(2026, 10, 12, 18, 30), c, no_history) == []
    assert rem.due_reminders(at(2026, 10, 12, 19, 0), c, no_history) == [("crash", MON_END)]


def test_other_weekday_and_expired_window_are_quiet(tmp_path):
    c = cfg_for(tmp_path)
    assert rem.due_reminders(at(2026, 10, 13, 19, 30), c, no_history) == []     # Tuesday
    assert rem.due_reminders(at(2026, 10, 13, 7, 0), c, no_history) == []       # 13h later: gave up


def test_late_class_is_due_after_midnight(tmp_path):
    c = cfg_for(tmp_path, schedule={"crash": [{"day": "mon", "time": "23:30"}]})
    assert rem.due_reminders(at(2026, 10, 13, 0, 45), c, no_history) == [("crash", "Mon 2026-10-12 23:30")]


def _entry(ts_utc):
    return {"session": "1", "ts": ts_utc.isoformat(), "counts": {}}


def test_upload_on_the_class_date_suppresses(tmp_path):
    c = cfg_for(tmp_path)
    up = at(2026, 10, 12, 17, 0).astimezone(timezone.utc)
    assert rem.due_reminders(at(2026, 10, 12, 19, 30), c, lambda _c: [_entry(up)]) == []


def test_upload_after_midnight_still_counts(tmp_path):
    c = cfg_for(tmp_path, schedule={"crash": [{"day": "mon", "time": "23:30"}]})
    up = at(2026, 10, 13, 0, 10).astimezone(timezone.utc)
    assert rem.due_reminders(at(2026, 10, 13, 0, 45), c, lambda _c: [_entry(up)]) == []


def test_last_weeks_upload_does_not_suppress(tmp_path):
    c = cfg_for(tmp_path)
    old = at(2026, 10, 5, 19, 0).astimezone(timezone.utc)
    assert rem.due_reminders(at(2026, 10, 12, 19, 30), c, lambda _c: [_entry(old)]) == [("crash", MON_END)]


def test_other_course_upload_does_not_suppress(tmp_path):
    c = cfg_for(tmp_path)
    up = at(2026, 10, 12, 17, 0).astimezone(timezone.utc)
    reader = lambda course: [_entry(up)] if course == "main" else []     # noqa: E731
    assert rem.due_reminders(at(2026, 10, 12, 19, 30), c, reader) == [("crash", MON_END)]


def test_unreadable_history_means_no_false_alarm(tmp_path):
    def boom(_c):
        raise OSError("disk")
    assert rem.due_reminders(at(2026, 10, 12, 19, 30), cfg_for(tmp_path), boom) == []


def test_junk_schedule_entries_are_skipped(tmp_path):
    c = cfg_for(tmp_path, schedule={"crash": [{"day": "funday", "time": "18:00"}, {"day": "mon"},
                                              {"day": "mon", "time": "99:99"}, "x"], "g9": "nope"})
    assert rem.due_reminders(at(2026, 10, 12, 19, 30), c, no_history) == []


def test_message_has_course_and_time_only():
    text = rem.message("crash", MON_END)
    assert "crash" in text and "2026-10-12" in text and "18:00" in text
    assert "|" not in text


def test_recipients_follow_the_setting(tmp_path):
    assert rem.recipients(cfg_for(tmp_path, recipients="admin"), {}) == [1, 2]
    assert rem.recipients(cfg_for(tmp_path, recipients="teacher"), {}) == [1, 2]
    assert rem.recipients(cfg_for(tmp_path, recipients="both"), {}) == [1, 2]
    assert rem.recipients(cfg_for(tmp_path, recipients="nonsense"), {}) == [1, 2]


# ─── run_check: sends once, remembers across restarts ─────────────────────────

class Sender:
    def __init__(self, ok=True):
        self.calls, self.ok = [], ok

    def __call__(self, text, ids):
        self.calls.append((text, list(ids)))
        return len(ids) if self.ok else 0


def test_sent_once_and_remembered_after_restart(tmp_path):
    c, s, now = cfg_for(tmp_path), Sender(), at(2026, 10, 12, 19, 30)
    assert rem.run_check(c, now, s, {}) == [("crash", MON_END)]
    assert s.calls[0][1] == [1, 2]
    rem._warned_no_recipients = False                       # a "restart": only the file remembers
    assert rem.run_check(c, now + timedelta(minutes=10), s, {}) == []
    assert len(s.calls) == 1


def test_failed_send_is_retried_not_forgotten(tmp_path):
    c, now = cfg_for(tmp_path), at(2026, 10, 12, 19, 30)
    assert rem.run_check(c, now, Sender(ok=False), {}) == []
    good = Sender()
    assert rem.run_check(c, now + timedelta(minutes=10), good, {}) == [("crash", MON_END)]


def test_no_recipients_sends_nothing_and_does_not_crash(tmp_path):
    c = cfg_for(tmp_path)
    c["attendance"]["authorized"] = {}
    s = Sender()
    assert rem.run_check(c, at(2026, 10, 12, 19, 30), s, {}) == [] and not s.calls


def test_existing_history_file_suppresses_in_run_check(tmp_path):
    c = cfg_for(tmp_path)
    path = history_mod._path(c["attendance"]["review"]["local_dir"], "crash")
    path.parent.mkdir(parents=True)
    up = at(2026, 10, 12, 17, 0).astimezone(timezone.utc)
    path.write_text(json.dumps([_entry(up)]), encoding="utf-8")
    s = Sender()
    assert rem.run_check(c, at(2026, 10, 12, 19, 30), s, {}) == [] and not s.calls


# ─── Wiring: the bot must start without the scheduler package ─────────────────

def test_install_without_job_queue_returns_false_and_does_not_raise(tmp_path):
    assert rem.install(SimpleNamespace(job_queue=None), lambda: cfg_for(tmp_path)) is False


def test_install_registers_one_repeating_job_when_enabled(tmp_path):
    calls = []
    jq = SimpleNamespace(run_repeating=lambda *a, **k: calls.append((a, k)))
    assert rem.install(SimpleNamespace(job_queue=jq), lambda: cfg_for(tmp_path)) is True
    assert len(calls) == 1 and calls[0][1]["interval"] == 600


def test_install_does_nothing_when_disabled(tmp_path):
    calls = []
    jq = SimpleNamespace(run_repeating=lambda *a, **k: calls.append(1))
    assert rem.install(SimpleNamespace(job_queue=jq), lambda: cfg_for(tmp_path, enabled=False)) is False
    assert not calls


def test_install_survives_broken_config():
    def boom():
        raise RuntimeError("x")
    assert rem.install(SimpleNamespace(job_queue=SimpleNamespace()), boom) is False
