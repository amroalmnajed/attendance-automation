"""Test script to debug Zoom API issues.

Run this locally to test the Zoom API directly.
"""
import os
import sys
from dotenv import load_dotenv

# Set UTF-8 encoding for Windows console
if sys.stdout.encoding != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

load_dotenv(override=True)

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from attendance.zoom_api import get_oauth_token, list_recent_meeting_instances, fetch_past_meeting_participants

def test_zoom_api():
    print("=" * 60)
    print("Zoom API Test Script")
    print("=" * 60)

    # Check environment variables
    print("\n1. Checking environment variables...")
    account_id = os.getenv("ZOOM_ACCOUNT_ID")
    client_id = os.getenv("ZOOM_CLIENT_ID")
    client_secret = os.getenv("ZOOM_CLIENT_SECRET")

    if not all([account_id, client_id, client_secret]):
        print("[X] Missing environment variables:")
        if not account_id:
            print("   - ZOOM_ACCOUNT_ID is missing")
        if not client_id:
            print("   - ZOOM_CLIENT_ID is missing")
        if not client_secret:
            print("   - ZOOM_CLIENT_SECRET is missing")
        return

    print("[OK] All environment variables found")
    print(f"   Account ID: {account_id[:10]}...{account_id[-4:] if len(account_id) > 14 else account_id}")
    print(f"   Client ID: {client_id[:10]}...{client_id[-4:] if len(client_id) > 14 else client_id}")

    # Test OAuth token
    print("\n2. Testing OAuth token fetch...")
    config = {
        "account_id_env": "ZOOM_ACCOUNT_ID",
        "client_id_env": "ZOOM_CLIENT_ID",
        "client_secret_env": "ZOOM_CLIENT_SECRET",
        "token_url": "https://zoom.us/oauth/token",
        "api_base_url": "https://api.zoom.us/v2",
    }

    try:
        token = get_oauth_token(config)
        print(f"[OK] Token fetched successfully")
        print(f"   Token length: {len(token)} characters")
        print(f"   Token preview: {token[:20]}...{token[-10:]}")
    except Exception as e:
        print(f"[X] Token fetch failed: {e}")
        return

    # Test listing instances
    print("\n3. Testing meeting instance listing...")
    meeting_id = "81795786552"  # The meeting ID from your logs

    try:
        instances = list_recent_meeting_instances(meeting_id, config)
        print(f"[OK] Listed {len(instances)} instances")
        if instances:
            print(f"   First instance UUID: {instances[0].get('uuid')}")
            print(f"   First instance time: {instances[0].get('start_time')}")
    except Exception as e:
        print(f"[X] Instance listing failed: {e}")
        return

    # Test fetching participants for the most recent instance
    print("\n4. Testing participant fetch (without instance UUID)...")
    try:
        participants = fetch_past_meeting_participants(meeting_id, None, config)
        print(f"[OK] Fetched {len(participants)} participants")
        if participants:
            print(f"   First participant: {participants[0].get('name')}")
            print(f"   First join time: {participants[0].get('join_time')}")
            print(f"   First leave time: {participants[0].get('leave_time')}")
    except Exception as e:
        print(f"[X] Participant fetch failed: {e}")

    print("\n" + "=" * 60)
    print("Test complete")
    print("=" * 60)

if __name__ == "__main__":
    test_zoom_api()
