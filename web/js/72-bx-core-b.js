/* 72-bx-core-b.js — 动作卡、人工接管、预约补验证与统一的按钮分发（bxRunAction）。 */
'use strict';

/* --------------------------------------------------------- 异常卡片 */
function bxAnomalyCard(item, full) {
  const box = el('div', 'anomaly');
  const head = el('div', 'anomaly-head');
  head.appendChild(el('span', 'anomaly-title', textOf(item.title) || '异常'));
  head.appendChild(el('span', 'tag warn', textOf(item.status) || '发现异常'));
  if (item.amount_yuan) { head.appendChild(el('span', 'tag', textOf(item.amount_yuan) + ' 元')); }
  if (bxIsObj(item.rule) && item.rule.id) { head.appendChild(el('span', 'tag info', textOf(item.rule.id))); }
  box.appendChild(head);
  if (item.detail) { box.appendChild(el('div', 'card-note', textOf(item.detail))); }

  if (full) {
    const rule = bxIsObj(item.rule) ? item.rule : {};
    if (rule.text) { box.appendChild(kvRow('规则依据', textOf(rule.text))); }
    if (rule.confidence) { box.appendChild(kvRow('规则置信度', textOf(rule.confidence) + '（启发式，不是欺诈认定）')); }
    if (item.window_text) { box.appendChild(kvRow('时间窗口', textOf(item.window_text))); }
    const cmp = bxIsObj(item.comparison) ? item.comparison : null;
    if (cmp) {
      box.appendChild(kvRow('对比', textOf(cmp.previous_month) + ' ' + bxCount(cmp.previous_fen, ' 分') +
        ' → ' + textOf(cmp.current_month) + ' ' + bxCount(cmp.current_fen, ' 分') +
        '（涨幅 ' + bxCount(cmp.hike_pct, '%') + '）'));
    }
    if (item.occurrence_count) { box.appendChild(kvRow('涉及笔数', bxCount(item.occurrence_count, ' 笔'))); }
    if (item.possible_false_positive) { box.appendChild(kvRow('误报可能', textOf(item.possible_false_positive))); }
    if (item.status_note) { box.appendChild(kvRow('怎么理解', textOf(item.status_note))); }
    const evidence = bxIsObj(item.evidence) ? item.evidence : {};
    const txs = bxArr(evidence.transactions);
    if (txs.length) {
      box.appendChild(el('div', 'field-label', '涉及流水（' + txs.length + ' 笔）'));
      box.appendChild(bxTable(
        ['时间', '对手方', '分类', '方向', '金额'],
        txs.map(function (tx) {
          return [
            textOf(tx.time_text),
            textOf(tx.counterparty),
            textOf(tx.category),
            textOf(tx.direction) === 'in' ? '收入' : '支出',
            textOf(tx.amount_yuan) + ' 元'
          ];
        }),
        [4]
      ));
    }
  }

  const actions = bxArr(item.actions);
  if (actions.length) {
    const bar = el('div', 'anomaly-actions');
    actions.forEach(function (act) {
      const btn = el('button', 'btn btn-mini', textOf(act.label) || '处理');
      btn.type = 'button';
      btn.addEventListener('click', function () { bxRunAction(act); });
      bar.appendChild(btn);
    });
    box.appendChild(bar);
  }
  return box;
}

/* --------------------------------------------------------- 人工接管队列 */
/* 后端 /api/handoffs 一直有完整接口（list/get/claim/reject/need-info/cancel/resume），
   但页面上一个入口都没有：收款人有歧义的那一轮就挂在那里，用户看不见、也处理不了。 */
function bxHandoffPost(item, action, note) {
  const hid = textOf(item && item.handoff_id);
  if (!hid) { return Promise.reject(new Error('这张单没有工单号')); }
  const key = textOf(action);
  const body = { operator: BX_OPERATOR };
  if (key === 'cancel') { body.reason = textOf(note); }
  else if (key !== 'claim') { body.note = textOf(note); }
  return bxSend('/api/handoffs/' + encodeURIComponent(hid) + '/' + key, 'POST', body);
}

