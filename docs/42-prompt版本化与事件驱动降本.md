# 42 · prompt 版本化与事件驱动降本（T4-A / T4-B）

> 生成 **2026-09-19** ｜ 对应改动：`prompts/`（新增）、`project2/event_gate.py`、
> `project2/agent_team.py`、`common/config.py`、`.env.example`、`.gitignore`
> 自检：`python project2\event_gate.py --selftest`（含新增的 30 条 T4-A/T4-B 断言）
> 实测：`python tools\news_sources.py --probe` → `python tools\news_sources.py --base NVDA --event-driven --save`
> 相关：`docs/33` §①-c（`analyst_news` 的位置）、`docs/35`（真 LLM 实调记录）

---

## 1. T4-A：prompt 外置为 `prompts/*.md`（版本化）

### 1.1 改之前是什么样（问题在哪）

prompt 是 `project2/event_gate.py` 里的一个常量：

```python
PROMPT_VERSION = "v2-2026-09-18"    # 改动必须升版本号
LLM_PROMPT = """你是交易系统的事件风险过滤器。..."""
```

两个后果：

1. **"改 prompt" = "改代码"**：评审时看到的是 diff 里的一段三引号文本，
   说不清"这一版相对上一版改了什么、为什么改"。
2. **"某次判断用了哪一版"不可核验**：版本号是**硬编码的字符串**，
   它只说明"代码里写的是 v2"，不说明**发出去的那份文本**是什么。
   文本被改过而忘记升版本号，日志照样写 `v2` —— 判断无法回溯。

### 1.2 现在怎么做

```
prompts/
├── README.md            # 版本化规则（本节的简版）
└── event_gate.v2.md     # 元信息 + '---' + 正文（正文与旧常量逐字一致）
```

* 启动时 `event_gate.load_prompt()` 从 `prompts/` 取 **版本号最大** 的
  `event_gate.v*.md`（按数字比：`v2 < v3 < v10`）。
* 解析出**元信息**（`prompt_id` / `version` / `updated` / `owner` / `purpose` /
  `changelog`）与**正文**；`PROMPT_VERSION` 由加载结果推导，**不再硬编码**。
* `prompts/` 缺失、没有版本文件、或解析失败 → 退回 `event_gate.py` 里的
  内嵌兜底 `EMBEDDED_PROMPT`，并在结果里如实标注 `prompt_source="embedded"`。
  **绝不静默**：解析失败的原因会写进 `Prompt.meta["parse_error"]`，一路带到 CLI 输出。
* 🔴 **元信息一个字节都不进 prompt 正文** —— 否则等于偷偷改了语义。
  自检里有一条专门盯这个：外部文件正文与内嵌兜底的 **SHA256 必须相等**。

### 1.3 某次判断如何回溯到具体 prompt 版本

每次真调 LLM 时，`llm_gate()` 把**实际发出去的那份 prompt 正文**的 SHA256
定格下来，并一路带出去：

| 位置 | 字段 |
|---|---|
| 闸门返回值 | `assess()["event"]["prompt_version"]` / `["prompt_sha256"]` / `["prompt_source"]` |
| LLM 留痕 | `assess()["llm"]["prompt_*"]` |
| 决策日志 | `analysts[].notes` 里的 `prompt v2-2026-09-18（source=…, sha256=51ca884e9247cdc4…）` |
| 事件驱动缓存 | `data/derived/event_driven_state.json` → `bases.<标的>.prompt_sha256` |

回溯步骤（一条命令就能对上号）：

```powershell
python -c "import hashlib,glob;[print(hashlib.sha256(open(p,encoding='utf-8').read().split(chr(10)+'---'+chr(10),1)[1].strip().encode()).hexdigest()[:16], p) for p in sorted(glob.glob('prompts/event_gate.v*.md'))]"
# 51ca884e9247cdc4 prompts/event_gate.v2.md
```

指纹**只覆盖正文**，所以补 changelog / 改 owner 不会改变指纹 ——
"判断用的 prompt 内容有没有变"因此是个可判定的问题。

### 1.4 当前版本的 changelog（v2 相对 v1）

v1（2026-09-17）只有 JSON 形状 + `block/caution/none` 映射 + 一条"只依据给定标题"。
**v2 增加了 RAG 注入的三条硬要求**：

