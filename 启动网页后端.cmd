@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Please install PaperLens with the original launcher first.
  pause
  exit /b 1
)
set PYTHONIOENCODING=utf-8
".venv\Scripts\python.exe" -m paperlens.web_agent run --data "%~dp0.web-service"
pause
