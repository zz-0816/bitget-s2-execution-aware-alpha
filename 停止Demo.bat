@echo off
REM ===========================================================
REM  Project 2 - Stop the keep-alive demo
REM
REM  Kills the whole process tree (otherwise orphan cloudflared
REM  processes pile up - a real problem we hit).
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

%PY% -u tools\demo_launcher.py stop %*
echo.
pause
