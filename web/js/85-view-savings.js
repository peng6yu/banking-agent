/* 85-view-savings.js — 储蓄计划：每期真的划钱、取消原路划回。
   这一块是计划页的主角，所以单独一个文件；页面里其他部分（预约、提醒、监测）
   仍在 84-view-plans.js。所有数字都来自 /api/business/plans 的 goals 项：
   amount_yuan 是"这一期该划多少"，saved_yuan 是"真的已经划走的"。 */
'use strict';

const BX_SAVINGS_TAG = { active: 'ok', draft: 'info', paused: 'warn', achieved: 'ok', cancelled: 'bad' };
const BX_SAVINGS_LAST = {
  deposited: '上次到点已经划进去了',
  insufficient: '上次到点没划成：卡里余额不够，已顺延一期',
  refunded: '已取消并划回'
};

function bxSavingsHint(goalId, text, kind) {
  const node = $('savings-hint-' + goalId);
  if (!node) { return; }
  node.textContent = textOf(text);
  node.className = 'pf-hint' + (kind ? ' ' + kind : '');
}

function bxRenderSavings(data) {
  data = bxIsObj(data) ? data : {};
  const host = $('plan-goals');
  const block = bxIsObj(data.saved_plans) ? data.saved_plans : {};
  const items = bxArr(block.items);
  const accounts = bxArr(data.funding_accounts);
  setText($('plan-goal-tag'), bxCount(block.count, ' 个'));
  bxFillSavingsAccounts($('plan-savings-account'), accounts);
  bxBindSavingsForm();
  if (!host) { return; }
  host.replaceChildren();
  if (!items.length) {
    bxEmpty(host, '还没有储蓄计划',
      '定一个目标，每期让它真的从卡里划一笔进去；中途取消，划过的钱原路退回。入口在下面「新建储蓄计划」。');
    return;
  }
  items.forEach(function (goal) { host.appendChild(bxSavingsCard(goal, accounts)); });
}

/* 老计划里的 saved_fen 是建计划时手填的起点，不是这个功能划走的；
   两件事混在一句里会变成「已划进 1,500，真的划过 0 笔」这种自相矛盾的话。 */
function bxSavingsSavedNote(goal) {
  const saved = textOf(goal.saved_yuan);
  const count = bxNum(goal.deposit_count);
  if (count > 0) {
    return saved + ' 元（真的从卡里划走的，' + bxCount(count, ' 笔') + '）';
  }
  if (bxNum(goal.saved_fen) > 0) {
    return saved + ' 元（建计划时记的起点，还没真的从卡里划过）';
  }
  return saved + ' 元（还没划过）';
}

function bxSavingsCard(goal, accounts) {
  const state = textOf(goal.state);
  const goalId = textOf(goal.goal_id);
  const box = el('div', 'anomaly');
  const head = el('div', 'anomaly-head');
  head.appendChild(el('span', 'anomaly-title', textOf(goal.name)));
  head.appendChild(el('span', 'tag ' + (BX_SAVINGS_TAG[state] || 'info'),
    textOf(goal.state_label) || BX_GOAL_STATUS[textOf(goal.status)] || textOf(goal.status)));
  box.appendChild(head);
  box.appendChild(bxBar(bxNum(goal.progress_pct), bxNum(goal.progress_pct) >= 100 ? 'ok' : ''));
  box.appendChild(kvRow('目标', textOf(goal.target_yuan) + ' 元 · 目标日 ' + textOf(goal.target_date) +
    ' · 还差 ' + textOf(goal.remaining_yuan) + ' 元'));
  box.appendChild(kvRow('已划进目标', bxSavingsSavedNote(goal), true));
  bxSavingsStateBlock(box, goal, state);
  bxSavingsDeposits(box, goal);
  const hint = el('div', 'pf-hint', '');
  hint.id = 'savings-hint-' + goalId;
  box.appendChild(bxSavingsActions(goal, state, accounts));
  box.appendChild(hint);
  if (state !== "cancelled" && state !== "achieved") {
    box.appendChild(bxSavingsAuthForm(goal, accounts));
  }
  return box;
}

