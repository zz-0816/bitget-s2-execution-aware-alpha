#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Demo 启动器（给 `.bat` 双击用的那一层）
=========================================

为什么要有这一层（一次真实的踩坑）
----------------------------------
最初我把启动逻辑直接写在 `.bat` 里，包括中文注释和中文 echo。结果在中文 Windows
上它**直接崩**：

    'xit' is not recognized as an internal or external command
    '长期运行与保活.md。' is not recognized ...（这行本来是 REM 注释）

原因是 cmd.exe 按**当前代码页**逐字节解析批处理文件，而文件是 UTF-8：
多字节汉字会把紧跟其后的换行/首字符一起吃掉，于是 `exit /b 0` 变成 `xit`、
`REM` 注释行的尾巴被当成命令执行。这类故障**只在中文路径/中文内容下出现**，
在英文环境下测不出来。

现在的分工：
  * `.bat`：**纯 ASCII**，只做三件事 —— 找 Python、把参数转交、`pause`。
  * 本文件：所有中文提示与全部逻辑（Python 侧 UTF-8 完全没问题）。

子命令
------
  start    前台保活运行（等价于 keep_alive，只是先做一次友好预检）
  bg       后台保活运行（脱离当前窗口；窗口关掉也不影响）
  stop     停止保活的实例（连子进程树一起杀）
  status   看状态 + 本机自检 + 当前公网地址 + 最近日志
  check    只做环境预检（Python 版本 / 依赖 / 端口 / cloudflared）
