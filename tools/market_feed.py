#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
📡 本项目自带的行情通道（实时补差 / 采样落盘）
================================================

用户 2026-09-20 的三条要求，本工具就是它们的落点：

  ① 「这个项目仓库也需要一个采集器来运行采集」
  ② 「目前情况下只用项目一所采集到的样本数据，定期导出到这边项目二仓库」
  ③ 「在未同步期间使用实时信息但**不采集**而已，只是补充项目一和项目二的
     样本差，缺口补足」

━━ 所以本工具有三种模式，边界写死在代码里 ━━

  ``--live``    按需取**一次**实时行情，**绝不落盘**。
                用途：项目一还没同步过来时，把"快照最后一刻 → 现在"这段
                **缺口**补上，让页面/决策看到的是当前市场，而不是 33 小时前的。
                —— 这就是用户说的"使用实时信息但不采集"。

  ``--sample``  采一次并**落盘**到 ``data/live/``（不是 ``data/spread/``！），
                为将来"本仓库独立持续采集"做准备。落盘目录刻意与冻结快照分开。

  ``--gap``     只报告缺口：快照最后时刻 vs 现在，差多少分钟；
                以及实时通道**能不能**补上（通不通）。

━━ 🔴 一条硬规矩：绝不写进 data/spread/ ━━

``data/spread/`` 是本仓库**已验证的证据基座**（26 项冻结哈希、``--verify``
逐字节通过、材料里每个数字都点回它）。采样器往里写一个字节，整套"可核验"
就废了。所以：

  · 落盘目录固定 ``data/live/``，且 ``--sample`` 会**断言**目标不在 spread 下；
  · 自检里有一条专门测这个守卫（``--selftest``）。

━━ 上游与代理（实测结论，不是猜） ━━

Bitget 公开行情**不需要账号、不需要 API key**。
但本机**直连被 ISP 掐断**（``URLError 10054``），必须走本机代理
``http://127.0.0.1:7890``（实测 3/3 通）。所以默认用代理，可用 ``--no-proxy`` 覆盖。

用法::

    python tools/market_feed.py --live --base NVDA          # 取一次，不落盘
    python tools/market_feed.py --live --json               # 机器可读
    python tools/market_feed.py --sample --bases NVDA,TSLA  # 落盘到 data/live/
    python tools/market_feed.py --gap                       # 只报告缺口
    python tools/market_feed.py --selftest                  # 离线自检纯函数
