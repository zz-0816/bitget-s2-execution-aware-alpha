#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
⚓ 外部价格锚：用**官方 bitget-mcp-server** 的真实美股报价，给 rToken 定价一个外部参照
==================================================================================

━━ 为什么需要它（本项目的真实缺口）━━

到目前为止，本项目**完全没有外部参照价**：所有数字都来自 Bitget 自己的盘口与成交，
是"自我参照"的。于是有一个问题一直答不了：

    rToken 现在这个价，相对**真实股票**是贵了还是便宜了？

而这个问题的答案直接决定执行辅助的结论：现货腿买在折价还是溢价，
挂单被逆向选择的概率完全不同。

━━ 数据源：官方 MCP，**不需要账号、不需要 API Key** ━━

    https://agent.bitget.com/mcp        （HTTP 传输，SSE 应答）

两级设计：``guide`` 列目录 → ``do_query`` 执行。实测美股 21 个条目，本次只用：

    equity_price_quote   -> 真实股票 bid/ask/last + 报价时刻 + provider

━━ 🔴 一个必须说准的口径问题（否则这个数会骗人）━━

美股**有开闭市**，rToken **7×24**。所以拿"最近一次美股报价"去比"当前 rToken 价"，
在不同时段量到的是**两个不同的东西**：

  · 美股**开市**时：量到的是**折溢价**（同一时刻的两个价，可比）
  · 美股**休市**时：量到的是**折溢价 + 休市期间的漂移**，**两者无法分离**
    （因为我们没有休市期间的真实股价——它根本不存在）

所以本工具用 `common/market_calendar.session_of()` 判定时段，
并**在字段名与输出里就写明是哪一个**：``premium_bp`` vs ``drift_and_premium_bp``。
把这两个混成一个数，就是编。

━━ 依赖与降级 ━━

需要网络 + 本机代理（实测直连被 ISP 掐断）。拿不到就**如实说拿不到**，
不退回"0 溢价"——那会让人以为「定价没有偏离」。

用法::

    python tools/mcp_anchor.py                    # 十个标的，对比实时 rToken 价
    python tools/mcp_anchor.py --rtoken snapshot  # rToken 用冻结快照的最后一笔
    python tools/mcp_anchor.py --json
    python tools/mcp_anchor.py --selftest         # 离线自检纯函数
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
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "common"))
sys.path.insert(0, os.path.join(BASE, "project2"))
sys.path.insert(0, os.path.join(BASE, "tools"))

MCP_URL = os.environ.get("P2_MCP_EQUITY", "https://agent.bitget.com/mcp")
PROXY = os.environ.get("P2_PROXY", "http://127.0.0.1:7890")
ENTRY_QUOTE = "equity_price_quote"
ENTRY_EARNINGS = "equity_calendar_earnings"
ENTRY_TARGETS = "equity_estimates_price_target"
ENTRY_CONSENSUS = "equity_estimates_consensus"
# 🗄️ 决策链只读这个缓存（取数与消费分开，见 cache_payload 的说明）
CACHE = os.path.join(BASE, "data", "derived", "anchor.json")
DEFAULT_BASES = ["NVDA", "TSLA", "AAPL", "META", "GOOGL",
                 "SPY", "QQQ", "SOXL", "HOOD", "MRVL"]
TIMEOUT = 40
RETRY = 3


# ---------------------------------------------------------------- 纯函数

def deviation_bp(rtoken_mid, stock_mid):
    """rToken 相对真实股价的偏离（bp）。正值 = rToken 更贵。

    读不到任一侧就返回 None —— **不返回 0**（0 的意思是"完全贴合"，那是结论）。
    """
    try:
        r, s = float(rtoken_mid), float(stock_mid)
    except (TypeError, ValueError):
        return None
    if s <= 0:
        return None
    return (r - s) / s * 10000.0


