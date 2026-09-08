@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File ".\scripts\stop-console.ps1"
if errorlevel 1 pause
