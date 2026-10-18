"""core/auth.py — GCP service-account authentication.

Returns a google.oauth2.service_account.Credentials object (or the
Application Default Credentials on Cloud Run) scoped for Sheets + Drive.
"""
from __future__ import annotations

import os
import base64
import json
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
    2. GOOGLE_CREDENTIALS_B64 containing a base64-encoded service-account JSON
       document (Streamlit Community Cloud).
    3. A local ``service_account.json`` file.
    4. Application Default Credentials (Cloud Run attached service account).
    """
    key_path_env = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
    if key_path_env and Path(key_path_env).exists():
        creds = service_account.Credentials.from_service_account_file(
            key_path_env, scopes=SCOPES
        )
        return creds

    encoded_credentials = os.getenv("GOOGLE_CREDENTIALS_B64", "").strip()
    if encoded_credentials:
        try:
            info = json.loads(base64.b64decode(encoded_credentials, validate=True).decode("utf-8"))
            if not isinstance(info, dict):
                raise ValueError("decoded credentials are not a JSON object")
            return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        except Exception as exc:
            raise RuntimeError("GOOGLE_CREDENTIALS_B64 is not valid service-account JSON") from exc

    local_sa = Path("service_account.json")
    if local_sa.exists():
        creds = service_account.Credentials.from_service_account_file(
            str(local_sa), scopes=SCOPES
        )
        return creds

    # Fallback: ADC (works on Cloud Run without a key file)
    creds, _ = google.auth.default(scopes=SCOPES)
    return creds  # type: ignore[return-value]
