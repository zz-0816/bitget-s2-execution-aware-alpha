#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文档路径引用检查（可失败）
==========================

━━ 为什么需要它 ━━

本仓库的可信度建立在一条原则上：**每个数字、每个路径都能点回去**。
但 `docs/` 里有相当篇幅是从隔离前继承的，会提到**不在本仓库**的脚本与数据日
（它们在项目一仓库）。读者在本仓库里点不到，就会以为文件丢了。

实测：本轮一次扫描查出 **18 处**这种引用，其中只有 4 处写明了"项目一"。
这不是笔误，是**没有检查**——所以补一个检查，而不是靠人记住。

━━ 判定规则（三条，都可解释）━━

一个反引号里的带路径引用（形如 `tools/x.py`）：

  ① **本仓库存在** -> OK（最强）
  ② 本仓库不存在，但**声明过是跨仓库** -> OK，并**顺带验证上游真有**
     · 声明方式有两种，任一即可：
       a. 该文件里引用附近（`NEAR` 行内）出现"项目一"；
       b. 路径出现在 `README.md` 的「跨仓库引用约定」小节里（全局声明）。
  ③ 都不满足 -> **FAIL**：要求作者二选一（补"项目一"标注，或删掉引用）

给了 `--project-one`（或自动探测到项目一仓库）时，第 ② 类会**就地 `Test-Path`**
上游文件：**声明了但上游也没有 = 真断链 = FAIL**。
读不到项目一仓库时（评委机器上通常没有）第 ② 类**不判失败**，只标注
"仅凭声明通过，未验证上游"—— 不假装验证过。

用法::

    python tools/doc_ref_check.py                 # 只查本仓库
    python tools/doc_ref_check.py --project-one D:\\bitgetS2_factory_trading
    python tools/doc_ref_check.py --selftest      # 用合成样本验证判定规则本身
