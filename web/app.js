/* 项目二 · 执行决策台 —— 前端逻辑
   只消费本仓库 run_p2.py 提供的 /api/*；无外部依赖（离线可用）。

   ⚠️ 记录一个踩过的坑：这个页面最初是项目一的统一页面，调用的是
   /api/overview、/api/timeline、/api/data-status —— 那三个端点**只有项目一
   的服务才有**。独立跑起来时页面全是"加载中…"，而所有自检都是绿的
   （因为没有一个自检去碰 HTTP）。现在：端点集合集中写在 API 里，
   run_p2.py --selftest 会照着这份清单真的打一遍。 */

'use strict';

const $ = (id) => document.getElementById(id);

// ⭐ 页面调用的**全部**端点（与 run_p2.py 的路由一一对应）
const API = {
  health: '/api/health',
  bases: '/api/bases',
  decision: '/api/decision',
  assess: '/api/assess',
  overview: '/api/overview',
  params: '/api/params',
  snapshot: '/api/snapshot',
  alerts: '/api/alerts',
};

const STATE = { base: null, qty: 5000, busy: false, seenAlerts: new Set() };

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
function fmt(v, d = 2) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '—';
  return Number(v).toFixed(d);
}
function pct(v, d = 2) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '—';
  return (Number(v) * 100).toFixed(d) + '%';
}
function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

async function api(path) {
  const r = await fetch(path, { cache: 'no-store' });
  const d = await r.json().catch(() => ({ ok: false, err: 'HTTP ' + r.status }));
  if (!r.ok || d.ok === false) throw new Error(d.err || (path + ' -> HTTP ' + r.status));
  return d;
}

const STANCE_CN = { proceed: '可执行', caution: '谨慎执行', stand_down: '不参与' };
const RISK_CN = { low: ['低', 'pos'], medium: ['中', ''], high: ['高', 'neg'] };

/* ---------------- 顶栏 ---------------- */

