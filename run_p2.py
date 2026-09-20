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


def _read_json_bom(path):
    """读 JSON，**容忍 UTF-8 BOM**（Windows 上 `Out-File`/`Set-Content` 默认带 BOM）。

    这是本仓库第 4 次碰到同一个坑（前三次见 `docs/DATA_DICT.md` 陷阱 #13），
    而持仓单偏偏是**用户手写**的，所以这里必须容忍。
    """
    last = None
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            with open(path, encoding=enc) as fh:
                return json.load(fh), None
        except json.JSONDecodeError as exc:
            last = "JSON 解析失败（%s）：%s" % (enc, exc)
        except OSError as exc:
            return None, "读不到 %s：%s" % (path, exc)
    return None, last or "无法解析"


def open_position(base, demo=False):
    """取**在途持仓单**里该标的的那一条 —— 执行进度官的输入。

    两种来源，页面/接口要能分辨（返回 ``(order_state, note)``）：

      · ``demo=False``：读 `data/positions/open.json`（**用户/上层提供**，
        格式见 `tools/position_watch.py` 头注释）。本服务**只读不写**；
        文件不存在 = 没有在途订单 -> 执行进度官不跑（**不猜**）。
      · ``demo=True`` ：**不读文件**，造一张现货腿已成交、永续腿未成交的
        **合成演示单**。它带 `synthetic: true`，一路进日志、进页面、进接口，
        不可能被误当成真实下单记录。

    为什么要 demo 这一路：执行进度官是本轮新增的能力，而"公开演示"时
    手上通常没有真实在途订单 —— 没有它，这个功能在演示里**永远不出现**，
    等于不可验证。合成单把"看得见"和"不撒谎"同时做到。
    """
    if demo:
        return ({"id": "demo-naked-leg", "base": base, "qty_usd": 5000.0,
                 "spot_filled": True, "perp_filled": False,
                 "synthetic": True},
                "**合成演示持仓**（?position=demo）：现货腿已成交、永续腿未成交，"
                "用于展示「裸露敞口」判定；不是真实下单记录")
    p = os.path.join(P2, "data", "positions", "open.json")
    if not os.path.exists(p):
        return None, "没有在途持仓单（data/positions/open.json 不存在）"
    d, err = _read_json_bom(p)
    if d is None:
        return None, "持仓单不可用：%s" % err
    for pos in (d.get("positions") or []):
        if str(pos.get("base") or "").upper() == base.upper():
            pos = dict(pos)
            pos.setdefault("synthetic", False)
            return pos, "来自 data/positions/open.json"
    return None, "持仓单里没有 %s 这一单" % base


def _project_execution_progress(ep):
    """把执行进度官的产出投影成页面契约（只保留能核验的字段）。"""
    if not isinstance(ep, dict):
        return {"present": False}
    rep = ep.get("report") or {}
    return {
        "present": True,
        "order": ep.get("order_state"),
        "synthetic": bool((ep.get("order_state") or {}).get("synthetic")),
        "error": ep.get("error"),
        "verdict": rep.get("verdict"),
        "confidence": rep.get("confidence"),
        "notes": rep.get("notes") or "",
        "evidence": rep.get("evidence") or [],
        "hypotheses": ep.get("hypotheses") or [],
        "dropped": ep.get("dropped") or [],
        "actions": ep.get("actions") or [],
    }


