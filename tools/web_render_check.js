/* 项目二 · 页面渲染冒烟（Node，无浏览器、无依赖）
 * ================================================================
 * 为什么需要它（这是一个真实踩过的坑）：
 *   仓库最初把项目一的 `web/` 一起复制了过来，页面调用的是项目一服务才有的
 *   端点，独立跑起来**整页是空的** —— 而当时所有自检都是绿的，因为没有任何
 *   一个自检去碰"页面到底渲染出了什么"。
 *
 * 这个脚本做的事：把 `web/index.html` 里真实存在的 id 建成一个最小 DOM，
 * 用真实的接口数据喂给 `web/app.js`，跑完 boot() 之后**逐个区块检查
 * innerHTML 是否真的渲染出了内容**，并检查有没有 `undefined` / `NaN` /
 * 占位文案残留。
 *
 * 用法（由 tools/web_smoke.py 调用，也可手动跑）：
 *   node tools/web_render_check.js _fixtures.json
 * 退出码 0 = 通过；1 = 有区块没渲染出来。
 */

'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const root = path.dirname(__dirname);
const fixturePath = process.argv[2];
if (!fixturePath) {
  console.error('用法：node tools/web_render_check.js <fixtures.json>');
  process.exit(2);
}
const fixtures = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));
const html = fs.readFileSync(path.join(root, 'web', 'index.html'), 'utf8');
const js = fs.readFileSync(path.join(root, 'web', 'app.js'), 'utf8');

/* ---------------- 最小 DOM ---------------- */

const store = new Map();          // key -> element
const rendered = new Map();       // key -> innerHTML（最后一次写入）

function makeEl(key) {
  const e = {
    _key: key,
    _html: '',
    textContent: '',
    className: '',
    title: '',
    disabled: false,
    dataset: {},
    children: [],
    firstChild: null,
    onclick: null,
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); rendered.set(this._key, this._html); },
    appendChild(c) { this.children.push(c); this.firstChild = this.children[0]; return c; },
    removeChild(c) {
      const i = this.children.indexOf(c);
      if (i >= 0) this.children.splice(i, 1);
      this.firstChild = this.children[0] || null;
      return c;
    },
    remove() {},
    querySelector() { return makeEl(key + ' >q'); },
    querySelectorAll() { return []; },
    setAttribute() {}, getAttribute() { return null; },
    addEventListener() {},
  };
  return e;
}
function el(key) {
  if (!store.has(key)) store.set(key, makeEl(key));
  return store.get(key);
}

// 从 index.html 里抽出**真实存在**的 id —— HTML 与 JS 漂移时必须报错
const ids = [...html.matchAll(/\sid="([^"]+)"/g)].map((m) => m[1]);
ids.forEach((i) => el(i));

const documentShim = {
  getElementById: (id) => {
    if (!ids.includes(id)) missingIds.push(id);
    return el(id);
  },
  querySelector: (sel) => el('sel:' + sel),
  querySelectorAll: () => [],
  createElement: (t) => makeEl('created:' + t + ':' + (createdSeq++)),
};
let createdSeq = 0;
const missingIds = [];

/* ---------------- 网络 shim（返回真实接口数据） ---------------- */

function norm(u) {
  const s = String(u);
  const q = s.indexOf('?');
  return q < 0 ? s : s.slice(0, q);
}
/* 夹具匹配：精确 URL -> **参数子集**匹配 -> 路径匹配。
   ⚠️ 为什么需要"子集"这一层：页面会给同一个端点加可选参数
   （例如 `&position=demo`，用来喂在途持仓单）。只做精确匹配时，
   夹具表里没有带新参数的 URL，shim 直接 404，`runDecision` 抛异常 ->
   **整页九个区块全部渲染不出来**，而真实浏览器里其实是好的。
   实测踩到：新增执行进度面板后 web_smoke 报"全都没渲染"，
   真正的原因只是夹具没覆盖新 URL。子集匹配让夹具对未来新增的可选参数免疫。 */