async function loadHealth() {
  const h = await api(API.health);
  $('snap-ts').textContent = (h.snapshot && h.snapshot.snapshot_utc) || '—';
  $('n-bases').textContent = (h.bases || []).length + ' 个';
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

/* ---------------- 决策链主视图 ---------------- */

function renderVerdict(d) {
  const f = d.final, mo = d.monotonic || {};
  const okMo = mo.stance_non_increasing !== false && mo.qty_non_increasing !== false;
  const order = f.order;
  const box = $('verdict');
  box.innerHTML =
    '<div class="verdict-bar ' + esc(f.stance) + '">' +
      '<div><div class="stance">' + esc(STANCE_CN[f.stance] || f.stance) + '</div>' +
      '<div class="mono-dim">最终立场 stance=' + esc(f.stance) + '</div></div>' +
      '<div><div class="qty">' + fmt(f.qty_usd, 0) + ' USD</div>' +
      '<div class="mono-dim">' + (order
        ? ('方式 ' + esc(order.mode || order.kind) + ' ｜ ' + (order.slices || 0) + ' 笔 ｜ 成本 ' + fmt(order.cost_bp) + ' bp')
        : '不下单（规模 0）') + '</div></div>' +
      '<div class="why">' + mdInline(f.why || '') + '</div>' +
      '<div><span class="tag ' + (okMo ? '' : 'veto') + '">' +
        (okMo ? '单调性校验通过' : '单调性校验失败') + '</span>' +
        '<div class="mono-dim">最终 ≤ 辩论；规模 ≤ 各环节最小值</div></div>' +
    '</div>' +
    (f.min_notional_binding
      ? '<p class="hint">⚠️ 可执行规模低于名义额下限（' +
        fmt(d.min_notional_usd || 100, 0) + ' USD）：正确结论是<b>不做</b>，' +
        '而不是拿小到没意义的钱去做一笔。</p>' : '');

  // 阶段条
  const stages = d.stages || [];
  $('stages').innerHTML = stages.map((s) => {
    const st = s.stance || (s.verdict === 'reject' ? 'stand_down' : '') || '';
    return '<div class="stage">' +
      '<div class="s-name">' + esc(s.stage || '') + '</div>' +
      '<div class="s-stance s-' + esc(st) + '">' +
        (st ? esc(STANCE_CN[st] || st) : (s.verdict ? esc(s.verdict) : '—')) + '</div>' +
      '<div class="s-detail">' +
        (s.qty_usd !== undefined && s.qty_usd !== null ? (fmt(s.qty_usd, 0) + ' USD ｜ ') : '') +
        mdInline((s.detail || '').slice(0, 150)) + '</div></div>';
  }).join('');
}

function renderAnalysts(d) {
  const list = d.analysts || [];
  $('analysts').innerHTML = list.map((a) => {
    const ev = (a.evidence || []).map((e) =>
      '<div>· ' + esc(e.metric) + ' = <b>' + esc(e.value) + '</b>' +
      '<span class="mono-dim"> ← ' + esc(e.source) + '</span></div>').join('');
    return '<div class="acard' + (a.valid ? '' : ' invalid') + '">' +
      '<div class="a-head"><span class="a-dim">' + esc(a.dimension) + '</span>' +
      '<span class="a-conf">置信度 ' + fmt(a.confidence, 2) + '</span></div>' +
      '<div><span class="badge ' + esc(a.verdict) + '">' + esc(a.verdict) + '</span>' +
      (a.valid ? '' : ' <span class="tag veto">已作废</span>') +
      ' <span class="mono-dim">证据 ' + (a.evidence || []).length + ' 条</span></div>' +
      (a.notes ? '<div class="a-notes">' + mdInline(a.notes) + '</div>' : '') +
      (a.invalid_reason ? '<div class="a-notes neg">作废原因：' + esc(a.invalid_reason) + '</div>' : '') +
      (ev ? '<div class="a-ev">' + ev + '</div>' : '') +
      '<div class="a-src">' + esc((a.sources || []).join(' ｜ ')) + '</div>' +
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
      '<span class="cb-track"><span class="cb-fill" style="width:' +
        clamp(100 * (v.bull_weight || 0) / Math.max(0.01, (v.bull_weight || 0) + (v.bear_weight || 0)), 0, 100) +
        '%;background:linear-gradient(90deg,#1d8f63,#35c98a)"></span></span>' +
      '<span class="cb-val">' + fmt(v.bull_weight, 2) + ' vs ' + fmt(v.bear_weight, 2) + '</span></div>' +
    '<p><span class="tag">裁决</span> <b class="s-' + esc(v.stance) + '">' +
      esc(STANCE_CN[v.stance] || v.stance) + '</b>　' + mdInline(v.reason || '') + '</p>' +
    (v.weighting_changed_stance
      ? '<p class="hint">⭐ 证据强度加权**改变了结论**：未加权时 raw ' +
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
        esc(v.direction_conflicts.join('；')).slice(0, 400) + '</p>' : '');
}

function renderGate(d) {
  const g = d.gate || {};
  const sev = g.gate_severity || 'none';
  const cls = sev === 'block' ? 'neg' : (sev === 'caution' ? '' : 'pos');
  $('gate').innerHTML =
    '<div class="verdict-bar ' + (sev === 'block' ? 'stand_down' : (sev === 'caution' ? 'caution' : 'proceed')) + '">' +
      '<div><div class="stance">' + esc(sev) + '</div>' +
      '<div class="mono-dim">severity</div></div>' +
      '<div class="why">' + mdInline(g.gate_reason || '') + '</div></div>' +
    '<table><tbody>' +
      '<tr><th>判断来源</th><td class="' + (String(g.gate_source).startsWith('llm') ? 'pos' : '') + '">' +
        esc(g.gate_source) + '</td></tr>' +
      '<tr><th>是否允许挂单</th><td>' + (g.maker_allowed ? '允许' : '<b class="neg">禁止（硬规则）</b>') + '</td></tr>' +
      '<tr><th>硬约束</th><td>severity=block → <b>挂单类方案直接作废</b>；agent 不能推翻</td></tr>' +
      (d.prompt && d.prompt.version
        ? '<tr><th>prompt 版本</th><td class="mono">' + esc(d.prompt.version) +
          (d.prompt.sha256 ? ' ｜ sha256 ' + esc(String(d.prompt.sha256).slice(0, 16)) : '') +
          (d.prompt.path ? ' ｜ ' + esc(d.prompt.path) : '') + '</td></tr>' : '') +
    '</tbody></table>' +
    (String(g.gate_source).startsWith('llm')
      ? '<p class="hint">✅ 本次由 <b>LLM</b> 判事件 —— 这是大模型在运行期的唯一职责。</p>'
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
      '<span class="cb-name">' + esc(k) + (k === best ? ' ✓' : '') + '</span>' +
      '<span class="cb-track"><span class="cb-fill" style="width:' +
        clamp(100 * Math.abs(modes[k]) / maxAbs, 2, 100) + '%"></span></span>' +
      '<span class="cb-val ' + (modes[k] > thr ? 'neg' : 'pos') + '">' + fmt(modes[k]) + ' bp</span></div>').join('');
  const o = t.order;
  $('trader').innerHTML =
    (bars || '<div class="loading">无成本方案</div>') +
    '<div class="thr-line"><span>净收益判据门槛 ' + thr + ' bp</span></div>' +
    '<table><tbody>' +
      '<tr><th>选定方式</th><td><b>' + esc(t.mode || '—') + '</b></td>' +
          '<th>成本</th><td>' + fmt(t.cost_bp) + ' bp</td></tr>' +
      '<tr><th>规模</th><td>' + (o ? fmt(o.qty_usd, 0) + ' USD' : '—') + '</td>' +
          '<th>拆单</th><td>' + (o ? (o.slices + ' 笔 × 上限 ' + fmt(o.slice_usd, 0)) : '—') + '</td></tr>' +
      '<tr><th>价位</th><td colspan="3">' + esc(t.price || (o && o.price_desc) || '—') + '</td></tr>' +
      '<tr><th>规模上限来自</th><td colspan="3">' + esc((o && o.size_cap_by) || '—') + '</td></tr>' +
      '<tr><th>毛边际 / 距门槛</th><td colspan="3">' +
        fmt(t.gross_edge_bp) + ' bp ／ <b class="' + ((t.edge_gap_bp || 0) < 0 ? 'neg' : 'pos') + '">' +
        fmt(t.edge_gap_bp) + ' bp</b></td></tr>' +
      ((o && o.size_bounds) ? o.size_bounds.map((b) =>
        '<tr><th>约束</th><td colspan="3">' + esc(b.name) + ' → ' + fmt(b.usd, 0) + ' USD</td></tr>').join('') : '') +
      ((t.barred_modes || []).length ? '<tr><th>被禁方式</th><td colspan="3" class="neg">' +
        esc(t.barred_modes.join('、')) + '</td></tr>' : '') +
    '</tbody></table>' +
    ((t.blocked_by || []).length
      ? '<p class="hint neg">未下单原因：' + mdInline(t.blocked_by[0]) + '</p>'
      : '') +
    '<p class="hint">交易员<b>不新造阈值、不做价格预测</b>：价位＝中价 ± 实测半幅点差；' +
    '规模＝min(请求, 可捕获名义额(实测), 首档深度×25%)。</p>';
}

function renderRisk(d) {
  const r = d.risk || {};
  const rules = r.rules || [];
  const rows = rules.map((x) =>
    '<tr class="' + (x.triggered ? 'hit' : '') + (x.agent ? ' agent-rule' : '') + '">' +
      '<td>' + esc(x.id) + (x.agent ? ' <span class="tag agent">agent 提出</span>' : '') + '</td>' +
      '<td><span class="tag ' + (x.level === 'veto' ? 'veto' : 'caution') + '">' + esc(x.level) + '</span></td>' +
      '<td>' + esc(x.action) + '</td>' +
      '<td class="sep">' + mdInline(x.statement || '') + '</td>' +
      '<td class="sep mono-dim">' + mdInline(x.falsifier || '') + '</td>' +
      '<td>' + (x.triggered ? '<b class="neg">触发</b>' : '<span class="mono-dim">未触发</span>') + '</td>' +
    '</tr>').join('');
  const hyps = (d.risk_hypotheses || []).map((h) =>
    '<div class="arg"><div class="cl">' + esc(h.id) + '：' + mdInline(h.hypothesis || '') + '</div>' +
    '<div class="fa">实测量 <b>' + esc(h.metric) + ' = ' + esc(h.value) + '</b> ｜ 阈值 ' +
      esc(h.threshold) + '</div>' +
    '<div class="fa"><b>证伪条件</b>：' + mdInline(h.falsifier || '') + ' ｜ 动作 ' + esc(h.action) + '</div></div>').join('');
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

function renderCost(d) {
  const c = d.cost || {};
  const bar = (name, val, good) => (typeof val === 'number'
    ? '<div class="costbar"><span class="cb-name">' + name + '</span>' +
      '<span class="cb-track"><span class="cb-fill" style="width:' +
      clamp(Math.abs(val) * 4, 2, 100) + '%;' + (good ? 'background:linear-gradient(90deg,#1d8f63,#35c98a)' : '') +
      '"></span></span><span class="cb-val">' + val.toFixed(1) + ' bp</span></div>' : '');
  $('cost').innerHTML =
    '<table><tbody>' +
      '<tr><th>最优方式</th><td><b>' + esc(c.best_mode || '—') + '</b></td>' +
          '<th>成本</th><td>' + fmt(c.best_cost) + ' bp</td></tr>' +
      '<tr><th>现货半幅点差</th><td>' + fmt(c.half_s, 3) + ' bp</td>' +
          '<th>永续半幅点差</th><td>' + fmt(c.half_p, 3) + ' bp</td></tr>' +
      '<tr><th>route / session</th><td>' + esc(c.route || '—') + ' / ' + esc(c.session || '—') + '</td>' +
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
      ? '<p class="hint neg">被硬规则作废的方式：' + esc(c.invalidated.join('、')) + '</p>' : '');
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
  $('run-state').textContent = '正在跑决策链（读快照 + 5 路分析 + 辩论 + 闸门 + 交易员 + 风控官）…';
  try {
    const d = (await api(API.decision + '?base=' + encodeURIComponent(base) + '&qty=' + qty)).decision;
    renderVerdict(d); renderAnalysts(d); renderDebate(d); renderGate(d);
    renderTrader(d); renderRisk(d); renderCost(d); renderProv(d);
    $('run-state').textContent = '完成 ｜ ' + d.base + ' ｜ ' + d.generated_utc;
  } catch (e) {
    $('run-state').innerHTML = '<span class="neg">失败：' + esc(e.message) + '</span>';
    $('live-dot').className = 'dot err';
  } finally {
    STATE.busy = false;
    $('run').disabled = false;
  }
}

/* ---------------- 全标的概览 ---------------- */

function renderOverview(items) {
  const tb = document.querySelector('#ov-table tbody');
  if (!items || !items.length) {
    tb.innerHTML = '<tr><td colspan="6" class="empty">无数据</td></tr>';
    return;
  }
  tb.innerHTML = items.map((it) => {
    const rm = RISK_CN[it.risk_level] || ['?', ''];
    const reasons = (it.rationale || []).map((x) => '<div>· ' + mdInline(x) + '</div>').join('');
    const warns = (it.warnings || []).map((x) => '<div class="neg">! ' + mdInline(x) + '</div>').join('');
    const cond = Object.keys(it.conditions || {}).map((k) => esc(k) + '=' + esc(String(it.conditions[k]))).join('；') || '—';
    const sev = ((it.event || {}).severity) || '—';
    return '<tr>' +
      '<td class="base-name"><a href="#" data-base="' + esc(it.base) + '">' + esc(it.base) + '</a></td>' +
      '<td class="' + rm[1] + '"><b>' + rm[0] + '</b></td>' +
      '<td>' + esc(it.verdict) + '</td>' +
      '<td class="sep">' + reasons + warns + '</td>' +
      '<td class="sep mono-dim">' + cond + '</td>' +
      '<td class="sep ' + (sev === 'block' ? 'neg' : '') + '">' + esc(sev) + '</td></tr>';
  }).join('');
  tb.querySelectorAll('a[data-base]').forEach((a) => {
    a.onclick = (ev) => {
      ev.preventDefault();
      selectBase(a.dataset.base);
      runDecision(a.dataset.base, Number($('qty').value) || 5000);
    };
  });
}

async function loadOverview() {
  try {
    const d = await api(API.overview + '?qty=' + (Number($('qty').value) || 5000));
    renderOverview(d.items);
    const noSrc = (d.items || []).filter((x) => !(x.sources || []).length).length;
    $('ov-note').innerHTML = mdInline(d.disclaimer || '') +
      (noSrc ? '　｜ ' + noSrc + ' 个标的无可回溯事件来源，其事件判断的置信度已被压到 0.40。' : '');
  } catch (e) {
    document.querySelector('#ov-table tbody').innerHTML =
      '<tr><td colspan="6" class="empty neg">加载失败：' + esc(e.message) + '</td></tr>';
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
  $('run').onclick = () => runDecision(STATE.base, Number($('qty').value) || 5000);
  $('run-overview').onclick = loadOverview;
  $('qty').onchange = () => runDecision(STATE.base, Number($('qty').value) || 5000);

  loadParams();
  loadSnapshot();
  loadOverview();
  pollAlerts();
  setInterval(pollAlerts, 60000);
  runDecision(STATE.base, Number($('qty').value) || 5000);
}
boot();
