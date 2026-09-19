@echo off
REM ===========================================================
REM  Project 2 - Demo status (incl. the CURRENT public URL)
REM
REM  The quick-tunnel domain changes on every restart, so this
REM  file (and data\run\public_url.txt) is the source of truth
REM  for "what link do I give the judges right now".
REM
REM  ASCII-ONLY on purpose - see 启动Demo.bat for the reason.
REM ===========================================================
cd /d "%~dp0"

set "PY="
py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
if not defined PY (
  python -c "import sys" >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo [X] Python not found.
  pause
  exit /b 1
)

%PY% -u tools\demo_launcher.py status %*
echo.
pause
