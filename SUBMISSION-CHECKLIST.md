# 提交清单（项目二 · Execution-aware Alpha）

> 用于 **Bitget Base Camp Hackathon S2** 项目二的提交前自查。
> **每条都写了怎么验证**（具体命令或文件路径）—— 打勾前请先跑那条命令。
> 依据：官方手册（经 `D:\bitgetS2_factory_trading\docs\23-手册要点核对（修正清单）.md` 核对）、
> `D:\bitgetS2_factory_trading\docs\31-两投材料与统一流程.md` §5、`docs\17-提交材料集中整理.md` §8。
> 填表成稿在 `docs/40-提交材料（填表照抄·项目二）.md`；赛道选择与两投接口在 `docs/41-赛道选择与两投接口.md`。

**图例**：🔴 = 不做即**无效提交**｜🟡 = 直接影响评分｜🟢 = 归档与形式

---

## 🔴 硬门禁（不做 = 无效提交）

### 🔴 1. 合规 X 帖 —— **必须转发官方帖**，且**发帖后再填表**

手册原文列出「什么算无效提交」时明确包含 **缺合规 X 帖**；且"纯转发或无实质介绍 = 提交不完整"。

- [ ] **已转发官方活动帖**（先转再发；是**转发**，不是引用转推）
- [ ] 正文含 **`#BitgetHackathon`**
- [ ] 正文含 **`@Bitget_AI`**
- [ ] **有实质内容**（介绍了你在构建的产品 / Agent / 策略，不是只有标签）
- [ ] **术语合规**：不出现"无风险套利 / risk-free arbitrage / 稳赚 / 保证收益"
- [ ] **不宣称收益**：不写"年化 X%"
- [ ] 若提到监管：措辞必须是"**场所与流动性提供者获得临时、有条件豁免**"，**不得**写成"SEC 批准了我们的策略"
- [ ] 写明"**代币 ≠ 股权**"
- [ ] **发帖之后再填表**（表单要填 X 帖链接）—— 顺序不能反

**怎么验证**：
- 点开自己的帖子，确认正文里能看到 `#BitgetHackathon` 与 `@Bitget_AI` 两个字符串；
- 点开自己主页的"转推"标签，确认官方帖在里面；
- 复制帖子链接贴进浏览器**无痕窗口**打开，确认**未登录也能看到**（不是仅粉丝可见）。

---

### 🔴 2. 项目说明 6 段（手册第四章的那道大题）

手册原文的 6 段：**① 思路（权重最高）② 目标用户与产品价值 ③ 验证数据与关键指标 ④ 完成度 ⑤ 材料清单 ⑥ 对 AI Trading 的看法（选填）**。
⚠️ 手册明确「**缺项目说明 → 直接判无效**」；且「目标用户与验证数据答不全不会判无效，但会明显拉低评审分」。

- [ ] 第 1 段 · 思路（为什么做、核心假设与逻辑、策略类的信号来源 / 决策逻辑 / 风控设计）
- [ ] 第 2 段 · 目标用户与产品价值（**具体画像**：风险偏好 / 资金规模 / 交易频率 / 主要市场与场景；**禁止写"所有 trader"**）
- [ ] 第 3 段 · 验证数据与关键指标（数字标明**实测 / 估算 / 目标**；再说明**怎么证明被有效使用或分发**）
- [ ] 第 4 段 · 完成度（做完了什么、还没做什么、遇到的问题与解法、下一步；用了哪些框架 / 模型 / API）
- [ ] 第 5 段 · 材料清单（列出「提交材料链接」里交了什么，方便评委定位）
- [ ] 第 6 段 · 对 AI Trading 的看法（选填）
- [ ] **「大模型在项目中的作用」独立字段**已填（开发期 + 运行期；运行期唯一职责 = 事件闸门；**无 key 时如实标注"本次未使用 LLM"**）
- [ ] **「数字不由 LLM 生成」这条红线**已写进材料

**怎么验证**：
- 直接照抄 `docs/40-提交材料（填表照抄·项目二）.md` 的第 1~6 段（每一段都标注了"可直接粘贴"），独立字段照抄 §7；
- 第 2 段粘完自查一遍：**有没有出现"所有 trader"**（有就删）；
- 第 3 段粘完自查一遍：**每个数字后面是否都带出处**（`docs/40` 第 3 段已按实测 / 估算 / 目标分三张表）。

