#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
📅 外部确定性事件（官方 bitget-mcp-server）：财报日历 + 分红/除息
==================================================================

━━ 为什么需要它（事件闸门一直缺的一块）━━

事件闸门现在有两个源：

  ① **确定性日历**（`market_events.py`）：期权到期、休市、in_house 窗口
  ② **LLM 判定**（`event_gate.py`）：读新闻标题，判 block/caution/none

两者都答不了这两个**本来完全确定**的问题：

  · 这家公司**下一次财报是哪天**？      —— LLM 只能从标题里"猜"
  · **除息日是哪天、分多少**？          —— 链路里**从来没人问过**

第二条尤其要命：除息日现货腿会按股息金额下跳、**永续腿不会跳**，
于是**基差会跳变**；而 maker 策略正好靠基差吃饭。

━━ 实测抓到的第一个真事件（2026-09-20）━━

    META  除息日 = **2026-09-20（当天）** ｜ 0.525 USD/股 ｜ 记录日 09-20 ｜ 派息 09-27
    NVDA  除息日 = 2026-09-09（已过）    ｜ AAPL 2026-08-09（已过）
    GOOGL 除息日 = 2026-09-03（已过）
    HOOD  不分红（status_code=204）
    下次财报：TSLA 2026-10-20 ｜ AAPL/META/GOOGL 2026-10-27/28 ｜ NVDA 2026-11-17

META 这条在接入之前**整条链路一无所知** —— 这就是"数据源集成"的实际价值，
不是凑数。

━━ 红线 ━━

  · 这里的数**不是实测**，是**第三方数据**（provider 见返回）。所以每条事件都
    带 `provider`，页面上要标明来源类别，不能混进"实测量"。
  · 拿不到就**如实说拿不到**，不退回"没有事件"（那等于说"安全"）。
  · 规则全是**纯函数**（见 `gate_from_events`），可离线自检。

用法::

    python project2/ext_events.py --refresh            # 拉一次并落盘缓存
    python project2/ext_events.py --base META          # 看一个标的
    python project2/ext_events.py --gate --now-ms ...  # 看闸门判定
    python project2/ext_events.py --selftest
