# Streamlit Cloud Deployment Checklist

## Step 1: Base64-Encoded Service Account Credentials

⚠️ **SECURITY WARNING**: DO NOT commit real credentials to this file.
Generate your own service account key and base64-encode it.

To create a service account key:
1. Go to Google Cloud Console → IAM & Admin → Service Accounts
2. Select your service account
3. Click "Keys" → "Add Key" → "Create new key"
4. Download the JSON file
5. Base64-encode it (on Windows: `certutil -encode in.json out.b64` then use the first line)
6. Add the base64 string to Streamlit Cloud secrets as `GOOGLE_CREDENTIALS_B64`

Example (placeholder only):
```
YOUR_BASE64_ENCODED_JSON_HERE
```

## Step 2: Gather Required Information

You need to provide the following values from your `.env` file. Please open your `.env` file and copy these values:

- **CRASH_SHEET_ID**: The Google Sheet ID for Crash Course
- **MAIN_SHEET_ID**: The Google Sheet ID for Main Course
- **G9_SHEET_ID**: The Google Sheet ID for Grade 9
- **G10_SHEET_ID**: The Google Sheet ID for Grade 10
- **DASHBOARD_PASSWORD**: A secure password for accessing the dashboard

You can find Sheet IDs in your Google Sheets URL:
`https://docs.google.com/spreadsheets/d/[SHEET_ID]/edit`

## Step 3: Create Streamlit Account

1. Go to https://share.streamlit.io
2. Sign up or log in with your GitHub account

## Step 4: Configure Secrets in Streamlit Cloud

After logging in to Streamlit Cloud:

1. Click "New app"
2. Select "From existing repo"
3. Connect your GitHub account (if not already connected)
4. Select the repository: `amroalmnajed/attendance-automation`
5. Set the main file path to: `app.py` (the root app.py now redirects to dashboard)
6. Before clicking "Deploy", go to "Settings" → "Secrets"
7. Add the following secrets:

```toml
# Google Service Account (base64-encoded JSON)
GOOGLE_CREDENTIALS_B64 = "YOUR_BASE64_ENCODED_JSON_HERE"

# Sheet IDs
CRASH_SHEET_ID = "YOUR_CRASH_SHEET_ID"
MAIN_SHEET_ID = "YOUR_MAIN_SHEET_ID"
G9_SHEET_ID = "YOUR_G9_SHEET_ID"
G10_SHEET_ID = "YOUR_G10_SHEET_ID"

# Tab names (matching bot config)
CRASH_TAB = "Crash-Attendance"
MAIN_TAB = "Main-Attendance"
G9_TAB = "Main-Attendance"
G10_TAB = "Main-Attendance"
REVIEW_TAB = "ReviewQueue"

# Session patterns (optional, using defaults)
CRASH_SESSION_PATTERN = "^Session\\s+\\d+$"
MAIN_SESSION_PATTERN = "^Session\\s+\\d+$"
G9_SESSION_PATTERN = "^S\\d+$"
G10_SESSION_PATTERN = "^S\\d+$"
G9_ATTENDANCE_PATTERN = "^S\\d+\\s+Attendance$"
G10_ATTENDANCE_PATTERN = "^S\\d+\\s+Attendance$"

# Dashboard password (REQUIRED - choose a strong password)
DASHBOARD_PASSWORD = "SET_YOUR_SECURE_PASSWORD_HERE"
```

## Step 5: Deploy

1. After configuring secrets, click "Deploy"
2. Wait for the deployment to complete (usually 1-2 minutes)
3. Streamlit Cloud will build and start your app

## Step 6: Verify Deployment

1. Once deployed, open the app URL provided by Streamlit
2. Enter your dashboard password
3. Verify:
   - Course selector shows your configured courses
   - Review Queue page loads (if you have pending items)
   - Attendance page loads with session selector
   - Refresh button works

## Troubleshooting

### App won't start
- Check the logs in Streamlit Cloud (click "Logs" in the app settings)
- Verify all secrets are set correctly
- Ensure `requirements.txt` is in the dashboard folder

### Sheet connection errors
- Verify `GOOGLE_CREDENTIALS_B64` is correctly base64-encoded
- Ensure the service account has editor access to your Google Sheets
- Check that sheet IDs are correct

### ReviewQueue tab not found
- Verify the tab name in your Google Sheets matches the default "ReviewQueue"
- Or set `REVIEW_TAB` in secrets to match your actual tab name

### Password not working
- Check that `DASHBOARD_PASSWORD` is set in secrets
- Try clearing browser cache and re-entering password

## Security Notes

⚠️ **Important:**
- The password is the only authentication mechanism in this MVP
- Choose a strong, unique password
- Rotate the password periodically
- Do not share the app URL with unauthorized users
- Never commit real credentials to git

## Free Tier Constraints

Streamlit Community Cloud free tier has:
- 1 GB RAM
- No persistent disk storage
- CPU time limits

The dashboard is designed to work within these constraints:
- All data lives in Google Sheets (no local state)
- Cache TTL of 60 seconds for sheet reads
- Memory-efficient pandas operations
- No background processes
