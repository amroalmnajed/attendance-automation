   #!/bin/sh
   set -e

   if [ -z "$GOOGLE_CREDENTIALS_B64" ]; then
     echo "ERROR: GOOGLE_CREDENTIALS_B64 is not set" >&2
     exit 1
   fi
   printf '%s' "$GOOGLE_CREDENTIALS_B64" | base64 -d > /tmp/service_account.json
   python -c "import json; json.load(open('/tmp/service_account.json'))" \
     || { echo "ERROR: credentials file is not valid JSON" >&2; exit 1; }
   export GOOGLE_APPLICATION_CREDENTIALS=/tmp/service_account.json

   mkdir -p /app/data/review_queue /app/data/review_images /app/data/logs
   rm -rf review_images
   ln -sfn /app/data/review_images review_images

   exec python -m attendance.ingest