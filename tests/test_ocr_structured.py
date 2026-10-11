"""T5 - Gemini structured JSON output (schema) with a plain-config fallback. Fake SDK only."""
import json
import sys
from types import SimpleNamespace

import pytest

from attendance import ocr

GOOD = '[{"raw_name": "Ann", "status_mark": null, "confidence": 0.9, "row_index": 1}]'


class _Cfg:
    def __init__(self, **kw):
        self.kw = kw


class _Err(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def _fake_gtypes(build_fails=False):
    def cfg(**kw):
        if build_fails and "response_schema" in kw:
            raise TypeError("response_schema not supported by this SDK")
        return _Cfg(**kw)
    return SimpleNamespace(
        GenerateContentConfig=cfg,
        Part=SimpleNamespace(from_bytes=lambda **k: ("img", k), from_text=lambda **k: ("txt", k)),
    )


class _Models:
    def __init__(self, script):
        self.script, self.configs = list(script), []

    def generate_content(self, model, contents, config):
        self.configs.append(config.kw)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(text=item)


def _provider(monkeypatch, script, **gt):
    monkeypatch.setitem(sys.modules, "google.genai", SimpleNamespace(types=_fake_gtypes(**gt)))
    p = ocr.GeminiProvider(model="m1")
    models = _Models(script)
    p._client = SimpleNamespace(models=models)
    return p, models


def test_structured_config_has_json_mime_and_schema():
    cfg = ocr._build_config(_fake_gtypes(), True).kw
    assert cfg["temperature"] == 0.0 and cfg["max_output_tokens"] == 8192
    assert cfg["response_mime_type"] == "application/json"
    schema = cfg["response_schema"]
    assert schema["type"] == "ARRAY" and schema["items"]["type"] == "OBJECT"
    props = schema["items"]["properties"]
    assert set(props) == {"raw_name", "status_mark", "confidence", "row_index", "bounding_box",
                          "matched_roster_name", "match_confidence"}
    assert schema["items"]["required"] == ["raw_name", "status_mark", "confidence", "row_index"]
    for nullable in ("status_mark", "bounding_box", "matched_roster_name", "match_confidence"):
        assert props[nullable]["nullable"] is True
    for strict in ("raw_name", "confidence", "row_index"):
        assert not props[strict].get("nullable")
    json.dumps(schema)                                   # plain data, no SDK objects


def test_plain_config_has_no_schema():
    cfg = ocr._build_config(_fake_gtypes(), False).kw
    assert cfg == {"temperature": 0.0, "max_output_tokens": 8192}


def test_schema_covers_every_field_the_prompt_asks_for():
    props = ocr._response_schema()["items"]["properties"]
    for field in ("raw_name", "status_mark", "confidence", "row_index", "bounding_box"):
        assert field in ocr._GEMINI_PROMPT and field in props
    assert "matched_roster_name" in ocr._ROSTER_MATCH_INSTRUCTIONS


def test_read_sends_structured_config_first(monkeypatch):
    p, m = _provider(monkeypatch, [GOOD])
    assert p.read(b"\xff\xd8\xff")[0].raw_name == "Ann"
    assert len(m.configs) == 1 and "response_schema" in m.configs[0]


def test_config_build_failure_falls_back_to_plain_once(monkeypatch):
    p, m = _provider(monkeypatch, [GOOD], build_fails=True)
    assert p.read(b"\xff\xd8\xff")[0].raw_name == "Ann"
    assert len(m.configs) == 1 and "response_schema" not in m.configs[0]


def test_api_400_about_schema_retries_once_without_it(monkeypatch):
    p, m = _provider(monkeypatch, [_Err(400, "400 INVALID_ARGUMENT. response_schema is not supported"), GOOD])
    assert p.read(b"\xff\xd8\xff")[0].raw_name == "Ann"
    assert len(m.configs) == 2
    assert "response_schema" in m.configs[0] and "response_schema" not in m.configs[1]


def test_fallback_sticks_for_later_models(monkeypatch):
    p, m = _provider(monkeypatch, [_Err(400, "INVALID_ARGUMENT response_mime_type unsupported"),
                                   _Err(429, "RESOURCE_EXHAUSTED"), GOOD])
    assert p.read(b"\xff\xd8\xff")
    assert [("response_schema" in c) for c in m.configs] == [True, False, False]


def test_other_400_is_not_retried(monkeypatch):
    p, m = _provider(monkeypatch, [_Err(400, "400 INVALID_ARGUMENT. image too large")])
    with pytest.raises(_Err):
        p.read(b"\xff\xd8\xff")
    assert len(m.configs) == 1


def test_schema_400_on_plain_config_is_not_looped(monkeypatch):
    err = _Err(400, "400 INVALID_ARGUMENT schema")
    p, m = _provider(monkeypatch, [err, _Err(400, "400 INVALID_ARGUMENT schema")])
    with pytest.raises(_Err):
        p.read(b"\xff\xd8\xff")
    assert len(m.configs) == 2


def test_structured_reply_with_nulls_parses_and_fenced_reply_still_works():
    raw = ('[{"raw_name": "Ann", "status_mark": null, "confidence": 0.9, "row_index": 1, '
           '"bounding_box": null, "matched_roster_name": null, "match_confidence": null}]')
    row = ocr._parse_gemini_response(raw)[0]
    assert row.status_mark is None and row.bounding_box is None and row.matched_roster_name is None
    assert ocr._parse_gemini_response("```json\n" + GOOD + "\n```")[0].raw_name == "Ann"
