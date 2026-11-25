@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\launch.ps1" %*
if errorlevel 1 (
  pause
  exit /b 1
)
endlocal
exit /b 0
