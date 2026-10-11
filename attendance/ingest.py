"""attendance/ingest.py — entry-point shim (start.sh runs `python -m attendance.ingest`)."""
from attendance.bot.app import main

if __name__ == "__main__":
    main()
