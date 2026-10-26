/* 70-bx-core-a.js — 业务视图的公共底座：常量、金额/计数格式化、卡与表、缓存、请求封装。 */
'use strict';

const BX_NA = '暂不可用';
const BX_API = {
  overview: '/api/business/overview',
  bill: '/api/business/bill',
  plans: '/api/business/plans',
  manage: '/api/business/management',
  security: '/api/business/executions'
};
const BX_TASK_LABEL = {
  draft: '草稿',
  need_info: '等待补充信息',
  waiting_confirm: '等待确认',
  waiting_factor: '等待验证',
  running: '执行中',
  done: '已成功',
  failed: '已失败',
  cancelled: '已取消',
  manual: '需要人工处理'
};
const BX_FACTOR_TEXT = {
  sms: '短信验证码',
  face: '人脸识别',
  otp: '动态口令',
  password: '交易密码'
};

/* 后端返回的是稳定枚举（active / feasible / monthly），界面必须翻成人话，
   否则用户会看到 "可行性 feasible" 这种既不像中文也不像英文的字段值。 */
const BX_GOAL_STATUS = { active: '进行中', achieved: '已达成', cancelled: '已停止' };
const BX_FEASIBILITY = {
  feasible: '按当前收支可行',
  infeasible: '按当前收支有缺口',
  achieved: '已经达成',
  expired: '目标日已过',
  unknown: '数据不足，无法判断'
};
const BX_PERIOD_UNIT = { month: '每月', weekly: '每周', week: '每周' };
const BX_PRODUCT_CATEGORY = { money: '货币型', bond: '债券型', mixed: '混合型', equity: '股票型' };
const BX_SCHED_KIND = {
  in_process_ticker: '应用内定时器（随服务进程启动/停止）',
  external_cron: '外部定时任务',
  none: '没有调度器'
};
const BX_RECURRENCE = { once: '一次性', monthly: '每月重复' };
const BX_ALERT_STATUS = { open: '待处理', ignored: '已忽略', resolved: '已处理' };
const BX_CAP_LABEL = {
  transfer: '转账',
  scheduled_transfer: '预约转账',
  card: '卡片管理',
  subscription: '订阅与代扣',
  investment: '理财',
  reversal: '冲正'
};
const BX_TOOL_PROMPT = {
  freeze_card: '帮我把这张卡临时冻结一下',
  unfreeze_card: '帮我解冻这张卡',
  set_card_limit: '我想调整这张卡的限额',
  report_card_loss: '我的卡丢了，帮我挂失',
  apply_virtual_card: '我想申请一张虚拟卡',
  cancel_subscription: '帮我把这个订阅的银行侧代扣停掉',
  reverse_transfer: '帮我看看那笔转账能不能冲正',
  buy_fund: '我想买一点理财产品',
  redeem_fund: '我想赎回理财',
  transfer: '帮我转 500 元给张三',
  scheduled_transfer: '帮我预约下个月 1 号转 500 元给张三'
};
const BX_HOSTS = {
  overview: ['ov-balance', 'ov-flow', 'ov-cats', 'ov-anomalies', 'ov-pending', 'ov-goals', 'ov-plans', 'ov-handoffs', 'ov-actions'],
  bill: ['bill-summary', 'bill-cats', 'bill-trend', 'bill-anomalies', 'bill-tx'],
  plans: ['plan-banner', 'plan-goals', 'plan-tasks', 'plan-alerts'],
  manage: ['mg-cards', 'mg-subs', 'mg-products', 'mg-caps', 'mg-memory'],
  security: ['sec-summary', 'sec-sessions', 'sec-handoffs', 'sec-denials', 'sec-note']
};

let bxView = 'overview';
let bxBillPeriod = 'month';
/* 接管队列写动作必须带操作员：演示环境固定一个名字，真实环境应取登录态里的工号。 */
const BX_OPERATOR = '演示客服';
const bxCache = {};
const bxSeq = {};
const bxSeen = {};
let bxPolling = false;

/* -------------------------------------------------------- 视图基础小工具 */
function bxIsObj(value) {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}

function bxArr(value) {
  return Array.isArray(value) ? value : [];
}

function bxNum(value) {
  if (value === null || value === undefined || value === '') { return null; }
  const n = typeof value === 'number' ? value : parseFloat(value);
  return isFinite(n) ? n : null;
}

/* 后端同时给 xxx_fen 与格式化好的 xxx_yuan；优先用后端格式化串，避免前端二次计算 */
function bxMoney(obj, base) {
  if (!bxIsObj(obj)) { return BX_NA; }
  const yuan = obj[base + '_yuan'];
  if (yuan !== undefined && yuan !== null && yuan !== '') { return textOf(yuan) + ' 元'; }
  const fen = bxNum(obj[base + '_fen']);
  if (fen === null) { return BX_NA; }
  return pfYuanFromFen(Math.round(fen)) + ' 元';
}

