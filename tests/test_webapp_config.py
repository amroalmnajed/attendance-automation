"""tests/test_webapp_config.py — Unit tests for webapp/config.py (W1).

These tests must run in a plain pytest session (no Streamlit server).
They verify that:
  - _secret() resolves env vars correctly when st.secrets is unavailable
  - COURSES is populated either from config/attendance.yaml or the fallback keys
  - CourseConfig fields are correctly typed
  - CONFIG_PATH points to a plausible location
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Ensure the repo root is on sys.path so `import webapp.config` works when
# pytest is run from the repo root (the normal case).
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reload_config() -> Any:
    """Re-import webapp.config, bypassing the module cache."""
    import importlib
    import webapp.config as cfg_mod  # noqa: PLC0415

    return importlib.reload(cfg_mod)


# ---------------------------------------------------------------------------
# _secret() tests
# ---------------------------------------------------------------------------

class TestSecret:
    def test_returns_env_var_when_st_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When Streamlit is not present / has no secrets, env var is used."""
        monkeypatch.setenv("TEST_SECRET_KEY_XYZ", "hello")
        # Import after monkeypatch so the env is visible
        from webapp.config import _secret  # noqa: PLC0415

        assert _secret("TEST_SECRET_KEY_XYZ") == "hello"

    def test_returns_default_when_key_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MISSING_KEY_ZZZZ", raising=False)
        from webapp.config import _secret  # noqa: PLC0415

        assert _secret("MISSING_KEY_ZZZZ", default="fallback") == "fallback"

    def test_returns_empty_string_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MISSING_KEY_ZZZZ", raising=False)
        from webapp.config import _secret  # noqa: PLC0415

        assert _secret("MISSING_KEY_ZZZZ") == ""

    def test_env_var_takes_precedence_over_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PRIO_TEST_KEY", "env-value")
        from webapp.config import _secret  # noqa: PLC0415

        assert _secret("PRIO_TEST_KEY", default="default-value") == "env-value"


# ---------------------------------------------------------------------------
# CourseConfig tests
# ---------------------------------------------------------------------------

class TestCourseConfig:
    def test_is_importable(self) -> None:
        from webapp.config import CourseConfig  # noqa: PLC0415

        cfg = CourseConfig(key="test", label="Test Course", sheet_id="abc123")
        assert cfg.key == "test"
        assert cfg.label == "Test Course"
        assert cfg.sheet_id == "abc123"

    def test_is_frozen(self) -> None:
        from webapp.config import CourseConfig  # noqa: PLC0415

        cfg = CourseConfig(key="test", label="Test", sheet_id="")
        # frozen=True means Python-level attribute assignment raises FrozenInstanceError
        # (subclass of AttributeError).  object.__setattr__ would bypass it — don't use that.
        try:
            cfg.key = "mutated"  # type: ignore[misc]
            pytest.fail("Expected frozen dataclass to raise on attribute set")
        except AttributeError:
            pass  # FrozenInstanceError(AttributeError) — correct


# ---------------------------------------------------------------------------
# COURSES registry tests
# ---------------------------------------------------------------------------

class TestCourses:
    def test_courses_is_nonempty_dict(self) -> None:
        from webapp.config import COURSES  # noqa: PLC0415

        assert isinstance(COURSES, dict)
        assert len(COURSES) > 0

    def test_courses_contains_expected_keys_or_yaml_keys(self) -> None:
        """COURSES must contain at least the canonical keys OR whatever the YAML defines."""
        from webapp.config import COURSES, _CONFIG_YAML  # noqa: PLC0415

        if _CONFIG_YAML.exists():
            # When YAML exists, all its keys must appear in COURSES
            import yaml  # noqa: PLC0415

            with _CONFIG_YAML.open() as fh:
                raw = yaml.safe_load(fh) or {}
            yaml_keys = set(raw.get("courses", {}).keys())
            if yaml_keys:
                assert yaml_keys == set(COURSES.keys()), (
                    f"COURSES keys {set(COURSES.keys())} don't match YAML keys {yaml_keys}"
                )
        else:
            # Fallback: must contain the four known course keys
            fallback = {"crash", "main", "g9", "g10"}
            assert fallback.issubset(set(COURSES.keys())), (
                f"Fallback keys missing from COURSES: {fallback - set(COURSES.keys())}"
            )

    def test_courses_values_are_course_config_instances(self) -> None:
        from webapp.config import COURSES, CourseConfig  # noqa: PLC0415

        for key, cfg in COURSES.items():
            assert isinstance(cfg, CourseConfig), f"COURSES[{key!r}] is not a CourseConfig"
            assert cfg.key == key, f"CourseConfig.key mismatch for {key!r}"
            assert isinstance(cfg.label, str)
            assert isinstance(cfg.sheet_id, str)

    def test_courses_keys_match_config_attr(self) -> None:
        """CourseConfig.key must equal the dict key it's stored under."""
        from webapp.config import COURSES  # noqa: PLC0415

        for k, v in COURSES.items():
            assert v.key == k


# ---------------------------------------------------------------------------
# CONFIG_PATH tests
# ---------------------------------------------------------------------------

class TestConfigPath:
    def test_config_path_is_string(self) -> None:
        from webapp.config import CONFIG_PATH  # noqa: PLC0415

        assert isinstance(CONFIG_PATH, str)

    def test_config_path_ends_with_attendance_yaml(self) -> None:
        from webapp.config import CONFIG_PATH  # noqa: PLC0415

        assert CONFIG_PATH.endswith("attendance.yaml"), CONFIG_PATH

    def test_config_path_is_absolute(self) -> None:
        from webapp.config import CONFIG_PATH  # noqa: PLC0415

        assert Path(CONFIG_PATH).is_absolute(), f"CONFIG_PATH is not absolute: {CONFIG_PATH}"
