#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🏦 账户 / 仓位接入层（**只读**，可降级）
==========================================

━━ 定位 ━━

用户 2026-09-20：「后续可能要提供 bitget 等交易所账号进行**仓位管理**、方便查看，
**但不自动进行交易而是辅助**。」

本模块是那件事的**接入层**：把"仓位从哪来"抽象成一个可替换的来源，
让上层的执行进度官 / 持仓巡检**不关心**数据是手写的、还是从交易所读的。

━━ 主路线：Agentic 账户（用户 2026-09-21 定）━━

用户 2026-09-21：「**都做，其中主要走 agentic 路线**」。

所以 `agentic` 是**主动线**，`bgc` 是它的命令行形态，`file` 是**没授权时的降级**。
"项目一那套 API Key 只读"路线**不在本模块**（在项目一的 `common/bitget_private.py`）——
本模块不重复它：一条数据只能有一个真相源。

**Agentic 账户是什么**（三处出处，都可核）：

  · **手册原文**（转录在 **项目一** `docs/23-手册要点核对（修正清单）.md` 修正 5）：
    「Agentic 账户｜资金隔离、额度控制、**不可提币**、OAuth 授权」，用途：若做 Demo 演示用。
  · **官方 Agent Hub FAQ** https://www.bitget.com/activity-hub/agent-hub ：
    "a dedicated trading account **isolated from your main account**. The agent can access
    only the amount of funds you transfer into this account"；
    "Can an Agentic Account withdraw funds? **No.**"
    ⚠️ 手册里的「**额度控制**」在官方 FAQ 里**并没有**"单笔限额"这项产品功能 ——
       它的真实含义是「**上限 = 你转入该账户的金额**」（资金隔离本身就构成额度控制）。
       材料按这个解释写才准确。
    ⚠️ 「不可提币」的出处是这份 FAQ（或 UTA 文档），**不是**下面那篇 Connection Guide。
  · **官方接入指南**（给 AI Agent 读的首次接入手册，2026-09-03）
    https://www.bitget.com/support/articles/12560603894122 ：
    Skill（`--skill agentic`，≥3.3.0）+ MCP（`@bitget-ai/bitget-agent-mcp`，stdio，Node 20+）
    → 浏览器 OAuth（`authorize_start` / `authorize_wait` / `get_auth_status`）
    → 凭证三件套由 MCP 回调**自动落盘**，用户**不用手填、不用粘贴**。
    撤销 = 在官网删掉该账户的 API Key（**删 Key 不会自动平仓或撤单**）。

相关文档：本项目 `docs/52`（编制与落地顺序）、`docs/53`（官方 MCP 集成）；
**项目一** `docs/54-接入只读与交易权限（方案与风险清单）.md`（API Key 备选路线）。
> 引用约定：写「**项目一** docs/NN」才是跨仓库引用；裸 `docs/NN` 一律指本仓库。

━━ 三条红线（在代码里，不只在文档里）━━

  ① **绝不自动下单**。本模块**不实现**任何下单/提币/划转端点，也**不执行任何外部命令**。
  ② **密钥绝不进日志 / 页面 / 仓库**。只从环境变量读；日志里只允许出现**指纹**。
  ③ **「没有仓位」与「读不到仓位」必须是两种不同的显示**：**没有数据 ≠ 没有风险**。

━━ 四种来源（按优先级）━━

  ``agentic``  Bitget Agentic 账户（**主动线**）：OAuth 授权、资金隔离、
               **不可提币**、不用手填 API Key。⚠️ 需要你先完成 OAuth。
  ``bgc``      Agent Hub 的 `bgc` CLI。⚠️ 同样需要授权；且**必须 `--read-only`**。
  ``file``     `data/positions/open.json`（**当前实际在用**的降级路径）。
  ``none``     明确没有配置任何来源。

━━ 取数在哪（2026-09-22 起）━━

授权跑通后，"没有授权就跑不了、写了也无法验证"这个理由**不再成立**，
所以**取数已经落地**，但落在**另一个工具**里，免得职责重叠：

    python tools/account_read.py --read        # 新起一个 MCP 进程做只读取数
    python tools/account_read.py --preflight    # 余额 / 持仓 / 手续费率 前置检查

本模块继续只管第二段（**离线可验证**的那段）：规范化、两腿归并、落盘。

    python project2/account_feed.py --ingest data/account/positions_raw.json \\
           --base-map ../bitgetS2_factory_trading/data/universe.csv

⚠️ 字段名**已用真实返回校准**（2026-09-22，见 SCHEMA_VERIFIED_WITH_REAL_OUTPUT）：
空仓时官方给的是 `data.list: null`（**不是 `[]`**）—— 那是「明确为空」这个**结论**，
绝不能报成"认不出结构"（那是"缺数据"）。自检里有这一条。

⚠️ 一个**已知没校准**的点，如实标出来，不猜：

  · **rToken 的 `R` 前缀会让 `RHOODUSDT` 变成一个凭空多出来的"标的"**
    （现货符号是 `RHOODUSDT`、永续是 `HOODUSDT`，两条腿必须归到同一个 `HOOD`）。
    这**不能靠猜** —— 因为真标的里也可能有以 R 开头的（如 `RIVN`），
    无条件剥 `R` 会把 `RIVN` 错切成 `IVN`。
    **解决方式是用现成的符号表**：`--base-map`，可直接指向
    **项目一** `data/universe.csv`（它带 `base` / `spot_symbol` / `perp_symbol` 三列）。
    不给映射表时，只有 `symbol` 的行**一律不猜**，原样报出来等一次校准。

用法::

    python project2/account_feed.py                     # 看当前来源与仓位
    python project2/account_feed.py --json
    python project2/account_feed.py --plan              # 打印"取数该怎么做"（本模块不执行）
    python project2/account_feed.py --ingest data/account/positions_raw.json
    python project2/account_feed.py --ingest a.json --base-map <项目一>/data/universe.csv
    python project2/account_feed.py --selftest