/* 状态不同，页面上该说的事也不同：没授权的不许让用户以为在攒钱。 */
function bxSavingsStateBlock(box, goal, state) {
  if (state === 'active') {
    box.appendChild(kvRow('每期划', textOf(goal.amount_yuan) + ' 元（' + textOf(goal.cadence_label) +
      (bxNum(goal.auto_fen) ? ' · 你锁了固定值' : ' · 按缺口自动算') + '）', true));
    box.appendChild(kvRow('下次划款', textOf(goal.next_due_text) || '—'));
    box.appendChild(kvRow('从这张卡', textOf(goal.account_label) || '—'));
  } else if (state === 'draft') {
    box.appendChild(el('div', 'notice info',
      '还没授权自动划款：这个计划现在只是算个数，一分钱没动。点下面「设置每期自动划」才会开始真的划钱。'));
  } else if (state === 'paused') {
    box.appendChild(el('div', 'notice warn',
      '已暂停：不会再自动划。已经划进去的 ' + textOf(goal.saved_yuan) + ' 元还在目标里，取消计划才会退回卡上。'));
  } else if (state === 'achieved') {
    box.appendChild(el('div', 'notice info',
      '已经存够 ' + textOf(goal.target_yuan) + ' 元，不会再自动划。想加码就改目标金额。'));
  }
  if (textOf(goal.last_result) === 'insufficient') {
    box.appendChild(el('div', 'notice warn', BX_SAVINGS_LAST.insufficient +
      (goal.last_run_text ? '（' + textOf(goal.last_run_text) + '）' : '') + '。'));
  }
}

function bxSavingsDeposits(box, goal) {
  const deposits = bxArr(goal.deposits);
  if (!deposits.length) { return; }
  const fold = el('details', 'goal-why');
  fold.appendChild(el('summary', 'caps-title', '划款记录（最近 ' + deposits.length + ' 笔）'));
  const list = el('div', 'list');
  deposits.forEach(function (item) {
    const refunded = textOf(item.status) === 'refunded';
    list.appendChild(bxRow(
      textOf(item.created_text) || textOf(item.due_text),
      textOf(item.amount_yuan) + ' 元 · ' + (refunded ? '已划回（' + textOf(item.refunded_text) + '）' : '已划入目标') +
        ' · 流水 ' + textOf(item.tx_id),
      ''
    ));
  });
  fold.appendChild(list);
  box.appendChild(fold);
}

