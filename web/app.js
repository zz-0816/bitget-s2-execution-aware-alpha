/* 项目二 · 执行决策台 —— 前端逻辑
   只消费本仓库 run_p2.py 提供的 /api/*；无外部依赖（离线可用）。

   ⚠️ 记录一个踩过的坑：这个页面最初是项目一的统一页面，调用的是
   /api/overview、/api/timeline、/api/data-status —— 那三个端点只有项目一
   的服务才有。独立跑起来时页面全是"加载中…"，而所有自检都是绿的
   （因为没有一个自检去碰 HTTP）。现在：端点集合集中写在 API 里，
   run_p2.py --selftest 会照着这份清单真的打一遍。

   ⚠️ 第二条纪律：页面文案里不要出现 markdown 标记（粗体星号之类）。
   文本框里写了星号只会原样显示出来 —— tools/ui_check.py 专门抓这个，
   tools/ui_design_check.py 也会在静态文件里扫一遍。 */

'use strict';

const $ = (id) => document.getElementById(id);

// ⭐ 页面调用的全部端点（与 run_p2.py 的路由一一对应）
const API = {
  health: '/api/health',
  bases: '/api/bases',
  decision: '/api/decision',
  assess: '/api/assess',
  overview: '/api/overview',
  params: '/api/params',
  snapshot: '/api/snapshot',
  alerts: '/api/alerts',
  account: '/api/account',
};

/* 面板/页面状态。
   pane  —— 当前功能页（决策台 / 执行闭环 / 全标的概览 / 可信度）
   stage —— 决策台里当前查看的决策链段落（步进器下标） */
const STATE = { base: null, qty: 5000, busy: false, seenAlerts: new Set(),
                pane: 'pane-decision', stage: 0 };

/* ---------------- 动效（可关、可验证） ---------------- */

/* 系统开了"减少动态效果"就一律不做动画 —— CSS 侧用 --m 开关，
   JS 侧（条形生长、数值闪烁）在这里单独判断。 */
const REDUCE_MOTION = !!(window.matchMedia
  && window.matchMedia('(prefers-reduced-motion: reduce)').matches);

/* 让条形从 0 长到目标宽度。
   ⚠️ 不能直接写 style="width:X%"：元素一出生就是终态，CSS transition 不会触发。
   必须先以 0 渲染，下一帧再改成目标值 —— 这是"看起来有没有做动效"的分界线。
   ⚠️ rAF 做个兜底：web_smoke 的 Node 最小 DOM 里没有 requestAnimationFrame，
   不兜底会让"页面渲染冒烟"整个挂掉（那一步正是用来防页面坏掉的）。 */
const RAF = (typeof window !== 'undefined' && window.requestAnimationFrame)
  ? window.requestAnimationFrame.bind(window)
  : function (fn) { setTimeout(fn, 16); };

function animateBars(root) {
  const scope = root || document;
  if (!scope.querySelectorAll) return;
  scope.querySelectorAll('.cb-fill[data-w]').forEach(function (el) {
    const w = el.getAttribute('data-w');
    if (REDUCE_MOTION) { el.style.width = w; return; }
    el.style.width = '0%';
    RAF(function () { RAF(function () { el.style.width = w; }); });
  });
}

/* ---------------- 工具 ---------------- */

function esc(s) {
  return String(s === null || s === undefined ? '' : s)
    .replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function mdInline(s) {
  // 只做最小够用的行内标记：**粗体** 与 `代码`（先转义再替换，避免 XSS）
  return esc(s)
    .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
    .replace(/`([^`]+)`/g, '<code>$1</code>');
}
/* 把行内标记去掉，得到纯文本 —— 给 title 属性用。
   ⚠️ 用 RegExp 构造而不是写字面量正则：源码里出现字面的双星号会被
   tools/ui_design_check.py 的静态扫描报出来（它分不清注释与真正的输出）。 */
const MD_MARK_RE = new RegExp('\\*\\*|`', 'g');
function plain(s) {
  return String(s === null || s === undefined ? '' : s).replace(MD_MARK_RE, '');
}
function fmt(v, d = 2) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '—';
  return Number(v).toFixed(d);
}
function pct(v, d = 2) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '—';
  return (Number(v) * 100).toFixed(d) + '%';
}
function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

/* ---------------------------------------------------------------------------
   把契约字段名翻成"人话"（只做显示层映射，接口字段一个不动）
   ---------------------------------------------------------------------------
   概览的 `conditions` 给的是契约键名：price_band_bp / size_usd / timing。
   直接摊到页面上等于让读者去猜 `price_band_bp=0.46` 是什么意思。
   这里翻成中文，并把值里的技术词也换掉（route=in_house → 内部撮合）。
   ⚠️ 不改接口字段：run_record / 表单稿引用的是原始键名，改了会破坏可复跑契约。
   未知键**不隐藏** —— 回退成"键名去下划线 + 原值"，宁可丑一点也不吞信息。

   ⭐ v3.1：把每条条件拆成 **短值** 与 **解释** 两半。
   原因是实测的冗余：这 3 条条件**每个标的都完全一样**（同一套口径、同一段解释），
   而差异只在数值上。逐行重复整段解释 -> 4 行就占掉半个屏幕，真正要看的东西被挤走。
   现在：行内只留短值（差异化的部分），解释**提到表格下方只写一次**。 */
const COND_LABEL = {
  price_band_bp: '现货点差',
  size_usd: '单笔规模',
  timing: '挂单时机',
};

/* 解释文案（表格下方图例里只出现一次）。
   写成常量是因为里面要引用"这个项目为什么这么定"的依据。
   ⚠️ 这里**不写 markdown 的星号加粗**：这些文案会走 mdInline，
      但静态检查（ui_design_check）会把源码里的 `**` 判成"页面上会原样显示"。
      与其给检查开例外，不如直接不写 —— 图例里本来就不需要强调。 */
const COND_EXPLAIN = {
  price_band_bp: '挂单赚不回点差，就等于白付一次手续费 —— 所以点差是挂单的成本底线，不是收益。',
  size_usd: '单笔规模上限 = min(可捕获名义额、首档深度 × 25%)，超过就不是"能吃到的量"了。',
  timing: '外部股票路由期间挂单不省点差；只有在内盘撮合时才值得挂单。',
};

function plainToken(s) {
  return String(s === null || s === undefined ? '' : s)
    .replace(/route\s*=\s*in_house/gi, '内部撮合')
    .replace(/stockroute/gi, '外部股票路由')
    .replace(/\broute\b/gi, '撮合路由')
    .replace(/\bsession\b/gi, '交易时段');
}

/* 返回 { short, full }：short 进表格（只放差异化的值），full 用于悬停/展开 */
function condParts(k, v) {
  if (k === 'price_band_bp') {
    const bp = fmt(v);
    return {
      short: '<span class="cv">' + bp + ' bp</span><span class="cq">→ 需覆盖 ' +
        fmt((Number(v) || 0) / 2) + ' bp</span>',
      full: '<b>现货点差</b> ' + mdInline(bp) + ' bp —— 挂单需至少覆盖 ' +
        mdInline(fmt((Number(v) || 0) / 2)) + ' bp 才不亏手续费',
    };
  }
  if (k === 'size_usd') {
    return {
      short: '<span class="cv">≤ ' + fmt(v, 0) + ' USD</span>',
      full: '<b>单笔规模</b> 不超过 ' + mdInline(fmt(v, 0)) + ' USD',
    };
  }
  if (k === 'timing') {
    const t = plainToken(v);
    // "仅在内盘撮合时挂单；外部股票路由 期间挂单不省点差" 里的后半段也是通用解释，
    // 行内只留前半段，后半段进图例。
    const head = String(t).split(/[；;]/)[0] || t;
    return { short: '<span class="cv">' + esc(head) + '</span>',
             full: '<b>挂单时机</b> ' + mdInline(t) };
  }
  return { short: '<span class="cv">' + esc(plainToken(v)) + '</span>',
           full: '<b>' + esc(COND_LABEL[k] || String(k).replace(/_/g, ' ')) + '</b> ' +
             mdInline(plainToken(v)) };
}

async function api(path) {
  const r = await fetch(path, { cache: 'no-store' });
  const d = await r.json().catch(() => ({ ok: false, err: 'HTTP ' + r.status }));
  if (!r.ok || d.ok === false) throw new Error(d.err || (path + ' -> HTTP ' + r.status));
  return d;
}

const STANCE_CN = { proceed: '可执行', caution: '谨慎执行', stand_down: '不参与' };
const RISK_CN = { low: ['低', 'pos'], medium: ['中', ''], high: ['高', 'neg'] };

/* ---------------- 顶栏 ---------------- */

/* 📉 数据新鲜度：顶栏一句话 + **只在危险时**弹横幅。
 *
 * 两种"旧"必须区别对待，否则这个提示会变成恒红、等于没有信号：
 *   · declared_offline（basis=asof）：离线演示读冻结快照，旧是**声明过的模式**
 *     -> 顶栏如实写"离线演示 · 滞后 X"，**不弹横幅**；
 *   · stale（basis=wallclock 且输入落后超阈值）：你以为在实时决策，其实输入停了
 *     -> 弹红条。这时"旧数据比没有数据更危险"：行情停滞/报价冻结这些按龄判据会失真。 */
function fmtAge(min) {
  if (min === null || min === undefined) return '—';
  if (min < 90) return fmt(min, 1) + ' 分钟';
  if (min < 60 * 36) return fmt(min / 60, 1) + ' 小时';
  return fmt(min / 1440, 1) + ' 天';
}

function renderFreshness(f) {
  const el = $('fresh-state');
  const banner = $('fresh-banner');
  if (!f || !el) return;
  const v = f.verdict;
  const age = fmtAge(f.oldest_age_min);
  const src = f.oldest_file ? ('（最旧：' + f.oldest_file + '）') : '';
  const cn = {
    // ⚠️ 离线模式的参照系是**决策基准时刻**（数据自带时刻），不是墙钟。
    //    写成"滞后 15 小时"会让人以为是"比现在旧 15 小时"，而快照本身比现在旧 33 小时
    //    —— 两个数都对，但混在一起就说不清了。所以把参照系写进文案里。
    declared_offline: '离线演示 · 最旧输入早于基准 ' + age,
    stale: '⚠️ 实时但输入已停 · 滞后 ' + age,
    ok: '新鲜 · 滞后 ' + age,
    unknown: '无法判定',
  }[v] || v;
  el.textContent = cn;
  el.title = (f.why || '') + '\n阈值 ' + (f.threshold_min || '—') + ' 分钟';
  el.className = (v === 'stale') ? 'neg' : '';
  // 输入源逐条摊开（悬停即可核对，不需要额外页面）。
  // ⚠️ 要标出**每个源用的是哪个钟**：离线模式下行情按数据自带时刻、
  //    消息面按墙钟（它天生是"本机抓取的现在"）。不标出来，
  //    "滞后 11 分钟"和"滞后 1 分钟"会被当成同一把尺子量出来的。
  const detail = (f.sources || []).map(function (s) {
    const ref = (s.clock === 'live') ? '按本机时钟' : '按决策基准时刻';
    return s.label + ' ' + s.file + ' ' + ref + ' 滞后 ' + fmtAge(s.age_min);
  }).join('　｜　');
  if (detail) el.title += '\n' + detail;

  if (!banner) return;
  if (v === 'stale') {
    banner.hidden = false;
    banner.innerHTML =
      '<b>⚠️ 数据新鲜度告警</b>　' + mdInline(f.why || '') +
      '<div class="mono-dim" style="margin-top:4px">' + esc(detail) + '</div>' +
      '<div style="margin-top:4px">旧数据比没有数据更危险：' +
      '「行情停滞」「报价冻结」这类<b>按龄判据</b>会失真，而结论看起来很正常。' +
      '请先恢复采集，再相信这一页的任何结论。</div>';
  } else if (v === 'declared_offline') {
    // 刻意**不弹**横幅：这是声明过的离线模式。但要写清它意味着什么。
    banner.hidden = false;
    banner.className = 'fresh-banner fresh-banner-quiet';
    banner.innerHTML =
      '<b>离线演示模式</b>　' + mdInline(f.why || '') +
      '<div class="mono-dim" style="margin-top:4px">' + esc(detail) + '</div>' +
      '<div style="margin-top:4px">这是<b>声明过的</b>模式，不是故障：所有判据都按' +
      '数据自带时刻算，所以结论可复跑。要接实时数据见 <code>docs/52</code>。</div>';
  } else {
    banner.hidden = true;
    banner.className = 'fresh-banner';
  }
}

