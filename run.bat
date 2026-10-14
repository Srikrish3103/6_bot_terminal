@echo off
cd /d %~dp0
if not exist .venv\Scripts\python.exe (python -m venv .venv && .venv\Scripts\python -m pip install -r requirements.txt)
.venv\Scripts\python run.py --live %*
pause
