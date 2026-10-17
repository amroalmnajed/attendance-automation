# Security Checklist

## ⚠️ Important: Rotate Your Credentials

If you have shared the `.env.local` file with anyone or included it in any archive/shared location, **you should rotate your Google Service Account credentials immediately**.

### How to Rotate:

1. Go to Google Cloud Console → IAM & Admin → Service Accounts
2. Select your service account
3. Click "Keys" → "Add Key" → "Create new key"
4. Download the new JSON file
5. Revoke/delete the old key
6. Update your `.env.local` with the new base64-encoded credentials

### Why?

The `.env.local` file contains:
- Base64-encoded Google Service Account JSON
- This gives full access to your Google Sheets
- Anyone with this file can read/write your attendance data

---

## Current Security Measures

### What's Working:
- ✅ `.env.local` is in `.gitignore` (won't be committed to git)
- ✅ `.env.example` is a template only (no real values)
- ✅ Password gate (single-password authentication)
- ✅ Secrets read from `st.secrets` in cloud deployment
- ✅ No hardcoded credentials in code
- ✅ User name input for audit trail (resolved_by column)
- ✅ Batched Sheets API calls to respect quota limits
- ✅ Minimal service account scope (spreadsheets only, no drive)

### What's MVP-Only (Upgrade Needed for Production):
- ⚠️ Single password for all users (no individual accounts)
- ⚠️ No role-based access control (RBAC)
- ⚠️ User name is free-text input (no authentication, easy to spoof)
- ⚠️ No rate limiting
- ⚠️ No audit logging
- ⚠️ No session timeout
- ⚠️ Session-column matching uses heuristic regex (word-boundary matching of session_num in column names)
    - Fixed the "1" → "Session 10" bug with word boundaries, but still relies on pattern matching
    - Dashboard uses separate Google Sheets ReviewQueue tab from bot's local SQLite queue
    - Column name is not stored at write time, reconstructed at read time
    - Consider adding a `session_column` field to ReviewQueue schema if session naming schemes diverge

---

## Recommended Security Upgrades (Future)

### 1. Proper Authentication
- Add OAuth integration (Google OAuth, SSO)
- Implement user accounts with email/password
- Add password reset functionality

### 2. Role-Based Access Control (RBAC)
- Admin: Full access (approve reviews, view all data)
- Teacher: Can view their course's attendance
- Read-only: Can only view, not modify

### 3. Audit Logging
- Log all write operations (who, what, when)
- Log all access attempts
- Store logs in secure location (Cloud Logging, etc.)

### 4. Rate Limiting
- Prevent brute-force password attacks
- Limit API calls to Google Sheets
- Implement backoff on errors

### 5. Session Management
- Implement proper session tokens
- Add session timeout
- Secure cookie handling

---

## Deployment Security

### Streamlit Cloud
- ✅ Secrets are encrypted at rest
- ✅ Secrets are injected at runtime
- ✅ Secrets are not visible in the app code
- ⚠️ Anyone with app URL can attempt to access (protected by password)

### Google Sheets
- ✅ Service account has scoped permissions (spreadsheets only)
- ✅ Service account can be revoked at any time
- ⚠️ Sheet sharing should be reviewed regularly
- ⚠️ Consider separate service accounts per environment

---

## Immediate Actions Required

### Before Going Public:
1. ✅ Verify `.env.local` is in `.gitignore`
2. ✅ Check git history for accidentally committed secrets: `git log --all --full-history --source -- "*env*"`
3. ✅ Rotate credentials if any leaks suspected
4. ✅ Set a strong `DASHBOARD_PASSWORD`
5. ✅ Review who has access to your Google Sheets

### After Deployment:
1. Monitor Streamlit Cloud logs for suspicious activity
2. Regularly rotate service account credentials (every 90 days)
3. Review sheet access permissions monthly
4. Keep dependencies updated (`pip install --upgrade`)

---

## Security Best Practices

### Never Commit:
- ❌ `.env` files with real values
- ❌ Service account JSON files
- ❌ API keys or tokens
- ❌ Passwords or secrets in code

### Always Use:
- ✅ Environment variables or secrets managers
- ✅ `.env.example` as a template
- ✅ Git pre-commit hooks to catch secrets
- ✅ Separate credentials per environment

### Tools to Help:
- [git-secrets](https://github.com/awslabs/git-secrets) - Prevent committing secrets
- [truffleHog](https://github.com/trufflesecurity/truffleHog) - Scan for secrets in git history
- [gitleaks](https://github.com/gitleaks/gitleaks) - Detect secrets in repos

---

## Contact

If you discover a security vulnerability, please:
1. Rotate affected credentials immediately
2. Review access logs
3. Report the issue following responsible disclosure
