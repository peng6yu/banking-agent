/* 80-view-overview.js — 财务总览与账单分析两个视图。 */
'use strict';

/* ------------------------------------------------------------- 财务总览 */
/* 首页只回答三件事：我现在有多少钱、有没有事等我决定、正在推进什么。
   具体数字的来龙去脉在账单页与计划页——以前首页把这两页的动作各抄了一份，
   同一个订阅能从三个地方点出四种按钮，用户不知道该信哪个。 */
function bxInboxBtn(label, cls, onClick) {
  const btn = el('button', 'btn btn-mini' + (cls ? ' ' + cls : ''), label);
  btn.type = 'button';
  btn.addEventListener('click', function () { onClick(btn); });
  return btn;
}

/* 待你确认的授权：能在页面批就地批，需要强验证的就地验证，其余回对话。 */
function bxInboxPending(list, item) {
  const expired = textOf(item.state_label).indexOf('过期') !== -1 || item.expired === true;
  const cid = textOf(item.confirmation_id);
  const sid = textOf(item.session_id);
  const factors = bxArr(item.required_factors);
  const facts = [textOf(item.state_label)];
  if (textOf(item.level)) { facts.push(levelText(textOf(item.level))); }
  if (factors.length) {
    facts.push('需要验证：' + factors.map(function (key) {
      return BX_FACTOR_TEXT[textOf(key)] || textOf(key);
    }).join('/'));
  }
  if (item.stale) { facts.push('会话已失效'); }
  list.appendChild(bxRow(
    (textOf(item.payee_name) || '有一个操作') + ' · ' + (textOf(item.amount_yuan) || BX_NA) + ' 元',
    textOf(item.created_text) + ' 发起 · ' + facts.join(' · '),
    '等你确认'
  ));
  const bar = el('div', 'anomaly-actions');
  if (item.can_confirm_here === true && !expired && cid) {
    bar.appendChild(bxInboxBtn('批准执行', 'btn-green', function (btn) {
      btn.disabled = true;
      Promise.resolve(confirmAction(cid, true, null, null, sid)).then(function () { bxRefresh(); },
        function () { btn.disabled = false; });
    }));
    bar.appendChild(bxInboxBtn('拒绝', 'btn-red', function (btn) {
      btn.disabled = true;
      Promise.resolve(confirmAction(cid, false, null, null, sid)).then(function () { bxRefresh(); },
        function () { btn.disabled = false; });
    }));
    list.appendChild(bar);
    return;
  }
  if (expired) {
    bar.appendChild(el('span', 'card-note', '这条已经过期，批准入口失效，需要重新发起一笔。'));
    list.appendChild(bar);
    return;
  }
  if (item.can_verify_here === true && factors.length > 0 && cid) {
    const box = el('div', 'factor-inline');
    bar.appendChild(bxInboxBtn('就地验证并批准', 'btn-green', function (btn) {
      btn.disabled = true;
      bxInlineFactor(box, cid, sid, function () { bxRefresh(); });
    }));
    bar.appendChild(el('span', 'card-note', '验证码由服务端签发、一次性、5 分钟有效；验过才批准，服务端仍会重新裁决。'));
    list.appendChild(bar);
    list.appendChild(box);
    return;
  }
  bar.appendChild(bxInboxBtn('去 AI 助手继续', '', function () {
    bxAsk('我有一个待确认的操作，继续处理');
  }));
  list.appendChild(bar);
}

/* 待核实的异常：首页只给入口，规则、证据、涉及流水都在账单页。 */
function bxInboxAnomaly(list, item) {
  const rule = bxIsObj(item.rule) ? item.rule : {};
  const confText = textOf(rule.confidence_label) || textOf(rule.confidence);
  const tail = bxArr([textOf(rule.id), confText ? ('把握 ' + confText) : '']).filter(Boolean).join(' · ');
  list.appendChild(bxRow(
    textOf(item.title) || '有一条异常待核实',
    textOf(item.detail) + (textOf(item.amount_note) ? '（' + textOf(item.amount_note) + '）' : '') +
      (tail ? '（' + tail + '）' : ''),
    (textOf(item.amount_yuan) || '') + ' 元待核实'
  ));
  const bar = el('div', 'anomaly-actions');
  bar.appendChild(bxInboxBtn('去账单分析看依据', '', function () {
    bxSetView('bill');
    bxFlashCard('bill-anomalies');
  }));
  /* 核实完就该能在这里收尾，不用为了点一下再跳去账单页。
     两档结论都摆出来：说「不是我刷的」的人，下一步是冻结卡与争议，不是「已核实」。 */
  const verdicts = bxArr(item.actions).filter(function (a) {
    return textOf(a.kind) === 'ack_anomaly';
  });
  verdicts.forEach(function (act) {
    const notMine = textOf(act.verdict) === 'not_mine';
    bar.appendChild(bxInboxBtn(textOf(act.label) || '已核实', notMine ? '' : 'green', function (btn) {
      bxArr(bar.querySelectorAll('button')).forEach(function (other) { other.disabled = true; });
      bxAckAnomaly(act).then(function (ok) {
        if (!ok) {
          bxArr(bar.querySelectorAll('button')).forEach(function (other) { other.disabled = false; });
        }
      });
    }));
  });
  list.appendChild(bar);
}

