#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
长跑记录器（连续运行 → 稳定性与效率的**实测证据**）
======================================================

为什么需要它
------------
自检是"跑一次、全绿"的**点**证据；但"稳定"和"效率"是**时间维度**的性质：
  · 连续跑 24 小时，成功率是多少？有没有内存缓慢增长？
  · 决策耗时分布（中位/95 分位）稳不稳？
  · LLM 到底调了多少次、有没有真的复用缓存（省下钱）？
  · 保活守护重启了几次、每次因为什么？
  · 复跑契约**每一次**都成立吗，还是偶尔会漂？

这些只能靠**持续记录**回答。本工具每 N 秒跑一轮真实决策，把结果追加到
`data/run/record.jsonl`（JSONL，一行一轮，断电也不丢前面的），并给出汇总。

用法
----
  python tools/run_record.py                    # 长跑（默认 5 分钟一轮）
  python tools/run_record.py --interval 60      # 60 秒一轮
  python tools/run_record.py --rounds 3         # 只跑 3 轮（调试）
  python tools/run_record.py --poll-news        # 每轮顺带刷新消息面（让事件驱动降本生效）
  python tools/run_record.py --replay-every 5   # 每 5 轮做一次契约复跑校验
  python tools/run_record.py --report           # 看汇总（含可粘贴进材料的 markdown）
  python tools/run_record.py --selftest         # 离线自检
  python tools/run_record.py --stop             # 停掉正在跑的长跑（读 PID 文件）

🔴 诚实边界：记录器**不会**替你把"跑够 24 小时"这件事变成真的 ——
   它只负责如实积累。跑得越久，结论越硬。
