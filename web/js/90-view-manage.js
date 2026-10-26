/* 90-view-manage.js — 业务管理：卡、订阅（含逐笔核实结论）、记忆与适当性。 */
'use strict';

/* ------------------------------------------------------------- 业务管理 */
function bxRenderManage(data) {
  data = bxIsObj(data) ? data : {};

  const cardHost = $('mg-cards');
  const cards = bxArr(data.cards);
  setText($('mg-card-tag'), bxCount(cards.length, ' 张'));
  if (cardHost) {
    if (!cards.length) {
      bxEmpty(cardHost, '没有卡片', '后端没有返回任何卡。');
    } else {
      cardHost.replaceChildren();
      cardHost.appendChild(bxTable(
        ['卡种', '尾号', '所属账户', '状态', '单笔限额'],
        cards.map(function (card) {
          return [
            textOf(card.kind_label) || textOf(card.kind),
            textOf(card.tail),
            textOf(card.account_name),
            textOf(card.status_label) || textOf(card.status),
            textOf(card.per_tx_limit_yuan) + ' 元'
          ];
        }),
        [4]
      ));
      // 按钮话术必须带对象：只说「冻结这张卡」，用户点下去还得自己补尾号，
      // 多张卡时更是不知道动的是哪一张。
      cards.forEach(function (card) {
        const tail = textOf(card.tail);
        const who = tail ? '尾号' + tail + '的卡' : (textOf(card.kind_label) || '这张卡');
        const frozen = textOf(card.status) === 'frozen';
        const bar = el('div', 'anomaly-actions');
        bar.appendChild(el('span', 'tag' + (frozen ? ' warn' : ''), who));
        const actions = [];
        if (frozen) {
          actions.push(['解冻这张卡', '帮我解冻' + who]);
        } else {
          actions.push(['临时冻结', '帮我把' + who + '临时冻结一下']);
        }
        actions.push(['调限额', '我想调整' + who + '的限额']);
        actions.push(['挂失', who + '丢了，帮我挂失']);
        actions.forEach(function (pair) {
          const btn = el('button', 'btn btn-mini', pair[0]);
          btn.type = 'button';
          btn.title = pair[1];
          btn.addEventListener('click', function () { bxAsk(pair[1]); });
          bar.appendChild(btn);
        });
        cardHost.appendChild(bar);
      });
      cardHost.appendChild(el('div', 'card-note', '卡片操作都需要你在 AI 助手里确认；本页没有直接生效的按钮，避免绕过权限判定。'));
    }
  }

  const subHost = $('mg-subs');
  const subs = bxIsObj(data.subscriptions) ? data.subscriptions : {};
  const items = bxArr(subs.items);
  if (subHost) {
    if (!items.length) {
      bxEmpty(subHost, '没有订阅代扣记录', textOf(subs.boundary));
    } else {
      subHost.replaceChildren();
      const list = el('div', 'list');
      items.forEach(function (sub) {
        list.appendChild(bxRow(
          textOf(sub.merchant),
          textOf(sub.period_label) + ' · 下次扣费 ' + textOf(sub.next_charge_text) + ' · ' + textOf(sub.status_label),
          textOf(sub.amount_yuan) + ' 元'
        ));
        list.appendChild(el('div', 'card-note', textOf(sub.boundary)));
        if (textOf(sub.status) === 'active') {
          const bar = el('div', 'anomaly-actions');
          const btn = el('button', 'btn btn-mini', '停掉银行侧代扣');
          btn.type = 'button';
          btn.addEventListener('click', function () {
            bxAsk('帮我把「' + textOf(sub.merchant) + '」的银行侧自动扣款停掉');
          });
          bar.appendChild(btn);
          list.appendChild(bar);
        }
      });
      subHost.appendChild(list);
      subHost.appendChild(el('div', 'notice warn', textOf(subs.boundary) + ' 月合计 ' + bxMoney(subs, 'monthly_active') + '。'));
    }
  }
  bxRenderInferred(subs.inferred);
  bxRenderManageUpcoming(subs.upcoming);
  bxRenderMemory();

  const invest = bxIsObj(data.investments) ? data.investments : {};
  bxRenderSuitability($('mg-suit'), invest);
  const products = bxArr(invest.products);
  /* summary 是 {holding_count, product_count} 对象；以前被 textOf 直接拼进正文，
     页面上就露出一段 {"holding_count":2,...} 的 JSON。这里只说人话。 */
  const invSummary = bxIsObj(invest.summary)
    ? ('共 ' + bxCount(invest.summary.product_count, ' 只产品') + '，其中 ' +
       bxCount(invest.summary.holding_count, ' 只有持仓') + '。')
    : '';
  const productHost = $('mg-products');
  if (productHost) {
    if (!products.length) {
      bxEmpty(productHost, '没有产品数据', invSummary || '后端没有返回产品。');
    } else {
      productHost.replaceChildren();
      productHost.appendChild(bxTable(
        ['产品', '类别', '风险等级', '起购', '净值更新', '我的持仓'],
        products.map(function (product) {
          return [
            textOf(product.name),
            BX_PRODUCT_CATEGORY[textOf(product.category)] || textOf(product.category),
            'R' + textOf(product.risk_level),
            textOf(product.min_buy_yuan) + ' 元',
            textOf(product.nav_updated_text),
            textOf(product.holding_shares_text) + ' 份'
          ];
        }),
        [3, 5]
      ));
      // 申购/赎回也要指名产品，否则用户点完还得自己补一只基金的名字。
      products.forEach(function (product) {
        const name = textOf(product.name);
        if (!name) { return; }
        const bar = el('div', 'anomaly-actions');
        bar.appendChild(el('span', 'tag', 'R' + textOf(product.risk_level)));
        const buy = el('button', 'btn btn-mini', '买「' + name + '」');
        buy.type = 'button';
        buy.addEventListener('click', function () { bxAsk('我想买一点「' + name + '」'); });
        bar.appendChild(buy);
        const redeem = el('button', 'btn btn-mini', '赎回「' + name + '」');
        redeem.type = 'button';
        redeem.addEventListener('click', function () { bxAsk('我想赎回「' + name + '」'); });
        bar.appendChild(redeem);
        productHost.appendChild(bar);
      });
    }
  }
  const fitTag = $('mg-fit');
  if (fitTag) {
    const ok = invest.suitability_assessed === true;
    setText(fitTag, ok ? '适当性：已评估' : '适当性：未评估');
    fitTag.className = 'tag ' + (ok ? 'ok' : 'bad');
  }
  const fitNote = $('mg-fit-note');
  if (fitNote) {
    fitNote.textContent = (invest.suitability_note ? textOf(invest.suitability_note) + ' ' : '') +
      (invest.data_source ? '数据来源：' + textOf(invest.data_source) + '。' : '') +
      invSummary;
  }

  const capHost = $('mg-caps');
  const caps = bxIsObj(data.capabilities) ? data.capabilities : {};
  if (capHost) {
    capHost.replaceChildren();
    const keys = Object.keys(caps);
    if (!keys.length) {
      bxEmpty(capHost, '没有能力说明', '后端没有返回 capabilities。');
    } else {
      keys.forEach(function (key) {
        const cap = bxIsObj(caps[key]) ? caps[key] : {};
        const box = el('div', 'anomaly');
        const head = el('div', 'anomaly-head');
        head.appendChild(el('span', 'anomaly-title', BX_CAP_LABEL[key] || key));
        head.appendChild(el('span', 'tag ' + (cap.supported === true ? 'ok' : 'bad'), cap.supported === true ? '可用' : '不可用'));
        if (cap.requires_authorization) { head.appendChild(el('span', 'tag warn', '需要授权')); }
        box.appendChild(head);
        if (cap.entry) { box.appendChild(kvRow('入口', textOf(cap.entry))); }
        bxArr(cap.notes).forEach(function (note) {
          box.appendChild(el('div', 'card-note', '· ' + textOf(note)));
        });
        capHost.appendChild(box);
      });
    }
  }
  const note = $('mg-note');
  if (note) {
    note.textContent = '写操作统一走 AI 助手 + 权限引擎 + 确认流程，本页只做浏览与入口，不提供绕过策略的直连按钮。';
  }
}

