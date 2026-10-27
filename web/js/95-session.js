/* 95-session.js — 会话续接、计划节点、收款人选择、导航与启动。必须最后执行。 */
'use strict';

/* ------------------------------------------------ 会话恢复与节点回填 */
const BX_SESSION_KEY = 'banking_session_id';

function bxRememberSession(sid) {
  if (!sid) { return; }
  try { window.sessionStorage.setItem(BX_SESSION_KEY, textOf(sid)); } catch (err) { return; }
}

function bxStoredSession() {
  try { return textOf(window.sessionStorage.getItem(BX_SESSION_KEY)); } catch (err) { return ''; }
}

function bxMarkSeen(nodes) {
  bxArr(nodes).forEach(function (node) {
    bxSeen[textOf(node.node_id) + ':' + textOf(node.state)] = true;
  });
}

function bxRenderPlanNodes(nodes) {
  const host = panelBody('panel-plan');
  if (!host) { return; }
  host.replaceChildren();
  const list = bxArr(nodes);
  if (!list.length) {
    host.appendChild(el('div', 'card-note', '还没有节点。'));
    return;
  }
  list.forEach(function (node) {
    const row = el('div', 'plan-node');
    row.appendChild(el('span', 'plan-tool', textOf(node.node_id) + ' · ' + textOf(node.tool)));
    row.appendChild(el('span', 'plan-status', textOf(node.state_label) || BX_TASK_LABEL[textOf(node.state)] || textOf(node.state)));
    host.appendChild(row);
    const decision = bxIsObj(node.decision) ? node.decision : null;
    if (decision && decision.level) {
      host.appendChild(kvRow('裁决', levelText(decision.level) + (decision.rule_id ? ' · ' + textOf(decision.rule_id) : '')));
      if (decision.reason_text) { host.appendChild(el('div', 'card-note', textOf(decision.reason_text))); }
    }
    const result = bxIsObj(node.result) ? node.result : null;
    if (result) {
      const summary = textOf(result.summary || (bxIsObj(result.data) ? result.data.summary : '') || result.error_text);
      if (summary) { host.appendChild(el('div', 'card-note', summary)); }
    }
  });
}

function bxRenderPendingCard(card) {
  if (!bxIsObj(card)) { return; }
  const cid = textOf(card.confirmation_id);
  const rebuilt = bxIsObj(card.card) ? card.card : null;
  const pending = textOf(card.status) === 'pending';
  const expired = card.expired === true;
  /* 服务端现在会用 action_json + dag_nodes.decision_json 把同一张卡重建出来（字段与当时推送的一致）。
     能重建就画真卡：让人在看不见金额和收款人的情况下再签一次字，等于把确认流程变成走过场。 */
  if (rebuilt && pending && !expired) {
    appendBubble('sys', '服务端还留着这张没处理完的确认（确认号 ' + cid + '），下面是它重新拼出来的同一张卡。', 'restore');
    renderConfirm(rebuilt);
    if (bxArr(rebuilt.required).length) { renderFactor(rebuilt); }
    return;
  }
  const box = el('div', 'notice warn');
  if (rebuilt && expired) {
    box.appendChild(el('strong', '', '这张确认已经过期'));
    box.appendChild(el('div', '', '确认号 ' + cid + ' · 节点 ' + textOf(card.node_id) +
      '。有效期到了就不能再签了（过期会被服务端按 confirm.expired 记一条审计）；要办的话重新说一次。'));
    appendNode(box);
    return;
  }
  box.appendChild(el('strong', '', '还有一个没处理完的确认'));
  box.appendChild(el('div', '', '确认号 ' + cid + ' · 节点 ' + textOf(card.node_id) +
    '。这张卡的历史字段不全，服务端重建不出金额与收款人，所以这里不画可签字的卡。'));
  const btn = el('button', 'btn btn-mini', '把这条确认再提交一次');
  btn.type = 'button';
  btn.addEventListener('click', function () {
    confirmAction(cid, true, factorValue());
  });
  box.appendChild(btn);
  appendNode(box);
}

