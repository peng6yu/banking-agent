/* 40-confirm.js — 确认卡与因子面板：把后端给的等级、因子、金额原样展示，不自己判色。 */
'use strict';

function renderConfirm(data) {
  data = data && typeof data === 'object' ? data : {};
  const cid = fieldVal(data, ['confirmation_id', 'id']);
  const actionText = fieldVal(data, ['action_text', 'action', 'summary']);
  const expiresRaw = fieldVal(data, ['expires_in_s', 'expires_in']);
  const amountInfo = fieldInfo(data, AMOUNT_KEYS);
  const payeeInfo = fieldInfo(data, PAYEE_KEYS);
  /* 没有确认号、也没有金额和操作说明的帧，不是一张可签字的卡片：
     画出来只会得到一张字段全是「见操作说明全文」的空卡，让人误以为系统出错了。 */
  if (!cid || (!amountInfo.value && !fieldVal(data, ['action_text', 'action', 'summary']))) {
    /* 以前这里静默 return null：审计里多一条，页面上什么都没有，
       用户只会觉得「我说完就没反应了」。至少留一句说明有操作在等。 */
    appendBubble('sys', '服务端说有一笔操作要等你确认，但这一帧没带确认号或金额，卡片画不出来。去「执行记录」看这一轮，或者重新发起一次。', 'confirm');
    return null;
  }
  const amountUnit = amountInfo.key.indexOf('fen') !== -1 ? ' 分' : '';
  /* 金额与收款人必须显式出现在卡片和按钮上：拿不到原文就指向操作说明全文，不做二次计算 */
  /* 停代扣、退订这类动作本来就没有「金额」：写「见操作说明全文」等于什么都没说。
     后端会带 subscription（商户/月费/下次扣费/边界），卡片直接按它说人话。 */
  const subs = bxIsObj(data.subscription) ? data.subscription : null;
  const amountText = amountInfo.value ? amountInfo.value + amountUnit
    : (subs ? '不动本金余额（只停银行侧代扣）' : '见操作说明全文');
  const payeeText = payeeInfo.value ? payeeInfo.value
    : (subs && textOf(subs.merchant) ? '「' + textOf(subs.merchant) + '」' : '见操作说明全文');

  const card = el('div', 'confirm-card');
  card.appendChild(el('div', 'confirm-head',
    data.tool_name ? ('确认这笔操作：' + textOf(data.tool_name)) : '确认这笔操作'));

  /* 收款人、转出账户、方式、预约时间、手续费、权限等级、验证步骤全部来自后端 describe_confirmation；
     后端没给的项不写，避免把推测值摆成事实。 */
  const rows = [['金额（后端原文）', amountText, 'confirm-amount']];
  bxConfirmRows(data).forEach(function (row) { rows.push(row); });
  if (subs) {
    rows.push(['订阅商户', textOf(subs.merchant) || '后端没给', 'confirm-sub']);
    if (textOf(subs.amount_yuan)) {
      rows.push(['每期金额', textOf(subs.amount_yuan) + ' 元 · ' + (textOf(subs.period_label) || '周期未知'), 'confirm-sub']);
    }
    if (textOf(subs.next_charge_text)) { rows.push(['下次扣费', textOf(subs.next_charge_text), 'confirm-sub']); }
    if (textOf(subs.status_label)) { rows.push(['当前状态', textOf(subs.status_label), 'confirm-sub']); }
  }
  rows.push(['操作说明（全文）', actionText, 'confirm-action']);
  rows.push(['有效期', expiresRaw ? expiresRaw + ' 秒' : '以后端为准', 'confirm-expire']);
  rows.forEach(function (row) {
    const line = el('div', 'confirm-row');
    line.appendChild(el('span', 'confirm-key', row[0]));
    line.appendChild(el('span', 'confirm-val ' + row[2], row[1]));
    card.appendChild(line);
  });
  card.appendChild(el('div', 'card-note', '点确认只是提交这次授权，不等于交易成功；最终状态以服务端执行结果为准。'));

  const actions = el('div', 'confirm-actions');
  const okBtn = el('button', 'btn btn-green', '确认：' + amountText + ' → ' + payeeText);
  okBtn.type = 'button';
  okBtn.addEventListener('click', function () {
    confirmAction(cid, true, factorValue());
  });
  const noBtn = el('button', 'btn btn-red', '拒绝');
  noBtn.type = 'button';
  noBtn.addEventListener('click', function () {
    confirmAction(cid, false, null);
  });
  actions.appendChild(okBtn);
  actions.appendChild(noBtn);
  card.appendChild(actions);

  const timer = el('div', 'confirm-timer', cid ? '确认号 ' + cid : '');
  card.appendChild(timer);
  appendNode(card);

  let left = parseInt(expiresRaw, 10);
  if (left > 0) {
    const tick = setInterval(function () {
      left -= 1;
      timer.textContent = '确认号 ' + cid + ' · 剩余 ' + Math.max(left, 0) + ' 秒（到期以后端为准）';
      if (left <= 0) {
        clearInterval(tick);
        okBtn.disabled = true;
        noBtn.disabled = true;
      }
    }, 1000);
  }

  panelBadge('panel-factor', '等待确认');
  setPanelLevel('panel-factor', 'yellow');
  flash('panel-factor');
}

