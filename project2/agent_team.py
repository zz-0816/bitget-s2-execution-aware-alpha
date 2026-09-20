# ⚠️ 冻结副本：本文件从项目一工作区（bitgetS2_factory_trading）复制而来，
#    复制日期 2026-09-19。项目二**只读使用**，请勿在此处反向修改项目一的逻辑；
#    若要同步上游修复，请回项目一改，然后重跑 tools/isolate_p2.py。
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
项目二 · 多 Agent 团队：分析师层（4 维度独立分析）
==================================================

设计依据：`docs/25-多Agent协作方案（可行性评估）.md`
（用户设想：参考证券分析团队分工 —— 分析师 → 研究员辩论 → 决策团队）

━━ 本文件只做第 ① 层：4 个分析师，输出**统一 schema** ━━

为什么"统一 schema"是第一步而不是先接 LLM：
辩论层要能**机械地**比较四个维度的结论，就必须先让它们说同一种话。
否则每个分析师各写一段散文，辩论层只能靠 LLM 去"读作文"，
那就退化成表演了。

统一 schema（每个分析师都必须返回）::

    {
      "dimension":  "basis" | "sentiment" | "news" | "technical",
      "verdict":    "favorable" | "unfavorable" | "neutral",
      "confidence": 0.0 ~ 1.0,
      "evidence":   [{"metric": str, "value": str, "source": str}, ...],
      "sources":    [str, ...],
      "notes":      str,
    }

━━ 🔴 铁律：**没有证据的结论一律作废** ━━

用户要求多 agent 提高**准确性**并**避免风险**。多 agent 最大的失败模式是
"把同一个判断换三个说法说出来" —— 看起来严谨，实际零增量。

所以本文件强制：**`evidence` 为空 -> `validate()` 直接判该分析师无效**，
且每条 evidence 必须带 `metric`（引用了哪个量）+ `value` + `source`。
"我觉得风险高"这种话在结构上就存不进来。

━━ 独立性 ━━
每个分析师**只拿自己那一路数据**，不允许读别人的结论。
这不是形式 —— 独立性是"四路输入"能提供增量的前提。

用法：
  python project2/agent_team.py --selftest
  python project2/agent_team.py --base NVDA
  python project2/agent_team.py --all
"""

import argparse
import collections
import csv
import datetime as dt
import glob
import hashlib
import json
import os
import re
import sys

P2 = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(P2)
sys.path.insert(0, BASE)
sys.path.insert(0, P2)
from common.console import install  # noqa: E402

install()

DERIVED = os.path.join(BASE, "data", "derived")
SPREAD = os.path.join(BASE, "data", "spread")

VERDICTS = ("favorable", "unfavorable", "neutral")
DIMENSIONS = ("basis", "sentiment", "news", "technical", "execution_risk",
              # ⚓ 2026-09-20：**只在接了外部锚时**才跑（第三方真实美股报价）
              "external_anchor",
              # ⭐ 2026-09-20：**只在有在途订单时**才跑（执行中闭环）
              "execution_progress")

# 执行风险假设 -> 风控官动作（**agent 提议、风控官执行**）
# agent 只说"该采取什么动作"，**最终立场与规模仍由风控官的规则决定**，
# 且仍然只能收紧 —— 这条边界见 RISK_LEVELS / decide() 的单调性检查。
HYPOTHESIS_ACTIONS = {
    "leg_risk": {"level": "caution", "action": "require_taker",
                 "measure": "P(只成交一腿) > 50%"},
    "stale_quotes": {"level": "veto", "action": "no_new_position",
                     "measure": "逐笔成交停滞 >= 30 分钟"},
    "thin_capacity": {"level": "caution", "action": "cap_size",
                      "measure": "可捕获名义额不足"},
    "adverse_selection": {"level": "veto", "action": "no_new_position",
                          "measure": "f_dmid <= -3.0 bp"},
    # ⭐ 2026-09-18 新增（用户确认）：停牌 / 报价冻结。
    # 依据不是猜的：SEC「创新豁免」明文要求
    # 「底层股票在主交易所停牌时，TSV 必须同时停止交易」（docs/32）。
    # 判据沿用 audit_samples.py 的口径：**价格跨度 == 0 = 报价完全冻结**。
    "quote_frozen": {"level": "veto", "action": "no_new_position",
                     "measure": "底层疑似停牌：现货中间价不动 **且** 窗口内零成交"},
    # ⭐ 2026-09-20 新增（执行进度官）：把链路从"只在下单前说话"补成**执行中闭环**。
    #    在此之前，挂单没成交、或者只成交了一条腿的时候，链路里**没有任何角色负责**
    #    —— 而这恰恰是赛道三「执行辅助」手册点名的位置（拆单与滑点管理）。
    #    两条判据都用**已实测的量**：成交状态（持仓单）+ 区间成交笔数（逐笔成交表）。
    "partial_fill_naked": {
        "level": "veto", "action": "no_new_position",
        "measure": "一腿已成交、另一腿超过宽限仍未成交 = 裸露敞口"},
    "progress_stalled": {
        "level": "caution", "action": "require_review",
        "measure": "挂单存活 >= 停滞阈值且期间该腿**零成交**（挂单不可能成交）"},
    # ⚓ 2026-09-20 新增（外部锚定分析师）：rToken 定价与真实美股脱节。
    #    判据只用**两个价**的偏离幅度，不含任何"偏离会怎么走"的假设
    #    —— 那会变成价格预测，触犯红线。
    "anchor_divergence": {
        "level": "caution", "action": "require_review",
        "measure": "|rToken 中价 − 真实美股中价| / 真实美股中价 > 阈值（第三方数据）"},
}


# ---------------------------------------------------------------- 通用

def ev(metric, value, source):
    """构造一条**可核验证据**。三个字段缺一不可。"""
    return {"metric": str(metric), "value": str(value), "source": str(source)}


def report(dim, verdict, confidence, evidence, notes="", sources=None):
    return {
        "dimension": dim,
        "verdict": verdict if verdict in VERDICTS else "neutral",
        "confidence": round(max(0.0, min(1.0, float(confidence))), 2),
        "evidence": [e for e in (evidence or []) if _ev_ok(e)],
        "sources": sorted(set(sources or [e["source"] for e in (evidence or [])
                                          if _ev_ok(e)])),
        "notes": notes,
    }


def _ev_ok(e):
    return (isinstance(e, dict) and e.get("metric") and e.get("value")
            and e.get("source"))


def validate(r):
    """🔴 铁律：没有证据的结论一律作废。

    返回 (ok: bool, why: str)。辩论层只会把 ok=True 的结论拿进去比。
    """
    if not isinstance(r, dict):
        return False, "不是 dict"
    if r.get("dimension") not in DIMENSIONS:
        return False, "dimension 非法：%r" % r.get("dimension")
    if r.get("verdict") not in VERDICTS:
        return False, "verdict 非法：%r" % r.get("verdict")
    if not r.get("evidence"):
        return False, "**没有引用任何已实测的量** —— 结论作废（防「换三个说法」）"
    if not r.get("sources"):
        return False, "没有可回溯来源"
    return True, ""


# ---------------------------------------------------------------- 读数据

def _read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _f(row, key, default=None):
    try:
        v = row.get(key)
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- ① 基本面

def analyst_basis(base, cost=None):
    """📊 价差基本面分析师 —— 只拿价格结构与成本数据。

    它回答：这个价差在**扣掉成本之后**还剩多少？
    """
    e = []
    ev_rows = {r.get("base"): r for r in _read_csv(os.path.join(DERIVED, "friction_budget.csv"))}
    r = ev_rows.get(base)
    if r:
        half_s = _f(r, "half_spread_spot_bp", 0.0)
        net_mm = _f(r, "net_all_maker_bp", 0.0)
        notional = _f(r, "capturable_notional_usd", 0.0)
        e.append(ev("现货半幅点差", "%.2f bp" % half_s, "data/derived/friction_budget.csv"))
        e.append(ev("往返净收益（全挂单）", "%+.2f bp" % net_mm,
                    "data/derived/friction_budget.csv"))
        e.append(ev("可捕获名义额", "$%s" % format(int(notional), ","),
                    "data/derived/friction_budget.csv"))

    f_rows = {x.get("base"): x for x in _read_csv(os.path.join(DERIVED, "funding_rates.csv"))}
    fr = f_rows.get(base)
    if fr:
        e.append(ev("48h 窗口资金费收入", "%+.3f bp" % _f(fr, "window_income_bp", 0.0),
                    "data/derived/funding_rates.csv"))

    if cost and isinstance(cost, dict) and "best_cost" in cost:
        e.append(ev("双腿最优执行成本", "%+.2f bp（%s）"
                    % (cost["best_cost"], cost.get("best_mode", "-")),
                    "project2/execution_cost.py"))

    if not e:
        return report("basis", "neutral", 0.0, [],
                      "缺少该标的的成本数据 —— 结构性无法判断（不是「中性」）")

    net = _f(r, "net_all_maker_bp", 0.0) if r else 0.0
    fund = _f(fr, "window_income_bp", 0.0) if fr else 0.0
    total = net + fund
    verdict = "favorable" if total > 3.0 else ("unfavorable" if total < 0 else "neutral")
    return report("basis", verdict, 0.8, e,
                  "净收益 + 资金费 = %+.2f bp" % total)


# ---------------------------------------------------------------- ② 情绪

def analyst_sentiment(base):
    """😱 情绪分析师 —— 持仓拥挤度。

    2026-09-18 升级：此前**只能用资金费当代理**（置信度压到 0.45）。
    现在读 `tools/sentiment_sampler.py` 采到的**实测拥挤度量**：
      OI 规模与变化（加仓/减仓）、当期资金费率、**历史百分位**、正费率占比。

    ⚠️ 仍然拿不到多空比：Bitget 公开 API 的
    `account-long-short` / `position-long-short` 返回 **400**，
    `taker-buy-sell-volume` / `elite-*` 返回 **404**（实测）。
    **不用别的量假装成多空比** —— 这条限制写在 notes 里。
    """
    e = []
    src = "data/spread/sentiment-*.csv"
    row = _latest_sentiment(base)
    fr = {x.get("base"): x for x in _read_csv(
        os.path.join(DERIVED, "funding_rates.csv"))}.get(base)
    if row:
        if row.get("oi_size") is not None:
            e.append(ev("合约持仓量 OI", "%.0f" % float(row["oi_size"]), src))
        if row.get("oi_delta_pct") not in (None, ""):
            e.append(ev("OI 变化（相对上一轮）", "%+.3f%%" % float(row["oi_delta_pct"]),
                        src))
        if row.get("funding_bp") not in (None, ""):
            e.append(ev("当期资金费率", "%+.3f bp" % float(row["funding_bp"]), src))
        if row.get("funding_pctile") not in (None, ""):
            e.append(ev("资金费率历史百分位", "%.0f%%" % float(row["funding_pctile"]), src))
        if row.get("funding_positive_share") not in (None, ""):
            e.append(ev("历史正费率占比", "%.1f%%" % float(row["funding_positive_share"]),
                        src))
    if fr:
        e.append(ev("48h 窗口资金费收入", "%+.3f bp" % _f(fr, "window_income_bp", 0.0),
                    "data/derived/funding_rates.csv"))
    if not e:
        return report("sentiment", "neutral", 0.0, [], "无情绪数据")

    # ---- 判据：都基于实测值 ----
    pos_share = _f(row, "funding_positive_share") if row else None
    pctile = _f(row, "funding_pctile") if row else None
    oi_delta = _f(row, "oi_delta_pct") if row else None
    verdict, conf, bits = "neutral", 0.45, []
    if pos_share is not None:
        bits.append("历史正费率占比 %.1f%%" % pos_share)
        if pos_share > 60:
            verdict, conf = "favorable", 0.6      # 多头常付费 -> 站空头一侧有利
        elif pos_share < 40:
            verdict, conf = "unfavorable", 0.6    # 空头常付费 -> 站空头一侧要付钱
    if pctile is not None:
        bits.append("当期费率处于历史 %.0f%% 分位" % pctile)
        if pctile >= 90 and verdict == "favorable":
            # 极度拥挤：短期可能反转，把结论压回中性 —— **这是风险提示，不是方向判断**
            verdict, conf = "neutral", 0.5
            bits.append("（分位 ≥90%：拥挤已极端，不给方向）")
    if oi_delta is not None:
        bits.append("OI 环比 %+.2f%%" % oi_delta)
    return report("sentiment", verdict, conf, e,
                  "实测拥挤度：" + " ｜ ".join(bits)
                  + "。⚠️ 多空比不可得（公开 API 400/404），未用其它量替代")


def _latest_sentiment(base):
    """取当天情绪采样里该标的**最后一行**。"""
    files = sorted(glob.glob(os.path.join(SPREAD, "sentiment-*.csv")))
    if not files:
        return None
    last = None
    try:
        with open(files[-1], newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r.get("base") == base:
                    last = r
    except OSError:
        return None
    return last


# ---------------------------------------------------------------- ③ 新闻

# 🔴 诚实标注：日历里**没有一条**带可回溯来源时，置信度封顶 0.40
CONF_CAP_NO_SOURCE = 0.4


def analyst_news(base, now_ms=None, mode="auto", headlines=None, auto_fetch=True,
                 assess_override=None):
    """📰 新闻/事件分析师 —— 这一段是**大模型在运行期的唯一职责**。

    ⚠️ 2026-09-18 修一个**实现缺口**：此前这里**硬编码 `mode="static"`**，
    于是 `event_gate.llm_gate()` 虽然写好了、**却从来没有在决策链里跑过**
    （日志里 `gate.source` 一直是 `static`）。而报告里写的是
    "LLM 在运行期的唯一职责 = 事件判断" —— 那就是**说了没做**。

    第二个缺口（同日再修）：LLM 跑起来后收到的是**空标题**，只能凭日历判断，
    理由是"未提供任何新闻标题"。现在 `auto_fetch=True` 时会从
    `tools/news_sources.py` 的落盘结果取**真实候选标题**（SEC 申报 / 官方 RSS /
    财报日历），且只认新鲜结果（默认 30 分钟内），**不拿旧闻当新闻**。

    按 `mode` 分三条路：
      * ``static``：只用确定性日历（**无 LLM 也能跑**，回退路径）
      * ``llm``   ：把标题喂给 OpenAI 兼容端点分类（需要 key）
      * ``auto``  ：有 key（含 `.env` 里的）就用 llm，否则 static ——
                    但会在 notes 里**明确写出"本次未使用 LLM"**，不含糊。

    ``assess_override``：**冻结的事件判定**（复跑用）。给了它就**完全不调 LLM**，
    直接拿这份判定继续 —— 因为 LLM 是活输入（有随机性、新闻每天在变），
    而硬闸门现在依赖它，不冻结就没法复跑。见 `run_decision(llm_event=...)`。

    ⭐ 返回的报告里额外带一个 ``event`` 字段（严重度 / 来源 / 理由 / 是否冻结），
    供 `run_decision` 与**硬闸门**合并 —— 这是 2026-09-19 修的第二处"说了没做"：
    在此之前 LLM 判出的 `block` **只到了辩论层**，硬闸门读的始终是确定性日历。
    """
    e = []
    note = ""
    sev, src, reason = None, None, ""
    _ev_probe = {}          # 事件判定里那几个"可复现"字段（prompt 指纹/缓存信息）
    frozen = assess_override is not None
    # ⚠️ `headlines=None` = 调用方没指定 -> 自动取消息面；
    #    `headlines=[]`   = 调用方**显式要求不带标题**（自检/复跑要冻结外部输入）-> 不取。
    #    初版写成 `if headlines is None and auto_fetch` 之后再判 `if headlines`，
    #    于是显式传 [] 也会去自动抓 —— 真 LLM 接上后**自检变得间歇性失败**（踩到）。
    if headlines is None and auto_fetch and not frozen:
        headlines, hsrc = _headlines_from_news(base)
        if headlines:
            note = "标题来源：%s。" % hsrc
        elif hsrc:
            note = "（%s）" % hsrc
    try:
        import event_gate as _eg
        try:
            if frozen:
                # 冻结判定是**小的 event 字典**（不是完整的 assess 结果）：
                # 它就是要进日志、要参与硬闸门的那几个字段，紧凑且可长期保存。
                # 这里把它包成 assess 的形状，让下游代码（notes/证据）不用分叉。
                fe = dict(assess_override or {})
                a = {
                    "event": {"in_window": (fe.get("severity") or "none") != "none",
                              "severity": fe.get("severity") or "none",
                              "reason": fe.get("reason") or "",
                              "source": fe.get("source") or "llm(frozen)",
                              "fail_closed": False,
                              "cache_reused": fe.get("cache_reused"),
                              "cache_age_min": fe.get("cache_age_min"),
                              "prompt_version": fe.get("prompt_version"),
                              "prompt_source": fe.get("prompt_source"),
                              "prompt_sha256": fe.get("prompt_sha256")},
                    "confidence": float(fe.get("confidence") or 0.4),
                    "sources": [], "rationale": [], "warnings": [], "conditions": {},
                    "event_driven": fe.get("event_driven") or {},
                    "llm": {"used": str(fe.get("source", "")).startswith("llm"),
                            "err": None, "fail_closed": False,
                            "prompt_version": fe.get("prompt_version")},
                    "rag_used": False,
                }
                note += ("🔒 **复用冻结的事件判定**（复跑/复现用，本次**不调 LLM**）："
                         "severity=%s。" % (fe.get("severity") or "none"))
            else:
                # ⚠️ `auto_headlines=False`：这一路的标题由**我们自己**管
                #    （上面已经抓过并做过新鲜度判断）。不显式关掉的话，
                #    `assess()` 会在"快照过期 -> 我们传 None"时**自己去抓一遍**，
                #    于是"冻结输入"的自检会间歇性失败（这条坑踩过）。
                a = _eg.assess(base, now_ms=now_ms, mode=mode,
                               headlines=headlines if headlines else None,
                               auto_headlines=False)
            sev = a["event"]["severity"]
            src = a["event"].get("source", "static")
            reason = a["event"].get("reason", "") or ""
            _ev_probe = a.get("event") or {}
            e.append(ev("事件严重度", sev, "project2/event_gate.py"))
            e.append(ev("判断置信度（受来源约束）", "%.2f" % a["confidence"],
                        "project2/event_gate.py"))
            e.append(ev("判断来源", src, "project2/event_gate.py"))
            if a["event"].get("reason"):
                e.append(ev("事件理由", a["event"]["reason"][:60],
                            "project2/event_gate.py"))
            if headlines:
                e.append(ev("交给 LLM 的候选标题", "%d 条" % len(headlines),
                            "data/derived/news_latest.json"))
            for line in _eg.calendar_quality():
                if "已复核" in line:
                    # ⚠️ 证据的 value 是**数据**，不是排版文本：`calendar_quality()`
                    #    那行里带 `**…**`（终端里当强调用），直接塞进 value 会在
                    #    页面上原样显示成字面星号（实测）。这里把标记去掉再当值。
                    e.append(ev("日历已复核条数",
                                line.strip().replace("**", "")[:60],
                                "project2/events_calendar.json"))
            used_llm = str(src).startswith("llm")
            n_tok = ((a.get("llm") or {}).get("llm_usage") or {})
            _ev = a.get("event") or {}
            _drv = a.get("event_driven") or {}
            if used_llm and _ev.get("cache_reused"):
                # ⭐ 复用缓存也必须**说清楚是复用**：别让日志看起来像刚调过 LLM。
                #    代价（最坏判定龄 = TTL）在 docs/42 §3 里写明了。
                note += ("♻️ 本次**复用上一次 LLM 判定**（来源=%s，判定龄 %.0f 分钟）："
                         "事件驱动闸门判定本轮**全是已见过的条目**（%s），"
                         "所以没有重复调用 LLM。"
                         "🔴 复用的是上一次判定的**原值**，没有因为『无新条目』降级；"
                         "最新申报由 EDGAR 立即触发路径兜住（新 8-K/10-Q/10-K 会强制重判）。"
                         % (src, a["event"].get("cache_age_min") or 0.0,
                            (_drv.get("reason") or "")[:60]))
            elif used_llm:
                note += ("✅ 本次由 **LLM** 判事件（来源=%s，tokens 输入 %s / 输出 %s）"
                         "—— 运行期职责已真实执行。"
                         "⚠️ 置信度被压到 %.2f 不是 LLM 的问题："
                         "日历里**没有一条带可回溯来源**，按 CONF_CAP_NO_SOURCE 封顶"
                         % (src, n_tok.get("prompt_tokens"),
                            n_tok.get("completion_tokens"), a["confidence"]))
            else:
                note += ("⚠️ **本次未使用 LLM**（来源=%s）："
                         "事件判断退化为确定性日历，只能挡可计算事件"
                         "（期权到期/休市），**挡不住突发新闻与财报**" % src)
            # prompt 可回溯：某次判断用的是哪一版 prompt（T4-A）
            if _ev.get("prompt_sha256"):
                note += ("｜prompt %s（source=%s, sha256=%s…）"
                         % (_ev.get("prompt_version"),
                            _ev.get("prompt_source"),
                            str(_ev.get("prompt_sha256"))[:16]))
            if _drv.get("reason"):
                note += "｜事件驱动：%s" % str(_drv.get("reason"))[:70]
        except Exception as exc:  # noqa: BLE001
            e.append(ev("闸门调用异常", repr(exc)[:60], "project2/event_gate.py"))
    except ImportError:
        e.append(ev("事件闸门", "模块不可用", "project2/event_gate.py"))

    if not e:
        return report("news", "neutral", 0.0, [], "无事件数据")
    verdict = "unfavorable" if sev == "block" else (
        "neutral" if sev == "caution" else "favorable")
    # 置信度：有可回溯来源（LLM 判断成功）才允许高；否则封顶 0.40
    conf = 0.85 if ("✅ 本次由" in note or "♻️" in note) else 0.4
    r = report("news", verdict, conf, e, note)
    # ⭐ 把**原始严重度**带出去：硬闸门要用它（不止辩论层）。
    #    之前只留了 verdict（favorable/neutral/unfavorable），硬闸门拿不到
    #    "这是 block 还是 caution"，于是 LLM 判出的 block 根本进不了硬闸门。
    #    这个字段同时是**日志里的 llm_event**（复跑时原样传回来冻结输入）。
    r["event"] = {"severity": (sev or "none"), "source": src or "static",
                  "reason": reason, "frozen": bool(frozen),
                  "confidence": conf,
                  # 下面几个只在真的调过 LLM 时有值；冻结复用会把它们带回来，
                  # 这样 notes 里的 prompt 指纹在复跑时也能如实重现。
                  "prompt_version": _ev_probe.get("prompt_version"),
                  "prompt_source": _ev_probe.get("prompt_source"),
                  "prompt_sha256": _ev_probe.get("prompt_sha256"),
                  "cache_reused": _ev_probe.get("cache_reused"),
                  "cache_age_min": _ev_probe.get("cache_age_min")}
    return r


# ---------------------------------------------------------------- ④ 技术面

def analyst_technical(base):
    """📈 技术面分析师 —— 只拿盘口形状与成交数据。

    **这一路是我们独有的**：盘口无历史接口，别人拿不到。
    """
    e = []
    rows = []
    files = sorted(glob.glob(os.path.join(SPREAD, "orderbook-*.csv")))[-1:]
    for p in files:
        try:
            with open(p, newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    if r.get("base") == base:
                        rows.append(r)
        except OSError:
            continue
    if rows:
        last = max(int(r["ts_ms"]) for r in rows)
        cur = [r for r in rows if int(r["ts_ms"]) == last]
        book = collections.defaultdict(dict)
        for r in cur:
            try:
                book[(r["venue"], r["side"])][int(r["level"])] = (
                    float(r["price"]), float(r["notional_usd"]))
            except (KeyError, ValueError, TypeError):
                continue
        # 计算"吃穿 5 档"的总深度与首档占比（形状指标）
        for (venue, side), lv in book.items():
            tot = sum(n for _p, n in lv.values())
            l1 = lv.get(1, (0, 0))[1]
            if tot > 0:
                e.append(ev("%s/%s 五档总深度" % (venue, side),
                            "$%s" % format(int(tot), ","), "data/spread/orderbook-*.csv"))
                e.append(ev("%s/%s 首档占比" % (venue, side),
                            "%.1f%%" % (100 * l1 / tot), "data/spread/orderbook-*.csv"))

    fp = {r.get("base"): r for r in _read_csv(
        os.path.join(DERIVED, "precise_fill_spot_bid.csv"))}.get(base)
    if fp:
        e.append(ev("现货腿成交率", "%.1f%%" % (100 * _f(fp, "fill_rate", 0.0)),
                    "data/derived/precise_fill_spot_bid.csv"))
        e.append(ev("现货腿逆向选择 f_dmid(k6)",
                    "%+.2f bp" % _f(fp, "fdmid_med_k6", 0.0),
                    "data/derived/precise_fill_spot_bid.csv"))

    if not e:
        return report("technical", "neutral", 0.0, [], "无盘口/成交数据")
    fill = _f(fp, "fill_rate", 0.0) if fp else 0.0
    adv = _f(fp, "fdmid_med_k6", 0.0) if fp else 0.0
    # 成交率过低或逆向选择过负 -> 不利
    if fill < 0.10 or adv < -3.0:
        verdict = "unfavorable"
    elif fill > 0.30 and adv > -1.0:
        verdict = "favorable"
    else:
        verdict = "neutral"
    return report("technical", verdict, 0.7, e,
                  "成交率 %.1f%% ｜ 逆向选择 %+.2f bp" % (100 * fill, adv))


# ------------------------------------------------- ⑤ 执行风险（agent 做的风险评估）

# 「行情停滞」判定：现货/永续**逐笔成交**多久没有新打印。
# 为什么必须单列：现货腿自 2026-09-14 起零成交（`docs/29`），而**报价仍在刷新** ——
# 报价动 ≠ 市场能成交。挂单策略的收益完全来自成交，所以"没有成交"是致命风险，
# 但在盘口数据上完全看不出来（点差、深度都正常）。这条只有把两边数据**联起来看**
# 才能发现 —— 这正是"多一个分析师"的价值，而不是多一层包装。
STALE_TRADE_MIN = 30.0      # 分钟
MAX_HYPOTHESES = 3          # 最多带进辩论/风控的假设条数（防止刷屏式围堵）
FROZEN_LOOKBACK = 20        # 停牌判定：最近 N 轮快照（20 轮 ≈ 10 分钟）
FROZEN_MAX_DISTINCT = 1     # 不同中间价个数 <= 它 = 报价不动（与 audit_samples.py 同口径）

# ---- 执行进度官（2026-09-20 新增）----
# 一腿成交后，给另一腿的**宽限**（分钟）。为什么是 1.0：
#   · 盘口采样节奏是 30 秒/轮 -> 2 轮 = 1 分钟，这是本判据的**分辨率下限**；
#   · 不是"统计最优值"，也不是从损益反推的 —— 本项目没有持有期损益口径，
#     所以这个数是**明说的策略选择**，不是实测值。要在材料里引用时请照此说明。
# ⚠️ 它与 STALE_TRADE_MIN 是**两个不同的问题**：
#     NAKED_GRACE_MIN 问"一腿成交后另一腿有没有及时跟上"，
#     STALE_TRADE_MIN 问"这条腿的市场还有没有成交"。
NAKED_GRACE_MIN = 1.0

# 决策基准时间：数据最新时刻比墙钟旧超过这么多分钟，就判定为"读冻结快照"，
# 决策改按**数据自带时刻**判定（并在输出里显式标注）。见 `time_basis()`。
ASOF_AUTO_AGE_MIN = 10.0


def halted_from(spot_mids, trades_in_window, lookback):
    """停牌判定（**纯函数**，便于正反两面自检）。

    返回 (是否停牌, 依据文本)。判据是**两条同时成立**：
      ① 底层（现货腿）中间价在窗口内完全不动；
      ② 窗口内**两个 venue 一笔成交都没有**。

    ⚠️ 为什么必须加 ②（这是一次实测踩坑后的修正）：
    初版只用了 ①（且只看永续腿、窗口 5 分钟），结果在**活市场**上误报。实测
    `data/spread/orderbook-2026-09-19.csv`：
      · 永续腿中间价连续 22 轮（11.0 分钟）完全不变，但窗口内**有 8 笔永续成交**；
      · 现货腿中间价连续 78 轮（39.0 分钟）完全不变，但窗口内**有 11 笔现货成交**。
    也就是说，"报价不动"在盘前是**常态**（做市商报价粘住），完全不代表市场停了。
    真停牌的签名是"报价不动 **且** 没有任何成交" —— 成交是市场还活着最直接的证据。
    判"停牌"却拿不出停牌证据，等于风险 agent 在**编造事实**，比不判更糟。

    再看方向性：真停牌时判不出来（漏报）会让挂单进一个停住的市场；但把活市场判成
    停牌（误报）会**无理由否决全部交易**。两个错都不可接受，所以判据必须**两边都有证据**。
    """
    if not spot_mids or len(spot_mids) < 2:
        return None, "现货中间价轮次不足"
    distinct = len(set(spot_mids))
    span = max(spot_mids) - min(spot_mids)
    frozen = distinct <= FROZEN_MAX_DISTINCT
    detail = ("最近 %d 轮现货中间价：不同值 %d 个 ｜ 跨度 %.8f ｜ 区间 [%.4f, %.4f]"
              % (len(spot_mids), distinct, span, min(spot_mids), max(spot_mids)))
    if not frozen:
        return False, detail + " ｜ 报价在动 -> 未停牌"
    if trades_in_window:
        return False, (detail + " ｜ 但窗口内仍有 %d 笔成交 -> 只是报价粘住，**不是停牌**"
                       % trades_in_window)
    return True, (detail + " ｜ 且窗口内零成交（两个 venue 都没有）-> 疑似停牌/死报价")


def _trades_between(base, lo_ms, hi_ms):
    """统计 `[lo_ms, hi_ms]` 区间内该标的的成交笔数（两个 venue 合计）。"""
    n = 0
    for p in sorted(glob.glob(os.path.join(SPREAD, "trades-*.csv"))):
        try:
            with open(p, newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    if r.get("base") != base:
                        continue
                    try:
                        t = int(r["ts_ms"])
                    except (KeyError, ValueError, TypeError):
                        continue
                    if lo_ms <= t <= hi_ms:
                        n += 1
        except OSError:
            continue
    return n


def _tail_last_ts_ms(path):
    """只读文件**末尾**，取最后一行里最大的 `ts_ms`（不整读 9 MB 的文件）。

    CSV 每行都有 `ts_ms` 且在文件里基本按时间递增，所以尾巴足够。
    读不到返回 None（**不猜**）。
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 65536))
            chunk = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    lines = [ln for ln in chunk.splitlines() if ln.strip()]
    # 第一行可能是被截断的半行，且可能有表头 -> 逐行 try，取得到就继续
    best = None
    for ln in lines[1:]:
        parts = ln.rsplit(",", 1)
        f = ln.split(",")
        if len(f) < 2:
            continue
        try:
            t = int(f[1])          # 约定：第 2 列 = ts_ms
        except (ValueError, IndexError):
            continue
        if best is None or t > best:
            best = t
    return best


def data_asof_ms(kinds=("trades", "orderbook", "sentiment")):
    """数据快照的**最后一刻**（各数据源最后一行 `ts_ms` 的最大值，毫秒）。

    ⚠️ 为什么需要它（一次实测踩坑）：
      离线演示读的是**冻结快照**，而 `analyst_execution_risk()` 默认拿**墙钟 now**
      去算"最后一笔成交距今多久"。于是每过一小时，"行情停滞"就越容易触发 ——
      实测快照最后成交停在 2026-09-19 07:16 UTC，13:53 跑的时候
      `stale_quotes = 399.3 分钟`（阈值 30）**触发一票否决**，演示结论恒为"不参与"；
      而按快照自带的 07:16 判定，停滞只有 **1.7 分钟**，不触发。
      也就是说：**同一份快照在不同时刻会跑出不同结论**（不可复现），
      而且演示会随时间越来越悲观。这不是市场的问题，是"用错了现在几点"。

    返回 (asof_ms, 来源说明)。一个都读不到就返回 (None, 原因) —— **不猜**。
    """
    best, src = None, []
    for k in kinds:
        cands = sorted(glob.glob(os.path.join(SPREAD, "%s-*.csv" % k)))
        if not cands:
            continue
        t = _tail_last_ts_ms(cands[-1])
        if t is None:
            continue
        src.append("%s:%s" % (k, dt.datetime.fromtimestamp(
            t / 1000, dt.UTC).strftime("%m-%d %H:%M")))
        if best is None or t > best:
            best = t
    if best is None:
        return None, "读不到任何数据源的 ts_ms"
    return best, " ｜ ".join(src)


# ------------------------------------------------------------------ 📉 数据新鲜度官

# 实时模式下，输入落后超过它就算"输入停了"。
DATA_FRESH_MIN = 30.0

# 每个输入源 → 怎么取它的"最后一刻"
_FRESH_SOURCES = (
    ("trades", "成交", "trades-*.csv"),
    ("orderbook", "盘口档位", "orderbook-*.csv"),
    ("sentiment", "情绪/资金费", "sentiment-*.csv"),
    ("quote", "报价（点差）", "2*.csv"),
)


