#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
盘口快照重截断（首段 → **尾段**）
==================================

为什么需要这个工具（一次真实的内部不一致）
------------------------------------------
上游导出脚本 `export_p2_snapshot.py`（项目一仓库内）对盘口保留的是**前** N 轮，
而它对 trades 保留的是**尾部** N 行。两者拼在一起，快照就自相矛盾了：

| 数据 | 截断方式 | 实际覆盖到 |
|---|---|---|
| `orderbook-*.csv` | 前 400 轮 | **09-18 19:19** |
| `trades-*.csv` | 尾部 8 万行 | **09-19 07:16** |

差了 12 小时。而本项目的演示卖点恰恰是"**同一时刻的盘口 + 逐笔成交**"
（腿风险、成交率、深度约束都要求两者取自同一段市场）。
盘口停在昨天、成交到了今天，看起来一切正常，实际上两个判据在描述不同的市场。

所以：把盘口改成**保留最后 N 轮**，与 trades 的尾部对齐。

用法：
  python tools/retruncate_orderbook.py --src <项目一>\\data\\spread\\orderbook-2026-09-19.csv
  python tools/retruncate_orderbook.py --src ... --rounds 400 --out data/spread/orderbook-2026-09-19.csv
  python tools/retruncate_orderbook.py --selftest        # 合成小样本自检（不碰真实数据）

