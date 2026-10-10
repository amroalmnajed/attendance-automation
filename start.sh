#!/bin/sh
set -e

# 1. Rebuild the Google service-account key file from the env var
printf '%s' "$GOOGLE_CREDENTIALS_JSON" > /tmp/service_account.json
export GOOGLE_APPLICATION_CREDENTIALS=/tmp/service_account.json

# 2. Persistent folders live on the Railway volume (mounted at /app/data)
mkdir -p /app/data/review_queue /app/data/review_images /app/data/logs
rm -rf review_images
ln -sfn /app/data/review_images review_images

# 3. Run the bot
exec python -m attendance.ingest