function bxCount(value, unit) {
  if (value === undefined || value === null || value === '') { return BX_NA; }
  return textOf(value) + (unit || '');
}

function bxStatCard(label, value, note) {
  const card = el('div', 'card tight');
  card.appendChild(el('div', 'stat-label', label));
  card.appendChild(el('div', 'stat-value', value));
  if (note) { card.appendChild(el('div', 'card-note', note)); }
  return card;
}

function bxRow(title, sub, amount) {
  const row = el('div', 'row');
  const main = el('div', 'row-main');
  main.appendChild(el('div', 'row-title', title));
  if (sub) { main.appendChild(el('div', 'row-sub', sub)); }
  row.appendChild(main);
  if (amount !== undefined && amount !== null && amount !== '') {
    row.appendChild(el('div', 'row-amount', amount));
  }
  return row;
}

function bxBar(pct, cls) {
  const bar = el('div', 'bar');
  const fill = el('div', 'bar-fill' + (cls ? ' ' + cls : ''));
  const width = bxNum(pct);
  fill.style.width = Math.max(0, Math.min(100, width === null ? 0 : width)) + '%';
  bar.appendChild(fill);
  return bar;
}

function bxTable(headers, rows, numericCols) {
  const table = el('table', 'table');
  const head = document.createElement('thead');
  const headRow = document.createElement('tr');
  headers.forEach(function (text) { headRow.appendChild(el('th', '', text)); });
  head.appendChild(headRow);
  table.appendChild(head);
  const body = document.createElement('tbody');
  rows.forEach(function (cells) {
    const tr = document.createElement('tr');
    cells.forEach(function (cell, index) {
      const num = (numericCols || []).indexOf(index) !== -1;
      tr.appendChild(el('td', num ? 'num' : '', textOf(cell)));
    });
    body.appendChild(tr);
  });
  table.appendChild(body);
  return table;
}

function bxLoading(host, lines) {
  if (!host) { return; }
  host.replaceChildren();
  let i;
  const total = lines || 3;
  for (i = 0; i < total; i += 1) { host.appendChild(el('div', 'skeleton')); }
}

function bxEmpty(host, title, hint) {
  if (!host) { return; }
  host.replaceChildren();
  const box = el('div', 'empty');
  box.appendChild(el('div', 'empty-title', title || '暂时没有数据'));
  if (hint) { box.appendChild(el('div', 'card-note', hint)); }
  host.appendChild(box);
}

function bxError(host, err) {
  if (!host) { return; }
  host.replaceChildren();
  const box = el('div', 'notice bad');
  box.appendChild(el('strong', '', '这块没加载出来：'));
  box.appendChild(el('div', '', errorText(err)));
  host.appendChild(box);
}

function bxLoadingFor(view) {
  bxArr(BX_HOSTS[view]).forEach(function (id) {
    const two = id === 'ov-balance' || id === 'bill-summary' || id === 'sec-summary';
    bxLoading($(id), two ? 2 : 3);
  });
}

function bxErrorFor(view, err) {
  bxArr(BX_HOSTS[view]).forEach(function (id) { bxError($(id), err); });
}

function bxCacheDrop(view) {
  if (view) { delete bxCache[view]; return; }
  Object.keys(bxCache).forEach(function (key) { delete bxCache[key]; });
}

function bxFlashCard(hostId) {
  const host = $(hostId);
  if (!host) { return; }
  let card = host.closest ? host.closest('.card') : null;
  if (!card) { card = host; }
  card.classList.remove('flash');
  void card.offsetWidth;
  card.classList.add('flash');
  if (card.scrollIntoView) { card.scrollIntoView({ block: 'center' }); }
}

/* -------------------------------------------------------------- 网络层 */
async function bxFetch(url) {
  let response;
  try {
    response = await fetch(url, { method: 'GET', headers: { Accept: 'application/json' } });
  } catch (err) {
    throw new Error('连不上本地服务（可能已经停止或刚重启过）。确认服务在跑，再刷新这一页。');
  }
  if (!response.ok) {
    let detail = '';
    try {
      const body = await response.json();
      detail = textOf((body && (body.user_text || body.detail)) || '');
    } catch (err) {
      detail = '';
    }
    if (response.status === 401) {
      /* 服务端只从请求头认令牌，?token= 一律 401（见 app/api/auth.py）。以前这里让人用
         ?token= 打开，等于把人往一条设计上不可能成功的路上引——正确入口是 #token=。 */
      throw new Error('接口鉴权没通过：这个标签页还没拿到访问令牌。用启动日志里带 #token= 的链接重新打开一次页面。');
    }
    throw new Error(detail || ('后端返回 HTTP ' + response.status));
  }
  return response.json();
}