async function bxLoadChatHistory() {
  /* 会话记忆：先把这个会话之前说过的话画回来，用户才知道「接着上次」不是一句空话。
     没有存过 session_id 时问 /api/chat/latest，续上最近一次真聊过的会话。 */
  const sid = bxStoredSession();
  let data = null;
  try {
    data = await bxFetch(sid
      ? '/api/chat/history?session_id=' + encodeURIComponent(sid)
      : '/api/chat/latest');
  } catch (err) {
    return null;
  }
  if (!bxIsObj(data)) { return null; }
  const turns = bxArr(data.turns);
  if (!turns.length) { return null; }
  const resumed = textOf(data.session_id);
  if (resumed && !sessionId) {
    sessionId = resumed;
    bxRememberSession(resumed);
  }
  bxHistoryOnScreen = true;
  appendBubble('sys', '这是上次的对话（' + (textOf(data.title) || resumed) + '，最近 ' + turns.length +
    ' 轮，服务端存着）。接着说就行。', 'history');
  turns.forEach(function (turn) {
    appendBubble(textOf(turn.role) === 'user' ? 'user' : 'agent', textOf(turn.content));
  });
  return data;
}

/* 历史已经摆到屏幕上了就别再喊「找不到了」：那时候用户看到的是接着的对话，
   再补一句「这一轮没了，重说吧」只会让人以为坏了。只有历史也读不到时才提示。 */
let bxHistoryOnScreen = false;

async function bxRestoreSession() {
  const sid = bxStoredSession();
  if (!sid) { return null; }
  if (bxHistoryOnScreen) { return null; }
  let data = null;
  try {
    data = await bxFetch('/api/session/' + encodeURIComponent(sid));
  } catch (err) {
    appendBubble('sys', '上次的会话读不出来（' + errorText(err) + '）。');
    return null;
  }
  if (!bxIsObj(data) || data.found === false) {
    /* 「找不到了」不等于「救不回来」：服务端有 POST /api/session/{id}/recover，
       库里还有记录就能把这一轮续上。先给一个可点的出口，再谈重说一句。 */
    appendBubble('sys', '上次的会话 ' + sid + ' 在服务端找不到了：执行状态在进程内存里，服务重启就会丢。可以先试试点下面把它接回来；接不回来再重说一句，会开一轮新的。', 'restore');
    const chips = el('div', 'cap-chips');
    const btn = el('button', 'cap-chip action', '试着接回这一轮（服务重启后用）');
    btn.type = 'button';
    btn.addEventListener('click', function () { bxRecoverSession(sid, btn); });
    chips.appendChild(btn);
    appendNode(chips);
    return null;
  }
  sessionId = sid;
  const nodes = bxArr(data.nodes);
  const cards = bxArr(data.confirmations);
  const waiting = cards.filter(function (card) { return textOf(card.status) === 'pending'; });
  const finished = nodes.filter(function (node) { return textOf(node.state) === 'done'; }).length;
  appendBubble('sys', '已接着上次的会话 ' + sid + '：' + nodes.length + ' 个节点，' + finished + ' 个已完成，' +
    (waiting.length ? waiting.length + ' 个还在等你确认。' : '没有待确认的操作。'), 'restore');
  bxRenderPlanNodes(nodes);
  bxMarkSeen(nodes);
  /* 刷新后默认落在「财务总览」。若还有待确认的卡，必须把用户带回 AI 助手：
     否则卡片被画在隐藏视图里，用户只看到总览页「待处理的授权」一行，点进来看不见金额与收款人。 */
  if (waiting.length) { bxSetView('assistant'); }
  waiting.forEach(bxRenderPendingCard);
  if (waiting.length) { scrollChat(); }
  if (bxIsObj(data) && data.balance_fen !== undefined && data.balance_fen !== null) {
    appendBubble('sys', '服务端当前余额：' + pfYuanFromFen(data.balance_fen) + ' 元（后端原文，不是前端推算）。');
  }
  return data;
}

