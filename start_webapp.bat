@echo off
:: ============================================================
:: start_webapp.bat - Start the Attendance Automation Streamlit Web App
:: Double-click this file to launch the web app.
:: The window will stay open if the app crashes so you can
:: read the error and contact your system administrator.
:: ============================================================

:: Move to the folder where this .bat file lives (project root)
cd /d "%~dp0"

:: Force UTF-8 so Arabic names display correctly in the console and logs
chcp 65001 >nul 2>&1

echo.
echo  ============================================================
echo   Attendance Automation Web App  -  Starting...
echo   Open your browser to http://localhost:8501
echo   Press Ctrl+C to stop the server.
echo  ============================================================
echo.

:: --- Prefer the project virtual environment if it exists ---
if exist ".venv\Scripts\activate.bat" (
    echo  [INFO] Activating virtual environment .venv ...
    call ".venv\Scripts\activate.bat"
) else (
    echo  [WARN] No .venv found - using system Python.
    echo         If you see "ModuleNotFoundError", run:
    echo           pip install -r requirements.txt
    echo.
)

:: --- Verify Python is available ---
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo  [ERROR] Python was not found on PATH.
    echo          Install Python 3.11+ from https://www.python.org/downloads/
    echo          and make sure to tick "Add Python to PATH" during setup.
    echo.
    pause
    exit /b 1
)

:: --- Verify .env exists ---
if not exist ".env" (
    echo.
    echo  [ERROR] .env file not found in %~dp0
    echo          Copy .env.example to .env and fill in your keys:
    echo            GEMINI_API_KEY=...
    echo            GOOGLE_APPLICATION_CREDENTIALS=...
    echo.
    pause
    exit /b 1
)

:: --- Auto-set GOOGLE_APPLICATION_CREDENTIALS if service_account.json exists locally ---
if not defined GOOGLE_APPLICATION_CREDENTIALS (
    if exist "service_account.json" set "GOOGLE_APPLICATION_CREDENTIALS=service_account.json"
)

:: --- Start the webapp ---
echo  [INFO] Starting Streamlit web app...
echo.
streamlit run webapp/app.py

:: --- If the app exits (crash or Ctrl+C) ---
echo.
if errorlevel 1 (
    echo  [ERROR] The web app stopped with an error ^(exit code %errorlevel%^).
    echo.
    echo  Common causes:
    echo    - Missing or incorrect GEMINI_API_KEY in .env
    echo    - No internet connection
    echo    - Google service account credentials file missing or invalid
    echo.
    echo  Check the logs\ folder for details.
) else (
    echo  [INFO] Web app stopped cleanly ^(Ctrl+C^).
)

echo.
pause