def comparison_kind(session):
    """按美股时段决定"这个偏离量到的是什么"。**这是本工具最要紧的一条规矩。**

    返回 (字段名, 中文口径, 是否可比)。
    """
    if session in ("premarket", "regular", "afterhours"):
        return ("premium_bp", "折溢价（同一时刻两个价，可比）", True)
    if session == "closed":
        return ("drift_and_premium_bp",
                "休市偏移（= 折溢价 + 休市期间漂移，**两者无法分离**）", False)
    return ("deviation_bp", "时段未知（日历给不出）—— 只报数不下结论", False)


def quote_from_stock(entry_text):
    """从 MCP 返回的 JSON 文本里取真实股票报价。取不到返回 (None, 原因)。"""
    try:
        j = json.loads(entry_text)
    except (TypeError, ValueError):
        return None, "MCP 返回不是 JSON"
    if not j.get("success"):
        return None, "MCP 报失败：%s" % str(j.get("error"))[:80]
    res = (((j.get("data") or {}).get("results")) or [])
    if not res:
        return None, "MCP 返回里没有 results"
    r = res[0]
    try:
        bid, ask = float(r["bid"]), float(r["ask"])
    except (KeyError, TypeError, ValueError):
        return None, "报价字段缺失（bid/ask）"
    mid = (bid + ask) / 2.0
    ms = r.get("time")
    return {"symbol": r.get("symbol"), "bid": bid, "ask": ask, "mid": mid,
            "last": r.get("last_price"), "prev_close": r.get("prev_close"),
            "quote_ms": ms,
            "quote_utc": (dt.datetime.fromtimestamp(ms / 1000, dt.UTC)
                          .strftime("%Y-%m-%dT%H:%M:%SZ") if ms else None),
            "provider": j.get("provider"),
            "spread_bp": (ask - bid) / mid * 10000.0 if mid > 0 else None}, None


