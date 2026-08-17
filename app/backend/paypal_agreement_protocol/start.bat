@echo off
cd /d "%~dp0"
py -m pip install -r requirements.txt
py web.py --host 0.0.0.0 --port 8080
pause