async function bxPollSession(sid, tries) {
  if (!sid || bxPolling) { return null; }
  bxPolling = true;
  let last = null;
  try {
    let i;
    const total = tries || 8;
    for (i = 0; i < total; i += 1) {
      try {
        last = await bxFetch('/api/session/' + encodeURIComponent(sid));
      } catch (err) {
        last = null;
      }
      if (last && (textOf(last.status) === 'done' || textOf(last.status) === 'cancelled')) { break; }
      await new Promise(function (resolve) { setTimeout(resolve, 600); });
    }
  } finally {
    bxPolling = false;
  }
  if (!bxIsObj(last)) {
    appendBubble('sys', '没读到最新会话状态，去「执行记录与安全」手动刷新再看。');
    return null;
  }
  bxRenderPlanNodes(last.nodes);
  bxEmitNewNodeResults(last.nodes);
  const status = textOf(last.status);
  if (status === 'done') {
    appendBubble('agent', '这一轮执行完毕（状态来自服务端会话）。余额：' + (last.balance_fen === null || last.balance_fen === undefined ? BX_NA : pfYuanFromFen(last.balance_fen) + ' 元'), 'done');
  } else if (status === 'cancelled') {
    appendBubble('sys', '这一轮已经被取消，未完成的占位会释放。', 'cancelled');
  } else {
    appendBubble('sys', '服务端还在处理（状态：' + (BX_TASK_LABEL[status] || status) + '）。可以过一会儿在「执行记录与安全」里刷新。');
  }
  bxCacheDrop();
  if (bxView !== 'assistant') { bxLoad(bxView, true); }
  return last;
}

function bxEmitNewNodeResults(nodes) {
  bxArr(nodes).forEach(function (node) {
    const state = textOf(node.state);
    const key = textOf(node.node_id) + ':' + state;
    if (bxSeen[key]) { return; }
    bxSeen[key] = true;
    const result = bxIsObj(node.result) ? node.result : null;
    const summary = result ? textOf(result.summary || (bxIsObj(result.data) ? result.data.summary : '') || result.error_text) : '';
    if (!summary) { return; }
    const ok = result && result.ok === true;
    appendBubble(ok ? 'agent' : 'sys', summary, textOf(node.tool));
  });
}

/* ------------------------------------------------ 收款人消歧选择 */
function bxRenderPayeePicker(clar) {
  const host = $('chat-log');
  if (!host) { return; }
  const list = el('div', 'pick-list');
  list.appendChild(el('div', 'field-label', textOf(clar && clar.question) || '有多个候选，请选一个'));
  const cands = bxArr(clar && clar.candidates);
  const accepts = bxArr(clar && clar.accepts);
  const selectable = !accepts.length || accepts.indexOf('select_candidate') >= 0;
  cands.forEach(function (cand, index) {
    const btn = el('button', 'pick');
    btn.type = 'button';
    const name = textOf(cand && cand.name);
    const alias = textOf(cand && cand.alias);
    btn.appendChild(el('span', 'pick-name', alias ? (name + '（' + alias + '）') : name));
    const meta = [];
    if (cand && cand.bank) { meta.push(textOf(cand.bank)); }
    if (cand && cand.card_tail) { meta.push('尾号' + textOf(cand.card_tail)); }
    if (cand && cand.phone_masked) { meta.push(textOf(cand.phone_masked)); }
    meta.push('第 ' + (index + 1) + ' 位');
    btn.appendChild(el('span', 'pick-meta', meta.join(' · ')));
    /* AA 拆分里不能点一下就替一位参与人做主（clar.accepts 只有 restate_with_tail）：
       那种场景点了也接不上，会让用户以为系统坏了。改成「写成尾号」填进输入框。 */
    if (selectable) {
      btn.addEventListener('click', function () { bxPickPayee(cand, btn, index); });
      list.appendChild(btn);
    } else {
      const row = el('div', 'pick');
      const bits = [];
      if (cand && cand.bank) { bits.push(textOf(cand.bank)); }
      if (cand && cand.card_tail) { bits.push('尾号' + textOf(cand.card_tail)); }
      bits.push('第 ' + (index + 1) + ' 位');
      row.appendChild(el('span', 'pick-name', (name || '（没有名字）') + (alias ? '（' + alias + '）' : '')));
      row.appendChild(el('span', 'pick-meta', bits.join(' · ')));
      if (cand && (cand.name || cand.card_tail)) {
        const fill = el('button', 'btn btn-mini', cand.card_tail ? '写成「' + name + ' 尾号' + textOf(cand.card_tail) + '」' : '写成「' + name + '」');
        fill.type = 'button';
        fill.addEventListener('click', function () {
          const box = $('chat-input');
          if (!box) { return; }
          const piece = cand.card_tail ? (name + ' 尾号' + textOf(cand.card_tail)) : name;
          box.value = piece + ' ';
          box.focus();
          appendBubble('sys', '这一位已经填进输入框了：把金额和其他参与人补齐再发一次，系统不会替你挑人。', 'sys');
        });
        row.appendChild(fill);
      }
      list.appendChild(row);
    }
  });
  list.appendChild(el('div', 'card-note',
    selectable
      ? '选完会重新生成确认卡片。系统只认「尾号 4 位」或「第几个」这样的回答，不会自己挑第一个；' +
        '收款人、金额任何一项变了，之前的授权都会作废。'
      : (textOf(clar && clar.note) || '这一次要带上尾号重说一遍：一处选择代表不了多位参与人。')));
  host.appendChild(list);
  scrollChat();
}

