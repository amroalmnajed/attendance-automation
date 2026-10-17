"""Streamlit entrypoint - redirects to dashboard."""
import sys
import os

# Ensure the dashboard directory is in the path
dashboard_dir = os.path.join(os.path.dirname(__file__), 'dashboard')
sys.path.insert(0, dashboard_dir)

# Import and run the dashboard app
if __name__ == "__main__":
    from dashboard.app import main
    main()
