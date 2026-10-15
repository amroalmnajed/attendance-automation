"""core/auth.py — GCP service-account authentication.

Returns a google.oauth2.service_account.Credentials object (or the
Application Default Credentials on Cloud Run) scoped for Sheets + Drive.
"""
from __future__ import annotations

import os
from pathlib import Path

from google.oauth2 import service_account
import google.auth

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def get_credentials() -> service_account.Credentials:
    """Return scoped credentials.

    Priority:
    1. GOOGLE_APPLICATION_CREDENTIALS env var pointing to a key file.
    2. Application Default Credentials (Cloud Run attached service account).
    """
    key_path_env = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
    if key_path_env and Path(key_path_env).exists():
        creds = service_account.Credentials.from_service_account_file(
            key_path_env, scopes=SCOPES
        )
        return creds

    local_sa = Path("service_account.json")
    if local_sa.exists():
        creds = service_account.Credentials.from_service_account_file(
            str(local_sa), scopes=SCOPES
        )
        return creds

    # Fallback: ADC (works on Cloud Run without a key file)
    creds, _ = google.auth.default(scopes=SCOPES)
    return creds  # type: ignore[return-value]