function matchFixture(full) {
  if (full in fixtures) return full;
  const q = full.indexOf('?');
  const path = q < 0 ? full : full.slice(0, q);
  const cands = Object.keys(fixtures).filter(
    (k) => k === path || k.startsWith(path + '?'));
  if (!cands.length) return null;
  if (q < 0) return cands.includes(path) ? path : null;
  const req = new URLSearchParams(full.slice(q + 1));
  // 夹具里的每个参数都必须在请求里出现且值相同（请求可以多带参数）
  for (const k of cands) {
    const kq = k.indexOf('?');
    if (kq < 0) continue;
    let ok = true;
    for (const [a, b] of new URLSearchParams(k.slice(kq + 1))) {
      if (req.get(a) !== b) { ok = false; break; }
    }
    if (ok) return k;
  }
  /* ⚠️ 兜底：夹具是按**路径**存的（`collect()` 对非 decision 端点只存路径），
     而页面一定会带上参数（`/api/overview?qty=5000`）。少了这一步，概览端点
     就会 404 → 页面渲染出"加载失败"，而本检查只看 undefined/NaN/加载中，
     **会把一个从未渲染成功的区块判成通过**（实测：全标的概览表长期只渲染
     出 86 字符的报错行）。参数化的端点用同一份夹具近似即可 —— 这里验证的是
     "这一块有没有渲染出来"，不是参数的逐字精确性。 */
  return cands.includes(path) ? path : null;
}

const fetchShim = async (url) => {
  const full = String(url);
  const key = matchFixture(full);
  if (key === null) {
    return { ok: false, status: 404, json: async () => ({ ok: false, err: 'no fixture ' + full }) };
  }
  return { ok: true, status: 200, json: async () => fixtures[key] };
};

const sandbox = {
  document: documentShim,
  fetch: fetchShim,
  window: {
    // 浏览器里一定有的两个 API：页面代码用它们做"减少动效"判断与逐帧动画。
    // shim 不提供的话，页面里任何用到它们的正常代码都会在这里假失败。
    matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
    requestAnimationFrame: (fn) => setTimeout(fn, 0),
  },
  console,
  setTimeout: (fn) => { try { fn(); } catch (_) {} return 0; },
  setInterval: () => 0,
  clearTimeout: () => {},
  clearInterval: () => {},
  requestAnimationFrame: (fn) => { try { fn(); } catch (_) {} return 0; },
  encodeURIComponent,
  Number, String, Math, JSON, Object, Array, Date, Set, Error, Promise,
};
sandbox.globalThis = sandbox;

/* ---------------- 跑 app.js ---------------- */