/* 多因子面板：验证码由服务端下发（POST /api/factor/challenge）、服务端校验，
   前端不再自己造一个「我验过了」的字符串。挑战一次性、5 分钟、绑这条确认号。 */
function renderFactor(data) {
  const cid = fieldVal(data, ['confirmation_id', 'id']);
  let required = asList(fieldVal(data, ['required', 'required_factors', 'factors']));
  /* 后端有的路径会把因子数组当字符串塞过来（"['face','sms']" 这种），
     这里拆一层再渲染，免得面板上直接显示一坨 JSON。 */
  if (required.length === 1 && typeof required[0] === 'string' && required[0].indexOf('[') === 0) {
    try { required = JSON.parse(required[0]); } catch (err) { required = asList(required[0]); }
  }
  /* 红灯但没有因子 = 策略直接拦截（例如日累计越线）：这是额度硬线，
     再画「需要验证」和提交按钮，会让人以为补个验证码就能过。 */
  if (!required.length) {
    const blockedHost = panelBody('panel-factor');
    if (!blockedHost) { return; }
    const ruleId = textOf(fieldVal(data, ['rule_id', 'rule']));
    const reason = textOf(fieldVal(data, ['reason_text', 'reason', 'message']));
    blockedHost.replaceChildren();
    blockedHost.appendChild(el('div', 'field-label', '这一步被策略直接拦下'));
    if (ruleId) { blockedHost.appendChild(kvRow('规则', ruleId, true)); }
    if (reason) { blockedHost.appendChild(kvRow('理由', reason)); }
    /* 以前这里写死「1000 元/日、1000 元/笔」，可拦截原因可能是「查别人的账户」，
       跟额度毫无关系。只在规则本身确实是额度线时才提额度。 */
    if (ruleId.indexOf('LIMIT') !== -1) {
      blockedHost.appendChild(el('div', 'card-note',
        '额度是规则常量（本赛题设定 1000 元/日、1000 元/笔），不是身份核验问题，补验证码也放不过去。'));
    }
    blockedHost.appendChild(el('div', 'card-note',
      '这一轮不会执行任何操作、也不会动钱。要办成，得换一种合法方式：本人账户、本人操作，或者把金额降到限额内。'));
    const actions = el('div', 'factor-actions');
    const gotIt = el('button', 'btn btn-mini', '知道了');
    gotIt.type = 'button';
    gotIt.addEventListener('click', function () {
      blockedHost.replaceChildren();
      blockedHost.appendChild(el('div', 'card-note', '这一轮已经结束，没有执行任何操作。'));
      panelBadge('panel-factor', '已拦截');
      setPanelLevel('panel-factor', 'red');
    });
    const retry = el('button', 'btn btn-mini btn-green', '换个说法再说');
    retry.type = 'button';
    retry.addEventListener('click', function () {
      bxSetView('assistant');
      const input = $('chat-input');
      if (input) {
        input.value = '';
        input.focus();
      }
    });
    actions.appendChild(retry);
    actions.appendChild(gotIt);
    blockedHost.appendChild(actions);
    panelBadge('panel-factor', '直接拦截');
    setPanelLevel('panel-factor', 'red');
    flash('panel-factor');
    return;
  }
  const host = panelBody('panel-factor');
  if (!host) { return; }
  host.replaceChildren();
  host.appendChild(el('div', 'field-label', '这一步需要多因子验证'));
  const list = el('ul', 'factor-list');
  required.forEach(function (item) {
    const key = textOf(item);
    list.appendChild(el('li', 'factor-item', BX_FACTOR_TEXT[key] ? (BX_FACTOR_TEXT[key] + '（' + key + '）') : key));
  });
  if (!required.length) {
    list.appendChild(el('li', 'factor-item', '后端没有说明具体需要哪几步'));
  }
  host.appendChild(list);
  host.appendChild(el('div', 'card-note',
    '验证码由服务端下发、服务端校验：一次性、5 分钟内有效、只对这条确认号有效。' +
    '演示环境走的是桩渠道（不会真的发短信、也不会真的采集人脸），真实银行要换成行方验证服务。'));

  const codeBox = el('div', 'factor-codes');
  host.appendChild(codeBox);
  const actions = el('div', 'factor-actions');
  let challenges = [];

  function paint(items) {
    challenges = items;
    codeBox.replaceChildren();
    if (!items.length) {
      codeBox.appendChild(el('div', 'card-note', '服务端没有可用的验证挑战，点下面「重新获取验证码」再试。'));
      return;
    }
    items.forEach(function (item) {
      const row = el('div', 'factor-code-row');
      const label = BX_FACTOR_TEXT[textOf(item.factor)] || textOf(item.factor);
      row.appendChild(el('span', 'factor-code-label', label + (item.target ? '（' + textOf(item.target) + '）' : '')));
      const input = document.createElement('input');
      input.type = 'text';
      input.className = 'factor-input';
      input.placeholder = '6 位验证码';
      input.setAttribute('data-challenge', textOf(item.challenge_id));
      if (item.code) { input.value = textOf(item.code); }
      row.appendChild(input);
      if (item.code) { row.appendChild(el('span', 'card-note', '演示验证码已自动填好')); }
      codeBox.appendChild(row);
    });
  }

  function load(btn) {
    if (!sessionId) { appendBubble('sys', '会话还没建立，拿不到验证码。'); return; }
    if (btn) { btn.disabled = true; }
    bxSend('/api/factor/challenge', 'POST', { session_id: sessionId, confirmation_id: cid })
      .then(function (res) {
        paint(asList(res && res.challenges));
        if (res && res.note) { codeBox.appendChild(el('div', 'card-note', textOf(res.note))); }
      })
      .catch(function (err) { appendBubble('sys', '获取验证码失败：' + pfErrText(err)); })
      .then(function () { if (btn) { btn.disabled = false; } });
  }

  const reloadBtn = el('button', 'btn btn-ghost btn-mini', '重新获取验证码');
  reloadBtn.type = 'button';
  reloadBtn.addEventListener('click', function () { load(reloadBtn); });

  const okBtn = el('button', 'btn btn-red', '提交验证并执行');
  okBtn.type = 'button';
  okBtn.addEventListener('click', function () {
    if (!challenges.length) {
      appendBubble('sys', '还没有可用的验证码，先点「重新获取验证码」。');
      return;
    }
    const proofs = [];
    let missing = 0;
    challenges.forEach(function (item) {
      const box = codeBox.querySelector('input[data-challenge="' + textOf(item.challenge_id) + '"]');
      const code = box ? textOf(box.value).trim() : '';
      if (!code) { missing += 1; return; }
      proofs.push({ factor: textOf(item.factor), challenge_id: textOf(item.challenge_id), code: code });
    });
    if (missing) {
      appendBubble('sys', '还有 ' + missing + ' 个验证码没填，服务端会拒绝这次提交。');
      return;
    }
    confirmAction(cid, true, null, proofs);
  });

  const noBtn = el('button', 'btn btn-ghost', '拒绝并终止');
  noBtn.type = 'button';
  noBtn.addEventListener('click', function () {
    confirmAction(cid, false, null);
  });
  actions.appendChild(okBtn);
  actions.appendChild(noBtn);
  actions.appendChild(reloadBtn);
  host.appendChild(actions);

  panelBadge('panel-factor', required.join('/'));
  setPanelLevel('panel-factor', 'red');
  flash('panel-factor');
  load(null);
}

