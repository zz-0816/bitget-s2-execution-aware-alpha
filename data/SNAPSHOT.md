# 项目二数据快照（只读引用项目一的采样结果）

- 清单生成：**2026-09-22 16:55 UTC**（本文件由 `tools/snapshot_manifest.py` 生成，可 `--verify` 就地核验）
- 上游导出脚本：`tools/export_p2_snapshot.py`（**项目一仓库内**，可重跑）
- 合计：**58.4 MB**，35 项（其中冻结快照 23 项）

## ⚠️ 四条必须知道的边界

1. **这是快照，不是完整数据集**：盘口按轮次截断（见下表『截断』行），
   所以工具报的『最近一轮盘口』指的是**快照里的最后一轮**，不是『现在』。
2. **盘口不可回补**：交易所不提供历史 bid/ask，项目一的原始数据只存在于
   项目一那台机器上；本快照是**唯一**可分发的那一份。
3. **只读**：项目二不修改①类文件；重新生成请回项目一跑上面的导出脚本，
   然后在**本仓库**重跑 `python tools/snapshot_manifest.py` 刷新清单。
4. **三类文件核验方式不同**（这是被坑过的地方）：混在一张表里用同一个哈希
   去验，只会得到『误报』或『没人验』两种结果。分开列、分开验。

## ⚠️ 盘口为什么是『**尾段** 400 轮』而不是首段

上游导出脚本默认保留**前** N 轮盘口，而 trades 保留的是**尾部** N 行 ——
两者拼在一起会出现内部不一致：『最后一轮盘口』停在 09-18 19:19，
而『最后一笔成交』已经到了 09-19 07:16，**差了 12 小时**。
对一个卖点是「同一时刻的市场」的决策演示，这是硬伤（腿风险/成交率/深度
会来自两个不同时段的市场）。所以本仓库把盘口改成**保留最后 400 轮**，
与 trades 的尾部对齐。命令：

```powershell
# ① 按上游脚本导出（盘口此时是『前 400 轮』）
python tools/export_p2_snapshot.py --out <临时目录> --rounds 400 --trade-rows 80000
# ② 把盘口重截为『最后 400 轮』（与 trades 的尾部对齐）
python tools/retruncate_orderbook.py --src <项目一>\data\spread\orderbook-2026-09-19.csv --rounds 400
# ③ 刷新本清单（重算全部 SHA256 并重新分类）
python tools/snapshot_manifest.py
```

> ② 的自检：`python tools/retruncate_orderbook.py --selftest`（合成样本，
> 不碰真实数据）。它会验证『保留最后 N 轮』确实只留下最后 N 轮、
> 表头保留、且**源文件未被改动**。
> 想知道本仓库当前盘口的覆盖时段，直接读清单里的 SHA256 对应文件，
> 或看 `--verify` 之后的输出。

## 核验

```powershell
python tools/snapshot_manifest.py --verify    # 冻结项不一致 -> 退出码 1
```

## 文件清单

### ① 冻结快照（上游复制品）—— **SHA256 必须逐字节一致**

