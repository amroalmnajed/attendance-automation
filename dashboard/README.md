# Teacher's Desk - Attendance Dashboard

A Streamlit web dashboard for reviewing, fixing, and understanding attendance data from the attendance automation bot.

## Setup

### 1. Install Dependencies

```bash
cd dashboard
pip install -r requirements.txt
```

### 2. Configure Environment

Copy `.env.example` to `.env.local` and fill in your values:

```bash
cp .env.example .env.local
```

Edit `.env.local`:
- `GOOGLE_CREDENTIALS_B64`: Base64-encoded service account JSON (already set from parent directory)
- `CRASH_SHEET_ID`, `MAIN_SHEET_ID`, `G9_SHEET_ID`, `G10_SHEET_ID`: Your Google Sheet IDs (already loaded from parent `.env`)
- `DASHBOARD_PASSWORD`: Password for the dashboard (default: `test123`)

### 3. Initialize ReviewQueue Tabs

Run the setup script to create ReviewQueue tabs in all your sheets:

```bash
python scripts/create_review_tabs.py
```

This creates a `ReviewQueue` tab in each course's spreadsheet with the proper schema.

### 4. Test the Setup

Run the test script to verify everything is connected:

```bash
python test_setup.py
```

You should see:
- All 4 courses loaded
- Credentials loaded successfully
- Sheet connections successful
- ReviewQueue tabs exist

### 5. Add Test Data (Optional)

To test the Review Queue page with sample data:

```bash
python scripts/add_test_review_items.py
```

This adds 2 test review items to the Crash course ReviewQueue tab.

## Running the Dashboard

```bash
streamlit run app.py
```

Then open http://localhost:8501 in your browser.

### First Run

On the first run, Streamlit may ask for your email. You can:
- Enter your email (optional)
- Or press Enter to skip

Enter the password from `DASHBOARD_PASSWORD` to access the dashboard.

## File Structure

```
dashboard/
├── app.py                 # Streamlit entrypoint
├── config.py              # Configuration and constants
├── sheets.py              # Google Sheets I/O
├── review.py              # Review Queue page logic
├── attendance.py          # Attendance page logic
├── requirements.txt       # Python dependencies
├── .env.example           # Environment template
├── .env.local             # Your local config (create this)
├── test_setup.py          # Setup verification script
├── scripts/
│   ├── sanity_check.py    # Diagnose sheet connectivity
│   ├── create_review_tabs.py  # Initialize ReviewQueue tabs
│   └── add_test_review_items.py  # Add test data for testing
└── .streamlit/
    └── config.toml        # Streamlit config
```

## Current Status

**Stage 1 Complete:**
- ✅ Configuration and course registry
- ✅ Google Sheets connection
- ✅ ReviewQueue tab schema creation
- ✅ Password gate
- ✅ Sidebar shell

**Stage 2 Complete:**
- ✅ Review Queue page implementation
- ✅ Load pending review items from ReviewQueue tab
- ✅ Display raw name + top 3 candidates with scores
- ✅ Bulk selection and approval
- ✅ Write resolved attendance to main sheet
- ✅ Mark items as approved in ReviewQueue
- ✅ Idempotency checks (no duplicate writes)

**Stage 3 Complete:**
- ✅ Attendance page implementation
- ✅ Session selector dropdown
- ✅ Search by student name
- ✅ Summary metrics (Total, Present, Absent, Needs Review)
- ✅ Color-coded status display
- ✅ Raw values view for debugging

**Dashboard MVP Complete!**
