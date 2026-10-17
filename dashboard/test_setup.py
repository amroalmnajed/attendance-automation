"""Test script to verify dashboard setup without running Streamlit."""
import sys
sys.path.insert(0, '.')

from config import COURSES, get_password, get_credentials_json
import sheets

print("=== Dashboard Setup Test ===\n")

# Test 1: Config
print("1. Testing config...")
print(f"   - Loaded {len(COURSES)} courses")
for c in COURSES:
    print(f"     • {c.key}: {c.display_name} (sheet_id: {'SET' if c.sheet_id else 'MISSING'})")
print(f"   - Password loaded: {bool(get_password())}")
print()

# Test 2: Credentials
print("2. Testing credentials...")
try:
    creds = get_credentials_json()
    print(f"   - Credentials loaded (project_id: {creds.get('project_id', 'N/A')})")
    print(f"   - Scopes: {creds.get('scopes', [])}")
except Exception as e:
    print(f"   - ERROR: {e}")
print()

# Test 3: Sheet connection
print("3. Testing sheet connection...")
for course in COURSES:
    if not course.sheet_id:
        print(f"   • {course.key}: SKIPPED (no sheet_id)")
        continue
    try:
        client = sheets.get_client()
        spreadsheet = client.open_by_key(course.sheet_id)
        print(f"   • {course.key}: Connected to '{spreadsheet.title}'")
        # Try to open the attendance tab
        ws = spreadsheet.worksheet(course.tab_name)
        print(f"     - Tab '{course.tab_name}' exists ({ws.row_count}×{ws.col_count})")
        # Check if review tab exists
        try:
            review_ws = spreadsheet.worksheet(course.review_tab)
            print(f"     - Review tab '{course.review_tab}' exists ({review_ws.row_count}×{review_ws.col_count})")
        except Exception:
            print(f"     - Review tab '{course.review_tab}' does NOT exist (you need to create it)")
    except Exception as e:
        print(f"   • {course.key}: ERROR - {e}")
print()

print("=== Test Complete ===")