/* 待人工接管：接管单从此只在这一个地方操作，安全页只留执行记录与审计。 */
function bxInboxHandoff(list, block, active) {
  const needsUser = active.some(function (item) { return item.needs_user === true; });
  list.appendChild(bxRow(
    '有 ' + active.length + ' 张单等你定',
    (needsUser ? '有的缺你本人决定（例如选哪一位收款人），操作员不能替选。' : '') +
      '领取、要求补充信息、拒绝、取消都在这里，全部写明操作员并进审计。',
    '待接管'
  ));
  const bar = el('div', 'anomaly-actions');
  const box = el('div', 'list');
  box.hidden = true;
  const btn = bxInboxBtn('展开处理这几张单', '', function () {
    box.hidden = !box.hidden;
    btn.textContent = box.hidden ? '展开处理这几张单' : '收起这几张单';
    if (!box.childNodes.length) { bxRenderHandoffs(box, block, null); }
  });
  bar.appendChild(btn);
  list.appendChild(bar);
  list.appendChild(box);
}

/* 计划提醒：只给入口，采纳/处理在「计划与提醒」页。 */
function bxInboxAlert(list, alert) {
  const goal = textOf(alert.goal_name);
  list.appendChild(bxRow(
    textOf(alert.reason_text) || '有一条提醒需要你看一眼',
    (goal ? '目标：' + goal + ' · ' : '') + '点进去可以看依据、改计划或忽略这条。',
    '待处理'
  ));
  const bar = el('div', 'anomaly-actions');
  bar.appendChild(bxInboxBtn('去计划与提醒', '', function () {
    bxSetView('plans');
    bxFlashCard('plan-alerts');
  }));
  list.appendChild(bar);
}

function bxRenderInbox(data) {
  const host = $('ov-inbox');
  if (!host) { return; }
  host.replaceChildren();
  const list = el('div', 'list');
  let count = 0;
  const pending = bxIsObj(data.pending_authorizations) ? data.pending_authorizations : {};
  bxArr(pending.items).slice(0, 3).forEach(function (item) { count += 1; bxInboxPending(list, item); });
  const anomalyBlock = bxIsObj(data.anomalies) ? data.anomalies : {};
  bxArr(anomalyBlock.items).slice(0, 3).forEach(function (item) { count += 1; bxInboxAnomaly(list, item); });
  const handoffs = bxIsObj(data.handoffs) ? data.handoffs : {};
  const active = bxArr(handoffs.items).filter(function (item) {
    return ['pending', 'claimed', 'need_info'].indexOf(textOf(item.status)) >= 0;
  });
  if (active.length) { count += 1; bxInboxHandoff(list, handoffs, active); }
  const plans = bxIsObj(data.plans_and_reminders) ? data.plans_and_reminders : {};
  const alerts = bxArr((bxIsObj(plans.alerts) ? plans.alerts : {}).items);
  alerts.slice(0, 2).forEach(function (alert) { count += 1; bxInboxAlert(list, alert); });

  /* 助手提醒走独立端点（要写动作才生成），条数异步回来，先占一个位置。 */
  const msgWrap = el('div', '');
  msgWrap.hidden = true;
  list.appendChild(msgWrap);
  const emptyNote = el('div', 'card-note', '现在没有需要你过目的事。有需要你决定的才会出现在这里。');
  emptyNote.hidden = count > 0;
  list.appendChild(emptyNote);
  host.appendChild(list);

  const tag = $('ov-inbox-tag');
  const paintTag = function () {
    if (!tag) { return; }
    tag.textContent = count ? (count + ' 件事') : '没有待办';
    tag.className = 'tag ' + (count ? 'warn' : 'ok');
  };
  paintTag();

  if (typeof bxMessagesSummary === 'function') {
    bxMessagesSummary(function (counts) {
      const pendingNew = Number((counts || {}).new || 0);
      if (!pendingNew) { return; }
      count += 1;
      emptyNote.hidden = true;
      msgWrap.hidden = false;
      msgWrap.appendChild(bxRow('助手有话要跟你说 · ' + pendingNew + ' 条',
        '每条都写明依据的那个数字；看完整理由、决定采纳还是别再提醒，都在「计划与提醒」。', '待你看'));
      const bar = el('div', 'anomaly-actions');
      bar.appendChild(bxInboxBtn('去计划与提醒', '', function () {
        bxSetView('plans');
        bxFlashCard('plan-messages');
      }));
      msgWrap.appendChild(bar);
      paintTag();
    });
  }
}

