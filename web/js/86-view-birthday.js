/* 86-view-birthday.js — 生日计划、提醒与监测开关。 */
'use strict';

const BX_REPEAT_TEXT = { none: '一次性', daily: '每天', weekly: '每周', monthly: '每月' };

/* datetime-local 的值是「本地时间字符串」，new Date() 解析它用的是浏览器本地时区，
   和后端 reminder_time 按本地日历算是同一套，不会出现差 8 小时的提醒。 */
function bxDueParts(value) {
  if (!value) { return null; }
  const at = new Date(value);
  if (isNaN(at.getTime())) { return null; }
  return { ms: at.getTime(), hour: at.getHours(), minute: at.getMinutes(), weekday: (at.getDay() + 6) % 7, day: at.getDate() };
}

function bxBindReminderForm() {
  const form = $('plan-reminder-form');
  if (!form || form.dataset.bound === '1') { return; }
  form.dataset.bound = '1';
  form.addEventListener('submit', function (event) {
    event.preventDefault();
    bxSubmitReminder();
  });
}

function bxSubmitReminder() {
  const text = textOf($('plan-reminder-text').value).trim();
  const btn = $('plan-reminder-submit');
  if (!btn) { return; }
  if (!text) {
    appendBubble('system', '先写一句「提醒我什么」，例如「提醒我明天上午 9 点开会」。');
    $('plan-reminder-text').focus();
    return;
  }
  const repeat = textOf($('plan-reminder-repeat').value) || 'none';
  const due = bxDueParts($('plan-reminder-due').value);
  const body = { text: text, repeat: repeat };
  if (due) {
    body.hour = due.hour;
    body.minute = due.minute;
    body.weekday = due.weekday;
    body.day = due.day;
    /* 一次性提醒直接给绝对时间；重复提醒只给「几点/周几/几号」，让后端从当前时间算下一次，
       否则用户选了一个已经过去的日期，就会立刻触发出一次莫名其妙的提醒。 */
    if (repeat === 'none') { body.due_ms = due.ms; }
  }
  btn.disabled = true;
  bxSend('/api/reminders', 'POST', body).then(function (res) {
    const data = bxIsObj(res) ? res : {};
    appendBubble('system', '提醒已建：' + textOf(data.repeat_label) + ' · 下次 ' + textOf(data.due_text) +
      '（不动钱，到点只提醒你回来确认）。', 'plan');
    $('plan-reminder-text').value = '';
    $('plan-reminder-due').value = '';
    btn.disabled = false;
    bxRefresh();
  }).catch(function (err) {
    btn.disabled = false;
    appendBubble('system', '建提醒失败：' + errorText(err));
  });
}

/* 生日计划：预算真实占用可支配金额（余额不动），付款永远走确认 + 多因子。 */
const BX_BIRTHDAY_STATUS = {
  reserved: '预算已预留（未付款）', paying: '付款执行中', committed: '已付款',
  declined: '已拒绝', failed: '付款失败', released: '预算已释放'
};

function bxYuanToFen(value) {
  const num = Number(String(value == null ? '' : value).replace(/[^0-9.]/g, ''));
  return isFinite(num) && num > 0 ? Math.round(num * 100) : 0;
}

function bxFactorNames(list) {
  const names = bxArr(list).map(function (factor) { return BX_FACTOR_TEXT[textOf(factor)] || textOf(factor); });
  return names.length ? names.join('、') : '多因子验证';
}

function bxBindBirthdayForm() {
  const form = $('plan-birthday-form');
  if (!form || form.dataset.bound === '1') { return; }
  form.dataset.bound = '1';
  form.addEventListener('submit', function (event) {
    event.preventDefault();
    bxSubmitBirthdayPlan();
  });
}

