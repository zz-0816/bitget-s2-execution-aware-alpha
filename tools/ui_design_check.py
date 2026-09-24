#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""前端设计的**可验证检查**（改样式前先跑，改完再跑）
================================================================

为什么要有它
------------
UI 改动最容易出的不是"不好看"，而是**悄悄坏了**：
  · JS 里给元素加的类，CSS 里根本没定义 → 那个状态永远不显示（或显示成别的颜色）；
  · CSS 里写好的类，HTML/JS 里从来不用 → 死代码，越积越多；
  · 深色主题下文字对比度不够 → 评委看不清，而这在开发机上"看着还行"；
  · 忘了 `prefers-reduced-motion` → 对动效敏感的用户直接难受。

这些都能**机器查**，就不该靠肉眼。本工具做四件事：

  ① 类名一致性：JS/HTML 里用到的类 vs CSS 里定义的类（双向差集）
  ② 对比度：按 CSS 里的 `--var` 取值，算 WCAG 对比度（正文 ≥4.5 / 大字 ≥3.0）
  ③ 无障碍：`prefers-reduced-motion` 兜底是否存在、`:focus-visible` 是否处理
  ④ 字面标记：静态文件里不该出现 `**` 这类 markdown 残留

用法：
  python tools/ui_design_check.py            # 全部检查
  python tools/ui_design_check.py --json     # 机器可读
  python tools/ui_design_check.py --strict   # 死 CSS 也算失败（默认只提示）
