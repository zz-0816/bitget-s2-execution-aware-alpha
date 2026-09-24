#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启动脚本（.bat / .cmd）卫生检查 —— 这个检查器是被一次真事故逼出来的。

为什么要有它（2026-09-25 实测事故）
-----------------------------------
`启动数据同步.bat` 被写成 **UTF-8 正文 + 裸 LF 行尾**，用户双击后 cmd 报：

    '竴' 不是内部或外部命令
    '鍐荤粨鐨勫巻鍙叉棩涓嶄細琚覆鐩?echo' 不是内部或外部命令
    '\\sentiment_sampler.py' 不是内部或外部命令

机制（逐字节推出来的，不是猜的）：

  · cmd.exe 按**当前代码页**解码批处理文件的每一行。简体中文系统 = 936(GBK)，
    而文件是 UTF-8 —— 一个汉字 UTF-8 占 **3** 字节、GBK 占 **2** 字节，
    于是**奇数个汉字**会让行尾剩一个"悬空"的引导字节。
  · 悬空字节必须和**下一个字节**配对，而下一字节就是行尾：
      - 行尾是 `\\r\\n` → `\\r`(0x0D) 不是合法 GBK 尾字节 → 最多吐个替换字符，
        **换行还在**（所以这类文件只是注释显示乱码，不一定坏）；
      - 行尾是**裸 `\\n`** → 0x0A 同样不合法，但宽松解码会**把 `\\n` 一起吞掉**
        → **两行粘成一行** → 下一行的 `echo` 被当成命令名、
        `python tools\\sentiment_sampler.py` 前半截被吃掉，只剩 `\\sentiment_sampler.py`。
  · 结论：**致命的是"裸 LF 行尾"，UTF-8 中文是把它引爆的引信。** 两条都要管。

规则
----
  R1 行尾必须是 CRLF（出现裸 LF = 失败）        —— 见上面的机制，这是真凶
  R2 正文必须纯 ASCII（一个非 ASCII 字节都不许） —— 中文一律交给 Python 打印
  R3 不许有 UTF-8 BOM                           —— BOM 会被 cmd 当成正文
  R4 文件里提到的 `.py` / `.bat` 必须真实存在    —— 脚本改过名会让 bat **静默失效**

用法
----
    python tools/bat_lint.py            # 扫描仓库里所有 .bat / .cmd
    python tools/bat_lint.py --selftest # 自检（构造坏文件，验证每条规则真的会响）