function bxSubmitBirthdayPlan() {
  const dateEl = $('plan-birthday-date');
  const btn = $('plan-birthday-submit');
  if (!dateEl || !btn) { return; }
  const day = textOf(dateEl.value).trim();
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) {
    appendBubble('system', '先选生日是哪天，后端要按这个日期算到点时间。');
    dateEl.focus();
    return;
  }
  const budget = bxYuanToFen($('plan-birthday-budget').value);
  if (!budget) {
    appendBubble('system', '预算要填一个大于 0 的数（元）。');
    return;
  }
  const items = [];
  const flowers = bxYuanToFen($('plan-birthday-flowers').value);
  const cake = bxYuanToFen($('plan-birthday-cake').value);
  if (flowers) { items.push({ key: 'flowers', label: '鲜花', merchant: 'flower_shop', amount_fen: flowers }); }
  if (cake) { items.push({ key: 'cake', label: '蛋糕', merchant: 'cake_shop', amount_fen: cake }); }
  if (!items.length) {
    appendBubble('system', '至少留一项：鲜花或蛋糕，金额大于 0。');
    return;
  }
  const body = {
    birthday: day,
    budget_fen: budget,
    items: items,
    save_authorization: !!($('plan-birthday-auth') || {}).checked
  };
  const title = textOf($('plan-birthday-title').value).trim();
  if (title) { body.title = title; }
  btn.disabled = true;
  bxSend('/api/plans/birthday', 'POST', body).then(function (res) {
    btn.disabled = false;
    const data = bxIsObj(res) ? res : {};
    const plan = bxIsObj(data.plan) ? data.plan : {};
    appendBubble('system', '生日计划已建：' + textOf(plan.title) +
      '，预算已占用可支配金额（余额没动），到点只提醒，付款要走确认 + 多因子。', 'plan');
    bxRenderBirthdayResult(plan);
    bxRefresh();
  }).catch(function (err) {
    btn.disabled = false;
    const msg = errorText(err);
    appendBubble('system', '建生日计划失败：' + msg);
    const host = $('plan-birthday-result');
    if (host) {
      host.replaceChildren();
      host.appendChild(el('div', 'card-note', '没有建起来：' + msg));
    }
  });
}

/* 建好以后把预算、可支配金额与每项付款入口都摆出来，不用再去聊天里找。 */
function bxRenderBirthdayResult(plan) {
  const host = $('plan-birthday-result');
  if (!host) { return; }
  host.replaceChildren();
  const data = bxIsObj(plan) ? plan : {};
  const planId = textOf(data.plan_id);
  if (!planId) { return; }
  const budget = bxIsObj(data.budget) ? data.budget : {};
  const meta = bxIsObj(data.meta) ? data.meta : {};
  const box = el('div', 'anomaly');
  const head = el('div', 'anomaly-head');
  head.appendChild(el('span', 'anomaly-title', textOf(data.title) || '生日计划'));
  head.appendChild(el('span', 'tag info', '生日 ' + (textOf(meta.birthday) || '—')));
  box.appendChild(head);
  box.appendChild(el('div', 'card-note',
    '预算 ' + (textOf(budget.budget_yuan) || '—') + ' 元 · 计划内 ' + (textOf(budget.planned_yuan) || '—') +
    ' 元 · 已付款 ' + (textOf(budget.committed_yuan) || '0') + ' 元'));
  box.appendChild(el('div', 'card-note',
    '可支配 ' + (textOf(budget.available_yuan) || '—') + ' 元（余额 ' + (textOf(budget.balance_yuan) || '—') +
    '，预留 ' + (textOf(budget.reserved_budget_yuan) || '0') + '） · 到点 ' + (textOf(budget.trigger_text) || textOf(meta.trigger_text) || '—')));
  bxArr(data.items).forEach(function (item) {
    const info = bxIsObj(item) ? item : {};
    const row = el('div', 'step-row');
    const main = el('div', 'step-main');
    main.appendChild(el('span', 'step-title', (textOf(info.label) || textOf(info.item_key)) +
      ' ' + (textOf(info.amount_yuan) || '') + ' 元'));
    main.appendChild(el('span', 'tag info', textOf(info.status_label) || BX_BIRTHDAY_STATUS[textOf(info.status)] || textOf(info.status)));
    row.appendChild(main);
    row.appendChild(el('span', 'card-note', textOf(info.merchant_name) || textOf(info.merchant)));
    box.appendChild(row);
    if (textOf(info.status) === 'reserved' && textOf(info.item_key)) {
      const acts = el('div', 'factor-actions');
      const pay = el('button', 'btn btn-mini btn-green', '现在付这一项');
      pay.type = 'button';
      acts.appendChild(pay);
      box.appendChild(acts);
      const payHost = el('div', 'pf-row');
      box.appendChild(payHost);
      pay.addEventListener('click', function () {
        bxPayBirthdayItem(planId, textOf(info.item_key), pay, payHost);
      });
    }
  });
  const auth = bxIsObj(data.authorization) ? data.authorization : {};
  if (textOf(auth.status)) {
    box.appendChild(el('div', 'card-note',
      '一次性授权：' + (textOf(auth.status_label) || textOf(auth.status)) +
      '（到点仍按最新日累计重新裁决，红灯/改计划/取消都会停下来）。'));
  }
  const foot = el('div', 'factor-actions');
  const cancel = el('button', 'btn btn-mini', '取消这份计划');
  cancel.type = 'button';
  cancel.addEventListener('click', function () {
    cancel.disabled = true;
    bxSend('/api/plans/' + encodeURIComponent(planId), 'PATCH', { status: 'cancelled' })
      .then(function () {
        appendBubble('system', '生日计划已取消，预留的预算会释放。', 'plan');
        bxRefresh();
        host.replaceChildren();
        host.appendChild(el('div', 'card-note', '计划已取消（预算已释放）。'));
      })
      .catch(function (err) {
        cancel.disabled = false;
        appendBubble('system', '取消失败：' + errorText(err));
      });
  });
  foot.appendChild(cancel);
  box.appendChild(foot);
  host.appendChild(box);
}