1. 只看给定标题里的事实，标题不足时给 `caution` 并说明"信息不足"；
2. 注入的「我方策略口径与历史案例」只用于理解我方在做什么，**不得据此编造事件**；
3. 保守原则：不确定时给 `caution`，不给 `none`。

> ⚠️ **已知缺口**：v1 **没有**对应文件（它只存在于代码历史里）。
> 所以 v1 时期的日志无法用本目录回溯，只能回查当时的 git 版本。
> 从现在起每一版都落文件。

### 1.5 与复跑日志契约的关系（**重点，别搞坏**）

`agent_team.py` 的 `--replay` 有一套契约字段校验（`REPRO_MUST_MATCH` /
`repro_projection`）。新增的 prompt 指纹**没有**进契约字段，原因是：

* `repro_projection()` 的 `must` 只取 `PARAM_FIELDS` = `(base, qty_usd, miss_bp,
  urgent, slice_usd, depth_take_ratio, now_ms, log_format, synthetic, scenario)`
  —— `parameters` 里的 `gate` / `thresholds` / `data_used` 本来就不参与比对；
* prompt 指纹只出现在 `analysts[].notes` 与 `llm` 留痕里，两者都**不在**投影里；
* 未新增任何 `evidence` 条目、未改 `analyst_news` 的 verdict/confidence 逻辑。

**实测**（真生成 + 真复跑，非自检里的合成路径）：

```
python project2\agent_team.py --base NVDA --log --log-outdir _tmp_t4_replay
python project2\agent_team.py --replay _tmp_t4_replay\debate-NVDA-*.json
  ==> 复跑**通过**：决策路径可复现     （① 契约字段全部一致 ｜ ② 漂移 0 个）
```

⚠️ 反过来说：**改了 prompt 正文，历史日志的复跑仍会"通过"** ——
因为 prompt 指纹不在契约字段里。这是**有意的保守选择**（不动既有契约、
不降低自检严格度），代价是"prompt 变更"这件事目前只能靠
`notes` 里的 sha256 人工比对发现，不能靠 `--replay` 自动报警。
见 §4「没能做到/有疑虑」。

---

## 2. T4-B：事件驱动真正接上（并修一个"说了没做"）

### 2.1 修的是什么

`common/config.py` 与 `.env.example` 早就声明：

```ini
NEWS_EVENT_DRIVEN=on    # on=只在出现新条目时才调 LLM（省成本的主要手段）
```

`tools/news_sources.py` 也实现了 `event_driven_check()` 与 `--event-driven`。
**但决策链里没有任何代码读这个配置** —— 每跑一次决策都会调一次 LLM。
"省成本的主要手段"当时只作用于 CLI 的提示文本，**没有作用于决策链**。

修法：在 `project2/event_gate.py` 里新增 `gate_decision()`，由
`assess(mode="llm")` 在调用 LLM **之前**问它一句"这一轮该不该调"。

### 2.2 完整规则表

输入来源（**复用 `tools/news_sources.py` 的判据，不另起一套**）：

* `data/derived/news_state.json` → `seen`（已经见过的条目 key）
* `data/derived/news_latest.json` → `items` / `headlines_for_gate`
* 条目 key = `news_sources._item_key()` = `url or "source|title"`
* form 权重 = `news_sources.FORM_WEIGHT`，**重大表种 = 权重 ≥ 3**

