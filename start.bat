@echo off
cd /d "%~dp0"
echo Korea Trade Monitor - close this window (or press Ctrl+C) to stop the server.
start "" http://127.0.0.1:8000
".venv\Scripts\python.exe" main.py
pause
