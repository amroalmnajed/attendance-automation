"""Diagnose Sheet connectivity: which tabs exist, what their headers are.

Run this BEFORE touching Streamlit to confirm the assumption set:
  python scripts/sanity_check.py
"""
from __future__ import annotations

import base64
import json
import os
import sys

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

# Add parent directory to path to import config
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Also try to load sheet IDs from parent .env
parent_env = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
if os.path.exists(parent_env):
    load_dotenv(parent_env)

load_dotenv()
if not os.getenv("GOOGLE_CREDENTIALS_B64"):
    load_dotenv(".env.local")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
]


def main() -> None:
    raw = os.getenv("GOOGLE_CREDENTIALS_B64", "")
    if not raw:
        # Try to fall back to the bot's credential file
        bot_creds_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "service_account.json")
        if os.path.exists(bot_creds_path):
            with open(bot_creds_path, "r") as f:
                creds_dict = json.load(f)
            print("Using service_account.json from parent directory")
        else:
            raise SystemExit("GOOGLE_CREDENTIALS_B64 is not set — populate .env first.")
    else:
        creds_dict = json.loads(base64.b64decode(raw).decode("utf-8"))

    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)

    for prefix in ("CRASH", "MAIN", "G9", "G10"):
        sheet_id = os.getenv(f"{prefix}_SHEET_ID", "")
        if not sheet_id:
            print(f"\n[{prefix}] skipped (no sheet ID)")
            continue

        try:
            spreadsheet = client.open_by_key(sheet_id)
        except Exception as exc:
            print(f"\n[{prefix}] ERROR opening sheet {sheet_id}: {exc}")
            continue

        print(f"\n=== [{prefix}] {spreadsheet.title} ===")
        for ws in spreadsheet.worksheets():
            print(f"  • tab: {ws.title}  ({ws.row_count}×{ws.col_count})")
            row1 = ws.row_values(1)
            print(f"    headers: {row1[:20]}")


if __name__ == "__main__":
    main()
