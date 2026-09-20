# 项目二任务清单*独立工作区）

> 生成 **2026-09-19** ｜ 由项目一的 `tools/isolate_p2.py` 隔离生成
> 项目一工作区：`D:\bitgetS2_factory_trading`***本项目运行时不依赖它**）
> 最近更新 **2026-09-19**：补齐"可独立提交"的全部缺漏*见 §2 完成记录）

---

## 0. 当前状态

| 项 | 状态 |
|---|---|
| 代码隔离 | ✅ 已拆出*34 个文件） |
| 数据快照 | ✅ 已生成，**盘口改为"最后 400 轮"***39.6 MB / 26 项，与 trades 尾部对齐） |
| 快照可核验 | ✅ `python tools/snapshot_manifest.py --verify`*冻结项逐字节） |
| 独立启动入口 | ✅ `python run_p2.py`*默认 8788 端口） |
| 自检 | ✅ `python run_p2.py --selftest`（**17 步离线 + 2 步联网**；实测 330 项 [OK] / 0 项 [!!] / **0 项 [skip]**，含 HTTP 冒烟 + 页面渲染冒烟 + **真浏览器布局验收** + 事件判定回归门槛） |
| 命令行演示 | ✅ `python run_p2.py --demo NVDA` |
| **独立页面** | ✅ 已重写为项目二自己的页面*原来那个是项目一的，独立跑是**空白页**） |
| **公网可访问** | ✅ 三条路都可跑：临时隧道***已从公网侧实测回打**）/ Render / Fly / Docker —— 见 `docs/43` |
| prompt 版本化 | ✅ `prompts/event_gate.v4.md`*两步判断：先相关性再类别；可回溯到版本 + SHA256） |
| 事件驱动降本 | ✅ 真正接进决策链**且实测命中***长跑复用率 **70.9%**；原来只是配置里写着，没代码读它） |
| **事件判定回归门槛** | ✅ `--selftest --net` 第 ⑲ 步：留出集 n=10，**危险方向错误必须为 0**、一致率 ≥90%、不得比基线退化*`data/calibration/baseline.json`） |
| 事件判定校准集 | ✅ `data/calibration/event_judgments.json`*n=10，**对 RAG 留出**） |
| **执行进度官***P0） | ✅ 第 6 路 agent：裸露敞口 → veto、挂单停滞 → caution；`/api/decision?position=demo` + 前端 ⑥ 面板 —— 见 `docs/48` |
| **长跑记录** | ✅ `tools/run_record.py`*稳定性/效率的实测证据源；已加 `src_sha16` **代码指纹**按版本分组，避免混版本算成功率） |
| 提交材料 | ✅ `docs/40`*填表照抄）+ `docs/41`*赛道对比与两投接口） |
| 提交前清单 | ✅ `SUBMISSION-CHECKLIST.md` |
| 独立仓库 | ✅ 已推上 GitHub*`zz-0816/bitget-s2-execution-aware-alpha`，**public**） |
| **合规 X 帖** | ❌ **只能你本人发***硬门禁：无 X 帖 = 提交不完整） |
| 主题定案 | ⚠️ 两套措辞都已备好，**临场勾选***见 §3） |

---

## 1. 文件构成

### 1.1 隔离时复制进来的*34 个）

`project2/`*execution_cost · agent_team · event_gate · market_events · mcp_client ·
signal_adapter · events_calendar.json · 架构图）、`common/`*config · console ·
market_calendar · rag_memory，**冻结副本**）、`tools/`*消息面 · 持仓巡检 · 情绪采样 ·
阈值敏感性 · 模型对比 · 联合分布 · 采样恢复）、`web/`、`docs/`*14/25/29/32/33/34/35/36/DATA_DICT）、
`.env.example`

### 1.2 本项目新增*本次补齐缺漏）

| 文件 | 为什么需要 |
|---|---|
| `run_p2.py`*重写） | 原入口只提供 3 个端点，**页面要的 4 个端点根本不存在**；现在端点、自检、隧道、部署全在这里 |
| `web/index.html` `web/app.js` `web/styles.css`*重写） | 独立页面：决策链 / 概览 / 阈值透明化 / 快照 / 告警弹窗 |
| `tools/snapshot_manifest.py` | 快照清单**生成 + 就地核验**；把三类文件分开列分开验 |
| `tools/web_smoke.py` + `tools/web_render_check.js` | 真起服务 → 真取数 → 在最小 DOM 里**真渲染一遍**页面 |
| `tools/event_calibration.py` + `data/calibration/event_judgments.json` | 事件判定的一致性/危险方向校准；**留出规则***校准集不得进 RAG） |
| `prompts/` | 事件判断 prompt 外部化 + 版本化 |
| `data/reports/debate-*.json|.md`*14 个） | 让 RAG 的"决策案例"**出处真实存在***原来 7 条案例指向的文件根本没复制进来） |
| `Dockerfile` `render.yaml` `fly.toml` `requirements.txt` `.dockerignore` | 一键部署 |
| `启动Demo.bat` `后台运行Demo.bat` `查看Demo状态.bat` `停止Demo.bat` `start_demo.sh` | 双击/一条命令起服务 + 公网链接；**带保活***崩溃自愈、公网地址落盘） |
| `tools/keep_alive.py` `tools/demo_launcher.py` | 保活守护与启动器*`.bat` 只做 ASCII 壳，中文与逻辑都在 Python 侧） |
| `docs/44-长期运行与保活.md` | 保活的原理、边界*能扛什么/扛不住什么）与开机自启 |
| `docs/40` `docs/41` `docs/42` `docs/43` `SUBMISSION-CHECKLIST.md` | 提交材料、赛道接口、prompt 与事件驱动、部署 |