"""

import io
import os
import re
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 扫描时跳过的目录：`.git` 不是我们的代码；`_ui_v2_backup` 是旧 UI 备份（故意不入库）
SKIP_DIRS = {".git", "_ui_v2_backup", "__pycache__", "node_modules"}

# 启动脚本正文里出现的路径引用（`.py` / `.bat` / `.cmd`），用于 R4。
# ⚠️ 末尾的 `(?![A-Za-z0-9_])` 是必须的：否则 `https://www.python.org/`
#    会被匹配成 `www.py`（把扩展名当成 URL 域名的一部分）—— 实测踩到过。
REF_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_\-\\/\.]*\.(?:py|bat|cmd)(?![A-Za-z0-9_])",
                    re.I)


def scan_file(path):
    """检查单个批处理文件。返回问题列表（每条是 (规则, 说明)）。"""
    problems = []
    raw = io.open(path, "rb").read()
    if not raw:
        return [("R0", "文件是空的")]

    # R3 BOM
    if raw.startswith(b"\xef\xbb\xbf"):
        problems.append(("R3", "有 UTF-8 BOM（BOM 字节会被 cmd 当成正文的一部分）"))

    # R1 行尾：裸 LF 的数量（CRLF 里的 LF 不算）
    crlf = raw.count(b"\r\n")
    bare_lf = raw.count(b"\n") - crlf
    bare_cr = raw.count(b"\r") - crlf
    if bare_lf:
        # 找出第一处裸 LF 的行号与内容，方便直接定位
        pos, line_no = 0, 0
        while True:
            i = raw.find(b"\n", pos)
            if i < 0:
                break
            if i == 0 or raw[i - 1] != 0x0D:
                line_no = raw[:i].count(b"\n") + 1
                seg = raw[max(0, i - 60):i]
                problems.append(
                    ("R1", "第 %d 行是**裸 LF** 行尾（全文件裸 LF %d 处）—— cmd 会把下一行粘上来；"
                           "该行末尾：%s" % (line_no, bare_lf,
                                           seg.decode("latin-1")[-40:])))
            pos = i + 1
        if not problems or problems[-1][0] != "R1":
            problems.append(("R1", "存在裸 LF 行尾 %d 处" % bare_lf))
    if bare_cr:
        problems.append(("R1", "存在孤立 CR %d 处（行尾应是 CRLF）" % bare_cr))

    # R2 非 ASCII
    bad = [(i, b) for i, b in enumerate(raw) if b > 127]
    if bad:
        first = bad[0][0]
        line_no = raw[:first].count(b"\n") + 1
        # 该行的 ASCII 近似（非 ASCII 用 ? 代替），足够定位
        line_raw = raw.split(b"\n")[line_no - 1] if line_no <= raw.count(b"\n") + 1 else b""
        approx = "".join(chr(b) if b < 128 else "?" for b in line_raw).rstrip("\r")[:70]
        problems.append(
            ("R2", "有 %d 个非 ASCII 字节（首个在第 %d 行）—— 中文请交给 Python 打印；"
                   "该行 ASCII 近似：%s" % (len(bad), line_no, approx.strip())))

    # R4 引用的脚本是否存在
    text = raw.decode("latin-1")
    seen = set()
    for m in REF_RE.finditer(text):
        tok = m.group(0)
        if tok in seen:
            continue
        seen.add(tok)
        rel = tok.replace("\\", "/").lstrip("./")
        if not rel:
            continue
        if not os.path.exists(os.path.join(BASE, rel)):
            problems.append(("R4", "引用了不存在的脚本 `%s`" % tok))
    return problems


def iter_bats(root=BASE):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in sorted(filenames):
            if fn.lower().endswith((".bat", ".cmd")):
                yield os.path.join(dirpath, fn)


def check(root=BASE, log=print):
    files = sorted(iter_bats(root))
    log("启动脚本卫生检查（.bat / .cmd）：共 %d 个文件" % len(files))
    log("  R1 行尾全 CRLF ｜ R2 纯 ASCII ｜ R3 无 BOM ｜ R4 引用的脚本存在")
    log("")
    n_bad = 0
    for p in files:
        rel = os.path.relpath(p, root).replace("\\", "/")
        problems = scan_file(p)
        if not problems:
            log("  [OK ] %s" % rel)
            continue
        n_bad += 1
        log("  [!! ] %s" % rel)
        for rule, why in problems:
            log("         %s %s" % (rule, why))
    log("")
    if n_bad:
        log("有 %d 个启动脚本不合格。" % n_bad)
        log("修法：用文本编辑器把文件另存为 ANSI/ASCII、行尾 CRLF；")
        log("      中文提示一律改成由 Python 脚本打印（见 启动Demo.bat 里的说明）。")
    else:
        log("全部合格：双击不会因为编码/行尾把命令行拆坏。")
    return 1 if n_bad else 0


def _write(path, data):
    with io.open(path, "wb") as fh:
        fh.write(data)


def selftest(log=print):
    """构造"坏文件"，验证每条规则真的会响 —— 检查器自己也要被检。"""
    ok = True
    tmp = tempfile.mkdtemp(prefix="batlint_")

    def chk(cond, msg):
        nonlocal ok
        log("  [%s] %s" % ("OK " if cond else "!! ", msg))
        if not cond:
            ok = False

    def rules_of(name, data):
        p = os.path.join(tmp, name)
        _write(p, data)
        return [r for r, _ in scan_file(p)]

    log("bat_lint 自检")
    log("")

    # ① 好文件：纯 ASCII + CRLF + 引用真实存在的脚本 -> 零问题
    good = (b"@echo off\r\n"
            b"REM  smoke fixture\r\n"
            b"py -3 tools\\bat_lint.py\r\n")
    chk(rules_of("good.bat", good) == [],
        "纯 ASCII + CRLF -> 零问题（否则检查器会满世界误报）")

    # ② R1：裸 LF（**这次真事故的形态**）
    lf = good.replace(b"\r\n", b"\n")
    r = rules_of("lf.bat", lf)
    chk("R1" in r, "裸 LF 行尾 -> 报 R1（实测事故里就是它把两行粘成一行）")

    # ③ R2：UTF-8 中文 + CRLF（旧仓库里那三个文件的形态）
    u8 = ("@echo off\r\nREM 中文注释\r\necho hi\r\n").encode("utf-8")
    r = rules_of("utf8.bat", u8)
    chk("R2" in r, "UTF-8 中文 -> 报 R2（注释会变乱码；且离'粘行'只差行尾一变）")

    # ④ 事故组合：UTF-8 + 裸 LF（正是 启动数据同步.bat 原来的样子）
    r = rules_of("both.bat", u8.replace(b"\r\n", b"\n"))
    chk("R1" in r and "R2" in r, "UTF-8 + 裸 LF -> R1 与 R2 同时报（事故原形）")

    # ⑤ R3：BOM
    r = rules_of("bom.bat", b"\xef\xbb\xbf" + good)
    chk("R3" in r, "UTF-8 BOM -> 报 R3")

    # ⑥ R4：引用不存在的脚本（脚本改名后 bat 会静默失效）
    r = rules_of("ref.bat", b"@echo off\r\npy -3 tools\\no_such_tool_here.py\r\n")
    chk("R4" in r, "引用不存在的 .py -> 报 R4（改过名 = bat 静默失效）")

    # ⑦ 真实仓库：全部必须合格（这条才是这次修完要守住的）
    real = sorted(iter_bats())
    bad = [os.path.basename(p) for p in real if scan_file(p)]
    chk(not bad, "仓库当前 %d 个启动脚本全部合格%s"
        % (len(real), ("" if not bad else "（不合格：%s）" % ", ".join(bad))))

    # 清理（沙箱里删文件可能被 safe-delete 接管，删不掉也不算失败）
    for fn in os.listdir(tmp):
        try:
            os.remove(os.path.join(tmp, fn))
        except OSError:
            pass
    try:
        os.rmdir(tmp)
    except OSError:
        pass

    log("")
    log("bat_lint 自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    # --check 与不带参数等价；带上就明确表示"这是检查"
    return check()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