/* 从流水推断出来的「疑似订阅」：只给依据和不确定性，不给「取消」按钮。 */
function bxRenderInferred(payload) {
  const host = $('mg-subs-inferred');
  if (!host) { return; }
  const data = bxIsObj(payload) ? payload : {};
  const items = bxArr(data.items);
  const settled = bxArr(data.settled).filter(function (row) { return bxIsObj(row); });
  host.replaceChildren();
  if (!items.length) {
    host.appendChild(el('div', 'card-sub', '疑似订阅（从流水推断，' + bxCount(0, ' 个') + '）'));
    host.appendChild(el('div', 'card-note', settled.length
      ? '没有待处理的疑似订阅：' + bxCount(settled.length, ' 条') + '你已经定过性，列在下面，随时可以收回来。'
      : (textOf(data.note) || '近 6 个月没有商户满足「至少 3 次、间隔稳定、金额稳定」的条件。')));
  } else {
    host.appendChild(el('div', 'card-sub', '疑似订阅（从流水推断，' + bxCount(data.count, ' 个') + '）'));
  }
  items.forEach(function (item) {
    const box = el('div', 'anomaly');
    const head = el('div', 'anomaly-head');
    head.appendChild(el('span', 'anomaly-title', textOf(item.merchant)));
    head.appendChild(el('span', 'tag ' + (item.confidence === 'high' ? 'ok' : 'warn'), '把握度：' + textOf(item.confidence_label)));
    head.appendChild(el('span', 'tag', textOf(item.period_label)));
    box.appendChild(head);
    box.appendChild(kvRow('金额', bxMoney(item, 'avg_amount') + '（平均，最近一次 ' + textOf(item.last_amount_yuan) + ' 元）'));
    box.appendChild(kvRow('次数', bxCount(item.occurrences, ' 次') + '，间隔 ' + bxCount(item.interval_days_min, ' 天') + '–' + bxCount(item.interval_days_max, ' 天')));
    box.appendChild(kvRow('时间范围', textOf(item.first_text) + ' → ' + textOf(item.last_text)));
    if (item.basis) { box.appendChild(el('div', 'card-note', '识别依据：' + textOf(item.basis))); }
    if (item.uncertainty) { box.appendChild(el('div', 'card-note', '不确定性：' + textOf(item.uncertainty))); }
    const txIds = bxArr(item.evidence).map(function (row) { return textOf(row.transaction_id); }).filter(Boolean);
    if (txIds.length) { box.appendChild(el('div', 'card-note', '依据流水：' + txIds.join('、'))); }
    /* 推断本身只读，但用户必须能把它关掉：两种定性都会让这条离开待办，
       定性过的会落到下面的「已定性的商户」里，随时可以收回来。 */
    const acts = bxArr(item.actions).filter(function (act) { return bxIsObj(act); });
    if (acts.length) {
      const bar = el('div', 'anomaly-actions');
      acts.forEach(function (act) {
        const btn = el('button', 'btn btn-mini' +
          (textOf(act.style) === 'ghost' ? '' : ' btn-green'), textOf(act.label) || '处理');
        btn.type = 'button';
        btn.addEventListener('click', function () { bxRunAction(act); });
        bar.appendChild(btn);
      });
      box.appendChild(bar);
    }
    host.appendChild(box);
  });
  if (settled.length) {
    host.appendChild(el('div', 'card-sub', '已定性的商户（' + bxCount(settled.length, ' 条') + '）'));
    host.appendChild(el('div', 'card-note', '这些已经不在待办里。点「重新当成疑似」会把结论撤掉，让流水重新推断。'));
    settled.forEach(function (row) {
      const line = el('div', 'anomaly');
      const head = el('div', 'anomaly-head');
      head.appendChild(el('span', 'anomaly-title', textOf(row.merchant)));
      head.appendChild(el('span', 'tag info', textOf(row.verdict_label)));
      if (textOf(row.updated_text)) { head.appendChild(el('span', 'tag', textOf(row.updated_text))); }
      line.appendChild(head);
      const acts = bxArr(row.actions).filter(function (act) { return bxIsObj(act); });
      if (acts.length) {
        const bar = el('div', 'anomaly-actions');
        acts.forEach(function (act) {
          const btn = el('button', 'btn btn-mini', textOf(act.label) || '撤回');
          btn.type = 'button';
          btn.addEventListener('click', function () { bxRunAction(act); });
          bar.appendChild(btn);
        });
        line.appendChild(bar);
      }
      host.appendChild(line);
    });
  }
  /* 停掉银行侧代扣的商户已经从待办里排除（后端 detect_recurring 过滤 cancelled 的 subs），
     但用户需要看到「我停过哪些、停的是什么」，所以单列一栏，不是又挂回待办。 */
  const stopped = bxArr(data.stopped).filter(function (row) { return bxIsObj(row); });
  if (stopped.length) {
    host.appendChild(el('div', 'card-sub', '已停止银行侧代扣（' + bxCount(data.stopped_count || stopped.length, ' 条') + '）'));
    host.appendChild(el('div', 'card-note', '这些是你停过的银行侧周期性代扣，已经不在待办里。'));
    stopped.forEach(function (row) {
      const line = el('div', 'anomaly');
      const head = el('div', 'anomaly-head');
      head.appendChild(el('span', 'anomaly-title', textOf(row.merchant)));
      head.appendChild(el('span', 'tag info', textOf(row.status_label) || '已停止'));
      if (textOf(row.period_label)) { head.appendChild(el('span', 'tag', textOf(row.period_label))); }
      if (textOf(row.amount_yuan)) { head.appendChild(el('span', 'tag', textOf(row.amount_yuan) + ' 元')); }
      line.appendChild(head);
      if (textOf(row.next_charge_text)) { line.appendChild(kvRow('原下次扣费', textOf(row.next_charge_text))); }
      if (textOf(row.boundary_text)) { line.appendChild(el('div', 'card-note', textOf(row.boundary_text))); }
      host.appendChild(line);
    });
  }
  host.appendChild(el('div', 'card-note', textOf(data.note) + ' ' + textOf(data.boundary)));
}

