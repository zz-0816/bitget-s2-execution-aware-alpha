#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
项目二 · 独立启动入口（Execution-aware Alpha）
================================================

**不依赖项目一的任何文件**：只读本仓库 `data/` 下的数据快照。

用法：
  python run_p2.py                    # 启动网页（默认 8788）
  python run_p2.py --port 9000
  python run_p2.py --selftest         # 只跑自检，不起服务
  python run_p2.py --demo NVDA        # 命令行跑完整决策链（不用浏览器）
"""

import argparse
import http.server
import json
import os
import socketserver
import sys
import urllib.parse

P2 = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, P2)
sys.path.insert(0, os.path.join(P2, "project2"))
sys.path.insert(0, os.path.join(P2, "tools"))

try:
    from common.console import install
    install()
except Exception:  # noqa: BLE001
    pass


def _assess(base):
    """项目二的核心能力：风险与理由引擎（**不代下单**，只给理由与条件）。"""
    from event_gate import assess
    from execution_cost import analyse_two_leg
    cost = analyse_two_leg(base, 5000.0, False, 3.0)
    a = assess(base, cost=cost, size_usd=5000.0)
    a["_cost"] = cost
    return a


class Handler(http.server.SimpleHTTPRequestHandler):
    """静态页面 + 一个 API。刻意只暴露项目二自己的端点。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=os.path.join(P2, "web"), **kw)

    def log_message(self, fmt, *a):        # 安静一点
        pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path in ("/", "/index.html"):
            self.path = "/index.html"
            return super().do_GET()
        if u.path == "/api/health":
            return self._json({"ok": True, "project": "execution-aware-alpha",
                               "version": 1})
        if u.path == "/api/assess":
            q = urllib.parse.parse_qs(u.query)
            base = (q.get("base") or ["NVDA"])[0].upper()
            try:
                a = _assess(base)
                return self._json({"ok": True, "base": base, "assess": a})
            except Exception as exc:  # noqa: BLE001
                return self._json({"ok": False, "err": "%s: %s"
                                   % (type(exc).__name__, exc)}, 500)
        if u.path == "/api/snapshot":
            p = os.path.join(P2, "data", "SNAPSHOT.md")
            if os.path.exists(p):
                with open(p, encoding="utf-8") as fh:
                    return self._json({"ok": True, "markdown": fh.read()})
            return self._json({"ok": False, "err": "无快照说明"}, 404)
        return super().do_GET()

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, indent=1).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main(argv=None):
    ap = argparse.ArgumentParser(description="项目二独立入口")
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--demo", default=None, help="命令行跑完整决策链")
    args = ap.parse_args(argv)

    if args.selftest:
        import subprocess
        rc = 0
        for cmd in ([sys.executable, os.path.join(P2, "project2", "execution_cost.py"),
                     "--selftest"],
                    [sys.executable, os.path.join(P2, "project2", "agent_team.py"),
                     "--selfcheck"],
                    [sys.executable, os.path.join(P2, "project2", "event_gate.py"),
                     "--selftest"]):
            p = subprocess.run(cmd, cwd=P2)
            rc |= p.returncode
        print("\n项目二自检%s" % ("通过" if rc == 0 else "**失败**"))
        return rc

    if args.demo:
        from agent_team import run_decision
        cost, items, debate, dec, book = run_decision(args.demo.upper(), qty_usd=5000.0)
        print("标的 %s" % args.demo.upper())
        for i in items:
            print("  %-16s %-12s 置信度 %.2f 证据 %d 条"
                  % (i["report"]["dimension"], i["report"]["verdict"],
                     i["report"]["confidence"], len(i["report"]["evidence"])))
        v = debate["verdict"]
        print("  辩论：多头 %.2f vs 空头 %.2f -> %s"
              % (v["bull_weight"], v["bear_weight"], v["stance"]))
        r = dec["risk"]
        print("  风控：%s（%d 条规则，触发 %s）"
              % (r["verdict"], r["checked_rules"], "、".join(r["hits"]) or "无"))
        print("  最终：%s ｜ %.0f USD ｜ %s"
              % (dec["final"]["stance"], dec["final"]["qty_usd"],
                 dec["final"]["why"][:80]))
        return 0

    os.chdir(P2)
    with socketserver.TCPServer(("127.0.0.1", args.port), Handler) as httpd:
        print("=" * 78)
        print("项目二 · Execution-aware Alpha   http://127.0.0.1:%d" % args.port)
        print("=" * 78)
        print("  /api/health    存活")
        print("  /api/assess?base=NVDA   风险与理由（项目二核心能力）")
        print("  /api/snapshot  数据快照的边界说明")
        print("  Ctrl+C 退出")
        httpd.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
