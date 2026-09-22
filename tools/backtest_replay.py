#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
📊 快照重放回测：把"这套系统可行吗"变成**可核验的数字**
========================================================

━━ 为什么这样设计（先说清楚不能做什么）━━

传统意义的"回测胜率"在这个项目里**做不出来**，原因写在明处：

  · maker 的成交取决于**你在队列里的位置** —— 快照里没有队列数据，谁也不能
    诚实地说"当时这单会成交"。本项目从未建队列位置模型（docs/40 已列为局限）。
  · LLM 事件判定**无法历史重放**（事件是当时的信息，重放就是编）。
  · 轮级盘口快照目前只有**一天**（2026-09-19，60s 一轮，15.3h）—— 样本窗口小。

所以本工具做的是**能诚实做到的那部分**：在冻结快照上做**确定性重放**，
把"胜率"定义成**可从快照数据逐笔复核的量**，并把每一条信号落成 CSV 让人验。

━━ 重放规则（全部确定性，任何人可复跑）━━

  数据   `data/spread/2026-*.csv`（轮级盘口，**冻结快照，只读**）
  对齐   每 base 把 perp 轮按 ts_ms 就近对齐到 spot 轮（容差 ±15s；采样节奏 60s）
  信号   系统自己的公式（`project2/entry_math.py`）：
             edge = 2×(half_s + half_p) − 往返手续费 ≥ EDGE_THRESHOLD_BP（11.34）
         half_* = 快照真实盘口的 spread_bp / 2；费率与门槛**从代码常量读**（不重抄）。
         ⚠️ 公式里的"逆向选择 f"不单独减 —— 重放的**实际结果**里已经隐含了它，
            这正是重放的价值：检验正的账面优势能不能活过现实。
  出场   t+H（H ∈ 15/30/60 分钟），按出场轮的**真实中间价**估值；
         出场轮不存在（信号在窗口末尾）→ 计入 `无法出场`，**单独报告，不混入胜率**。
  两条口径并列报告（不给挑对自己有利的那种的机会）：
    · **taker 保守下界**：进出都按对手价成交（付两腿半幅点差），费 2×(5+6)=22bp
    · **maker 乐观上界**：进出都按自己报价成交（赚两腿半幅点差），费按路由
      （`in_house` 2×(5+2)=14bp；`stockroute` 挂单按 Taker 计费 → 该轮 maker=taker）
  价格腿 对冲持仓的已实现盈亏 = ((S_H−S_t) − (P_H−P_t)) / S_t × 1e4 bp
         —— 两腿对冲后剩下的就是 rToken↔永续 的基差漂移，这是**真实承担**的风险
  不重叠 每 base 触发后 H 分钟内的后续轮不再触发（60s 节奏下相邻轮高度自相关，
         逐轮都算会把同一个信号数十几遍，那是自己骗自己）
  基线   **全体对齐轮不过滤直接进场**（同样的不重叠规则）——
         门槛有没有用，就看信号组与基线组的差

━━ 明确不测什么（这些才是"可行性"的边界）━━

  · maker 成交率（队列位置）→ 用实测联合分布折算（`data/derived/friction_budget.csv`
    与 `joint_fill_*.csv`：P(两腿)=0.70% / P(单腿)=24.9%）
  · LLM 事件判定 → 无法重放（信号规则只用确定性成本层）
  · 资金费 → H ≤ 60min 影响量级小，且快照无逐笔资金费结算记录 → **声明排除**
  · 现货腿自 09-14 起零成交（docs/29）→ "能挂出来"与"能成交"是两回事

用法::

    python tools/backtest_replay.py                # 重放 + 打印报告 + 落盘证据
    python tools/backtest_replay.py --json         # 机器可读
    python tools/backtest_replay.py --no-save      # 只看报告
    python tools/backtest_replay.py --selftest     # 离线自检（不联网）
