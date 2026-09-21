#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
📏 阈值标定：把"约定值"换成"实测值"
=====================================

━━ 背景 ━━

项目里有几个阈值**目前是约定，不是标定值**（代码注释里都写明了）：

    ANCHOR_DIVERGENCE_BP  = 20.0    rToken 与真实美股的偏离告警线
    ESTIMATE_DISPERSION_BP = 2000.0 机构目标价的 IQR 分歧告警线

它们各自的注释里写着"样本不足以标定，攒够样本后用实测替换"。
本工具就是那个"替换"动作，**并且把样本量一起写进结果** ——
样本太少时它**不会**给出一个看起来很权威的数，而是明确说"还不能标定"。

━━ 🔴 为什么不能直接拿记录条数当样本量 ━━

长跑记录每 2 分钟一条，一天就是 700+ 条。但偏离量**高度自相关**：
同一个周末的漂移在几小时里几乎是同一个数。把它们当成 700 个独立样本，
算出来的分位数是**假的**。

所以本工具按**不同的美股交易日**去重（`--by-day`），并在结果里同时给出：

    n_raw       原始记录条数
    n_effective 有效样本数（按交易日去重后）
    verdict     ok / too_few —— 少于 --min-samples 就**拒绝标定**

━━ 数据从哪来 ━━

`tools/run_record.py` 每一轮会把当时的锚偏离写进 `data/run/record.jsonl`
（字段 `anchor_dev_bp` / `estimate_dispersion_bp`）。
本工具只读它，**不写任何原始数据**。

用法::

    python tools/anchor_calibration.py                # 看能不能标定 + 建议阈值
    python tools/anchor_calibration.py --write        # 写进 data/derived/anchor_calibration.json
    python tools/anchor_calibration.py --selftest