---

### 🔴 3. 可访问的提交材料（仓库公开 + Demo 公网可访问）

- [x] **代码仓库已公开** —— <https://github.com/zz-0816/bitget-s2-execution-aware-alpha>（**PUBLIC**，默认分支 `main`，10 个提交已推送）
- [ ] **Demo 公网可访问**（不是 `127.0.0.1`）
- [ ] 所有链接集中填在表单「**提交材料链接**」**一个框**里

**仓库公开 —— 怎么验证**：

```powershell
# 本机状态（remote 已配好，分支 main）
git log --oneline -3
git remote -v                      # 应输出 origin  https://github.com/zz-0816/bitget-s2-execution-aware-alpha.git

# 远端状态（不需要登录即可读）
gh repo view zz-0816/bitget-s2-execution-aware-alpha --json visibility,defaultBranchRef
# 另外：确认 .env 没有被推上去（应返回 404 Not Found）
gh api repos/zz-0816/bitget-s2-execution-aware-alpha/contents/.env
```

再用**无痕窗口**打开仓库链接，确认未登录能看见代码与 `README.md`。

> ✅ 2026-09-19 已实测：仓库 PUBLIC、默认分支 `main`、10 个提交署名统一、
> `.env` 远端 404（密钥未泄漏）；并从公开 raw 地址下载数据文件，
> **字节与 `data/SNAPSHOT.md` 里的 SHA256 一致**（跨平台哈希修复生效）。

> ⚠️ 推送时的两个坑（记下来省时间）：
> ① 若 `git push` 报 `schannel: AcquireCredentialsHandle failed`，加
>    `-c http.sslBackend=openssl` 换 TLS 后端即可；
> ② 本仓库 `data/` 已标 `-text`，git 不做换行转换 —— **不要把这条规则改掉**，
>    否则 clone 后的数据字节会变，快照 SHA256 会全部对不上。

**Demo 公网可访问 —— 当前状态与做法（如实标注）**：

> ✅ **当前状态（2026-09-19 实跑核对）：已经跑通，并且从公网侧回打过。**
> - `run_p2.py` 支持 `--tunnel`：起网页 + 开一条 cloudflared quick tunnel，
>   自动抓出 `https://*.trycloudflare.com` 并打印（该行强制 flush，输出被重定向也看得到）。
> - 本机实测（2026-09-19）：`python -u run_p2.py --tunnel --port 8788` 打印出公网地址，
>   随后**用 Python 从公网侧回打**：
>   ```
>   OK   /api/health 200 720 bytes 3.6 s   （bases: 10 | snapshot: 2026-09-19 07:27 UTC）
>   OK   /          200 5501 bytes 2.4 s   （title: 执行决策台 · Execution-aware Alpha（项目二））
>   ```
> - 完整的部署路线（临时隧道 / Render / Fly / Docker / 自备服务器）、
>   常见故障与**提交前自证清单**：**`docs/43-公网可访问.md`**（该文件现已存在）。
> - ⚠️ **临时链接的代价**：进程一停就失效、每次重启域名都会变 ——
>   所以**提交当天必须重新生成一次并截图存证**（截图里要有哪四样，见 `docs/43` §2）。

- [ ] **步骤 1 · 确认本机 Demo 活着**（两个终端窗口）：

```powershell
# 终端 A：起服务（默认 8788 端口）
python run_p2.py --port 8788

# 终端 B：确认端点都对（现在共 7 个，全部只读 GET）
curl.exe http://127.0.0.1:8788/api/health                        # {"ok": true, ...}
curl.exe "http://127.0.0.1:8788/api/decision?base=NVDA&qty=5000"  # 完整决策链 + decision_hash
curl.exe "http://127.0.0.1:8788/api/assess?base=NVDA"             # rationale / warnings / conditions
curl.exe http://127.0.0.1:8788/api/overview                       # 全标的汇总
curl.exe http://127.0.0.1:8788/api/params                         # 生效阈值与来源
curl.exe http://127.0.0.1:8788/api/snapshot                       # data/SNAPSHOT.md 原文
curl.exe http://127.0.0.1:8788/api/alerts                         # 持仓期告警（可能为空）
```

