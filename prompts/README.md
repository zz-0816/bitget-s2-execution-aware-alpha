# `prompts/` — LLM prompt 的版本化存放

这个目录放**运行期真正发给 LLM 的 prompt**。在此之前，prompt 是
`project2/event_gate.py` 里的一个三引号常量（`LLM_PROMPT`）—— 于是"改 prompt"
和"改代码"是同一件事，评审时说不清"这一版 prompt 到底改了什么"，
也无法回答"某次判断用的是哪一版"。

## 命名与选取规则

| 规则 | 说明 |
|---|---|
| 文件名 | `event_gate.v<主版本>.md`（如 `event_gate.v2.md`；允许 `v2.1`） |
| 选取 | 启动时从本目录取**版本号最大**的一个（按**数字**比：`v2 < v3 < v10`，不是字符串序） |
| 加载不到 | 退回 `project2/event_gate.py` 里的内嵌兜底 `EMBEDDED_PROMPT`，并在结果里标注 `prompt_source="embedded"` |
| 解析失败 | 同样退回内嵌兜底，**并把失败原因写进 meta**（不静默） |

## 文件结构（这是契约）

```
<元信息：key: value，缩进行是同一条的续行>

---

<prompt 正文：一字不改地作为 system prompt 发送>
```

🔴 **`---` 之前的内容一个字节都不会进 prompt**。元信息混进正文 = 偷偷改了语义。

必填元信息：

| 字段 | 含义 |
|---|---|
| `prompt_id` | prompt 的逻辑名字（这里是 `event_gate`） |
| `version` | 版本号，必须与文件名里的 `v<N>` 对得上（`v2-2026-09-18`） |
| `updated` | 最后改动日期 `YYYY-MM-DD` |
| `owner` | 谁负责维护 |
| `purpose` | 这个 prompt 要回答的**唯一**问题 |
| `changelog` | **相对上一版改了什么**（不写 = 别人无法判断改动的影响面） |

## 改 prompt 的流程（三条硬规则）

1. **新建版本文件**：`event_gate.v3.md`，**不要**原地改 `v2`。
2. **升 `version`**（并与文件名一致），在 `changelog` 里写明改了什么、为什么。
3. **同步内嵌兜底**：把新正文逐字复制进 `project2/event_gate.py::EMBEDDED_PROMPT`，
   并把 `EMBEDDED_PROMPT_VERSION` 改成新版本号。

   ```powershell
   python project2\event_gate.py --selftest
   ```

   自检里有一条"外部 prompt 正文与内嵌兜底**逐字一致**"—— 忘了同步会**直接失败**。
   这条不是形式主义：`prompts/` 缺失时（例如别人只拷了 `.py` 文件）跑的就是兜底，
   两边不一致意味着"同一份日志在不同机器上可能对应两个不同的 prompt"。

4. **旧版本文件保留不删** —— 历史判断要能回溯到它当时用的那一版。

## 某次判断如何回溯到具体 prompt 版本

`event_gate.llm_gate()` 每次调用都会把**实际发出去的那份 prompt** 的
SHA256 记进返回值，一路带到：

* `assess()["event"]["prompt_version" / "prompt_sha256" / "prompt_source"]`
* `assess()["llm"]["prompt_*"]`
* 决策日志里 `analysts[].notes` 的 `prompt v2-2026-09-18（source=…, sha256=…）`
* 事件驱动缓存 `data/derived/event_driven_state.json` 的 `prompt_sha256`

回溯步骤：

```powershell
# ① 从日志里取 sha256（前 16 位即可）
# ② 逐个算 prompts/*.md **正文**（'---' 之后）的 sha256，找对上号的那一版
python -c "import hashlib,glob;[print(hashlib.sha256(open(p,encoding='utf-8').read().split(chr(10)+'---'+chr(10),1)[1].strip().encode()).hexdigest()[:16], p) for p in sorted(glob.glob('prompts/event_gate.v*.md'))]"
```

指纹只覆盖**正文**，所以补 changelog、改 `owner` 这类元信息改动**不会**改变指纹 ——
"判断用的 prompt 内容有没有变"这个问题因此是可判定的。

> ⚠️ 当前版本：`event_gate.v4.md`（`v4-2026-09-20`，正文 SHA256 前 16 位
> `f8ee78a9d652bba9`）。
>
> v4 相对 v3 的改动（**依据是留出校准集上的实测错误**，不是感觉）：
> ① 判据改成**两步、顺序不能颠倒**：先看"这条消息与我方标的相关吗"，再看类别；
>    v3 的判据写着"不看它对某个标的相不相关"，于是在 `peer-amd-rumor` 上把
>    **期望 `caution` 判成了 `none`** —— 这是**危险方向**错误（该停手时说"没事"），
>    比"过保守"严重得多；
> ② 明确"同业竞争格局 / 产业链传导" -> `caution`；
> ③ 同时明确"**别家公司自己的公司行为**（如别家发布财报）不等于我方事件"，
>    防止修过头变成"什么都是 caution"；
> ④ few-shot 全部用**与校准集不同的合成例子** —— 校准集是**留出集**，
>    把它的用例写进 prompt 等于拿测试集训练。
>
> 结果：留出集 n=10 上 v3 = 80% / 危险方向 1 条，v4 = **100% / 危险方向 0 条**，
> 并已接成 `python run_p2.py --selftest --net` 第 ⑱ 步的**回归门槛**
> （`data/calibration/baseline.json`，`max_dangerous_errors=0`）—— 改回去就红。
> ⚠️ n=10 只够做回归，**不能当准确率结论**。
>
> v3 相对 v2 的改动（用户要求：**稳定不出错、不产生幻觉、严格遵守 prompt**）：
> ① 「只依据标题」升级为第一条独立铁律，并写死「标题没写 = 不知道」；
> ② 输出从示例改成**字段契约**（不得增删字段），与代码侧
>    `_validate_llm_output` 的强校验**一一对应** —— prompt 与校验不再是两套说法；
> ③ 判据补齐两类实测漏判：指数成分/权重调整、分析师评级或目标价变动；
> ④ 新增要求 reason **必须能被核对**（代码侧检查能否回溯到标题，
>    找不到即判不合格并**带错误信息重试**）；
> ⑤ 新增 4 条 few-shot 示例（只示范格式与类别，不引入真实标的的额外事实）。
>
> v1 只存在于代码历史里，**没有**对应文件 —— 这是已知缺口：
> v1 时期的日志无法用本目录回溯（只能回查当时的 `git` 版本）。
> v2、v3 文件**保留不删**（历史判断要能回溯到它当时用的那一版）。