async function loadHealth() {
  const h = await api(API.health);
  $('snap-ts').textContent = (h.snapshot && h.snapshot.snapshot_utc) || '—';
  $('n-bases').textContent = (h.bases || []).length + ' 个';
  renderFreshness(h.freshness);
  const llm = h.llm || {};
  $('llm-state').textContent = llm.configured
    ? ('已配置（' + esc(llm.model || '') + '）')
    : '未配置 → 事件判断退化为确定性日历';
  $('llm-state').title = llm.note || '';
  $('live-dot').className = 'dot on';
  $('disclaimer').innerHTML =
    '<b>阅读须知</b>　' + mdInline(h.disclaimer || '') +
    '<br /><span class="mono-dim">离线自足：本页只读本仓库的数据快照，' +
    '不依赖项目一、不需要网络。快照时间点 ' +
    esc((h.snapshot && h.snapshot.snapshot_utc) || '—') +
    '（' + (h.snapshot && h.snapshot.rounds) + ' 轮盘口；盘口是时点快照，' +
    '交易所无历史接口）。</span>';
  $('footer').textContent =
    '项目二 · Execution-aware Alpha ｜ 服务 ' + (h.server_utc || '—') +
    ' ｜ 版本 v' + h.version + ' ｜ 页面每次刷新都重新跑一遍决策链，不读缓存结论';
  return h;
}

/* 决策基准时间：把"这次是按几点判的、凭什么"**显式写在页面上**。
 *
 * 为什么非要写出来：离线演示读的是冻结快照。若拿墙钟去比，时间衰减规则
 * （行情停滞 >=30 分钟）会把"数据停在 07:16"误判成"市场停了 6 小时"，
 * 结论恒为"不参与"，而且同一份快照在不同时刻跑出不同结论（不可复现）。
 * 现在基准时间由数据本身推导，并把依据一并展示 —— 读者能自己核对。
 */
function renderBasis(tb) {
  if (!tb) return;
  const el = $('basis-ts');
  if (!el) return;
  const asof = tb.data_asof_ms ? fmtTime(tb.data_asof_ms) : '—';
  const kind = tb.basis === 'asof' ? '快照时刻' : '墙钟';
  el.textContent = asof + '（' + kind + '）';
  el.className = tb.basis === 'asof' ? 'basis-asof' : '';
  el.title = (tb.why || '') +
    (tb.data_age_min != null
      ? '\n数据比墙钟旧 ' + fmt(tb.data_age_min, 1) + ' 分钟' : '');
}

function fmtTime(ms) {
  try {
    return new Date(ms).toISOString().replace('T', ' ').slice(0, 19) + ' UTC';
  } catch (e) { return String(ms); }
}

/* ---------------- 决策链主视图 ---------------- */

/* 决策链步进器：后端返回的 6 段（analysts → debate → gate → trader →
   risk_officer → final）既是一条链的"总览"，也是详情区的切换器 ——
   点一段，下面就看那一段的完整面板。
   ⚠️ 下标必须与 STAGE_PANES 严格对齐，错一个就是"点了没反应"。 */
const STAGE_LABEL = {
  analysts: '① 五路独立分析',
  debate: '② 多空辩论',
  gate: '③ 事件闸门',
  trader: '④ 交易员',
  risk_officer: '⑤ 风控官',
  final: '⑥ 最终决策',
};

function renderVerdict(d) {
  const f = d.final, mo = d.monotonic || {};
  renderBasis(d.time_basis);
  const okMo = mo.stance_non_increasing !== false && mo.qty_non_increasing !== false;
  const order = f.order;
  const box = $('verdict');
  box.innerHTML =
    '<div class="verdict-bar ' + esc(f.stance) + '">' +
      '<div class="vblock"><div class="stance">' + esc(STANCE_CN[f.stance] || f.stance) + '</div>' +
      '<div class="mono-dim">最终立场 stance=' + esc(f.stance) + '</div></div>' +
      '<div class="vblock"><div class="qty">' + fmt(f.qty_usd, 0) + ' USD</div>' +
      '<div class="mono-dim">' + (order
        ? ('方式 ' + mdInline(order.mode || order.kind) + ' ｜ ' + (order.slices || 0) + ' 笔 ｜ 成本 ' + fmt(order.cost_bp) + ' bp')
        : '不下单（规模 0）') + '</div></div>' +
      '<div class="why">' + mdInline(f.why || '') + '</div>' +
      '<div class="vblock"><span class="tag ' + (okMo ? '' : 'veto') + '">' +
        (okMo ? '单调性校验通过' : '单调性校验失败') + '</span>' +
        '<span class="mono-dim">最终 ≤ 辩论；规模 ≤ 各环节最小值</span></div>' +
    '</div>' +
    /* 🔴 **所有**触发规则都要列出来。
       实测踩到：执行进度官的那条 agent 规则因为"优先级最高"而被放在 vetoes 首位，
       于是 `f.why`（只写第一条）显示成 "风控官一票否决：agent:partial_fill_naked"，
       把**真正的市场原因** `debate_stand_down` 完全盖住了 —— 读者会以为是持仓问题，
       其实是这一单本来就不该做。原因可以被排序，但不许被隐藏。 */
    (function () {
      const hits = (d.risk && d.risk.hits) || [];
      if (hits.length <= 1) return '';
      const head = (d.risk.vetoes || [])[0] || hits[0];
      return '<p class="hint">触发规则共 <b>' + hits.length + ' 条</b>（上面只写了排在最前的一条 ' +
        '<code>' + esc(head) + '</code>）：' +
        hits.map((h) => '<span class="tag ' +
          (((d.risk.vetoes || []).indexOf(h) >= 0) ? 'veto' : 'caution') + '">' +
          esc(h) + '</span>').join(' ') + '</p>';
    })() +
    (f.min_notional_binding
      ? '<p class="hint">⚠️ 可执行规模低于名义额下限（' +
        fmt(d.min_notional_usd || 100, 0) + ' USD）：正确结论是<b>不做</b>，' +
        '而不是拿小到没意义的钱去做一笔。</p>' : '');

  // 步进器（6 段链条：既是总览，也是详情切换器）
  const stages = d.stages || [];
  const last = Math.max(0, stages.length - 1);
  const sel = Math.min(Math.max(0, STATE.stage || 0), last);
  const stagesEl = $('stages');
  if (stagesEl) {
    stagesEl.innerHTML = stages.map(function (s, i) {
      const st = s.stance || (s.verdict === 'reject' ? 'stand_down' : '') || '';
      const on = (i === sel);
      const full = plain(s.detail || '');
      return '<button type="button" class="stage' + (on ? ' is-on' : '') + '"' +
        ' role="tab" data-idx="' + i + '" data-st="' + esc(st) + '"' +
        ' aria-selected="' + (on ? 'true' : 'false') + '"' +
        ' aria-controls="' + esc(STAGE_PANES[i] || '') + '"' +
        ' title="' + esc(full.slice(0, 160)) + '">' +
        '<span class="s-head"><span class="s-name">' +
          esc(STAGE_LABEL[s.stage] || s.stage || '') + '</span>' +
          '<span class="s-dot" data-st="' + esc(st) + '"></span></span>' +
        '<span class="s-stance s-' + esc(st) + '">' +
          (st ? esc(STANCE_CN[st] || st) : (s.verdict ? esc(s.verdict) : '—')) + '</span>' +
        '<span class="s-detail">' +
          (s.qty_usd !== undefined && s.qty_usd !== null ? (fmt(s.qty_usd, 0) + ' USD ｜ ') : '') +
          mdInline(full.slice(0, 88)) + '</span></button>';
    }).join('');
  }
  showStage(sel);

  // 顶栏常驻胶囊：切到别的功能页时，这一单的结论也还看得见
  const hs = $('head-status');
  if (hs) {
    hs.textContent = String(d.base || '') + ' · ' + (STANCE_CN[f.stance] || f.stance) +
      ' ｜ ' + fmt(f.qty_usd, 0) + ' USD';
    if (hs.setAttribute) hs.setAttribute('data-st', f.stance);
  }
  renderFinalPlan(d);
}

/* ⑥ 最终决策详情：把"这一单到底怎么下"汇总成一张表。
   这里**不新增任何判断** —— 数字全部来自交易员/风控官/闸门，只做汇总。 */
function renderFinalPlan(d) {
  const el = $('final-plan');
  if (!el) return;
  const f = d.final || {};
  const o = f.order;
  const t = d.trader || {};
  const mo = d.monotonic || {};
  const okMo = mo.stance_non_increasing !== false && mo.qty_non_increasing !== false;
  const thr = typeof d.edge_threshold_bp === 'number' ? d.edge_threshold_bp : null;
  const rows = [];
  const row = (k, v) => rows.push('<tr><th>' + k + '</th><td>' + v + '</td></tr>');

  row('最终立场', '<b class="s-' + esc(f.stance) + '">' +
    esc(STANCE_CN[f.stance] || f.stance) + '</b>' +
    ' <span class="mono-dim">stance=' + esc(f.stance) + '</span>');
  row('可执行规模', '<b>' + fmt(f.qty_usd, 0) + ' USD</b>' +
    ' <span class="mono-dim">请求 ' + fmt(d.qty_requested, 0) + ' USD</span>');
  row('单调性校验', okMo
    ? '<span class="pos">通过</span> <span class="mono-dim">最终 ≤ 辩论；规模 ≤ 各环节最小值</span>'
    : '<span class="neg">失败</span> <span class="mono-dim">属于硬校验失败，这一单不该执行</span>');
  if (o) {
    row('下单方式', '<b>' + mdInline(o.mode || o.kind || '—') + '</b>' +
      (t.mode ? ' <span class="mono-dim">成本模型选定 ' + mdInline(t.mode) + '</span>' : ''));
    row('拆单', esc(String(o.slices || 0)) + ' 笔 × 单笔上限 ' + fmt(o.slice_usd, 0) + ' USD');
    row('挂 / 吃价位', mdInline(t.price || o.price_desc || '—'));
    row('规模上限来自', mdInline(o.size_cap_by || '—'));
    row('往返成本', fmt(o.cost_bp) + ' bp' +
      (thr !== null ? ' <span class="mono-dim">净收益判据门槛 ' + fmt(thr) + ' bp</span>' : ''));
  } else {
    row('下单', '<span class="mono-dim">不下单（规模 0）—— 不是算不出来，而是判据或硬规则不允许</span>');
  }

  const bounds = (o && o.size_bounds) || [];
  const banned = t.barred_modes || [];
  const blocked = t.blocked_by || [];
  el.innerHTML =
    '<div class="verdict-bar ' + esc(f.stance) + '">' +
      '<div class="vblock"><div class="stance">' + esc(STANCE_CN[f.stance] || f.stance) + '</div>' +
      '<div class="mono-dim">这一单的最终结论</div></div>' +
      '<div class="vblock"><div class="qty">' + fmt(f.qty_usd, 0) + ' USD</div>' +
      '<div class="mono-dim">' + (o ? (mdInline(o.mode || '—') + ' ｜ ' + (o.slices || 0) + ' 笔') : '规模 0') +
      '</div></div>' +
      '<div class="why">' + mdInline(f.why || '') + '</div>' +
    '</div>' +
    '<div class="table-wrap"><table><tbody>' + rows.join('') + '</tbody></table></div>' +
    (bounds.length
      ? '<p class="hint">规模是被这些约束夹出来的：' +
        bounds.map((b) => mdInline(b.name) + ' → ' + fmt(b.usd, 0) + ' USD').join('；') + '</p>' : '') +
    (banned.length ? '<p class="hint neg">被硬规则禁止的方式：' + mdInline(banned.join('、')) + '</p>' : '') +
    (blocked.length ? '<p class="hint neg">未下单原因：' + mdInline(blocked[0]) + '</p>' : '') +
    (f.min_notional_binding
      ? '<p class="hint">⚠️ 可执行规模低于名义额下限（' + fmt(d.min_notional_usd || 100, 0) +
        ' USD）：正确结论是<b>不做</b>。</p>' : '') +
    '<p class="hint">这一页只做汇总，<b>不新增任何判断</b>：方式与规模来自交易员，' +
    '上限来自风控官，硬约束来自事件闸门。要看某一项的来龙去脉，点上方对应那一段。</p>';
}

