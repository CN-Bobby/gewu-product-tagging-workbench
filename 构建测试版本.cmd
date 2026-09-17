@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\build-test.ps1" %*
if errorlevel 1 (
  echo.
  echo Development build or launch failed. See the error above.
  pause
  exit /b 1
)