function bxScheduledVerify(scheduledId, factors, bar) {
  // 预约转账到点复判变红时，行状态还是 scheduled，只是挂起等强验证。
  // 只给一句「让助手撤销」等于逼用户放弃这笔预约——这里补上就地验证。
  const panel = el('div', 'factor-inline');
  bar.appendChild(panel);
  panel.appendChild(el('div', 'card-note',
    '这笔预约到点时被判成了高风险，停在这里等你补一次强验证。验证通过后才会重新执行，执行前服务端还会再判一次，不是刷一下就放行。'));
  const wanted = bxArr(factors);
  if (!wanted.length) {
    panel.appendChild(el('div', 'card-note', '服务端没说这笔要验什么，先刷新看看。'));
    return;
  }
  const boxes = [];
  Promise.all(wanted.map(function (factor) {
    return bxSend('/api/scheduled/' + encodeURIComponent(scheduledId) + '/challenge', 'POST', { factor: factor })
      .then(function (res) { return { factor: factor, res: res }; },
        function (err) { return { factor: factor, err: err }; });
  })).then(function (rows) {
    rows.forEach(function (item) {
      if (item.err) {
        panel.appendChild(el('div', 'card-note', '拿不到' + bxFactorNames([item.factor]) + '的验证码：' + errorText(item.err)));
        return;
      }
      const data = bxIsObj(item.res) ? item.res : {};
      if (!data.challenge_id) { return; }
      const row = el('div', 'factor-code-row');
      row.appendChild(el('span', 'field-label', bxFactorNames([textOf(data.factor) || item.factor]) + ' 口令'));
      const input = el('input', 'factor-input');
      input.type = 'text';
      input.maxLength = 6;
      input.value = textOf(data.code);
      input.dataset.challenge = textOf(data.challenge_id);
      input.dataset.factor = textOf(data.factor) || item.factor;
      row.appendChild(input);
      panel.appendChild(row);
      boxes.push(input);
    });
    if (!boxes.length) {
      panel.appendChild(el('div', 'card-note', '一张挑战都没拿到，这笔预约现在没法就地验证。'));
    }
    const actions = el('div', 'factor-actions');
    const ok = el('button', 'btn btn-mini btn-green', '提交验证并放行');
    ok.type = 'button';
    ok.disabled = !boxes.length;
    ok.addEventListener('click', function () {
      const proofs = boxes.map(function (box) {
        return {
          factor: box.dataset.factor,
          challenge_id: box.dataset.challenge,
          code: String(box.value || '').replace(/\D/g, '').slice(0, 6)
        };
      });
      if (proofs.some(function (p) { return !p.code; })) {
        appendBubble('sys', '验证码还没填完。', 'confirm');
        return;
      }
      ok.disabled = true;
      bxSend('/api/scheduled/' + encodeURIComponent(scheduledId) + '/verify', 'POST', { proofs: proofs }).then(function (res) {
        const data = bxIsObj(res) ? res : {};
        const status = textOf(data.status);
        if (status === 'executed') {
          appendBubble('sys', '验证通过，这笔预约已放行执行。', 'money');
        } else if (status === 'needs_verification') {
          const missing = bxArr(data.missing);
          appendBubble('sys', '还是没通过：' + (missing.length ? '还差 ' + bxFactorNames(missing) + '。' : textOf(data.reason) || '验证没成立。'), 'confirm');
        } else {
          appendBubble('sys', '验证收到了，这笔预约现在的状态是「' + (status || '未知') + '」（没到点就不会立刻执行）。', 'sys');
        }
        bxCacheDrop();
        bxRefresh();
      }).catch(function (err) {
        ok.disabled = false;
        appendBubble('sys', '补验证没成功：' + errorText(err), 'confirm');
      });
    });
    actions.appendChild(ok);
    const later = el('button', 'btn btn-mini', '先不处理');
    later.type = 'button';
    later.addEventListener('click', function () { panel.remove(); });
    actions.appendChild(later);
    panel.appendChild(actions);
  });
}