- [ ] **步骤 2 · 装上 cloudflared**（`--tunnel` 依赖它）：

```powershell
winget install --id Cloudflare.cloudflared
# 装完先确认能找到（任一命中即可）
Get-Command cloudflared
```

  若 `winget` 不可用，也可以直接下单个二进制（**实测这条更稳**，
  因为 PowerShell 的 `Invoke-WebRequest` 在某些机器上会因 TLS 报
  "基础连接已经关闭"）：

```powershell
python -c "import urllib.request,os;u='https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe';o=os.path.join(os.environ['TEMP'],'cloudflared.exe');urllib.request.urlretrieve(u,o);print(o)"
# 程序会在 $env:CLOUDFLARED -> tools\bin\cloudflared.exe -> %TEMP%\cloudflared.exe -> PATH 里依次找
# 也可以直接指定：python run_p2.py --tunnel --cloudflared "<路径>"
```

- [ ] **步骤 3 · 一条命令起网页 + 隧道**（评审期建议用**保活**方式，见 `docs/44`）：

```powershell
# Windows：双击 启动Demo.bat（前台保活）或 后台运行Demo.bat（关掉窗口也不停）
# 任意平台：
python -u tools/keep_alive.py --port 8788
# 输出里会有一行：  ✅ 公网地址：https://<随机>.trycloudflare.com
# 它同时写进 data/run/public_url.txt —— 随时用 查看Demo状态.bat 查当前地址

# 不想保活、只想起一次（临时看两眼）：
python -u run_p2.py --tunnel --port 8788
```

  > ⚠️ 临时隧道的域名**每次重启都会变**。所以"当前地址"必须现场查
  > （`查看Demo状态.bat` 或 `data/run/public_url.txt`），**不要用旧截图里的**。

- [ ] **步骤 4 · 从公网侧验证**（不要只看隧道进程有没有打印成功）：

```powershell
# 把 <公网地址> 换成隧道给出的 URL（注意是 https）
curl.exe "https://<公网地址>/api/health"
```

  然后用**手机流量（不连家里 WiFi）**打开 `https://<公网地址>/`，
  确认页面能渲染、能选标的、能出结论与 `decision_hash`。

- [ ] **步骤 5 · 连续性**：隧道与 `run_p2.py` 进程要留到评审期结束
      （本机休眠 / 关窗 / 切网络都会掉线；quick tunnel 的 URL 每次重启都会变，
      **变了就要回填材料**）。想要半永久地址就走 `docs/43` §3（Render / Fly，都配好了）。
- [ ] **步骤 6 · 回填**：把公网地址写进 `docs/40` 第 5 段的对应行。
- [ ] **兜底**：若隧道无法长期稳定，**录屏 + 截图**并把视频链接写进材料 ——
      但要在材料里如实写"Demo 以录屏形式提供"，不要假装是可访问服务。
- [ ] **一键复现整条链**：`python run_p2.py --selftest` 里的第 ⑪ 步
      （HTTP 冒烟）会**照着前端声明的端点清单**逐个真打一遍，
      第 ⑫ 步还会用 Node 在最小 DOM 里把页面渲染一遍 —— 这两步就是防"页面空白但自检全绿"的。

---

## 🟡 评分相关

### 🟡 4. 内容与合规自查（粘完材料后逐条过）

- [ ] **术语**：全文用「**做市型价差捕获**」；没有"无风险套利 / risk-free arbitrage / 稳赚 / 保证收益"
- [ ] **代币 ≠ 股权**已写明（并统一用"代币化美股 / rToken"，不直接叫"股票"）
- [ ] **非投资建议**已写明
- [ ] **本项目不代下单**已写明（输出只有理由与条件，没有任何下单接口被调用）
- [ ] **监管表述准确**：不是"SEC 批准了我们的策略"，准确表述见 `docs/40` §9 第 6 条
- [ ] **局限 10 条已逐条写进材料**（`docs/40` §10）；主动写局限是可信度来源，不要删
- [ ] 材料里**没有**出现 §7「不要写什么」里的三条

**怎么验证**：在粘好的正文里搜关键词 ——

```powershell
# 在 docs/40 里搜禁用词（应全部为空）
Select-String -Path docs\40-*.md -Pattern "无风险套利|risk-free|稳赚|保证收益"
```

