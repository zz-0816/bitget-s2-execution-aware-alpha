@echo off
REM ===========================================================
REM  Project 2 - Execution Demo  (background, keep-alive)
REM
REM  Starts the keep-alive supervisor detached from this window,
REM  so closing the window does NOT stop the demo.
REM  Stop it with:  停止Demo.bat   (or tools\demo_launcher.py stop)
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
  echo.
  echo [X] Python not found. Please install Python 3.11+ .
  echo.
  pause
  exit /b 1
)

%PY% -u tools\demo_launcher.py bg %*
echo.
pause
