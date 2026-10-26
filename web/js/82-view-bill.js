/* 82-view-bill.js — 账单页的手动记一笔；计划页的调度（立刻跑）。 */
'use strict';

/* ----------------------------------------------------- 手动记一笔 */
function bxBindManualForm() {
  const form = $('bill-manual-form');
  if (!form || form.dataset.bound === '1') { return; }
  form.dataset.bound = '1';
  form.addEventListener('submit', function (event) {
    event.preventDefault();
    bxSubmitManualTransaction();
  });
}

function bxSubmitManualTransaction() {
  const btn = $('bill-manual-submit');
  const result = $('bill-manual-result');
  const merchant = textOf($('bill-manual-merchant') && $('bill-manual-merchant').value).trim();
  const amountText = textOf($('bill-manual-amount') && $('bill-manual-amount').value).trim();
  const direction = textOf($('bill-manual-direction') && $('bill-manual-direction').value) || 'out';
  const category = textOf($('bill-manual-category') && $('bill-manual-category').value).trim() || '其他';
  const note = textOf($('bill-manual-note') && $('bill-manual-note').value).trim();
  const amountFen = bxYuanToFen(amountText);
  /* 校验提示写在这张表单自己的位置上：只发到 AI 助手页的话，人在账单页点提交
     会觉得按钮没反应（同 A21 那类反馈跑到别的页）。 */
  const say = function (text) {
    appendBubble('sys', text, 'manual');
    if (result) { result.replaceChildren(); result.appendChild(el('div', 'card-note', text)); }
  };
  if (result) { result.replaceChildren(); }
  if (!merchant) { say('先写这笔是跟谁发生的，例如「楼下便利店」。'); return; }
  if (!amountFen) { say('金额要写大于 0 的数，例如 23.50。'); return; }
  if (btn) { btn.disabled = true; }
  bxSend('/api/transactions/manual', 'POST', {
    merchant: merchant, amount_fen: amountFen, direction: direction, category: category, note: note
  }).then(function (res) {
    const tx = bxIsObj(res && res.transaction) ? res.transaction : {};
    const balance = bxNum(res && res.balance_after_fen);
    if (btn) { btn.disabled = false; }
    appendBubble('sys', '已记下：' + textOf(tx.counterparty) + ' ' + textOf(tx.amount_yuan) + ' 元（' +
      (textOf(tx.direction) === 'in' ? '收入' : '支出') + '）' +
      (balance === null ? '' : '，记完余额 ' + pfYuanFromFen(balance) + ' 元') +
      (res && res.idempotent ? '。这笔今天已经记过，没有重复记账。' : '。'), 'manual');
    if (res && res.notice) { appendBubble('sys', textOf(res.notice)); }
    if (result) {
      result.replaceChildren();
      result.appendChild(el('div', 'card-note', '流水号 ' + textOf(tx.transaction_id) + ' · 渠道 manual · 分类 ' +
        textOf(tx.category) + (tx.note ? ' · 备注 ' + textOf(tx.note) : '')));
    }
    ['bill-manual-merchant', 'bill-manual-amount', 'bill-manual-note'].forEach(function (id) {
      const node = $(id);
      if (node) { node.value = ''; }
    });
    bxCacheDrop();
    return bxLoad('bill', true);
  }).catch(function (err) {
    if (btn) { btn.disabled = false; }
    const why = errorText(err);
    appendBubble('sys', '这笔没记上：' + why, 'error');
    if (result) {
      result.replaceChildren();
      result.appendChild(el('div', 'card-note', '没记上：' + why));
    }
  });
}

/* --------------------------------------------------------- 计划与提醒 */
function bxRenderScheduler(host, scheduler, extra) {
  if (!host) { return; }
  host.replaceChildren();
  const on = bxIsObj(scheduler) && scheduler.enabled === true && scheduler.running === true;
  const card = el('div', 'card tight');
  const head = el('div', 'card-head');
  head.appendChild(el('h3', '', '调度状态'));
  head.appendChild(el('span', 'spacer'));
  head.appendChild(el('span', 'tag ' + (on ? 'ok' : 'warn'), on ? '后台调度在跑' : '没有后台调度'));
  card.appendChild(head);
  const body = el('div', 'list');
  if (bxIsObj(scheduler)) {
    const kindText = BX_SCHED_KIND[textOf(scheduler.kind)] || textOf(scheduler.kind);
    const everySec = bxNum(scheduler.interval_ms) !== null ? Math.round(bxNum(scheduler.interval_ms) / 1000) : null;
    body.appendChild(bxRow('调度方式', kindText, ''));
    body.appendChild(bxRow('多久检查一次', everySec === null ? BX_NA : bxCount(everySec, ' 秒'), ''));
    if (scheduler.monitoring_scope) {
      body.appendChild(bxRow('持续监测的范围', textOf(scheduler.monitoring_scope), ''));
    }
    const monitor = bxIsObj(scheduler.monitor) ? scheduler.monitor : null;
    if (monitor) {
      const everyMs = bxNum(monitor.interval_ms) !== null ? Math.round(bxNum(monitor.interval_ms) / 1000) : null;
      body.appendChild(bxRow('目标监测开关',
        monitor.auto === true ? '打开的目标会被节拍器唤起' + (everyMs === null ? '' : '（同一个目标最多每 ' + bxCount(everyMs, ' 秒') + ' 一次）') : '不会自动跑',
        ''));
    }
  }
  card.appendChild(body);
  if (bxIsObj(scheduler) && scheduler.note) { card.appendChild(el('div', 'card-note', textOf(scheduler.note))); }
  if (bxIsObj(scheduler) && scheduler.manual_endpoint) {
    card.appendChild(el('div', 'card-note',
      '技术细节（普通使用不需要关心）：手动触发接口 ' + textOf(scheduler.manual_endpoint) +
      '，也可以直接点下面的「立即执行到期任务」。'));
  }
  if (extra) { card.appendChild(el('div', 'card-note', extra)); }
  host.appendChild(card);
}

function bxRunScheduledNow() {
  const btn = $('plan-run-now');
  if (btn) { btn.disabled = true; }
  return fetch('/api/scheduled/run', { method: 'POST', headers: { Accept: 'application/json' } })
    .then(function (response) {
      if (!response.ok) { throw new Error('后端返回 HTTP ' + response.status); }
      return response.json();
    })
    .then(function (json) {
      appendBubble('sys', '手动触发了一次到期任务检查：执行 ' + textOf(json && json.executed) + ' 条。');
      bxRefresh();
    })
    .catch(function (err) {
      appendBubble('sys', '触发失败：' + errorText(err));
    })
    .finally(function () {
      if (btn) { btn.disabled = false; }
    });
}