def _project_decision(base, qty=5000.0, miss_bp=None, urgent=False,
                      with_log=True, asof_ms=None, basis=None,
                      order_state=None, order_age_min=None):
    """跑完整决策链，并**投影成页面/接口契约**（只保留能核验的字段）。

    ``asof_ms`` / ``basis``：决策基准时间。默认交给 `time_basis()` **自动判定**
    （读冻结快照时按数据自带时刻，实时时按墙钟），并在返回里如实标注依据。
    见 `project2/agent_team.py::time_basis` 的注释。

    ``order_state`` / ``order_age_min``：在途持仓单（执行进度官的输入）与
    "它已经挂了多久"。给了 ``order_age_min`` 就按**本次决策基准时间**倒推
    ``opened_ms`` —— 这是必须的：持仓单的 `opened_ms` 必须与决策用**同一个时钟**，
    否则执行进度官会（正确地）拒绝判龄，页面上就什么都看不到（实测踩到）。
    """
    from agent_team import build_log, run_decision, time_basis
    _os = dict(order_state) if isinstance(order_state, dict) else None
    _tb_pin = None
    if _os and order_age_min is not None:
        # 先定"现在几点"，再把 opened_ms 钉在**同一个钟**上。
        # 然后把解出来的时刻显式交给 run_decision —— 一次请求只有一个时钟判定，
        # 不给"两次调用之间墙钟跳了一下"留下缝隙。
        _tb_pin = time_basis(asof_ms, force=basis)
        _os["opened_ms"] = int(_tb_pin["now_ms"] - float(order_age_min) * 60000)
    cost, items, debate, dec, book = run_decision(
        base, qty_usd=qty, miss_bp=miss_bp, urgent=urgent,
        now_ms=(_tb_pin["now_ms"] if _tb_pin else asof_ms),
        time_basis_force=basis, order_state=_os)
    if _tb_pin:
        # run_decision 里因为显式传了 now_ms，basis 会标成 "explicit"；
        # 但**这次请求真正的依据**是上面那次 time_basis 判出来的（asof/wallclock）。
        # 如实还原成那一个，避免同一页面上两种口径。
        dec["time_basis"] = _tb_pin

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
        # ⭐ 闸门合并留痕：静态日历说了什么 / LLM 说了什么 / 最后生效的是哪个。
        #    页面上要能一眼看出"硬闸门最后听谁的"，而不是只看一个结论。
        "gate_merge": dec.get("gate_merge"),
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
        # ⏱️ 执行进度官（执行中闭环）：有没有在途订单、判成了什么、该处置什么。
        #    没有在途订单时 present=False —— 页面据此**不显示**这一块，
        #    而不是显示一个"一切正常"的假绿灯（没有数据 ≠ 没有风险）。
        "execution_progress": _project_execution_progress(
            dec.get("execution_progress")),
        "cost": {k: cost.get(k) for k in
                 ("best_mode", "best_cost", "cost_mm", "cost_mix", "cost_tk",
                  "spread_s", "spread_p", "half_s", "half_p",
                  "p_s", "p_p", "p_both", "p_part", "p_none",
                  "p_both_indep", "p_part_indep", "joint_source",
                  "joint_prov", "leg_risk", "miss", "route", "session",
                  "n_s", "n_p", "invalidated")},
        "generated_utc": dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
        # ⭐ 决策基准时间：页面顶部要显式写出来"这次是按几点判的、凭什么"。
        #    离线演示读冻结快照时必须按数据自带时刻，否则时间衰减规则
        #    （行情停滞 >=30 分钟）会把冻结数据误判成"市场停了"。
        "time_basis": dec.get("time_basis"),
        # 📉 数据新鲜度：每个输入源落后多少 + 这算不算危险。
        #    离线声明模式（basis=asof）只说"旧"，实时模式但输入停了才叫 stale。
        "freshness": dec.get("freshness"),
        # 💵 入场损益测算：给定金额 -> 能算的（摩擦/资金费/裸露期望）+ **算不出来的**
        "entry": dec.get("entry"),
        "min_notional_usd": _min_notional(),
        "edge_threshold_bp": _edge_threshold(),
        "prompt": _prompt_info(),
        "disclaimer": DISCLAIMER,
    }
    if with_log:
        # 每个结论都带 decision_hash：页面上看到的东西可离线复跑核对
        # 🔴 `now_ms` 必须传**本次决策真正用的那个时刻**（as-of），不能留空：
        #    `build_log` 留空会退回 `generated_ms`（墙钟），而 now_ms 是
        #    **契约参数**（闸门判定依赖它）—— 复跑时两边不一致会直接判"不可复现"。
        _tb = dec.get("time_basis") or {}
        log = build_log(base=base, items=items, debate=debate, cost=cost,
                        decision=dec, qty_usd=qty,
                        miss_bp=(3.0 if miss_bp is None else float(miss_bp)),
                        urgent=urgent, now_ms=_tb.get("now_ms"), book=book)
        out["decision_hash"] = log.get("decision_hash")
        out["log_format"] = log.get("format")
        out["input_manifest"] = [
            {"path": m.get("path"), "exists": m.get("exists"),
             "sha256_16": (m.get("sha256") or "")[:16]}
            for m in (log.get("input_manifest") or [])]
        out["replay_cmd"] = ("python project2/agent_team.py --replay "
                             "data/reports/<本次日志>.json")
    return out