let loadErr = null;
try {
  vm.runInNewContext(js, sandbox, { filename: 'web/app.js' });
} catch (e) {
  loadErr = e;
}

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  await wait(400);          // 让 boot() 里的 promise 链跑完

  const problems = [];
  const notes = [];

  if (loadErr) problems.push('app.js 执行抛异常：' + loadErr.message);
  missingIds.forEach((id) => problems.push('app.js 取了 HTML 里不存在的 id：' + id));

  // 必须真的渲染出内容的区块
  const mustRender = {
    verdict: '最终裁决',
    stages: '阶段条',
    analysts: '五路分析师',
    debate: '多空辩论',
    gate: '事件闸门',
    trader: '交易员',
    risk: '风控官（含规则表）',
    execprog: '执行进度官',
    cost: '成本与联合分布',
    prov: '可复跑溯源',
    'sel:#ov-table tbody': '全标的概览表',
    'sel:#params-table tbody': '阈值表',
    snapshot: '数据快照',
  };
  const placeholders = ['加载中', '尚未运行', '读取中', '—', '失败', 'undefined', 'NaN', 'null'];
  for (const [key, label] of Object.entries(mustRender)) {
    const htmlOut = rendered.get(key);
    if (!htmlOut || htmlOut.trim().length < 40) {
      problems.push(label + '（' + key + '）没有渲染出内容');
      continue;
    }
    const bad = placeholders.filter((p) =>
      htmlOut.includes(p) && !(p === '—' && htmlOut.length > 400));
    if (bad.includes('undefined') || bad.includes('NaN')) {
      problems.push(label + ' 渲染里出现 ' + bad.join('/') + '（字段没取到）');
    } else if (htmlOut.includes('加载中') || htmlOut.includes('尚未运行')) {
      problems.push(label + ' 仍停留在占位文案');
    } else {
      notes.push(label + ' ✓ ' + htmlOut.length + ' 字符');
    }
  }

  // 关键内容抽查：决策链必须真的把四层都画出来了
  const v = rendered.get('verdict') || '';
  const r = rendered.get('risk') || '';
  const a = rendered.get('analysts') || '';
  if (!/stance=/.test(v)) problems.push('最终裁决里没有 stance 字段');
  if (!/单调性校验/.test(v)) problems.push('最终裁决里没有单调性校验标记');
  if (!/规则/.test(r)) problems.push('风控官区块里没有规则表');
  if ((a.match(/acard/g) || []).length < 5) {
    problems.push('分析师卡片少于 5 张（实际 ' + (a.match(/acard/g) || []).length + '）');
  }
  if (/不可用|暂不可用/.test(rendered.get('verdict') || '')) {
    problems.push('前端报"风险引擎不可用" —— 说明端点或字段对不上');
  }

  // ---- 概览表：信息密度开关（列显隐 / 条件去重 / 图例）----
  //   ⚠️ 这一块是用户实拍的反馈改的：原来每行的「条件」原文一模一样、
  //      4 行就吃掉半个屏幕。改成"行内只放数值 + 通用口径只写一次"。
  //      渲染对不对必须被检查，否则改回去也没人知道。
  const head = rendered.get('ov-head') || '';
  const chips = rendered.get('ov-cols') || '';
  const legend = rendered.get('ov-legend-body') || '';
  const body = rendered.get('sel:#ov-table tbody') || '';
  const nth = (head.match(/<th/g) || []).length;
  if (nth !== 4) {
    problems.push('概览表默认列数应为 4（标的/风险/结论/事件），实际 ' + nth);
  }
  if ((chips.match(/data-col=/g) || []).length !== 6) {
    problems.push('「显示列」应有 6 个开关 chip，实际 ' +
      (chips.match(/data-col=/g) || []).length);
  }
  if ((chips.match(/aria-pressed="true"/g) || []).length !== 4) {
    problems.push('默认应只有 4 列是打开的（理由/条件默认收起），实际 ' +
      (chips.match(/aria-pressed="true"/g) || []).length);
  }
  if (!/现货点差|挂单时机/.test(legend)) {
    problems.push('条件图例没有渲染出内容（通用口径必须只写一次，不能丢）');
  }
  if ((body.match(/<tr/g) || []).length < 5) {
    problems.push('概览表行数过少（' + (body.match(/<tr/g) || []).length + '）');
  }
  if (/cond-line/.test(body)) {
    problems.push('「条件」列默认应收起，但行里出现了 cond-line');
  }
  notes.push('概览密度开关 ✓（默认 ' + nth + ' 列 / ' + (chips.match(/data-col=/g) || []).length +
    ' 个开关 / 图例 ' + legend.length + ' 字符）');

  // 条件去重的**核心逻辑**：短值不含通用解释，完整版才含。
  // condParts 是纯函数，直接喂合成输入 —— 不依赖夹具里有没有 conditions。
  if (typeof sandbox.condParts === 'function') {
    const p = sandbox.condParts('price_band_bp', 0.46);
    if (!/0\.46 bp/.test(p.short) || !/0\.23 bp/.test(p.short)) {
      problems.push('条件短值没带上"点差 -> 需覆盖"的换算：' + p.short);
    }
    if (/手续费/.test(p.short)) {
      problems.push('条件短值里混进了通用解释（应该只在图例/完整版里出现）：' + p.short);
    }
    if (!/手续费/.test(p.full)) {
      problems.push('条件完整版丢了通用解释');
    }
    notes.push('条件去重 ✓（短值 ' + p.short.replace(/<[^>]+>/g, '') + '）');
  } else {
    problems.push('condParts 不可用（条件去重的逻辑无法被测）');
  }

  // 🔴 回归：演示持仓**不能**是默认值。
  //    实测事故：默认 demo -> 每个标的每跑一次都带"只成交一腿"的合成持仓
  //    -> 执行进度官判裸露敞口 -> 永远"不参与"，还把真实市场原因盖掉了。
  const pmIdx = html.indexOf('id="pos-mode"');
  const firstOpt = pmIdx >= 0 ? (html.slice(pmIdx).match(/<option value="(\w+)"/) || [])[1] : null;
  if (firstOpt !== 'auto') {
    problems.push('「在途持仓」的默认值必须是 auto（真实持仓），实际 ' + firstOpt);
  } else {
    notes.push('在途持仓默认 auto ✓（演示单不是默认值）');
  }

  // ---- ⏱️ 执行进度官：两条分支都要验 ----
  //   ⚠️ 加面板却只检查"有没有渲染出东西"是不够的。这一块两个方向都会错：
  //      ① 有裸露敞口时**没标注是合成单** -> 截图会被当成真实下单记录；
  //      ② 没有在途订单时**画绿灯** -> "没有数据"被读成"没有风险"。
  //   主渲染路径现在走的是 `position=auto`（页面默认），夹具里它 present=false。
  //   ⚠️ 先把**页面默认渲染的结果**取下来再用：下面的 demo 分支会覆盖 execprog，
  //      顺序写反会读到 demo 的内容（实测踩到，报"默认路径没有无在途订单"）。
  const epDefault = rendered.get('execprog') || '';
  const demoKey = Object.keys(fixtures).find(
    (k) => k.startsWith('/api/decision') && k.includes('position=demo'));
  const epData = (fixtures[demoKey] || {}).decision;
  if (!epData) {
    problems.push('夹具里没有带 position=demo 的决策（演示分支无法验证）');
  } else if (typeof sandbox.renderExecProg !== 'function') {
    problems.push('renderExecProg 不可用（执行进度官无法验证）');
  } else {
    try {
      sandbox.renderExecProg(epData);
      const ep = rendered.get('execprog') || '';
      const ed = epData.execution_progress || {};
      if (!ed.present) {
        problems.push('position=demo 的决策里 execution_progress 缺失（执行进度官没接线）');
      } else {
        if (!/合成演示持仓/.test(ep)) {
          problems.push('合成演示单没有在页面上显眼标注（截图会被当成真实下单记录）');
        }
        (ed.hypotheses || []).forEach((h) => {
          if (!ep.includes(h.id)) problems.push('执行中假设没有渲染出来：' + h.id);
        });
        (ed.actions || []).forEach((a) => {
          if (!ep.includes(a.title)) problems.push('确定性处置口径没有渲染出来：' + a.title);
        });
        if ((ed.dropped || []).length && !/未过阈值/.test(ep)) {
          problems.push('"未过阈值"的留痕没有渲染（读者无法判断判据有没有在工作）');
        }
        notes.push('执行进度官·有敞口分支 ✓（' + ed.verdict + '，' +
                   (ed.hypotheses || []).length + ' 条假设 / ' + (ed.dropped || []).length +
                   ' 条留痕 / ' + (ed.actions || []).length + ' 条处置，含合成单标注）');
      }
    } catch (e) {
      problems.push('渲染 demo 执行进度时抛异常：' + e.message);
    }
  }
  // 页面**默认**路径（position=auto）必须落在"无在途订单"上，且不画绿灯
  {
    if (!/无在途订单/.test(epDefault)) {
      problems.push('页面默认路径（position=auto，无持仓单）没有显示"无在途订单"');
    } else if (/正常|通过|✓/.test(epDefault)) {
      problems.push('无在途订单时页面画了绿灯 —— 没有数据 ≠ 没有风险');
    } else {
      notes.push('执行进度官·无持仓分支 ✓（如实说明，不画绿灯）');
    }
  }

  // ---- 第二条路径：**被一票否决**（stand_down + order=null）----
  // 这是本快照里最常见的结果（现货腿停滞 -> agent:stale_quotes -> reject）。
  // 只测"可执行"那条路径是不够的：order 为 null 时最容易渲染出 "undefined"。
  const metaKey = Object.keys(fixtures).find(
    (k) => k.startsWith('/api/decision') && k.includes('base=META'));
  if (metaKey && typeof sandbox.renderVerdict === 'function') {
    const md = fixtures[metaKey].decision;
    try {
      sandbox.renderVerdict(md);
      sandbox.renderRisk(md);
      sandbox.renderTrader(md);
      const v2 = rendered.get('verdict') || '';
      const r2 = rendered.get('risk') || '';
      const t2 = rendered.get('trader') || '';
      if (md.final.stance !== 'stand_down') {
        notes.push('META 决策本次不是 stand_down（' + md.final.stance + '），跳过否决路径检查');
      } else if (!/不参与/.test(v2)) {
        problems.push('stand_down 路径没渲染出"不参与"');
      } else if (/undefined|NaN/.test(v2 + r2 + t2)) {
        problems.push('stand_down 路径渲染里出现 undefined/NaN（order 为 null 时最容易踩）');
      } else {
        notes.push('一票否决路径 ✓（' + esc0(md.risk.hits.join('、')) + '）');
      }
    } catch (e) {
      problems.push('渲染 stand_down 决策时抛异常：' + e.message);
    }
  }

  // ---- 🔴 最后一段：多规则不隐藏（放在最后，因为它会重画 verdict 与阶段条）----
  //   事故：agent 规则被排到 vetoes 首位 -> 理由栏只写 agent:partial_fill_naked，
  //   把真正的市场原因 debate_stand_down 藏了。原因可以被排序，但不许被隐藏。
  if (typeof sandbox.renderVerdict === 'function') {
    const fake = {
      base: 'X', time_basis: null,
      final: { stance: 'stand_down', qty_usd: 0, order: null,
               why: '风控官一票否决：agent:a' },
      monotonic: { stance_non_increasing: true, qty_non_increasing: true },
      risk: { hits: ['agent:a', 'debate_stand_down'],
              vetoes: ['agent:a', 'debate_stand_down'] },
      stages: [],
    };
    try {
      sandbox.renderVerdict(fake);
      const vv = rendered.get('verdict') || '';
      if (!/debate_stand_down/.test(vv)) {
        problems.push('多条触发规则时，被排到后面的那条没有显示出来（会被误读成唯一原因）');
      } else if (!/触发规则共/.test(vv)) {
        problems.push('没有提示"触发规则共 N 条"');
      } else {
        notes.push('多规则不隐藏 ✓（排在后面的 veto 也列出来了）');
      }
    } catch (e) {
      problems.push('渲染多规则决策时抛异常：' + e.message);
    }
  }
  function esc0(s) { return String(s || '').slice(0, 60); }

  const out = { ok: problems.length === 0, problems, notes,
                toastCount: (store.get('toasts') || { children: [] }).children.length,
                renderedKeys: [...rendered.keys()] };
  console.log(JSON.stringify(out, null, 1));
  process.exit(out.ok ? 0 : 1);
})();
