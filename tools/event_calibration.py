#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
事件判定校准（人工复核集 → 一致性 + **危险方向**检查）
========================================================

为什么需要它
------------
项目二里 LLM 在运行期的**唯一**职责就是"这条新闻是不是信息事件"。所以
"它能跑"是不够的，必须能回答：**它判得稳不稳？错会往哪个方向错？**

这里做三件事：

1. **校准集的完整性自检**（离线，不需要 key）
   * schema、id 唯一、严重度取值合法；
   * 🔴 **留出检查**：校准集的标题**不得**出现在 RAG 索引里 ——
     否则等于把答案先喂给被测模型，测出来的"一致性"是假的。
2. **跑一遍校准**（`--run`，需要 LLM key）
   逐条调用**生产同一条路径**（`event_gate.llm_gate`），比对期望值，产出：
   * 一致率；
   * **危险错误**条数（期望 block/caution 却判成 none）——这是唯一真正致命的错误方向；
   * 混淆矩阵。
3. 结果落盘 `data/derived/event_calibration_result.json`，供材料引用。

🔴 主动写明的局限
-----------------
* 样本量 **n=10**，只能做**回归**与**危险方向**检查，**不能**当准确率结论；
* 期望值由本项目作者按 `docs/33` 判据表逐条复核，其中 3 条标注
  `confidence_in_label=medium`（属判断题）——材料引用时必须一并写出；
* `--run` 需要 LLM key，**未配置 key 时明确报"未执行"**，不假装跑过。

用法：
  python tools/event_calibration.py --selftest          # 离线自检（含留出检查）
  python tools/event_calibration.py --list              # 看校准集
  python tools/event_calibration.py --run               # 需要 LLM key
  python tools/event_calibration.py --run --json