function renderAnalysts(d) {
  const list = d.analysts || [];
  $('analysts').innerHTML = list.map((a) => {
    // 证据行只留"指标 = 值"。原来每行尾巴都挂一个文件路径（← data/…），
    // 十来个指标就是十来个路径，卡片被撑得很长，读者也不看。
    // ⭐ 但**来源类别**必须留在行上：`third_party` 是**别人的数据/判断**，
    //    不是我们量出来的 —— 不标出来，读者会把分析师目标价当成我们的实测。
    const KIND_TAG = { third_party: '第三方', declared: '约定值', derived: '派生' };
    const ev = (a.evidence || []).map((e) => {
      const t = KIND_TAG[e.kind];
      return '<div>· ' + mdInline(e.metric) + ' = <b>' + mdInline(e.value) + '</b>' +
        (t ? ' <span class="tag ' + (e.kind === 'third_party' ? 'agent' : '') + '">' +
             esc(t) + '</span>' : '') + '</div>';
    }).join('');
    /* 溯源没有删，只是**收起来**：默认折叠，点开才逐条列出"指标 ← 文件"。
       项目原则是"页面上每个数字都能点回数据文件"，所以不能拿掉，
       但也没必要让路径占满版面。 */
    const pairs = [];
    const seen = {};
    (a.evidence || []).forEach((e) => {
      const src = String(e.source || '').trim();
      if (!src) return;
      pairs.push('<div>' + (KIND_TAG[e.kind]
        ? '[' + esc(KIND_TAG[e.kind]) + '] ' : '') +
        mdInline(e.metric) + ' → ' + mdInline(src) + '</div>');
      seen[src] = 1;
    });
    (a.sources || []).forEach((s) => {
      const src = String(s || '').trim();
      if (src && !seen[src]) { seen[src] = 1; pairs.push('<div>' + mdInline(src) + '</div>'); }
    });
    const srcBox = pairs.length
      ? '<details class="a-src"><summary>证据来源（' + pairs.length + ' 条）</summary>' +
        '<div class="a-src-list">' + pairs.join('') + '</div></details>'
      : '';
    return '<div class="acard' + (a.valid ? '' : ' invalid') + '">' +
      '<div class="a-head"><span class="a-dim">' + esc(a.dimension) + '</span>' +
      '<span class="a-conf">置信度 ' + fmt(a.confidence, 2) + '</span></div>' +
      '<div><span class="badge ' + esc(a.verdict) + '">' + esc(a.verdict) + '</span>' +
      (a.valid ? '' : ' <span class="tag veto">已作废</span>') +
      ' <span class="mono-dim">证据 ' + (a.evidence || []).length + ' 条</span></div>' +
      (a.notes ? '<div class="a-notes">' + mdInline(a.notes) + '</div>' : '') +
      (a.invalid_reason ? '<div class="a-notes neg">作废原因：' + mdInline(a.invalid_reason) + '</div>' : '') +
      (ev ? '<div class="a-ev">' + ev + '</div>' : '') +
      srcBox +
      '</div>';
  }).join('') || '<div class="loading">无报告</div>';
}

function renderDebate(d) {
  const dv = d.debate || {};
  const v = dv.verdict || {};
  const side = (s, cls) => {
    const o = dv[s] || {};
    const args = (o.arguments || []).map((a) =>
      '<div class="arg"><div class="cl">' + mdInline(a.claim || '') + '</div>' +
      '<div class="fa"><b>证伪条件</b>：' + mdInline(a.falsifier || '（未给）') + '</div></div>').join('');
    const dropped = (o.dropped || []).length;
    return '<div class="side ' + cls + '">' +
      '<div class="sd-head"><span>' + (cls === 'bull' ? '🐂 多头立论' : '🐻 空头立论') + '</span>' +
      '<span>得分 ' + fmt(o.weight, 2) + '</span></div>' +
      (args || '<div class="arg mono-dim">无论点</div>') +
      (dropped ? '<div class="fa mono-dim">' + dropped + ' 条论点因给不出证伪条件被没收</div>' : '') +
      '</div>';
  };
  const cross = dv.cross || {};
  const cx = (x, label) => {
    if (!x) return '';
    const t = x.target_claim || x.target || '';
    const f = x.target_falsifier || x.falsifier || '';
    const r = x.rebuttal || x.text || x.response || (typeof x === 'string' ? x : '');
    return '<div class="cx"><b>' + label + '</b> 指名对方最强一条：' + mdInline(t) +
      (f ? '（其证伪条件：' + mdInline(f) + '）' : '') +
      (r ? '<br />反驳：' + mdInline(r) : '') + '</div>';
  };
  $('debate').innerHTML =
    '<div class="costbar"><span class="cb-name">多头 / 空头得分</span>' +
      '<span class="cb-track"><span class="cb-fill best" data-w="' +
        clamp(100 * (v.bull_weight || 0) / Math.max(0.01, (v.bull_weight || 0) + (v.bear_weight || 0)), 0, 100) +
        '%"></span></span>' +
      '<span class="cb-val">' + fmt(v.bull_weight, 2) + ' vs ' + fmt(v.bear_weight, 2) + '</span></div>' +
    '<p><span class="tag">裁决</span> <b class="s-' + esc(v.stance) + '">' +
      esc(STANCE_CN[v.stance] || v.stance) + '</b>　' + mdInline(v.reason || '') + '</p>' +
    (v.weighting_changed_stance
      ? '<p class="hint">⭐ 证据强度加权<strong>改变了结论</strong>：未加权时 raw ' +
        fmt(v.raw_bull_weight, 2) + ' vs ' + fmt(v.raw_bear_weight, 2) +
        '，加权后 ' + fmt(v.bull_weight, 2) + ' vs ' + fmt(v.bear_weight, 2) +
        '（measured 1.0 / verified 0.85 / derived 0.6 / inference 0.3）</p>' : '') +
    '<div class="debate-split">' + side('bull', 'bull') + side('bear', 'bear') + '</div>' +
    (cross.bull_rebuts || cross.bear_rebuts
      ? '<div class="cross"><div class="sd-head"><span>交叉质证</span>' +
        '<span class="mono-dim">必须指名对方最强一条 + 复述其证伪条件</span></div>' +
        cx(cross.bull_rebuts, '多头 → 空头') + cx(cross.bear_rebuts, '空头 → 多头') + '</div>'
      : '') +
    ((v.direction_conflicts || []).length
      ? '<p class="hint neg">方向自相矛盾扣分 ' + fmt(v.direction_penalty, 2) + '/条：' +
        mdInline(v.direction_conflicts.join('；')).slice(0, 400) + '</p>' : '');
  animateBars($('debate'));
}

function renderGate(d) {
  const g = d.gate || {};
  const gm = d.gate_merge || {};
  const sev = g.gate_severity || 'none';
  // ⚠️ "这次用没用 LLM" **不能**靠生效来源的前缀判断。
  //    实测踩到：外部确定性事件胜出时来源是 `mcp:...`，前缀不是 llm，
  //    于是页面底部写"本次**未使用** LLM 判事件"，而上面 ② 明明列着 LLM 判定 ——
  //    自相矛盾。**参与过就必须体现出来**（这条规矩本项目已经踩过一次）。
  //    正确做法：直接看 `gate_merge.llm` 这份**记录**，而不是猜来源字符串。
  const usedLlm = !!(gm.llm && gm.llm.source
                     && String(gm.llm.source).startsWith('llm'));
  const llmRow = gm.llm
    ? (mdInline(gm.llm.severity || '—') +
       ' <span class="mono-dim">' + mdInline(String(gm.llm.reason || '').slice(0, 60)) +
       (gm.llm.frozen ? ' ｜ 冻结复用' : '') +
       (gm.llm.source ? ' ｜ ' + mdInline(gm.llm.source) : '') + '</span>')
    : '<span class="mono-dim">未参与（无 key / 明确用 static）</span>';
  const extRow = gm.ext
    ? (mdInline(gm.ext.severity || '—') +
       ' <span class="mono-dim">' + mdInline(String(gm.ext.reason || '').slice(0, 90)) + '</span>')
    : ('<span class="mono-dim">窗口内无事件</span>'
       + (gm.ext_note ? ' <span class="mono-dim">｜' + mdInline(String(gm.ext_note).slice(0, 70)) + '</span>' : ''));
  $('gate').innerHTML =
    '<div class="verdict-bar ' + (sev === 'block' ? 'stand_down' : (sev === 'caution' ? 'caution' : 'proceed')) + '">' +
      '<div><div class="stance">' + esc(sev) + '</div>' +
      '<div class="mono-dim">severity（生效值）</div></div>' +
      '<div class="why">' + mdInline(g.gate_reason || '') + '</div></div>' +
    // ⭐ 合并留痕：硬闸门 = 确定性日历 **与** LLM 判定 **与** 外部确定性事件，
    //    三者取更保守的一侧。
    //    第一段是 2026-09-19 修的"说了没做"（LLM 判出的 block 只到辩论层）；
    //    第三段是 2026-09-20 新增的（财报日历 / 除息 —— 实测接入当天就抓到
    //    META 除息日 = 当天，此前链路一无所知）。
    '<table><tbody>' +
      '<tr><th>① 确定性日历</th><td>' + mdInline((gm.static || {}).severity || '—') +
        ' <span class="mono-dim">' + mdInline(String((gm.static || {}).reason || '').slice(0, 60)) + '</span></td></tr>' +
      '<tr><th>② LLM 判定</th><td>' + llmRow + '</td></tr>' +
      '<tr><th>③ 外部确定性事件<br><span class="mono-dim">财报 / 除息</span></th><td>' + extRow +
        '<div class="mono-dim" style="margin-top:3px">来自官方 bitget-mcp-server（' +
        '<b>第三方数据，不是本项目的实测量</b>）；拿不到事件表时<b>不当作「没有事件」</b></div></td></tr>' +
      '<tr><th><b>生效（取更严的一侧）</b></th><td><b>' +
        mdInline((gm.effective || {}).severity || sev) + '</b>' +
        ' <span class="mono-dim">来源 ' + mdInline(g.gate_source || '') + '</span></td></tr>' +
      '<tr><th>是否允许挂单</th><td>' + (g.maker_allowed ? '允许' : '<b class="neg">禁止（硬规则）</b>') + '</td></tr>' +
      '<tr><th>硬约束</th><td>severity=block → <b>挂单类方案直接作废</b>；agent 不能推翻</td></tr>' +
      (d.prompt && d.prompt.version
        ? '<tr><th>prompt 版本</th><td class="mono">' + esc(d.prompt.version) +
          (d.prompt.sha256 ? ' ｜ sha256 ' + esc(String(d.prompt.sha256).slice(0, 16)) : '') +
          (d.prompt.path ? ' ｜ ' + esc(d.prompt.path) : '') + '</td></tr>' : '') +
    '</tbody></table>' +
    (usedLlm
      ? '<p class="hint">✅ 本次<b>用了 LLM</b> 判事件（大模型在运行期的唯一职责），' +
        '且它的判定<b>已经进入硬闸门</b> —— 不再只停在辩论层。</p>'
      : '<p class="hint">⚠️ <b>本次未使用 LLM</b>：事件判断退化为确定性日历，' +
        '只挡得住可计算事件（期权到期/休市），<b>挡不住突发新闻与财报</b>。' +
        '这是如实标注，不是"没跑过却假装跑过"。</p>');
}