"""

import argparse
import csv
import datetime as dt
import glob
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPREAD = os.path.join(BASE, "data", "spread")
LIVE_DIR = os.path.join(BASE, "data", "live")      # 落盘只允许在这里
DEFAULT_BASES = ["NVDA", "TSLA", "AAPL", "META", "GOOGL",
                 "SPY", "QQQ", "SOXL", "HOOD", "MRVL"]
PROXY = os.environ.get("P2_PROXY", "http://127.0.0.1:7890")
API = "https://api.bitget.com/api/v2"
TIMEOUT = 20
RETRY = 3

# 与快照 CSV 一致的列（这样将来切换数据源时下游不用改）
QUOTE_COLS = ["ts_utc", "ts_ms", "date_cn", "symbol", "venue", "base",
              "bid", "ask", "mid", "spread_bp", "bid_sz", "ask_sz", "last"]
DEPTH_COLS = ["ts_utc", "ts_ms", "date_cn", "base", "symbol", "venue",
              "side", "level", "price", "size", "notional_usd", "cum_notional_usd"]
TRADE_COLS = ["ts_utc", "ts_ms", "date_cn", "base", "symbol", "venue",
              "trade_id", "side", "price", "size", "notional_usd"]


# ---------------------------------------------------------------- 纯函数

def spread_bp(bid, ask):
    """点差（bp）。中点用 (bid+ask)/2，与快照口径一致。**读不到就返回 None**。"""
    try:
        b, a = float(bid), float(ask)
    except (TypeError, ValueError):
        return None
    mid = (a + b) / 2.0
    if mid <= 0:
        return None
    return (a - b) / mid * 10000.0


def _iso(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).strftime(
        "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _date_cn(ms):
    # 北京日期（与快照的 date_cn 一致）
    return (dt.datetime.fromtimestamp(ms / 1000, dt.UTC)
            + dt.timedelta(hours=8)).strftime("%Y-%m-%d")


def sniff(t):
    """嗅探是哪个 venue 的 ticker（两个接口字段名一样，但要标明来源）。"""
    sym = str((t or {}).get("symbol") or "")
    return "spot" if sym.startswith("R") else "perp"


def quote_row(base, venue, ticker):
    """把交易所 ticker 归一成快照 quote 的一行。字段缺就留空，**不编**。"""
    if not isinstance(ticker, dict):
        return None
    ms = int(ticker.get("ts") or int(time.time() * 1000))
    bid, ask = ticker.get("bidPr"), ticker.get("askPr")
    mid = None
    try:
        mid = (float(bid) + float(ask)) / 2.0
    except (TypeError, ValueError):
        mid = None
    sbp = spread_bp(bid, ask)
    return {
        "ts_utc": _iso(ms), "ts_ms": ms, "date_cn": _date_cn(ms),
        "symbol": ticker.get("symbol") or "", "venue": venue, "base": base,
        "bid": bid, "ask": ask,
        "mid": None if mid is None else round(mid, 8),
        "spread_bp": None if sbp is None else round(sbp, 6),
        "bid_sz": ticker.get("bidSz"), "ask_sz": ticker.get("askSz"),
        "last": ticker.get("lastPr"),
    }


def depth_rows(base, venue, symbol, depth, levels=5):
    """把 merge-depth 归一成快照 orderbook 的行（含累计名义额）。"""
    out = []
    if not isinstance(depth, dict):
        return out
    ms = int(depth.get("ts") or int(time.time() * 1000))
    for side in ("bids", "asks"):
        cum = 0.0
        for i, lv in enumerate((depth.get(side) or [])[:levels], start=1):
            try:
                price, size = float(lv[0]), float(lv[1])
            except (IndexError, TypeError, ValueError):
                continue
            notional = price * size
            cum += notional
            out.append({"ts_utc": _iso(ms), "ts_ms": ms, "date_cn": _date_cn(ms),
                        "base": base, "symbol": symbol, "venue": venue,
                        "side": side[:-1], "level": i,
                        "price": price, "size": size,
                        "notional_usd": round(notional, 6),
                        "cum_notional_usd": round(cum, 6)})
    return out


def trade_rows(base, venue, symbol, fills):
    """把 fills-history 归一成快照 trades 的行。"""
    out = []
    for f in (fills or []):
        try:
            ms = int(f.get("ts") or f.get("cTime") or 0)
            price, size = float(f.get("price")), float(f.get("size"))
        except (TypeError, ValueError):
            continue
        if ms <= 0:
            continue
        out.append({"ts_utc": _iso(ms), "ts_ms": ms, "date_cn": _date_cn(ms),
                    "base": base, "symbol": symbol, "venue": venue,
                    "trade_id": f.get("tradeId") or "", "side": f.get("side") or "",
                    "price": price, "size": size,
                    "notional_usd": round(price * size, 6)})
    return out


def guard_target(path):
    """🔴 落盘守卫：**绝不允许**写进 data/spread/（那是冻结的证据基座）。

    这个函数存在的唯一目的是能被自检直接测 —— 一条规则如果只写在注释里，
    迟早会被一次"顺手改个路径"绕过。
    """
    ap = os.path.abspath(path)
    sp = os.path.abspath(SPREAD)
    if ap == sp or ap.startswith(sp + os.sep):
        raise ValueError(
            "拒绝写入 %s：data/spread/ 是**已验证的冻结快照**（SNAPSHOT.md 逐字节核验）。"
            "采样与实时数据只能写 data/live/。" % path)
    return ap


def snapshot_last_ms():
    """冻结快照的最后一刻（与 agent_team.data_asof_ms 同源思路，独立实现便于单测）。"""
    best = None
    for pat in ("trades-*.csv", "orderbook-*.csv", "sentiment-*.csv", "2*.csv"):
        for p in sorted(glob.glob(os.path.join(SPREAD, pat)))[-1:]:
            try:
                with io.open(p, "rb") as fh:
                    fh.seek(0, os.SEEK_END)
                    fh.seek(max(0, fh.tell() - 8192))
                    tail = fh.read().decode("utf-8", "ignore").strip().splitlines()
                for ln in reversed(tail):
                    parts = ln.split(",")
                    if len(parts) > 1 and parts[1].isdigit():
                        best = max(best or 0, int(parts[1]))
                        break
            except OSError:
                continue
    return best


def gap_report(now_ms=None, live_ok=None, live_ms=None):
    """缺口报告：快照停在哪、现在几点、差多少、实时通道通不通。"""
    now = int(now_ms or time.time() * 1000)
    snap = snapshot_last_ms()
    r = {"now_ms": now, "snapshot_last_ms": snap,
         "snapshot_last_utc": _iso(snap) if snap else None,
         "gap_min": round((now - snap) / 60000.0, 1) if snap else None,
         "live_reachable": live_ok, "live_ms": live_ms}
    if snap is None:
        r["verdict"] = "no_snapshot"
        r["why"] = "读不到快照的任何 ts_ms —— 不猜缺口有多大"
    elif r["gap_min"] is not None and r["gap_min"] <= 0:
        r["verdict"] = "no_gap"
        r["why"] = "快照不比现在旧（gap ≤ 0）"
    elif live_ok:
        r["verdict"] = "gap_fillable"
        r["why"] = ("快照落后 %.0f 分钟；实时通道**可用** —— 用 `--live` 按需补差"
                    "（不落盘），或等下一次从项目一导出" % r["gap_min"])
    else:
        r["verdict"] = "gap_unfillable"
        r["why"] = ("快照落后 %.0f 分钟，而实时通道**当前不可用** —— "
                    "只剩「等下一次从项目一导出」这一条路" % r["gap_min"])
    return r


# ---------------------------------------------------------------- 网络

def opener(use_proxy=True):
    if use_proxy:
        h = urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
    else:
        h = urllib.request.ProxyHandler({})
    return urllib.request.build_opener(h)


def get_json(op, url):
    """带重试的 GET。**把失败原因如实带回去**，不吞。"""
    last = None
    for i in range(RETRY):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "p2-market-feed"})
            with op.open(req, timeout=TIMEOUT) as r:
                d = json.loads(r.read().decode("utf-8"))
            if str(d.get("code")) != "00000":
                return None, "交易所返回 code=%s msg=%s" % (d.get("code"), d.get("msg"))
            return d.get("data"), None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = "%s: %s" % (type(exc).__name__, str(exc)[:80])
            if i < RETRY - 1:
                time.sleep(1.5 * (i + 1))
    return None, last


def spot_sym(base):
    return "R%sUSDT" % base.upper()


def perp_sym(base):
    return "%sUSDT" % base.upper()


def fetch_base(op, base, fills_limit=20):
    """取一个标的的实时行情。每一项**独立报告成败**（不因一项失败就全丢）。"""
    out = {"base": base.upper(), "errors": []}
    for venue, sym, mk in (("spot", spot_sym(base), "spot"), ("perp", perp_sym(base), "mix")):
        if mk == "spot":
            turl = "%s/spot/market/tickers?symbol=%s" % (API, sym)
            durl = "%s/spot/market/merge-depth?symbol=%s&limit=5" % (API, sym)
            furl = "%s/spot/market/fills-history?symbol=%s&limit=%d" % (API, sym, fills_limit)
        else:
            turl = ("%s/mix/market/ticker?symbol=%s&productType=usdt-futures"
                    % (API, sym))
            durl = ("%s/mix/market/merge-depth?symbol=%s&productType=usdt-futures&limit=5"
                    % (API, sym))
            furl = ("%s/mix/market/fills-history?symbol=%s&productType=usdt-futures&limit=%d"
                    % (API, sym, fills_limit))
        d, err = get_json(op, turl)
        if err:
            out["errors"].append("%s ticker: %s" % (venue, err))
        else:
            t = d[0] if isinstance(d, list) and d else d
            row = quote_row(out["base"], venue, t)
            if row:
                out.setdefault("quotes", {})[venue] = row
        d, err = get_json(op, durl)
        if err:
            out["errors"].append("%s depth: %s" % (venue, err))
        else:
            rows = depth_rows(out["base"], venue, sym, d)
            if rows:
                out.setdefault("depth", {})[venue] = rows
        d, err = get_json(op, furl)
        if err:
            out["errors"].append("%s fills: %s" % (venue, err))
        else:
            rows = trade_rows(out["base"], venue, sym, d)
            if rows:
                out.setdefault("fills", {})[venue] = rows
    # 现货-永续价差（做市型价差捕获关心的就是它）
    q = out.get("quotes") or {}
    try:
        s, p = q["spot"], q["perp"]
        out["basis_bp"] = round(
            (float(p["mid"]) - float(s["mid"])) / float(s["mid"]) * 10000.0, 4)
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        out["basis_bp"] = None
    return out


def live_snapshot(bases, use_proxy=True, fills_limit=20):
    op = opener(use_proxy)
    # 先打一次 public/time 判可达性（一次失败就不要把十个标的都试一遍）
    t, err = get_json(op, "%s/public/time" % API)
    reachable = err is None
    data = {"ok": reachable, "reachable": reachable, "proxy": PROXY if use_proxy else None,
            "fetched_utc": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "bases": [], "error": err}
    if not reachable:
        data["why"] = ("实时通道不可达：%s —— 直连被 ISP 掐断、代理未启用或代理挂了。"
                       "**不假装拿到了实时数据。**" % err)
        return data
    for b in bases:
        data["bases"].append(fetch_base(op, b, fills_limit=fills_limit))
    return data


# ---------------------------------------------------------------- 落盘

def write_csv(path, cols, rows, append=True):
    guard_target(path)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    exists = os.path.exists(path) and os.path.getsize(path) > 0
    mode = "a" if (append and exists) else "w"
    with io.open(path, mode, encoding="utf-8", newline="\n") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        if mode == "w":
            w.writeheader()
        for r in rows:
            w.writerow(r)
    return path


def persist(snap, outdir=LIVE_DIR):
    """把一次实时快照落盘到 ``data/live/``（**永远不是 spread**）。"""
    written = []
    day = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d")
    guard_target(outdir)
    for b in snap.get("bases", []):
        base = b["base"]
        for venue, row in (b.get("quotes") or {}).items():
            written.append(write_csv(
                os.path.join(outdir, "quote-%s.csv" % day), QUOTE_COLS, [row]))
        for venue, rows in (b.get("depth") or {}).items():
            written.append(write_csv(
                os.path.join(outdir, "orderbook-%s.csv" % day), DEPTH_COLS, rows))
        for venue, rows in (b.get("fills") or {}).items():
            written.append(write_csv(
                os.path.join(outdir, "trades-%s.csv" % day), TRADE_COLS, rows))
    return sorted(set(written))


# ---------------------------------------------------------------- 渲染

def render_live(snap, max_show=10):
    L = []
    if not snap.get("ok"):
        return "📡 实时通道**不可用** —— %s" % snap.get("why")
    L.append("📡 实时行情（**不落盘**）｜ %s ｜ 代理 %s"
             % (snap["fetched_utc"], snap.get("proxy") or "未用"))
    L.append("   %-7s %10s %10s %10s %10s %10s"
             % ("标的", "现货bid", "现货ask", "永续bid", "永续ask", "价差bp"))
    for b in snap["bases"][:max_show]:
        q = b.get("quotes") or {}
        def g(v, k):
            return (q.get(v) or {}).get(k) or "—"
        L.append("   %-7s %10s %10s %10s %10s %10s"
                 % (b["base"], g("spot", "bid"), g("spot", "ask"),
                    g("perp", "bid"), g("perp", "ask"),
                    "—" if b.get("basis_bp") is None else "%.2f" % b["basis_bp"]))
        for e in b.get("errors") or []:
            L.append("      [!] %s" % e)
    return "\n".join(L)


def render_gap(g):
    L = ["📡 项目一 → 项目二 的样本缺口"]
    L.append("   快照最后一刻  %s" % (g.get("snapshot_last_utc") or "读不到"))
    L.append("   现在          %s" % _iso(g["now_ms"]))
    L.append("   缺口          %s"
             % ("读不到" if g.get("gap_min") is None else "%.1f 分钟" % g["gap_min"]))
    L.append("   实时通道      %s"
             % {True: "可用（可按需补差，不落盘）", False: "不可用", None: "未探测"}
             [g.get("live_reachable")])
    L.append("   结论          %s" % g["why"])
    return "\n".join(L)


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description="📡 本项目自带的行情通道")
    ap.add_argument("--live", action="store_true", help="取一次实时行情（**不落盘**）")
    ap.add_argument("--sample", action="store_true", help="取一次并落盘到 data/live/")
    ap.add_argument("--gap", action="store_true", help="只报告样本缺口")
    ap.add_argument("--base", default=None, help="单个标的")
    ap.add_argument("--bases", default=None, help="逗号分隔多个标的")
    ap.add_argument("--fills", type=int, default=20, help="每腿取多少笔成交")
    ap.add_argument("--no-proxy", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()

    if a.gap or not (a.live or a.sample):
        op = opener(not a.no_proxy)
        _, err = get_json(op, "%s/public/time" % API)
        g = gap_report(live_ok=(err is None))
        print(json.dumps(g, ensure_ascii=False, indent=1) if a.json else render_gap(g))
        return 0 if a.gap else 2

    bases = ([x.strip().upper() for x in a.bases.split(",") if x.strip()]
             if a.bases else ([a.base.upper()] if a.base else DEFAULT_BASES))
    snap = live_snapshot(bases, use_proxy=not a.no_proxy, fills_limit=a.fills)
    if a.sample and snap.get("ok"):
        snap["written"] = persist(snap)
    if a.json:
        print(json.dumps(snap, ensure_ascii=False, indent=1, default=str))
    else:
        print(render_live(snap))
        if a.sample:
            for p in snap.get("written") or []:
                print("   ↳ 落盘 %s" % os.path.relpath(p, BASE))
    return 0 if snap.get("ok") else 1


# ---------------------------------------------------------------- 自检

def selftest():
    """离线自检**纯函数**（不联网）。重点是那条落盘守卫。"""
    ok = True

    def chk(cond, msg):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s" % ("OK " if cond else "!! ", msg))

    # ① 点差换算
    chk(abs(spread_bp(100.0, 101.0) - 99.50248756218906) < 1e-9,
        "点差 bp：bid 100 / ask 101 = %.4f bp" % spread_bp(100.0, 101.0))
    chk(spread_bp(None, 1) is None and spread_bp("x", "y") is None,
        "读不到就返回 None（不编 0）")

    # ② 归一化到快照 schema
    q = quote_row("NVDA", "spot", {"symbol": "RNVDAUSDT", "ts": "1789921695389",
                                   "bidPr": "221.04", "askPr": "221.06",
                                   "bidSz": "0.66", "askSz": "0.47", "lastPr": "221.03"})
    chk(set(q) == set(QUOTE_COLS), "quote 行的列与快照 CSV 完全一致（%d 列）" % len(q))
    chk(q["venue"] == "spot" and abs(q["mid"] - 221.05) < 1e-9,
        "中价 = (bid+ask)/2 = %.3f" % q["mid"])
    chk(q["ts_utc"].endswith("Z") and q["ts_ms"] == 1789921695389,
        "时间戳两种形态都对：%s" % q["ts_utc"])

    d = depth_rows("NVDA", "perp", "NVDAUSDT",
                   {"ts": "1789921699317",
                    "bids": [["221.22", "8.18"], ["221.21", "15.39"]],
                    "asks": [["221.23", "6.3"]]}, levels=5)
    chk(len(d) == 3 and set(d[0]) == set(DEPTH_COLS),
        "depth 行数与列都对（%d 行）" % len(d))
    chk(abs(d[1]["cum_notional_usd"] - (221.22 * 8.18 + 221.21 * 15.39)) < 1e-6,
        "累计名义额是逐档累加：%.2f" % d[1]["cum_notional_usd"])

    t = trade_rows("NVDA", "spot", "RNVDAUSDT",
                   [{"ts": "1789921695389", "price": "221.0", "size": "2",
                     "side": "buy", "tradeId": "x1"}])
    chk(len(t) == 1 and set(t[0]) == set(TRADE_COLS)
        and abs(t[0]["notional_usd"] - 442.0) < 1e-9,
        "trade 行列对、名义额 = price×size")

    # ③ 🔴 落盘守卫：**绝不能**写进 data/spread/
    raised = False
    try:
        guard_target(os.path.join(SPREAD, "trades-2026-09-21.csv"))
    except ValueError:
        raised = True
    chk(raised, "守卫：写 data/spread/ 被**拒绝**（那是冻结的证据基座）")
    raised2 = False
    try:
        guard_target(SPREAD)
    except ValueError:
        raised2 = True
    chk(raised2, "守卫：直接写 spread 目录本身也被拒绝")
    chk(guard_target(os.path.join(LIVE_DIR, "trades-x.csv")).endswith("trades-x.csv"),
        "守卫：写 data/live/ 放行")

    # ④ 缺口报告三态
    now = 1_800_000_000_000
    g1 = gap_report(now_ms=now, live_ok=True)
    chk(g1["verdict"] in ("gap_fillable", "no_gap", "no_snapshot"),
        "缺口报告给出明确结论：%s（gap=%s 分钟）" % (g1["verdict"], g1["gap_min"]))
    g2 = gap_report(now_ms=1, live_ok=True)
    chk(g2["verdict"] == "no_gap", "快照比现在还新 -> no_gap（不报假缺口）")
    g3 = gap_report(now_ms=now, live_ok=False)
    chk(g3["verdict"] in ("gap_unfillable", "no_gap", "no_snapshot")
        and isinstance(g3["why"], str),
        "实时通道不可用时如实说只剩「等导出」：%s" % g3["verdict"])

    # ⑤ 嗅探 venue
    chk(sniff({"symbol": "RNVDAUSDT"}) == "spot"
        and sniff({"symbol": "NVDAUSDT"}) == "perp",
        "venue 由符号前缀判定（R = 现货）")

    print("\n行情通道自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
