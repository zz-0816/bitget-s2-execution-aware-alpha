#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🔄 持续同步项目一样本（把"离线演示"变成"实时模式"的唯一途径）
================================================================

━━ 为什么需要它（先把因果讲清）━━

本仓库**不采集**数据（采集器在项目一）。而决策链读的是 `data/spread/` ——
`agent_team.time_basis()` 的规则是：

    数据比墙钟旧 > `ASOF_AUTO_AGE_MIN`(=10) 分钟  ->  basis=asof   （离线演示模式）
    10 分钟以内                                  ->  basis=wallclock（实时模式）

所以"离线演示"**不是代码限制，而是数据年龄**。⚠️ 注意：
本项目自带的 `tools/market_feed.py` 虽然能取实时行情，但它刻意只写
`data/live/`（不污染证据基座），**决策链并不读它** —— 挂着它也不会解除离线模式。
真正管用的只有一件事：**把项目一的新样本持续同步进 `data/spread/`**。

━━ 同步规则（边界写死）━━

  · **只同步"最近两天"**（北京口径：今天与昨天）。更早的日已经冻结、
    哈希被 docs/55 等文档引用 —— 覆盖它们等于毁证据，脚本**拒绝**写（除非 --days 显式指定）。
  · 轮级盘口 `2026-MM-DD.csv`：全量复制。
  · 成交 `trades-MM-DD.csv`：**现货行全量 + 永续尾部 N 行**（默认 2 万）。
    🔴 现货行一条都不许丢 —— 它是停牌判据（stale_quotes）的输入（详见 docs/55 §7.3）。
  · **原子替换**：先写 `.tmp` 再 `os.replace`，避免读取方读到半个文件
    （项目一正在往当天的文件里追加，直接覆盖会撞上"读一半"）。
  · 滚动日的文件在 `data/SNAPSHOT.md` 里被归为 **③ 运行期可变**（只查存在，不比哈希）——
    否则它每几分钟就变一次，`--verify` 会恒红。

━━ 用法 ━━

    python tools/sync_p1_samples.py --once            # 同步一次（默认）
    python tools/sync_p1_samples.py --loop            # 持续同步（每 --interval-sec 秒）
    python tools/sync_p1_samples.py --status          # 只看差了多少分钟
    python tools/sync_p1_samples.py --selftest

    # 指定项目一的数据目录（默认找同级的 ../bitgetS2_factory_trading/data/spread）
    python tools/sync_p1_samples.py --once --src D:\\path\\to\\p1\\data\\spread
