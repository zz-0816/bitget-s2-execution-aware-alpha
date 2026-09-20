# 53 · 接入官方 MCP：把「数据源集成」做成可核验的能力

> 起因：用户让我去看 S2 手册里的 **MCP 与 SkillHub**，判断有没有能直接拿来优化的。
> 本文记录**看懂了什么、接了什么、拒绝了什么、以及接入时踩到的两个坑**。

---

## 0. 先说手册里与本项目直接相关的三条

| # | 手册原文（要点） | 对本项目的意义 |
|---|---|---|
| 1 | 赛道三「AI Trading Desk」的具名子主题里有 **「执行辅助」**：*trader 决策后，AI 如何负责拆单与滑点管理？典型思路：大单拆分；盘口深度分析；滑点模式调整* | 本项目做的**就是这一题** —— 表单选它 |
| 2 | 该赛道**评审重点**：功能深度（**数据源 / Skill 集成数量及有效性**）、研究质量、LUI 流畅性、个性化主张 | 接官方数据源**直接对应评分项** |
| 3 | 提交截止 **9/27（UTC+8）** | ⚠️ 与本仓库文档里写的 9/23 不一致，见 §5 |

手册里的工具链全貌（供选型）：Agent Hub（MCP Server / CLI / 89 个 UTA v3 工具 / Skills /
Agentic 账户 OAuth）、`bitget-signal` 5 个投研 Skill（**无需账号**）、
`bitget-mcp-server`（**美股 / ETF 只读数据，无需账号**）、Playbook + GetAgent（回测 /
Paper Trading）、Chainbase AgentKey（外部 Partner）。

> 赛道三手册原文：**「一般不需要：Playbook 回测（除非你的 Demo 本身含策略验证模块）」**
> —— 这条印证了本项目的赛道选择与工具选型。

---

## 1. 接了什么：`bitget-mcp-server` → 硬闸门的**第三个源**

### 1.1 它是什么（实测，不是照抄文档）

```
endpoint : https://agent.bitget.com/mcp      （HTTP 传输，SSE 应答）
凭证     : **不需要账号、不需要 API Key**
形态     : 两级 —— guide（列目录）→ do_query（执行，参数名是 entry_id）
目录     : crypto 40 ｜ **equity 21** ｜ etf 3 ｜ news 1 ｜ sentiment 2
```

美股的 21 个条目里，本项目用得上的是：`equity_price_quote`、`equity_calendar_earnings`、
`equity_fundamental_dividends`、`equity_ownership_insider_trading`、
`equity_estimates_consensus` / `price_target`、`equity_ownership_form_13f`…

### 1.2 为什么挑「财报日历 + 除息」

事件闸门原本有两个源：

| 源 | 覆盖 | 答不了的问题 |
|---|---|---|
| ① 确定性日历（`market_events.py`） | 期权到期、休市、in_house 窗口 | 这家公司**哪天财报**？**哪天除息**？ |
| ② LLM 判定（`event_gate.py`） | 读新闻标题判 block/caution/none | 同上 —— 它只能从标题里**猜** |

而这两件事**本来是确定的**。第二条尤其要命：

> **除息日现货腿会按股息金额下跳、永续腿不会跳 → 基差跳变**，
> 而 maker 策略正好靠基差吃饭。

### 1.3 接入当天就抓到一个真事件（这就是"有效性"）

```
META   除息日 = **2026-09-20（当天）** ｜ 0.525 USD/股 ≈ **7.8 bp** 的基差跳变
NVDA   除息 2026-09-09（已过 10 天） ｜ 财报 2026-11-17（+59 天）
TSLA   财报 2026-10-20 ｜ AAPL/META/GOOGL 2026-10-27~28 ｜ HOOD 2026-11-03 ｜ MRVL 2026-11-30
SPY/QQQ/SOXL  无财报（ETF）｜ HOOD 不分红
```

META 这条在接入之前**整条链路一无所知**。接上之后，META 的硬闸门从 `none` 抬到
**`caution`（来源 `mcp+llm`）**，理由行写着"除息日 2026-09-20（还有 1 天）｜股息 0.525 USD
≈ 7.8 bp 的基差跳变"。

### 1.4 三源合并：只取更保守的一侧

`merge_gate(static, llm, ext)` —— 三源取严重度最高的那个，**谁更严谁当来源**，
其余两源**写进 reason，不许被藏**。`ext=None` 时行为与两源版**逐字节一致**
（有自检守着，避免悄悄改了旧行为）。

### 1.5 严重度只到 `caution`，不到 `block`（这条边界是刻意的）

除息 / 财报都是**已知会过去**的事件，有明确的对冲动作（避开那个时点挂单），
不需要"完全停手"。`block` 留给"必须停手"：停牌、突发 8-K、日历硬约束。
改这条边界要连带改文档与自检 —— 所以它写在函数 docstring 的第一段。