若上一条有输出，说明 `docs/40` 里只在**红线说明**处提到这些词（那是允许的）；但**你粘进表单的正文里不能有**，请以粘贴后的文本为准再搜一遍。

---

### 🟡 5. 关键数字可复跑（材料里每个数字都要能跑出来）

跑下面这几条，材料第 3 段的数字就能逐条对上：

```powershell
python run_p2.py --selftest                           # 12 步全部 [OK] / 0 失败，退出码 0（无需网络与 key；项数会随自检增加，**以现场输出为准**）
python project2\agent_team.py --selfcheck             # agent 层单独跑：全部 [OK] / 0 失败
python run_p2.py --demo NVDA                          # 完整决策链（本次实测：最终不参与、0 USD）
python run_p2.py --demo META                          # 可复算的 agent 闭环：agent:stale_quotes -> reject
python project2\execution_cost.py --base NVDA --two-leg   # 三方案并排 + 腿风险期望
python project2\event_gate.py --assess --base NVDA --mode static   # 风险与理由（不给 --base 会报错退出）
python tools\joint_fill_check.py                      # 只读；新旧口径对照（GOOGL 17.67x / META 12.32x / 3-of-9）
python tools\snapshot_manifest.py --verify            # 冻结快照逐字节核验（当前 26 项 / 39.6 MB，冻结 16 项）
python tools\event_calibration.py --selftest          # 校准集 schema + 覆盖 + **留出检查**（离线）
python tools\retruncate_orderbook.py --selftest       # 盘口"保留最后 N 轮"的自检（合成样本）
```

- [ ] 上述命令**全部**跑过，输出与 `docs/40` 第 3 段表格一致
- [ ] 材料里的每个数字都能在 `docs/40` 里点回一个路径或命令

> ⚠️ **不要跑** `python tools\threshold_calibration.py` —— 它会把结果**覆盖写入** `data/derived/threshold_calibration.json`（见该脚本的 `OUT` 常量）。
> 要核对阈值敏感性，直接读该 JSON。

---

### 🟡 6. 两投的独立性与接口

- [ ] **分两次填表**（手册：每个主题须是独立项目，**不能合成一项提交**）
- [ ] 项目二填的是**自己的入口**：`python run_p2.py`（8788），**不是**项目一的 8787
- [ ] 本份材料里写了**与项目一的七个交接点**（`docs/41` §2）
- [ ] 项目一的材料里也写了**与项目二的接口**（七个交接点，项目一仓库 `docs/31` §1）
- [ ] 主题措辞已定：填 `docs/40` §8 **甲套**（赛道三 · 具名「执行辅助」）或**乙套**（赛道一 · 开放主题 Execution-aware Alpha）
- [ ] 主题与子主题**两套措辞都备好**（`docs/40` §8），切换成本只是换一段文字

**怎么验证**：
- 打开表单，确认这是**第二次**填表（第一次是项目一）；
- 搜自己粘的正文里有没有"项目一 / 姊妹项目 / 另一份材料"这类接口描述（`docs/41` §2 的七个交接点表可直接压缩成两三句）。

---

### 🟡 7. 表单勾选与补贴

- [ ] **Demo Day**（建议勾选；**未填视为否**）
- [ ] **K3 补贴**（按需，需完成 KYC；与 Qwen 可叠加，**单队最多 60U**）
- [ ] **高校名称**（如适用则填学校全称；**与主赛道奖互斥**，与 Demo Day 不互斥）
- [ ] **Qwen 额度**走**独立表单**（前 300 支通过 KYC 的队伍 30U；Base URL `https://hackathon.bitgetops.com/v1`，Model `qwen3.8-max`）
- [ ] **最受欢迎项目奖**：9/22–9/28 公众在 X 评论区投项目编号（与所有评委奖**可叠加**）
- [ ] 禁止复用 S1：若延续 S1 思路须说明**实质性新增内容**

**怎么验证**：提交后把表单的**确认页 / 回执截图**存进 `docs/`（归档用），并核对你勾的项与上面一致。

---

## 🟢 归档

### 🟢 8. 归档动作

