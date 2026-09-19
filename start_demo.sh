#!/usr/bin/env bash
# ============================================================
#  项目二 · 一键开公网 Demo（Linux / macOS / WSL）
#
#  做两件事：① 起本机服务  ② 用 cloudflared 开一条临时公网隧道
#  注意：这是**临时**链接，Ctrl+C 就失效。
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

echo "============================================================"
echo "  项目二 · Execution-aware Alpha  一键公网 Demo"
echo "============================================================"

# 找 python
PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then
  echo "[X] 没找到 python3。请先安装 Python 3.11+。"
  exit 1
fi
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || {
  echo "[X] Python 版本过低：本项目用了 datetime.UTC，需要 3.11+。"
  "$PY" --version
  exit 1
}

# 找 cloudflared（没有就不开隧道，只起本机服务，并说清楚为什么不）
CF="$(command -v cloudflared || true)"
if [ -z "$CF" ]; then
  echo "[!] 没找到 cloudflared —— **不会**开公网隧道，只起本机服务。"
  echo "    装它（任选其一）："
  echo "      macOS : brew install cloudflared"
  echo "      Linux : 见 docs/43-公网可访问.md（下载单文件二进制即可，免账号）"
  echo "    装好后重跑本脚本，公网地址会自动打印出来。"
  echo
fi

echo "[1/2] 先跑自检（不需要网络、不需要 key）..."
"$PY" run_p2.py --selftest || {
  echo "[!] 自检没通过。先修好再开公网 —— 挂上去只会让评委看到同一个问题。"
  exit 1
}

echo
echo "[2/2] 启动服务 + 临时公网隧道（Ctrl+C 停止）..."
if [ -n "$CF" ]; then
  exec "$PY" -u run_p2.py --tunnel --port 8788 --cloudflared "$CF"
else
  exec "$PY" -u run_p2.py --host 127.0.0.1 --port 8788
fi
