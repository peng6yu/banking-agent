/* 72-bx-core-b.js — 动作卡、人工接管、预约补验证与统一的按钮分发（bxRunAction）。 */
'use strict';

/* 页面级轻提示：卡片一刷新，卡内那句状态行就跟着没了；写操作至少要留一条
   「刚才那一下到底成没成」的可见回执，否则用户会以为按钮没接上。 */
function bxToast(text, kind) {
  let host = document.getElementById('bx-toast');
  if (!host) {
    host = document.createElement('div');
    host.id = 'bx-toast';
    document.body.appendChild(host);
  }
  const item = el('div', 'bx-toast-item' + (kind ? ' is-' + kind : ''), textOf(text));
  host.appendChild(item);
  setTimeout(function () { item.classList.add('is-out'); }, 2800);
  setTimeout(function () {
    if (item.parentNode) { item.parentNode.removeChild(item); }
  }, 3400);
}

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
            textOf(tx.ts_text),
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
      /* 与 /api/factor/challenge 同一形状：挑战在 challenges 列表里，不再读扁平字段。 */
      const data = bxIsObj(item.res) ? item.res : {};
      const challenge = bxArr(data.challenges)[0] || {};
      if (!challenge.challenge_id) { return; }
      const row = el('div', 'factor-code-row');
      row.appendChild(el('span', 'field-label', bxFactorNames([textOf(challenge.factor) || item.factor]) + ' 口令'));
      const input = el('input', 'factor-input');
      input.type = 'text';
      input.maxLength = 6;
      input.value = textOf(challenge.code);
      input.dataset.challenge = textOf(challenge.challenge_id);
      input.dataset.factor = textOf(challenge.factor) || item.factor;
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
  /* 反馈必须留在卡片上：以前成功/失败都只往 AI 助手页的画布塞一条气泡，
     用户停在总览页时看不到任何变化，会以为按钮点坏了。 */
  const status = el('div', 'card-note handoff-status');
  status.style.display = 'none';
  const say = function (text, kind) {
    status.style.display = '';
    status.textContent = text;
    status.className = 'card-note handoff-status' + (kind ? ' is-' + kind : '');
    if (kind === 'ok' || kind === 'bad') { bxToast(text, kind); }
  };
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
  const closeNoteBtn = el('button', 'btn btn-mini', '不填了');
  closeNoteBtn.type = 'button';
  let pending = null;
  closeNoteBtn.addEventListener('click', function () {
    pending = null;
    noteRow.style.display = 'none';
    noteInput.value = '';
    say('这一步没提交，什么都没变。', '');
  });
  const fail = function (err, btn) {
    if (btn) { btn.disabled = false; }
    say('没成功：' + pfErrText(err) + '（正在刷新队列，确认这张单现在的状态）', 'bad');
    appendBubble('sys', '接管动作没成功：' + pfErrText(err), 'handoff');
    /* 失败最常见的原因是这张单已经被处理、或者服务重启过——刷新一次，让卡片说真话。 */
    bxCacheDrop();
    bxRefresh();
  };
  const submitter = function () {
    if (!pending) { return; }
    const act = pending;
    const label = textOf(act.label) || textOf(act.key);
    submitBtn.disabled = true;
    say('正在提交「' + label + '」…', 'busy');
    bxHandoffPost(item, act.key, noteInput.value).then(function () {
      say('已提交「' + label + '」，正在刷新队列…', 'ok');
      appendBubble('sys', '已提交「' + label + '」，接管队列已更新。', 'handoff');
      bxCacheDrop();
      bxRefresh();
    }, function (err) {
      submitBtn.disabled = false;
      fail(err, null);
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
        say('正在提交「' + label + '」…', 'busy');
        bxHandoffPost(item, key, '').then(function () {
          say('已提交「' + label + '」，正在刷新队列…', 'ok');
          appendBubble('sys', '已提交「' + label + '」，接管队列已更新。', 'handoff');
          bxCacheDrop();
          bxRefresh();
        }, function (err) {
          fail(err, btn);
        });
        return;
      }
      pending = act;
      noteRow.style.display = '';
      noteInput.value = '';
      noteInput.placeholder = key === 'cancel'
        ? '写清取消原因（会记进审计，钱不动）'
        : (key === 'reject' ? '写清拒绝原因（会记进审计，钱不动）'
          : (key === 'resume' ? '写清你确认了什么，恢复执行会按这句记审计' : '写清缺哪条信息'));
      submitBtn.textContent = '提交：' + label;
      noteInput.focus();
      say('这一步要写一句备注，写好再点「提交：' + label + '」。不写也会被服务端打回。', 'busy');
    });
    bar.appendChild(btn);
  });
  box.appendChild(bar);
  /* 收款人歧义这类单，缺的是「本人选哪一位」，操作员不能替选，后端也刻意不给 resume。
     以前卡上只有领取/拒绝/取消，用户会去备注框里写答案、然后误点「提交：取消这一轮」。
     这里补一个回对话的入口，并把这句话讲明白。 */
  if (item.needs_user) {
    const go = el('button', 'btn btn-mini btn-green', '去对话里选收款人');
    go.type = 'button';
    go.addEventListener('click', function () {
      bxSetView('assistant');
      const input = $('chat-input');
      if (input) {
        input.value = textOf(item.request_text) || '这单我选收款人：';
        input.focus();
      }
      appendBubble('sys', '这单缺的是你本人的选择，操作员不能替你选：在输入框里补一句付给哪一位（例如「选尾号 4417 的那个张三」），再发送。备注框只是写给操作员的说明，不会替你选收款人。', 'handoff');
    });
    bar.appendChild(go);
  }
  noteRow.appendChild(noteInput);
  noteRow.appendChild(submitBtn);
  noteRow.appendChild(closeNoteBtn);
  box.appendChild(noteRow);
  box.appendChild(status);
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
  /* 已取消/已拒绝的单只是历史，不该和「还在等你处理」的单混在一个数字里（截图里
     「共 2 张」其中一张早就取消了）。进行中的按 ACTIVE 状态算，和服务端 ACTIVE_STATUSES 一致。 */
  const activeCount = items.filter(function (item) {
    return ['pending', 'claimed', 'need_info'].indexOf(textOf(item.status)) >= 0;
  }).length;
  const endedCount = items.length - activeCount;
  const countText = endedCount
    ? '进行中 ' + activeCount + ' 张 · 已结束 ' + endedCount + ' 张（历史只作记录）'
    : '共 ' + items.length + ' 张';
  host.appendChild(el('div', 'card-note', countText + ' · ' + (textOf(data.source) || '按创建时间倒序')));
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
