#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据快照清单（生成 + **就地核验**）
====================================

为什么需要这个工具（这是一个真实踩过的坑）：

快照原本由项目一的 `tools/export_p2_snapshot.py` 生成 `data/SNAPSHOT.md`，
里面给**每个文件**都记了 SHA256。但本项目里有三类文件的哈希**注定会变**：

| 类别 | 例子 | 为什么哈希会变 |
|---|---|---|
| `frozen` 冻结快照 | `data/spread/orderbook-*.csv` | **不该变**。变了就是快照被改过，必须报警 |
| `derived` 本地派生物 | `data/derived/rag_index.json` | 索引是在**本仓库**上重建的（文档集/案例集与项目一不同），哈希必然与上游那张表不符 |
| `runtime` 运行期可变 | `data/derived/news_state.json`、`news_latest.json` | 采样/事件驱动**运行时会写**它们 |

如果三类混在一张表里用同一个哈希去核验，结果只有两种：要么**误报**（把正常运行
当成数据被篡改），要么**干脆没人核验**（因为反正对不上）。两种都是"清单撒谎"。

本工具把三类**分开列、分开验**：

* `frozen`：SHA256 必须逐字节一致，否则退出码 1；
* `derived`：记录哈希，但**核验方式是"能重建"** —— 工具本身只检查文件存在且非空，
  重建命令写在表里（`--rebuild-hint`）；
* `runtime`：只检查存在与非空，哈希如实列为"参考值（会变）"。

用法：
  python tools/snapshot_manifest.py            # 重新生成 data/SNAPSHOT.md
  python tools/snapshot_manifest.py --verify   # 就地核验（冻结项不一致 -> 退出码 1）
  python tools/snapshot_manifest.py --verify --json