⚠️ 本工具**只读源文件、只写目标文件**，不会修改项目一的任何东西。
"""

import argparse
import collections
import csv
import datetime as dt
import hashlib
import io
import os
import shutil
import sys

P2 = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, P2)
try:
    from common.console import install as _install_console
    _install_console()
except Exception:  # noqa: BLE001
    pass


def retruncate(src, dst, rounds=400, verbose=True):
    """保留 `src` 里**最后 rounds 个不同的 ts_ms** 所对应的全部行。

    返回统计 dict。**两次遍历**：先数出要保留的 ts 集合，再写出。
    （一遍流式写法在这里不行：读到第 N 轮时并不知道后面还有没有更多轮。）
    """
    with open(src, newline="", encoding="utf-8") as fh:
        rd = csv.reader(fh)
        head = next(rd)
        try:
            ts_idx = head.index("ts_ms")
        except ValueError:
            raise SystemExit("源文件缺少 ts_ms 列：%s" % head)
        order = collections.OrderedDict()
        total = 0
        for row in rd:
            total += 1
            if len(row) > ts_idx:
                order.setdefault(row[ts_idx], 0)
    keep = set(list(order)[-rounds:])
    if not keep:
        raise SystemExit("源文件没有数据行：%s" % src)

    kept, per_round = 0, collections.Counter()
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    with open(src, newline="", encoding="utf-8") as fh, \
            io.open(dst, "w", encoding="utf-8", newline="\n") as fo:
        rd = csv.reader(fh)
        wr = csv.writer(fo)
        wr.writerow(next(rd))
        for row in rd:
            if len(row) > ts_idx and row[ts_idx] in keep:
                wr.writerow(row)
                kept += 1
                per_round[row[ts_idx]] += 1

    ks = sorted(int(k) for k in keep)
    info = {
        "src": src, "dst": dst, "src_rows": total, "rows": kept,
        "rounds": len(keep),
        "ts_from": ks[0], "ts_to": ks[-1],
        "from_utc": dt.datetime.fromtimestamp(ks[0] / 1000, dt.UTC).isoformat(),
        "to_utc": dt.datetime.fromtimestamp(ks[-1] / 1000, dt.UTC).isoformat(),
        "rows_per_round_min": min(per_round.values()),
        "rows_per_round_max": max(per_round.values()),
        "partial_rounds": sum(1 for v in per_round.values() if v < 190),
        "bytes": os.path.getsize(dst),
        "sha256_16": hashlib.sha256(open(dst, "rb").read()).hexdigest()[:16],
    }
    if verbose:
        print("已重截断：%s" % os.path.relpath(dst, P2))
        print("  行数 %s -> %s ｜ 轮数 %d"
              % (format(total, ","), format(kept, ","), info["rounds"]))
        print("  覆盖时段 %s -> %s" % (info["from_utc"], info["to_utc"]))
        print("  每轮行数 %d~%d（其中 %d 轮不足 190 行 —— 采样器当时未写全）"
              % (info["rows_per_round_min"], info["rows_per_round_max"],
                 info["partial_rounds"]))
        print("  %.2f MB ｜ SHA256(16) %s"
              % (info["bytes"] / 1e6, info["sha256_16"]))
        print("  ⚠️ 改了它就要刷新清单：python tools/snapshot_manifest.py")
    return info


def selftest():
    """合成样本自检：证明"取最后 N 轮"这件事本身是对的。"""
    ok = True

    def chk(cond, msg):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s" % ("OK " if cond else "!! ", msg))

    # 用**仓库内**的临时目录，而不是系统 TEMP：
    # ① 某些受限环境里系统临时目录不可写（本机实测 PermissionError）；
    # ② `_tmp_*` 已在 .gitignore 里，不会污染提交。
    tmp = os.path.join(P2, "_tmp_retrunc")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    src = os.path.join(tmp, "ob.csv")
    dst = os.path.join(tmp, "ob_out.csv")
    # 5 轮，每轮 4 行；另加一轮不完整的（2 行）
    rows = [("ts_utc", "ts_ms", "base", "level")]
    for r in range(5):
        for i in range(4):
            rows.append(("t%d" % r, str(1000 + r), "X", str(i)))
    for i in range(2):
        rows.append(("t9", "2000", "X", str(i)))       # 最后一轮只有 2 行
    with io.open(src, "w", encoding="utf-8", newline="\n") as fh:
        csv.writer(fh).writerows(rows)

    info = retruncate(src, dst, rounds=2, verbose=False)
    chk(info["rounds"] == 2, "只保留最后 2 轮（实际 %d）" % info["rounds"])
    chk(info["rows"] == 6, "行数 = 4 + 2（实际 %d）" % info["rows"])
    chk(info["ts_from"] == 1000 + 4, "起点是**第 5 轮**（ts=%d）" % info["ts_from"])
    chk(info["ts_to"] == 2000, "终点是最后那一轮（ts=%d）" % info["ts_to"])
    with open(dst, newline="", encoding="utf-8") as fh:
        got = list(csv.reader(fh))
    chk(got[0] == list(rows[0]), "表头被保留")
    chk(int(got[1][1]) == 1004, "第一行数据来自最后 2 轮（ts=%s）" % got[1][1])
    chk(all(int(r[1]) in (1004, 2000) for r in got[1:]), "没有混进更早的轮次")

    # 源文件不得被修改
    with open(src, newline="", encoding="utf-8") as fh:
        n_src = sum(1 for _ in fh)
    chk(n_src == len(rows), "源文件**未被改动**（仍 %d 行）" % n_src)

    shutil.rmtree(tmp, ignore_errors=True)
    print("\n盘口重截断自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="盘口快照重截断（保留最后 N 轮）")
    ap.add_argument("--src", help="源盘口 CSV（项目一的原始文件，只读）")
    ap.add_argument("--out", default=None,
                    help="输出路径（默认按源文件名写到本仓库 data/spread/）")
    ap.add_argument("--rounds", type=int, default=400)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        print("=" * 74)
        print("盘口重截断自检（保留最后 N 轮）")
        print("=" * 74)
        return selftest()
    if not args.src:
        ap.error("给 --src（或 --selftest）")
    if not os.path.exists(args.src):
        print("找不到源文件：%s" % args.src)
        return 2
    out = args.out or os.path.join(P2, "data", "spread",
                                   os.path.basename(args.src))
    retruncate(args.src, out, args.rounds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