### 1.6 🔴 "窗口内无事件" 与 "缺数据" 是两种东西

`gate_from_events()` 返回 `None` 的语义是**"窗口内没有事件"（判定结果）**；
而"读不到事件表 / 缓存里没有该标的"是**缺数据**。两者都不许被当成"没有事件"：

```
· 传了冻结值            -> 用它（复跑路径）
· 有缓存、窗口内无事件   -> ext=None，note="窗口内**无**财报/除息事件"
· 没有缓存 / 无该标的    -> ext=None，note="**没有外部事件缓存**……这不等于『没有事件』"
```

页面上第三个源那一行会把这句原文显示出来。

### 1.7 进复跑契约（与 `llm_event` 完全同一个模式）

外部事件是**活数据**（靠 `project2/ext_events.py --refresh` 更新）。不冻进契约，
隔一天复跑就会读到另一份事件表 → 必然不一致。所以：

* `PARAM_FIELDS` 增加 `ext_event`（现共 13 项）；
* `collect_params` 只记**实际用到的那份**（没有就是 `None`）；
* `--replay` 与 `tools/run_record.py` 的复跑都把它读回来当冻结输入；
* 自检里有闭环：写日志 → 用日志参数复跑 → `replay_check` 必须通过。

---

## 2. 接入时踩的两个坑（都值得记）

### 坑一：返回形状不一致会在某处 `.get()` 上炸

`gate_from_events()` 初版返回**元组** `(severity, reason, source)`，而 `merge_gate`
按 `llm_event` 的**字典**形状去 `.get("severity")` → `AttributeError: 'tuple' object
has no attribute 'get'`。

修法：统一成**字典**（与 `llm_event` 同形）—— 两者都是硬闸门输入、都要进契约、
都要被页面直接渲染，形状不一致迟早在别处再炸一次。

### 坑二：🔴 来源前缀不能用来判断"有没有用 LLM"

外部事件胜出时，生效来源变成 `mcp:bitget-mcp-server`，**前缀不再是 `llm`**。
而页面判断"这次用没用 LLM"看的就是前缀 → 于是页面底部写
**"本次未使用 LLM 判事件"**，而正上方 ② 行明明列着 LLM 判定 —— **自相矛盾**。

这个坑本项目**踩过一次**（2026-09-19：LLM 判 none 且与日历同级时，来源若写 static
就会自相矛盾）。当时的结论是"参与过就必须体现出来"，这次是同一个坑的**新入口**。

修法两条一起上：

* 后端：外部事件胜出时来源写成 `mcp:+llm` / `mcp:+llm(frozen)`，把参与标记**拼进来源**；
* 前端：不再猜来源字符串，直接看 `gate_merge.llm` 这**份记录**是否存在；
* 渲染冒烟加了一条**自相矛盾检测**：② 行有 LLM 而底部说"未使用 LLM" → 直接失败。

---

## 3. 明确**没有**接的（以及为什么）

| 条目 | 为什么不接 |
|---|---|
| `equity_estimates_consensus` / `price_target` | 是**第三方观点**，不是实测量。接进 evidence 会污染"每个数字都能点回实测来源"这条底线。要接必须先给 evidence 加 `source_kind: third_party` 这一层 —— 独立的一件事 |
| `equity_ownership_*`（内部人 / 13F） | 与新闻源高度重叠，边际价值低于上面两项 |
| Agent Hub `--read-only` / Agentic 账户 | **需要用户提供授权**（OAuth）。这是 `docs/52` P0-2 的路径，代码一行没写 —— 没 key 写出来也无法验证 |
| Playbook / GetAgent 回测 | 赛道三手册明写"一般不需要" |
| Chainbase AgentKey | 外部 Partner，**不是 Bitget 官方产品**；本项目当前用不上第 4 个数据源 |

---

## 4. 顺带改好的工程问题：自检步骤不再需要重编号

以前**每加一个工具，后面所有步骤都要重编号**（⑰→⑱→⑲ 一路顺延），而编号散落在
README / 提交材料 / 表单稿 / TASKS / CHECKLIST 十来处 —— 改一次就是一轮体力活、还容易漏。

新增 `_run_any(cmds, label)`：**一条自检步骤可以跑一串工具**。第 ⑱ 步现在同时跑
`market_feed --selftest`、`mcp_anchor --selftest`、`ext_events --selftest`，
**以后加工具不用再动编号**。

---

## 5. ⚠️ 一个与本项目文档冲突的事实：截止日期