def rtoken_from_snapshot(base):
    """从冻结快照里取该标的**最后一笔**现货报价（离线可比对）。"""
    files = sorted(glob.glob(os.path.join(BASE, "data", "spread", "2*.csv")))
    if not files:
        return None
    best = None
    try:
        with io.open(files[-1], encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if (row.get("base") or "").upper() != base.upper():
                    continue
                if row.get("venue") != "spot":
                    continue
                best = (int(row.get("ts_ms") or 0), row)
    except (OSError, ValueError, csv.Error):
        return None
    if not best:
        return None
    ts, row = best
    try:
        mid = float(row["mid"])
    except (KeyError, TypeError, ValueError):
        return None
    return {"mid": mid, "ts_ms": ts,
            "ts_utc": dt.datetime.fromtimestamp(ts / 1000, dt.UTC)
                      .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": os.path.basename(files[-1])}


# ---------------------------------------------------------------- 第三方观点

# 目标价的**新鲜度窗口**。为什么必须有它：全历史有 850+ 条、最早到 2016 年，
# 而 NVDA 的目标价区间是 **59 ~ 1400** —— 拿 2016 年的 59 美元和今天比，
# 算出来的"分歧度"毫无意义。实测只有 22~39 条落在 90 天内。
TARGET_FRESH_DAYS = 90
# 少于这么多条近期目标价就**不判**（样本不足时说"不知道"，不硬给结论）
MIN_TARGETS = 8
# 一致预期的陈旧闸门。⚠️ 实测：`fore_org_num` 看起来是 42 家机构的共识，
# 但它的 `scraped_date` 是 **2024-11-21**（陈旧 22 个月）——
# 这种数据**必须拒绝使用**，否则等于拿两年前的预期当今天的。
CONSENSUS_STALE_DAYS = 30


def _pct(sorted_vals, q):
    """线性插值分位数（样本少时比 nearest 稳）。"""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def parse_targets(text, now_ms=None, fresh_days=TARGET_FRESH_DAYS):
    """解析分析师目标价 -> **只用近期样本**的稳健统计量。

    🔴 用 **IQR（p75−p25）/ 中位** 而不是极差：实测 TSLA 近期目标价里有一个
       **24.9** 的离群值（中位 418.5），用极差算出来的"分歧度"会被它一个人主导
       （11473 bp）。IQR 抗离群，更能代表"机构之间到底分歧多大"。
    """
    try:
        j = json.loads(text)
        res = ((j.get("data") or {}).get("results")) or []
    except (TypeError, ValueError):
        return None, "目标价返回不可解析"
    if not res:
        return None, "没有目标价数据（ETF 通常没有）"
    now = dt.datetime.fromtimestamp(
        (now_ms or time.time() * 1000) / 1000, dt.UTC).date()
    recent, all_dates = [], []
    for r in res:
        d, t = r.get("published_date"), r.get("price_target")
        if not d or t is None:
            continue
        all_dates.append(str(d)[:10])
        try:
            days = (now - dt.date.fromisoformat(str(d)[:10])).days
            tv = float(t)
        except (TypeError, ValueError):
            continue
        if 0 <= days <= fresh_days:
            recent.append({"firm": r.get("analyst_firm"), "target": tv,
                           "rating": r.get("rating_current"),
                           "action": r.get("action"), "date": str(d)[:10],
                           "days": days})
    out = {"n_all": len(all_dates), "n_recent": len(recent),
           "fresh_days": fresh_days,
           "oldest_all": min(all_dates) if all_dates else None,
           "newest_all": max(all_dates) if all_dates else None,
           "provider": j.get("provider")}
    if not recent:
        out["enough"] = False
        return out, "近 %d 天内没有目标价" % fresh_days
    vals = sorted(x["target"] for x in recent)
    med = _pct(vals, 0.5)
    out.update({
        "median": med, "min": vals[0], "max": vals[-1],
        "p25": _pct(vals, 0.25), "p75": _pct(vals, 0.75),
        "enough": len(recent) >= MIN_TARGETS,
        "min_targets": MIN_TARGETS,
        # 稳健分歧度（IQR 口径）；同时留极差供对照，但**规则只用前者**
        "dispersion_bp": (None if not med else
                          (_pct(vals, 0.75) - _pct(vals, 0.25)) / med * 10000.0),
        "range_bp": (None if not med else (vals[-1] - vals[0]) / med * 10000.0),
        "newest_recent": max(x["date"] for x in recent),
        "actions": sorted({x["action"] for x in recent if x.get("action")}),
        "items": recent[:12],
    })
    return out, None


def parse_consensus(text, now_ms=None, stale_days=CONSENSUS_STALE_DAYS):
    """解析一致预期，并**判它是不是陈旧数据**。

    🔴 实测（2026-09-20）：`equity_estimates_consensus` 返回的
       `scraped_date` 是 **2024-11-21** —— 陈旧 22 个月。
       这种数据必须**拒绝使用**：拿两年前的 EPS 预期当今天的，
       比没有数据更糟（它会看起来很权威：`fore_org_num: 42`）。
    """
    try:
        j = json.loads(text)
        res = ((j.get("data") or {}).get("results")) or []
    except (TypeError, ValueError):
        return None, "一致预期返回不可解析"
    if not res:
        return None, "没有一致预期数据"
    now = dt.datetime.fromtimestamp(
        (now_ms or time.time() * 1000) / 1000, dt.UTC).date()
    dates = sorted({str(r.get("scraped_date"))[:10]
                    for r in res if r.get("scraped_date")})
    newest = dates[-1] if dates else None
    stale = None
    if newest:
        try:
            stale = (now - dt.date.fromisoformat(newest)).days
        except ValueError:
            stale = None
    return ({"n_periods": len(res), "newest_scraped": newest,
             "oldest_scraped": dates[0] if dates else None,
             "stale_days": stale, "stale_limit": stale_days,
             "usable": (stale is not None and stale <= stale_days),
             "provider": j.get("provider"),
             "sample": {k: res[0].get(k) for k in
                        ("fore_indicator_name", "target_consensus",
                         "target_low", "target_high", "fore_org_num",
                         "report_period_scraped")}}, None)


# ---------------------------------------------------------------- MCP

def opener(use_proxy=True):
    h = (urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
         if use_proxy else urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(h)


def _post(op, body, sid=None):
    h = {"Content-Type": "application/json",
         "Accept": "application/json, text/event-stream",
         "User-Agent": "p2-mcp-anchor"}
    if sid:
        h["Mcp-Session-Id"] = sid
    req = urllib.request.Request(MCP_URL, data=json.dumps(body).encode("utf-8"),
                                 headers=h, method="POST")
    with op.open(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", "replace"), r.headers.get("Mcp-Session-Id")


def _unwrap(text):
    """MCP 走 SSE：真正的内容在 `data: {...}` 那一行。"""
    raw = text.split("data: ", 1)[1] if "data: " in text else text
    return json.loads(raw.split("\nevent:")[0])


class Anchor:
    """极简 MCP 客户端（**只**调 gitbook 里公开的 guide / do_query）。"""

    def __init__(self, use_proxy=True):
        self.op = opener(use_proxy)
        self.sid = None

    def connect(self):
        t, sid = _post(self.op, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                 "params": {"protocolVersion": "2024-11-05",
                                            "capabilities": {},
                                            "clientInfo": {"name": "p2-anchor",
                                                           "version": "1"}}})
        self.sid = sid
        return _unwrap(t)

    def query(self, entry_id, params, req_id=9):
        last = None
        for i in range(RETRY):
            try:
                t, _ = _post(self.op, {"jsonrpc": "2.0", "id": req_id,
                                       "method": "tools/call",
                                       "params": {"name": "do_query",
                                                  "arguments": {"entry_id": entry_id,
                                                                "params": params}}},
                             self.sid)
                d = _unwrap(t)
                if d.get("error"):
                    return None, "MCP error: %s" % json.dumps(
                        d["error"], ensure_ascii=False)[:120]
                c = (d.get("result") or {}).get("content") or []
                return (c[0].get("text") if c else ""), None
            except (urllib.error.URLError, OSError, ValueError) as exc:
                last = "%s: %s" % (type(exc).__name__, str(exc)[:70])
                if i < RETRY - 1:
                    time.sleep(1.5 * (i + 1))
        return None, last


def session_now(now_ms=None):
    """当前美股时段（用项目自己的 market_calendar，不另造一套）。"""
    try:
        from market_calendar import session_of
        return session_of(int(now_ms or time.time() * 1000))
    except Exception:  # noqa: BLE001
        return None


def fetch(bases, rtoken="live", use_proxy=True, now_ms=None):
    """取一次外部锚。每个标的**独立报告成败**。"""
    now = int(now_ms or time.time() * 1000)
    sess = session_now(now)
    field, label, comparable = comparison_kind(sess)
    out = {"ok": False, "fetched_utc": dt.datetime.fromtimestamp(now / 1000, dt.UTC)
           .strftime("%Y-%m-%dT%H:%M:%SZ"),
           "mcp": MCP_URL, "proxy": PROXY if use_proxy else None,
           "session": sess, "compare_field": field, "compare_label": label,
           "same_instant": comparable, "rows": [], "errors": []}
    rt = {}
    if rtoken == "live":
        try:
            import market_feed as mf
            snap = mf.live_snapshot(bases, use_proxy=use_proxy, fills_limit=1)
            if not snap.get("ok"):
                out["errors"].append("rToken 实时价取不到：%s" % snap.get("why"))
            else:
                for b in snap["bases"]:
                    q = (b.get("quotes") or {}).get("spot")
                    if q:
                        rt[b["base"]] = {"mid": q["mid"], "ts_ms": q["ts_ms"],
                                         "ts_utc": q["ts_utc"], "source": "market_feed.live"}
        except Exception as exc:  # noqa: BLE001
            out["errors"].append("rToken 实时价异常：%r" % (exc,))
    if not rt:
        for b in bases:
            s = rtoken_from_snapshot(b)
            if s:
                rt[b.upper()] = dict(s, source="snapshot:" + s["source"])
        if not rt:
            out["errors"].append("rToken 价一个都取不到（实时与快照都没有）")

    a = Anchor(use_proxy)
    try:
        a.connect()
    except Exception as exc:  # noqa: BLE001
        out["why"] = ("外部锚**不可用**：连不上 %s（%r）—— 需要网络 + 本机代理。"
                      "不退回『0 偏离』，那会让人以为定价没有偏离。" % (MCP_URL, exc))
        return out
    for b in bases:
        base = b.upper()
        txt, err = a.query(ENTRY_QUOTE, {"symbol": base})
        row = {"base": base, "rtoken": rt.get(base), "stock": None,
               "error": None, "field": field, "label": label}
        if err:
            row["error"] = err
        else:
            st, err2 = quote_from_stock(txt)
            if err2:
                row["error"] = err2
            else:
                row["stock"] = st
                v = deviation_bp((rt.get(base) or {}).get("mid"), st["mid"])
                row[field] = v
                # ⭐ 同时存一个**稳定的键名**：决策链只读缓存，而字段名会随
                #    "开市/休市"在 premium_bp / drift_and_premium_bp 之间变 ——
                #    下游不该为了取值去猜今天用哪个名字。
                row["deviation_bp"] = v
        # 第三方**观点**：目标价 + 一致预期。这里只**取回并统计**，
        # 判定留给 agent_team（保持"取数与消费分开"，只有一份实现）。
        _tt, _terr = a.query(ENTRY_TARGETS, {"symbol": base})
        if _terr:
            row["targets_error"] = _terr
        else:
            _tg, _tgerr = parse_targets(_tt, now_ms=now)
            row["targets"] = _tg
            if _tgerr:
                row["targets_note"] = _tgerr
        _ct, _cerr = a.query(ENTRY_CONSENSUS, {"symbol": base})
        if _cerr:
            row["consensus_error"] = _cerr
        else:
            _cs, _cserr = parse_consensus(_ct, now_ms=now)
            row["consensus"] = _cs
            if _cserr:
                row["consensus_note"] = _cserr
        out["rows"].append(row)
    ok_rows = [r for r in out["rows"] if r.get("stock") and r.get("rtoken")]
    out["ok"] = bool(ok_rows)
    out["n_ok"] = len(ok_rows)
    if not out["ok"]:
        out["why"] = "一个标的都没拿到可比的（真实股价 + rToken 价）"
    return out


def earnings(base, use_proxy=True):
    """下一次财报日（确定性事件，可用于事件闸门）。"""
    a = Anchor(use_proxy)
    try:
        a.connect()
    except Exception as exc:  # noqa: BLE001
        return None, "连不上 MCP：%r" % (exc,)
    txt, err = a.query(ENTRY_EARNINGS, {"symbol": base.upper()})
    if err:
        return None, err
    try:
        j = json.loads(txt)
        res = (j.get("data") or {}).get("results") or []
    except (TypeError, ValueError):
        return None, "返回不是 JSON"
    if not res:
        return None, "没有财报日历数据"
    r = res[0]
    return {"symbol": r.get("symbol"), "report_date": r.get("report_date"),
            "eps_consensus": r.get("eps_consensus"),
            "provider": j.get("provider")}, None


def cache_payload(out):
    """把一次取数压成**决策链可直读**的缓存（只留能核验的字段）。

    ⚠️ 为什么缓存要由这个工具写、而不是让决策链自己取：
       取数是**外部活数据 + 网络**，决策链必须能离线跑。所以取数与消费分开 ——
       本工具 `--refresh` 负责取并落盘，`agent_team` 只读盘。
       这样**只有一份实现**，不会出现"两套口径"。
    """
    bases = {}
    for r in out.get("rows") or []:
        if not r.get("stock") or not r.get("rtoken"):
            continue
        bases[r["base"]] = {
            "stock_mid": r["stock"]["mid"],
            "rtoken_mid": r["rtoken"]["mid"],
            "deviation_bp": r.get("deviation_bp"),
            # 口径：开市 = premium_bp（同一时刻可比）；休市 = drift_and_premium_bp
            # （含休市漂移、**不可分离**）。字段名与说明一起存，下游照抄即可。
            "field": r.get("field"), "label": r.get("label"),
            "same_instant": out.get("same_instant"),
            "session": out.get("session"),
            "quote_utc": r["stock"].get("quote_utc"),
            "provider": r["stock"].get("provider"),
            "rtoken_source": (r.get("rtoken") or {}).get("source"),
            # 第三方**观点**（目标价 / 一致预期）—— 与"价格锚"不同：
            # 它们是**别人的判断**，用的时候必须标 source_kind=third_party。
            "targets": r.get("targets"),
            "targets_note": r.get("targets_note"),
            "consensus": r.get("consensus"),
            "consensus_note": r.get("consensus_note"),
        }
    return {"fetched_utc": out.get("fetched_utc"), "mcp": out.get("mcp"),
            "session": out.get("session"), "compare_label": out.get("compare_label"),
            "same_instant": out.get("same_instant"),
            "source": "mcp:bitget-mcp-server（第三方数据，不是本项目的实测量）",
            "bases": bases}


def save_cache(out, path=None):
    path = path or CACHE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(cache_payload(out), fh, ensure_ascii=False, indent=1)
    return path


def load_cache(path=None):
    """读缓存。**读不到返回 None**（不返回空字典 —— 那会被当成"没有偏离"）。"""
    path = path or CACHE
    try:
        with io.open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) and d.get("bases") else None
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- 渲染

def render(out):
    L = ["⚓ 外部价格锚（官方 bitget-mcp-server，无需账号）"]
    if not out.get("ok"):
        L.append("   **不可用** —— %s" % (out.get("why") or "；".join(out.get("errors") or [])))
        return "\n".join(L)
    L.append("   取数时刻 %s ｜ 美股时段 **%s**" % (out["fetched_utc"], out["session"]))
    L.append("   口径     %s" % out["compare_label"])
    if not out["same_instant"]:
        L.append("            ⚠️ 两个价**不在同一时刻**，所以这个数**不是**纯折溢价 —— "
                 "字段名就叫 %s" % out["compare_field"])
    L.append("   %-7s %11s %11s %13s %-7s %s"
             % ("标的", "真实股中价", "rToken中价", "偏离bp", "provider", "美股报价时刻"))
    for r in out["rows"]:
        st, rt = r.get("stock"), r.get("rtoken")
        if not st or not rt:
            L.append("   %-7s %s" % (r["base"], ("取不到：" + (r.get("error") or "缺一侧"))[:60]))
            continue
        v = r.get(out["compare_field"])
        L.append("   %-7s %11.3f %11.3f %13s %-7s %s"
                 % (r["base"], st["mid"], rt["mid"],
                    "—" if v is None else "%+.1f" % v,
                    (st.get("provider") or "—")[:7], st.get("quote_utc") or "—"))
    L.append("   %d/%d 个标的拿到可比数据" % (out["n_ok"], len(out["rows"])))
    for e in out.get("errors") or []:
        L.append("   [!] %s" % e)
    return "\n".join(L)


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description="⚓ 外部价格锚（真实美股 vs rToken）")
    ap.add_argument("--bases", default=None)
    ap.add_argument("--rtoken", choices=("live", "snapshot"), default="live")
    ap.add_argument("--earnings", default=None, help="查某标的下一次财报日")
    ap.add_argument("--refresh", action="store_true",
                    help="取一次并落盘到 data/derived/anchor.json（决策链读它）")
    ap.add_argument("--show-cache", action="store_true", help="只看缓存")
    ap.add_argument("--no-proxy", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.show_cache:
        d = load_cache()
        if d is None:
            print("⚓ 没有缓存（先跑 --refresh）—— **不当作『没有偏离』**")
            return 1
        print(json.dumps(d, ensure_ascii=False, indent=1) if a.json else
              "⚓ 缓存 %s ｜ %s ｜ 口径 %s ｜ %d 个标的"
              % (d.get("fetched_utc"), d.get("session"),
                 d.get("compare_label"), len(d.get("bases") or {})))
        return 0
    if a.earnings:
        d, err = earnings(a.earnings, use_proxy=not a.no_proxy)
        if err:
            print("⚓ 财报日历取不到：%s" % err)
            return 1
        print("⚓ %s 下一次财报：%s（EPS 一致预期 %s ｜ provider %s）"
              % (a.earnings.upper(), d["report_date"], d["eps_consensus"], d["provider"]))
        return 0
    bases = ([x.strip().upper() for x in a.bases.split(",") if x.strip()]
             if a.bases else DEFAULT_BASES)
    out = fetch(bases, rtoken=a.rtoken, use_proxy=not a.no_proxy)
    if a.refresh and out.get("ok"):
        try:
            p = save_cache(out)
            out["cached_to"] = os.path.relpath(p, BASE)
        except OSError as exc:
            out["errors"] = (out.get("errors") or []) + ["缓存写入失败：%s" % exc]
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str) if a.json
          else render(out))
    if a.refresh and out.get("cached_to") and not a.json:
        print("   ↳ 已落盘 %s（决策链读它）" % out["cached_to"])
    return 0 if out.get("ok") else 1


