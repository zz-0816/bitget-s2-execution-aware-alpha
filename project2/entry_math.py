#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
💵 入场损益测算（给定你要投入的金额，算**能算的**，并明说哪些算不出来）
==========================================================================

用户 2026-09-20 的要求：
  「这个执行辅助能够在我需要入场时帮我根据我所填入的金额计算我的盈利、
    盈亏比、风险这些因素显示出来」

━━ 这个模块存在的全部意义：把"能算的"和"算不出来的"分开摆 ━━

口径直接沿用项目一的 `tools/friction_budget.py`（**不是我另造的**）：

    PnL = (B_e − B_x) + (现货价位优势) + (永续价位优势) − 手续费
    一次完整往返 = 建仓 2 笔 + 平仓 2 笔

拆成三块，**只有前两块能算**：

  ✅ ① 摩擦账（可算，全部实测）
       价差优势 2×(half_spot + half_perp)  +  逆向选择 f_s + f_p  −  往返手续费
  ✅ ② 资金费（可算，但**带持有期口径**：表里是 48 小时窗口）
  ❌ ③ 基差变动 (B_e − B_x) —— **算不出来**。
       它是这类交易真正的盈亏来源，但它是**未来价格**。
       本项目有一条红线：不做价格预测。所以这里**不给数字**，
       只把它列进"不含"，并在结论里说清"结论只覆盖摩擦与资金费"。

━━ 为什么还要单独算"裸露腿" ━━

实测 `P(只成交一腿) = 23.6%`，是 `P(两腿都成交)` 的 6.6 倍 —— 一腿不跟是**常态**。
只成交一腿时你必须处置（补腿或平腿），**至少要再付一次往返成本**。
所以除了"顺利情形"，必须把"裸露情形"也摆出来 —— 这才是真正的**风险**。

━━ 红线 ━━

本模块**只做算术**：所有输入都来自实测文件与成本模型，没有任何预测值、
没有 LLM 参与。金额换算就是 `bp × 金额 / 10000`。

用法::

    python project2/entry_math.py --base NVDA --qty 5000
    python project2/entry_math.py --base NVDA --qty 5000 --json
    python project2/entry_math.py --selftest