def data_freshness(now_ms=None, basis=None):
    """📉 **数据新鲜度官** —— 每个输入源各自落后多少，以及**这算不算危险**。

    ━━ 为什么必须有它 ━━

    实测（2026-09-20）：本仓库快照的最后一笔数据停在 **09-19 07:16 UTC**，
    而墙钟已经是 09-20 16:08 —— **滞后 33 小时**。链路照常输出结论，
    页面上只有一行小字写着快照时刻，没有任何地方说"它有多旧"。

    ━━ 关键：必须区分两种"旧"，否则这个判据会变成恒红 ━━

      · ``basis=asof``（读冻结快照）→ 旧是**声明过的**离线模式，不是故障。
        如实标注即可，**不报警**（报警会让演示永远红，等于没有信号）。
      · ``basis=wallclock``（实时模式）→ 你以为在实时决策，但输入可能早就停了
        （采样器挂了 / 代理断了）。这时**旧数据比没有数据更危险**：
        "行情停滞""报价冻结"这类按龄判据会全部失真，而结论看起来很正常。
        实测踩到过：采样器 09-18 停了 4 小时，链路照常给结论。

    所以 ``verdict`` 三态：``ok`` / ``declared_offline``（标注即可）/ ``stale``（危险）。

    返回的每一项都带**来源**（哪个文件、哪一刻），没有猜测值。
    """
    wall = int(dt.datetime.now(dt.UTC).timestamp() * 1000)
    now = int(now_ms) if now_ms else wall
    basis = basis or "unknown"
    declared = basis in ("asof", "explicit")
    note_clock = []
    srcs = []
    for name, label, pat in _FRESH_SOURCES:
        cands = sorted(glob.glob(os.path.join(SPREAD, pat)))
        if not cands:
            continue
        t = _tail_last_ts_ms(cands[-1])
        if t is None:
            continue
        age = (now - t) / 60000.0
        item = {"name": name, "label": label,
                "file": os.path.basename(cands[-1]),
                "clock": "data", "ref": "decision_clock",
                "ts_utc": dt.datetime.fromtimestamp(t / 1000, dt.UTC)
                          .strftime("%Y-%m-%dT%H:%M:%SZ")}
        if age < 0:
            # 行情比"决策基准时刻"还新 —— 说明两者**不在一个钟上**。
            # 不许显示成"滞后 -1970 分钟"（那是负数，读者只会更糊涂）。
            item.update({"age_min": 0.0, "ahead": True})
            note_clock.append("%s 早于基准 %.1f 分钟" % (label, -age))
        else:
            item["age_min"] = round(age, 1)
        srcs.append(item)
    # 消息面：天生是"本机抓取的现在"，**不属于行情时钟**。
    # ⚠️ 实测踩到：离线模式下（基准=09-19 07:27）新闻是 09-20 16:18 抓的，
    #    按基准算就是 **-1970 分钟**。根因不是新闻有问题，而是**两个钟**：
    #    行情按数据自带时刻、新闻按墙钟。所以它必须用**自己的参照系**算龄，
    #    并且要把"这次决策的输入跨了两个钟"这件事**明说**出来。
    try:
        with open(os.path.join(BASE, "data", "derived", "news_latest.json"),
                  encoding="utf-8") as fh:
            probed = (json.load(fh) or {}).get("probed_at")
        if probed:
            pt = dt.datetime.fromisoformat(probed.replace("Z", "+00:00"))
            if pt.tzinfo is None:
                pt = pt.replace(tzinfo=dt.UTC)
            srcs.append({"name": "news", "label": "消息面",
                         "file": "data/derived/news_latest.json",
                         "clock": "live", "ref": "wallclock",
                         "ts_utc": pt.astimezone(dt.UTC)
                                   .strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "age_min": round((wall - pt.timestamp() * 1000) / 60000.0, 1)})
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass

    if not srcs:
        return {"now_ms": now, "basis": basis, "mode": "unknown",
                "sources": [], "oldest": None, "oldest_age_min": None,
                "verdict": "unknown", "threshold_min": DATA_FRESH_MIN,
                "why": "读不到任何输入源的时间戳 —— **不猜**，也不假装新鲜"}

    # 「行情有多旧」只由**行情源**决定：新闻是实时的，把它算进"最旧"会掩盖真问题。
    market = [s for s in srcs if s.get("clock") == "data"] or srcs
    oldest = max(market, key=lambda s: s["age_min"])
    news = next((s for s in srcs if s.get("clock") == "live"), None)
    clock_split = bool(declared and news is not None)
    fresh_min = max(0.0, min(s["age_min"] for s in market))
    if declared:
        verdict = "declared_offline"
        why = ("本次按**数据自带时刻**判定（basis=%s）：数据旧是声明过的离线模式，"
               "不是故障 —— 如实标注，不当作告警。最新行情 %s（%s），最旧 %s（%.1f 分钟）"
               % (basis, oldest["file"],
                  dt.datetime.fromtimestamp(
                      (now - fresh_min * 60000) / 1000, dt.UTC)
                  .strftime("%m-%d %H:%M"), oldest["label"], oldest["age_min"]))
    elif oldest["age_min"] > DATA_FRESH_MIN:
        verdict = "stale"
        why = ("**实时模式但输入已停**：最旧的行情输入是「%s」（%s，滞后 %.1f 分钟 > 阈值 %.0f）"
               "—— 旧数据比没有数据更危险，按龄判据（行情停滞/报价冻结）会失真"
               % (oldest["label"], oldest["file"], oldest["age_min"], DATA_FRESH_MIN))
    else:
        verdict = "ok"
        why = ("行情输入新鲜：最旧的是「%s」，滞后 %.1f 分钟 ≤ 阈值 %.0f"
               % (oldest["label"], oldest["age_min"], DATA_FRESH_MIN))
    if clock_split:
        why += ("　⚠️ 本次决策的输入**跨了两个时钟**：行情按数据自带时刻（%s），"
                "消息面是本机抓取的现在（%s，滞后 %.1f 分钟）—— "
                "事件闸门读的是**实时标题**，而盘口是快照。"
                % (dt.datetime.fromtimestamp(now / 1000, dt.UTC)
                   .strftime("%m-%d %H:%M"),
                   news["ts_utc"][:16].replace("T", " "), news["age_min"]))
    if note_clock:
        why += "　⚠️ 时钟异常：" + "；".join(note_clock)
    return {"now_ms": now, "basis": basis, "mode": mode_of(declared),
            "sources": srcs, "oldest": oldest["name"],
            "oldest_age_min": oldest["age_min"],
            "oldest_file": oldest["file"],
            "news_age_min": (news or {}).get("age_min"),
            "clock_split": clock_split,
            "threshold_min": DATA_FRESH_MIN,
            "verdict": verdict, "why": why}


def mode_of(declared):
    return "offline_declared" if declared else "live"


def time_basis(now_ms=None, force=None):
    """决定"这次决策的『现在』是几点"，并**如实说明依据**。

    返回 dict：
      · ``now_ms``       本次使用的基准时刻
      · ``basis``        ``asof``（按数据自带时刻）或 ``wallclock``（按墙钟）
      · ``data_asof_ms`` 数据快照最后一刻（读不到为 None）
      · ``data_age_min`` 数据比墙钟旧多少分钟
      · ``why``          一句话依据（会进日志与页面，**不许含糊**）

    规则：
      · 显式传了 ``now_ms`` -> 直接用，basis=explicit（测试与复跑要能钉死时刻）；
      · ``force="asof"`` / ``"wallclock"`` -> 强制（给页面/CLI 留一个开关）；
      · 否则 **auto**：数据比墙钟旧超过 `ASOF_AUTO_AGE_MIN` 分钟 = 在读冻结快照
        -> 按数据自带时刻判定；数据是新鲜的 = 实时模式 -> 用墙钟。
    """
    wall = int(dt.datetime.now(dt.UTC).timestamp() * 1000)
    asof, src = data_asof_ms()
    age_min = ((wall - asof) / 60000.0) if asof else None

    if now_ms is not None:
        return {"now_ms": int(now_ms), "basis": "explicit",
                "data_asof_ms": asof, "data_age_min": age_min,
                "why": "调用方显式指定了决策时刻（测试/复跑要能钉死）"}
    if force == "asof":
        return {"now_ms": asof if asof else wall, "basis": "asof",
                "data_asof_ms": asof, "data_age_min": age_min,
                "why": "调用方强制按数据自带时刻判定（%s）" % src}
    if force == "wallclock":
        return {"now_ms": wall, "basis": "wallclock",
                "data_asof_ms": asof, "data_age_min": age_min,
                "why": "调用方强制按墙钟判定"}
    if asof is None:
        return {"now_ms": wall, "basis": "wallclock", "data_asof_ms": None,
                "data_age_min": None,
                "why": "读不到数据时刻，退回墙钟（%s）" % src}
    if age_min is not None and age_min > ASOF_AUTO_AGE_MIN:
        return {"now_ms": asof, "basis": "asof", "data_asof_ms": asof,
                "data_age_min": age_min,
                "why": ("数据比墙钟旧 %.1f 分钟（> %.0f）-> 判定为『读冻结快照』，"
                        "按数据自带时刻 %s 判定；否则时间衰减规则会把冻结数据"
                        "误判成『行情停滞』"
                        % (age_min, ASOF_AUTO_AGE_MIN,
                           dt.datetime.fromtimestamp(asof / 1000, dt.UTC)
                           .strftime("%Y-%m-%d %H:%M:%S UTC")))}
    return {"now_ms": wall, "basis": "wallclock", "data_asof_ms": asof,
            "data_age_min": age_min,
            "why": "数据是新鲜的（旧 %.1f 分钟 <= %.0f）-> 按墙钟判定"
                   % (age_min, ASOF_AUTO_AGE_MIN)}


