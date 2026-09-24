@echo off
chcp 65001 >nul
cd /d %~dp0
title 项目二 · 数据同步（保持实时模式）
echo ============================================================
echo  数据同步：项目一 -^> 项目二（每 3 分钟）+ 情绪/资金费采样
echo ------------------------------------------------------------
echo  · 它解决的是"离线演示 vs 实时模式"的差别（阈值 10 分钟）
echo  · 只写最近两天，冻结的历史日不会被覆盖
echo  · 关掉这个窗口就停；要长期开着就把它最小化
echo  · 状态随时可查：python tools\sync_p1_samples.py --status
echo ============================================================
echo.
:loop
python tools\sync_p1_samples.py --once
python tools\sentiment_sampler.py
echo.
echo ---- 下一轮 180 秒后（%DATE% %TIME%）----
timeout /t 180 /nobreak >nul
goto loop