"""

import argparse
import bisect
import datetime as dt
import glob
import io
import json
import os
import re
import statistics as st
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPREAD_DIR = os.path.join(BASE, "data", "spread")
OUT_DIR = os.path.join(BASE, "data", "backtest")
ALIGN_TOL_MS = 15_000          # 60s 节奏下，双腿同轮采样差应在秒级
HORIZONS_MIN = (15, 30, 60)
ROUND_FILE_GLOB = "2026-*.csv"  # 轮级盘口（与 orderbook-*/trades-* 区分开）


# ────────────────────────────────────────────── 常量来源（口径唯一，不重抄）

def _sys_paths():
    for p in (BASE, os.path.join(BASE, "project2"), os.path.join(BASE, "common")):
        if p not in sys.path:
            sys.path.insert(0, p)


def fee_refs():
    """费率从 `project2/execution_cost.py` 读。"""
    _sys_paths()
    import execution_cost as ec
    return {"spot": ec.FEE_SPOT, "perp_maker": ec.FEE_PERP_MAKER,
            "perp_taker": ec.FEE_PERP_TAKER}


def edge_threshold_bp():
    """门槛从 `project2/agent_team.py` 读（EDGE_THRESHOLD_BP = 11.34）。"""
    _sys_paths()
    import agent_team
    return float(agent_team.EDGE_THRESHOLD_BP)


def route_of_cached(ts_ms, _cache={}):
    """路由判定用 `common/market_calendar.route_of`（唯一口径），按毫秒缓存。"""
    r = _cache.get(ts_ms)
    if r is None:
        _sys_paths()
        import market_calendar as mc
        r = mc.route_of(ts_ms)
        _cache[ts_ms] = r
    return r


def fee_side_bp(route, mode, refs):
    """**单程**手续费（bp，现货+永续两条腿各一笔）。

    taker：现货 + 永续 taker。
    maker：仅 `in_house` 区分 maker/taker；`stockroute` 所有订单按 Taker（DATA_DICT §1）。
    """
    if mode == "taker" or route != "in_house":
        return refs["spot"] + refs["perp_taker"]
    return refs["spot"] + refs["perp_maker"]


def fee_roundtrip_bp(route, mode, refs):
    """往返手续费（bp）= 2 × 单程。与 docs/14 的 `fee_all_maker_bp=14.0`、
    `fee_perp_taker_bp=22.0` 同一口径。"""
    return 2.0 * fee_side_bp(route, mode, refs)


# ────────────────────────────────────────────── 数据装载与对齐

def load_rounds(path):
    """读轮级盘口 -> {base: {venue: [(ts_ms, bid, ask, mid, spread_bp), ...升序]}}。

    ⚠️ bid/ask 缺失或非正的行跳过（冻结快照里有极少数不完整轮）——
       但**计数并报告**，不静默吞掉。
    """
    import csv
    out, skipped = {}, 0
    with io.open(path, encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                ts = int(r["ts_ms"])
                bid = float(r["bid"])
                ask = float(r["ask"])
                spread = float(r["spread_bp"])
            except (KeyError, TypeError, ValueError):
                skipped += 1
                continue
            if bid <= 0 or ask <= 0 or ask < bid:
                skipped += 1
                continue
            base, venue = r["base"].upper(), r["venue"].lower()
            out.setdefault(base, {}).setdefault(venue, []).append(
                (ts, bid, ask, (bid + ask) / 2.0, spread))
    for base in out:
        for venue in out[base]:
            out[base][venue].sort(key=lambda x: x[0])
    return out, skipped


def align_pairs(rounds, tol_ms=ALIGN_TOL_MS):
    """perp 轮就近对齐到 spot 轮。

    返回 10 元组列表（按 spot 时间升序）：
        (ts_s, ts_p, s_bid, s_ask, s_mid, hs_bp, p_bid, p_ask, p_mid, hp_bp)
    """
    spot = rounds.get("spot") or []
    perp = rounds.get("perp") or []
    if not spot or not perp:
        return []
    p_ts = [x[0] for x in perp]
    out = []
    for (ts, s_bid, s_ask, s_mid, s_sp) in spot:
        i = bisect.bisect_left(p_ts, ts)
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(perp):
                d = abs(perp[j][0] - ts)
                if best is None or d < best[0]:
                    best = (d, j)
        if best is None or best[0] > tol_ms:
            continue
        _, j = best
        _, p_bid, p_ask, p_mid, p_sp = perp[j]
        out.append((ts, perp[j][0], s_bid, s_ask, s_mid, s_sp / 2.0,
                    p_bid, p_ask, p_mid, p_sp / 2.0))
    return out


# ────────────────────────────────────────────── 重放核心（纯函数，可自检）

def hedged_pnl_bp(s_mid_t, s_mid_h, p_mid_t, p_mid_h):
    """对冲双腿的已实现价格盈亏（bp，名义额 = 进场现货中间价）。

    买 1 单位现货 + 卖 1 单位永续：PnL = (S_H − S_t) − (P_H − P_t)。
    两腿对冲后剩下的就是基差漂移 —— 这是策略**真实承担**的风险。
    """
    if s_mid_t <= 0:
        return None
    return ((s_mid_h - s_mid_t) - (p_mid_h - p_mid_t)) / s_mid_t * 1e4


def pnl_two_modes(a_t, a_h, route_t, route_h, refs):
    """两条口径的净盈亏（bp）。

    a = 对齐轮 10 元组（见 align_pairs）。返回 (pnl_taker, pnl_maker)。

      taker：价格腿 − 进场两腿半幅点差 − 出场两腿半幅点差 − taker 往返费
      maker：价格腿 + 进场两腿半幅点差 + 出场两腿半幅点差 − 按路由的往返费
             （stockroute 出场按 Taker 计费，所以出场半幅点差仍要**付**）
    """
    price = hedged_pnl_bp(a_t[4], a_h[4], a_t[8], a_h[8])
    if price is None:
        return None, None
    half_t = a_t[5] + a_t[9]
    half_h = a_h[5] + a_h[9]
    fee_taker = fee_side_bp(route_t, "taker", refs) \
        + fee_side_bp(route_h, "taker", refs)
    pnl_taker = price - half_t - half_h - fee_taker
    fee_maker = fee_side_bp(route_t, "maker", refs) \
        + fee_side_bp(route_h, "maker", refs)
    if route_t != "in_house" or route_h != "in_house":
        # 任一端不是 in_house -> 那一端按 Taker 计费且**付**半幅点差
        pnl_maker = (price
                     + (half_t if route_t == "in_house" else -half_t)
                     + (half_h if route_h == "in_house" else -half_h)
                     - fee_maker)
    else:
        pnl_maker = price + half_t + half_h - fee_maker
    return pnl_taker, pnl_maker


def edge_maker_bp(hs, hp, route, refs):
    """系统的账面优势（`entry_math` 公式：2×(half_s+half_p) − 往返手续费）。"""
    return 2.0 * (hs + hp) - fee_roundtrip_bp(route, "maker", refs)


def _exit_index(aligned, i, hold_ms):
    """第一个 perp 轮时间 ≥ ts+H 的对齐轮下标；找不到返回 None。"""
    ts = aligned[i][1]
    for k in range(i + 1, len(aligned)):
        if aligned[k][1] >= ts + hold_ms:
            return k
    return None


def pick_signals(aligned, threshold_bp, refs, hold_min):
    """非重叠信号抽取。返回 (signals, skipped_overlap)。

    signal = {idx, exit, ts, route, hs, hp, edge}；exit=None 表示窗口末尾无法出场。
    """
    out, skipped = [], 0
    hold_ms = hold_min * 60_000
    last_fire = None
    for i, a in enumerate(aligned):
        ts = a[0]
        if last_fire is not None and ts - last_fire < hold_ms:
            skipped += 1
            continue
        hs, hp = a[5], a[9]
        route = route_of_cached(ts)
        edge = edge_maker_bp(hs, hp, route, refs)
        if edge < threshold_bp:
            continue
        out.append({"idx": i, "exit": _exit_index(aligned, i, hold_ms),
                    "ts": ts, "route": route, "hs": hs, "hp": hp, "edge": edge})
        last_fire = ts
    return out, skipped


def evaluate_signals(aligned, sigs, refs):
    """给信号算两条口径的已实现盈亏。返回 (taker_vals, maker_vals, n_no_exit)。"""
    pt, pm, no_exit = [], [], 0
    for s in sigs:
        if s["exit"] is None:
            no_exit += 1
            continue
        a_t, a_h = aligned[s["idx"]], aligned[s["exit"]]
        x, m = pnl_two_modes(a_t, a_h, s["route"], route_of_cached(a_h[1]), refs)
        if x is None:
            continue
        pt.append(x)
        pm.append(m)
    return pt, pm, no_exit


def baseline_rounds(aligned, hold_min):
    """基线：全体对齐轮不过滤直接进场（同样的不重叠规则）。返回 (i, j) 对。"""
    pairs, last = [], None
    hold_ms = hold_min * 60_000
    for i, a in enumerate(aligned):
        ts = a[0]
        if last is not None and ts - last < hold_ms:
            continue
        j = _exit_index(aligned, i, hold_ms)
        if j is None:
            continue
        pairs.append((i, j))
        last = ts
    return pairs


def stats_of(vals):
    """胜率与分布。空列表 -> None（**不编 0**：缺数据与"零胜率"是两回事）。"""
    if not vals:
        return None
    vs = sorted(vals)
    n = len(vs)
    return {"n": n,
            "win_rate": round(sum(1 for v in vs if v > 0) / n, 4),
            "mean_bp": round(st.mean(vs), 2),
            "median_bp": round(st.median(vs), 2),
            "p5_bp": round(vs[max(0, int(0.05 * n) - 1)], 2),
            "p95_bp": round(vs[min(n - 1, int(0.95 * n))], 2),
            "worst_bp": round(vs[0], 2),
            "best_bp": round(vs[-1], 2)}


# ────────────────────────────────────────────── 主流程

def run_replay(horizons=HORIZONS_MIN, verbose=False, log=print):
    """对快照里每个日期文件做重放。返回汇总 dict（含逐信号 rows，不落盘）。"""
    files = sorted(glob.glob(os.path.join(SPREAD_DIR, ROUND_FILE_GLOB)))
    if not files:
        return {"status": "unavailable",
                "why": "找不到轮级盘口快照（data/spread/%s）" % ROUND_FILE_GLOB}
    refs = fee_refs()
    thr = edge_threshold_bp()
    summary = {"status": "ok", "threshold_bp": thr, "fee_refs": refs,
               "horizons_min": list(horizons), "files": [], "totals": {}}
    all_rows = []
    for path in files:
        name = os.path.basename(path)
        rounds, skipped = load_rounds(path)
        bases = sorted(b for b in rounds
                       if "spot" in rounds[b] and "perp" in rounds[b])
        file_res = {"file": name, "skipped_bad_rows": skipped,
                    "bases": bases, "by_horizon": {}}
        if verbose:
            log("   窗口文件 %s：坏行 %d，可对齐标的 %d 个" % (name, skipped, len(bases)))
        for H in horizons:
            sig_all, base_all = [], []
            per_base = {}
            for base in bases:
                aligned = align_pairs(rounds[base])
                if len(aligned) < 30:
                    continue
                sigs, skip_ov = pick_signals(aligned, thr, refs, H)
                pt, pm, no_exit = evaluate_signals(aligned, sigs, refs)
                for s, x, m in zip([s for s in sigs if s["exit"] is not None],
                                   pt, pm):
                    a_t = aligned[s["idx"]]
                    row = {"file": name, "base": base, "horizon_min": H,
                           "ts_utc": dt.datetime.fromtimestamp(
                               a_t[0] / 1000, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                           "route": s["route"], "edge_maker_bp": round(s["edge"], 2),
                           "half_spot_bp": round(a_t[5], 3),
                           "half_perp_bp": round(a_t[9], 3),
                           "pnl_taker_bp": round(x, 2), "pnl_maker_bp": round(m, 2)}
                    all_rows.append(row)
                    sig_all.append((x, m))
                pairs = baseline_rounds(aligned, H)
                for i, j in pairs:
                    x, m = pnl_two_modes(aligned[i], aligned[j],
                                         route_of_cached(aligned[i][0]),
                                         route_of_cached(aligned[j][1]), refs)
                    if x is None:
                        continue
                    base_all.append((x, m))
                per_base[base] = {
                    "n_signals": len(sigs), "n_no_exit": no_exit,
                    "overlap_skipped": skip_ov,
                    "signal_taker": stats_of(pt), "signal_maker": stats_of(pm)}
            file_res["by_horizon"][str(H)] = {
                "per_base": per_base,
                "signal": {"n": len(sig_all),
                           "taker": stats_of([x for x, _ in sig_all]),
                           "maker": stats_of([m for _, m in sig_all])}}
        summary["files"].append(file_res)

    # 跨文件合并（按 horizon × 口径）
    merged = {}
    for f in summary["files"]:
        for H, d in f["by_horizon"].items():
            m = merged.setdefault(H, {"t": [], "m": []})
            if d["signal"]["taker"]:
                pass          # 数值在上面逐行收集，这里只聚合 rows（见下）
    import collections
    agg = collections.defaultdict(lambda: {"t": [], "m": []})
    for row in all_rows:
        agg[str(row["horizon_min"])]["t"].append(row["pnl_taker_bp"])
        agg[str(row["horizon_min"])]["m"].append(row["pnl_maker_bp"])
    # 🔴 按路由拆分：maker 只在 `in_house` 有意义 —— stockroute 里的"宽点差信号"
    #    是陷阱（挂单按 Taker 计费），必须单独摆出来，不能混进 maker 口径里。
    by_route = collections.defaultdict(lambda: collections.defaultdict(lambda: {"t": [], "m": []}))
    for row in all_rows:
        by_route[str(row["horizon_min"])][row["route"]]["t"].append(row["pnl_taker_bp"])
        by_route[str(row["horizon_min"])][row["route"]]["m"].append(row["pnl_maker_bp"])
    summary["totals"] = {
        H: {"n_signals": len(v["t"]),
            "signal": {"taker": stats_of(v["t"]), "maker": stats_of(v["m"])},
            "by_route": {rt: {"n": len(d["t"]),
                              "maker": stats_of(d["m"]), "taker": stats_of(d["t"])}
                         for rt, d in sorted(by_route[H].items())}}
        for H, v in sorted(agg.items(), key=lambda kv: int(kv[0]))}
    summary["rows"] = all_rows
    return summary


def baseline_stats(horizons=HORIZONS_MIN):
    """基线（不过滤）的合并统计 —— 独立算一遍，避免与信号路径耦合。"""
    files = sorted(glob.glob(os.path.join(SPREAD_DIR, ROUND_FILE_GLOB)))
    refs = fee_refs()
    out = {}
    for path in files:
        rounds, _ = load_rounds(path)
        bases = [b for b in rounds if "spot" in rounds[b] and "perp" in rounds[b]]
        for H in horizons:
            d = out.setdefault(str(H), {"t": [], "m": []})
            for base in bases:
                aligned = align_pairs(rounds[base])
                for i, j in baseline_rounds(aligned, H):
                    x, m = pnl_two_modes(aligned[i], aligned[j],
                                         route_of_cached(aligned[i][0]),
                                         route_of_cached(aligned[j][1]), refs)
                    if x is None:
                        continue
                    d["t"].append(x)
                    d["m"].append(m)
    return {H: {"n": len(v["t"]),
                "taker": stats_of(v["t"]), "maker": stats_of(v["m"])}
            for H, v in sorted(out.items(), key=lambda kv: int(kv[0]))}


# ────────────────────────────────────────────── 报告与落盘

def print_report(res, base_stats, log=print):
    if res.get("status") != "ok":
        log("❌ %s" % res.get("why"))
        return 1
    log("📊 快照重放回测（确定性重放，全部可复跑）")
    log("   门槛：EDGE_THRESHOLD_BP = %s（从 agent_team.py 读）" % res["threshold_bp"])
    log("   费率：现货 %s / 永续 maker %s / taker %s bp（从 execution_cost.py 读）"
        % (res["fee_refs"]["spot"], res["fee_refs"]["perp_maker"],
           res["fee_refs"]["perp_taker"]))
    log("")
    for H, s in res["totals"].items():
        b = base_stats.get(H) or {}
        log("   ── 持仓 %s 分钟 ──" % H)
        for mode, label in (("maker", "maker 乐观上界"), ("taker", "taker 保守下界")):
            sig = s["signal"][mode]
            bl = b.get(mode)
            if not sig:
                log("     %-14s 无信号（该窗口内没有一轮过得了门槛）" % label)
                continue
            log("     %-13s 信号 %5d 笔 ｜ 胜率 %5.1f%% ｜ 均值 %8.2f bp ｜ "
                "中位 %8.2f bp ｜ 最差 %9.2f bp"
                % (label, sig["n"], sig["win_rate"] * 100, sig["mean_bp"],
                   sig["median_bp"], sig["worst_bp"]))
            if bl and bl["n"]:
                log("     %-13s 基线 %5d 笔 ｜ 胜率 %5.1f%% ｜ 均值 %8.2f bp ｜ "
                    "门槛增量：胜率 %+.1f pp ｜ 均值 %+.2f bp"
                    % ("（不过滤）", bl["n"], bl["win_rate"] * 100, bl["mean_bp"],
                       (sig["win_rate"] - bl["win_rate"]) * 100,
                       sig["mean_bp"] - bl["mean_bp"]))
        for rt, d in (s.get("by_route") or {}).items():
            mk = d.get("maker")
            if mk:
                log("     · 按路由 %-10s maker %3d 笔 ｜ 胜率 %5.1f%% ｜ 均值 %8.2f bp"
                    % (rt, mk["n"], mk["win_rate"] * 100, mk["mean_bp"]))
        log("")
    return 0


def save(res, base_stats, out_dir=OUT_DIR):
    """落盘证据（逐信号明细 CSV + 汇总 JSON）。返回 (ok, 说明)。"""
    if res.get("status") != "ok":
        return False, res.get("why")
    os.makedirs(out_dir, exist_ok=True)
    import csv
    rows = res.get("rows") or []
    fields = ["file", "base", "horizon_min", "ts_utc", "route", "edge_maker_bp",
              "half_spot_bp", "half_perp_bp", "pnl_taker_bp", "pnl_maker_bp"]
    csv_path = os.path.join(out_dir, "replay_signals.csv")
    with io.open(csv_path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in fields})
    js = {k: v for k, v in res.items() if k != "rows"}
    js["baseline"] = base_stats
    js_path = os.path.join(out_dir, "replay_summary.json")
    with io.open(js_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(js, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    return True, "%s + %s（%d 笔信号明细）" % (
        os.path.relpath(csv_path, BASE), os.path.relpath(js_path, BASE), len(rows))


# ────────────────────────────────────────────── 离线自检

def selftest():
    """离线自检：公式 / 费率口径 / 门槛来源 / 不重叠 / 对齐 / 缺数据语义 / 只读边界。"""
    ok = True

    def chk(cond, msg):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s" % ("OK " if cond else "!! ", msg))

    refs = fee_refs()

    def mk(ts, hs, hp, s_mid=100.4, p_mid=100.3):
        """造一个对齐轮（bid/ask 由 mid±half 推出）。"""
        return (ts, ts, s_mid - hs, s_mid + hs, s_mid, hs,
                p_mid - hp, p_mid + hp, p_mid, hp)

    # ① 费率口径（数值从 execution_cost.py 读，不重抄字面量）
    chk(abs(fee_roundtrip_bp("in_house", "taker", refs)
            - 2 * (refs["spot"] + refs["perp_taker"])) < 1e-9,
        "taker 往返 = 2×(现货+永续taker) = %.1f bp（docs/14 的 22.0 同口径）"
        % fee_roundtrip_bp("in_house", "taker", refs))
    chk(abs(fee_roundtrip_bp("in_house", "maker", refs)
            - 2 * (refs["spot"] + refs["perp_maker"])) < 1e-9,
        "in_house maker 往返 = %.1f bp（fee_all_maker_bp 同口径）"
        % fee_roundtrip_bp("in_house", "maker", refs))
    chk(fee_roundtrip_bp("stockroute", "maker", refs)
        == fee_roundtrip_bp("stockroute", "taker", refs),
        "stockroute 的 maker == taker（所有订单按 Taker，DATA_DICT §1）")

    # ② 门槛来自 agent_team（不重抄字面量）
    chk(abs(edge_threshold_bp() - 11.34) < 1e-9,
        "EDGE_THRESHOLD_BP = %.2f（与 agent_team.py / docs/14 同源）" % edge_threshold_bp())

    # ③ 对冲盈亏公式：手算样例。S: 100→101，P: 100.5→101.4
    #    ((101−100) − (101.4−100.5))/100 × 1e4 = 10 bp（基差收敛）
    chk(abs(hedged_pnl_bp(100.0, 101.0, 100.5, 101.4) - 10.0) < 1e-6,
        "对冲盈亏公式：手算样例 = 10 bp（基差收敛 10bp）")
    chk(abs(hedged_pnl_bp(100.0, 101.0, 100.5, 101.5)) < 1e-6,
        "两腿同幅上涨 -> 对冲后 ≈ 0：策略**不赌方向**，赌的是基差")

    # ④ 价格不动时：taker 必亏（付点差+22bp 费），in_house maker 视点差大小而定
    a = mk(0, 5.0, 5.0)
    pt, pm = pnl_two_modes(a, a, "in_house", "in_house", refs)
    chk(pt < 0, "价格不动 + taker：%.2f bp（必亏：付两腿点差 + 22bp 费）" % pt)
    chk(pm > 0, "价格不动 + in_house maker：%.2f bp（赚 2×(5+5)=20bp 点差 − 14bp 费）" % pm)
    # stockroute 端按 Taker -> 那一端的半幅点差要**付**
    pt2, pm2 = pnl_two_modes(a, a, "stockroute", "stockroute", refs)
    chk(abs(pm2 - pt2) < 1e-9,
        "stockroute 下两条口径重合（%.2f）：挂单无意义，全部按 Taker" % pm2)

    # ⑤ 不重叠：60s 一轮、half=12（edge=2×24−费 ≥ 26，**无论路由**都过门槛），H=15min
    t0 = 1789800000000
    aligned = [mk(t0 + i * 60_000, 12.0, 12.0) for i in range(40)]
    sigs, skipped = pick_signals(aligned, edge_threshold_bp(), refs, 15)
    fires = [s["ts"] for s in sigs]
    chk(len(sigs) == 3 and skipped > 0,
        "非重叠抽取：40 轮里只触发 %d 个（跳过 %d 个相邻轮）" % (len(sigs), skipped))
    chk(all(b - a >= 15 * 60_000 for a, b in zip(fires, fires[1:])),
        "相邻触发间隔 ≥ 持仓期（自相关去重生效）")

    # ⑥ 对齐容差：perp 轮偏 20s（> ±15s）不应对齐
    spot = [(1789800000000, 100.0, 100.8, 100.4, 1.6)]
    perp = [(1789800020000, 100.0, 100.6, 100.3, 1.2)]
    chk(align_pairs({"spot": spot, "perp": perp}) == [],
        "对齐容差 ±15s：偏 20s 的轮**不对齐**（宁缺毋滥）")

    # ⑦ 窗口末尾的信号 exit=None（单独计数，不混入胜率）
    sigs2, _ = pick_signals([mk(t0 + i * 60_000, 12.0, 12.0) for i in range(3)],
                            edge_threshold_bp(), refs, 15)
    chk(len(sigs2) == 1 and sigs2[0]["exit"] is None,
        "窗口末尾信号 exit=None -> 计入「无法出场」")

    # ⑧ in_house 与 stockroute 的费率差会影响门槛通过率
    chk(fee_roundtrip_bp("in_house", "maker", refs)
        < fee_roundtrip_bp("stockroute", "maker", refs),
        "同一窗口内两种计费制度并存：信号按**各自轮**的路由计费（route_of 判定）")

    # ⑨ 快照目录**只读**：源码里对 SPREAD_DIR 的 open 不允许出现写模式
    with io.open(os.path.abspath(__file__), encoding="utf-8") as fh:
        src = fh.read()
    bad = re.findall(r"open\([^)]*SPREAD_DIR[^)]*['\"]w", src)
    chk(not bad, "对 data/spread/ 只有读（写路径只指向 data/backtest/）")

    print("\n快照重放回测自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


# ────────────────────────────────────────────── 入口

def main(argv=None):
    ap = argparse.ArgumentParser(description="📊 快照重放回测（可核验的胜率）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-save", action="store_true", help="不落盘证据文件")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)

    if a.selftest:
        return selftest()

    res = run_replay()
    if res.get("status") != "ok":
        print("❌ %s" % res.get("why"))
        return 1
    base_stats = baseline_stats()
    if a.json:
        out = {k: v for k, v in res.items() if k != "rows"}
        out["baseline"] = base_stats
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    rc = print_report(res, base_stats)
    if not a.no_save:
        okw, msg = save(res, base_stats)
        print("   %s" % ("证据落盘：" + msg if okw else "❌ " + msg))
    return rc


if __name__ == "__main__":
    sys.exit(main())
