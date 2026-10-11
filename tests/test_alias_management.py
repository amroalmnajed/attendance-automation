"""Feature 7 — learned-alias management: dates, flags, remove (+backup), CLI, admin-only bot command."""
from __future__ import annotations

import asyncio
import json
from datetime import date
from types import SimpleNamespace

import yaml

from attendance import alias_admin, cli, pipeline, writer
from attendance.aliases import AliasStore
from tests.test_absent import FakeWS, cfg, fake_session  # noqa: F401  (cfg is a fixture)
from attendance.bot import admin
from tests.botutil import patch_bot

TODAY = date(2026, 10, 11)


def _store(tmp_path, legacy=None, **kw):
    p = tmp_path / "aliases.json"
    if legacy is not None:
        p.write_text(json.dumps(legacy), encoding="utf-8")
    return AliasStore(path=str(p), **kw), p


def test_old_file_format_is_untouched_and_dates_go_to_a_sidecar(tmp_path):
    s, p = _store(tmp_path, {"g10": {"eyad abd latif": {"Eyad Ahmed Abdellatif": 1}}})
    assert s.lookup("g10", "Eyad Abd Latif") == "Eyad Ahmed Abdellatif"           # legacy data still works
    s.learn("g10", "Eyad Abd Latif", "Eyad Ahmed Abdellatif")
    assert json.loads(p.read_text(encoding="utf-8")) == {"g10": {"eyad abd latif": {"Eyad Ahmed Abdellatif": 2}}}
    meta = json.loads((tmp_path / "aliases.meta.json").read_text(encoding="utf-8"))
    assert meta["g10"]["eyad abd latif"]["last_confirmed"]


def test_last_used_is_stamped_only_after_flush(tmp_path):
    s, _ = _store(tmp_path, {"g10": {"sara n": {"Sara Nour": 1}}})
    s.lookup("g10", "Sara N")
    assert not (tmp_path / "aliases.meta.json").exists()
    s.flush_usage(today=TODAY)
    meta = json.loads((tmp_path / "aliases.meta.json").read_text(encoding="utf-8"))
    assert meta["g10"]["sara n"]["last_used"] == "2026-10-11"


def test_flags_old_unused_undated_conflict_and_placeholder(tmp_path):
    legacy = {"g10": {"a a": {"A One": 2}, "b b": {"B One": 2}, "c c": {"C One": 2}, "d d": {"D One": 1, "D Two": 1},
                      "e e": {"Unknown": 1}, "f f": {"F One": 3}}}
    s, p = _store(tmp_path, legacy)
    (tmp_path / "aliases.meta.json").write_text(json.dumps({"g10": {
        "a a": {"first_seen": "2026-01-01", "last_confirmed": "2026-01-01", "last_used": "2026-02-01"},   # old
        "b b": {"first_seen": "2026-01-01", "last_confirmed": "2026-01-01"},                              # unused
        "c c": {"first_seen": "2026-10-01", "last_confirmed": "2026-10-01", "last_used": "2026-10-05"},    # fine
    }}), encoding="utf-8")
    flags = {r["key"]: r["flags"] for r in s.entries(stale_days=90, today=TODAY)}
    assert "old" in flags["a a"] and "unused" in flags["b b"] and flags["c c"] == []
    assert "conflict" in flags["d d"] and "unknown-name" in flags["e e"]
    assert "undated" in flags["f f"] and "old" not in flags["f f"]       # legacy entry: age unknown, not guessed


def test_remove_keeps_a_backup_and_cleans_up(tmp_path):
    s, p = _store(tmp_path, {"g10": {"x y": {"X One": 1, "X Two": 1}, "z z": {"Z One": 1}}})
    before = p.read_text(encoding="utf-8")
    assert s.remove("g10", "X Y", roster_name="X Two") == 1
    assert json.loads(p.read_text(encoding="utf-8"))["g10"]["x y"] == {"X One": 1}
    assert (tmp_path / "aliases.json.bak").read_text(encoding="utf-8") == before
    assert s.remove("g10", "Z Z") == 1
    assert "z z" not in json.loads(p.read_text(encoding="utf-8"))["g10"]
    assert s.remove("g10", "nobody here") == 0 and s.remove("g10", "X Y", roster_name="nope") == 0


