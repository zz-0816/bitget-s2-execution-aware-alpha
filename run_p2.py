#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
项目二 · 独立启动入口（Execution-aware Alpha）
================================================

**不依赖项目一的任何文件**：只读本仓库 `data/` 下的数据快照。

用法：
  python run_p2.py                     # 起网页（默认 127.0.0.1:8788）
  python run_p2.py --host 0.0.0.0      # 允许外部访问（云服务器/容器里用这个）
  python run_p2.py --tunnel            # 起网页 + 开一条临时公网隧道（需 cloudflared）
  python run_p2.py --selftest          # 全量自检（含 HTTP 冒烟；不需要网络、不需要 key）
  python run_p2.py --selftest --net    # 额外跑联网检查（消息面源可用性）
  python run_p2.py --demo NVDA         # 命令行跑完整决策链（不用浏览器）

本文件是**唯一**的 Web 入口，刻意只暴露项目二自己的端点：

  /api/health              存活 + 快照时间点 + LLM 是否配置
  /api/bases               快照里真实存在的标的
  /api/decision?base=NVDA  ④ 完整决策链（5 路分析→辩论→闸门→交易员→风控官→最终）
  /api/assess?base=NVDA    风险与理由（不代下单，只给理由与条件）
  /api/overview            全标的的 assess 汇总（页面首屏用）
  /api/params              当前生效的全部阈值与来源（透明化）
  /api/snapshot            数据快照的边界说明（data/SNAPSHOT.md 原文）
  /api/alerts              持仓期巡检告警（data/positions/alerts.json，运行时产物）