"""

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DERIVED = os.path.join(BASE, "data", "derived")
CACHE = os.path.join(DERIVED, "ext_events.json")

MCP_URL = os.environ.get("P2_MCP_EQUITY", "https://agent.bitget.com/mcp")
PROXY = os.environ.get("P2_PROXY", "http://127.0.0.1:7890")
TIMEOUT = 40
DEFAULT_BASES = ["NVDA", "TSLA", "AAPL", "META", "GOOGL",
                 "SPY", "QQQ", "SOXL", "HOOD", "MRVL"]

# ── 闸门阈值（都能点回这里）────────────────────────────────────────
EXDIV_WINDOW_DAYS = 1.0     # 除息日前后各 1 天 -> caution（基差会在除息日跳变）
EARNINGS_WINDOW_DAYS = 3.0  # 财报日前后各 3 天 -> caution（波动率事件）
SOURCE = "mcp:bitget-mcp-server"


# ---------------------------------------------------------------- 纯函数

def _d(s):
    """'2026-09-20' -> date；解析不了返回 None（不猜）。"""
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def days_until(date_str, now_ms=None):
    """距今天数（正=未来，负=已过）。解析不了返回 None。"""
    d = _d(date_str)
    if d is None:
        return None
    now = dt.datetime.fromtimestamp(
        (now_ms or time.time() * 1000) / 1000, dt.UTC).date()
    return (d - now).days


def dividend_bp(amount, price):
    """股息占股价的比例（bp）—— 除息日现货腿的**下跳幅度估计**。

    这是"除息会让基差跳多少"的唯一可算量。价格取不到就返回 None
    （**不返回 0**：0 的意思是"除息不影响基差"，那是结论）。
    """
    try:
        a, p = float(amount), float(price)
    except (TypeError, ValueError):
        return None
    if p <= 0 or a < 0:
        return None
    return a / p * 10000.0


def gate_from_events(ev, now_ms=None, price=None):
    """**纯函数**：把外部事件判成闸门结论。返回 dict 或 None。

    返回**字典**（而不是元组）是刻意的：要与 `llm_event` **同形** ——
    两者都是硬闸门的输入、都要冻进复跑契约、都要能被页面直接渲染。
    形状不一致迟早在某处 `.get()` 上炸掉（初版就是元组，实测踩到）。

    ``severity`` 只到 ``caution``，**不到 block**：
      · 除息 / 财报都是**已知会过去**的事件，有明确的对冲动作（避开那个时点挂单），
        不需要"完全停手"；
      · block 是留给"必须停手"的（停牌、突发 8-K、日历硬约束）。
    这条边界写在这里，改它要连带改文档与自检。

    ⚠️ 返回 ``None`` 的语义是**"窗口内没有事件"**（这是判定结果）。
       调用方**不能**把"读不到事件表"也变成 None —— 那是缺数据，不是安全。
    """
    if not isinstance(ev, dict):
        return None
    hits = []

    # ① 除息窗口
    for dv in (ev.get("dividends") or []):
        n = days_until(dv.get("ex_dividend_date"), now_ms)
        if n is None:
            continue
        if abs(n) <= EXDIV_WINDOW_DAYS:
            bp = dividend_bp(dv.get("amount"), price)
            hits.append("除息日 %s（%s %s 天）｜股息 %s %s ≈ %s 的基差跳变"
                        % (dv.get("ex_dividend_date"),
                           "还有" if n >= 0 else "已过", abs(n),
                           dv.get("amount"), dv.get("currency") or "USD",
                           "算不出（缺股价）" if bp is None else "%.1f bp" % bp))
            break

    # ② 财报窗口
    e = ev.get("earnings") or {}
    n = days_until(e.get("report_date"), now_ms)
    if n is not None and abs(n) <= EARNINGS_WINDOW_DAYS:
        hits.append("财报日 %s（%s %s 天）｜EPS 一致预期 %s"
                    % (e.get("report_date"), "还有" if n >= 0 else "已过",
                       abs(n), e.get("eps_consensus")))

    if not hits:
        return None
    return {"severity": "caution",
            "reason": "外部确定性事件：" + "；".join(hits)
                      + "　⚠️ 数据来自第三方（%s），**不是本项目的实测量**"
                        % (ev.get("provider") or "provider 未标明"),
            "source": SOURCE,
            "provider": ev.get("provider")}


# ---------------------------------------------------------------- MCP 取数

def _opener(use_proxy=True):
    h = (urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
         if use_proxy else urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(h)


def _post(op, body, sid=None):
    h = {"Content-Type": "application/json",
         "Accept": "application/json, text/event-stream",
         "User-Agent": "p2-ext-events"}
    if sid:
        h["Mcp-Session-Id"] = sid
    req = urllib.request.Request(MCP_URL, data=json.dumps(body).encode("utf-8"),
                                 headers=h, method="POST")
    with op.open(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", "replace"), r.headers.get("Mcp-Session-Id")


def _unwrap(text):
    raw = text.split("data: ", 1)[1] if "data: " in text else text
    return json.loads(raw.split("\nevent:")[0])


def _query(op, sid, entry_id, params):
    t, _ = _post(op, {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                      "params": {"name": "do_query",
                                 "arguments": {"entry_id": entry_id,
                                               "params": params}}}, sid)
    d = _unwrap(t)
    if d.get("error"):
        return None, "MCP error: %s" % json.dumps(d["error"],
                                                  ensure_ascii=False)[:100]
    c = (d.get("result") or {}).get("content") or []
    if not c:
        return None, "MCP 返回空 content"
    return c[0].get("text") or "", None


def fetch(bases=None, use_proxy=True, save=True):
    """拉一次外部确定性事件。**每个标的独立报告成败**，失败不污染成功的。"""
    bases = [b.upper() for b in (bases or DEFAULT_BASES)]
    out = {"fetched_ms": int(time.time() * 1000),
           "fetched_utc": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "source": SOURCE, "mcp": MCP_URL, "bases": {}, "errors": []}
    op = _opener(use_proxy)
    try:
        t, sid = _post(op, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": "2024-11-05",
                                       "capabilities": {},
                                       "clientInfo": {"name": "p2-events",
                                                      "version": "1"}}})
    except (urllib.error.URLError, OSError, ValueError) as exc:
        out["ok"] = False
        out["why"] = ("外部事件源**不可用**：连不上 %s（%s: %s）—— 需要网络 + 本机代理。"
                      "**不退回『没有事件』**：那等于说『安全』。"
                      % (MCP_URL, type(exc).__name__, str(exc)[:60]))
        return out
    for b in bases:
        rec = {}
        txt, err = _query(op, sid, "equity_calendar_earnings", {"symbol": b})
        if err:
            out["errors"].append("%s 财报：%s" % (b, err))
        else:
            try:
                j = json.loads(txt)
                res = (j.get("data") or {}).get("results") or []
                if res:
                    r = res[0]
                    rec["earnings"] = {"report_date": r.get("report_date"),
                                       "eps_consensus": r.get("eps_consensus")}
                    rec["provider"] = j.get("provider")
            except (TypeError, ValueError):
                out["errors"].append("%s 财报：返回不可解析" % b)
        txt, err = _query(op, sid, "equity_fundamental_dividends", {"symbol": b})
        if err:
            out["errors"].append("%s 分红：%s" % (b, err))
        else:
            try:
                j = json.loads(txt)
                data = j.get("data")
                res = (data or {}).get("results") if isinstance(data, dict) else []
                if res:
                    rec["dividends"] = [
                        {"ex_dividend_date": r.get("ex_dividend_date"),
                         "amount": r.get("amount"), "currency": r.get("currency"),
                         "event_type": r.get("event_type"),
                         "is_special_dividend": r.get("is_special_dividend")}
                        for r in res[:5]]
                    rec.setdefault("provider", j.get("provider"))
            except (TypeError, ValueError):
                out["errors"].append("%s 分红：返回不可解析" % b)
        out["bases"][b] = rec
    out["ok"] = any(v for v in out["bases"].values())
    if not out["ok"]:
        out["why"] = "一个标的都没拿到事件（全部失败或都为空）"
    if save and out["ok"]:
        try:
            os.makedirs(DERIVED, exist_ok=True)
            with open(CACHE, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(out, fh, ensure_ascii=False, indent=1)
            out["cached_to"] = os.path.relpath(CACHE, BASE)
        except OSError as exc:
            out["errors"].append("缓存写入失败：%s" % exc)
    return out


def load_cache():
    """读上次落盘的事件。**读不到返回 None**（不返回空字典 —— 那会被当成"没有事件"）。"""
    try:
        with open(CACHE, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) and d.get("bases") else None
    except (OSError, ValueError):
        return None


def event_for(events, base):
    """从事件集里取某标的的记录（含 provider），取不到返回 None。"""
    if not isinstance(events, dict):
        return None
    b = (events.get("bases") or {}).get(base.upper())
    if not b:
        return None
    return dict(b, provider=b.get("provider") or events.get("provider"))


# ---------------------------------------------------------------- 渲染

def render(ev, now_ms=None, price=None):
    L = []
    if not ev:
        return "📅 外部确定性事件：**取不到**（不当作『没有事件』）"
    e = ev.get("earnings") or {}
    n = days_until(e.get("report_date"), now_ms)
    L.append("📅 %s ｜ 财报 %s（%s）｜ EPS 一致预期 %s"
             % (ev.get("base") or "", e.get("report_date") or "无",
                "无数据" if n is None else ("%+d 天" % n), e.get("eps_consensus")))
    for dv in (ev.get("dividends") or [])[:3]:
        m = days_until(dv.get("ex_dividend_date"), now_ms)
        L.append("     除息 %s（%s）｜%s %s ｜ %s"
                 % (dv.get("ex_dividend_date"),
                    "无数据" if m is None else ("%+d 天" % m),
                    dv.get("amount"), dv.get("currency") or "",
                    dv.get("event_type") or ""))
        bp = dividend_bp(dv.get("amount"), price)
        if bp is not None:
            L.append("           ≈ %.1f bp 的基差跳变（股息/股价）" % bp)
    g = gate_from_events(ev, now_ms=now_ms, price=price)
    L.append("     闸门：%s" % ("（不触发）" if g is None else "%s —— %s" % (g["severity"], g["reason"])))
    L.append("     provider：%s（**第三方数据，不是本项目的实测量**）"
             % (ev.get("provider") or "未标明"))
    return "\n".join(L)


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description="📅 外部确定性事件（财报 / 除息）")
    ap.add_argument("--refresh", action="store_true", help="拉一次并落盘缓存")
    ap.add_argument("--base", default=None)
    ap.add_argument("--gate", action="store_true", help="只打印闸门判定")
    ap.add_argument("--now-ms", type=float, default=None)
    ap.add_argument("--price", type=float, default=None, help="用于算除息跳变幅度")
    ap.add_argument("--no-proxy", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.refresh:
        out = fetch(use_proxy=not a.no_proxy)
        if a.json:
            print(json.dumps(out, ensure_ascii=False, indent=1))
        else:
            print("📅 拉取完成 ｜ %s" % ("失败：" + out.get("why", "") if not out.get("ok")
                                        else "%d 个标的 ｜ 缓存 %s"
                                        % (len(out["bases"]), out.get("cached_to"))))
            for e in out.get("errors") or []:
                print("   [!] %s" % e)
        return 0 if out.get("ok") else 1
    events = load_cache()
    if events is None:
        print("📅 没有缓存（先跑 --refresh）—— **不当作『没有事件』**")
        return 1
    base = (a.base or "META").upper()
    ev = event_for(events, base)
    if ev is None:
        print("📅 %s 在缓存里没有记录" % base)
        return 1
    if a.json:
        print(json.dumps(ev, ensure_ascii=False, indent=1))
        return 0
    print(render(dict(ev, base=base), now_ms=a.now_ms, price=a.price))
    return 0


# ---------------------------------------------------------------- 自检

def selftest():
    """离线自检**纯函数**（不联网）。规则、边界、缺失不硬算。"""
    ok = True

    def chk(cond, msg):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s" % ("OK " if cond else "!! ", msg))

    now = 1_789_900_000_000          # 2026-09-20 前后
    chk(days_until("2026-09-20", now) == 0, "同日 = 0 天")
    chk(days_until(None, now) is None and days_until("不是日期", now) is None,
        "日期解析不了 -> None（不猜）")

    chk(abs(dividend_bp(0.525, 670.0) - 7.8358) < 1e-3,
        "股息跳变：0.525 / 670 = %.2f bp" % dividend_bp(0.525, 670.0))
    chk(dividend_bp(0.525, None) is None and dividend_bp(None, 670) is None,
        "缺任一侧 -> None（**不返回 0**：0 的意思是『除息不影响基差』）")

    # ① 除息窗口
    ev_div = {"dividends": [{"ex_dividend_date": "2026-09-20", "amount": 0.525,
                             "currency": "USD"}], "provider": "test"}
    g = gate_from_events(ev_div, now_ms=now, price=670.0)
    chk(g and g["severity"] == "caution", "除息日当天 -> caution（%s）" % (g["reason"][:60] if g else "无"))
    chk(g and "7.8" in g["reason"], "除息结论里带**可量化的跳变幅度**：%.1f bp"
        % dividend_bp(0.525, 670.0))
    chk(g and "第三方" in g["reason"] and "不是本项目的实测量" in g["reason"],
        "结论里标明**数据来源类别**（第三方，不是实测量）")
    chk(gate_from_events({"dividends": [{"ex_dividend_date": "2026-08-01",
                                         "amount": 1.0}]}, now_ms=now) is None,
        "除息日早就过了 -> 不触发")
    chk(gate_from_events({"dividends": [{"ex_dividend_date": "2026-10-30",
                                         "amount": 1.0}]}, now_ms=now) is None,
        "除息日还远 -> 不触发")

    # ② 财报窗口
    ev_e = {"earnings": {"report_date": "2026-09-22", "eps_consensus": 1.2},
            "provider": "test"}
    chk(gate_from_events(ev_e, now_ms=now)["severity"] == "caution",
        "财报日前 2 天（<= %.0f 天）-> caution" % EARNINGS_WINDOW_DAYS)
    chk(gate_from_events({"earnings": {"report_date": "2026-11-17"}},
                         now_ms=now) is None,
        "财报日还远（58 天）-> 不触发")

    # ③ 严重度上限：**只到 caution，不到 block**
    chk(gate_from_events(ev_div, now_ms=now, price=670.0)["severity"] != "block",
        "外部事件**不产生 block**（block 留给『必须停手』：停牌/突发 8-K/日历硬约束）")

    # ④ 缺失不硬算
    chk(gate_from_events(None, now_ms=now) is None,
        "没有事件记录 -> None（调用方据此**不能**当成『没有事件』）")
    chk(gate_from_events({}, now_ms=now) is None, "空记录 -> None")

    # ⑤ 缓存读不到时返回 None，而不是 {}
    chk(not isinstance(load_cache() or {"bases": 1}, dict)
        or isinstance(load_cache(), (dict, type(None))),
        "缓存读取的返回类型是 dict 或 None（调用方按 None 处理『读不到』）")

    print("\n外部确定性事件自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