function bxHandoffCard(item) {
  const box = el('div', 'anomaly');
  const head = el('div', 'anomaly-head');
  head.appendChild(el('span', 'anomaly-title', textOf(item.reason_label) || textOf(item.reason) || '需要人工处理'));
  head.appendChild(el('span', 'tag warn', textOf(item.status_label) || textOf(item.status) || '等待接管'));
  if (item.needs_user) { head.appendChild(el('span', 'tag bad', '需要你来定')); }
  if (item.created_text) { head.appendChild(el('span', 'tag', textOf(item.created_text))); }
  box.appendChild(head);
  if (item.request_text) { box.appendChild(kvRow('用户原话', textOf(item.request_text))); }
  if (item.failure_text) { box.appendChild(kvRow('卡在哪一步', textOf(item.failure_text))); }
  if (item.needs_user_note) { box.appendChild(el('div', 'card-note', textOf(item.needs_user_note))); }
  const meta = [];
  if (item.operator) { meta.push('上一次经手：' + textOf(item.operator)); }
  if (item.note) { meta.push('备注：' + textOf(item.note)); }
  if (item.run_id) { meta.push('轮次 ' + textOf(item.run_id)); }
  if (meta.length) { box.appendChild(el('div', 'card-note', meta.join(' · '))); }

  const actions = bxArr(item.actions).filter(function (act) { return act && textOf(act.key); });
  if (!actions.length) {
    box.appendChild(el('div', 'card-note', '这张单当前没有可执行的动作。'));
    return box;
  }
  const bar = el('div', 'anomaly-actions');
  const noteRow = el('div', 'handoff-note-row');
  noteRow.style.display = 'none';
  const noteInput = document.createElement('input');
  noteInput.type = 'text';
  noteInput.className = 'handoff-note';
  noteInput.maxLength = 500;
  const submitBtn = el('button', 'btn btn-mini btn-green', '提交');
  submitBtn.type = 'button';
  let pending = null;
  const submitter = function () {
    if (!pending) { return; }
    const act = pending;
    submitBtn.disabled = true;
    bxHandoffPost(item, act.key, noteInput.value).then(function () {
      appendBubble('sys', '已提交「' + (textOf(act.label) || textOf(act.key)) + '」，接管队列已更新。', 'handoff');
      bxCacheDrop();
      bxRefresh();
    }, function (err) {
      submitBtn.disabled = false;
      appendBubble('sys', '接管动作没成功：' + pfErrText(err), 'handoff');
    });
  };
  submitBtn.addEventListener('click', submitter);
  actions.forEach(function (act) {
    const key = textOf(act.key);
    const label = textOf(act.label) || key;
    const btn = el('button', 'btn btn-mini' + (key === 'claim' ? ' btn-green' : (key === 'reject' || key === 'cancel' ? ' btn-red' : '')), label);
    btn.type = 'button';
    btn.addEventListener('click', function () {
      if (!act.needs_note) {
        btn.disabled = true;
        bxHandoffPost(item, key, '').then(function () {
          appendBubble('sys', '已提交「' + label + '」，接管队列已更新。', 'handoff');
          bxCacheDrop();
          bxRefresh();
        }, function (err) {
          btn.disabled = false;
          appendBubble('sys', '接管动作没成功：' + pfErrText(err), 'handoff');
        });
        return;
      }
      pending = act;
      noteRow.style.display = '';
      submitBtn.textContent = '提交：' + label;
      noteInput.focus();
    });
    bar.appendChild(btn);
  });
  box.appendChild(bar);
  noteRow.appendChild(noteInput);
  noteRow.appendChild(submitBtn);
  box.appendChild(noteRow);
  box.appendChild(el('div', 'card-note', '所有动作都以「' + BX_OPERATOR + '」的身份记进审计；恢复执行不会再替你点确认卡。'));
  return box;
}

