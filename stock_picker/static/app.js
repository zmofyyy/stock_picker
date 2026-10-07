/* 放量选股工具前端逻辑（原生 JS，无构建步骤） */
'use strict';

/* 中国习惯：涨红、跌绿；取 macOS 深色系统色，保证暗底可读 */
const UP = '#ff453a';
const DOWN = '#30d158';

/* macOS Vibrancy 主题取色（与 style.css 的 CSS 变量保持一致） */
const C = {
  text: '#f5f5f7',
  text2: '#c7c7cc',
  muted: '#98989d',
  line: 'rgba(255,255,255,.10)',
  grid: 'rgba(255,255,255,.06)',
  glass: 'rgba(44,44,46,.96)',
  accent: '#0a84ff',
  warning: '#ff9f0a',
  ma5: '#ffd60a',
  ma10: '#0a84ff',
  ma20: '#bf5af2',
  ma60: '#98989d',
  volma: '#64d2ff',
  // 偏离度附图（通达信原作 COLOR2191ED）：主体 T 线沿用该蓝色
  bias: '#2191ed',
  biasM5: '#ff9f0a',
  biasM20: '#30d158',
};

const state = {
  rows: [],
  plans: [],
  watch: [],
  current: null,
  klineChart: null,
  klineBias: true,      // 偏离度附图默认显示（抽屉顶部可关）
  pollTimer: null,
  industries: [],       // 全部二级行业名
  industryTree: {},     // 一级 -> [二级]
  indSummary: [],       // 本次命中的二级行业分布
  indFilter: '',        // 当前选中的二级行业筛选
  concepts: [],         // 全部概念名（通达信板块文件顺序）
  conceptItems: [],     // 概念目录 [{name, n, index_code, ...}]
  conceptSummary: [],   // 本次命中的概念分布
  conceptFilter: '',    // 当前概念筛选的原始输入串
  tradeDates: [],       // 本地缓存覆盖的交易日（升序 ISO）
  tradeDateSet: null,   // 同上，用于 O(1) 判断某天是否交易日
  assetVersion: null,   // 页面加载时后端的前端资源指纹（用于发现「本页已过期」）
  sort: { key: 'vol_ratio', dir: 'desc' },   // 结果表排序（默认与后端一致：量比降序）

  /* ---- 连板梯队 ---- */
  streak: null,             // 最近一次 /api/streaks 的完整返回
  streakRows: [],           // 涨停明细（未过滤）
  streakIndFilter: '',      // 连板梯队的二级行业筛选
  streakCptFilter: '',      // 连板梯队的概念筛选
  streakSort: { key: 'streak', dir: 'desc' },
  streakChart: null,        // ECharts 实例

  /* ---- 板块交集（二级行业 ∩ 概念） ---- */
  industriesCounts: [],     // 二级行业静态成员数 [{l2, n}]（已按 n 降序）
  ixInd: [],                // 已选二级行业（多选即并集）
  ixCpt: [],                // 已选概念
  ixIndSearch: '',
  ixCptSearch: '',
  intersect: null,          // 最近一次 /api/intersect 的完整返回
  ixRows: [],               // 交集明细（未排序）
  ixSort: { key: 'amount_yi', dir: 'desc' },

  /* ---- 板块看盘（概念板块 / 行业板块） ---- */
  bpCatalog: null,          // /api/board_catalog 的返回（类别清单）
  bpCategory: '概念',        // 当前类别 tab（index.html 里两个 .bp-tab 的 data-cat 之一）
  bpPanel: null,            // /api/board_panel 的返回
  bpRows: [],               // 板块列表（未排序）
  bpSort: { key: 'pct', dir: 'desc' },
  bpBoard: null,            // 当前选中的板块（一行）
  bpMembers: null,          // /api/board_members 的返回
  bpMemberRows: [],
  bpMemberSort: { key: 'pct', dir: 'desc' },
  bpStock: null,            // 当前选中的成分股（一行）
  bpIndexChart: null,       // 板块指数图（主图 + 量 + MACD）
  bpStockChart: null,       // 个股图
  bpLoaded: false,

  /* ---- 自选板块（通达信 T0002/blocknew） ---- */
  wbCatalog: null,          // /api/watch_blocks 的返回（板块清单）
  wbKey: '',                // 当前选中的板块 key（.blk 文件名去后缀）
  wbPanel: null,            // /api/watch_block_panel 的返回
  wbRows: [],               // 成员股明细（未排序）
  wbSort: { key: 'amount_yi', dir: 'desc' },
  wbIndFilter: [],          // 点选的二级行业（服务端过滤）
  wbCptFilter: [],          // 点选的概念
  wbCptMode: 'any',
  wbLoaded: false,
};

/* 自选板块成员表可排序的列（默认与后端一致：成交额降序） */
const WB_SORT_COLS = {
  code:          { type: 'text', get: (r) => r.symbol || r.code },
  name:          { type: 'text', get: (r) => r.name || '' },
  industry_l2:   { type: 'text', get: (r) => r.industry_l2 || '' },
  hit_n:         { type: 'num',  get: (r) => (r.hit_concepts || []).length },
  concept_n:     { type: 'num',  get: (r) => r.concept_n ?? (r.concepts || []).length },
  close:         { type: 'num',  get: (r) => r.close },
  pct_change:    { type: 'num',  get: (r) => r.pct_change },
  amount_yi:     { type: 'num',  get: (r) => r.amount_yi },
  float_mcap_yi: { type: 'num',  get: (r) => r.float_mcap_yi },
  board:         { type: 'text', get: (r) => r.board || '' },
  limit:         { type: 'text', get: (r) => r.limit || '' },
};
const WB_DEFAULT_SORT = { key: 'amount_yi', dir: 'desc' };

/* 结果表可排序的列：get(r) 取排序值；type 决定比较方式与首次点击的方向 */
const SORT_COLS = {
  code:          { type: 'text', get: (r) => r.symbol || r.code },
  name:          { type: 'text', get: (r) => r.name || '' },
  industry_l2:   { type: 'text', get: (r) => r.industry_l2 || '' },
  concept_n:     { type: 'num',  get: (r) => r.concept_n ?? (r.concepts || []).length },
  signal_date:   { type: 'text', get: (r) => r.signal_date || '' },
  close:         { type: 'num',  get: (r) => r.close },
  pct_change:    { type: 'num',  get: (r) => r.pct_change },
  vol_ratio:     { type: 'num',  get: (r) => r.vol_ratio },
  float_mcap_yi: { type: 'num',  get: (r) => r.float_mcap_yi },
  buy_price:     { type: 'num',  get: (r) => r.buy_price },
  sell_price:    { type: 'num',  get: (r) => r.sell_price },
  volume_hand:   { type: 'num',  get: (r) => r.volume_hand },
  ma_volume:     { type: 'num',  get: (r) => r.ma_volume },
  amount_yi:     { type: 'num',  get: (r) => r.amount_yi },
};
const DEFAULT_SORT = { key: 'vol_ratio', dir: 'desc' };

/* 连板梯队明细表的可排序列 */
const STREAK_SORT_COLS = {
  streak:        { type: 'num',  get: (r) => r.streak },
  code:          { type: 'text', get: (r) => r.symbol || r.code },
  name:          { type: 'text', get: (r) => r.name || '' },
  seal:          { type: 'text', get: (r) => r.seal || '' },
  industry_l2:   { type: 'text', get: (r) => r.industry_l2 || '' },
  concept_n:     { type: 'num',  get: (r) => r.concept_n ?? (r.concepts || []).length },
  close:         { type: 'num',  get: (r) => r.close },
  pct_change:    { type: 'num',  get: (r) => r.pct_change },
  amount_yi:     { type: 'num',  get: (r) => r.amount_yi },
  float_mcap_yi: { type: 'num',  get: (r) => r.float_mcap_yi },
  limit_rate:    { type: 'num',  get: (r) => r.limit_factor },
  board:         { type: 'text', get: (r) => r.board || '' },
};

/* 板块交集明细表的可排序列（默认与后端一致：成交额降序） */
const INTERSECT_SORT_COLS = {
  code:          { type: 'text', get: (r) => r.symbol || r.code },
  name:          { type: 'text', get: (r) => r.name || '' },
  industry_l2:   { type: 'text', get: (r) => r.industry_l2 || '' },
  hit_n:         { type: 'num',  get: (r) => r.hit_n ?? (r.hit_concepts || []).length },
  concept_n:     { type: 'num',  get: (r) => r.concept_n ?? (r.concepts || []).length },
  close:         { type: 'num',  get: (r) => r.close },
  pct_change:    { type: 'num',  get: (r) => r.pct_change },
  amount_yi:     { type: 'num',  get: (r) => r.amount_yi },
  float_mcap_yi: { type: 'num',  get: (r) => r.float_mcap_yi },
  board:         { type: 'text', get: (r) => r.board || '' },
};
const IX_DEFAULT_SORT = { key: 'amount_yi', dir: 'desc' };

/* 概念有 269 个：无搜索词时只渲染前这么多个（按成员数降序），
   一次铺 269 个按钮既难找也拖慢渲染，搜一下就能拿到其余全部。 */
const IX_CPT_PREVIEW = 120;

/* ------------------------------------------------------------------ */
/* 工具                                                                */
/* ------------------------------------------------------------------ */
const $ = (sel) => document.querySelector(sel);

function toast(msg, type = '', ms = 3200) {
  const el = document.createElement('div');
  el.className = 'toast ' + type;
  el.textContent = msg;
  $('#toastWrap').appendChild(el);
  setTimeout(() => el.remove(), ms);
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const text = await res.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = { raw: text }; }
  if (!res.ok) throw new Error(data.detail || data.reason || `HTTP ${res.status}`);
  return data;
}

const fmt = (v, d = 2) => (v === null || v === undefined || v === '' ? '-' : Number(v).toFixed(d));
const fmtPct = (v, d = 2) => (v === null || v === undefined ? '-' : (Number(v) * 100).toFixed(d) + '%');
const fmtInt = (v) => (v === null || v === undefined ? '-' : Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 0 }));
/* 手 → 万手（≥1 万手才换算），列宽能省下不少 */
const fmtWan = (hand) => {
  const v = Number(hand);
  if (!Number.isFinite(v)) return '-';
  return Math.abs(v) >= 1e4 ? (v / 1e4).toFixed(1) + '万' : v.toFixed(0);
};