function renderToolResult(data) {
  const nodeId = fieldVal(data, ['node_id', 'id']);
  const ok = data && data.ok === true;
  const summary = fieldVal(data, ['summary', 'reason_text', 'error_text']);
  appendBubble(ok ? 'agent' : 'sys', summary || (ok ? '已完成。' : '这一步没有成功。'),
    nodeId ? nodeId : '');
  renderPlan(data);
}

/* 权限裁决面板当前是什么灯，就从类名读回来；读不到就说“未知”，不猜绿灯。 */
function policyLevelLabel() {
  const panel = $('panel-policy');
  if (!panel) { return '未知'; }
  if (panel.classList.contains('lv-red')) { return '红灯'; }
  /* LEVEL_CLASS 把 yellow 映射成 lv-amber（web/index.html:960），别只盯 lv-yellow */
  if (panel.classList.contains('lv-amber') || panel.classList.contains('lv-yellow')) { return '黄灯'; }
  if (panel.classList.contains('lv-green')) { return '绿灯'; }
  return '未知';
}

function renderDone(data) {
  data = data && typeof data === 'object' ? data : {};
  const summary = fieldVal(data, ['summary']);
  const balance = fieldVal(data, ['balance_after_fen']);
  const clar = data.clarification && typeof data.clarification === 'object' ? data.clarification : null;
  /* tool 事件已经把这个节点的摘要单独推过一条气泡了；done 里再推一次就成了复读。
     只在这一条确实不同的时候才追加。 */
  const log = $('chat-log');
  let repeated = false;
  if (log) {
    const bodies = log.querySelectorAll('.bubble-agent .bubble-body');
    if (bodies.length) {
      repeated = bodies[bodies.length - 1].textContent.trim() === textOf(summary).trim();
    }
  }
  const lines = [];
  if (summary && !repeated) { lines.push(summary); }
  /* 原来这里把后端的分直接当元打印（1286050），还写成「交易后余额」——
     歧义收口时根本没交易，说「交易后余额」是错的。统一按元展示并说清口径。 */
  if (balance !== null && balance !== undefined && balance !== '') {
    lines.push('账户当前余额（后端返回值）：' + pfYuanFromFen(balance) + ' 元');
  }
  if (!lines.length) { lines.push(summary || '本次请求处理完毕。'); }
  /* 带 clarification 说明这一轮停在「等你补充信息」，不能标成已完成 */
  appendBubble('agent', lines.join('\n'), clar ? 'need-info' : 'done');
  ['panel-intent', 'panel-plan'].forEach(function (id) {
    panelBadge(id, clar ? '等待补充信息' : '已完成');
  });
  /* 权限裁决那一块的徽标不能只剩「已完成」：黄灯/红灯本身就是结论，
     抹成绿色会让“这笔其实需要你确认”这个事实消失。 */
  panelBadge('panel-policy', clar ? '等待补充信息' : policyLevelLabel() + ' · 已完成');
  if (clar) { setPanelLevel('panel-policy', 'yellow'); }
  if (clar && clar.kind === 'payee') { bxRenderPayeePicker(clar); }
  /* 反问必须给可点的选项：后端已经把金额/收款人/卡/产品这些槽位从库里查出来了，
     再让用户照着抄一遍，等于把结构化数据又变回自然语言。 */
  const choices = Array.isArray(data.choices) ? data.choices : [];
  const usable = choices.filter(function (item) {
    return item && typeof item === 'object' && (item.value || item.label);
  }).slice(0, 8);
  if (usable.length) {
    appendNode(el('div', 'card-note', '点一个直接发送（不用自己打字）：'));
    const chips = el('div', 'cap-chips');
    usable.forEach(function (item) {
      const label = textOf(item.label || item.value);
      const value = textOf(item.value || item.label);
      const chip = el('button', 'cap-chip action', label);
      chip.type = 'button';
      chip.title = '点击发送：' + value;
      chip.addEventListener('click', function () { bxAsk(value); });
      chips.appendChild(chip);
    });
    appendNode(chips);
  } else if (data.question) {
    /* 后端这一轮没给选项（例如完全没听懂时的兜底）：前端也得留几条可点的，
       只有一个问句、零个入口的话，用户除了自己重新打字没有别的出路。 */
    appendNode(el('div', 'card-note', '没对上你的意思。可以直接点下面这些，或者换一种说法：'));
    const fallback = el('div', 'cap-chips');
    [
      ['查一下我的余额', '查一下我的余额'],
      ['看看我这个月的账单', '看看我这个月的账单'],
      ['我有哪些提醒', '我有哪些提醒'],
      ['我的计划列表', '我的计划列表']
    ].forEach(function (pair) {
      const chip = el('button', 'cap-chip action', pair[0]);
      chip.type = 'button';
      chip.title = '点击发送：' + pair[1];
      chip.addEventListener('click', function () { bxAsk(pair[1]); });
      fallback.appendChild(chip);
    });
    appendNode(fallback);
  }
}

