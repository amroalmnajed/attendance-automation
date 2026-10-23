"""Runtime configuration bridge for Streamlit and the shared pipeline."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


_REPO_ROOT = Path(__file__).resolve().parents[1]
_RUNTIME_SECRET_KEYS = (
    "WEBAPP_PASSWORD",
    "GOOGLE_CREDENTIALS_B64",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "FREEAI_API_KEY",
    "GEMINI_API_KEY",
    "CRASH_SHEET_ID",
    "MAIN_SHEET_ID",
    "G9_SHEET_ID",
    "G10_SHEET_ID",
    "ZOOM_ACCOUNT_ID",
    "ZOOM_CLIENT_ID",
    "ZOOM_CLIENT_SECRET",
)


def configure_runtime_secrets(st_module: Any) -> None:
    """Expose configured Streamlit secrets to the shared, env-based pipeline.

    The attendance modules predate the webapp and read their configuration from
    ``os.environ``.  Streamlit Community Cloud provides values through
    ``st.secrets`` instead, so this one-way bridge keeps the runtime contract
    consistent without writing secrets to disk.
    """
    load_dotenv(_REPO_ROOT / ".env", override=False)
    for key in _RUNTIME_SECRET_KEYS:
        try:
            value = st_module.secrets[key]
        except Exception:
            continue
        if value is not None and str(value):
            os.environ[key] = str(value)
