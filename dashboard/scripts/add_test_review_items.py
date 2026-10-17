"""Add test review items to the ReviewQueue tab for testing."""
import sys
import os
import uuid
from datetime import datetime, timezone

# Add dashboard directory to path
dashboard_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, dashboard_dir)

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
import base64
import json

# Load env files
load_dotenv()
if not os.getenv("GOOGLE_CREDENTIALS_B64"):
    load_dotenv(os.path.join(dashboard_dir, ".env.local"))

# Also try to load sheet IDs from parent .env if not set
parent_env = os.path.join(dashboard_dir, "..", ".env")
if os.path.exists(parent_env):
    load_dotenv(parent_env, override=False)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
]

TEST_ITEMS = [
    {
        "id": str(uuid.uuid4()),
        "upload_id": str(uuid.uuid4()),
        "course": "crash",
        "session_num": "1",
        "raw_name": "Ahmd Ali",
        "candidate_1": "Ahmad Ali",
        "candidate_1_score": "92",
        "candidate_2": "Ahmed Aly",
        "candidate_2_score": "88",
        "candidate_3": "Ahmad Ali Hassan",
        "candidate_3_score": "71",
        "reason_code": "MATCH_LOW_SCORE",
        "status": "pending",
        "resolved_name": "",
        "resolved_value": "",
        "resolved_by": "",
        "resolved_at": "",
        "created_at": datetime.now(timezone.utc).isoformat(),
    },
    {
        "id": str(uuid.uuid4()),
        "upload_id": str(uuid.uuid4()),
        "course": "crash",
        "session_num": "1",
        "raw_name": "Mohamd S.",
        "candidate_1": "Mohamed Samir",
        "candidate_1_score": "85",
        "candidate_2": "Mohamed Said",
        "candidate_2_score": "83",
        "candidate_3": "Mohamad Saleh",
        "candidate_3_score": "80",
        "reason_code": "MATCH_AMBIGUOUS",
        "status": "pending",
        "resolved_name": "",
        "resolved_value": "",
        "resolved_by": "",
        "resolved_at": "",
        "created_at": datetime.now(timezone.utc).isoformat(),
    },
]

def main():
    raw = os.getenv("GOOGLE_CREDENTIALS_B64", "")
    if not raw:
        # Try to fall back to the bot's credential file
        parent_dir = os.path.dirname(dashboard_dir)
        bot_creds_path = os.path.join(parent_dir, "service_account.json")
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

    # Use crash course for testing
    sheet_id = os.getenv("CRASH_SHEET_ID", "")
    if not sheet_id:
        raise SystemExit("CRASH_SHEET_ID not set")

    spreadsheet = client.open_by_key(sheet_id)
    print(f"Connected to: {spreadsheet.title}")

    review_ws = spreadsheet.worksheet("ReviewQueue")

    # Get existing rows to append
    existing_rows = review_ws.get_all_values()
    start_row = len(existing_rows) + 1

    # Add test items
    for item in TEST_ITEMS:
        row = [
            item["id"],
            item["upload_id"],
            item["course"],
            item["session_num"],
            item["raw_name"],
            item["candidate_1"],
            item["candidate_1_score"],
            item["candidate_2"],
            item["candidate_2_score"],
            item["candidate_3"],
            item["candidate_3_score"],
            item["reason_code"],
            item["status"],
            item["resolved_name"],
            item["resolved_value"],
            item["resolved_by"],
            item["resolved_at"],
            item["created_at"],
        ]
        review_ws.append_row(row)

    print(f"Added {len(TEST_ITEMS)} test review items to ReviewQueue tab")

if __name__ == "__main__":
    main()