"""

import argparse
import datetime as dt
import io
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

P2 = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, P2)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from common.console import install as _install_console
    _install_console()
except Exception:  # noqa: BLE001
    pass

for _n in ("stdout", "stderr"):
    if getattr(sys, _n, None) is None:
        try:
            setattr(sys, _n, open(os.devnull, "w", encoding="utf-8"))
        except OSError:
            pass


def _c(txt=""):
    print(txt, flush=True)


def _line(ch="=", n=74):
    _c(ch * n)


# ================================================================ 环境预检

def check(verbose=True):
    """返回 (ok, 问题列表)。**只检查，不修改任何东西。**"""
    problems, notes = [], []

    v = sys.version_info
    if v < (3, 11):
        problems.append("Python 版本过低：%d.%d（需要 3.11+，代码用了 datetime.UTC）"
                        % (v.major, v.minor))
    else:
        notes.append("Python %d.%d.%d" % (v.major, v.minor, v.micro))

    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("America/New_York")
        notes.append("时区库可用（America/New_York）")
    except Exception as exc:  # noqa: BLE001
        notes.append("⚠️ 时区库读不到 America/New_York（%s）—— 美股时段判定会退回"
                     "固定偏移（只影响夏令时边界）" % type(exc).__name__)

    for f in ("run_p2.py", os.path.join("web", "index.html"),
              os.path.join("data", "SNAPSHOT.md")):
        if not os.path.exists(os.path.join(P2, f)):
            problems.append("缺少关键文件：%s" % f)
    if not problems:
        notes.append("关键文件齐全（入口 / 页面 / 数据快照）")

    try:
        import keep_alive  # noqa: F401
        notes.append("保活模块可导入")
    except Exception as exc:  # noqa: BLE001
        problems.append("保活模块导入失败：%r" % (exc,))

    cf = None
    try:
        import keep_alive as _k
        cf = _k.find_cloudflared_kind() if hasattr(_k, "find_cloudflared_kind") \
            else None
    except Exception:  # noqa: BLE001
        pass
    try:
        import run_p2
        cfpath = run_p2.find_cloudflared()
    except Exception:  # noqa: BLE001
        cfpath = None
    if cfpath:
        notes.append("cloudflared：%s" % cfpath)
    else:
        notes.append("⚠️ 没找到 cloudflared —— **不会**开公网隧道，只起本机服务。"
                     "装法见 docs/43-公网可访问.md §1.1")
    assert cf is None or True

    if verbose:
        for n in notes:
            _c("  [OK ] %s" % n)
        for p in problems:
            _c("  [!! ] %s" % p)
    return (not problems), problems


# ================================================================ 子命令

def cmd_start(argv):
    _line()
    _c("  项目二 · Execution Demo   ——   保活启动（前台）")
    _line()
    ok, problems = check(verbose=False)
    if not ok:
        _c("  启动前预检没通过：")
        for p in problems:
            _c("    - %s" % p)
        return 1
    _c("  预检通过。窗口关掉 = 停止；要长期后台运行请用 后台运行Demo.bat")
    _c("  公网地址会打印在下面，同时写入 data/run/public_url.txt")
    _line()
    _c()
    import keep_alive
    return keep_alive.main(["--port", str(argv.port)] +
                           ([] if not argv.no_tunnel else ["--no-tunnel"]) +
                           (["--cloudflared", argv.cloudflared]
                            if argv.cloudflared else []))


def cmd_bg(argv):
    """后台启动：脱离当前窗口，日志进文件。"""
    _line()
    _c("  项目二 · Execution Demo   ——   后台保活启动")
    _line()
    ok, problems = check(verbose=False)
    if not ok:
        _c("  启动前预检没通过：")
        for p in problems:
            _c("    - %s" % p)
        return 1

    import keep_alive as k
    # 已经在跑？
    st = k.read_json(k.PID_FILE, {}) or {}
    if st.get("pid") and k.pid_alive(st["pid"]):
        _c("  已经有一个保活实例在跑（PID %s，端口 %s）—— 不重复启动。"
           % (st["pid"], st.get("port")))
        _c("  要重启就先跑 停止Demo.bat。")
        return 0
    if k.port_in_use(argv.port):
        _c("  端口 %d 已被占用（不是本守护起的）—— 拒绝启动。" % argv.port)
        _c("  先看看是谁：netstat -ano | findstr :%d" % argv.port)
        return 2

    os.makedirs(k.LOG_DIR, exist_ok=True)
    console_log = os.path.join(k.LOG_DIR, "console-bg.log")
    cmd = [sys.executable, "-u", os.path.join(P2, "tools", "keep_alive.py"),
           "--port", str(argv.port)]
    if not argv.no_tunnel:
        cmd.append("--tunnel")
        if argv.cloudflared:
            cmd += ["--cloudflared", argv.cloudflared]
    _c("  正在后台拉起：%s" % " ".join(cmd))
    flags = 0
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        flags = 0x00000008 | 0x00000200
    with io.open(console_log, "a", encoding="utf-8") as fh:
        fh.write("\n===== 后台启动 %s =====\n" % dt.datetime.now().isoformat())
        # 同样要强制 UTF-8：stdout 是文件时 Python 会按区域编码写，
        # 日志就会变成 GBK 字节而读的人按 UTF-8 解 —— 乱码。
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        subprocess.Popen(cmd, cwd=P2, stdout=fh, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, creationflags=flags,
                         close_fds=True, env=env)
    _c("  已发起。给它 6 秒起来（隧道通常还要再等几秒）……")
    time.sleep(6)
    _c()
    rc = cmd_status(argv, brief=True)
    _c()
    _c("  控制台日志：%s" % os.path.relpath(console_log, P2))
    _c("  守护自己的日志：data/run/logs/demo-*.log（带时间戳）")
    return rc


def cmd_stop(argv):
    import keep_alive
    _line()
    _c("  项目二 · 停止保活 Demo")
    _line()
    rc = keep_alive.cmd_stop()
    if rc == 0:
        time.sleep(1)
        if not keep_alive.port_in_use(argv.port):
            _c("  端口 %d 已释放。" % argv.port)
        else:
            _c("  [!] 端口 %d 仍被占用 —— 见 netstat -ano | findstr :%d"
               % (argv.port, argv.port))
    return rc


def cmd_status(argv, brief=False):
    import keep_alive as k
    st = k.read_json(k.STATUS_FILE, {}) or {}
    pid = st.get("pid")
    alive = k.pid_alive(pid) if pid else False
    url = None
    if os.path.exists(k.URL_FILE):
        with io.open(k.URL_FILE, encoding="utf-8") as fh:
            url = fh.read().strip() or None

    if not brief:
        _line()
        _c("  项目二 · Demo 状态")
        _line()
    if not st:
        _c("  没有状态文件 —— 保活守护还没跑过。")
        _c("  起一个：双击 启动Demo.bat（前台）或 后台运行Demo.bat")
        return 1

    _c("  守护进程    %s（PID %s，端口 %s）"
       % ("[运行中]" if alive else "[已停止]", pid, st.get("port")))
    _c("  状态        %s ｜ 已重启 %s 次 ｜ 启动于 %s"
       % (st.get("state"), st.get("restarts"), st.get("started_utc")))
    if st.get("last_exit_utc"):
        _c("  上次退出    exit=%s（存活 %ss，%s）"
           % (st.get("last_exit_code"), st.get("last_lived_s"), st.get("last_exit_utc")))

    # 本机服务自检
    port = st.get("port") or 8788
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:%s/api/health" % port, timeout=8) as r:
            h = json.loads(r.read().decode("utf-8"))
        _c("  本机服务    [正常] ｜ 快照 %s ｜ 标的 %d 个 ｜ LLM %s"
           % ((h.get("snapshot") or {}).get("snapshot_utc"), len(h.get("bases") or []),
              "已配置" if (h.get("llm") or {}).get("configured") else "未配置"))
    except Exception as exc:  # noqa: BLE001
        _c("  本机服务    [没响应]（%s）—— 可能正在重启" % type(exc).__name__)

    _c("  公网地址    %s" % (url or "（还没有；隧道可能仍在建立）"))
    if url:
        _c()
        _c("  打开：%s" % url)

    if not alive:
        _c()
        _c("  ⚠️ 守护进程不在跑 —— 上面那个公网地址**很可能已经失效**。")
        _c("     重新起：双击 启动Demo.bat（或 后台运行Demo.bat）")

    if not brief:
        _c()
        _c("  日志        %s" % (st.get("log") or "—"))
        logp = os.path.join(P2, st["log"]) if st.get("log") else None
        if logp and os.path.exists(logp):
            _c("  ---- 最近 12 行 ----")
            with io.open(logp, encoding="utf-8", errors="replace") as fh:
                tail = fh.read().splitlines()[-12:]
            for ln in tail:
                _c("   " + ln)
        _c()
        _c("  ⚠️ 临时链接：守护重启后会换域名 —— 提交前请重跑一次本命令并截图。")
    return 0 if alive else 1


def cmd_check(argv):
    _line()
    _c("  项目二 · 环境预检（只检查，不改任何东西）")
    _line()
    ok, _ = check()
    _c()
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Demo 启动器（给 .bat 用的那一层）")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--port", type=int,
                       default=int(os.environ.get("PORT") or 8788))
        p.add_argument("--no-tunnel", action="store_true")
        p.add_argument("--cloudflared", default=None)

    for name, helptext in (("start", "前台保活运行"), ("bg", "后台保活运行"),
                           ("stop", "停止"), ("status", "看状态"),
                           ("check", "环境预检")):
        sp = sub.add_parser(name, help=helptext)
        common(sp)
    args = ap.parse_args(argv)
    if not args.cmd:
        args = ap.parse_args(["status"])

    if args.cmd == "start":
        return cmd_start(args)
    if args.cmd == "bg":
        return cmd_bg(args)
    if args.cmd == "stop":
        return cmd_stop(args)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "check":
        return cmd_check(args)
    ap.error("未知子命令 %r" % args.cmd)


if __name__ == "__main__":
    sys.exit(main())
