#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🏦 真实账户**只读取数** + 交易前置检查
========================================

━━ 为什么要有这个工具（补上那个刻意留着的缺口）━━

`project2/account_feed.py` 一直把"取数那一次调用"**故意空着**，理由是：

    没有授权就跑不了真接口，**写出来的东西无法验证**。本项目不写验证不了的东西。

2026-09-22 授权跑通之后，这个理由**不再成立** —— 现在可以真调一次、真看返回。
所以取数在这里落地，`account_feed.py` 继续只管"规范化成项目持仓单"（职责不重叠）。

━━ 🔴 为什么必须**新起一个进程**（这是本工具存在的第二个理由）━━

实测踩过：MCP 进程在**授权之前**启动的话，**不会热加载**新落盘的凭证，
私有接口一律报 `ConfigError: Private endpoint requires API credentials.`
（其 `suggestion` 会诱导你去配 `BITGET_API_*`，**千万别照做** —— 那是手动 Key 路线。）

本工具因此**每次自己起一个全新的 stdio 子进程**：新进程启动时读
`~/.bitget/oauth_token.json`，私有接口立刻可用（已实测）。

━━ 三条红线（写在代码里，不只在文档里）━━

  ① **只读**：只允许白名单里的三个只读工具（`get_auth_status` /
     `account_overview` / `position` 的 info·history·adlRank）。
     下单/撤单/提币/划转/杠杆设置**既不在白名单里，也没有任何参数能触达**。
  ② **绝不设 `BITGET_API_*`**：从环境里**剥掉**（那是手动 Key 路线，本项目走 OAuth）。
  ③ **凭证绝不进报告**：落盘前先过 `credential_guard()` —— 出现 `secretKey` /
     `passphrase` 之类的键，或 `apiKey` 竟然没有掩码，**直接拒写**。

━━ 已用真实返回校准的字段（2026-09-22，Agentic 账户 uid 7000******）━━

    取数入口      官方 MCP `@bitget-ai/bitget-agent-mcp` 3.3.1（stdio）
    余额          data.assets.data.{accountEquity, usdtEquity, assets[]}
    持仓          data.positions.data.list   ← 🔴 **空仓时是 `null`，不是 `[]`**
    手续费率      data.feeRate.data.{makerFeeRate, takerFeeRate}
    账户设置      data.settings.data.{uid, accountMode, holdMode, accountLevel}

    实测费率与项目假设**逐一对上**（这直接支撑 EDGE_THRESHOLD_BP = 11.34 的前提）：
      · 永续 USDT-FUTURES：maker 0.0002 / taker 0.0006 = **2 / 6 bp**  ✅
      · 现货 rToken（RHOODUSDT）：maker=taker 0.0005 = **5 bp**           ✅
    两个**新发现**（此前不知道，都会影响结论）：
      · 费率是**逐 symbol** 的 —— 同为 SPOT，`BTCUSDT` 是 10 bp、`RHOODUSDT` 是 5 bp。
        所以前置检查**必须按自己的标的查**，拿 BTC 的费率当代表就是错的。
      · `category=SPOT` 的 positions 段**不支持**，会返回
        `HTTP 400 / Parameter SPOT does not exist` —— 现货持仓不在这里（如实报出，不假装空仓）。

用法::

    python tools/account_read.py --read                    # 只读取数 -> data/account/
    python tools/account_read.py --read --bases NVDA HOOD
    python tools/account_read.py --preflight               # 交易前置检查（余额/持仓/费率）
    python tools/account_read.py --preflight --json
    python tools/account_read.py --selftest                # 离线自检（不联网）

取数后接项目持仓单（两步，职责分明）::

    python tools/account_read.py --read
    python project2/account_feed.py --ingest data/account/positions_raw.json \\
           --base-map ../bitgetS2_factory_trading/data/universe.csv