function bxRenderHandoffs(host, block, tagId) {
  if (!host) { return; }
  const data = bxIsObj(block) ? block : {};
  const items = bxArr(data.items);
  if (tagId) { setText($(tagId), items.length ? items.length + ' 张' : '—'); }
  if (!items.length) {
    bxEmpty(host, textOf(data.empty_reason) || '现在没有需要人工接管的单', textOf(data.source));
    return;
  }
  host.replaceChildren();
  host.appendChild(el('div', 'card-note', '共 ' + items.length + ' 张 · ' + (textOf(data.source) || '按创建时间倒序')));
  items.slice(0, 6).forEach(function (item) { host.appendChild(bxHandoffCard(item)); });
}

/* 疑似订阅的定性：只落一条结论，不动钱、不停代扣。
   判成「不是我订的」之后这条推断不会再被端上来，否则用户每次刷新都被同一批
   猜测打扰却什么都做不了。 */
function bxSubscriptionVerdict(merchant, verdict) {
  const name = textOf(merchant);
  if (!name) { return Promise.resolve(); }
  return bxSend('/api/subscriptions/verdict', 'POST', { merchant: name, verdict: verdict })
    .then(function (res) {
      const label = textOf((bxIsObj(res) && bxIsObj(res.verdict)) ? res.verdict.verdict_label : '') || verdict;
      const tail = verdict === 'confirmed'
        ? '它已经从「疑似订阅」待办里拿掉了。这条只是留个定性，不会替你去商户退订，也不会变成银行侧代扣。'
        : '它已经从「疑似订阅」待办里拿掉了，以后不再拿这条来问你。';
      appendBubble('sys', '已记下：' + name + ' —— ' + label + '。' + tail + '点错了可以在管理页「已定性的商户」里点「重新当成疑似」。', 'money');
      bxCacheDrop();
      return bxRefresh();
    });
}

function bxSubscriptionVerdictClear(merchant) {
  const name = textOf(merchant);
  if (!name) { return Promise.resolve(); }
  return bxSend('/api/subscriptions/verdict/clear', 'POST', { merchant: name })
    .then(function (res) {
      const back = bxIsObj(res) && res.cleared === true;
      appendBubble('sys', back
        ? '已撤回对「' + name + '」的定性。它重新回到「疑似订阅」待办里，你也可以再说一句「不是订阅」。'
        : '「' + name + '」本来就没有定性记录，现在仍是它原本的样子。', 'money');
      bxCacheDrop();
      return bxRefresh();
    });
}

function bxRunAction(act) {
  if (!bxIsObj(act)) { return; }
  const kind = textOf(act.kind);
  if (kind === 'subscription_verdict') {
    bxSubscriptionVerdict(act.merchant, textOf(act.verdict)).catch(function (err) {
      appendBubble('sys', '这条结论没能记下来：' + errorText(err), 'error');
    });
    return;
  }
  if (kind === 'subscription_verdict_clear') {
    bxSubscriptionVerdictClear(act.merchant).catch(function (err) {
      appendBubble('sys', '这条结论没能撤回：' + errorText(err), 'error');
    });
    return;
  }
  if (kind === 'subscription_check') {
    const merchant = textOf(act.merchant);
    bxAsk(textOf(act.prompt) || ('帮我核实一下「' + merchant + '」这几笔是不是订阅'));
    return;
  }
  if (kind === 'prompt' && act.prompt) { bxAsk(textOf(act.prompt)); return; }
  if (kind === 'inspect') {
    if (textOf(act.id) === 'open_subscriptions') { bxSetView('manage'); bxFlashCard('mg-subs'); return; }
    bxSetView('bill');
    bxFlashCard('bill-tx');
    return;
  }
  if (kind === 'tool') {
    const prompt = BX_TOOL_PROMPT[textOf(act.tool)] || ('帮我用一下「' + textOf(act.label) + '」这个能力');
    bxAsk(prompt);
    return;
  }
  if (act.prompt) { bxAsk(textOf(act.prompt)); return; }
  appendBubble('sys', '这个入口还没接上具体动作。');
}
