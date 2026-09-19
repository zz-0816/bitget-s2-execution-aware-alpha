#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Demo 保活守护（进程掉了自己回来，且**随时能查到当前公网地址**）
================================================================

为什么需要它
------------
`python run_p2.py --tunnel` 有个致命的使用问题：**进程一掉，公网链接就死了**，
而临时隧道的域名每次重启都会变。提交期评委可能在任何时间点开你的链接，
所以我们需要三件事：

1. **自愈**：进程崩溃/断网/被误关 -> 自动重启（带退避，不会疯狂刷日志）；
2. **可查**：无论重启多少次，当前公网地址都写在一个**固定文件**里
   （`data/run/public_url.txt`）并能从状态里读到；
3. **可控**：单实例（不会起两份抢端口）、能干净地停（**连子进程树一起杀**，
   否则会攒下一堆孤儿 cloudflared）。

Windows 上"杀干净"尤其重要：`run_p2.py` 会再拉起 `cloudflared`，
直接 kill 父进程**不会**带走孙进程 —— 本文件用 `taskkill /T /F` 处理。

用法
----
  python tools/keep_alive.py                 # 保活：本机服务 + 公网隧道
  python tools/keep_alive.py --no-tunnel     # 只要本机服务（不起公网）
  python tools/keep_alive.py --port 8789
  python tools/keep_alive.py --once          # 只跑一次、不重启（调试用）
  python tools/keep_alive.py --status        # 看当前状态（含公网地址）
  python tools/keep_alive.py --stop          # 停掉正在保活的实例
  python tools/keep_alive.py --selftest      # 离线自检（不需要网络）

产物（都在 `data/run/`，已 gitignore）
--------------------------------------
  public_url.txt   当前公网地址（一行纯文本；进程重启会覆盖它）
  status.json      机器可读的完整状态
  demo.pid         守护进程 PID（用于单实例与 --stop）
  logs/demo-*.log  带时间戳的运行日志（只保留最近 10 份）