function renderError(data) {
  const code = fieldVal(data, ['code']);
  const userText = fieldVal(data, ['user_text', 'message']) || '系统开小差了，已经记录。';
  appendBubble('sys', userText, code ? 'code=' + code : '');
  if (code === 'SESSION_RUN_CONFLICT') {
    /* 这不是「策略拦下了这笔」，而是「上一轮还没走完，这一轮没开始」。
       画成红灯 + 已拦截会让人以为钱被规则挡了。 */
    setPanelLevel('panel-policy', 'yellow');
    panelBadge('panel-policy', '等待上一轮');
    appendBubble('sys', '没有替你选其中一轮继续：先把上面的卡确认或拒绝，或者点「中断」取消上一轮，再发新的写操作。', 'conflict');
  } else {
    setPanelLevel('panel-policy', 'red');
    panelBadge('panel-policy', '已拦截');
  }
  setBusy(false);
}

function onStart(data) {
  const sid = fieldVal(data, ['session_id', 'id']);
  const prev = sessionId;
  if (sid) { sessionId = sid; bxRememberSession(sid); }
  panelBadge('panel-audit', auditCount + ' 条');
  /* 以前每发一句话都打一条「已建立会话」：看着像每轮都新开会话，用户以为没有记忆。
     现在只在「第一次拿到会话」或「服务端换了会话号」时说一句，同一会话内闭嘴。 */
  if (!sid) {
    appendBubble('sys', '服务端没回传 session_id，这一轮的执行结果可能接不上。', 'start');
  } else if (!prev) {
    appendBubble('sys', '会话 ' + sid + '（同一条会话一直用下去，刷新也接得上）。', 'start');
  } else if (prev !== sid) {
    appendBubble('sys', '换到了另一条会话：' + sid + '（刚才那条是 ' + prev + '）。刚才的对话还在上面。', 'start');
  }
  setBusy(streaming);
  renderAudit(fromEvent('session.start', data));
}