"""

import argparse
import datetime as dt
import io
import json
import os
import re
import subprocess
import sys
import threading
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACC_DIR = os.path.join(BASE, "data", "account")
LATEST = os.path.join(ACC_DIR, "read_latest.json")
POS_RAW = os.path.join(ACC_DIR, "positions_raw.json")
MCP_CFG = os.path.join(os.path.expanduser("~"), ".workbuddy", "mcp.json")
MCP_ENTRY = "bitget-agentic"
# 默认符号表：**项目一**的 universe.csv（列 base / perp_symbol / spot_symbol）
SIBLING_UNIVERSE = os.path.join(os.path.dirname(BASE), "bitgetS2_factory_trading",
                                "data", "universe.csv")

# 与 run_p2.py::DEFAULT_BASES、tools/mcp_anchor.py::DEFAULT_BASES 同源（自检会核这三处一致）
DEFAULT_BASES = ["NVDA", "TSLA", "AAPL", "META", "GOOGL", "SPY", "QQQ",
                 "SOXL", "HOOD", "MRVL"]
PERP_CAT = "USDT-FUTURES"

# 🔴 只读白名单：**唯一**允许出现的工具与参数。
#    写操作工具（order / strategy_order / withdraw / transfer_funds / deposit /
#    account_config / raw …）**整类不在表里**，连参数都到不了它们那儿。
WHITELIST = {
    "get_auth_status": {"args": ()},
    "account_overview": {"args": ("category", "symbol", "coin", "view", "fields")},
    "position": {"args": ("action", "category", "symbol", "posSide", "view",
                          "fields", "limit", "cursor"),
                 "enum": {"action": ("info", "history", "adlRank")}},
}
# 出现任何一个说明只读边界被写漏了（自检会核）
FORBIDDEN_TOOLS = ("order", "withdraw", "transfer", "deposit", "repay",
                   "subaccount", "account_config", "raw", "strategy")
FORBIDDEN_ACTIONS = ("close", "closeall", "place", "cancel", "setleverage",
                     "switch", "create", "submit")

# 凭证泄漏哨兵：键名命中即视为泄漏（值是否掩码另行判断）
SECRET_KEY_PARTS = ("secretkey", "apisecret", "passphrase", "privatekey", "secret")
HEXISH = re.compile(r"^[0-9a-fA-F]{32,}$")      # 长十六进制串 = 疑似未掩码密钥


# ────────────────────────────────────────────── 只读边界

def is_allowed(tool, args):
    """(可否调用, 原因)。**任何不在白名单的工具/参数一律拒绝**。"""
    spec = WHITELIST.get(tool)
    if spec is None:
        return False, "工具 %r 不在只读白名单里" % tool
    for k in (args or {}):
        if k not in spec["args"]:
            return False, "参数 %r 不在 %s 的白名单里" % (k, tool)
    for k, allowed in (spec.get("enum") or {}).items():
        v = (args or {}).get(k)
        if v is not None and v not in allowed:
            return False, "%s 的 %r=%r 不是只读取值（只允许 %s）" % (
                tool, k, v, "/".join(allowed))
    return True, ""


def build_calls(bases, syms):
    """构造**只读**调用集。syms: {base: {"perp":…, "spot":…}}。"""
    calls = [("get_auth_status", {}),
             ("account_overview", {"view": "full"}),
             ("account_overview", {"category": PERP_CAT, "view": "full"})]
    for b in bases:
        perp = (syms.get(b) or {}).get("perp") or (b + "USDT")
        calls.append(("account_overview",
                      {"category": PERP_CAT, "symbol": perp, "view": "full"}))
        spot = (syms.get(b) or {}).get("spot")
        if spot:
            calls.append(("account_overview",
                          {"category": "SPOT", "symbol": spot, "view": "full"}))
    return calls


def _default_base_map():
    """默认符号表 = 项目一 `data/universe.csv`（存在才用；不存在就退到"只查永续"）。"""
    return SIBLING_UNIVERSE if os.path.exists(SIBLING_UNIVERSE) else None


def load_symbols(path):
    """符号表 -> {base: {"perp":…, "spot":…}}。没有表就返回 ({}, 原因)。

    ⚠️ 现货符号带 `R` 前缀，**不能无条件剥**（真标的里也有 R 开头的）——
    没有符号表时**不猜**，只查永续（`<base>USDT` 是公开约定），现货费率留空并说明。
    """
    if not path:
        return {}, "未给符号表：只查永续（%sUSDT），现货费率不查（不猜 R 前缀）" % "<base>"
    if not os.path.exists(path):
        return {}, "符号表不存在：%s" % path
    import csv
    try:
        with io.open(path, encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
    except (OSError, ValueError) as exc:
        return {}, "符号表读取失败：%s: %s" % (type(exc).__name__, exc)
    if not rows:
        return {}, "符号表是空的：%s" % path
    cols = {c.lower(): c for c in rows[0].keys()}
    bcol = next((cols[c] for c in ("base", "coin", "asset") if c in cols), None)
    if not bcol:
        return {}, "符号表里找不到 base 列（现有列：%s）" % "、".join(list(cols.values())[:8])
    out = {}
    for r in rows:
        b = (r.get(bcol) or "").strip().upper()
        if not b:
            continue
        e = out.setdefault(b, {"perp": None, "spot": None})
        for key, cands in (("perp", ("perp_symbol", "symbol")),
                           ("spot", ("spot_symbol",))):
            for c in cands:
                if c in cols and (r.get(cols[c]) or "").strip():
                    e[key] = r[cols[c]].strip().upper()
                    break
    return out, None


# ────────────────────────────────────────────── MCP stdio 客户端

def mcp_launch():
    """从 `~/.workbuddy/mcp.json` 读 `bitget-agentic` 的启动方式。

    返回 ``(command, args, env, stripped, error)``。
    🔴 只**读配置**，不接受外部传入；并且**剥掉所有 `BITGET_*`**（手动 Key 路线）。
    """
    if not os.path.exists(MCP_CFG):
        return None, None, None, [], "找不到 MCP 配置：%s" % MCP_CFG
    try:
        with io.open(MCP_CFG, encoding="utf-8-sig") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError) as exc:
        return None, None, None, [], "MCP 配置解析失败：%s" % exc
    srv = (cfg.get("mcpServers") or {}).get(MCP_ENTRY)
    if not srv:
        return None, None, None, [], "MCP 配置里没有 %r 这一项" % MCP_ENTRY
    cmd, args = srv.get("command"), srv.get("args") or []
    if not cmd or not os.path.exists(str(cmd).replace("/", os.sep)):
        return None, None, None, [], "MCP 的 node 可执行文件不存在：%s" % cmd
    env = dict(os.environ)
    env.update(srv.get("env") or {})
    stripped = sorted(k for k in env if k.startswith("BITGET_"))
    for k in stripped:
        del env[k]
    # ⚠️ Node 的 fetch **不读** HTTP_PROXY，必须再加这一个（踩过：静默连不上）
    env["NODE_USE_ENV_PROXY"] = "1"
    return cmd, args, env, stripped, None


class McpStdio(object):
    """最小的 MCP stdio 客户端 —— 只为"起新进程 + 叫只读工具"而存在。"""

    def __init__(self, cmd, args, env):
        self.p = subprocess.Popen([cmd] + list(args), stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  env=env, text=True, encoding="utf-8", bufsize=1)
        self._lock = threading.Lock()
        self._inbox = {}
        self._err = []
        self.server = None
        threading.Thread(target=self._pump_out, daemon=True).start()
        threading.Thread(target=self._pump_err, daemon=True).start()

    def _pump_err(self):
        try:
            for ln in self.p.stderr:
                self._err.append(ln.rstrip())
        except (ValueError, OSError):
            pass

    def _pump_out(self):
        try:
            for ln in self.p.stdout:
                ln = ln.strip()
                if not ln.startswith("{"):
                    continue
                try:
                    msg = json.loads(ln)
                except ValueError:
                    continue
                if "id" in msg:
                    with self._lock:
                        self._inbox[msg["id"]] = msg
        except (ValueError, OSError):
            pass

    def _send(self, obj):
        self.p.stdin.write(json.dumps(obj) + "\n")
        self.p.stdin.flush()

    def _wait(self, i, timeout):
        t0 = time.time()
        while time.time() - t0 < timeout:
            with self._lock:
                if i in self._inbox:
                    return self._inbox.pop(i)
            time.sleep(0.05)
        return None

    def handshake(self, timeout=60):
        self._send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                               "clientInfo": {"name": "p2-account-read",
                                              "version": "1.0"}}})
        r = self._wait(1, timeout)
        if r is None:
            return None
        self.server = ((r.get("result") or {}).get("serverInfo"))
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self.server

    def call(self, i, tool, args, timeout=90):
        ok, why = is_allowed(tool, args)
        if not ok:                       # 🔴 兜底：白名单外的东西**根本发不出去**
            return {"_blocked": why}
        self._send({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                    "params": {"name": tool, "arguments": args}})
        r = self._wait(i, timeout)
        if r is None:
            return {"_timeout": True}
        res = r.get("result") or {}
        texts = [c.get("text") for c in (res.get("content") or [])
                 if c.get("type") == "text"]
        parsed = None
        if texts:
            try:
                parsed = json.loads(texts[0])
            except ValueError:
                parsed = {"_raw_text": texts[0][:600]}
        return {"isError": res.get("isError"), "error": r.get("error"), "data": parsed}

    def close(self):
        try:
            self.p.stdin.close()
        except (OSError, ValueError):
            pass
        time.sleep(0.2)
        try:
            self.p.kill()
        except OSError:
            pass


# ────────────────────────────────────────────── 脱敏守卫

def credential_guard(payload):
    """扫出凭证泄漏点。返回列表（空 = 干净）。**命中即拒绝落盘**。"""
    hits = []

    def walk(o, path):
        if isinstance(o, dict):
            for k, v in o.items():
                lk = str(k).lower()
                if any(p in lk for p in SECRET_KEY_PARTS):
                    hits.append("%s.%s（键名是凭证）" % (path, k))
                if lk == "apikey" and isinstance(v, str) and "*" not in v:
                    hits.append("%s.%s 未掩码（%d 字符）" % (path, k, len(v)))
                walk(v, "%s.%s" % (path, k))
        elif isinstance(o, list):
            for i, v in enumerate(o):
                walk(v, "%s[%d]" % (path, i))
        elif isinstance(o, str) and HEXISH.match(o):
            hits.append("%s 是长十六进制串（疑似未掩码密钥）" % path)

    walk(payload, "$")
    return hits


# ────────────────────────────────────────────── 取数

def read_account(bases=None, base_map=None, timeout=90, log=print):
    """新起一个 MCP 进程做**只读**取数，返回 payload（不落盘）。"""
    cmd, args, env, stripped, err = mcp_launch()
    if err:
        return {"status": "unavailable", "why": err}
    syms, sym_note = load_symbols(base_map)
    bases = [b.upper() for b in (bases or DEFAULT_BASES)]
    calls = build_calls(bases, syms)
    for tool, a in calls:                       # 构造完再查一遍（双保险）
        ok, why = is_allowed(tool, a)
        if not ok:
            return {"status": "unavailable", "why": "调用集里有非只读项：%s" % why}

    log("🏦 起一个**全新** MCP 进程（避开「长驻进程不热加载凭证」）…")
    cli = McpStdio(cmd, args, env)
    out = {"_README": "由 tools/account_read.py 的**只读**取数生成；"
                      "来源=Bitget Agentic 账户官方 MCP。本文件不含任何凭证。",
           "_generated_utc": dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
           "_base_map": base_map, "_base_map_entries": len(syms), "_base_map_note": sym_note,
           "_bases": bases, "_stripped_env": stripped,
           "calls": []}
    try:
        server = cli.handshake()
        if server is None:
            return {"status": "unavailable",
                    "why": "MCP 握手失败（stdio 无响应）",
                    "stderr": cli._err[:6]}
        out["_mcp"] = {"transport": "stdio", "server": server}
        i = 2
        for tool, a in calls:
            r = cli.call(i, tool, a, timeout=timeout)
            i += 1
            rec = {"tool": tool, "args": a}
            if r.get("_blocked"):
                rec.update({"blocked": r["_blocked"]})
            elif r.get("_timeout"):
                rec.update({"timeout": True})
            else:
                d = r.get("data") or {}
                rec.update({"isError": r.get("isError"), "error": r.get("error"),
                            "ok": d.get("ok"), "requestTime": d.get("requestTime"),
                            "data": d.get("data"), "_err": d.get("error")})
            out["calls"].append(rec)
            log("   · %-17s %-44s -> %s" % (tool, json.dumps(a, ensure_ascii=False),
                                            _brief(rec)))
    finally:
        cli.close()
    if not any(c.get("ok") for c in out["calls"]):
        out["status"] = "unavailable"
        out["why"] = "所有调用都没有 ok（网络/代理/授权问题）"
        out["stderr"] = cli._err[:6]
        return out
    out["status"] = "ok"
    # 凭证指纹：只留**服务端已掩码**的那串
    auth = _find(out["calls"], "get_auth_status", exact=[])
    d = (auth or {}).get("data") or {}
    out["_cred"] = {"api_key_masked": d.get("apiKey"),
                    "obtained_at": d.get("obtainedAt"),
                    "authorized": d.get("authorized")}
    return out


def _brief(rec):
    if rec.get("blocked"):
        return "BLOCKED " + rec["blocked"]
    if rec.get("timeout"):
        return "TIMEOUT"
    if rec.get("error"):
        return "ERR " + json.dumps(rec["error"], ensure_ascii=False)[:70]
    if rec.get("isError"):
        return "isError"
    return "ok=%s%s" % (rec.get("ok"), ("／" + str(rec["_err"])) if rec.get("_err") else "")


def _find(calls, tool, exact=None, **kw):
    """按 tool（+ 指定参数）定位一条调用记录。

    `exact`：参数键集合必须**完全相等** —— 同工具的多次调用（如每个标的查一次费率）
    必须靠它区分，否则会取到顺手第一个。
    """
    for c in calls or []:
        if c.get("tool") != tool:
            continue
        a = c.get("args") or {}
        if exact is not None and set(a) != set(exact):
            continue
        if all(a.get(k) == v for k, v in kw.items()):
            return c
    for c in calls or []:                      # 退化：放宽 exact，保证还能定位
        if c.get("tool") == tool and all((c.get("args") or {}).get(k) == v
                                         for k, v in kw.items()):
            return c
    return None


def save(payload, path=LATEST):
    """落盘（**先过脱敏守卫**，命中就拒写）。返回 (ok, 说明)。"""
    hits = credential_guard(payload)
    if hits:
        return False, "拒绝落盘：检出 %d 处疑似凭证 —— %s" % (len(hits), "；".join(hits[:3]))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    return True, path


# ────────────────────────────────────────────── 交易前置检查

def project_fee_refs():
    """项目自己假设的费率口径（**从代码读，不在这里重抄一遍字面量**）。"""
    try:
        sys.path.insert(0, os.path.join(BASE, "project2"))
        import execution_cost as ec
        return {"SPOT": (ec.FEE_SPOT, ec.FEE_SPOT),
                PERP_CAT: (ec.FEE_PERP_MAKER, ec.FEE_PERP_TAKER)}, None
    except Exception as exc:                      # noqa: BLE001 —— 读不到就如实说
        return {}, "读不到项目费率常量：%s: %s" % (type(exc).__name__, exc)


def _bp(v):
    x = _as_float(v)
    return None if x is None else round(x * 10000.0, 4)


def _as_float(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None


def preflight(payload):
    """余额 / 持仓 / 手续费率 三层前置检查。返回 (report, can_trade)。

    🔴 三态语义：**读不到**与**明确为零**必须分开。读不到时 `can_trade = None`
    （未知），绝不显示成"可以交易"，也绝不显示成"没有资金"。
    """
    calls = payload.get("calls") or []
    rep = {"source": payload.get("_generated_utc"), "file": None,
           "cred": payload.get("_cred") or {}, "checks": [], "notes": [], "warnings": []}

    # ---- ① 授权 ----
    auth = _find(calls, "get_auth_status", exact=[])
    ad = (auth or {}).get("data") or {}
    rep["authorized"] = bool(ad.get("authorized"))
    if not rep["authorized"]:
        rep["warnings"].append("授权状态为否 —— 下面所有数字都不可信，请先完成 OAuth")

    # ---- ② 余额 ----
    agg = _find(calls, "account_overview", exact=["view"])
    assets = ((agg or {}).get("data") or {}).get("assets")
    if not assets or not assets.get("ok"):
        rep["notes"].append("余额段读不到（%s）—— **这不是「没钱」，是没读到**"
                            % ((assets or {}).get("error") or "缺 assets 段"))
        rep["balance"] = {"status": "unavailable", "headline": "余额读不到（≠ 没有资金）"}
    else:
        a = assets.get("data") or {}
        eq = _as_float(a.get("accountEquity"))
        ueq = _as_float(a.get("usdtEquity"))
        zero = (eq == 0 and ueq == 0 and not (a.get("assets") or []))
        rep["balance"] = {
            "status": "zero" if zero else "ok",
            "account_equity": eq, "usdt_equity": ueq,
            "coins": [(c.get("coin"), c.get("available")) for c in (a.get("assets") or [])],
            "headline": ("账户可用权益 **0**（已确认，非缺数据）" if zero
                         else "账户权益 %.2f / USDT %.2f" % (eq or 0, ueq or 0))}
    r2 = agg
    fund = ((r2 or {}).get("data") or {}).get("fundingAssets")
    rep["funding_assets"] = ((fund or {}).get("data") or []) if (fund or {}).get("ok") else None
    setg = ((r2 or {}).get("data") or {}).get("settings") or {}
    rep["settings"] = (((setg.get("data") or {}).get("holdMode"),
                        (setg.get("data") or {}).get("accountMode"))
                       if setg.get("ok") else None)

    # ---- ③ 持仓 ----
    posrec = _find(calls, "account_overview", exact=["category", "view"],
                   category=PERP_CAT)
    pos = ((posrec or {}).get("data") or {}).get("positions")
    if not pos:
        rep["notes"].append("持仓段读不到（缺 positions 段）—— 不能当成空仓")
        rep["positions"] = {"status": "unavailable", "headline": "持仓读不到（≠ 没有持仓）"}
    elif not pos.get("ok"):
        # 例：category=SPOT 会返回 Parameter SPOT does not exist
        rep["notes"].append("持仓段报错：%s" % (pos.get("error") or "未知"))
        rep["positions"] = {"status": "unavailable", "headline": "持仓读不到（≠ 没有持仓）",
                            "error": pos.get("error")}
    else:
        lst = ((pos.get("data") or {}).get("list"))
        if lst in (None, []):        # 🔴 空仓时官方给的是 null
            rep["positions"] = {"status": "empty", "count": 0,
                                "headline": "**没有在途持仓**（已确认）"}
        else:
            rep["positions"] = {"status": "ok", "count": len(lst), "rows": lst,
                                "headline": "读到 %d 条持仓" % len(lst)}
            rep["warnings"].append(
                "非空持仓的字段名**尚未校准**（当前账户为空，拿不到真实非空返回）—— "
                "上面 rows 是**原样**贴出的，请人工核对，别按字段名硬解读")

    # ---- ④ 手续费率（逐 symbol，与项目口径对照）----
    refs, ref_err = project_fee_refs()
    if ref_err:
        rep["notes"].append(ref_err)
    fees, mism, unread = {}, [], []
    for c in calls:
        if c.get("tool") != "account_overview":
            continue
        sec = (c.get("data") or {}).get("feeRate") if isinstance(c.get("data"), dict) else None
        a = c.get("args") or {}
        if not a.get("symbol"):
            continue
        if not sec or not sec.get("ok"):
            unread.append("%s %s（%s）" % (a.get("category"), a.get("symbol"),
                                          (sec or {}).get("error") or "无 feeRate 段"))
            continue
        d = sec.get("data") or {}
        m, t = _bp(d.get("makerFeeRate")), _bp(d.get("takerFeeRate"))
        fees["%s %s" % (a.get("category"), a.get("symbol"))] = {"maker_bp": m, "taker_bp": t}
        exp = refs.get(a.get("category"))
        if exp and None not in (m, t):
            if abs(m - exp[0]) > 0.01 or abs(t - exp[1]) > 0.01:
                mism.append("%s：实际 maker %.1f / taker %.1f bp，项目假设 %.1f / %.1f bp"
                            % (a.get("symbol"), m, t, exp[0], exp[1]))
    rep["fee"] = {"by_symbol": fees, "mismatch": mism, "unread": unread,
                  "refs": {"%s" % k: v for k, v in refs.items()}}
    if mism:
        rep["warnings"].append(
            "手续费率与项目假设**不一致** —— 这会直接动摇 EDGE_THRESHOLD_BP=11.34 的"
            "推导前提（往返手续费 2×(现货5.0+永续2.0)=14.0 bp），必须重算门槛：" + "；".join(mism))
    if unread:
        rep["notes"].append("有 %d 项费率没读到：%s" % (len(unread), "；".join(unread[:4])))

    # ---- ⑤ 结论 ----
    b = rep["balance"]["status"]
    p = rep["positions"]["status"]
    if b == "unavailable" or p == "unavailable":
        can, why = None, "有读不到的项（余额或持仓）—— **未知不等于可以**，先修好读取"
    elif b == "zero":
        can, why = False, ("账户可用权益为 0：**无法下任何单**。"
                           "需要先把资金划入 Agentic 账户 —— 提币与主→子划转按约定"
                           "**由用户在 Web 手动完成**（Agent 不碰资金）")
    else:
        can, why = True, "余额与持仓都读到了，可以做单前检查"
    rep["verdict"] = {"can_trade": can, "why": why}
    if rep["warnings"]:
        rep["verdict"]["with_warnings"] = len(rep["warnings"])
    return rep, can


# ────────────────────────────────────────────── 输出

def print_report(rep):
    print("  授权    ：%s%s" % ("已授权" if rep.get("authorized") else "**未授权**",
                              (" ｜ %s" % rep["cred"].get("obtained_at"))
                              if rep.get("cred", {}).get("obtained_at") else ""))
    if rep.get("cred", {}).get("api_key_masked"):
        print("  凭据指纹：%s（服务端已掩码，**不是**明文）" % rep["cred"]["api_key_masked"])
    print("  ── ① 余额 ──")
    print("     %s" % rep["balance"]["headline"])
    if rep.get("funding_assets") is not None:
        print("     资金账户：%s" % (rep["funding_assets"] or "**空**"))
    print("  ── ② 持仓 ──")
    print("     %s" % rep["positions"]["headline"])
    for row in (rep["positions"].get("rows") or [])[:5]:
        print("     · %s" % json.dumps(row, ensure_ascii=False)[:160])
    print("  ── ③ 手续费率（逐 symbol，与项目口径对照）──")
    for k, v in list(rep["fee"]["by_symbol"].items())[:12]:
        m = v["maker_bp"]
        t = v["taker_bp"]
        print("     %-26s maker %s bp ｜ taker %s bp"
              % (k, "  --" if m is None else "%6.2f" % m,
                 "  --" if t is None else "%6.2f" % t))
    if not rep["fee"]["by_symbol"]:
        print("     （没读到任何费率）")
    if rep["fee"].get("refs"):
        print("     项目假设：%s"
              % " ｜ ".join("%s %s/%s bp" % (k, v[0], v[1])
                            for k, v in rep["fee"]["refs"].items()))
    for w in rep["warnings"]:
        print("     ⚠️ %s" % w)
    for n in rep["notes"]:
        print("     · %s" % n)
    v = rep["verdict"]
    tag = {True: "可以做单前检查", False: "**不能下单**", None: "**未知**"}[v["can_trade"]]
    print("  ── 结论 ── %s：%s" % (tag, v["why"]))


# ────────────────────────────────────────────── 离线自检

# 已校准的真实返回（2026-09-22 实测）—— **uid 与未掩码字段已剔除**，
# 用真结构做 fixture，自检因此**不需要网络**。
FIXTURE = {
    "_generated_utc": "2026-09-21 18:56:53 UTC",
    "_cred": {"api_key_masked": "bg_ff" + "*" * 26 + "7fe4",
              "obtained_at": "2026-09-21T18:50:23.414Z", "authorized": True},
    "calls": [
        {"tool": "get_auth_status", "args": {}, "ok": True,
         "data": {"authorized": True, "apiKey": "bg_ff" + "*" * 26 + "7fe4",
                  "obtainedAt": "2026-09-21T18:50:23.414Z"}},
        {"tool": "account_overview", "args": {"view": "full"}, "ok": True,
         "data": {"assets": {"ok": True, "data": {
             "accountEquity": "0", "usdtEquity": "0", "btcEquity": "0",
             "unrealisedPnl": "0", "effEquity": "0", "positionValue": "0",
             "assets": []}},
             "settings": {"ok": True, "data": {
                 "uid": "7000######", "accountMode": "unified",
                 "assetMode": "multi_assets", "holdMode": "hedge_mode",
                 "accountLevel": "basic"}},
             "fundingAssets": {"ok": True, "data": []}}},
        {"tool": "account_overview",
         "args": {"category": PERP_CAT, "view": "full"}, "ok": True,
         "data": {"assets": {"ok": True, "data": {"accountEquity": "0",
                                                  "usdtEquity": "0", "assets": []}},
                  "positions": {"ok": True, "data": {"list": None}}}},
        {"tool": "account_overview",
         "args": {"category": PERP_CAT, "symbol": "HOODUSDT", "view": "full"},
         "ok": True, "data": {"positions": {"ok": True, "data": {"list": None}},
                              "feeRate": {"ok": True, "data": {
                                  "makerFeeRate": "0.0002", "takerFeeRate": "0.0006"}}}},
        {"tool": "account_overview",
         "args": {"category": "SPOT", "symbol": "RHOODUSDT", "view": "full"},
         "ok": True, "data": {"positions": {"ok": False,
                                            "error": "HTTP 400 from Bitget: "
                                                     "Parameter SPOT does not exist"},
                              "feeRate": {"ok": True, "data": {
                                  "makerFeeRate": "0.0005", "takerFeeRate": "0.0005"}}}},
    ],
}


def selftest():
    """离线自检：**只读边界 / 脱敏守卫 / 三态语义 / 费率对照 / 结论**。不联网。"""
    ok = True

    def chk(cond, msg):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s" % ("OK " if cond else "!! ", msg))

    # ① 🔴 只读边界：白名单里没有写操作，且**参数名也不含写动作**
    bad = [t for t in WHITELIST if any(f in t.lower() for f in FORBIDDEN_TOOLS)]
    chk(not bad, "白名单里没有写操作工具（只 %s）" % "、".join(sorted(WHITELIST)))
    badargs = [(t, a) for t, s in WHITELIST.items() for a in s["args"]
               if any(f in a.lower() for f in FORBIDDEN_ACTIONS)]
    chk(not badargs, "白名单的参数名里没有写动作（命中 %s）" % badargs)
    chk(WHITELIST["position"]["enum"]["action"] == ("info", "history", "adlRank"),
        "position 只放行 info/history/adlRank —— close / closeAll **连参数都传不进去**")

    # ② 白名单外的一律拒绝（含最容易误用的几个）
    for tool, args in (("order", {"action": "place"}),
                       ("withdraw", {"coin": "USDT"}),
                       ("transfer_funds", {"from": "main"}),
                       ("account_config", {"action": "setLeverage"}),
                       ("raw", {"operationId": "placeOrder"}),
                       ("position", {"action": "close", "category": PERP_CAT}),
                       ("position", {"action": "info", "confirm": True})):
        allowed, why = is_allowed(tool, args)
        chk(not allowed, "拒掉：%s %s（%s）" % (tool, json.dumps(args, ensure_ascii=False), why))

    # ③ 构造出来的调用集必须**逐条**过白名单
    calls = build_calls(["HOOD", "NVDA"], {"HOOD": {"perp": "HOODUSDT",
                                                    "spot": "RHOODUSDT"}})
    viol = [(t, a) for t, a in calls if not is_allowed(t, a)[0]]
    chk(not viol, "取数调用集 %d 条全部在只读白名单内（含 get_auth_status/overview/fee）"
        % len(calls))
    chk(any(a.get("category") == "SPOT" for _, a in calls),
        "有符号表时**会**查现货费率（RHOODUSDT 这种 R 符号只能从表里拿，不靠剥前缀）")
    calls_nomap = build_calls(["HOOD"], {})
    chk(not any(a.get("category") == "SPOT" for _, a in calls_nomap),
        "没符号表时**不猜** R 前缀 -> 不查现货费率")

    # ④ 脱敏守卫：真凭证要拦住，掩码的要放行
    leak = {"data": {"secretKey": "abc", "passphrase": "12345678",
                     "apiKey": "bg_ff0123456789abcdef0123456789abcdef"}}
    hits = credential_guard(leak)
    chk(len(hits) >= 3, "脱敏守卫拦下未掩码密钥（%d 处）：%s" % (len(hits), hits[0]))
    chk(not credential_guard(FIXTURE), "已掩码的真实返回 -> 无泄漏点，可落盘")
    chk(len(credential_guard({"nested": [{"b": {"apiSecret": "x"}}]})) == 1,
        "深挖嵌套结构（list / dict 都要扫到）")

    # ⑤ 三态语义：**空仓 = null**，必须报 empty 而**不是**"读不到"
    rep, can = preflight(FIXTURE)
    chk(rep["positions"]["status"] == "empty" and rep["positions"]["count"] == 0,
        "`data.list = null`（官方空仓写法）-> **empty（已确认）**，不是 unavailable")
    chk(rep["balance"]["status"] == "zero",
        "accountEquity=0 且 assets=[] -> **zero（已确认没钱）**，不是 unavailable")
    chk(can is False and "手动" in rep["verdict"]["why"],
        "余额为 0 -> can_trade=False，并写清『提币与主→子划转由用户 Web 手动完成』")

    # ⑥ 读不到时**不许**当成 0 / 不许当成可以
    broken = {"calls": [{"tool": "account_overview", "args": {"view": "full"},
                         "ok": False, "data": None}]}
    rep_b, can_b = preflight(broken)
    chk(rep_b["balance"]["status"] == "unavailable" and can_b is None,
        "余额读不到 -> unavailable 且 can_trade=**None（未知）** —— 不显示成「没有资金」，"
        "也不显示成「可以交易」")
    chk("≠ 没有资金" in rep_b["balance"]["headline"],
        "读数缺失的措辞明确区分『读不到』与『没有』：%s" % rep_b["balance"]["headline"])

    # ⑦ 费率换算与对照：实测 2/6 bp 与 5 bp 应当**一致**；改一动就报警
    fees = rep["fee"]["by_symbol"]
    chk(fees.get("USDT-FUTURES HOODUSDT") == {"maker_bp": 2.0, "taker_bp": 6.0},
        "永续费率换算：0.0002/0.0006 -> 2.0/6.0 bp（与项目假设一致，无 warning）")
    chk(fees.get("SPOT RHOODUSDT") == {"maker_bp": 5.0, "taker_bp": 5.0},
        "现货 rToken 费率换算：0.0005 -> 5.0 bp（maker=taker，与 docs/09 一致）")
    chk(not rep["fee"]["mismatch"], "费率一致 -> **不**报 mismatch（不狼来了）")
    refs, ref_err = project_fee_refs()
    chk(ref_err is None and refs.get(PERP_CAT) == (2.0, 6.0) and refs.get("SPOT") == (5.0, 5.0),
        "费率基准**从 project2/execution_cost.py 读**（不在这里重抄字面量）：%s" % refs)
    drift = json.loads(json.dumps(FIXTURE))
    for c in drift["calls"]:
        if (c.get("args") or {}).get("symbol") == "HOODUSDT":
            c["data"]["feeRate"]["data"]["makerFeeRate"] = "0.0001"
    rep_d, _ = preflight(drift)
    chk(rep_d["fee"]["mismatch"] and "11.34" in rep_d["warnings"][-1],
        "费率漂移 -> 报警，并点明它动摇 EDGE_THRESHOLD_BP=11.34 的前提")

    # ⑧ `category=SPOT` 的 positions 段会 400（实测）—— 如实报出，不当空仓
    spot = [c for c in FIXTURE["calls"] if (c.get("args") or {}).get("category") == "SPOT"]
    chk(spot and spot[0]["data"]["positions"]["ok"] is False,
        "现货持仓段实测返回 400（SPOT 不支持）—— fixture 保留这个事实")
    withspot = json.loads(json.dumps(FIXTURE))
    withspot["calls"] = [c for c in withspot["calls"]
                         if (c.get("args") or {}).get("category") != PERP_CAT]
    rep_s, can_s = preflight(withspot)
    chk(rep_s["positions"]["status"] == "unavailable" and can_s is None,
        "缺持仓段 -> unavailable + can_trade=None（**不假装空仓**）")

    # ⑨ 非空持仓：字段名未校准 → 原样贴出并**明确警告**，不硬解读
    nonempty = json.loads(json.dumps(FIXTURE))
    for c in nonempty["calls"]:
        if (c.get("args") or {}).get("category") == PERP_CAT and "symbol" not in c["args"]:
            c["data"]["positions"]["data"]["list"] = [{"symbol": "HOODUSDT",
                                                       "total": "5000", "_uncalibrated": True}]
    rep_n, can_n = preflight(nonempty)
    chk(rep_n["positions"]["status"] == "ok" and rep_n["positions"]["count"] == 1
        and "尚未校准" in rep_n["warnings"][-1],
        "非空持仓 -> 报出条数，并警告字段名未校准（当前账户为空，校准不了）")

    # ⑩ 标的名录三处一致（防漂）
    def _grep_list(path):
        with io.open(path, encoding="utf-8") as fh:
            src = fh.read()
        m = re.search(r"DEFAULT_BASES\s*=\s*\[(.*?)\]", src, re.S)
        return [x.strip().strip('"\'').upper() for x in m.group(1).split(",") if x.strip()] \
            if m else None
    a1 = _grep_list(os.path.join(BASE, "run_p2.py"))
    a2 = _grep_list(os.path.join(BASE, "tools", "mcp_anchor.py"))
    chk(a1 == DEFAULT_BASES and a2 == DEFAULT_BASES,
        "DEFAULT_BASES 三处一致（run_p2 / mcp_anchor / 本工具）：%s" % "、".join(DEFAULT_BASES))

    # ⑪ 启动方式只从 mcp.json 读，且**剥掉 BITGET_***
    cmd, args_, env, stripped, err = mcp_launch()
    chk(err is None and cmd and args_, "从 ~/.workbuddy/mcp.json 读到 bitget-agentic 启动方式")
    chk(err is None and not any(k.startswith("BITGET_") for k in env),
        "启动环境里**没有** BITGET_*（手动 Key 路线，本工具剥掉 %s）" % (stripped or "无"))
    chk(env.get("NODE_USE_ENV_PROXY") == "1",
        "带上 NODE_USE_ENV_PROXY=1（Node 的 fetch 不读 HTTP_PROXY，缺它连不上）")
    chk("./lib/index.js" in " ".join(args_) or "lib/index.js" in " ".join(args_),
        "启动的是官方 MCP 包本体（不是别的什么）")

    # ⑫ 只读的**结构性**证据：本文件的调用集里没有写端点
    #    ⚠️ 判据字符串**拼出来**再比 —— 直接写字面量会命中本自检自己的这一行（踩过两次）
    with io.open(os.path.abspath(__file__), encoding="utf-8") as fh:
        src = fh.read()
    needles = ["/api/v3/trade/" + "place" + "-order",
               "/api/v3/account/" + "with" + "drawal",
               "/api/v3/account/" + "trans" + "fer",
               "close" + "AllPositions"]
    badpaths = [p for p in needles if p in src]
    chk(not badpaths, "本文件里没有任何写端点路径（命中 %d 个）" % len(badpaths))

    print("\n真实账户取数与前置检查自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


# ────────────────────────────────────────────── 入口

def main(argv=None):
    ap = argparse.ArgumentParser(description="🏦 真实账户只读取数 + 交易前置检查")
    ap.add_argument("--read", action="store_true",
                    help="新起 MCP 进程做一次**只读**取数，落盘到 data/account/")
    ap.add_argument("--preflight", action="store_true",
                    help="交易前置检查：余额 / 持仓 / 手续费率")
    ap.add_argument("--bases", nargs="*", default=None, help="取数标的（默认 10 个）")
    ap.add_argument("--base-map", default=None,
                    help="符号表（可用项目一 data/universe.csv）—— 现货 R 符号只从表里拿")
    ap.add_argument("--file", default=None, help="--preflight 读哪个文件（默认 read_latest.json）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)

    if a.selftest:
        return selftest()

    if a.read:
        # 默认用**项目一**的符号表（有它才查得到 RHOODUSDT 这类现货符号的费率；
        # 没有就不查现货 —— **绝不靠剥 R 前缀去猜**）
        bm = a.base_map or _default_base_map()
        payload = read_account(bases=a.bases, base_map=bm)
        if payload.get("status") != "ok":
            print("❌ 取数失败：%s" % payload.get("why"))
            for ln in (payload.get("stderr") or [])[:4]:
                print("   stderr: %s" % ln)
            return 1
        okw, msg = save(payload, LATEST)
        if not okw:
            print("❌ %s" % msg)
            return 1
        # 顺手把"持仓段"单独落一份，给 account_feed --ingest 直接吃
        # （就是官方 positions 段的原样，`{"data": {"list": null}}` 这种空仓写法也保留）
        posrec = _find(payload["calls"], "account_overview", exact=["category", "view"],
                       category=PERP_CAT)
        pos_sec = (posrec or {}).get("data", {}).get("positions") \
            if isinstance((posrec or {}).get("data"), dict) else None
        okp, msgp = (save(pos_sec, POS_RAW) if pos_sec
                     else (False, "取数结果里没有 positions 段"))
        print("✅ 已落盘：%s%s" % (os.path.relpath(LATEST, BASE),
                                  "" if okp else "（持仓段未落盘：%s）" % msgp))
        if okp:
            print("   持仓段：%s → 可直接 `python project2/account_feed.py --ingest`"
                  % os.path.relpath(POS_RAW, BASE))
        print("   下一步：python tools/account_read.py --preflight")
        return 0

    # 默认动作 = 前置检查（读已落盘的取数结果，不联网）
    path = a.file or LATEST
    if not os.path.exists(path):
        print("❌ 没有取数结果：%s" % path)
        print("   先跑：python tools/account_read.py --read")
        return 1
    try:
        with io.open(path, encoding="utf-8-sig") as fh:
            payload = json.load(fh)
    except (OSError, ValueError) as exc:
        print("❌ 解析失败：%s" % exc)
        return 1
    rep, can = preflight(payload)
    rep["file"] = path
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    else:
        print("🏦 交易前置检查：%s" % path)
        print_report(rep)
    return 0 if can is not False else 1


if __name__ == "__main__":
    sys.exit(main())