"""

import argparse
import datetime as dt
import glob
import hashlib
import io
import json
import os
import sys

P2 = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, P2)
try:
    from common.console import install as _install_console
    _install_console()
except Exception:  # noqa: BLE001
    pass

BASE = P2
DATA = os.path.join(BASE, "data")
MANIFEST = os.path.join(DATA, "SNAPSHOT.md")

# 类别：frozen = 上游冻结快照（哈希必须一致）
#       derived = 本仓库可重建的派生物
#       runtime = 运行期会被写
FROZEN, DERIVED, RUNTIME = "frozen", "derived", "runtime"

# (相对路径或 glob, 类别, 处理方式, 用途, 重建/写入者)
SPEC = [
    # ---- 成本与成交：结论所系，全部来自项目一实测派生 ----
    ("data/derived/friction_budget.csv", FROZEN, "复制",
     "成本门槛与可捕获额（含 11.34 bp 门槛的输入）", "-"),
    ("data/derived/funding_rates.csv", FROZEN, "复制",
     "资金费：48h 窗口收入（门槛里扣掉的那一项）", "-"),
    ("data/derived/precise_fill_spot_bid.csv", FROZEN, "复制",
     "现货腿成交率与逆向选择（逐笔实测）", "-"),
    ("data/derived/precise_fill_perp_ask.csv", FROZEN, "复制",
     "永续腿同上", "-"),
    ("data/derived/joint_fill_all.csv", FROZEN, "复制",
     "双腿联合成交四格（全部窗口）", "-"),
    ("data/derived/joint_fill_all_in_house.csv", FROZEN, "复制",
     "同上，in_house 分层（模型实际取用）", "-"),
    ("data/derived/joint_fill_all_stockroute.csv", FROZEN, "复制",
     "同上，stockroute 分层", "-"),
    ("data/derived/joint_fill_check_in_house.csv", FROZEN, "复制",
     "新旧口径影响对照", "-"),
    ("data/derived/threshold_calibration.json", FROZEN, "复制",
     "僵持阈值敏感性分析结果（docs/36）",
     "python tools/threshold_calibration.py"),
    # ---- 盘口 / 成交 / 点差：交易所无历史接口，快照是唯一可分发的一份 ----
    # ⚠️ 盘口取**尾段**（最后 400 轮）而不是首段：这是被一次内部不一致逼出来的 ——
    #    上游导出脚本默认保留**前** N 轮，而 trades 保留的是**尾部** N 行，
    #    于是"最后一轮盘口"(09-18 19:19) 与"最后一笔成交"(09-19 07:16) 差了 12 小时。
    #    对一个卖点是"同一时刻的市场"的决策演示来说，这是硬伤。
    ("data/spread/orderbook-*.csv", FROZEN, "**尾段截断**（保留最后 400 轮）",
     "5 档盘口：容量/深度/首档约束（原文单日 37 MB）", "-"),
    ("data/spread/trades-*.csv", FROZEN, "",
     "逐笔成交：成交率/逆向选择/联合分布，以及『最后一笔成交距今』（停牌判据）。"
     "⚠️ 截断规则见 TRADES_NOTE —— **现货行一条都不能丢**", "-"),
    ("data/spread/2026-*.csv", FROZEN, "",
     "点差/中间价：定挂单价位与 route 对照", "-"),
    ("data/spread/sentiment-*.csv", FROZEN, "复制",
     "情绪采样：OI 与资金费率的实测值", "python tools/sentiment_sampler.py"),
    # ---- 本仓库派生物：哈希与上游那张表**本就不该一致** ----
    ("data/derived/rag_index.json", DERIVED, "**本仓库重建**",
     "RAG 索引（本仓库口径文档 + 本仓库决策案例）",
     "python common/rag_memory.py --build"),
    # ---- 运行期可变：如实列哈希，但不作为核验依据 ----
    ("data/derived/news_latest.json", RUNTIME, "运行期写入",
     "最近一次消息面抓取（事件闸门输入）",
     "python tools/news_sources.py --base NVDA --save"),
    ("data/derived/news_state.json", RUNTIME, "运行期写入",
     "**事件驱动**的已见清单（避免重复调 LLM）",
     "python tools/news_sources.py --event-driven --save"),
]

# 运行期/派生物里"预期存在但不是上游复制"的其它文件（存在才列）
EXTRA_GLOBS = [
    ("data/reports/debate-*.json", DERIVED,
     "本项目决策日志（RAG 的『决策案例』来源，`--log` 产生）",
     "python project2/agent_team.py --base NVDA --trader --log"),
    ("data/derived/event_driven_state.json", RUNTIME,
     "事件驱动闸门的判定缓存（复用上次 LLM 判断 + TTL）",
     "python project2/event_gate.py --selftest"),
]

TRADES_NOTE = {
    "trades-2026-09-12.csv": "复制（全量）",
    "trades-2026-09-13.csv": "复制（全量）",
    # 🔴 规则修订（2026-09-23，被一次**假局限**逼出来的）：
    #    原规则"只留尾部 8 万行"会把**现货行整段切掉** —— 现货成交稀少，
    #    且多发生在当日更早的时段（盘中 / 所内窗口前段），于是：
    #      · 09-14 原始 3445 笔现货 → 快照里 **0 笔**；
    #      · "最后一笔现货成交"被推早十几个小时 → stale_quotes / quote_frozen
    #        几乎必然触发，页面全站"不参与"；
    #      · 甚至让 docs/29 得出"现货腿自 09-14 起零成交"这个**错误的结论**。
    #    现行规则：**现货行全量保留**，永续行可截尾。
    "trades-2026-09-14.csv": "尾部 8 万行 + **现货行全量**（补回 3445 笔）",
    "trades-2026-09-19.csv": "尾部 8 万行 + **现货行全量**（补回 916 笔）",
    "trades-2026-09-20.csv": "现货全量（4206 笔）+ 永续尾部 2 万行",
    "trades-2026-09-21.csv": "现货全量（2946 笔）+ 永续尾部 2 万行",
    "trades-2026-09-22.csv": "现货全量（**该日 0 笔**）+ 永续尾部 2 万行",
}


def sha256_16(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def rows_of(path):
    """行数（CSV 减表头；json 记 0 —— 行数对 JSON 没有意义）。"""
    if path.endswith(".json"):
        return 0
    try:
        with io.open(path, encoding="utf-8", errors="replace") as fh:
            return max(0, sum(1 for _ in fh) - 1)
    except OSError:
        return 0


def collect():
    """按 SPEC 收集实际存在的文件 -> [{rel, cls, status, why, by, bytes, rows, sha}]"""
    out, seen = [], set()
    for pat, cls, status, why, by in SPEC:
        paths = sorted(glob.glob(os.path.join(BASE, pat.replace("/", os.sep))))
        if not paths:
            out.append({"rel": pat, "cls": cls, "status": "**缺失**",
                        "why": why, "by": by, "bytes": 0, "rows": 0,
                        "sha": None})
            continue
        for p in paths:
            rel = os.path.relpath(p, BASE).replace("\\", "/")
            if rel in seen:
                continue
            seen.add(rel)
            st = status or TRADES_NOTE.get(os.path.basename(rel), "复制")
            out.append({"rel": rel, "cls": cls, "status": st, "why": why,
                        "by": by, "bytes": os.path.getsize(p),
                        "rows": rows_of(p), "sha": sha256_16(p)})
    for pat, cls, why, by in EXTRA_GLOBS:
        for p in sorted(glob.glob(os.path.join(BASE, pat.replace("/", os.sep)))):
            rel = os.path.relpath(p, BASE).replace("\\", "/")
            if rel in seen:
                continue
            seen.add(rel)
            out.append({"rel": rel, "cls": cls, "status": "本项目生成",
                        "why": why, "by": by, "bytes": os.path.getsize(p),
                        "rows": rows_of(p), "sha": sha256_16(p)})
    out.sort(key=lambda r: (r["cls"] != FROZEN, r["rel"]))
    return out


CLS_TITLE = {
    FROZEN: "① 冻结快照（上游复制品）—— **SHA256 必须逐字节一致**",
    DERIVED: "② 本仓库派生物 —— 哈希与上游那张表**本就不该一致**，核验方式是『能重建』",
    RUNTIME: "③ 运行期可变 —— 采样/事件驱动会写它们，哈希只作参考，**不作为核验依据**",
}


def render(recs):
    now = dt.datetime.now(dt.UTC)
    frozen = [r for r in recs if r["cls"] == FROZEN]
    total = sum(r["bytes"] for r in recs)
    lines = [
        "# 项目二数据快照（只读引用项目一的采样结果）",
        "",
        "- 清单生成：**%s UTC**（本文件由 `tools/snapshot_manifest.py` 生成，"
        "可 `--verify` 就地核验）" % now.strftime("%Y-%m-%d %H:%M"),
        "- 上游导出脚本：`tools/export_p2_snapshot.py`（**项目一仓库内**，可重跑）",
        "- 合计：**%.1f MB**，%d 项（其中冻结快照 %d 项）"
        % (total / 1e6, len(recs), len(frozen)),
        "",
        "## ⚠️ 四条必须知道的边界",
        "",
        "1. **这是快照，不是完整数据集**：盘口按轮次截断（见下表『截断』行），",
        "   所以工具报的『最近一轮盘口』指的是**快照里的最后一轮**，不是『现在』。",
        "2. **盘口不可回补**：交易所不提供历史 bid/ask，项目一的原始数据只存在于",
        "   项目一那台机器上；本快照是**唯一**可分发的那一份。",
        "3. **只读**：项目二不修改①类文件；重新生成请回项目一跑上面的导出脚本，",
        "   然后在**本仓库**重跑 `python tools/snapshot_manifest.py` 刷新清单。",
        "4. **三类文件核验方式不同**（这是被坑过的地方）：混在一张表里用同一个哈希",
        "   去验，只会得到『误报』或『没人验』两种结果。分开列、分开验。",
        "",
        "## ⚠️ 盘口为什么是『**尾段** 400 轮』而不是首段",
        "",
        "上游导出脚本默认保留**前** N 轮盘口，而 trades 保留的是**尾部** N 行 ——",
        "两者拼在一起会出现内部不一致：『最后一轮盘口』停在 09-18 19:19，",
        "而『最后一笔成交』已经到了 09-19 07:16，**差了 12 小时**。",
        "对一个卖点是「同一时刻的市场」的决策演示，这是硬伤（腿风险/成交率/深度",
        "会来自两个不同时段的市场）。所以本仓库把盘口改成**保留最后 400 轮**，",
        "与 trades 的尾部对齐。命令：",
        "",
        "```powershell",
        "# ① 按上游脚本导出（盘口此时是『前 400 轮』）",
        "python tools/export_p2_snapshot.py --out <临时目录> --rounds 400 --trade-rows 80000",
        "# ② 把盘口重截为『最后 400 轮』（与 trades 的尾部对齐）",
        "python tools/retruncate_orderbook.py --src <项目一>\\data\\spread\\orderbook-2026-09-19.csv --rounds 400",
        "# ③ 刷新本清单（重算全部 SHA256 并重新分类）",
        "python tools/snapshot_manifest.py",
        "```",
        "",
        "> ② 的自检：`python tools/retruncate_orderbook.py --selftest`（合成样本，",
        "> 不碰真实数据）。它会验证『保留最后 N 轮』确实只留下最后 N 轮、",
        "> 表头保留、且**源文件未被改动**。",
        "> 想知道本仓库当前盘口的覆盖时段，直接读清单里的 SHA256 对应文件，",
        "> 或看 `--verify` 之后的输出。",
        "",
        "## 核验",
        "",
        "```powershell",
        "python tools/snapshot_manifest.py --verify    # 冻结项不一致 -> 退出码 1",
        "```",
        "",
        "## 文件清单",
        "",
    ]
    for cls in (FROZEN, DERIVED, RUNTIME):
        rs = [r for r in recs if r["cls"] == cls]
        if not rs:
            continue
        lines += ["### " + CLS_TITLE[cls], "",
                  "| 路径 | 处理 | 字节 | 行数 | SHA256(16) | 用途 | 重建/写入者 |",
                  "|---|---|---|---|---|---|---|"]
        for r in rs:
            # ⚠️ 运行期文件与本地派生物的哈希**只作参考**，不参与核验。
            #    直接印一个哈希出来，读者很容易误以为它是契约值 —— 明确标出来。
            #    标记放在反引号**外面**：`--verify` 是按 `` `hash` `` 找行的。
            sha = "`%s`" % r["sha"] if r["sha"] else "—"
            if cls == RUNTIME and r["sha"]:
                sha += " （参考值，运行期会变）"
            elif cls == DERIVED and r["sha"]:
                sha += " （重建后会变）"
            lines.append("| `%s` | %s | %s | %s | %s | %s | %s |"
                         % (r["rel"], r["status"], format(r["bytes"], ","),
                            format(r["rows"], ","), sha, r["why"], r["by"]))
        lines.append("")
    return "\n".join(lines) + "\n"


def verify(json_out=False):
    """核验：frozen 逐字节比对；derived/runtime 只查存在与非空。"""
    if not os.path.exists(MANIFEST):
        print("!! 清单不存在：%s（先跑 python tools/snapshot_manifest.py）"
              % os.path.relpath(MANIFEST, BASE))
        return 1
    with io.open(MANIFEST, encoding="utf-8") as fh:
        text = fh.read()

    problems, checked, drift = [], 0, []
    for r in collect():
        rel = r["rel"]
        if not os.path.exists(os.path.join(BASE, rel.replace("/", os.sep))):
            if r["cls"] == FROZEN:
                problems.append("冻结项缺失：%s" % rel)
            continue
        checked += 1
        line_ok = ("`%s`" % (r["sha"] or "")) in text
        if r["cls"] == FROZEN:
            if not line_ok:
                problems.append("冻结项哈希与清单不符：%s（现在 %s）"
                                % (rel, r["sha"]))
        elif not line_ok:
            drift.append("%s（现在 %s）" % (rel, r["sha"]))
    # 清单里列了、但磁盘上已经没有的**冻结**项
    for line in text.splitlines():
        if line.startswith("| `data/") and "**缺失**" in line:
            problems.append("清单里标记为缺失：%s" % line.split("|")[1].strip())

    if json_out:
        print(json.dumps({"ok": not problems, "checked": checked,
                          "problems": problems, "drift": drift},
                         ensure_ascii=False, indent=1))
    else:
        print("=" * 74)
        print("数据快照核验（frozen 逐字节 / derived·runtime 只查存在）")
        print("=" * 74)
        for p in problems:
            print("  [!! ] %s" % p)
        for d in drift:
            print("  [ ~ ] 本仓库派生物或运行期文件已更新（不算失败）：%s" % d)
        print("  核验 %d 项 ｜ 冻结项不符 %d 项 ｜ 派生物/运行期已更新 %d 项"
              % (checked, len(problems), len(drift)))
        print("\n数据快照核验%s" % ("通过" if not problems else "**失败**"))
    return 1 if problems else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="数据快照清单（生成 + 就地核验）")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.verify:
        return verify(args.json)

    recs = collect()
    with io.open(MANIFEST, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(render(recs))
    print("清单已生成：%s" % os.path.relpath(MANIFEST, BASE))
    print("  合计 %.1f MB / %d 项" % (sum(r["bytes"] for r in recs) / 1e6,
                                     len(recs)))
    for r in sorted(recs, key=lambda z: -z["bytes"])[:5]:
        print("    %-44s %8.2f MB  %s"
              % (r["rel"], r["bytes"] / 1e6, r["status"]))
    miss = [r["rel"] for r in recs if r["status"] == "**缺失**"]
    if miss:
        print("  ⚠️ 缺失：%s" % "、".join(miss))
    return 0


if __name__ == "__main__":
    sys.exit(main())
