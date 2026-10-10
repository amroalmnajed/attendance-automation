@echo off
cd C:\Users\lenovo\Desktop\attendance-automation
.venv\Scripts\python.exe -m attendance.cli process --image "C:\Users\lenovo\Desktop\agy-project-help\g10 sheet.png" --course g10 --session 1 --type "Saturday - Center" --no-dry-run
pause
