@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\dev-launcher.ps1" %*
if errorlevel 1 (
  echo.
  echo Development launch failed. See the error above.
  pause
  exit /b 1
)