/* 正在进行：只给进度与摘要，按钮在所属栏目里。 */
function bxRenderProgress(data) {
  const goalHost = $('ov-goals');
  const savings = bxIsObj(data.savings_goals) ? data.savings_goals : {};
  const goals = bxArr(savings.items);
  if (goalHost) {
    goalHost.replaceChildren();
    goalHost.appendChild(el('div', 'field-label', '储蓄目标'));
    if (!goals.length) {
      goalHost.appendChild(el('div', 'card-note', '还没有储蓄目标，可以让 AI 助手帮你定一个。'));
    } else {
      const list = el('div', 'list');
      goals.slice(0, 3).forEach(function (goal) {
        const plan = bxIsObj(goal.plan) ? goal.plan : null;
        const pct = plan ? bxNum(plan.progress_pct) : null;
        list.appendChild(bxRow(
          textOf(goal.name),
          textOf(goal.saved_yuan) + ' / ' + textOf(goal.target_yuan) + ' 元 · 目标日 ' + textOf(goal.target_date),
          BX_GOAL_STATUS[textOf(goal.status)] || textOf(goal.status)
        ));
        if (pct !== null) { list.appendChild(bxBar(pct, pct >= 100 ? 'ok' : '')); }
        if (plan) {
          const unit = BX_PERIOD_UNIT[textOf(plan.period_unit)] || textOf(plan.period_unit);
          list.appendChild(el('div', 'card-note', '按后端计划：' + unit + '需要 ' + bxMoney(plan, 'required_per_period') +
            (plan.feasibility ? '，' + (BX_FEASIBILITY[textOf(plan.feasibility)] || textOf(plan.feasibility)) : '')));
        }
      });
      goalHost.appendChild(list);
    }
  }

  const planHost = $('ov-plans');
  const plans = bxIsObj(data.plans_and_reminders) ? data.plans_and_reminders : {};
  if (planHost) {
    planHost.replaceChildren();
    planHost.appendChild(el('div', 'field-label', '预约与已保存的计划'));
    const scheduled = bxIsObj(plans.scheduled) ? plans.scheduled : {};
    const tasks = bxArr(scheduled.items);
    if (!tasks.length) {
      planHost.appendChild(el('div', 'card-note', textOf(scheduled.empty_reason) || '没有已安排的预约任务。'));
    } else {
      const list = el('div', 'list');
      tasks.slice(0, 3).forEach(function (task) {
        list.appendChild(bxRow(
          task.payee_name ? ('转给 ' + textOf(task.payee_name)) : '预约任务',
          textOf(task.due_text) + ' · ' + textOf(task.state_label),
          textOf(task.amount_yuan) + ' 元'
        ));
      });
      planHost.appendChild(list);
    }
  }
}