async function bxPickPayee(cand, btn, index) {
  const tail = cand && cand.card_tail ? textOf(cand.card_tail) : '';
  /* 没有卡尾号的候选以前点了直接 return——按钮看着能点，点了没反应。
     后端 clarify.augment() 还认「第几个」，就用序号兜底。 */
  const reply = tail ? '尾号' + tail : '第' + (Number(index) + 1) + '位';
  const log = $('chat-log');
  if (log) {
    const btns = log.querySelectorAll('.pick');
    for (let i = 0; i < btns.length; i += 1) { btns[i].disabled = true; }
  }
  if (btn) { btn.classList.add('picked'); }
  appendBubble('user', reply);
  await bxSendText(reply);
}

/* ------------------------------------------------ 确认卡片的信息补全 */
function bxConfirmRows(data) {
  const rows = [];
  const payee = bxIsObj(data.payee) ? data.payee : null;
  const payeeName = payee ? textOf(payee.name) : fieldVal(data, PAYEE_KEYS);
  const payeeBits = [];
  if (payee) {
    if (payee.bank) { payeeBits.push(textOf(payee.bank)); }
    if (payee.card_tail) { payeeBits.push('尾号 ' + textOf(payee.card_tail)); }
    if (payee.alias) { payeeBits.push(textOf(payee.alias)); }
    if (payee.phone_masked) { payeeBits.push(textOf(payee.phone_masked)); }
  }
  if (payeeName) {
    rows.push(['收款人', payeeName + (payeeBits.length ? '（' + payeeBits.join(' · ') + '）' : ''), 'confirm-payee']);
  }

  const account = bxIsObj(data.source_account) ? data.source_account : null;
  if (account) {
    const bits = [];
    if (account.name) { bits.push(textOf(account.name)); }
    if (account.tail) { bits.push('尾号 ' + textOf(account.tail)); }
    if (account.balance_yuan) { bits.push('当前余额 ' + textOf(account.balance_yuan) + ' 元'); }
    rows.push(['转出账户', bits.join(' · ') + (account.note ? '（' + textOf(account.note) + '）' : ''), 'confirm-source']);
  }

  if (data.tool_name || data.method || data.arrival) {
    const bits = [];
    const human = CONFIRM_TOOL_LABEL[textOf(data.tool) || ''] || (typeof CONFIRM_TOOL_LABEL === 'object' ? '' : '');
    if (data.tool_name) { bits.push(human || textOf(data.tool_name)); }
    if (data.method) { bits.push(textOf(data.method)); }
    if (data.arrival) { bits.push(textOf(data.arrival)); }
    /* 没有「方式/到账时间」说明它不是资金动作，标签再写「转账方式」就驴唇不对马嘴。 */
    const moneyTool = CONFIRM_MONEY_TOOLS[textOf(data.tool) || ''] === 1;
    rows.push([(moneyTool || data.arrival) ? '转账方式' : '操作类型', bits.join(' · '), 'confirm-method']);
  }

  if (data.scheduled_for) {
    rows.push(['预约时间', textOf(data.scheduled_for), 'confirm-time']);
  } else if (data.tool === 'scheduled_transfer') {
    rows.push(['预约时间', '后端没有回传执行时间', 'confirm-time']);
  }

  const fee = bxIsObj(data.fee) ? data.fee : null;
  if (fee) {
    rows.push(['手续费', fee.known === true ? (bxCount(fee.amount_fen, ' 分') + (fee.text ? ' · ' + textOf(fee.text) : '')) : (textOf(fee.text) || '本演示环境不计算手续费'), 'confirm-fee']);
  }

  const verify = bxIsObj(data.verification) ? data.verification : null;
  if (verify) {
    const steps = bxArr(verify.steps).map(function (step) {
      return textOf(step.label) + (step.required === false ? '（可选）' : '');
    });
    rows.push(['权限等级', (textOf(verify.level_label) || levelText(verify.level)) + (verify.note ? ' · ' + textOf(verify.note) : ''), 'confirm-level']);
    rows.push(['验证步骤', steps.length ? steps.join(' → ') : '不需要额外验证', 'confirm-steps']);
  }
  return rows;
}