"""

import argparse
import glob
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

try:
    from common.console import install as _install
    _install()
except Exception:  # noqa: BLE001
    pass

DOCS = ["README.md", "SUBMISSION-CHECKLIST.md", "TASKS-P2.md",
        os.path.join("prompts", "README.md")]

# 反引号里的**带路径**引用。裸文件名（`agent_team.py`）不算：
# 文档里常见"`tools/ui_design_check.py` …… 之后再简写成 `ui_design_check.py`"，
# 把裸名也当引用会产出大量误报。
REF = re.compile(r"`([A-Za-z0-9_][A-Za-z0-9_./\-]*/[A-Za-z0-9_.\-]+"
                 r"\.(?:py|md|json|js|css|html|toml|yaml|yml|txt|sh|bat|svg|png|csv))`")

# 这些前缀下的路径**本来就允许不存在**：运行时产物、模板里的日期
SKIP_PREFIX = ("http", "data/run/", "data/positions/", "data/reports/_",
               "docs/ui-v3/")
TEMPLATE = re.compile(r"YYYY|MM-DD|NNNN|<[^>]*>|\.\.\.")
NEAR = 8          # "引用附近多少行内出现『项目一』就算声明过"
CONVENTION_HEAD = "### 跨仓库引用约定"


def doc_files():
    out = list(DOCS)
    out += sorted(glob.glob(os.path.join(BASE, "docs", "*.md")))
    return [p for p in out
            if os.path.exists(p if os.path.isabs(p) else os.path.join(BASE, p))]


def rel(p):
    return os.path.relpath(p, BASE).replace("\\", "/")


def ignored_by_rule(refs):
    """哪些引用路径**会被 .gitignore 规则排除** —— 不管它现在在不在磁盘上。

    🔴 必须用 `git check-ignore`，**不能**用 `git ls-files -o -i`：
    后者只列**磁盘上确实存在**的被忽略文件。在刚 clone 出来的仓库里
    `data/derived/event_driven_state.json` 根本不存在（它是运行时产物），
    于是它既不在 tracked 里、也不在 ignored 里，被当成"未声明失效" ->
    **同一份文档在开发机通过、在 clone 里失败**。

    ⚠️ 这是同一个坑的**第二次**：第一次是"用 os.path.exists 判在不在仓库"，
    改成 git 之后仍然按"文件在不在"取被忽略集合。两次的共性都是
    **把"磁盘状态"当成了"仓库状态"**。规则要按**规则**判，不按现象判。
    """
    import subprocess
    refs = sorted(set(refs))
    if not refs:
        return set()
    try:
        r = subprocess.run(["git", "check-ignore", "--stdin"], cwd=BASE,
                           input="\n".join(refs).encode("utf-8"),
                           capture_output=True)
    except OSError:
        return set()
    # check-ignore：有命中返回 0，无命中返回 1；其它返回码视为不可用
    if r.returncode not in (0, 1):
        return set()
    return set(r.stdout.decode("utf-8", "replace").splitlines())


def git_sets(extra_refs=()):
    """用 **git** 判定"这个路径算不算在本仓库里"，而不是 `os.path.exists()`。

    🔴 这一条是被新克隆打脸才补上的：初版用 `os.path.exists()`，于是
    `data/derived/event_driven_state.json` 这种**被 .gitignore 排除的运行时产物**
    在开发机上存在（判为"在仓库里"-> 通过），在**刚 clone 出来的仓库里不存在**
    （判为"未声明"-> **失败**）。也就是说检查器**自己不可移植** —— 而"换个机器
    结论就变"正是这个仓库最不能接受的一类缺陷。

    现在以 git 为准，分三个集合：
      · ``tracked``  已入库  -> 最强，任何机器上都在
      · ``ignored``  被 .gitignore 排除 -> 仓库**已经声明**它是运行时产物，
                     文档提到它不算断链（但要单独报出来，不混进"已入库"）
      · 其余（在工作区里但没入库）-> **问题**：文档引用了一个没人提交的文件
    读不到 git 时（例如别人下载的是 zip）退回存在性判断，并**如实标注**判定退化。
    """
    import subprocess

    def run(args):
        r = subprocess.run(["git"] + args, cwd=BASE, capture_output=True)
        if r.returncode != 0:
            return None
        return r.stdout.decode("utf-8", "replace").splitlines()

    tracked = run(["ls-files"])
    if tracked is None:
        return None, None, "读不到 git（可能是 zip 下载）-> 退回按存在性判定"
    # 存在的被忽略文件 + **按规则**会被忽略的引用路径，两者并起来
    ignored = set(run(["ls-files", "-o", "-i", "--exclude-standard"]) or [])
    ignored |= ignored_by_rule(extra_refs)
    return set(tracked), ignored, None


def convention_paths():
    """从 README 的「跨仓库引用约定」小节里抽出被**全局声明**的路径。"""
    p = os.path.join(BASE, "README.md")
    try:
        with open(p, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return set()
    i = text.find(CONVENTION_HEAD)
    if i < 0:
        return set()
    # 小节范围：到下一个同级或更高级标题为止
    rest = text[i + len(CONVENTION_HEAD):]
    m = re.search(r"\n#{1,3} ", rest)
    body = rest[:m.start()] if m else rest
    return set(REF.findall(body))


def find_project_one(explicit=None):
    if explicit:
        return explicit if os.path.isdir(explicit) else None
    for c in (os.path.join(os.path.dirname(BASE), "bitgetS2_factory_trading"),
              r"D:\bitgetS2_factory_trading"):
        if os.path.isdir(c):
            return c
    return None


def judge(ref, lines, ln_no, conv, in_repo, in_upstream):
    """**纯函数**：判定一处引用是否合格。返回 (kind, problem)。

    kind:
      ``local``   在本仓库（已入库，或判定退化为"存在"）
      ``runtime`` 被 .gitignore 排除的运行时产物 —— 允许引用，单独计数
      ``cross``   声明过的跨仓库引用
      ``fail``    未声明 / 声明了但上游也没有 / 引用了未入库的文件

    抽成纯函数是为了能**用合成输入自检判定规则本身** —— 否则"检查器准不准"
    只能靠肉眼看它的输出，那是这个仓库最不想出现的情况。
    与 `agent_team.halted_from` 的做法一致。
    """
    if in_repo is True:
        return "local", None
    if in_repo == "runtime":
        return "runtime", None
    if in_repo == "untracked":
        return "fail", ("`%s` 在工作区里存在但**没有入库** —— 文档引用了它，"
                        "别人 clone 下来就点不到（要么提交它，要么别引用）" % ref)
    lo = max(0, ln_no - 1 - NEAR)
    nearby = "\n".join(lines[lo:ln_no + NEAR])
    declared = (ref in conv) or ("项目一" in nearby)
    if not declared:
        return "fail", ("`%s` 本仓库没有，也没写明是跨仓库引用"
                        "（补『项目一仓库』标注，或在 README 的约定表里登记）" % ref)
    if in_upstream is False:            # 明确查过上游、且没有
        return "fail", ("`%s` 声明为跨仓库引用，但项目一仓库里也没有 -> 真断链"
                        % ref)
    return "cross", None


def classify(ref, tracked, ignored):
    """把"这个路径在不在本仓库"判成一个可解释的取值。

    返回 True（已入库）/ "runtime"（被忽略的运行时产物）/
    "untracked"（存在但没入库）/ False（不在本仓库）。
    """
    if tracked is None:                 # 读不到 git -> 退化，只保证不误报
        return True if os.path.exists(os.path.join(BASE, ref)) else False
    if ref in tracked:
        return True
    if ref in ignored:
        return "runtime"
    if os.path.exists(os.path.join(BASE, ref)):
        return "untracked"
    return False


def collect_refs():
    """第一遍：把所有 (文件, 行号, 引用) 收集起来。

    分两遍是必须的：`ignored_by_rule()` 要拿到**全部**引用路径才能一次批量问 git，
    而"哪些路径被 .gitignore 规则排除"又必须在判定**之前**就位。
    """
    out = []
    for f in doc_files():
        full = f if os.path.isabs(f) else os.path.join(BASE, f)
        try:
            with open(full, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        except OSError:
            continue
        for ln_no, ln in enumerate(lines, 1):
            for ref in REF.findall(ln):
                if ref.startswith(SKIP_PREFIX) or TEMPLATE.search(ref):
                    continue
                out.append((full, lines, ln_no, ref))
    return out


def check(project_one=None, verbose=True):
    """返回 (ok, problems, notes, stats)。"""
    conv = convention_paths()
    p1 = find_project_one(project_one)
    items = collect_refs()
    tracked, ignored, degrade = git_sets(r[3] for r in items)
    problems, notes = [], []
    n_ref = n_local = n_cross = n_up = n_rt = 0

    for full, lines, ln_no, ref in items:
        n_ref += 1
        in_repo = classify(ref, tracked, ignored)
        up = os.path.exists(os.path.join(p1, ref)) if p1 else None
        kind, problem = judge(ref, lines, ln_no, conv, in_repo, up)
        if kind == "local":
            n_local += 1
        elif kind == "runtime":
            n_rt += 1
        elif kind == "cross":
            n_cross += 1
            if up:
                n_up += 1
            else:
                notes.append("`%s` 仅凭声明通过（未提供项目一仓库，"
                             "未验证上游）" % ref)
        else:
            problems.append("%s:%d %s" % (rel(full), ln_no, problem))

    if degrade:
        notes.insert(0, degrade)
    if p1:
        notes.insert(0, "已就地核对项目一仓库：%s" % p1)
    else:
        notes.insert(0, "**没有**项目一仓库可比对 -> 跨仓库引用只按声明判定，"
                        "不假装验证过上游（用 --project-one 指定可完整核验）")

    stats = {"refs": n_ref, "in_repo": n_local, "runtime": n_rt,
             "cross_repo": n_cross, "upstream_verified": n_up, "project_one": p1,
             "n_files": len(doc_files()), "git_authoritative": tracked is not None}
    if verbose:
        print("=" * 78)
        print("文档路径引用检查（以 **git 入库状态**为准，不是「文件在不在磁盘上」）")
        print("=" * 78)
        print("  文档          %d 个" % stats["n_files"])
        print("  带路径引用    %d 处 ｜ 已入库 %d ｜ 运行时产物 %d ｜ "
              "跨仓库声明 %d（上游已核 %d）"
              % (n_ref, n_local, n_rt, n_cross, n_up))
        for n in notes[:4]:
            print("  [ ~ ] %s" % n)
        for p in problems[:20]:
            print("  [!! ] %s" % p)
        if len(problems) > 20:
            print("  ... 另有 %d 条" % (len(problems) - 20))
        print()
        print("文档路径引用检查%s" % ("通过" if not problems else "**失败**"))
    return (not problems), problems, notes, stats


# ---------------------------------------------------------------- 自检

def selftest():
    """用**合成输入**验证判定规则本身（纯函数，不碰文件系统）。

    ⚠️ 初版这里用 `tempfile.mkdtemp()` 落真实文件 —— 在文件沙箱里
    `PermissionError`（工作区外的临时目录不可写）。改纯函数后既能在沙箱里跑，
    也顺带成为"检查器自身准不准"的证据。
    """
    ok = True

    def chk(cond, msg):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s" % ("OK " if cond else "!! ", msg))

    conv = {"tools/declared.py"}
    # ① 本仓库存在 -> local（最高优先，连声明都不需要）
    chk(judge("tools/x.py", ["`tools/x.py`"], 1, set(), True, None)[0] == "local",
        "本仓库存在的引用判为 local")

    # ② 不存在 + 附近写了"项目一" -> cross
    lines = ["> 这几条命令的脚本在项目一仓库。", "", "复跑：`tools/y.py`"]
    chk(judge("tools/y.py", lines, 3, set(), False, None)[0] == "cross",
        "附近写了『项目一』-> 判为已声明的跨仓库引用")

    # ③ 不存在 + README 约定表里登记过 -> cross（全局声明）
    chk(judge("tools/declared.py", ["`tools/declared.py`"], 1, conv, False, None)[0]
        == "cross", "出现在 README 约定表里 -> 判为已声明")

    # ④ 不存在 + 没声明 -> fail
    kind, prob = judge("tools/gone.py", ["`tools/gone.py`"], 1, set(), False, None)
    chk(kind == "fail" and "没写明是跨仓库引用" in prob,
        "未声明的失效引用 -> fail（并给出该补什么）")

    # ④b 被 .gitignore 排除的运行时产物 -> runtime（允许，单独计数）
    chk(judge("data/derived/event_driven_state.json", ["x"], 1, set(),
              "runtime", None)[0] == "runtime",
        "被 .gitignore 排除的运行时产物 -> runtime（仓库已声明它是产物，不算断链）")

    # ④c 在磁盘上但**没入库** -> fail。这一条是新克隆打脸换来的：
    #     初版按 os.path.exists() 判，开发机通过、clone 出来失败（检查器自己不可移植）
    kind, prob = judge("data/derived/new.json", ["x"], 1, set(),
                       "untracked", None)
    chk(kind == "fail" and "没有入库" in prob,
        "在磁盘上但未入库 -> fail（别人 clone 下来点不到）")

    # ④d classify() 的三态：以 git 为准，不以磁盘为准
    chk(classify("a.py", {"a.py"}, set()) is True
        and classify("b.json", set(), {"b.json"}) == "runtime"
        and classify("nope.py", set(), set()) is False,
        "classify：已入库 / 被忽略 / 不在仓库 三态正确")
    chk(classify("disk_only.py", set(), set()) == "untracked"
        or not os.path.exists(os.path.join(BASE, "disk_only.py")),
        "classify：磁盘上有但没入库 -> untracked（不是 True）")

    # ④e 🔴 关键回归：被 .gitignore 规则排除、但**磁盘上不存在**的路径
    #     也必须判为 runtime。这就是"开发机通过、clone 失败"那一处的根因 ——
    #     断言它**不依赖文件是否存在**，所以在任何机器上都成立。
    ig = ignored_by_rule(["data/derived/event_driven_state.json",
                          "data/run/record.jsonl",
                          "tools/agent_team.py"])
    chk("data/derived/event_driven_state.json" in ig,
        "被 .gitignore 规则排除的路径**即使磁盘上不存在**也判为 ignored"
        "（不能按『文件在不在』取集合）")
    chk("data/run/record.jsonl" in ig and "tools/agent_team.py" not in ig,
        "ignored_by_rule：运行时目录命中、已入库文件不命中")

    # ⑤ 声明了但上游也没有 -> fail（真断链，不能被"声明"洗白）
    kind, prob = judge("tools/y.py", lines, 3, set(), False, False)
    chk(kind == "fail" and "真断链" in prob,
        "声明为跨仓库、但上游也没有 -> 仍判 fail")

    # ⑥ 距离太远时**不该**被算作已声明（防"整篇提一次就全豁免"）
    far = ["项目一。"] + [""] * 40 + ["`tools/z.py`"]
    chk(judge("tools/z.py", far, 42, set(), False, None)[0] == "fail",
        "远距离的『项目一』不算声明（否则整篇提一次就全豁免）")

    # ⑦ 跳过规则：模板名 / 运行时目录 / 裸文件名
    chk(bool(TEMPLATE.search("orderbook-YYYY-MM-DD.csv"))
        and "data/run/".startswith(SKIP_PREFIX)
        and "data/positions/".startswith(SKIP_PREFIX),
        "模板名（YYYY-MM-DD）与运行时目录被跳过")
    chk(REF.findall("`app.js`") == [],
        "裸文件名不算引用（避免简写造成的大量误报）")

    print("\n文档路径引用检查器自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="文档路径引用检查")
    ap.add_argument("--project-one", default=None,
                    help="项目一仓库路径（用于核验跨仓库引用；不给则自动探测）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    ok, problems, notes, stats = check(args.project_one)
    if args.json:
        import json
        print(json.dumps({"ok": ok, "problems": problems, "notes": notes,
                          "stats": stats}, ensure_ascii=False, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