| # | 情形 | 动作 | `reason` | 说明 |
|---|---|---|---|---|
| 1 | `NEWS_EVENT_DRIVEN=off` | **每轮都调** | `event_driven_off` | 与引入事件驱动之前**完全一致** |
| 2 | 该标的出现**新的重大 EDGAR 申报**（权重 ≥ 3：8-K / 10-Q / 10-K / S-1 / SC 13D / SC 13G） | **跳过缓存，立即调** | `edgar_new_material_filing` | 一级信号优先于缓存；Form 4（权重 1）**不**触发 |
| 3 | 候选标题**全是已见过的** + 缓存**未过 TTL** | **不调**，复用上一次 LLM 判定 | `no_new_items_cache_hit` | 复用的是**原值**，只加"判定龄"标注 |
| 4 | 无新条目但**缓存不存在** | 调 | `no_new_items_but_cache_missing` | 宁可多花一次，也不拿"没缓存"当"没风险" |
| 5 | 无新条目但**缓存已过 TTL** | 调 | `cache_expired` | 见 §3 的代价 |
| 6 | 无新条目但**候选集合与缓存那次不同** | 调 | `cand_set_changed` | 防止"换了输入却复用旧判定" |
| 7 | 无新条目但缓存判定来自**另一版 prompt** | 调 | `prompt_changed` | 缓存里记了 `prompt_sha256`，与当前版本不一致就作废（判断口径变了） |
| 8 | 本轮**没有候选标题** | 调 | `no_candidates` | ⚠️ **不许**把"看不见"当"没风险" |
| 9 | `news_state.json` **不可读** | 调 | `no_state` | 无法确证"无新条目" |
| 10 | 消息面状态**未 bootstrap** | 调 | `first_run` | 首轮不调会漏掉全部现存事件 |

**两条红线（代码里有断言盯着）**：

1. **绝不允许**因为"没有新条目"就把 `severity` 降级成 `none`。
   规则 3 复用缓存时复用的是 `{in_window, severity, reason, confidence}` 的**原值**；
   若原值是 `none`，还会在 reason 里补一句"这是 N 分钟前 LLM 判的 none，非本轮重新判断"。
2. **拿不到候选就不许复用缓存**（规则 8/9/10）。复用只在
   "**能确证**这一轮没有新信息"时才发生；确证不了就照常调。

### 2.3 缓存落在哪、长什么样

**新文件** `data/derived/event_driven_state.json`（已加进 `.gitignore`）：

```json
{ "bases": { "NVDA": {
      "ts_ms": 1800000000000,
      "updated_utc": "2027-01-15T08:00:00+00:00",
      "verdict": {"in_window": true, "severity": "block",
                  "reason": "...", "confidence": 0.9},
      "n_headlines": 2,
      "cand_keys": ["https://x/8k", "https://x/news1"],
      "prompt_version": "v2-2026-09-18",
      "prompt_sha256": "51ca884e...",
      "ttl_min": 30 } },
  "updated_ms": 1800000000000, "ttl_min": 30 }
```

🔴 **绝不写 `data/derived/news_state.json`**：那个文件由 `news_sources.py` 管理，
并在数据快照里带 SHA256 —— 改它等于改快照。我们只读它。

### 2.4 TTL 怎么定

| 配置 | 默认 | 含义 |
|---|---|---|
| `NEWS_EVENT_DRIVEN` | `on` | `on` / `off`（`1/true/yes` 也认） |
| `EVENT_CACHE_TTL_MIN` | `30` | 无新条目时，上一次 LLM 判定可复用的最长时间（分钟） |

定 30 分钟的依据（**是取舍，不是最优解**）：

* 我们的轮询节奏是 60 秒（`NEWS_POLL_SECONDS`），采样器 30~60 秒 ——
  判定龄 30 分钟 ≈ 30 个轮询周期；
* 期内**真正致命的那一类事件走的是规则 2**（EDGAR 一手申报，分钟级），
  不靠缓存兜 —— 所以 TTL 覆盖的主要是"聚合新闻/RSS 有没有新说法"这一层；
* 代价见 §3。**要更保守就把 TTL 调小**（调到 0 = 每轮都调，等于关掉降本）；
  要更省钱就调大，但最坏判定龄随之线性变大。

### 2.5 如实标注（三处都要能看出"这次到底调没调"）

| 情形 | `event.source` 长什么样 |
|---|---|
| 真调了 LLM | `llm` |
| 复用缓存 | `llm(cache: 无新条目, 判定龄 12min｜TTL 30min)` |
| LLM 失败 / 无 key | `static(LLM 失败 3 次: …)` / `static(LLM 未执行: 未配置 LLM_API_KEY…)` |

另外 `assess()` 顶层新增 `event_driven`，含 `should_call_llm` / `reason` /
`age_min` / `enabled` / `edgar_triggers` / `state_saved`；
`analyst_news` 的 `notes` 会跟着写清"✅ 本次由 LLM 判" /
"♻️ 本次**复用上一次 LLM 判定**（判定龄 N 分钟）" / "⚠️ 本次未使用 LLM"。
CLI `python project2\event_gate.py --base NVDA --assess` 也会印出来。

