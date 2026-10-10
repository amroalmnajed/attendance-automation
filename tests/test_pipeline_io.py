import pytest
from attendance.pipeline import load_config, run
from core.notifier import RunSummary
import os

def test_load_config_missing_file():
    """Test that load_config handles a missing file gracefully, raising a RuntimeError or returning an empty dict."""
    with pytest.raises(RuntimeError, match="Configuration file not found"):
        load_config("nonexistent_config_12345.yaml")

def test_run_missing_image_file():
    """Test that run handles a missing image file gracefully by returning a summary with the error."""
    # We will need a dummy config file for this test to pass the config loading step
    dummy_config = "tests/test_dummy_config.yaml"
    with open(dummy_config, "w") as f:
        f.write("attendance:\n  ocr:\n    provider: mock\n")
        
    try:
        summary = run(
            image_path="nonexistent_image_12345.jpg",
            course="g10",
            session_num=1,
            dry_run=True,
            config_path=dummy_config
        )
        assert isinstance(summary, RunSummary)
        assert len(summary.errors) > 0
        assert "not found" in summary.errors[0].lower()
    finally:
        if os.path.exists(dummy_config):
            os.remove(dummy_config)
