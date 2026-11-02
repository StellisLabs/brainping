@echo off
REM Runs one Job Watcher check. Point Windows Task Scheduler at this file.
REM Email settings come from secrets.bat (git-ignored) — see README.
cd /d "%~dp0"
if exist secrets.bat call secrets.bat
if not exist state mkdir state
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe monitor.py >> state\log.txt 2>&1
) else (
  python monitor.py >> state\log.txt 2>&1
)