function renderTrader(d) {
  const t = d.trader || {};
  const modes = t.all_modes_bp || {};
  const best = t.mode;
  // 门槛来自后端（agent_team.EDGE_THRESHOLD_BP），前端不硬编码第二个真相
  const thr = typeof d.edge_threshold_bp === 'number' ? d.edge_threshold_bp : 11.34;
  const vals = Object.values(modes).filter((x) => typeof x === 'number');
  const maxAbs = Math.max(1, ...vals.map((x) => Math.abs(x)), thr);
  const bars = Object.keys(modes).map((k) =>
    '<div class="costbar' + (k === best ? ' best' : '') + '">' +
      '<span class="cb-name">' + mdInline(k) + (k === best ? ' ✓' : '') + '</span>' +
      '<span class="cb-track"><span class="cb-fill" data-w="' +
        clamp(100 * Math.abs(modes[k]) / maxAbs, 2, 100) + '%"></span></span>' +
      '<span class="cb-val ' + (modes[k] > thr ? 'neg' : 'pos') + '">' + fmt(modes[k]) + ' bp</span></div>').join('');
  const o = t.order;
  $('trader').innerHTML =
    (bars || '<div class="loading">无成本方案</div>') +
    '<div class="thr-line"><span>净收益判据门槛 ' + thr + ' bp</span></div>' +
    '<table><tbody>' +
      '<tr><th>选定方式</th><td><b>' + mdInline(t.mode || '—') + '</b></td>' +
          '<th>成本</th><td>' + fmt(t.cost_bp) + ' bp</td></tr>' +
      '<tr><th>规模</th><td>' + (o ? fmt(o.qty_usd, 0) + ' USD' : '—') + '</td>' +
          '<th>拆单</th><td>' + (o ? (o.slices + ' 笔 × 上限 ' + fmt(o.slice_usd, 0)) : '—') + '</td></tr>' +
      '<tr><th>价位</th><td colspan="3">' + mdInline(t.price || (o && o.price_desc) || '—') + '</td></tr>' +
      '<tr><th>规模上限来自</th><td colspan="3">' + mdInline((o && o.size_cap_by) || '—') + '</td></tr>' +
      '<tr><th>毛边际 / 距门槛</th><td colspan="3">' +
        fmt(t.gross_edge_bp) + ' bp ／ <b class="' + ((t.edge_gap_bp || 0) < 0 ? 'neg' : 'pos') + '">' +
        fmt(t.edge_gap_bp) + ' bp</b></td></tr>' +
      ((o && o.size_bounds) ? o.size_bounds.map((b) =>
        '<tr><th>约束</th><td colspan="3">' + mdInline(b.name) + ' → ' + fmt(b.usd, 0) + ' USD</td></tr>').join('') : '') +
      ((t.barred_modes || []).length ? '<tr><th>被禁方式</th><td colspan="3" class="neg">' +
        mdInline(t.barred_modes.join('、')) + '</td></tr>' : '') +
    '</tbody></table>' +
    ((t.blocked_by || []).length
      ? '<p class="hint neg">未下单原因：' + mdInline(t.blocked_by[0]) + '</p>'
      : '') +
    '<p class="hint">交易员<b>不新造阈值、不做价格预测</b>：价位＝中价 ± 实测半幅点差；' +
    '规模＝min(请求, 可捕获名义额(实测), 首档深度×25%)。</p>';
  animateBars($('trader'));
}

function renderRisk(d) {
  const r = d.risk || {};
  const rules = r.rules || [];
  const rows = rules.map((x) =>
    '<tr class="' + (x.triggered ? 'hit' : '') + (x.agent ? ' agent-rule' : '') + '">' +
      '<td>' + esc(x.id) + (x.agent ? ' <span class="tag agent">agent 提出</span>' : '') + '</td>' +
      '<td><span class="tag ' + (x.level === 'veto' ? 'veto' : 'caution') + '">' + esc(x.level) + '</span></td>' +
      '<td>' + mdInline(x.action) + '</td>' +
      '<td class="sep">' + mdInline(x.statement || '') + '</td>' +
      '<td class="sep mono-dim">' + mdInline(x.falsifier || '') + '</td>' +
      '<td>' + (x.triggered ? '<b class="neg">触发</b>' : '<span class="mono-dim">未触发</span>') + '</td>' +
    '</tr>').join('');
  const hyps = (d.risk_hypotheses || []).map((h) =>
    '<div class="arg"><div class="cl">' + esc(h.id) + '：' + mdInline(h.hypothesis || '') + '</div>' +
    '<div class="fa">实测量 <b>' + mdInline(h.metric) + ' = ' + mdInline(h.value) + '</b> ｜ 阈值 ' +
      mdInline(h.threshold) + '</div>' +
    '<div class="fa"><b>证伪条件</b>：' + mdInline(h.falsifier || '') + ' ｜ 动作 ' + mdInline(h.action) + '</div></div>').join('');
  $('risk').innerHTML =
    '<div class="verdict-bar ' + (r.verdict === 'reject' ? 'stand_down' : (r.verdict === 'caution' ? 'caution' : 'proceed')) + '">' +
      '<div><div class="stance">' + esc(r.verdict) + '</div>' +
      '<div class="mono-dim">' + (r.checked_rules || 0) + ' 条规则</div></div>' +
      '<div class="why">' + mdInline(r.reason || '') + '</div></div>' +
    '<p>' + ((r.hits || []).length
      ? '<span class="tag veto">触发</span> ' + esc(r.hits.join('、'))
      : '<span class="tag">无规则触发</span>') +
      (r.qty_out_usd !== null && r.qty_out_usd !== undefined
        ? '　<span class="mono-dim">风控规模上限 ' + fmt(r.qty_out_usd, 0) + ' USD</span>' : '') + '</p>' +
    (hyps ? '<div class="side"><div class="sd-head"><span>🛡️ agent 提出的风险假设</span>' +
      '<span class="mono-dim">每条必须有 实测量+阈值+证伪条件</span></div>' + hyps + '</div>' : '') +
    '<div class="table-wrap" style="max-height:320px;overflow:auto"><table>' +
      '<thead><tr><th>规则</th><th>级别</th><th>动作</th><th class="sep">引用什么</th>' +
      '<th class="sep">什么条件下撤销</th><th>本次</th></tr></thead><tbody>' + rows + '</tbody></table></div>' +
    '<p class="hint">风控官<b>只收紧，不放松</b>；每条规则都留痕（含未触发的）：' +
    '引用什么 · 触发做什么 · <b>什么条件下撤销</b>。</p>';
}

/* ⏱️ ⑥ 执行进度官 —— 下单**之后**这一段。
   三条硬规矩（与后端一致，前端不得美化）：
     1. 没有在途订单 -> 明确显示"没有在途订单"，**不显示绿灯**
        （没有数据 ≠ 没有风险，这是本项目最容易犯的错）；
     2. 合成演示单必须**显眼标注**，不能被截图当成真实下单记录；
     3. 处置口径里的数字只能来自成本模型；算不出来的直说算不出来。 */
function renderExecProg(d) {
  const e = d.execution_progress || {};
  const src = d.position_source ? '<p class="hint">持仓来源：' + mdInline(d.position_source) + '</p>' : '';
  if (!e.present) {
    $('execprog').innerHTML =
      '<div class="verdict-bar"><div><div class="stance mono-dim">无在途订单</div>' +
      '<div class="mono-dim">执行进度官未运行</div></div>' +
      '<div class="why">没有在途持仓单时不产出任何执行中结论 —— ' +
      '<b>没有数据 ≠ 没有风险</b>，所以这里不显示绿灯。' +
      '要演示请把上面的「在途持仓」切到合成演示单，或用 ' +
      '<code>data/positions/open.json</code> 提供真实在途订单。</div></div>' + src;
    return;
  }
  if (e.error) {
    $('execprog').innerHTML =
      '<div class="verdict-bar stand_down"><div><div class="stance">执行中判断失败</div></div>' +
      '<div class="why">' + esc(e.error) + '<br>失败<b>不静默</b>：如实记录，但不会让整条链挂掉。</div></div>' + src;
    return;
  }
  const o = e.order || {};
  const syn = e.synthetic
    ? '<div class="verdict-bar caution"><div><div class="stance">合成演示持仓</div>' +
      '<div class="mono-dim">synthetic = true</div></div>' +
      '<div class="why">这不是真实下单记录，仅用于展示判据。它带着 <code>synthetic</code> ' +
      '标记一路进日志、接口与页面，<b>不得</b>被当成实测结论引用。</div></div>'
    : '';
  const ev = (e.evidence || []).map((x) =>
    '<tr><td>' + mdInline(x.metric) + '</td><td class="mono-dim">' + mdInline(x.value) + '</td>' +
    '<td class="sep mono-dim">' + mdInline(x.source) + '</td></tr>').join('');
  /* 假设已并入风控；这里同时显示"触发了几条 / 有哪几条没过阈值"，
     后者是**留痕不删**：未触发也要能看见，否则读者无法判断判据有没有在工作。 */
  const hy = (e.hypotheses || []).map((h) =>
    '<div class="arg"><div class="cl">' + esc(h.id) + '：' + mdInline(h.hypothesis || '') + '</div>' +
    '<div class="fa">实测量 <b>' + mdInline(h.metric) + ' = ' + mdInline(h.value) + '</b> ｜ 阈值 ' + mdInline(h.threshold) + '</div>' +
    '<div class="fa"><b>证伪条件</b>：' + mdInline(h.falsifier || '') + ' ｜ 动作 ' + mdInline(h.action) + '</div></div>').join('');
  const dr = (e.dropped || []).map((x) =>
    '<div class="arg"><div class="cl mono-dim">' + esc(x.id) + '：' + mdInline(x.reason) + '</div>' +
    '<div class="fa mono-dim">' + mdInline(x.metric || '') + ' = ' + mdInline(x.value || '') + '</div></div>').join('');
  const ac = (e.actions || []).map((a) =>
    '<tr><td>' + mdInline(a.title) + '</td>' +
    '<td>' + (a.cost_bp === null || a.cost_bp === undefined
      ? '<span class="mono-dim">—</span>' : '<b>' + fmt(a.cost_bp) + ' bp</b>') + '</td>' +
    '<td class="sep mono-dim">' + mdInline(a.cost_note || '') + '</td></tr>').join('');
  const filled = (o.spot_filled ? '现货=已成交' : '现货=未成交') + ' ｜ ' +
                 (o.perp_filled ? '永续=已成交' : '永续=未成交');
  $('execprog').innerHTML = syn +
    '<div class="verdict-bar ' + (e.verdict === 'unfavorable' ? 'stand_down' : 'caution') + '">' +
      '<div><div class="stance">' + esc(e.verdict || '—') + '</div>' +
      '<div class="mono-dim">' + (e.hypotheses || []).length + ' 条执行中假设</div></div>' +
      '<div class="why">' + mdInline(e.notes || '') + '</div></div>' +
    '<table><tbody>' +
      '<tr><th>在途订单</th><td>' + esc(o.id || '—') + '　' + fmt(o.qty_usd, 0) + ' USD</td>' +
          '<th>两腿成交</th><td>' + esc(filled) + '</td></tr>' +
    '</tbody></table>' +
    '<div class="table-wrap"><table><thead><tr><th>实测量</th><th>值</th><th class="sep">来源</th></tr></thead>' +
    '<tbody>' + ev + '</tbody></table></div>' +
    (hy ? '<div class="side"><div class="sd-head"><span>⏱️ 执行中假设（已并入风控官）</span>' +
      '<span class="mono-dim">触发即按 agent 规则收紧，最多不允许放松</span></div>' + hy + '</div>' : '') +
    (ac ? '<div class="side"><div class="sd-head"><span>确定性处置口径</span>' +
      '<span class="mono-dim">agent 说"该处置了"，数字由成本模型给</span></div>' +
      '<table><thead><tr><th>动作</th><th>成本</th><th class="sep">数字从哪来</th></tr></thead><tbody>' +
      ac + '</tbody></table></div>' : '') +
    (dr ? '<div class="side"><div class="sd-head"><span>未过阈值（留痕不删）</span>' +
      '<span class="mono-dim">判据有没有在工作，看这里</span></div>' + dr + '</div>' : '') +
    '<p class="hint">执行进度官<b>不产出任何数字</b>（不给新价位、不给新规模）—— ' +
    '那是确定性交易员的职责。它只给"该复核什么"的触发条件，且每条都能证伪。</p>' +
    src;
}