历史教训（写在这里防复发）：这个仓库最初把项目一的 `web/` 一起复制了过来，
但页面调用的是 `/api/overview`、`/api/timeline`、`/api/data-status` 这些
**项目一服务才有的端点** —— 独立跑起来页面是**空的**。前端还期望
`/api/assess` 返回 `{items:[...]}`，而当时后端返回 `{ok,base,assess}`，
字段对不上也不报错，只是"什么都不显示"。
所以现在：**页面调用的每个端点都在本文件里**，且 `--selftest` 会真的去打一遍。
"""

import argparse
import datetime as dt
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

P2 = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, P2)
sys.path.insert(0, os.path.join(P2, "project2"))
sys.path.insert(0, os.path.join(P2, "tools"))

try:
    from common.console import install
    install()
except Exception:  # noqa: BLE001
    pass

VERSION = 2
DISCLAIMER = ("参赛作品展示：本项目是**做市型价差捕获**，不是无风险套利；"
              "代币 ≠ 股权；**不代下单**，输出的是理由与条件，非投资建议。")

# 快照里真实存在的标的（`--selftest` 会核对，见 _bases()）
DEFAULT_BASES = ["NVDA", "TSLA", "AAPL", "META", "GOOGL", "SPY", "QQQ",
                 "SOXL", "HOOD", "MRVL"]


# ================================================================ 数据底座

def snapshot_info():
    """快照的**时间点**与边界 —— 页面必须显示它，否则会让人以为盘口是"现在"。"""
    import glob
    ob = sorted(glob.glob(os.path.join(P2, "data", "spread", "orderbook-*.csv")))
    info = {"orderbook_file": os.path.relpath(ob[-1], P2).replace("\\", "/")
            if ob else None, "snapshot_ts": None, "snapshot_utc": None,
            "rounds": 0}
    if not ob:
        return info
    import csv
    rounds, last = set(), None
    with open(ob[-1], newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            ts = r.get("ts_ms")
            if ts:
                rounds.add(ts)
                last = ts
    if last:
        info["snapshot_ts"] = int(last)
        info["snapshot_utc"] = dt.datetime.fromtimestamp(
            int(last) / 1000, dt.UTC).strftime("%Y-%m-%d %H:%M UTC")
    info["rounds"] = len(rounds)
    return info


def _bases():
    """从快照里**实际读到**的标的，而不是写死的清单（写死会撒谎）。"""
    import csv
    import glob
    ob = sorted(glob.glob(os.path.join(P2, "data", "spread", "orderbook-*.csv")))
    got = []
    if ob:
        seen = set()
        with open(ob[-1], newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                b = r.get("base")
                if b and b not in seen:
                    seen.add(b)
                    got.append(b)
    return [b for b in DEFAULT_BASES if b in got] or got or list(DEFAULT_BASES)


def llm_config():
    """LLM 是否可用（只报**配置状态**，永不回显 key）。"""
    try:
        from common import config as cfg
        c = cfg.load()
        key = (c.get("LLM_API_KEY") or "").strip()
        return {"configured": bool(key), "model": c.get("LLM_MODEL"),
                "base_url": c.get("LLM_BASE_URL"),
                "thinking": c.get("LLM_THINKING"),
                "note": ("已配置：事件闸门会真实调用 LLM"
                         if key else
                         "**未配置 key**：事件判断退化为确定性日历，"
                         "输出里会如实标注『本次未使用 LLM』")}
    except Exception as exc:  # noqa: BLE001
        return {"configured": False, "model": None, "base_url": None,
                "note": "配置读取失败：%s: %s" % (type(exc).__name__, exc)}


def params_snapshot():
    """把代码里生效的阈值**原样摊开** —— 页面上每个数字都要能点回一个常量。"""
    try:
        import agent_team as at
        import execution_cost as ec
        return [
            {"name": "净收益判据门槛", "value": at.EDGE_THRESHOLD_BP, "unit": "bp",
             "source": "docs/14（往返手续费 13.70 − 资金费收入 2.36）",
             "code": "agent_team.EDGE_THRESHOLD_BP"},
            {"name": "僵持阈值（辩论）", "value": at.DEBATE_MARGIN, "unit": "得分差",
             "source": "docs/36 敏感性分析（tools/threshold_calibration.py）",
             "code": "agent_team.DEBATE_MARGIN"},
            {"name": "方向自相矛盾扣分", "value": at.DIRECTION_PENALTY, "unit": "分/条",
             "source": "docs/36（防『一个判断换三个说法』）",
             "code": "agent_team.DIRECTION_PENALTY"},
            {"name": "逆向选择否决线", "value": at.ADV_MIN_BP, "unit": "bp",
             "source": "docs/14 实测 f_dmid", "code": "agent_team.ADV_MIN_BP"},
            {"name": "名义额下限", "value": at.MIN_NOTIONAL_USD, "unit": "USD",
             "source": "低于它则结论＝不做", "code": "agent_team.MIN_NOTIONAL_USD"},
            {"name": "拆单粒度", "value": at.SLICE_USD, "unit": "USD/笔",
             "source": "docs/33 §4（交易员不新造阈值）",
             "code": "agent_team.SLICE_USD"},
            {"name": "首档深度占比上限", "value": at.DEPTH_TAKE_RATIO, "unit": "比例",
             "source": "docs/33 §4", "code": "agent_team.DEPTH_TAKE_RATIO"},
            {"name": "成交率否决线", "value": 0.10, "unit": "比例",
             "source": "docs/14 实测", "code": "agent_team.analyst_technical"},
            {"name": "报价冻结判定（轮数）", "value": at.FROZEN_LOOKBACK,
             "unit": "轮", "source": "与 audit_samples.py 同口径",
             "code": "agent_team.FROZEN_LOOKBACK"},
            {"name": "行情停滞阈值", "value": at.STALE_TRADE_MIN, "unit": "分钟",
             "source": "docs/33 §1 执行风险分析师",
             "code": "agent_team.STALE_TRADE_MIN"},
            {"name": "未成交机会成本（默认）", "value": ec.DEFAULT_MISS_BP,
             "unit": "bp", "source": "保守估计（约半个点差）",
             "code": "execution_cost.DEFAULT_MISS_BP"},
            {"name": "费率：现货往返", "value": ec.FEE_SPOT * 2, "unit": "bp",
             "source": "docs/09 官方公告核实", "code": "execution_cost.FEE_SPOT"},
            {"name": "费率：永续 maker", "value": ec.FEE_PERP_MAKER, "unit": "bp",
             "source": "docs/09", "code": "execution_cost.FEE_PERP_MAKER"},
            {"name": "费率：永续 taker", "value": ec.FEE_PERP_TAKER, "unit": "bp",
             "source": "docs/09", "code": "execution_cost.FEE_PERP_TAKER"},
        ]
    except Exception as exc:  # noqa: BLE001
        return [{"name": "参数读取失败", "value": None, "unit": "",
                 "source": "%s" % exc, "code": "-"}]


# ================================================================ 核心能力

def _assess(base, qty=5000.0):
    """风险与理由引擎（**不代下单**）。"""
    from event_gate import assess
    from execution_cost import analyse_two_leg
    cost = analyse_two_leg(base, qty, False, 3.0)
    a = assess(base, cost=cost, size_usd=qty)
    a["_cost"] = {k: cost.get(k) for k in
                  ("best_mode", "best_cost", "spread_s", "spread_p",
                   "p_both", "p_part", "p_none", "route", "session",
                   "joint_source", "leg_risk")}
    return a


def _min_notional():
    try:
        import agent_team as at
        return at.MIN_NOTIONAL_USD
    except Exception:  # noqa: BLE001
        return 100.0


def _edge_threshold():
    """净收益判据门槛（bp）。页面不硬编码它 —— 否则就有了第二个真相来源。"""
    try:
        import agent_team as at
        return at.EDGE_THRESHOLD_BP
    except Exception:  # noqa: BLE001
        return None


def _prompt_info():
    """当前生效的事件判断 prompt 版本（**只读**，不改任何行为）。

    `prompt_version`/`sha256` 由 event_gate 从 `prompts/*.md` 加载后提供；
    取不到就如实返回 None，页面会自己隐藏这一行，不编造。
    """
    try:
        import event_gate as eg
        return {"version": getattr(eg, "PROMPT_VERSION", None),
                "source": getattr(eg, "PROMPT_SOURCE", None),
                "sha256": getattr(eg, "PROMPT_SHA256", None),
                "path": getattr(eg, "PROMPT_PATH", None)}
    except Exception:  # noqa: BLE001
        return {"version": None, "source": None, "sha256": None, "path": None}


def _project_decision(base, qty=5000.0, miss_bp=None, urgent=False,
                      with_log=True):
    """跑完整决策链，并**投影成页面/接口契约**（只保留能核验的字段）。"""
    from agent_team import build_log, run_decision
    cost, items, debate, dec, book = run_decision(
        base, qty_usd=qty, miss_bp=miss_bp, urgent=urgent)

    v = debate.get("verdict") or {}
    out = {
        "base": base,
        "qty_requested": qty,
        "analysts": [{
            "dimension": (i.get("report") or {}).get("dimension"),
            "verdict": (i.get("report") or {}).get("verdict"),
            "confidence": (i.get("report") or {}).get("confidence"),
            "evidence": (i.get("report") or {}).get("evidence") or [],
            "sources": (i.get("report") or {}).get("sources") or [],
            "notes": (i.get("report") or {}).get("notes") or "",
            "valid": bool(i.get("valid")),
            "invalid_reason": i.get("invalid_reason") or "",
        } for i in items],
        "debate": {
            "verdict": {k: v.get(k) for k in
                        ("stance", "base_stance", "reason", "cap_reason",
                         "bull_weight", "bear_weight", "raw_bull_weight",
                         "raw_bear_weight", "margin", "direction_penalty",
                         "weighting_changed_stance")},
            "strength_mix": v.get("strength_mix"),
            "direction_conflicts": v.get("direction_conflicts") or [],
            "does_not_alter": v.get("does_not_alter") or [],
            "bull": {"weight": (debate.get("bull") or {}).get("weight"),
                     "arguments": (debate.get("bull") or {}).get("arguments") or [],
                     "concessions": (debate.get("bull") or {}).get("concessions") or [],
                     "dropped": (debate.get("bull") or {}).get("dropped") or []},
            "bear": {"weight": (debate.get("bear") or {}).get("weight"),
                     "arguments": (debate.get("bear") or {}).get("arguments") or [],
                     "concessions": (debate.get("bear") or {}).get("concessions") or [],
                     "dropped": (debate.get("bear") or {}).get("dropped") or []},
            "cross": debate.get("cross") or {},
            "excluded_reports": debate.get("excluded_reports") or [],
        },
        "gate": {k: cost.get(k) for k in
                 ("gate_severity", "gate_reason", "gate_source",
                  "maker_allowed")},
        "trader": {
            "order": dec["trader"].get("order"),
            "mode": dec["trader"].get("mode"),
            "cost_bp": dec["trader"].get("cost_bp"),
            "all_modes_bp": dec["trader"].get("all_modes_bp") or {},
            "barred_modes": dec["trader"].get("barred_modes") or [],
            "blocked_by": dec["trader"].get("blocked_by") or [],
            "price": dec["trader"].get("price"),
            "max_qty_usd": dec["trader"].get("max_qty_usd"),
            "gross_edge_bp": dec["trader"].get("gross_edge_bp"),
            "edge_gap_bp": dec["trader"].get("edge_gap_bp"),
        },
        "risk": {
            "verdict": dec["risk"]["verdict"],
            "reason": dec["risk"]["reason"],
            "hits": dec["risk"]["hits"],
            "vetoes": dec["risk"].get("vetoes") or [],
            "cautions": dec["risk"].get("cautions") or [],
            "qty_out_usd": dec["risk"].get("qty_out_usd"),
            "checked_rules": len(dec["risk"].get("rules") or []),
            "rules": [{
                "id": r.get("id"), "level": r.get("level"),
                "action": r.get("action"), "statement": r.get("statement"),
                "falsifier": r.get("falsifier"),
                "agent": str(r.get("id", "")).startswith("agent:"),
                "triggered": r.get("id") in (dec["risk"]["hits"] or []),
            } for r in (dec["risk"].get("rules") or [])],
        },
        "stages": dec["stages"],
        "final": dec["final"],
        "monotonic": dec["monotonic"],
        "risk_hypotheses": dec.get("risk_hypotheses") or [],
        "risk_hypotheses_dropped": dec.get("risk_hypotheses_dropped") or [],
        "cost": {k: cost.get(k) for k in
                 ("best_mode", "best_cost", "cost_mm", "cost_mix", "cost_tk",
                  "spread_s", "spread_p", "half_s", "half_p",
                  "p_s", "p_p", "p_both", "p_part", "p_none",
                  "p_both_indep", "p_part_indep", "joint_source",
                  "joint_prov", "leg_risk", "miss", "route", "session",
                  "n_s", "n_p", "invalidated")},
        "generated_utc": dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "min_notional_usd": _min_notional(),
        "edge_threshold_bp": _edge_threshold(),
        "prompt": _prompt_info(),
        "disclaimer": DISCLAIMER,
    }
    if with_log:
        # 每个结论都带 decision_hash：页面上看到的东西可离线复跑核对
        log = build_log(base=base, items=items, debate=debate, cost=cost,
                        decision=dec, qty_usd=qty,
                        miss_bp=(3.0 if miss_bp is None else float(miss_bp)),
                        urgent=urgent, now_ms=None, book=book)
        out["decision_hash"] = log.get("decision_hash")
        out["log_format"] = log.get("format")
        out["input_manifest"] = [
            {"path": m.get("path"), "exists": m.get("exists"),
             "sha256_16": (m.get("sha256") or "")[:16]}
            for m in (log.get("input_manifest") or [])]
        out["replay_cmd"] = ("python project2/agent_team.py --replay "
                             "data/reports/<本次日志>.json")
    return out


def read_alerts():
    """持仓期巡检告警（`tools/position_watch.py` 的产物；不存在就是没有）。"""
    p = os.path.join(P2, "data", "positions", "alerts.json")
    if not os.path.exists(p):
        return {"alerts": [], "exists": False,
                "note": "无告警文件（持仓期巡检未运行过，或从未触发）"}
    try:
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        return {"alerts": [], "exists": True,
                "note": "告警文件读取失败：%s" % type(exc).__name__}
    items = d if isinstance(d, list) else (d.get("alerts") or [])
    if isinstance(items, dict):
        items = [items]
    meta = d if isinstance(d, dict) else {}
    return {"alerts": items, "exists": True,
            "generated_utc": meta.get("ts") or meta.get("generated_utc"),
            "positions": meta.get("positions"),
            "counts": meta.get("counts"),
            "note": ""}


# ================================================================ HTTP

def make_handler():
    import http.server
    web = os.path.join(P2, "web")

    class Handler(http.server.SimpleHTTPRequestHandler):
        """静态页面 + 项目二自己的 API。刻意只暴露本仓库的端点。"""

        def __init__(self, *a, **kw):
            super().__init__(*a, directory=web, **kw)

        def log_message(self, fmt, *a):     # 安静一点
            pass

        # ---- 工具 ----
        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False, indent=1,
                              default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _err(self, exc, base=None):
            return self._json({"ok": False, "base": base,
                               "err": "%s: %s" % (type(exc).__name__, exc)},
                              500)

        # ---- 路由 ----
        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            path = u.path

            if path in ("", "/", "/index.html"):
                self.path = "/index.html"
                return super().do_GET()
            if path.startswith("/api/"):
                return self._api(path, q)
            return super().do_GET()

        def _api(self, path, q):
            try:
                if path == "/api/health":
                    return self._json({
                        "ok": True, "project": "execution-aware-alpha",
                        "version": VERSION, "bases": _bases(),
                        "snapshot": snapshot_info(), "llm": llm_config(),
                        "server_utc": dt.datetime.now(dt.UTC).strftime(
                            "%Y-%m-%d %H:%M:%S UTC"),
                        "offline": True, "disclaimer": DISCLAIMER})
                if path == "/api/bases":
                    return self._json({"ok": True, "bases": _bases()})
                if path == "/api/params":
                    return self._json({"ok": True, "params": params_snapshot()})
                if path == "/api/snapshot":
                    p = os.path.join(P2, "data", "SNAPSHOT.md")
                    if not os.path.exists(p):
                        return self._json({"ok": False, "err": "无快照说明"}, 404)
                    with open(p, encoding="utf-8") as fh:
                        return self._json({"ok": True, "markdown": fh.read()})
                if path == "/api/alerts":
                    return self._json(dict({"ok": True}, **read_alerts()))
                if path == "/api/assess":
                    base = (q.get("base") or ["NVDA"])[0].upper()
                    a = _assess(base)
                    return self._json({"ok": True, "available": True,
                                       "base": base, "assess": a,
                                       # 兼容层：早期前端按 items 取值
                                       "items": [dict(a, base=base)],
                                       "disclaimer": DISCLAIMER})
                if path == "/api/overview":
                    qty = float((q.get("qty") or ["5000"])[0])
                    items, errs = [], []
                    for b in _bases():
                        try:
                            items.append(_assess(b, qty))
                        except Exception as exc:  # noqa: BLE001
                            errs.append({"base": b,
                                         "error": "%s" % type(exc).__name__})
                    return self._json({"ok": True, "available": True,
                                       "items": items, "errors": errs,
                                       "qty": qty, "disclaimer": DISCLAIMER})
                if path == "/api/decision":
                    base = (q.get("base") or ["NVDA"])[0].upper()
                    qty = float((q.get("qty") or ["5000"])[0])
                    miss = q.get("miss_bp")
                    urgent = (q.get("urgent") or ["0"])[0] not in ("0", "false", "")
                    d = _project_decision(base, qty,
                                          float(miss[0]) if miss else None,
                                          urgent)
                    return self._json({"ok": True, "decision": d})
                return self._json({"ok": False, "err": "未知端点 %s" % path}, 404)
            except Exception as exc:  # noqa: BLE001
                return self._err(exc, (q.get("base") or [None])[0])
    return Handler


def serve(host, port, tunnel=False, open_browser=False):
    import http.server
    handler = make_handler()
    httpd = http.server.ThreadingHTTPServer((host, port), handler)
    real_port = httpd.server_address[1]

    print("=" * 78)
    print("项目二 · Execution-aware Alpha")
    print("=" * 78)
    print("  本机入口   http://127.0.0.1:%d" % real_port)
    if host not in ("127.0.0.1", "localhost"):
        print("  对外监听   http://%s:%d  （已绑定 %s，注意防火墙/安全组）"
              % (host, real_port, host))
    print("  快照时间点 %s" % (snapshot_info().get("snapshot_utc") or "—"))
    print("  LLM        %s" % llm_config()["note"])
    print("-" * 78)
    for p, why in (("/api/health", "存活 + 快照时间点"),
                   ("/api/decision?base=NVDA", "完整决策链（页面主视图）"),
                   ("/api/assess?base=NVDA", "风险与理由"),
                   ("/api/overview", "全标的汇总"),
                   ("/api/params", "生效阈值与来源"),
                   ("/api/snapshot", "数据边界"),
                   ("/api/alerts", "持仓期告警")):
        print("  %-26s %s" % (p, why))
    print("  Ctrl+C 退出", flush=True)

    if open_browser:
        import webbrowser
        threading.Timer(0.6, lambda: webbrowser.open(
            "http://127.0.0.1:%d" % real_port)).start()

    tproc = start_tunnel(real_port) if tunnel else None
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        # ⚠️ 必须收掉隧道子进程：cloudflared 是**独立进程**，不主动收它就会变成
        #    孤儿，一直挂在那儿占着一条隧道（保活守护反复重启时会越攒越多）。
        if tproc is not None and tproc.poll() is None:
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/F", "/T", "/PID",
                                    str(tproc.pid)], capture_output=True)
                else:
                    tproc.terminate()
                    try:
                        tproc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        tproc.kill()
                print("  已收掉公网隧道进程（PID %d）" % tproc.pid)
            except Exception:  # noqa: BLE001
                pass
    return 0


# ================================================================ 公网隧道

def find_cloudflared(explicit=None):
    cands = [explicit, os.environ.get("CLOUDFLARED"),
             os.path.join(P2, "tools", "bin", "cloudflared.exe"),
             os.path.join(P2, "tools", "bin", "cloudflared"),
             os.path.join(os.environ.get("TEMP", ""), "cloudflared.exe"),
             shutil.which("cloudflared")]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None


def start_tunnel(port, explicit=None):
    """起一条临时公网隧道（cloudflared quick tunnel，免账号）。

    ⚠️ 如实说明：这是**临时**链接，进程停就失效；它把本机端口暴露到公网，
    因此只在本机 demo 演示时使用，**不要**在含真实凭据的机器上长期开着。
    """
    exe = find_cloudflared(explicit)
    if not exe:
        print("  ⚠️ 没找到 cloudflared，跳过隧道。安装方式见 docs/43-公网可访问.md")
        return None
    url = ("http://127.0.0.1:%d" % port)
    print("-" * 78)
    print("  正在开临时公网隧道（cloudflared quick tunnel，免账号）…")
    proc = subprocess.Popen(
        [exe, "tunnel", "--no-autoupdate", "--url", url],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1)

    def pump():
        for line in proc.stdout:
            low = line.lower()
            if "trycloudflare.com" in low:
                # 只认真正的 URL 形态（cloudflared 的日志行里也会有别的词条
                # 含这个域名，之前把 "trycloudflare.com..." 当成地址打了出来）
                for tok in line.split():
                    tok = tok.strip().strip('"\'')
                    if tok.startswith("https://") and "trycloudflare.com" in tok:
                        # ⚠️ 必须 flush：输出被重定向/被父进程捕获时，
                        #    缓冲区不刷新就等于"用户永远看不到公网地址"
                        print("  ✅ 公网地址：%s" % tok, flush=True)
                        print("     （临时链接，本进程退出即失效；截图存证见 "
                              "docs/43-公网可访问.md）", flush=True)
                        break
            elif "error" in low or "failed" in low:
                print("  [tunnel] %s" % line.strip()[:160], flush=True)

    threading.Thread(target=pump, daemon=True).start()
    return proc


# ================================================================ 自检

def _run(cmd, label):
    print("-" * 78)
    print("▶ %s" % label)
    p = subprocess.run(cmd, cwd=P2)
    return p.returncode


def http_smoke(verbose=True):
    """HTTP 冒烟：**真的去打一遍**页面要用的每个端点。

    为什么必须有它：这个仓库踩过的坑就是"页面调用的端点根本不存在"，
    而当时所有自检都是绿的 —— 因为没有一个自检去碰 HTTP。
    """
    import http.server
    ok = True
    handler = make_handler()
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = "http://127.0.0.1:%d" % port

    def chk(cond, msg):
        nonlocal ok
        ok = ok and bool(cond)
        if verbose:
            print("  [%s] %s" % ("OK " if cond else "!! ", msg))

    def get(path):
        with urllib.request.urlopen(base + path, timeout=180) as r:
            return r.status, json.loads(r.read().decode("utf-8"))

    try:
        # ---- 页面本体与其引用的静态资源 ----
        for f in ("/", "/app.js", "/styles.css"):
            with urllib.request.urlopen(base + f, timeout=30) as r:
                body = r.read()
            chk(r.status == 200 and len(body) > 200,
                "静态资源可用 %s（%d 字节）" % (f, len(body)))
        # ⭐ 关键：页面里 fetch 的每个 /api/ 端点都必须真实存在
        with urllib.request.urlopen(base + "/app.js", timeout=30) as r:
            js = r.read().decode("utf-8")
        called = sorted(set(
            p for p in __import__("re").findall(r"['\"](/api/[a-z\-]+)", js)))
        chk(bool(called), "前端确实调用了 API（%s）" % "、".join(called))

        st, h = get("/api/health")
        chk(st == 200 and h.get("ok") and h.get("bases"),
            "/api/health 存活（%d 个标的，快照 %s）"
            % (len(h.get("bases") or []), (h.get("snapshot") or {}).get("snapshot_utc")))
        for path in called:
            if path in ("/api/decision", "/api/assess"):
                path += "?base=NVDA"
            if path == "/api/overview":
                pass
            st, d = get(path)
            chk(st == 200 and d.get("ok") is not False,
                "前端调用的端点真实存在：%s" % path)

        st, d = get("/api/decision?base=NVDA&qty=5000")
        dec = d["decision"]
        chk(len(dec["analysts"]) == 5, "决策链返回 5 路分析师")
        chk(dec.get("decision_hash") and len(dec["decision_hash"]) == 64,
            "带 decision_hash（%s…）" % (dec.get("decision_hash") or "")[:12])
        chk(dec["monotonic"]["stance_non_increasing"]
            and dec["monotonic"]["qty_non_increasing"],
            "单调性硬校验通过（立场与规模都只能收紧）")
        chk(bool(dec["risk"]["rules"]) and dec["risk"]["checked_rules"] > 0,
            "风控官规则表非空（%d 条，触发 %s）"
            % (dec["risk"]["checked_rules"], "、".join(dec["risk"]["hits"]) or "无"))
        chk(all(a["sources"] for a in dec["analysts"] if a["valid"]),
            "有效报告都带可回溯来源")

        st, d = get("/api/overview")
        chk(len(d["items"]) >= 5, "/api/overview 覆盖 %d 个标的" % len(d["items"]))
        chk(all(it.get("rationale") for it in d["items"]),
            "每个标的都给出可核验理由")

        st, d = get("/api/params")
        chk(len(d["params"]) >= 10, "/api/params 摊开 %d 条阈值" % len(d["params"]))
        st, d = get("/api/alerts")
        chk(st == 200, "/api/alerts 可用（存在告警文件=%s）" % d.get("exists"))
        st, d = get("/api/snapshot")
        chk(st == 200 and "SHA256" in d.get("markdown", ""),
            "/api/snapshot 返回快照清单")
    except Exception as exc:  # noqa: BLE001
        chk(False, "HTTP 冒烟异常：%s: %s" % (type(exc).__name__, exc))
    finally:
        httpd.shutdown()
        httpd.server_close()

    if verbose:
        print("\nHTTP 冒烟%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


def selftest(with_net=False):
    py = sys.executable
    print("=" * 92)
    print("项目二 · 全量自检（离线；含 HTTP 冒烟）")
    print("=" * 92)
    rc = 0
    for cmd, label in (
            ([py, os.path.join("project2", "execution_cost.py"), "--selftest"],
             "① 执行成本模型（确定性核心）"),
            ([py, os.path.join("project2", "event_gate.py"), "--selftest"],
             "② 事件闸门（含失败语义与日历质量）"),
            ([py, os.path.join("project2", "agent_team.py"), "--selfcheck"],
             "③ 多 Agent 团队（分析师→辩论→交易员/风控官→复现日志）"),
            ([py, os.path.join("common", "rag_memory.py"), "--selftest"],
             "④ RAG 记忆（能消化 + 不膨胀 + 只读）"),
            ([py, os.path.join("tools", "snapshot_manifest.py"), "--verify"],
             "⑤ 数据快照核验（冻结项逐字节）"),
            ([py, os.path.join("tools", "position_watch.py"), "--selftest"],
             "⑥ 持仓期巡检（含告警产出）"),
            ([py, os.path.join("project2", "market_events.py"), "--selftest"],
             "⑦ 可计算事件（期权到期/休市）"),
            ([py, os.path.join("common", "config.py"), "--check"],
             "⑧ 配置解析（含 .env 优先级）"),
            ([py, os.path.join("tools", "event_calibration.py"), "--selftest"],
             "⑨ 事件判定校准集（schema + 覆盖 + **校准集不得进 RAG** 的留出检查）"),
            ([py, os.path.join("tools", "retruncate_orderbook.py"), "--selftest"],
             "⑩ 盘口重截断（保留最后 N 轮 —— 与 trades 尾部对齐）"),
            ([py, os.path.join("tools", "keep_alive.py"), "--selftest"],
             "⑪ 保活守护（地址解析 / 退避 / 状态文件 / 隧道默认值 / 杀进程树）"),
    ):
        rc |= _run(cmd, label)

    print("-" * 78)
    print("▶ ⑫ HTTP 冒烟（页面调用的每个端点都真打一遍）")
    rc |= http_smoke()

    rc |= _run([py, os.path.join("tools", "web_smoke.py")],
               "⑬ 页面渲染冒烟（Node 最小 DOM 里真跑一遍 web/app.js）")

    if with_net:
        rc |= _run([py, os.path.join("tools", "news_sources.py"), "--base", "NVDA"],
                   "⑭ 消息面源可用性（联网）")

    print("=" * 92)
    print("全量自检%s" % ("通过" if rc == 0 else "**失败**"))
    print("=" * 92)
    return rc


def demo(base, qty=5000.0, as_json=False):
    d = _project_decision(base.upper(), qty)
    if as_json:
        print(json.dumps(d, ensure_ascii=False, indent=1, default=str))
        return 0
    print("标的 %s ｜ 请求名义额 %.0f USD ｜ 快照 %s"
          % (d["base"], qty, snapshot_info().get("snapshot_utc")))
    print("-" * 78)
    for a in d["analysts"]:
        print("  %-16s %-12s 置信度 %.2f  证据 %d 条  %s"
              % (a["dimension"], a["verdict"], a["confidence"] or 0,
                 len(a["evidence"]), "" if a["valid"] else "（无效）"))
    v = d["debate"]["verdict"]
    print("  辩论：多头 %.2f vs 空头 %.2f -> %s（%s）"
          % (v["bull_weight"] or 0, v["bear_weight"] or 0, v["stance"],
             v["reason"]))
    g = d["gate"]
    print("  闸门：severity=%s 来源=%s（%s）" % (g["gate_severity"],
                                               g["gate_source"], g["gate_reason"]))
    r = d["risk"]
    print("  风控：%s（%d 条规则，触发 %s）"
          % (r["verdict"], r["checked_rules"], "、".join(r["hits"]) or "无"))
    f = d["final"]
    print("  最终：%s ｜ %.0f USD ｜ 订单 %s"
          % (f["stance"], f["qty_usd"],
             (f["order"] or {}).get("mode") or "无（不下单）"))
    print("        为什么：%s" % f["why"][:110])
    print("  decision_hash：%s" % d["decision_hash"])
    print("  复跑：%s --base %s --trader --log"
          % ("python project2/agent_team.py", d["base"]))
    return 0


def main(argv=None):
    # 平台/容器里通常用环境变量给端口（Render/Railway/Fly 都是 $PORT）。
    # 设了 PORT 就默认对外监听 —— 否则容器里"起得来但外面连不上"，
    # 而日志上一切正常，是最容易白跑半小时的一种故障。
    env_port = os.environ.get("PORT")
    ap = argparse.ArgumentParser(
        description="项目二独立入口（网页 / 自检 / 命令行演示 / 公网隧道）")
    ap.add_argument("--host", default=os.environ.get("HOST")
                    or ("0.0.0.0" if env_port else "127.0.0.1"),
                    help="监听地址；对外访问用 0.0.0.0（设了 $PORT 时自动如此）")
    ap.add_argument("--port", type=int, default=int(env_port or 8788))
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--net", action="store_true",
                    help="自检时额外跑联网检查（消息面源）")
    ap.add_argument("--demo", default=None, help="命令行跑完整决策链")
    ap.add_argument("--qty", type=float, default=5000.0, help="名义额 USD")
    ap.add_argument("--json", action="store_true", help="--demo 输出 JSON")
    ap.add_argument("--tunnel", action="store_true",
                    help="起网页并开一条临时公网隧道（需 cloudflared）")
    ap.add_argument("--cloudflared", default=None, help="cloudflared 可执行文件路径")
    ap.add_argument("--open", action="store_true", help="自动打开浏览器")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest(with_net=args.net)
    if args.demo:
        return demo(args.demo, args.qty, args.json)
    if args.tunnel:
        args.host = "127.0.0.1"      # 隧道在外部，本地仍只听回环（更安全）
    return serve(args.host, args.port, tunnel=args.tunnel,
                 open_browser=args.open)


if __name__ == "__main__":
    sys.exit(main())