| 路径 | 处理 | 字节 | 行数 | SHA256(16) | 用途 | 重建/写入者 |
|---|---|---|---|---|---|---|
| `data/derived/friction_budget.csv` | 复制 | 903 | 9 | `8f0a4da8567f47f7` | 成本门槛与可捕获额（含 11.34 bp 门槛的输入） | - |
| `data/derived/funding_rates.csv` | 复制 | 889 | 10 | `29682ab7865284f5` | 资金费：48h 窗口收入（门槛里扣掉的那一项） | - |
| `data/derived/joint_fill_all.csv` | 复制 | 3,055 | 9 | `5d2335a7b32e760f` | 双腿联合成交四格（全部窗口） | - |
| `data/derived/joint_fill_all_in_house.csv` | 复制 | 3,219 | 9 | `c2a4f26dac04d889` | 同上，in_house 分层（模型实际取用） | - |
| `data/derived/joint_fill_all_stockroute.csv` | 复制 | 2,964 | 9 | `30801a51508d8316` | 同上，stockroute 分层 | - |
| `data/derived/joint_fill_check_in_house.csv` | 复制 | 1,563 | 9 | `86bd54aa627562aa` | 新旧口径影响对照 | - |
| `data/derived/precise_fill_perp_ask.csv` | 复制 | 2,379 | 10 | `df660587f081a72c` | 永续腿同上 | - |
| `data/derived/precise_fill_spot_bid.csv` | 复制 | 2,053 | 9 | `5830432dd331a2ac` | 现货腿成交率与逆向选择（逐笔实测） | - |
| `data/derived/threshold_calibration.json` | 复制 | 3,268 | 0 | `9a19a24e6cb9e58f` | 僵持阈值敏感性分析结果（docs/36） | python tools/threshold_calibration.py |
| `data/spread/2026-09-19.csv` | 复制 | 2,408,798 | 17,971 | `a9f84602523e7ae1` | 点差/中间价：定挂单价位与 route 对照 | - |
| `data/spread/2026-09-20.csv` | 复制 | 1,645,078 | 12,390 | `8076414ca3adbb43` | 点差/中间价：定挂单价位与 route 对照 | - |
| `data/spread/2026-09-21.csv` | 复制 | 3,566,093 | 26,875 | `82e357d4d2e9b752` | 点差/中间价：定挂单价位与 route 对照 | - |
| `data/spread/2026-09-22.csv` | 复制 | 3,690,783 | 27,455 | `ebbcf1a6f85c0b94` | 点差/中间价：定挂单价位与 route 对照 | - |
| `data/spread/2026-09-23.csv` | 复制 | 139,117 | 1,038 | `06c228b91d4eb1a6` | 点差/中间价：定挂单价位与 route 对照 | - |
| `data/spread/orderbook-2026-09-19.csv` | **尾段截断**（保留最后 400 轮） | 7,295,377 | 66,730 | `af434742857325cb` | 5 档盘口：容量/深度/首档约束（原文单日 37 MB） | - |
| `data/spread/sentiment-2026-09-18.csv` | 复制 | 233 | 1 | `523fd25639f79e2b` | 情绪采样：OI 与资金费率的实测值 | python tools/sentiment_sampler.py |
| `data/spread/trades-2026-09-12.csv` | 复制（全量） | 2,207,160 | 19,296 | `e253962ffe3d39e9` | 逐笔成交：成交率/逆向选择/联合分布，以及『最后一笔成交距今』（停牌判据）。⚠️ 截断规则见 TRADES_NOTE —— **现货行一条都不能丢** | - |
| `data/spread/trades-2026-09-13.csv` | 复制（全量） | 7,724,867 | 67,515 | `b871fa89e1736880` | 逐笔成交：成交率/逆向选择/联合分布，以及『最后一笔成交距今』（停牌判据）。⚠️ 截断规则见 TRADES_NOTE —— **现货行一条都不能丢** | - |
| `data/spread/trades-2026-09-14.csv` | 尾部 8 万行 + **现货行全量**（补回 3445 笔） | 9,528,697 | 83,445 | `77790b618bdf2e09` | 逐笔成交：成交率/逆向选择/联合分布，以及『最后一笔成交距今』（停牌判据）。⚠️ 截断规则见 TRADES_NOTE —— **现货行一条都不能丢** | - |
| `data/spread/trades-2026-09-19.csv` | 尾部 8 万行 + **现货行全量**（补回 916 笔） | 9,249,041 | 80,916 | `2139d5fa6258af87` | 逐笔成交：成交率/逆向选择/联合分布，以及『最后一笔成交距今』（停牌判据）。⚠️ 截断规则见 TRADES_NOTE —— **现货行一条都不能丢** | - |
| `data/spread/trades-2026-09-20.csv` | 现货全量（4206 笔）+ 永续尾部 2 万行 | 2,788,404 | 24,206 | `37950ea3bff6ed0d` | 逐笔成交：成交率/逆向选择/联合分布，以及『最后一笔成交距今』（停牌判据）。⚠️ 截断规则见 TRADES_NOTE —— **现货行一条都不能丢** | - |
| `data/spread/trades-2026-09-21.csv` | 现货全量（2946 笔）+ 永续尾部 2 万行 | 2,634,360 | 22,946 | `2f9cc7516bb25f05` | 逐笔成交：成交率/逆向选择/联合分布，以及『最后一笔成交距今』（停牌判据）。⚠️ 截断规则见 TRADES_NOTE —— **现货行一条都不能丢** | - |
| `data/spread/trades-2026-09-22.csv` | 现货全量（**该日 0 笔**）+ 永续尾部 2 万行 | 2,285,930 | 20,000 | `76bc782ab2924643` | 逐笔成交：成交率/逆向选择/联合分布，以及『最后一笔成交距今』（停牌判据）。⚠️ 截断规则见 TRADES_NOTE —— **现货行一条都不能丢** | - |

