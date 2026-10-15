@echo off
:: ============================================================
:: start_bot.bat - Start the Attendance Automation Telegram Bot
:: Double-click this file to launch the bot.
:: The window will stay open if the bot crashes so you can
:: read the error and contact your system administrator.
:: ============================================================

:: Move to the folder where this .bat file lives (project root)
cd /d "%~dp0"

:: Force UTF-8 so Arabic names display correctly in the console and logs
chcp 65001 >nul 2>&1

echo.
echo  ============================================================
echo   Attendance Automation Bot  -  Starting...
echo   Commands available via Telegram:
echo     /start   - Choose image or paste names
echo     /report  - Session summary
echo     /streaks - Chronic absentees
echo     /revert  - Undo an upload
echo     /pending - Show review queue
echo   Press Ctrl+C to stop the bot.
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
    echo            TELEGRAM_BOT_TOKEN=...
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

:: --- Start the bot ---
echo  [INFO] Bot is running. Open Telegram and send /start
echo.
python -m attendance.ingest

:: --- If the bot exits (crash or Ctrl+C) ---
echo.
if errorlevel 1 (
    echo  [ERROR] The bot stopped with an error ^(exit code %errorlevel%^).
    echo.
    echo  Common causes:
    echo    - Missing or incorrect TELEGRAM_BOT_TOKEN in .env
    echo    - Missing or incorrect GEMINI_API_KEY in .env
    echo    - No internet connection
    echo    - Google service account credentials file missing or invalid
    echo.
    echo  Check the logs\ folder for details.
) else (
    echo  [INFO] Bot stopped cleanly ^(Ctrl+C^).
)

echo.
pause