"""

import argparse
import datetime as dt
import json
import os
import sys

P2 = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, P2)
sys.path.insert(0, os.path.join(P2, "project2"))
try:
    from common.console import install as _install_console
    _install_console()
except Exception:  # noqa: BLE001
    pass

CASES = os.path.join(P2, "data", "calibration", "event_judgments.json")
BASELINE = os.path.join(P2, "data", "calibration", "baseline.json")
RAG_INDEX = os.path.join(P2, "data", "derived", "rag_index.json")
OUT = os.path.join(P2, "data", "derived", "event_calibration_result.json")

SEVERITIES = ("none", "caution", "block")
# 危险方向：期望是 block/caution，却判成 none
DANGEROUS = {("block", "none"), ("caution", "none")}


def load_cases():
    with open(CASES, encoding="utf-8") as fh:
        d = json.load(fh)
    return d, d.get("cases") or []


def load_rag_index():
    try:
        with open(RAG_INDEX, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def check_holdout(cases, idx):
    """🔴 留出检查：校准集标题不得出现在 RAG 索引里。

    为什么这条是硬断言：如果校准标题进了 RAG，那么每次判定时模型都能在
    上下文里看到"我方过去对同一标题的判定"，测出来的就是**抄答案**的能力，
    而不是判断力。这类"校准"在材料里是负分项。
    """
    if idx is None:
        return ["RAG 索引不存在或不可解析 —— 无法执行留出检查（先跑 "
                "python common/rag_memory.py --build）"]
    blob = "\n".join((it.get("text") or "") + " " + (it.get("heading") or "")
                     for it in idx.get("items") or [])
    problems = []
    for c in cases:
        h = (c.get("headline") or "").strip()
        if not h:
            continue
        # 用较长的一段做子串匹配，避免短标题误命中
        probe = h[:60]
        if probe and probe in blob:
            problems.append("校准条目 %s 的标题出现在 RAG 索引里（违反留出规则）"
                            % c.get("id"))
    return problems


def selftest():
    ok = True

    def chk(cond, msg):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s" % ("OK " if cond else "!! ", msg))

    meta, cases = load_cases()
    chk(len(cases) >= 8, "校准集有 %d 条（>=8）" % len(cases))

    ids = [c.get("id") for c in cases]
    chk(len(ids) == len(set(ids)), "id 唯一（%d 个）" % len(set(ids)))
    chk(all(c.get("base") for c in cases), "每条都写明标的")
    chk(all(c.get("expected_severity") in SEVERITIES for c in cases),
        "严重度取值全部合法（%s）" % "、".join(SEVERITIES))
    chk(all(c.get("basis") for c in cases), "每条都写明**判据依据**（不是拍脑袋）")
    chk(all(c.get("source") for c in cases), "每条都带可回溯来源")
    chk(all(c.get("confidence_in_label") in ("high", "medium", "low")
            for c in cases), "每条都标注了『标注本身的把握』")
    chk(all(c.get("headline") for c in cases), "每条都有标题文本")

    dist = {s: sum(1 for c in cases if c["expected_severity"] == s)
            for s in SEVERITIES}
    chk(all(dist[s] >= 1 for s in SEVERITIES),
        "三个严重度都有样本（none %d / caution %d / block %d）"
        % (dist["none"], dist["caution"], dist["block"]))
    chk(any(c["expected_severity"] == "none" for c in cases)
        and any(c["expected_severity"] == "block" for c in cases),
        "含『抗干扰』样本（无关新闻）与『必须停手』样本（财报）")

    n_med = sum(1 for c in cases if c.get("confidence_in_label") == "medium")
    chk(bool(meta.get("_sample_size_warning")) and bool(meta.get("_holdout_rule")),
        "校准集文件里**主动写明**了样本量警告与留出规则")
    print("       ↳ 主动标注：n=%d（**不能当准确率结论**），其中 %d 条属判断题"
          % (len(cases), n_med))

    # ---- 🔴 留出检查 ----
    idx = load_rag_index()
    probs = check_holdout(cases, idx)
    chk(not probs, "留出检查：校准标题**没有**进 RAG 索引"
        + ("" if not probs else "（%s）" % "；".join(probs)))
    if idx is not None:
        print("       ↳ RAG 索引 %d 块（%s）"
              % (idx.get("n_items", 0),
                 "、".join(sorted({it.get("kind", "?")
                                   for it in idx.get("items") or []}))))

    # ---- 关键闭环：事件闸门必须真的能在无 key 时如实降级 ----
    try:
        import event_gate as eg
        import inspect
        src = inspect.getsource(eg.llm_gate)
        chk("fail_closed" in inspect.getsource(eg) or hasattr(
            eg, "FAIL_CLOSED_ON_LLM_ERROR"),
            "闸门保留了 fail-closed 语义（保守优先）")
        chk(callable(getattr(eg, "llm_gate", None)),
            "校准走的是**生产同一条路径**（event_gate.llm_gate）")
        assert src
    except Exception as exc:  # noqa: BLE001
        chk(False, "事件闸门不可用：%r" % (exc,))

    print("\n事件判定校准集自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


def run_calibration(as_json=False, return_result=False, quiet=False):
    """逐条跑 LLM（生产同一条路径），比对期望值。

    ``return_result=True`` 时返回结果 dict（没跑成返回 None），供回归门槛复用。
    """
    meta, cases = load_cases()
    try:
        import event_gate as eg
        from common import config as cfg
    except Exception as exc:  # noqa: BLE001
        print("无法加载 event_gate / 配置：%r" % (exc,))
        return None if return_result else 2

    c = cfg.load()
    key = (c.get("LLM_API_KEY") or "").strip()
    if not key:
        if not quiet:
            print("=" * 74)
            print("⚠️ **未配置 LLM key —— 校准未执行**（不是通过，也不是失败）")
            print("   配置方式：复制 .env.example 为 .env 并填 LLM_API_KEY；")
            print("   不配 key 时事件判断会退化为确定性日历，并在输出里如实标注。")
            print("=" * 74)
        if as_json:
            print(json.dumps({"executed": False,
                              "reason": "no_llm_key",
                              "n_cases": len(cases)},
                             ensure_ascii=False, indent=1))
        return None if return_result else 0

    # 与生产一致：带上同一条 RAG 上下文路径（受字符预算约束）
    rag_ctx = None
    try:
        from common.rag_memory import build_context
        rag_ctx = build_context("NVDA")
    except Exception:  # noqa: BLE001
        pass
    hold = check_holdout(cases, load_rag_index())
    if hold:
        print("🔴 留出检查失败，**拒绝执行校准**（测出来的一致性没有意义）：")
        for p in hold:
            print("   - %s" % p)
        return None if return_result else 2

    rows, danger = [], 0
    for cse in cases:
        now_ms = int(dt.datetime.now(dt.UTC).timestamp() * 1000)
        r = eg.llm_gate(cse["base"], now_ms, [cse["headline"]],
                        c.get("LLM_MODEL"), key, c.get("LLM_BASE_URL"),
                        timeout=int(c.get("LLM_TIMEOUT") or 45),
                        max_retry=int(c.get("LLM_MAX_RETRY") or 2),
                        thinking=str(c.get("LLM_THINKING", "off")).lower() == "on",
                        rag_context=rag_ctx)
        got = r.get("severity")
        exp = cse["expected_severity"]
        hit = (got == exp)
        dang = (exp, got) in DANGEROUS
        danger += 1 if dang else 0
        rows.append({"id": cse["id"], "base": cse["base"],
                     "headline": cse["headline"], "expected": exp, "got": got,
                     "agree": hit, "dangerous": dang,
                     "llm_ok": r.get("llm_ok"),
                     "source": r.get("source"),
                     "reason": r.get("reason"),
                     "confidence": r.get("confidence"),
                     "label_confidence": cse.get("confidence_in_label")})
        print("  %-20s 期望 %-8s 实得 %-8s %s%s"
              % (cse["id"], exp, got, "OK" if hit else "**不一致**",
                 "  ← 危险方向！" if dang else ""))

    n = len(rows)
    agree = sum(1 for r in rows if r["agree"])
    res = {
        "executed": True,
        "executed_utc": dt.datetime.now(dt.UTC).isoformat(),
        "model": c.get("LLM_MODEL"), "prompt_version": getattr(
            eg, "PROMPT_VERSION", None),
        "n_cases": n, "agree": agree,
        "agreement": round(agree / float(n), 3) if n else None,
        "dangerous_errors": danger,
        "sample_size_warning": "n=%d，只能用于回归与危险方向检查，"
                               "**不能作为准确率结论**" % n,
        "holdout_ok": True,
        "rag_context_chars": len(rag_ctx or ""),
        "rows": rows,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=1)
    if as_json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
    else:
        print("-" * 74)
        print("一致 %d/%d（%.0f%%）｜ **危险方向错误 %d 条**（期望停手却判成 none）"
              % (agree, n, 100.0 * agree / n if n else 0, danger))
        print("结果已落盘：%s" % os.path.relpath(OUT, P2))
        print("⚠️ n=%d：这是回归证据，不是准确率结论。" % n)
    if return_result:
        return res
    return 1 if danger else 0


# ---------------------------------------------------------------- 回归门槛

def load_baseline():
    try:
        with open(BASELINE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def cmd_gate(as_json=False, update=False):
    """**回归门槛**：新 prompt / 新模型在同一套留出校准集上不得低于基线。

    没有 key 时**如实跳过**（返回 0）—— 这不是通过，也不是失败，
    输出里会写清楚"未执行"。这一点与项目里其它地方的一致原则相同：
    不假装跑过。

    ``--update-baseline``：只有**有意**更换 prompt/模型时才用，
    它会把本次结果写进 baseline 并保留 history（谁在什么时候、为什么改的）。
    """
    base = load_baseline()
    if base is None:
        print("找不到基线文件：%s" % os.path.relpath(BASELINE, P2))
        return 2

    res = run_calibration(return_result=True, quiet=True)
    if res is None:
        print("=" * 74)
        print("事件判定回归门槛：**未执行**（没有 LLM key）—— 既不算通过也不算失败")
        print("  配好 key 后复跑：python tools/event_calibration.py --gate")
        print("=" * 74)
        if as_json:
            print(json.dumps({"gate": "skipped", "reason": "no_llm_key"},
                             ensure_ascii=False, indent=1))
        return 0

    min_agree = float(base.get("min_agreement", 0.9))
    max_danger = int(base.get("max_dangerous_errors", 0))
    b = base.get("baseline") or {}
    agree = float(res.get("agreement") or 0.0)
    danger = int(res.get("dangerous_errors") or 0)

    problems = []
    if danger > max_danger:
        problems.append("危险方向错误 %d 条 > 门槛 %d（期望停手却判成 none）"
                        % (danger, max_danger))
    if agree < min_agree:
        problems.append("一致率 %.0f%% < 门槛 %.0f%%" % (agree * 100,
                                                        min_agree * 100))
    if b.get("agreement") is not None and agree < float(b["agreement"]):
        problems.append("一致率比基线退化：%.0f%% -> %.0f%%"
                        % (float(b["agreement"]) * 100, agree * 100))

    print("=" * 74)
    print("事件判定回归门槛")
    print("=" * 74)
    print("  prompt      %s（基线 %s）"
          % (res.get("prompt_version"), b.get("prompt_version")))
    print("  模型        %s" % res.get("model"))
    print("  一致率      %.0f%%（基线 %.0f%%，门槛 ≥%.0f%%）"
          % (agree * 100, float(b.get("agreement") or 0) * 100, min_agree * 100))
    print("  危险方向    %d 条（基线 %s，门槛 ≤%d）"
          % (danger, b.get("dangerous_errors"), max_danger))
    print("  样本        n=%d —— 回归证据，**不是准确率结论**" % res.get("n_cases"))

    if update:
        hist = list(b.get("history") or [])
        hist.append({"prompt_version": res.get("prompt_version"),
                     "model": res.get("model"),
                     "agreement": agree, "dangerous_errors": danger,
                     "measured_utc": res.get("executed_utc"),
                     "note": "由 --update-baseline 写入"})
        base["baseline"] = {"prompt_version": res.get("prompt_version"),
                            "model": res.get("model"),
                            "agreement": agree, "dangerous_errors": danger,
                            "measured_utc": res.get("executed_utc"),
                            "history": hist}
        base["n_cases"] = res.get("n_cases")
        with open(BASELINE, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(base, fh, ensure_ascii=False, indent=1)
        print("\n  已更新基线：%s" % os.path.relpath(BASELINE, P2))
        return 0

    if problems:
        print()
        for p in problems:
            print("  [!! ] %s" % p)
        print("\n回归门槛**未通过** —— 这次 prompt/模型改动比基线差，别合并。")
        if as_json:
            print(json.dumps({"gate": "fail", "problems": problems, **res},
                             ensure_ascii=False, indent=1))
        return 1
    print("\n  [OK ] 不低于基线")
    print("\n回归门槛通过")
    if as_json:
        print(json.dumps({"gate": "pass", "agreement": agree,
                          "dangerous_errors": danger, **res},
                         ensure_ascii=False, indent=1))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="事件判定校准（人工复核集）")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--run", action="store_true", help="需要 LLM key")
    ap.add_argument("--gate", action="store_true",
                    help="回归门槛：不低于 data/calibration/baseline.json（无 key 时如实跳过）")
    ap.add_argument("--update-baseline", action="store_true",
                    help="有意更换 prompt/模型时才用：把本次结果写成新基线")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        print("=" * 74)
        print("事件判定校准集自检（schema + 覆盖 + **留出检查**）")
        print("=" * 74)
        return selftest()
    if args.gate or args.update_baseline:
        return cmd_gate(args.json, update=args.update_baseline)
    if args.run:
        return run_calibration(args.json)
    if args.list:
        meta, cases = load_cases()
        print("校准集：%s" % os.path.relpath(CASES, P2))
        print("复核人 %s ｜ 复核时间 %s ｜ n=%d"
              % (meta.get("_reviewed_by"), meta.get("_reviewed_utc"),
                 len(cases)))
        for c in cases:
            print("  %-20s [%s] %-8s %s"
                  % (c["id"], c["base"], c["expected_severity"],
                     c["headline"][:70]))
        print("\n⚠️ %s" % meta.get("_sample_size_warning"))
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
