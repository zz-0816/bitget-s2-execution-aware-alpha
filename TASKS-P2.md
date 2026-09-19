# 项目二任务清单（独立工作区）

> 生成 **2026-09-19** ｜ 由项目一的 `tools/isolate_p2.py` 自动生成
> 项目一工作区：`D:\bitgetS2_factory_trading`（**本项目不依赖它**）

## 0. 当前状态

| 项 | 状态 |
|---|---|
| 代码隔离 | ✅ 已拆出（34 个文件） |
| 数据快照 | ✅ 已生成 |
| 独立启动入口 | ✅ `python run_p2.py`（默认 8788 端口） |
| 自检 | ✅ `python run_p2.py --selftest` |
| 命令行演示 | ✅ `python run_p2.py --demo NVDA` |
| Demo 公网可访问 | ❌ **待办**（提交所需，见下 T1） |
| 独立仓库 | ❌ **待办**（T2） |
| 手册 6 段填表 | ⚠️ 草稿已就绪（README 内），需按表单格式再核一遍 |

## 1. 已复制进来的文件

- `project2/execution_cost.py`
- `project2/agent_team.py`
- `project2/event_gate.py`
- `project2/market_events.py`
- `project2/mcp_client.py`
- `project2/signal_adapter.py`
- `project2/events_calendar.json`
- `project2/demo_architecture.svg`
- `project2/demo_architecture.png`
- `common/config.py`
- `common/console.py`
- `common/market_calendar.py`
- `common/rag_memory.py`
- `tools/news_sources.py`
- `tools/position_watch.py`
- `tools/sentiment_sampler.py`
- `tools/threshold_calibration.py`
- `tools/model_compare.py`
- `tools/joint_fill_analysis.py`
- `tools/joint_fill_check.py`
- `tools/recover_sampling.py`
- `web/index.html`
- `web/app.js`
- `web/styles.css`
- `docs/14-往返摩擦预算与精确化成交判定.md`
- `docs/29-现货腿零成交事件（0914起）.md`
- `docs/32-代币化美股监管突破（SEC创新豁免）.md`
- `docs/33-Agent团队分工图与规格.md`
- `docs/34-项目二叙事重写（agent团队主线）.md`
- `docs/35-真实LLM实调记录（模型对比）.md`
- `docs/36-辩论层证据强度加权与阈值敏感性.md`
- `docs/25-多Agent协作方案（可行性评估）.md`
- `docs/DATA_DICT.md`
- `.env.example`

缺失项：
（无）

## 2. 待办任务（按优先级）

### 🔴 T1 · Demo 公网可访问（提交硬需求）

现在只能本机 `http://127.0.0.1:8788`。三条路：
1. **最小自包含包**（当前状态）：评委 `git clone` + `python run_p2.py` 即可用
   —— 若表单只收"仓库链接"就够用；
2. **公网部署**：需要服务器/域名；注意数据快照要一起部署；
3. **录屏 + 截图**：最省事，但赛道三重视"可访问"，分低一些。

### 🔴 T2 · 独立仓库与提交材料

- [ ] 建独立 git 仓库（`git init` 已可直接用，`.gitignore` 已就位）
- [ ] 确认 `common/` 是**冻结副本**（已在文件头标注来源与冻结日期）
- [ ] 填表：主题 = 赛道? · 子主题 ?（**待定：见 §3**）
- [ ] 免责与术语合规：「做市型价差捕获」「代币 ≠ 股权」「非投资建议」

### 🟡 T3 · 数据快照的补充

- [ ] 现在盘口是**截断**的（前 100 轮）——若演示需要更长区间，重跑项目一的
      `tools/export_p2_snapshot.py --rounds 400`
- [ ] 快照只覆盖 09-12~09-14 的现货成交带（`docs/29` 已说明原因）
- [ ] **现货腿零成交**必须写进材料（不是藏起来）

### 🟡 T4 · 你之前确认要补的 prompt / RAG 细化

- [ ] `docs/33` 规格表里所有 ⚠️ 项：每个 agent 的 prompt 版本化（现在 prompt 在
      `event_gate.LLM_PROMPT`，可外置为 `prompts/*.md` 便于版本管理）
- [ ] RAG：现在索引 182 块（口径文档 + 决策案例）；考虑加入
      "人工复核过的历史事件判定"作为校准集
- [ ] 事件驱动降本已做（`NEWS_EVENT_DRIVEN=on`）；可再加"EDGAR 新申报立即触发"

### 🟢 T5 · 前端（可选）

- [ ] 右下角**闪烁弹窗**接 `data/positions/alerts.json`（持仓期巡检已产出该文件）
- [ ] 页面目前是项目一的统一页面；项目二**独立页面**可只保留"执行决策"区块

## 3. 待你决定的一件事：投哪个主题

| 方案 | 第一次提交 | 第二次提交 |
|---|---|---|
| A | 赛道一 · 具名「套利」 | 赛道一 · **开放主题** Execution-aware Alpha |
| B（推荐） | 赛道一 · 具名「套利」 | **赛道三** · 具名「执行辅助」 |

见 `docs/31` 与项目一的 `docs/23` 修正 2。**定下来后本清单的 T2 才能完成。**