function bxPayBirthdayItem(planId, itemKey, btn, host) {
  if (!planId || !itemKey) { return; }
  // 会话 id 带 :ui 后缀：后台到点自动付款用的是 plan:{id}:{key}，不能共用同一轮
  const sid = 'plan:' + planId + ':' + itemKey + ':ui';
  if (btn) { btn.disabled = true; }
  bxSend('/api/plans/' + encodeURIComponent(planId) + '/items/' + encodeURIComponent(itemKey) + '/pay',
    'POST', { session_id: sid })
    .then(function (res) { bxPayBirthdayOutcome(res, planId, itemKey, sid, btn, host); })
    .catch(function (err) {
      if (btn) { btn.disabled = false; }
      appendBubble('system', '付款没成功：' + errorText(err));
    });
}

function bxReloadBirthdayResult(planId) {
  if (!planId) { return; }
  bxSend('/api/plans/' + encodeURIComponent(planId), 'GET')
    .then(function (res) {
      const data = bxIsObj(res) ? res : {};
      const plan = bxIsObj(data.plan) ? data.plan : null;
      if (plan) { bxRenderBirthdayResult(plan); }
    })
    .catch(function () {});
}

function bxPayBirthdayOutcome(res, planId, itemKey, sid, btn, host) {
  const data = bxIsObj(res) ? res : {};
  const status = textOf(data.status);
  if (btn) { btn.disabled = false; }
  if (status === 'waiting_factor') {
    appendBubble('system', '这一项要动钱，还差' + bxFactorNames(data.required) +
      '；补齐之前一分钱不会动。', 'security');
    bxBirthdayFactor(host, planId, itemKey, sid, textOf(data.confirmation_id), btn);
    return;
  }
  if (status === 'paid' || status === 'already_paid') {
    appendBubble('system', textOf(data.message) || '这一项已付款。', 'money');
    bxRefresh();
    bxReloadBirthdayResult(planId);
    return;
  }
  if (status === 'declined') {
    appendBubble('system', '这一项已拒绝付款，预算会释放，不会扣钱。', 'money');
    bxRefresh();
    bxReloadBirthdayResult(planId);
    return;
  }
  appendBubble('system', '这一项没付成：' + (textOf(data.user_text) || textOf(data.message) || status || '未知原因'));
  bxRefresh();
  bxReloadBirthdayResult(planId);
}

function bxBirthdayFactor(host, planId, itemKey, sid, cid, btn) {
  if (!host || !cid) { return; }
  host.replaceChildren();
  const box = el('div', 'factor-inline');
  box.appendChild(el('div', 'field-label', '补一次验证就能付这一项'));
  bxSend('/api/factor/challenge', 'POST', { session_id: sid, confirmation_id: cid }).then(function (res) {
    const data = bxIsObj(res) ? res : {};
    const challenges = bxArr(data.challenges);
    if (!challenges.length) {
      box.appendChild(el('div', 'card-note', '这次没拿到验证挑战，可以重试一次。'));
      const retry = el('button', 'btn btn-mini', '重新获取验证码');
      retry.type = 'button';
      retry.addEventListener('click', function () {
        bxBirthdayFactor(host, planId, itemKey, sid, cid, btn);
      });
      box.appendChild(retry);
      host.appendChild(box);
      return;
    }
    const inputs = {};
    challenges.forEach(function (ch) {
      const info = bxIsObj(ch) ? ch : {};
      const row = el('div', 'factor-code-row');
      row.appendChild(el('span', 'field-label', (BX_FACTOR_TEXT[textOf(info.factor)] || textOf(info.factor)) + ' 口令'));
      const input = el('input', 'factor-input');
      input.type = 'text';
      input.maxLength = 8;
      input.value = textOf(info.code);
      input.setAttribute('data-challenge', textOf(info.challenge_id));
      input.setAttribute('data-factor', textOf(info.factor));
      row.appendChild(input);
      box.appendChild(row);
      inputs[textOf(info.factor)] = input;
    });
    box.appendChild(el('div', 'card-note', '口令由服务端签发、只为这一笔有效；提交后服务端会重新校验并重新裁决。'));
    const actions = el('div', 'factor-actions');
    const submit = el('button', 'btn btn-mini btn-green', '提交验证并付款');
    submit.type = 'button';
    submit.addEventListener('click', function () {
      const proofs = [];
      Object.keys(inputs).forEach(function (factor) {
        proofs.push({
          factor: factor,
          challenge_id: inputs[factor].getAttribute('data-challenge'),
          code: textOf(inputs[factor].value).trim()
        });
      });
      submit.disabled = true;
      bxSend('/api/plans/' + encodeURIComponent(planId) + '/items/' + encodeURIComponent(itemKey) + '/pay',
        'POST', { session_id: sid, proofs: proofs })
        .then(function (r2) { bxPayBirthdayOutcome(r2, planId, itemKey, sid, btn, host); })
        .catch(function (err) {
          submit.disabled = false;
          appendBubble('system', '验证没通过：' + errorText(err));
        });
    });
    const deny = el('button', 'btn btn-mini btn-red', '拒绝这一项');
    deny.type = 'button';
    deny.addEventListener('click', function () {
      deny.disabled = true;
      bxSend('/api/plans/' + encodeURIComponent(planId) + '/items/' + encodeURIComponent(itemKey) + '/pay',
        'POST', { session_id: sid, approve: false })
        .then(function (r3) { bxPayBirthdayOutcome(r3, planId, itemKey, sid, btn, host); })
        .catch(function (err) {
          deny.disabled = false;
          appendBubble('system', '拒绝失败：' + errorText(err));
        });
    });
    actions.appendChild(submit);
    actions.appendChild(deny);
    box.appendChild(actions);
    host.appendChild(box);
  }).catch(function (err) {
    box.appendChild(el('div', 'card-note', '拿验证挑战失败：' + errorText(err)));
    host.appendChild(box);
  });
}

