"""T2 - startup config validation + empty session-type keyboard guard."""
import asyncio
from types import SimpleNamespace

import pytest

from attendance.bot import app as bot_app, names
from attendance.config_check import ConfigError, config_problems, validate_cfg
from tests.botutil import patch_bot


def _cfg(**course):
    return {"attendance": {"courses": {"g10": course}}}


GOOD_TYPES = {"odd": ["Saturday - Center"], "even": ["Tuesday - Center"]}


def test_real_config_is_valid():
    import yaml
    with open("config/attendance.yaml", encoding="utf-8") as fh:
        validate_cfg(yaml.safe_load(fh))


def test_single_layout_needs_no_types():
    assert config_problems(_cfg(layout="single")) == []


def test_typed_pair_with_types_is_fine():
    assert config_problems(_cfg(layout="typed_pair", types=GOOD_TYPES)) == []


@pytest.mark.parametrize("types", [None, {}, {"odd": []}, {"odd": ["", "  "]}, {"odd": "not-a-list"}, ["x"], "x"])
def test_typed_pair_without_usable_types_is_a_problem(types):
    kw = {"layout": "typed_pair"}
    if types is not None:
        kw["types"] = types
    problems = config_problems(_cfg(**kw))
    assert len(problems) == 1 and "g10" in problems[0] and "types" in problems[0]
    with pytest.raises(ConfigError, match="g10"):
        validate_cfg(_cfg(**kw))


@pytest.mark.parametrize("cfg", [None, {}, {"attendance": None}, {"attendance": {"courses": None}}])
def test_missing_sections_do_not_crash(cfg):
    assert config_problems(cfg) == []


def test_malformed_courses_are_reported_not_raised():
    assert config_problems({"attendance": {"courses": ["a"]}})
    assert config_problems({"attendance": {"courses": {"g9": "oops"}}})


def test_main_exits_1_on_bad_config(monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TEST-TOKEN-NOT-REAL")
    patch_bot(monkeypatch, "_load_cfg", lambda *a, **k: _cfg(layout="typed_pair"))
    with pytest.raises(SystemExit) as exc:
        bot_app.main()
    assert exc.value.code == 1
    assert "typed_pair" in capsys.readouterr().err


def test_main_still_starts_when_config_unreadable(monkeypatch, tmp_path):
    from telegram.ext import Application
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TEST-TOKEN-NOT-REAL")
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("yaml broke")
        return {"attendance": {"review": {"local_dir": str(tmp_path)}}}
    patch_bot(monkeypatch, "_load_cfg", flaky)
    ran = []
    monkeypatch.setattr(Application, "run_polling", lambda self, *a, **k: ran.append(1))
    bot_app.main()
    assert ran


def test_empty_type_list_gives_a_message_never_an_empty_keyboard(monkeypatch):
    cfg = _cfg(layout="typed_pair", types={})
    patch_bot(monkeypatch, "_load_cfg", lambda *a, **k: cfg)
    replies = []

    async def reply_text(text, **kw):
        replies.append((text, kw))
    upd = SimpleNamespace(message=SimpleNamespace(text="5", reply_text=reply_text),
                          effective_user=SimpleNamespace(id=1))
    ctx = SimpleNamespace(user_data={"nc_course": "g10", "nc_needs_type": True}, bot=None)
    result = asyncio.run(names.handle_names_session_text(upd, ctx))
    assert result == names.ConversationHandler.END
    assert len(replies) == 1 and "reply_markup" not in replies[0][1] and "G10" in replies[0][0]
    assert ctx.user_data == {}