"""

import argparse
import io
import json
import os
import re
import sys

P2 = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, P2)
try:
    from common.console import install as _install_console
    _install_console()
except Exception:  # noqa: BLE001
    pass

WEB = os.path.join(P2, "web")
CSS = os.path.join(WEB, "styles.css")
HTML = os.path.join(WEB, "index.html")
JS = os.path.join(WEB, "app.js")

# 这些类是本文件**故意**定义但可能暂时没用的（保留给状态/未来），不算死代码
ALLOW_UNUSED = {"flash-up", "flash-down", "skeleton", "num", "skip-link"}

# 需要检查对比度的语义组合：(前景变量, 背景变量, 最低要求, 说明)
CONTRAST_PAIRS = [
    ("--text", "--bg", 4.5, "正文 on 页面底色"),
    ("--text", "--surface", 4.5, "正文 on 卡片"),
    ("--text-dim", "--bg", 4.5, "次要文字 on 底色"),
    ("--text-dim", "--surface-2", 4.5, "次要文字 on 次级面"),
    ("--accent-text", "--bg", 4.5, "链接/强调 on 底色"),
    ("--on-accent", "--accent", 4.5, "实心按钮文字 on 强调色"),
    ("--ok", "--bg", 4.5, "成功色 on 底色"),
    ("--warn", "--bg", 4.5, "警示色 on 底色"),
    ("--danger", "--bg", 4.5, "危险色 on 底色"),
    ("--danger", "--danger-bg", 4.5, "危险文字 on 危险底"),
    ("--warn", "--warn-bg", 4.5, "警示文字 on 警示底"),
    ("--ok", "--ok-bg", 4.5, "成功文字 on 成功底"),
]


def _read(p):
    with io.open(p, encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------- ① 类名

def classes_in_css(text):
    """CSS 里定义过的类名（含 .a.b 这种复合选择器拆开）。"""
    body = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    out = set()
    for m in re.finditer(r"\.(-?[_a-zA-Z][\w-]*)", body):
        out.add(m.group(1))
    return out


def classes_in_html(text):
    out = set()
    for m in re.finditer(r'class="([^"]*)"', text):
        out.update(m.group(1).split())
    return out


def classes_in_js(text):
    """JS 里动态加的类：class="..."、className = '...'、classList.add('...')。"""
    out = set()
    for m in re.finditer(r"""class=\\?["']([^"'\\]+)""", text):
        out.update(m.group(1).split())
    for m in re.finditer(r"""className\s*=\s*\\?["']([^"'\\]+)""", text):
        out.update(m.group(1).split())
    for m in re.finditer(r"""classList\.(?:add|remove|toggle)\(\s*['"]([\w-]+)""", text):
        out.add(m.group(1))
    # 三元里拼的：' veredict-bar ' + esc(...) 这种拿不到，用"已知状态词"兜底
    for m in re.finditer(r"""['"]([a-z][\w-]*)\s*['"]\s*\+""", text):
        pass
    return out


# ---------------------------------------------------------------- ② 对比度

def css_vars(text):
    """从 :root 里取 --var，并**解析 var() 链**。

    ⚠️ 关键：语义别名是 `--bg: var(--s1);` 这种形式，只匹配 `#hex` 会漏掉它们
    （初版就漏了，报了一屏"变量缺失"）。所以这里做两步：先取原始值，
    再沿 var() 链一路解析到真正的颜色。
    """
    raw = {}
    for m in re.finditer(r"(--[\w-]+)\s*:\s*([^;]+);", text):
        raw[m.group(1)] = m.group(2).strip()

    def resolve(name, depth=0):
        if depth > 8 or name not in raw:
            return None
        v = raw[name]
        m = re.fullmatch(r"var\(\s*(--[\w-]+)\s*\)", v)
        if m:
            return resolve(m.group(1), depth + 1)
        m = re.match(r"^(#[0-9a-fA-F]{3,8})\b", v)
        return m.group(1) if m else None

    return {k: resolve(k) for k in raw if resolve(k)}


def _lum(hexs):
    h = hexs.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))

    def f(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def contrast(a, b):
    la, lb = _lum(a), _lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


# ---------------------------------------------------------------- 主流程

def run(strict=False):
    problems, notes, warns = [], [], []

    css = _read(CSS)
    html = _read(HTML)
    js = _read(JS)

    # ---- ① 类名一致性 ----
    in_css = classes_in_css(css)
    used = classes_in_html(html) | classes_in_js(js)
    # 这些类只在 JS 里以**拼字符串**的方式产生（'s-' + stance、'badge ' + verdict …），
    # 静态扫描拿不到调用点 —— 两个方向都要放行，否则会假报"没定义"和"没人用"。
    DYN = {"s-", "s-proceed", "s-caution", "s-stand_down", "proceed", "caution",
           "stand_down", "favorable", "unfavorable", "neutral", "veto", "agent",
           "bull", "bear", "info", "best", "on", "err", "active", "invalid",
           "hit", "agent-rule", "pos", "neg", "r-line", "r-warn", "r-cell",
           "basis-asof", "num", "skip-link", "flash-up", "flash-down", "skeleton",
           # 分析师卡片的严重度色条与行级批注：类名来自 JS 里的映射对象
           # （VERDICT_CLS[verdict] / KIND_CLS[kind] —— 完整字面量，但不写在 class=" 里），
           # 静态扫描看不到调用点。这正是白名单存在的理由。
           "v-unfavorable", "v-favorable", "v-neutral", "ev-neg", "ev-legend"}
    missing = sorted(c for c in used if c not in in_css and c not in DYN)
    unused = sorted(c for c in in_css
                    if c not in used and c not in ALLOW_UNUSED and c not in DYN)
    if missing:
        problems.append("**JS/HTML 用到、CSS 里没定义**的类（这些状态会显示不对）：%s"
                        % "、".join(missing))
    else:
        notes.append("类名一致：JS/HTML 用到的类在 CSS 里都有定义")
    if unused:
        msg = "CSS 里定义但没人用的类（死代码候选）：%s" % "、".join(unused)
        (problems if strict else warns).append(msg)
    else:
        notes.append("没有死 CSS")

    # ---- ② 对比度 ----
    v = css_vars(css)
    bad = []
    for fg, bg, need, label in CONTRAST_PAIRS:
        if fg not in v or bg not in v:
            bad.append("%s：变量缺失（%s / %s）" % (label, fg, bg))
            continue
        r = contrast(v[fg], v[bg])
        if r < need:
            bad.append("%s：%.2f < %.1f（%s on %s）"
                       % (label, r, need, v[fg], v[bg]))
    if bad:
        problems.append("对比度不达标（WCAG AA）：" + "；".join(bad))
    else:
        notes.append("对比度全部达标（%d 组，最低 %.2f）"
                     % (len(CONTRAST_PAIRS),
                        min(contrast(v[f], v[b]) for f, b, _n, _l in CONTRAST_PAIRS
                            if f in v and b in v)))

    # ---- ③ 无障碍 ----
    if "prefers-reduced-motion" in css:
        notes.append("有 prefers-reduced-motion 兜底 ✅")
    else:
        problems.append("缺 prefers-reduced-motion 兜底（对动效敏感的用户会难受）")
    if ":focus-visible" in css:
        notes.append("有 :focus-visible 焦点环 ✅")
    else:
        problems.append("缺 :focus-visible（键盘用户看不出焦点在哪）")

    # ---- ④ 字面标记 ----
    #    ⚠️ 要**区分注释与真正的输出**：注释里写 `**` 无所谓，模板字符串里写
    #    `**` 才会原样显示到页面上。初版没做这个区分，把自己解释规则的注释也报了。
    lit = []
    for name, txt in (("index.html", html), ("app.js", js)):
        in_block = False
        for i, ln in enumerate(txt.splitlines(), 1):
            s = ln.strip()
            if in_block:
                if "*/" in s:
                    in_block = False
                continue
            if s.startswith("/*"):
                if "*/" not in s:
                    in_block = True
                continue
            if s.startswith(("//", "*", "<!--")):
                continue
            if "**" in ln:
                lit.append("%s:%d" % (name, i))
    if lit:
        warns.append("静态文件里疑似出现字面 `**`（页面上会原样显示成星号）：%s"
                     % "、".join(lit[:6]))
    else:
        notes.append("静态文件没有字面 `**` [OK]")

    return problems, warns, notes


def main(argv=None):
    ap = argparse.ArgumentParser(description="前端设计可验证检查")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true", help="死 CSS 也算失败")
    args = ap.parse_args(argv)

    problems, warns, notes = run(strict=args.strict)
    if args.json:
        print(json.dumps({"ok": not problems, "problems": problems,
                          "warnings": warns, "notes": notes},
                         ensure_ascii=False, indent=1))
        return 1 if problems else 0

    print("=" * 74)
    print("前端设计检查（类名一致性 / 对比度 / 无障碍 / 字面标记）")
    print("=" * 74)
    for n in notes:
        print("  [OK ] %s" % n)
    for w in warns:
        print("  [ ~ ] %s" % w)
    for p in problems:
        print("  [!! ] %s" % p)
    print("\n前端设计检查%s" % ("通过" if not problems else "**失败**"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