/* 💵 入场损益测算 —— 「我投这么多，能赚多少 / 亏多少 / 赔率如何」
 *
 * 三条硬规矩（与后端 entry_math 一致，前端**不得美化**）：
 *   1. **算不出来的必须显示出来**。"基差变动 (B_e − B_x)" 是这类交易真正的
 *      盈亏来源，但它是未来价格 —— 后端不给数字，前端也**不许暗示**有数字。
 *   2. 顺利情形为负时，盈亏比是"没有意义"而不是"0"或"很差"。
 *   3. 每个数字都要能点回来源（evidence 里有 metric/value/source）。
 * 金额直接取页面上的「名义额 USD」—— 用户填多少就算多少。 */
function renderEntry(d) {
  const e = (d && d.entry) || {};
  const box = $('entry');
  if (!box) return;
  if (!e.ok) {
    box.innerHTML = '<div class="verdict-bar"><div><div class="stance mono-dim">暂不可用</div>' +
      '<div class="mono-dim">入场测算</div></div><div class="why">' +
      mdInline(e.why || '（后端没有返回 entry）') + '</div></div>';
    return;
  }
  const usd = (v) => (v === null || v === undefined) ? '—'
    : ((v >= 0 ? '+' : '') + fmt(v, 2) + ' USD');
  const bp = (v) => (v === null || v === undefined) ? '—'
    : ((v >= 0 ? '+' : '') + fmt(v, 3) + ' bp');
  const cls = (v) => (v === null || v === undefined) ? ''
    : (v > 0 ? 'pos' : (v < 0 ? 'neg' : ''));

  const f = e.friction || {};
  const modes = (e.modes || []).map((m) => {
    const src = (m.fee_bp === null || m.fee_bp === undefined)
      ? '<span class="mono-dim">（此行为成本模型口径，与上两行不同源）</span>' : '';
    return '<tr' + (m.mode === e.mode ? ' class="hit"' : '') + '>' +
      '<td>' + esc(m.mode) + (m.mode === e.mode ? ' <span class="tag">选定</span>' : '') +
        (src ? '<br>' + src : '') + '</td>' +
      '<td class="mono-dim">' + bp(m.fee_bp) + '</td>' +
      '<td class="' + cls(m.net_bp) + '"><b>' + bp(m.net_bp) + '</b></td>' +
      '<td class="' + cls(m.net_usd) + '">' + usd(m.net_usd) + '</td></tr>';
  }).join('');

  const scen = (e.scenarios || []).map((s) =>
    '<tr><td>' + esc(s.name) + '</td>' +
    '<td class="mono-dim">' + (s.prob === null || s.prob === undefined ? '—'
      : pct(s.prob, 1)) + '</td>' +
    '<td class="' + cls(s.net_bp) + '"><b>' + bp(s.net_bp) + '</b></td>' +
    '<td class="' + cls(s.net_usd) + '">' + usd(s.net_usd) + '</td></tr>').join('');

  const ev = (e.evidence || []).map((x) =>
    '<tr><td>' + mdInline(x.metric) + '</td><td class="mono-dim">' + esc(x.value) + '</td>' +
    '<td class="sep mono-dim">' + esc(x.source) + '</td></tr>').join('');

  const notInc = (e.not_included || []).map((x) =>
    '<li>' + mdInline(x) + '</li>').join('');
  const notes = (e.notes || []).map((x) => '<li class="neg">' + mdInline(x) + '</li>').join('');

  const verdictCls = (e.verdict === 'positive') ? 'proceed'
    : (e.verdict === 'negative_expectation' ? 'caution' : 'stand_down');
  const xc = e.cross_check;
  box.innerHTML =
    '<div class="verdict-bar ' + verdictCls + '">' +
      '<div><div class="stance">' + usd((e.scenarios || [{}])[0].net_usd) + '</div>' +
      '<div class="mono-dim">投入 ' + fmt(e.qty_usd, 0) + ' USD ｜ 方式「' +
      esc(e.mode) + '」</div></div>' +
      '<div class="why">' + mdInline(e.why || '') + '</div></div>' +
    '<div class="table-wrap"><table><thead><tr>' +
      '<th>执行方式</th><th>往返费率</th><th>摩擦净额</th><th>按你的金额</th>' +
    '</tr></thead><tbody>' + modes + '</tbody></table></div>' +
    '<div class="table-wrap" style="margin-top:8px"><table><thead><tr>' +
      '<th>情形</th><th>实测概率</th><th>净额</th><th>按你的金额</th>' +
    '</tr></thead><tbody>' + scen + '</tbody></table></div>' +
    (e.expectation ? '<p class="hint">概率加权期望（用<b>实测联合成交分布</b>加权）：<b class="' +
      cls(e.expectation.bp) + '">' + bp(e.expectation.bp) + '</b> = ' +
      usd(e.expectation.usd) + '　' + mdInline(e.expectation.note || '') + '</p>' : '') +
    '<p class="hint"><b>盈亏比</b>：' +
      (e.rr_ratio === null || e.rr_ratio === undefined
        ? '<span class="mono-dim">不适用</span>' : '<b>' + fmt(e.rr_ratio, 2) + ' : 1</b>') +
      '　' + mdInline(e.rr_note || '') + '</p>' +
    '<div class="table-wrap"><table><thead><tr>' +
      '<th>实测量</th><th>值</th><th class="sep">来源</th></tr></thead><tbody>' +
      ev + '</tbody></table></div>' +
    (xc ? '<p class="hint">两套成本口径交叉核对（' +
      (xc.checks || []).map((c) => esc(c.mode) + ' 差 ' + fmt(c.diff_bp, 2) + ' bp').join('；') +
      '）：' + mdInline(xc.note || '') + '</p>' : '') +
    /* 🔴 这一块**必须**在页面上，而且不能折叠 —— 它是这个卡片诚实与否的分界线 */
    '<div class="side" style="border-color:var(--warn-line)">' +
      '<div class="sd-head"><span>⚠️ 这张表<b>算不出来</b>什么（比上面的数字更重要）</span></div>' +
      '<ul class="not-inc">' + notInc + '</ul>' +
      (notes ? '<ul class="not-inc">' + notes + '</ul>' : '') +
    '</div>';
}

function renderCost(d) {
  const c = d.cost || {};
  // 条形只写 data-w（目标宽度），由 animateBars 在下一帧设 style.width —— 这样
  // CSS 的 transition 才会真的跑起来（直接写死宽度 = 元素一出生就是终态，没有动画）。
  const bar = (name, val, good) => (typeof val === 'number'
    ? '<div class="costbar' + (good ? ' best' : '') + '">' +
      '<span class="cb-name">' + name + '</span>' +
      '<span class="cb-track"><span class="cb-fill" data-w="' +
      clamp(Math.abs(val) * 4, 2, 100) + '%"></span></span>' +
      '<span class="cb-val">' + val.toFixed(1) + ' bp</span></div>' : '');
  const el = $('cost');
  el.innerHTML =
    '<table><tbody>' +
      '<tr><th>最优方式</th><td><b>' + mdInline(c.best_mode || '—') + '</b></td>' +
          '<th>成本</th><td>' + fmt(c.best_cost) + ' bp</td></tr>' +
      '<tr><th>现货半幅点差</th><td>' + fmt(c.half_s, 3) + ' bp</td>' +
          '<th>永续半幅点差</th><td>' + fmt(c.half_p, 3) + ' bp</td></tr>' +
      '<tr><th>撮合路由 / 交易时段</th><td>' + esc(plainToken(c.route) || '—') + ' / ' +
          esc(plainToken(c.session) || '—') + '</td>' +
          '<th>腿风险 leg_risk</th><td>' + fmt(c.leg_risk) + '</td></tr>' +
    '</tbody></table>' +
    '<h2 style="margin-top:14px">双腿联合成交分布 <span class="hint">' +
      esc(c.joint_source === 'measured' ? '实测' : (c.joint_source || '—')) + '</span></h2>' +
    bar('P(两腿都成交)', c.p_both, true) +
    bar('P(只成交一腿)', c.p_part, false) +
    bar('P(都没成交)', c.p_none, false) +
    '<table style="margin-top:10px"><tbody>' +
      '<tr><th>独立假设 P(两腿)</th><td class="mono-dim">' + pct(c.p_both_indep) +
        '（旧口径近似，已被实测取代）</td></tr>' +
      '<tr><th>只用一腿独立概率</th><td class="mono-dim">现货 ' + pct(c.p_s) +
        ' ｜ 永续 ' + pct(c.p_p) + '</td></tr>' +
      '<tr><th>样本</th><td class="mono-dim">现货 ' + (c.n_s || 0) + ' 条 ｜ 永续 ' +
        (c.n_p || 0) + ' 条 ｜ 窗口 ' + (c.joint_prov ? '' : '') + '</td></tr>' +
    '</tbody></table>' +
    '<p class="hint">' + mdInline(c.joint_prov || '') + '</p>' +
    ((c.invalidated || []).length
      ? '<p class="hint neg">被硬规则作废的方式：' + mdInline(c.invalidated.join('、')) + '</p>' : '');
  animateBars(el);
}

