@echo off
REM ===========================================================
REM  Project 2 - Execution Demo  (foreground, keep-alive)
REM
REM  NOTE: this file is intentionally ASCII-ONLY.
REM  A .bat containing UTF-8 Chinese text gets mis-parsed by
REM  cmd.exe (it reads the file with the current code page, so
REM  a multi-byte char eats the next newline: "exit /b 0" became
REM  "xit"). All Chinese messages are printed by Python instead.
REM ===========================================================
cd /d "%~dp0"

set "PY="
py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
if not defined PY (
  python -c "import sys" >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo.
  echo [X] Python not found. Please install Python 3.11+ and tick
  echo     "Add Python to PATH" during installation.
  echo     https://www.python.org/downloads/
  echo.
  pause
  exit /b 1
)

%PY% -u tools\demo_launcher.py start %*
echo.
echo (launcher exited with code %ERRORLEVEL%)
pause