function bxRenderReminders(block) {
  const host = $('plan-reminders');
  if (!host) { return; }
  host.replaceChildren();
  const data = bxIsObj(block) ? block : {};
  const items = bxArr(data.items);
  if (items.length) { host.appendChild(el('div', 'card-note', '共 ' + items.length + ' 条提醒，到点只提醒、不动钱。')); }
  if (!items.length) {
    bxEmpty(host, '还没有提醒', textOf(data.empty_reason) || '在上面填一句、选个时间就能建；也可以直接跟助手说「提醒我…」。');
    return;
  }
  const list = el('div', 'list');
  items.forEach(function (item) {
    const box = el('div', 'anomaly');
    const head = el('div', 'anomaly-head');
    head.appendChild(el('span', 'anomaly-title', textOf(item.title)));
    head.appendChild(el('span', 'tag info', textOf(item.repeat_label) || BX_REPEAT_TEXT[textOf(item.repeat)] || '一次性'));
    // 这条提醒盯的是哪笔钱：还款日 / 会员扣费 / 预约转账 / 预算 / 储蓄目标 / 额度
    if (textOf(item.anchor_label)) { head.appendChild(el('span', 'tag', textOf(item.anchor_label))); }
    box.appendChild(head);
    box.appendChild(el('div', 'card-note',
      '下次 ' + (textOf(item.due_text) || BX_NA) + ' · ' + (textOf(item.state_label) || textOf(item.state)) +
      ' · 计划号 ' + textOf(item.plan_id)));
    const acts = el('div', 'anomaly-actions');
    const end = el('button', 'btn btn-mini', '结束这条提醒');
    end.type = 'button';
    end.addEventListener('click', function () {
      end.disabled = true;
      bxSend('/api/plans/' + encodeURIComponent(textOf(item.plan_id)), 'PATCH', { status: 'cancelled' })
        .then(function () {
          appendBubble('system', '提醒已结束：' + textOf(item.title));
          bxRefresh();
        })
        .catch(function (err) {
          end.disabled = false;
          appendBubble('system', '结束失败：' + errorText(err));
        });
    });
    acts.appendChild(end);
    box.appendChild(acts);
    list.appendChild(box);
  });
  host.appendChild(list);
}

function bxToggleMonitor(goalId, enabled, btn) {
  if (!goalId) { return Promise.resolve(null); }
  if (btn) { btn.disabled = true; }
  return bxSend('/api/savings/goals/' + encodeURIComponent(goalId), 'PATCH', { monitor_enabled: enabled === true })
    .then(function () {
      appendBubble('sys', enabled === true
        ? '已开启后台监测：节拍器会按间隔检查这个目标，提醒写进「提醒」，不动钱。'
        : '已关闭后台监测：这个目标不会再被后台唤起，需要时可以在页面手动跑一次。');
      bxRefresh();
    })
    .catch(function (err) {
      appendBubble('sys', '切换监测开关失败：' + errorText(err));
    })
    .finally(function () {
      if (btn) { btn.disabled = false; }
    });
}

/* 近期可能发生的支出：已知代扣的下次扣费日期，打开页面时算，不推送。 */
/* 落库的跨场景计划：状态与后端一致（active/paused/completed/cancelled/failed），
   资金动作永远是「等你确认」，所以这里只有「推进到点步骤」，没有自动扣款。 */
