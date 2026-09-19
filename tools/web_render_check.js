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
// 先按**完整 URL**匹配（这样可以同时喂 NVDA 与 META 两份决策数据，
// 用于分别验证"可执行"与"被一票否决"两条渲染路径），再退回按路径匹配。
const fetchShim = async (url) => {
  const full = String(url);
  const key = (full in fixtures) ? full : norm(full);
  if (!(key in fixtures)) {
    return { ok: false, status: 404, json: async () => ({ ok: false, err: 'no fixture ' + key }) };
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
  function esc0(s) { return String(s || '').slice(0, 60); }

  const out = { ok: problems.length === 0, problems, notes,
                toastCount: (store.get('toasts') || { children: [] }).children.length,
                renderedKeys: [...rendered.keys()] };
  console.log(JSON.stringify(out, null, 1));
  process.exit(out.ok ? 0 : 1);
})();