def frozen_quote(base, lookback=FROZEN_LOOKBACK):
    """底层是否**停牌/报价冻结** —— 返回 (是否停牌, 说明, 明细)。

    为什么需要它（不是凑维度）：SEC「创新豁免」明文要求
    「底层股票在主交易所停牌时，TSV 必须同时停止交易」（`docs/32`）。
    停牌期间**挂单挂着也不会成交**，而且一旦复牌价格可能跳空 ——
    这是"报价看起来正常、但市场已经停了"的情形，与"行情停滞"不同：
      · 行情停滞：成交不动（`stale_quotes` 负责）
      · 报价冻结：底层报价也不动了 + 一笔成交都没有（本函数负责）

    ⚠️ 判据用**现货腿**（底层），不是永续腿：豁免条款的触发条件是**底层股票**停牌。
    永续腿自己的报价异常属于"行情停滞"，由 `stale_quotes` 负责，两者不重复。
    """
    files = sorted(glob.glob(os.path.join(SPREAD, "orderbook-*.csv")))
    if not files:
        return None, "无盘口数据", []
    rows = []
    try:
        with open(files[-1], newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r.get("base") != base or r.get("level") != "1":
                    continue
                if r.get("side") not in ("bid", "ask"):
                    continue
                try:
                    rows.append((int(r["ts_ms"]), r["venue"], r["side"],
                                 float(r["price"])))
                except (KeyError, ValueError, TypeError):
                    continue
    except OSError:
        return None, "盘口文件读不到", []
    if not rows:
        return None, "该标的无最新盘口", []
    rows.sort()
    # 按时间戳把最新 lookback 轮分组
    stamps = sorted({t for t, _v, _s, _p in rows}, reverse=True)[:lookback]
    if len(stamps) < 2:
        return None, "盘口轮次不足（%d 轮）" % len(stamps), []
    by = collections.defaultdict(dict)
    for t, v, s, p in rows:
        if t in stamps:
            by[t][(v, s)] = p
    mids = []
    for t in sorted(stamps):
        b = by[t].get(("spot", "bid"))
        a = by[t].get(("spot", "ask"))
        if b and a:
            mids.append(round((a + b) / 2.0, 8))
    if len(mids) < 2:
        return None, "可用现货中间价轮次不足", mids
    n_tr = _trades_between(base, min(stamps), max(stamps))
    frozen, detail = halted_from(mids, n_tr, lookback)
    return frozen, detail, mids


def _trades_between_venue(venue, base, lo_ms, hi_ms):
    """统计 [lo, hi] 区间内**某个 venue** 该标的的成交笔数。

    ⚠️ 与上面那个 `_trades_between(base, lo, hi)`（**两个 venue 合计**，
    用于停牌判据）是**两个不同的问题**：执行进度官要问的是
    "**我挂单的那条腿**在这段时间里有没有成交"，所以必须按腿分开数。
    两个函数都在，别混用。
    """
    files = sorted(glob.glob(os.path.join(SPREAD, "trades-*.csv")))
    n = 0
    for p in reversed(files):          # 从最新一天往回找
        try:
            with open(p, newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    if r.get("venue") != venue or r.get("base") != base:
                        continue
                    try:
                        t = int(r["ts_ms"])
                    except (KeyError, ValueError, TypeError):
                        continue
                    if lo_ms <= t <= hi_ms:
                        n += 1
        except OSError:
            continue
    return n


def _last_trade_ts(venue, base):
    """该 venue 下该标的**最后一笔**成交的时间戳（毫秒）。读不到返回 None。"""
    files = sorted(glob.glob(os.path.join(SPREAD, "trades-*.csv")))
    for p in reversed(files):
        last = None
        try:
            with open(p, newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    if r.get("venue") != venue or r.get("base") != base:
                        continue
                    try:
                        last = int(r["ts_ms"])
                    except (KeyError, ValueError, TypeError):
                        continue
        except OSError:
            continue
        if last is not None:
            return last
    return None


def _headlines_from_news(base, max_age_min=30, max_items=12):
    """从 `tools/news_sources.py` 的落盘结果里取**交给 LLM 的候选标题**。

    ⚠️ 这一步补的是**闭环缺口**：初版 LLM 确实跑了，但 `assess()` 收到的是**空标题**，
    于是它只能凭日历判断，理由写着"未提供任何新闻标题，无事件信息可判断" ——
    LLM 在跑、却没有输入，等于白跑（实测踩到）。

    取用规则（避免"旧闻当新闻"，也避免凭空造标题）：
      · 只认 ≤ `max_age_min` 分钟内抓到的结果；过期就返回 None，让 LLM 按"无标题"判
      · 优先用 `fresh_headlines`（事件驱动那批），退回 `headlines_for_gate`
    """
    p = os.path.join(DERIVED, "news_latest.json")
    if not os.path.exists(p):
        return None, "无消息面落盘（先跑 python tools\\news_sources.py --save）"
    try:
        with open(p, encoding="utf-8-sig") as fh:
            d = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        return None, "消息面落盘不可读：%s" % str(exc)[:50]
    try:
        t = dt.datetime.fromisoformat(d.get("probed_at", ""))
        age = (dt.datetime.now(dt.UTC) - t).total_seconds() / 60.0
    except (TypeError, ValueError):
        age = 9e9
    if age > max_age_min:
        return None, ("消息面已过期 %.1f 分钟（阈值 %d 分钟）—— 不拿旧闻当新闻"
                      % (age, max_age_min))
    heads = d.get("fresh_headlines") or d.get("headlines_for_gate") or []
    heads = [h for h in heads[:max_items] if h]
    if not heads:
        return None, "消息面里没有候选标题（当日无重大申报/事件）"
    return heads, ("来自 data/derived/news_latest.json（%.1f 分钟前抓取，%d 条）"
                   % (age, len(heads)))


def analyst_execution_risk(base, cost=None, now_ms=None, size_usd=None):
    """🛡️ **执行风险分析师** —— agent 做的风险评估层。

    与其它四个分析师的差别：它不回答"哪一路数据怎么样"，而是回答
    **"这一单可能怎么死"**，并且每一条风险都必须给全四样东西：

      ``hypothesis`` 风险陈述 ｜ ``metric/value`` 实测量（引用了哪个、值多少）
      ｜ ``threshold`` 验证阈值 ｜ ``falsifier`` **什么条件下这条风险不成立**

    只有四样齐全的假设才会被带进辩论层与风控层；缺证的直接丢弃并留痕
    （与 `docs/25` §2.1 的"可证伪"铁律同源）。

    目前覆盖五类（全部来自实测踩过的坑或已核实的规则依据）：
      1. **单腿裸露**：P(只成交一腿) 高 -> 只成交一边 = 裸露方向敞口
      2. **行情停滞**：逐笔成交长时间无新打印 -> 挂单不会成交（报价在动也没用）
      3. **容量约束**：可捕获名义额 / 首档深度 -> 规模撑不住
      4. **逆向选择**：现货腿 f_dmid 负向加深 -> 挂单被系统性挑选
      5. **停牌/报价冻结**（2026-09-18 新增）：报价跨度归零 -> 可能已停牌；
         依据是 SEC「创新豁免」明文要求"底层股票停牌时 TSV 必须同时停止交易"
         （`docs/32`），判据沿用 `audit_samples.py` 的"价格跨度 == 0 判死"口径
    """
    now_ms = now_ms or int(dt.datetime.now(dt.UTC).timestamp() * 1000)
    e, hyps, dropped = [], [], []

    def H(hid, text, metric, value, threshold, falsifier, action):
        return {"id": hid, "hypothesis": text, "metric": metric, "value": value,
                "threshold": threshold, "falsifier": falsifier, "action": action}

    # ---- ① 单腿裸露（来自实测联合分布）----
    j = None
    try:
        try:
            from execution_cost import load_joint_fill
        except ImportError:
            from project2.execution_cost import load_joint_fill
        route = (cost or {}).get("route")
        j = (load_joint_fill(route).get(base) or load_joint_fill(None).get(base))
    except Exception:  # noqa: BLE001
        j = None
    if j:
        e.append(ev("P(只成交一腿)", "%.1f%%" % (100 * j["p_part"]),
                    j["prov"]))
        e.append(ev("P(两腿都成交)", "%.2f%%" % (100 * j["p_both"]), j["prov"]))
        if j["p_part"] > 0.5:
            hyps.append(H(
                "leg_risk", "只成交一腿的概率超过一半 —— 会留下裸露的方向敞口",
                "P(只成交一腿)", "%.1f%%" % (100 * j["p_part"]),
                ">50%", "若 P(只成交一腿) 降到 50% 以内，本条不成立",
                "禁止挂单类方案，只允许双腿全吃单"))
        else:
            dropped.append({"id": "leg_risk", "reason": "未超过阈值",
                            "metric": "P(只成交一腿)",
                            "value": "%.1f%%" % (100 * j["p_part"])})
    # ---- ② 行情停滞（联表看：报价在动 ≠ 能成交）----
    stale = {}
    for venue in ("spot", "perp"):
        ts = _last_trade_ts(venue, base)
        if ts:
            stale[venue] = (now_ms - ts) / 60000.0
    if stale:
        worst = max(stale, key=lambda v: stale[v])
        e.append(ev("%s 最后一笔成交距今" % worst, "%.1f 分钟" % stale[worst],
                    "data/spread/trades-*.csv"))
        e.append(ev("双边停滞对照",
                    "现货 %.1f 分钟 ｜ 永续 %.1f 分钟"
                    % (stale.get("spot", 0.0), stale.get("perp", 0.0)),
                    "data/spread/trades-*.csv"))
        if stale[worst] >= STALE_TRADE_MIN:
            hyps.append(H(
                "stale_quotes",
                "%s 腿已 %.0f 分钟没有成交 —— **报价在动但市场没有成交**，"
                "挂单不会成交（策略收益来自成交，不是来自报价）"
                % ({"spot": "现货", "perp": "永续"}[worst], stale[worst]),
                "%s 最后成交距今" % worst, "%.1f 分钟" % stale[worst],
                ">=%.0f 分钟" % STALE_TRADE_MIN,
                "若该腿重新出现成交（距今 < %.0f 分钟），本条不成立" % STALE_TRADE_MIN,
                "该腿不走 maker（挂单无意义），或直接不做"))
        else:
            dropped.append({"id": "stale_quotes", "reason": "未超过阈值",
                            "metric": "%s 停滞" % worst,
                            "value": "%.1f 分钟" % stale[worst]})
    # ---- ③ 容量约束 ----
    cap = None
    try:
        cap = max_capturable_usd(base)
    except Exception:  # noqa: BLE001
        cap = None
    if cap is not None:
        e.append(ev("可捕获名义额", "$%s" % format(int(cap), ","),
                    "data/derived/friction_budget.csv"))
        if cap < 10000:
            hyps.append(H(
                "thin_capacity", "可捕获名义额不足 1 万美元 —— 规模撑不住",
                "可捕获名义额", "$%s" % format(int(cap), ","), "<$10,000",
                "若可捕获名义额回到 $10,000 以上，本条不成立",
                "规模上限按实测容量收缩"))
    # ---- ④ 逆向选择 ----
    adv = None
    if cost and cost.get("adv_s") is not None:
        adv = float(cost["adv_s"])
    if adv is not None:
        e.append(ev("现货腿逆向选择 f_dmid(k6)", "%+.2f bp" % adv,
                    "data/derived/precise_fill_spot_bid.csv"))
        if adv <= -3.0:
            hyps.append(H(
                "adverse_selection", "现货腿逆向选择负向加深 —— 挂单被系统性挑选",
                "f_dmid(现货腿,k6)", "%+.2f bp" % adv, "<=-3.0 bp",
                "若 f_dmid 回升到 -3.0 bp 以上，本条不成立",
                "不做 maker（挂单等于把成交让给知情方）"))

    # ---- ⑤ 停牌 / 报价冻结（依据 docs/32 的豁免条款 + audit_samples 口径）----
    #   判据 = 底层中间价不动 **且** 窗口内零成交（`halted_from`）。
    #   ⚠️ 只用"报价不动"会在盘前活市场上误报（实测踩到，见 `halted_from` 注释）。
    froz, fdetail, _mids = frozen_quote(base)
    if froz is not None:
        e.append(ev("底层报价活性（最近 %d 轮现货中间价）" % FROZEN_LOOKBACK,
                    fdetail, "data/spread/orderbook-*.csv"))
        if froz:
            hyps.append(H(
                "quote_frozen",
                "底层疑似**停牌**（现货中间价不同值 <= %d 个 **且** 窗口内零成交）——"
                "停牌期间挂单不会成交，且复牌可能跳空"
                % FROZEN_MAX_DISTINCT,
                "最近 %d 轮现货中间价跨度 + 窗口成交笔数" % FROZEN_LOOKBACK,
                fdetail[:80],
                "不同中间价 <= %d 且成交笔数 == 0" % FROZEN_MAX_DISTINCT,
                "若报价恢复变动**或**重新出现成交，本条不成立",
                "不下新单；已挂的撤掉（停牌期间挂着无意义且承担跳空风险）"))
        else:
            dropped.append({"id": "quote_frozen", "reason": "未见停牌证据",
                            "metric": "现货中间价跨度 + 窗口成交",
                            "value": fdetail[:60]})

    hyps = hyps[:MAX_HYPOTHESES]
    if not e:
        return report("execution_risk", "neutral", 0.0, [],
                      "无执行风险数据"), hyps, dropped
    # 有假设 -> 不利；无假设但有数据 -> 中性（**不假装"有利"**）
    verdict = "unfavorable" if hyps else "neutral"
    conf = 0.75 if hyps else 0.5
    note = ("提出 %d 条可执行风险假设（每条带实测量 + 阈值 + 证伪条件）：%s"
            % (len(hyps), "、".join(h["id"] for h in hyps))
            if hyps else "未触发任何风险假设（阈值见 evidence）")
    if dropped:
        note += "；另有 %d 条未过阈值（留痕不删）" % len(dropped)
    return report("execution_risk", verdict, conf, e, note), hyps, dropped


# ------------------------------------------------- ⚓ 外部锚定（第三方价格参照）

# 定价偏离的告警阈值（bp）。
# ⚠️ **这是一个约定，不是标定值** —— 我们只有一份偏离快照（10 个标的，
#    −117 ~ +34 bp），样本不足以标定。写成常量是为了"改它要留痕"，
#    将来攒够样本再用实测分布替换它（见 docs/53 的"没做什么"）。
ANCHOR_DIVERGENCE_BP = 20.0


def analyst_external_anchor(base, anchor, cost=None):
    """⚓ **外部锚定分析师** —— 拿真实美股报价给 rToken 定价做参照。

    ━━ 为什么需要它 ━━

    在此之前本项目**完全没有外部参照价**：所有数字都来自 Bitget 自己的盘口与成交，
    是"自我参照"的。于是有一个问题一直答不了：

        rToken 现在这个价，相对**真实股票**是贵了还是便宜了？

    数据来自官方 `bitget-mcp-server`（**无需账号**），
    由 `tools/mcp_anchor.py --refresh` 取并落盘，本函数**只读缓存**
    （取数与消费分开：决策链必须能离线跑）。

    ━━ 🔴 它只报**偏离幅度**，不报方向 ━━

    刻意**不判**"折价 = 看多 / 溢价 = 看空"：那需要一个"偏离会收敛"的价格假设，
    而本项目有红线 —— **不做价格预测**。所以：

      · ``|偏离| ≤ 阈值`` -> ``neutral``（贴合；**不构成有利证据**）
      · ``|偏离| > 阈值`` -> ``unfavorable``（定价与外部锚脱节 = **风险**，不是机会）

    ━━ 🔴 口径必须说清（否则这个数会骗人）━━

    美股有开闭市、rToken 7×24：

      · 美股**开市** -> 同一时刻两个价 -> 叫**折溢价**（字段 ``premium_bp``）
      · 美股**休市** -> 量到的是**折溢价 + 休市漂移**，**两者无法分离**
        （休市期间根本没有真实股价可比）-> 字段 ``drift_and_premium_bp``

    这个口径由 `mcp_anchor` 在取数时判好并写进缓存，本函数**照抄不自作主张**。

    ━━ 数据来源类别 ━━

    真实美股报价是**第三方数据**（provider 见缓存），
    **不是本项目的实测量** —— evidence 的 source 里必须写明这一点。
    """
    e, hyps, dropped = [], [], []
    if not isinstance(anchor, dict) or anchor.get("deviation_bp") is None:
        return report("external_anchor", "neutral", 0.0, [],
                      "未接外部锚（没有缓存或该标的取不到）—— "
                      "**这不等于『定价没有偏离』**"), hyps, dropped

    dev = float(anchor["deviation_bp"])
    field = anchor.get("field") or "deviation_bp"
    label = anchor.get("label") or "偏离"
    same = anchor.get("same_instant")
    src = ("第三方：mcp:bitget-mcp-server（%s）｜ 美股报价 %s"
           % (anchor.get("provider") or "provider 未标明",
              anchor.get("quote_utc") or "时刻未知"))

    e.append(ev("真实美股中价", "%.4f" % anchor["stock_mid"], src))
    e.append(ev("rToken 中价", "%.4f（来源 %s）"
                % (anchor["rtoken_mid"], anchor.get("rtoken_source") or "?"),
                "data/spread（本项目侧）"))
    e.append(ev(label, "%+.1f bp" % dev, src))
    e.append(ev("两个价是否同一时刻", "是" if same else "**否**（口径含不可分离的漂移）",
                "美股时段 %s" % (anchor.get("session") or "未知")))

    if abs(dev) > ANCHOR_DIVERGENCE_BP:
        hyps.append({
            "id": "anchor_divergence",
            "hypothesis": "rToken 定价与**外部锚（真实美股）**脱节 %+.1f bp，"
                          "超过阈值 %.0f —— 定价偏离意味着挂单可能被"
                          "**收敛**吃掉（不是机会）" % (dev, ANCHOR_DIVERGENCE_BP),
            "metric": label, "value": "%+.1f bp" % dev,
            "threshold": "|偏离| > %.0f bp" % ANCHOR_DIVERGENCE_BP,
            "falsifier": "若偏离回到阈值以内（|偏离| ≤ %.0f bp），本条不成立"
                         % ANCHOR_DIVERGENCE_BP,
            "action": "复核执行方式：缩小规模 / 改吃单 / 避开该腿挂单",
        })
    else:
        dropped.append({"id": "anchor_divergence", "reason": "偏离在阈值内",
                        "metric": label, "value": "%+.1f bp" % dev})

    conf = 0.7 if same else 0.4     # 休市时两个价不同时刻 -> 置信度按规矩压低
    verdict = "unfavorable" if hyps else "neutral"
    note = ("与外部锚偏离 %+.1f bp（%s）%s"
            % (dev, label,
               "；**两个价不同时刻**，所以这个数含不可分离的漂移，置信度已压低"
               if not same else "；两个价同一时刻，可比"))
    note += "｜⚠️ 真实美股报价是**第三方数据**，不是本项目的实测量"
    return report("external_anchor", verdict, conf, e, note), hyps, dropped


# ------------------------------------------------- ⑥ 执行进度（执行中闭环）

# ⏱️ 执行进度官读的**恰好**是这几个字段 —— 复跑契约里也只记这几个。
#    为什么不把整个持仓单塞进日志：单子里可能有点位、备注、券商单号之类的
#    **不影响本决策**的字段，它们一变，复跑就会误报"不一致"，而真正的原因只是
#    多存了无关信息。契约应当**只包含决策真正依赖的输入**。
ORDER_STATE_FIELDS = ("base", "qty_usd", "spot_filled", "perp_filled",
                      "opened_ms", "id", "synthetic")


def norm_order_state(order_state):
    """把持仓单规范化成**契约字段**（只留决策真正读的那几个）。

    返回 `None` 表示"没有在途订单"，与空 dict 等价 —— 两者都走"零假设"分支。
    """
    if not isinstance(order_state, dict) or not order_state:
        return None
    out = {}
    for k in ORDER_STATE_FIELDS:
        if k in order_state:
            out[k] = order_state[k]
    if not str(out.get("base") or "").strip():
        return None
    for k in ("spot_filled", "perp_filled"):
        out[k] = bool(out.get(k))
    for k, cast in (("opened_ms", int), ("qty_usd", float)):
        try:
            out[k] = cast(out.get(k) or 0)
        except (TypeError, ValueError):
            out[k] = cast(0)
    return out


def analyst_execution_progress(base, order_state, cost=None, now_ms=None):
    """⏱️ **执行进度官** —— 回答"下单之后怎么办"（2026-09-20 新增）。

    ━━ 为什么必须有它 ━━

    v1 的链路只在**开仓前**跑一次（`docs/33` 原文：「只在开仓前的少数时点触发」）。
    于是**挂单没成交、或者只成交了一条腿的时候，链路里没有任何角色负责** ——
    而这恰恰是赛道三「执行辅助」手册点名的位置：trader 决策后，AI 如何负责
    **拆单与滑点管理**。没有这一段，「执行辅助」就只是「决策辅助」。

    ━━ 它只回答两个问题（都必须可证伪）━━

      ① **裸露敞口**：一腿已成交、另一腿超过 `NAKED_GRACE_MIN` 仍未成交
         -> veto 级。依据不是猜的，是**实测联合成交分布**（本次快照）：
             · `joint_fill_all_in_house.csv`（in_house 路线）：
               `P(只成交一腿)=23.6%` vs `P(两腿都成交)=3.60%` ≈ **6.6 倍**
             · `joint_fill_all.csv`（不分 venue）：`35.5%` vs `2.39%` ≈ **14.9 倍**
            也就是说"一腿成交后另一腿不跟"是**常态**，不是意外。
            ⚠️ 数值随窗口/路线变，所以**不在代码里写死**：报告 evidence 与
               `--decision-selftest` 都直接读实测表并打印当期值。
               （早先注释里写的是 24.9%/0.70%/35 倍 —— 那是**旧窗口**的数，
                 换窗口后就不成立了，属于已修正的过期数字。）
      ② **挂单停滞**：挂单存活已 >= `STALE_TRADE_MIN`，且这期间**该腿零成交**
         -> caution 级（提示复核：改价 / 撤单 / 转吃单）。
            依据：挂单的收益完全来自成交；市场没有成交时，挂单不可能成交。

    ━━ 红线 ━━

    **它不产出任何数字**（不给新价位、不给新规模、不给损益估计）——
    那是确定性交易员与成本模型的职责。它只给"该复核什么"的**触发条件**。
    给出 `order_state` 时，`run_decision` 会另外调用 `execution_actions()`
    用**成本模型**算出唯一的实测数字（补腿成本）。

    ``order_state`` 形状（与 `tools/position_watch.py` 的持仓单一致）::

        {"id": "p1", "base": "NVDA", "qty_usd": 5000,
         "spot_filled": true, "perp_filled": false,
         "opened_ms": 1789720000000}

    ⚠️ **`opened_ms` 必须与本次决策的 `now_ms` 用同一个时钟**：实时模式下两个都是
    墙钟，天然一致；读冻结快照时 `now_ms` 由 `time_basis()` 按**数据自带时刻**给出，
    这时 `opened_ms` 也必须落在数据时钟上。两者不一致时本函数**显式拒绝判断**
    （返回中性 + 写清理由），不会钳成 0 假装算过 —— 那会让裸露敞口**静默消失**。
    """
    e, hyps, dropped = [], [], []
    if not isinstance(order_state, dict):
        return report("execution_progress", "neutral", 0.0, [],
                      "无在途订单"), hyps, dropped
    if (order_state.get("base") or "").upper() != base.upper():
        return report("execution_progress", "neutral", 0.0, [],
                      "该订单不属于本标的（%s）" % order_state.get("base")), \
            hyps, dropped
    # 🔴 合成演示单必须**一路带着标记**：证据来源、结论备注都要能看出
    #    "这不是真实下单记录"。否则演示截图会被当成实测结论引用。
    synthetic = bool(order_state.get("synthetic"))
    prov = ("**合成演示持仓**（synthetic=true，非真实下单）" if synthetic
            else "持仓单（用户/上层提供）")
    if synthetic:
        e.append(ev("持仓来源", "合成演示单 —— 仅用于展示判据，"
                                "**不得**当成真实下单结论引用", "order_state.synthetic"))

    now_ms = now_ms or int(dt.datetime.now(dt.UTC).timestamp() * 1000)
    try:
        opened = int(order_state.get("opened_ms") or 0)
    except (TypeError, ValueError):
        opened = 0
    if opened <= 0:
        return report("execution_progress", "neutral", 0.0, [],
                      "订单缺少 opened_ms，无法判断进度（不猜）"), hyps, dropped

    raw_min = (now_ms - opened) / 60000.0
    # 🔴 时钟一致性：`opened_ms` 是"我这单什么时候下的"（墙钟），
    #    而 `now_ms` 可能是 **time_basis 判出来的决策基准时间**（读冻结快照时
    #    按数据自带时刻）。两者不是一个钟时，`now - opened` 是**没有意义的数**。
    #    ⚠️ 初版这里写的是 `elapsed = max(0.0, raw_min)` —— 后果实测到了：
    #       订单被算成"存活 0 分钟"，于是**宽限期内**，裸露敞口假设**静默消失**，
    #       决策里一条 agent 规则都不产生，而页面上还显示"执行进度：正常"。
    #       这是最危险的一类失败（判据说"没事"），所以必须**显式拒绝判断**，
    #       而不是钳成 0 假装算过了。
    if raw_min < 0:
        why = ("持仓单 opened_ms 晚于本次决策基准时间（差 %.1f 分钟）—— "
               "两者不是同一个时钟，**不硬判**" % -raw_min)
        return report("execution_progress", "neutral", 0.0,
                      [ev("挂单存活", "%.1f 分钟（负值）" % raw_min,
                          "持仓单 opened_ms 与决策基准时间之差"),
                       ev("决策基准时间", str(now_ms), "time_basis().now_ms"),
                       ev("订单开仓时间", str(opened), "持仓单 opened_ms")],
                      why), hyps, [
            {"id": "partial_fill_naked", "reason": "时钟不一致，无法判龄",
             "metric": "挂单存活", "value": "%.1f 分钟" % raw_min},
            {"id": "progress_stalled", "reason": "时钟不一致，无法判龄",
             "metric": "挂单存活", "value": "%.1f 分钟" % raw_min}]
    elapsed = raw_min
    s_fill = bool(order_state.get("spot_filled"))
    p_fill = bool(order_state.get("perp_filled"))
    qty = float(order_state.get("qty_usd") or 0.0)

    e.append(ev("挂单存活", "%.1f 分钟" % elapsed,
                "opened_ms（%s）" % prov))
    if qty > 0:
        e.append(ev("在途订单规模", "%.0f USD" % qty, "qty_usd（%s）" % prov))
    e.append(ev("两腿成交状态",
                "现货=%s ｜ 永续=%s" % ("已成交" if s_fill else "未成交",
                                        "已成交" if p_fill else "未成交"),
                "spot_filled/perp_filled（%s）" % prov))

    # 未成交的那条腿，在"挂单期间"到底有没有成交（逐笔实测，不靠猜）
    missing = None
    if s_fill != p_fill:
        missing = "perp" if s_fill else "spot"
    n_tr = None
    if not (s_fill and p_fill):
        legs = [v for v, f in (("spot", s_fill), ("perp", p_fill)) if not f]
        n_tr = {}
        for v in legs:
            n_tr[v] = _trades_between_venue(v, base, opened, now_ms)
            e.append(ev("%s 腿在挂单期间成交笔数" % v, "%d 笔" % n_tr[v],
                        "data/spread/trades-*.csv"))

    # ---- ① 裸露敞口（veto）----
    if s_fill != p_fill and elapsed >= NAKED_GRACE_MIN:
        got = "现货腿" if s_fill else "永续腿"
        lack = "永续腿" if s_fill else "现货腿"
        hyps.append({
            "id": "partial_fill_naked",
            "hypothesis": "只成交了%s —— **裸露的方向性敞口**（另一腿 %s 超过宽限"
                          "%.0f 分钟仍未成交）" % (got, lack, NAKED_GRACE_MIN),
            "metric": "挂单存活", "value": "%.1f 分钟" % elapsed,
            "threshold": ">=%.0f 分钟" % NAKED_GRACE_MIN,
            "falsifier": "若%s也成交（两腿齐平），本条不成立" % lack,
            "action": "二选一：① 立刻吃单补上%s ② 平掉已成交的%s"
                      "（两者都付一次往返成本）；在此之前**不得开新仓**"
                      % (lack, got)})
    elif s_fill != p_fill:
        dropped.append({"id": "partial_fill_naked", "reason": "在宽限期内，先观察",
                        "metric": "挂单存活", "value": "%.1f 分钟" % elapsed})

    # ---- ② 挂单停滞（caution）----
    if not (s_fill and p_fill) and elapsed >= STALE_TRADE_MIN:
        tr = (n_tr or {}).get(missing if missing else "spot")
        if tr == 0:
            hyps.append({
                "id": "progress_stalled",
                "hypothesis": "挂单已存活 %.0f 分钟，而这期间该腿**零成交** —— "
                              "市场没有成交时，挂单不可能成交（收益完全来自成交）"
                              % elapsed,
                "metric": "挂单期间该腿成交笔数", "value": "0 笔",
                "threshold": ">=%d 分钟仍为 0" % int(STALE_TRADE_MIN),
                "falsifier": "若该腿在挂单期间重新出现成交（笔数 > 0），本条不成立",
                "action": "复核执行方式：改价 / 撤单 / 转双腿全吃单"})
        elif tr is not None:
            dropped.append({"id": "progress_stalled",
                            "reason": "期间有成交（只是没成交到我的单）",
                            "metric": "挂单期间该腿成交笔数", "value": "%d 笔" % tr})
    elif not (s_fill and p_fill):
        dropped.append({"id": "progress_stalled", "reason": "未到停滞阈值",
                        "metric": "挂单存活", "value": "%.1f 分钟" % elapsed})

    if missing:
        # 实测联合分布：用来解释"为什么一腿不跟是常态"，不是用来算命的
        try:
            try:
                from execution_cost import load_joint_fill
            except ImportError:
                from project2.execution_cost import load_joint_fill
            j = (load_joint_fill((cost or {}).get("route")).get(base)
                 or load_joint_fill(None).get(base))
            if j:
                e.append(ev("实测 P(只成交一腿)", "%.1f%%" % (100 * j["p_part"]),
                            j["prov"]))
                e.append(ev("实测 P(两腿都成交)", "%.2f%%" % (100 * j["p_both"]),
                            j["prov"]))
        except Exception:  # noqa: BLE001
            pass

    if not e:
        return report("execution_progress", "neutral", 0.0, [],
                      "无执行进度数据"), hyps, dropped
    verdict = "unfavorable" if any(h["id"] == "partial_fill_naked" for h in hyps) \
        else ("neutral" if hyps else "neutral")
    conf = 0.8 if verdict == "unfavorable" else (0.6 if hyps else 0.5)
    note = ("提出 %d 条执行中假设：%s" % (len(hyps), "、".join(h["id"] for h in hyps))
            if hyps else "执行中未触发假设（阈值见 evidence）")
    if dropped:
        note += "；另有 %d 条未过阈值（留痕不删）" % len(dropped)
    if synthetic:
        note = "⚠️ **合成演示持仓**（非真实下单记录）：" + note
    note += "｜⚠️ 本路**不产出数字**（新价位/新规模由确定性交易员给）"
    return (report("execution_progress", verdict, conf, e, note), hyps, dropped)


def execution_actions(base, order_state, cost=None):
    """裸露/停滞时**确定性**给出的处置口径（数字全部来自成本模型）。

    与 `analyst_execution_progress` 的分工（红线 1 的落地）：
      · agent 说"**该处置了**"（触发条件、可证伪）；
      · 这个函数说"**处置要付多少**"（数字来自实测成本模型，可复跑）。

    ⚠️ 刻意**不给损益估计**：本项目不下单、没有持有期损益口径，
       所以"不处置会亏多少"这种数字我们**算不出来也不编**。
    """
    out = []
    if not isinstance(order_state, dict):
        return out
    s_fill = bool(order_state.get("spot_filled"))
    p_fill = bool(order_state.get("perp_filled"))
    if s_fill == p_fill:
        return out
    got = "现货腿" if s_fill else "永续腿"
    lack = "永续腿" if s_fill else "现货腿"
    c = cost or {}
    tk = c.get("cost_tk")
    out.append({
        "id": "complete_leg",
        "title": "立刻吃单补上%s" % lack,
        "cost_bp": tk,
        "cost_note": ("双腿全吃单口径的往返成本（来自实测成本模型 "
                      "execution_cost.analyse_two_leg）" if tk is not None
                      else "成本模型不可用 —— 不给数字"),
        "immediate": True,
    })
    out.append({
        "id": "flatten_leg",
        "title": "平掉已成交的%s" % got,
        "cost_bp": None,
        "cost_note": ("需要该腿当时的点差与费率才能算 —— 本页不编数字；"
                      "口径见 docs/09 与 docs/14"),
        "immediate": False,
    })
    out.append({
        "id": "no_new_position",
        "title": "处置完成前不要开新仓",
        "cost_bp": None,
        "cost_note": "风控官已按 agent 假设给出 veto 级规则",
        "immediate": True,
    })
    return out


# ---------------------------------------------------------------- 汇总

def run_team(base, cost=None, now_ms=None, news_mode="auto", headlines=None,
             size_usd=None, news_assess=None):
    """跑齐五个"分析师"。**相互独立**：每个只拿自己那一路数据。

    第 5 个（`execution_risk`）是**agent 做的风险评估层** —— 它不碰数字，
    只把"这一单可能怎么死"组织成可证伪的假设，交给辩论层与风控官。

    ``news_assess``：**冻结的事件判定**（复跑用）—— 直接透传给 `analyst_news`，
    该路就完全不调 LLM。见 `analyst_news(assess_override=...)`。

    ⚠️ 外部输入的可控性：`news_mode="llm"` 与自动抓新闻都是**活的输入**
    （LLM 输出有随机性、新闻每天在变），所以**确定性自检/复跑必须显式冻结**：
    传 `news_mode="static"` + `headlines=[]`（见 `run_decision(freeze_news=True)`），
    或传 `news_assess=<冻结判定>`。
    真 LLM 接上后，不冻结会让"端到端复跑一致"这条断言**间歇性失败**（实测踩到）。
    """
    risk_rep, hyps, dropped = analyst_execution_risk(
        base, cost=cost, now_ms=now_ms, size_usd=size_usd)
    reps = [
        analyst_basis(base, cost=cost),
        analyst_sentiment(base),
        analyst_news(base, now_ms=now_ms, mode=news_mode, headlines=headlines,
                     assess_override=news_assess),
        analyst_technical(base),
        risk_rep,
    ]
    out = []
    for r in reps:
        ok, why = validate(r)
        out.append({"report": r, "valid": ok, "invalid_reason": why})
    return out, hyps, dropped


def render_team(base, items, verbose=True):
    L = ["  %-6s —— %d 路独立分析（第 5 路是 agent 做的**执行风险评估**）"
         % (base, len(items))]
    for it in items:
        r = it["report"]
        tag = {"favorable": "[有利]", "unfavorable": "[不利]",
               "neutral": "[中性]"}.get(r["verdict"], "[?]")
        mark = "" if it["valid"] else "  ✗ 无效：%s" % it["invalid_reason"]
        L.append("    %-9s %s 置信度 %.2f%s" % (r["dimension"], tag,
                                               r["confidence"], mark))
        for e in r["evidence"][:4]:
            L.append("        · %s = %s   [%s]" % (e["metric"], e["value"],
                                                   e["source"]))
        if r["notes"]:
            L.append("        ~ %s" % r["notes"])
    if verbose:
        print("\n".join(L))
    return "\n".join(L)


# ---------------------------------------------------------------- ⑤ 多空辩论层
#
# 为什么需要辩论层，以及它**不许**做什么（docs/25 的四条诚实边界）：
#   1. 每条论点必须**可证伪** —— 必须说清"看到什么就该撤回它"。
#      给不出证伪条件的论点一律作废，和"没有证据就作废"是同一条铁律。
#      多 agent 最坏的样子是两边各说各话、听起来都很像样，却没人能判对错。
#   2. 辩论层**只影响边缘决策，绝不改量化基线** ——
#      basis_bp、成本、闸门结论都不因辩论而变。返回体里用 does_not_alter 明写。
#   3. 它**不能替代硬闸门**。闸门 block 时，辩论再怎么偏多头也到不了 proceed。
#   4. 只在**开仓前**的几个时点触发，不做持续轮询。

BULL = "bull"
BEAR = "bear"
STANCES = ("proceed", "caution", "stand_down")

# ---- ⭐ 证据强度加权（2026-09-18 新增，用户第 15 条要求）----
#
# 为什么要它：辩论得分原来是 `Σ 置信度`，于是**一条"机制推断"和一条"实测"等权**。
# 例：情绪分析师的「持仓拥挤度代理 = 正费率占比越高 = 多头越拥挤（机制推断，非实测）」
# 与「现货腿成交率 = 46.9%（逐笔实测）」本来不是一回事，却按同样权重计分。
#
# 权重怎么定（**不是拍的**，按"这条证据错了，我们多久能发现、代价多大"排序）：
#
#   | 强度 | 权重 | 什么算 | 判据 |
#   |---|---|---|---|
#   | **measured** | 1.00 | 我方自采/自算的实测量 | 来源指向 data/spread、data/derived、或我方的分析脚本 |
#   | **verified** | 0.85 | 外部可回溯来源 | 来源带 URL 或指向已归档的外部记录 |
#   | **derived**  | 0.60 | 由实测量再计算出的量 | 来源是我方**其它脚本的产出**（中间层，可被上游改动影响） |
#   | **inference**| 0.30 | 机制推断 / 非实测 | 来源里明写"非实测""机制推断"，或无来源 |
#
# 为什么 inference 压到 0.30：它**无法被证伪**（没有可测的量可对照），
# 而本层的铁律就是"给不出证伪条件的论点作废"。既然它还能进来（因为没被没收），
# 那就让它的**计分权重**反映"不可证伪"这件事。
STRENGTH_WEIGHTS = {"measured": 1.0, "verified": 0.85, "derived": 0.6,
                    "inference": 0.3}

# 来源 -> 强度。**先匹配先赢**；匹配不到按 derived（保守中间值）。
#
# ⚠️ 顺序有讲究，踩过：URL 里常带 `2026-`（如 SEC 新闻稿链接
# `.../press-releases/2026-90-...`），若把 `2026-` 放在 URL 之前，
# **外部可回溯来源会被误判成 measured** —— 那等于把"别人说的话"当成"我们实测"。
# 所以 `http(s)://` 必须排在具体度量名之前。
STRENGTH_RULES = (
    (("非实测", "机制推断", "假设值", "推断"), "inference"),
    (("http://", "https://"), "verified"),
    (("friction_budget.csv", "funding_rates.csv", "precise_fill_",
      "joint_fill_", "orderbook-", "trades-", "sentiment-",
      "news_latest.json"), "measured"),
    (("data/spread", "data/derived"), "measured"),
    (("project2/", "tools/", "common/", "docs/"), "derived"),
)

# 这些阈值全部来自已实测的结论，不是这里新编的
COST_THRESHOLD_BP = 11.34   # docs/14：扣掉资金费收入后的成本阈值
DEPTH_MIN_USD = 5000.0      # 前端深度徽章使用的容量阈值
FEE_RT_MM_BP = 14.0         # docs/09：全挂单往返费率
FEE_RT_TK_BP = 22.0         # docs/09：永续吃单往返费率
DEBATE_MARGIN = 0.25        # 两侧得分差超过它才算"有倾向"，否则算僵持
DIRECTION_PENALTY = 0.15    # 每个"方向自相矛盾"的论据扣这么多分（固定值，公开可查）


# 度量名 -> 证伪条件。**只覆盖已实测过的度量**；匹配不到就没收论点。
FALSIFIER_RULES = (
    (("往返净收益", "双腿最优执行成本", "现货半幅点差"),
     "若实测往返成本越过 %.2f bp 阈值（或净收益转负），此论点作废" % COST_THRESHOLD_BP),
    (("可捕获名义额", "五档总深度", "首档占比"),
     "若五档总深度低于 $%s，此论点作废（容量不足）" % format(int(DEPTH_MIN_USD), ",")),
    (("资金费", "持仓拥挤度"),
     "若短永续由收资金费转为付费（费率转负），此论点作废"),
    (("成交率", "逆向选择"),
     "若现货腿成交率下滑、或逆向选择 f_dmid 的负值进一步加深，此论点作废"),
    (("事件严重度", "判断置信度", "事件理由", "日历已复核"),
     "若能取得可回溯来源、且严重度降为 none，此论点作废"),
)


def falsifier_for(metric):
    """给一个度量名找出**具体**的证伪条件。找不到就返回 None（论点将被没收）。"""
    m = str(metric)
    for keys, text in FALSIFIER_RULES:
        for k in keys:
            if k in m:
                return text
    return None


# 度量方向：polarity=+1 表示**数值越大对我们越有利**，-1 表示越大越不利。
# 用途：检查一方的论据方向是否和它的立场相反。
#
# 为什么必须查这个（实测踩到）：
#   空头曾把「48h 窗口资金费收入 = +1.571 bp」当作**不利**论据引用 ——
#   可资金费收入是正的，短永续是**收钱**的那一方，这笔收入明显对我们有利。
#   辩论层如实转述了分析师的标签，却没发现方向自相矛盾。
#   不查这一条，辩论就退化成"两边各说各话"，看起来都很有道理。
DIRECTIONAL = (
    (("往返净收益", "48h 窗口资金费收入", "成交率"), +1),
    (("双腿最优执行成本", "现货半幅点差", "逆向选择"), -1),
    (("五档总深度", "可捕获名义额", "首档占比"), +1),
)


def _num(s):
    m = re.search(r"[-+]?\d+(?:\.\d+)?", str(s).replace(",", ""))
    try:
        return float(m.group()) if m else None
    except (AttributeError, ValueError):
        return None


def direction_conflict(side, evidence):
    """点名**方向与立场相反**的论据。返回说明字符串列表。"""
    want = 1 if side == BULL else -1
    bad = []
    for e in evidence:
        for keys, pol in DIRECTIONAL:
            if not any(k in e["metric"] for k in keys):
                continue
            v = _num(e["value"])
            if v is None or v == 0:
                break
            good_for_us = (pol * v) > 0
            if good_for_us != (want > 0):
                bad.append(
                    "%s = %s 的数值方向偏%s，却被当作%s的论据"
                    % (e["metric"], e["value"],
                       "有利" if good_for_us else "不利",
                       "多头" if side == BULL else "空头"))
            break
    return bad


def arg(claim, evidence, falsifier):
    """构造一条可证伪的论点。claim / evidence / falsifier 三者缺一不可。"""
    return {"claim": str(claim),
            "evidence": [e for e in (evidence or []) if _ev_ok(e)],
            "falsifier": str(falsifier or "")}


def _arg_ok(a):
    return (isinstance(a, dict) and a.get("claim") and a.get("evidence")
            and a.get("falsifier"))


def strength_of(source):
    """给一条证据的**来源**定强度等级。见 STRENGTH_WEIGHTS 的推导表。"""
    s = str(source or "")
    for keys, level in STRENGTH_RULES:
        if any(k in s for k in keys):
            return level
    return "derived"


def weighted_weight(evidence, confidence):
    """一条论点的加权得分 = **最高强度证据的权重** × 置信度。

    为什么取"最高强度"而不是平均：一条论点可以打包多条证据（同一证伪条件分组），
    其中只要有一条是实测，这条论点的**可证伪性**就成立了 ——
    用平均会把"1 条实测 + 3 条推断"压得比"1 条实测"还低，那是错的。
    """
    levels = [strength_of(e.get("source")) for e in (evidence or [])]
    if not levels:
        return 0.0, None
    best = max(levels, key=lambda l: STRENGTH_WEIGHTS.get(l, 0.0))
    return float(confidence) * STRENGTH_WEIGHTS.get(best, 0.0), best


def argue(side, reports):
    """一方立论。返回该方的论点、让步与**被没收的论点**（如实记录，不藏）。

    得分有两套，**都保留**（这样"加权改变了什么"永远可审计）：
      * ``raw_weight``  = Σ 置信度（旧口径，逐条等权）
      * ``weight``      = Σ（置信度 × **证据强度权重**）（现口径，用户第 15 条）
    """
    want = "favorable" if side == BULL else "unfavorable"
    other = "unfavorable" if side == BULL else "favorable"

    args, dropped = [], []
    weight, raw_weight = 0.0, 0.0
    for r in reports:
        ok, _why = validate(r)
        if not ok or r["verdict"] != want:
            continue
        # 🔴 按**证伪条件**分组：共享同一证伪条件的度量只出一条论点。
        # 实测踩到过：NVDA 的多头一上来就有 10 条论点，其中 3 条是
        # spot/bid 深度、首档占比、spot/ask 深度 —— 同一个度量族、同一条证伪条件。
        # 那就是**把一个判断换三个说法**，正是本层要防的失败模式。
        # 不修的话，论点数会变成纯粹的灌水指标。
        groups = collections.OrderedDict()
        for e in r["evidence"]:
            f = falsifier_for(e["metric"])
            if not f:
                # 找不到证伪条件 -> 没收。宁可少一条论点，不留一条无法判对错的。
                dropped.append({"metric": e["metric"], "value": e["value"],
                                "reason": "该度量没有已实测的证伪条件"})
                continue
            groups.setdefault(f, []).append(e)
        got = False
        for f, evs in groups.items():
            a = arg(
                "%s 维度（置信度 %.2f）指向%s：%s"
                % (r["dimension"], r["confidence"],
                   "有利" if side == BULL else "不利",
                   "；".join("%s = %s" % (x["metric"], x["value"]) for x in evs)),
                evs, f)
            a["direction_conflicts"] = direction_conflict(side, evs)
            _sc, _lvl = weighted_weight(evs, r["confidence"])
            a["score"] = round(_sc, 4)
            a["strength"] = _lvl
            args.append(a)
            got = True
        if got:
            # ⭐ 按强度加权（一条报告的论点共享同一置信度，所以简单相加即可）
            raw_weight += r["confidence"]
            sc, lvl = weighted_weight(
                [e for f, evs in groups.items() for e in evs], r["confidence"])
            weight += sc
            if lvl:
                r.setdefault("_strength_used", lvl)

    # 让步：必须承认对方最强的一条，否则就是立稻草人
    concessions = []
    for r in reports:
        ok, _ = validate(r)
        if ok and r["verdict"] == other and r["evidence"]:
            e = max(r["evidence"], key=lambda x: STRENGTH_WEIGHTS.get(
                strength_of(x.get("source")), 0.0))
            concessions.append({"dimension": r["dimension"],
                                "metric": e["metric"], "value": e["value"],
                                "confidence": r["confidence"],
                                "strength": strength_of(e.get("source"))})
    # 让步顺序：先按强度、再按置信度（"对方最强的一条"应当指**最硬**的那条）
    concessions.sort(key=lambda c: (-STRENGTH_WEIGHTS.get(c["strength"], 0.0),
                                    -c["confidence"]))
    conflicts = [c for a in args for c in a.get("direction_conflicts", [])]
    return {"side": side, "arguments": args, "weight": round(weight, 2),
            "raw_weight": round(raw_weight, 2),
            "strength_mix": collections.Counter(
                a.get("strength") for a in args if a.get("strength")),
            "concessions": concessions[:2], "dropped": dropped,
            "direction_conflicts": conflicts}


def cross_examine(bull, bear):
    """交叉质证：每方必须**指名**对方最强的一条，并复述其证伪条件。

    这一层的作用是让"两边各说各话"在结构上不可能 ——
    你必须回应对方最强的那条，而不是挑一条软的打。
    """
    out = {"bull_rebuts": None, "bear_rebuts": None}
    if bear["arguments"] and bull["arguments"]:
        t = bear["arguments"][0]
        out["bull_rebuts"] = {
            "target_claim": t["claim"],
            "target_falsifier": t["falsifier"],
            "bull_position": "接受该证伪条件；在它被触发前不据此加仓",
        }
    if bull["arguments"] and bear["arguments"]:
        t = bull["arguments"][0]
        out["bear_rebuts"] = {
            "target_claim": t["claim"],
            "target_falsifier": t["falsifier"],
            "bear_position": "接受该证伪条件；在它成立前不下「不参与」的结论",
        }
    return out


def adjudicate(bull, bear, gate=None, cost=None):
    """确定性裁决。**不引入任何新量，也不修改任何已有量。**

    gate: (severity, reason, source, maker_allowed) 或 None
    cost: analyse_two_leg 的返回，仅用于如实展示，不参与打分
    """
    # 方向自相矛盾的论据要扣分：一方靠"方向反了"的证据赢下来是假赢。
    # 固定扣分、公开可查，不做隐藏调节。
    bull_pen = DIRECTION_PENALTY * len(bull.get("direction_conflicts") or [])
    bear_pen = DIRECTION_PENALTY * len(bear.get("direction_conflicts") or [])
    bw = max(0.0, round(bull["weight"] - bull_pen, 2))
    rw = max(0.0, round(bear["weight"] - bear_pen, 2))

    diff = bw - rw
    if bw <= 0 and rw <= 0:
        base = "caution"
        why = "两侧都没有站得住的论点（可能全部因缺证据/缺证伪条件被没收）"
    elif diff >= DEBATE_MARGIN:
        base, why = "proceed", "多头得分高出 %.2f，超过僵持阈值 %.2f" % (diff, DEBATE_MARGIN)
    elif diff <= -DEBATE_MARGIN:
        base, why = "stand_down", "空头得分高出 %.2f，超过僵持阈值 %.2f" % (-diff, DEBATE_MARGIN)
    else:
        base, why = "caution", "两侧相差仅 %.2f，未超过僵持阈值 %.2f" % (abs(diff), DEBATE_MARGIN)

    # 强度加权的**可审计性**：同时给出旧口径（逐条等权）会得出什么结论。
    # 两者不一致时必须在输出里说清"是加权改变了结论"，否则读者无法归因。
    raw_bull = float(bull.get("raw_weight", bull["weight"]))
    raw_bear = float(bear.get("raw_weight", bear["weight"]))
    raw_diff = raw_bull - raw_bear
    if raw_bull <= 0 and raw_bear <= 0:
        raw_base = "caution"
    elif raw_diff >= DEBATE_MARGIN:
        raw_base = "proceed"
    elif raw_diff <= -DEBATE_MARGIN:
        raw_base = "stand_down"
    else:
        raw_base = "caution"

    stance, cap_reason = base, None
    if gate:
        sev = gate[0]
        if sev == "block":
            # 🔴 硬闸门优先：辩论不能把被闸门否掉的东西说成可执行
            if stance != "stand_down":
                cap_reason = "事件闸门 block —— 辩论结论被压到 stand_down（硬闸门不可被辩论推翻）"
            stance = "stand_down"
        elif sev == "caution" and stance == "proceed":
            stance = "caution"
            cap_reason = "事件闸门 caution —— proceed 被降为 caution"

    return {
        "stance": stance,
        "base_stance": base,
        "reason": why,
        "cap_reason": cap_reason,
        "bull_weight": bw,
        "bear_weight": rw,
        "raw_bull_weight": bull["weight"],
        "raw_bear_weight": bear["weight"],
        # ⭐ 证据强度加权（用户第 15 条）：两套口径并存，便于审计归因
        "raw_weights_old_scale": {"bull": raw_bull, "bear": raw_bear,
                                  "stance": raw_base, "diff": round(raw_diff, 2)},
        "weighting_changed_stance": bool(raw_base != base and not cap_reason),
        "strength_weights": dict(STRENGTH_WEIGHTS),
        "strength_mix": {"bull": dict(bull.get("strength_mix") or {}),
                         "bear": dict(bear.get("strength_mix") or {})},
        "direction_penalty": DIRECTION_PENALTY,
        "direction_conflicts": (bull.get("direction_conflicts") or [])
                               + (bear.get("direction_conflicts") or []),
        "margin": DEBATE_MARGIN,
        # 明写这一层碰了什么、没碰什么 —— 免得被误读成"辩论改写了收益"
        "does_not_alter": [
            "basis_bp / 点差 / 成本等一切量化基线",
            "execution_cost 的最优执行方案",
            "event_gate 的严重度判定",
        ],
        "cost_snapshot": ({"best_mode": cost.get("best_mode"),
                           "best_cost": cost.get("best_cost")}
                          if isinstance(cost, dict) else None),
    }


def run_debate(base, items, gate=None, cost=None):
    """跑完整辩论：立论 -> 交叉质证 -> 裁决。"""
    reps = [i["report"] for i in items if i.get("valid")]
    bull = argue(BULL, reps)
    bear = argue(BEAR, reps)
    return {"base": base, "bull": bull, "bear": bear,
            "cross": cross_examine(bull, bear),
            "verdict": adjudicate(bull, bear, gate=gate, cost=cost),
            "excluded_reports": [i["report"]["dimension"] for i in items
                                 if not i.get("valid")]}


def render_debate(d, verbose=True):
    L = ["  %s —— 多空辩论" % d["base"]]
    for side, tag in ((BULL, "多头"), (BEAR, "空头")):
        s = d[side]
        mix = s.get("strength_mix") or {}
        mix_txt = "、".join("%s×%d" % (k, v) for k, v in sorted(mix.items())) or "无"
        L.append("    [%s] 论点 %d 条 ｜ 得分 %.2f（原始 Σ置信度 %.2f）｜ 被没收 %d 条"
                 % (tag, len(s["arguments"]), s["weight"],
                    s.get("raw_weight", s["weight"]), len(s["dropped"])))
        L.append("        证据强度构成：%s（measured1.00 / verified0.85 / "
                 "derived0.60 / inference0.30）" % mix_txt)
        for a in s["arguments"][:3]:
            e = a["evidence"][0]
            L.append("        · %s" % a["claim"])
            L.append("          证据: %s = %s  [%s]  强度=%s 得分=%.3f"
                     % (e["metric"], e["value"], e["source"],
                        a.get("strength"), a.get("score", 0.0)))
            L.append("          证伪: %s" % a["falsifier"])
        if s["concessions"]:
            c = s["concessions"][0]
            L.append("        让步: 承认 %s 的 %s = %s（置信度 %.2f，强度 %s）"
                     % (c["dimension"], c["metric"], c["value"], c["confidence"],
                        c.get("strength", "?")))
    v = d["verdict"]
    L.append("    ==> 裁决: %s（原始倾向 %s）" % (v["stance"], v["base_stance"]))
    L.append("        得分: 多头 %.2f ｜ 空头 %.2f（含方向矛盾扣分 %.2f/条）"
             % (v["bull_weight"], v["bear_weight"], v["direction_penalty"]))
    if v.get("direction_conflicts"):
        L.append("        ⚠️ 方向自相矛盾的论据 %d 条 —— 已扣分，需人工确认："
                 % len(v["direction_conflicts"]))
        for c in v["direction_conflicts"]:
            L.append("           · %s" % c)
    L.append("        理由: %s" % v["reason"])
    if v["cap_reason"]:
        L.append("        ⚠️ 被闸门压制: %s" % v["cap_reason"])
    L.append("        本层未改动: %s" % "、".join(v["does_not_alter"]))
    if d["excluded_reports"]:
        L.append("    （%s 的报告因不合铁律已排除在辩论之外）"
                 % "、".join(d["excluded_reports"]))
    if verbose:
        print("\n".join(L))
    return "\n".join(L)


def debate_selftest():
    """辩论层自检 —— 重点是**防住多 agent 的典型失败模式**。"""
    ok = True

    def chk(cond, msg):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s" % ("OK " if cond else "!! ", msg))

    # ① 没有证据的论点必须被没收
    bad = arg("凭空看多", [], "若成本越过 11.34 bp 则作废")
    chk(not _arg_ok(bad), "无证据的论点被没收")

    # ② 没有证伪条件的论点必须被没收
    good_ev = [ev("往返净收益（全挂单）", "+6.50 bp", "data/derived/friction_budget.csv")]
    bad2 = arg("看多", good_ev, "")
    chk(not _arg_ok(bad2), "无证伪条件的论点被没收")

    # ③ 未实测过的度量取不到证伪条件 -> 没收
    chk(falsifier_for("某个我编的指标") is None, "未实测度量取不到证伪条件（会被没收）")
    chk(bool(falsifier_for("往返净收益（全挂单）")), "已实测度量能取到证伪条件")
    chk(str(COST_THRESHOLD_BP) in falsifier_for("双腿最优执行成本"),
        "证伪条件引用的是**已实测阈值** %.2f bp" % COST_THRESHOLD_BP)

    # ④ 用合成报告跑一遍，双方都能立论且每条都有证伪条件
    rep_fav = report("basis", "favorable", 0.8, good_ev)
    rep_unf = report("news", "unfavorable", 0.4,
                     [ev("事件严重度", "block", "project2/event_gate.py")])
    reps = [rep_fav, rep_unf]
    bull, bear = argue(BULL, reps), argue(BEAR, reps)
    chk(bull["arguments"] and bear["arguments"], "双方都能立论（多头 %d / 空头 %d）"
        % (len(bull["arguments"]), len(bear["arguments"])))
    chk(all(a["falsifier"] for a in bull["arguments"] + bear["arguments"]),
        "保留下来的论点**每一条都有证伪条件**")
    chk(bull["concessions"] and bear["concessions"], "双方都必须让步（承认对方最强一条）")

    # ④b 🔴 同一证伪条件不得重复计分（防「把一个判断换三个说法」灌水）
    rep_multi = report("technical", "favorable", 0.7, [
        ev("spot/bid 五档总深度", "$123,561", "data/spread/orderbook-*.csv"),
        ev("spot/bid 首档占比", "46.7%", "data/spread/orderbook-*.csv"),
        ev("spot/ask 五档总深度", "$109,565", "data/spread/orderbook-*.csv"),
    ])
    b_multi = argue(BULL, [rep_multi])
    chk(len(b_multi["arguments"]) == 1,
        "共享同一证伪条件的 3 个度量只出 1 条论点（实际 %d 条）"
        % len(b_multi["arguments"]))
    chk(len(b_multi["arguments"][0]["evidence"]) == 3,
        "但 3 条证据都保留在同一条论点里（未丢证据）")
    falsifiers = [a["falsifier"] for a in b_multi["arguments"]]
    chk(len(falsifiers) == len(set(falsifiers)), "论点的证伪条件互不重复")

    # ④c 🔴 方向自相矛盾的论据必须被点名（实测踩到过）
    #     空头曾把「48h 窗口资金费收入 = +1.571 bp」当**不利**论据引用，
    #     可资金费收入是正的 —— 短永续是收钱那方，明显对我们有利。
    bad_dir = direction_conflict(BEAR, [
        ev("48h 窗口资金费收入", "+1.571 bp", "data/derived/funding_rates.csv")])
    chk(bool(bad_dir), "空头引用正的资金费收入被判定为方向矛盾")
    ok_dir = direction_conflict(BEAR, [
        ev("双腿最优执行成本", "+12.18 bp（双腿全吃单）", "docs/14")])
    chk(not ok_dir, "空头引用偏高的执行成本**不**算方向矛盾（方向正确）")
    ok_bull_dir = direction_conflict(BULL, [
        ev("spot/bid 五档总深度", "$123,561", "data/spread/orderbook-*.csv")])
    chk(not ok_bull_dir, "多头引用充足深度**不**算方向矛盾")
    chk(direction_conflict(BULL, [
        ev("48h 窗口资金费收入", "-0.800 bp", "data/derived/funding_rates.csv")]),
        "多头引用负的资金费收入被判定为方向矛盾（反向也成立）")

    # ④d ⭐ 证据强度加权（用户第 15 条）：实测与推断**不得等权**
    chk(strength_of("data/derived/precise_fill_spot_bid.csv") == "measured",
        "实测来源判为 measured")
    chk(strength_of("机制推断（非实测）") == "inference",
        "写明『机制推断（非实测）』的判为 inference")
    chk(strength_of("https://www.sec.gov/newsroom/press-releases/2026-90") == "verified",
        "外部可回溯 URL 判为 verified")
    chk(strength_of("project2/execution_cost.py") == "derived",
        "我方脚本产出判为 derived")
    s_m, l_m = weighted_weight(
        [ev("往返净收益（全挂单）", "-12.41 bp", "data/derived/friction_budget.csv")], 0.8)
    s_i, l_i = weighted_weight(
        [ev("持仓拥挤度代理", "正费率占比高", "机制推断（非实测）")], 0.8)
    chk(s_m > s_i and l_m == "measured" and l_i == "inference",
        "同置信度下 实测(%.3f) > 推断(%.3f) —— 等权问题已修" % (s_m, s_i))
    # 一条实测 + 三条推断 = 该论点仍按**最高强度**计（可证伪性由那条实测成立）
    s_best, l_best = weighted_weight(
        [ev("持仓拥挤度代理", "x", "机制推断（非实测）"),
         ev("往返净收益（全挂单）", "-12.41 bp", "data/derived/friction_budget.csv")], 1.0)
    chk(l_best == "measured" and abs(s_best - 1.0) < 1e-9,
        "论点内取**最高强度**而非平均（否则'1 实测+3 推断'会比'1 实测'还低）")
    chk(STRENGTH_WEIGHTS["inference"] < STRENGTH_WEIGHTS["derived"]
        < STRENGTH_WEIGHTS["verified"] <= STRENGTH_WEIGHTS["measured"],
        "权重单调：inference < derived < verified <= measured")

    # ⑤ 交叉质证必须指名对方最强的一条
    cx = cross_examine(bull, bear)
    chk(cx["bull_rebuts"] and cx["bear_rebuts"],
        "交叉质证：双方都指名了对方最强的一条并复述其证伪条件")

    # ⑥ 🔴 硬闸门优先：闸门 block 时，即使多头占优也不能 proceed
    v_block = adjudicate(bull, bear, gate=("block", "合成测试", "selftest", False))
    chk(v_block["stance"] == "stand_down",
        "闸门 block 时裁决被压到 stand_down（多头本可得 %s）" % v_block["base_stance"])

    # ⑦ 闸门 caution 时 proceed 被降级
    v_cau = adjudicate(bull, bear, gate=("caution", "合成测试", "selftest", True))
    chk(v_cau["stance"] in ("caution", "stand_down"), "闸门 caution 时不会给出 proceed")

    # ⑦b 加权可审计：裁决必须同时给出新旧两套口径
    chk(v_cau.get("raw_weights_old_scale") is not None
        and "weighting_changed_stance" in v_cau,
        "裁决同时保留新旧两套口径（加权改变了什么可审计：旧口径倾向 %s）"
        % (v_cau.get("raw_weights_old_scale") or {}).get("stance"))

    # ⑧ 不碰量化基线：必须显式声明，且不含任何可写回基线的字段
    chk(v_block["does_not_alter"] and len(v_block["does_not_alter"]) >= 3,
        "裁决显式声明未改动量化基线（%d 项）" % len(v_block["does_not_alter"]))
    forbidden = {"basis_bp", "spread_bp", "cost", "maker_allowed"}
    chk(not (forbidden & set(v_block.keys())),
        "裁决返回体里没有可写回基线的字段（%s）" % "、".join(sorted(forbidden)))

    # ⑨ 确定性：同输入两次结果必须一致
    a1 = adjudicate(argue(BULL, reps), argue(BEAR, reps))
    a2 = adjudicate(argue(BULL, reps), argue(BEAR, reps))
    chk(a1 == a2, "确定性：同输入两次裁决完全一致")

    print("\n辩论层自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


# ================================================================ ④ 交易员 + ⑤ 风控官
#
# 用户设想里的最后一环是「决策团队：风控执行」。
# 落到我们的代码上，它**不是**再拉两个 LLM 出来说话，而是：
#
#   💰 交易员  = execution_cost 的双腿联合模型（确定性）
#                —— 把「做不做」翻译成**可执行订单**：方式 / 规模 / 价位 / 拆几笔
#   🛡️ 风控官 = 一张**规则表**，每条规则都能独立触发一票否决（确定性）
#                —— 与事件闸门同源，但覆盖闸门管不到的四类风险（成本/逆向选择/
#                   容量/数据缺失）
#
# ━━ 为什么这两个必须是确定性的、不能是 LLM ━━
# `docs/25` §2.2 的边界：**agent 决定"这一单做不做"，不决定"门槛是多少"。**
# 规模和价位一旦由 LLM 生成，报告里的数字就无法复跑（第 5 步的复现日志立刻失效）。
# 所以这里全部是纯函数：同输入 -> 同输出 -> 同一个决策哈希。
#
# ━━ 🔴 必须成立的单调性（自检里用穷举矩阵双向证明）━━
#   最终立场 ≤ 辩论立场，最终规模 ≤ 交易员规模，最终规模 ≤ 风控规模上限。
#   交易员与风控官**只能收紧，不能放松** —— 否则「一票否决」就是装饰。

RISK_LEVELS = ("pass", "caution", "veto")
DOWNGRADE_ONLY = ("proceed", "caution", "stand_down")   # 单调方向：只能往右走

# 名义额下限（低于它的单没有意义：手续费与固定成本占比过高）
MIN_NOTIONAL_USD = 100.0

# 拆单粒度（USD）与深度占用上限。两者都是**公开可查的固定规则**，不做隐藏调节。
SLICE_USD = 2000.0
DEPTH_TAKE_RATIO = 0.25     # 单笔不超过首档可用深度的 1/4

# 证伪条件文本中引用的阈值 -> 供人机都容易核对
EDGE_THRESHOLD_BP = 11.34   # docs/14：净收益判据的门槛（与 docs/13 同一口径）
ADV_MIN_BP = -3.0           # docs/14 实测量级：逆向选择负向加深即视为被挑选

# 🔴 「成本超过边际」的判定：**两侧都用同一个已实测阈值**（11.34 bp），不新造数字。
#
# 为什么是 AND（成本越线 **且** 毛边际不足）：
#   * 「最优方案成本 > 门槛」= 执行层吃掉的钱比策略允许的还多（`project2/execution_cost.py`）；
#   * 「毛边际 < 门槛」= 项目一已实测的净收益+资金费本身就不够。
#   两者同时成立 -> 这一单在**执行层无解**：没有任何执行方式能让它不亏。
#
# 为什么不在成本越线时**直接**否决：毛边际是**项目一的策略结论**，
#   只凭它下结论就等于风控官替研究层拍板了。加成本这一侧，
#   否决才完全建立在执行层的事实上（`docs/25` §3.1）。
EDGE_THRESHOLD_NAME = "净收益判据门槛"

FEE_SPOT_RT = 5.0           # 与 execution_cost.FEE_SPOT 同源（docs/09）


def _fee(name, fallback):
    """从 execution_cost 取费率常量 —— **只读**，取不到就用同源常量兜底。"""
    try:
        try:
            import execution_cost as _ec
        except ImportError:
            from project2 import execution_cost as _ec
        return float(getattr(_ec, name, fallback))
    except Exception:  # noqa: BLE001
        return float(fallback)


FEE_PERP_MAKER = _fee("FEE_PERP_MAKER", 2.0)   # docs/09：永续 maker 2 bp
FEE_PERP_TAKER = _fee("FEE_PERP_TAKER", 6.0)   # docs/09：永续 taker 6 bp

# ---- 参考价：证伪条件文本里的历史阈值（docs/14），仅用于展示"差多少" ----
REF_EDGE_BP = 11.34          # 净收益判据门槛
REF_HALF_SPREAD_BP = 25.00   # 现货半幅点差的历史上界量级（docs/14）


def stage_rank(stance):
    """立场宽松度：数值越大越激进。单调性检查全靠它。"""
    try:
        return len(DOWNGRADE_ONLY) - 1 - DOWNGRADE_ONLY.index(stance)
    except ValueError:
        return 0


def _cost_scaled(cost, qty):
    """把执行成本按名义额**同比例缩放**后重新取最优方案（**只在允许的方案里选**）。

    ⚠️ 已声明的模型局限（不藏）：
      实测成交率 `p_fill` 是在**该标的实测成交的中位名义额**上得到的，
      缩规模不会让 `p_fill` 变大。所以规模缩放只影响冲击成本，
      **不改变成交率** —— 这是保守方向（不会高估小单的成交概率）。

    ⚠️ 闸门作废的挂单类方案**不参与取最优** —— 否则输出里会出现一个
      「成本更低但已经被否决」的方案，读者会以为它可选。
    """
    if not cost:
        return None
    q = float(qty or 0.0)
    q0 = float(cost.get("qty") or 0.0) or 1.0
    out = dict(cost)
    # impact_bp 不在 analyse_two_leg 的返回体里，所以这里如实分解：
    #   三项成本里只有「冲击」随规模变，费/点差/逆向选择**不随规模变**。
    #   先把不随规模的部分（fixed）算出来，再只缩放差额（即冲击）那一块。
    fixed = _fixed_cost_part(cost)
    for k in ("cost_mm", "cost_mix", "cost_tk"):
        if cost.get(k) is not None:
            out[k] = (fixed.get(k, 0.0)
                      + (float(cost[k]) - fixed.get(k, 0.0)) * q / q0)
    allowed = bool(cost.get("maker_allowed", True))
    cands = [("双腿全挂单", out["cost_mm"]),
             ("现货挂单+永续吃单", out["cost_mix"]),
             ("双腿全吃单", out["cost_tk"])]
    if not allowed:
        cands = [c for c in cands if c[0] == "双腿全吃单"] or cands
    best = min(cands, key=lambda z: z[1])
    out["qty"] = q
    out["best_mode"], out["best_cost"] = best
    out["allowed_modes"] = [n for n, _c in cands]
    out["barred_modes"] = ([] if allowed else ["双腿全挂单", "现货挂单+永续吃单"])
    return out


def _fixed_cost_part(cost):
    """不随规模变化的成本部分（费 + 半幅点差 + 逆向选择），用于规模缩放时不缩错。"""
    p_s = float(cost.get("p_s") or 0.0)
    p_p = float(cost.get("p_p") or 0.0)
    p_both = p_s * p_p
    p_part = p_s * (1 - p_p) + p_p * (1 - p_s)
    p_none = (1 - p_s) * (1 - p_p)
    half_s = float(cost.get("half_s") or 0.0)
    half_p = float(cost.get("half_p") or 0.0)
    adv_s = float(cost.get("adv_s") or 0.0)
    adv_p = float(cost.get("adv_p") or 0.0)
    miss = float(cost.get("miss") or 0.0)
    leg = float(cost.get("leg_risk") or 0.0)
    mm = (FEE_SPOT_RT + FEE_PERP_MAKER
          - p_both * (half_s + half_p) - p_both * (adv_s + adv_p)
          + p_part * leg + p_none * miss)
    mix = FEE_SPOT_RT + FEE_PERP_TAKER - p_s * half_s - p_s * adv_s + (1 - p_s) * miss
    tk = FEE_SPOT_RT + FEE_PERP_TAKER
    return {"cost_mm": mm, "cost_mix": mix, "cost_tk": tk}


# 盘口读取很贵（最新一个 orderbook 文件 ~45 MB），所以按 (base, venue) 缓存。
# ⚠️ 缓存只在**单次进程**内有效 —— 复跑时重新读，不会拿到陈旧盘口。
_BOOK_CACHE = {}


def trader_book(base, cost=None, force=False):
    """📖 交易员的"账本"：盘口 / 深度 / 可捕获容量。**只读，不改任何量。**

    ``force=True`` 绕过进程内缓存重新读盘口 —— 复跑必须这样做，
    否则会拿到"这个进程开始时"的陈旧盘口，把复跑变成假象。
    """
    try:
        from execution_cost import latest_book as _lb, spread_stats as _ss
    except ImportError:
        from project2.execution_cost import latest_book as _lb, spread_stats as _ss

    out = {"book": {"spot": None, "perp": None}, "depth_usd": {}, "mid": {},
           "snapshot_ts": None, "paths": []}
    # 记录**实际被读的那个** orderbook 文件（采样器在写，所以它每天变）
    files = sorted(glob.glob(os.path.join(SPREAD, "orderbook-*.csv")))
    if files:
        out["paths"].append(os.path.relpath(files[-1], BASE).replace("\\", "/"))
    for venue in ("spot", "perp"):
        key = (base, venue)
        if force or key not in _BOOK_CACHE:
            _BOOK_CACHE[key] = _lb(base, venue)
        b = _BOOK_CACHE[key]
        if not b:
            continue
        out["book"][venue] = b
        ss = _ss(base, venue)
        out["mid"][venue] = ss["mid"] if ss else None
        out["snapshot_ts"] = max(out["snapshot_ts"] or 0, int(b["ts"]))
        for side in ("bid", "ask"):
            tot = sum(n for _p, n in b[side])
            l1 = b[side][0][1] if b[side] else 0.0
            out["depth_usd"]["%s/%s" % (venue, side)] = {
                "five_level": tot, "level1": l1,
                "first_share": (l1 / tot) if tot > 0 else 0.0}
    return out


def _px(v):
    """中间价的展示（拿不到就写「缺盘口」，不编数字）。"""
    return "%.4f" % v if isinstance(v, (int, float)) and v else "缺盘口"


def max_capturable_usd(base):
    """`friction_budget.csv` 的可捕获名义额 —— 容量的**实测**上界。"""
    r = {x.get("base"): x for x in _read_csv(
        os.path.join(DERIVED, "friction_budget.csv"))}.get(base)
    return _f(r, "capturable_notional_usd") if r else None


def trader(cost, book, stance, qty_usd, slice_usd=SLICE_USD,
           depth_take_ratio=DEPTH_TAKE_RATIO):
    """💰 交易员：把「做不做」变成**可执行订单**（方式 / 规模 / 价位 / 拆几笔）。

    一切数字都来自已实测的量，交易员**不新造任何阈值**：
      * 规模上界 = min(请求规模, 可捕获名义额(实测), 首档深度 × 占比上限)
      * 价位     = 中价 ± 半幅点差（挂单）/ 中价 + 点差（吃单，实测冲击另计）
      * 拆单     = 规模 / 单笔粒度

    返回 ``order``：``None`` 表示**不下单**（每个不下单的理由都必须写在
    ``blocked_by`` 里，不许静默返回空）。
    """
    empty = {"order": None, "blocked_by": [], "mode": None, "cost_bp": None,
             "max_qty_usd": 0.0, "slices": 0, "price": None,
             "gross_edge_bp": None, "edge_gap_bp": None}
    if not cost:
        empty["blocked_by"].append("没有执行成本数据（双腿联合模型不可用）—— 结构性无法定价")
        return empty
    if stance == "stand_down":
        empty["blocked_by"].append("裁决立场 stand_down —— 交易员不下单")
        return empty

    # ---- 规模上界：三个上界取最小，且都带来源 ----
    caps = []
    cap = max_capturable_usd(cost.get("base"))
    if cap:
        caps.append(("可捕获名义额（data/derived/friction_budget.csv）", float(cap)))
    else:
        caps.append(("可捕获名义额（缺数据，按 0 处理 —— fail-safe）", 0.0))
    for venue in ("spot", "perp"):
        d = (book or {}).get("depth_usd", {}).get("%s/ask" % venue) or {}
        l1 = float(d.get("level1") or 0.0)
        if l1 > 0:
            caps.append(("%s/ask 首档深度 × %.2f（data/spread/orderbook-*.csv）"
                         % (venue, depth_take_ratio), l1 * depth_take_ratio))
    qty = float(qty_usd or 0.0)
    cap_qty = min([qty] + [v for _n, v in caps])
    cap_why = min(caps, key=lambda z: z[1])[0] if caps else "无"

    # ---- 规模缩放并重取最优方案（缩放只改冲击，见 _cost_scaled 的局限声明）----
    scaled = _cost_scaled(cost, cap_qty)
    if not scaled or scaled.get("best_cost") is None:
        empty["blocked_by"].append("规模缩放后成本模型不可用")
        return empty
    pure = {"双腿全挂单": "mm", "现货挂单+永续吃单": "mix", "双腿全吃单": "taker"}
    mode2, cost2 = scaled["best_mode"], scaled["best_cost"]
    mk = pure.get(mode2, "taker")

    # ---- 价位：只用已实测的点差，不用任何预测 ----
    mid_s = (book or {}).get("mid", {}).get("spot")
    mid_p = (book or {}).get("mid", {}).get("perp")
    if mk == "mm":
        band = float(cost.get("half_s") or 0.0)
        price_desc = ("现货买腿挂 bid ≈ %s（中价 − %.2f bp 半幅）；"
                      "永续卖腿挂 ask ≈ %s"
                      % (_px(mid_s), band, _px(mid_p)))
        note = "挂单价只由**实测半幅点差**给出，不做任何价格预测"
    elif mk == "mix":
        price_desc = ("现货买腿挂 bid ≈ %s（中价 − %.2f bp）；永续卖腿吃 bid 立即成交"
                      % (_px(mid_s), float(cost.get("half_s") or 0.0)))
        note = "现货挂单+永续吃单：腿风险由 execution_cost 实测的 p_s 表达"
    else:
        price_desc = ("现货买腿吃 ask ≈ %s；永续卖腿吃 bid ≈ %s（均立即成交）"
                      % (_px(mid_s), _px(mid_p)))
        note = "吃单付满点差与冲击，但**不承担「在事件里等」的逆向选择**"

    n_slices = max(1, int(round(cap_qty / slice_usd + 0.4999))) if cap_qty > 0 else 0
    order = {
        "mode": mode2, "kind": mk, "qty_usd": round(cap_qty, 2),
        "slices": n_slices, "slice_usd": round(slice_usd, 2),
        "price_desc": price_desc,
        "cost_bp": round(float(cost2), 2),
        "cost_mode": mode2,
        "all_modes_bp": {"双腿全挂单": round(scaled["cost_mm"], 2),
                         "现货挂单+永续吃单": round(scaled["cost_mix"], 2),
                         "双腿全吃单": round(scaled["cost_tk"], 2)},
        "size_cap": round(cap_qty, 2),
        "size_cap_by": cap_why,
        "size_bounds": [{"name": n, "usd": round(v, 2)} for n, v in caps],
        "maker_allowed": bool(cost.get("maker_allowed", True)),
        "gate_severity": cost.get("gate_severity"),
        "barred_modes": scaled.get("barred_modes") or [],
        "note": note,
    }

    # ---- 毛边际（净收益 + 资金费）对门槛：差多少，如实写出来 ----
    edge = _gross_edge_bp(cost)
    gap = None if edge is None else round(edge - EDGE_THRESHOLD_BP, 2)

    out = dict(empty)
    out.update({"order": order, "mode": mode2, "cost_bp": order["cost_bp"],
                "max_qty_usd": order["size_cap"], "slices": n_slices,
                "price": order["price_desc"],
                # 三方案成本与让闸门作废的方案 —— 供风控官引用（**不因订单为空而丢**）
                "all_modes_bp": order["all_modes_bp"],
                "barred_modes": order["barred_modes"],
                "gross_edge_bp": None if edge is None else round(edge, 2),
                "edge_gap_bp": gap})
    if gap is not None and gap < 0:
        out["blocked_by"].append(
            "毛边际 %+.2f bp 低于门槛 %.2f bp（差 %.2f bp）—— **不是一票否决，"
            "交给风控官裁**（交易员只负责如实报差距）" % (edge, EDGE_THRESHOLD_BP, gap))
    return out


def _gross_edge_bp(cost):
    """毛边际 = 往返净收益(全挂单) + 48h 资金费收入。两个都是已实测量。"""
    base = cost.get("base")
    r = {x.get("base"): x for x in _read_csv(
        os.path.join(DERIVED, "friction_budget.csv"))}.get(base)
    f = {x.get("base"): x for x in _read_csv(
        os.path.join(DERIVED, "funding_rates.csv"))}.get(base)
    if not r:
        return None
    return (_f(r, "net_all_maker_bp", 0.0)
            + (_f(f, "window_income_bp", 0.0) if f else 0.0))


def render_order(t, verbose=True):
    L = ["  💰 交易员（执行成本模型 · 双腿联合）"]
    o = t.get("order")
    if not o:
        L.append("     ==> **不下单**")
        for b in t.get("blocked_by") or ["（未说明理由 —— 这是缺陷）"]:
            L.append("         · %s" % b)
        if verbose:
            print("\n".join(L))
        return "\n".join(L)
    L.append("     ==> %s（%s）" % (o["mode"], o["kind"]))
    L.append("         规模 %.0f USD ｜ 拆 %d 笔 × %.0f USD"
             % (o["qty_usd"], o["slices"], o["slice_usd"]))
    L.append("         规模上界 %.0f USD ← 取最小：%s" % (o["size_cap"], o["size_cap_by"]))
    for b in o["size_bounds"]:
        L.append("           · %-58s %10.0f USD" % (b["name"], b["usd"]))
    L.append("         价位 %s" % o["price_desc"])
    L.append("         成本 %+.2f bp ｜ 三方案对比：全挂单 %+.2f ｜ 混合 %+.2f ｜ 全吃单 %+.2f"
             % (o["cost_bp"], o["all_modes_bp"]["双腿全挂单"],
                o["all_modes_bp"]["现货挂单+永续吃单"],
                o["all_modes_bp"]["双腿全吃单"]))
    if o.get("barred_modes"):
        L.append("         [X] 已被事件闸门作废（不参与取最优）：%s"
                 % "、".join(o["barred_modes"]))
    if t.get("gross_edge_bp") is not None:
        L.append("         毛边际 %+.2f bp vs 门槛 %.2f bp（差 %+.2f bp）"
                 % (t["gross_edge_bp"], EDGE_THRESHOLD_BP, t["edge_gap_bp"]))
    for b in t.get("blocked_by") or []:
        L.append("         [!] %s" % b)
    if verbose:
        print("\n".join(L))
    return "\n".join(L)


def risk_officer(base, cost=None, book=None, trader_out=None, debate=None,
                 event=None, now_ms=None, hypotheses=None):
    """🛡️ 风控官：**一票否决**，且只收紧不放松。

    两类输入，**权力不同**（这是本项目的核心设计）：
      * **固定规则表**（下面 R）：确定性、可审计、覆盖已实测过的失败模式；
      * **agent 提出的风险假设**（``hypotheses``）：由第 5 个分析师
        （`analyst_execution_risk`）产出，每条带实测量 + 验证阈值 + 证伪条件。
        agent 只能**提议动作**（`HYPOTHESIS_ACTIONS`），
        **最终立场与规模仍由本函数决定**，且仍然只能收紧 —— 见 `decide()` 的单调性。

    ⚠️ 为什么不让 agent 直接给最终结论：`docs/25` §2.2 —— 门槛与规模必须可复跑。
    agent 负责"**发现与论证**"（报价在动但没成交，这种跨表的判断很适合它），
    代码负责"**执行与守边界**"。

    返回体里逐条列出 ``measured``（实际读到的值）与 ``falsifier``（撤销条件）——
    **没触发的规则也要留痕**，否则"风控通过"无法被审计。
    """
    b = book or {}
    t = trader_out or {}
    d = debate or {}
    v = (d or {}).get("verdict") or {}
    order = t.get("order")
    gate_sev = (event or {}).get("severity")
    if gate_sev is None:
        gate_sev = cost.get("gate_severity") if cost else None
    gate_block = (gate_sev == "block")
    cap = max_capturable_usd(base)
    depth_s = ((b.get("depth_usd") or {}).get("spot/ask") or {}).get("level1") or 0.0
    depth_p = ((b.get("depth_usd") or {}).get("perp/ask") or {}).get("level1") or 0.0
    adv_s = float(cost.get("adv_s")) if cost and cost.get("adv_s") is not None else None
    edge = t.get("gross_edge_bp")
    modes = t.get("all_modes_bp") or {}
    stance = v.get("stance")
    qty = float(order["qty_usd"]) if order else 0.0

    def _cap_action():
        return {"action": "cap_size",
                "qty_usd": min(qty, float(cap) * 0.1) if cap else 0.0,
                "why": "可捕获名义额 %s USD 的 10%% —— 容量不足时先缩规模" % (
                    format(int(cap), ",") if cap else "缺数据")}

    def _depth_action():
        return {"action": "cap_size",
                "qty_usd": max(0.0, min(qty, min(depth_s, depth_p) * DEPTH_TAKE_RATIO)),
                "why": "按较小的首档深度 %s USD 的 %.0f%% 缩规模"
                       % (format(int(min(depth_s, depth_p)), ","),
                          DEPTH_TAKE_RATIO * 100)}

    def _taker_action():
        return {"action": "require_taker",
                "why": "腿风险过高时禁止挂单类方案，只允许双腿全吃单"}

    R = [
        {"id": "gate_block", "level": "veto", "action": "no_new_position",
         "statement": "事件窗口内禁止新开仓（挂单类方案在 execution_cost 里已作废）",
         "measured": "闸门 severity=%s" % gate_sev,
         "falsifier": "若能取得可回溯来源、且严重度降为 none，本条撤销",
         "hit": bool(gate_block)},
        {"id": "debate_stand_down", "level": "veto", "action": "no_new_position",
         "statement": "裁决立场为 stand_down 时不得开仓",
         "measured": "辩论裁决 stance=%s" % stance,
         "falsifier": "若两侧得分差重回僵持阈值 %.2f 以内，本条不再触发"
                      % DEBATE_MARGIN,
         "hit": stance == "stand_down"},
        {"id": "cost_exceeds_edge", "level": "veto", "action": "no_new_position",
         "statement": "最优执行成本越过 %s（%.2f bp）**且**毛边际本身低于该门槛 —— "
                      "执行层无解" % (EDGE_THRESHOLD_NAME, EDGE_THRESHOLD_BP),
         "measured": "最优执行成本 {} ｜ 毛边际(净收益+资金费) {} ｜ 门槛 {:+.2f} bp "
                     "｜ 三方案：全挂单 {} ｜ 混合 {} ｜ 全吃单 {}".format(
                         ("%+.2f bp" % t["cost_bp"]) if t.get("cost_bp") is not None else "缺",
                         ("%+.2f bp" % edge) if edge is not None else "缺",
                         EDGE_THRESHOLD_BP,
                         ("%+.2f" % modes["双腿全挂单"]) if "双腿全挂单" in modes else "缺",
                         ("%+.2f" % modes["现货挂单+永续吃单"]) if "现货挂单+永续吃单" in modes else "缺",
                         ("%+.2f" % modes["双腿全吃单"]) if "双腿全吃单" in modes else "缺"),
         "falsifier": "若最优成本回落到 %.2f bp 以内，或毛边际升到 %.2f bp 以上，本条撤销"
                      % (EDGE_THRESHOLD_BP, EDGE_THRESHOLD_BP),
         "hit": bool(t.get("cost_bp") is not None and edge is not None
                     and float(t["cost_bp"]) > EDGE_THRESHOLD_BP
                     and float(edge) < EDGE_THRESHOLD_BP)},
        {"id": "adverse_selection", "level": "veto", "action": "no_new_position",
         "statement": "现货腿逆向选择负向加深（f_dmid ≤ %.1f bp = 挂单被系统性挑选）"
                      % ADV_MIN_BP,
         "measured": "f_dmid(现货腿,k6) = %s bp"
                     % (("%+.2f" % adv_s) if adv_s is not None else "缺"),
         "falsifier": "若 f_dmid 回升到 %.1f bp 以上，本条撤销" % ADV_MIN_BP,
         "hit": bool(adv_s is not None and adv_s <= ADV_MIN_BP)},
        {"id": "thin_capacity", "level": "caution", "action": "cap_size",
         "statement": "可捕获名义额不足（< %.0f USD）—— 容量撑不住计划规模"
                      % (MIN_NOTIONAL_USD * 10),
         "measured": "可捕获名义额 = %s USD"
                     % (format(int(cap), ",") if cap else "缺数据"),
         "falsifier": "若可捕获名义额回升到 %.0f USD 以上，本条撤销"
                      % (MIN_NOTIONAL_USD * 10),
         "hit": bool(cap is not None and cap < MIN_NOTIONAL_USD * 10),
         "remedy": _cap_action},
        {"id": "thin_depth", "level": "caution", "action": "cap_size",
         "statement": "单笔超过首档深度的 %.0f%% —— 会显著消耗档位" % (DEPTH_TAKE_RATIO * 100),
         "measured": "spot/ask 首档 %s USD ｜ perp/ask 首档 %s USD（单笔上限 %.0f%%）"
                     % (format(int(depth_s), ","), format(int(depth_p), ","),
                        DEPTH_TAKE_RATIO * 100),
         "falsifier": "若两腿首档深度均高于单笔规模 ÷ %.2f，本条撤销" % DEPTH_TAKE_RATIO,
         "hit": bool(qty > 0 and min(depth_s, depth_p) > 0
                     and qty > min(depth_s, depth_p) * DEPTH_TAKE_RATIO),
         "remedy": _depth_action},
        {"id": "leg_risk_high", "level": "caution", "action": "require_taker",
         "statement": "「只成交一腿」概率 > 50% —— 会留下裸露的方向敞口",
         "measured": "P(只成交一腿) = %.1f%%（实测成交率反推）"
                     % (100.0 * (float(cost.get("p_part") or 0.0) if cost else 0.0)),         "falsifier": "若 P(只成交一腿) 降到 50% 以内，本条撤销",
         "hit": bool(cost and float(cost.get("p_part") or 0.0) > 0.5),
         "remedy": _taker_action},
        {"id": "no_capacity_data", "level": "caution", "action": "no_new_position",
         "statement": "容量数据缺失时不得按无上限处理（**数据缺失 ≠ 没有风险**）",
         "measured": "可捕获名义额 = %s" % ("缺数据" if cap is None else "有"),
         "falsifier": "若能读到可捕获名义额，本条撤销",
         "hit": cap is None},
        {"id": "no_cost_data", "level": "caution", "action": "no_new_position",
         "statement": "成本数据缺失时不得下单（无法定价的单不许下）",
         "measured": "最优执行成本 = %s ｜ 交易员未下单原因：%s"
                     % ("缺" if t.get("cost_bp") is None
                        else "%+.2f bp" % t["cost_bp"],
                        (t.get("blocked_by") or ["（未说明）"])[0][:40]),
         "falsifier": "若能算出双腿执行成本，本条撤销",
         # ⚠️ 只有"成本结构本身缺失"才算数据缺失；**立场导致的空订单不算**
         #    （否则 stand_down 时会出现 "成本=缺" 的**假警报**，实测踩到）
         "hit": bool(t.get("cost_bp") is None and not t.get("order")
                     and any("立场" not in b and "裁决" not in b
                             for b in (t.get("blocked_by") or ["成本缺失"])))},
    ]

    # ---- ⭐ agent 提出的风险假设 -> 变成风控动作（**agent 提议，风控执行**）----
    hyp_rules = []
    for h in (hypotheses or []):
        spec = HYPOTHESIS_ACTIONS.get(h.get("id"))
        if not spec:
            continue
        hyp_rules.append({
            "id": "agent:" + str(h.get("id")),
            "level": spec["level"], "action": spec["action"],
            "source": "agent(execution_risk)",
            "statement": h.get("hypothesis", ""),
            "measured": "%s = %s（阈值 %s）" % (h.get("metric"), h.get("value"),
                                               h.get("threshold")),
            "falsifier": h.get("falsifier", ""),
            "proposed_action": h.get("action", ""),
            "hit": True,
        })
    R = R + hyp_rules

    # 规则表顺序固定 -> 确定性；remedy 只在触发时求值，且**求值后立刻摘掉**
    # （返回体必须能 json.dumps：函数对象既不可序列化，也会让"同输入同输出"失真）
    for r in R:
        r["qty_cap"] = None
        if r["hit"] and r["action"] == "cap_size":
            rem = (r.pop("remedy", None) or (lambda: {"qty_usd": 0.0}))()
            r["qty_cap"] = round(max(0.0, min(qty, float(rem.get("qty_usd") or 0.0))), 2)
            r["remedy_note"] = rem.get("why", "")
        elif r["hit"] and r["action"] == "require_taker":
            r["remedy_note"] = (r.pop("remedy", None) or (lambda: {}))().get("why", "")
        else:
            r.pop("remedy", None)
    hits = [r for r in R if r["hit"]]
    vetoes = [r for r in hits if r["level"] == "veto"]
    cautions = [r for r in hits if r["level"] == "caution"]

    if vetoes:
        verdict, final_qty = "reject", 0.0
        reason = "、".join(r["id"] for r in vetoes)
    elif cautions:
        verdict = "caution"
        caps = [r["qty_cap"] for r in cautions if r["qty_cap"] is not None]
        final_qty = min(caps) if caps else qty
        reason = "、".join(r["id"] for r in cautions)
    else:
        verdict, final_qty, reason = "pass", qty, "无规则触发"

    return {
        "officer": "risk", "verdict": verdict, "reason": reason,
        "rules": R, "hits": [r["id"] for r in hits],
        "vetoes": [r["id"] for r in vetoes], "cautions": [r["id"] for r in cautions],
        "qty_in_usd": round(qty, 2), "qty_out_usd": round(final_qty, 2),
        "rejected": verdict == "reject",
        "veto_rule": vetoes[0]["id"] if vetoes else None,
        # agent 参与度：留痕，便于审计"agent 的话有没有真的起作用"
        "agent_hypotheses": len(hypotheses or []),
        "agent_rules": [r["id"] for r in hyp_rules],
        "agent_driven": bool([r for r in hits if r.get("source") == "agent(execution_risk)"]),
        "does_not_alter": ["execution_cost 的成本与最优方案",
                           "event_gate 的严重度判定",
                           "辩论层的得分与裁决"],
        "checked_rules": len(R), "triggered_rules": len(hits),
    }


def render_risk(r, verbose=True):
    L = ["  🛡️ 风控官（%d 条规则逐条留痕，%d 条触发）"
         % (r["checked_rules"], r["triggered_rules"])]
    for rule in r["rules"]:
        tag = {"veto": "[否决]", "caution": "[警示]"}.get(rule["level"], "[通过]")
        mark = " ← 触发" if rule["hit"] else ""
        L.append("     %s %-20s %s%s" % (tag, rule["id"], rule["statement"], mark))
        L.append("         实测: %s" % rule["measured"])
        L.append("         撤销条件: %s" % rule["falsifier"])
        if rule.get("qty_cap") is not None:
            L.append("         处置: 规模上限收到 %.0f USD —— %s"
                     % (rule["qty_cap"], rule.get("remedy_note", "")))
        elif rule["hit"] and rule.get("remedy_note"):
            L.append("         处置: %s" % rule["remedy_note"])
    L.append("     ==> 风控结论: %s（%s）" % (r["verdict"], r["reason"]))
    L.append("         规模 %.0f -> %.0f USD" % (r["qty_in_usd"], r["qty_out_usd"]))
    L.append("         本层未改动: %s" % "、".join(r["does_not_alter"]))
    if verbose:
        print("\n".join(L))
    return "\n".join(L)


def decide(base, *, items=None, debate=None, cost=None, book=None, event=None,
           qty_usd=5000.0, stance=None, now_ms=None, hypotheses=None):
    """④+⑤ 决策层：辩论 -> 交易员 -> 风控官 -> 最终订单（+ 可复现留痕）。

    **单调性（硬约束）**：``final_stance ≤ debate_stance``，且
    ``final_qty ≤ trader_qty ≤ risk_qty_cap``。任何一个环节想放松，都会被
    这里用 ``min_stage_rank`` / ``min qty`` 夹住并计入 ``upgrade_blocked``。

    ``hypotheses``：第 5 个分析师（agent）提出的风险假设。它们会被风控官
    **当作规则执行**（`agent:*`），但仍受同一条单调性约束。
    """
    d = debate or {}
    v = (d.get("verdict") or {})
    st_debate = stance or v.get("stance") or "caution"
    ev = event or {}
    if not ev:
        ev = {"severity": (cost or {}).get("gate_severity"),
              "reason": (cost or {}).get("gate_reason"),
              "source": (cost or {}).get("gate_source")}

    # ---- ① 事件闸门（与辩论层同一层硬约束，这里对最终单再确认一次）----
    st_gate, gate_note = st_debate, "闸门 severity=%s" % ev.get("severity")
    if ev.get("severity") == "block" and st_gate != "stand_down":
        st_gate = "stand_down"
        gate_note = "闸门 block —— 最终立场被压到 stand_down（硬闸门优先于辩论）"

    # ---- ② 交易员：把立场翻译成订单 ----
    t_out = trader(cost, book, st_gate, qty_usd)

    # ---- ③ 风控官：固定规则 + **agent 提出的风险假设** ----
    risk = risk_officer(base, cost=cost, book=book, trader_out=t_out,
                        debate={"verdict": {"stance": st_gate}}, event=ev,
                        now_ms=now_ms, hypotheses=hypotheses)

    # ---- ④ 最终立场（单调不增）与最终规模（逐级取小）----
    st_final, caps = st_gate, []
    if risk["verdict"] == "reject":
        st_final = "stand_down"
        caps.append(("risk_officer", 0.0))
    else:
        caps.append(("risk_officer", risk["qty_out_usd"]))
    caps.append(("trader", float((t_out.get("order") or {}).get("qty_usd") or 0.0)))
    final_qty = min(v for _n, v in caps)
    executable_usd = final_qty          # 下限拦截前的"可执行规模"（如实保留）
    if risk["verdict"] == "caution" and st_final == "proceed":
        st_final = "caution"
    # 名义额下限：规模被压到没有意义的量级时，正确结论是"不做"，
    # 而不是"用 73 USD 去做一笔套利"。**必须写明是哪一级把它压下来的**。
    min_notional_binding = False
    if 0 < final_qty < MIN_NOTIONAL_USD and st_final != "stand_down":
        st_final = "stand_down"
        min_notional_binding = True
        caps.append(("MIN_NOTIONAL_USD=%.0f 下限" % MIN_NOTIONAL_USD, 0.0))
    if final_qty <= 0 and st_final != "stand_down":
        st_final = "stand_down"
    if st_final == "stand_down":
        final_qty = 0.0                 # 不参与 = 规模 0，不能留一个"看似下单"的数

    # 如果有任何环节想把立场放松，在这里被夹住并**如实记账**
    extra = [{"stage": "gate",
              "would_be": st_debate, "clamped_to": st_gate,
              "why": gate_note}] if stage_rank(st_gate) > stage_rank(st_debate) else []
    upgrades = [x for x in extra if stage_rank(x["would_be"]) > stage_rank(x["clamped_to"])]

    # ---- ⑤ 最终订单：在「交易员订单」上叠加风控处置，只做**降级** ----
    order = t_out.get("order")
    final_order = dict(order) if order else None
    if final_order is not None:
        final_order["qty_usd"] = round(final_qty, 2)
        final_order["slices"] = (max(1, int(round(final_qty / SLICE_USD + 0.4999)))
                                 if final_qty > 0 else 0)
        if risk["verdict"] == "caution" and "leg_risk_high" in risk["hits"] \
                and final_order.get("kind") != "taker":
            # 腿风险高 -> 风控官要求吃单（只在成本模型确实给出吃单方案时降级）
            tk = (order.get("all_modes_bp") or {}).get("双腿全吃单")
            if tk is not None:
                final_order["kind"] = "taker"
                final_order["mode"] = "双腿全吃单"
                final_order["cost_bp"] = tk
                final_order["note"] = ("风控官 leg_risk_high 触发：改用双腿全吃单，"
                                       "不承担「只成交一腿」的裸露敞口")
    if st_final == "stand_down" or final_qty < MIN_NOTIONAL_USD:
        final_order = None

    # 最终"为什么不做"必须指向**真正起作用的那一级**
    if risk["verdict"] == "reject":
        why = "风控官一票否决：%s" % risk["reason"]
    elif min_notional_binding:
        why = ("可执行规模被压到 %.0f USD，低于名义额下限 %.0f USD —— "
               "正确结论是不做，而不是拿 %.0f USD 去做一笔套利"
               % (executable_usd, MIN_NOTIONAL_USD, executable_usd))
    elif st_final == "stand_down":
        why = "立场被收紧到 stand_down（%s）%s" % (
            st_gate, ("；%s" % (t_out.get("blocked_by") or ["交易员未给订单"])[0])
            if not t_out.get("order") else "")
    else:
        why = v.get("reason", "辩论与风控均无异议")
    trace = [
        {"stage": "analysts", "detail": "%d 份报告，%d 份有效"
         % (len(items or []), len([i for i in (items or []) if i.get("valid")]))},
        {"stage": "debate", "stance": st_debate,
         "detail": v.get("reason", "（未提供辩论裁决）")},
        {"stage": "gate", "stance": st_gate, "severity": ev.get("severity"),
         "detail": gate_note},
        {"stage": "trader", "stance": st_gate,
         "qty_usd": float((order or {}).get("qty_usd") or 0.0),
         "mode": (order or {}).get("mode"),
         "detail": "；".join(t_out.get("blocked_by") or []) or "订单已生成"},
        {"stage": "risk_officer", "stance": st_final,
         "qty_usd": risk["qty_out_usd"], "verdict": risk["verdict"],
         "detail": risk["reason"]},
        {"stage": "final", "stance": st_final, "qty_usd": round(final_qty, 2),
         "executable_usd": round(executable_usd, 2), "detail": why},
    ]
    monotonic = {
        "stance_non_increasing": stage_rank(st_final) <= stage_rank(st_debate),
        "qty_non_increasing": final_qty <= (float((order or {}).get("qty_usd") or 0.0)
                                            + 1e-9),
        "upgrade_blocked": upgrades,
        "note": "proceed(2) > caution(1) > stand_down(0)；只允许往右走",
    }
    return {
        "base": base,
        "stages": trace,
        "trader": t_out,
        "risk": risk,
        "final": {"stance": st_final, "qty_usd": round(final_qty, 2),
                  "order": final_order, "why": why,
                  "min_notional_binding": min_notional_binding},
        "monotonic": monotonic,
    }


def render_decision(r, verbose=True):
    L = ["  ══════ 最终决策：%s ｜ 规模 %.0f USD"
         % ({"proceed": "可执行", "caution": "谨慎执行",
             "stand_down": "不参与"}.get(r["final"]["stance"], r["final"]["stance"]),
            r["final"]["qty_usd"])]
    for s in r["stages"]:
        L.append("     %-13s stance=%-11s qty=%-9.0f %s"
                 % (s["stage"], s.get("stance", "-"), s.get("qty_usd", 0.0),
                    (s.get("detail") or "")[:60]))
    o = r["final"]["order"]
    if o:
        L.append("     订单: %s ｜ %.0f USD ｜ 拆 %d 笔 ｜ 成本 %+.2f bp ｜ %s"
                 % (o.get("mode"), o.get("qty_usd"), o.get("slices", 0),
                    o.get("cost_bp") or 0.0, o.get("price_desc", "")))
    else:
        L.append("     订单: 无（%s）" % r["final"]["why"])
    L.append("     单调性: 立场不放松 %s ｜ 规模不放大 %s ｜ 被拦下的放松 %d 次"
             % ("✓" if r["monotonic"]["stance_non_increasing"] else "✗",
                "✓" if r["monotonic"]["qty_non_increasing"] else "✗",
                len(r["monotonic"]["upgrade_blocked"])))
    if verbose:
        print("\n".join(L))
    return "\n".join(L)


# ================================================================ ⑤ 可复现辩论日志

LOG_FORMAT = "project2.debate-log/1"

# 哪些字段**必须**逐字节一致（确定性契约），哪些允许漂移（实时盘口）
REPRO_MUST_MATCH = (
    "parameters", "rules_skeleton", "final_stance", "final_qty_usd",
    "final_order_mode", "degrade", "monotonic",
)
REPRO_MAY_DRIFT = ("data_snapshot", "rule_measures", "evidence_values",
                   "final_order_live")

RULES_SKELETON_FIELDS = ("id", "level", "action", "statement", "falsifier")
DECISION_FIELDS = ("kind", "mode", "qty_usd", "slices", "cost_bp",
                   "price_desc", "size_cap", "size_cap_by", "note",
                   "all_modes_bp", "barred_modes")
PARAM_FIELDS = ("base", "qty_usd", "miss_bp", "urgent", "slice_usd",
                "depth_take_ratio", "now_ms", "log_format", "synthetic",
                "scenario",
                # ⭐ LLM 事件判定进契约：硬闸门依赖它，复跑必须拿同一份。
                #    （旧日志里没有这个字段 -> 复跑时会真调一次 LLM，
                #      结果不一致是**如实暴露**，不是 bug；见 docs/46）
                "llm_event",
                # ⏱️ 在途订单**同样是决策输入**（2026-09-20）：执行进度官据此
                #    提出裸露敞口假设 -> 风控一票否决 -> 规则表/否决清单都变。
                #    不记进契约，`--replay` 就会漏掉那条 `agent:` 规则，
                #    复跑报"不一致"而真正原因是输入根本没被记下来。
                "order_state",
                # 📅 **外部确定性事件**（财报/除息）同样是决策输入（2026-09-20）：
                #    它会进硬闸门、能改 severity、能作废挂单类方案。
                #    它是**外部活数据**（靠 `ext_events.py --refresh` 更新），
                #    不冻进契约的话，隔一天复跑就会读到另一份事件表 -> 必然不一致。
                "ext_event",
                # ⚓ **外部锚**（第三方真实美股报价）同理：它进第 6 路分析师的
                #    evidence 与假设、能改风控规则，且是**活数据**
                #    （靠 `mcp_anchor.py --refresh` 更新）—— 不冻就复跑不一致。
                "anchor")


def sha256_file(path):
    if not os.path.exists(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def input_manifest(paths):
    """输入证据表：每个被引用的文件 -> 字节数 / 修改时间 / SHA256。"""
    out = []
    for p in paths:
        full = p if os.path.isabs(p) else os.path.join(BASE, p)
        if not os.path.exists(full):
            out.append({"path": p, "exists": False})
            continue
        st = os.stat(full)
        out.append({"path": p, "exists": True, "bytes": st.st_size,
                    "mtime": dt.datetime.fromtimestamp(st.st_mtime, dt.UTC)
                    .strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "sha256": sha256_file(full)})
    return out


def collect_params(base, *, items, debate, decision, cost, qty_usd, miss_bp,
                   urgent, now_ms=None, book=None, slice_usd=SLICE_USD,
                   depth_take_ratio=DEPTH_TAKE_RATIO):
    scenario = (decision or {}).get("scenario")
    # 🔴 parameters.now_ms 必须是**决策实际使用的时刻**（time_basis 的结果），
    #    不能是墙钟：否则 --replay 用另一个时刻重算，按龄判据（行情停滞/停牌）
    #    会得出不同结论 —— 实测到"停滞 13.6 分钟"与"572.1 分钟"两种结果，
    #    契约字段 rules_skeleton / degrade 直接不一致。
    _tb = (decision or {}).get("time_basis") or {}
    eff_now = _tb.get("now_ms") or now_ms
    # 🔴 llm_event 是**判定内容**，要进契约；但 "frozen" 只是"这份判定是实时拿到的
    #    还是复跑时冻结输入的"，两次运行必然一个 false 一个 true ——
    #    它是**过程信息**，留在 parameters.llm_event 里会让复跑必然不一致。
    #    （展示上仍然照实说：report 里的 event 保留 frozen 标记。）
    le = (decision or {}).get("llm_gate_used")
    if isinstance(le, dict):
        le = {k: v for k, v in le.items() if k != "frozen"}
    # ⏱️ 在途订单（执行进度官的输入）：**规范化后**记录，只留决策真正读的字段。
    _ep = (decision or {}).get("execution_progress")
    os_param = _ep.get("order_state") if isinstance(_ep, dict) else None
    # 📅 外部确定性事件：**只记实际用到的那份**（没有就是 None）。
    #    它进契约的理由与 llm_event 完全相同：外部活数据不冻结，复跑必然不一致。
    ext_param = (decision or {}).get("ext_gate_used")
    # ⚓ 外部锚：同理，只记实际用到的那份
    anchor_param = (decision or {}).get("anchor_used")
    return {
        "log_format": LOG_FORMAT, "base": base, "qty_usd": qty_usd,
        "miss_bp": miss_bp, "urgent": bool(urgent),
        "slice_usd": slice_usd, "depth_take_ratio": depth_take_ratio,
        "now_ms": eff_now,
        # 🔴 合成标记：合成场景的结论**不得**被当成实测结论引用
        "synthetic": bool(scenario),
        "scenario": scenario,
        "scenario_note": (decision or {}).get("scenario_note"),
        "thresholds": {"edge_bp": EDGE_THRESHOLD_BP, "cost_bp": COST_THRESHOLD_BP,
                       "depth_min_usd": DEPTH_MIN_USD,
                       "fee_rt_mm_bp": FEE_RT_MM_BP, "fee_rt_tk_bp": FEE_RT_TK_BP,
                       "debate_margin": DEBATE_MARGIN,
                       "direction_penalty": DIRECTION_PENALTY,
                       "adv_min_bp": ADV_MIN_BP,
                       "min_notional_usd": MIN_NOTIONAL_USD},
        "gate": {"severity": (cost or {}).get("gate_severity"),
                 "source": (cost or {}).get("gate_source"),
                 "reason": (cost or {}).get("gate_reason")},
        # 🔴 LLM 事件判定**是决策契约的一部分**（硬闸门现在依赖它）：
        #    所以必须进日志、并且复跑时**读回来当冻结输入**，否则 LLM 的随机性
        #    会让 `--replay` 的契约字段（最终立场/规模）偶发不一致。
        #    它也可能是 None（无 key / 未启用）——那表示这次没有 LLM 判定参与。
        "llm_event": le,
        # ⏱️ 在途订单（执行进度官的输入）：与 llm_event 同理 —— 它是**决策契约的
        #    一部分**。存的是 `norm_order_state()` 规范化后的最小字段集，复跑读回来
        #    当冻结输入，否则 `agent:partial_fill_naked` 那条规则会凭空消失。
        #    None = 这次没有在途订单（不是"记丢了"）。
        "order_state": os_param,
        # 📅 外部确定性事件（财报/除息）。与 llm_event 同理：它是**决策输入的
        #    一部分**，复跑读回来当冻结输入；None = 这次窗口内没有事件。
        "ext_event": ext_param,
        # ⚓ 外部锚（第三方美股报价）。它是第 6 路分析师的输入，**是决策输入**，
        #    所以必须进契约；None = 这次没接外部锚。
        "anchor": anchor_param,
        "data_used": {
            "analysts": sorted({e["source"] for i in items if i.get("valid")
                                for e in i["report"]["evidence"]}),
            "orderbook_snapshot_ts": (book or {}).get("snapshot_ts"),
            "orderbook_file": ((book or {}).get("paths") or [None])[-1],
        },
    }


def _canon(obj):
    """规范化：浮点收敛到 6 位、字典按键排序 —— 保证哈希只反映内容，不反映顺序。"""
    if isinstance(obj, dict):
        return {k: _canon(obj[k]) for k in sorted(obj)}
    if isinstance(obj, (list, tuple)):
        return [_canon(x) for x in obj]
    if isinstance(obj, bool) or obj is None or isinstance(obj, str):
        return obj
    if isinstance(obj, (int, float)):
        return round(float(obj), 6)
    return str(obj)


def log_for_decision(base, *, items, debate, cost, decision, qty_usd, miss_bp,
                     urgent, book=None):
    """按 **CLI / 接口共用的口径**落一份可复跑日志。

    ⚠️ 存在的唯一理由：`now_ms` 必须取**决策真正用的那个时刻**
    （`decision["time_basis"]["now_ms"]`，读冻结快照时它是 as-of），
    **不能留空**让 `build_log` 退回墙钟 `generated_ms`。

    实测踩到过：CLI 的 `--log` 没传 now_ms -> 日志记墙钟、决策用快照时刻 ->
    `--replay` 直接把决策判成"不可复现"（凭空多出 `agent:stale_quotes`
    一票否决，因为复跑时"最后一笔成交距今"从 12 分钟变成了 6 小时）。
    把这段收进一个函数，CLI 与自检走同一条路，就不会再各写一份、各自漂移。
    """
    return build_log(base=base, items=items, debate=debate, cost=cost,
                     decision=decision, qty_usd=qty_usd, miss_bp=miss_bp,
                     urgent=urgent,
                     now_ms=(decision.get("time_basis") or {}).get("now_ms"),
                     book=book)


def build_log(*, base, items, debate, cost, decision, qty_usd, miss_bp, urgent,
              now_ms=None, book=None, generated_ms=None, paths=None):
    """组装一份**可复跑**的辩论日志（含参数、输入哈希、全链路、决策哈希）。"""
    generated_ms = generated_ms or int(dt.datetime.now(dt.UTC).timestamp() * 1000)
    risk = decision["risk"]
    order = (decision["trader"].get("order") or {})
    final_order = decision["final"].get("order")
    evidence = []
    for i in items:
        rep = i["report"]
        for e in rep["evidence"]:
            evidence.append({"dimension": rep["dimension"],
                             "metric": e["metric"], "value": e["value"],
                             "source": e["source"]})
    for side in (BULL, BEAR):
        for a in (debate.get(side) or {}).get("arguments", []):
            for e in a["evidence"]:
                evidence.append({"dimension": "debate/" + side,
                                 "metric": e["metric"], "value": e["value"],
                                 "source": e["source"]})

    default_paths = ["data/derived/friction_budget.csv",
                     "data/derived/funding_rates.csv",
                     "data/derived/precise_fill_spot_bid.csv",
                     "data/derived/precise_fill_perp_ask.csv",
                     # 联合分布的标定输入也要进清单：改了它，成本结论就会变
                     "data/derived/joint_fill_all_in_house.csv",
                     "data/derived/joint_fill_all_stockroute.csv",
                     "data/derived/joint_fill_all.csv",
                     "project2/agent_team.py", "project2/execution_cost.py",
                     "project2/event_gate.py"]
    # ⚠️ 合成场景**没有读盘口**，就别把 orderbook 写进输入清单 ——
    #    否则读者会以为这份日志基于真实盘口。
    if book and book.get("paths") and not (decision or {}).get("scenario"):
        default_paths = list(book["paths"]) + default_paths
    log = {
        "format": LOG_FORMAT,
        "generated_ms": generated_ms,
        "base": base,
        "parameters": collect_params(base, items=items, debate=debate,
                                     decision=decision, cost=cost, qty_usd=qty_usd,
                                     miss_bp=miss_bp, urgent=urgent,
                                     # ⚠️ 这里原来传的是 `now_ms or generated_ms`
                                     #    （= **墙钟**），但决策实际用的是
                                     #    `time_basis()` 定出来的**基准时刻**（读冻结快照
                                     #    时 = 数据自带时刻）。两者能差好几个小时，
                                     #    于是 `--replay` 会用一个**不同的时刻**重算：
                                     #    "行情停滞/停牌"这类**按龄判据**得出不同结论，
                                     #    契约字段（rules_skeleton / degrade）直接不一致。
                                     #    实测踩到：现货腿停滞 13.6 分钟 vs 572.1 分钟。
                                     #    传给 collect_params 的 now_ms 只作兜底，
                                     #    它内部优先取 decision.time_basis 的实际时刻。
                                     now_ms=now_ms, book=book),
        "input_manifest": input_manifest(paths or default_paths),
        "data_snapshot": {
            "orderbook_snapshot_ts": (book or {}).get("snapshot_ts"),
            "mid": (book or {}).get("mid"),
            "depth_usd": (book or {}).get("depth_usd"),
            "cost": {k: (cost or {}).get(k) for k in (
                "best_mode", "best_cost", "cost_mm", "cost_mix", "cost_tk",
                "p_s", "p_p", "p_part", "adv_s", "adv_p", "spread_s", "spread_p",
                "gate_severity", "maker_allowed",
                # 联合分布的出处与三格概率：成本结论的可复现性依赖它们
                "joint_source", "joint_prov", "p_both", "p_none",
                "p_both_indep_pertrade")},
        },
        "analysts": [{"dimension": i["report"]["dimension"],
                      "verdict": i["report"]["verdict"],
                      "confidence": i["report"]["confidence"],
                      "valid": i["valid"],
                      "invalid_reason": i["invalid_reason"],
                      "evidence": i["report"]["evidence"],
                      "notes": i["report"]["notes"]} for i in items],
        "evidence_index": evidence,
        "debate": {
            "bull": {"weight": debate[BULL]["weight"],
                     "arguments": debate[BULL]["arguments"],
                     "concessions": debate[BULL]["concessions"],
                     "dropped": debate[BULL]["dropped"],
                     "direction_conflicts": debate[BULL]["direction_conflicts"]},
            "bear": {"weight": debate[BEAR]["weight"],
                     "arguments": debate[BEAR]["arguments"],
                     "concessions": debate[BEAR]["concessions"],
                     "dropped": debate[BEAR]["dropped"],
                     "direction_conflicts": debate[BEAR]["direction_conflicts"]},
            "cross_examination": debate["cross"],
            "verdict": debate["verdict"],
            "excluded_reports": debate["excluded_reports"],
        },
        "trader": {"order": order,
                   "blocked_by": decision["trader"].get("blocked_by"),
                   "gross_edge_bp": decision["trader"].get("gross_edge_bp"),
                   "edge_gap_bp": decision["trader"].get("edge_gap_bp")},
        "risk_officer": {"verdict": risk["verdict"], "reason": risk["reason"],
                         "hits": risk["hits"], "vetoes": risk["vetoes"],
                         "cautions": risk["cautions"],
                         "qty_in_usd": risk["qty_in_usd"],
                         "qty_out_usd": risk["qty_out_usd"],
                         "checked_rules": risk["checked_rules"],
                         "triggered_rules": risk["triggered_rules"],
                         # ⭐ agent 参与度留痕：审计"agent 的话有没有真的起作用"
                         "agent_hypotheses": risk.get("agent_hypotheses", 0),
                         "agent_rules": risk.get("agent_rules") or [],
                         "agent_driven": bool(risk.get("agent_driven")),
                         "rules": [{"id": r["id"], "level": r["level"],
                                    "action": r["action"],
                                    "statement": r["statement"],
                                    "measured": r["measured"],
                                    "falsifier": r["falsifier"], "hit": r["hit"],
                                    "qty_cap": r.get("qty_cap"),
                                    "remedy_note": r.get("remedy_note")}
                                   for r in risk["rules"]]},
        "decision": {"stages": decision["stages"],
                     "final_stance": decision["final"]["stance"],
                     "final_qty_usd": decision["final"]["qty_usd"],
                     "final_order": final_order,
                     "why": decision["final"]["why"],
                     "monotonic": decision["monotonic"]},
    }
    log["rules_skeleton"] = rules_skeleton(log)
    body = {k: v for k, v in log.items() if k not in ("decision_hash",)}
    log["decision_hash"] = hashlib.sha256(
        json.dumps(_canon(body), ensure_ascii=False,
                   sort_keys=True).encode("utf-8")).hexdigest()
    return log


def rules_skeleton(log):
    """规则骨架：只保留规则的身份+阈值+处置，**不含实测值**（实测值允许漂移）。"""
    return [{"id": r["id"], "level": r["level"], "action": r["action"],
             "statement": r["statement"], "falsifier": r["falsifier"]}
            for r in log.get("risk_officer", {}).get("rules", [])]


def repro_projection(log):
    """把日志压成**可比对投影**：确定性契约字段 + 允许漂移的实测值分开列。"""
    dec = log.get("decision", {})
    risk = log.get("risk_officer", {})
    fo = dec.get("final_order") or {}
    must = {
        "parameters": {k: (log.get("parameters") or {}).get(k)
                       for k in PARAM_FIELDS},
        "rules_skeleton": rules_skeleton(log),
        "final_stance": dec.get("final_stance"),
        "final_qty_usd": round(float(dec.get("final_qty_usd") or 0.0), 2),
        "final_order_mode": fo.get("mode"),
        "degrade": {"risk_verdict": risk.get("verdict"),
                    "hits": sorted(risk.get("hits") or []),
                    "vetoes": sorted(risk.get("vetoes") or []),
                    "order_present": bool(fo)},
        "monotonic": dec.get("monotonic"),
    }
    drift = {
        "data_snapshot": log.get("data_snapshot"),
        "rule_measures": {r["id"]: {"hit": r["hit"], "measured": r["measured"]}
                          for r in risk.get("rules", [])},
        "evidence_values": sorted({(e["dimension"], e["metric"], e["value"])
                                   for e in log.get("evidence_index", [])}),
        # 订单里的成本/规模上界直接来自实时点差与深度，会随时间漂移
        "final_order_live": {k: fo.get(k) for k in
                             ("qty_usd", "cost_bp", "all_modes_bp", "size_cap",
                              "price_desc", "note")} if fo else None,
    }
    return must, drift


def repro_hash(log):
    """复跑出来的决策哈希。与日志里的 decision_hash 用同一套规范化。"""
    body = {k: v for k, v in log.items() if k not in ("decision_hash",)}
    return hashlib.sha256(json.dumps(_canon(body), ensure_ascii=False,
                                     sort_keys=True).encode("utf-8")).hexdigest()


def hash_selfcheck(log):
    """**自校验**：日志内容重新算哈希，跟它自己记录的 decision_hash 比。

    这一步不能省 —— 没有它，"记录哈希"和"复跑哈希"可能拿的是同一个**没被
    重算过**的字段，于是篡改过的日志照样显示"一致"（实测踩到过）。
    """
    recorded = log.get("decision_hash")
    recomputed = repro_hash(log)
    return recorded == recomputed, recorded, recomputed


def replay_check(old, new):
    """比对「记录的日志」与「现在重跑的日志」。返回 (ok, report)。

    **契约字段必须逐字节一致**（参数 / 规则表 / 立场 / 规模 / 降级路径 / 单调性）；
    盘口、点差、成本这类**实时量允许漂移**，但要单独列出来，不许混在一起说"通过"。
    """
    om, od = repro_projection(old)
    nm, nd = repro_projection(new)
    diffs = [k for k in REPRO_MUST_MATCH if om.get(k) != nm.get(k)]
    drift = [k for k in REPRO_MAY_DRIFT if od.get(k) != nd.get(k)]
    ok_old, rec_old, calc_old = hash_selfcheck(old)
    ok_new, rec_new, calc_new = hash_selfcheck(new)
    ok = (not diffs) and ok_old
    rep = {
        "ok": ok, "base": old.get("base"),
        "recorded_hash": old.get("decision_hash"),
        "replayed_hash": new.get("decision_hash"),
        "hash_identical": old.get("decision_hash") == new.get("decision_hash"),
        # 两侧日志**各自**与其记录哈希是否自洽（防"篡改后仍显示一致"）
        "integrity_recorded": ok_old, "integrity_replayed": ok_new,
        "recomputed_recorded": calc_old, "recomputed_replayed": calc_new,
        "must_match_failed": diffs,
        "drifted": sorted(set(drift)),
        "old": om, "new": nm,
        "old_generated_ms": old.get("generated_ms"),
        "new_generated_ms": new.get("generated_ms"),
    }
    if not ok_old:
        rep.setdefault("detail", []).append(
            {"field": "integrity", "recorded": rec_old[:16] + "…",
             "replayed": calc_old[:16] + "…"})
    if diffs:
        rep.setdefault("detail", []).extend([
            {"field": k, "recorded": _short(om.get(k)), "replayed": _short(nm.get(k))}
            for k in diffs])
    return ok, rep


def render_replay(rep, old_path=None):
    L = ["=" * 92]
    L.append("复跑校验：%s" % (old_path or rep.get("base")))
    L.append("=" * 92)
    L.append("  记录决策哈希: %s" % rep["recorded_hash"])
    L.append("  复跑决策哈希: %s  %s" % (rep["replayed_hash"],
                                     "（一致）" if rep["hash_identical"]
                                     else "（不同 —— 见下方契约/漂移两类）"))
    if not rep.get("integrity_recorded", True):
        L.append("  🔴 诚信校验**失败**：日志内容与它自己记录的哈希不符 —— "
                 "文件被改动过（%.16s… vs 重算 %.16s…）"
                 % (rep["recorded_hash"] or "", rep["recomputed_recorded"] or ""))
    else:
        L.append("  ✓ 诚信校验通过：日志内容与其记录的哈希自洽（未被改动）")
    if not rep["hash_identical"]:
        L.append("  ℹ️ 两个哈希不同是**预期**的：哈希覆盖了生成时间与输入文件指纹，"
                 "复跑必然产生新的时间戳；")
        L.append("     真正要比的是下面的「契约字段」。")
    L.append("")
    L.append("  ① 契约字段（**必须一致**）:")
    if rep["must_match_failed"]:
        for d in rep.get("detail", []):
            L.append("     ✗ %s" % d["field"])
            L.append("         记录: %s" % d["recorded"])
            L.append("         复跑: %s" % d["replayed"])
    else:
        L.append("     ✓ 参数 / 规则表 / 最终立场 / 最终规模 / 订单方式 / "
                 "风控降级路径 / 单调性 —— 全部一致")
    L.append("")
    L.append("  ② 实时量（允许漂移，如实列出）:")
    if rep["drifted"]:
        for k in rep["drifted"]:
            L.append("     ~ %s 已变化（盘口/点差是实时采样，采样器在持续写入）" % k)
    else:
        L.append("     ~ 本次没有任何漂移（盘口未更新）")
    L.append("")
    if rep["ok"]:
        L.append("  ==> 复跑**通过**：决策路径可复现"
                 + ("（实时量已漂移，属预期）" if rep["drifted"] else ""))
    else:
        L.append("  ==> 复跑**失败**：契约字段不一致 —— 说明代码或参数变了，"
                 "不可当作同一次决策")
    txt = "\n".join(L)
    print(txt)
    return txt


def _short(x, n=220):
    s = json.dumps(x, ensure_ascii=False, sort_keys=True)
    return s if len(s) <= n else s[:n] + "…"


def render_log_md(log, md_name=None):
    """人类可读版：阶段化 + 每条结论都带来源。"""
    p = log["parameters"]
    dec = log["decision"]
    risk_log = log["risk_officer"]
    L = []
    A = L.append
    A("# 多 Agent 决策日志（可复跑）")
    A("")
    A("| 项 | 值 |")
    A("|---|---|")
    A("| 标的 | `%s` |" % log["base"])
    A("| 生成时间(UTC) | %s |" % _ts(log["generated_ms"]))
    A("| 决策哈希 | `%s` |" % log["decision_hash"])
    A("| 复跑参数 | qty=%.0f USD ｜ miss=%.2f bp ｜ urgent=%s ｜ slice=%.0f USD "
      "｜ depth_take=%.2f |" % (p["qty_usd"], p["miss_bp"], p["urgent"],
                                p["slice_usd"], p["depth_take_ratio"]))
    A("| 事件闸门 | severity=`%s` ｜ source=`%s` |"
      % (p["gate"]["severity"], p["gate"]["source"]))
    A("| 最终决策 | **%s** ｜ %.0f USD |"
      % (dec["final_stance"], dec["final_qty_usd"]))
    if p.get("synthetic"):
        A("| 🔶 数据来源 | **合成场景 `%s`**（%s）—— 非实测盘口，结论不得当作实测结论 |"
          % (p.get("scenario"), p.get("scenario_note") or ""))
    A("")
    if md_name:
        A("> 复跑：`python project2\\agent_team.py --replay data\\reports\\%s`"
          % (md_name.replace(".md", ".json")))
        A("")
    A("## 0. 铁律（本日志的自我约束）")
    A("")
    A("1. **没有引用已实测的量的结论一律作废**（分析师层强制）。")
    A("2. **给不出证伪条件的论点一律作废**（辩论层强制）。")
    A("3. 交易员与风控官**只能收紧，不能放松**（`monotonic` 已校验并留痕）。")
    A("4. basis_bp / 成本 / 闸门判定等量化基线**不因 agent 而变**。")
    A("")
    A("## 1. 输入证据（可核对 SHA256）")
    A("")
    A("| 文件 | 字节 | 修改时间(UTC) | SHA256(前 16) |")
    A("|---|---|---|---|")
    for m in log["input_manifest"]:
        if not m.get("exists"):
            A("| `%s` | — | — | **缺失** |" % m["path"])
        else:
            A("| `%s` | %s | %s | `%s` |"
              % (m["path"], format(m["bytes"], ","), m["mtime"],
                 (m["sha256"] or "")[:16]))
    A("")
    snap = log["data_snapshot"]
    A("盘口快照 ts_ms=`%s`（%s）" % (snap.get("orderbook_snapshot_ts"),
                                      _ts(snap.get("orderbook_snapshot_ts"))))
    A("")
    A("## 2. ① 分析师层（4 维度独立）")
    A("")
    for a in log["analysts"]:
        tag = {"favorable": "有利", "unfavorable": "不利",
               "neutral": "中性"}.get(a["verdict"], a["verdict"])
        A("### %s ｜ %s ｜ 置信度 %.2f%s" % (a["dimension"], tag, a["confidence"],
                                            "" if a["valid"] else " ｜ **无效**"))
        for e in a["evidence"]:
            A("- `%s` = %s  ← `%s`" % (e["metric"], e["value"], e["source"]))
        if a.get("notes"):
            A("")
            A("> %s" % a["notes"])
        if not a["valid"]:
            A("")
            A("> ✗ 无效：%s" % a["invalid_reason"])
        A("")
    A("## 3. ② 多空辩论层")
    A("")
    for side, tag in ((BULL, "多头"), (BEAR, "空头")):
        s = log["debate"][side]
        A("### %s ｜ 得分 %.2f ｜ 论点 %d 条 ｜ 被没收 %d 条"
          % (tag, s["weight"], len(s["arguments"]), len(s["dropped"])))
        for a in s["arguments"]:
            A("- **%s**" % a["claim"])
            for e in a["evidence"]:
                A("  - 证据：`%s` = %s ← `%s`" % (e["metric"], e["value"], e["source"]))
            A("  - 证伪：%s" % a["falsifier"])
        for c in s["concessions"]:
            A("- 让步：承认 %s 的 `%s` = %s（置信度 %.2f）"
              % (c["dimension"], c["metric"], c["value"], c["confidence"]))
        for d in s["dropped"]:
            A("- 被没收：`%s` = %s —— %s" % (d["metric"], d["value"], d["reason"]))
        A("")
    v = log["debate"]["verdict"]
    A("**裁决**：`%s`（原始倾向 `%s`）｜ 多头 %.2f ｜ 空头 %.2f ｜ %s"
      % (v["stance"], v["base_stance"], v["bull_weight"], v["bear_weight"],
         v["reason"]))
    if v.get("cap_reason"):
        A("")
        A("> ⚠️ %s" % v["cap_reason"])
    A("")
    A("## 4. ③ 交易员（执行成本模型）")
    A("")
    t = log["trader"]
    if t.get("order"):
        o = t["order"]
        A("- 方式：**%s** ｜ 规模 %.0f USD ｜ 拆 %d 笔 × %.0f USD"
          % (o["mode"], o["qty_usd"], o["slices"], o["slice_usd"]))
        A("- 价位：%s" % o["price_desc"])
        A("- 成本：%+.2f bp ｜ 三方案：全挂单 %+.2f ｜ 混合 %+.2f ｜ 全吃单 %+.2f"
          % (o["cost_bp"], o["all_modes_bp"]["双腿全挂单"],
             o["all_modes_bp"]["现货挂单+永续吃单"], o["all_modes_bp"]["双腿全吃单"]))
        A("- 规模上界：%.0f USD ← %s" % (o["size_cap"], o["size_cap_by"]))
        for b in o["size_bounds"]:
            A("  - %s → %.0f USD" % (b["name"], b["usd"]))
    else:
        A("- **不下单**")
    for b in (t.get("blocked_by") or []):
        A("- [!] %s" % b)
    A("")
    A("## 5. ④ 风控官（%d 条规则逐条留痕，%d 条触发）"
      % (risk_log["checked_rules"], len(risk_log["hits"])))
    A("")
    A("| 规则 | 级别 | 触发 | 实测 | 撤销条件 |")
    A("|---|---|---|---|---|")
    for r in risk_log["rules"]:
        A("| `%s` | %s | %s | %s | %s |"
          % (r["id"], r["level"], "**是**" if r["hit"] else "否",
             r["measured"].replace("|", "/"), r["falsifier"]))
    A("")
    A("**风控结论**：`%s`（%s）｜ 规模 %.0f → %.0f USD"
      % (risk_log["verdict"], risk_log["reason"],
         risk_log["qty_in_usd"], risk_log["qty_out_usd"]))
    A("")
    A("## 6. ⑤ 最终决策")
    A("")
    A("| 阶段 | 立场 | 规模(USD) | 说明 |")
    A("|---|---|---|---|")
    for s in dec["stages"]:
        A("| %s | `%s` | %.0f | %s |"
          % (s["stage"], s.get("stance", "-"), s.get("qty_usd", 0.0),
             (s.get("detail") or "").replace("|", "/")[:120]))
    A("")
    A("**最终**：`%s` ｜ %.0f USD ｜ %s"
      % (dec["final_stance"], dec["final_qty_usd"], dec["why"]))
    fo = dec.get("final_order")
    if fo:
        A("")
        A("订单：`%s` ｜ %.0f USD ｜ 拆 %d 笔 ｜ 成本 %+.2f bp ｜ %s"
          % (fo.get("mode"), fo.get("qty_usd"), fo.get("slices", 0),
             fo.get("cost_bp") or 0.0, fo.get("price_desc")))
    A("")
    A("**单调性**：立场不放松 %s ｜ 规模不放大 %s ｜ 被拦下的放松 %d 次"
      % ("✓" if dec["monotonic"]["stance_non_increasing"] else "✗",
         "✓" if dec["monotonic"]["qty_non_increasing"] else "✗",
         len(dec["monotonic"]["upgrade_blocked"])))
    A("")
    A("## 7. 诚实边界")
    A("")
    A("1. 本日志记录的是**一次决策的全链路**，不是「agent 数量」的展示。")
    A("2. 盘口与点差是**实时采样**：标的、参数、规则与决策路径可复跑；")
    A("   盘口数值会随时间漂移 —— `--replay` 会把「必须一致」与「允许漂移」分开报。")
    A("3. 很多论点会被「没有已实测的证伪条件」没收，这是**如实**，不是缺陷。")
    A("")
    return "\n".join(L) + "\n"


def _ts(ms):
    if not ms:
        return "—"
    return dt.datetime.fromtimestamp(int(ms) / 1000.0, dt.UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def write_log(log, outdir=None, md=True, tag=None):
    """落盘：`debate-<base>-<UTC时间戳>[-synthetic-<场景>].json` + 同名 `.md`。

    文件名里带 `synthetic-<场景>` —— 合成日志与实测日志**绝不重名**，
    免得一份演示用的合成日志被误当成实测证据。
    """
    outdir = outdir or os.path.join(BASE, "data", "reports")
    os.makedirs(outdir, exist_ok=True)
    pr = log.get("parameters") or {}
    sc = pr.get("scenario")
    name = "debate-%s-%s%s%s" % (
        log["base"],
        _ts(log["generated_ms"]).replace(":", "").replace("-", ""),
        ("-synthetic-%s" % sc) if sc else "",
        ("-" + tag) if tag else "")
    jp = os.path.join(outdir, name + ".json")
    with open(jp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(log, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    mp = None
    if md:
        mp = os.path.join(outdir, name + ".md")
        with open(mp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(render_log_md(log, md_name=os.path.basename(mp)))
    return jp, mp


# ---------------------------------------------------------------- 自检

def selftest():
    ok = True

    def chk(cond, msg):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s" % ("OK " if cond else "!! ", msg))

    base = "NVDA"
    items, _hyps, _dropped = run_team(base)

    # ⚠️ `DIMENSIONS` 里既有**常跑**的路，也有**条件跑**的路：
    #    `external_anchor` 要有外部锚数据、`execution_progress` 要有在途订单。
    #    `run_team` 只跑常跑的那几路，条件路分别在 run_decision 里挂上去。
    #    写死 `len(items) == len(DIMENSIONS)` 会在新增条件路的那一刻变成假失败
    #    （这个坑本轮踩了两次：先被 execution_progress、再被 external_anchor）。
    _COND = ("external_anchor", "execution_progress")
    _PRE = [d for d in DIMENSIONS if d not in _COND]
    chk(len(items) == len(_PRE)
        and {i["report"]["dimension"] for i in items} == set(_PRE),
        "常跑的 %d 路分析师都返回了结果（%d 个：%s）；"
        "%s 是**条件跑**的（分别要有外部锚数据 / 在途订单）"
        % (len(_PRE), len(items),
           "、".join(sorted(i["report"]["dimension"] for i in items)),
           "、".join(_COND)))
    chk(all(i["report"]["dimension"] in DIMENSIONS for i in items),
        "dimension 取值合法")
    chk(all(i["report"]["verdict"] in VERDICTS for i in items),
        "verdict 取值合法")
    chk(all(0.0 <= i["report"]["confidence"] <= 1.0 for i in items),
        "confidence 在 [0,1]")

    # 🔴 铁律：没有证据的结论必须被判无效
    bad = report("basis", "favorable", 0.9, [], "我强烈认为可以")
    v, why = validate(bad)
    chk(not v and "没有引用" in why, "**空证据的结论被判无效**（防「换三个说法」）")

    # 缺 source 的证据要被过滤掉
    r2 = report("basis", "favorable", 0.9,
                [{"metric": "x", "value": "1"}], "缺 source")
    chk(not r2["evidence"], "缺 source 的证据被过滤")

    # 至少要有分析师真的产出了证据（而不是四个都空转）
    with_ev = [i for i in items if i["valid"]]
    chk(len(with_ev) >= 3, "至少 3 个分析师给出了有效证据（实际 %d）" % len(with_ev))

    # 独立性：四个维度的证据来源不应完全相同
    srcs = [tuple(i["report"]["sources"]) for i in items if i["valid"]]
    chk(len(set(srcs)) >= 3, "各路证据来源不完全重合（独立性）")

    print()
    render_team(base, items)
    print("\n分析师层自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


# ------------------------------------------------- 单次决策（CLI 与复跑共用）

# 严重度排序：只允许往更保守的方向合并（与全项目"只收紧不放松"一致）
SEVERITY_RANK = {"none": 0, "caution": 1, "block": 2}
SEVERITY_ORDER = ("none", "caution", "block")


def merge_gate(static_gate, llm_event, ext_event=None):
    """把**三个事件源**合并成**唯一一个硬闸门**。

    三源：
      ① ``static_gate`` 确定性日历（期权到期 / 休市 / in_house 窗口）
      ② ``llm_event``   LLM 判定（读新闻标题）
      ③ ``ext_event``   📅 **外部确定性事件**（官方 MCP：财报日历 / 除息）
                        —— 2026-09-20 新增。它答的是前两源都答不了的**确定**问题：
                        "下次财报是哪天""除息日是哪天"（除息会让现货腿按股息下跳、
                        永续腿不跳 -> 基差跳变，而 maker 策略正好靠基差吃饭）。
                        实测接入当天就抓到 **META 除息日 = 当天**，此前链路一无所知。

    🔴 为什么必须合并（2026-09-19 修的第二处"说了没做"）
    ---------------------------------------------------
    文档写的是「severity=block → **挂单类方案直接作废**（硬规则，agent 不能推翻）」，
    但实现里硬闸门走的是：

        run_decision -> execution_cost.consult_gate() -> event_gate.static_gate()

    也就是**只读确定性日历、从不问 LLM**；LLM 的判定只到了辩论层（变成一条证据）。
    后果有两条：
      ① 安全上：LLM 判出 block（例如刚披露的 8-K）时，**挂单类方案照样生成**——
         而"挡不住突发新闻与财报"恰恰是我们自己写在局限里的最大缺口；
      ② 观感上：把 key 配上之后页面会自相矛盾——闸门区块写"本次未使用 LLM"，
         而它上面的新闻分析师写"✅ 本次由 LLM 判事件"。

    实测证据（把 `assess` 打桩成必定返回 block，跑全部 10 个标的）：
        news 分析师收到 block：True
        硬闸门收到 block     ：False（cost['gate_severity']='none'、maker_allowed=True）

    ⚠️ 当时**没有改变任何结论**：那 10 个标的都因别的原因（现货腿停滞等）已经
    `stand_down`。所以它是**潜伏**缺陷 —— 等现货腿恢复成交、策略真正可执行时才会咬人，
    而那正是最需要事件闸门的时候。

    合并规则（**只取更保守的一侧**）：
      * 三源取严重度最高的那个；
      * 谁更严就让谁当 `source`，并在 reason 里写明其它源说了什么 —— 不藏；
      * 返回 (severity, reason, source, maker_allowed)。

    ⚠️ 调用方必须同时把结果写回 `cost`（见 `apply_gate_to_cost`），
       否则会出现"闸门说 block、可 cost 里最优方式仍是挂单"的自相矛盾。
    """
    sev, why, src, maker = _merge_two(static_gate, llm_event)
    if not ext_event:
        return sev, why, src, maker
    e_sev = ext_event.get("severity") or "none"
    e_why = (ext_event.get("reason") or "")[:80]
    e_src = ext_event.get("source") or "ext"
    tag_e = "外部确定性事件 %s（%s）" % (e_sev, e_why)
    r_e, r_cur = SEVERITY_RANK.get(e_sev, 1), SEVERITY_RANK.get(sev, 1)
    # ⚠️ 外部事件胜出时来源会变成 `mcp:...`，**前缀不再是 llm** ——
    #    而页面/日志要靠来源判断"这次有没有用 LLM"。所以这里把 LLM 的**参与**
    #    显式拼进来源（`mcp:+llm`），否则会出现"底部说没用 LLM、② 行却列着 LLM 判定"
    #    的自相矛盾（实测踩到）。**参与过就必须体现出来**。
    _ev = llm_event or {}
    _part = (bool(_ev) and str(_ev.get("source") or "").startswith("llm"))
    _tag = ("+llm" if _part else "") + ("(frozen)" if _ev.get("frozen") else "")
    if r_e > r_cur:
        return (e_sev, "%s｜%s（更宽松，不采信）" % (tag_e, why),
                e_src + _tag, e_sev != "block")
    if r_cur > r_e:
        return sev, "%s｜%s（更宽松，不采信）" % (why, tag_e), src, maker
    if sev == "none" and e_sev == "none":
        return sev, why, src, maker
    return sev, "%s｜%s（同级，一致）" % (why, tag_e), src, maker


def _merge_two(static_gate, llm_event):
    """把**确定性日历闸门**与**LLM 事件判定**合并成**唯一一个硬闸门**。

    🔴 为什么必须合并（2026-09-19 修的第二处"说了没做"）
    ---------------------------------------------------
    文档写的是「severity=block → **挂单类方案直接作废**（硬规则，agent 不能推翻）」，
    但实现里硬闸门走的是：

        run_decision -> execution_cost.consult_gate() -> event_gate.static_gate()

    也就是**只读确定性日历、从不问 LLM**；LLM 的判定只到了辩论层（变成一条证据）。
    后果有两条：
      ① 安全上：LLM 判出 block（例如刚披露的 8-K）时，**挂单类方案照样生成**——
         而"挡不住突发新闻与财报"恰恰是我们自己写在局限里的最大缺口；
      ② 观感上：把 key 配上之后页面会自相矛盾——闸门区块写"本次未使用 LLM"，
         而它上面的新闻分析师写"✅ 本次由 LLM 判事件"。

    实测证据（把 `assess` 打桩成必定返回 block，跑全部 10 个标的）：
        news 分析师收到 block：True
        硬闸门收到 block     ：False（cost['gate_severity']='none'、maker_allowed=True）

    ⚠️ 当时**没有改变任何结论**：那 10 个标的都因别的原因（现货腿停滞等）已经
    `stand_down`。所以它是**潜伏**缺陷 —— 等现货腿恢复成交、策略真正可执行时才会咬人，
    而那正是最需要事件闸门的时候。

    合并规则（**只取更保守的一侧**）：
      * 两侧取严重度更高的那个；
      * 谁更严就让谁当 `source`，并在 reason 里写明另一侧说了什么 —— 不藏；
      * 返回 (severity, reason, source, maker_allowed)。

    ⚠️ 调用方必须同时把结果写回 `cost`（见 `apply_gate_to_cost`），
       否则会出现"闸门说 block、可 cost 里最优方式仍是挂单"的自相矛盾。
    """
def _merge_two(static_gate, llm_event):
    """两个源的合并（确定性日历 + LLM）。三源版本见 `merge_gate`。

    单独拆出来是为了**不改动已有语义** —— 这一段的 `llm+static` 来源标记、
    `(frozen)` 标记、以及"同级一致"的措辞都被文档与自检引用着。
    第三源（外部确定性事件）在 `merge_gate` 里**叠在它之上**，
    这样 ext_event=None 时行为与从前逐字节一致。
    """
    sev_s, why_s, src_s = (static_gate or ("none", "", "static"))[:3]
    ev = llm_event or {}
    sev_l = ev.get("severity") or "none"
    src_l = ev.get("source") or "?"
    # LLM 这次到底**参与**了没有？这决定了 source 怎么写 ——
    # ⚠️ 不能只看"谁更严"：LLM 判了 none 且与日历同级时，如果 source 仍写 "static"，
    #    页面就会出现自相矛盾的画面：闸门区块说"本次未使用 LLM"，
    #    而它上面的新闻分析师说"✅ 本次由 LLM 判事件"（实测踩到）。
    #    参与过就必须体现出来：`llm+static` 这类来源以 "llm" 开头，页面据此显示"用了 LLM"。
    participated = bool(ev) and str(src_l).startswith("llm")
    frozen_tag = "(frozen)" if ev.get("frozen") else ""
    tag_l = "LLM 判定 %s（%s）" % (sev_l, (ev.get("reason") or "")[:60])
    r_s = SEVERITY_RANK.get(sev_s, 1)
    r_l = SEVERITY_RANK.get(sev_l, 1)

    if r_l > r_s:
        # LLM 更严 -> 以它为准（这就是本次修的核心：block 真的能作废挂单）
        return (sev_l, "%s｜确定性日历=%s（更宽松，不采信）" % (tag_l, sev_s),
                "llm" + frozen_tag, sev_l != "block")
    if r_s > r_l:
        # 日历更严：仍如实标注 LLM 参与过，只是这次它更宽松
        return (sev_s, "%s｜%s（更宽松，不采信）" % (why_s, tag_l),
                (("llm+static" + frozen_tag) if participated else src_s),
                sev_s != "block")
    # 同级：两边一致（或都没判定）
    if sev_s == "none" and sev_l == "none":
        return (sev_s, why_s, (("llm+static" + frozen_tag) if participated
                               else src_s), True)
    return (sev_s, "%s｜%s（同级，一致）" % (why_s, tag_l),
            (("llm+static" + frozen_tag) if participated else src_s),
            sev_s != "block")


def apply_gate_to_cost(cost, gate):
    """把**合并后的闸门**写回 `cost` —— trader / 风控官读的就是这几个字段。

    复刻 `execution_cost.analyse_two_leg()` 里闸门生效时的**同一套**处理
    （挂单类方案直接作废、并从候选里剔除），否则会出现自相矛盾：
    闸门字段说 block，而 `best_mode` 还是"双腿全挂单"、`maker_allowed` 还是 True。
    """
    if not isinstance(cost, dict):
        return cost
    sev, reason, src, maker_allowed = gate
    cost["gate_severity"] = sev
    cost["gate_reason"] = reason
    cost["gate_source"] = src
    cost["maker_allowed"] = bool(maker_allowed)
    if not maker_allowed:
        rows = [("双腿全挂单", cost.get("cost_mm")),
                ("现货挂单+永续吃单", cost.get("cost_mix")),
                ("双腿全吃单", cost.get("cost_tk"))]
        cost["invalidated"] = [n for n, _c in rows if n != "双腿全吃单"]
        if cost.get("cost_tk") is not None:
            cost["best_mode"], cost["best_cost"] = "双腿全吃单", cost["cost_tk"]
    return cost


def run_decision(base, *, qty_usd=5000.0, miss_bp=None, urgent=False,
                 now_ms=None, gate=True, scenario=None, fresh=True,
                 freeze_news=False, time_basis_force=None, llm_event=None,
                 order_state=None, ext_event=None, anchor=None):
    """端到端跑一次：分析师 -> 辩论 -> 闸门 -> 交易员 -> 风控官 -> 最终决策。

    返回 ``(cost, items, debate, decision, book)``。
    复跑与实跑**共用这一条路径** —— 否则"可复现"就是两套代码在自说自话。

    ``fresh``：是否强制重读盘口（默认 True）。复跑**必须**是新采一次，
    否则拿到的只是同一个进程里的缓存，等于没复跑。

    ``scenario`` 非空时，**成本与盘口用合成的、并在日志里标注 `synthetic`**；
    分析师与辩论仍走真实数据（它们本来就不依赖盘口）。

    ``now_ms`` / ``time_basis_force``：决策基准时间。不传时由 `time_basis()`
    **自动判定**并在结果里如实标注 —— 读冻结快照时按数据自带时刻，实时时按墙钟。
    见 `time_basis()` 的注释（这是"离线演示会随时间衰减且不可复现"的修正）。
    """
    # 🔴 先定"现在几点"，并把**依据**一路带下去（日志与页面都要能说明白）
    tb = time_basis(now_ms, force=time_basis_force)
    now_ms = tb["now_ms"]
    try:
        from execution_cost import (analyse_two_leg as _atl, consult_gate as _cg,
                                    DEFAULT_MISS_BP as _dmb)
    except ImportError:
        from project2.execution_cost import (analyse_two_leg as _atl,
                                             consult_gate as _cg,
                                             DEFAULT_MISS_BP as _dmb)
    if fresh:
        _BOOK_CACHE.clear()
    miss = _dmb if miss_bp is None else float(miss_bp)
    sc_note = None
    if scenario:
        cost, book, sc = make_scenario(scenario)
        if sc is None:
            raise ValueError("未知场景：%r（可选 %s）"
                             % (scenario, "、".join(sorted(SCENARIOS))))
        cost["miss"] = miss
        sc_note = sc["note"]
    else:
        cost = _atl(base, qty_usd, urgent, miss, gate=gate, now_ms=now_ms)
        book = trader_book(base, cost=cost, force=fresh)
    items, hyps, dropped = run_team(base, cost=cost, now_ms=now_ms,
                                    size_usd=qty_usd,
                                    # 冻结模式：显式空标题 + static，**不碰 LLM、不抓新闻**
                                    headlines=([] if freeze_news else None),
                                    news_mode=("static" if freeze_news else "auto"),
                                    news_assess=llm_event)

    # ---- ⚓ 外部锚定（第三方真实美股报价）：**冻结输入优先**（复跑靠它）----
    #    不传时读 `data/derived/anchor.json`（由 `tools/mcp_anchor.py --refresh` 落盘）。
    #    ⚠️ 三种情况分开说，**都不能被当成"定价没有偏离"**：
    #      · 传了冻结值        -> 用它（复跑路径）
    #      · 有缓存、有该标的   -> 正常比对
    #      · 没有缓存/无该标的  -> 报告写"未接外部锚"，如实标注缺数据
    anchor_used, anchor_note = None, None
    if isinstance(anchor, dict) and anchor.get("deviation_bp") is not None:
        anchor_used = anchor
        anchor_note = "冻结输入：偏离 %+.1f bp" % anchor["deviation_bp"]
    else:
        try:
            _ap = os.path.join(BASE, "data", "derived", "anchor.json")
            with open(_ap, encoding="utf-8") as _fh:
                _ac = json.load(_fh)
            _a = (_ac.get("bases") or {}).get(base.upper())
            if _a:
                anchor_used = _a
                anchor_note = ("缓存（%s，美股 %s）：偏离 %s bp"
                               % (_ac.get("fetched_utc"), _ac.get("session"),
                                  "—" if _a.get("deviation_bp") is None
                                  else "%+.1f" % _a["deviation_bp"]))
            else:
                anchor_note = "缓存里没有 %s 的报价（**不等于定价没有偏离**）" % base
        except (OSError, ValueError) as exc:
            anchor_note = ("**没有外部锚缓存**（`mcp_anchor.py --refresh` 可更新）：%s"
                           "—— 这不等于『定价没有偏离』" % type(exc).__name__)
    if anchor_used is not None:
        try:
            _arep, _ahyps, _adropped = analyst_external_anchor(
                base, anchor_used, cost=cost)
            _aok, _awhy = validate(_arep)
            items = items + [{"report": _arep, "valid": _aok,
                              "invalid_reason": _awhy}]
            # 它的假设与别的 agent 假设一样并进风控 —— 不搞特殊通道
            hyps = list(hyps) + list(_ahyps)
            dropped = list(dropped) + list(_adropped)
            if len(hyps) > MAX_HYPOTHESES:
                dropped = dropped + [
                    {"id": h["id"], "reason": "超出假设条数上限（%d）"
                     % MAX_HYPOTHESES, "metric": h.get("metric"),
                     "value": h.get("value")} for h in hyps[MAX_HYPOTHESES:]]
                hyps = hyps[:MAX_HYPOTHESES]
        except Exception as exc:  # noqa: BLE001
            anchor_note = (anchor_note or "") + "｜锚定分析失败：%r" % (exc,)

    # ---- ⏱️ 执行进度官（执行中闭环）：**只在有在途订单时**才跑 ----
    # 为什么放在这里：它看的是"我已经下的那一单"，与上面 5 路（看市场）不是一回事。
    # 它的假设会**并进** hyps 一起交给风控官 —— 裸露敞口因此真的能触发一票否决。
    exec_prog = None
    os_norm = norm_order_state(order_state)
    if os_norm:
        try:
            prep, phyps, pdropped = analyst_execution_progress(
                base, os_norm, cost=cost, now_ms=now_ms)
            ok_r, why_r = validate(prep)
            items = items + [{"report": prep, "valid": ok_r,
                              "invalid_reason": why_r}]
            # ⭐ 优先级：**执行中的真实敞口** 排在事前风险假设之前 ——
            #    否则 MAX_HYPOTHESES 的截断可能把"一腿裸着"这条 veto 挤掉。
            hyps = list(phyps) + list(hyps)
            dropped = list(dropped) + list(pdropped)
            if len(hyps) > MAX_HYPOTHESES:
                dropped = dropped + [
                    {"id": h["id"], "reason": "超出假设条数上限（%d）"
                     % MAX_HYPOTHESES, "metric": h.get("metric"),
                     "value": h.get("value")}
                    for h in hyps[MAX_HYPOTHESES:]]
                hyps = hyps[:MAX_HYPOTHESES]
            exec_prog = {"order_state": os_norm,
                         "report": prep, "hypotheses": phyps,
                         "dropped": pdropped,
                         "actions": execution_actions(base, os_norm,
                                                      cost=cost)}
        except Exception as exc:  # noqa: BLE001
            # 执行中判断失败**不能静默**：如实记下来，但不要让整条链挂掉
            exec_prog = {"order_state": os_norm,
                         "error": "%s: %s" % (type(exc).__name__, exc)}
    try:
        g = _cg(base, now_ms)
    except Exception as exc:  # noqa: BLE001
        # 闸门取不到时**不能当作没有事件**（fail-safe，与 execution_cost 一致）
        g = ("caution", "闸门不可用：%s" % type(exc).__name__, "unavailable", True)
    # ---- 🔴 硬闸门 = 确定性日历 **与** LLM 事件判定，取更保守的一侧 ----
    # 这一步是 2026-09-19 修的"说了没做"：在此之前 `_cg()` 只读确定性日历，
    # LLM 判出的 block 到不了硬闸门（详见 merge_gate 的注释与实测证据）。
    news_ev = None
    for i in items:
        r = i.get("report") or {}
        if r.get("dimension") == "news" and isinstance(r.get("event"), dict):
            news_ev = r["event"]
            break
    static_gate = g
    # 📅 外部确定性事件（财报 / 除息）：**冻结输入优先**（复跑靠它）。
    #    不传时读 `data/derived/ext_events.json` 缓存（由 `ext_events.py --refresh`
    #    更新）。⚠️ 三种情况必须分开说，**都不能被当成"没有事件"**：
    #      · 传了冻结值           -> 用它（复跑路径）
    #      · 有缓存、窗口内无事件  -> 明确记 "none"（这是**判定结果**，不是缺数据）
    #      · 没有缓存 / 没有该标的 -> 记 "unavailable"（**缺数据**，不许冒充安全）
    ext_ev, ext_note = None, None
    if ext_event is not None:
        ext_ev = ext_event if ext_event.get("severity") else None
        ext_note = ("冻结输入：%s" % (ext_event.get("severity") or "无事件"))
    else:
        try:
            try:
                from ext_events import (event_for as _ef, gate_from_events as _gfe,
                                        load_cache as _lc)
            except ImportError:
                from project2.ext_events import (event_for as _ef,  # type: ignore
                                                 gate_from_events as _gfe,
                                                 load_cache as _lc)
            _cache = _lc()
            if _cache is None:
                ext_note = ("**没有外部事件缓存**（`ext_events.py --refresh` 可更新）"
                            "—— 这不等于『没有事件』")
            else:
                _e = _ef(_cache, base)
                if _e is None:
                    ext_note = "缓存里没有 %s 的记录（不等于没有事件）" % base
                else:
                    _px = (book or {}).get("mid")
                    if isinstance(_px, dict):
                        _px = _px.get("spot") or _px.get("mid")
                    ext_ev = _gfe(_e, now_ms=now_ms, price=_px)
                    ext_note = ("缓存（%s）：窗口内**无**财报/除息事件"
                                % (_cache.get("fetched_utc") or "?")
                                if ext_ev is None else
                                "缓存（%s）：触发 %s"
                                % (_cache.get("fetched_utc") or "?",
                                   ext_ev.get("severity")))
        except Exception as exc:  # noqa: BLE001
            ext_note = "外部事件源不可用：%s: %s" % (type(exc).__name__, exc)
    g = merge_gate(static_gate, news_ev, ext_ev)
    apply_gate_to_cost(cost, g)          # trader / 风控官读的是 cost
    event = {"severity": g[0], "reason": g[1], "source": g[2],
             "maker_allowed": g[3]}
    debate = run_debate(base, items, gate=g, cost=cost)
    decision = decide(base, items=items, debate=debate, cost=cost, book=book,
                      event=event, qty_usd=qty_usd, now_ms=now_ms,
                      hypotheses=hyps)
    decision["scenario"] = scenario
    decision["scenario_note"] = sc_note
    decision["risk_hypotheses"] = hyps
    decision["risk_hypotheses_dropped"] = dropped
    # ⭐ 闸门合并留痕：写清"硬闸门最后听谁的"，以及 LLM 那一侧说了什么。
    #    同时把**实际用到的** LLM 判定（实时调用的 / 冻结复用的）记下来 ——
    #    复跑靠它冻结输入（否则 LLM 是活输入，硬闸门依赖它就没法复跑）。
    decision["llm_gate_used"] = news_ev
    # 📅 外部确定性事件：**实际用到的那份**（复跑靠它冻结输入），以及一句来源说明
    #    （说明区分"窗口内无事件"与"缺数据"——这两件事绝不能混）。
    decision["ext_gate_used"] = ext_ev
    decision["ext_gate_note"] = ext_note
    # ⚓ 外部锚：**实际用到的那份**（复跑靠它冻结）+ 一句来源说明
    decision["anchor_used"] = anchor_used
    decision["anchor_note"] = anchor_note
    # ⏱️ 执行进度（有在途订单时才有）：报告 + 假设 + **确定性**处置口径
    if exec_prog is not None:
        decision["execution_progress"] = exec_prog
    decision["gate_merge"] = {
        "static": {"severity": static_gate[0], "reason": static_gate[1],
                   "source": static_gate[2]},
        "llm": news_ev,
        # 📅 第三源：外部确定性事件（财报/除息）。None 表示"窗口内无事件"或
        #    "缺数据"，到底哪种看 ext_gate_note —— **不在这里下结论**。
        "ext": ext_ev,
        "ext_note": ext_note,
        "effective": {"severity": g[0], "reason": g[1], "source": g[2],
                      "maker_allowed": g[3]},
    }
    # ⭐ 决策基准时间：连同依据一起带出去（页面/接口/日志都要能说清"现在几点、凭什么"）
    decision["time_basis"] = tb
    # 📉 数据新鲜度官：**每个输入源各自**落后多少，以及这算不算危险。
    #    必须放在 time_basis 之后 —— 它是"离线声明模式"还是"实时但输入停了"，
    #    取决于 basis，而 basis 是 time_basis 判出来的。
    decision["freshness"] = data_freshness(now_ms=now_ms,
                                           basis=(tb or {}).get("basis"))
    # 💵 入场损益测算：给定金额，算**能算的**（摩擦 + 资金费 + 裸露腿期望），
    #    并明确列出**算不出来的**（基差变动等）。纯派生 —— 不改任何输入，
    #    所以它**不需要进复跑契约**（不是新的决策输入）。
    try:
        try:
            from entry_math import entry_math as _entry
        except ImportError:
            from project2.entry_math import entry_math as _entry
        decision["entry"] = _entry(base, qty_usd, cost=cost,
                                   mode=(decision.get("trader") or {}).get("mode"))
    except Exception as exc:  # noqa: BLE001
        # 测算失败**不能静默**：如实记下，但不要让整条链挂掉
        decision["entry"] = {"ok": False,
                             "why": "入场测算不可用：%s: %s"
                                    % (type(exc).__name__, exc)}
    return cost, items, debate, decision, book


def _synthetic_debate(stance, base="SYNTH"):
    """合成一个辩论裁决 —— **只用于自检**，不参与任何真实结论。"""
    return {"base": base, "bull": {"arguments": [], "weight": 0.0,
                                   "concessions": [], "dropped": [],
                                   "direction_conflicts": []},
            "bear": {"arguments": [], "weight": 0.0, "concessions": [],
                     "dropped": [], "direction_conflicts": []},
            "cross": {"bull_rebuts": None, "bear_rebuts": None},
            "verdict": {"stance": stance, "base_stance": stance,
                        "reason": "自检合成", "cap_reason": None,
                        "bull_weight": 0.0, "bear_weight": 0.0,
                        "margin": DEBATE_MARGIN, "direction_penalty":
                        DIRECTION_PENALTY,
                        "does_not_alter": ["合成"], "cost_snapshot": None},
            "excluded_reports": []}


def _synthetic_cost(base="NVDA", *, maker_allowed=True, severity="none"):
    """合成成本结构：数值全部取自**实测口径**的量级，只用于自检。

    显式带冲击（4 bp @ 5000 USD），这样规模缩放**真的会改变成本**，
    规模相关的代码路径才算被测到（否则缩放到 0 也不会有差别）。
    """
    q0, imp_mm, imp_mix, imp_tk = 5000.0, 0.0, 1.5, 4.0
    mm0, mix0, tk0 = -12.41, 3.00, 15.00
    return {"base": base, "qty": q0, "half_s": 1.14, "half_p": 0.23,
            "p_s": 0.469, "p_p": 0.23, "p_both": 0.469 * 0.23,
            "p_part": 0.469 * 0.77 + 0.23 * 0.531, "p_none": 0.531 * 0.77,
            "adv_s": -0.228, "adv_p": -0.929, "leg_risk": 4.37, "miss": 3.0,
            "cost_mm": mm0 + imp_mm, "cost_mix": mix0 + imp_mix,
            "cost_tk": tk0 + imp_tk,
            "best_mode": "双腿全挂单" if maker_allowed else "双腿全吃单",
            "best_cost": (mm0 + imp_mm) if maker_allowed else (tk0 + imp_tk),
            "gate_severity": severity, "gate_reason": "自检合成",
            "gate_source": "selftest", "maker_allowed": maker_allowed,
            "invalidated": [] if maker_allowed else ["双腿全挂单",
                                                     "现货挂单+永续吃单"],
            "spread_s": 2.28, "spread_p": 0.47, "route": "in_house",
            "session": "us", "n_s": 1007, "n_p": 8075}


def _synthetic_book(deep=False):
    """合成盘口：spot/ask 首档 70,165 USD（与评测时实测同量级）。

    ``deep=True`` 时深度大到不成为约束 —— 用来单独测「薄深度」规则不误触发。
    """
    if deep:
        lv = 1e9
        return {"snapshot_ts": 0, "mid": {"spot": 219.30, "perp": 219.35},
                "depth_usd": {"spot/ask": {"level1": lv, "five_level": lv * 5,
                                           "first_share": 0.2},
                              "perp/ask": {"level1": lv, "five_level": lv * 5,
                                           "first_share": 0.2}},
                "paths": []}
    return {"snapshot_ts": 0, "mid": {"spot": 219.30, "perp": 219.35},
            "depth_usd": {"spot/ask": {"level1": 70165.0, "five_level": 412000.0,
                                       "first_share": 0.17},
                          "spot/bid": {"level1": 141487.0, "five_level": 500000.0,
                                       "first_share": 0.28},
                          "perp/ask": {"level1": 51014.0, "five_level": 91200.0,
                                       "first_share": 0.56},
                          "perp/bid": {"level1": 238229.0, "five_level": 610000.0,
                                       "first_share": 0.39}},
            "paths": []}


# ------------------------------------------------- 合成场景（**不是实测**，只用于演示）

SCENARIO_FLAG = ("synthetic", "**合成分支，非实测盘口**")

# 场景 -> (三方案成本, maker_allowed, 说明)。数值取自本轮实测记账的量级。
SCENARIOS = {
    "viable": {
        "costs": {"cost_mm": -6.00, "cost_mix": -1.00, "cost_tk": 12.18},
        "maker_allowed": True,
        "note": "假想盘口：现货深度充足、往返成本为负（挂单赚点差）",
    },
    "blocked": {
        "costs": {"cost_mm": 9.00, "cost_mix": 10.00, "cost_tk": 12.18},
        "maker_allowed": False,
        "note": "假想事件窗口：闸门 block，挂单类方案作废，只剩全吃单",
    },
    "thin": {
        "costs": {"cost_mm": 10.42, "cost_mix": 12.27, "cost_tk": 11.02},
        "maker_allowed": True,
        "note": "本轮真实账面成本，但盘口薄（首档 293 USD）—— 复现「压到下限」路径",
    },
}


def make_scenario(name):
    """构造一个**明确标注为合成**的 (cost, book)，供演示与回归用。

    🔴 合成场景的结果**不得**出现在任何"实测结论"里 ——
       日志与输出里都由 ``synthetic`` 字段标明，`--replay` 也会保留这个标记。
    """
    sc = SCENARIOS.get(name)
    if not sc:
        return None, None, None
    cost = _synthetic_cost(maker_allowed=sc["maker_allowed"], severity="none")
    cost.update(sc["costs"])
    if not sc["maker_allowed"]:
        cost["best_mode"] = "双腿全吃单"
        cost["best_cost"] = sc["costs"]["cost_tk"]
        cost["spread_s"], cost["spread_p"] = 2.28, 0.47
    else:
        cost["best_mode"] = min(
            (("双腿全挂单", sc["costs"]["cost_mm"]),
             ("现货挂单+永续吃单", sc["costs"]["cost_mix"]),
             ("双腿全吃单", sc["costs"]["cost_tk"])), key=lambda z: z[1])[0]
        cost["best_cost"] = sc["costs"][
            {"双腿全挂单": "cost_mm", "现货挂单+永续吃单": "cost_mix",
             "双腿全吃单": "cost_tk"}[cost["best_mode"]]]
    book = _synthetic_book()
    book["mid"] = {"spot": 219.30, "perp": 219.35}
    return cost, book, sc


def decision_selftest():
    """④ 交易员 + 风控官自检 —— 重点是**规则真的会否决**、以及**只能收紧**。"""
    ok = True

    def chk(cond, msg):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s" % ("OK " if cond else "!! ", msg))

    c = _synthetic_cost()
    bk = _synthetic_book()

    # ---- ① 事件闸门 block 时：交易员不得给出挂单类方案 ----
    t_block = trader(_synthetic_cost(maker_allowed=False, severity="block"),
                     bk, "proceed", 5000.0)
    chk(t_block["order"] is not None and t_block["order"]["kind"] == "taker",
        "闸门 block：交易员只剩吃单方案（%s）" % (t_block.get("mode")))

    # ---- ② 立场 stand_down 时：交易员必须不下单，且写清理由 ----
    t_sd = trader(c, bk, "stand_down", 5000.0)
    chk(t_sd["order"] is None and t_sd["blocked_by"],
        "立场 stand_down：不下单且**写清理由**（%s）" % (t_sd["blocked_by"][:1]))

    # ---- ③ 规模上界必须由实测容量/深度夹住，不能超 ----
    t = trader(c, bk, "proceed", 10_000_000.0)
    o = t["order"]
    cap = max_capturable_usd("NVDA")
    bounds = [b["usd"] for b in o["size_bounds"]]
    chk(abs(o["size_cap"] - min(bounds)) < 0.01,
        "规模上界 = min(所有上界) = %.0f USD（请求 1e7 被夹住）" % o["size_cap"])
    chk(any("可捕获名义额" in b["name"] for b in o["size_bounds"]),
        "可捕获名义额 %.0f USD 在约束列表里" % (cap or -1))
    chk(abs(o["size_cap"] - 12753.5) < 1.0,
        "最小约束来自 perp/ask 首档 25%%（51014 × 0.25 = 12753 USD）")
    chk(o["slices"] >= 1 and o["size_cap"] <= cap + 1e-6,
        "拆单笔数 >= 1 且规模不超上界（%d 笔）" % o["slices"])
    chk(all(b["usd"] > 0 for b in o["size_bounds"]),
        "三个规模上界都带来源：%s" % "、".join(b["name"] for b in o["size_bounds"]))
    # 🔴 交易员不得改写输入的成本结构（否则日志与复跑会失真）
    c_probe = _synthetic_cost()
    c_before = json.dumps(c_probe, sort_keys=True)
    trader(c_probe, bk, "proceed", 5000.0)
    chk(json.dumps(c_probe, sort_keys=True) == c_before,
        "交易员**不修改**输入的成本结构（纯函数）")

    # ---- ④ 风控官：每条规则都要能回答 引用什么/触发做什么/何时撤销 ----
    risk = risk_officer("NVDA", cost=c, book=bk, trader_out=t, debate=
                        {"verdict": {"stance": "proceed"}},
                        event={"severity": "none"})
    rule_fields = all(r.get("statement") and r.get("measured") and r.get("falsifier")
                      and r.get("action") for r in risk["rules"])
    chk(rule_fields, "%d 条风控规则都写清了 引用什么/做什么/何时撤销"
        % len(risk["rules"]))
    chk(len(risk["rules"]) == risk["checked_rules"], "规则条数与 checked_rules 一致")

    # ---- ⑤ 一票否决真的会否决（而不是只写一句"注意风险"）----
    r_gate = risk_officer("NVDA", cost=_synthetic_cost(maker_allowed=False,
                                                       severity="block"),
                          book=bk, trader_out=t,
                          debate={"verdict": {"stance": "proceed"}},
                          event={"severity": "block"})
    chk(r_gate["verdict"] == "reject" and r_gate["qty_out_usd"] == 0.0,
        "闸门 block -> 风控 reject 且规模归零（触发 %s）" % r_gate["vetoes"])
    # 「成本越过门槛 且 毛边际不足」的合成场景：最优方案钉在吃单 +15.00 bp
    c_allpos = _synthetic_cost(maker_allowed=False, severity="none")
    c_allpos["best_mode"], c_allpos["best_cost"] = "双腿全吃单", 15.0
    c_allpos["cost_tk"] = 15.0
    t_allpos = trader(c_allpos, bk, "proceed", 5000.0)
    r_cost = risk_officer("NVDA", cost=c_allpos, book=bk, trader_out=t_allpos,
                          debate={"verdict": {"stance": "proceed"}},
                          event={"severity": "none"})
    chk(r_cost["verdict"] == "reject" and "cost_exceeds_edge" in r_cost["vetoes"],
        "成本 %+.2f bp > 门槛 %.2f bp 且毛边际 %+.2f bp 不足 -> 否决（%s）"
        % (t_allpos["cost_bp"], EDGE_THRESHOLD_BP, t_allpos["gross_edge_bp"],
           r_cost["vetoes"]))
    r_mm = risk_officer("NVDA", cost=c, book=bk, trader_out=t,
                        debate={"verdict": {"stance": "proceed"}},
                        event={"severity": "none"})
    chk("cost_exceeds_edge" not in r_mm["vetoes"],
        "挂单方案成本为负（%.2f bp = 赚点差）时**不**触发成本否决"
        % t["cost_bp"])
    # 门槛是**同一个**已实测值：两侧都用 EDGE_THRESHOLD_BP，不新造数字
    chk(all(str(EDGE_THRESHOLD_BP) in r["statement"] or str(EDGE_THRESHOLD_BP) in r["falsifier"]
            for r in r_cost["rules"] if r["id"] == "cost_exceeds_edge"),
        "成本否决条引用的是文档化的 %.2f bp 门槛（两侧同一个值）" % EDGE_THRESHOLD_BP)
    r_adv = risk_officer("NVDA", cost=dict(_synthetic_cost(), adv_s=-8.0), book=bk,
                         trader_out=t, debate={"verdict": {"stance": "proceed"}},
                         event={"severity": "none"})
    chk(r_adv["verdict"] == "reject" and "adverse_selection" in r_adv["vetoes"],
        "逆向选择 -8.00 bp（实测出现过）-> 否决（%s）" % r_adv["vetoes"])
    blank = {"order": None, "blocked_by": ["合成：无订单"], "mode": None,
             "cost_bp": None, "max_qty_usd": 0.0, "slices": 0, "price": None,
             "gross_edge_bp": None, "edge_gap_bp": None}
    r_none = risk_officer("NVDA", cost=c, book=bk, trader_out=blank,
                          debate={"verdict": {"stance": "proceed"}},
                          event={"severity": "none"})
    chk("no_cost_data" in r_none["vetoes"] or r_none["qty_out_usd"] == 0.0,
        "交易员不给订单时，风控**不会**凭空放行（no_cost_data 触发，规模 %.0f USD）"
        % r_none["qty_out_usd"])

    # ---- ⑥ 数据缺失 ≠ 没有风险：容量缺数据必须降级，而不是当成无上限 ----
    r_nocap = risk_officer("不存在的标的", cost=dict(c, base="不存在的标的"), book=bk,
                           trader_out=t,
                           debate={"verdict": {"stance": "proceed"}},
                           event={"severity": "none"})
    chk("no_capacity_data" in r_nocap["hits"]
        and r_nocap["hits"].count("no_capacity_data") == 1,
        "容量缺数据 -> 触发 no_capacity_data 并降级（%s）" % r_nocap["hits"])

    # ---- ⑦ 警示级规则只缩规模，不否决 ----
    bk_deep = _synthetic_book(deep=True)
    big = {"order": {"kind": "mm", "mode": "双腿全挂单", "qty_usd": 40000.0,
                     "slices": 20, "slice_usd": 2000.0, "cost_bp": -12.41,
                     "price_desc": "合成", "size_cap": 40000.0,
                     "size_cap_by": "合成", "size_bounds": [],
                     "all_modes_bp": {"双腿全挂单": -12.41,
                                      "现货挂单+永续吃单": 4.5,
                                      "双腿全吃单": 15.0},
                     "maker_allowed": True, "gate_severity": "none",
                     "barred_modes": [], "note": "合成"},
           "blocked_by": [], "mode": "双腿全挂单", "cost_bp": -12.41,
           "max_qty_usd": 40000.0, "slices": 20, "price": "合成",
           "gross_edge_bp": -10.84, "edge_gap_bp": -22.18}
    r_deep = risk_officer("NVDA", cost=c, book=bk_deep, trader_out=big,
                          debate={"verdict": {"stance": "proceed"}},
                          event={"severity": "none"})
    chk("thin_depth" not in r_deep["hits"],
        "深度充足时 thin_depth **不**触发（不误报）")
    r_thin = risk_officer("NVDA", cost=c, book=bk, trader_out=big,
                          debate={"verdict": {"stance": "proceed"}},
                          event={"severity": "none"})
    chk("thin_depth" in r_thin["hits"],
        "单笔 40000 USD > 首档 %s USD 的 %.0f%% -> thin_depth 触发"
        % (format(int(51014), ","), DEPTH_TAKE_RATIO * 100))
    chk(r_thin["verdict"] == "caution"
        and r_thin["qty_out_usd"] < r_thin["qty_in_usd"],
        "警示级只缩规模：%.0f -> %.0f USD（不否决）"
        % (r_thin["qty_in_usd"], r_thin["qty_out_usd"]))

    # ---- ⑧ 🔴 单调性：立场不放松、规模不放大（对手方=想要更激进的输入）----
    dec_ok = True
    for st in DOWNGRADE_ONLY:
        for sev, mk in (("none", True), ("caution", True), ("block", False)):
            cost = _synthetic_cost(maker_allowed=mk, severity=sev)
            d = decide("NVDA", debate=_synthetic_debate(st), cost=cost, book=bk,
                       event={"severity": sev}, qty_usd=5000.0)
            dec_ok = dec_ok and stage_rank(d["final"]["stance"]) <= stage_rank(st)
            dec_ok = dec_ok and d["monotonic"]["stance_non_increasing"]
            tq = float((d["trader"].get("order") or {}).get("qty_usd") or 0.0)
            dec_ok = dec_ok and d["final"]["qty_usd"] <= tq + 1e-6
            dec_ok = dec_ok and d["monotonic"]["qty_non_increasing"]
            if d["final"]["stance"] == "stand_down":
                dec_ok = dec_ok and d["final"]["qty_usd"] == 0.0
                dec_ok = dec_ok and d["final"]["order"] is None
    chk(dec_ok, "3 立场 × 3 闸门状态共 9 组：最终立场只收紧、规模只变小、"
                "stand_down 时规模必为 0 且无订单")

    # ---- ⑨ 两处**放松尝试**必须被拦下并留痕（否则"一票否决"是装饰）----
    d_bad = decide("NVDA", debate=_synthetic_debate("proceed"),
                   cost=_synthetic_cost(maker_allowed=False, severity="block"),
                   book=bk, event={"severity": "block"}, qty_usd=5000.0)
    chk(d_bad["final"]["stance"] == "stand_down" and not d_bad["final"]["order"],
        "辩论想 proceed + 闸门 block -> 最终 stand_down 且无订单")
    chk(d_bad["monotonic"]["upgrade_blocked"] == [],
        "没有放松被放行（upgrade_blocked 为空 = 夹紧成功）")

    # ---- ⑩ 确定性：同输入两次决策完全一致 ----
    k1 = decide("NVDA", debate=_synthetic_debate("proceed"), cost=c, book=bk,
                event={"severity": "none"}, qty_usd=5000.0)
    k2 = decide("NVDA", debate=_synthetic_debate("proceed"), cost=c, book=bk,
                event={"severity": "none"}, qty_usd=5000.0)
    chk(k1 == k2, "确定性：同输入两次决策完全一致")

    # ---- ⑪ 🔴 agent 风险层：它必须**真的能改变决策**，不是装饰 ----
    rep_r, hyps_r, dropped_r = analyst_execution_risk(
        "NVDA", cost=_synthetic_cost(), now_ms=int(dt.datetime.now(dt.UTC)
                                                   .timestamp() * 1000))
    chk(rep_r["dimension"] == "execution_risk" and rep_r["evidence"],
        "执行风险分析师产出报告（%d 条证据，%d 条假设）"
        % (len(rep_r["evidence"]), len(hyps_r)))
    chk(all(h.get("metric") and h.get("value") and h.get("threshold")
            and h.get("falsifier") for h in hyps_r),
        "每条风险假设都带 实测量 + 阈值 + **证伪条件**（缺一不可）")
    chk(len(hyps_r) <= MAX_HYPOTHESES, "假设条数受上限约束（%d <= %d）"
        % (len(hyps_r), MAX_HYPOTHESES))
    # 现货腿**零成交**是 2026-09-14~09-18 的真实状态（docs/29），09-19 已恢复；
    # 所以自检不能假设"必然停滞"，要**按真实数据分两种情形**检查。
    # ⚠️ 初版写成 `chk(any(h["id"] == "stale_quotes"))` —— 那是把"当时的数据"
    #    写死进自检，现货一恢复成交自检就误报失败（实测踩到）。
    stale_h = [h for h in hyps_r if h["id"] == "stale_quotes"]
    stale_d = [d for d in dropped_r if d["id"] == "stale_quotes"]
    chk(bool(stale_h) or bool(stale_d),
        "行情停滞判据有结论（触发或如实未过阈值）：%s"
        % (("%s" % stale_h[0]["value"]) if stale_h
           else ("未过阈值 %s" % stale_d[0].get("value")) if stale_d else "无数据"))
    chk(not stale_h or stale_h[0].get("falsifier"),
        "若触发，则必须带**证伪条件**（重新出现成交即撤销）")
    # ⭐ 停牌/报价冻结：正反两面都要测。
    #    ⚠️ 初版这里是 `chk(froz is False, "活市场不误报")` —— 那是把"采样时的
    #    行情恰好没冻结"当成不变量写进自检。实测踩到：永续腿报价在盘前会连续
    #    10 轮不动，自检随机报失败，而**真实原因是判据本身错**（只看价格不动、
    #    不看有没有成交）。现在拆成两层：
    #      (a) 判据是纯函数 `halted_from`，直接喂合成输入，正反两个方向都断言；
    #      (b) 真实数据只作**如实汇报**，不再当成断言（数据怎么变都不该让自检翻车）。
    F = [100.0] * 20                                  # 20 轮价格完全不动
    V = [100.0 + i * 0.01 for i in range(20)]         # 20 轮价格在动
    h_frozen, d_frozen = halted_from(F, 0, 20)
    chk(h_frozen is True,
        "判据：报价不动 **且** 零成交 -> 判停牌（%s）" % d_frozen[-32:])
    h_live, d_live = halted_from(F, 8, 20)
    chk(h_live is False,
        "判据：报价不动**但有成交** -> **不判停牌**（%s）" % d_live[-40:])
    h_move, _d_move = halted_from(V, 0, 20)
    chk(h_move is False, "判据：报价在动 -> 不判停牌（零成交也不误判）")
    chk(halted_from([100.0], 0, 20)[0] is None, "判据：轮次不足 -> 不硬判（None）")
    # (b) 真实数据：如实汇报，不作断言
    froz, fdet, _m = frozen_quote("NVDA")
    print("  [ ~ ] live 实测（只汇报、不断言）：frozen=%s ｜ %s"
          % (froz, fdet[:88]))
    _orig = globals()["frozen_quote"]
    globals()["frozen_quote"] = lambda b, lookback=FROZEN_LOOKBACK: (
        True, "合成：最近 10 轮中间价完全相同（跨度 0）", [1.0] * 10)
    try:
        _rep_f, _h_f, _d_f = analyst_execution_risk("NVDA", cost=_synthetic_cost())
    finally:
        globals()["frozen_quote"] = _orig
    froz_h = [h for h in _h_f if h["id"] == "quote_frozen"]
    chk(bool(froz_h), "报价冻结时提出 quote_frozen 假设（%s）"
        % ("、".join(h["id"] for h in _h_f) or "无"))
    chk(bool(froz_h) and froz_h[0].get("falsifier"),
        "停牌假设带**证伪条件**（报价恢复变动即撤销）")
    chk(HYPOTHESIS_ACTIONS.get("quote_frozen", {}).get("level") == "veto",
        "停牌假设在风控里是**否决级**（依据 docs/32 的豁免条款）")
    # ⑤ agent 的假设必须**变成风控规则**（否则就是装饰）。
    #    ⚠️ 同理不能假设"一定有假设"：现货恢复成交、腿风险低于 50% 时，
    #    合法结果就是**零假设** —— 那时要验的是"不凭空产生 agent 规则"。
    fo = {"order": {"kind": "taker", "mode": "双腿全吃单", "qty_usd": 5000.0,
                    "slices": 3, "slice_usd": 2000.0, "cost_bp": 12.0,
                    "price_desc": "合成", "size_cap": 5000.0, "size_cap_by": "合成",
                    "size_bounds": [], "all_modes_bp": {"双腿全挂单": 10.0,
                                                        "现货挂单+永续吃单": 11.0,
                                                        "双腿全吃单": 12.0},
                    "maker_allowed": True, "gate_severity": "none",
                    "barred_modes": [], "note": "合成"},
          "blocked_by": [], "mode": "双腿全吃单", "cost_bp": 12.0,
          "max_qty_usd": 5000.0, "slices": 3, "price": "合成",
          "gross_edge_bp": 5.0, "edge_gap_bp": -6.34}
    r_agent = risk_officer("NVDA", cost=c, book=bk, trader_out=fo,
                           debate={"verdict": {"stance": "proceed"}},
                           event={"severity": "none"}, hypotheses=hyps_r)
    chk(len(r_agent["agent_rules"]) == len(hyps_r)
        and all(x.startswith("agent:") for x in r_agent["agent_rules"]),
        "agent 假设已转成风控规则（%d 条：%s）"
        % (len(r_agent["agent_rules"]), "、".join(r_agent["agent_rules"]) or "无"))
    if hyps_r:
        chk(r_agent["verdict"] == "reject" and r_agent["agent_driven"],
            "**agent 的假设真的改变了一票否决**（verdict=%s，由 %s 驱动）"
            % (r_agent["verdict"], "、".join(r_agent["vetoes"])))
    else:
        chk(not r_agent["agent_driven"] or r_agent["verdict"] == "reject",
            "零假设时 agent 规则为空（不凭空否决）—— 当前 regime 下无风险假设"
            "（腿风险/停滞/冻结均未过阈值）")
    # 无假设时不得凭空否决（防"agent 层变成万能借口"）
    r_noagent = risk_officer("NVDA", cost=fo and _synthetic_cost(), book=bk,
                             trader_out=fo,
                             debate={"verdict": {"stance": "proceed"}},
                             event={"severity": "none"}, hypotheses=[])
    chk(not any(x.startswith("agent:") for x in r_noagent["hits"]),
        "没有 agent 假设时不产生 agent 规则（不凭空否决）")

    # ---- ⑫ 🔴 LLM 事件判定必须**真的进硬闸门**（2026-09-19 修的"说了没做"）----
    #    修之前：硬闸门走 consult_gate() -> static_gate()，**只读确定性日历**，
    #    LLM 判出的 block 只到辩论层。实测（打桩 assess 必返 block）：
    #        news 收到 block：True ｜ 硬闸门收到 block：False ｜ maker_allowed：True
    #    这里用**冻结的 LLM 判定**验证闭环 —— 不调 API，因此自检仍然离线可跑。
    BLOCK = {"severity": "block", "source": "llm", "frozen": True,
             "confidence": 0.9, "reason": "自检冻结样本：刚披露的 8-K"}
    NONE_EV = {"severity": "none", "source": "llm", "frozen": True,
               "confidence": 0.9, "reason": "自检冻结样本：无事件"}
    try:
        c_gb, _i, _d, dec_gb, _b = run_decision("NVDA", qty_usd=5000.0,
                                                llm_event=BLOCK)
        gm = (dec_gb.get("gate_merge") or {})
        chk((gm.get("effective") or {}).get("severity") == "block",
            "LLM 冻结判 block -> 硬闸门收到 block（effective=%s，静态侧=%s）"
            % ((gm.get("effective") or {}).get("severity"),
               (gm.get("static") or {}).get("severity")))
        chk(c_gb.get("maker_allowed") is False
            and c_gb.get("gate_severity") == "block",
            "LLM 判 block 时 cost 同步为 block 且 maker_allowed=False"
            "（否则会出现『闸门说 block、最优方式还是挂单』）")
        chk(set(c_gb.get("invalidated") or []) == {"双腿全挂单",
                                                   "现货挂单+永续吃单"},
            "挂单类方案进入 invalidated（%s）" % (c_gb.get("invalidated"),))
        _ord = dec_gb["final"].get("order") or {}
        chk(dec_gb["final"]["stance"] == "stand_down" and not _ord,
            "LLM 判 block -> 最终 stand_down 且无订单（stance=%s，订单=%s）"
            % (dec_gb["final"]["stance"], _ord.get("mode")))

        # 反向：LLM 判 none 时**不得**凭空 block（不能修过头变成"总是停手"）
        c_n, _i2, _d2, dec_n, _b2 = run_decision("NVDA", qty_usd=5000.0,
                                                 llm_event=NONE_EV)
        chk((dec_n.get("gate_merge") or {}).get("effective", {})
            .get("severity") != "block",
            "LLM 判 none 时不产生虚假 block（effective=%s）"
            % ((dec_n.get("gate_merge") or {}).get("effective", {})
               .get("severity"),))

        # 🔴 复跑确定性：冻结同一份 LLM 判定 -> 契约投影必须逐字段一致
        lg1 = build_log(base="NVDA", items=_i, debate=_d, cost=c_gb,
                        decision=dec_gb, qty_usd=5000.0, miss_bp=3.0,
                        urgent=False, now_ms=None, book=_b)
        lg2 = build_log(base="NVDA", items=_i2, debate=_d2, cost=c_n,
                        decision=dec_n, qty_usd=5000.0, miss_bp=3.0,
                        urgent=False, now_ms=None, book=_b2)
        _le = (lg1.get("parameters") or {}).get("llm_event") or {}
        chk(_le.get("severity") == "block" and "frozen" not in _le,
            "日志把**用到的 LLM 判定**存进了 parameters.llm_event"
            "（severity=%s；且刻意**不含** frozen —— 那是『这次是实时拿到的还是"
            "复跑冻结的』的过程信息，写进契约会让复跑必然不一致）"
            % (_le.get("severity"),))
        mu1 = repro_projection(lg1)[0]
        mu2 = repro_projection(lg2)[0]
        chk((mu1["parameters"].get("llm_event") or {}).get("severity") == "block"
            and (mu2["parameters"].get("llm_event") or {}).get("severity") == "none",
            "契约投影里带着各自的冻结判定（block / none，两份日志可区分）")
        # 🔴 复跑确定性：**用日志里的参数**（含冻结的 LLM 判定）复跑一遍，
        #    走生产同一条比对路径 `replay_check`。
        #    ⚠️ 不能拿"两次独立运行"去比：那样 `parameters.now_ms` 是各自的墙钟，
        #       本来就不该相等（这是契约的一部分，不是缺陷）。
        _pr = lg1["parameters"]
        c_r, i_r, d_r, dec_r, b_r = run_decision(
            "NVDA", qty_usd=float(_pr["qty_usd"]), miss_bp=float(_pr["miss_bp"]),
            urgent=bool(_pr["urgent"]), now_ms=_pr.get("now_ms"),
            llm_event=_pr.get("llm_event"))
        lg1b = build_log(base="NVDA", items=i_r, debate=d_r, cost=c_r,
                         decision=dec_r, qty_usd=float(_pr["qty_usd"]),
                         miss_bp=float(_pr["miss_bp"]),
                         urgent=bool(_pr["urgent"]), now_ms=_pr.get("now_ms"),
                         book=b_r)
        ok_replay, rep_replay = replay_check(lg1, lg1b)
        chk(ok_replay,
            "用日志参数 + **冻结 LLM 判定**复跑 -> replay_check 通过"
            "（契约字段全一致%s）"
            % ("" if ok_replay else "；差异：%s"
               % str(rep_replay.get("must_diff") or rep_replay)[:120]))

        # 反向：把 LLM 判定从 block 换成 none -> 复跑**必须**报不一致。
        # 这一条证明 llm_event 真的进了契约，而不是一个装饰字段。
        ok_bad, _rep_bad = replay_check(lg1, lg2)
        chk(not ok_bad,
            "把 LLM 判定由 block 改成 none -> replay 报不一致（它确实是契约的一部分）")
    except Exception as exc:  # noqa: BLE001
        chk(False, "LLM 闸门闭环自检异常：%r" % (exc,))

    # ---- ⑯ 📅 外部确定性事件：硬闸门的**第三个源**（2026-09-20 新增）----
    #    它答的是前两源都答不了的**确定**问题：下次财报是哪天、除息日是哪天。
    #    实测接入当天就抓到 **META 除息日 = 当天**（0.525 USD ≈ 7.8 bp 基差跳变），
    #    而在此之前整条链路对此一无所知 —— 这就是"数据源集成"的实际价值。
    try:
        try:
            import ext_events as _xe
        except ImportError:
            from project2 import ext_events as _xe  # type: ignore

        # (a) 三源合并：更严的一侧胜出，且其它源**不许被藏**
        _g_static = ("none", "日历无事件", "static")
        _g_llm = {"severity": "none", "reason": "LLM 判无事件", "source": "llm"}
        _g_ext = {"severity": "caution", "reason": "除息日当天", "source": "mcp"}
        _m = merge_gate(_g_static, _g_llm, _g_ext)
        chk(_m[0] == "caution" and "外部确定性事件" in _m[1],
            "外部事件更严时**由它生效**（%s）" % _m[0])
        chk("LLM" in _m[1] or "日历" in _m[1],
            "另外两源仍写进 reason（**不许被藏**）：%s" % _m[1][:56])
        _m2 = merge_gate(("block", "日历硬约束", "static"), _g_llm, _g_ext)
        chk(_m2[0] == "block" and "更宽松，不采信" in _m2[1],
            "外部事件更宽松时不越权（仍取 block，并如实标注它更宽松）")
        # 🔴 外部事件胜出时来源里必须**保留 LLM 参与**的标记。
        #    实测踩到：来源变成 `mcp:...` 后，页面按前缀判"没用 LLM"，
        #    于是底部写"本次未使用 LLM"、而 ② 行列着 LLM 判定 —— 自相矛盾。
        _m3 = merge_gate(_g_static, _g_llm, _g_ext)
        chk("llm" in _m3[2], "外部事件胜出时来源仍标明 LLM 参与过（%s）" % _m3[2])
        # (b) ext=None 时行为必须与两源版**逐字节一致**（不能悄悄改了旧行为）
        chk(merge_gate(_g_static, _g_llm) == merge_gate(_g_static, _g_llm, None),
            "不传外部事件时，合并结果与从前**逐字节一致**")
        # (c) 纯函数规则：窗口内触发 / 窗口外不触发 / 只到 caution
        _nowx = 1_789_900_000_000
        chk(_xe.gate_from_events({"dividends": [
            {"ex_dividend_date": "2026-09-20", "amount": 0.525}]},
            now_ms=_nowx, price=670.0)["severity"] == "caution",
            "除息日在窗口内 -> caution")
        chk(_xe.gate_from_events({"earnings": {"report_date": "2026-11-17"}},
                                 now_ms=_nowx) is None,
            "财报日远在窗口外 -> 不触发（None 是**判定结果**，不是缺数据）")
        # (d) 🔴 "窗口内无事件" 与 "缺数据" 必须是两种不同的东西
        _cost, _i, _d, _dec_x, _b = run_decision("META", qty_usd=500.0,
                                                 llm_event=NONE_EV)
        _gm = _dec_x.get("gate_merge") or {}
        _note = _gm.get("ext_note") or ""
        # ⚠️ 断言要覆盖**四种**措辞：触发 / 窗口内无事件 / 缓存里没有该标的 /
        #    没有缓存。初版只写了后三种，而 META 实际是"触发" —— 断言过窄会误报。
        chk(any(k in _note for k in ("触发", "窗口内", "缓存里没有",
                                     "没有外部事件缓存")),
            "来源说明是四种情况之一（触发／窗口内无事件／缓存无此标的／无缓存）：%s"
            % _note[:50])
        chk("ext_event" in PARAM_FIELDS,
            "ext_event 进了复跑契约（PARAM_FIELDS 共 %d 项）" % len(PARAM_FIELDS))
        # (e) 契约闭环：写日志 -> 用日志参数复跑 -> 必须一致
        _lgx = build_log(base="META", items=_i, debate=_d, cost=_cost,
                         decision=_dec_x, qty_usd=500.0, miss_bp=3.0,
                         urgent=False, now_ms=None, book=_b)
        _px = _lgx["parameters"]
        _c2x, _i2x, _d2x, _dec2x, _b2x = run_decision(
            "META", qty_usd=float(_px["qty_usd"]), miss_bp=float(_px["miss_bp"]),
            urgent=bool(_px["urgent"]), now_ms=_px.get("now_ms"),
            llm_event=_px.get("llm_event"), ext_event=_px.get("ext_event"))
        _lg2x = build_log(base="META", items=_i2x, debate=_d2x, cost=_c2x,
                          decision=_dec2x, qty_usd=500.0, miss_bp=3.0,
                          urgent=False, now_ms=_px.get("now_ms"), book=_b2x)
        _okx, _repx = replay_check(_lgx, _lg2x)
        chk(_okx, "带外部事件的决策复跑一致（契约字段全一致%s）"
            % ("" if _okx else "；差异 %s" % str(_repx.get("must_match_failed"))[:88]))
    except Exception as exc:  # noqa: BLE001
        chk(False, "外部确定性事件自检异常：%r" % (exc,))

    # ---- ⑰ ⚓ 外部锚定分析师：第三方真实美股报价（2026-09-20 新增）----
    #    它答的是"rToken 这个价，相对**真实股票**贵还是便宜" ——
    #    在此之前本项目**完全没有外部参照价**，所有数字都是自我参照的。
    #    🔴 两条纪律要守住：① 只报**偏离幅度**，不报方向（不做价格预测）；
    #    ② 美股休市时两个价**不同时刻**，口径含不可分离的漂移，置信度必须压低。
    try:
        def _mk(**kw):
            d = {"stock_mid": 100.0, "rtoken_mid": 100.0, "deviation_bp": 0.0,
                 "field": "deviation_bp", "label": "偏离", "same_instant": True,
                 "session": "regular", "quote_utc": "t", "provider": "test"}
            d.update(kw)
            return d

        # (a) 阈值内 -> neutral，**且不产生假设**（不能把小偏离说成风险）
        _r0, _h0, _d0 = analyst_external_anchor("X", _mk(deviation_bp=5.0))
        chk(_r0["dimension"] == "external_anchor" and _r0["verdict"] == "neutral"
            and not _h0,
            "偏离在阈值内 -> neutral 且**零假设**（%s）" % _r0["verdict"])
        # (b) 超阈值 -> **正负都算风险**（刻意不判方向）
        _both = []
        for _v in (ANCHOR_DIVERGENCE_BP + 10, -(ANCHOR_DIVERGENCE_BP + 10)):
            _r, _h, _ = analyst_external_anchor("X", _mk(deviation_bp=_v))
            _both.append(_r["verdict"] == "unfavorable" and bool(_h)
                         and _h[0]["id"] == "anchor_divergence")
        chk(all(_both),
            "%+.0f 与 %+.0f bp 都判 unfavorable + 假设（**正负都算风险，不判方向**）"
            % (ANCHOR_DIVERGENCE_BP + 10, -(ANCHOR_DIVERGENCE_BP + 10)))
        _h1 = analyst_external_anchor(
            "X", _mk(deviation_bp=ANCHOR_DIVERGENCE_BP + 10))[1][0]
        chk(bool(_h1.get("falsifier")) and "阈值" in _h1["falsifier"],
            "偏离假设带**证伪条件**：%s" % _h1["falsifier"][:36])
        # (c) 休市（不同时刻）-> 置信度压低 + notes 说清口径
        _rs = analyst_external_anchor(
            "X", _mk(deviation_bp=5.0, same_instant=False, session="closed"))[0]
        chk(_rs["confidence"] < _r0["confidence"],
            "休市（两个价不同时刻）置信度被压低：%.2f < %.2f"
            % (_rs["confidence"], _r0["confidence"]))
        chk("不同时刻" in _rs["notes"] and "不可分离" in _rs["notes"],
            "notes 写明口径：%s" % _rs["notes"][:46])
        # (d) 来源类别必须标明（第三方 ≠ 实测量）
        chk("第三方" in " ".join(e["source"] for e in _r0["evidence"]),
            "evidence 里标明**第三方数据**（不是本项目的实测量）")
        # (e) 没有锚 -> 明确"未接"，**不能**说成"没有偏离"
        _rn, _hn, _ = analyst_external_anchor("X", None)
        chk(_rn["verdict"] == "neutral" and not _hn and "不等于" in _rn["notes"],
            "没有外部锚时明确标注『未接』，而不是说『没有偏离』")
        # (f) 契约：anchor 进 PARAM_FIELDS；真实决策里带上第 6 路
        chk("anchor" in PARAM_FIELDS,
            "anchor 进了复跑契约（PARAM_FIELDS 共 %d 项）" % len(PARAM_FIELDS))
        _cad, _iad, _dad, _dcad, _bad = run_decision("NVDA", qty_usd=500.0,
                                                     llm_event=NONE_EV)
        _dims = sorted(str((i.get("report") or {}).get("dimension")) for i in _iad)
        if "external_anchor" in _dims:
            chk(len(_dims) == 6, "真实决策里带上第 6 路（%s）" % "、".join(_dims))
            _lgad = build_log(base="NVDA", items=_iad, debate=_dad, cost=_cad,
                              decision=_dcad, qty_usd=500.0, miss_bp=3.0,
                              urgent=False, now_ms=None, book=_bad)
            _pad = _lgad["parameters"]
            chk(_pad.get("anchor") is not None,
                "日志把**用到的外部锚**存进 parameters.anchor（偏离 %s bp）"
                % (_pad.get("anchor") or {}).get("deviation_bp"))
            _c3, _i3, _d3, _dec3, _b3 = run_decision(
                "NVDA", qty_usd=500.0, miss_bp=3.0, now_ms=_pad.get("now_ms"),
                llm_event=_pad.get("llm_event"), anchor=_pad.get("anchor"))
            _lg3 = build_log(base="NVDA", items=_i3, debate=_d3, cost=_c3,
                             decision=_dec3, qty_usd=500.0, miss_bp=3.0,
                             urgent=False, now_ms=_pad.get("now_ms"), book=_b3)
            _ok3, _rep3 = replay_check(_lgad, _lg3)
            chk(_ok3, "带外部锚的决策复跑一致（契约字段全一致%s）"
                % ("" if _ok3 else "；差异 %s"
                   % str(_rep3.get("must_match_failed"))[:88]))
        else:
            chk(True, "本机没有外部锚缓存 —— 第 6 路未参与（**如实跳过**，不假装通过）")
    except Exception as exc:  # noqa: BLE001
        chk(False, "外部锚定自检异常：%r" % (exc,))

    # ---- ⑬ 🔴 执行进度官：把链路从"只在下单前说话"补成**执行中闭环** ----
    #    2026-09-20 新增（P0）。修之前：挂单一直不成交、或**只成交一条腿**的时候，
    #    整条链路里**没有任何角色负责** —— 而这正是赛道三「执行辅助」手册点名的
    #    位置（trader 决策后的拆单与滑点管理）。没有它，"执行辅助"就只是"决策辅助"。
    NOW = int(dt.datetime.now(dt.UTC).timestamp() * 1000)
    MI = 60_000

    def _os(**kw):
        d = {"id": "selftest-order", "base": "NVDA", "qty_usd": 5000.0,
             "opened_ms": NOW - 10 * MI}
        d.update(kw)
        return d

    # (a) 只成交一腿 -> 裸露敞口，**否决级**，且必须带证伪条件与处置口径
    rep_nk, h_nk, _d_nk = analyst_execution_progress(
        "NVDA", _os(spot_filled=True, perp_filled=False))
    nk = [h for h in h_nk if h["id"] == "partial_fill_naked"]
    chk(bool(nk), "只成交一腿（已挂 10 分钟）-> 提出 partial_fill_naked（%s）"
        % ("、".join(h["id"] for h in h_nk) or "无"))
    chk(bool(nk) and nk[0].get("falsifier") and nk[0].get("action"),
        "裸露敞口带**证伪条件**（另一腿也成交即撤销）与处置口径")
    #    ⚠️ 这里**不写死** P 值：它随窗口/路线变化（同一份代码实测到过
    #       in_house 6.6 倍、不分 venue 14.9 倍，以及旧窗口的 35 倍）。
    #       改成**断言关系**：只要"一腿不跟"仍显著多于"两腿都成交"，
    #       这条 veto 的依据就成立；数字由实测表给。
    try:
        try:
            from execution_cost import load_joint_fill as _ljf
        except ImportError:
            from project2.execution_cost import load_joint_fill as _ljf
        _j = (_ljf(_synthetic_cost().get("route")).get("NVDA")
              or _ljf(None).get("NVDA"))
        _ratio = (_j["p_part"] / max(_j["p_both"], 1e-9)) if _j else 0.0
        chk(bool(_j) and _ratio >= 5.0,
            "实测联合分布支撑该判据：P(只成交一腿)=%.1f%% vs "
            "P(两腿都成交)=%.2f%% -> **%.1f 倍**（%s）"
            % (100 * _j["p_part"], 100 * _j["p_both"], _ratio, _j["prov"])
            if _j else "读不到实测联合分布 -> 这条 veto 的依据无法核验")
    except Exception as _exc:  # noqa: BLE001
        chk(False, "联合分布读取异常：%r" % (_exc,))
    chk(HYPOTHESIS_ACTIONS.get("partial_fill_naked", {}).get("level") == "veto",
        "裸露敞口在风控里是**否决级**（一腿不跟是常态，且方向性敞口无对冲）")
    chk(rep_nk["verdict"] == "unfavorable",
        "裸露敞口时本路判定 unfavorable（%s）" % rep_nk["verdict"])
    # 🔴 红线 1 现场验证：本路**不产出任何数字**（价位/规模只能由确定性交易员给）
    _keys = set()
    for h in h_nk:
        _keys |= set(h)
    chk(not (_keys & {"size_usd", "price", "qty_usd", "limit_price", "target"}),
        "本路假设里**没有任何价位/规模字段**（键只有：%s）"
        % "、".join(sorted(_keys)))

    # 处置口径：agent 说"该处置了"，**数字由成本模型给**（算不出来的不编）
    _c_syn = _synthetic_cost()
    acts = execution_actions("NVDA", _os(spot_filled=True, perp_filled=False),
                             cost=_c_syn)
    chk([a["id"] for a in acts] == ["complete_leg", "flatten_leg",
                                    "no_new_position"],
        "裸露敞口给出三条处置口径（%s）" % "、".join(a["id"] for a in acts))
    _cl = acts[0]
    chk(_c_syn.get("cost_tk") is not None and _cl["cost_bp"] == _c_syn["cost_tk"],
        "补腿成本 = 实测成本模型的双腿全吃单往返成本（%s bp），不是编的"
        % _cl["cost_bp"])
    chk(all(a.get("cost_note") for a in acts),
        "每条处置都写明**数字从哪来**（算不出来的直说算不出来）")
    chk(execution_actions("NVDA", _os(spot_filled=True, perp_filled=True)) == [],
        "两腿齐平时**不给处置建议**（没有裸露敞口就不该有动作）")

    # (b) 两腿齐平 -> **零假设**（防"执行层变成万能借口"）
    _rep_ok, h_ok, _d_ok = analyst_execution_progress(
        "NVDA", _os(spot_filled=True, perp_filled=True))
    chk(not h_ok, "两腿都成交时**不产生任何假设**（%s）"
        % ("、".join(h["id"] for h in h_ok) or "零假设 ✓"))

    # (c) 宽限期内 -> 只留痕、不报警（否则每次下单那一瞬间都会误报裸露）
    _rep_g, h_g, d_g = analyst_execution_progress(
        "NVDA", _os(spot_filled=True, perp_filled=False, opened_ms=NOW - 20_000))
    chk(not h_g and any(x["id"] == "partial_fill_naked" for x in d_g),
        "宽限期（%.0f 分钟）内只留痕不报警：%s"
        % (NAKED_GRACE_MIN,
           "；".join(x["reason"] for x in d_g if x["id"] == "partial_fill_naked")))

    # (d) 挂单停滞：判据是"**这条腿**在挂单期间零成交"（逐笔实测），正反都测。
    #     ⚠️ 打桩的是**数据源**（成交表），不是被判定的逻辑本身 —— 与上面
    #       `frozen_quote` 的打桩同理：把"数据说什么"固定住，才能问
    #       "给定零成交 / 给定有成交，判据分别怎么响"。
    _orig_tbv = globals()["_trades_between_venue"]
    try:
        globals()["_trades_between_venue"] = lambda v, b, lo, hi: 0
        _r0, _h0, _d0 = analyst_execution_progress(
            "NVDA", _os(spot_filled=False, perp_filled=False,
                        opened_ms=NOW - 40 * MI))
        globals()["_trades_between_venue"] = lambda v, b, lo, hi: 7
        _r7, _h7, _d7 = analyst_execution_progress(
            "NVDA", _os(spot_filled=False, perp_filled=False,
                        opened_ms=NOW - 40 * MI))
    finally:
        globals()["_trades_between_venue"] = _orig_tbv
    st0 = [h for h in _h0 if h["id"] == "progress_stalled"]
    chk(bool(st0), "挂 40 分钟且该腿**零成交** -> progress_stalled（%s）"
        % ("、".join(h["id"] for h in _h0) or "无"))
    chk(bool(st0) and st0[0].get("falsifier"),
        "停滞假设带**证伪条件**（该腿重新出现成交即撤销）")
    chk(not [h for h in _h7 if h["id"] == "progress_stalled"]
        and any(x["id"] == "progress_stalled" for x in _d7),
        "同样挂 40 分钟但期间**有 7 笔成交** -> **不**触发（如实留痕：%s）"
        % "；".join(x["reason"] for x in _d7 if x["id"] == "progress_stalled"))
    chk(HYPOTHESIS_ACTIONS.get("progress_stalled", {}).get("level") == "caution",
        "停滞在风控里是**警示级**（只缩规模不否决 —— 挂单本身不是错）")
    # 真实数据只**如实汇报**、不作断言（现货腿 09-14~09-18 零成交、09-19 已恢复，
    # 把"当时的数据"写死进自检，数据一变自检就误报 —— 这个坑上面已经踩过一次）
    # ⚠️ 窗口必须锚在**决策基准时间**上：用墙钟去截数据时钟的成交表，
    #    窗口整个落在数据范围之外，"0 笔"会被误读成"市场没成交"（实测踩到）。
    _tb_live = time_basis()
    _win = _tb_live["now_ms"] - 40 * MI
    _rr, _rh, _rd = analyst_execution_progress(
        "NVDA", _os(spot_filled=False, perp_filled=False, opened_ms=_win),
        now_ms=_tb_live["now_ms"])
    print("  [ ~ ] live 实测（只汇报、不断言）：以决策基准时间 %d 截最近 40 分钟"
          "窗口，真实成交 %s" % (_tb_live["now_ms"],
                              "、".join(e["value"] for e in _rr["evidence"]
                                        if "成交笔数" in e["metric"]) or "无数据"))

    # (e) 🔴 接线验证：执行中的假设必须**真的进风控**，不能只是报告里的一段话
    rep_no, h_no, _ = analyst_execution_progress("NVDA", _os(spot_filled=True))
    chk(rep_no["dimension"] == "execution_progress" and rep_no["evidence"],
        "报告进 DIMENSIONS 且带 %d 条可核验证据" % len(rep_no["evidence"]))
    chk("execution_progress" in DIMENSIONS,
        "execution_progress 已在 DIMENSIONS 里（%d 路）" % len(DIMENSIONS))
    chk(norm_order_state(None) is None and norm_order_state({}) is None
        and norm_order_state({"base": ""}) is None,
        "空/无标的的持仓单一律规范化成 None（不猜）")
    _n1 = norm_order_state({"base": "nvda", "spot_filled": 1, "perp_filled": 0,
                            "opened_ms": "1789720000000", "qty_usd": "5000",
                            "备注": "无关字段", "avg_price": 222.5})
    chk(_n1 is not None and set(_n1) <= set(ORDER_STATE_FIELDS)
        and _n1["opened_ms"] == 1789720000000 and _n1["qty_usd"] == 5000.0
        and _n1["perp_filled"] is False,
        "规范化只留决策真正读的字段（丢掉 avg_price/备注 等无关项，"
        "否则复跑会因无关字段漂移而误报）：%s" % sorted(_n1))
    chk("order_state" in PARAM_FIELDS,
        "order_state 进了复跑契约（PARAM_FIELDS 共 %d 项）" % len(PARAM_FIELDS))
    # 🔴 时钟不一致必须**显式拒绝判断**，不能钳成 0 假装算过。
    #    这条是上面那次真实翻车换来的回归：钳成 0 -> 落在宽限期内 ->
    #    裸露敞口假设**静默消失** -> 页面还显示"执行进度：正常"。
    _rep_sk, h_sk, d_sk = analyst_execution_progress(
        "NVDA", _os(spot_filled=True, perp_filled=False, opened_ms=NOW + 10 * MI))
    chk(not h_sk and len(d_sk) == 2,
        "订单时间**晚于**决策基准时间（时钟不一致）-> 零假设且如实留痕"
        "（%s）" % "；".join(x["reason"] for x in d_sk))
    chk(_rep_sk["verdict"] == "neutral" and "不硬判" in _rep_sk["notes"],
        "时钟不一致时报告**写清「不硬判」**，而不是给一个假结论：%s"
        % _rep_sk["notes"][:48])

    try:
        # ⚠️ 必须用**决策基准时间**造 opened_ms，不能用墙钟 —— 读冻结快照时
        #    `time_basis()` 给的是数据自带时刻，用墙钟会造出"开在未来的订单"。
        #    （第一次写这条自检时就是这么翻车的，也正因为翻车才补了上面那个
        #      "时钟不一致就不硬判" 的分支 —— 这个自检本身成了一次真实回归。）
        _tb_t = time_basis()
        _os_live = _os(spot_filled=True, perp_filled=False,
                       opened_ms=_tb_t["now_ms"] - 10 * MI)
        _c_e, _i_e, _d_e, _dec_e, _b_e = run_decision(
            "NVDA", qty_usd=5000.0, llm_event=NONE_EV, now_ms=_tb_t["now_ms"],
            order_state=_os_live)
        _ep = _dec_e.get("execution_progress") or {}
        chk((_ep.get("report") or {}).get("dimension") == "execution_progress",
            "有在途订单时决策里带出执行进度报告（verdict=%s，%d 条假设）"
            % ((_ep.get("report") or {}).get("verdict"),
               len(_ep.get("hypotheses") or [])))
        _ids = [h["id"] for h in (_dec_e.get("risk_hypotheses") or [])]
        chk(_ids[:1] == ["partial_fill_naked"],
            "执行中的假设**排在事前风险假设之前**并入风控输入（顺序=%s）"
            % "、".join(_ids))
        chk("agent:partial_fill_naked" in (_dec_e["risk"]["vetoes"] or []),
            "裸露敞口已转成风控规则并**触发一票否决**（vetoes=%s）"
            % "、".join(_dec_e["risk"]["vetoes"] or []))
        chk(_dec_e["final"]["stance"] == "stand_down"
            and not _dec_e["final"].get("order"),
            "裸露敞口 -> 最终不做单（stance=%s，规模=%.0f）"
            % (_dec_e["final"]["stance"], _dec_e["final"]["qty_usd"]))
        chk(bool(_ep.get("actions")), "决策里同时带出**确定性**处置口径（%d 条）"
            % len(_ep.get("actions") or []))
        # 反向：不给在途订单 -> 不得凭空产生执行层规则
        _dec_no = run_decision("NVDA", qty_usd=5000.0, llm_event=NONE_EV)[3]
        chk("execution_progress" not in _dec_no
            and (not _dec_no.get("execution_progress")),
            "无在途订单时决策里**没有**执行进度块（不凭空产生）")
        chk(not any(str(x).startswith("agent:partial_fill_naked")
                    for x in (_dec_no["risk"]["hits"] or [])),
            "无在途订单时不产生 agent:partial_fill_naked 规则（不凭空否决）")
        # 🔴 契约闭环：带在途订单的决策，写日志 -> 用日志参数复跑 -> 必须一致
        _lg = build_log(base="NVDA", items=_i_e, debate=_d_e, cost=_c_e,
                        decision=_dec_e, qty_usd=5000.0, miss_bp=3.0,
                        urgent=False, now_ms=None, book=_b_e)
        chk((_lg["parameters"].get("order_state") or {}).get("spot_filled") is True,
            "日志把**规范化后的在途订单**存进了 parameters.order_state（%s）"
            % _lg["parameters"].get("order_state"))
        _pr2 = _lg["parameters"]
        _c2, _i2, _d2, _dec2, _b2 = run_decision(
            "NVDA", qty_usd=float(_pr2["qty_usd"]),
            miss_bp=float(_pr2["miss_bp"]), urgent=bool(_pr2["urgent"]),
            now_ms=_pr2.get("now_ms"), llm_event=_pr2.get("llm_event"),
            order_state=_pr2.get("order_state"))
        _lg2 = build_log(base="NVDA", items=_i2, debate=_d2, cost=_c2,
                         decision=_dec2, qty_usd=5000.0, miss_bp=3.0,
                         urgent=False, now_ms=_pr2.get("now_ms"), book=_b2)
        _ok_os, _rep_os = replay_check(_lg, _lg2)
        chk(_ok_os, "带在途订单的决策复跑一致（否则 agent:partial_fill_naked "
                    "会在复跑里凭空消失%s）"
            % ("" if _ok_os else "；差异：%s"
               % str(_rep_os.get("must_match_failed") or _rep_os)[:120]))
        # 反向：复跑时**丢掉**在途订单 -> 必须报不一致（证明它真的是契约的一部分）
        _ok_drop, _ = replay_check(
            _lg, build_log(base="NVDA", items=_i2, debate=_d2, cost=_c2,
                           decision=run_decision(
                               "NVDA", qty_usd=5000.0, miss_bp=3.0,
                               now_ms=_pr2.get("now_ms"),
                               llm_event=_pr2.get("llm_event"))[3],
                           qty_usd=5000.0, miss_bp=3.0, urgent=False,
                           now_ms=_pr2.get("now_ms"), book=_b2))
        chk(not _ok_drop,
            "复跑时丢掉在途订单 -> replay 报不一致（它确实是契约的一部分）")
    except Exception as exc:  # noqa: BLE001
        chk(False, "执行进度官接线自检异常：%r" % (exc,))

    # ---- ⑭ 📉 数据新鲜度官：**两种"旧"必须分开判** ----
    #    离线演示（basis=asof）时数据旧是**声明过的模式**，不该报警；
    #    实时模式（wallclock）下输入停了才是危险 —— 那时按龄判据会失真。
    #    两个方向都断言，否则"恒红"和"恒绿"都发现不了。
    try:
        _f_asof = data_freshness(basis="asof")
        _f_live = data_freshness(basis="wallclock")
        chk(_f_asof["verdict"] == "declared_offline" and _f_asof["sources"],
            "离线演示（basis=asof）判为**声明过的旧**，不报警：%s ｜ 最旧 %s 滞后 %s 分钟"
            % (_f_asof["verdict"], _f_asof["oldest"], _f_asof["oldest_age_min"]))
        chk(_f_asof["verdict"] != "stale",
            "离线模式下**不得**报 stale（否则演示恒红，真正的告警就没人看了）")
        chk(_f_live["mode"] == "live" and _f_live["verdict"] in ("ok", "stale"),
            "实时模式按阈值判：%s（最旧 %s 滞后 %s 分钟 vs 阈值 %.0f）"
            % (_f_live["verdict"], _f_live["oldest"], _f_live["oldest_age_min"],
               DATA_FRESH_MIN))
        # 真实数据：本仓库快照确实停在 09-19，所以 wallclock 下**必须**判 stale。
        # （这不是"测试碰巧通过"，而是这个判据存在的理由本身。）
        chk(_f_live["verdict"] == "stale",
            "实测快照滞后 %s 分钟 -> 实时模式下判 **stale**（这就是它存在的理由）"
            % _f_live["oldest_age_min"])
        chk(all(s.get("file") and s.get("ts_utc") is not None
                and s.get("age_min") is not None for s in _f_live["sources"]),
            "%d 个输入源每条都带 文件 + 时刻 + 滞后分钟（没有猜测值）"
            % len(_f_live["sources"]))
        # 🔴 回归：**任何一条的龄都不得为负**。
        #    实测踩到：离线模式下（基准=09-19 07:27）消息面是 09-20 抓的 ->
        #    按基准算就是 **-1970 分钟**，页面上显示"滞后 -1970 分钟"。
        #    根因不是新闻有问题，是**两个钟**（行情按数据时刻、新闻按墙钟）。
        #    现在每个源各用自己的参照系，并且把"跨钟"这件事明说出来。
        _neg = [s["name"] for s in _f_live["sources"] if s["age_min"] < 0]
        chk(not _neg,
            "没有任何输入源的龄是负数（负龄 = 两个钟混用，实测踩过）：%s"
            % ("、".join(_neg) if _neg else "无"))
        _f_asof2 = data_freshness(now_ms=_f_asof["now_ms"], basis="asof")
        chk(all(s["age_min"] >= 0 for s in _f_asof2["sources"]),
            "离线模式（按数据自带时刻）下也全为非负 —— 消息面用**墙钟**算龄")
        chk("clock_split" in _f_asof2,
            "输出里带 clock_split 标记（跨钟时必须能看出来）：%s"
            % _f_asof2.get("clock_split"))
        chk(isinstance(_f_live.get("why"), str) and _f_live["why"],
            "结论带一句话依据：%s" % _f_live["why"][:60])
        # 接线：决策里必须真的带上它
        _dec_f = run_decision("NVDA", qty_usd=500.0, llm_event=NONE_EV)[3]
        chk(isinstance(_dec_f.get("freshness"), dict)
            and _dec_f["freshness"].get("verdict"),
            "决策里带出新鲜度（verdict=%s）"
            % ((_dec_f.get("freshness") or {}).get("verdict"),))
    except Exception as exc:  # noqa: BLE001
        chk(False, "数据新鲜度官自检异常：%r" % (exc,))

    # ---- ⑮ 💵 入场损益测算：模块自检（走同一条链，避免再加一步自检步骤）----
    #    它的数字是**给用户拿去做决定**的，所以判定规则本身必须被验证：
    #    换算只有一处公式、公式与项目一一致、负数/缺失时不硬算、
    #    "算不出来的"（基差变动）必须被显式列出。
    try:
        try:
            import entry_math as _em
        except ImportError:
            from project2 import entry_math as _em  # type: ignore
        print("  ── 入场损益测算（模块自检）──")
        chk(_em.selftest() == 0, "入场测算模块自检通过（换算 / 公式 / 不硬算）")
    except Exception as exc:  # noqa: BLE001
        chk(False, "入场测算自检异常：%r" % (exc,))

    print("\n交易员/风控官自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


def repro_selftest(write=True, outdir=None):
    """⑤ 可复现日志自检：同输入同哈希 / 复跑通过 / 篡改被抓。"""
    ok = True

    def chk(cond, msg):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s" % ("OK " if cond else "!! ", msg))

    base = "NVDA"
    kw = dict(base=base, qty_usd=5000.0, miss_bp=3.0, urgent=False,
              now_ms=1_700_000_000_000,
              # 🔴 **冻结外部输入**：真 LLM 与实时新闻都不是确定性的，
              #    不冻结这条自检会间歇性失败（实测踩到）
              freeze_news=True)
    gen_ms = 1_700_000_000_000

    cost, items, debate, decision, book = run_decision(**kw)
    log1 = build_log(base=base, items=items, debate=debate, cost=cost,
                     decision=decision, qty_usd=kw["qty_usd"],
                     miss_bp=kw["miss_bp"], urgent=kw["urgent"], book=book,
                     now_ms=kw["now_ms"], generated_ms=gen_ms)
    log2 = build_log(base=base, items=items, debate=debate, cost=cost,
                     decision=decision, qty_usd=kw["qty_usd"],
                     miss_bp=kw["miss_bp"], urgent=kw["urgent"], book=book,
                     now_ms=kw["now_ms"], generated_ms=gen_ms)
    chk(log1["decision_hash"] == log2["decision_hash"],
        "确定性：同输入两次生成的日志哈希一致（%s…）" % log1["decision_hash"][:16])

    # 日志里必须能查到"引用了哪些实测值"
    chk(len(log1["evidence_index"]) >= 5,
        "证据索引非空（%d 条，每条带 source）" % len(log1["evidence_index"]))
    chk(all(e.get("source") for e in log1["evidence_index"]),
        "证据索引**每条都有可回溯来源**")
    chk(all(m.get("sha256") for m in log1["input_manifest"] if m.get("exists")),
        "输入清单里的文件都带 SHA256（%d 个）"
        % len([m for m in log1["input_manifest"] if m.get("exists")]))

    # 🔴 日志里的决策时刻必须**等于决策真正用的那个时刻**。
    #    实测踩到：CLI 的 `--log` 没传 now_ms，`build_log` 就退回墙钟
    #    `generated_ms`；而 `now_ms` 是契约参数（闸门与"行情停滞"都依赖它）——
    #    读冻结快照时决策用 as-of（07:27）、日志却记墙钟（14:24），
    #    复跑时"最后一笔成交距今"从 12 分钟变成 6 小时，凭空多出
    #    `agent:stale_quotes` 一票否决 -> 复跑假失败。
    #    这里把断言钉在**共用口径** log_for_decision 上：谁改了它，这条就红。
    _tb = decision.get("time_basis") or {}
    chk(_tb.get("now_ms") is not None,
        "决策自带基准时刻（basis=%s，%s）"
        % (_tb.get("basis"),
           dt.datetime.fromtimestamp((_tb.get("now_ms") or 0) / 1000, dt.UTC)
           .strftime("%Y-%m-%d %H:%M:%S UTC")))
    _lg = log_for_decision(base, items=items, debate=debate, cost=cost,
                           decision=decision, qty_usd=kw["qty_usd"],
                           miss_bp=kw["miss_bp"], urgent=kw["urgent"], book=book)
    _lg_now = (_lg.get("parameters") or {}).get("now_ms")
    chk(_lg_now == _tb.get("now_ms"),
        "日志记录的 now_ms == 决策用的时刻（否则复跑必假失败）：%s"
        % (dt.datetime.fromtimestamp((_lg_now or 0) / 1000, dt.UTC)
           .strftime("%Y-%m-%d %H:%M:%S UTC") if _lg_now else "缺"))

    # 复跑：**同一批输入**重跑一次组装 -> 必须通过；且报告里不许有"契约不一致"
    # 注意 now_ms 是**决策时刻**（闸门判定依赖它），属于契约参数，要比就得相同；
    # 变的是 generated_ms（生成时间），它只影响哈希，不影响判定。
    log3 = build_log(base=base, items=items, debate=debate, cost=cost,
                     decision=decision, qty_usd=kw["qty_usd"],
                     miss_bp=kw["miss_bp"], urgent=kw["urgent"], book=book,
                     now_ms=kw["now_ms"], generated_ms=gen_ms + 1)
    ok_r, rep = replay_check(log1, log3)
    chk(ok_r and not rep["must_match_failed"],
        "复跑校验：契约字段一致（漂移项 %d 个，如实列出）" % len(rep["drifted"]))

    # 真·端到端复跑：**强制重读盘口**再跑一遍（盘口已变，允许 Evidence 漂移）
    cost_n, items_n, debate_n, dec_n, book_n = run_decision(**kw)
    log_n = build_log(base=base, items=items_n, debate=debate_n, cost=cost_n,
                      decision=dec_n, qty_usd=kw["qty_usd"], miss_bp=kw["miss_bp"],
                      urgent=kw["urgent"], book=book_n, now_ms=kw["now_ms"])
    ok_n, rep_n = replay_check(log1, log_n)
    chk(ok_n, "端到端复跑：重读盘口后**决策路径**仍然一致（漂移项 %d 个：%s）"
        % (len(rep_n["drifted"]), "、".join(rep_n["drifted"]) or "无"))

    # 换参数（规模）-> 契约必须判不一致
    _, _, _, dec2, _ = run_decision(base=base, qty_usd=1234.0, miss_bp=3.0,
                                    urgent=False)
    log4 = build_log(base=base, items=items, debate=debate, cost=cost,
                     decision=dec2, qty_usd=1234.0, miss_bp=3.0, urgent=False,
                     book=book, generated_ms=1_700_000_000_002)
    ok4, rep4 = replay_check(log1, log4)
    chk((not ok4) and "parameters" in rep4["must_match_failed"],
        "参数不同 -> 复跑判**不一致**（抓到：%s）" % rep4["must_match_failed"])

    # 篡改最终规模 -> 自校验必须抓到（内容变了，记录的哈希就对不上了）
    tampered = json.loads(json.dumps(log1))
    tampered["decision"]["final_qty_usd"] = 999.0
    ok5, rep5 = replay_check(tampered, log1)
    chk(not ok5 and not rep5["integrity_recorded"],
        "篡改 final_qty_usd -> 诚信校验失败（抓到 %s）"
        % (rep5["must_match_failed"] or ["integrity"]))

    # 篡改规则表 -> 也要被抓
    tampered2 = json.loads(json.dumps(log1))
    tampered2["risk_officer"]["rules"] = tampered2["risk_officer"]["rules"][:-1]
    ok6, rep6 = replay_check(tampered2, log1)
    chk(not ok6 and "rules_skeleton" in rep6["must_match_failed"],
        "删掉一条风控规则 -> 复跑判不一致（抓到 %s）" % rep6["must_match_failed"])

    if write and ok:
        jp, mp = write_log(log1, outdir=outdir)
        chk(os.path.exists(jp) and (mp is None or os.path.exists(mp)),
            "日志已落盘：%s" % os.path.basename(jp))
        print("     复跑命令: python project2\\agent_team.py --replay %s"
              % os.path.relpath(jp, BASE))

    print("\n可复现日志自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="多 Agent 团队 · 分析师层 + 多空辩论层 + 交易员/风控官 + 可复现日志")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--debate-selftest", action="store_true",
                    help="只跑辩论层自检")
    ap.add_argument("--decision-selftest", action="store_true",
                    help="交易员/风控官自检（一票否决 + 只能收紧）")
    ap.add_argument("--repro-selftest", action="store_true",
                    help="可复现日志自检（同输入同哈希 + 复跑 + 篡改可抓）")
    ap.add_argument("--selfcheck", action="store_true",
                    help="全部自检（分析师 + 辩论 + 交易员/风控官 + 复现），全程无需网络")
    ap.add_argument("--debate", action="store_true",
                    help="在分析师层之上跑多空辩论（开仓前的少数时点才用）")
    ap.add_argument("--trader", action="store_true",
                    help="跑完整决策链：分析师 -> 辩论 -> 交易员 -> 风控官 -> 最终订单")
    ap.add_argument("--log", action="store_true",
                    help="落一份可复跑日志（隐含 --trader）：JSON + Markdown")
    ap.add_argument("--log-outdir", default=None,
                    help="日志输出目录（默认 data/reports）")
    ap.add_argument("--replay", default=None,
                    help="复跑校验：读一份日志 JSON，用记录参数重跑并比对")
    ap.add_argument("--scenario", default=None, choices=sorted(SCENARIOS),
                    help="合成场景（**非实测**，用于演示不同盘口下的决策路径）")
    ap.add_argument("--qty", type=float, default=5000.0, help="名义额 USD")
    ap.add_argument("--miss-bp", type=float, default=None, help="未成交的机会成本(bp)")
    ap.add_argument("--urgent", action="store_true", help="急着成交（放大未成交代价）")
    ap.add_argument("--base")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--json", action="store_true", help="输出结构化 JSON")
    args = ap.parse_args(argv)

    # ---- 复跑校验（不需要 --base：参数全部来自日志）----
    if args.replay:
        path = args.replay
        if not os.path.isabs(path):
            cand = path if os.path.exists(path) else os.path.join(BASE, path)
            path = cand
        if not os.path.exists(path):
            print("找不到日志文件：%s" % args.replay)
            return 2
        with open(path, encoding="utf-8") as fh:
            old = json.load(fh)
        if old.get("format") != LOG_FORMAT:
            print("日志格式不匹配：%r（本版本只认 %s）" % (old.get("format"), LOG_FORMAT))
            return 2
        pr = old.get("parameters") or {}
        base = old.get("base")
        # 🔴 把日志里记的 **LLM 事件判定**当冻结输入传回去：
        #    硬闸门现在依赖它，不冻结就会真的再调一次 LLM，
        #    而 LLM 有随机性 -> 最终立场/规模可能不一致 -> 复跑误报失败。
        #    旧日志没有这个字段时 llm_event=None（如实走一遍实时路径）。
        llm_ev = pr.get("llm_event")
        # ⏱️ 在途订单同理：它是执行进度官（-> agent 一票否决）的输入。
        #    旧日志没有这个字段时 order_state=None（如实走"无在途订单"路径）。
        os_frozen = pr.get("order_state")
        # 📅 外部确定性事件同理：它是硬闸门的第三源。
        #    旧日志没有这个字段时 ext_event=None（如实走"读缓存"路径）。
        ext_frozen = pr.get("ext_event")
        # ⚓ 外部锚同理：它是第 6 路分析师的输入。
        anchor_frozen = pr.get("anchor")
        if llm_ev is None and pr.get("gate", {}).get("source", "").startswith("llm"):
            print("⚠️ 这份日志记录了 LLM 判定来源，但没有存下判定本体"
                  "（旧版本日志）。本次复跑会**重新调用一次 LLM**，"
                  "结果不一致属于如实暴露，不是复跑机制坏了。")
        cost, items, debate, decision, book = run_decision(
            base, qty_usd=float(pr.get("qty_usd") or 5000.0),
            miss_bp=float(pr.get("miss_bp") or 3.0),
            urgent=bool(pr.get("urgent")), now_ms=pr.get("now_ms"),
            scenario=pr.get("scenario"), llm_event=llm_ev,
            order_state=os_frozen, ext_event=ext_frozen,
            anchor=anchor_frozen)
        new = build_log(base=base, items=items, debate=debate, cost=cost,
                        decision=decision, qty_usd=float(pr.get("qty_usd") or 5000.0),
                        miss_bp=float(pr.get("miss_bp") or 3.0),
                        urgent=bool(pr.get("urgent")), now_ms=pr.get("now_ms"),
                        book=book)
        ok_r, rep = replay_check(old, new)
        render_replay(rep, old_path=args.replay)
        if args.json:
            print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0 if ok_r else 1

    if args.selfcheck:
        print("=" * 92)
        print("多 Agent 团队 · 全部自检（分析师 -> 辩论 -> 交易员/风控官 -> 复现日志）")
        print("=" * 92)
        rc = selftest()
        print()
        rc |= debate_selftest()
        print()
        rc |= decision_selftest()
        print()
        rc |= repro_selftest(write=False)
        print()
        print("=" * 92)
        print("全部自检%s" % ("通过" if rc == 0 else "**失败**"))
        print("=" * 92)
        return rc

    if args.selftest:
        print("=" * 92)
        print("分析师层自检（统一 schema + 证据铁律 + 独立性）")
        print("=" * 92)
        return selftest()

    if args.debate_selftest:
        print("=" * 92)
        print("多空辩论层自检（可证伪 + 硬闸门优先 + 不碰量化基线）")
        print("=" * 92)
        return debate_selftest()

    if args.decision_selftest:
        print("=" * 92)
        print("交易员/风控官自检（一票否决 + 只能收紧不放松 + 数据缺失即降级）")
        print("=" * 92)
        return decision_selftest()

    if args.repro_selftest:
        print("=" * 92)
        print("可复现日志自检（同输入同哈希 + 复跑一致 + 篡改可抓）")
        print("=" * 92)
        return repro_selftest(write=True, outdir=args.log_outdir)

    bases = ["TSLA", "NVDA", "AAPL", "META", "GOOGL", "SPY", "QQQ", "SOXL",
             "HOOD", "MRVL"]
    targets = bases if args.all else [args.base.upper()] if args.base else []
    if not targets:
        ap.error("给 --base NAME 或 --all（或用 --selftest / --selfcheck）")

    # ---- 完整决策链（④ 交易员 + ⑤ 风控官 + 可选日志）----
    if args.trader or args.log:
        if args.log and len(targets) > 1:
            ap.error("--log 一次只落一个标的的日志（用 --base 指定）")
        out = {}
        for b in targets:
            cost, items, debate, decision, book = run_decision(
                b, qty_usd=args.qty, miss_bp=args.miss_bp, urgent=args.urgent,
                scenario=args.scenario)
            log = None
            if args.log:
                # 🔴 走共用口径：now_ms = 决策真正用的时刻（见 log_for_decision）
                log = log_for_decision(
                    b, items=items, debate=debate, cost=cost, decision=decision,
                    qty_usd=args.qty,
                    miss_bp=(3.0 if args.miss_bp is None else args.miss_bp),
                    urgent=args.urgent, book=book)
            out[b] = {"debate": debate, "decision": decision,
                      "log": log, "evidence_index":
                          (log or {}).get("evidence_index")}
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0
        print("=" * 92)
        print("多 Agent 团队 · 决策链（分析师 → 辩论 → 交易员 → 风控官 → 最终订单）")
        print("=" * 92)
        if args.scenario:
            print("  🔶 合成场景：**%s**（%s）"
                  % (args.scenario, SCENARIOS[args.scenario]["note"]))
            print("     —— 成本与盘口是**合成的**，分析师与辩论仍走真实数据；")
            print("        结论**不得**当作实测结论引用（日志里 synthetic=true）。")
            print()
        print("  🔴 铁律：没有实测量的结论作废；没有证伪条件的论点作废；")
        print("     交易员与风控官**只能收紧**，量化基线不因 agent 而变。")
        print()
        for b in targets:
            render_debate(out[b]["debate"])
            render_order(out[b]["decision"]["trader"])
            render_risk(out[b]["decision"]["risk"])
            render_decision(out[b]["decision"])
            if out[b]["log"]:
                jp, mp = write_log(out[b]["log"], outdir=args.log_outdir)
                print("     📄 决策日志已落盘：%s" % os.path.relpath(jp, BASE))
                if mp:
                    print("        （人读版 %s）" % os.path.relpath(mp, BASE))
                print("        复跑：python project2\\agent_team.py --replay %s"
                      % os.path.relpath(jp, BASE))
            print()
        print("  ⚠️ 诚实边界（**agent 与代码的分工**）：")
        print("    1. **agent 做的**：5 路分析师（含执行风险）+ 多空辩论 + 风险假设提出")
        print("       —— agent 负责「发现与论证」；")
        print("       **确定性代码做的**：成本数字、执行方式、一票否决、最终规模")
        print("       —— 代码负责「执行与守边界」。")
        print("       为什么这样分：门槛与规模必须可复跑（否则报告里的数字无法验证），")
        print("       而 agent 的价值恰恰在于说出跨表的风险 —— 例如"
              "「现货报价在动、但 4 天没有成交」。")
        print("    2. 风控官规则带 `agent:` 前缀的，表示它来自 agent 提出的假设；")
        print("       未触发的规则也留痕，否则「风控通过」无法被审计。")
        print("    3. 规模上界取「请求 / 可捕获名义额 / 首档深度」的最小值。")
        print("    4. 只在**开仓前的少数时点**触发，不做逐 tick 辩论。")
        return 0

    cost_by = {}
    try:
        from execution_cost import analyse_two_leg as _atl
        for b in targets:
            cost_by[b] = _atl(b, args.qty, args.urgent,
                              3.0 if args.miss_bp is None else args.miss_bp)
    except Exception:  # noqa: BLE001
        pass

    if args.debate:
        try:
            from execution_cost import consult_gate as _cg
        except ImportError:
            from project2.execution_cost import consult_gate as _cg

        out = {}
        for b in targets:
            # ⚠️ run_team 返回三元组 (items, hypotheses, dropped)：
            #    直接写 `items = run_team(...)` 会把元组当 items 传给 run_debate，
            #    报 `'list' object has no attribute 'get'`（踩到过）
            items, hyps_b, _dropped_b = run_team(b, cost=cost_by.get(b))
            try:
                gate = _cg(b, None)
            except Exception as exc:  # noqa: BLE001
                # 闸门取不到时**不能当作没有事件**（fail-safe），与 execution_cost 一致
                gate = ("caution", "闸门不可用：%s" % type(exc).__name__,
                        "unavailable", True)
            d = run_debate(b, items, gate=gate, cost=cost_by.get(b))
            d["agent_hypotheses"] = hyps_b
            out[b] = d

        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0

        print("=" * 92)
        print("多 Agent 团队 · ② 多空辩论层（立论 -> 交叉质证 -> 确定性裁决）")
        print("=" * 92)
        print("  🔴 铁律：**给不出证伪条件的论点一律作废** ——")
        print("     辩论的价值不在于两边说得像样，而在于每条都能被判对错。")
        print()
        for b in targets:
            render_debate(out[b])
            print()
        print("  ⚠️ 诚实边界：")
        print("    1. 本层**只影响边缘决策，不改量化基线** —— basis_bp、成本、")
        print("       闸门判定都不因辩论而变（裁决返回体里用 does_not_alter 明写）。")
        print("    2. **硬闸门优先**：闸门 block 时，多头再占优也到不了 proceed。")
        print("    3. 只在**开仓前的少数时点**触发，不做持续轮询。")
        print("    4. 很多论点会被「没有已实测的证伪条件」没收 —— 这是**如实**，")
        print("       不是缺陷：宁可少一条论点，也不留一条无法判对错的。")
        return 0

    if args.json:
        out = {b: [{"report": i["report"], "valid": i["valid"],
                    "invalid_reason": i["invalid_reason"]}
                   for i in run_team(b, cost=cost_by.get(b))[0]] for b in targets}
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    print("=" * 92)
    print("多 Agent 团队 · ① 分析师层（%d 路独立分析，含 agent 做的执行风险评估）"
          % len(DIMENSIONS))
    print("=" * 92)
    print("  🔴 铁律：**没有引用已实测的量的结论一律作废** ——")
    print("     多 agent 最大的失败模式是「把同一个判断换三个说法」，结构上防住它。")
    print()
    for b in targets:
        render_team(b, run_team(b, cost=cost_by.get(b))[0])
        print()
    print("  ⚠️ 诚实边界：")
    print("    1. 情绪/新闻两路尚未接入 bitget-signal，当前置信度**已如实压低**")
    print("       （新闻 0.40 / 情绪 0.45），不假装它们和实测数据一样可靠。")
    print("    2. 独立性：每个分析师只拿自己那一路数据，不读别人的结论。")
    print("    3. 第 5 路 `execution_risk` 是 **agent 做的风险评估**：它不碰数字，")
    print("       只把「这一单可能怎么死」组织成**可证伪的假设**（实测量+阈值+证伪条件），")
    print("       交给辩论层与风控官执行 —— 见 `--trader` 的输出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