function bxBindNav() {
  const nav = $('nav');
  if (nav) {
    nav.addEventListener('click', function (ev) {
      const btn = ev.target && ev.target.closest ? ev.target.closest('.nav-tab') : null;
      if (!btn) { return; }
      bxSetView(btn.getAttribute('data-view'));
    });
    nav.addEventListener('keydown', function (ev) {
      if (ev.key !== 'ArrowRight' && ev.key !== 'ArrowLeft') { return; }
      const tabs = Array.prototype.slice.call(nav.querySelectorAll('.nav-tab'));
      const index = tabs.indexOf(document.activeElement);
      if (index === -1) { return; }
      ev.preventDefault();
      const next = tabs[(index + (ev.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length];
      next.focus();
      bxSetView(next.getAttribute('data-view'));
    });
  }
  document.addEventListener('click', function (ev) {
    const go = ev.target && ev.target.closest ? ev.target.closest('[data-goto]') : null;
    if (!go) { return; }
    bxSetView(go.getAttribute('data-goto'));
  });
  const billView = $('view-bill');
  if (billView) {
    billView.addEventListener('click', function (ev) {
      const per = ev.target && ev.target.closest ? ev.target.closest('[data-bill-period]') : null;
      if (!per) { return; }
      bxBillPeriod = per.getAttribute('data-bill-period');
      bxLoad('bill', true);
    });
  }
  const refresh = $('bill-refresh');
  if (refresh) {
    refresh.addEventListener('click', function () { bxLoad('bill', true); });
  }
  bxBindManualForm();
}


function bootAll() {
  boot();
  bxBindNav();
  bxLoad('overview');
  /* 先把历史对话画回来，再恢复执行状态面板：顺序反了会看到「已接着会话」在气泡上面。 */
  bxLoadChatHistory().then(function () { return bxRestoreSession(); });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', bootAll);
} else {
  bootAll();
}

/* 服务重启后的接续：POST /api/session/{id}/recover。
   能接回来就把节点与待确认卡重画一遍；接不回来要说清「记录不完整」，不能假装成功。 */
async function bxRecoverSession(sid, btn) {
  if (!sid) { return null; }
  if (btn) { btn.disabled = true; }
  try {
    const res = await bxSend('/api/session/' + encodeURIComponent(sid) + '/recover', 'POST', {});
    if (res && res.ok === false) {
      appendBubble('sys', '接不回来：' + (textOf(res.user_text) || '服务端没有这条会话的完整记录') + '。重说一句会开一轮新的，前面的对话还在上面。', 'recover');
      if (btn) { btn.disabled = false; }
      return null;
    }
    appendBubble('sys', '已经把它接回来了（服务端按库里的记录把这一轮续上）。继续就行。', 'recover');
    return await bxRestoreSession();
  } catch (err) {
    appendBubble('sys', '接回失败：' + errorText(err) + '。重说一句会开一轮新的。', 'recover');
    if (btn) { btn.disabled = false; }
    return null;
  }
}