"""

import argparse
import csv
import io
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DERIVED = os.path.join(BASE, "data", "derived")

# 从"最坏"到"最好"的三种执行方式（与 agent_team / execution_cost 的命名一致）
MODES = (
    ("双腿全挂单", "fee_all_maker_bp"),
    ("现货挂单+永续吃单", "fee_perp_taker_bp"),
    ("双腿全吃单", None),          # 表里没有这一列的费率，用成本模型的口径
)

# 一次完整往返的腿数：建仓 2 笔 + 平仓 2 笔
LEGS_PER_ROUND_TRIP = 2.0


def _rows(path):
    try:
        with io.open(path, encoding="utf-8") as fh:
            return list(csv.DictReader(fh))
    except (OSError, csv.Error):
        return []


def _num(row, key, default=None):
    try:
        v = (row or {}).get(key)
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def friction_table(base):
    """从 `data/derived/friction_budget.csv` 取该标的的实测分解。

    返回 None 表示**读不到** —— 那时本模块**不出结论**（不拿默认值糊弄）。
    """
    for r in _rows(os.path.join(DERIVED, "friction_budget.csv")):
        if (r.get("base") or "").upper() == base.upper():
            return {
                "base": r.get("base"),
                "half_spot_bp": _num(r, "half_spread_spot_bp"),
                "half_perp_bp": _num(r, "half_spread_perp_bp"),
                "fdmid_spot_bp": _num(r, "fdmid_spot_k6"),
                "fdmid_perp_bp": _num(r, "fdmid_perp_k6"),
                "fee_all_maker_bp": _num(r, "fee_all_maker_bp"),
                "fee_perp_taker_bp": _num(r, "fee_perp_taker_bp"),
                "net_all_maker_bp": _num(r, "net_all_maker_bp"),
                "net_perp_taker_bp": _num(r, "net_perp_taker_bp"),
                "capturable_notional_usd": _num(r, "capturable_notional_usd"),
                "k": _num(r, "k"),
                "source": "data/derived/friction_budget.csv",
            }
    return None


def funding_table(base):
    """48 小时窗口的资金费收入（bp）。读不到就返回 None，**不当 0 用**。"""
    for r in _rows(os.path.join(DERIVED, "funding_rates.csv")):
        if (r.get("base") or "").upper() == base.upper():
            v = _num(r, "window_income_bp")
            if v is None:
                return None
            return {"window_income_bp": v,
                    "window_hours": _num(r, "window_hours", 48.0),
                    "source": "data/derived/funding_rates.csv"}
    return None


def joint_fill(base):
    """实测联合成交分布：P(两腿都成交) / P(只成交一腿) / P(都没成交)。"""
    for name in ("joint_fill_all_in_house.csv", "joint_fill_all.csv"):
        for r in _rows(os.path.join(DERIVED, name)):
            if (r.get("base") or "").upper() == base.upper():
                p_both, p_part, p_none = (_num(r, "p_both"), _num(r, "p_part"),
                                          _num(r, "p_none"))
                if None in (p_both, p_part, p_none):
                    continue
                return {"p_both": p_both, "p_part": p_part, "p_none": p_none,
                        "windows": _num(r, "windows"),
                        "date_from": r.get("date_from"), "date_to": r.get("date_to"),
                        "source": "data/derived/%s" % name}
    return None


# ------------------------------------------------------------------ 核心

def bp_to_usd(bp, qty_usd):
    """bp × 金额 / 10000。**唯一的换算公式**，全模块只此一处。"""
    if bp is None or qty_usd is None:
        return None
    return bp * float(qty_usd) / 10000.0


def entry_math(base, qty_usd, cost=None, mode=None, hold_hours=None):
    """给定金额，算**能算的**盈亏与风险；算不出来的**明确列出**。

    ``cost``：决策链里那份实测成本结构（可选）。给了就用它做**交叉核对** ——
    两套口径（friction_budget 的简单式 vs execution_cost 的详细式）算出来的
    数应当同量级；差得离谱要**说出来**，而不是挑一个好看的。
    """
    qty = float(qty_usd or 0.0)
    out = {"base": base.upper(), "qty_usd": qty, "ok": False,
           "evidence": [], "not_included": [], "notes": []}

    fr = friction_table(base)
    if fr is None:
        out["why"] = ("读不到 data/derived/friction_budget.csv 里 %s 的实测分解 —— "
                      "**不出结论**（不拿默认值糊弄）" % base.upper())
        return out
    out["friction"] = fr
    out["evidence"].append({"metric": "价差优势（两腿半幅点差之和）",
                            "value": "%.3f bp" % (2.0 * (fr["half_spot_bp"] + fr["half_perp_bp"])),
                            "source": "%s（half_spread_spot/perp_bp ×2）" % fr["source"]})
    out["evidence"].append({"metric": "逆向选择（k=%d 后中间价不利变动）" % int(fr["k"] or 0),
                            "value": "%.3f bp" % (fr["fdmid_spot_bp"] + fr["fdmid_perp_bp"]),
                            "source": "%s（fdmid_spot/perp_k6）" % fr["source"]})

    # ---- ① 摩擦账：三种执行方式各算一遍（公式与项目一一致）----
    adv_bp = 2.0 * (fr["half_spot_bp"] + fr["half_perp_bp"]) \
        + fr["fdmid_spot_bp"] + fr["fdmid_perp_bp"]
    # ⚠️ 「双腿全吃单」的费率**不在 friction_budget 表里**（表里只有全挂单 14.0 与
    #    现货挂单+永续吃单 22.0）。**不编这个数**：改用**成本模型**给出的那一路
    #    （`cost_tk`，决策链实际用的就是它），并在行里写明来源不同。
    modes = []
    for name, fee_key in MODES:
        if fee_key:
            fee = fr.get(fee_key)
            net = None if fee is None else (adv_bp - fee)
            src = fr["source"]
        else:
            tk = None if not cost else cost.get("cost_tk")
            fee = None
            net = None if tk is None else -float(tk)
            src = ("execution_cost.analyse_two_leg（cost_tk）" if tk is not None
                   else "取不到 —— **不编**")
        modes.append({"mode": name, "fee_bp": fee, "net_bp": net,
                      "net_usd": bp_to_usd(net, qty), "source": src})
    out["price_advantage_bp"] = round(adv_bp, 4)
    out["modes"] = modes
    if modes[-1]["net_bp"] is None:
        out["evidence"].append({
            "metric": "「双腿全吃单」的净额",
            "value": "取不到",
            "source": "friction_budget.csv 没有这一列；且本次没有传入成本模型口径 "
                      "—— 取不到就不显示（**不编**）"})

    chosen = None
    if mode:
        chosen = next((m for m in modes if m["mode"] == mode), None)
    if chosen is None:
        known = [m for m in modes if m["net_bp"] is not None]
        chosen = max(known, key=lambda m: m["net_bp"]) if known else None
    if chosen is None or chosen["net_bp"] is None:
        out["why"] = "三种执行方式的费率都取不到 —— 不出结论"
        return out
    out["mode"] = chosen["mode"]

    # ---- ② 资金费（带持有期口径）----
    fd = funding_table(base)
    if fd:
        hold = float(hold_hours) if hold_hours else fd["window_hours"]
        # 表里是窗口口径；持有期不等于窗口时**按比例折算并说明**（线性外推，
        # 这是**假设**，所以要写进 notes，不能当成实测）
        scaled = fd["window_income_bp"] * (hold / fd["window_hours"]) \
            if fd["window_hours"] else fd["window_income_bp"]
        out["funding"] = {"window_income_bp": fd["window_income_bp"],
                          "window_hours": fd["window_hours"],
                          "hold_hours": hold, "income_bp": round(scaled, 4),
                          "scaled": abs(hold - fd["window_hours"]) > 1e-9,
                          "source": fd["source"]}
        out["evidence"].append({
            "metric": "资金费收入（持有 %.0f 小时）" % hold,
            "value": "%+.3f bp" % scaled,
            "source": "%s（窗口 %.0f 小时口径%s）"
                      % (fd["source"], fd["window_hours"],
                         "，按比例折算——**这是假设不是实测**"
                         if out["funding"]["scaled"] else "")})
        if out["funding"]["scaled"]:
            out["notes"].append("资金费按 %.0f/%.0f 线性折算，**是假设**："
                                "真实资金费每 8 小时结算一次，不是连续线性的。"
                                % (hold, fd["window_hours"]))
    else:
        out["funding"] = None
        out["not_included"].append("资金费收入：读不到 funding_rates.csv 里 %s 的行"
                                   % base.upper())

    # ---- 两种情形 ----
    net_bp = chosen["net_bp"]
    fund_bp = (out["funding"] or {}).get("income_bp") or 0.0
    pnl_a = net_bp + fund_bp                       # 顺利：两腿都成交
    # 裸露：只成交一腿 -> 必须处置 -> **至少再付一次往返成本**
    extra_bp = abs(net_bp - adv_bp)                # = 所选方式的手续费（一次往返）
    pnl_b = pnl_a - extra_bp
    out["scenarios"] = [
        {"key": "smooth", "name": "顺利：两腿都成交，按计划走完一个往返",
         "net_bp": round(pnl_a, 4), "net_usd": bp_to_usd(pnl_a, qty),
         "prob": None},
        {"key": "naked", "name": "裸露：只成交一腿，必须处置（至少再付一次往返）",
         "net_bp": round(pnl_b, 4), "net_usd": bp_to_usd(pnl_b, qty),
         "prob": None},
    ]
    jf = joint_fill(base)
    if jf:
        out["scenarios"][0]["prob"] = jf["p_both"]
        out["scenarios"][1]["prob"] = jf["p_part"]
        out["evidence"].append({
            "metric": "实测联合成交分布",
            "value": "P(两腿)=%.2f%% ｜ P(一腿)=%.2f%% ｜ P(都没)=%.2f%%"
                     % (100 * jf["p_both"], 100 * jf["p_part"], 100 * jf["p_none"]),
            "source": "%s（窗口 %s 个，%s~%s）"
                      % (jf["source"], int(jf["windows"] or 0),
                         jf["date_from"], jf["date_to"])})
        exp_bp = (jf["p_both"] * pnl_a + jf["p_part"] * pnl_b
                  + jf["p_none"] * 0.0)            # 都没成交 = 没有仓位 = 不亏不赚
        out["expectation"] = {
            "bp": round(exp_bp, 4), "usd": bp_to_usd(exp_bp, qty),
            "note": "用**实测联合分布**加权；P(都没成交) 按 0 计（没有仓位就没有损益，"
                    "但也没赚到）"}
    else:
        out["expectation"] = None
        out["not_included"].append("概率加权期望：读不到该标的的实测联合成交分布")

    # ---- 盈亏比（只有在"顺利情形为正"时才有意义）----
    if pnl_a > 0 and pnl_b < 0:
        out["rr_ratio"] = round(pnl_a / abs(pnl_b), 3)
        out["rr_note"] = ("盈亏比 = 顺利情形净收益 %.3f bp ÷ 裸露情形净损失 %.3f bp"
                          % (pnl_a, abs(pnl_b)))
    elif pnl_a <= 0:
        out["rr_ratio"] = None
        out["rr_note"] = ("**顺利情形本身就是亏的（%+.3f bp）** —— "
                          "这时谈盈亏比没有意义：先要让它转正，再谈赔率。"
                          "打平需要毛收益再改善 **%.3f bp**。" % (pnl_a, -pnl_a))
    else:
        out["rr_ratio"] = None
        out["rr_note"] = ("裸露情形竟然也不亏（%+.3f bp）—— "
                          "这通常说明口径有问题，请核对成本模型。" % pnl_b)

    # ---- 交叉核对：**逐方式**比较两套独立口径 ----
    # ⚠️ 初版这里是拿"所选方式的摩擦净额"去比 `-cost_<mode>` —— 而「双腿全吃单」
    #    那一行的净额**本身就是取自 cost_tk**，等于拿它跟自己比，差恒为 0。
    #    那种"核对"是假的：看起来通过了，其实什么也没验证。
    #    现在只比较**两边各自独立算出来**的方式（全挂单 / 混合），差得多就说话。
    if cost:
        pairs = (("双腿全挂单", "cost_mm"), ("现货挂单+永续吃单", "cost_mix"))
        checks = []
        for mname, ckey in pairs:
            m = next((x for x in modes if x["mode"] == mname), None)
            cm = cost.get(ckey)
            if m is None or m["net_bp"] is None or cm is None:
                continue
            checks.append({"mode": mname,
                           "friction_formula_bp": round(m["net_bp"], 4),
                           "execution_cost_bp": round(-float(cm), 4),
                           "diff_bp": round(m["net_bp"] - (-float(cm)), 4)})
        if checks:
            worst = max(checks, key=lambda c: abs(c["diff_bp"]))
            out["cross_check"] = {
                "checks": checks,
                "max_abs_diff_bp": abs(worst["diff_bp"]),
                "note": "两套口径**各自独立**算同一种方式：friction_budget 的简单式"
                        "（2×半幅点差 + 逆向选择 − 费率，取自窗口**中位数**）vs "
                        "execution_cost 的详细式（用**当前盘口** + 冲击/深度/腿风险）。"
                        "两者的**输入时点本就不同**，差几 bp 属正常；"
                        "阈值 5 bp 只是启发式，不是判据。"}
            if abs(worst["diff_bp"]) > 5.0:
                out["notes"].append(
                    "⚠️ 两套口径在「%s」上相差 %.2f bp（> 5）—— 结论以 "
                    "**execution_cost**（决策链实际用的那套）为准，但差异本身值得查。"
                    % (worst["mode"], worst["diff_bp"]))

    # ---- 结论 ----
    if pnl_a <= 0:
        out["verdict"] = "negative"
        out["why"] = ("按实测摩擦与资金费，**顺利情形下这一笔就是亏的**"
                      "（%+.3f bp ≈ %+.2f USD）。不该入场。" % (pnl_a, bp_to_usd(pnl_a, qty)))
    elif out.get("expectation") and out["expectation"]["bp"] <= 0:
        out["verdict"] = "negative_expectation"
        out["why"] = ("顺利情形为正（%+.3f bp），但按**实测成交分布**加权后期望为负"
                      "（%+.3f bp）—— 一腿不跟是常态，期望被它吃掉。"
                      % (pnl_a, out["expectation"]["bp"]))
    else:
        out["verdict"] = "positive"
        out["why"] = ("顺利情形 %+.3f bp，按实测分布加权的期望 %s bp。"
                      % (pnl_a,
                         ("%+.3f" % out["expectation"]["bp"]) if out.get("expectation")
                         else "（无分布，未加权）"))

    # ---- 明确列出"算不出来的"（这一节比上面的数字更重要）----
    out["not_included"] += [
        "**基差变动 (B_e − B_x)** —— 这类交易真正的盈亏来源，但它是**未来价格**。"
        "本项目有红线：不做价格预测，所以这里**不给数字**。"
        "也就是说：上面的结论只覆盖**摩擦与资金费**，不含基差往哪边走。",
        "**持有期损益曲线** —— 没有建仓价/平仓价，也没有持有期口径。",
        "**事件冲击的金额** —— 事件严重度是分类（block/caution/none），不是金额。",
        "**停牌/跳空** —— 无法量化，只由风控官按规则拦（`quote_frozen`）。",
    ]
    out["ok"] = True
    return out


# ------------------------------------------------------------------ 渲染

def render(r):
    L = []
    if not r.get("ok"):
        return "💵 入场测算：暂不可用 —— %s" % r.get("why")
    q = r["qty_usd"]
    L.append("💵 入场损益测算 ｜ %s ｜ 投入 %.0f USD ｜ 方式「%s」"
             % (r["base"], q, r["mode"]))
    L.append("   ── 能算的（全部来自实测）──")
    f = r["friction"]
    L.append("   价差优势  2×(%+.3f + %+.3f) = %+.3f bp"
             % (f["half_spot_bp"], f["half_perp_bp"],
                2.0 * (f["half_spot_bp"] + f["half_perp_bp"])))
    L.append("   逆向选择  (%+.3f) + (%+.3f) = %+.3f bp"
             % (f["fdmid_spot_bp"], f["fdmid_perp_bp"],
                f["fdmid_spot_bp"] + f["fdmid_perp_bp"]))
    for m in r["modes"]:
        mark = " ← 选它" if m["mode"] == r["mode"] else ""
        L.append("   %-18s 费率 %s bp -> 摩擦净额 %s bp（%s USD）%s"
                 % (m["mode"],
                    "—" if m["fee_bp"] is None else "%+.2f" % m["fee_bp"],
                    "—" if m["net_bp"] is None else "%+.3f" % m["net_bp"],
                    "—" if m["net_usd"] is None else "%+.2f" % m["net_usd"],
                    mark))
        if m["fee_bp"] is None and m["net_bp"] is not None:
            L.append("   %-18s ↑ 这一行来自 %s（与上面两行**不同口径**）"
                     % ("", m["source"]))
    if r.get("funding"):
        fu = r["funding"]
        L.append("   资金费    %+.3f bp（持有 %.0f 小时；窗口 %.0f 小时）%s"
                 % (fu["income_bp"], fu["hold_hours"], fu["window_hours"],
                    "（按比例折算，是假设）" if fu["scaled"] else ""))
    L.append("   ── 两种情形 ──")
    for s in r["scenarios"]:
        p = ("%.1f%%" % (100 * s["prob"])) if s.get("prob") is not None else "—"
        L.append("   %-46s P=%s  %+.3f bp = %+.2f USD"
                 % (s["name"], p, s["net_bp"], s["net_usd"]))
    if r.get("expectation"):
        L.append("   概率加权期望（实测分布）%+.3f bp = %+.2f USD"
                 % (r["expectation"]["bp"], r["expectation"]["usd"]))
    L.append("   盈亏比    %s" % (r.get("rr_ratio") if r.get("rr_ratio") is not None
                                  else "—"))
    if r.get("rr_note"):
        L.append("             %s" % r["rr_note"])
    L.append("   结论      %s" % r["why"])
    L.append("   ── **算不出来的**（比上面的数字更重要）──")
    for x in r["not_included"]:
        L.append("   · %s" % x)
    for n in r.get("notes") or []:
        L.append("   [!] %s" % n)
    return "\n".join(L)


# ------------------------------------------------------------------ CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description="入场损益测算（只算能算的）")
    ap.add_argument("--base", default="NVDA")
    ap.add_argument("--qty", type=float, default=5000.0)
    ap.add_argument("--mode", default=None)
    ap.add_argument("--hold-hours", type=float, default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    r = entry_math(a.base, a.qty, mode=a.mode, hold_hours=a.hold_hours)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
    else:
        print(render(r))
    return 0 if r.get("ok") else 1


# ------------------------------------------------------------------ 自检

def selftest():
    """判定规则本身要能被验证：换算、公式、以及**负数/缺失时不硬算**。"""
    ok = True

    def chk(cond, msg):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s" % ("OK " if cond else "!! ", msg))

    # ① 换算只有一处公式，且方向正确
    chk(abs(bp_to_usd(10.0, 5000.0) - 5.0) < 1e-9,
        "bp -> USD：10 bp × 5000 USD = 5 USD")
    chk(bp_to_usd(None, 100) is None, "输入缺失就返回 None（不拿 0 糊弄）")

    # ② 公式与项目一一致：价差优势 = 2×(half_s + half_p)
    fr = friction_table("NVDA")
    if fr:
        adv = 2.0 * (fr["half_spot_bp"] + fr["half_perp_bp"]) \
            + fr["fdmid_spot_bp"] + fr["fdmid_perp_bp"]
        chk(abs(adv - (fr["net_all_maker_bp"] + fr["fee_all_maker_bp"])) < 1e-6,
            "毛收益 = 净额 + 费率（用 CSV 自身校验公式）：%.4f vs %.4f"
            % (adv, fr["net_all_maker_bp"] + fr["fee_all_maker_bp"]))
    else:
        chk(False, "读不到 NVDA 的 friction_budget 行（公式无法校验）")

    # ③ 真实标的端到端：结论必须自洽
    r = entry_math("NVDA", 5000.0)
    chk(r["ok"], "真实标的能算出结论：%s" % r.get("why", "")[:70])
    chk(len(r["not_included"]) >= 3,
        "明确列出「算不出来的」%d 条（基差变动必须在内）"
        % len(r["not_included"]))
    chk(any("基差" in x for x in r["not_included"]),
        "「基差变动」被明确列为**算不出来**（红线：不做价格预测）")
    sc = {s["key"]: s for s in r["scenarios"]}
    chk(sc["naked"]["net_bp"] < sc["smooth"]["net_bp"],
        "裸露情形的净额**必须低于**顺利情形（%.3f < %.3f）"
        % (sc["naked"]["net_bp"], sc["smooth"]["net_bp"]))
    if r.get("expectation"):
        # ⚠️ 初版这里写的是"期望必须 ≤ 顺利情形" —— **断言写错了**，不是代码错。
        #    因为三种情形是 {顺利, 裸露, 都没成交=0}，期望是它们的**凸组合**：
        #    当顺利与裸露都为负时，"都没成交 = 不亏不赚 = 0"会把期望**拉高**到
        #    高于顺利情形。正确的不变量是"落在三者构成的区间内"。
        lo = min(sc["smooth"]["net_bp"], sc["naked"]["net_bp"], 0.0) - 1e-9
        hi = max(sc["smooth"]["net_bp"], sc["naked"]["net_bp"], 0.0) + 1e-9
        chk(lo <= r["expectation"]["bp"] <= hi,
            "加权期望落在 {顺利, 裸露, 0} 构成的区间内 [%.3f, %.3f]，实际 %.3f"
            % (lo, hi, r["expectation"]["bp"]))
    # ④ 顺利情形为负时不得硬给盈亏比
    if sc["smooth"]["net_bp"] <= 0:
        chk(r.get("rr_ratio") is None,
            "顺利情形为负 -> 盈亏比必须是 None（谈赔率没有意义）")
    # ⑤ 未知标的：必须明确不可用，而不是编
    bad = entry_math("NOPE", 1000.0)
    chk(not bad["ok"] and "不出结论" in bad["why"],
        "未知标的**不出结论**（不拿默认值糊弄）")
    # ⑥ 金额线性：翻倍则 USD 翻倍
    r2 = entry_math("NVDA", 10000.0)
    if r["ok"] and r2["ok"]:
        chk(abs(r2["scenarios"][0]["net_usd"]
                - 2 * r["scenarios"][0]["net_usd"]) < 1e-6,
            "金额翻倍 -> 损益 USD 精确翻倍（线性，只有一处换算公式）")

    print("\n入场测算自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
