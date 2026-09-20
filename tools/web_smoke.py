#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
页面渲染冒烟（真起服务 + 真渲染 + 真检查）
============================================

背景：这个仓库最危险的一次故障不是算错数，而是**页面整页空白**——
`web/app.js` 调用的是项目一服务才有的端点，独立跑起来什么都不显示，
而当时所有自检都是绿的（没有一个自检去看"页面渲染出了什么"）。

本工具补上这一环：

  1. 用**本仓库真实的路由**起一个服务（`run_p2.make_handler`）；
  2. 把页面会用到的每个端点真打一遍，取回**真实数据**当夹具；
  3. 交给 `tools/web_render_check.js`（Node）在最小 DOM 里跑一遍 `web/app.js`；
  4. 逐个区块检查是否真的渲染出内容，并对照 `web/index.html` 的 id 抓"漂移"。

Node 不可用时**跳过**并明确说"跳过"，不伪装成通过。

用法：
  python tools/web_smoke.py
  python tools/web_smoke.py --fixtures _tmp_web_fixtures.json   # 保留夹具供排查
"""

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.request

P2 = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, P2)

ENDPOINTS = ["/api/health", "/api/bases", "/api/assess?base=NVDA",
             "/api/decision?base=NVDA&qty=5000",
             # ⭐ 页面**默认**带的是 `&position=auto`（读真实在途订单；没有就不带）。
             #    这个 URL 必须在夹具里：夹具匹配是"参数子集"式的，只备无参 URL 时
             #    它也会命中，于是页面真实走的那条路径**根本没被测到** ——
             #    实测踩到一次（默认值从 demo 改成 auto 后，执行进度面板的断言
             #    悄悄换成了另一条分支的内容）。
             "/api/decision?base=NVDA&qty=5000&position=auto",
             # ⭐ 再备一份合成演示单：它带 synthetic 标记与"只成交一腿"，
             #    是执行进度面板**信息最全**的那条渲染路径（裸露敞口 + 处置口径）。
             "/api/decision?base=NVDA&qty=5000&position=demo",
             # ⭐ 再取一份"META"的决策：本快照里它会被 agent 假设一票否决
             #    （order 为 null / stand_down），是**最容易渲染出 undefined** 的那条路径
             "/api/decision?base=META&qty=5000",
             "/api/overview?qty=5000",
             "/api/params", "/api/snapshot", "/api/alerts"]


def collect(verbose=True):
    """起服务 -> 取真实数据 -> 关服务。返回 {规范化路径: 响应体}。"""
    import http.server
    import run_p2

    handler = run_p2.make_handler()
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    fixtures = {}
    try:
        for ep in ENDPOINTS:
            with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, ep),
                                        timeout=180) as r:
                body = json.loads(r.read().decode("utf-8"))
            # 决策端点按**完整 URL** 存（要有 NVDA 与 META 两份）；
            # 其余按路径存（前端只会那样调）。
            key = ep if ep.startswith("/api/decision") else ep.split("?")[0]
            fixtures[key] = body
            if verbose:
                print("  取数 %-34s %d 字节" % (ep, len(json.dumps(body))))
    finally:
        httpd.shutdown()
        httpd.server_close()
    return fixtures


def main(argv=None):
    ap = argparse.ArgumentParser(description="页面渲染冒烟（Node 最小 DOM）")
    ap.add_argument("--fixtures", default=os.path.join(P2, "_tmp_web_fixtures.json"),
                    help="夹具落盘路径（排查用；默认写到临时文件）")
    ap.add_argument("--keep", action="store_true", help="保留夹具文件")
    args = ap.parse_args(argv)

    print("=" * 74)
    print("页面渲染冒烟（真起服务 → 真取数 → 真渲染 → 真检查）")
    print("=" * 74)

    node = shutil.which("node")
    if not node:
        print("  [--] 未找到 node，**跳过**渲染检查（不是通过）。")
        print("       装了 Node 再跑一次即可：python tools/web_smoke.py")
        return 0

    fixtures = collect()
    with io.open(args.fixtures, "w", encoding="utf-8") as fh:
        json.dump(fixtures, fh, ensure_ascii=False)

    harness = os.path.join(P2, "tools", "web_render_check.js")
    p = subprocess.run([node, harness, args.fixtures], cwd=P2,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    try:
        rep = json.loads(p.stdout)
    except (ValueError, TypeError):
        print(p.stdout[-2000:])
        print(p.stderr[-2000:])
        print("\n页面渲染冒烟**失败**（Node 没给出可解析的结果）")
        return 1

    for n in rep.get("notes") or []:
        print("  [OK ] %s" % n)
    for prob in rep.get("problems") or []:
        print("  [!! ] %s" % prob)
    ok = bool(rep.get("ok"))
    print("\n页面渲染冒烟%s" % ("通过" if ok else "**失败**"))

    # ---- 告警弹窗：接线了不等于能用，得**证明它真的弹出来** ----
    # 真实快照里没有 data/positions/alerts.json（那是运行期产物），
    # 所以这里**明确注入一条合成告警**，只为验证前端那条渲染路径。
    # 如实标注：这条合成告警不参与任何结论，只用于渲染检查。
    probe = dict(fixtures)
    probe["/api/alerts"] = {
        "ok": True, "exists": True, "_synthetic_for_render_check": True,
        "generated_utc": "2026-09-19T00:00:00+00:00",
        "alerts": [{"level": "critical", "base": "NVDA", "code": "naked_leg",
                    "title": "只成交了现货腿 —— 存在裸露的方向性敞口",
                    "detail": "缺失：永续腿 ｜ 规模 5000 USD（合成样本，仅用于渲染检查）",
                    "action": "立刻吃单补上永续腿", "ts": "2026-09-19T00:00:00+00:00"}]}
    probe_path = args.fixtures + ".toast.json"
    with io.open(probe_path, "w", encoding="utf-8") as fh:
        json.dump(probe, fh, ensure_ascii=False)
    p2 = subprocess.run([node, harness, probe_path], cwd=P2,
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace")
    try:
        rep2 = json.loads(p2.stdout)
        n_toast = int(rep2.get("toastCount") or 0)
    except (ValueError, TypeError):
        n_toast = 0
        print("  [!! ] 注入告警后渲染失败：%s" % (p2.stderr or p2.stdout)[-300:])
    if n_toast >= 1:
        print("  [OK ] 告警弹窗渲染路径可用（注入 1 条合成告警 → 弹出 %d 个）" % n_toast)
    else:
        ok = False
        print("  [!! ] 告警弹窗没有渲染出来（/api/alerts 的告警没进 DOM）")
    if not args.keep:
        for f in (args.fixtures, probe_path):
            try:
                os.remove(f)
            except OSError:
                pass
    print("\n页面渲染冒烟（含告警弹窗）%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
