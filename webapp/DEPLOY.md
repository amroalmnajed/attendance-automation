# Deployment Guide - Streamlit Community Cloud

This guide explains how to deploy the Attendance Webapp to Streamlit Community Cloud (free tier).

## Prerequisites

1. A Streamlit account (sign up at https://share.streamlit.io)
2. Your Google Sheets configured with course data
3. Google Service Account credentials with editor access to your sheets
4. (Optional) Zoom API credentials for Zoom ingestion feature

## Step 1: Prepare Your Code

The webapp code is already structured for deployment. The main files are:
- `webapp/app.py` - Streamlit entrypoint
- `webapp/requirements.txt` - Dependencies (references root requirements.txt)
- `webapp/config.py` - Configuration (reads from st.secrets in cloud)
- `webapp/pages/` - Page modules for each feature

## Step 2: Set Up Secrets in Streamlit Cloud

1. Go to your Streamlit Cloud dashboard
2. Create a new app or select an existing one
3. Go to Settings → Secrets
4. Add the following secrets:

```toml
# --------------------------------------------------------------------------
# Webapp auth
# --------------------------------------------------------------------------
WEBAPP_PASSWORD = "your-secure-password"

# --------------------------------------------------------------------------
# Google credentials
# --------------------------------------------------------------------------
# Base64-encoded contents of your service-account JSON key file.
# Generate: base64 -i your-key.json | tr -d '\n'
GOOGLE_CREDENTIALS_B64 = "your-base64-encoded-service-account-json"

# --------------------------------------------------------------------------
# Course Google Sheet IDs  (one per course key in config/attendance.yaml)
# --------------------------------------------------------------------------
CRASH_SHEET_ID = "your-crash-sheet-id"
MAIN_SHEET_ID  = "your-main-sheet-id"
G9_SHEET_ID    = "your-g9-sheet-id"
G10_SHEET_ID   = "your-g10-sheet-id"

# --------------------------------------------------------------------------
# Gemini API key (required by attendance/ocr.py)
# --------------------------------------------------------------------------
GEMINI_API_KEY = "your-gemini-api-key"

# --------------------------------------------------------------------------
# Zoom API credentials (optional — needed only for the Zoom ingestion screen)
# --------------------------------------------------------------------------
ZOOM_ACCOUNT_ID    = "your-zoom-account-id"
ZOOM_CLIENT_ID     = "your-zoom-client-id"
ZOOM_CLIENT_SECRET = "your-zoom-client-secret"
```

**Important**: The password should be a strong, unique password since this is the only authentication mechanism.

## Step 3: Base64-Encode Your Service Account JSON

On Linux/Mac:
```bash
base64 -i your-key.json | tr -d '\n'
```

On Windows (PowerShell):
```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("your-key.json"))
```

Copy the output and paste it as the value for `GOOGLE_CREDENTIALS_B64` in Streamlit secrets.

## Step 4: Deploy

### Option A: Deploy from GitHub (Recommended)

1. Push your code to a GitHub repository
2. In Streamlit Cloud, click "New app"
3. Select "From existing repo"
4. Connect your GitHub account
5. Select the repository
6. Set the main file path to `webapp/app.py`
7. Click "Deploy"

### Option B: Deploy from Local Directory

1. In Streamlit Cloud, click "New app"
2. Select "From local directory"
3. Upload the entire repository (or the `webapp/` folder with parent directory structure)
4. Set the main file path to `webapp/app.py`
5. Click "Deploy"

## Step 5: Verify Deployment

1. Once deployed, open the app URL
2. Enter your name (for audit trails) and the webapp password
3. Verify:
   - Course selector shows your configured courses (from config/attendance.yaml)
   - Upload Photos page loads and accepts file uploads
   - Paste Names page loads with textarea
   - Zoom Ingestion page loads (if Zoom credentials are configured)
   - Review Queue page loads (if you have pending items)
   - Attendance Viewer page loads with session selector
   - Admin Tools page loads with stubs

## Free Tier Constraints

Streamlit Community Cloud free tier has:
- 1 GB RAM
- No persistent disk storage
- CPU time limits

Our design respects these constraints:
- ✅ No local state - all data lives in Google Sheets
- ✅ Review queue uses SQLite (in-memory for cloud deployment)
- ✅ Temp files for uploads are cleaned up after processing
- ✅ No background processes
- ✅ Memory-efficient operations

## Troubleshooting

### App won't start
- Check the logs in Streamlit Cloud
- Verify all secrets are set correctly
- Ensure `config/attendance.yaml` exists in the repository
- Verify `webapp/requirements.txt` is present

### Sheet connection errors
- Verify `GOOGLE_CREDENTIALS_B64` is correctly base64-encoded
- Ensure the service account has editor access to your Google Sheets
- Check that sheet IDs are correct
- Verify course keys in config/attendance.yaml match the sheet ID secrets

### Course selector empty
- Check that `config/attendance.yaml` has the `courses` section
- Verify course keys (crash, main, g9, g10) match your configuration
- Check the logs for config loading errors

### Zoom ingestion not working
- Verify Zoom API credentials are set in secrets
- Check that `attendance.zoom_csv.enabled` is `true` in config/attendance.yaml
- Ensure meeting ID mappings are configured

### Password not working
- Check that `WEBAPP_PASSWORD` is set in secrets
- Try clearing browser cache and re-entering password

## Security Notes

⚠️ **Important Security Considerations:**

1. **Shared Password Model**: The current implementation uses a single shared password for all users. This is suitable for a single-teacher/small-team deployment but not for multi-tenant use.

2. **No User Accounts**: There are no user accounts, roles, or permissions. Anyone with the password has full access to all courses and features.

3. **Password in Secrets**: The password is stored in Streamlit secrets, which is secure, but the password itself should be strong and rotated periodically.

4. **Audit Trail**: The webapp records the user name (entered at login) in the `resolved_by` field for all write actions (review resolutions, reverts, absent-marking). This provides basic auditability but not full accountability.

5. **Future Improvements**: For production use with multiple teachers, consider:
   - Proper user authentication (OAuth, SSO)
   - Role-based access control (RBAC)
   - Per-course access restrictions
   - More detailed audit logging

## Next Steps

After successful deployment, you may want to:
1. Set up custom domain (Streamlit Cloud feature)
2. Configure backup strategy for Google Sheets
3. Add more detailed logging
4. Implement proper user authentication (beyond MVP)
5. Port remaining admin tools (revert, absent-offer, alias management, health) from CLI/bot

## Architecture Notes

The webapp calls the same pipeline functions the bot and CLI use:
- `attendance.pipeline.run()` / `run_multi()` / `run_text()` / `run_zoom()`
- `attendance.review_queue.ReviewQueue` (SQLite queue)
- `attendance.resolver_ops.resolve_item()` for resolutions
- `attendance.history.count_marks()` for viewer metrics
- `attendance.reporter.generate_session_report()` / `generate_streaks_report()` for reports

This ensures consistency between the webapp, bot, and CLI.