def test_cli_list_export_remove(tmp_path, capsys):
    s, p = _store(tmp_path, {"g10": {"x y": {"X One": 1}}})
    ns = lambda **k: SimpleNamespace(alias_path=str(p), stale_days=90, **k)  # noqa: E731
    cli.cmd_aliases_list(ns(course=None, flagged_only=False))
    assert "x y" in capsys.readouterr().out
    cli.cmd_aliases_export(ns(out=str(tmp_path / "out" / "a.csv")))
    assert "x y" in (tmp_path / "out" / "a.csv").read_text(encoding="utf-8-sig")
    cli.cmd_aliases_remove(ns(course="g10", name="X Y", roster_name=None, yes=True))
    assert json.loads(p.read_text(encoding="utf-8")) == {}


# ─── Bot command: admin only, never open to everyone ──────────────────────────

def _call(monkeypatch, cfg, user_id, args, env=None):
    patch_bot(monkeypatch, "_load_cfg", lambda: cfg)
    for k in ("ADMIN_TELEGRAM_IDS",):
        monkeypatch.delenv(k, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    replies = []

    async def reply(text, **_k):
        replies.append(text)
    upd = SimpleNamespace(effective_user=SimpleNamespace(id=user_id), message=SimpleNamespace(reply_text=reply))
    asyncio.run(admin.cmd_aliases(upd, SimpleNamespace(args=args)))
    return replies[0]


def test_command_is_admins_only_when_no_admin_is_configured(monkeypatch, cfg):
    assert "admins only" in _call(monkeypatch, cfg, 1, [])


def test_command_refuses_non_admins_and_serves_admins(monkeypatch, cfg, tmp_path):
    cfg["attendance"]["match"]["aliases_file"] = str(tmp_path / "a.json")
    (tmp_path / "a.json").write_text(json.dumps({"g10": {"x y": {"X One": 1}}}), encoding="utf-8")
    assert "admins only" in _call(monkeypatch, cfg, 2, ["list"], env={"ADMIN_TELEGRAM_IDS": "7"})
    assert "x y" not in _call(monkeypatch, cfg, 2, ["list"], env={"ADMIN_TELEGRAM_IDS": "7"})
    assert "x y → X One" in _call(monkeypatch, cfg, 7, ["list"], env={"ADMIN_TELEGRAM_IDS": "7"})
    assert "Removed 1" in _call(monkeypatch, cfg, 7, ["remove", "g10", "x", "y"], env={"ADMIN_TELEGRAM_IDS": "7"})


def test_admin_ids_from_config_and_env_ignore_junk():
    c = {"attendance": {"admin": {"telegram_ids": [5, "6", "oops"]}}}
    assert alias_admin.admin_ids(c, {"ADMIN_TELEGRAM_IDS": "7; 8,,x"}) == {5, 6, 7, 8}
    assert alias_admin.admin_ids({"attendance": {}}, {}) == set()          # fake/minimal config is fine


def test_bot_command_survives_a_broken_config(monkeypatch):
    patch_bot(monkeypatch, "_load_cfg", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    replies = []

    async def reply(text, **_k):
        replies.append(text)
    upd = SimpleNamespace(effective_user=SimpleNamespace(id=1), message=SimpleNamespace(reply_text=reply))
    asyncio.run(admin.cmd_aliases(upd, SimpleNamespace(args=[])))
    assert replies[0].startswith("❌")   # Phase 4: role check fails safe before the command runs


# ─── Pipeline: usage dates are stamped on real runs, never on dry runs ───────

def _run(monkeypatch, cfg, tmp_path, dry_run):
    cfg["attendance"]["match"]["learn_aliases"] = True
    cfg["attendance"]["match"]["aliases_file"] = str(tmp_path / "al.json")
    (tmp_path / "al.json").write_text(json.dumps({"crash": {"ahmed ali": {"Ahmed Hassan Ali": 1}}}), encoding="utf-8")
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(pipeline, "resolve", lambda **_k: fake_session(FakeWS([""] * 6), course="crash"))
    monkeypatch.setattr(writer, "audit_write", lambda **_k: None)
    pipeline.run_text(names=["Ahmed Ali"], course="crash", session_num=3, dry_run=dry_run,
                      config_path=str(cfg_path), review_store_dir=str(tmp_path / "rq"))


def test_pipeline_stamps_last_used_on_real_runs_only(monkeypatch, cfg, tmp_path):
    _run(monkeypatch, cfg, tmp_path, dry_run=True)
    assert not (tmp_path / "al.meta.json").exists()
    _run(monkeypatch, cfg, tmp_path, dry_run=False)
    meta = json.loads((tmp_path / "al.meta.json").read_text(encoding="utf-8"))
    assert meta["crash"]["ahmed ali"]["last_used"]