function pctClass(v) {
  if (v === null || v === undefined || Number(v) === 0) return '';
  return Number(v) > 0 ? 'up' : 'down';
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/* ------------------------------------------------------------------ */
/* 状态栏                                                              */
/* ------------------------------------------------------------------ */
async function loadStatus() {
  try {
    const s = await api('/api/status');
    const pills = [];
    const ready = s.ready;
    pills.push(`<span class="pill ${ready ? 'ok' : 'err'}">数据源 ${ready ? '就绪' : '不可用'}</span>`);
    pills.push(`<span class="pill">${esc(s.tdx_dir || '-')}</span>`);
    pills.push(`<span class="pill">A股 ${s.a_share_total} 只 / 文件 ${s.file_total}</span>`);
    pills.push(`<span class="pill">名称 ${s.names.size} 条</span>`);
    if (s.industries) {
      pills.push(`<span class="pill">行业 ${s.industries.l1} 一级 / ${s.industries.l2} 二级 · 覆盖 ${s.industries.size} 只</span>`);
    }
    if (s.concepts && s.concepts.count) {
      pills.push(`<span class="pill">概念 ${s.concepts.count} 个 · 覆盖 ${s.concepts.size} 只</span>`);
    }
    if (s.cache && s.cache.exists) {
      pills.push(`<span class="pill ok">缓存 ${s.cache.codes_loaded} 只 · ${esc(s.cache.last_date || '')}</span>`);
      pills.push(`<span class="pill">读取模式 ${esc(s.cache.reader_mode || s.reader_mode)} · ${s.cache.elapsed || 0}s</span>`);
    } else {
      pills.push(`<span class="pill warn">尚无缓存，请先刷新数据</span>`);
    }
    if (s.storage) {
      const dbName = String(s.storage.db_path || '').split(/[\\/]/).pop() || 'stock_picker.db';
      pills.push(`<span class="pill ok">SQLite ${esc(dbName)} · 备份 ${s.storage.count} 份</span>`);
    }
    $('#statusPills').innerHTML = pills.join('');
    return s;
  } catch (e) {
    $('#statusPills').innerHTML = `<span class="pill err">后端连接失败：${esc(e.message)}</span>`;
    return null;
  }
}

/* 版本自检：后端换了前端资源（＝重新打包 / 改了 static）就提示刷新
   ------------------------------------------------------------------
   页面加载时记下 /api/version 的资源指纹，之后定时比对；不一致说明这个
   标签页还跑着旧脚本。不提示的话用户会以为「代码改了没生效」——
   踩过一次：日期控件下界仍是旧的 250 个交易日，实际是新脚本没被加载。 */
async function loadVersion() {
  try {
    const v = await api('/api/version');
    const ver = v.asset_version || '';
    if (!state.assetVersion) { state.assetVersion = ver; return ver; }
    const stale = !!ver && ver !== state.assetVersion;
    const el = $('#staleBanner');
    if (el) el.hidden = !stale;
    return ver;
  } catch { return null; }
}

/* 指定日期：用原生日期控件，但只认交易日
   ------------------------------------------------------------------
   留空 = 最新交易日（此时「回看交易日」生效）；
   选中非交易日时，吸附到不晚于该日的最近交易日，并提示用户。
   这里必须拉**全部**交易日（n=0）——返回值的第一项会被写进 input.min，
   当作日历的可选下界；只拉最近 250 天的话，更早的年份会被整个灰掉。 */
async function loadTradeDates() {
  try {
    const d = await api('/api/trade_dates?n=0');
    state.tradeDates = d.dates || [];
    state.tradeDateSet = new Set(state.tradeDates);
    const input = $('#fDate');
    if (!state.tradeDates.length) return;
    const first = state.tradeDates[0];
    const last = state.tradeDates[state.tradeDates.length - 1];
    const tip = `可选 ${first} ~ ${last}（共 ${state.tradeDates.length} 个交易日）；留空＝最新交易日`;
    // 选股 / 连板梯队 / 板块交集三处的日期控件同源，一起设好边界
    [$('#fDate'), $('#sDate'), $('#xDate')].forEach((el) => {
      if (!el) return;
      el.min = first;
      el.max = last;
      el.title = tip;
    });
    // 数据刷新后，原先选中的日期可能已越界 → 自动纠正
    snapTradeDate(input, { notify: true });
  } catch { /* ignore */ }
}

/* 把非交易日吸附到不晚于该日的最近交易日；早于数据起点则清空（＝最新交易日） */
function snapTradeDate(input, { notify = false } = {}) {
  const v = input.value;
  if (!v || !state.tradeDates.length) return v;
  const known = state.tradeDateSet || (state.tradeDateSet = new Set(state.tradeDates));
  if (known.has(v)) return v;
  const prev = state.tradeDates.filter((x) => x < v).pop();
  if (prev) {
    input.value = prev;
    if (notify) toast(`${v} 非交易日，已调整为最近交易日 ${prev}`, 'warn');
    return prev;
  }
  input.value = '';
  if (notify) toast(`${v} 早于本地数据起点（${state.tradeDates[0]}），已回到最新交易日`, 'warn');
  return '';
}

/* 二级行业（通达信研究行业）下拉 */
async function loadIndustries() {
  try {
    const d = await api('/api/industries');
    state.industries = d.l2_names || [];
    state.industryTree = d.tree || {};
    state.industriesCounts = d.l2_counts || [];   // 板块交集页的行业选择器要用
    const sel = $('#fIndustry');
    // 按一级行业分组，便于在原生下拉里定位
    let html = '<option value="">全部行业</option>';
    Object.keys(state.industryTree).forEach((l1) => {
      const group = state.industryTree[l1] || [];
      if (!group.length) return;
      html += `<optgroup label="${esc(l1)}">`
        + group.map((l2) => `<option value="${esc(l2)}">${esc(l2)}</option>`).join('')
        + '</optgroup>';
    });
    sel.innerHTML = html;
    sel.value = state.indFilter || '';
    return d;
  } catch { return null; }
}

/* 概念（通达信概念板块）目录 + 输入框自动补全
   ------------------------------------------------------------------
   269 个概念全部塞进原生 select 会很难找，改用 datalist：输入即过滤，
   再按成员数降序喂给候选项 —— 越主流的题材越靠前。 */
async function loadConcepts() {
  try {
    const d = await api('/api/concepts');
    state.concepts = d.names || [];
    state.conceptItems = d.items || [];
    const dl = $('#conceptList');
    if (!dl) return d;
    const items = state.conceptItems
      .filter((x) => (x.n || 0) > 0)
      .sort((a, b) => (b.n || 0) - (a.n || 0));
    dl.innerHTML = items.map((x) => `<option value="${esc(x.name)}" label="${x.n} 只"></option>`).join('');
    return d;
  } catch { return null; }
}

/* 输入框里的概念（支持中文逗号 / 英文逗号 / 空格分隔） */
function conceptList() {
  const raw = ($('#fConcept') && $('#fConcept').value) || '';
  return raw.replace(/[，、]/g, ',').split(',').map((s) => s.trim()).filter(Boolean);
}

/* ------------------------------------------------------------------ */
/* 进度条                                                              */
/* ------------------------------------------------------------------ */
function showProgress(text, ratio) {
  $('#progressBar').hidden = false;
  $('#progressText').textContent = text;
  $('#progressFill').style.width = ratio === null || ratio === undefined ? '100%' : `${Math.round(ratio * 100)}%`;
}
function hideProgress(delay = 900) {
  setTimeout(() => { $('#progressBar').hidden = true; $('#progressFill').style.width = '0'; }, delay);
}

async function pollJob(jobId) {
  if (state.pollTimer) clearInterval(state.pollTimer);
  return new Promise((resolve) => {
    state.pollTimer = setInterval(async () => {
      let job;
      try {
        job = (await api(`/api/jobs/${jobId}`)).job;
      } catch {
        clearInterval(state.pollTimer); resolve(null); return;
      }
      const p = job.progress || {};
      if (job.status === 'running' || job.status === 'pending') {
        const ratio = p.total ? p.done / p.total : null;
        showProgress(p.message || '处理中…', p.phase === 'read' ? ratio : null);
      }
      if (job.status === 'done' || job.status === 'error') {
        clearInterval(state.pollTimer); resolve(job);
      }
    }, 700);
  });
}

async function doRefresh() {
  $('#btnRefresh').disabled = true;
  try {
    const { job_id } = await api('/api/data/refresh', { method: 'POST' });
    showProgress('开始刷新…', null);
    const job = await pollJob(job_id);
    if (job && job.status === 'done' && job.result && job.result.ok) {
      const r = job.result;
      toast(`已刷新：${r.codes_loaded} 只 / ${r.rows} 行，耗时 ${r.elapsed}s（最新 ${r.last_date}）`, 'ok');
      if (r.failed) toast(`${r.failed} 只读取失败（多为停牌/退市）`, 'warn');
      await loadStatus(); await loadTradeDates();
    } else {
      toast('刷新失败：' + ((job && (job.error || (job.result && job.result.reason))) || '未知错误'), 'err');
    }
  } catch (e) {
    toast('刷新失败：' + e.message, 'err');
  } finally {
    $('#btnRefresh').disabled = false;
    hideProgress();
  }
}

/* 启动预加载：等后端把本地已存在的数据全部载入，页面接管后自动选股          */
/* 后端可能在重建缓存（本地行情比缓存新），这里把进度显示出来，别让用户干等    */
async function waitBootstrap() {
  let b = null;
  try { b = await api('/api/bootstrap'); } catch { return null; }
  if (!b) return null;
  if (!b.running) {
    // 已经跑完（可能是页面开得晚）——失败也要让用户知道，别默默空着
    if (b.failed) toast('数据预加载失败：' + ((b.job && b.job.error) || '未知错误'), 'err', 6000);
    return b;
  }

  const f = b.freshness || {};
  showProgress(f.stale ? '本地行情比缓存新，正在重建缓存…' : '正在加载本地数据…', null);
  const job = await pollJob(b.job_id);
  hideProgress(400);
  const r = (job && job.result) || null;
  if (job && job.status === 'done' && r && r.ok) {
    toast(`数据已就绪：${r.codes_loaded} 只 / ${r.rows} 行 · ${r.last_date || '-'}`
      + (r.rebuilt ? `（重建缓存 ${r.elapsed}s）` : '（缓存已是最新）'), 'ok', 5000);
  } else if (job && job.status === 'error') {
    toast('数据预加载失败：' + (job.error || '未知错误'), 'err', 6000);
  } else if (r && r.ok === false) {
    toast('预加载未取到行情：' + (r.rebuild_error || r.cache_reason || ''), 'warn', 6000);
  }
  return b;
}

async function doVerify() {
  $('#btnVerify').disabled = true;
  showProgress('抽样比对 pytdx 与向量化解析…', null);
  try {
    const r = await api('/api/verify?sample=24', { method: 'POST' });
    if (r.ok) {
      toast(`一致性自检通过：${r.checked}/${r.requested} 个文件逐字段完全一致`, 'ok', 5000);
    } else if (r.reason) {
      toast('自检未完成：' + r.reason, 'warn');
    } else {
      toast(`发现 ${r.mismatches.length} 处不一致，请检查`, 'err', 6000);
      console.warn('reader mismatches', r.mismatches);
    }
  } catch (e) {
    toast('自检失败：' + e.message, 'err');
  } finally {
    $('#btnVerify').disabled = false;
    hideProgress();
  }
}

async function doReloadNames() {
  $('#btnNames').disabled = true;
  try {
    const r = await api('/api/names/reload', { method: 'POST' });
    toast(`名称索引已重建：${r.names.size} 条`, 'ok');
    await loadStatus();
  } catch (e) {
    toast('重建失败：' + e.message, 'err');
  } finally {
    $('#btnNames').disabled = false;
  }
}

/* 立刻生成一份 SQLite 一致性快照（计划/追踪不怕丢） */
async function doBackup() {
  const btn = $('#btnBackup');
  btn.disabled = true;
  try {
    const r = await api('/api/storage/backup', { method: 'POST' });
    const it = r.item || {};
    const kb = it.size ? (it.size / 1024).toFixed(1) : '0';
    toast(`已备份 ${kb} KB → ${it.file}（目录内共 ${r.count} 份）`, 'ok', 5000);
    await loadStatus();
  } catch (e) {
    toast('备份失败：' + e.message, 'err');
  } finally {
    btn.disabled = false;
  }
}

/* ------------------------------------------------------------------ */
/* 选股                                                                */
/* ------------------------------------------------------------------ */
function screenParams() {
  const markets = Array.from(document.querySelectorAll('.fMk:checked')).map((o) => o.value);
  const boards = Array.from(document.querySelectorAll('.fBd:checked')).map((o) => o.value);
  const date = $('#fDate').value;
  return {
    ma_window: Number($('#fMaWindow').value) || 20,
    volume_ratio: Number($('#fRatio').value) || 2,
    lookback_days: Number($('#fLookback').value) || 1,
    date: date || undefined,
    min_amount: (Number($('#fMinAmount').value) || 0) * 1e8,
    max_float_mcap: Number($('#fMaxFloatMcap').value) || 0,
    exclude_st: $('#fExcludeSt').checked,
    markets,
    boards,
    industries: state.indFilter ? [state.indFilter] : [],
    concepts: conceptList(),
    concept_mode: ($('#fConceptMode') && $('#fConceptMode').value) || 'any',
    sell_profit: (Number($('#fSellProfit').value) || 0) / 100,
    max_results: Number($('#fMaxResults').value) || 300,
  };
}

async function doScreen() {
  const btn = $('#btnScreen');
  snapTradeDate($('#fDate'), { notify: true });   // 兜底：手输日期后未失焦就点选股
  btn.disabled = true;
  btn.textContent = '选股中…';
  try {
    const data = await api('/api/screen', { method: 'POST', body: screenParams() });
    if (!data.ok) {
      toast('选股失败：' + (data.reason || '未知原因'), 'err');
      // 指定的日期不在本地数据覆盖范围内：直接清空，回到最新交易日
      if (data.date_out_of_range && data.date_min) {
        $('#fDate').value = '';
        $('#fDate').min = data.date_min;
        $('#fDate').max = data.date_max;
      }
      $('#conditions').innerHTML = '';
      state.rows = []; renderScreen();
      return;
    }
    state.rows = data.rows || [];
    state.indSummary = data.industry_summary || [];
    state.conceptSummary = data.concept_summary || [];
    state.conceptFilter = ($('#fConcept').value || '').trim();
    $('#conditions').innerHTML = (data.conditions || []).map((c) => `<li>${esc(c)}</li>`).join('');
    const ma = data.params.ma_window;
    const maTh = document.querySelector('#screenTable th[data-w="ma"]');
    maTh.dataset.label = `${ma}日均量(万手)`;   // 表头文案交给 renderSortHeaders 统一渲染
    const truncated = data.truncated ? `（共命中 ${data.total_hits} 只，已截取前 ${data.matched} 只）` : '';
    $('#screenMeta').textContent =
      `命中 ${data.matched} 只${truncated} · 扫描 ${data.total_scanned} 只`
      + (data.total_universe ? ` / 全市场 ${data.total_universe} 只` : '')
      + ` · 最新 ${data.last_date || '-'} · ${data.screen_elapsed || 0}s`;
    renderIndustrySummary(data);
    renderConceptSummary(data);
    renderSortHeaders();
    renderScreen();
    toast(`命中 ${data.matched} 只${data.truncated ? `（截取，共 ${data.total_hits} 只）` : ''}`, data.matched ? 'ok' : 'warn');
  } catch (e) {
    toast('选股失败：' + e.message, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = '开始选股';
  }
}

/* 二级行业分布：点击 chip 即按该行业过滤，再点取消
   注意：分布基数由后端给出（data.summary_base），是「除行业/概念筛选本身以外」
   全部条件的命中集 —— 所以点选某个行业后，其余行业 chip 依然在，可以连续切换。 */
function renderIndustrySummary(data) {
  const card = $('#indSummaryCard');
  const box = $('#indSummary');
  const groups = data.industry_summary || [];
  if (!groups.length) { card.hidden = true; box.innerHTML = ''; return; }
  card.hidden = false;
  const base = data.summary_base != null ? data.summary_base : data.matched;
  let top = groups.slice(0, 40);
  // 选中的行业若排在 40 名开外，提到最前，避免「筛了却看不见自己选的是哪个」
  if (state.indFilter && !top.some((g) => g.l2 === state.indFilter)) {
    const cur = groups.find((g) => g.l2 === state.indFilter);
    if (cur) top = [cur].concat(top);
  }
  $('#indSummaryMeta').textContent =
    `${base} 只命中覆盖 ${groups.length} 个二级行业`
    + (state.indFilter ? ` · 已筛「${state.indFilter}」→ ${data.total_hits} 只` : '')
    + (data.industry_unmapped ? ` · ${data.industry_unmapped} 只无行业分类` : '')
    + (data.industry_filtered_missing ? ` · 因缺分类被过滤 ${data.industry_filtered_missing} 只` : '');
  box.innerHTML = top.map((g) => `
    <button class="ind-chip${state.indFilter === g.l2 ? ' active' : ''}"
            data-l2="${esc(g.l2)}" title="点击按「${esc(g.l2)}」筛选；再点取消">
      ${esc(g.l2)} <b>${g.n}</b>
    </button>`).join('')
    + (groups.length > top.length ? `<span class="ind-more">…另有 ${groups.length - top.length} 个行业</span>` : '');
}

function bindIndustrySummary() {
  $('#indSummary').addEventListener('click', (ev) => {
    const chip = ev.target.closest('.ind-chip');
    if (!chip) return;
    const l2 = chip.dataset.l2;
    state.indFilter = state.indFilter === l2 ? '' : l2;
    $('#fIndustry').value = state.indFilter;
    doScreen();
  });
  $('#btnIndClear').onclick = () => {
    if (!state.indFilter) { toast('当前未设置行业筛选', 'warn'); return; }
    state.indFilter = '';
    $('#fIndustry').value = '';
    doScreen();
  };
}

/* 概念分布：命中股票的概念频次，点击 chip 即按该概念筛选
   基数同行业分布 —— 不受行业/概念筛选影响，所以点了一个概念后其余 chip 仍在。 */
function renderConceptSummary(data) {
  const card = $('#conceptSummaryCard');
  const box = $('#conceptSummary');
  const groups = data.concept_summary || [];
  if (!groups.length) { card.hidden = true; box.innerHTML = ''; return; }
  card.hidden = false;
  const base = data.summary_base != null ? data.summary_base : data.matched;
  const active = new Set(conceptList());
  let top = groups.slice(0, 40);
  // 已启用但排在 40 名开外的概念，提到最前
  active.forEach((name) => {
    if (!top.some((g) => g.concept === name)) {
      const cur = groups.find((g) => g.concept === name);
      if (cur) top = [cur].concat(top);
    }
  });
  $('#conceptSummaryMeta').textContent =
    `${base} 只命中覆盖 ${groups.length} 个概念`
    + (active.size ? ` · 已筛 ${active.size} 个概念 → ${data.total_hits} 只` : '')
    + ` · 平均每股 ${data.concept_avg ?? 0} 个`
    + (data.concept_unmapped ? ` · ${data.concept_unmapped} 只无概念分类` : '')
    + (data.concept_filtered_missing ? ` · 因缺分类被过滤 ${data.concept_filtered_missing} 只` : '');
  box.innerHTML = top.map((g) => `
    <button class="ind-chip${active.has(g.concept) ? ' active' : ''}"
            data-concept="${esc(g.concept)}" title="点击按「${esc(g.concept)}」筛选；再点取消">
      ${esc(g.concept)} <b>${g.n}</b>
    </button>`).join('')
    + (groups.length > top.length ? `<span class="ind-more">…另有 ${groups.length - top.length} 个概念</span>` : '');
}

function bindConceptSummary() {
  $('#conceptSummary').addEventListener('click', (ev) => {
    const chip = ev.target.closest('.ind-chip');
    if (!chip) return;
    const name = chip.dataset.concept;
    const cur = conceptList();
    // chip 是单选语义：点它就是「只看这个概念」，再点同一个取消
    const next = cur.length === 1 && cur[0] === name ? [] : [name];
    $('#fConcept').value = next.join('，');
    if (next.length) $('#fConceptMode').value = 'any';
    doScreen();
  });
  $('#btnConceptClear').onclick = () => {
    if (!conceptList().length) { toast('当前未设置概念筛选', 'warn'); return; }
    $('#fConcept').value = '';
    doScreen();
  };
}

/* ---- 排序（选股与连板梯队共用同一套比较规则） --------------------- */
/* 返回渲染顺序（下标数组）。不改动原数组，
   保证行内买卖价输入框、抽屉、加入计划都还按原下标取数。 */
function orderBy(rows, sort, cols) {
  const n = rows.length;
  const idx = Array.from({ length: n }, (_, i) => i);
  const spec = cols[sort.key];
  if (!spec) return idx;
  const sign = sort.dir === 'asc' ? 1 : -1;
  const missing = (v) => v === null || v === undefined || v === ''
    || (spec.type === 'num' && Number.isNaN(Number(v)));
  idx.sort((a, b) => {
    const va = spec.get(rows[a]);
    const vb = spec.get(rows[b]);
    const na = missing(va);
    const nb = missing(vb);
    if (na && nb) return a - b;
    if (na) return 1;            // 缺值永远沉底，不随方向翻转
    if (nb) return -1;
    const c = spec.type === 'num'
      ? Number(va) - Number(vb)
      : String(va).localeCompare(String(vb), 'zh-Hans-CN');
    return c === 0 ? a - b : c * sign;   // 同值保持后端原序（稳定）
  });
  return idx;
}

function sortOrder() {
  return orderBy(state.rows, state.sort, SORT_COLS);
}

/* 表头状态：排序列表头加箭头 + 高亮，其余恢复原样 */
function paintSortHeaders(tableSel, sort) {
  const table = document.querySelector(tableSel);
  if (!table) return;
  table.querySelectorAll('thead th[data-sort]').forEach((th) => {
    const key = th.dataset.sort;
    if (!th.dataset.label) th.dataset.label = th.textContent.trim();
    const base = th.dataset.label;
    const active = key === sort.key;
    th.classList.toggle('sorted', active);
    th.setAttribute('aria-sort',
      active ? (sort.dir === 'desc' ? 'descending' : 'ascending') : 'none');
    th.textContent = active ? `${base} ${sort.dir === 'desc' ? '↓' : '↑'}` : base;
  });
}

/* 绑定表头点击：同列再点反向；换列时按列类型决定首次方向（文字升序、数字降序） */
function bindSortHead(tableSel, sort, cols, onChange) {
  const table = document.querySelector(tableSel);
  if (!table) return;
  table.querySelector('thead').addEventListener('click', (ev) => {
    const th = ev.target.closest('th[data-sort]');
    if (!th) return;
    const key = th.dataset.sort;
    if (sort.key === key) {
      sort.dir = sort.dir === 'desc' ? 'asc' : 'desc';
    } else {
      sort.key = key;
      sort.dir = cols[key] && cols[key].type === 'text' ? 'asc' : 'desc';
    }
    onChange();
  });
}

function renderSortHeaders() {
  paintSortHeaders('#screenTable', state.sort);
  updateSortHint();
}

function updateSortHint() {
  const el = $('#sortHint');
  if (!el) return;
  const th = document.querySelector(`#screenTable thead th[data-sort="${state.sort.key}"]`);
  const label = th ? (th.dataset.label || state.sort.key) : state.sort.key;
  el.textContent = `点行看 K 线 · 排序：${label} ${state.sort.dir === 'desc' ? '降序' : '升序'}`;
  el.title = `点击表头排序，再点同一列反向；当前按「${label}」${state.sort.dir === 'desc' ? '降序' : '升序'}`;
  const btn = $('#btnSortReset');
  if (btn) {
    btn.hidden = state.sort.key === DEFAULT_SORT.key && state.sort.dir === DEFAULT_SORT.dir;
  }
}

function bindScreenSort() {
  bindSortHead('#screenTable', state.sort, SORT_COLS, () => {
    renderSortHeaders();
    renderScreen();
  });
}

function renderScreen() {
  const tb = $('#screenTable tbody');
  if (!state.rows.length) {
    tb.innerHTML = '<tr><td colspan="14" class="empty">暂无结果，点击「开始选股」</td></tr>';
    return;
  }
  tb.innerHTML = sortOrder().map((i) => {
    const r = state.rows[i];
    const cpts = r.concepts || [];
    const cptHead = cpts.slice(0, 2);
    const cptTitle = cpts.length
      ? `${cpts.join('、')}\n（共 ${cpts.length} 个概念）`
      : '本地板块文件未收录该标的概念';
    const cptHtml = cpts.length
      ? cptHead.map((c) => `<span class="cpt">${esc(c)}</span>`).join('')
        + (cpts.length > cptHead.length
          ? `<span class="cpt-more" title="${esc(cptTitle)}">+${cpts.length - cptHead.length}</span>` : '')
      : '<span class="cpt-empty">—</span>';
    return `
    <tr class="clickable" data-i="${i}">
      <td class="code" title="${esc((r.board || '') + ' ' + (r.market || ''))}">${esc(r.code)}</td>
      <td><b>${esc(r.name || '—')}</b></td>
      <td><span class="ind" title="${esc([r.industry_l1, r.industry_l2, r.industry_l3].filter(Boolean).join(' / ') || '无行业分类')}">${esc(r.industry_l2 || '—')}</span></td>
      <td class="cpt-cell" title="${esc(cptTitle)}">${cptHtml}</td>
      <td>${esc(r.signal_date)}</td>
      <td class="num">${fmt(r.close, 2)}</td>
      <td class="num ${pctClass(r.pct_change)}">${fmtPct(r.pct_change)}</td>
      <td class="num"><b class="up">${fmt(r.vol_ratio, 2)}×</b></td>
      <td class="num" title="${r.float_shares_yi != null ? `流通股本 ${fmt(r.float_shares_yi, 2)} 亿股` + (r.total_mcap_yi != null ? ` / 总市值 ${fmt(r.total_mcap_yi, 1)} 亿` : '') : '缺流通股本'}">${fmt(r.float_mcap_yi, 1)}</td>
      <td class="num"><input class="cell buy" data-i="${i}" value="${fmt(r.buy_price, 2)}" /></td>
      <td class="num"><input class="cell sell" data-i="${i}" value="${fmt(r.sell_price, 2)}" /></td>
      <td class="num">${fmtWan(r.volume_hand)}</td>
      <td class="num">${fmtWan(r.ma_volume / 100)}</td>
      <td class="num">${fmt(r.amount_yi, 2)}</td>
    </tr>`;
  }).join('');
}

function bindScreenTable() {
  const tb = $('#screenTable tbody');
  tb.addEventListener('click', (ev) => {
    const btn = ev.target.closest('button[data-act]');
    if (btn) {
      ev.stopPropagation();
      const i = Number(btn.dataset.i);
      const row = state.rows[i];
      if (btn.dataset.act === 'kline') openDrawer(row);
      if (btn.dataset.act === 'plan') addPlan(row, i);
      if (btn.dataset.act === 'watch') addWatch(row, i);
      return;
    }
    const tr = ev.target.closest('tr[data-i]');
    if (tr && !ev.target.closest('input')) openDrawer(state.rows[Number(tr.dataset.i)]);
  });
  tb.addEventListener('input', (ev) => {
    const inp = ev.target.closest('input.cell');
    if (!inp) return;
    const i = Number(inp.dataset.i);
    const v = Number(inp.value);
    if (inp.classList.contains('buy')) state.rows[i].buy_price = v;
    if (inp.classList.contains('sell')) state.rows[i].sell_price = v;
  });
}

/* ------------------------------------------------------------------ */
/* 计划 / 追踪 写入                                                    */
/* ------------------------------------------------------------------ */
function planPayload(r, i) {
  const row = state.rows[i] || r;
  return {
    code: r.code,
    name: r.name,
    signal_date: r.signal_date,
    base_close: r.close,
    volume: r.volume,
    ma_volume: r.ma_volume,
    vol_ratio: r.vol_ratio,
    buy_price: Number(row.buy_price ?? r.buy_price),
    sell_price: Number(row.sell_price ?? r.sell_price),
    status: '待买入',
  };
}

async function addPlan(r, i) {
  try {
    await api('/api/plans', { method: 'POST', body: planPayload(r, i) });
    toast(`${r.code} ${r.name || ''} 已加入计划`, 'ok');
    await Promise.all([loadPlans(), loadCounts()]);
  } catch (e) { toast('加入计划失败：' + e.message, 'err'); }
}

async function addWatch(r, i) {
  const p = planPayload(r, i);
  try {
    await api('/api/watchlist', {
      method: 'POST',
      body: {
        code: p.code, name: p.name, signal_date: p.signal_date, base_close: p.base_close,
        buy_price: p.buy_price, sell_price: p.sell_price, status: '关注中',
      },
    });
    toast(`${r.code} ${r.name || ''} 已加入追踪`, 'ok');
    await Promise.all([loadWatch(), loadCounts()]);
  } catch (e) { toast('加入追踪失败：' + e.message, 'err'); }
}

async function planAll() {
  if (!state.rows.length) { toast('没有可加入的结果', 'warn'); return; }
  let ok = 0;
  for (let i = 0; i < state.rows.length; i++) {
    try { await api('/api/plans', { method: 'POST', body: planPayload(state.rows[i], i) }); ok++; }
    catch { /* 跳过失败项 */ }
  }
  toast(`已加入计划 ${ok}/${state.rows.length} 只`, 'ok');
  await Promise.all([loadPlans(), loadCounts()]);
}

function exportCsv() {
  if (!state.rows.length) { toast('没有可导出的结果', 'warn'); return; }
  const cols = ['code', 'name', 'board', 'market', 'industry_l1', 'industry_l2', 'industry_l3',
    'concepts', 'concept_n', 'signal_date', 'close', 'pct_change', 'volume_hand', 'ma_volume',
    'vol_ratio', 'amount_yi', 'float_mcap_yi', 'float_shares_yi', 'total_mcap_yi',
    'buy_price', 'sell_price'];
  const head = ['代码', '名称', '板块', '交易所', '一级行业', '二级行业', '三级行业',
    '概念', '概念数', '信号日', '收盘价', '涨跌幅', '成交量(手)', '均量(股)', '量比', '成交额(亿)',
    '流通市值(亿)', '流通股本(亿股)', '总市值(亿)', '计划买入价', '计划卖出价'];
  const lines = [head.join(',')];
  sortOrder().forEach((i) => {          // 按当前表头排序导出，与屏幕所见一致
    const r = state.rows[i];
    lines.push(cols.map((c) => {
      if (c === 'buy_price') return Number(r.buy_price);
      if (c === 'sell_price') return Number(r.sell_price);
      // 概念名之间用顿号连接，避免与 CSV 的分隔逗号冲突
      if (c === 'concepts') return (r.concepts || []).join('、');
      if (c === 'concept_n') return (r.concepts || []).length;
      const v = r[c];
      return v === null || v === undefined ? '' : v;
    }).join(','));
  });
  const blob = new Blob(['\uFEFF' + lines.join('\n')], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `volume_surge_${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
}

/* ------------------------------------------------------------------ */
/* 计划表                                                              */
/* ------------------------------------------------------------------ */
async function loadPlans() {
  try {
    const d = await api('/api/plans');
    state.plans = d.items || [];
    renderPlans();
  } catch (e) { toast('加载计划失败：' + e.message, 'err'); }
}

/* 行业 + 概念合并成一个 tooltip（计划/追踪表列已经很宽，不再单独开概念列） */
function indConceptTitle(item) {
  const ind = [item.industry_l1, item.industry_l2, item.industry_l3]
    .filter(Boolean).join(' / ') || '无行业分类';
  const cpts = item.concepts || [];
  return cpts.length
    ? `${ind}\n概念 ${cpts.length} 个：${cpts.join('、')}`
    : `${ind}\n（无概念分类）`;
}

function renderPlans() {
  const tb = $('#planTable tbody');
  $('#planMeta').textContent = `共 ${state.plans.length} 条`;
  if (!state.plans.length) {
    tb.innerHTML = '<tr><td colspan="14" class="empty">暂无计划</td></tr>';
    return;
  }
  tb.innerHTML = state.plans.map((p) => {
    const v = p.valuation || {};
    const toBuy = v.reach_buy_pct === null || v.reach_buy_pct === undefined ? '-' : fmtPct(v.reach_buy_pct);
    return `
    <tr data-id="${p.id}">
      <td class="code">${esc(p.code)}</td>
      <td>${esc(p.name || '—')}</td>
      <td><span class="ind" title="${esc(indConceptTitle(p))}">${esc(p.industry_l2 || '—')}</span></td>
      <td>${esc(p.signal_date)}</td>
      <td class="num">${fmt(p.base_close, 2)}</td>
      <td class="num">${fmt(p.vol_ratio, 2)}×</td>
      <td class="num"><input class="cell f-buy" value="${fmt(p.buy_price, 2)}" /></td>
      <td class="num"><input class="cell f-sell" value="${fmt(p.sell_price, 2)}" /></td>
      <td class="num"><input class="cell f-qty" value="${p.qty ?? ''}" /></td>
      <td class="num">${fmt(v.latest_close, 2)}</td>
      <td class="num ${pctClass(v.reach_buy_pct)}">${toBuy}</td>
      <td>
        <select class="f-status">
          ${['待买入', '已买入', '已卖出', '已过期', '放弃'].map((s) => `<option ${p.status === s ? 'selected' : ''}>${s}</option>`).join('')}
        </select>
      </td>
      <td><input class="cell f-note" value="${esc(p.note || '')}" style="width:100px" /></td>
      <td>
        <button class="btn small" data-act="save">保存</button>
        <button class="btn small" data-act="kline">K线</button>
        <button class="btn small" data-act="towatch">转追踪</button>
        <button class="btn small danger" data-act="del">删除</button>
      </td>
    </tr>`;
  }).join('');
}

function bindPlanTable() {
  $('#planTable tbody').addEventListener('click', async (ev) => {
    const btn = ev.target.closest('button[data-act]');
    if (!btn) return;
    const tr = btn.closest('tr');
    const id = Number(tr.dataset.id);
    const act = btn.dataset.act;
    if (act === 'kline') {
      const p = state.plans.find((x) => x.id === id);
      openDrawer({ code: p.code, name: p.name, signal_date: p.signal_date, close: p.base_close,
        buy_price: p.buy_price, sell_price: p.sell_price, industry_l1: p.industry_l1,
        industry_l2: p.industry_l2, industry_l3: p.industry_l3, concepts: p.concepts });
      return;
    }
    if (act === 'save') {
      const patch = {
        buy_price: Number(tr.querySelector('.f-buy').value) || null,
        sell_price: Number(tr.querySelector('.f-sell').value) || null,
        qty: Number(tr.querySelector('.f-qty').value) || null,
        status: tr.querySelector('.f-status').value,
        note: tr.querySelector('.f-note').value,
      };
      try { await api(`/api/plans/${id}`, { method: 'PATCH', body: patch }); toast('已保存', 'ok'); await loadPlans(); }
      catch (e) { toast('保存失败：' + e.message, 'err'); }
      return;
    }
    if (act === 'towatch') {
      try { await api(`/api/plans/${id}/to_watch`, { method: 'POST' }); toast('已转入追踪', 'ok'); await Promise.all([loadWatch(), loadCounts()]); }
      catch (e) { toast('转追踪失败：' + e.message, 'err'); }
      return;
    }
    if (act === 'del') {
      try { await api(`/api/plans/${id}`, { method: 'DELETE' }); toast('已删除', 'ok'); await Promise.all([loadPlans(), loadCounts()]); }
      catch (e) { toast('删除失败：' + e.message, 'err'); }
    }
  });
}

/* ------------------------------------------------------------------ */
/* 追踪表                                                              */
/* ------------------------------------------------------------------ */
async function loadWatch() {
  try {
    const d = await api('/api/watchlist');
    state.watch = d.items || [];
    renderWatch();
  } catch (e) { toast('加载追踪失败：' + e.message, 'err'); }
}

function renderWatch() {
  const tb = $('#watchTable tbody');
  $('#watchMeta').textContent = `共 ${state.watch.length} 条`;
  if (!state.watch.length) {
    tb.innerHTML = '<tr><td colspan="15" class="empty">暂无追踪</td></tr>';
    return;
  }
  tb.innerHTML = state.watch.map((w) => {
    const v = w.valuation || {};
    const ret = v.ret_pct;
    return `
    <tr data-id="${w.id}">
      <td class="code">${esc(w.code)}</td>
      <td>${esc(w.name || '—')}</td>
      <td><span class="ind" title="${esc(indConceptTitle(w))}">${esc(w.industry_l2 || '—')}</span></td>
      <td>${esc(w.signal_date)}</td>
      <td class="num"><input class="cell w-buy" value="${fmt(w.buy_price, 2)}" /></td>
      <td class="num"><input class="cell w-sell" value="${fmt(w.sell_price, 2)}" /></td>
      <td class="num"><input class="cell w-qty" value="${w.qty ?? ''}" style="width:60px" /></td>
      <td class="num">${fmt(v.latest_close, 2)}</td>
      <td class="num">${fmt(v.low_since, 2)}</td>
      <td class="num">${fmt(v.high_since, 2)}</td>
      <td>${v.touched_buy ? '<span class="tag buy">已到</span>' : '<span class="tag wait">未到</span>'}</td>
      <td>${v.hit_target ? '<span class="tag ok">已达标</span>' : '<span class="tag wait">未达标</span>'}</td>
      <td class="num ${pctClass(ret)}">${ret === undefined || ret === null ? '-' : fmtPct(ret)}</td>
      <td>
        <select class="w-status">
          ${['关注中', '已买入', '已止盈', '已止损', '已过期', '放弃'].map((s) => `<option ${w.status === s ? 'selected' : ''}>${s}</option>`).join('')}
        </select>
      </td>
      <td>
        <button class="btn small" data-act="save">保存</button>
        <button class="btn small" data-act="entry">记买入</button>
        <button class="btn small" data-act="kline">K线</button>
        <button class="btn small danger" data-act="del">删除</button>
      </td>
    </tr>`;
  }).join('');
}

function bindWatchTable() {
  $('#watchTable tbody').addEventListener('click', async (ev) => {
    const btn = ev.target.closest('button[data-act]');
    if (!btn) return;
    const tr = btn.closest('tr');
    const id = Number(tr.dataset.id);
    const act = btn.dataset.act;
    const item = state.watch.find((x) => x.id === id);
    if (act === 'kline') {
      openDrawer({ code: item.code, name: item.name, signal_date: item.signal_date,
        close: item.base_close, buy_price: item.buy_price, sell_price: item.sell_price,
        industry_l1: item.industry_l1, industry_l2: item.industry_l2,
        industry_l3: item.industry_l3, concepts: item.concepts });
      return;
    }
    if (act === 'save') {
      const patch = {
        buy_price: Number(tr.querySelector('.w-buy').value) || null,
        sell_price: Number(tr.querySelector('.w-sell').value) || null,
        qty: Number(tr.querySelector('.w-qty').value) || null,
        status: tr.querySelector('.w-status').value,
      };
      try { await api(`/api/watchlist/${id}`, { method: 'PATCH', body: patch }); toast('已保存', 'ok'); await loadWatch(); }
      catch (e) { toast('保存失败：' + e.message, 'err'); }
      return;
    }
    if (act === 'entry') {
      const price = prompt('实际买入价（元）', fmt((item.valuation || {}).latest_close ?? item.buy_price, 2));
      if (price === null) return;
      const date = prompt('买入日期（YYYY-MM-DD）', (item.valuation || {}).latest_date || '');
      if (date === null) return;
      try {
        await api(`/api/watchlist/${id}`, {
          method: 'PATCH',
          body: { entry_price: Number(price), entry_date: date, status: '已买入' },
        });
        toast('已记录买入', 'ok');
        await loadWatch();
      } catch (e) { toast('记录失败：' + e.message, 'err'); }
      return;
    }
    if (act === 'del') {
      try { await api(`/api/watchlist/${id}`, { method: 'DELETE' }); toast('已删除', 'ok'); await Promise.all([loadWatch(), loadCounts()]); }
      catch (e) { toast('删除失败：' + e.message, 'err'); }
    }
  });
}

async function loadCounts() {
  try {
    const s = await api('/api/stats');
    $('#planCount').textContent = s.plans;
    $('#watchCount').textContent = s.watch;
  } catch { /* ignore */ }
}

/* ------------------------------------------------------------------ */
/* 连板梯队                                                            */
/* ------------------------------------------------------------------ */
/* 涨停判定与连板数全在后端算好（limits.py），前端只做展示与筛选。
   行业 / 概念筛选在本地做 —— 数据只有几十到几百条，重新请求反而更慢。 */

function streakParams() {
  const boards = Array.from(document.querySelectorAll('.sBd:checked')).map((o) => o.value);
  if (!boards.length) { toast('请至少选择一个板块', 'warn'); return null; }
  const q = new URLSearchParams();
  const date = $('#sDate').value;
  if (date) q.set('date', date);
  q.set('min_streak', String(Number($('#sMinStreak').value) || 1));
  q.set('st_limit', $('#sStLimit').checked ? 'true' : 'false');
  q.set('boards', boards.join(','));
  return q.toString();
}

async function loadStreaks() {
  const q = streakParams();
  if (!q) return;
  const btn = $('#btnStreak');
  btn.disabled = true;
  btn.textContent = '加载中…';
  try {
    const d = await api('/api/streaks?' + q);
    if (!d.ok) {
      toast('加载失败：' + (d.reason || '未知原因'), 'err');
      if (d.date_out_of_range && d.date_min) {
        $('#sDate').value = '';
        $('#sDate').min = d.date_min;
        $('#sDate').max = d.date_max;
      }
      return;
    }
    state.streak = d;
    state.streakRows = d.rows || [];
    state.streakIndFilter = '';
    state.streakCptFilter = '';
    $('#streakConditions').innerHTML =
      (d.conditions || []).map((c) => `<li>${esc(c)}</li>`).join('');
    renderStreakKpi(d);
    renderStreakLadder(d);
    renderStreakChart(d);
    renderStreakSummary(d);
    paintSortHeaders('#streakTable', state.streakSort);
    updateStreakSortUI();
    renderStreakTable();
    const m = d.metrics || {};
    toast(`${d.date} 涨停 ${m.limit_up} 只 · 最高 ${m.max_streak} 板 · ${d.elapsed}s`, 'ok');
  } catch (e) {
    toast('加载失败：' + e.message, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = '加载梯队';
  }
}

function renderStreakKpi(d) {
  const m = d.metrics || {};
  $('#streakKpiCard').hidden = false;
  const promo = m.promotion == null ? '—' : (m.promotion * 100).toFixed(1) + '%';
  const items = [
    { k: `涨停家数 · ${d.date} 周${d.weekday}`, v: `${m.limit_up}`, cls: 'hot',
      s: `前一日 ${m.prev_limit_up} 只 · 首板 ${m.first} 只` },
    { k: '连板股（2 板及以上）', v: `${m.second + m.high3}`, cls: 'hot',
      s: `2 板 ${m.second} · 3 板+ ${m.high3}` },
    { k: '最高连板', v: `${m.max_streak}板`, cls: 'hot',
      s: m.max_streak_name ? `${m.max_streak_name}（${m.max_streak_code}）` : '—' },
    { k: '晋级率', v: promo, cls: 'warn',
      s: `昨日涨停 ${m.prev_limit_up} 只 → 今日晋级 ${m.promote_n} 只` },
    { k: '一字板', v: `${m.one_word}`, cls: '',
      s: `全天未打开 · ST ${m.st_count} 只` },
    { k: '炸板 / 跌停', v: `${m.touched_up} / ${m.limit_down}`, cls: 'cool',
      s: '触板未封 / 跌停' },
  ];
  $('#streakKpi').innerHTML = items.map((it) => `
    <div class="kpi ${it.cls}">
      <div class="k">${esc(it.k)}</div>
      <div class="v">${esc(it.v)}</div>
      <div class="s">${esc(it.s)}</div>
    </div>`).join('');
}

/* 梯队视图：最高板在最上、逐层向下；每只股票一个标签，点击看 K 线 */
function renderStreakLadder(d) {
  const card = $('#streakLadderCard');
  const box = $('#streakLadder');
  const hist = d.hist || [];
  if (!hist.length) {
    card.hidden = true;
    box.innerHTML = '';
    return;
  }
  card.hidden = false;
  const rows = d.rows || [];
  const minStreak = d.min_streak || 1;
  const shown = hist.filter((h) => h.streak >= minStreak);
  const m = d.metrics || {};
  $('#streakLadderMeta').textContent =
    `${d.date} 周${d.weekday} · 最高 ${m.max_streak} 板 · 涨停 ${m.limit_up} 只`
    + (minStreak > 1 ? ` · 只显示 ${minStreak} 板及以上` : '');

  box.innerHTML = shown.map((h, li) => {
    const items = rows.filter((r) => r.streak === h.streak);
    const chips = items.map((r) => {
      const cpts = (r.concepts || []).slice(0, 8).join('、') || '—';
      const tip = [
        `${r.code} ${r.name} · ${r.streak_label} · ${r.seal} · ${r.board}（${r.limit_rate}）`,
        `收盘 ${fmt(r.close, 2)} · 涨跌 ${fmtPct(r.pct_change)} · 成交额 ${fmt(r.amount_yi, 2)} 亿`,
        `流通市值 ${fmt(r.float_mcap_yi, 1)} 亿 · ${r.industry_l2 || '无行业分类'}`,
        `概念：${cpts}`,
        '点击查看 K 线',
      ].join('\n');
      return `
      <button type="button" class="ladder-item${r.is_st ? ' st' : ''}"
              data-code="${esc(r.code)}" data-seal="${esc(r.seal)}" title="${esc(tip)}">
        <b>${esc(r.name)}</b><span class="tag">${esc(String(r.seal).replace('板', ''))}</span>
      </button>`;
    }).join('');
    return `
    <div class="ladder-row${li === 0 ? ' top' : ''}${h.streak === 1 ? ' first' : ''}">
      <div class="ladder-tag">
        <span class="lv">${h.streak === 1 ? '首板' : h.streak + '板'}</span>
        <span class="cnt">${h.n} 只 · ${fmt(h.amount_yi, 1)}亿</span>
      </div>
      <div class="ladder-items">${chips || '<span class="ladder-empty">—</span>'}</div>
    </div>`;
  }).join('');
}

function renderStreakChart(d) {
  const hist = d.hist || [];
  const card = $('#streakChartCard');
  if (!hist.length) {
    card.hidden = true;
    return;
  }
  card.hidden = false;
  const el = $('#streakChart');
  if (!state.streakChart) state.streakChart = echarts.init(el);
  const chart = state.streakChart;
  const asc = hist.slice().reverse();          // 横轴从低到高更符合直觉
  const total = hist.reduce((a, b) => a + b.n, 0);
  chart.setOption({
    grid: { left: 58, right: 26, top: 26, bottom: 32 },
    tooltip: {
      trigger: 'axis', axisPointer: { type: 'shadow' },
      backgroundColor: C.glass, borderColor: C.line,
      textStyle: { color: C.text, fontSize: 12 },
      formatter: (ps) => {
        const p = ps[0];
        const h = asc[p.dataIndex];
        const pct = total ? ((h.n / total) * 100).toFixed(1) : '0';
        return `${h.label}<br/>${h.n} 只（占 ${pct}%）<br/>成交额 ${fmt(h.amount_yi, 1)} 亿`;
      },
    },
    xAxis: {
      type: 'category', data: asc.map((h) => h.label),
      axisLine: { lineStyle: { color: C.line } },
      axisTick: { show: false },
      axisLabel: { color: C.text2, fontSize: 11 },
    },
    yAxis: {
      type: 'value', name: '家数',
      nameTextStyle: { color: C.muted, fontSize: 11 },
      splitLine: { lineStyle: { color: C.grid } },
      axisLabel: { color: C.text2, fontSize: 11 },
      minInterval: 1,
    },
    series: [{
      type: 'bar', data: asc.map((h) => h.n), barMaxWidth: 36,
      itemStyle: { color: UP, borderRadius: [3, 3, 0, 0] },
      label: { show: true, position: 'top', color: C.text, fontSize: 11 },
    }],
  }, true);
  chart.resize();
  setTimeout(() => chart.resize(), 60);
  $('#streakChartMeta').textContent = `${d.date} · 合计 ${total} 只涨停`;
}

/* 涨停股的行业 / 概念分布：点标签即筛选，再点取消（本地过滤，不重新请求） */
function renderStreakSummary(d) {
  const ind = d.industry_summary || [];
  const indCard = $('#streakIndCard');
  if (!ind.length) {
    indCard.hidden = true;
  } else {
    indCard.hidden = false;
    $('#streakIndMeta').textContent =
      `${d.summary_base} 只涨停覆盖 ${ind.length} 个二级行业`
      + (state.streakIndFilter ? ` · 已筛「${state.streakIndFilter}」` : '')
      + (d.industry_unmapped ? ` · ${d.industry_unmapped} 只无行业分类` : '');
    $('#streakIndSummary').innerHTML = ind.slice(0, 40).map((g) => `
      <button class="ind-chip${state.streakIndFilter === g.l2 ? ' active' : ''}"
              data-l2="${esc(g.l2)}" title="点击按「${esc(g.l2)}」筛选；再点取消">
        ${esc(g.l2)} <b>${g.n}</b>
      </button>`).join('')
      + (ind.length > 40 ? `<span class="ind-more">…另有 ${ind.length - 40} 个行业</span>` : '');
  }

  const cpt = d.concept_summary || [];
  const cptCard = $('#streakCptCard');
  if (!cpt.length) {
    cptCard.hidden = true;
  } else {
    cptCard.hidden = false;
    $('#streakCptMeta').textContent =
      `${d.summary_base} 只涨停覆盖 ${cpt.length} 个概念`
      + (state.streakCptFilter ? ` · 已筛「${state.streakCptFilter}」` : '')
      + ` · 平均每股 ${d.concept_avg ?? 0} 个`;
    $('#streakCptSummary').innerHTML = cpt.slice(0, 40).map((g) => `
      <button class="ind-chip${state.streakCptFilter === g.concept ? ' active' : ''}"
              data-concept="${esc(g.concept)}" title="点击按「${esc(g.concept)}」筛选；再点取消">
        ${esc(g.concept)} <b>${g.n}</b>
      </button>`).join('')
      + (cpt.length > 40 ? `<span class="ind-more">…另有 ${cpt.length - 40} 个概念</span>` : '');
  }
}

function bindStreakSummary() {
  $('#streakIndSummary').addEventListener('click', (ev) => {
    const chip = ev.target.closest('.ind-chip');
    if (!chip) return;
    const l2 = chip.dataset.l2;
    state.streakIndFilter = state.streakIndFilter === l2 ? '' : l2;
    renderStreakSummary(state.streak);
    renderStreakTable();
  });
  $('#btnStreakIndClear').onclick = () => {
    if (!state.streakIndFilter) { toast('当前未设置行业筛选', 'warn'); return; }
    state.streakIndFilter = '';
    renderStreakSummary(state.streak);
    renderStreakTable();
  };
  $('#streakCptSummary').addEventListener('click', (ev) => {
    const chip = ev.target.closest('.ind-chip');
    if (!chip) return;
    const name = chip.dataset.concept;
    state.streakCptFilter = state.streakCptFilter === name ? '' : name;
    renderStreakSummary(state.streak);
    renderStreakTable();
  });
  $('#btnStreakCptClear').onclick = () => {
    if (!state.streakCptFilter) { toast('当前未设置概念筛选', 'warn'); return; }
    state.streakCptFilter = '';
    renderStreakSummary(state.streak);
    renderStreakTable();
  };
}

/* 行业 / 概念筛选后的明细 */
function streakFiltered() {
  let rows = state.streakRows;
  if (state.streakIndFilter) rows = rows.filter((r) => r.industry_l2 === state.streakIndFilter);
  if (state.streakCptFilter) {
    rows = rows.filter((r) => (r.concepts || []).includes(state.streakCptFilter));
  }
  return rows;
}

function renderStreakTable() {
  const tb = $('#streakTable tbody');
  if (!tb) return;
  const rows = streakFiltered();
  if (!rows.length) {
    tb.innerHTML = '<tr><td colspan="12" class="empty">暂无涨停个股</td></tr>';
    $('#streakMeta').textContent = '';
    return;
  }
  const order = orderBy(rows, state.streakSort, STREAK_SORT_COLS);
  tb.innerHTML = order.map((i) => {
    const r = rows[i];
    const cpts = r.concepts || [];
    const cptHead = cpts.slice(0, 2);
    const cptTitle = cpts.length
      ? `${cpts.join('、')}\n（共 ${cpts.length} 个概念）`
      : '本地板块文件未收录该标的概念';
    const cptHtml = cpts.length
      ? cptHead.map((c) => `<span class="cpt">${esc(c)}</span>`).join('')
        + (cpts.length > cptHead.length
          ? `<span class="cpt-more" title="${esc(cptTitle)}">+${cpts.length - cptHead.length}</span>` : '')
      : '<span class="cpt-empty">—</span>';
    const tag = r.is_st ? ' <span class="muted">ST</span>' : '';
    return `
    <tr class="clickable" data-code="${esc(r.code)}">
      <td><b class="up">${esc(r.streak_label)}</b></td>
      <td class="code">${esc(r.symbol)}</td>
      <td><b>${esc(r.name)}</b>${tag}</td>
      <td>${esc(r.seal)}</td>
      <td><span class="ind" title="${esc([r.industry_l1, r.industry_l2, r.industry_l3].filter(Boolean).join(' / ') || '无行业分类')}">${esc(r.industry_l2 || '—')}</span></td>
      <td class="cpt-cell" title="${esc(cptTitle)}">${cptHtml}</td>
      <td class="num">${fmt(r.close, 2)}</td>
      <td class="num ${pctClass(r.pct_change)}">${fmtPct(r.pct_change)}</td>
      <td class="num">${fmt(r.amount_yi, 2)}</td>
      <td class="num">${fmt(r.float_mcap_yi, 1)}</td>
      <td>${esc(r.limit_rate)}${tag}</td>
      <td>${esc(r.board)}</td>
    </tr>`;
  }).join('');
  const extra = rows.length !== state.streakRows.length
    ? `（共 ${state.streakRows.length} 只，已按板块筛选）` : '';
  $('#streakMeta').textContent = `${rows.length} 只${extra}`;
}

function updateStreakSortUI() {
  const el = $('#streakSortHint');
  if (!el) return;
  const th = document.querySelector(`#streakTable thead th[data-sort="${state.streakSort.key}"]`);
  const label = th ? (th.dataset.label || state.streakSort.key) : state.streakSort.key;
  const dir = state.streakSort.dir === 'desc' ? '降序' : '升序';
  el.textContent = `点行看 K 线 · 排序：${label} ${dir}`;
  el.title = `点击表头排序，再点同一列反向；当前按「${label}」${dir}`;
  const btn = $('#streakSortReset');
  if (btn) btn.hidden = state.streakSort.key === 'streak' && state.streakSort.dir === 'desc';
}

/* 连板梯队的行没有「计划买入价」这类字段，进抽屉前补一份默认值，
   这样 K 线、概念、加入计划/追踪都能照常用。 */
function openStreakDrawer(r) {
  const sellP = (Number($('#fSellProfit').value) || 4.5) / 100;
  openDrawer({
    ...r,
    signal_date: r.date,
    buy_price: r.close,
    sell_price: Number((r.close * (1 + sellP)).toFixed(2)),
    vol_ratio: null,
    ma_volume: null,
    volume: null,
  });
}

function bindStreakTable() {
  $('#streakTable tbody').addEventListener('click', (ev) => {
    const tr = ev.target.closest('tr[data-code]');
    if (!tr) return;
    const r = state.streakRows.find((x) => x.code === tr.dataset.code);
    if (r) openStreakDrawer(r);
  });
  const ladder = $('#streakLadder');
  ladder.addEventListener('click', (ev) => {
    const btn = ev.target.closest('.ladder-item');
    if (!btn) return;
    const r = state.streakRows.find((x) => x.code === btn.dataset.code);
    if (r) openStreakDrawer(r);
  });
}

function exportStreakCsv() {
  const rows = streakFiltered();
  if (!rows.length) { toast('没有可导出的结果', 'warn'); return; }
  const cols = ['streak_label', 'symbol', 'name', 'board', 'seal', 'limit_rate',
    'industry_l1', 'industry_l2', 'industry_l3', 'concepts', 'concept_n', 'date',
    'close', 'open', 'high', 'low', 'pct_change', 'amount_yi', 'volume_hand',
    'float_mcap_yi', 'float_shares_yi'];
  const head = ['连板', '代码', '名称', '板块', '封板', '涨跌幅限制', '一级行业', '二级行业',
    '三级行业', '概念', '概念数', '交易日', '收盘价', '开盘价', '最高价', '最低价', '涨跌幅',
    '成交额(亿)', '成交量(手)', '流通市值(亿)', '流通股本(亿股)'];
  const lines = [head.join(',')];
  orderBy(rows, state.streakSort, STREAK_SORT_COLS).forEach((i) => {
    const r = rows[i];
    lines.push(cols.map((c) => {
      if (c === 'concepts') return (r.concepts || []).join('、');
      if (c === 'concept_n') return (r.concepts || []).length;
      const v = r[c];
      return v === null || v === undefined ? '' : v;
    }).join(','));
  });
  const blob = new Blob(['\uFEFF' + lines.join('\n')], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  const d = state.streak ? state.streak.date : new Date().toISOString().slice(0, 10);
  a.download = `连板梯队_${d}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
}

/* ------------------------------------------------------------------ */
/* 板块交集（二级行业 ∩ 概念）                                          */
/* ------------------------------------------------------------------ */
/* 这一页与「选股」是两件事：选股问的是「哪些票放量了」，这里问的是
   「哪些票同时属于这些行业和这些概念」—— 纯板块归属的集合运算，与量价无关。
   所以后端只取锚点日那一行，前端把 A / B / A∩B 三个数摊开给人看。 */

let ixTimer = null;

/* 点一下 chip 就重算，但别每点一下都发一次请求（连点多选时很浪费） */
function scheduleIntersect() {
  clearTimeout(ixTimer);
  ixTimer = setTimeout(loadIntersect, 220);
}

function ixQuery() {
  const boards = Array.from(document.querySelectorAll('.xBd:checked')).map((o) => o.value);
  if (!boards.length) { toast('请至少勾选一个板块', 'warn'); return null; }
  const markets = Array.from(document.querySelectorAll('.xMk:checked')).map((o) => o.value);
  if (!markets.length) { toast('请至少勾选一个交易所', 'warn'); return null; }
  const p = new URLSearchParams();
  const date = $('#xDate').value;
  if (date) p.set('date', date);
  if (state.ixInd.length) p.set('industries', state.ixInd.join(','));
  if (state.ixCpt.length) p.set('concepts', state.ixCpt.join(','));
  p.set('concept_mode', $('#xCptMode').value);
  p.set('boards', boards.join(','));
  p.set('markets', markets.join(','));
  p.set('exclude_st', $('#xExcludeSt').checked ? 'true' : 'false');
  p.set('min_amount', String(Number($('#xMinAmount').value) || 0));
  p.set('max_float_mcap', String(Number($('#xMaxMcap').value) || 0));
  p.set('max_rows', '1000');
  return p.toString();
}

function ixEmptyState() {
  state.intersect = null;
  state.ixRows = [];
  $('#intersectVennCard').hidden = true;
  $('#intersectSuggestCard').hidden = true;
  $('#intersectConditions').innerHTML =
    '<li>请在下方「二级行业 / 概念」里至少选一项，再点『求交集』'
    + '（选行业 = 并集，选概念可选任一 / 全部）</li>';
  $('#intersectMeta').textContent = '尚未选择条件';
  renderIntersectTable();
}

async function loadIntersect() {
  const q = ixQuery();
  if (!q) return;
  if (!state.ixInd.length && !state.ixCpt.length) { ixEmptyState(); return; }
  const btn = $('#btnIntersect');
  clearTimeout(ixTimer);
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = '计算中…';
  try {
    const d = await api('/api/intersect?' + q);
    if (!d.ok) {
      toast('求交集失败：' + (d.reason || '未知原因'), 'err');
      if (d.date_out_of_range && d.date_min) {
        $('#xDate').value = '';
        $('#xDate').min = d.date_min;
        $('#xDate').max = d.date_max;
      }
      return;
    }
    state.intersect = d;
    state.ixRows = d.rows || [];
    renderIntersect(d);
  } catch (e) {
    toast('求交集失败：' + e.message, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

function renderIntersect(d) {
  $('#intersectConditions').innerHTML =
    (d.conditions || []).map((c) => `<li>${esc(c)}</li>`).join('');
  renderVenn(d);
  renderIntersectKpi(d);
  renderIntersectSuggest(d);
  paintSortHeaders('#intersectTable', state.ixSort);
  updateIntersectSortUI();
  renderIntersectTable();
}

/* 双圆文氏图：纯 SVG（ECharts 画两个圆反而更绕），viewBox 自适应宽度 */
function renderVenn(d) {
  const box = $('#intersectVenn');
  if (!box) return;
  const aLab = d.industries.length
    ? `A 二级行业 ${d.industries.length} 个 · ${d.set_a} 只`
    : `A 不限行业（全集）· ${d.set_a} 只`;
  const bLab = d.concepts.length
    ? `B 概念 ${d.concepts.length} 个 · ${d.set_b} 只`
    : `B 不限概念（全集）· ${d.set_b} 只`;
  box.innerHTML = `
    <svg viewBox="0 0 360 214" preserveAspectRatio="xMidYMid meet" role="img"
         aria-label="行业集合 A ${d.set_a} 只，概念集合 B ${d.set_b} 只，交集 ${d.intersect} 只">
      <circle cx="152" cy="100" r="74" fill="rgba(10,132,255,.14)"
              stroke="#0a84ff" stroke-width="1.5" />
      <circle cx="208" cy="100" r="74" fill="rgba(191,90,242,.14)"
              stroke="#bf5af2" stroke-width="1.5" />
      <text class="v-lab" x="180" y="14" text-anchor="middle">A 独有 · A∩B · B 独有</text>
      <text class="v-num" x="108" y="108" text-anchor="middle" dominant-baseline="middle">${d.only_a}</text>
      <text class="v-num-hit ${d.intersect ? 'hot' : 'zero'}" x="180" y="108"
            text-anchor="middle" dominant-baseline="middle">${d.intersect}</text>
      <text class="v-num" x="252" y="108" text-anchor="middle" dominant-baseline="middle">${d.only_b}</text>
      <text class="v-lab" x="18" y="204">${esc(aLab)}</text>
      <text class="v-lab" x="342" y="204" text-anchor="end">${esc(bLab)}</text>
    </svg>`;
}

function renderIntersectKpi(d) {
  const pct = (x) => `${(x * 100).toFixed(1)}%`;
  const modeLabel = d.concept_mode === 'all' ? '全部命中' : '任一命中';
  const items = [
    { k: '候选范围', v: d.candidates, s: `全市场 ${d.total_universe} 只` },
    {
      k: '行业集合 A', v: d.set_a,
      s: d.industries.length ? `${d.industries.length} 个行业（并集）` : '不限行业',
    },
    {
      k: '概念集合 B', v: d.set_b,
      s: d.concepts.length ? `${d.concepts.length} 个概念 · ${modeLabel}` : '不限概念',
    },
    {
      k: '交集 A∩B', v: d.intersect, cls: d.intersect ? 'hot' : 'warn',
      s: d.set_a && d.set_b ? `占 A ${pct(d.ratio_a)} · 占 B ${pct(d.ratio_b)}` : '',
    },
    { k: 'A 独有', v: d.only_a, s: '在行业内、不在概念内' },
    { k: 'B 独有', v: d.only_b, s: '在概念内、不在行业内' },
  ];
  $('#intersectKpi').innerHTML = items.map((x) => `
    <div class="kpi ${x.cls || ''}">
      <div class="k">${esc(x.k)}</div>
      <div class="v">${fmtInt(x.v)}</div>
      <div class="s">${esc(x.s)}</div>
    </div>`).join('');
}

/* 交集为空时给「下一步往哪调」的两组候选标签（点一下就并入条件） */
function renderIntersectSuggest(d) {
  const card = $('#intersectSuggestCard');
  const sg = d.suggest || {};
  const cpts = sg.concepts_in_industry || [];
  const inds = sg.industries_in_concept || [];
  if (!cpts.length && !inds.length) { card.hidden = true; return; }
  card.hidden = false;
  $('#intersectSuggestMeta').textContent =
    `A ${d.set_a} 只、B ${d.set_b} 只，交集 0 只`;
  $('#suggestConcepts').innerHTML = cpts.map((g) => `
    <button type="button" class="ind-chip" data-kind="concept" data-name="${esc(g.concept)}"
            title="把「${esc(g.concept)}」加进概念侧">${esc(g.concept)} <b>${g.n}</b></button>`).join('');
  $('#suggestIndustries').innerHTML = inds.map((g) => `
    <button type="button" class="ind-chip" data-kind="industry" data-name="${esc(g.l2)}"
            title="把「${esc(g.l2)}」加进行业侧">${esc(g.l2)} <b>${g.n}</b></button>`).join('');
}

/* ---- 两个多选器 -------------------------------------------------- */
function ixToggle(arr, name) {
  const i = arr.indexOf(name);
  if (i >= 0) arr.splice(i, 1); else arr.push(name);
}

function ixChip(name, n, on) {
  return `<button type="button" class="ind-chip${on ? ' on' : ''}" data-name="${esc(name)}"
      title="点击${on ? '取消' : '选中'}「${esc(name)}」">${esc(name)}`
    + (n ? ` <b>${n}</b>` : '') + '</button>';
}

function renderIndPicker() {
  const box = $('#indPickList');
  if (!box) return;
  const q = state.ixIndSearch.trim().toLowerCase();
  const counts = {};
  (state.industriesCounts || []).forEach((g) => { counts[g.l2] = g.n; });
  const chosen = new Set(state.ixInd);
  const parts = [];
  Object.keys(state.industryTree || {}).forEach((l1) => {
    let names = state.industryTree[l1] || [];
    if (q) names = names.filter((n) => n.toLowerCase().includes(q));
    if (!names.length) return;
    parts.push(`<div class="pick-group-title">${esc(l1)}</div>`);
    parts.push('<div class="pick-group">'
      + names.map((n) => ixChip(n, counts[n], chosen.has(n))).join('') + '</div>');
  });
  box.innerHTML = parts.length ? parts.join('') : '<div class="pick-none">没有匹配的行业</div>';
  $('#indPickMeta').textContent = state.ixInd.length
    ? `已选 ${state.ixInd.length} 个（并集）`
    : `${(state.industries || []).length} 个可选 · 不选 = 不限行业`;
}

function renderCptPicker() {
  const box = $('#cptPickList');
  if (!box) return;
  const q = state.ixCptSearch.trim().toLowerCase();
  const all = (state.conceptItems || []).filter((x) => (x.n || 0) > 0);
  const matched = q ? all.filter((x) => x.name.toLowerCase().includes(q)) : all;
  const shown = q ? matched : matched.slice(0, IX_CPT_PREVIEW);
  const chosen = new Set(state.ixCpt);
  box.innerHTML = shown.length
    ? '<div class="pick-group">'
      + shown.map((x) => ixChip(x.name, x.n, chosen.has(x.name))).join('') + '</div>'
      + (!q && matched.length > shown.length
        ? `<div class="pick-none">已按成员数降序显示前 ${shown.length} 个，`
          + `另有 ${matched.length - shown.length} 个，用搜索框找</div>`
        : '')
    : '<div class="pick-none">没有匹配的概念</div>';
  $('#cptPickMeta').textContent = state.ixCpt.length
    ? `已选 ${state.ixCpt.length} 个`
    : `${all.length} 个可选 · 不选 = 不限概念`;
}

/* 已选区：始终占位（空时给一句说明），否则点选时整页会上下跳 */
function renderIxChosen() {
  const indBox = $('#indPickChosen');
  indBox.innerHTML = state.ixInd.length
    ? state.ixInd.map((n) => `<button type="button" class="ind-chip" data-name="${esc(n)}"
        title="点击移除「${esc(n)}」">${esc(n)}</button>`).join('')
    : '<span class="pick-none">未选 → A 等于候选全集</span>';
  const cptBox = $('#cptPickChosen');
  const mode = $('#xCptMode').value === 'all' ? '需同时命中' : '命中任一即可';
  cptBox.innerHTML = state.ixCpt.length
    ? state.ixCpt.map((n) => `<button type="button" class="ind-chip" data-name="${esc(n)}"
        title="点击移除「${esc(n)}」">${esc(n)}</button>`).join('')
      + `<span class="pick-none">（${mode}）</span>`
    : '<span class="pick-none">未选 → B 等于候选全集</span>';
}

/* ---- 明细表 ------------------------------------------------------ */
function renderIntersectTable() {
  const tb = $('#intersectTable tbody');
  if (!tb) return;
  const rows = state.ixRows || [];
  if (!rows.length) {
    tb.innerHTML = `<tr><td colspan="10" class="empty">${
      state.intersect ? '没有同时满足两侧条件的个股，见上方调整建议'
        : '请先选择条件并点『求交集』'}</td></tr>`;
    if (state.intersect) {
      $('#intersectMeta').textContent =
        `交集为空（A ${state.intersect.set_a} 只 · B ${state.intersect.set_b} 只）`;
    }
    return;
  }
  const order = orderBy(rows, state.ixSort, INTERSECT_SORT_COLS);
  tb.innerHTML = order.map((i) => {
    const r = rows[i];
    const hits = r.hit_concepts || [];
    const all = r.concepts || [];
    const hitTitle = hits.length ? hits.join('、') : '（本次未限定概念）';
    const hitHtml = hits.length
      ? hits.slice(0, 3).map((c) => `<span class="cpt">${esc(c)}</span>`).join('')
        + (hits.length > 3
          ? `<span class="cpt-more" title="${esc(hitTitle)}">+${hits.length - 3}</span>` : '')
      : '<span class="hit-empty">—</span>';
    const indTitle = [r.industry_l1, r.industry_l2, r.industry_l3]
      .filter(Boolean).join(' / ') || '无行业分类';
    const tag = r.is_st ? ' <span class="muted">ST</span>' : '';
    return `
    <tr class="clickable" data-code="${esc(r.code)}">
      <td class="code">${esc(r.symbol)}</td>
      <td><b>${esc(r.name)}</b>${tag}</td>
      <td><span class="ind" title="${esc(indTitle)}">${esc(r.industry_l2 || '—')}</span></td>
      <td class="hit-cell" title="${esc(hitTitle)}">${hitHtml}</td>
      <td class="num" title="本地板块文件收录的全部概念：${esc(all.join('、') || '无')}">${r.concept_n}</td>
      <td class="num">${fmt(r.close, 2)}</td>
      <td class="num ${pctClass(r.pct_change)}">${fmtPct(r.pct_change)}</td>
      <td class="num">${fmt(r.amount_yi, 2)}</td>
      <td class="num">${fmt(r.float_mcap_yi, 1)}</td>
      <td>${esc(r.board)}</td>
    </tr>`;
  }).join('');
  const d = state.intersect;
  $('#intersectMeta').textContent =
    `${rows.length} 只（A ${d.set_a} ∩ B ${d.set_b} = ${d.intersect}`
    + `，占比 A ${(d.ratio_a * 100).toFixed(1)}%）`
    + (d.truncated ? ` · 已截断，共 ${d.total} 只` : '');
}

function updateIntersectSortUI() {
  const el = $('#intersectSortHint');
  if (!el) return;
  const th = document.querySelector(`#intersectTable thead th[data-sort="${state.ixSort.key}"]`);
  const label = th ? (th.dataset.label || state.ixSort.key) : state.ixSort.key;
  const dir = state.ixSort.dir === 'desc' ? '降序' : '升序';
  el.textContent = `点行看 K 线 · 排序：${label} ${dir}`;
  el.title = `点击表头排序，再点同一列反向；当前按「${label}」${dir}`;
  const btn = $('#intersectSortReset');
  if (btn) {
    btn.hidden = state.ixSort.key === IX_DEFAULT_SORT.key
      && state.ixSort.dir === IX_DEFAULT_SORT.dir;
  }
}

/* 交集行没有「计划买入价」这类字段，进抽屉前补一份默认值 */
function openIntersectDrawer(r) {
  const sellP = (Number($('#fSellProfit').value) || 4.5) / 100;
  openDrawer({
    ...r,
    signal_date: r.trade_date,
    buy_price: r.close,
    sell_price: Number((r.close * (1 + sellP)).toFixed(2)),
    vol_ratio: null,
    ma_volume: null,
    volume: null,
  });
}

function exportIntersectCsv() {
  const rows = state.ixRows || [];
  if (!rows.length) { toast('没有可导出的结果', 'warn'); return; }
  const cols = ['symbol', 'name', 'board', 'industry_l1', 'industry_l2', 'industry_l3',
    'hit_concepts', 'concepts', 'concept_n', 'trade_date', 'close', 'pct_change',
    'amount_yi', 'float_mcap_yi', 'float_shares_yi'];
  const head = ['代码', '名称', '板块', '一级行业', '二级行业', '三级行业',
    '命中概念', '全部概念', '概念数', '交易日', '收盘价', '涨跌幅',
    '成交额(亿)', '流通市值(亿)', '流通股本(亿股)'];
  const cell = (s) => (/[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s);
  const lines = [head.join(',')];
  orderBy(rows, state.ixSort, INTERSECT_SORT_COLS).forEach((i) => {
    const r = rows[i];
    lines.push(cols.map((c) => {
      if (c === 'concepts') return cell((r.concepts || []).join('、'));
      if (c === 'hit_concepts') return cell((r.hit_concepts || []).join('、'));
      const v = r[c];
      return v === null || v === undefined ? '' : String(v);
    }).join(','));
  });
  const blob = new Blob(['\uFEFF' + lines.join('\n')], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `板块交集_${(state.intersect && state.intersect.anchor) || 'latest'}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
}

function bindIntersect() {
  // 这一页的元素很多，任何一处拼错 id 都会让 addEventListener 抛错、
  // 把后面的 init 全部带停（表现为「其他页签也一起坏了」）。
  // 先整体查一遍，缺了就只跳过本页。
  const needed = ['#indPickList', '#indPickChosen', '#cptPickList', '#cptPickChosen',
    '#indPickSearch', '#cptPickSearch', '#btnIndPickClear', '#btnCptPickClear',
    '#btnIntersectClear', '#intersectSuggestCard', '#intersectTable',
    '#btnIntersect', '#btnIntersectExport', '#intersectSortReset', '#xCptMode',
    '#xDate', '#xDateLatest', '#xExcludeSt', '#xMinAmount', '#xMaxMcap'];
  const missing = needed.filter((s) => !$(s));
  if (missing.length) {
    console.warn('板块交集页元素缺失，已跳过绑定：', missing);
    return;
  }
  const picker = (listSel, chosenSel, arr, redraw) => {
    const handler = (ev) => {
      const chip = ev.target.closest('.ind-chip');
      if (!chip) return;
      ixToggle(arr, chip.dataset.name);
      redraw();
      renderIxChosen();
      scheduleIntersect();
    };
    document.querySelector(listSel).addEventListener('click', handler);
    document.querySelector(chosenSel).addEventListener('click', handler);
  };
  picker('#indPickList', '#indPickChosen', state.ixInd,
    () => { renderIndPicker(); });
  picker('#cptPickList', '#cptPickChosen', state.ixCpt,
    () => { renderCptPicker(); });

  $('#indPickSearch').addEventListener('input', (e) => {
    state.ixIndSearch = e.target.value;
    renderIndPicker();
  });
  $('#cptPickSearch').addEventListener('input', (e) => {
    state.ixCptSearch = e.target.value;
    renderCptPicker();
  });
  $('#btnIndPickClear').onclick = () => {
    if (!state.ixInd.length) { toast('行业侧未选任何项', 'warn'); return; }
    state.ixInd = [];
    renderIndPicker();
    renderIxChosen();
    loadIntersect();
  };
  $('#btnCptPickClear').onclick = () => {
    if (!state.ixCpt.length) { toast('概念侧未选任何项', 'warn'); return; }
    state.ixCpt = [];
    renderCptPicker();
    renderIxChosen();
    loadIntersect();
  };
  $('#btnIntersectClear').onclick = () => {
    state.ixInd = [];
    state.ixCpt = [];
    state.ixIndSearch = '';
    state.ixCptSearch = '';
    $('#indPickSearch').value = '';
    $('#cptPickSearch').value = '';
    renderIndPicker();
    renderCptPicker();
    renderIxChosen();
    ixEmptyState();
  };

  // 空交集时的建议标签：点一下并入对应一侧，再立即重算
  $('#intersectSuggestCard').addEventListener('click', (ev) => {
    const chip = ev.target.closest('.ind-chip');
    if (!chip) return;
    const { name, kind } = chip.dataset;
    if (kind === 'concept') {
      if (!state.ixCpt.includes(name)) state.ixCpt.push(name);
      state.ixCptSearch = '';
      $('#cptPickSearch').value = '';
      renderCptPicker();
    } else {
      if (!state.ixInd.includes(name)) state.ixInd.push(name);
      state.ixIndSearch = '';
      $('#indPickSearch').value = '';
      renderIndPicker();
    }
    renderIxChosen();
    // 建议标签来自「无交集」那次结果，条件一变它就过期了，所以直接重算
    loadIntersect();
  });

  $('#intersectTable tbody').addEventListener('click', (ev) => {
    const tr = ev.target.closest('tr[data-code]');
    if (!tr) return;
    const r = (state.ixRows || []).find((x) => x.code === tr.dataset.code);
    if (r) openIntersectDrawer(r);
  });

  $('#btnIntersect').onclick = loadIntersect;
  $('#btnIntersectExport').onclick = exportIntersectCsv;
  $('#intersectSortReset').onclick = () => {
    state.ixSort = { ...IX_DEFAULT_SORT };
    paintSortHeaders('#intersectTable', state.ixSort);
    updateIntersectSortUI();
    renderIntersectTable();
  };
  bindSortHead('#intersectTable', state.ixSort, INTERSECT_SORT_COLS, () => {
    paintSortHeaders('#intersectTable', state.ixSort);
    updateIntersectSortUI();
    renderIntersectTable();
  });
  $('#xCptMode').onchange = () => {
    renderIxChosen();
    if (state.ixCpt.length) loadIntersect();
  };
  $('#xDate').onchange = (e) => {
    snapTradeDate(e.target, { notify: true });
    loadIntersect();
  };
  $('#xDateLatest').onclick = () => {
    $('#xDate').value = '';
    loadIntersect();
  };
  $('#xExcludeSt').onchange = loadIntersect;
  $('#xMinAmount').onchange = loadIntersect;
  $('#xMaxMcap').onchange = loadIntersect;
  document.querySelectorAll('.xBd, .xMk').forEach((o) => { o.onchange = loadIntersect; });
}

/* ------------------------------------------------------------------ */
/* K 线抽屉                                                            */
/* ------------------------------------------------------------------ */
function openDrawer(row) {
  state.current = row;
  $('#drawer').hidden = false;
  $('#drawerMask').hidden = false;
  // 勾选框是「持久偏好」：每次开抽屉把控件同步回 state，避免关闭过的人再打开时对不上
  $('#klineBiasOn').checked = state.klineBias !== false;
  $('#drawerTitle').textContent = `${row.code} ${row.name || ''}`;
  const ind = [row.industry_l1, row.industry_l2, row.industry_l3].filter(Boolean).join(' / ');
  const cpts = row.concepts || [];
  $('#drawerSub').textContent =
    `信号日 ${row.signal_date || '-'} · 放量日收盘 ${fmt(row.close, 2)}`
    + (ind ? ` · 行业 ${ind}` : ' · 行业 未分类')
    + (cpts.length ? ` · 概念 ${cpts.length} 个` : '');
  // 概念全量展开（结果表只显示前 2 个，这里给全）
  const cptBar = $('#drawerConcepts');
  cptBar.hidden = !cpts.length;
  cptBar.innerHTML = cpts.length
    ? `<span class="label">概念 ${cpts.length} 个</span>`
      + cpts.map((c) => `<span class="cpt">${esc(c)}</span>`).join('')
    : '';
  $('#drawerPlanBar').innerHTML = `
    <label>计划买入价 <input id="dBuy" type="number" step="0.01" value="${fmt(row.buy_price, 2)}" /></label>
    <label>计划卖出价 <input id="dSell" type="number" step="0.01" value="${fmt(row.sell_price, 2)}" /></label>
    <label>数量(股) <input id="dQty" type="number" step="100" value="" /></label>
    <button class="btn primary small" id="dSavePlan">保存计划</button>
    <button class="btn small" id="dAddPlan">加入计划</button>
    <button class="btn small" id="dAddWatch">加入追踪</button>
    <button class="btn small" id="dReset">按收盘价重置</button>`;
  $('#dSavePlan').onclick = async () => {
    try {
      await api('/api/plans', { method: 'POST', body: {
        code: row.code, name: row.name, signal_date: row.signal_date, base_close: row.close,
        volume: row.volume, ma_volume: row.ma_volume, vol_ratio: row.vol_ratio,
        buy_price: Number($('#dBuy').value), sell_price: Number($('#dSell').value),
        qty: Number($('#dQty').value) || null, status: '待买入',
      } });
      toast('计划已保存', 'ok');
      await Promise.all([loadPlans(), loadCounts()]);
      loadKline();
    } catch (e) { toast('保存失败：' + e.message, 'err'); }
  };
  $('#dAddPlan').onclick = () => $('#dSavePlan').click();
  $('#dAddWatch').onclick = async () => {
    try {
      await api('/api/watchlist', { method: 'POST', body: {
        code: row.code, name: row.name, signal_date: row.signal_date, base_close: row.close,
        buy_price: Number($('#dBuy').value), sell_price: Number($('#dSell').value),
        qty: Number($('#dQty').value) || null, status: '关注中',
      } });
      toast('已加入追踪', 'ok');
      await Promise.all([loadWatch(), loadCounts()]);
    } catch (e) { toast('加入追踪失败：' + e.message, 'err'); }
  };
  $('#dReset').onclick = () => {
    $('#dBuy').value = fmt(row.close, 2);
    $('#dSell').value = (Number(row.close) * 1.045).toFixed(2);
    loadKline();
  };
  ['dBuy', 'dSell'].forEach((id) => { $('#' + id).onchange = loadKline; });
  // 偏离度附图开关：只改状态并重画（不重新请求数据），默认开
  $('#klineBiasOn').onchange = (e) => {
    state.klineBias = !!e.target.checked;
    loadKline();
  };
  loadKline();
}

function closeDrawer() {
  $('#drawer').hidden = true;
  $('#drawerMask').hidden = true;
  state.current = null;
}

async function loadKline() {
  const row = state.current;
  if (!row) return;
  const bars = Number($('#klineBars').value) || 160;
  const maWindow = Number($('#fMaWindow').value) || 20;
  let d;
  try {
    d = await api(`/api/kline/${encodeURIComponent(row.code)}?bars=${bars}&ma_window=${maWindow}`);
  } catch (e) { toast('加载K线失败：' + e.message, 'err'); return; }
  if (!d.ok) { toast(d.reason || '加载失败', 'err'); return; }

  const buy = Number($('#dBuy').value) || null;
  const sell = Number($('#dSell').value) || null;

  /* 偏离度附图（对应通达信公式）：T = (close - MA250)/close*100，M5/M20 为 T 的 EMA。
     数据由后端用**前复权**口径算好（行情缓存本身是不复权的，跨除权日直算会假跳空）。 */
  const bias = d.bias || {};
  const showBias = !!(bias.t && bias.t.some((v) => v != null)) && state.klineBias !== false;

  if (!state.klineChart) state.klineChart = echarts.init($('#klineChart'));
  const chart = state.klineChart;
  // 附图开关会改变栅格布局：容器高度必须跟着变，否则 ECharts 算出来的
  // grid 会超出画布（表现为最下面那栏被裁掉）。改完 class 先 resize，
  // setOption 之后还会再 resize 一次。
  $('#klineChart').classList.toggle('with-bias', showBias);
  chart.resize();

  const volColors = d.ohlc.map((o) => (o[1] >= o[0] ? UP : DOWN));
  const signalIdx = (d.signals || []).map((s) => s.index);

  /* 让 y 轴把买入/卖出价一起纳入范围，否则虚线会被挤到图外看不见 */
  const yLow = Math.min(buy || Infinity, sell || Infinity);
  const yHigh = Math.max(buy || -Infinity, sell || -Infinity);
  /* 掐掉浮点尾巴：否则轴顶端会出现 4.140000000000001 这种刻度标签 */
  const clean = (n) => Number(n.toFixed(3));
  const axisSpan = (v) => {
    const lo = Number.isFinite(yLow) ? Math.min(v.min, yLow) : v.min;
    const hi = Number.isFinite(yHigh) ? Math.max(v.max, yHigh) : v.max;
    const pad = (hi - lo) * 0.04 || 0.05;
    return { lo: clean(lo - pad), hi: clean(hi + pad) };
  };

  /* 买卖价文字放到图表右上角，避免与 y 轴刻度、放量标记打架 */
  const priceLabels = [];
  if (buy) priceLabels.push({
    type: 'text', right: 16, top: 4,
    style: { text: `买入 ${fmt(buy)}`, fill: C.accent, font: '11px -apple-system, "PingFang SC", sans-serif' },
  });
  if (sell) priceLabels.push({
    type: 'text', right: 16, top: 20,
    style: { text: `卖出 ${fmt(sell)}`, fill: UP, font: '11px -apple-system, "PingFang SC", sans-serif' },
  });

  const option = {
    animation: false,
    backgroundColor: 'transparent',
    textStyle: { color: C.text2, fontSize: 11 },
    legend: {
      top: 4, left: 8, itemGap: 14,
      textStyle: { color: C.muted }, inactiveColor: '#48484a',
      data: showBias
        ? ['K线', 'MA5', 'MA10', 'MA20', 'MA60', '20日均量', 'T', 'M5', 'M20']
        : ['K线', 'MA5', 'MA10', 'MA20', 'MA60', '20日均量'],
    },
    tooltip: {
      trigger: 'axis', axisPointer: { type: 'cross' },
      backgroundColor: C.glass, borderColor: C.line,
      extraCssText: 'backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);',
      textStyle: { color: C.text, fontSize: 12 },
      formatter: (ps) => {
        const i = ps[0].dataIndex;
        const h = d.ohlc[i];
        const prev = i > 0 ? d.ohlc[i - 1][1] : h[0];
        const chg = prev ? (h[1] / prev - 1) * 100 : 0;
        const c = chg >= 0 ? UP : DOWN;
        let extra = '';
        if (showBias) {
          const tv = bias.t[i];
          const m5v = (bias.m5 || [])[i];
          const m20v = (bias.m20 || [])[i];
          const tc = tv == null ? C.muted : (tv >= 0 ? UP : DOWN);
          extra = `<br/>偏离度 <b style="color:${tc}">${tv == null ? '—' : tv.toFixed(2) + '%'}</b>　`
            + `M5 ${m5v == null ? '—' : m5v.toFixed(2)}　M20 ${m20v == null ? '—' : m20v.toFixed(2)}`;
        }
        return `<b>${d.dates[i]}</b><br/>开 ${fmt(h[0])}　高 ${fmt(h[3])}<br/>低 ${fmt(h[2])}　收 <b style="color:${c}">${fmt(h[1])}</b><br/>`
          + `涨跌 <span style="color:${c}">${chg.toFixed(2)}%</span><br/>`
          + `量 ${fmtInt(d.volume[i])} 手　均量 ${fmtInt(d.volume_ma[i])} 手<br/>`
          + `量比 <b>${fmt(d.vol_ratio[i], 2)}×</b>` + extra;
      },
    },
    axisPointer: { link: [{ xAxisIndex: 'all' }] },
    graphic: priceLabels.length ? priceLabels : undefined,
    grid: showBias ? [
      // 三栏：主图 / 成交量 / 偏离度。总高 640（CSS #klineChart.with-bias），
      // 底部留 ~26px 给 dataZoom 滑块。
      { left: 58, right: 26, top: 34, height: 296 },
      { left: 58, right: 26, top: 344, height: 108 },
      { left: 58, right: 26, top: 466, height: 138 },
    ] : [
      { left: 58, right: 26, top: 34, height: 330 },
      { left: 58, right: 26, top: 400, height: 140 },
    ],
    xAxis: showBias ? [
      {
        type: 'category', data: d.dates, gridIndex: 0, boundaryGap: true,
        axisLine: { lineStyle: { color: C.line } }, axisLabel: { show: false },
        splitLine: { show: false }, min: 'dataMin', max: 'dataMax',
      },
      {
        type: 'category', data: d.dates, gridIndex: 1, boundaryGap: true,
        axisLine: { lineStyle: { color: C.line } }, axisLabel: { show: false },
        splitLine: { show: false }, min: 'dataMin', max: 'dataMax',
      },
      {
        type: 'category', data: d.dates, gridIndex: 2, boundaryGap: true,
        axisLine: { lineStyle: { color: C.line } },
        axisLabel: { color: C.muted, formatter: (v) => v.slice(5) },
        splitLine: { show: false }, min: 'dataMin', max: 'dataMax',
      },
    ] : [
      {
        type: 'category', data: d.dates, gridIndex: 0, boundaryGap: true,
        axisLine: { lineStyle: { color: C.line } }, axisLabel: { show: false },
        splitLine: { show: false }, min: 'dataMin', max: 'dataMax',
      },
      {
        type: 'category', data: d.dates, gridIndex: 1, boundaryGap: true,
        axisLine: { lineStyle: { color: C.line } },
        axisLabel: { color: C.muted, formatter: (v) => v.slice(5) },
        splitLine: { show: false }, min: 'dataMin', max: 'dataMax',
      },
    ],
    yAxis: showBias ? [
      {
        scale: true, gridIndex: 0, splitLine: { lineStyle: { color: C.grid } },
        axisLabel: { color: C.muted }, axisLine: { show: false },
        min: (v) => axisSpan(v).lo,
        max: (v) => axisSpan(v).hi,
      },
      {
        gridIndex: 1, splitLine: { show: false }, axisLabel: { color: C.muted, showMaxLabel: false },
        axisLine: { show: false }, axisTick: { show: false },
      },
      {
        // 偏离度：带正负号的百分比，轴标加 '%'
        scale: true, gridIndex: 2, splitLine: { lineStyle: { color: C.grid } },
        axisLabel: { color: C.muted, formatter: (v) => `${fmt(v, 0)}%` },
        axisLine: { show: false }, axisTick: { show: false },
      },
    ] : [
      {
        scale: true, gridIndex: 0, splitLine: { lineStyle: { color: C.grid } },
        axisLabel: { color: C.muted }, axisLine: { show: false },
        min: (v) => axisSpan(v).lo,
        max: (v) => axisSpan(v).hi,
      },
      {
        gridIndex: 1, splitLine: { show: false }, axisLabel: { color: C.muted },
        axisLine: { show: false }, axisTick: { show: false },
      },
    ],
    dataZoom: showBias ? [
      { type: 'inside', xAxisIndex: [0, 1, 2], start: 40, end: 100 },
      { type: 'slider', xAxisIndex: [0, 1, 2], bottom: 4, height: 16, start: 40, end: 100,
        borderColor: C.line, fillerColor: 'rgba(10,132,255,.16)',
        handleStyle: { color: C.accent }, moveHandleStyle: { color: C.accent },
        dataBackground: { lineStyle: { color: C.line }, areaStyle: { color: C.grid } } },
    ] : [
      { type: 'inside', xAxisIndex: [0, 1], start: 40, end: 100 },
      { type: 'slider', xAxisIndex: [0, 1], bottom: 4, height: 16, start: 40, end: 100,
        borderColor: C.line, fillerColor: 'rgba(10,132,255,.16)',
        handleStyle: { color: C.accent }, moveHandleStyle: { color: C.accent },
        dataBackground: { lineStyle: { color: C.line }, areaStyle: { color: C.grid } } },
    ],
    series: [
      {
        name: 'K线', type: 'candlestick', data: d.ohlc, xAxisIndex: 0, yAxisIndex: 0,
        itemStyle: { color: UP, color0: DOWN, borderColor: UP, borderColor0: DOWN },
        markPoint: {
          symbol: 'pin', symbolSize: 34,
          data: signalIdx.map((i) => ({ coord: [i, d.ohlc[i][3]], value: '放量' })),
          itemStyle: { color: C.warning },
          label: { fontSize: 10, color: '#1c1c1e', fontWeight: 600 },
        },
        markLine: buy || sell ? {
          symbol: 'none', silent: true,
          data: [
            buy ? { yAxis: buy, lineStyle: { color: C.accent, type: 'dashed' } } : null,
            sell ? { yAxis: sell, lineStyle: { color: UP, type: 'dashed' } } : null,
          ].filter(Boolean),
        } : undefined,
      },
      { name: 'MA5', type: 'line', data: d.ma.ma5, xAxisIndex: 0, yAxisIndex: 0, smooth: true, showSymbol: false, lineStyle: { width: 1, color: C.ma5 }, itemStyle: { color: C.ma5 } },
      { name: 'MA10', type: 'line', data: d.ma.ma10, xAxisIndex: 0, yAxisIndex: 0, smooth: true, showSymbol: false, lineStyle: { width: 1, color: C.ma10 }, itemStyle: { color: C.ma10 } },
      { name: 'MA20', type: 'line', data: d.ma.ma20, xAxisIndex: 0, yAxisIndex: 0, smooth: true, showSymbol: false, lineStyle: { width: 1.4, color: C.ma20 }, itemStyle: { color: C.ma20 } },
      { name: 'MA60', type: 'line', data: d.ma.ma60, xAxisIndex: 0, yAxisIndex: 0, smooth: true, showSymbol: false, lineStyle: { width: 1, color: C.ma60 }, itemStyle: { color: C.ma60 } },
      {
        name: '成交量', type: 'bar', data: d.volume, xAxisIndex: 1, yAxisIndex: 1,
        itemStyle: { color: (p) => volColors[p.dataIndex] },
      },
      {
        name: '20日均量', type: 'line', data: d.volume_ma, xAxisIndex: 1, yAxisIndex: 1,
        showSymbol: false, lineStyle: { width: 1.2, color: C.volma }, itemStyle: { color: C.volma },
      },
      ...(showBias ? [
        {
          // 偏离度主体线：T = (close - MA250)/close*100
          // 用 0 轴虚线做基准（对应原公式 `0,,DOTLINE,COLOR2191ED`），
          // 线在 0 上方=价格高于长期成交额加权成本，下方=低于。
          name: 'T', type: 'line', data: bias.t, xAxisIndex: 2, yAxisIndex: 2,
          showSymbol: false, lineStyle: { width: 1.6, color: C.bias },
          itemStyle: { color: C.bias },
          markLine: {
            symbol: 'none', silent: true,
            data: [{ yAxis: 0, lineStyle: { color: C.bias, type: 'dotted', width: 1 } }],
            label: { show: false },
          },
        },
        {
          name: 'M5', type: 'line', data: bias.m5, xAxisIndex: 2, yAxisIndex: 2,
          showSymbol: false, lineStyle: { width: 1, color: C.biasM5 }, itemStyle: { color: C.biasM5 },
        },
        {
          name: 'M20', type: 'line', data: bias.m20, xAxisIndex: 2, yAxisIndex: 2,
          showSymbol: false, lineStyle: { width: 1.2, color: C.biasM20 }, itemStyle: { color: C.biasM20 },
        },
      ] : []),
    ],
  };
  chart.setOption(option, true);
  chart.resize();

  const sig = (d.signals || []).slice(-1)[0];
  $('#drawerFoot').innerHTML =
    `共 ${d.bars} 根 K 线 · 最新 ${esc(d.latest.date)} 收 ${fmt(d.latest.close)}　`
    + (sig ? `最近放量信号：<b>${esc(sig.date)}</b> 量比 ${fmt(sig.vol_ratio, 2)}×` : '区间内无 ≥2 倍放量信号');
}

/* ------------------------------------------------------------------ */
/* 板块看盘（概念板块 / 行业板块）                                      */
/* ------------------------------------------------------------------ */
/* 板块列表可排序的列。默认按涨幅降序 —— 看盘第一眼看的就是「谁在涨」。
   注意：没有 category 列 —— 类别已由上方 tab 选定，整列同值是噪音。 */
const BOARD_SORT_COLS = {
  code:       { type: 'text', get: (r) => r.index_code },
  name:       { type: 'text', get: (r) => r.name || '' },
  pct:        { type: 'num',  get: (r) => r.pct },
  close:      { type: 'num',  get: (r) => r.close },
  amount_yi:  { type: 'num',  get: (r) => r.amount_yi },
  vol_ratio:  { type: 'num',  get: (r) => r.vol_ratio },
  turnover:   { type: 'num',  get: (r) => r.turnover },
  n_up:       { type: 'num',  get: (r) => r.n_up },
  n_down:     { type: 'num',  get: (r) => r.n_down },
  n_limit_up: { type: 'num',  get: (r) => r.n_limit_up },
  avg_pct:    { type: 'num',  get: (r) => r.avg_pct },
  n_members:  { type: 'num',  get: (r) => r.n_members },
};

/* 成分股表可排序的列 */
const BP_MEMBER_SORT_COLS = {
  code:          { type: 'text', get: (r) => r.symbol || r.code },
  name:          { type: 'text', get: (r) => r.name || '' },
  industry_l2:   { type: 'text', get: (r) => r.industry_l2 || '' },
  pct:           { type: 'num',  get: (r) => r.pct },
  close:         { type: 'num',  get: (r) => r.close },
  amount_yi:     { type: 'num',  get: (r) => r.amount_yi },
  vol_ratio:     { type: 'num',  get: (r) => r.vol_ratio },
  turnover:      { type: 'num',  get: (r) => r.turnover },
  volume_hand:   { type: 'num',  get: (r) => r.volume_hand },
  float_mcap_yi: { type: 'num',  get: (r) => r.float_mcap_yi },
  concept_n:     { type: 'num',  get: (r) => r.concept_n },
  board:         { type: 'text', get: (r) => r.board || '' },
  limit:         { type: 'text', get: (r) => r.limit || '' },
};

/* 「板块类别」在主视图只保留概念板块与行业板块两类（2026-09-26 用户要求）：
   地区板块本地没有成分文件（只有指数行情），风格板块是因子/盘口指标而非题材，
   两类都不适合做题材看盘。tab 写死在 index.html，这里只按目录补数量。 */
const BP_CATEGORY_LABEL = { 概念: '概念板块', 行业: '行业板块', 地区: '地区板块', 风格: '风格板块' };

function bpTabButtons() {
  return [...document.querySelectorAll('#bpTabs .bp-tab[data-cat]')];
}

/* 切类别 tab：只改选中态，取数由调用方决定（loadBoardPanel） */
function setBoardCategory(cat) {
  state.bpCategory = cat;
  bpTabButtons().forEach((b) => {
    const on = b.dataset.cat === cat;
    b.classList.toggle('active', on);
    b.setAttribute('aria-selected', on ? 'true' : 'false');
  });
}

async function loadBoardCatalog() {
  const d = await api('/api/board_catalog');
  state.bpCatalog = d;
  // 数量取「有成分」的那个数：与「仅有成分」勾选（默认开）下的行数一致。
  // 用板块总数会对不上（概念 269/268、行业 145/132），tab 上的数字必须等于点了之后的行数。
  const byCat = Object.fromEntries((d.categories || []).map((c) => [c.value, c]));
  bpTabButtons().forEach((b) => {
    const c = byCat[b.dataset.cat];
    const n = c ? (c.n_members ?? c.n) : null;
    b.querySelector('b').textContent = n === null ? '' : String(n);
  });
  setBoardCategory(state.bpCategory);
  return d;
}

async function loadBoardPanel() {
  const q = new URLSearchParams();
  const date = $('#bpDate').value;
  if (date) q.set('date', date);
  if (state.bpCategory) q.set('category', state.bpCategory);
  // 后端只负责给「cfg 顺序」的全量，排序由表头在前端做（604 行，即时响应）
  q.set('sort', 'order');
  q.set('order', 'asc');
  q.set('limit', '0');
  q.set('with_members_only', $('#bpOnlyMembers').checked ? 'true' : 'false');
  const kw = $('#bpSearch').value.trim();
  if (kw) q.set('keyword', kw);

  let d;
  try {
    d = await api('/api/board_panel?' + q.toString());
  } catch (e) { toast('加载板块失败：' + e.message, 'err'); return; }
  if (!d.ok) { toast(d.reason || '加载失败', 'err'); return; }

  state.bpPanel = d;
  state.bpRows = d.rows || [];
  renderBoardConditions(d);
  renderBoardTable();
  // 数据整体换了（切类别 / 换日期 / 改筛选），旧的选中板块可能已经不在列表里
  if (state.bpBoard && !state.bpRows.some((r) => r.index_code === state.bpBoard.index_code)) {
    clearBoardPicks();
  }
}

/* 选中板块失效时把右下三处一起复位。
   只清 state.bpBoard 不清图，会出现「左上已是行业板块、右下还挂着概念板块的 K 线」 */
function clearBoardPicks() {
  state.bpBoard = null;
  state.bpMembers = null;
  state.bpMemberRows = [];
  state.bpStock = null;
  renderBoardMembers();
  if (state.bpIndexChart) state.bpIndexChart.clear();
  if (state.bpStockChart) state.bpStockChart.clear();
  $('#bpIndexTitle').textContent = '板块指数 K 线';
  $('#bpIndexMeta').textContent = '先在上面选一个板块';
  $('#bpStockTitle').textContent = '个股 K 线';
  $('#bpStockMeta').textContent = '再点一只成分股';
}

function renderBoardConditions(d) {
  const s = d.summary || {};
  const catLabel = BP_CATEGORY_LABEL[d.category] || d.category || '全部';
  const items = [
    `交易日 <b>${esc(d.date)}</b>（本地缓存最新交易日）`,
    `板块类别 <b>${esc(catLabel)}</b> · 命中 <b>${d.total}</b> 个`,
    `板块指数涨跌：<b>${s.boards_up ?? 0}</b> 涨 / <b>${s.boards_down ?? 0}</b> 跌`
      + (s.boards_flat ? ` / ${s.boards_flat} 平` : '')
      + (s.boards_no_quote ? ` / ${s.boards_no_quote} 当日无行情` : ''),
    `全市场 <b>${s.stocks_quoted ?? 0}</b> 只有行情：涨停 <b>${s.limit_up_stocks ?? 0}</b> 只 / `
      + `跌停 <b>${s.limit_down_stocks ?? 0}</b> 只（去重口径，与连板梯队一致）`,
    '成分统计口径：涨停按「前收 × 涨跌幅限制」用整数分判定；换手 = Σ成交量 ÷ Σ流通股本',
    '概念板块来自 GN_ 成分文件，行业板块按通达信 T 码归属聚合；同一只股票可属多个概念',
  ];
  $('#boardConditions').innerHTML = items.map((t) => `<li>${t}</li>`).join('');
}

function bpPctCell(v, digits = 2) {
  return `<td class="num ${pctClass(v)}">${v === null || v === undefined ? '-' : fmtPct(v, digits)}</td>`;
}

function renderBoardTable() {
  const tb = $('#bpBoardTable tbody');
  const rows = state.bpRows;
  const total = state.bpPanel ? state.bpPanel.total : rows.length;
  $('#bpBoardMeta').textContent = rows.length
    ? `${rows.length} 个板块${rows.length < total ? `（全部 ${total}）` : ''}`
    : '';

  if (!rows.length) {
    tb.innerHTML = '<tr><td colspan="9" class="empty">暂无板块数据，点「加载板块」</td></tr>';
    return;
  }
  const idx = orderBy(rows, state.bpSort, BOARD_SORT_COLS);
  tb.innerHTML = idx.map((i) => {
    const r = rows[i];
    const sel = state.bpBoard && state.bpBoard.index_code === r.index_code ? ' sel' : '';
    const nm = r.n_quoted !== null && r.n_quoted !== undefined && r.n_quoted !== r.n_members
      ? `${r.n_members}<span class="bp-sub">/${r.n_quoted}</span>`
      : `${r.n_members ?? '-'}`;
    const noM = r.n_up === null || r.n_up === undefined;
    return `<tr class="clickable${sel}" data-code="${esc(r.index_code)}"
                title="${esc(r.name)} · 成分 ${r.n_members ?? 0} 只，当日有行情 ${r.n_quoted ?? 0} 只">
      <td class="code">${esc(r.index_code)}</td>
      <td>${esc(r.name)}</td>
      ${bpPctCell(r.pct)}
      <td class="num">${fmt(r.amount_yi, 2)}</td>
      <td class="num">${fmt(r.vol_ratio, 2)}</td>
      ${bpPctCell(r.turnover)}
      <td class="num">${noM ? '-' : `<span class="up">${r.n_up}</span><span class="bp-sep">/</span><span class="down">${r.n_down}</span>`}</td>
      <td class="num ${r.n_limit_up ? 'up bp-strong' : ''}">${noM ? '-' : (r.n_limit_up || 0)}</td>
      <td class="num">${nm}</td>
    </tr>`;
  }).join('');
}

async function selectBoard(row) {
  state.bpBoard = row;
  renderBoardTable();
  await Promise.all([loadBoardMembers(row.index_code), loadBoardIndexKline(row)]);
}

async function loadBoardMembers(code) {
  const q = new URLSearchParams({ board: code, sort: 'pct', order: 'desc', limit: '0' });
  const date = $('#bpDate').value;
  if (date) q.set('date', date);
  let d;
  try {
    d = await api('/api/board_members?' + q.toString());
  } catch (e) { toast('加载成分股失败：' + e.message, 'err'); return; }
  if (!d.ok) { toast(d.reason || '加载成分股失败', 'err'); return; }
  state.bpMembers = d;
  state.bpMemberRows = d.rows || [];
  renderBoardMembers();
}

function renderBoardMembers() {
  const tb = $('#bpMemberTable tbody');
  const rows = state.bpMemberRows;
  const head = state.bpMembers;
  const meta = $('#bpMemberMeta');
  if (!head || !state.bpBoard) {
    meta.textContent = '';
    $('#bpMemberHint').textContent = '点一行：出个股 K 线';
    tb.innerHTML = '<tr><td colspan="13" class="empty">在左边点一个板块</td></tr>';
    return;
  }
  const b = head.board || {};
  meta.textContent = rows.length
    ? `${b.name}（${b.category}）· ${rows.length} 只有行情`
      + (head.n_suspended ? ` · 停牌 ${head.n_suspended}` : '')
      + (head.n_limit_up ? ` · 涨停 ${head.n_limit_up}` : '')
    : '';
  $('#bpMemberHint').textContent = head.reason
    ? head.reason
    : `点一行：出个股 K 线（${esc(head.date)}）`;

  if (!rows.length) {
    tb.innerHTML = `<tr><td colspan="13" class="empty">${esc(head.reason || '该板块当日没有成分股行情')}</td></tr>`;
    return;
  }
  const idx = orderBy(rows, state.bpMemberSort, BP_MEMBER_SORT_COLS);
  tb.innerHTML = idx.map((i) => {
    const r = rows[i];
    const tag = r.limit === '涨停'
      ? '<span class="tag buy">涨停</span>'
      : (r.limit === '跌停' ? '<span class="tag ok">跌停</span>' : '');
    const sel = state.bpStock && state.bpStock.code === r.code ? ' sel' : '';
    return `<tr class="clickable${sel}" data-code="${esc(r.code)}">
      <td class="code">${esc(r.symbol || r.code)}</td>
      <td>${esc(r.name || '')}</td>
      <td>${r.industry_l2 ? `<span class="ind">${esc(r.industry_l2)}</span>` : '<span class="muted">-</span>'}</td>
      ${bpPctCell(r.pct)}
      <td class="num">${fmt(r.close, 2)}</td>
      <td class="num">${fmt(r.amount_yi, 2)}</td>
      <td class="num">${fmt(r.vol_ratio, 2)}</td>
      ${bpPctCell(r.turnover)}
      <td class="num">${fmtWan(r.volume_hand)}</td>
      <td class="num">${fmt(r.float_mcap_yi, 1)}</td>
      <td class="num">${r.concept_n ?? '-'}</td>
      <td><span class="tag">${esc(r.board || '')}</span></td>
      <td>${tag}</td>
    </tr>`;
  }).join('');
}

/* ---- K 线：主图 + 成交量 + MACD 三栏（板块指数与个股同一套渲染） ---- */
function bpChartOption(d, bars) {
  const volColors = d.ohlc.map((o) => (o[1] >= o[0] ? UP : DOWN));
  const macd = d.macd || { dif: [], dea: [], macd: [] };
  const macdBars = (macd.macd || []).map((v) => ({
    value: v,
    itemStyle: { color: Number(v) >= 0 ? UP : DOWN },
  }));
  const baseGrid = { left: 56, right: 20 };
  const axisCommon = {
    type: 'category', boundaryGap: true, min: 'dataMin', max: 'dataMax',
    splitLine: { show: false },
  };
  return {
    animation: false,
    backgroundColor: 'transparent',
    textStyle: { color: C.text2, fontSize: 11 },
    legend: {
      top: 2, left: 8, itemGap: 12, itemWidth: 14, itemHeight: 8,
      textStyle: { color: C.muted, fontSize: 11 }, inactiveColor: '#48484a',
      data: ['K线', 'MA5', 'MA10', 'MA20', 'MA60', '成交量', '20日均量', 'MACD', 'DIF', 'DEA'],
    },
    tooltip: {
      trigger: 'axis', axisPointer: { type: 'cross' },
      backgroundColor: C.glass, borderColor: C.line,
      extraCssText: 'backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);',
      textStyle: { color: C.text, fontSize: 12 },
      formatter: (ps) => {
        const i = ps[0].dataIndex;
        const h = d.ohlc[i];
        if (!h) return '';
        const prev = i > 0 ? d.ohlc[i - 1][1] : h[0];
        const chg = prev ? (h[1] / prev - 1) * 100 : 0;
        const c = chg >= 0 ? UP : DOWN;
        const dif = macd.dif[i];
        const dea = macd.dea[i];
        return `<b>${d.dates[i]}</b><br/>开 ${fmt(h[0])}　高 ${fmt(h[3])}<br/>`
          + `低 ${fmt(h[2])}　收 <b style="color:${c}">${fmt(h[1])}</b>　`
          + `涨跌 <span style="color:${c}">${chg.toFixed(2)}%</span><br/>`
          + `量 ${fmtInt(d.volume[i])} 手　量比 <b>${fmt(d.vol_ratio[i], 2)}×</b><br/>`
          + `MACD DIF ${fmt(dif, 3)}　DEA ${fmt(dea, 3)}`;
      },
    },
    axisPointer: { link: [{ xAxisIndex: 'all' }] },
    grid: [
      { ...baseGrid, top: 26, height: 186 },
      { ...baseGrid, top: 224, height: 68 },
      { ...baseGrid, top: 304, height: 68 },
    ],
    xAxis: [0, 1, 2].map((g) =>
      g === 2
        ? { ...axisCommon, gridIndex: g, data: d.dates,
            axisLine: { lineStyle: { color: C.line } },
            axisLabel: { color: C.muted, formatter: (v) => String(v).slice(5) } }
        : { ...axisCommon, gridIndex: g, data: d.dates,
            axisLine: { lineStyle: { color: C.line } }, axisLabel: { show: false } }
    ),
    yAxis: [
      { scale: true, gridIndex: 0, splitLine: { lineStyle: { color: C.grid } },
        axisLabel: { color: C.muted }, axisLine: { show: false } },
      { gridIndex: 1, splitLine: { show: false }, axisLine: { show: false },
        axisTick: { show: false }, axisLabel: { color: C.muted, showMaxLabel: false } },
      { scale: true, gridIndex: 2, splitLine: { show: false }, axisLine: { show: false },
        axisTick: { show: false }, axisLabel: { color: C.muted, showMaxLabel: false } },
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1, 2], start: 55, end: 100 },
      { type: 'slider', xAxisIndex: [0, 1, 2], bottom: 6, height: 14,
        start: 55, end: 100, borderColor: C.line,
        fillerColor: 'rgba(10,132,255,.16)',
        handleStyle: { color: C.accent }, moveHandleStyle: { color: C.accent },
        dataBackground: { lineStyle: { color: C.line }, areaStyle: { color: C.grid } } },
    ],
    series: [
      {
        name: 'K线', type: 'candlestick', data: d.ohlc, xAxisIndex: 0, yAxisIndex: 0,
        itemStyle: { color: UP, color0: DOWN, borderColor: UP, borderColor0: DOWN },
      },
      { name: 'MA5', type: 'line', data: d.ma.ma5, xAxisIndex: 0, yAxisIndex: 0, showSymbol: false, lineStyle: { width: 1, color: C.ma5 }, itemStyle: { color: C.ma5 } },
      { name: 'MA10', type: 'line', data: d.ma.ma10, xAxisIndex: 0, yAxisIndex: 0, showSymbol: false, lineStyle: { width: 1, color: C.ma10 }, itemStyle: { color: C.ma10 } },
      { name: 'MA20', type: 'line', data: d.ma.ma20, xAxisIndex: 0, yAxisIndex: 0, showSymbol: false, lineStyle: { width: 1.4, color: C.ma20 }, itemStyle: { color: C.ma20 } },
      { name: 'MA60', type: 'line', data: d.ma.ma60, xAxisIndex: 0, yAxisIndex: 0, showSymbol: false, lineStyle: { width: 1, color: C.ma60 }, itemStyle: { color: C.ma60 } },
      { name: '成交量', type: 'bar', data: d.volume, xAxisIndex: 1, yAxisIndex: 1,
        itemStyle: { color: (p) => volColors[p.dataIndex] } },
      { name: '20日均量', type: 'line', data: d.volume_ma, xAxisIndex: 1, yAxisIndex: 1,
        showSymbol: false, lineStyle: { width: 1.2, color: C.volma }, itemStyle: { color: C.volma } },
      { name: 'MACD', type: 'bar', data: macdBars, xAxisIndex: 2, yAxisIndex: 2 },
      { name: 'DIF', type: 'line', data: macd.dif, xAxisIndex: 2, yAxisIndex: 2,
        showSymbol: false, lineStyle: { width: 1, color: C.ma5 }, itemStyle: { color: C.ma5 } },
      { name: 'DEA', type: 'line', data: macd.dea, xAxisIndex: 2, yAxisIndex: 2,
        showSymbol: false, lineStyle: { width: 1, color: C.volma }, itemStyle: { color: C.volma } },
    ],
  };
}

async function loadBoardIndexKline(row) {
  const bars = 160;
  let d;
  try {
    d = await api(`/api/board_kline/${encodeURIComponent(row.index_code)}?bars=${bars}`);
  } catch (e) { toast('加载板块指数 K 线失败：' + e.message, 'err'); return; }
  if (!d.ok) { toast(d.reason || '加载失败', 'err'); return; }
  $('#bpIndexTitle').textContent = `${d.code} ${d.name} · 板块指数 K 线`;
  $('#bpIndexMeta').textContent =
    `${d.category} · 成分 ${d.n_members} 只 · ${d.bars} 根 · 最新 ${d.latest.date} 收 ${fmt(d.latest.close, 2)}`;
  if (!state.bpIndexChart) state.bpIndexChart = echarts.init($('#bpIndexChart'));
  state.bpIndexChart.setOption(bpChartOption(d, bars), true);
  state.bpIndexChart.resize();
}

async function loadBoardStockKline(row) {
  $('#bpStockTitle').textContent = `${row.code} ${row.name || ''} · K 线`;
  let d;
  try {
    d = await api(`/api/kline/${encodeURIComponent(row.code)}?bars=160&ma_window=20`);
  } catch (e) { toast('加载个股 K 线失败：' + e.message, 'err'); return; }
  if (!d.ok) { toast(d.reason || '加载失败', 'err'); return; }
  $('#bpStockMeta').textContent =
    `${row.industry_l2 || '未分类'} · ${d.bars} 根 · 最新 ${d.latest.date} 收 ${fmt(d.latest.close, 2)}`;
  if (!state.bpStockChart) state.bpStockChart = echarts.init($('#bpStockChart'));
  state.bpStockChart.setOption(bpChartOption(d, 160), true);
  state.bpStockChart.resize();
}

function bindBoard() {
  $('#btnBoardLoad').onclick = loadBoardPanel;
  // 类别 tab：点已选中的那个不重复请求
  $('#bpTabs').addEventListener('click', (ev) => {
    const b = ev.target.closest('.bp-tab[data-cat]');
    if (!b || b.dataset.cat === state.bpCategory) return;
    setBoardCategory(b.dataset.cat);
    clearBoardPicks();          // 换类别＝整个板块池都换了，右下的残留先清掉
    loadBoardPanel();
  });
  $('#bpOnlyMembers').onchange = loadBoardPanel;
  $('#bpSearch').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); loadBoardPanel(); }
  });
  $('#bpDate').onchange = (e) => { snapTradeDate(e.target, { notify: true }); loadBoardPanel(); };
  $('#bpDateLatest').onclick = () => { $('#bpDate').value = ''; loadBoardPanel(); };

  bindSortHead('#bpBoardTable', state.bpSort, BOARD_SORT_COLS, () => {
    paintSortHeaders('#bpBoardTable', state.bpSort);
    renderBoardTable();
  });
  bindSortHead('#bpMemberTable', state.bpMemberSort, BP_MEMBER_SORT_COLS, () => {
    paintSortHeaders('#bpMemberTable', state.bpMemberSort);
    renderBoardMembers();
  });
  paintSortHeaders('#bpBoardTable', state.bpSort);
  paintSortHeaders('#bpMemberTable', state.bpMemberSort);

  $('#bpBoardTable').addEventListener('click', (ev) => {
    const tr = ev.target.closest('tr[data-code]');
    if (!tr) return;
    const row = state.bpRows.find((r) => r.index_code === tr.dataset.code);
    if (row) selectBoard(row);
  });
  $('#bpMemberTable').addEventListener('click', (ev) => {
    const tr = ev.target.closest('tr[data-code]');
    if (!tr) return;
    const row = state.bpMemberRows.find((r) => r.code === tr.dataset.code);
    if (!row) return;
    state.bpStock = row;
    renderBoardMembers();
    loadBoardStockKline(row);
  });
}

/* ------------------------------------------------------------------ */
/* 自选板块（通达信 T0002/blocknew）                                    */
/* ------------------------------------------------------------------ */
async function loadWatchBlocks(opts) {
  const o = opts || {};
  let d;
  try {
    d = await api('/api/watch_blocks');
  } catch (e) {
    if (!o.silent) toast('加载自选板块失败：' + e.message, 'err');
    return null;
  }
  if (!d.ok) {
    if (!o.silent) toast(d.reason || '加载自选板块失败', 'err');
    return null;
  }
  const prevKeys = (state.wbCatalog && state.wbCatalog.blocks)
    ? state.wbCatalog.blocks.map((b) => b.key) : null;
  state.wbCatalog = d;
  const sel = $('#wbSelect');
  const blocks = d.blocks || [];
  // 下拉只显示「显示名 · 成员数」：
  //  - blocknew.cfg 里没改过名的板块，显示名就等于文件名（60-11-14 / GL-12-03 …），
  //    不再画蛇添足补「（xxx.blk）」—— 文件名对用户没有意义，还会把选项撑破；
  //  - 成员数为 0 的板块保留在列表里（用户可能刚建好还没加股票），置灰不可选。
  sel.innerHTML = blocks.map((b) => {
    const label = b.name || b.key;
    const count = b.has_members ? String(b.n) : '空';
    return `<option value="${esc(b.key)}"${b.has_members ? '' : ' disabled'}>`
      + `${esc(label)} · ${count}</option>`;
  }).join('');
  $('#wbCount').textContent = d.total ? String(d.with_members) : '';
  $('#wbCount').title = `${d.total} 个自选板块（${d.with_members} 个有成分股）`;
  // 选中项：① 重新扫描时优先保留用户当前选中的那个（只要它还在）；
  //         ② 否则回落到第一个有成分的板块，省得用户先选一次。
  const keys = blocks.map((b) => b.key);
  let want = (o.keep && state.wbKey && keys.includes(state.wbKey)) ? state.wbKey : '';
  if (!want) {
    const first = blocks.find((b) => b.has_members) || blocks[0];
    want = first ? first.key : '';
  }
  if (want) {
    sel.value = want;
    state.wbKey = want;
  } else {
    sel.value = '';
    state.wbKey = '';
  }
  // 重新扫描时告诉用户「这一趟到底有没有变化」——没变化也要有反馈，
  // 否则用户会以为按钮没生效而反复点。
  if (o.notify) {
    if (prevKeys === null) {
      toast(`已扫描到 ${d.total} 个自选板块`, 'ok');
    } else {
      const added = keys.filter((k) => !prevKeys.includes(k));
      const removed = prevKeys.filter((k) => !keys.includes(k));
      if (!added.length && !removed.length) {
        toast(`板块清单无变化（${d.total} 个）`, 'ok');
      } else {
        const bits = [];
        if (added.length) bits.push(`新增 ${added.length} 个：${added.slice(0, 3).join('、')}`
          + (added.length > 3 ? ' 等' : ''));
        if (removed.length) bits.push(`移除 ${removed.length} 个：${removed.slice(0, 3).join('、')}`
          + (removed.length > 3 ? ' 等' : ''));
        toast(`板块清单已更新（共 ${d.total} 个）· ${bits.join('；')}`, 'ok', 6000);
      }
    }
  }
  return d;
}

function wbQuery() {
  const q = new URLSearchParams({ block: state.wbKey, sort: 'amount', max_rows: '3000' });
  const date = $('#wbDate').value;
  if (date) q.set('date', date);
  if ($('#wbMinAmount').value) q.set('min_amount', String(Number($('#wbMinAmount').value) * 1e8));
  q.set('exclude_st', $('#wbExcludeSt').checked ? 'true' : 'false');
  if (state.wbIndFilter.length) q.set('industries', state.wbIndFilter.join(','));
  if (state.wbCptFilter.length) {
    q.set('concepts', state.wbCptFilter.join(','));
    q.set('concept_mode', state.wbCptMode);
  }
  return q;
}

async function loadWatchBlockPanel() {
  if (!state.wbKey) {
    const d = await loadWatchBlocks();
    if (!state.wbKey) { toast('本地没有自选板块（T0002/blocknew）', 'warn'); return; }
  }
  let d;
  try {
    d = await api('/api/watch_block_panel?' + wbQuery().toString());
  } catch (e) { toast('加载自选板块失败：' + e.message, 'err'); return; }
  if (!d.ok) {
    toast(d.reason || '加载失败', 'err');
    // 板块本身无效（被用户删了）→ 重新拉一次清单
    if (d.reason && d.reason.indexOf('未知自选板块') === 0) { state.wbKey = ''; loadWatchBlocks(); }
    return;
  }
  state.wbPanel = d;
  state.wbRows = d.rows || [];
  renderWatchBlock();
}

function renderWatchBlock() {
  const d = state.wbPanel;
  if (!d) return;
  renderWbConditions(d);
  renderWbKpi(d);
  renderWbIndSummary(d);
  renderWbCptSummary(d);
  renderWbTable();
}

function renderWbConditions(d) {
  const b = d.block || {};
  const bname = b.name || b.key || '';
  // 只有「显示名 ≠ 文件名」时才补文件名（用户改过名的板块），否则纯属噪音
  const bfile = b.key && b.key !== bname ? `（${esc(b.key)}）` : '';
  const items = [
    `自选板块 <b>${esc(bname)}</b>${bfile} · 成分 <b>${d.n_total ?? 0}</b> 只`,
    `交易日 <b>${esc(d.date || '-')}</b> · 当日有行情 <b>${d.candidates ?? 0}</b> 只`
      + (d.n_suspended ? ` · 停牌/未上市 ${d.n_suspended} 只` : ''),
    (d.industries && d.industries.length)
      ? `二级行业筛选：${d.industries.map((s) => `<b>${esc(s)}</b>`).join('、')}`
      : '不限二级行业',
    (d.concepts && d.concepts.length)
      ? `概念筛选（${d.concept_mode === 'all' ? '全部命中' : '任一命中'}）：`
        + d.concepts.map((s) => `<b>${esc(s)}</b>`).join('、')
      : '不限概念',
    `命中 <b>${d.matched ?? 0}</b> 只`,
  ];
  $('#wbConditions').innerHTML = items.map((t) => `<li>${t}</li>`).join('');
}

function renderWbKpi(d) {
  const ix = d.industry_summary || {};
  const cp = d.concept_summary || {};
  const items = [
    { k: '板块成分', v: d.n_total ?? 0, s: 'blocknew 文件静态成员数' },
    { k: '当日有行情', v: d.candidates ?? 0, s: d.n_suspended ? `停牌/未上市 ${d.n_suspended} 只` : '全部有行情' },
    {
      k: '当前命中', v: d.matched ?? 0,
      cls: d.matched === 0 ? 'warn' : '',
      s: `占当日有行情 ${d.candidates ? ((d.matched / d.candidates) * 100).toFixed(1) : '0.0'}%`,
    },
    { k: '覆盖行业', v: (ix.groups || []).length, s: `个二级行业${ix.unmapped ? ` · ${ix.unmapped} 只无分类` : ''}` },
    { k: '覆盖概念', v: (cp.groups || []).length, s: `个概念${cp.unmapped ? ` · ${cp.unmapped} 只无分类` : ''}` },
    { k: '平均概念数', v: cp.avg ?? 0, s: `${cp.tags ?? 0} 个概念归属` },
  ];
  $('#wbKpi').innerHTML = items.map((x) => `
    <div class="kpi ${x.cls || ''}">
      <div class="k">${esc(x.k)}</div>
      <div class="v">${typeof x.v === 'number' ? x.v : esc(String(x.v))}</div>
      <div class="s">${esc(x.s)}</div>
    </div>`).join('');
  $('#wbKpiCard').hidden = false;
}

/* 两个分布卡：基数固定为「该板块当日有行情的成员数」（后端 summary_base），
   不随行业/概念筛选变化 —— 所以点掉一个 chip 之后其余 chip 仍在，可以连续切换。 */
function renderWbIndSummary(d) {
  const card = $('#wbIndCard');
  const box = $('#wbIndSummary');
  const ix = d.industry_summary || {};
  const groups = ix.groups || [];
  if (!groups.length) { card.hidden = true; box.innerHTML = ''; return; }
  card.hidden = false;
  const base = d.summary_base ?? d.matched;
  const on = new Set(state.wbIndFilter);
  let top = groups.slice(0, 40);
  on.forEach((name) => {
    if (!top.some((g) => g.l2 === name)) {
      const cur = groups.find((g) => g.l2 === name);
      if (cur) top = [cur].concat(top);
    }
  });
  $('#wbIndMeta').textContent =
    `${base} 只有行情，覆盖 ${groups.length} 个二级行业`
    + (on.size ? ` · 已筛「${[...on].join('、')}」→ ${d.matched} 只` : '')
    + (ix.unmapped ? ` · ${ix.unmapped} 只无行业分类` : '');
  box.innerHTML = top.map((g) => `
    <button class="ind-chip${on.has(g.l2) ? ' active' : ''}"
            data-l2="${esc(g.l2)}" title="点击按「${esc(g.l2)}」筛选；再点取消">
      ${esc(g.l2)} <b>${g.n}</b>
    </button>`).join('')
    + (groups.length > top.length ? `<span class="ind-more">…另有 ${groups.length - top.length} 个行业</span>` : '');
}

function renderWbCptSummary(d) {
  const card = $('#wbCptCard');
  const box = $('#wbCptSummary');
  const cp = d.concept_summary || {};
  const groups = cp.groups || [];
  if (!groups.length) { card.hidden = true; box.innerHTML = ''; return; }
  card.hidden = false;
  const base = d.summary_base ?? d.matched;
  const on = new Set(state.wbCptFilter);
  let top = groups.slice(0, 40);
  on.forEach((name) => {
    if (!top.some((g) => g.concept === name)) {
      const cur = groups.find((g) => g.concept === name);
      if (cur) top = [cur].concat(top);
    }
  });
  $('#wbCptMeta').textContent =
    `${base} 只有行情，覆盖 ${groups.length} 个概念`
    + (on.size ? ` · 已筛 ${on.size} 个概念（${state.wbCptMode === 'all' ? '全部命中' : '任一命中'}）→ ${d.matched} 只` : '')
    + ` · 平均每股 ${cp.avg ?? 0} 个`
    + (cp.unmapped ? ` · ${cp.unmapped} 只无概念分类` : '');
  box.innerHTML = top.map((g) => `
    <button class="ind-chip${on.has(g.concept) ? ' active' : ''}"
            data-concept="${esc(g.concept)}" title="点击按「${esc(g.concept)}」筛选；再点取消">
      ${esc(g.concept)} <b>${g.n}</b>
    </button>`).join('')
    + (groups.length > top.length ? `<span class="ind-more">…另有 ${groups.length - top.length} 个概念</span>` : '');
}

function renderWbTable() {
  const tb = $('#wbTable tbody');
  if (!tb) return;
  const rows = state.wbRows || [];
  const d = state.wbPanel;
  if (!rows.length) {
    tb.innerHTML = `<tr><td colspan="11" class="empty">${
      d ? '当前条件下没有成员股（试试清除筛选）' : '请选择自选板块并点『加载板块』'}</td></tr>`;
    $('#wbMeta').textContent = '';
    return;
  }
  const order = orderBy(rows, state.wbSort, WB_SORT_COLS);
  tb.innerHTML = order.map((i) => {
    const r = rows[i];
    const hits = r.hit_concepts || [];
    const all = r.concepts || [];
    // 没有概念筛选时「命中概念」恒为空，整列都是「—」白占地方 ——
    // 那种情况改展示该股最主要的几个概念，列的意义就还在。
    const show = hits.length ? hits : all.slice(0, 3);
    const hitTitle = hits.length
      ? `命中概念：${hits.join('、')}`
      : (all.length ? `全部概念：${all.join('、')}` : '无概念归属');
    const hitHtml = show.length
      ? show.slice(0, 3).map((c) => `<span class="cpt">${esc(c)}</span>`).join('')
        + ((hits.length ? hits.length : all.length) > 3
          ? `<span class="cpt-more" title="${esc(hitTitle)}">+${(hits.length ? hits.length : all.length) - 3}</span>` : '')
      : '<span class="hit-empty">—</span>';
    const indTitle = [r.industry_l1, r.industry_l2, r.industry_l3]
      .filter(Boolean).join(' / ') || '无行业分类';
    const tag = r.is_st ? ' <span class="muted">ST</span>' : '';
    // 涨跌停标记：复用连板页的 tag 风格
    const lim = r.limit === '涨停' ? '<span class="tag buy">涨停</span>'
      : (r.limit === '跌停' ? '<span class="tag ok">跌停</span>' : '');
    return `
    <tr class="clickable" data-code="${esc(r.code)}">
      <td class="code">${esc(r.symbol || r.code)}</td>
      <td><b>${esc(r.name || '')}</b>${tag}</td>
      <td><span class="ind" title="${esc(indTitle)}">${esc(r.industry_l2 || '—')}</span></td>
      <td class="hit-cell" title="${esc(hitTitle)}">${hitHtml}</td>
      <td class="num" title="本地板块文件收录的全部概念：${esc(all.join('、') || '无')}">${r.concept_n ?? 0}</td>
      <td class="num">${fmt(r.close, 2)}</td>
      <td class="num ${pctClass(r.pct_change)}">${fmtPct(r.pct_change)}</td>
      <td class="num">${fmt(r.amount_yi, 2)}</td>
      <td class="num">${fmt(r.float_mcap_yi, 1)}</td>
      <td>${esc(r.board || '')}</td>
      <td>${lim}</td>
    </tr>`;
  }).join('');
  const b = d ? (d.block || {}) : {};
  $('#wbMeta').textContent =
    `${rows.length} 只（${b.name || ''} 当日有行情 ${d.candidates ?? 0} 只`
    + (d.matched !== d.candidates ? `，筛选后 ${d.matched} 只` : '')
    + '）'
    + (d.truncated ? ` · 已截断，共 ${d.total} 只` : '');
}

function updateWbSortUI() {
  const el = $('#wbSortHint');
  if (!el) return;
  const th = document.querySelector(`#wbTable thead th[data-sort="${state.wbSort.key}"]`);
  const label = th ? (th.dataset.label || state.wbSort.key) : state.wbSort.key;
  const dir = state.wbSort.dir === 'desc' ? '降序' : '升序';
  el.textContent = `点行看 K 线 · 排序：${label} ${dir}`;
  el.title = `点击表头排序，再点同一列反向；当前按「${label}」${dir}`;
  const btn = $('#wbSortReset');
  if (btn) {
    btn.hidden = state.wbSort.key === WB_DEFAULT_SORT.key
      && state.wbSort.dir === WB_DEFAULT_SORT.dir;
  }
}

/* 自选板块成员行复用选股抽屉：先补上抽屉需要的默认值 */
function openWbDrawer(r) {
  const sellP = (Number($('#fSellProfit').value) || 4.5) / 100;
  openDrawer({
    ...r,
    signal_date: r.trade_date,
    buy_price: r.close,
    sell_price: Number((r.close * (1 + sellP)).toFixed(2)),
    vol_ratio: null,
    ma_volume: null,
    volume: null,
  });
}

function exportWbCsv() {
  const rows = state.wbRows || [];
  if (!rows.length) { toast('没有可导出的结果', 'warn'); return; }
  const cols = ['symbol', 'name', 'board', 'industry_l1', 'industry_l2', 'industry_l3',
    'hit_concepts', 'concepts', 'concept_n', 'trade_date', 'close', 'pct_change',
    'amount_yi', 'float_mcap_yi'];
  const head = ['代码', '名称', '板块', '一级行业', '二级行业', '三级行业',
    '命中概念', '全部概念', '概念数', '交易日', '收盘价', '涨跌幅',
    '成交额(亿)', '流通市值(亿)'];
  const cell = (s) => (/[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s);
  const lines = [head.join(',')];
  orderBy(rows, state.wbSort, WB_SORT_COLS).forEach((i) => {
    const r = rows[i];
    lines.push(cols.map((c) => {
      if (c === 'concepts') return cell((r.concepts || []).join('、'));
      if (c === 'hit_concepts') return cell((r.hit_concepts || []).join('、'));
      const v = r[c];
      return v === null || v === undefined ? '' : String(v);
    }).join(','));
  });
  const blob = new Blob(['\uFEFF' + lines.join('\n')], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  const b = state.wbPanel ? (state.wbPanel.block || {}) : {};
  a.download = `自选板块_${b.name || state.wbKey || 'block'}_${(state.wbPanel && state.wbPanel.date) || 'latest'}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
}

function bindWatchBlock() {
  // 同板块交集页：元素多，先整体查一遍，缺了就只跳过本页，
  // 免得一处拼错 id 把后面的绑定全部带停（表现为「其他页签也一起坏了」）。
  const needed = ['#wbSelect', '#wbDate', '#wbDateLatest', '#wbMinAmount', '#wbExcludeSt',
    '#btnWbLoad', '#btnWbRescan', '#btnWbClear', '#wbKpiCard', '#wbIndCard', '#wbCptCard',
    '#wbIndSummary', '#wbCptSummary', '#btnWbIndClear', '#btnWbCptClear', '#wbCptMode',
    '#wbTable', '#btnWbExport', '#wbSortReset'];
  const missing = needed.filter((s) => !$(s));
  if (missing.length) {
    console.warn('自选板块页元素缺失，已跳过绑定：', missing);
    return;
  }
  $('#btnWbLoad').onclick = loadWatchBlockPanel;
  // 重新扫描：用户在通达信里改动自选板块之后，不必重启服务、也不必手动刷新页面。
  // 后端每次请求都会重算指纹；这里保留当前选中的板块，只重画下拉 + 重取面板。
  $('#btnWbRescan').onclick = async () => {
    const btn = $('#btnWbRescan');
    btn.disabled = true;
    try {
      const d = await loadWatchBlocks({ keep: true, notify: true });
      if (d && state.wbKey) await loadWatchBlockPanel();
    } finally {
      btn.disabled = false;
    }
  };
  $('#wbSelect').onchange = (e) => {
    if (e.target.value === state.wbKey) return;
    state.wbKey = e.target.value;
    // 换板块＝整个成员集都换了，筛选与排序先复位，免得挂着上一个板块的条件
    state.wbIndFilter = [];
    state.wbCptFilter = [];
    loadWatchBlockPanel();
  };
  $('#wbDate').onchange = (e) => { snapTradeDate(e.target, { notify: true }); loadWatchBlockPanel(); };
  $('#wbDateLatest').onclick = () => { $('#wbDate').value = ''; loadWatchBlockPanel(); };
  $('#wbMinAmount').onchange = loadWatchBlockPanel;
  $('#wbExcludeSt').onchange = loadWatchBlockPanel;
  $('#wbCptMode').onchange = (e) => {
    state.wbCptMode = e.target.value;
    if (state.wbCptFilter.length) loadWatchBlockPanel();
  };
  $('#btnWbClear').onclick = () => {
    if (!state.wbIndFilter.length && !state.wbCptFilter.length
        && !$('#wbMinAmount').value && !$('#wbDate').value) {
      toast('当前没有筛选条件', 'warn');
      return;
    }
    state.wbIndFilter = [];
    state.wbCptFilter = [];
    $('#wbMinAmount').value = '0';
    $('#wbDate').value = '';
    loadWatchBlockPanel();
  };

  // chip 点击 = 切换该行业/概念的选中态，然后重算（服务端过滤，保证与分布卡同口径）
  $('#wbIndSummary').addEventListener('click', (ev) => {
    const chip = ev.target.closest('.ind-chip');
    if (!chip) return;
    const name = chip.dataset.l2;
    const i = state.wbIndFilter.indexOf(name);
    if (i >= 0) state.wbIndFilter.splice(i, 1);
    else state.wbIndFilter.push(name);
    loadWatchBlockPanel();
  });
  $('#wbCptSummary').addEventListener('click', (ev) => {
    const chip = ev.target.closest('.ind-chip');
    if (!chip) return;
    const name = chip.dataset.concept;
    const i = state.wbCptFilter.indexOf(name);
    if (i >= 0) state.wbCptFilter.splice(i, 1);
    else state.wbCptFilter.push(name);
    loadWatchBlockPanel();
  });
  $('#btnWbIndClear').onclick = () => {
    if (!state.wbIndFilter.length) { toast('当前未设置行业筛选', 'warn'); return; }
    state.wbIndFilter = [];
    loadWatchBlockPanel();
  };
  $('#btnWbCptClear').onclick = () => {
    if (!state.wbCptFilter.length) { toast('当前未设置概念筛选', 'warn'); return; }
    state.wbCptFilter = [];
    loadWatchBlockPanel();
  };

  bindSortHead('#wbTable', state.wbSort, WB_SORT_COLS, () => {
    paintSortHeaders('#wbTable', state.wbSort);
    updateWbSortUI();
    renderWbTable();
  });
  paintSortHeaders('#wbTable', state.wbSort);
  updateWbSortUI();
  $('#wbSortReset').onclick = () => {
    state.wbSort = { ...WB_DEFAULT_SORT };
    paintSortHeaders('#wbTable', state.wbSort);
    updateWbSortUI();
    renderWbTable();
  };
  $('#wbTable').addEventListener('click', (ev) => {
    const tr = ev.target.closest('tr[data-code]');
    if (!tr) return;
    const row = state.wbRows.find((r) => r.code === tr.dataset.code);
    if (row) openWbDrawer(row);
  });
  $('#btnWbExport').onclick = exportWbCsv;
}

/* ------------------------------------------------------------------ */
/* 初始化                                                              */
/* ------------------------------------------------------------------ */
const VIEW_META = {
  screen: {
    title: '选股',
    sub: '本地通达信日线 · 放量 ≥ N 倍 M 日均量 · 二级行业 / 概念分类 · 计划买入/卖出价 · 追踪',
  },
  streak: {
    title: '连板梯队',
    sub: '当日涨停个股按连板高度分层 · 封板形态 / 二级行业 / 概念 / 晋级率 · 点标签看 K 线',
  },
  intersect: {
    title: '板块交集',
    sub: '二级行业 ∩ 概念 = 同时属于两侧的个股 · 左侧行业多选（并集）· 右侧概念可任一/全部 · 点行看 K 线',
  },
  board: {
    title: '板块看盘',
    sub: '顶部 tab 切「概念板块 / 行业板块」（本地有成分的两类，来源于通达信板块指数表）· 涨幅 / 成交额 / 量比 / 成分涨跌家数 · 上下两栏联动，左看板块右看成分股',
  },
  watchblock: {
    title: '自选板块',
    sub: '读通达信 T0002/blocknew 的自选板块（你自己维护的那批）· 成员股做二级行业 / 概念映射 · 点分布标签即筛选，基数不随筛选变化 · 点行看 K 线',
  },
  plan: {
    title: '计划',
    sub: '以放量日收盘价为基准：买入价可改，卖出价默认 +4.5% · 存于本地 SQLite',
  },
  watch: {
    title: '追踪',
    sub: '最新价、区间高低、是否到买入价 / 已达卖出价 · 存于本地 SQLite',
  },
};

function switchTab(name) {
  document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.panel').forEach((p) => p.classList.toggle('active', p.id === 'panel-' + name));
  const meta = VIEW_META[name];
  if (meta) {
    $('#viewTitle').textContent = meta.title;
    $('#viewSub').textContent = meta.sub;
  }
  if (name === 'plan') loadPlans();
  if (name === 'watch') loadWatch();
  // 板块看盘：首次切过去时加载（ECharts 必须在面板可见后再 init，否则宽度为 0）
  if (name === 'board') {
    if (!state.bpLoaded) {
      state.bpLoaded = true;
      loadBoardCatalog().then(loadBoardPanel).catch((e) => toast('加载板块失败：' + e.message, 'err'));
    } else {
      setTimeout(() => {
        if (state.bpIndexChart) state.bpIndexChart.resize();
        if (state.bpStockChart) state.bpStockChart.resize();
      }, 80);
    }
  }
  // 连板梯队：首次切过去时加载；已有数据就只是把图重新量一次尺寸
  if (name === 'streak') {
    if (!state.streak) loadStreaks();
    else if (state.streakChart) setTimeout(() => state.streakChart.resize(), 80);
  }
  // 自选板块：**每次切过来都重扫清单**（后端每次请求都会重算源文件指纹，
  // 0.34ms），这样在通达信里新建 / 改名 / 删掉自选板块之后，只要回到本页
  // 就能看到最新结果 —— 不必重启服务、也不必手动点「重新扫描板块」。
  // 首次进入才顺带取面板；后续切回来只在「选中项已失效」时重取，避免白跑。
  if (name === 'watchblock') {
    // 「首次」以**面板是否已存在**为准，而不是只看 wbLoaded 标志：
    // init() 末尾会后台预取一次清单+面板，那时 wbLoaded 还是 false，
    // 若只看标志就会把预取过的面板当首次、白跑一趟。
    const first = !state.wbPanel;
    state.wbLoaded = true;
    loadWatchBlocks({ keep: !first, notify: false })
      .then((d) => {
        if (!d) return null;
        if (first || !state.wbPanel) return loadWatchBlockPanel();
        // 之前选中的板块被删了 → 面板要跟着换成新的选中项
        if (state.wbPanel.block && state.wbPanel.block.key !== state.wbKey) {
          return loadWatchBlockPanel();
        }
        return null;
      })
      .catch((e) => toast('加载自选板块失败：' + e.message, 'err'));
  }
  if (state.klineChart) setTimeout(() => state.klineChart.resize(), 80);
}

async function init() {
  document.querySelectorAll('.tab').forEach((t) => t.addEventListener('click', () => switchTab(t.dataset.tab)));
  $('#btnRefresh').onclick = doRefresh;
  $('#btnVerify').onclick = doVerify;
  $('#btnNames').onclick = doReloadNames;
  $('#btnBackup').onclick = doBackup;
  $('#btnScreen').onclick = doScreen;
  $('#btnPlanAll').onclick = planAll;
  $('#btnExport').onclick = exportCsv;
  $('#btnPlanReload').onclick = loadPlans;
  $('#btnWatchReload').onclick = loadWatch;
  $('#btnDrawerClose').onclick = closeDrawer;
  $('#drawerMask').onclick = closeDrawer;
  $('#btnKlineReload').onclick = loadKline;
  bindScreenTable();
  bindScreenSort();
  $('#btnSortReset').onclick = () => {
    state.sort = { ...DEFAULT_SORT };
    renderSortHeaders();
    renderScreen();
  };
  bindPlanTable();
  bindWatchTable();
  bindIndustrySummary();
  bindConceptSummary();
  // ---- 连板梯队 ----
  $('#btnStreak').onclick = loadStreaks;
  $('#btnStreakExport').onclick = exportStreakCsv;
  $('#streakSortReset').onclick = () => {
    state.streakSort = { key: 'streak', dir: 'desc' };
    paintSortHeaders('#streakTable', state.streakSort);
    updateStreakSortUI();
    renderStreakTable();
  };
  bindStreakTable();
  bindStreakSummary();
  // ---- 板块交集 ----
  bindIntersect();
  bindSortHead('#streakTable', state.streakSort, STREAK_SORT_COLS, () => {
    paintSortHeaders('#streakTable', state.streakSort);
    updateStreakSortUI();
    renderStreakTable();
  });
  $('#sDate').onchange = (e) => {
    snapTradeDate(e.target, { notify: true });
    loadStreaks();
  };
  $('#sDateLatest').onclick = () => {
    $('#sDate').value = '';
    loadStreaks();
  };
  $('#sMinStreak').onchange = loadStreaks;
  $('#sStLimit').onchange = loadStreaks;
  document.querySelectorAll('.sBd').forEach((o) => { o.onchange = loadStreaks; });
  $('#fIndustry').onchange = (e) => { state.indFilter = e.target.value; doScreen(); };
  // 概念输入框：回车即筛；失焦不改输入（允许慢慢敲完再点选股）
  $('#fConcept').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); doScreen(); }
  });
  $('#fConceptClear').onclick = () => {
    if (!conceptList().length) { toast('当前未设置概念筛选', 'warn'); return; }
    $('#fConcept').value = '';
    doScreen();
  };
  $('#fConceptMode').onchange = () => { if (conceptList().length) doScreen(); };
  $('#fDate').onchange = (e) => snapTradeDate(e.target, { notify: true });
  $('#fDateLatest').onclick = () => { $('#fDate').value = ''; };
  $('#staleReload').onclick = () => location.reload();
  // ---- 板块看盘 ----
  bindBoard();
  // ---- 自选板块 ----
  bindWatchBlock();
  window.addEventListener('resize', () => {
    if (state.klineChart) state.klineChart.resize();
    if (state.streakChart) state.streakChart.resize();
    if (state.bpIndexChart) state.bpIndexChart.resize();
    if (state.bpStockChart) state.bpStockChart.resize();
  });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDrawer(); });

  // 启动即预加载：先等后端把本地已存在的数据全部载入（可能包含重建行情缓存）
  const boot = await waitBootstrap();

  await loadStatus();
  await loadVersion();              // 记下本页加载时的前端资源指纹
  await loadTradeDates();
  await loadIndustries();
  await loadConcepts();
  // 板块交集的两个多选器依赖上面两份目录，这里一次性铺好（切页即见）
  renderIndPicker();
  renderCptPicker();
  renderIxChosen();
  ixEmptyState();
  await loadCounts();

  // 服务端换版本（重新打包/改了 static）→ 本页自动提示刷新；
  // 切回标签页时立刻查一次，避免盯着一个过期页面。
  setInterval(() => { if (!document.hidden) loadVersion(); }, 20000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) loadVersion(); });

  const s = await api('/api/status').catch(() => null);
  const autoScreen = !(boot && boot.settings && boot.settings.auto_screen === false);
  if (autoScreen && s && s.cache && s.cache.exists) {
    await doScreen();                 // 自动跑一次选股，不用手点
    loadPlans().catch(() => {});      // 计划 / 追踪也一并预取，切页即见
    loadWatch().catch(() => {});
  }

  // 自选板块也后台预取：不阻塞首屏（放到最后一个 await 之后），
  // 用户第一次点过去时下拉与面板已经就绪，不会看到一下空白。
  // 预取失败无所谓 —— switchTab 切过去时还会再扫一次。
  loadWatchBlocks({ keep: false, silent: true })
    .then((d) => { if (d && state.wbKey) return loadWatchBlockPanel(); return null; })
    .catch(() => {});
}

document.addEventListener('DOMContentLoaded', init);