def _overview(qty=5000.0, fresh=False):
    """全标的概览，**带记忆化**（返回 payload, 是否命中缓存）。

    为什么必须缓存（实测）：
      概览要对 10 个标的各跑一次 `assess()`，**串行约 10.3 秒**，而且冷热一个样 ——
      页面首屏的表因此"加载中…"整整十秒，点一次"刷新全标的概览"再等十秒。
      评委打开演示的第一印象就是这个。

    为什么缓存是**安全**的（不是拿旧数据糊弄）：
      本服务读的是**冻结快照**，而决策基准时间现在由 `time_basis()` 从数据本身推导
      （见 `project2/agent_team.py`）—— 同一份快照在同一 as-of 下的结论**不会变**。
      所以缓存不损失任何新鲜度，只是不再把同一件事算十遍。
      （实时模式下数据会变，那时请用 `?fresh=1` 强制重算。）
    """
    global _OV_CACHE
    from agent_team import time_basis
    tb = time_basis()
    key = (round(float(qty), 4), tb["now_ms"])
    with _OV_LOCK:
        if not fresh and _OV_CACHE.get("key") == key and _OV_CACHE.get("payload"):
            p = dict(_OV_CACHE["payload"])
            p["cached"] = True
            return p, True
    items, errs = [], []
    for b in _bases():
        try:
            items.append(_assess(b, qty))
        except Exception as exc:  # noqa: BLE001
            errs.append({"base": b, "error": "%s" % type(exc).__name__})
    payload = {"ok": True, "available": True, "items": items, "errors": errs,
               "qty": qty, "disclaimer": DISCLAIMER,
               "cached": False, "time_basis": tb,
               "built_utc": dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")}
    with _OV_LOCK:
        _OV_CACHE = {"key": key, "payload": payload}
    return payload, False


_OV_CACHE = {}
_OV_LOCK = threading.Lock()


def warm_overview():
    """启动时后台预热概览 —— 让**第一个**打开页面的人也不用等十秒。

    为什么值得单独做：缓存只解决"第二次以后"。而演示是"评委第一次打开"，
    那一次恰好是最慢的一次。预热把十秒挪到服务启动后（无人等待的时刻）。
    """
    def _work():
        try:
            _overview(5000.0)
        except Exception:  # noqa: BLE001
            pass          # 预热失败不影响服务：真请求时还会再算一次
    t = threading.Thread(target=_work, daemon=True, name="warm-overview")
    t.start()
    return t


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
                    # 📉 数据新鲜度也在这里给：顶栏要能**不跑决策**就说清
                    #    "数据有多旧、这是声明过的离线模式还是输入停了"。
                    try:
                        from agent_team import data_freshness, time_basis
                        _tb = time_basis()
                        _fr = data_freshness(now_ms=_tb.get("now_ms"),
                                             basis=_tb.get("basis"))
                    except Exception as exc:  # noqa: BLE001
                        _fr = {"verdict": "unknown",
                               "why": "新鲜度不可用：%s" % type(exc).__name__}
                    return self._json({
                        "ok": True, "project": "execution-aware-alpha",
                        "version": VERSION, "bases": _bases(),
                        "snapshot": snapshot_info(), "llm": llm_config(),
                        "freshness": _fr,
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
                    fresh = (q.get("fresh") or ["0"])[0] not in ("0", "false", "")
                    payload, cached = _overview(qty, fresh=fresh)
                    return self._json(payload)
                if path == "/api/decision":
                    base = (q.get("base") or ["NVDA"])[0].upper()
                    qty = float((q.get("qty") or ["5000"])[0])
                    miss = q.get("miss_bp")
                    urgent = (q.get("urgent") or ["0"])[0] not in ("0", "false", "")
                    # 决策基准时间可显式指定（复现/演示用）：
                    #   ?basis=asof | ?basis=wallclock | ?asof=1789802876375
                    _b = (q.get("basis") or [None])[0]
                    _a = (q.get("asof") or [None])[0]
                    # ⏱️ 在途持仓单（执行进度官的输入）：
                    #   ?position=auto（默认，读 data/positions/open.json）
                    #   ?position=demo（合成演示单，带 synthetic 标记）
                    #   ?position=none（显式关掉，用来看"没有在途订单"的样子）
                    _pm = (q.get("position") or ["auto"])[0].lower()
                    _osv, _onote, _oage = None, None, None
                    if _pm == "demo":
                        _osv, _onote = open_position(base, demo=True)
                        _oage = 12.0        # 演示单：挂了 12 分钟（> 宽限 1 分钟）
                    elif _pm != "none":
                        _osv, _onote = open_position(base, demo=False)
                    d = _project_decision(base, qty,
                                          float(miss[0]) if miss else None,
                                          urgent,
                                          asof_ms=float(_a) if _a else None,
                                          basis=_b,
                                          order_state=_osv,
                                          order_age_min=_oage)
                    d["position_source"] = _onote
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

    # ⭐ 后台预热概览：概览要跑 10 个标的（串行约 10 秒），而"第一个打开页面的人"
    #    恰好要等最慢的那一次。预热把它挪到服务刚起来、没人在等的时刻。
    from agent_team import time_basis
    _tb = time_basis()
    print("  预热       全标的概览（后台；基准时间 %s / basis=%s）"
          % (dt.datetime.fromtimestamp(_tb["now_ms"] / 1000, dt.UTC)
             .strftime("%Y-%m-%d %H:%M:%S UTC"), _tb["basis"]), flush=True)
    warm_overview()

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


def _run_any(cmds, label):
    """一条自检步骤里跑**多条命令**，任一失败即失败。

    为什么要这样（而不是"一个工具一步"）：每加一个工具，后面的步骤就得整体
    重编号（⑰→⑱→⑲ 一路顺延），而编号散落在 README、提交材料、表单稿、
    TASKS、CHECKLIST 十来处 —— 改一次就是一轮体力活，还容易漏。
    把"步骤"与"命令"解耦之后，**加工具不再需要动编号**。
    """
    print("-" * 78)
    print("▶ %s" % label)
    rc = 0
    for cmd in cmds:
        p = subprocess.run(cmd, cwd=P2)
        rc |= p.returncode
    return rc


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


def ui_layout_check():
    """前端**布局**验收：真浏览器打开页面，量尺寸、点按钮、看有没有卡住。

    为什么 HTTP 冒烟 + 最小 DOM 渲染冒烟还不够（实测）：
      这两步都**不做布局**，所以下面这些真实缺陷在它们眼里全是绿的 ——
        · 标题里 `**没有引用已实测的量的结论一律作废**` 原样显示成字面星号；
        · 「全标的概览」一直停在"加载中…"（接口要 10.3 秒，页面毫无提示）；
        · 元素右缘越出视口（项目一那张表就是这样丢掉一整列）。
      只有真渲染 + 量尺寸才发现得了，所以补这一步。

    没有 Chrome/Edge 或 Node 时 **优雅跳过**（`ui_check.py` 自己会说明并返回 0）：
    评委机器上可能没装浏览器，不能因为缺浏览器就让整套自检变红。
    """
    import http.server
    handler = make_handler()
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % port
    try:
        return _run([sys.executable, os.path.join("tools", "ui_check.py"),
                     "--url", base + "/",
                     # 决策链要点了按钮才有东西可量
                     "--click", "#run",
                     "--until",
                     "document.querySelectorAll('#stages .stage').length>0",
                     "--wait", "4000"],
                    "⑮ 前端布局验收（真浏览器：溢出/字面标记/截断/卡住的占位符）")
    finally:
        httpd.shutdown()


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

    # 设计也能"可验证"：类名一致性 / 对比度（WCAG）/ 无障碍兜底 / 字面标记。
    # 放在浏览器验收**之前** —— 它不需要浏览器，任何机器上都能跑。
    rc |= _run([py, os.path.join("tools", "ui_design_check.py")],
               "⑭ 前端设计检查（类名一致 / 对比度 / reduced-motion / focus-visible）")

    rc |= ui_layout_check()

    # ⑯ 长跑记录器自检：它是**稳定性与效率的实测证据源**，所以它自己也要被检
    #    （成功率/分位/缓存计数的算法对不对、坏行会不会毁掉整份记录）。
    #    与 ⑥⑨⑪ 一致：凡是材料里引用了其输出的工具，都进一键自检。
    rc |= _run([py, os.path.join("tools", "run_record.py"), "--selftest"],
               "⑯ 长跑记录器（汇总统计算法 / 记录格式 / 空与坏数据）")

    # ⑰ 文档路径引用检查：本仓库的可信度建立在"每个路径都能点回去"上，
    #    而 docs/ 里有一批**继承自隔离前**的引用指向项目一仓库。实测一次扫出
    #    18 处未标注 —— 说明这件事**没有检查**，只靠人记。现在它会失败。
    #    判定**以 git 入库状态为准**（不是"磁盘上有没有"）：被 .gitignore 排除的
    #    运行时产物允许引用；"在磁盘上但没入库"反而是失败（别人 clone 点不到）。
    #    ⚠️ 这一条是被**新克隆**打脸才改的：初版按存在性判，同一份文档在开发机上
    #       通过、在 clone 出来的仓库里失败 —— 检查器自己不可移植。
    rc |= _run([py, os.path.join("tools", "doc_ref_check.py")],
               "⑰ 文档路径引用（以 git 入库状态为准 / 已声明的跨仓库引用）")

    # ⑱ 📡 本项目自带的行情通道 + ⚓ 外部价格锚（官方 bitget-mcp-server）。
    #    ⭐ 这一步**可以含多条命令** —— 以前每加一个工具就要把后面所有步骤重编号
    #       （⑰→⑱→⑲ 一路顺延），纯属自找的麻烦。现在一条步骤可以跑一串工具，
    #       以后加工具**不用再动编号**。
    #    两者的自检**都不联网**：
    #      · market_feed：归一化到快照 schema 的列、换算与累计名义额、
    #        以及**落盘守卫**（绝不允许写进 data/spread/）
    #      · mcp_anchor：偏离换算、**口径守卫**（开市才叫折溢价，休市只能叫偏移）、
    #        缺失不硬算（读不到返回 None 而不是 0）
    rc |= _run_any([[py, os.path.join("tools", "market_feed.py"), "--selftest"],
                    [py, os.path.join("tools", "mcp_anchor.py"), "--selftest"],
                    [py, os.path.join("project2", "ext_events.py"), "--selftest"]],
                   "⑱ 行情通道 + 外部锚 + 外部确定性事件"
                   "（schema 归一 / 落盘守卫 / 口径守卫 / **事件窗口规则**）")

    if with_net:
        rc |= _run([py, os.path.join("tools", "news_sources.py"), "--base", "NVDA"],
                   "⑲ 消息面源可用性（联网）")

        # 🔴 ⑳ 事件判定**回归门槛**：新 prompt / 新模型必须在同一套**留出**校准集上
        #    不低于基线 —— 这是"改正了危险方向错误之后，不许再退回去"的回归锁。
        #    ⚠️ 没有 LLM key 时它**如实报"未执行"并返回 0**：既不冒充通过，
        #       也不算失败（这与仓库里其它地方"不假装跑过"的原则一致）。
        rc |= _run([py, os.path.join("tools", "event_calibration.py"), "--gate"],
                   "⑳ 事件判定回归门槛（留出校准集：危险方向错误必须为 0）")

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