"""

import argparse
import datetime as dt
import io
import json
import os
import statistics as st
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECORD = os.path.join(BASE, "data", "run", "record.jsonl")
OUT = os.path.join(BASE, "data", "derived", "anchor_calibration.json")

# 少于这么多**有效**样本就拒绝标定（有效 = 按交易日去重后）
MIN_SAMPLES = 20
# 取哪个分位数当阈值：偏离是**双向**风险，所以用 |偏离| 的 p90
QUANTILE = 0.90


def load_records(path=None):
    """读长跑记录。坏行跳过（**不因为一行坏掉就丢掉整份记录**）。"""
    out = []
    try:
        with io.open(path or RECORD, encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    out.append(json.loads(ln))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out


def pct(vals, q):
    """分位数（线性插值）。空表返回 None。"""
    v = sorted(vals)
    if not v:
        return None
    if len(v) == 1:
        return v[0]
    pos = q * (len(v) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] * (1 - (pos - lo)) + v[hi] * (pos - lo)


def collect(recs, field, use_abs=False):
    """把记录里某个字段收成 (值, 交易日) 列表。缺字段的记录直接跳过。"""
    out = []
    for r in recs:
        v = r.get(field)
        if v is None:
            continue
        try:
            x = float(v)
        except (TypeError, ValueError):
            continue
        ts = str(r.get("ts") or "")[:10]
        out.append((abs(x) if use_abs else x, ts))
    return out


def calibrate(pairs, min_samples=MIN_SAMPLES, quantile=QUANTILE):
    """**纯函数**：从 (值, 日) 列表给出标定结论。

    ``n_effective`` = **不同交易日**的条数。这是刻意的保守：
    同一交易日内的高频采样不能算独立样本（高度自相关）。
    """
    if not pairs:
        return {"ok": False, "verdict": "no_data", "n_raw": 0, "n_effective": 0,
                "suggested": None, "min_samples": min_samples,
                "quantile": quantile, "days": [],
                "why": "记录里没有该字段 —— 先让长跑记录器跑起来"}
    vals = [p[0] for p in pairs]
    days = sorted({p[1] for p in pairs if p[1]})
    n_eff = len(days)
    res = {
        "n_raw": len(vals), "n_effective": n_eff,
        "days": days[:40],
        "median": st.median(vals), "p50": pct(vals, 0.50),
        "p90": pct(vals, 0.90), "p95": pct(vals, 0.95),
        "p99": pct(vals, 0.99), "max": max(vals), "min": min(vals),
        "min_samples": min_samples, "quantile": quantile,
    }
    if n_eff < min_samples:
        res.update({"ok": False, "verdict": "too_few",
                    "suggested": None,
                    "why": ("有效样本只有 %d 个（按**交易日**去重；原始 %d 条）"
                            "，少于门槛 %d —— **拒绝标定**。"
                            "同一交易日内的高频采样高度自相关，"
                            "拿来当独立样本算出来的分位数是假的。"
                            % (n_eff, len(vals), min_samples))})
        return res
    sug = pct(vals, quantile)
    res.update({"ok": True, "verdict": "ok", "suggested": sug,
                "why": ("有效样本 %d 个（原始 %d 条，覆盖 %d 个交易日）>= 门槛 %d，"
                        "取 |值| 的 p%.0f = %.1f 作为建议阈值。"
                        "⚠️ 这是**历史分位数**，不是「正确值」："
                        "它保证历史上有 %.0f%% 的样本落在阈值内，"
                        "换一个市场状态就可能不合适。"
                        % (n_eff, len(vals), n_eff, min_samples,
                           quantile * 100, sug, quantile * 100))})
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description="📏 阈值标定（约定值 -> 实测值）")
    ap.add_argument("--file", default=None, help="记录文件（默认 data/run/record.jsonl）")
    ap.add_argument("--min-samples", type=int, default=MIN_SAMPLES)
    ap.add_argument("--write", action="store_true", help="把结论落盘")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()

    recs = load_records(a.file)
    fields = (("anchor_dev_bp", True, "ANCHOR_DIVERGENCE_BP", "锚偏离（取绝对值）"),
              ("estimate_dispersion_bp", False, "ESTIMATE_DISPERSION_BP",
               "机构目标价 IQR 分歧度"))
    res = {"records": len(recs), "generated_utc":
           dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "targets": {}}
    for field, use_abs, const, label in fields:
        c = calibrate(collect(recs, field, use_abs), min_samples=a.min_samples)
        c.update({"field": field, "constant": const, "label": label})
        res["targets"][field] = c

    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
    else:
        print("=" * 78)
        print("阈值标定：约定值 -> 实测值")
        print("=" * 78)
        print("  记录文件 %s ｜ %d 条"
              % (os.path.relpath(a.file or RECORD, BASE), len(recs)))
        for field, c in res["targets"].items():
            print()
            print("  【%s】当前常量 %s" % (c["label"], c["constant"]))
            print("    原始条数 %s ｜ **有效样本（按交易日去重）%s** ｜ 门槛 %s"
                  % (c["n_raw"], c["n_effective"], c["min_samples"]))
            if c["verdict"] == "ok":
                print("    分布 |值|：p50 %.2f ｜ p90 %.2f ｜ p95 %.2f ｜ max %.2f"
                      % (c["p50"], c["p90"], c["p95"], c["max"]))
                print("    **建议阈值 %.2f**（p%.0f）"
                      % (c["suggested"], c["quantile"] * 100))
            print("    结论：%s" % c["why"])
    if a.write:
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        with io.open(OUT, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(res, fh, ensure_ascii=False, indent=1)
        print("\n  已落盘 %s" % os.path.relpath(OUT, BASE))
    return 0


def selftest():
    ok = True

    def chk(cond, msg):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s" % ("OK " if cond else "!! ", msg))

    chk(pct([1, 2, 3, 4], 0.5) == 2.5, "分位数线性插值：中位 = 2.5")
    chk(pct([5], 0.9) == 5 and pct([], 0.9) is None, "单样本 / 空表都不炸")

    # 空数据 -> 明确说没数据，不给阈值
    c0 = calibrate([])
    chk(c0["verdict"] == "no_data" and c0["suggested"] is None,
        "没有数据 -> no_data 且**不给建议阈值**")

    # 🔴 关键：高频但只有一天 -> **拒绝标定**
    same_day = [(10.0, "2026-09-20")] * 500
    c1 = calibrate(same_day)
    chk(c1["n_raw"] == 500 and c1["n_effective"] == 1 and not c1["ok"],
        "500 条但只有 **1 个交易日** -> 有效样本 1，拒绝标定（自相关）")

    # 够了才给
    many = [(float(i), "2026-%02d-%02d" % (9, (i % 28) + 1)) for i in range(30)]
    c2 = calibrate(many)
    chk(c2["ok"] and c2["n_effective"] == 28 and c2["suggested"] is not None,
        "30 条覆盖 28 个交易日 -> 可以标定，建议阈值 %.1f" % (c2["suggested"] or 0))
    chk("历史分位数" in c2["why"],
        "标定结论里写明它是**历史分位数**、不是「正确值」")

    # 坏行 / 缺字段不参与
    chk(collect([{"anchor_dev_bp": None}, {}, {"anchor_dev_bp": "x"},
                 {"anchor_dev_bp": -5, "ts": "2026-09-20T00:00:00Z"}],
                "anchor_dev_bp", use_abs=True) == [(5.0, "2026-09-20")],
        "缺字段/坏值被跳过；**取绝对值**（偏离是双向风险）")

    print("\n阈值标定自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
