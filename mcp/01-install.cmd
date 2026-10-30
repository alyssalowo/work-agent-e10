@echo off
set PYTHONUTF8=1
cd /d "%~dp0.."
py -3.12 mcp\install.py %*
if errorlevel 1 (pause & exit /b 1)
pause
