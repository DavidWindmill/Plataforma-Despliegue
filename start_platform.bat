@echo off
cd /d "%~dp0"
if not exist .venv (py -3 -m venv .venv)
call .venv\Scripts\activate.bat
python -m pip install -r requirements.txt
echo Abre http://127.0.0.1:8000
python scripts\run_stack.py
pause
