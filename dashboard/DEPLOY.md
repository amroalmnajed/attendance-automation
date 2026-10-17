# Deployment Guide - Streamlit Community Cloud

This guide explains how to deploy the Teacher's Desk dashboard to Streamlit Community Cloud (free tier).

## Prerequisites

1. A Streamlit account (sign up at https://share.streamlit.io)
2. Your Google Sheets with ReviewQueue tabs created
3. Google Service Account credentials

## Step 1: Prepare Your Code

The dashboard code is already structured for deployment. The main files are:
- `app.py` - Streamlit entrypoint
- `requirements.txt` - Dependencies
- `config.py` - Configuration (reads from st.secrets in cloud)

## Step 2: Set Up Secrets in Streamlit Cloud

1. Go to your Streamlit Cloud dashboard
2. Create a new app or select an existing one
3. Go to Settings → Secrets
4. Add the following secrets:

```toml
# Google Service Account (base64-encoded JSON)
GOOGLE_CREDENTIALS_B64 = "your-base64-encoded-service-account-json"

# Sheet IDs
CRASH_SHEET_ID = "your-crash-sheet-id"
MAIN_SHEET_ID = "your-main-sheet-id"
G9_SHEET_ID = "your-g9-sheet-id"
G10_SHEET_ID = "your-g10-sheet-id"

# Tab names (optional, defaults are set in config.py)
CRASH_TAB = "Crash-Attendance"
MAIN_TAB = "Main-Attendance"
G9_TAB = "Main-Attendance"
G10_TAB = "Main-Attendance"
REVIEW_TAB = "ReviewQueue"

# Session patterns (optional, defaults are set in config.py)
CRASH_SESSION_PATTERN = "^Session\\s+\\d+$"
MAIN_SESSION_PATTERN = "^Session\\s+\\d+$"
G9_SESSION_PATTERN = "^S\\d+$"
G10_SESSION_PATTERN = "^S\\d+$"
G9_ATTENDANCE_PATTERN = "^S\\d+\\s+Attendance$"
G10_ATTENDANCE_PATTERN = "^S\\d+\\s+Attendance$"

# Dashboard password
DASHBOARD_PASSWORD = "your-secure-password"
```

**Important**: The password should be a strong, unique password since this is the only authentication mechanism in MVP.

## Step 3: Base64-Encode Your Service Account JSON

On Linux/Mac:
```bash
base64 -w0 service_account.json
```

On Windows (PowerShell):
```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("service_account.json"))
```

Copy the output and paste it as the value for `GOOGLE_CREDENTIALS_B64` in Streamlit secrets.

## Step 4: Deploy

### Option A: Deploy from GitHub (Recommended)

1. Push your code to a GitHub repository
2. In Streamlit Cloud, click "New app"
3. Select "From existing repo"
4. Connect your GitHub account
5. Select the repository
6. Set the main file path to `dashboard/app.py`
7. Click "Deploy"

### Option B: Deploy from Local Directory

1. In Streamlit Cloud, click "New app"
2. Select "From local directory"
3. Upload the `dashboard` folder
4. Set the main file path to `app.py`
5. Click "Deploy"

## Step 5: Verify Deployment

1. Once deployed, open the app URL
2. Enter your dashboard password
3. Verify:
   - Course selector shows your configured courses
   - Review Queue page loads (if you have pending items)
   - Attendance page loads with session selector
   - Refresh button works

## Free Tier Constraints

Streamlit Community Cloud free tier has:
- 1 GB RAM
- No persistent disk storage
- CPU time limits

Our design respects these constraints:
- ✅ No local state - all data lives in Google Sheets
- ✅ Cache TTL of 60 seconds for sheet reads
- ✅ Memory-efficient pandas operations
- ✅ No background processes

## Troubleshooting

### App won't start
- Check the logs in Streamlit Cloud
- Verify all secrets are set correctly
- Ensure `requirements.txt` is in the root of your deployed directory

### Sheet connection errors
- Verify `GOOGLE_CREDENTIALS_B64` is correctly base64-encoded
- Ensure the service account has editor access to your Google Sheets
- Check that sheet IDs are correct

### ReviewQueue tab not found
- Run `python scripts/create_review_tabs.py` locally to create the tabs
- Verify the tab name matches your `REVIEW_TAB` secret (default: "ReviewQueue")

### Password not working
- Check that `DASHBOARD_PASSWORD` is set in secrets
- Try clearing browser cache and re-entering password

## Security Notes

⚠️ **Important Security Considerations for MVP:**

1. **Single Password Gate**: The current implementation uses a single password for all users. This is suitable for a single-teacher dashboard but not for multi-tenant use.

2. **No User Accounts**: There are no user accounts, roles, or permissions. Anyone with the password has full access.

3. **Password in Secrets**: The password is stored in Streamlit secrets, which is secure, but the password itself should be strong and rotated periodically.

4. **Future Improvements**: For production use with multiple teachers, consider:
   - Proper user authentication (OAuth, SSO)
   - Role-based access control (RBAC)
   - Audit logging
   - Per-course access restrictions

## Next Steps

After successful deployment, you may want to:
1. Set up custom domain (Streamlit Cloud feature)
2. Configure backup strategy for Google Sheets
3. Add more detailed logging
4. Implement proper user authentication (beyond MVP)
5. Add more advanced analytics (beyond MVP scope)