/* 写操作统一走这里：页面上的按钮只允许改「计划/目标/提醒」这类非资金状态，
   转钱之类一律回 AI 助手走权限引擎，避免页面绕过确认流程。
   网络层失败（fetch 直接 reject）原来只会冒出浏览器原话 "Failed to fetch"，
   用户看不懂、也不知道服务是不是挂了——这里统一翻译成人话。 */
async function bxSend(url, method, body) {
  let response;
  try {
    response = await fetch(url, {
      method: method || 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body)
    });
  } catch (err) {
    throw new Error('连不上本地服务（可能已经停止或刚重启过）。确认服务在跑，再刷新这张卡片重试。');
  }
  if (!response.ok) {
    let detail = '';
    try {
      const payload = await response.json();
      detail = textOf((payload && (payload.user_text || payload.detail)) || '');
    } catch (err) {
      detail = '';
    }
    /* 后端每个错误都带 user_text（一句人话），优先用它；HTTP 码降级成括号里的线索。 */
    throw new Error(detail || ('后端返回 HTTP ' + response.status));
  }
  return response.json();
}

function bxUrl(view) {
  let url = BX_API[view] || '';
  if (view === 'bill') {
    url += '?period=' + encodeURIComponent(bxBillPeriod);
  }
  return url;
}

function bxLoad(view, force) {
  if (!view || view === 'assistant' || !BX_API[view]) { return Promise.resolve(null); }
  const now = Date.now();
  const cached = bxCache[view];
  if (!force && cached && (now - cached.at) < 15000) {
    bxRender(view, cached.data);
    return Promise.resolve(cached.data);
  }
  bxSeq[view] = (bxSeq[view] || 0) + 1;
  const mine = bxSeq[view];
  bxLoadingFor(view);
  return bxFetch(bxUrl(view)).then(function (data) {
    if (mine !== bxSeq[view]) { return data; }
    bxCache[view] = { at: Date.now(), data: data };
    bxRender(view, data);
    return data;
  }).catch(function (err) {
    if (mine !== bxSeq[view]) { return null; }
    bxErrorFor(view, err);
    return null;
  });
}

function bxRefresh() {
  bxCacheDrop();
  if (bxView !== 'assistant') { bxLoad(bxView, true); }
  return bxLoad('overview', true);
}

function bxRender(view, data) {
  if (view === 'overview') { bxRenderOverview(data); return; }
  if (view === 'bill') { bxRenderBill(data); return; }
  if (view === 'plans') { bxRenderPlans(data); return; }
  if (view === 'manage') { bxRenderManage(data); return; }
  if (view === 'security') { bxRenderSecurity(data); return; }
}

/* ----------------------------------------------------------- 导航切换 */
function bxSetView(name) {
  if (!name || !BX_API[name] && name !== 'assistant') { return; }
  bxView = name;
  const tabs = document.querySelectorAll('#nav .nav-tab');
  let i;
  for (i = 0; i < tabs.length; i += 1) {
    const on = tabs[i].getAttribute('data-view') === name;
    tabs[i].setAttribute('aria-selected', on ? 'true' : 'false');
  }
  const views = document.querySelectorAll('main.views > .view');
  for (i = 0; i < views.length; i += 1) {
    views[i].classList.toggle('active', views[i].id === 'view-' + name);
  }
  if (name === 'assistant') { scrollChat(); return; }
  bxLoad(name);
}

/* 页面上的快捷入口、卡片动作统一走这里：只把话填进输入框，不替用户按发送。
   以前这里填完直接 sendMessage()，点一下「转账」就已经把钱指令发出去了，和页脚
   「快捷入口只负责把话填好」的承诺相反，也让人来不及改金额/收款人。 */
function bxAsk(prompt) {
  if (!prompt) { return; }
  bxSetView('assistant');
  const input = $('chat-input');
  if (!input) { return; }
  input.value = prompt;
  if (input.focus) { input.focus(); }
  try { input.setSelectionRange(prompt.length, prompt.length); } catch (err) { /* 非文本输入忽略 */ }
  if (streaming) {
    appendBubble('sys', '上一句还在跑，这句话已经填进输入框，等它结束再发送。');
    return;
  }
  appendBubble('sys', '已经填进输入框，金额和收款人确认无误后按「发送」。（快捷入口只负责把话填好，不会替你发出去）', 'quick');
}

function bxSendText(value) {
  const input = $('chat-input');
  if (!input) { return; }
  /* 流式期间以前是静默 return：调用方（例如点候选收款人）已经把用户气泡画上去了，
     实际什么都没发出去，看起来就是「点了没反应」。 */
  if (streaming) {
    input.value = value;
    appendBubble('sys', '上一句还在跑，这句已经填进输入框；等它结束再发送。');
    return;
  }
  input.value = value;
  sendMessage();
}