function bxRenderOverview(data) {
  data = bxIsObj(data) ? data : {};
  const month = bxIsObj(data.this_month) ? data.this_month : {};
  const accounts = bxArr(data.accounts);
  const main = bxIsObj(data.account) ? data.account : (accounts.length ? accounts[0] : null);

  const balHost = $('ov-balance');
  if (balHost) {
    balHost.replaceChildren();
    if (main) {
      balHost.appendChild(bxStatCard(
        '余额 · ' + textOf(main.name) + ' 尾号 ' + textOf(main.tail),
        textOf(main.balance_yuan) + ' 元',
        '接口返回的当前余额，不代表待执行操作后的余额。'
      ));
    } else {
      balHost.appendChild(bxStatCard('账户余额', BX_NA, '后端没有返回账户'));
    }
    /* 支出只算钱真的离开净资产的：转给自己另一张卡、买理财是钱换个地方，
       账单页把它们单列成「内部搬运」，这里也不重复计算。 */
    const cardLabel = month.account_name
      ? textOf(month.account_name) + ' 尾号 ' + textOf(month.account_tail) + ' · '
      : '';
    balHost.appendChild(bxStatCard('本月支出', month.spend_out_yuan ? textOf(month.spend_out_yuan) + ' 元' : BX_NA,
      cardLabel + bxCount(month.out_count, ' 笔') + '，不含账户互转与理财'));
    balHost.appendChild(bxStatCard('本月结余', month.net_yuan !== undefined && month.net_yuan !== null ? textOf(month.net_yuan) + ' 元' : BX_NA,
      '本月收入 ' + bxMoney(month, 'spend_in') + ' · 收入 − 支出，和账单页同一个口径'));
  }
  setText($('ov-month-tag'), data.month_label || '—');

  bxRenderInbox(data);
  bxRenderProgress(data);

  const actionHost = $('ov-actions');
  const actions = bxArr(data.quick_actions);
  if (actionHost) {
    actionHost.replaceChildren();
    actions.forEach(function (action) {
      const chip = el('button', 'cap-chip action', textOf(action.label));
      chip.type = 'button';
      chip.addEventListener('click', function () { bxAsk(textOf(action.prompt)); });
      actionHost.appendChild(chip);
    });
    if (!actions.length) { actionHost.appendChild(el('span', 'cap-chip', BX_NA)); }
  }

  const note = $('ov-note');
  if (note) {
    const parts = ['快捷入口只负责把话填好，真正的执行仍然要走权限判定与确认。'];
    bxArr(data.unavailable).forEach(function (item) {
      parts.push('暂不可用：' + textOf(item.section) + '（' + textOf(item.reason) + '）');
    });
    if (data.generated_text) { parts.push('数据生成时间：' + textOf(data.generated_text)); }
    note.textContent = parts.join(' ');
  }
}