/* 长期记忆：助手在聊天里自己记下来的稳定偏好与事实，跨会话、跨刷新都还在。
   这里只做展示/手动补一条/忘掉一条；写删一律走后端 /api/memory，前端不自己合并。 */
function bxMemoryField(id, label, placeholder) {
  const field = el('label', 'pf-field');
  field.appendChild(el('span', 'field-label', label));
  const input = el('input', 'pf-in');
  input.id = id;
  input.type = 'text';
  input.placeholder = placeholder || '';
  field.appendChild(input);
  return field;
}

function bxRenderMemory() {
  const host = $('mg-memory');
  if (!host) { return; }
  host.replaceChildren(el('div', 'card-note', '正在读记忆…'));
  bxFetch('/api/memory').then(function (payload) {
    const data = bxIsObj(payload) ? payload : {};
    const facts = bxArr(data.facts).filter(function (row) { return bxIsObj(row); });
    const tag = $('mg-memory-tag');
    if (tag) { setText(tag, facts.length ? bxCount(facts.length, ' 条') : '—'); }
    host.replaceChildren();
    if (!facts.length) {
      host.appendChild(el('div', 'card-note',
        '还没有记忆。你在聊天里说过「我习惯…」「以后都…」这类稳定偏好，助手会记下来，下次直接按这个来。'));
    } else {
      facts.forEach(function (fact) {
        const box = el('div', 'anomaly');
        const head = el('div', 'anomaly-head');
        head.appendChild(el('span', 'anomaly-title', textOf(fact.key)));
        head.appendChild(el('span', 'tag info', textOf(fact.kind_label) || '事实'));
        if (textOf(fact.updated_text)) { head.appendChild(el('span', 'tag', textOf(fact.updated_text))); }
        box.appendChild(head);
        box.appendChild(kvRow('内容', textOf(fact.value)));
        if (textOf(fact.source)) { box.appendChild(el('div', 'card-note', '来源：' + textOf(fact.source))); }
        const bar = el('div', 'anomaly-actions');
        const del = el('button', 'btn btn-mini', '忘掉这条');
        del.type = 'button';
        del.addEventListener('click', function () { bxForgetMemory(textOf(fact.key), del); });
        bar.appendChild(del);
        box.appendChild(bar);
        host.appendChild(box);
      });
    }

    const form = el('div', 'pf-form');
    form.appendChild(bxMemoryField('mg-memory-key', '记什么（键，例如「常去商户」）', '常去商户'));
    form.appendChild(bxMemoryField('mg-memory-value', '内容（例如「乐刻健身」）', '乐刻健身'));
    const kindField = el('label', 'pf-field');
    kindField.appendChild(el('span', 'field-label', '类型'));
    const kindSel = el('select', 'pf-in');
    kindSel.id = 'mg-memory-kind';
    [['fact', '事实'], ['preference', '偏好'], ['profile', '画像']].forEach(function (pair) {
      const opt = el('option', '', pair[1]);
      opt.value = pair[0];
      kindSel.appendChild(opt);
    });
    kindField.appendChild(kindSel);
    form.appendChild(kindField);
    const bar = el('div', 'pf-actions');
    const add = el('button', 'btn btn-green btn-mini', '记住这条');
    add.type = 'button';
    add.addEventListener('click', function () { bxSaveMemory(add); });
    bar.appendChild(add);
    form.appendChild(bar);
    host.appendChild(form);
  }).catch(function (err) {
    bxError(host, err);
  });
}

