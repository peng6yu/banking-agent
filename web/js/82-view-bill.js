/* 82-view-bill.js — 账单页的手动记一笔；计划页的调度（立刻跑）。 */
'use strict';

/* ----------------------------------------------------- 手动记一笔 */

/* 一次提交的身份证：同一次提交重试（比如网络抖了一下又点一次）只记一笔；
   用户改了金额/时间/商户就换一张身份证，变成新的一笔。
   这就是「一天两杯同价咖啡」能记成两笔、而手抖双击不会变两笔的原因。 */
let bxManualNonce = '';

function bxNewNonce() {
  if (window.crypto && typeof window.crypto.randomUUID === 'function') {
    return window.crypto.randomUUID();
  }
  return 'n' + Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
}

/* 「记到哪张卡」的选项由账单响应里的 accounts 填：和上面切卡看账单用同一份，
   不会出现「切卡时有这张卡、补录时没有」。 */
function bxFillManualAccounts(accounts, current) {
  const sel = $('bill-manual-account');
  if (!sel) { return; }
  /* 用户在表单里自己选过卡，就尊重他的选择；没动过则跟着上面切卡的那张走——
     否则页面明明在看信用卡，补录却还记到储蓄卡上。 */
  if (sel.dataset.touchedBound !== '1') {
    sel.dataset.touchedBound = '1';
    sel.addEventListener('change', function () { sel.dataset.touched = '1'; });
  }
  const touched = sel.dataset.touched === '1';
  const keep = sel.value;
  sel.replaceChildren();
  const def = document.createElement('option');
  def.value = '';
  def.textContent = '默认（主储蓄卡）';
  sel.appendChild(def);
  bxArr(accounts).forEach(function (acct) {
    const opt = document.createElement('option');
    opt.value = textOf(acct.account_id);
    opt.textContent = textOf(acct.name) + ' 尾号 ' + textOf(acct.tail) +
      (textOf(acct.type) === 'credit' ? '（信用卡，总额度 ' + textOf(acct.credit_limit_yuan) + ' 元）' : '');
    sel.appendChild(opt);
  });
  const target = touched
    ? keep
    : ((current && textOf(current.account_id)) || keep);
  if (target) { sel.value = target; }
}

