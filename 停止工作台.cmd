@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop.ps1"
if errorlevel 1 (
  echo Workbench stop failed. See storage\logs\server.err.log
  pause
  exit /b 1
)