"""

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import os
import shutil
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POS_FILE = os.path.join(BASE, "data", "positions", "open.json")

# 🔴 只读保证：本模块**只允许**调用这些查询类入口。
#    任何写操作端点（下单/撤单/提币/划转）**都不在**这个列表里，
#    自检会核验"列表里没有写操作"。
READ_ONLY_COMMANDS = ("account", "position", "orders", "balance", "fills")

# 🔴 OAuth 凭证的**落盘路径**（2026-09-22 实测确认，此前记为"官方未公开"）。
#    本模块**只判断这个文件在不在**，绝不读取、绝不打印其中任何一个字段
#    （里面是 userId / apiKey / secretKey / passphrase / obtainedAt）。
OAUTH_TOKEN_REL = (".bitget", "oauth_token.json")

# 危险动作关键词：出现任何一个就说明我们把只读边界写漏了
FORBIDDEN = ("place", "cancel", "withdraw", "transfer", "order-create",
             "submit", "close-position", "leverage-set")

# ✅ 字段名**已用真实返回校准**（2026-09-22，tools/account_read.py --read 的落盘结果）：
#      root: ok / endpoint / requestTime / data
#      持仓: data.positions.data.list      ← 🔴 **空仓时是 `null`，不是 `[]`**
#      余额: data.assets.data.{accountEquity, usdtEquity, assets[]}
#      费率: data.feeRate.data.{makerFeeRate, takerFeeRate}
#    实测费率与项目假设逐一对上（永续 2/6 bp、现货 rToken 5/5 bp）。
#    下面的键名候选保留，用于**识别**上游可能的写法差异；认不出来依旧报不认识。
SCHEMA_VERIFIED_WITH_REAL_OUTPUT = True
BASE_KEYS = ("base", "coin", "asset")
SYMBOL_KEYS = ("symbol", "instId", "inst_id", "pair")
VENUE_KEYS = ("venue", "side", "market", "product", "marketType", "market_type")
SIZE_KEYS = ("qty_usd", "usdt_amount", "notional_usd", "notional", "qty",
             "available", "amount", "total", "size")
FILLED_KEYS = ("filled", "is_filled", "filled_flag")


def cred_fingerprint(name=None):
    """凭据**指纹**（只用于"这次用的是哪把 key"的可核验性，**不可逆推**）。

    ⚠️ 绝不返回凭据本身，也绝不写进日志 —— 只允许 8 位指纹。
    """
    for k in (name, "BITGET_API_KEY", "BITGET_ACCESS_KEY", "BGC_API_KEY"):
        if not k:
            continue
        v = os.environ.get(k)
        if v:
            return hashlib.sha256(v.encode("utf-8")).hexdigest()[:8]
    return None


def _rel(p):
    """仓库相对路径；**跨盘符**（D: 仓库 / C: 用户目录）时回退成绝对路径 —— 踩过。"""
    try:
        return os.path.relpath(p, BASE)
    except ValueError:
        return os.path.abspath(p)


def cred_files():
    """可能在的授权凭证文件（**只判断存在性**，不读内容、不打印任何字段）。"""
    home = os.path.expanduser("~")
    return (os.path.join(home, *OAUTH_TOKEN_REL),          # ~/.bitget/oauth_token.json
            os.path.join(BASE, ".bgc", "auth.json"),
            os.path.join(home, ".bgc", "auth.json"))


def detect_source():
    """判断当前能用的仓位来源。**返回 (source, 说明, 是否已授权)**。

    顺序刻意如此：Agentic/BGC（真实账户）优先于文件；但**没授权时不停在那里**，
    而是继续退到文件，并在说明里写清"真实账户未授权"—— 让读者知道
    "你看到的仓位是手写的，不是从交易所读的"。
    """
    notes = []
    # ① Agentic 账户（OAuth）：授权时 MCP 会把凭证落盘（官方指南 Step 5）
    for p in cred_files():
        if os.path.exists(p):
            return ("agentic", "检测到 OAuth 凭证文件（%s）；**只读模式**" % _rel(p), True)
    if os.environ.get("BGC_READ_ONLY") == "1":
        return ("agentic", "环境变量 BGC_READ_ONLY=1 已设置（**只读模式**）", True)
    notes.append("Agentic / bgc 未授权")
    # ② bgc CLI
    if shutil.which("bgc"):
        notes.append("检测到 bgc 可执行文件，但它**不是** Agentic 通道"
                     "（auth 走 BITGET_API_KEY，属备选的手动 Key 路线）")
    # ③ 文件（降级）
    if os.path.exists(POS_FILE):
        return ("file", "；".join(notes + ["退到文件源：%s（**手写，不是交易所读数**）"
                                          % _rel(POS_FILE)]), False)
    notes.append("文件源也不存在（%s）" % _rel(POS_FILE))
    return ("none", "；".join(notes), False)


def plan_lines():
    """取数该怎么做 —— 本模块只**打印**，**不执行**任何命令。

    取数已有专门的工具 `tools/account_read.py`：它每次**新起一个 MCP 进程**
    （长驻进程不会热加载新落盘的凭证 —— 实测踩过），且只调只读白名单工具。
    本模块**不重复实现**它，只把"该跑什么"说清楚。

    ⚠️ 别再照抄 `bgc --read-only`：本机装的 `bgc` 是**手动 API Key 版**
    （auth 走 `BITGET_API_KEY`），不是 Agentic 通道 —— 那条路是备选，本项目不走。
    """
    return [
        "# ① 取数（只读）：新起一个 MCP 进程读 Agentic 账户（凭证走 OAuth）",
        "python tools/account_read.py --read",
        "",
        "# ② 交易前置检查：余额 / 持仓 / 手续费率（费率与项目口径逐一对照）",
        "python tools/account_read.py --preflight",
        "",
        "# ③ 把持仓段规范化成项目持仓单（默认只打印，加 --write 才落盘）",
        "#    带符号表 —— 现货 rToken 是 RHOODUSDT、永续是 HOODUSDT，",
        "#    必须归到同一个 HOOD，否则会凭空多出一个标的。",
        "python project2/account_feed.py --ingest data/account/positions_raw.json",
        "python project2/account_feed.py --ingest data/account/positions_raw.json \\",
        "       --base-map ../bitgetS2_factory_trading/data/universe.csv --write",
        "",
        "# 落盘后，下游（执行进度官 / 持仓巡检）读的是项目既有格式，**零改动**。",
        "# 只允许上面这些查询类动作（%s）。" % "、".join(READ_ONLY_COMMANDS),
    ]


def _pick(row, keys):
    """从一行里按候选键取值 —— 返回 (值, 命中的键)。**取不到就返回 (None, None)**。"""
    for k in keys:
        if k in row and row[k] not in (None, "", []):
            return row[k], k
    return None, None


def _as_float(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None      # 挡掉 NaN


def _find_rows(raw):
    """定位"行列表"。认不出来返回 (None, 说明)。"""
    if isinstance(raw, list):
        return raw, "顶层就是列表"
    if isinstance(raw, dict):
        for k in ("positions", "data", "list", "result", "items", "rows", "records"):
            v = raw.get(k)
            if isinstance(v, list):
                return v, "取 dict[%r]（列表）" % k
            if isinstance(v, dict):
                for k2 in ("list", "items", "rows", "positions"):
                    if isinstance(v.get(k2), list):
                        return v[k2], "取 dict[%r][%r]（列表）" % (k, k2)
        vals = list(raw.values())
        if vals and all(isinstance(x, dict) for x in vals):
            rows = [dict(x, base=x.get("base") or k) for k, x in raw.items()]
            return rows, "顶层是 {base: {...}} 映射"
    return None, "认不出结构（顶层是 %s）" % type(raw).__name__


def load_base_map(path):
    """读符号 → base 的映射表。支持两种现成格式：

      · **项目一 `data/universe.csv`**（列 `base` / `spot_symbol` / `perp_symbol`）—— 直接可用；
      · 或任意 JSON（``{"RHOODUSDT": "HOOD", ...}``，嵌套一层也认）。

    ⚠️ 为什么需要它：现货 rToken 符号带 `R` 前缀，**不能无条件剥** ——
    真标的里也有以 R 开头的（如 `RIVN`），无条件剥会切成 `IVN`。
    有权威符号表就别用启发式。
    """
    if not path:
        return {}, None
    if not os.path.exists(path):
        return {}, "映射表不存在：%s" % path
    try:
        if path.lower().endswith(".json"):
            with io.open(path, encoding="utf-8-sig") as fh:
                raw = json.load(fh)
            out = {}

            def walk(d):
                for k, v in (d or {}).items():
                    if isinstance(v, dict):
                        walk(v)
                    elif isinstance(v, str) and v:
                        out[str(k).upper()] = v.upper()
            walk(raw if isinstance(raw, dict) else {})
            return out, None
        # CSV：优先用列名找（兼容 universe.csv）
        with io.open(path, encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        if not rows:
            return {}, "映射表是空的：%s" % path
        cols = {c.lower(): c for c in rows[0].keys()}
        base_col = next((cols[c] for c in ("base", "coin", "asset") if c in cols), None)
        if not base_col:
            return {}, "映射表里找不到 base 列（现有列：%s）" % "、".join(list(cols.values())[:8])
        sym_cols = [cols[c] for c in ("spot_symbol", "perp_symbol", "symbol", "instid")
                    if c in cols]
        out = {}
        for r in rows:
            b = (r.get(base_col) or "").strip().upper()
            if not b:
                continue
            for sc in sym_cols:
                s = (r.get(sc) or "").strip().upper()
                if s:
                    out[s] = b
        return out, None
    except (OSError, ValueError) as exc:
        return {}, "映射表读取失败：%s: %s" % (type(exc).__name__, exc)


def _explicit_empty(raw, depth=5):
    """识别官方"**明确为空**"的写法。

    🔴 实测（2026-09-22）：空仓时 `positions.data.list` 给的是 **`null`，不是 `[]`**。
    这是**结论**（这就是"没有持仓"），跟"认不出结构"（= 缺数据）是两件事 ——
    混起来就正好踩中本模块第三条红线。
    """
    if depth <= 0:
        return False
    if isinstance(raw, dict):
        for k in ("list", "items", "rows", "positions"):
            if k in raw and raw[k] is None:
                return True
        for v in raw.values():
            if isinstance(v, (dict, list)) and _explicit_empty(v, depth - 1):
                return True
    if isinstance(raw, list):
        for v in raw:
            if isinstance(v, (dict, list)) and _explicit_empty(v, depth - 1):
                return True
    return False


def _rows_look_real(rows):
    """这些"行"像不像真的读数行（至少一行带得出规模，或带得出 symbol）？

    🔴 用来挡掉一种**凭空造行**：`_find_rows` 有个 `{base: {...}}` 映射的启发式，
    遇到 `{"data": {"positions": {...}}}` 这种结构会给 `data` 编出一行
    `base='data'` —— 那样"空"就永远走不到 empty 分支了。
    """
    if not isinstance(rows, list) or not rows:
        return False
    for r in rows:
        if isinstance(r, dict) and (_pick(r, SIZE_KEYS)[0] is not None
                                    or any(k in r for k in SYMBOL_KEYS)):
            return True
    return False


def normalize_positions(raw, base_map=None):
    """把交易所的**只读**返回规范成项目持仓单格式。返回 ``(positions, report)``。

    ⚠️ 四条不许违反的规矩：
      · **不猜 base**：有 `base` 字段就用；否则查 `base_map`；都没有就**报出来**，
        绝不靠"剥 R 前缀"这类启发式去编一个（见 load_base_map 的说明）；
      · **不造数**：缺规模的行**不填 0**（填 0 = 把"读不到"说成"没有敞口"）；
      · **可核对**：report 写清每列取自哪个键、哪些行没认出来、假设了什么；
      · **不破坏下游契约**：产出的键就是项目既有的 base / qty_usd /
        spot_filled / perp_filled / synthetic。

    两种输入形态都收：
      A. 已经是项目格式（带 `spot_filled`/`perp_filled`）→ 逐条校验后原样通过；
      B. 按 venue 分行的读数（现货一行、永续一行）→ 按 base 合并成"一标的一条"。
    """
    base_map = base_map or {}
    rows, how = _find_rows(raw)
    # 🔴 `data.list: null` 是官方对"空"的写法 —— **明确为空**（结论），
    #    不是"认不出结构"（缺数据）。两者混起来就是本模块第三条红线。
    if _explicit_empty(raw) and not _rows_look_real(rows):
        return [], {"status": "empty",
                    "why": "交易所明确返回空（`list: null`）—— "
                           "**这是结论，不是缺数据**",
                    "how": how, "schema_verified": SCHEMA_VERIFIED_WITH_REAL_OUTPUT}
    if rows is None:
        return [], {"status": "unavailable", "why": how,
                    "hint": "把这份 JSON 的顶层结构贴出来，一次性校准字段名即可"}
    if not rows:
        return [], {"status": "empty", "why": "返回的就是一个**空列表**（这是结论）",
                    "how": how, "schema_verified": SCHEMA_VERIFIED_WITH_REAL_OUTPUT}

    # ---- A. 项目既有格式：直接通过（下游契约不变）----
    def has_leg_flags(r):
        return any(k in r for k in ("spot_filled", "perp_filled"))

    if any(isinstance(r, dict) and has_leg_flags(r) for r in rows):
        out = []
        for r in rows:
            if not isinstance(r, dict) or not r.get("base"):
                continue
            out.append({"base": str(r["base"]).upper(),
                        "qty_usd": abs(_as_float(r.get("qty_usd")) or 0.0),
                        "spot_filled": bool(r.get("spot_filled")),
                        "perp_filled": bool(r.get("perp_filled")),
                        "synthetic": bool(r.get("synthetic", False))})
        return out, {"status": "ok" if out else "empty",
                     "why": "项目既有格式（%d 条）" % len(out), "how": how,
                     "schema_verified": SCHEMA_VERIFIED_WITH_REAL_OUTPUT}

    # ---- B. 按 venue 分行的读数 → 按 base 合并 ----
    legs, bad, unresolved = [], [], []
    used = {"base_keys": set(), "symbol_keys": set(), "size_keys": set(), "venue_keys": set()}
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            bad.append({"index": i, "why": "不是对象", "raw_type": type(r).__name__})
            continue
        size, size_k = _pick(r, SIZE_KEYS)
        venue, venue_k = _pick(r, VENUE_KEYS)
        base, base_k = _pick(r, BASE_KEYS)
        sym, sym_k = _pick(r, SYMBOL_KEYS)
        if base:
            used["base_keys"].add(base_k)
        elif sym:
            used["symbol_keys"].add(sym_k)
            base = base_map.get(str(sym).upper())
            if not base:
                # 🔴 不猜：符号表里没有就报出来，等一次校准
                unresolved.append({"index": i, "symbol": str(sym).upper(),
                                   "keys": sorted(r.keys())[:12]})
                continue
        if not base or size is None or _as_float(size) is None:
            bad.append({"index": i, "why": "缺 base 或缺可解析的规模（**不填 0**）",
                        "base": base, "symbol": sym, "size_key": size_k,
                        "size_raw": size, "keys": sorted(r.keys())[:12]})
            continue
        used["size_keys"].add(size_k)
        if venue_k:
            used["venue_keys"].add(venue_k)
        legs.append({"base": str(base).upper(), "qty_usd": abs(_as_float(size)),
                     "venue": str(venue).lower() if venue else None})

    if not legs and not unresolved:
        return [], {"status": "unavailable",
                    "why": "所有 %d 行都认不出来 —— 字段名可能不叫这些" % len(bad),
                    "rows": bad[:8], "how": how,
                    "hint": "把上面 rows 的 keys 发出来，校准 BASE_KEYS/SIZE_KEYS/VENUE_KEYS 一次即可"}

    by_base = {}
    for lg in legs:
        e = by_base.setdefault(lg["base"], {"base": lg["base"], "qty_usd": 0.0,
                                            "spot_filled": False, "perp_filled": False,
                                            "synthetic": False})
        e["qty_usd"] = max(e["qty_usd"], lg["qty_usd"])
        v = lg["venue"] or ""
        if "perp" in v or "future" in v or "swap" in v or "contract" in v:
            e["perp_filled"] = True
        elif "spot" in v or "cash" in v:
            e["spot_filled"] = True
        else:
            # 没有 venue 标记 → **不能说它是哪条腿**（这正是"两条腿"判定的前提）
            e["venue_unknown"] = True
    out = list(by_base.values())
    unknown = [e["base"] for e in out if e.get("venue_unknown")]

    rep = {"status": "ok" if out else "unavailable",
           "why": "%d 行 → %d 个标的" % (len(legs), len(out)),
           "how": how,
           "base_keys_used": sorted(used["base_keys"]),
           "symbol_keys_used": sorted(used["symbol_keys"]),
           "size_keys_used": sorted(used["size_keys"]),
           "venue_keys_used": sorted(used["venue_keys"]),
           "schema_verified": SCHEMA_VERIFIED_WITH_REAL_OUTPUT,
           "unmapped_rows": bad[:8]}
    if unresolved:
        # 🔴 关键：这类行**没有任何 base**，绝不能落成一条持仓
        rep["unresolved_symbols"] = [u["symbol"] for u in unresolved]
        rep["unresolved_rows"] = unresolved[:8]
        rep["warn_unresolved"] = (
            "%d 行只有 symbol、缺 base。**没有符号表就不猜**（现货 rToken 带 R 前缀，"
            "剥错会凭空多出一个标的）。加 `--base-map`（可直接用项目一的 "
            "data/universe.csv）即可一次解决：%s"
            % (len(unresolved), "、".join(rep["unresolved_symbols"][:6])))
    if unknown:
        rep["venue_unknown"] = unknown
        rep["warn_venue"] = ("这些标的的行没带 venue，**不能说它属于哪条腿**："
                             + "、".join(unknown))
    return out, rep


def ingest(path, write=False, base_map_path=None):
    """读一个**只读**查询的返回文件并规范化。返回 (ok, 报告 dict)。"""
    if not os.path.exists(path):
        return False, {"error": "文件不存在：%s" % path}
    try:
        with io.open(path, encoding="utf-8-sig") as fh:      # 容忍 BOM
            raw = json.load(fh)
    except (OSError, ValueError) as exc:
        return False, {"error": "解析失败：%s: %s" % (type(exc).__name__, exc)}

    base_map, merr = load_base_map(base_map_path)
    pos, rep = normalize_positions(raw, base_map=base_map)
    if base_map_path:
        rep["base_map"] = {"path": base_map_path, "entries": len(base_map), "error": merr}
    with io.open(path, "rb") as fh:
        inp_sha = hashlib.sha256(fh.read()).hexdigest()[:16]
    rep["input"] = path
    rep["input_sha256_16"] = inp_sha
    rep["count"] = len(pos)

    if rep.get("status") != "ok" or not pos:
        rep["wrote"] = False
        return False, rep

    if write:
        os.makedirs(os.path.dirname(POS_FILE), exist_ok=True)
        payload = {"_README": ("由 project2/account_feed.py --ingest 规范化；"
                               "来源=交易所只读查询（不是手写）"),
                   "_ingested_utc": dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
                   "_input_sha256_16": inp_sha,
                   "_cred_fingerprint": cred_fingerprint(),
                   "positions": pos}
        with io.open(POS_FILE, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
        # ⚠️ 跨盘符（D: 仓库 / C: 临时目录）时 relpath 会抛 ValueError —— 踩过。
        try:
            rep["wrote"] = os.path.relpath(POS_FILE, BASE)
        except ValueError:
            rep["wrote"] = os.path.abspath(POS_FILE)
    else:
        rep["wrote"] = False
    return True, rep


def load_positions(source=None):
    """读仓位。**返回 (positions, 状态)**，状态是 ``ok`` / ``empty`` / ``unavailable``。

    🔴 三者绝不能混：
      · ``ok``          读到了 N 条
      · ``empty``       读到了、而且确实是空的（**这是结论**）
      · ``unavailable`` 读不到（**这是缺数据，不是"没有仓位"**）
    """
    src, note, authed = detect_source() if source is None else (source, "", False)
    if src == "agentic":
        # 取数**已经**实现，但落在 tools/account_read.py（职责不重叠）。
        # 本模块只负责"规范化"那一段 —— 所以这里如实说"去哪个工具取"，
        # 而不是假装自己读到了。
        return [], {"status": "unavailable", "source": "agentic",
                    "why": "Agentic 已授权：取数在 `python tools/account_read.py --read`"
                           "（它每次新起一个 MCP 进程 —— 长驻进程不会热加载新凭证）。"
                           "取完再 `--ingest data/account/positions_raw.json --base-map <符号表>`"
                           "规范化（合并逻辑已实现并自检）。",
                    "note": note, "authorized": True}
    if src == "none":
        return [], {"status": "unavailable", "source": "none",
                    "why": "没有配置任何仓位来源：" + note, "authorized": False}
    # 文件源（当前实际在用）
    try:
        raw = None
        for enc in ("utf-8-sig", "utf-8", "gbk"):   # 容忍 BOM（用户手写，踩过 3 次）
            try:
                with io.open(POS_FILE, encoding=enc) as fh:
                    raw = json.load(fh)
                break
            except json.JSONDecodeError:
                continue
        if raw is None:
            return [], {"status": "unavailable", "source": "file",
                        "why": "持仓单存在但解析不了（三种编码都试过）",
                        "note": note, "authorized": False}
        pos = raw.get("positions") or []
        if not pos:
            return [], {"status": "empty", "source": "file",
                        "why": "持仓单存在且**明确是空的**（这是结论，不是缺数据）",
                        "note": note, "authorized": False}
        src_note = raw.get("_README") or "手写"
        return pos, {"status": "ok", "source": "file",
                     "why": "读到 %d 条在途持仓（来源：%s）" % (len(pos), src_note),
                     "note": note, "authorized": False}
    except OSError as exc:
        return [], {"status": "unavailable", "source": "file",
                    "why": "读持仓单失败：%s" % exc, "note": note,
                    "authorized": False}


def summary(pos, st):
    """给页面/接口用的一句话摘要 + 该不该显示成"安全"。"""
    if st["status"] == "unavailable":
        # 🔴 关键：读不到时**不许**显示 0 仓位 —— 那是把缺数据说成安全
        return {"present": False, "count": None,
                "headline": "仓位**读不到**（不是『没有仓位』）",
                "why": st.get("why"), "source": st.get("source")}
    return {"present": bool(pos), "count": len(pos),
            "headline": ("%d 条在途持仓" % len(pos)) if pos else "没有在途持仓（已确认）",
            "why": st.get("why"), "source": st.get("source")}


def _print_report(rep):
    print("   状态  ：%s —— %s" % (rep.get("status"), rep.get("why") or rep.get("error")))
    print("   条数  ：%s" % rep.get("count", 0))
    if rep.get("input_sha256_16"):
        print("   输入哈希：%s" % rep["input_sha256_16"])
    if rep.get("base_map"):
        bm = rep["base_map"]
        print("   符号表：%s（%d 条%s）" % (bm["path"], bm["entries"],
                                          ("；" + bm["error"]) if bm.get("error") else ""))
    for key, label in (("size_keys_used", "规模取自"), ("base_keys_used", "base 取自"),
                       ("symbol_keys_used", "symbol 取自"), ("venue_keys_used", "venue 取自")):
        if rep.get(key):
            print("   %s：%s" % (label, "、".join(rep[key])))
    for key in ("warn_unresolved", "warn_venue"):
        if rep.get(key):
            print("   ⚠️ %s" % rep[key])
    for b in (rep.get("rows") or rep.get("unmapped_rows") or [])[:5]:
        print("   · 认不出第 %s 行：%s ｜ keys=%s"
              % (b.get("index"), b.get("why"), b.get("keys") or b.get("raw_type")))
    if rep.get("wrote"):
        print("   ✅ 已落盘：%s" % rep["wrote"])
    elif rep.get("status") == "ok":
        print("   （未落盘；加 --write 才会写 %s）" % _rel(POS_FILE))
    elif rep.get("status") == "empty":
        print("   （明确为空 —— **不落盘**：写一份空持仓单，会让下游从『读不到』"
              "变成『已确认空仓』，那是你该拍板的决定，不是 --ingest 顺手做的）")
    if rep.get("status") == "ok" and not rep.get("schema_verified"):
        print("   ⚠️ 字段名**尚未用真实返回校准**"
              "（SCHEMA_VERIFIED_WITH_REAL_OUTPUT=False）—— 认错请把 keys 发出来")


def main(argv=None):
    ap = argparse.ArgumentParser(description="🏦 账户/仓位接入层（只读）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--plan", action="store_true",
                    help="打印授权后该跑的**只读**命令（本模块不执行任何命令）")
    ap.add_argument("--ingest", default=None, metavar="FILE",
                    help="把一个只读查询的返回 JSON 规范化成项目持仓单格式")
    ap.add_argument("--base-map", default=None, metavar="FILE",
                    help="符号→base 映射表（可直接用项目一的 data/universe.csv）")
    ap.add_argument("--write", action="store_true",
                    help="配合 --ingest：落盘到 data/positions/open.json（默认只打印）")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)

    if a.selftest:
        return selftest()

    if a.plan:
        for ln in plan_lines():
            print(ln)
        print("")
        print("⚠️ 本模块**不执行**上面任何命令 —— 只读边界写在 %s。"
              % os.path.relpath(os.path.abspath(__file__), BASE))
        return 0

    if a.ingest:
        ok, rep = ingest(a.ingest, write=a.write, base_map_path=a.base_map)
        if a.json:
            print(json.dumps(rep, ensure_ascii=False, indent=1))
        else:
            print("📥 规范化：%s" % a.ingest)
            _print_report(rep)
        return 0 if ok else 1

    src, note, authed = detect_source()
    pos, st = load_positions(src)
    out = {"source": src, "authorized": authed, "note": note,
           "status": st["status"], "why": st.get("why"),
           "cred_fingerprint": cred_fingerprint(),
           "summary": summary(pos, st)}
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        print("🏦 仓位来源：%s（已授权=%s）" % (src, authed))
        print("   状态    ：%s —— %s" % (st["status"], st.get("why")))
        print("   摘要    ：%s" % out["summary"]["headline"])
        if out["cred_fingerprint"]:
            print("   凭据指纹：%s（**只有指纹，绝不打印凭据本身**）"
                  % out["cred_fingerprint"])
        else:
            print("   凭据指纹：无（Agentic 路线走 OAuth，不依赖 API key）")
        if src != "agentic":
            print("   下一步  ：`python project2/account_feed.py --plan` 看授权后该跑什么")
    return 0 if st["status"] in ("ok", "empty") else 1


def selftest():
    """离线自检：**只读边界**、三态语义、脱敏、规范化（含"认不出不猜"）。"""
    ok = True

    def chk(cond, msg):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s" % ("OK " if cond else "!! ", msg))

    # ① 🔴 只读边界：允许列表里**不能**出现任何写操作
    bad = [c for c in READ_ONLY_COMMANDS
           if any(f in c.lower() for f in FORBIDDEN)]
    chk(not bad, "只读命令白名单里没有任何写操作（%s）" % "、".join(READ_ONLY_COMMANDS))

    # ② 全仓核验：本文件里不能**执行**外部命令
    #    ⚠️ 判据用"导入/调用形态"，而且**拼出来**再比 —— 直接写字面量会命中本自检
    #       自己的这一行（踩过：扫描器把自己的断言文本当成违规）。
    try:
        with io.open(os.path.abspath(__file__), encoding="utf-8") as fh:
            src = fh.read()
        needles = ("import " + "subprocess", "from " + "subprocess",
                   "os" + ".system(", "Po" + "pen(", "check_" + "output(")
        hit = [n for n in needles if n in src]
        chk(not hit, "本模块**不执行任何外部命令**（命中 %d 个可疑形态）" % len(hit))
        chk(not any(("client." + f) in src or ("api/v2/" + f) in src for f in FORBIDDEN),
            "本文件里没有真实写调用")
    except OSError:
        chk(False, "读不到自身源码（无法核验只读边界）")

    # ③ --plan 打出来的**只能是查询类**命令，且指向**真正的**取数工具
    txt = "\n".join(plan_lines())
    chk("tools/account_read.py --read" in txt,
        "--plan 指向真正的取数工具（tools/account_read.py --read，只读）")
    low = txt.lower()
    verbs = [f for f in ("place", "cancel", "withdraw") if f in low]
    chk(not verbs, "--plan 里不出现任何写动作字样（%s）" % ("、".join(verbs) or "无"))
    chk("BITGET_API_KEY" not in txt,
        "--plan 里**不出现**手动 Key 路线的环境变量名（本机 bgc 是那种，别照抄）")
    chk("--write" in txt and "默认只打印" in txt, "--plan 写清『--write 才落盘』")

    # ④ 三态语义：unavailable **绝不能**被显示成 0 仓位
    s_bad = summary([], {"status": "unavailable", "why": "未授权"})
    chk(s_bad["count"] is None and "读不到" in s_bad["headline"],
        "unavailable -> count=None 且写『读不到』：%s" % s_bad["headline"])
    s_ok = summary([{"id": "p1"}], {"status": "ok", "why": ""})
    chk(s_ok["present"] and s_ok["count"] == 1, "ok -> present=True 且 count 正确")
    s_e = summary([], {"status": "empty", "why": ""})
    chk(s_e["present"] is False and s_e["count"] == 0 and "已确认" in s_e["headline"],
        "empty -> 明确是**结论**（『已确认』），与 unavailable 区分")

    # ⑤ 脱敏：指纹不可逆、且不等于凭据本身
    os.environ["__P2_TEST_KEY"] = "super-secret-value"
    fp = cred_fingerprint("__P2_TEST_KEY")
    chk(fp and len(fp) == 8 and "super" not in fp,
        "凭据指纹 = 8 位哈希（%s），**不含明文**" % fp)
    del os.environ["__P2_TEST_KEY"]
    chk(cred_fingerprint("__P2_NOT_SET__") is None, "没有凭据 -> None（不编一个）")

    # ⑥ 规范化（带 base 字段）：按 venue 分行的读数 → 合并成"一标的一条"
    sample = {"code": "00000", "data": {"list": [
        {"base": "HOOD", "venue": "spot", "available": "5000.0"},
        {"base": "HOOD", "venue": "perp", "available": "5000.0"},
        {"base": "NVDA", "venue": "spot", "available": "3000"},
    ]}}
    pos, rep = normalize_positions(sample)
    by = {p["base"]: p for p in pos}
    chk(rep["status"] == "ok" and len(pos) == 2, "规范化：3 行 → 2 个标的（%s）" % rep["why"])
    chk(by.get("HOOD", {}).get("spot_filled") and by.get("HOOD", {}).get("perp_filled"),
        "HOOD 两条腿都被认出来 -> 对冲完好")
    chk(by.get("NVDA", {}).get("spot_filled") and not by["NVDA"]["perp_filled"],
        "NVDA 只有现货腿 -> spot_filled=True / perp_filled=False（**裸多敞口**）")
    chk(rep.get("size_keys_used") == ["available"],
        "报告写清规模取自哪个键（%s）" % rep.get("size_keys_used"))

    # ⑦ 🔴 只有 symbol、没有符号表 -> **不猜 base**，原样报出来
    sym_only = {"data": {"list": [{"symbol": "RHOODUSDT", "venue": "spot",
                                  "available": "5000"}]}}
    pos_u, rep_u = normalize_positions(sym_only)
    chk(not pos_u and rep_u.get("unresolved_symbols") == ["RHOODUSDT"],
        "只有 symbol 且无符号表 -> **不产出持仓**，把符号报出来（%s）"
        % rep_u.get("unresolved_symbols"))
    chk("RHOODUSDT" in (rep_u.get("warn_unresolved") or "")
        and "凭空多出一个标的" in (rep_u.get("warn_unresolved") or ""),
        "并说明为什么不能剥 R 前缀（会凭空多出一个标的）")

    # ⑧ 给了符号表 -> 正确地归到同一个 base（这是 R 前缀的正确解法）
    bm = {"RHOODUSDT": "HOOD", "HOODUSDT": "HOOD", "RNVDAUSDT": "NVDA"}
    pos_m, rep_m = normalize_positions(sym_only, base_map=bm)
    chk(rep_m["status"] == "ok" and pos_m[0]["base"] == "HOOD"
        and pos_m[0]["spot_filled"] is True,
        "带符号表 -> RHOODUSDT 正确归到 HOOD（%s）" % [p["base"] for p in pos_m])

    # ⑨ 符号表能直接吃**项目一**的 universe.csv（列 base/spot_symbol/perp_symbol）
    import tempfile
    tmpd = tempfile.mkdtemp(prefix="acct_feed_self_")
    try:
        csvp = os.path.join(tmpd, "universe.csv")
        with io.open(csvp, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["base", "perp_symbol", "spot_symbol", "has_spot"])
            w.writerow(["HOOD", "HOODUSDT", "RHOODUSDT", "True"])
            w.writerow(["NVDA", "NVDAUSDT", "RNVDAUSDT", "True"])
        m, err = load_base_map(csvp)
        chk(err is None and m.get("RHOODUSDT") == "HOOD" and m.get("HOODUSDT") == "HOOD",
            "符号表支持项目一 universe.csv 的列名（%d 条）" % len(m))
        # 认不出的符号表要如实报错，不静默返回空
        badp = os.path.join(tmpd, "bad.csv")
        with io.open(badp, "w", encoding="utf-8", newline="") as fh:
            fh.write("foo,bar\n1,2\n")
        m2, err2 = load_base_map(badp)
        chk(err2 and not m2, "符号表缺 base 列 -> 明确报错（%s）" % (err2 or "")[:40])
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)

    # ⑩ 🔴 缺规模的行**不许填 0**（填 0 = 把"读不到"说成"没有敞口"）
    pos2, rep2 = normalize_positions({"data": {"list": [
        {"base": "HOOD", "venue": "perp"}]}})
    chk(not pos2 and rep2["status"] == "unavailable",
        "缺规模 -> unavailable，且**不产出半条持仓**（不填 0）")
    chk(rep2.get("rows") and "不填 0" in rep2["rows"][0]["why"],
        "报告点明「缺规模且不填 0」，并给出 keys 供一次校准")

    # ⑪ 认不出的结构 / 认不出的字段名 -> 如实说认不出
    pos3, rep3 = normalize_positions({"foo": 1})
    chk(not pos3 and rep3["status"] == "unavailable" and "认不出" in rep3["why"],
        "结构认不出 -> unavailable 并说明（%s）" % rep3["why"])
    pos4, rep4 = normalize_positions({"data": {"list": [{"weird": 1}]}})
    chk(not pos4 and rep4["status"] == "unavailable" and rep4.get("rows"),
        "字段名认不出 -> unavailable 并回传 keys 供校准")

    # ⑫ 已经是项目格式 -> 原样通过（下游契约不变，synthetic 标记保留）
    pos5, rep5 = normalize_positions({"positions": [
        {"id": "demo", "base": "NVDA", "qty_usd": 5000, "spot_filled": True,
         "perp_filled": False, "synthetic": True}]})
    chk(rep5["status"] == "ok" and pos5[0]["base"] == "NVDA"
        and pos5[0]["synthetic"] is True,
        "项目既有格式直接通过，`synthetic` 保留（合成演示单不会被当成真单）")

    # ⑬ `--write` 落盘：**在临时目录里跑**，不碰仓库的 data/positions/
    #    （改 POS_FILE 前先存原值，跑完无论成败都还原）
    global POS_FILE
    _saved_pos = POS_FILE
    tmpd2 = tempfile.mkdtemp(prefix="acct_feed_write_")
    try:
        srcp = os.path.join(tmpd2, "positions.json")
        with io.open(srcp, "w", encoding="utf-8") as fh:
            json.dump({"data": {"list": [
                {"base": "HOOD", "venue": "spot", "available": "5000"},
                {"base": "HOOD", "venue": "perp", "available": "5000"}]}}, fh)
        POS_FILE = os.path.join(tmpd2, "out", "open.json")
        okw, repw = ingest(srcp, write=True)
        chk(okw and repw.get("wrote"), "`--write` 落盘成功（%s）" % repw.get("wrote"))
        written = json.load(io.open(POS_FILE, encoding="utf-8"))
        chk(written.get("positions") and written["positions"][0]["base"] == "HOOD"
            and written["positions"][0]["spot_filled"]
            and written["positions"][0]["perp_filled"],
            "落盘内容符合项目既有格式，且两条腿标记正确")
        chk("_input_sha256_16" in written and "_cred_fingerprint" in written
            and "_ingested_utc" in written,
            "落盘带溯源字段（输入哈希 / 凭据指纹 / 时间），**不含任何密钥**")
        okd, repd = ingest(srcp, write=False)
        chk(okd and not repd.get("wrote"), "默认（不带 --write）**只打印不落盘**")
    finally:
        POS_FILE = _saved_pos
        shutil.rmtree(tmpd2, ignore_errors=True)

    # ⑭ 来源探测：本机现在的真实情况要能如实说出来
    src, note, authed = detect_source()
    chk(src in ("agentic", "file", "none") and isinstance(note, str),
        "来源探测可用：%s（已授权=%s）｜ %s" % (src, authed, note[:52]))
    if src == "agentic":
        pos6, st6 = load_positions(src)
        chk(st6["status"] == "unavailable" and "tools/account_read.py" in st6["why"],
            "已授权 -> 如实指向取数工具（**不假装自己读到了**）")

    # ⑮ 🔴 认得**实测确认**的凭证路径（此前只认 .bgc/auth.json，会把已授权的机器
    #    误报成"未授权" —— 这是 2026-09-22 修掉的真实缺陷）
    paths = cred_files()
    chk(any(p.endswith(os.path.join(".bitget", "oauth_token.json")) for p in paths),
        "认得 ~/.bitget/oauth_token.json（实测确认的落盘路径）")
    chk(all(isinstance(_rel(p), str) and len(_rel(p)) > 3 for p in paths),
        "跨盘符取相对路径不抛异常（D: 仓库 / C: 用户目录）—— **踩过**")

    # ⑯ 🔴 官方"空"的写法是 `list: null`，不是 `[]`
    #    （tools/account_read.py 落盘的 positions_raw.json 就是这个形状）
    pos7, rep7 = normalize_positions({"ok": True, "data": {"ok": True, "data": {"list": None}}})
    chk(not pos7 and rep7["status"] == "empty",
        "`list: null` -> **empty（这是结论）**，不是 unavailable（缺数据）")
    pos8, rep8 = normalize_positions({"data": {"list": []}})
    chk(not pos8 and rep8["status"] == "empty", "空列表 [] -> 同样是 empty")
    pos9, rep9 = normalize_positions({"data": {"positions": {"data": {"list": None}}}})
    chk(rep9["status"] == "empty", "再嵌一层的 null 也认得出来")
    tmpd3 = tempfile.mkdtemp(prefix="acct_feed_empty_")
    try:
        ep = os.path.join(tmpd3, "positions_raw.json")
        with io.open(ep, "w", encoding="utf-8") as fh:
            json.dump({"ok": True, "data": {"ok": True, "data": {"list": None}}}, fh)
        okE, repE = ingest(ep, write=True)
        chk(okE is False and repE["status"] == "empty" and not repE.get("wrote"),
            "**明确为空**不落盘（写一个空持仓单会让下游从『读不到』变成『已确认空仓』——"
            "那是个决定，不该由 --ingest 顺手做掉）")
        chk(repE.get("count") == 0, "报告里条数是 0（读到了 0 条，不是读不到）")
    finally:
        shutil.rmtree(tmpd3, ignore_errors=True)

    print("\n账户接入层自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