function bxBindManualForm() {
  const form = $('bill-manual-form');
  if (!form || form.dataset.bound === '1') { return; }
  form.dataset.bound = '1';
  form.addEventListener('submit', function (event) {
    event.preventDefault();
    bxSubmitManualTransaction();
  });
  /* 改了内容就是另一笔：换一张提交编号，服务端不会再把这次当成上一次的重试。 */
  form.addEventListener('input', function () { bxManualNonce = ''; });
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
  const accountSel = $('bill-manual-account');
  const accountId = textOf(accountSel && accountSel.value).trim();
  const tsInput = $('bill-manual-ts');
  const tsText = textOf(tsInput && tsInput.value).trim();
  const tsMs = tsText ? new Date(tsText).getTime() : null;
  /* 校验提示写在这张表单自己的位置上：只发到 AI 助手页的话，人在账单页点提交
     会觉得按钮没反应（同 A21 那类反馈跑到别的页）。 */
  const say = function (text) {
    appendBubble('sys', text, 'manual');
    if (result) { result.replaceChildren(); result.appendChild(el('div', 'card-note', text)); }
  };
  if (result) { result.replaceChildren(); }
  if (!merchant) { say('先写这笔是跟谁发生的，例如「楼下便利店」。'); return; }
  if (!amountFen) { say('金额要写大于 0 的数，例如 23.50。'); return; }
  if (tsText && !isFinite(tsMs)) { say('发生时间没看懂，换一个日期时间，或者留空记成现在。'); return; }
  /* 先在前端挡一次未来时间：让人当场改，而不是等后端 400。后端仍然会再挡一次。 */
  if (tsMs !== null && tsMs > Date.now() + 60000) { say('发生时间不能是未来；补录过去的账才需要填时间。'); return; }
  if (!bxManualNonce) { bxManualNonce = bxNewNonce(); }
  if (btn) { btn.disabled = true; }
  const payload = {
    merchant: merchant, amount_fen: amountFen, direction: direction, category: category, note: note,
    client_nonce: bxManualNonce
  };
  if (accountId) { payload.account_id = accountId; }
  if (tsMs !== null) { payload.ts_ms = tsMs; }
  bxSend('/api/transactions/manual', 'POST', payload).then(function (res) {
    const tx = bxIsObj(res && res.transaction) ? res.transaction : {};
    const account = bxIsObj(res && res.account) ? res.account : {};
    const isCredit = textOf(account.type) === 'credit';
    const balance = bxNum(res && res.balance_after_fen);
    if (btn) { btn.disabled = false; }
    /* 记成功了这张编号就作废：下一次提交是另一笔，哪怕内容一模一样。 */
    bxManualNonce = '';
    appendBubble('sys', '已记下：' + textOf(tx.counterparty) + ' ' + textOf(tx.amount_yuan) + ' 元（' +
      (textOf(tx.direction) === 'in' ? '收入' : '支出') + '）' +
      (balance === null ? '' : (isCredit ? '，这张卡还能用 ' : '，记完余额 ') + pfYuanFromFen(balance) + ' 元') +
      (res && res.idempotent ? '。这一次提交刚才已经记过，没有重复记账。' : '。'), 'manual');
    if (res && res.notice) { appendBubble('sys', textOf(res.notice)); }
    if (result) {
      result.replaceChildren();
      const where = account.name
        ? '账户 ' + textOf(account.name) + ' 尾号 ' + textOf(account.tail)
        : '账户 ' + BX_NA;
      result.appendChild(el('div', 'card-note', '流水号 ' + textOf(tx.transaction_id) + ' · 渠道 ' +
        textOf(tx.channel || 'manual') + ' · 分类 ' + textOf(tx.category) + ' · ' + where +
        (tx.note ? ' · 备注 ' + textOf(tx.note) : '')));
      /* 余额与「记在信用卡上」的边界说明也留在表单下面：人正看着这张面板，
         只发到 AI 助手页等于没说。 */
      if (balance !== null) {
        result.appendChild(el('div', 'card-note',
          (isCredit ? '这张卡还能用 ' : '记完余额 ') + pfYuanFromFen(balance) + ' 元。'));
      }
      if (res && res.notice) { result.appendChild(el('div', 'card-note', textOf(res.notice))); }
      const hits = bxArr(res && res.hits);
      if (hits.length) {
        result.appendChild(el('div', 'field-label', '这笔命中了 ' + bxCount(hits.length, ' 条账单规则')));
        const list = el('div', 'list');
        hits.forEach(function (hit) {
          list.appendChild(bxRow(textOf(hit.title), textOf(hit.detail), bxMoney(hit, 'amount')));
        });
        result.appendChild(list);
      } else if (res && res.hits_note) {
        result.appendChild(el('div', 'card-note', textOf(res.hits_note)));
      }
      /* 补录完助手已经当场复核过（服务端 review），不用再点一次按钮。
         把新提醒说出来并对齐徽标，切到总览/计划页时数字已经是对的。 */
      const review = bxIsObj(res && res.review) ? res.review : {};
      const fresh = bxNum(review.created) || 0;
      result.appendChild(el('div', 'card-note', fresh > 0
        ? ('另外助手当场看过这笔账：' + bxCount(fresh, ' 条新提醒') +
           (bxArr(review.titles).length ? '（' + bxArr(review.titles).join('、') + '）' : '') +
           '，已经放进「需要你过目」。')
        : '助手当场看过这笔账：没有新的要提醒你的事。'));
      if (typeof bxReviewAfterWrite === 'function') { bxReviewAfterWrite(res); }
    }
    ['bill-manual-merchant', 'bill-manual-amount', 'bill-manual-note', 'bill-manual-ts'].forEach(function (id) {
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
/* 已核实：核实只免打扰，不是把流水删掉。这里把「哪一笔、什么结论、什么时候核实的」
   摆出来，并给一个撤销入口——点错了要能改回来，否则「核实」就成了单向门。 */
function bxRenderAcked(block, account) {
  block = bxIsObj(block) ? block : {};
  const host = $('bill-acked');
  const tag = $('bill-acked-tag');
  const note = $('bill-acked-note');
  const items = bxArr(block.items);
  const accountId = textOf((account || {}).account_id);
  if (tag) { tag.textContent = bxCount(block.count, ' 笔'); }
  if (note) { note.textContent = textOf(block.note); }
  if (!host) { return; }
  host.replaceChildren();
  if (!items.length) {
    bxEmpty(host, '还没有核实过的流水',
      '在「异常与依据」里点「是我自己刷的」或「不是我刷的」，核实过的那几笔就记在这里，随时能恢复。');
    return;
  }
  items.forEach(function (item) {
    host.appendChild(bxRow(
      textOf(item.counterparty) + ' ' + textOf(item.amount_yuan) + ' 元',
      textOf(item.ts_text) + ' · ' + textOf(item.rule_id) +
        ' · 结论：' + (textOf(item.verdict_label) || '我自己刷的') +
        (textOf(item.acked_text) ? '（' + textOf(item.acked_text) + ' 核实）' : ''),
      textOf(item.direction) === 'in' ? '收入' : '支出'
    ));
    const bar = el('div', 'anomaly-actions');
    const btn = el('button', 'btn btn-mini', '恢复成待核实');
    btn.type = 'button';
    btn.addEventListener('click', function () {
      bxArr(bar.querySelectorAll('button')).forEach(function (other) { other.disabled = true; });
      bxUnackAnomaly({ kind: item.kind, account_id: accountId, tx_ids: [item.tx_id] }).then(function (ok) {
        if (!ok) {
          bxArr(bar.querySelectorAll('button')).forEach(function (other) { other.disabled = false; });
        }
      });
    });
    bar.appendChild(btn);
    if (textOf(item.verdict) === 'not_mine') {
      bar.appendChild(el('span', 'card-note', '你标了「不是我刷的」：先临时冻结这张卡、保留流水，再找银行争议。'));
    }
    host.appendChild(bar);
  });
}