"""

import argparse
import datetime as dt
import io
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

P2 = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, P2)
try:
    from common.console import install as _install_console
    _install_console()
except Exception:  # noqa: BLE001
    pass

# 用 pythonw.exe（无控制台）启动时 sys.stdout 是 None，print() 会直接抛异常。
# 让它在任何启动方式下都不会因为"打印"而崩。
for _name in ("stdout", "stderr"):
    if getattr(sys, _name, None) is None:
        try:
            setattr(sys, _name, open(os.devnull, "w", encoding="utf-8"))
        except OSError:
            pass

RUN_DIR = os.path.join(P2, "data", "run")
LOG_DIR = os.path.join(RUN_DIR, "logs")
PID_FILE = os.path.join(RUN_DIR, "demo.pid")
STATUS_FILE = os.path.join(RUN_DIR, "status.json")
URL_FILE = os.path.join(RUN_DIR, "public_url.txt")

# 只认真正的 URL 形态。踩过的坑：cloudflared 的日志行里会出现别的词条也含这个
# 域名（例如 "trycloudflare.com..."），早期的解析把它当成地址打了出来。
URL_RE = re.compile(r"https://[a-z0-9][a-z0-9\-]*\.trycloudflare\.com")

KEEP_LOGS = 10


# ================================================================ 小工具

def now_iso():
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def port_in_use(port, host="127.0.0.1"):
    with socket.socket() as s:
        s.settimeout(1.0)
        return s.connect_ex((host, port)) == 0


def pid_alive(pid):
    """这个 PID 还活着吗？

    ⚠️ Windows 上不要用 `os.kill(pid, 0)` 探活：Windows 的 os.kill 对任意
    信号都会走 TerminateProcess —— 探活探成**误杀**。这里的顺序是：
    ① ctypes OpenProcess（最快、不依赖外部命令）；
    ② 退回 tasklist（某些受限环境里 ctypes 不可用）；
    ③ POSIX 用 os.kill(pid, 0)。
    """
    if not pid:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            ERROR_ACCESS_DENIED = 5
            h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False,
                                int(pid))
            if not h:
                # 打不开但错误是"拒绝访问" -> 进程存在（只是不归我们管）
                return ctypes.get_last_error() == ERROR_ACCESS_DENIED
            try:
                code = wintypes.DWORD()
                if k32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return code.value == STILL_ACTIVE
                return True
            finally:
                k32.CloseHandle(h)
        except Exception:  # noqa: BLE001  —— 落到下面的兜底
            pass
        try:
            out = subprocess.run(["tasklist", "/NH", "/FI", "PID eq %d" % pid],
                                 capture_output=True, text=True,
                                 encoding="utf-8", errors="replace")
            return str(pid) in (out.stdout or "")
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def extract_url(line):
    """从一行输出里抽出公网地址（抽不到返回 None）。"""
    m = URL_RE.search(line or "")
    return m.group(0) if m else None


def backoff_next(cur, lo, hi):
    """指数退避，封顶 hi。"""
    return min(hi, max(lo, cur * 2)) if cur else lo


def read_json(path, default=None):
    try:
        with io.open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1, sort_keys=True)


def kill_tree(proc):
    """连**子进程树**一起杀。

    Windows 上必须这样：`run_p2.py` 会拉起 `cloudflared`，
    只 kill 父进程会把 cloudflared 留成孤儿（实测会越攒越多）。
    """
    if proc.poll() is not None:
        return
    if os.name == "nt":
        r = subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, text=True)
        # taskkill 被拒时（受限环境）退一步：直接 TerminateProcess 自己这个子进程。
        # 至少把服务停掉；孙进程 cloudflared 由 kill_orphan_cloudflared 兜底。
        if r.returncode != 0 and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            time.sleep(1.5)
            if proc.poll() is None:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def kill_orphan_cloudflared(port, verbose=False):
    """清掉"父进程已死、且连的是本端口"的 cloudflared 残留。

    为什么需要：`cloudflared` 是独立进程，父进程被强杀（任务管理器/taskkill /F）
    时它**不会跟着死**，而保活守护会再拉起一个新的 —— 于是孤儿越攒越多，
    每个都占着一条隧道。这里按"命令行里含本端口 + 父进程已不存在"精确识别，
    只杀自己那一类，**不碰别人的进程**（本机上还有其它项目在跑）。
    """
    if os.name != "nt":
        return 0
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='cloudflared.exe'\" | "
          "Select-Object ProcessId,ParentProcessId,CommandLine | "
          "ConvertTo-Json -Compress")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=30)
        txt = (r.stdout or "").strip()
        if not txt:
            return 0
        data = json.loads(txt)
        items = data if isinstance(data, list) else [data]
    except Exception:  # noqa: BLE001
        return 0
    killed = 0
    for it in items:
        try:
            pid = int(it.get("ProcessId"))
            ppid = int(it.get("ParentProcessId") or 0)
            cl = str(it.get("CommandLine") or "")
        except (TypeError, ValueError):
            continue
        if (":%d" % port) not in cl:
            continue                      # 不是连我们端口的，别碰
        if ppid and pid_alive(ppid):
            continue                      # 父进程还在 -> 不是孤儿
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True, text=True)
        killed += 1
        if verbose:
            print("  已清理孤儿 cloudflared（PID %d，连的是本端口 %d）"
                  % (pid, port), flush=True)
    return killed


def rotate_logs(keep=KEEP_LOGS):
    try:
        logs = sorted(
            (os.path.join(LOG_DIR, f) for f in os.listdir(LOG_DIR)
             if f.startswith("demo-") and f.endswith(".log")),
            key=os.path.getmtime)
    except OSError:
        return
    for p in logs[:-keep]:
        try:
            os.remove(p)
        except OSError:
            pass


# ================================================================ 守护主体

class Keeper:
    def __init__(self, args):
        self.args = args
        self.port = args.port
        # 隧道**默认开**；只有显式 --no-tunnel 才关。
        # ⚠️ 这里曾经写成 `bool(args.tunnel)`，而 --tunnel 是 store_true（默认 False），
        #    于是"不写 --tunnel 就不开隧道" —— 与默认开的行为正好相反（实测踩到）。
        self.tunnel = not getattr(args, "no_tunnel", False)
        self.proc = None
        self.stop_flag = threading.Event()
        self.url = None
        self.url_utc = None
        self.restarts = 0
        self.started_utc = now_iso()
        self.log_path = None
        self.log_fh = None
        self.backoff = args.backoff_min
        self.exits = []                  # 最近若干次退出时间戳（判崩溃循环）

    # ---- 日志 ----
    def _open_log(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        rotate_logs()
        name = "demo-%s.log" % dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.log_path = os.path.join(LOG_DIR, name)
        self.log_fh = io.open(self.log_path, "a", encoding="utf-8", newline="\n")

    def log(self, msg):
        line = "[%s] %s" % (dt.datetime.now().strftime("%H:%M:%S"), msg)
        if self.log_fh:
            self.log_fh.write(line + "\n")
            self.log_fh.flush()
        print(line, flush=True)

    # ---- 状态 ----
    def write_status(self, state, **kw):
        st = read_json(STATUS_FILE, {}) or {}
        st.update({
            "state": state, "pid": os.getpid(), "port": self.port,
            "tunnel": self.tunnel, "url": self.url,
            "url_updated_utc": self.url_utc,
            "started_utc": self.started_utc,
            "restarts": self.restarts,
            "log": os.path.relpath(self.log_path, P2) if self.log_path else None,
            "updated_utc": now_iso(),
        })
        st.update(kw)
        write_json(STATUS_FILE, st)

    def write_url(self, url):
        os.makedirs(RUN_DIR, exist_ok=True)
        with io.open(URL_FILE, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(url + "\n")
        self.url, self.url_utc = url, now_iso()

    # ---- 子进程输出 ----
    def _pump(self, pipe):
        for raw in pipe:
            line = raw.rstrip("\n")
            if self.log_fh:
                self.log_fh.write("    | " + line + "\n")
                self.log_fh.flush()
            # 子进程的原样输出**不加前缀**：公网地址那一行要能直接复制
            print(line, flush=True)
            u = extract_url(line)
            if u and u != self.url:
                self.write_url(u)
                self.write_status("running")
                print("\n" + "=" * 74, flush=True)
                print("  ✅ 公网地址（已写入 %s）"
                      % os.path.relpath(URL_FILE, P2), flush=True)
                print("     %s" % u, flush=True)
                print("=" * 74 + "\n", flush=True)

    # ---- 起一次 ----
    def spawn(self):
        cmd = [sys.executable, "-u", os.path.join(P2, "run_p2.py"),
               "--port", str(self.port)]
        # 测试钩子：把子进程换成任意命令，用于在**没有杀进程权限**的环境里
        # 验证"退出 -> 退避 -> 重启 -> 崩溃循环告警"这一整条链路。
        #   例：set P2_KEEPALIVE_CHILD=python -c "import sys;sys.exit(3)"
        override = os.environ.get("P2_KEEPALIVE_CHILD")
        if override:
            import shlex
            # 用 posix 规则切分：Windows 的 non-posix 模式会把引号原样留在参数里
            cmd = shlex.split(override)
            self.tunnel = False          # 假子进程不会有隧道
        if self.tunnel:
            cmd.append("--tunnel")
            if self.args.cloudflared:
                cmd += ["--cloudflared", self.args.cloudflared]
        if self.args.host and not override:
            cmd += ["--host", self.args.host]
        self.log("启动子进程：%s" % " ".join(
            ('"%s"' % c if " " in c else c) for c in cmd))
        kw = {}
        if os.name != "nt":
            kw["start_new_session"] = True
        else:
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        # 🔴 必须强制子进程输出 UTF-8：它的 stdout 是**管道**，Python 在管道下
        #    会退回区域编码（中文 Windows 是 cp936），而这边按 UTF-8 解码 ——
        #    结果就是日志里全是乱码（实测踩到，中文全丢）。
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        self.proc = subprocess.Popen(
            cmd, cwd=P2, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
            errors="replace", bufsize=1, env=env, **kw)
        threading.Thread(target=self._pump, args=(self.proc.stdout,),
                         daemon=True).start()

    # ---- 主循环 ----
    def run(self):
        a = self.args
        # 单实例：PID 文件 + 端口检查（防止两份守护抢同一个端口）
        old = read_json(PID_FILE, {}) or {}
        if old.get("pid") and old["pid"] != os.getpid() and pid_alive(old["pid"]):
            print("已有保活实例在跑：PID %s（端口 %s）"
                  % (old["pid"], old.get("port")), flush=True)
            print("  看状态：python tools/keep_alive.py --status", flush=True)
            print("  先停掉：python tools/keep_alive.py --stop", flush=True)
            return 2
        if port_in_use(self.port):
            print("端口 %d 已被占用，且不是本守护起的 —— 拒绝启动。" % self.port,
                  flush=True)
            print("  （先确认那是什么：netstat -ano | findstr :%d）" % self.port,
                  flush=True)
            return 2

        os.makedirs(RUN_DIR, exist_ok=True)
        # 启动时先清一遍孤儿隧道（上次被强杀可能留下的）
        if self.tunnel:
            kill_orphan_cloudflared(self.port, verbose=True)
        # 上一轮的公网地址在本次启动后**一定**失效（quick tunnel 每次换域名）。
        # 留着它只会让人把死链接发出去，所以先清掉，等新地址出来再写。
        if self.tunnel:
            for p in (URL_FILE,):
                try:
                    os.remove(p)
                except OSError:
                    pass
        write_json(PID_FILE, {"pid": os.getpid(), "port": self.port,
                              "started_utc": self.started_utc})
        self._open_log()
        self.write_status("starting")

        print("=" * 74, flush=True)
        print("  项目二 · Demo 保活守护", flush=True)
        print("=" * 74, flush=True)
        print("  本机      http://127.0.0.1:%d" % self.port, flush=True)
        print("  公网      %s"
              % ("启动后会自动打印，并写入 %s" % os.path.relpath(URL_FILE, P2)
                 if self.tunnel else "未启用（--no-tunnel）"), flush=True)
        print("  日志      %s" % os.path.relpath(self.log_path, P2), flush=True)
        print("  停止      按 Ctrl+C，或另开窗口跑 "
              "python tools\\keep_alive.py --stop", flush=True)
        print("=" * 74, flush=True)

        try:
            while not self.stop_flag.is_set():
                self.spawn()
                t0 = time.time()
                rc = self.proc.wait()
                lived = time.time() - t0
                self.proc = None

                if self.stop_flag.is_set():
                    break
                self.restarts += 1
                now = time.time()
                self.exits = [x for x in self.exits if now - x < a.crash_window]
                self.exits.append(now)
                self.write_status("restarting", last_exit_code=rc,
                                  last_exit_utc=now_iso(), last_lived_s=round(lived, 1))
                self.log("子进程退出（exit=%s，存活 %.0f 秒）—— 第 %d 次重启"
                         % (rc, lived, self.restarts))
                # 子进程被强杀时它的 cloudflared 可能没死 —— 重启前先清掉，
                # 否则每重启一次就多挂一条隧道
                if self.tunnel:
                    n = kill_orphan_cloudflared(self.port, verbose=True)
                    if n:
                        self.log("已清理 %d 个孤儿隧道进程" % n)

                if len(self.exits) >= a.crash_max:
                    self.log("🔴 注意：%d 秒内已经退出 %d 次 —— 这不像偶发崩溃，"
                             "更像配置/端口/网络问题。日志：%s"
                             % (a.crash_window, len(self.exits),
                                os.path.relpath(self.log_path, P2)))

                if a.once:
                    self.log("--once：不重启，直接退出")
                    break

                if lived >= a.stable_seconds:
                    self.backoff = a.backoff_min       # 稳定跑过 -> 退避重置
                else:
                    self.backoff = backoff_next(self.backoff, a.backoff_min,
                                                a.backoff_max)
                self.log("%.0f 秒后重启（退避上限 %ds；Ctrl+C 可中止）"
                         % (self.backoff, a.backoff_max))
                # 退避期间也要能被 Ctrl+C 立刻打断
                if self.stop_flag.wait(self.backoff):
                    break
        except KeyboardInterrupt:
            pass
        finally:
            self.log("正在停止 ……")
            if self.proc:
                kill_tree(self.proc)
            self.write_status("stopped", stopped_utc=now_iso())
            for p in (PID_FILE,):
                try:
                    os.remove(p)
                except OSError:
                    pass
            if self.log_fh:
                self.log_fh.close()
            print("已停止（公网地址文件保留在 %s，内容可能已失效）"
                  % os.path.relpath(URL_FILE, P2), flush=True)
        return 0

    def stop(self):
        self.stop_flag.set()
        if self.proc:
            kill_tree(self.proc)


# ================================================================ status / stop

def cmd_status(as_json=False):
    st = read_json(STATUS_FILE, {}) or {}
    pid = st.get("pid")
    alive = pid_alive(pid) if pid else False
    url = None
    if os.path.exists(URL_FILE):
        with io.open(URL_FILE, encoding="utf-8") as fh:
            url = fh.read().strip() or None
    if as_json:
        print(json.dumps(dict(st, keeper_alive=alive, url_file=url),
                         ensure_ascii=False, indent=1))
        return 0 if alive else 1
    print("=" * 74)
    print("项目二 · Demo 状态")
    print("=" * 74)
    if not st:
        print("  没有找到状态文件（%s）—— 说明保活守护没跑过。"
              % os.path.relpath(STATUS_FILE, P2))
        return 1
    print("  守护进程   %s（PID %s，端口 %s）"
          % ("运行中" if alive else "**已停止**", pid, st.get("port")))
    print("  状态       %s" % st.get("state"))
    print("  已重启     %s 次" % st.get("restarts"))
    print("  启动于     %s" % st.get("started_utc"))
    if st.get("last_exit_utc"):
        print("  上次退出   exit=%s（存活 %ss，%s）"
              % (st.get("last_exit_code"), st.get("last_lived_s"),
                 st.get("last_exit_utc")))
    print("  公网地址   %s" % (url or "（还没有；隧道可能仍在建立）"))
    print("  地址写入   %s" % (st.get("url_updated_utc") or "—"))
    print("  日志       %s" % (st.get("log") or "—"))
    if url:
        print()
        print("  打开：%s" % url)
    if not alive:
        print("\n  ⚠️ 守护进程不在跑 —— 上面那个公网地址**很可能已经失效**。")
        print("     重新起：双击 启动Demo.bat（或 python tools\\keep_alive.py）")
    return 0 if alive else 1


def cmd_stop():
    st = read_json(PID_FILE, {}) or {}
    pid = st.get("pid")
    if not pid:
        print("没有找到 PID 文件（%s）—— 没有在保活的实例。"
              % os.path.relpath(PID_FILE, P2))
        return 1
    if not pid_alive(pid):
        print("PID %s 已经不在跑了，清理残留的 PID 文件。" % pid)
        try:
            os.remove(PID_FILE)
        except OSError:
            pass
        return 0
    print("正在停止保活守护 PID %s（含子进程树）…" % pid)
    if os.name == "nt":
        r = subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            # 兜底：直接对该 PID 发终止（Windows 上 os.kill 走 TerminateProcess）
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError as exc:
                print("  taskkill 失败（%s），os.kill 也失败：%r"
                      % ((r.stderr or "").strip()[:60], exc))
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            print("停不掉：%r" % (exc,))
            return 1
    for _ in range(20):
        if not pid_alive(pid):
            break
        time.sleep(0.5)
    ok = not pid_alive(pid)
    print("已停止" if ok else "**没停掉**，请手动处理 PID %s" % pid)
    if ok:
        try:
            os.remove(PID_FILE)
        except OSError:
            pass
    return 0 if ok else 1


# ================================================================ 自检

def selftest():
    ok = True

    def chk(cond, msg):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s" % ("OK " if cond else "!! ", msg))

    # ---- ① 公网地址解析：必须只认真正的 URL ----
    #    前两行是 cloudflared **真实输出**的两种形态（方框里带 URL / 直接带 URL）
    good = [
        '2026-09-19T15:31:22Z INF |  https://cancel-ampland-des-gasoline.trycloudflare.com  |',
        'INF Your quick Tunnel has been created! Visit it at https://abc-def-123.trycloudflare.com',
    ]
    #    下面这些**不能**被当成地址 —— 中间那条正是我踩过的坑：
    #    程序自己打印的提示语里也含这个域名，早期解析把它当成地址打了出来
    bad = [
        '+--------------------------------------------------------------------------------------------+',
        'INF trycloudflare.com is the domain used for quick tunnels',
        '    （临时链接，进程停即失效；见 docs/43-公网可访问.md）',
    ]
    got = [extract_url(x) for x in good]
    chk(all(g and g.startswith("https://") and g.endswith("trycloudflare.com")
            for g in got), "两种真实日志形态都能抽出地址（%s）" % (got[0] or "-"))
    chk(got[0] == "https://cancel-ampland-des-gasoline.trycloudflare.com"
        and got[1] == "https://abc-def-123.trycloudflare.com",
        "抽出来的是**完整**地址，不是域名片段")
    chk(all(extract_url(x) is None for x in bad),
        "不会把日志里的域名片段/方框线误当成地址（这是踩过的坑）")
    chk(extract_url("https://x.TRYCLOUDFLARE.COM") is None,
        "大写域名不误判（cloudflared 只输出小写，宁可漏也不要错）")

    # ---- ② 退避：单调上升、封顶、稳定后重置 ----
    b, seq = 5, []
    for _ in range(6):
        b = backoff_next(b, 5, 60)
        seq.append(b)
    chk(seq == [10, 20, 40, 60, 60, 60], "退避序列正确（%s）" % seq)
    chk(backoff_next(0, 5, 60) == 5, "首次用的是下限，不是 0")

    # ---- ②b 隧道的默认值：不写参数时**必须**开（这是踩过的坑）----
    parser = build_parser()
    for argv2, want in ((["--port", "8788"], True),
                        (["--port", "8788", "--tunnel"], True),
                        (["--port", "8788", "--no-tunnel"], False)):
        ns = parser.parse_args(argv2)
        chk(bool(Keeper(ns).tunnel) == want,
            "隧道默认值正确：`%s` -> tunnel=%s" % (" ".join(argv2), want))

    # ---- ③ PID / status / url 文件的读写往返 ----
    tmp = os.path.join(P2, "_tmp_keeptest")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    global PID_FILE, STATUS_FILE, URL_FILE
    keep = (PID_FILE, STATUS_FILE, URL_FILE)
    PID_FILE = os.path.join(tmp, "demo.pid")
    STATUS_FILE = os.path.join(tmp, "status.json")
    URL_FILE = os.path.join(tmp, "public_url.txt")
    try:
        write_json(PID_FILE, {"pid": os.getpid(), "port": 8788})
        chk(read_json(PID_FILE, {}).get("port") == 8788, "PID 文件读写往返")
        chk(pid_alive(os.getpid()), "能认出**自己**这个 PID 是活的")
        chk(not pid_alive(999999), "明显不存在的 PID 判为不活")
        k = Keeper(argparse.Namespace(port=8788, host=None, tunnel=True,
                                      cloudflared=None, backoff_min=5,
                                      backoff_max=60, crash_window=300,
                                      crash_max=5, once=True,
                                      stable_seconds=120))
        k.write_url("https://unit-test.trycloudflare.com")
        chk(os.path.exists(URL_FILE), "公网地址写进了固定文件")
        with io.open(URL_FILE, encoding="utf-8") as fh:
            chk(fh.read().strip() == "https://unit-test.trycloudflare.com",
                "地址文件内容正确（一行纯文本，方便脚本读）")
        k.write_status("running", restarts=3)
        st = read_json(STATUS_FILE, {})
        chk(st.get("url") == "https://unit-test.trycloudflare.com"
            and st.get("restarts") == 3 and st.get("port") == 8788,
            "status.json 字段完整（state/pid/port/url/restarts）")
    finally:
        PID_FILE, STATUS_FILE, URL_FILE = keep
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- ④ 关键行为：必须能连子进程树一起杀 ----
    import inspect
    src = inspect.getsource(kill_tree)
    chk("taskkill" in src and "/T" in src,
        "Windows 上杀进程树（否则会留下孤儿 cloudflared）")

    print("\n保活守护自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


# ================================================================ CLI

def build_parser():
    """参数表单独抽出来 —— 自检要能**按真实参数解析**去验证默认值。"""
    ap = argparse.ArgumentParser(
        description="Demo 保活守护（自动重启 + 公网地址固定落盘）")
    ap.add_argument("--run-dir", default=None,
                    help="状态/日志目录（默认 data/run）。"
                         "多实例或隔离测试时用，避免互相覆盖 PID/状态/地址文件")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("PORT") or 8788))
    ap.add_argument("--host", default=None)
    ap.add_argument("--tunnel", action="store_true",
                    help="起公网隧道（**默认就起**；这个参数只是为了让命令行"
                         "和 run_p2.py 的用法一致，写不写都一样）")
    ap.add_argument("--no-tunnel", action="store_true", help="不起公网隧道")
    ap.add_argument("--cloudflared", default=None)
    ap.add_argument("--backoff-min", type=int, default=5)
    ap.add_argument("--backoff-max", type=int, default=60)
    ap.add_argument("--stable-seconds", type=int, default=120,
                    help="子进程活过这么久就算『稳定』，退避重置")
    ap.add_argument("--crash-window", type=int, default=300)
    ap.add_argument("--crash-max", type=int, default=5,
                    help="窗口内退出这么多次就明确报警（仍会继续重启）")
    ap.add_argument("--once", action="store_true", help="只跑一次，不重启")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.run_dir:
        # 隔离运行目录：PID/状态/地址/日志都换地方，实例之间互不干扰
        global RUN_DIR, LOG_DIR, PID_FILE, STATUS_FILE, URL_FILE
        RUN_DIR = os.path.abspath(args.run_dir)
        LOG_DIR = os.path.join(RUN_DIR, "logs")
        PID_FILE = os.path.join(RUN_DIR, "demo.pid")
        STATUS_FILE = os.path.join(RUN_DIR, "status.json")
        URL_FILE = os.path.join(RUN_DIR, "public_url.txt")

    if args.selftest:
        print("=" * 74)
        print("保活守护自检（地址解析 / 退避 / 状态文件 / 杀进程树）")
        print("=" * 74)
        return selftest()
    if args.status:
        return cmd_status(args.json)
    if args.stop:
        return cmd_stop()

    k = Keeper(args)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: k.stop())
        except (ValueError, OSError):
            pass
    return k.run()


if __name__ == "__main__":
    sys.exit(main())