/* ---------------- 🏦 真实账户（只读） ----------------
   数据来自 /api/account，而**后端只读本机落盘**的取数结果
   （tools/account_read.py --read 的产物）。所以页面这一侧根本没有
   "点一下就去打交易所"的路径 —— 「交易所读数 → 页面」之间只有一个可核验的中间文件。

   🔴 三态必须分开显示：empty（读到、而且确实是空的）与 unavailable（读不到）
      不是一回事。把后者画成 0 仓位，就是把「缺数据」说成「安全」——
      所以这两态的徽标颜色也不一样。 */

const ACCT_TAG = { ok: 'badge', zero: 'badge unfavorable', empty: 'badge neutral',
                   unavailable: 'badge unfavorable' };
const ACCT_CN = { ok: '已读到', zero: '读到且确认', empty: '读到且确认',
                  unavailable: '读不到' };

function acctLine(status, headline, extra) {
  const s = status || 'unavailable';
  return '<div class="hashbox"><span class="' + (ACCT_TAG[s] || 'badge unfavorable') +
    '">' + (ACCT_CN[s] || '未知') + '</span> <span>' + mdInline(headline || '') +
    '</span>' + (extra ? '<div class="mono-dim">' + extra + '</div>' : '') + '</div>';
}

function renderAccount(d) {
  const el = $('acct');
  const meta = $('acct-meta');
  const r = d || {};
  if (meta) {
    meta.innerHTML = r.available
      ? ('取数于 ' + esc(r.generated_utc || '（时刻未记录）') + ' ｜ ' + esc(r.file || '') +
         ' ｜ 本服务只读本地文件，不打交易所')
      : '没有取数结果';
  }
  if (!r.available) {
    el.innerHTML =
      '<p class="hint neg">' + mdInline('还没有取数结果：' + (r.why || '')) + '</p>' +
      '<p class="hint">在本机跑一次只读取数：<code>' + esc(r.next || '') + '</code></p>' +
      '<p class="hint">账户数据是<b>运行时产物</b>，刻意不进仓库 —— 刚克隆下来的人会看到这一段。</p>';
    return;
  }
  const bal = r.balance || {};
  const pos = r.positions || {};
  const fee = r.fee || {};
  const vd = r.verdict || {};
  const cred = r.cred || {};
  // ⚠️ 服务端给的是 `bg_ff****************7fe4` 这种掩码 —— 那一串星号在页面上
  //    会被"字面标记检查"数成 markdown 残留（实测：13 处，全来自它）。
  //    页面本来也不需要整串掩码，压成 `bg_ff…7fe4` 这种更好读的形式。
  const keyMask = String(cred.api_key_masked || '—').replace(/\*+/g, '…');
  const bySym = fee.by_symbol || {};
  const symKeys = Object.keys(bySym);
  const feeRefs = Object.keys(fee.refs || {}).map((c) => {
    const v = fee.refs[c] || [];
    return esc(c) + ' ' + fmt(v[0]) + '/' + fmt(v[1]) + ' bp';
  }).join(' ｜ ');
  const symRows = symKeys.map((k) => {
    const v = bySym[k] || {};
    return '<tr><td class="mono-dim">' + esc(k) + '</td>' +
      '<td class="mono">' + fmt(v.maker_bp) + '</td>' +
      '<td class="mono">' + fmt(v.taker_bp) + '</td></tr>';
  }).join('');
  const posRows = (pos.rows || []).map((row) =>
    '<p class="mono-dim">' + esc(JSON.stringify(row)) + '</p>').join('');
  const coins = (bal.coins || []).map((c) =>
    esc(String(c[0])) + ' ' + esc(String(c[1]))).join(' ｜ ');
  const canTxt = vd.can_trade === true ? '<span class="pos">可以做单前检查</span>'
    : (vd.can_trade === false ? '<span class="neg">不能下单</span>'
      : '<span class="neg">未知（有读不到的项）</span>');

  el.innerHTML =
    '<table><tbody>' +
      '<tr><th>授权</th><td>' + (r.authorized ? '<span class="pos">已授权</span>'
        : '<span class="neg">未授权</span>') + '</td>' +
      '<th>凭据</th><td class="mono-dim">' + esc(keyMask) +
        '（服务端已掩码，不是明文）</td></tr>' +
      '<tr><th>账户设置</th><td class="mono-dim">' + esc(String(r.settings || '—')) +
        '</td><th>资金账户</th><td class="mono-dim">' +
        (r.funding_assets === null || r.funding_assets === undefined ? '—'
          : (r.funding_assets.length ? String(r.funding_assets.length) + ' 项' : '空')) +
        '</td></tr>' +
    '</tbody></table>' +

    '<h3 class="sub-title">余额</h3>' +
    acctLine(bal.status, bal.headline,
             '权益 ' + fmt(bal.account_equity) + ' ｜ USDT ' + fmt(bal.usdt_equity) +
             (coins ? ' ｜ ' + coins : '')) +

    '<h3 class="sub-title">持仓</h3>' +
    acctLine(pos.status, pos.headline,
             (pos.count === undefined || pos.count === null ? '' : '条数 ' + pos.count)) +
    posRows +

    '<h3 class="sub-title">手续费率（与项目口径逐项对照）</h3>' +
    '<p class="hint">项目假设：' + (feeRefs || '—') + '</p>' +
    ((fee.mismatch || []).length
      ? '<p class="hint neg">' + mdInline('与假设不一致：' + fee.mismatch.join('；')) + '</p>'
      : '') +
    '<details class="legend"><summary>逐 symbol 手续费率（' + symKeys.length +
      ' 项 —— 费率是<b>逐 symbol</b> 的，所以要按自己的标的查）</summary>' +
      '<div class="table-wrap"><table><thead><tr><th>symbol</th><th>maker bp</th>' +
      '<th>taker bp</th></tr></thead><tbody>' +
      (symRows || '<tr><td colspan="3" class="empty">—</td></tr>') +
      '</tbody></table></div></details>' +

    '<h3 class="sub-title">交易前置检查结论</h3>' +
    '<div class="hashbox"><div>' + canTxt + '：' + mdInline(vd.why || '') + '</div></div>' +
    (r.warnings || []).map((w) => '<p class="hint neg">' + mdInline(w) + '</p>').join('') +
    (r.notes || []).map((n) => '<p class="hint">' + mdInline(n) + '</p>').join('');
}

function renderProv(d) {
  const man = d.input_manifest || [];
  $('prov').innerHTML =
    '<div class="hashbox"><div class="mono-dim">decision_hash（本次全部契约字段的哈希）</div>' +
      '<div class="mono" style="color:var(--green);word-break:break-all">' + esc(d.decision_hash || '—') + '</div>' +
      '<div class="mono-dim" style="margin-top:6px">log_format ' + esc(d.log_format || '') +
      ' ｜ 生成于 ' + esc(d.generated_utc || '') + '</div></div>' +
    '<p class="prov" style="margin-top:10px">输入清单（存在性 + SHA256(16)）：</p>' +
    '<div class="table-wrap" style="max-height:220px;overflow:auto"><table><tbody>' +
      man.map((m) => '<tr><td class="mono">' + esc(m.path) + '</td>' +
        '<td>' + (m.exists ? '<span class="pos">存在</span>' : '<span class="neg">缺失</span>') + '</td>' +
        '<td class="mono-dim">' + esc((m.sha256_16 || '').slice(0, 16)) + '</td></tr>').join('') +
    '</tbody></table></div>' +
    '<p class="hint">复跑（同一份数据快照下，契约字段应逐字节一致）：' +
    '<code>python project2/agent_team.py --base ' + esc(d.base) + ' --trader --log</code>；' +
    '再用 <code>--replay data/reports/&lt;本次日志&gt;.json</code> 比对。</p>';
}

async function runDecision(base, qty) {
  if (STATE.busy) return;
  STATE.busy = true;
  $('run').disabled = true;
  /* 默认 auto：读真实的 data/positions/open.json，没有就不带在途订单。
     ⚠️ 这里曾经默认 `demo`（合成演示单），后果很严重：**每个标的每跑一次**
        都会带上"现货已成交、永续未成交"，执行进度官判裸露敞口 -> 一票否决 ->
        页面上永远显示"不参与"，而且理由栏写的是 `agent:partial_fill_naked`，
        把真实的市场原因盖掉了。演示数据绝不能是默认值。 */
  const pm = ($('pos-mode') && $('pos-mode').value) || 'auto';
  $('run-state').textContent = '正在跑决策链（读快照 + 5 路分析 + 辩论 + 闸门 + 交易员 + 风控官 + 执行进度）…';
  if ($('pos-state')) $('pos-state').textContent = '在途持仓模式：' + pm;
  try {
    const d = (await api(API.decision + '?base=' + encodeURIComponent(base) +
      '&qty=' + qty + '&position=' + encodeURIComponent(pm))).decision;
    renderVerdict(d); renderAnalysts(d); renderDebate(d); renderGate(d);
    renderTrader(d); renderRisk(d); renderEntry(d); renderExecProg(d); renderCost(d); renderProv(d);
    $('run-state').textContent = '完成 ｜ ' + d.base + ' ｜ ' + d.generated_utc;
    if ($('pos-state')) {
      const e = d.execution_progress || {};
      $('pos-state').textContent = e.present
        ? ('在途持仓：' + ((e.hypotheses || []).length ? '触发 ' + e.hypotheses.length + ' 条执行中假设'
                                                       : '未触发假设') + (e.synthetic ? '（合成演示单）' : ''))
        : '无在途持仓单';
    }
  } catch (e) {
    $('run-state').innerHTML = '<span class="neg">失败：' + esc(e.message) + '</span>';
    $('live-dot').className = 'dot err';
  } finally {
    STATE.busy = false;
    $('run').disabled = false;
  }
}

/* ---------------- 全标的概览 ---------------- */

/* 记住上一次每行的渲染结果：刷新后哪一行**变了**就闪一下。
   数据密集页面里"哪一格动了"比"整页刷新了"有用得多。 */
const LAST_ROWS = {};

function flashChangedRows(tb) {
  if (!tb.querySelectorAll) return;
  tb.querySelectorAll('tr[data-base]').forEach(function (tr) {
    const b = tr.getAttribute('data-base');
    const now = tr.innerHTML;
    if (LAST_ROWS[b] !== undefined && LAST_ROWS[b] !== now && !REDUCE_MOTION
        && tr.classList) {
      tr.classList.add('flash-up');
      setTimeout(function () { tr.classList.remove('flash-up'); }, 950);
    }
    LAST_ROWS[b] = now;
  });
}

/* ---------------- 全标的概览 ----------------
   ⭐ v3.2（按用户第二次反馈重做）：
   用户要的不是"选列"，而是**选币种**——
   「这里显示的是可选择的币种，我可以手动多选我想横向对比的币种，
     点击显示常驻与不显示；那些（列）都需要显示」。

   所以：
     · **列全部显示**（6 列，不再有列开关）；
     · 一排 **标的多选 chips**（10 个标的，点一下加入/移出对比），
       选择**常驻**（localStorage），并写明"显示 N / 10 个标的"；
     · 「行密度」保留：紧凑 = 行内只放差异化数值 + 通用口径进表下图例；
       展开全部 = 每行摊开完整原文。
   为什么行内还要压缩：10 行 ×「同一段解释」重复 10 遍正是最初被抱怨的冗余；
   把不变量提到图例只写一次，变量留在行里 —— 这跟"列都要显示"不冲突。 */