- [ ] 本仓库所有改动已提交（**不要**把 `data/spread/*` 的运行期抖动当成代码改动一起提交 —— 先看清楚 `git status`）
- [ ] 表单确认页 / 回执截图存进 `docs/`
- [ ] X 帖链接、仓库链接、Demo 公网地址**三处都有备份**（写进 `docs/40` 第 5 段的对应行）

```powershell
git status --short
git log --oneline -5
```

> ⚠️ 本仓库的 `data/` 下有采样器运行期仍在写入的文件（`data/SNAPSHOT.md`、`data/derived/news_state.json`、`data/derived/rag_index.json`、`data/derived/threshold_calibration.json`、`data/spread/2026-09-19.csv`、`data/spread/orderbook-2026-09-19.csv`、`data/spread/trades-2026-09-19.csv` 会显示为 modified），代码与文档也在同步改动中。
> **提交前先决定**：是连这些更新一起提交，还是只提交代码与文档 —— **不要不加判断地 `git add -A`**。
> 判断方法：先 `git status --short`，再看每个文件的 diff 是不是你有意为之。

---

## ⚠️ 不要写什么

> 这三条抄准（来源：`docs/34-项目二叙事重写（agent团队主线）.md` 文末的三条 ⚠️）。
> 它们是**已经踩过的坑**，写进去就是硬伤。

| # | **不要写** | 为什么 | 该写什么 |
|---|---|---|---|
| 1 | ❌ "rToken 跟踪偏离 **−33~+85 bp**" | 该结论**已撤回**：初版错在**跨时点相减**（当前快照 vs 时间中位），量到的是"周末涨跌幅"而不是跟踪偏离 | 用 Yahoo 原生股价做**完全外生**参照、严格同刻对齐后，实测偏离**中位 +1.7 bp**、区间 `[−7.1, +12.0] bp`（n=10），**跟踪很紧、该风险不成立**。出处：`docs/14` §4.2 |
| 2 | ❌ "用周末样本跑了 **60 天回测**" | **两条轴必须分开声明** —— 周末是可交易窗口，"60 天"是样本跨度；混成一句话会让读者以为我们在周末样本上做了 60 天连续回测 | 分开写：① 可交易窗口 = 平台所内撮合窗口（周末/节假日）；② 样本跨度 = 具体多少天、覆盖哪几个窗口。⚠️ 另外：**60 天回测是项目一的材料，本项目（项目二）没有回测记录**，不要在项目二的材料里出现 |
| 3 | ❌ "**SEC 批准了我们的策略**" | 豁免是**临时、有条件**的，且**正在征求意见**；这句话既不准确，也把自己置于"合规无风险"的错误位置 | "场所与流动性提供者获得了**临时、有条件的豁免**，我们的**做市型价差捕获**落在该豁免认可的**做市行为范围内**；豁免**附条件、有期限（5 年）、且正在征求意见**。" 出处：`docs/32` §4 |

**额外一条自查红线**（同样重要）：材料里**不得出现**「无风险套利 / risk-free arbitrage / 稳赚 / 保证收益」——
统一用「**做市型价差捕获**」；并写明「**代币 ≠ 股权**」与「**非投资建议**」。

---

## 📋 一页速查（照这个顺序做）

1. **跑一遍自检**：`python run_p2.py --selftest` → 12 步全部通过 / 0 失败
2. **核验快照**：`python tools\snapshot_manifest.py --verify` → 26 项 / 39.6 MB（冻结快照 16 项，逐字节核验）
3. **起 Demo 并挂隧道**：`winget install --id Cloudflare.cloudflared` → `python run_p2.py --tunnel` → 用手机流量验证公网地址
4. **仓库已公开** ✅ <https://github.com/zz-0816/bitget-s2-execution-aware-alpha>（推新提交：`git push`）
5. **发 X 帖**：先**转发官方帖**，正文含 `#BitgetHackathon` + `@Bitget_AI` + 实质内容 → 拿链接
6. **填表**：`docs/40` 逐段粘贴（6 段 + 大模型作用 + 主题措辞 + 局限 10 条 + 术语红线）
7. **链接进一个框**：仓库 / Demo / X 帖 / 材料清单（`docs/40` 第 5 段）
8. **勾选**：Demo Day（建议）/ K3（按需）/ 高校（如适用）
9. **回执归档**：截图存 `docs/`