### 1.3 本次修掉的三处"说了没做"

这三条都是**文档/配置里写着、代码里没有**，属于本项目最该避免的失败模式：

| # | 症状 | 修法 |
|---|---|---|
| 1 | `NEWS_EVENT_DRIVEN=on` 写在 `common/config.py` 与 `.env.example` 里，注释说"只在出现新条目时才调 LLM"，但**没有任何代码读这个配置** → 每轮都调 | 真正接进决策链：无新条目时**复用上次 LLM 判断***带 TTL，如实标注判定龄），**不允许**降级成 none；EDGAR 新申报则跳过缓存立即调。见 `docs/42` |
| 2 | 页面调用的 4 个端点只有项目一的服务才有 → 独立跑**整页空白**，而自检全绿 | 端点全部实现在 `run_p2.py`；自检 ⑨ 照着前端声明的端点真打一遍，⑩ 再用 Node 渲染一遍 |
| 3 | `common/rag_memory.py` 的索引包含了 `project2/README.md`***项目一目录结构**下的路径），本仓库没有 → 静默少索引一整类文档 | 改为按本仓库真实结构取；并把本地修改**逐条记在文件头***冻结副本的漂移记录） |

---

## 2. 待办***只剩需要你本人在场才能做的**）

### 🔴 T1 · 合规 X 帖*硬门禁，不做即无效提交）

- [ ] 转发官方活动帖***必须是转发，不是引用转推**）
- [ ] 内容含 `#BitgetHackathon` + `@Bitget_AI`，**有实质介绍***纯转发 = 不完整）
- [ ] 发帖**之后**再填表*表单要填帖子链接）
- 草稿与检查单：项目一 `docs/16-X帖草稿.md`

### 🔴 T2 · 主题勾选*两套措辞都已写好，你只需要选一个）

见 §3。填表文案在 `docs/40` §「主题与子主题」；对比与推荐在 `docs/41`。

### 🟡 T3 · 提交当天的公网链接与截图

```powershell
python run_p2.py --tunnel          # 拿到临时公网地址 -> 截图*含地址栏）
python run_p2.py --demo NVDA       # 命令行版，写进材料
```

- [ ] 从**另一台机器/手机**打开过公网链接*不是只在本机点过）
- [ ] 截图含：地址栏 + 页面顶部**快照时间点** + 最终裁决与 `decision_hash`
- [ ] 材料里写明"这条链接是临时的"，并给出失效后自己起的一条命令

### 🟢 T4 · 可选*不影响提交有效性）

- [ ] 录屏*`docs/43` §2 列了"截图里必须出现哪四样"）
- [x] 事件判定校准：`python tools/event_calibration.py --gate` —— v4 实测
      **10/10、危险方向错误 0 条***基线已落盘，且接成自检第 ⑲ 步防回退）
- [ ] 让长跑记录**继续累积带代码指纹的轮次***`python tools/run_record.py --interval 120
      --poll-news --replay-every 3`）：旧 412 轮是改动前的代码跑的，
      **不能**给当前代码背书；`python tools/run_record.py --report` 可随时看

---

## 3. 投哪个主题*两套都备好了，临场勾）

| 方案 | 赛道 / 子主题 | 评分方式 | 说明 |
|---|---|---|---|
| **A** | 赛道三 AI Trading Desk · 具名「**执行辅助**」 | 纯评委主观 | 手册点名要的正是"拆单 + 盘口深度分析 + 滑点管理"，本项目三样全有 |
| **B** | 赛道一 Alpha Factory · **开放主题** Execution-aware Alpha | 纯量化 | 沿用 `docs/34` 的既有叙事 |

**推荐 A**：两次提交落在**两个不同赛道***项目一在赛道一），风险不相关；
B 会让两份材料同处赛道一*同批评委、同一套数据叙事），互相稀释差异化。

⚠️ 规则层面两者都不违规：手册允许"最多向 2 个不同主题提交，每个主题须是独立项目，
分两次填表"。选 A 不是为了合规，是为了**不把两个项目放进同一个池子**。

**Demo 的麻烦程度与赛道无关** —— 同一个仓库、同一个页面、同一条命令；
赛道三反而**不需要**展示 Sharpe/回撤那套回测指标，页面要讲的东西更少。
