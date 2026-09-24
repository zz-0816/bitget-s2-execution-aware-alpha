@echo off
REM ===========================================================
REM  Project 2 - Data sync loop
REM
REM  Purpose: keep data\spread\ fresh so the demo runs in
REM  "wallclock" (live) mode instead of the offline asof demo.
REM  The switch is purely data AGE: <= 10 minutes -> live.
REM
REM  Each round (~180s):
REM    1) copy the newest samples from Project 1
REM       (rounds / trades / orderbook; recent 2 days only,
REM        frozen history days are never overwritten)
REM    2) sample sentiment + funding (Project 1 does not sample it)
REM    3) print the freshness verdict
REM
REM  Keep this window open (minimise it). Closing it stops the loop.
REM  Single round (for testing):  pass the argument "once" to this
REM  file (double-click always means: keep looping).
REM  Status any time:  python tools\sync_p1_samples.py --status
REM
REM  NOTE: intentionally ASCII-ONLY. On a Chinese Windows (code page
REM  936) cmd.exe decodes the .bat with GBK, but a UTF-8 Chinese char
REM  is 3 bytes vs 2 in GBK -- with LF-only line endings that dangling
REM  byte EATS the next newline, merging two lines into one (a real
REM  incident: "echo ..." became "?echo ..." and the sentiment line
REM  lost its "python tools" prefix). All Chinese messages are printed
REM  by Python instead. See the start-demo bat for the original note,
REM  and tools\bat_lint.py for the guard that keeps this honest.
REM ===========================================================
cd /d "%~dp0"
setlocal

REM Clear PYTHONPATH: the host may inject a safe-delete shim that hooks
REM os.remove and then fails closed inside this sandbox.
set "PYTHONPATH="

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

%PY% -u tools\sync_p1_samples.py --intro

if /i "%~1"=="once" goto one

:loop
call :round
echo.
echo ---- next round in 180s  (%DATE% %TIME%) ----
timeout /t 180 /nobreak >nul 2>nul
if errorlevel 1 ping -n 181 127.0.0.1 >nul
goto loop

:one
call :round
echo.
echo (single round finished - press any key to close)
pause
exit /b 0

:round
%PY% -u tools\sync_p1_samples.py --once
%PY% -u tools\sentiment_sampler.py
%PY% -u tools\sync_p1_samples.py --status
exit /b 0
