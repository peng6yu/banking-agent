/* 80-view-overview.js — 财务总览与账单分析两个视图。 */
'use strict';

/* ------------------------------------------------------------- 财务总览 */
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
    balHost.appendChild(bxStatCard('本月支出', month.total_out_yuan ? textOf(month.total_out_yuan) + ' 元' : BX_NA, bxCount(month.out_count, ' 笔')));
    balHost.appendChild(bxStatCard('本月收入', month.total_in_yuan ? textOf(month.total_in_yuan) + ' 元' : BX_NA, bxCount(month.in_count, ' 笔')));
  }
  setText($('ov-month-tag'), data.month_label || '—');

  /* 助手提醒走独立端点（要写动作才生成），不并进总览那份只读快照。 */
  if (typeof bxLoadMessages === 'function') { bxLoadMessages(); }

  const flowHost = $('ov-flow');
  if (flowHost) {
    flowHost.replaceChildren();
    const list = el('div', 'list');
    const window = bxIsObj(month.window) ? month.window : {};
    list.appendChild(bxRow('收入', textOf(window.label) || textOf(month.label), bxMoney(month, 'total_in')));
    list.appendChild(bxRow('支出', bxCount(month.out_count, ' 笔'), bxMoney(month, 'total_out')));
    const trend = bxArr(data.trend);
    const last = trend.length ? trend[trend.length - 1] : null;
    if (bxIsObj(last)) {
      /* 趋势口径不含转账与理财（账户内部搬运），和上面两行的"全部支出"不是一个口径，
         所以标签里必须写明，否则同一个页面会出现两个对不上的结余。 */
      list.appendChild(bxRow(
        '结余·' + textOf(last.label) + '（不含转账/理财）',
        last.partial ? '这个月还没过完' : '完整月',
        textOf(last.net_yuan) + ' 元'
      ));
    }
    flowHost.appendChild(list);
    if (month.summary) { flowHost.appendChild(el('div', 'card-note', textOf(month.summary))); }
  }

  const catHost = $('ov-cats');
  const cats = bxArr(month.by_category);
  if (catHost) {
    if (!cats.length) {
      bxEmpty(catHost, '本期没有分类支出', '后端没有返回任何支出流水。');
    } else {
      catHost.replaceChildren();
      const list = el('div', 'list');
      cats.slice(0, 8).forEach(function (cat) {
        list.appendChild(bxRow(
          textOf(cat.category),
          bxCount(cat.count, ' 笔') + ' · 占比 ' + bxCount(cat.pct, '%'),
          bxMoney(cat, 'amount')
        ));
        list.appendChild(bxBar(cat.pct));
      });
      catHost.appendChild(list);
    }
  }

  const anomalyHost = $('ov-anomalies');
  const anomalyBlock = bxIsObj(data.anomalies) ? data.anomalies : {};
  const anomalyItems = bxArr(anomalyBlock.items);
  if (anomalyHost) {
    if (!anomalyItems.length) {
      bxEmpty(anomalyHost, textOf(anomalyBlock.state) || '本期没有命中任何规则', textOf(anomalyBlock.disclaimer));
    } else {
      anomalyHost.replaceChildren();
      anomalyItems.slice(0, 3).forEach(function (item) { anomalyHost.appendChild(bxAnomalyCard(item, false)); });
      if (anomalyBlock.more_count) {
        anomalyHost.appendChild(el('div', 'card-note', '还有 ' + textOf(anomalyBlock.more_count) + ' 条在「账单分析」里。'));
      }
      if (anomalyBlock.disclaimer) { anomalyHost.appendChild(el('div', 'card-note', textOf(anomalyBlock.disclaimer))); }
      const link = el('button', 'btn btn-mini', '去账单分析看依据');
      link.type = 'button';
      link.addEventListener('click', function () { bxSetView('bill'); });
      anomalyHost.appendChild(link);
    }
  }

  const pendingHost = $('ov-pending');
  const pending = bxIsObj(data.pending_authorizations) ? data.pending_authorizations : {};
  const pendingItems = bxArr(pending.items);
  if (pendingHost) {
    if (!pendingItems.length) {
      bxEmpty(pendingHost, '没有待处理的授权', textOf(pending.empty_reason) || textOf(pending.source));
    } else {
      pendingHost.replaceChildren();
      const list = el('div', 'list');
      pendingItems.forEach(function (item) {
        const expired = textOf(item.state_label).indexOf('过期') !== -1 || item.expired === true;
        const cid = textOf(item.confirmation_id);
        const ownerSid = textOf(item.session_id);
        const level = textOf(item.level);
        const factors = bxArr(item.required_factors);
        const canHere = item.can_confirm_here === true && !expired;
        const facts = [textOf(item.state_label)];
        if (level) { facts.push(levelText(level)); }
        if (factors.length) {
          facts.push('需要验证：' + factors.map(function (key) {
            return BX_FACTOR_TEXT[textOf(key)] || textOf(key);
          }).join('/'));
        }
        if (item.stale) { facts.push('会话已失效'); }
        list.appendChild(bxRow(
          (textOf(item.payee_name) || '操作') + ' · ' + (textOf(item.amount_yuan) || BX_NA) + ' 元',
          textOf(item.created_text) + ' 发起 · ' + facts.join(' · '),
          ''
        ));
        const bar = el('div', 'anomaly-actions');
        if (canHere && cid) {
          /* 只有后端明确说「这一步在页面就能批」才放批准按钮。
             需要人脸/短信验证、或者上一轮已经结束的，放上去就是点了会失败的假入口。 */
          const ok = el('button', 'btn btn-mini btn-green', '批准执行');
          ok.type = 'button';
          ok.title = '批准这一次授权；批准后仍由服务端重新裁决再执行，全程留痕。';
          ok.addEventListener('click', function () {
            ok.disabled = true;
            Promise.resolve(confirmAction(cid, true, null, null, ownerSid)).then(function () {
              bxRefresh();
            }, function () { ok.disabled = false; });
          });
          const no = el('button', 'btn btn-mini btn-red', '拒绝');
          no.type = 'button';
          no.addEventListener('click', function () {
            no.disabled = true;
            Promise.resolve(confirmAction(cid, false, null, null, ownerSid)).then(function () {
              bxRefresh();
            }, function () { no.disabled = false; });
          });
          bar.appendChild(ok);
          bar.appendChild(no);
        } else {
          /* can_verify_here：后端说这一条还能验证、并且确实需要人脸/短信这类因子。
             那就把验证码输入框摆在这里，验完直接批准——以前只给一句
             「去完成验证」，点了发一句没有指代的话，等于没有入口。 */
          const canVerify = item.can_verify_here === true && !expired && factors.length > 0;
          const btn = el('button', 'btn btn-mini' + (canVerify ? ' btn-green' : ''),
            expired ? '已过期，需要重新发起' : (canVerify ? '就地验证并批准' : (factors.length ? '去完成验证' : '去确认')));
          btn.type = 'button';
          btn.disabled = expired;
          const box = el('div', 'factor-inline');
          if (canVerify) {
            btn.title = '在这一页输入验证码完成核验并批准；服务端会重新校验一次。';
            btn.addEventListener('click', function () {
              btn.disabled = true;
              bxInlineFactor(box, cid, ownerSid, function () { bxRefresh(); });
            });
          } else {
            btn.addEventListener('click', function () { bxAsk('我有一个待确认的操作，继续处理'); });
          }
          bar.appendChild(btn);
          bar.appendChild(el('span', 'card-note', expired
            ? '这条已经过期，批准入口失效，需要重新发起一笔。'
            : (canVerify ? '验证码由服务端签发、一次性、5 分钟有效；验过才批准，服务端仍会重新裁决。'
              : (factors.length ? '这一步的验证已经结束或这一轮没在跑，重新发起一次才有新的验证入口。'
                : '这一轮已经结束，批准不了。'))));
          list.appendChild(bar);
          if (canVerify) { list.appendChild(box); }
          return;
        }
        list.appendChild(bar);
      });
      pendingHost.appendChild(list);
      pendingHost.appendChild(el('div', 'card-note', textOf(pending.source)));
    }
  }

  bxRenderHandoffs($('ov-handoffs'), data.handoffs, 'ov-handoffs-tag');

  const goalHost = $('ov-goals');
  const savings = bxIsObj(data.savings_goals) ? data.savings_goals : {};
  const goals = bxArr(savings.items);
  if (goalHost) {
    if (!goals.length) {
      bxEmpty(goalHost, '还没有储蓄目标', textOf(savings.empty_reason));
    } else {
      goalHost.replaceChildren();
      goals.slice(0, 3).forEach(function (goal) {
        const plan = bxIsObj(goal.plan) ? goal.plan : null;
        const pct = plan ? bxNum(plan.progress_pct) : null;
        const list = el('div', 'list');
        list.appendChild(bxRow(
          textOf(goal.name),
          textOf(goal.saved_yuan) + ' / ' + textOf(goal.target_yuan) + ' 元 · 目标日 ' + textOf(goal.target_date),
          BX_GOAL_STATUS[textOf(goal.status)] || textOf(goal.status)
        ));
        if (pct !== null) { list.appendChild(bxBar(pct, pct >= 100 ? 'ok' : '')); }
        const need = plan ? bxMoney(plan, 'required_per_period') : BX_NA;
        const unit = plan ? (BX_PERIOD_UNIT[textOf(plan.period_unit)] || textOf(plan.period_unit)) : '';
        list.appendChild(el('div', 'card-note', '按后端计划：' + unit + '需要 ' + need +
          (plan && plan.feasibility ? '，' + (BX_FEASIBILITY[textOf(plan.feasibility)] || textOf(plan.feasibility)) : '')));
        goalHost.appendChild(list);
      });
    }
  }

  const planHost = $('ov-plans');
  const plans = bxIsObj(data.plans_and_reminders) ? data.plans_and_reminders : {};
  if (planHost) {
    planHost.replaceChildren();
    const list = el('div', 'list');
    const scheduled = bxIsObj(plans.scheduled) ? plans.scheduled : {};
    const tasks = bxArr(scheduled.items);
    if (!tasks.length) {
      list.appendChild(el('div', 'card-note', '预约任务：' + (textOf(scheduled.empty_reason) || '没有已安排的任务。')));
    } else {
      tasks.slice(0, 3).forEach(function (task) {
        list.appendChild(bxRow(
          task.payee_name ? ('转给 ' + textOf(task.payee_name)) : '预约任务',
          textOf(task.due_text) + ' · ' + textOf(task.state_label),
          textOf(task.amount_yuan) + ' 元'
        ));
      });
    }
    const alerts = bxIsObj(plans.alerts) ? plans.alerts : {};
    const alertItems = bxArr(alerts.items);
    if (!alertItems.length) {
      list.appendChild(el('div', 'card-note', '提醒：' + (textOf(alerts.empty_reason) || '没有新的提醒。')));
    } else {
      alertItems.slice(0, 2).forEach(function (alert) {
        list.appendChild(bxRow(textOf(alert.reason_text), textOf(alert.goal_name), ''));
      });
    }
    planHost.appendChild(list);
    const scheduler = bxIsObj(plans.scheduler) ? plans.scheduler : null;
    if (scheduler) {
      const on = scheduler.enabled === true && scheduler.running === true;
      const everySec = bxNum(scheduler.interval_ms) !== null ? Math.round(bxNum(scheduler.interval_ms) / 1000) : null;
      planHost.appendChild(el('div', 'card-note',
        '预约任务调度：' + (on
          ? '应用内定时器在跑，每 ' + bxCount(everySec, ' 秒') + '检查一次到期任务'
          : '没有在跑，只有点「立即执行到期任务」才会执行')));
      if (scheduler.monitoring_scope) {
        planHost.appendChild(el('div', 'card-note', '持续监测：' + textOf(scheduler.monitoring_scope)));
      }
    }
  }

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

  const host = $('bill-summary');
  if (host) {
    host.replaceChildren();
    host.appendChild(bxStatCard('支出合计', bxMoney(data, 'total_out'), textOf(range.label) + ' · ' + bxCount(data.out_count, ' 笔')));
    host.appendChild(bxStatCard('收入合计', bxMoney(data, 'total_in'), bxCount(data.in_count, ' 笔')));
    /* 结余必须交代口径：这张卡含转账/理财，下面的趋势表排除它们（和总览一致）。
       只说「净结余」就会出现同一屏 -1,311 与 3,476 两个数、谁也不服谁。 */
    const netAll = bxMoney(data, 'net_all');
    const netExt = bxMoney(data, 'net_external');
    const netHint = netExt === BX_NA
      ? '全部收支相减（含转账/理财）'
      : '全部收支相减（含转账/理财）。不算转账/理财是 ' + netExt + '，和总览同一个口径';
    host.appendChild(bxStatCard('净结余', netAll, netHint));
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
        ['月份', '收入', '支出', '结余（不含转账/理财）', '笔数'],
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
        rules.map(function (rule) { return [textOf(rule.rule_id), textOf(rule.rule_text), textOf(rule.confidence)]; })
      ));
    }
  }

  const txHost = $('bill-tx');
  const seen = {};
  const txs = [];
  items.forEach(function (item) {
    const evidence = bxIsObj(item.evidence) ? item.evidence : {};
    bxArr(evidence.transactions).forEach(function (tx) {
      const key = textOf(tx.transaction_id) || (textOf(tx.ts_ms) + ':' + textOf(tx.counterparty));
      if (seen[key]) { return; }
      seen[key] = true;
      txs.push(tx);
    });
  });
  if (txHost) {
    if (!txs.length) {
      bxEmpty(txHost, '没有相关流水', '本区间没有命中的异常，或者后端没有回传流水明细。');
    } else {
      txs.sort(function (a, b) { return (bxNum(b.ts_ms) || 0) - (bxNum(a.ts_ms) || 0); });
      txHost.replaceChildren();
      txHost.appendChild(bxTable(
        ['时间', '对手方', '分类', '备注', '方向', '金额'],
        txs.map(function (tx) {
          return [
            textOf(tx.time_text),
            textOf(tx.counterparty),
            textOf(tx.category),
            textOf(tx.note),
            textOf(tx.direction) === 'in' ? '收入' : '支出',
            textOf(tx.amount_yuan) + ' 元'
          ];
        }),
        [5]
      ));
    }
  }
  const note = $('bill-note');
  if (note) {
    note.textContent = '这里只列规则命中的流水，不是完整对账单；导出完整流水不在本轮范围内。' +
      (data.summary ? ' ' + textOf(data.summary) : '');
  }
}