"""

import argparse
import csv
import datetime as dt
import glob
import io
import json
import os
import shutil
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DST = os.path.join(BASE, "data", "spread")
DEFAULT_SRC = os.path.join(os.path.dirname(BASE), "bitgetS2_factory_trading",
                           "data", "spread")
DEFAULT_TAIL = 20000
DEFAULT_INTERVAL = 300          # 5 分钟（阈值 10 分钟，留一半余量）


def find_src(explicit=None):
    """找项目一的数据目录。

    显式给了 `--src` 就**只认它**（写错了就返回 None 并让调用方报错）——
    静默回退到别处会让人以为"同步成功了"，而其实同步的是另一个目录。
    没给 `--src` 时才按 环境变量 > 同级目录 的顺序找。
    """
    if explicit:
        return os.path.abspath(explicit) if os.path.isdir(explicit) else None
    for c in (os.environ.get("P1_SPREAD_DIR"), DEFAULT_SRC):
        if c and os.path.isdir(c):
            return os.path.abspath(c)
    return None


def cn_today():
    """北京口径的"今天"（文件名按北京日期分区，见 DATA_DICT §0）。"""
    return (dt.datetime.now(dt.UTC) + dt.timedelta(hours=8)).date()


def rolling_days(days=2):
    t = cn_today()
    return {(t - dt.timedelta(days=i)).isoformat() for i in range(days)}


def _atomic_write_rows(path, fields, rows):
    """按 ts_ms 排序后**原子**落盘（写 .tmp 再 replace）。"""
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in sorted(rows, key=lambda x: int(x["ts_ms"])):
            w.writerow(r)
    os.replace(tmp, path)
    return len(rows)


def _read_csv(path):
    with io.open(path, encoding="utf-8-sig", newline="") as fh:
        rd = csv.DictReader(fh)
        return rd.fieldnames, list(rd)


def _sync_orderbook(src, day, rounds, log=print):
    """把项目一当天的 5 档盘口**截成最后 N 轮**再落盘。

    为什么必须截：项目一单日原始盘口约 58 MB，而本仓库的规则是"保留最后 400 轮"
    （见 `tools/retruncate_orderbook.py` 与 README §3 的说明 —— 取尾段而不是首段，
    是为了让"最后一轮盘口"与"最后一笔成交"落在**同一时刻附近**）。
    两遍流式处理，不把 58 MB 读进内存。
    """
    s = os.path.join(src, "orderbook-%s.csv" % day)
    d = os.path.join(DST, "orderbook-%s.csv" % day)
    if not os.path.exists(s):
        return None
    if os.path.exists(d) and os.path.getmtime(s) <= os.path.getmtime(d):
        return None
    ts_all = set()
    with io.open(s, encoding="utf-8-sig", newline="") as fh:
        rd = csv.DictReader(fh)
        fields = rd.fieldnames
        for r in rd:
            ts_all.add(r.get("ts_ms"))
    keep = set(sorted(ts_all)[-rounds:])
    tmp = d + ".tmp"
    n = 0
    with io.open(s, encoding="utf-8-sig", newline="") as fh, \
            io.open(tmp, "w", encoding="utf-8", newline="") as out:
        w = csv.DictWriter(out, fieldnames=fields)
        w.writeheader()
        for r in csv.DictReader(fh):
            if r.get("ts_ms") in keep:
                w.writerow(r)
                n += 1
    os.replace(tmp, d)
    log("  ↻ 盘口 %s：最后 %d 轮 -> %d 行（%.2f MB）"
        % (day, len(keep), n, os.path.getsize(d) / 1e6))
    return day


def _sync_sentiment(src, day, log=print):
    """情绪/资金费：项目一有就当天的复制（本项目自己也能采，见 sentiment_sampler）。"""
    s = os.path.join(src, "sentiment-%s.csv" % day)
    d = os.path.join(DST, "sentiment-%s.csv" % day)
    if not os.path.exists(s):
        return None
    if os.path.exists(d) and os.path.getmtime(s) <= os.path.getmtime(d):
        return None
    tmp = d + ".tmp"
    shutil.copy2(s, tmp)
    os.replace(tmp, d)
    log("  ↻ 情绪 %s（%.1f KB）" % (day, os.path.getsize(d) / 1e3))
    return day


def sync_once(src, days=2, tail=DEFAULT_TAIL, allow_old=False, rounds=400, log=print):
    """同步一次。返回 {"rounds": [...], "trades": [...], "orderbook": [...], ...}。"""
    if not src:
        return {"error": "找不到项目一的数据目录（用 --src 指定，或设 P1_SPREAD_DIR）"}
    keep = rolling_days(days)
    res = {"rounds": [], "trades": [], "orderbook": [], "sentiment": [],
           "skipped": [], "rows": 0}

    # ① 轮级盘口：只碰最近 N 天
    for p in sorted(glob.glob(os.path.join(src, "2026-*.csv"))):
        day = os.path.basename(p)[:-4]
        if day not in keep and not allow_old:
            res["skipped"].append("轮级 %s（已冻结，拒绝覆盖）" % day)
            continue
        d = os.path.join(DST, day + ".csv")
        if os.path.exists(d) and os.path.getmtime(p) <= os.path.getmtime(d):
            continue
        tmp = d + ".tmp"
        shutil.copy2(p, tmp)
        os.replace(tmp, d)
        res["rounds"].append(day)
        log("  ↻ 轮级 %s（%.2f MB）" % (day, os.path.getsize(d) / 1e6))

    # ② 成交：现货全量 + 永续尾部
    for p in sorted(glob.glob(os.path.join(src, "trades-2026-*.csv"))):
        day = os.path.basename(p)[len("trades-"):-4]
        if day not in keep and not allow_old:
            continue
        d = os.path.join(DST, "trades-%s.csv" % day)
        if os.path.exists(d) and os.path.getmtime(p) <= os.path.getmtime(d):
            continue
        try:
            fields, raw = _read_csv(p)      # ⚠️ 可能撞上"正在追加"-> 读失败就跳过本轮
        except (OSError, csv.Error, ValueError) as exc:
            log("  ⏭ trades-%s 读取失败（可能正在写入）：%s" % (day, type(exc).__name__))
            continue
        spot = [r for r in raw if r.get("venue") == "spot"]
        perp = [r for r in raw if r.get("venue") == "perp"]
        rows = spot + perp[-tail:]
        n = _atomic_write_rows(d, fields, rows)
        res["trades"].append(day)
        res["rows"] += n
        log("  ↻ 成交 %s：现货 %d（**全量**）+ 永续尾部 %d = %d 行"
            % (day, len(spot), len(perp[-tail:]), n))

    # ③ 5 档盘口：只保留最后 N 轮（否则单日 58 MB）
    for day in sorted(keep):
        r = _sync_orderbook(src, day, rounds, log)
        if r:
            res["orderbook"].append(day)

    # ④ 情绪/资金费
    for day in sorted(keep):
        r = _sync_sentiment(src, day, log)
        if r:
            res["sentiment"].append(day)

    return res


def status(src, log=print):
    """报告缺口：快照最后一刻 vs 现在（分钟），以及当前会被判成哪种模式。"""
    for p in (BASE, os.path.join(BASE, "project2"), os.path.join(BASE, "common")):
        if p not in sys.path:
            sys.path.insert(0, p)
    import agent_team as at
    tb = at.time_basis()
    asof = tb.get("data_asof_ms")
    age = tb.get("data_age_min")
    log("快照最后一刻 : %s"
        % (dt.datetime.fromtimestamp(asof / 1000, dt.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
           if asof else "**读不到**"))
    log("距今         : %s" % ("%.1f 分钟" % age if age is not None else "未知"))
    log("当前判定     : basis=%s —— %s" % (tb.get("basis"), str(tb.get("why"))[:70]))
    if age is not None:
        log("离实时还差   : %s" % ("**已经是实时模式**" if tb.get("basis") == "wallclock"
                                  else "%.1f 分钟（阈值 10）" % max(0.0, age - 10.0)))
    if src:
        p = sorted(glob.glob(os.path.join(src, "2026-*.csv")))
        if p:
            log("项目一最新   : %s" % os.path.basename(p[-1]))
    return 0


# ────────────────────────────────────────────── 自检（离线，不依赖项目一）

def selftest():
    ok = True

    def chk(cond, msg):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s" % ("OK " if cond else "!! ", msg))

    import tempfile
    tmp = tempfile.mkdtemp(prefix="p1sync-")
    try:
        # 造一个假的"项目一"目录
        src = os.path.join(tmp, "src")
        os.makedirs(src)
        today = cn_today().isoformat()
        old = (cn_today() - dt.timedelta(days=5)).isoformat()
        fields = ["ts_utc", "ts_ms", "date_cn", "base", "symbol", "venue",
                  "trade_id", "side", "price", "size", "notional_usd"]
        rows = []
        for i in range(25000):      # 永续 25000 行（多于 tail），现货 3 行（必须全留）
            rows.append({"ts_utc": "2026-09-24T00:%02d:%02d.000Z" % (i // 60 % 60, i % 60),
                         "ts_ms": str(1789800000000 + i), "date_cn": today,
                         "base": "NVDA", "symbol": "NVDAUSDT", "venue": "perp",
                         "trade_id": "p%d" % i, "side": "buy", "price": "100",
                         "size": "1", "notional_usd": "100"})
        for j in range(3):
            rows.append({"ts_utc": "2026-09-24T00:00:0%d.000Z" % j,
                         "ts_ms": str(1789800000000 + j), "date_cn": today,
                         "base": "NVDA", "symbol": "RNVDAUSDT", "venue": "spot",
                         "trade_id": "s%d" % j, "side": "buy", "price": "100",
                         "size": "1", "notional_usd": "100"})
        tp = os.path.join(src, "trades-%s.csv" % today)
        with io.open(tp, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            for r in rows:
                w.writerow(r)
        # 一个"已冻结的老日"
        oldp = os.path.join(src, "trades-%s.csv" % old)
        with io.open(oldp, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()

        # 临时把 DST 指到沙盒（不动真仓库）
        global DST
        real_dst = DST
        DST = os.path.join(tmp, "dst")
        os.makedirs(DST)
        try:
            r = sync_once(src, days=2, tail=20000, log=lambda *a: None)
            got = os.path.join(DST, "trades-%s.csv" % today)
            fields2, out = _read_csv(got)
            spot = [x for x in out if x["venue"] == "spot"]
            perp = [x for x in out if x["venue"] == "perp"]
            chk(len(spot) == 3, "**现货行全量保留**：源 3 行 -> 落盘 %d 行" % len(spot))
            chk(len(perp) == 20000, "永续按 tail 截尾：源 25000 -> 落盘 %d 行" % len(perp))
            chk(not any("trades-%s" % old in s for s in r["trades"]),
                "**已冻结的老日拒绝覆盖**（%s 未被同步）" % old)
            chk(all(os.path.basename(p).startswith(("2026-", "trades-"))
                    for p in glob.glob(os.path.join(DST, "*"))),
                "落盘目录只有快照文件（无 .tmp 残留）")
            chk(not glob.glob(os.path.join(DST, "*.tmp")), "原子写：不留 .tmp")
            # 幂等：第二次不应重复写（mtime 不比源新）
            r2 = sync_once(src, days=2, tail=20000, log=lambda *a: None)
            chk(not r2["trades"], "幂等：第二次同步不再重写（源未更新）")
        finally:
            DST = real_dst
        # 老日规则
        chk(old not in rolling_days(2), "滚动窗口=最近 2 天（%s 不在内）" % old)
        chk(len(rolling_days(2)) == 2 and cn_today().isoformat() in rolling_days(2),
            "滚动窗口包含今天（北京口径）")
        chk(find_src(r"D:\definitely\not\here") is None,
            "源目录不存在时返回 None（不猜、不崩）")
    finally:
        try:
            shutil.rmtree(tmp)
        except OSError:
            pass

    print("\n持续同步工具自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


# ────────────────────────────────────────────── 入口

def intro(log=print):
    """打印启动脚本（.bat）的中文说明。

    ⚠️ 这段文字**必须**留在这里，不能写进 `.bat`：批处理文件一旦包含非 ASCII
    字节，cmd.exe 会按本机代码页（简体中文 = 936/GBK）解码，而文件是 UTF-8 ——
    一个汉字 3 字节 vs GBK 2 字节，配上**裸 LF 行尾**就会吃掉换行、把两行粘成
    一行（2026-09-25 实测事故：`echo` 变成 `?echo`、情绪采样那行丢了前缀）。
    所以 `.bat` 保持纯 ASCII，中文一律由 Python 打印。守这条规矩的是
    `tools/bat_lint.py`。
    """
    log("=" * 68)
    log(" 项目二 · 数据同步（让 Demo 保持在「实时模式」）")
    log("-" * 68)
    log(" 每轮做三件事：")
    log("   ① 从项目一拉最新样本（轮级盘口 / 成交 / 盘口五档，只写最近两天）")
    log("   ② 采情绪与资金费（项目一没在采，所以本项目自己采）")
    log("   ③ 打印新鲜度判定")
    log("")
    log(" 为什么需要它：决定「离线演示 / 实时模式」的**只有数据年龄**")
    log(" （快照最新时刻距今 ≤ 10 分钟 -> 实时）。本仓库按设计不采集数据，")
    log("   所以靠这个循环持续喂。")
    log("")
    log(" 关掉这个窗口就停；要长期挂着就把它最小化。")
    log(" 随时查状态：python tools\\sync_p1_samples.py --status")
    log("=" * 68)
    log("")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="🔄 持续同步项目一样本 -> data/spread/")
    ap.add_argument("--src", default=None, help="项目一 data/spread 目录")
    ap.add_argument("--once", action="store_true", help="同步一次（默认）")
    ap.add_argument("--loop", action="store_true", help="持续同步")
    ap.add_argument("--interval-sec", type=int, default=DEFAULT_INTERVAL)
    ap.add_argument("--days", type=int, default=2, help="只同步最近 N 天（默认 2）")
    ap.add_argument("--tail", type=int, default=DEFAULT_TAIL, help="永续保留尾部行数")
    ap.add_argument("--allow-old", action="store_true",
                    help="⚠️ 允许覆盖已冻结的老日（一般不用，会改哈希）")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--intro", action="store_true",
                    help="打印本窗口在做什么（给 .bat 用：中文提示只能由 Python 打印）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)

    if a.selftest:
        return selftest()
    if a.intro:
        return intro()

    src = find_src(a.src)
    if a.status:
        return status(src)
    if not src:
        print("❌ 找不到项目一的数据目录：%s" % (a.src or DEFAULT_SRC))
        print("   用 --src 指定，或设环境变量 P1_SPREAD_DIR")
        return 1

    print("源   ：%s" % src)
    print("目标 ：%s（只写最近 %d 天）" % (DST, a.days))
    if a.loop:
        print("持续同步：每 %d 秒一次（Ctrl+C 退出）" % a.interval_sec)
        while True:
            try:
                r = sync_once(src, a.days, a.tail, a.allow_old)
                if a.json:
                    print(json.dumps({"ts": dt.datetime.now(dt.UTC).isoformat(),
                                      "rounds": r["rounds"], "trades": r["trades"]}))
                elif not r["rounds"] and not r["trades"]:
                    print("  · %s 无更新" % dt.datetime.now(dt.UTC).strftime("%H:%M:%S"))
            except Exception as exc:  # noqa: BLE001 —— 单轮失败不能中断长期同步
                print("  ⚠️ 本轮同步失败（继续下一轮）：%s: %s"
                      % (type(exc).__name__, exc))
            time.sleep(max(30, a.interval_sec))
    r = sync_once(src, a.days, a.tail, a.allow_old)
    if a.json:
        print(json.dumps(r, ensure_ascii=False))
    elif not r["rounds"] and not r["trades"]:
        print("  无更新（两边已经一致）")
    status(src)
    return 0


if __name__ == "__main__":
    sys.exit(main())