/* 列定义：**全部显示**（不再有 on/off）。 */
const OV_COLS = [
  { key: 'base', sep: false, th: '标的' },
  { key: 'risk', sep: false, th: '风险' },
  { key: 'verdict', sep: false, th: '结论' },
  { key: 'event', sep: true, th: '事件严重度' },
  { key: 'reason', sep: true, th: '可核验理由 / 警告' },
  { key: 'cond', sep: true, th: '条件（数值）' },
];

const OV_STATE = { bases: null, density: 'compact', open: {} };

function ovLoad(items) {
  const all = (items || []).map((it) => it.base);
  try {
    const raw = window.localStorage && window.localStorage.getItem('p2.ovbases');
    if (raw) {
      const saved = JSON.parse(raw);
      if (Array.isArray(saved) && saved.length) {
        // 与当前接口返回的标的取交集：接口加了新标的时它默认**是选中的**，
        // 否则"新标的不出现"会让人以为接口坏了。
        const keep = saved.filter((b) => all.indexOf(b) >= 0);
        OV_STATE.bases = keep.length ? keep : null;
      }
    }
    const d = window.localStorage && window.localStorage.getItem('p2.ovdensity');
    if (d === 'full' || d === 'compact') OV_STATE.density = d;
  } catch (e) { /* 隐私模式 / 无 localStorage：用默认值，不报错 */ }
  if (!OV_STATE.bases) OV_STATE.bases = all.slice();   // 默认全选
}

function ovSave() {
  try {
    if (window.localStorage) {
      window.localStorage.setItem('p2.ovbases', JSON.stringify(OV_STATE.bases || []));
      window.localStorage.setItem('p2.ovdensity', OV_STATE.density);
    }
  } catch (e) { /* 存不下就算了，不影响使用 */ }
}

/* **纯函数**：按选中的标的过滤。抽出来是为了能被测试直接喂合成输入 ——
   `OV_STATE` 是 const，在最小 DOM 的沙箱里拿不到（不是全局对象属性），
   所以"能不能过滤"必须能脱离它单独验证。 */
function ovFilter(items, sel) {
  const list = sel || [];
  if (!list.length) return [];
  return (items || []).filter((it) => list.indexOf(it.base) >= 0);
}

function ovSelected(items) { return ovFilter(items, OV_STATE.bases); }

/* 标的多选 chips（常驻、状态持久化） */
function renderOvControls(items) {
  const box = $('ov-bases');
  if (!box) return;
  const sel = OV_STATE.bases || [];
  const allSel = sel.length === (items || []).length;
  box.innerHTML = (items || []).map((it) =>
    '<button type="button" class="chip' + (sel.indexOf(it.base) >= 0 ? ' active' : '') +
    '" data-base="' + esc(it.base) + '" aria-pressed="' +
    (sel.indexOf(it.base) >= 0 ? 'true' : 'false') + '">' + esc(it.base) + '</button>').join('') +
    '<button type="button" class="chip chip-quiet" data-all="1">' +
    (allSel ? '全不选' : '全选') + '</button>';
  box.querySelectorAll('.chip').forEach((el) => {
    el.onclick = () => {
      const all = (items || []).map((x) => x.base);
      if (el.dataset.all) {
        OV_STATE.bases = allSel ? [] : all.slice();
      } else {
        const b = el.dataset.base;
        const cur = (OV_STATE.bases || []).slice();
        const i = cur.indexOf(b);
        if (i >= 0) cur.splice(i, 1); else cur.push(b);
        OV_STATE.bases = cur;
      }
      ovSave();
      renderOverview(STATE.ovItems || []);
    };
  });
  const db = $('ov-density');
  if (db) {
    db.querySelectorAll('.chip').forEach((el) => {
      el.classList.toggle('active', el.dataset.density === OV_STATE.density);
      el.onclick = () => {
        OV_STATE.density = el.dataset.density;
        OV_STATE.open = {};           // 切密度时复位展开状态，避免"切了没反应"的错觉
        ovSave();
        renderOverview(STATE.ovItems || []);
      };
    });
  }
  const cnt = $('ov-count');
  if (cnt) {
    cnt.textContent = '显示 ' + sel.length + ' / ' + (items || []).length + ' 个标的' +
      (sel.length ? '' : '　（一个都没选，表格会是空的）');
  }
}

/* 图例：把"所有标的共用"的口径只写一次 */
function renderOvLegend(items) {
  const body = $('ov-legend-body');
  if (!body) return;
  const seen = {}, order = [];
  (items || []).forEach((it) => {
    Object.keys(it.conditions || {}).forEach((k) => {
      if (!seen[k]) { seen[k] = it.conditions[k]; order.push(k); }
    });
  });
  body.innerHTML = order.length
    ? order.map((k) => '<div class="lg-row"><div class="lg-name">' +
        esc(COND_LABEL[k] || k) + '</div><div>' +
        (COND_EXPLAIN[k] ? mdInline(COND_EXPLAIN[k])
                         : '（口径见 data/SNAPSHOT.md 与 docs/DATA_DICT.md）') +
        '</div></div>').join('') +
      '<p class="hint">上表「条件」列只放<b>每个标的不同的数值</b>；' +
      '同一段口径不逐行重复。</p>'
    : '<div class="loading">接口没有返回 conditions</div>';
}

function renderOverview(items) {
  const tb = document.querySelector('#ov-table tbody');
  const head = $('ov-head');
  if (!items || !items.length) {
    tb.innerHTML = '<tr><td colspan="6" class="empty">无数据</td></tr>';
    return;
  }
  STATE.ovItems = items;
  ovLoad(items);
  renderOvControls(items);
  renderOvLegend(items);
  // 表头只画一次（列是固定的 6 列，不再随开关变）
  if (head && !head.innerHTML) {
    head.innerHTML = OV_COLS.map((c) =>
      '<th' + (c.sep ? ' class="sep"' : '') + '>' + esc(c.th) + '</th>').join('');
  }

  const shown = ovSelected(items);
  if (!shown.length) {
    tb.innerHTML = '<tr><td colspan="6" class="empty">' +
      '一个标的都没选 —— 点上面的标的 chip 加入横向对比（选择会记住）</td></tr>';
    flashChangedRows(tb);
    return;
  }

  tb.innerHTML = shown.map((it) => {
    const b = it.base;
    const rm = RISK_CN[it.risk_level] || ['?', ''];
    const sev = ((it.event || {}).severity) || '—';
    const open = !!OV_STATE.open[b] || OV_STATE.density === 'full';
    const reasons = it.rationale || [];
    const warns = it.warnings || [];
    const condKeys = Object.keys(it.conditions || {});

    /* 紧凑：理由只留第一条 + "还有 N 条"；条件压成一行数值。
       open（点行或"展开全部"）：全都摊开。 */
    let reasonHtml = '';
    if (reasons.length || warns.length) {
      const head1 = reasons.length ? '<div>· ' + mdInline(reasons[0]) + '</div>' : '';
      const more = (reasons.length - 1) + warns.length;
      const rest = open
        ? reasons.slice(1).map((x) => '<div>· ' + mdInline(x) + '</div>').join('') +
          warns.map((x) => '<div class="neg">! ' + mdInline(x) + '</div>').join('')
        : (more > 0 ? '<div class="more">…还有 ' + more + ' 条（点这一行展开）</div>' : '');
      reasonHtml = head1 + rest;
    } else { reasonHtml = '—'; }

    let condHtml = '—';
    if (condKeys.length) {
      const parts = condKeys.map((k) => condParts(k, it.conditions[k]));
      condHtml = open
        ? parts.map((x) => '<div>· ' + x.full + '</div>').join('')
        : '<div class="cond-line">' + parts.map((x) => x.short).join('<span class="csep">｜</span>') + '</div>';
    }

    const cells = {
      base: '<td class="base-name"><a href="#" data-base="' + esc(b) + '">' + esc(b) + '</a></td>',
      risk: '<td class="' + rm[1] + '"><b>' + rm[0] + '</b></td>',
      verdict: '<td>' + mdInline(it.verdict) + '</td>',
      event: '<td class="sep ' + (sev === 'block' ? 'neg' : '') + '">' + esc(sev) + '</td>',
      reason: '<td class="sep">' + reasonHtml + '</td>',
      cond: '<td class="sep mono-dim">' + condHtml + '</td>',
    };
    return '<tr data-base="' + esc(b) + '"' + (open ? ' data-open="1"' : '') + '>' +
      OV_COLS.map((c) => cells[c.key]).join('') + '</tr>';
  }).join('');
  flashChangedRows(tb);
  tb.querySelectorAll('a[data-base]').forEach((a) => {
    a.onclick = (ev) => {
      ev.preventDefault();
      // 从概览点标的 = 换个标的重跑：切回决策台，并把步进器复位到第①段
      selectBase(a.dataset.base);
      showPane('pane-decision');
      showStage(0);
      runDecision(a.dataset.base, Number($('qty').value) || 5000);
    };
  });
  // 点行的空白处 = 展开/收起该行（紧凑模式下"还有 N 条"的出口）
  tb.querySelectorAll('tr[data-base]').forEach((tr) => {
    tr.onclick = (ev) => {
      if (ev.target.closest && ev.target.closest('a')) return;
      const b = tr.dataset.base;
      OV_STATE.open[b] = !OV_STATE.open[b];
      renderOverview(STATE.ovItems);
    };
  });
}

/* 概览要跑 10 个标的（串行约 10 秒）。先铺一层骨架屏：
   "等 10 秒看着一张空表"和"等 10 秒看着它在动"是两种体验。 */
function skeletonRows(n, cols) {
  let out = '';
  for (let i = 0; i < n; i++) {
    out += '<tr>';
    for (let j = 0; j < cols; j++) out += '<td><span class="skeleton">&nbsp;</span></td>';
    out += '</tr>';
  }
  return out;
}

async function loadOverview() {
  const tb = document.querySelector('#ov-table tbody');
  if (tb) tb.innerHTML = skeletonRows(6, 6);
  try {
    const d = await api(API.overview + '?qty=' + (Number($('qty').value) || 5000));
    renderOverview(d.items);
    const noSrc = (d.items || []).filter((x) => !(x.sources || []).length).length;
    $('ov-note').innerHTML = mdInline(d.disclaimer || '') +
      (noSrc ? '　｜ ' + noSrc + ' 个标的无可回溯事件来源，其事件判断的置信度已被压到 0.40。' : '');
  } catch (e) {
    tb.innerHTML = '<tr><td colspan="6" class="empty neg">加载失败：' + esc(e.message) + '</td></tr>';
  }
}

/* ---------------- 阈值表 ---------------- */

async function loadParams() {
  try {
    const d = await api(API.params);
    document.querySelector('#params-table tbody').innerHTML = (d.params || []).map((p) =>
      '<tr><td>' + esc(p.name) + '</td>' +
      '<td class="mono"><b>' + esc(p.value) + '</b></td>' +
      '<td class="mono-dim">' + esc(p.unit) + '</td>' +
      '<td class="sep">' + esc(p.source) + '</td>' +
      '<td class="sep mono-dim">' + esc(p.code) + '</td></tr>').join('');
  } catch (e) {
    document.querySelector('#params-table tbody').innerHTML =
      '<tr><td colspan="5" class="empty neg">加载失败：' + esc(e.message) + '</td></tr>';
  }
}

/* ---------------- 快照（极简 markdown 渲染） ---------------- */

