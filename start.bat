@echo off
title TEN FORWARD
cd /d "%~dp0"
set TF_PORT=8410
set TF_HOST=0.0.0.0
set PYTHONIOENCODING=utf-8
set TF_VOICES=0
set TF_PERSONAL=0
echo ============================================================
echo  TEN FORWARD - your own AI radio
echo  Open http://localhost:8410 in a browser.
echo  Other machines and phones on your network use this computer's
echo  address, for example http://192.168.1.50:8410
echo ============================================================
if exist "venv\Scripts\python.exe" (
  "venv\Scripts\python.exe" server.py
) else (
  python server.py
)
echo.
echo Ten Forward stopped. Press any key to close.
pause >nul