---

## 3. 诚实的边界（**这一段比上面所有功能都重要**）

1. **缓存复用 = 放弃"每轮都重新判断"**。最坏情况下，一次事件判断的
   **判定龄 = 不大于 TTL**（默认 30 分钟）。
   也就是说：**聚合新闻层里 30 分钟内新出现的小道消息，我们可能复用 30 分钟前的判断。**
   这不是 bug，是省成本的**直接代价**，写在这里以免被人当成"实时"。

2. **兜住的是什么、兜不住的是什么**：
   * 兜得住：**法定披露**（8-K/10-Q/10-K/S-1/SC 13D…）—— 规则 2 立即重判，
     与 TTL **无关**。这是"最致命的那一类"。
   * 兜不住：社交媒体传闻、盘中快讯、以及**权重 < 3 的表单**（Form 4 内部人交易）。
     这些要等 TTL 到期才会重新判断。

3. **拿不到候选时我们不会假装看不见**（规则 7/8/9）——
   但反过来说：**候选标题本身的质量上限 = `news_sources.py` 的抓取质量**。
   `news_latest.json` 过期（默认 > 30 分钟）时 `_headlines_from_news()` 会返回
   `None`，此时 `assess` 收到的是**空标题**，事件驱动会判 `no_candidates`
   并照常调 LLM。**副作用**：`news_latest.json` 不新鲜时，**降本不会生效**
   （每轮都调）。要让降本真正生效，需要有人持续跑
   `python tools\news_sources.py --base <标的> --event-driven --save`（默认 60 秒一轮）。

4. **缓存里存的是 LLM 的判断，不是事实**。LLM 抖动、端点换模型、
   prompt 升版本，都会让"上一次判定"的代表性下降。
   缓存里记了 `prompt_version` / `prompt_sha256`；若当前 prompt 的正文
   与缓存那次不一致，规则 7 会**作废缓存并重判**（`prompt_changed`）。
   剩下没能覆盖的是**换模型**（`llm_usage.model` 没进缓存比对）——
   同一版 prompt 换个模型，缓存仍会被复用。

5. **无 key 时行为完全不变**：仍然退化为确定性日历，并明确标注"本次未使用 LLM"，
   且按 `FAIL_CLOSED_ON_LLM_ERROR` 暂停挂单。事件驱动**不参与**这条路径。

6. **省了多少没有实测数字**。我们只证明了"全旧条目时不调 LLM"（见 §5 证据），
   **没有**接入真实计费端点统计 token 花销下降了多少 ——
   省下的次数取决于"每小时有多少新条目"，而这个数在盘中是**变的**。

---

## 4. 没能做到 / 有疑虑

| # | 事项 | 现状 |
|---|---|---|
| 1 | **换模型**不会作废事件缓存 | 缓存比对了 `cand_keys` 与 `prompt_sha256`，但**没比对 `llm_usage.model`**。同一版 prompt 下从 `deepseek-flash` 换到 `deepseek-v4-pro`，TTL 内的旧判定仍会被复用 |
| 2 | prompt 指纹**不在** `REPRO_MUST_MATCH` 里 | 于是改了 prompt，历史日志的 `--replay` 仍判"通过"。这是为不动既有契约而做的保守选择；若要让复跑对 prompt 变敏感，需要把 `prompt_sha256` 加进 `PARAM_FIELDS` 并重跑全部历史日志 |
| 3 | `NEWS_EVENT_DRIVEN` 只作用于**决策链**（`assess`） | `tools/news_sources.py --event-driven` 自己那套状态/`calls` 计数照旧；两条路径共用 `news_state.json`，所以从 CLI 跑的 `--event-driven` 会影响决策链看到的"已见过"集合（这是**共享真相**，不是 bug，但值得知道） |
| 4 | `_mark_state` 复用了 `news_sources` 的私有函数 | `_load_state` / `_save_state` / `_item_key` 是下划线开头的内部实现。已用 `hasattr` 兜底（拿不到就如实报告、不假装成功），但上游改签名时我们会**静默降级**到"每轮都调" |
| 5 | **没有 `tools/` 的任何改动** | 任务边界只允许改 4 个文件 + 新建 `prompts/`、`docs/42`。所以 `news_sources.py` 的 `--event-driven` 输出**没变**："决策链真的读了这个配置"这件事只在 `project2/event_gate.py` 侧落地 |
| 6 | v1 prompt 没有文件 | 见 §1.4 |
| 7 | 缓存文件首次运行前不存在 | `gate_decision` 会判 `cache_missing` 并照常调 LLM —— 预期行为，但意味着**第一次**永远是全价 |