"""

import argparse
import datetime as dt
import hashlib
import io
import json
import os
import signal
import statistics
import subprocess
import sys
import time

P2 = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, P2)
sys.path.insert(0, os.path.join(P2, "project2"))
sys.path.insert(0, os.path.join(P2, "tools"))
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

RUN_DIR = os.path.join(P2, "data", "run")
RECORD = os.path.join(RUN_DIR, "record.jsonl")
PID_FILE = os.path.join(RUN_DIR, "record.pid")
BASES = ["NVDA", "META", "TSLA", "AAPL", "GOOGL", "SPY", "QQQ", "SOXL", "HOOD",
         "MRVL"]


def now_iso():
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def _rss_mb():
    """当前进程常驻内存（MB）。只用于看"有没有缓慢增长"。"""
    try:
        import ctypes
        from ctypes import wintypes
        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        h = ctypes.windll.kernel32.GetCurrentProcess()
        if ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.c_void_p(h),
                                                    ctypes.byref(pmc), pmc.cb):
            return round(pmc.WorkingSetSize / 1048576.0, 1)
    except Exception:  # noqa: BLE001
        pass
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            for ln in fh:
                if ln.startswith("VmRSS:"):
                    return round(int(ln.split()[1]) / 1024.0, 1)
    except OSError:
        pass
    return None


def keeper_state():
    """顺带记录保活守护的状态（重启次数是"稳定性"的直接证据）。"""
    try:
        with io.open(os.path.join(RUN_DIR, "status.json"), encoding="utf-8") as fh:
            st = json.load(fh)
        return {"keeper_state": st.get("state"),
                "keeper_restarts": st.get("restarts"),
                "keeper_alive": bool(st.get("pid"))}
    except (OSError, json.JSONDecodeError):
        return {"keeper_state": None, "keeper_restarts": None,
                "keeper_alive": False}


def _src_files():
    files = [os.path.join(P2, "run_p2.py")]
    for d in ("project2", "tools"):
        try:
            for fn in sorted(os.listdir(os.path.join(P2, d))):
                if fn.endswith(".py"):
                    files.append(os.path.join(P2, d, fn))
        except OSError:
            pass
    return files


def _src_signature():
    """廉价签名（每个文件的 路径/大小/mtime），用来判断"代码有没有变过"。

    为什么不每轮重算哈希：一轮要读 20+ 个文件，虽然不大，但记录器是**连续跑几天**
    的；用 stat 判"没变就复用上次的哈希"既便宜又不会漏 —— mtime 变了就一定重算。
    """
    sig = []
    for p in _src_files():
        try:
            st = os.stat(p)
            sig.append((os.path.basename(p), st.st_size, int(st.st_mtime_ns)))
        except OSError:
            continue
    return tuple(sorted(sig))


def code_fingerprint():
    """**代码指纹** —— 决定这次记录属于哪一版实现。

    ⚠️ 为什么必须有它：长跑记录是"稳定性/效率"的证据来源，而代码在跑的过程中
       **会改**。没有指纹时，把改动前后的轮次混在一起算"成功率 100%"是
       **无法归因**的 —— 读者没法知道这 100% 是哪一版跑出来的（本仓库实测
       踩到：412 轮记录横跨了执行进度官接入前后的两版代码）。
    指纹取关键源文件的 SHA-256 前 16 位 + git 短提交号；文件读不到就如实记 None。

    🔴 **必须每轮都能反映当前代码**（这条是被自己的记录打脸才改的）：
       初版把指纹缓存在进程级（`_CODE_FP`，一次进程只算一次）。于是记录器跑了
       几小时、中途代码改了 3 次，它给后面 123 轮**全都盖了启动那一刻的指纹** ——
       这比没有指纹**更糟**：不是"没标注"，而是**自信地标错**。
       现在按 `_src_signature()`（大小+mtime）判断，变了就重算。
    """
    global _CODE_FP, _CODE_SIG
    sig = _src_signature()
    if _CODE_FP is not None and sig == _CODE_SIG:
        return _CODE_FP

    h = hashlib.sha256()
    n = 0
    for p in _src_files():
        try:
            with open(p, "rb") as fh:
                h.update(fh.read())
            n += 1
        except OSError:
            continue
    git = None
    dirty = None
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=P2,
                           capture_output=True, timeout=10)
        if r.returncode == 0:
            git = r.stdout.decode("utf-8", "replace").strip() or None
        # ⚠️ `git` 只是**当时的最后一次提交**，而工作区完全可能领先于它
        #    （本轮实测就是：源码改了还没提交，git 指向上一版）。
        #    所以必须同时记 dirty —— 否则读者会以为 src_sha16 就是那个提交的内容。
        #    **权威标识是 `src_sha16`**（它哈希的是工作区里的真实文件），
        #    `git` + `dirty` 只是给人看的位置线索。
        r2 = subprocess.run(["git", "status", "--porcelain"], cwd=P2,
                            capture_output=True, timeout=10)
        if r2.returncode == 0:
            dirty = bool(r2.stdout.decode("utf-8", "replace").strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return {"src_sha16": h.hexdigest()[:16], "n_files": n, "git": git,
            "git_dirty": dirty}


# 缓存"上一轮算指纹时源文件长什么样"：签名没变就复用，变了就重算。
# ⚠️ 缓存的是**与签名绑定**的结果，不是"进程启动那一刻"的结果（见 code_fingerprint 的注释）。
_CODE_FP = None
_CODE_SIG = None


def code_fp():
    """每轮都调；只有源文件真的变了才会重新算哈希。"""
    global _CODE_FP, _CODE_SIG
    fp = code_fingerprint()
    _CODE_FP, _CODE_SIG = fp, _src_signature()
    return fp


# ---------------------------------------------------------------- 一轮

def one_round(base, *, replay=False, poll_news=False, qty=5000.0):
    """跑一轮真实决策并**如实**记录。任何异常都记成 ok=False，不抛。"""
    rec = {"ts": now_iso(), "base": base, "qty_usd": qty,
           "pid": os.getpid(), "rss_mb": _rss_mb(), "ok": False, "err": None,
           "replay_checked": False, "replay_ok": None,
           "news_polled": False}
    rec.update(code_fp())
    rec.update(keeper_state())

    if poll_news:
        # 让"事件驱动降本"有条件生效：刷新消息面快照（新条目才会触发 LLM）。
        t0 = time.time()
        try:
            r = subprocess.run([sys.executable,
                                os.path.join(P2, "tools", "news_sources.py"),
                                "--base", base, "--event-driven", "--save"],
                               cwd=P2, capture_output=True, timeout=120)
            rec["news_polled"] = (r.returncode == 0)
        except Exception as exc:  # noqa: BLE001
            rec["news_err"] = type(exc).__name__
        rec["news_ms"] = int((time.time() - t0) * 1000)

    t0 = time.time()
    try:
        import agent_team as at
        cost, items, debate, dec, book = at.run_decision(base, qty_usd=qty)
        rec["duration_ms"] = int((time.time() - t0) * 1000)
        gm = dec.get("gate_merge") or {}
        llm = gm.get("llm") or {}
        risk = dec.get("risk") or {}
        rec.update({
            "ok": True,
            "n_analysts_valid": sum(1 for i in items if i.get("valid")),
            "gate_severity": (gm.get("effective") or {}).get("severity"),
            "gate_source": (gm.get("effective") or {}).get("source"),
            "static_severity": (gm.get("static") or {}).get("severity"),
            "llm_severity": llm.get("severity"),
            "llm_source": llm.get("source"),
            "llm_cache_reused": bool(llm.get("cache_reused")),
            "llm_cache_age_min": llm.get("cache_age_min"),
            "risk_verdict": risk.get("verdict"),
            "risk_hits": risk.get("hits") or [],
            "rules_checked": risk.get("checked_rules")
                             or len(risk.get("rules") or []),
            "n_hypotheses": len(dec.get("risk_hypotheses") or []),
            "final_stance": (dec.get("final") or {}).get("stance"),
            "final_qty_usd": (dec.get("final") or {}).get("qty_usd"),
            "monotonic_ok": bool((dec.get("monotonic") or {})
                                 .get("stance_non_increasing")
                                 and (dec.get("monotonic") or {})
                                 .get("qty_non_increasing")),
        })
        if replay:
            # 契约复跑：用**本次日志的参数 + 冻结的 LLM 判定**重跑一遍再比对。
            rec["replay_checked"] = True
            try:
                log = at.build_log(base=base, items=items, debate=debate,
                                   cost=cost, decision=dec, qty_usd=qty,
                                   miss_bp=3.0, urgent=False, now_ms=None,
                                   book=book)
                pr = log["parameters"]
                c2, i2, d2, dec2, b2 = at.run_decision(
                    base, qty_usd=float(pr["qty_usd"]),
                    miss_bp=float(pr["miss_bp"]), urgent=bool(pr["urgent"]),
                    now_ms=pr.get("now_ms"), llm_event=pr.get("llm_event"),
                    order_state=pr.get("order_state"))
                log2 = at.build_log(base=base, items=i2, debate=d2, cost=c2,
                                    decision=dec2, qty_usd=float(pr["qty_usd"]),
                                    miss_bp=float(pr["miss_bp"]),
                                    urgent=bool(pr["urgent"]),
                                    now_ms=pr.get("now_ms"), book=b2)
                ok_r, rep = at.replay_check(log, log2)
                rec["replay_ok"] = bool(ok_r)
                if not ok_r:
                    rec["replay_diff"] = rep.get("must_match_failed")
            except Exception as exc:  # noqa: BLE001
                rec["replay_ok"] = False
                rec["replay_err"] = repr(exc)[:80]
        rec["decision_hash"] = None
        try:
            rec["decision_hash"] = at.build_log(
                base=base, items=items, debate=debate, cost=cost, decision=dec,
                qty_usd=qty, miss_bp=3.0, urgent=False, now_ms=None,
                book=book).get("decision_hash", "")[:16]
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        rec["duration_ms"] = int((time.time() - t0) * 1000)
        rec["err"] = "%s: %s" % (type(exc).__name__, str(exc)[:160])
    return rec


def append(rec):
    os.makedirs(RUN_DIR, exist_ok=True)
    with io.open(RECORD, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_records():
    out = []
    try:
        with io.open(RECORD, encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    out.append(json.loads(ln))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out


# ---------------------------------------------------------------- 汇总

def summarize(recs):
    """把记录压成结论。**纯函数**，便于自检。"""
    n = len(recs)
    if not n:
        return {"rounds": 0}
    ok = [r for r in recs if r.get("ok")]
    durs = sorted(r["duration_ms"] for r in ok if r.get("duration_ms"))
    ts = [r["ts"] for r in recs if r.get("ts")]

    def pct(p):
        if not durs:
            return None
        k = max(0, min(len(durs) - 1, int(round((len(durs) - 1) * p))))
        return durs[k]

    llm_calls = sum(1 for r in ok if str(r.get("llm_source") or "") == "llm")
    llm_cached = sum(1 for r in ok if r.get("llm_cache_reused"))
    replay = [r for r in recs if r.get("replay_checked")]
    restarts = [r.get("keeper_restarts") for r in recs
                if r.get("keeper_restarts") is not None]
    rss = [r["rss_mb"] for r in recs if r.get("rss_mb")]
    # 🔴 按**代码指纹**分组：不同版本的轮次不能混在一起算成功率。
    #    没有指纹的旧记录单独成组（如实标注"旧记录"，不假装也是当前版本）。
    vers = {}
    for r in recs:
        k = r.get("src_sha16") or "(无指纹·旧记录)"
        v = vers.setdefault(k, {"src_sha16": k, "git": r.get("git"),
                                "git_dirty": r.get("git_dirty"),
                                "rounds": 0, "ok": 0, "from": None, "to": None})
        v["rounds"] += 1
        v["ok"] += 1 if r.get("ok") else 0
        ts0 = r.get("ts")
        if ts0:
            v["from"] = min(v["from"], ts0) if v["from"] else ts0
            v["to"] = max(v["to"], ts0) if v["to"] else ts0
    for v in vers.values():
        v["success_rate"] = round(100.0 * v["ok"] / v["rounds"], 1)
    version_list = sorted(vers.values(), key=lambda x: (x["to"] or ""),
                          reverse=True)
    cur = next((v for v in version_list if v["src_sha16"] not in
                ("(无指纹·旧记录)",)), None)
    return {
        "rounds": n,
        "from": ts[0] if ts else None,
        "to": ts[-1] if ts else None,
        "ok": len(ok),
        "fail": n - len(ok),
        "success_rate": round(100.0 * len(ok) / n, 1),
        "duration_ms_p50": pct(0.50),
        "duration_ms_p95": pct(0.95),
        "duration_ms_max": durs[-1] if durs else None,
        "llm_calls": llm_calls,
        "llm_cached_reuse": llm_cached,
        "llm_cache_rate": (round(100.0 * llm_cached / (llm_calls + llm_cached), 1)
                           if (llm_calls + llm_cached) else None),
        "replay_checks": len(replay),
        "replay_ok": sum(1 for r in replay if r.get("replay_ok")),
        "monotonic_violations": sum(1 for r in ok if r.get("monotonic_ok") is False),
        "keeper_restarts": (max(restarts) - min(restarts)) if len(restarts) > 1
                           else (0 if restarts else None),
        "rss_mb_first": rss[0] if rss else None,
        "rss_mb_last": rss[-1] if rss else None,
        "gate_sources": sorted({r.get("gate_source") for r in ok
                                if r.get("gate_source")}),
        "final_stances": sorted({r.get("final_stance") for r in ok
                                 if r.get("final_stance")}),
        "errors": sorted({r["err"] for r in recs if r.get("err")})[:5],
        "replay_diffs": sorted({tuple(r.get("replay_diff") or [])
                                for r in replay if r.get("replay_ok") is False}),
        "versions": version_list,
        "current_version": (cur or {}).get("src_sha16"),
    }


def render_report(s):
    L = ["=" * 78, "长跑记录汇总", "=" * 78]
    if not s.get("rounds"):
        return "\n".join(L + ["  （还没有记录 —— 先跑 python tools/run_record.py）"])
    L += [
        "  轮数            %s（成功 %s / 失败 %s，成功率 %s%%）"
        % (s["rounds"], s["ok"], s["fail"], s["success_rate"]),
        "  时间段          %s  →  %s" % (s["from"], s["to"]),
        "  决策耗时        p50 %s ms ｜ p95 %s ms ｜ 最慢 %s ms"
        % (s["duration_ms_p50"], s["duration_ms_p95"], s["duration_ms_max"]),
        "  LLM 调用        %s 次 ｜ 复用缓存 %s 次（复用率 %s%%）"
        % (s["llm_calls"], s["llm_cached_reuse"], s["llm_cache_rate"]),
        "  契约复跑        %s 次，通过 %s 次%s"
        % (s["replay_checks"], s["replay_ok"],
           "" if s["replay_checks"] == s["replay_ok"] else "  ← **有漂移**"),
        "  单调性违背      %s 次（必须为 0）" % s["monotonic_violations"],
        "  保活重启        %s 次" % (s["keeper_restarts"],),
        "  内存            %s MB → %s MB（看有没有缓慢增长）"
        % (s["rss_mb_first"], s["rss_mb_last"]),
        "  闸门来源        %s" % "、".join(s["gate_sources"] or []),
        "  最终立场        %s" % "、".join(s["final_stances"] or []),
    ]
    if s["errors"]:
        L.append("  失败样本        %s" % "；".join(s["errors"]))
    if s["replay_diffs"]:
        L.append("  复跑差异字段    %s" % s["replay_diffs"])
    # 🔴 代码版本：把"这段记录是哪一版跑的"摊开说。
    vs = s.get("versions") or []
    if len(vs) > 1:
        L.append("  代码版本        **%d 个**（下面的总成功率是**混合**口径，"
                 "不能归因到单一版本）" % len(vs))
    for v in vs[:4]:
        L.append("    · %s%s%s  %s 轮 ｜ 成功 %s%% ｜ %s → %s"
                 % (v["src_sha16"],
                    ("（git %s%s）" % (v["git"],
                                       "＋未提交改动" if v.get("git_dirty") else ""))
                    if v.get("git") else "",
                    "",
                    v["rounds"], v["success_rate"],
                    (v["from"] or "")[:19], (v["to"] or "")[:19]))
    L += ["", "  ⚠️ 记录的时长决定结论强度：跑得越久越硬。这里只有如实积累，没有估算。"]
    return "\n".join(L)


def render_md(s):
    if not s.get("rounds"):
        return "（尚无长跑记录）"
    return "\n".join([
        "| 长跑指标 | 实测 |",
        "|---|---|",
        "| 轮数 / 成功率 | %s 轮 ｜ **%s%%**（失败 %s） |"
        % (s["rounds"], s["success_rate"], s["fail"]),
        "| 时间段 | %s → %s |" % (s["from"], s["to"]),
        "| 决策耗时 | p50 **%s ms** ｜ p95 %s ms |"
        % (s["duration_ms_p50"], s["duration_ms_p95"]),
        "| LLM 调用 / 缓存复用 | %s 次 ｜ %s 次（%s%%） |"
        % (s["llm_calls"], s["llm_cached_reuse"], s["llm_cache_rate"]),
        "| 契约复跑通过 | %s / %s |" % (s["replay_ok"], s["replay_checks"]),
        "| 单调性违背 | %s（必须 0） |" % s["monotonic_violations"],
        "| 保活重启 | %s 次 |" % s["keeper_restarts"],
        "| 内存 | %s → %s MB |" % (s["rss_mb_first"], s["rss_mb_last"]),
        "| 代码版本 | %s |" % ("、".join(
            "%s（%s 轮）" % (v["src_sha16"], v["rounds"])
            for v in (s.get("versions") or [])[:3]) or "—"),
    ])


# ---------------------------------------------------------------- 自检

def selftest():
    ok = True

    def chk(cond, msg):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s" % ("OK " if cond else "!! ", msg))

    # 用合成记录验证汇总逻辑（不碰真实数据、不调网络）
    fake = [
        {"ts": "t0", "ok": True, "duration_ms": 100, "llm_source": "llm",
         "gate_source": "llm+static", "final_stance": "stand_down",
         "monotonic_ok": True, "keeper_restarts": 0, "rss_mb": 30.0},
        {"ts": "t1", "ok": True, "duration_ms": 300, "llm_source": "llm",
         "llm_cache_reused": True, "gate_source": "llm+static",
         "final_stance": "stand_down", "monotonic_ok": True,
         "keeper_restarts": 1, "rss_mb": 31.0,
         "replay_checked": True, "replay_ok": True},
        {"ts": "t2", "ok": False, "err": "ValueError: x",
         "duration_ms": 5, "keeper_restarts": 1, "rss_mb": 31.5},
    ]
    s = summarize(fake)
    chk(s["rounds"] == 3 and s["ok"] == 2 and s["success_rate"] == 66.7,
        "成功率统计正确（%s%%）" % s["success_rate"])
    chk(s["duration_ms_p50"] == 100 and s["duration_ms_max"] == 300,
        "耗时分位统计正确（p50=%s p95=%s）" % (s["duration_ms_p50"],
                                               s["duration_ms_p95"]))
    chk(s["llm_calls"] == 2 and s["llm_cached_reuse"] == 1,
        "LLM 调用/缓存分别计数（%s / %s）" % (s["llm_calls"],
                                              s["llm_cached_reuse"]))
    chk(s["keeper_restarts"] == 1, "保活重启按差值统计（%s）" % s["keeper_restarts"])
    chk(s["replay_checks"] == 1 and s["replay_ok"] == 1, "复跑校验计数")
    chk(s["monotonic_violations"] == 0, "单调性违背计数")
    chk(summarize([])["rounds"] == 0, "空记录不炸")
    chk("长跑记录汇总" in render_report(s), "报告可渲染")
    chk("| 长跑指标 | 实测 |" in render_md(s), "markdown 可渲染")

    # 记录格式：必须**一行一条 JSON**（断电不丢前面的）
    import tempfile
    tmp = os.path.join(P2, "_tmp_recordtest.jsonl")
    try:
        io.open(tmp, "w", encoding="utf-8").close()
        with io.open(tmp, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps({"a": 1}, ensure_ascii=False) + "\n")
            fh.write(json.dumps({"a": 2}, ensure_ascii=False) + "\n")
            fh.write("{坏行\n")
            fh.write(json.dumps({"a": 3}, ensure_ascii=False) + "\n")
        got = []
        with io.open(tmp, encoding="utf-8") as fh:
            for ln in fh:
                try:
                    got.append(json.loads(ln))
                except json.JSONDecodeError:
                    pass
        chk(len(got) == 3, "坏行会被跳过、不影响已记录的行（%d 条）" % len(got))
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    # 🔴 代码指纹必须**跟着代码变**，不能是"进程启动那一刻"的快照。
    #    这条是拿自己的记录打脸换来的：初版缓存在进程级，记录器跑了 3 小时、
    #    中途代码改了 3 次，它给后面 123 轮全盖了启动时的指纹 —— 比没有指纹更糟
    #    （不是"没标注"，而是**自信地标错**）。这里用**打桩签名**验证缓存按签名失效。
    _orig_sig, _orig_fp, _orig_s = _src_signature, _CODE_FP, _CODE_SIG
    try:
        globals()["_src_signature"] = lambda: (("a.py", 1, 1),)
        globals()["_CODE_FP"] = globals()["_CODE_SIG"] = None
        code_fp()
        chk(_CODE_SIG == (("a.py", 1, 1),),
            "指纹缓存记下了当时的源文件签名（而不是进程启动时间）")
        globals()["_src_signature"] = lambda: (("a.py", 1, 2),)
        code_fp()
        chk(_CODE_SIG == (("a.py", 1, 2),),
            "源文件签名一变就重算并更新缓存 -> 长跑中途改代码**不会**被盖上旧指纹")
    finally:
        # 复原（`selftest()` 是模块级函数，globals() 就是模块命名空间）
        globals()["_src_signature"] = _orig_sig
        globals()["_CODE_FP"] = _orig_fp
        globals()["_CODE_SIG"] = _orig_s

    print("\n长跑记录器自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


# ---------------------------------------------------------------- 主流程

def run_loop(args):
    # 单实例
    if os.path.exists(PID_FILE):
        try:
            with io.open(PID_FILE, encoding="utf-8") as fh:
                old = json.load(fh)
            if old.get("pid") and old["pid"] != os.getpid():
                import keep_alive as ka
                if ka.pid_alive(old["pid"]):
                    print("已经有一个长跑记录器在跑（PID %s）。先 --stop。" % old["pid"])
                    return 2
        except (OSError, json.JSONDecodeError):
            pass
    os.makedirs(RUN_DIR, exist_ok=True)
    with io.open(PID_FILE, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"pid": os.getpid(), "started": now_iso()}, fh)

    stop = {"v": False}

    def _sig(*_a):
        stop["v"] = True
    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(s, _sig)
        except (ValueError, OSError):
            pass

    print("=" * 78)
    print("  项目二 · 长跑记录器")
    print("=" * 78)
    print("  间隔      %s 秒 ｜ 标的轮转 %d 个 ｜ 刷新消息面 %s"
          % (args.interval, len(BASES), "开" if args.poll_news else "关"))
    print("  记录      %s" % os.path.relpath(RECORD, P2))
    print("  复跑校验  每 %d 轮一次" % args.replay_every)
    print("  Ctrl+C 停止（或在另一个窗口 --stop）")
    print("=" * 78, flush=True)

    i, fails = 0, 0
    try:
        while not stop["v"] and (args.rounds <= 0 or i < args.rounds):
            base = BASES[i % len(BASES)]
            rec = one_round(base,
                            replay=(args.replay_every > 0
                                    and i % args.replay_every == 0),
                            poll_news=args.poll_news, qty=args.qty)
            append(rec)
            i += 1
            fails += 0 if rec.get("ok") else 1
            print("  [%s] #%-4d %-6s %5s ms  %-10s 闸门 %-6s(%s)  风控 %-7s %s"
                  % (rec["ts"][11:19], i, base, rec.get("duration_ms"),
                     rec.get("final_stance") or "-",
                     rec.get("gate_severity") or "-",
                     rec.get("gate_source") or "-",
                     rec.get("risk_verdict") or "-",
                     "".join(rec.get("risk_hits") or [])[:40]),
                  flush=True)
            if not rec.get("ok"):
                print("        **失败**：%s" % rec.get("err"), flush=True)
            if stop["v"] or (args.rounds > 0 and i >= args.rounds):
                break
            # 分片睡眠，Ctrl+C 能立刻响应
            for _ in range(args.interval):
                if stop["v"]:
                    break
                time.sleep(1)
    finally:
        try:
            os.remove(PID_FILE)
        except OSError:
            pass
    print("\n长跑记录器已停止：本次 %d 轮，失败 %d 轮" % (i, fails), flush=True)
    print(render_report(summarize(load_records())), flush=True)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="长跑记录器（稳定性与效率的实测证据）")
    ap.add_argument("--interval", type=int, default=300, help="轮间隔（秒）")
    ap.add_argument("--rounds", type=int, default=0, help="跑多少轮（0=一直跑）")
    ap.add_argument("--qty", type=float, default=5000.0)
    ap.add_argument("--poll-news", action="store_true",
                    help="每轮刷新消息面快照（让事件驱动降本有条件生效）")
    ap.add_argument("--replay-every", type=int, default=5,
                    help="每 N 轮做一次契约复跑校验（0=不做）")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--md", action="store_true", help="--report 时输出 markdown 表")
    ap.add_argument("--out", default=None,
                    help="--report 时把报告落盘（相对仓库根；建议 "
                         "data/derived/run_record_report.md）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--stop", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        print("=" * 78)
        print("长跑记录器自检（汇总统计 / 记录格式 / 空与坏数据）")
        print("=" * 78)
        return selftest()
    if args.stop:
        if not os.path.exists(PID_FILE):
            print("没有在跑的长跑记录器（%s 不存在）"
                  % os.path.relpath(PID_FILE, P2))
            return 1
        with io.open(PID_FILE, encoding="utf-8") as fh:
            pid = (json.load(fh) or {}).get("pid")
        print("停止长跑记录器 PID %s …" % pid)
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            print("停不掉：%r（可手动结束进程）" % (exc,))
            return 1
        return 0
    if args.report:
        s = summarize(load_records())
        # ⚠️ 原始记录（data/run/record.jsonl）是**运行时产物**，被 .gitignore 排除
        #    （它在持续追加，不适合进仓库）。但材料里引用的复用率/耗时是要能
        #    **点回一个仓库内文件**的 —— 所以这里支持把报告落盘到 data/derived/，
        #    那个目录是随仓库走的。**报告是汇总，原始证据在本地记录里**，
        #    这一点在文件头写清楚，不含糊。
        if args.out:
            body = (json.dumps(s, ensure_ascii=False, indent=1) if args.json
                    else render_md(s) if args.md else render_report(s))
            outp = args.out if os.path.isabs(args.out) else os.path.join(P2, args.out)
            os.makedirs(os.path.dirname(outp), exist_ok=True)
            header = (
                "<!-- 由 tools/run_record.py --report 生成；**不要手改**。\n"
                "     数据源：data/run/record.jsonl（运行时产物，未进仓库）。\n"
                "     复现：python tools/run_record.py --report%s --out %s\n"
                "     生成时间：%s\n"
                "     ⚠️ 这是**汇总**；原始逐轮证据在本地记录文件里，本文件不是原始数据。-->\n\n"
                % (" --md" if args.md else "", args.out, now_iso()))
            with io.open(outp, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(header + body + "\n")
            print("已写入 %s（%d 字节）" % (args.out, os.path.getsize(outp)))
        if args.json:
            print(json.dumps(s, ensure_ascii=False, indent=1))
        elif args.md:
            print(render_md(s))
        else:
            print(render_report(s))
        return 0
    return run_loop(args)


if __name__ == "__main__":
    sys.exit(main())
