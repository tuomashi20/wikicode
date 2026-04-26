@echo off
powershell -ExecutionPolicy ByPass -File "%~dp0install_win.ps1"
if %errorlevel% neq 0 pause