function mdToHtml(md) {
  const lines = String(md).split(/\r?\n/);
  let out = '', inCode = false, inTable = false, inList = false;
  const closeAll = () => {
    if (inTable) { out += '</tbody></table>'; inTable = false; }
    if (inList) { out += '</ul>'; inList = false; }
  };
  for (const raw of lines) {
    const line = raw;
    if (/^```/.test(line)) {
      closeAll();
      out += inCode ? '</pre>' : '<pre>';
      inCode = !inCode;
      continue;
    }
    if (inCode) { out += esc(line) + '\n'; continue; }
    if (/^\s*$/.test(line)) { closeAll(); continue; }
    let m;
    if ((m = line.match(/^(#{1,3})\s+(.*)$/))) {
      closeAll();
      const lvl = m[1].length;
      out += '<h' + lvl + '>' + mdInline(m[2]) + '</h' + lvl + '>';
      continue;
    }
    if (/^\|/.test(line)) {
      if (/^\|[\s:\-|]+\|$/.test(line.trim())) continue;   // 分隔行
      const cells = line.trim().replace(/^\||\|$/g, '').split('|');
      if (!inTable) {
        out += '<table><tbody>';
        inTable = true;
        out += '<tr>' + cells.map((c) => '<th>' + mdInline(c.trim()) + '</th>').join('') + '</tr>';
      } else {
        out += '<tr>' + cells.map((c) => '<td>' + mdInline(c.trim()) + '</td>').join('') + '</tr>';
      }
      continue;
    }
    if (inTable) { out += '</tbody></table>'; inTable = false; }
    if ((m = line.match(/^\s*(?:[-*]|\d+\.)\s+(.*)$/))) {
      if (!inList) { out += '<ul>'; inList = true; }
      out += '<li>' + mdInline(m[1]) + '</li>';
      continue;
    }
    if (inList) { out += '</ul>'; inList = false; }
    out += '<p>' + mdInline(line) + '</p>';
  }
  closeAll();
  if (inCode) out += '</pre>';
  return out;
}

async function loadSnapshot() {
  try {
    const d = await api(API.snapshot);
    $('snapshot').innerHTML = mdToHtml(d.markdown);
  } catch (e) {
    $('snapshot').innerHTML = '<div class="neg">加载失败：' + esc(e.message) + '</div>';
  }
}

/* 🏦 取账户摘要 —— 注意：**后端不去打交易所**，它只读本机那份落盘的取数结果。
   所以这个请求再频繁也不会产生一次外部调用，更不会碰资金。 */
async function loadAccount() {
  try {
    renderAccount(await api(API.account));
  } catch (e) {
    const el = $('acct');
    if (el) {
      el.innerHTML = '<p class="hint neg">账户接口暂时取不到：' + esc(e.message) +
        ' —— 检查本机服务是否在跑（python run_p2.py）。</p>';
    }
    const meta = $('acct-meta');
    if (meta) meta.innerHTML = '';
  }
}

/* ---------------- 右下角告警弹窗（接 data/positions/alerts.json） ---------------- */

function toast(level, title, detail) {
  const box = $('toasts');
  const el = document.createElement('div');
  el.className = 'toast' + (level === 'critical' ? '' : ' info');
  el.innerHTML = '<span class="t-close">✕</span>' +
    '<div class="t-head">' + esc(title) + '</div><div>' + mdInline(detail) + '</div>';
  el.querySelector('.t-close').onclick = () => el.remove();
  box.appendChild(el);
  if (level !== 'critical') setTimeout(() => el.remove(), 12000);
  while (box.children.length > 4) box.removeChild(box.firstChild);
}

async function pollAlerts() {
  try {
    const d = await api(API.alerts);
    const list = d.alerts || [];
    if (!d.exists) return;
    list.forEach((a, i) => {
      const key = (a.code || '') + '|' + (a.base || '') + '|' + (a.ts || i);
      if (STATE.seenAlerts.has(key)) return;
      STATE.seenAlerts.add(key);
      toast(a.level || 'warn',
        (a.level === 'critical' ? '🔴 ' : '⚠️ ') + (a.base ? a.base + ' · ' : '') + (a.title || a.code || '告警'),
        (a.detail || '') + (a.action ? '　→ ' + a.action : ''));
    });
  } catch (e) { /* 告警是可选功能：不可用时不该拖死整页 */ }
}

/* ================================================================================
   v3 应用外壳：功能区切换 + 决策链步进器
   ================================================================================
   为什么要有它：v2 是 12 个平权面板纵向堆叠（实测 5040px 高），
   要横向比较"链路上哪一段卡住了"得来回滚，而且看不出系统一共有哪些功能。
   v3 把它拆成 4 个功能页 + 决策台内部的 6 段步进器：
     · 顶栏 + 左侧导航常驻 —— 任何时候都知道"我在哪、还有哪些功能"；
     · 步进器把 6 段链条横排 —— 一眼看清"链路上每段判成了什么"，
       点一段看那一段的完整面板（面板常驻 DOM，只是切换显隐）。
   与"可验证"原则一致：切换只改 hidden / class，不改任何渲染逻辑，
   所以 tools/web_smoke.py（Node 最小 DOM）里九个区块照样全部渲染。 */

const PANES = ['pane-decision', 'pane-exec', 'pane-overview', 'pane-trust'];
const STAGE_PANES = ['p-analysts', 'p-debate', 'p-gate', 'p-trader', 'p-risk', 'p-final'];

function setHidden(id, hide) {
  const el = $(id);
  if (!el) return;
  if (hide) {
    if (el.setAttribute) el.setAttribute('hidden', '');
    el.hidden = true;
  } else {
    if (el.removeAttribute) el.removeAttribute('hidden');
    el.hidden = false;
  }
}

function showPane(id) {
  const target = PANES.indexOf(id) >= 0 ? id : PANES[0];
  STATE.pane = target;
  PANES.forEach(function (p) {
    setHidden(p, p !== target);
    const el = $(p);
    if (el && el.classList) el.classList.toggle('is-in', p === target);
  });
  const nav = $('app-nav');
  if (nav && nav.querySelectorAll) {
    nav.querySelectorAll('.nav-item').forEach(function (b) {
      const on = b.getAttribute('data-pane') === target;
      if (b.classList) b.classList.toggle('is-on', on);
      b.setAttribute('aria-current', on ? 'page' : 'false');
    });
  }
  // 深链：把当前功能页写进地址栏，方便直接分享"打开就是概览页"
  if (typeof location !== 'undefined' && typeof history !== 'undefined') {
    try { history.replaceState(null, '', '#' + target); } catch (e) { /* file:// 下可能不允许 */ }
  }
}

function showStage(i) {
  const idx = (typeof i === 'number' && i >= 0 && i < STAGE_PANES.length) ? i : 0;
  STATE.stage = idx;
  STAGE_PANES.forEach(function (id, k) { setHidden(id, k !== idx); });
  const box = $('stages');
  if (box && box.querySelectorAll) {
    box.querySelectorAll('.stage').forEach(function (b) {
      const on = Number(b.getAttribute('data-idx')) === idx;
      if (b.classList) b.classList.toggle('is-on', on);
      b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
  }
}

/* 事件委托：步进器与侧栏内容都是**动态生成**的，逐个绑 onclick 会随重渲染失效。
   ⚠️ Node 最小 DOM（tools/web_render_check.js）里没有 addEventListener /
   querySelectorAll / closest，所以每一步都做能力判断 —— 页面在冒烟里也要能跑完。 */
function initShell() {
  const nav = $('app-nav');
  if (nav && nav.addEventListener) {
    nav.addEventListener('click', function (ev) {
      const t = ev.target;
      const btn = (t && t.closest) ? t.closest('.nav-item') : null;
      if (btn) showPane(btn.getAttribute('data-pane'));
    });
    nav.addEventListener('keydown', function (ev) {
      if (ev.key !== 'ArrowDown' && ev.key !== 'ArrowUp') return;
      if (!nav.querySelectorAll) return;
      const items = Array.prototype.slice.call(nav.querySelectorAll('.nav-item'));
      const cur = items.indexOf(ev.target && ev.target.closest ? ev.target.closest('.nav-item') : null);
      if (cur < 0) return;
      ev.preventDefault();
      const step = ev.key === 'ArrowDown' ? 1 : items.length - 1;
      const next = items[(cur + step) % items.length];
      if (next && next.focus) next.focus();
    });
  }

  const box = $('stages');
  if (box && box.addEventListener) {
    box.addEventListener('click', function (ev) {
      const t = ev.target;
      const btn = (t && t.closest) ? t.closest('.stage') : null;
      if (btn) showStage(Number(btn.getAttribute('data-idx')));
    });
    box.addEventListener('keydown', function (ev) {
      if (ev.key !== 'ArrowRight' && ev.key !== 'ArrowLeft') return;
      if (!box.querySelectorAll) return;
      const items = Array.prototype.slice.call(box.querySelectorAll('.stage'));
      const cur = items.indexOf(ev.target && ev.target.closest ? ev.target.closest('.stage') : null);
      if (cur < 0) return;
      ev.preventDefault();
      const step = ev.key === 'ArrowRight' ? 1 : items.length - 1;
      const next = items[(cur + step) % items.length];
      if (next) {
        showStage(Number(next.getAttribute('data-idx')));
        if (next.focus) next.focus();
      }
    });
  }

  // 深链：地址栏带 #pane-xxx 时直接打开那一页
  let want = null;
  if (typeof location !== 'undefined') {
    const h = String(location.hash || '').replace(/^#/, '');
    if (PANES.indexOf(h) >= 0) want = h;
  }
  showPane(want || STATE.pane);
  showStage(STATE.stage);
}

/* ---------------- 启动 ---------------- */

function selectBase(b) {
  STATE.base = b;
  const box = $('base-chips');
  box.innerHTML = (STATE.bases || []).map((x) =>
    '<span class="chip' + (x === b ? ' active' : '') + '" data-b="' + esc(x) + '">' + esc(x) + '</span>').join(' ');
  box.querySelectorAll('.chip').forEach((el) => {
    el.onclick = () => {
      selectBase(el.dataset.b);
      runDecision(el.dataset.b, Number($('qty').value) || 5000);
    };
  });
}

async function boot() {
  try {
    const h = await loadHealth();
    STATE.bases = h.bases || [];
    selectBase(STATE.base || STATE.bases[0]);
  } catch (e) {
    $('live-dot').className = 'dot err';
    $('disclaimer').innerHTML = '<b class="neg">无法连接本机服务</b>：' + esc(e.message) +
      '　（请在仓库根目录运行 <code>python run_p2.py</code>）';
    return;
  }
  if (STATE.bases.length) initShell();

  $('run').onclick = () => {
    showPane('pane-decision');
    runDecision(STATE.base, Number($('qty').value) || 5000);
  };
  // 刷新概览的同时切到概览页 —— 点了按钮却看不到结果，是最容易让人以为"没反应"的
  $('run-overview').onclick = () => {
    showPane('pane-overview');
    loadOverview();
  };
  $('qty').onchange = () => runDecision(STATE.base, Number($('qty').value) || 5000);
  // 切换在途持仓模式要**立刻重跑**：执行进度官的结论完全取决于这个输入，
  // 不重跑就会出现"下拉框显示 demo、面板还是上一次的结果"这种自相矛盾的页面。
  if ($('pos-mode')) {
    $('pos-mode').onchange = () => runDecision(STATE.base, Number($('qty').value) || 5000);
  }
  // 🏦 账户区块是**独立**的一块：它不参与决策链，也不随标的/金额变化。
  //    手动刷新按钮的用途是"我刚刚在本机重新取过数了，重读一遍"。
  if ($('acct-refresh')) {
    $('acct-refresh').onclick = () => loadAccount();
  }

  loadParams();
  loadSnapshot();
  loadAccount();
  loadOverview();
  pollAlerts();
  setInterval(pollAlerts, 60000);
  runDecision(STATE.base, Number($('qty').value) || 5000);
}
boot();