---

## 5. 实测证据

### 5.1 自检（不需要网络 / 不需要 key）

```powershell
python project2\event_gate.py --selftest
python project2\agent_team.py --selfcheck
python project2\execution_cost.py --selftest
python tools\news_sources.py --probe        # 需要网络
```

`event_gate --selftest` 里新增 30 条 T4-A/T4-B 断言，覆盖：
prompt 加载/兜底/解析失败/版本号比较、条目 key 与 `news_sources` 一致、
规则表 1~9、TTL 可配置、EDGAR Form 4 不误触发、
"复用缓存不改 severity"、"无 key 行为不变"。

### 5.2 事件驱动端到端（假 LLM 端点，不打网络）

用一个临时脚本把 `urllib.request.urlopen` 换成假端点（**只拦截
`/chat/completions`**，其余照旧），然后走**真实的** `assess(mode="llm")`：

```
[OK] 第 1 轮：调了 LLM（调用次数=1，source=llm，severity=block）
[OK] 实际发出的 system prompt 的 SHA256 == prompts/event_gate.v2.md 正文 == 内嵌兜底（51ca884e9247cdc4）
[OK] 第 2 轮（全旧条目，判定龄 12min < TTL 30min）：**没有**新增 LLM 调用（累计仍是 1 次）
[OK] 如实标注为缓存复用：source=llm(cache: 无新条目, 判定龄 12min｜TTL 30min)
[OK] 🔴 复用后 severity 与原判定一致（block -> block），**没有**降级成 none
[OK] 新 EDGAR 重大申报(10-Q) -> **跳过缓存立即调**（累计 2 次，source=llm）
[OK] 判定龄 31min > TTL 30min -> 照常调（reason=cache_expired…）
[OK] NEWS_EVENT_DRIVEN=off -> 连续两轮**都调**（各 +1）
[OK] 无 key：退化为 static 且明确标注（source=static(LLM 未执行: …，fail_closed=True）
```

### 5.3 复跑契约（真日志，非合成）

```powershell
python project2\agent_team.py --base NVDA --log --log-outdir _tmp_t4_replay
python project2\agent_team.py --replay _tmp_t4_replay\debate-NVDA-<时间戳>.json
```

结果：`==> 复跑**通过**：决策路径可复现`（契约字段全部一致，漂移 0 个）。

---

## 6. 复跑命令速查

```powershell
# ---- 自检（全部不需要网络/key）----
python project2\execution_cost.py --selftest
python project2\agent_team.py --selfcheck
python project2\event_gate.py --selftest          # 含 T4-A/T4-B 的 30 条断言

# ---- 消息面（需要网络）----
python tools\news_sources.py --probe
python tools\news_sources.py --base NVDA --event-driven --save

# ---- 闸门：看 prompt 版本/指纹 与 事件驱动配置 ----
python project2\event_gate.py --base NVDA
python project2\event_gate.py --base NVDA --assess          # 风险与理由引擎
python project2\event_gate.py --all --mode llm --assess     # 需要 .env 里配 key

# ---- 配置 ----
python common\config.py --check          # 当前生效配置（key 打码）
python common\config.py --write-example  # 刷新 .env.example（与代码里的模板必须一致）

# ---- 复跑契约 ----
python project2\agent_team.py --base NVDA --log --log-outdir data\reports
python project2\agent_team.py --replay data\reports\debate-NVDA-<时间戳>.json

# ---- 回溯某次判断用了哪一版 prompt ----
python -c "import hashlib,glob;[print(hashlib.sha256(open(p,encoding='utf-8').read().split(chr(10)+'---'+chr(10),1)[1].strip().encode()).hexdigest()[:16], p) for p in sorted(glob.glob('prompts/event_gate.v*.md'))]"
```
