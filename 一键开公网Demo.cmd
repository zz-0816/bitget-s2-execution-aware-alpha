@echo off
chcp 65001 >nul
REM ============================================================
REM  项目二 · 一键开公网 Demo（Windows 双击即可）
REM
REM  做两件事：① 起本机服务  ② 用 cloudflared 开一条临时公网隧道
REM  注意：这是**临时**链接，关掉这个窗口就失效。
REM        需要 cloudflared；没装的话窗口里会提示怎么装。
REM ============================================================
setlocal
cd /d "%~dp0"

echo ============================================================
echo   项目二 · Execution-aware Alpha  一键公网 Demo
echo ============================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [X] 没找到 python。请先安装 Python 3.11+ 并勾选 "Add to PATH"。
  pause
  exit /b 1
)

echo [1/3] 先跑自检（不需要网络、不需要 key）...
python run_p2.py --selftest
if errorlevel 1 (
  echo.
  echo [!] 自检没通过。先解决上面的问题再开公网 ——
  echo     把有问题的页面挂到公网，只会让评委看到同一个问题。
  pause
  exit /b 1
)

echo.
echo [2/3] 启动服务 + 临时公网隧道 ...
echo       （公网地址会打印在下面；要停止就按 Ctrl+C 或关掉本窗口）
echo.
python -u run_p2.py --tunnel --port 8788

echo.
echo [3/3] 已停止。
pause
