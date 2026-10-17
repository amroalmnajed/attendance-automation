"""Tests for T10: Pydantic config model."""
from __future__ import annotations

import pytest
import yaml

from attendance.config_model import (
    AttendanceConfig,
    ConfigError,
    CourseConfig,
    FullConfig,
    HealthConfig,
    OcrConfig,
    RemindersConfig,
    config_problems,
    load_config_model,
    validate_cfg,
)


def test_load_real_attendance_yaml():
    """Verify that the real config/attendance.yaml loads cleanly into FullConfig."""
    model = load_config_model("config/attendance.yaml")
    assert isinstance(model, FullConfig)
    assert model.attendance is not None
    assert "crash" in model.attendance.courses
    assert "main" in model.attendance.courses
    assert "g10" in model.attendance.courses
    assert "g9" in model.attendance.courses


def test_ocr_config_defaults():
    ocr = OcrConfig()
    assert ocr.provider == "gemini"
    assert ocr.passes == 2
    assert ocr.min_confidence == 0.75
    assert ocr.retry_preprocessing is False


def test_health_config_defaults():
    h = HealthConfig()
    assert h.enabled is True
    assert h.window_minutes == 60
    assert h.gemini_threshold == 5
    assert h.sheets_threshold == 3


def test_course_single_layout_no_types():
    c = CourseConfig(layout="single", sheet_id_env="CRASH_SHEET_ID")
    assert c.layout == "single"
    assert c.types is None


def test_course_typed_pair_valid_types():
    types_data = {"odd": ["Saturday - Center"], "even": ["Tuesday - Center"]}
    c = CourseConfig(layout="typed_pair", types=types_data)
    assert c.layout == "typed_pair"
    assert "odd" in c.types


@pytest.mark.parametrize("bad_types", [
    None,
    {},
    {"odd": []},
    {"odd": ["", "  "]},
    {"odd": "not-a-list"},
    ["x"],
])
def test_course_typed_pair_invalid_types_raises(bad_types):
    with pytest.raises(Exception):
        CourseConfig(layout="typed_pair", types=bad_types)


def test_validate_cfg_success():
    raw = {
        "core": {"app": {"env": "prod"}},
        "attendance": {
            "courses": {
                "crash": {"sheet_id_env": "CRASH_SHEET_ID", "layout": "single"},
            }
        },
    }
    cfg = validate_cfg(raw)
    assert isinstance(cfg, FullConfig)
    assert cfg.core.app.env == "prod"


def test_validate_cfg_error_raises_config_error():
    raw = {
        "attendance": {
            "courses": {
                "g10": {"sheet_id_env": "G10_SHEET_ID", "layout": "typed_pair", "types": {}},
            }
        }
    }
    with pytest.raises(ConfigError, match="g10"):
        validate_cfg(raw)


def test_config_problems_reports_issues_without_raising():
    raw = {
        "attendance": {
            "courses": {
                "g10": {"sheet_id_env": "G10_SHEET_ID", "layout": "typed_pair", "types": None},
            }
        }
    }
    problems = config_problems(raw)
    assert len(problems) >= 1
    assert any("g10" in p for p in problems)


def test_extra_keys_allowed_for_forward_compatibility():
    raw = {
        "core": {"app": {"env": "dev", "future_key": 12345}},
        "attendance": {"future_feature": {"enabled": True}},
    }
    cfg = validate_cfg(raw)
    assert cfg.core.app.future_key == 12345