/* ------------------------------------------------------------- 账单分析 */
function bxRenderBill(data) {
  data = bxIsObj(data) ? data : {};
  const range = bxIsObj(data.range) ? data.range : {};
  const accounts = bxArr(data.accounts);
  const scoped = bxIsObj(data.account) ? data.account : {};

  /* 账单是「某一张卡」的视图：储蓄卡看花销，信用卡看刷卡与还款、还剩多少额度。
     不把当前卡摆出来，用户切到信用卡后会觉得数字凭空变了。 */
  const chipHost = $('bill-account-chips');
  if (chipHost) {
    chipHost.replaceChildren();
    accounts.forEach(function (acct) {
      const id = textOf(acct.account_id);
      const active = id === textOf(scoped.account_id);
      const btn = el('button', 'btn btn-mini' + (active ? ' btn-green' : ''),
        '尾号 ' + textOf(acct.tail) + ' · ' + textOf(acct.name) + (textOf(acct.type) === 'credit' ? '（信用卡）' : ''));
      btn.type = 'button';
      btn.setAttribute('aria-pressed', active ? 'true' : 'false');
      btn.addEventListener('click', function () {
        /* 显式写死选中的卡，而不是「再点一下回到默认」：切走再切回来时，
           用户期望看到的是同一张卡，不是悄悄换了默认。 */
        bxBillAccount = id;
        bxLoad('bill', true);
      });
      chipHost.appendChild(btn);
    });
  }
  setText($('bill-scope-note'), textOf(data.scope_note));

  /* 手动补录的「记到哪张卡」用同一份账户列表，两处不会出现一张卡有、一张没有。 */
  if (typeof bxFillManualAccounts === 'function') { bxFillManualAccounts(accounts, scoped); }

  const host = $('bill-summary');
  if (host) {
    host.replaceChildren();
    /* 全站只有一个「支出」定义：钱真的离开净资产。账户互转与理财是钱换个地方，
       单独列在下面的「内部搬运」里；把这两笔混进支出再算结余，就会和总览对不上。 */
    host.appendChild(bxStatCard('支出合计', bxMoney(data, 'spend_out'), textOf(range.label) + ' · ' + bxCount(data.out_count, ' 笔，不含账户互转与理财')));
    host.appendChild(bxStatCard('收入合计', bxMoney(data, 'spend_in'), bxCount(data.in_count, ' 笔')));
    const internal = bxIsObj(data.internal) ? data.internal : {};
    const internalItems = bxArr(internal.items);
    const netAll = bxMoney(data, 'net_all');
    /* 没有内部搬运时不要写「把内部搬运也算进去是 X」——这句话在这种卡上等于把同一个数说两遍，
       还会让人以为差在哪。 */
    const netHint = netAll === BX_NA || !internalItems.length
      ? '收入 − 支出，和总览同一个口径'
      : '收入 − 支出，和总览同一个口径。把内部搬运也算进去是 ' + netAll + '（明细见下方「内部搬运」）';
    host.appendChild(bxStatCard('净结余', bxMoney(data, 'net'), netHint));

    if (internalItems.length) {
      const inner = el('div', 'card-note');
      inner.textContent = '内部搬运：' + internalItems.map(function (item) {
        return textOf(item.category) + ' ' + textOf(item.amount_yuan) + ' 元';
      }).join('、') + '（共 ' + bxCount(internal.count, ' 笔') + '）。' + textOf(internal.note);
      host.appendChild(inner);
    }
  }

  const catHost = $('bill-cats');
  const cats = bxArr(data.by_category);
  if (catHost) {
    if (!cats.length) {
      bxEmpty(catHost, '这个区间没有支出', '换个区间试试，或者确实没有花钱。');
    } else {
      catHost.replaceChildren();
      const list = el('div', 'list');
      cats.forEach(function (cat) {
        list.appendChild(bxRow(textOf(cat.category), bxCount(cat.count, ' 笔') + ' · ' + bxCount(cat.pct, '%'), bxMoney(cat, 'amount')));
        list.appendChild(bxBar(cat.pct));
      });
      catHost.appendChild(list);
    }
  }

  const trendHost = $('bill-trend');
  const trend = bxArr(data.trend);
  if (trendHost) {
    if (!trend.length) {
      bxEmpty(trendHost, '没有趋势数据', '后端没有返回月份序列。');
    } else {
      trendHost.replaceChildren();
      trendHost.appendChild(bxTable(
        ['月份', '收入', '支出', '结余（不含账户互转与理财）', '收支笔数'],
        trend.map(function (row) {
          return [
            textOf(row.label) + (row.partial ? '（未完）' : ''),
            textOf(row.income_yuan),
            textOf(row.expense_yuan),
            textOf(row.net_yuan),
            textOf(row.transaction_count)
          ];
        }),
        [1, 2, 3, 4]
      ));
    }
  }

  const anomalyHost = $('bill-anomalies');
  const block = bxIsObj(data.anomalies) ? data.anomalies : {};
  const items = bxArr(block.items);
  if (anomalyHost) {
    anomalyHost.replaceChildren();
    if (!items.length) {
      bxEmpty(anomalyHost, textOf(block.state) || '没有命中任何规则', textOf(block.disclaimer));
    } else {
      items.forEach(function (item) { anomalyHost.appendChild(bxAnomalyCard(item, true)); });
      if (block.disclaimer) { anomalyHost.appendChild(el('div', 'notice info', textOf(block.disclaimer))); }
    }
    const rules = bxArr(block.rules);
    if (rules.length) {
      anomalyHost.appendChild(el('div', 'field-label', '判定规则（全部是启发式线索）'));
      anomalyHost.appendChild(bxTable(
        ['规则', '内容', '置信度'],
        rules.map(function (rule) {
          return [textOf(rule.rule_id), textOf(rule.rule_text), textOf(rule.confidence_label) || textOf(rule.confidence)];
        })
      ));
    }
  }

  const note = $('bill-note');
  if (note) {
    /* 异常证据流水不再单开一张卡：命中规则的笔在下面「流水明细」里标红、点开就是规则，
       这里只交代口径，不重复列同一批流水。 */
    note.textContent = '规则命中只说明值得核实，不是欺诈认定；命中规则的流水在下面「流水明细」里标红，点开能看是哪条规则、凭什么命中。' +
      (data.summary ? ' ' + textOf(data.summary) : '');
  }
  /* 核实过的流水不消失：给一个撤销入口，别让「点错了」变成永久免打扰。 */
  if (typeof bxRenderAcked === 'function') { bxRenderAcked(data.acked, scoped); }
  /* 流水明细跟当前卡与时间段走：账单重渲染（切卡、换月）时一起重新取。 */
  if (typeof bxLoadLedger === 'function') { bxLoadLedger(true); }
}
