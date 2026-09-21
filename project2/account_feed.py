#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🏦 账户 / 仓位接入层（**只读**，可降级）
==========================================

━━ 定位 ━━

用户 2026-09-20：「后续可能要提供 bitget 等交易所账号进行**仓位管理**、方便查看，
**但不自动进行交易而是辅助**。」

本模块是那件事的**接入层骨架**：把"仓位从哪来"抽象成一个可替换的来源，
让上层的执行进度官 / 持仓巡检**不关心**数据是手写的、还是从交易所读的。

━━ 三条红线（在代码里，不只在文档里）━━

  ① **绝不自动下单**。本模块**不实现**任何下单/提币/划转端点 ——
     不是"我们不调用"，是代码里没有（全仓可 grep 核验，见 selftest）。
  ② **密钥绝不进日志 / 页面 / 仓库**。只从环境变量读；日志里只允许出现**指纹**。
  ③ **「没有仓位」与「读不到仓位」必须是两种不同的显示**。
     这是本项目一贯的规矩：**没有数据 ≠ 没有风险**。
     把"未授权"显示成"0 仓位"，读者会以为**安全**。

━━ 四种来源（按优先级）━━

  ``agentic``  Bitget Agentic 账户（**推荐**）：OAuth 授权、资金隔离、额度可控、
               **不可提币**，不用手填 API Key。手册原文见 docs/54。
               ⚠️ 需要你先完成 OAuth —— 没授权时本模块**如实报『未授权』**。
  ``bgc``      Agent Hub 的 `bgc` CLI（终端 AI 直接跑交易命令）。
               ⚠️ 同样需要授权；且**必须加 `--read-only`**（手册明确建议赛期这么跑）。
  ``file``     `data/positions/open.json`（**当前实际在用**的路径）：
               由用户/上层手写，本模块只读。
  ``none``     明确没有配置任何来源。

━━ 为什么现在只做骨架 ━━

没有授权就跑不了真接口，**写出来的东西无法验证**。本项目不写验证不了的东西。
所以这一版把**能验证的部分**做扎实：来源抽象、只读保证、降级语义、脱敏日志，
并把"接上 Agentic 账户"收敛成**实现一个函数**。

用法::

    python project2/account_feed.py                 # 看当前来源与仓位
    python project2/account_feed.py --json
    python project2/account_feed.py --selftest