function bxSaveMemory(btn) {
  const keyEl = $('mg-memory-key');
  const valEl = $('mg-memory-value');
  const kindEl = $('mg-memory-kind');
  const key = keyEl ? textOf(keyEl.value).trim() : '';
  const value = valEl ? textOf(valEl.value).trim() : '';
  if (!key || !value) {
    appendBubble('sys', '手动记一条记忆要填「记什么」和「内容」两项。', 'memory');
    return;
  }
  if (btn) { btn.disabled = true; }
  bxSend('/api/memory', 'POST', { key: key, value: value, kind: kindEl ? textOf(kindEl.value) : 'fact' })
    .then(function (res) {
      const fact = bxIsObj(res) && bxIsObj(res.fact) ? res.fact : null;
      appendBubble('sys', '记住了：' + key + ' —— ' + (fact ? textOf(fact.value) : value) + '。以后我会按这个当默认值。', 'memory');
      if (keyEl) { keyEl.value = ''; }
      if (valEl) { valEl.value = ''; }
      bxRenderMemory();
    })
    .catch(function (err) {
      if (btn) { btn.disabled = false; }
      appendBubble('sys', '这条没记上：' + errorText(err));
    });
}

function bxForgetMemory(key, btn) {
  if (!key) { return; }
  if (btn) { btn.disabled = true; }
  bxSend('/api/memory/' + encodeURIComponent(key), 'DELETE')
    .then(function (res) {
      const removed = bxIsObj(res) ? res.removed !== false : true;
      appendBubble('sys', removed ? ('已经忘掉「' + key + '」这条记忆。') : ('本来就没有「' + key + '」这条记忆。'), 'memory');
      bxRenderMemory();
    })
    .catch(function (err) {
      if (btn) { btn.disabled = false; }
      appendBubble('sys', '这条记忆没删掉：' + errorText(err));
    });
}