# ---------------------------------------------------------------- 自检

def selftest():
    """离线自检**纯函数**：偏离换算、口径选择、缺失不硬算。"""
    ok = True

    def chk(cond, msg):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s" % ("OK " if cond else "!! ", msg))

    chk(abs(deviation_bp(101.0, 100.0) - 100.0) < 1e-9,
        "rToken 101 / 股票 100 -> +100 bp（rToken 更贵）")
    chk(abs(deviation_bp(99.0, 100.0) + 100.0) < 1e-9,
        "rToken 99 / 股票 100 -> -100 bp（rToken 更便宜）")
    chk(deviation_bp(None, 100) is None and deviation_bp(100, 0) is None,
        "缺一侧或除零 -> None（**不返回 0**：0 的意思是『完全贴合』，那是结论）")

    # 🔴 口径：开市才是"折溢价"，休市只能叫"休市偏移"
    f_open, lab_open, cmp_open = comparison_kind("regular")
    f_shut, lab_shut, cmp_shut = comparison_kind("closed")
    chk(f_open == "premium_bp" and cmp_open is True,
        "美股开市 -> 字段叫 premium_bp，且标记为『同一时刻可比』")
    chk(f_shut == "drift_and_premium_bp" and cmp_shut is False,
        "美股休市 -> 字段叫 drift_and_premium_bp，且**明确标记不可比**")
    chk("无法分离" in lab_shut,
        "休市口径的说明里写明『两者无法分离』：%s" % lab_shut)
    chk(comparison_kind(None)[0] == "deviation_bp",
        "时段未知 -> 中性字段名（不冒充折溢价）")

    # 解析 MCP 返回
    txt = json.dumps({"success": True, "provider": "massive",
                      "data": {"results": [{"symbol": "NVDA", "bid": 222.5,
                                            "ask": 222.53, "last_price": 222.53,
                                            "time": 1789775999952}]}})
    q, err = quote_from_stock(txt)
    chk(err is None and abs(q["mid"] - 222.515) < 1e-9 and q["provider"] == "massive",
        "解析真实报价：中价 %.3f ｜ provider %s" % (q["mid"], q["provider"]))
    chk(q["quote_utc"] == "2026-09-18T23:59:59Z",
        "报价时刻解析正确：%s（这正是「休市」的证据）" % q["quote_utc"])
    bad, e2 = quote_from_stock('{"success": false, "error": "boom"}')
    chk(bad is None and "失败" in e2, "MCP 报失败 -> 如实返回原因，不硬造数据")
    bad2, e3 = quote_from_stock("not json")
    chk(bad2 is None and "不是 JSON" in e3, "返回不可解析 -> 如实说")

    # 快照兜底
    s = rtoken_from_snapshot("NVDA")
    chk(s is None or (s["mid"] > 0 and s["ts_utc"].endswith("Z")),
        "冻结快照兜底可读：%s" % (("中价 %.3f @ %s" % (s["mid"], s["ts_utc"])) if s else "（本机无快照，跳过）"))

    print("\n外部价格锚自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