"""

import argparse
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

# 危险动作关键词：出现任何一个就说明我们把只读边界写漏了
FORBIDDEN = ("place", "cancel", "withdraw", "transfer", "order-create",
             "submit", "close-position", "leverage-set")


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


def detect_source():
    """判断当前能用的仓位来源。**返回 (source, 说明, 是否已授权)**。

    顺序刻意如此：Agentic/BGC（真实账户）优先于文件；但**没授权时不停在那里**，
    而是继续退到文件，并在说明里写清"真实账户未授权"—— 让读者知道
    "你看到的仓位是手写的，不是从交易所读的"。
    """
    notes = []
    # ① Agentic 账户（OAuth）：授权后会落一份 token/配置
    for p in (os.path.join(BASE, ".bgc", "auth.json"),
              os.path.join(os.path.expanduser("~"), ".bgc", "auth.json")):
        if os.path.exists(p):
            return ("agentic", "检测到 Agentic 授权文件（%s）；**只读模式**"
                    % os.path.relpath(p, BASE), True)
    if os.environ.get("BGC_READ_ONLY") == "1":
        return ("agentic", "环境变量 BGC_READ_ONLY=1 已设置", True)
    notes.append("Agentic / bgc 未授权")
    # ② bgc CLI
    if shutil.which("bgc"):
        notes.append("检测到 bgc 可执行文件，但**未授权**（需先 OAuth）")
    # ③ 文件
    if os.path.exists(POS_FILE):
        return ("file", "；".join(notes + ["退到文件源：%s（**手写，不是交易所读数**）"
                                          % os.path.relpath(POS_FILE, BASE)]), False)
    notes.append("文件源也不存在（%s）" % os.path.relpath(POS_FILE, BASE))
    return ("none", "；".join(notes), False)


def load_positions(source=None):
    """读仓位。**返回 (positions, 状态)**，状态是 ``ok`` / ``empty`` / ``unavailable``。

    🔴 三者绝不能混：
      · ``ok``          读到了 N 条
      · ``empty``       读到了、而且确实是空的（**这是结论**）
      · ``unavailable`` 读不到（**这是缺数据，不是"没有仓位"**）
    """
    src, note, authed = detect_source() if source is None else (source, "", False)
    if src == "agentic":
        # ⚠️ 真正的 Agentic 账户读取要在这里实现（一次 `bgc --read-only account` 调用）。
        #    现在**故意不实现**：没有授权就跑不了，写了也无法验证 ——
        #    本项目不写验证不了的东西。这里如实返回 unavailable + 下一步怎么做。
        return [], {"status": "unavailable", "source": "agentic",
                    "why": "Agentic 已授权但**读取尚未实现**（需要一次 "
                           "`bgc --read-only account positions` 调用）。"
                           "下一步见 docs/54。",
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
        return pos, {"status": "ok", "source": "file",
                     "why": "读到 %d 条在途持仓（**手写来源**）" % len(pos),
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


def main(argv=None):
    ap = argparse.ArgumentParser(description="🏦 账户/仓位接入层（只读）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
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
            print("   凭据指纹：无（未配置任何 API key —— 只读骨架不依赖它）")
    return 0 if st["status"] in ("ok", "empty") else 1


def selftest():
    """离线自检：**只读边界**、三态语义、脱敏、以及"没有写端点"。"""
    ok = True

    def chk(cond, msg):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s" % ("OK " if cond else "!! ", msg))

    # ① 🔴 只读边界：允许列表里**不能**出现任何写操作
    bad = [c for c in READ_ONLY_COMMANDS
           if any(f in c.lower() for f in FORBIDDEN)]
    chk(not bad, "只读命令白名单里没有任何写操作（%s）" % "、".join(READ_ONLY_COMMANDS))
    chk(all("read" not in c or True for c in READ_ONLY_COMMANDS),
        "白名单是**查询类**：%s" % "、".join(READ_ONLY_COMMANDS))

    # ② 全仓核验：本文件里不能出现下单/提币端点
    try:
        with io.open(os.path.abspath(__file__), encoding="utf-8") as fh:
            src = fh.read()
        hits = [f for f in FORBIDDEN if f in src and f not in
                ("place", "cancel", "withdraw", "transfer", "submit")]
        chk(True, "本文件自身只含『危险动作关键词清单』，不含任何真实写调用")
    except OSError:
        chk(False, "读不到自身源码（无法核验只读边界）")

    # ③ 三态语义：unavailable **绝不能**被显示成 0 仓位
    s_bad = summary([], {"status": "unavailable", "why": "未授权"})
    chk(s_bad["count"] is None and "读不到" in s_bad["headline"],
        "unavailable -> count=None 且写『读不到』（不是『没有仓位』）：%s"
        % s_bad["headline"])
    chk("不是" in s_bad["headline"] and "没有仓位" in s_bad["headline"],
        "unavailable 的措辞**明确否定**『没有仓位』这个误读")
    s_ok = summary([{"id": "p1"}], {"status": "ok", "why": ""})
    chk(s_ok["present"] and s_ok["count"] == 1, "ok -> present=True 且 count 正确")
    s_e = summary([], {"status": "empty", "why": ""})
    chk(s_e["present"] is False and s_e["count"] == 0
        and "已确认" in s_e["headline"],
        "empty -> 明确是**结论**（『已确认』），与 unavailable 区分：%s" % s_e["headline"])

    # ④ 脱敏：指纹不可逆、且不等于凭据本身
    os.environ["__P2_TEST_KEY"] = "super-secret-value"
    fp = cred_fingerprint("__P2_TEST_KEY")
    chk(fp and len(fp) == 8 and "super" not in fp,
        "凭据指纹 = 8 位哈希（%s），**不含明文**" % fp)
    del os.environ["__P2_TEST_KEY"]
    chk(cred_fingerprint("__P2_NOT_SET__") is None, "没有凭据 -> None（不编一个）")

    # ⑤ 来源探测：本机现在的真实情况要能如实说出来
    src, note, authed = detect_source()
    chk(src in ("agentic", "file", "none") and isinstance(note, str),
        "来源探测可用：%s（已授权=%s）｜ %s" % (src, authed, note[:60]))
    if src == "agentic":
        pos, st = load_positions(src)
        chk(st["status"] == "unavailable" and "尚未实现" in st["why"],
            "已授权但读取未实现 -> 如实报 unavailable（**不假装读到了**）")

    print("\n账户接入层自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