### ② 本仓库派生物 —— 哈希与上游那张表**本就不该一致**，核验方式是『能重建』

| 路径 | 处理 | 字节 | 行数 | SHA256(16) | 用途 | 重建/写入者 |
|---|---|---|---|---|---|---|
| `data/derived/rag_index.json` | **本仓库重建** | 2,658,377 | 0 | `dde16ab0ed7c28ff` （重建后会变） | RAG 索引（本仓库口径文档 + 本仓库决策案例） | python common/rag_memory.py --build |
| `data/reports/debate-NVDA-20231114T221320Z.json` | 本项目生成 | 45,468 | 0 | `190cd5a76c8c2cf8` （重建后会变） | 本项目决策日志（RAG 的『决策案例』来源，`--log` 产生） | python project2/agent_team.py --base NVDA --trader --log |
| `data/reports/debate-NVDA-20260917T181814Z-synthetic-viable.json` | 本项目生成 | 33,537 | 0 | `269992098af2a115` （重建后会变） | 本项目决策日志（RAG 的『决策案例』来源，`--log` 产生） | python project2/agent_team.py --base NVDA --trader --log |
| `data/reports/debate-NVDA-20260918T051936Z.json` | 本项目生成 | 33,644 | 0 | `01cb9227095f74d9` （重建后会变） | 本项目决策日志（RAG 的『决策案例』来源，`--log` 产生） | python project2/agent_team.py --base NVDA --trader --log |
| `data/reports/debate-NVDA-20260918T152117Z.json` | 本项目生成 | 39,934 | 0 | `ef2e20162fb61bde` （重建后会变） | 本项目决策日志（RAG 的『决策案例』来源，`--log` 产生） | python project2/agent_team.py --base NVDA --trader --log |
| `data/reports/debate-NVDA-20260918T152138Z.json` | 本项目生成 | 40,076 | 0 | `8af27d8b2848f5cf` （重建后会变） | 本项目决策日志（RAG 的『决策案例』来源，`--log` 产生） | python project2/agent_team.py --base NVDA --trader --log |
| `data/reports/debate-NVDA-20260918T165129Z.json` | 本项目生成 | 42,596 | 0 | `710aa63762771848` （重建后会变） | 本项目决策日志（RAG 的『决策案例』来源，`--log` 产生） | python project2/agent_team.py --base NVDA --trader --log |
| `data/reports/debate-NVDA-20260918T165244Z.json` | 本项目生成 | 46,877 | 0 | `4ba739e028c24900` （重建后会变） | 本项目决策日志（RAG 的『决策案例』来源，`--log` 产生） | python project2/agent_team.py --base NVDA --trader --log |
| `data/reports/debate-NVDA-20260919T164703Z.json` | 本项目生成 | 44,156 | 0 | `a8a208b059273c93` （重建后会变） | 本项目决策日志（RAG 的『决策案例』来源，`--log` 产生） | python project2/agent_team.py --base NVDA --trader --log |

### ③ 运行期可变 —— 采样/事件驱动会写它们，哈希只作参考，**不作为核验依据**

| 路径 | 处理 | 字节 | 行数 | SHA256(16) | 用途 | 重建/写入者 |
|---|---|---|---|---|---|---|
| `data/derived/event_driven_state.json` | 本项目生成 | 9,652 | 0 | `76bc1983ea08e1da` （参考值，运行期会变） | 事件驱动闸门的判定缓存（复用上次 LLM 判断 + TTL） | python project2/event_gate.py --selftest |
| `data/derived/news_latest.json` | 运行期写入 | 47,794 | 0 | `1566a183440278f5` （参考值，运行期会变） | 最近一次消息面抓取（事件闸门输入） | python tools/news_sources.py --base NVDA --save |
| `data/derived/news_state.json` | 运行期写入 | 139,406 | 0 | `d3118d4919ef9fb9` （参考值，运行期会变） | **事件驱动**的已见清单（避免重复调 LLM） | python tools/news_sources.py --event-driven --save |