| 来源 | 说的日期 |
|---|---|
| **S2 官方手册**（gitbook，时间线表两处 + 赛期描述） | **9 月 27 日**（UTC+8） |
| 同一页的 FAQ 里有一处写 | 9/23 |
| 本仓库 `docs/45` 记录（据称来自 Google Form） | 9 月 23 日 23:59 |

**两者不一致，必须由表单自己写的为准** —— 本仓库不擅自改日期，只把冲突记在这里。
行动：提交前打开 Google Form 读一遍它的原文。

---

## 6. 验证（全部可复跑）

```
python project2/ext_events.py --selftest      -> 15 项：窗口规则 / 只到 caution /
                                                 缺数据不硬算 / 缓存 None 语义
python project2/ext_events.py --refresh       -> 10 个标的，落盘 data/derived/ext_events.json
python project2/agent_team.py --decision-selftest
                                              -> 含"三源合并""来源保留 LLM 参与"
                                                 "带外部事件复跑一致"等断言
python run_p2.py --selftest                   -> 18 步全过、退出码 0
                                                 397 项 [OK] / 0 项 [!!] / 0 项 [skip]
tools/web_smoke.py                            -> 通过（含"闸门三源""LLM 参与标记一致"）
实拍 docs/ui-v3/16-gate-three-sources.png     -> META：三源摊开，生效 caution（来源 mcp+llm）
```


---

## 7. 追加：外部价格锚**接进决策链**（第 6 路分析师）

§3 里写着"`equity_price_quote` 只做了独立工具、还没接进决策链"。现在接上了。

### 7.1 为什么是**分析师**而不是风控规则

它带来的是"rToken 相对真实股票是否脱节"，这是一个**可观测的市场结构量**，
与其它 5 路同级；做成分析师就能进辩论、被证据强度加权，而不是只当一条硬规则。

### 7.2 🔴 两条纪律（写在函数 docstring 第一段，改它要连带改自检）

**① 只报偏离幅度，不报方向。**
刻意**不判**"折价 = 看多 / 溢价 = 看空" —— 那需要一个"偏离会收敛"的价格假设，
而本项目有红线：**不做价格预测**。所以：

```
|偏离| ≤ 20 bp  ->  neutral      （贴合；**不构成有利证据**）
|偏离| > 20 bp  ->  unfavorable  （脱节 = **风险**，不是机会）
```

**② 美股休市时两个价不同时刻，置信度必须压低。**
美股有开闭市、rToken 7×24。休市时量到的是"折溢价 + 休市漂移"、**无法分离**，
所以 `same_instant=False` 时置信度从 0.7 压到 **0.4**，并在 notes 里写明口径。

⚠️ `ANCHOR_DIVERGENCE_BP = 20.0` 是**约定，不是标定值** —— 只有一份偏离快照
（10 个标的，−117 ~ +34 bp），样本不足以标定。写成常量是为了"改它要留痕"。

### 7.3 取数与消费分开（避免"两份真相"）

```
tools/mcp_anchor.py --refresh   ->  取数 + 算偏离 + 判口径  ->  data/derived/anchor.json
project2/agent_team.py          ->  只读缓存，照抄口径，不说自己的话
```

缓存里同时存 `deviation_bp`（**稳定键名**）与 `field`/`label`（当日口径）。
下游不该为了取值去猜"今天用 premium_bp 还是 drift_and_premium_bp"。

### 7.4 进复跑契约

`anchor` 入 `PARAM_FIELDS`（现共 **14** 项）；`--replay` 与 `tools/run_record.py`
的复跑都把它读回来当冻结输入。自检有闭环：写日志 → 用日志参数复跑 → 契约全一致。

### 7.5 实测（十个标的，走同一份缓存）

```
NVDA  真实股 222.515 ｜ rToken 221.195 ｜ 偏离 **−59.3 bp** -> unfavorable + agent:anchor_divergence
META  真实股 668.485 ｜ rToken 670.805 ｜ 偏离 **+34.7 bp** -> unfavorable + agent:anchor_divergence
SPY   真实股 762.965 ｜ rToken 762.825 ｜ 偏离  **−1.8 bp** -> neutral（阈值内，**零假设**）
```

### 7.6 顺带修掉两处"写死路数"的旧断言

`DIMENSIONS` 现在有 **7** 项：**5 路常跑** + **2 路条件跑**
（`external_anchor` 要有外部锚数据、`execution_progress` 要有在途订单）。
两处旧断言写死了"5 路"，于是新增一路时变成假失败：

* `agent_team --selfcheck`：`len(items) == len(DIMENSIONS)` → 改成排除**条件路**；
* `run_p2 --selftest` 的 HTTP 冒烟：`len(dec["analysts"]) == 5` →
  改成断言"**5 路常跑的全在**"，不再写死总数。

同一个坑本轮踩了两次，所以两处都加了注释说明为什么不能写死。