function bxRenderManageUpcoming(payload) {
  const host = $('mg-upcoming');
  if (!host) { return; }
  const data = bxIsObj(payload) ? payload : {};
  const items = bxArr(data.items);
  if (!items.length) {
    if (data.note) { host.replaceChildren(el('div', 'card-note', textOf(data.note))); }
    return;
  }
  host.replaceChildren(el('div', 'card-sub', '近 ' + bxCount(data.within_days, ' 天') + '可能扣费'));
  const list = el('div', 'list');
  items.forEach(function (item) {
    list.appendChild(bxRow(
      textOf(item.merchant),
      textOf(item.next_charge_text) + ' · 还有 ' + bxCount(item.days_left, ' 天') + ' · ' + textOf(item.period_label),
      textOf(item.amount_yuan) + ' 元'
    ));
  });
  host.appendChild(list);
  host.appendChild(el('div', 'card-note', textOf(data.note) + ' ' + textOf(data.action)));
}

/* 风险测评：5 道题，提交给后端算分；分数和等级都由后端返回，前端不自己算。 */
function bxRenderSuitability(host, invest) {
  if (!host) { return; }
  const fit = bxIsObj(invest.suitability) ? invest.suitability : {};
  const questions = bxArr(fit.questions);
  host.replaceChildren();
  const profile = bxIsObj(fit.profile) ? fit.profile : null;
  if (profile) {
    const box = el('div', 'anomaly');
    const head = el('div', 'anomaly-head');
    head.appendChild(el('span', 'anomaly-title', '你的风险测评：' + textOf(profile.level) + '（' + textOf(profile.level_label) + '）'));
    head.appendChild(el('span', 'tag ok', '得分 ' + bxCount(profile.score)));
    box.appendChild(head);
    box.appendChild(el('div', 'card-note',
      '最高可买 R' + bxCount(profile.capacity) + ' 风险的产品。' + (fit.disclaimer ? ' ' + textOf(fit.disclaimer) : '')));
    host.appendChild(box);
  }
  if (!questions.length) {
    host.appendChild(el('div', 'card-note', '后端没有下发测评题目，暂时无法测评。'));
    return;
  }
  const details = el('details', 'caps');
  const summary = el('summary', 'caps-title',
    profile ? '重新做一次风险测评（5 道题）' : '做风险测评（5 道题，做完才能买基金）');
  details.appendChild(summary);
  const form = el('div', 'pf-form');
  const picks = {};
  questions.forEach(function (question) {
    const field = el('label', 'pf-field pf-field-wide');
    field.appendChild(el('span', 'field-label', textOf(question.text)));
    const select = el('select', 'pf-in');
    select.id = 'suit-' + textOf(question.id);
    const placeholder = el('option', '', '请选择');
    placeholder.value = '';
    select.appendChild(placeholder);
    bxArr(question.options).forEach(function (option) {
      const opt = el('option', '', textOf(option.label));
      opt.value = textOf(option.value);
      select.appendChild(opt);
    });
    select.addEventListener('change', function () {
      picks[textOf(question.id)] = select.value;
    });
    picks[textOf(question.id)] = '';
    field.appendChild(select);
    form.appendChild(field);
  });
  const bar = el('div', 'pf-actions');
  const submit = el('button', 'btn btn-green btn-mini', '提交测评');
  submit.type = 'button';
  submit.addEventListener('click', function () {
    const answers = {};
    const missing = [];
    questions.forEach(function (question) {
      const key = textOf(question.id);
      if (!picks[key]) { missing.push(key); }
      answers[key] = picks[key];
    });
    if (missing.length) {
      appendBubble('sys', '还有 ' + missing.length + ' 题没选，5 道题都选了才能生成测评结果。');
      return;
    }
    submit.disabled = true;
    bxSend('/api/investment/suitability', 'POST', { answers: answers })
      .then(function (json) {
        const saved = bxIsObj(json && json.profile) ? json.profile : {};
        appendBubble('sys', '测评已保存：' + textOf(saved.level) + '（' + textOf(saved.level_label) + '），最高可买 R' + bxCount(saved.capacity) + ' 的产品。这是演示用简化问卷，不是监管口径的风险评估。');
        bxRefresh();
      })
      .catch(function (err) {
        appendBubble('sys', '提交测评失败：' + errorText(err));
      })
      .finally(function () { submit.disabled = false; });
  });
  bar.appendChild(submit);
  form.appendChild(bar);
  details.appendChild(form);
  host.appendChild(details);
}
