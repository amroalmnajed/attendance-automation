"""Create ReviewQueue tabs in all configured sheets with the proper schema."""
import sys
import os

# Add dashboard directory to path
dashboard_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, dashboard_dir)

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
import base64
import json
from config import get_course, COURSES

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

REVIEW_HEADERS = [
    "id", "upload_id", "course", "session_num", "session_column", "session_type",
    "raw_name", "image_ref", "bounding_box", "paper_row_index", "photo_index",
    "candidate_1", "candidate_1_score",
    "candidate_2", "candidate_2_score",
    "candidate_3", "candidate_3_score",
    "reason_code", "status",
    "resolved_name", "resolved_value", "resolved_by", "resolved_at",
    "created_at",
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

    # Iterate over configured courses
    for course in COURSES:
        if not course.sheet_id:
            print(f"\n[{course.key}] skipped (no sheet ID)")
            continue

        try:
            spreadsheet = client.open_by_key(course.sheet_id)
        except Exception as exc:
            print(f"\n[{course.key}] ERROR opening sheet {course.sheet_id}: {exc}")
            continue

        print(f"\n=== [{course.key}] {spreadsheet.title} ===")

        # Use the course's configured review tab name
        review_tab_name = course.review_tab
        try:
            review_ws = spreadsheet.worksheet(review_tab_name)
            print(f"  ReviewQueue tab ('{review_tab_name}') already exists")
            # Verify headers
            existing_headers = review_ws.row_values(1)
            if existing_headers == REVIEW_HEADERS:
                print(f"  Headers are correct")
            else:
                print(f"  WARNING: Headers don't match expected schema")
                print(f"  Expected: {REVIEW_HEADERS}")
                print(f"  Found: {existing_headers}")
        except gspread.WorksheetNotFound:
            print(f"  Creating ReviewQueue tab ('{review_tab_name}')...")
            review_ws = spreadsheet.add_worksheet(title=review_tab_name, rows=1000, cols=25)
            review_ws.update([REVIEW_HEADERS], "A1")
            print(f"  ReviewQueue tab created with headers")
            # Freeze header row
            review_ws.freeze(rows=1)
            print(f"  Header row frozen")

if __name__ == "__main__":
    main()