function bxSavingsActions(goal, state, accounts) {
  const nodes = [];
  nodes.push(pfButton('现在存一笔 ' + textOf(goal.amount_yuan) + ' 元', 'btn btn-mini btn-green',
    function (event) { bxSavingsDeposit(goal, event.currentTarget); }));
  if (state === 'draft' || state === 'active' || state === 'paused') {
    const closing = state !== 'draft';
    nodes.push(pfButton(closing ? '改每期划款规则' : '设置每期自动划', 'btn btn-mini', function () {
      const form = $('savings-auth-' + textOf(goal.goal_id));
      if (form) { form.open = true; form.scrollIntoView({ block: 'nearest' }); }
    }));
  }
  if (state === 'active') {
    nodes.push(pfButton('暂停自动划款', 'btn btn-mini', function (event) {
      bxSavingsSimple(goal, 'pause', {}, '已暂停，不会再划。', event.currentTarget);
    }));
  }
  if (state === 'paused') {
    nodes.push(pfButton('继续自动划款', 'btn btn-mini btn-green', function (event) {
      bxSavingsSimple(goal, 'resume', {}, '已继续，下次到点照旧划。', event.currentTarget);
    }));
  }
  if (state !== 'cancelled') {
    nodes.push(pfButton('改目标金额', 'btn btn-mini', function () {
      const panel = $('panel-proactive');
      if (panel) { panel.open = true; }
      if (typeof pfStartEdit === 'function') { pfStartEdit(goal); }
    }));
    const monitorOn = goal.monitor_enabled === true;
    nodes.push(pfButton(monitorOn ? '关闭后台监测' : '开启后台监测', 'btn btn-mini' + (monitorOn ? '' : ' btn-green'),
      function (event) {
        if (typeof bxToggleMonitor === 'function') { bxToggleMonitor(goal.goal_id, !monitorOn, event.currentTarget); }
      }));
  }
  const hasMoney = bxNum(goal.deposited_fen) > 0;
  const cancel = pfButton(hasMoney ? '取消计划，钱划回' : '取消这个计划', 'btn btn-mini', function (event) {
    bxSavingsRefund(goal, event.currentTarget);
  });
  cancel.id = 'savings-cancel-' + textOf(goal.goal_id);
  nodes.push(cancel);
  const note = '按钮都会真的动钱或动计划状态，每一笔都写审计。划款记录里能点到对应流水号；'
    + '后台监测只写提醒，不动钱。';
  void accounts;
  return bxMore('这笔计划要做什么', nodes, note);
}

function bxSavingsAuthForm(goal, accounts) {
  const form = el('details', 'goal-why');
  form.id = 'savings-auth-' + textOf(goal.goal_id);
  const authorized = textOf(goal.account_id) !== '';
  form.appendChild(el('summary', 'caps-title',
    (authorized ? '改每期划款规则' : '授权每期自动划款') + '（选卡、节奏、金额）'));
  const body = el('div', 'pf-form');
  const account = el('select', 'pf-in');
  bxArr(accounts).forEach(function (item) {
    if (item.can_save !== true) { return; }
    const option = el('option', '', textOf(item.label) + ' · 余额 ' + textOf(item.balance_yuan) + ' 元');
    option.value = textOf(item.account_id);
    account.appendChild(option);
  });
  if (textOf(goal.account_id)) { account.value = textOf(goal.account_id); }
  const cadence = el('select', 'pf-in');
  const optM = el('option', '', '每月'); optM.value = 'monthly'; cadence.appendChild(optM);
  const optW = el('option', '', '每周'); optW.value = 'weekly'; cadence.appendChild(optW);
  if (textOf(goal.cadence) === 'weekly') { cadence.value = 'weekly'; }
  const auto = el('input', 'pf-in');
  auto.type = 'text';
  auto.inputMode = 'decimal';
  auto.placeholder = '留空=按缺口自动算';
  if (bxNum(goal.auto_fen)) { auto.value = textOf(goal.auto_yuan); }
  body.appendChild(kvRow('从哪张卡', '', false));
  body.appendChild(account);
  body.appendChild(kvRow('节奏', '', false));
  body.appendChild(cadence);
  body.appendChild(kvRow('每期固定金额（元）', '', false));
  body.appendChild(auto);
  const submit = el('button', 'btn btn-green', authorized ? '保存划款规则' : '授权并开始划款');
  submit.type = 'button';
  submit.addEventListener('click', function () {
    const amount = auto.value.trim() ? bxYuanToFen(auto.value) : 0;
    if (auto.value.trim() && amount <= 0) {
      bxSavingsHint(textOf(goal.goal_id), '每期金额要么留空，要么写一个大于 0 的数字。', 'bad');
      return;
    }
    bxSavingsSimple(goal, 'authorize',
      { account_id: account.value, cadence: cadence.value, auto_fen: amount },
      '授权好了：下一期开始自动划。', submit);
  });
  body.appendChild(submit);
  form.appendChild(body);
  return form;
}

