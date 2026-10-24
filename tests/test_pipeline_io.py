import pytest
from attendance.pipeline import load_config, run
from core.notifier import RunSummary

def test_load_config_missing_file():
    """Test that load_config handles a missing file gracefully, raising a RuntimeError or returning an empty dict."""
    with pytest.raises(RuntimeError, match="Configuration file not found"):
        load_config("nonexistent_config_12345.yaml")

def test_run_missing_image_file(tmp_path):
    """Test that run handles a missing image file gracefully by returning a summary with the error."""
    # We will need a dummy config file for this test to pass the config loading step
    from pathlib import Path
    dummy_config = tmp_path / "config.yaml"
    dummy_config.write_text(Path("config/attendance.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    summary = run(
        image_path="nonexistent_image_12345.jpg",
        course="g10",
        session_num=1,
        dry_run=True,
        config_path=str(dummy_config),
    )
    assert isinstance(summary, RunSummary)
    assert len(summary.errors) > 0
    assert "not found" in summary.errors[0].lower()


def test_load_config_rejects_malformed_or_incomplete_config(tmp_path):
    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("attendance: []\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Invalid config"):
        load_config(str(invalid))