/* --------------------------------------------------------------- 网络动作 */
async function reportHttpError(response) {
  let code = '';
  let userText = '';
  try {
    const text = await response.text();
    try {
      const json = JSON.parse(text);
      code = textOf(json.code);
      userText = textOf(json.user_text);
    } catch (err) {
      userText = text;
    }
  } catch (err) {
    userText = '';
  }
  if (!userText) {
    userText = response.status === 429 ? '请求太频繁，请稍后再试。'
      : (response.status >= 500 ? '系统开小差了，已经记录。' : '请求没通过，请换个说法再试。');
  }
  appendBubble('sys', userText, code ? ('HTTP ' + response.status + ' · code=' + code) : ('HTTP ' + response.status));
}

async function sendMessage() {
  const input = $('chat-input');
  if (!input || streaming) { return; }
  const text = input.value.trim();
  if (!text) {
    appendBubble('sys', '请先输入一句话再发送。');
    return;
  }
  appendBubble('user', text);
  input.value = '';
  resetTheater();
  setBusy(true);
  try {
    const response = await fetch(API_CHAT, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify({ session_id: sessionId, text: text })
    });
    if (!response.ok) {
      await reportHttpError(response);
      return;
    }
    await pumpSse(response);
  } catch (err) {
    appendBubble('sys', '连不上后端：' + errorText(err) + '。请确认服务已启动，然后重试。');
  } finally {
    setBusy(false);
  }
}