function bxSavingsSimple(goal, action, body, okText, button) {
  bxSavingsHint(textOf(goal.goal_id), '正在处理…', '');
  if (button) { button.disabled = true; }
  return bxSend('/api/savings/goals/' + encodeURIComponent(textOf(goal.goal_id)) + '/' + action, 'POST', body)
    .then(function () {
      bxToast(okText, 'ok');
      return bxLoad('plans', true);
    })
    .catch(function (err) {
      bxSavingsHint(textOf(goal.goal_id), '没成：' + errorText(err), 'bad');
    })
    .then(function () { if (button) { button.disabled = false; } },
      function () { if (button) { button.disabled = false; } });
}

function bxSavingsDeposit(goal, button) {
  const amount = bxNum(goal.amount_fen);
  const text = amount > 0 ? '现在存一笔（' + textOf(goal.amount_yuan) + ' 元）' : '现在存一笔';
  bxSavingsHint(textOf(goal.goal_id), '正在从 ' + (textOf(goal.account_label) || '储蓄卡') + ' 划 ' +
    textOf(goal.amount_yuan) + ' 元…', '');
  if (button) { button.disabled = true; }
  bxSend('/api/savings/goals/' + encodeURIComponent(textOf(goal.goal_id)) + '/deposit', 'POST', {})
    .then(function (data) {
      const got = bxIsObj(data) && bxIsObj(data.deposit) ? data.deposit : {};
      bxToast(text + ' 成功：' + textOf(got.amount_yuan) + ' 元已划入目标，余额剩 ' +
        textOf(got.balance_after_yuan) + ' 元。', 'ok');
      return bxLoad('plans', true);
    })
    .catch(function (err) {
      bxSavingsHint(textOf(goal.goal_id), '没划成：' + errorText(err), 'bad');
    })
    .then(function () { if (button) { button.disabled = false; } },
      function () { if (button) { button.disabled = false; } });
}

/* 取消是唯一一个「钱往回流」的动作，点一次只变成确认态，再点一次才真退。 */
function bxSavingsRefund(goal, button) {
  const goalId = textOf(goal.goal_id);
  /* 能退的只有真的划走过的那部分：老计划里手填的起点金额退不出来，
     拿 saved_yuan 当承诺会把「退 2,250」说成实话，实际只退得出 750。 */
  const refund = textOf(goal.deposited_yuan) || '0.00';
  const nothing = bxNum(goal.deposited_fen) <= 0;
  if (button.dataset.armed !== '1') {
    button.dataset.armed = '1';
    button.textContent = nothing
      ? '确定取消？再点一下关掉这个计划'
      : '确定取消？再点一下把 ' + refund + ' 元划回卡上';
    button.classList.add('btn-danger');
    bxSavingsHint(goalId, nothing
      ? '这个计划还没真的划过钱，取消只是关掉它，余额不动；再点一次才执行。'
      : '取消会把已经划走的 ' + refund + ' 元一次性退回原卡，计划随即关闭；再点一次才执行。', 'warn');
    return;
  }
  bxSavingsHint(goalId, nothing ? '正在关掉这个计划…' : '正在把 ' + refund + ' 元划回卡上…', '');
  button.disabled = true;
  bxSend('/api/savings/goals/' + encodeURIComponent(goalId) + '/refund', 'POST', { reason: '页面上取消' })
    .then(function (data) {
      const got = bxIsObj(data) ? data : {};
      bxToast(bxNum(got.refunded_fen) > 0
        ? '已取消并划回 ' + textOf(got.refunded_yuan) + ' 元。'
        : '已取消（这个计划没划过钱，余额没动）。', 'ok');
      return bxLoad('plans', true);
    })
    .catch(function (err) {
      button.dataset.armed = '';
      button.disabled = false;
      button.textContent = nothing ? '取消这个计划' : '取消计划，钱划回';
      bxSavingsHint(goalId, '没取消成：' + errorText(err), 'bad');
    });
}

function bxFillSavingsAccounts(select, accounts) {
  if (!select) { return; }
  const keep = select.value;
  select.replaceChildren();
  let usable = 0;
  bxArr(accounts).forEach(function (item) {
    if (item.can_save !== true) { return; }
    const option = el('option', '', textOf(item.label) + ' · 余额 ' + textOf(item.balance_yuan) + ' 元');
    option.value = textOf(item.account_id);
    select.appendChild(option);
    usable += 1;
  });
  if (!usable) {
    const option = el('option', '', '没有可用的储蓄卡');
    option.value = '';
    select.appendChild(option);
  }
  if (keep) { select.value = keep; }
}

function bxBindSavingsForm() {
  const form = $('plan-savings-form');
  if (!form || form.dataset.bound === '1') { return; }
  form.dataset.bound = '1';
  form.addEventListener('submit', function (event) {
    event.preventDefault();
    bxSubmitSavingsPlan();
  });
}

function bxSubmitSavingsPlan() {
  const name = String(pfValue('plan-savings-name') || '').trim();
  const target = bxYuanToFen(pfValue('plan-savings-target'));
  const date = String(pfValue('plan-savings-date') || '').trim();
  const accountId = pfValue('plan-savings-account');
  const cadence = pfValue('plan-savings-cadence') || 'monthly';
  const autoText = String(pfValue('plan-savings-auto') || '').trim();
  const auto = autoText ? bxYuanToFen(autoText) : 0;
  const authNode = $('plan-savings-auth');
  const authorized = !authNode || authNode.checked;
  if (!name) { pfHint('plan-savings-hint', '先给这个计划起个名字，比如「换一台相机」。', 'bad'); return; }
  if (target <= 0) { pfHint('plan-savings-hint', '目标金额要写一个大于 0 的数字。', 'bad'); return; }
  if (!/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(date)) { pfHint('plan-savings-hint', '目标日期要选一个具体日期。', 'bad'); return; }
  if (autoText && auto <= 0) { pfHint('plan-savings-hint', '每期金额要么留空，要么写一个大于 0 的数字。', 'bad'); return; }
  /* 上限先自己拦一次：后端只说「最多 5,000」，来回一趟才知道很慢。 */
  if (autoText && auto > 500000) {
    pfHint('plan-savings-hint', '每期最多划 5,000 元。想多存就多分几期，建成之后也能点「现在存一笔」逐笔存。', 'bad');
    return;
  }
  const body = { name: name, target_fen: target, target_date: date };
  if (authorized) {
    if (!accountId) { pfHint('plan-savings-hint', '没有可用的储蓄卡，先把卡补上再授权自动划款。', 'bad'); return; }
    body.account_id = accountId;
    body.cadence = cadence;
    body.auto_fen = auto;
  }
  const button = $('plan-savings-submit');
  pfHint('plan-savings-hint', '正在建…', '');
  if (button) { button.disabled = true; }
  bxSend('/api/savings/goals', 'POST', body)
    .then(function (data) {
      const goal = bxIsObj(data) && bxIsObj(data.goal) ? data.goal : {};
      const done = authorized
        ? '建好了：' + textOf(goal.cadence_label) + '从 ' + textOf(goal.account_label) + ' 划 ' + textOf(goal.amount_yuan) + ' 元，下次 ' + textOf(goal.next_due_text) + '。'
        : '建好了：还没授权自动划款，只是一个计划，一分钱没动。';
      pfHint('plan-savings-hint', done, 'good');
      ['plan-savings-name', 'plan-savings-target', 'plan-savings-date', 'plan-savings-auto'].forEach(function (id) {
        pfSetValue(id, '');
      });
      return bxLoad('plans', true);
    })
    .catch(function (err) {
      pfHint('plan-savings-hint', '没建成：' + errorText(err), 'bad');
    })
    .then(function () { if (button) { button.disabled = false; } },
      function () { if (button) { button.disabled = false; } });
}
