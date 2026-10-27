/* 88-view-monitor.js — 计划状态与步骤文案、立刻跑、到点记录。 */
'use strict';

const BX_PLAN_STATUS = {
  active: '进行中', paused: '已暂停·待你确认', completed: '已完成',
  cancelled: '已取消', failed: '已失败'
};
const BX_STEP_KIND = { check: '检查条件', action: '资金动作', reminder: '提醒' };
const BX_STEP_STATE = {
  pending: '等待到点', running: '正在执行', done: '已完成',
  failed: '已失败', skipped: '已交回你确认'
};

function bxStepClass(state) {
  if (state === 'done') { return 'tag ok'; }
  if (state === 'failed') { return 'tag bad'; }
  if (state === 'skipped') { return 'tag warn'; }
  return 'tag info';
}

function bxRunPlans(btn, planId) {
  btn.disabled = true;
  btn.textContent = '正在推进…';
  bxSend('/api/plans/run', 'POST', planId ? { plan_id: planId } : {}).then(function (data) {
    const done = bxNum(data.count);
    appendBubble('system', done > 0 ? ('这次推进了 ' + done + ' 步。') : '这次没有到点的步骤。');
    bxRefresh();
  }).catch(function (err) {
    appendBubble('system', '推进失败：' + errorText(err));
    btn.disabled = false;
    btn.textContent = '立即推进到点步骤';
  });
}

function bxRenderPersistedPlans(block) {
  const host = $('plan-persist');
  if (!host) { return; }
  host.replaceChildren();
  const data = bxIsObj(block) ? block : {};
  const items = bxArr(data.items);
  setText($('plan-persist-tag'), bxCount(data.count, ' 个'));
  const note = $('plan-persist-note');
  if (note) {
    note.textContent = textOf(data.boundary) ||
      '计划里的资金动作不会在后台自动执行，到期只提醒你回来确认。';
  }
  if (!items.length) {
    bxEmpty(host, '还没有跨场景计划', textOf(data.empty_reason));
    return;
  }
  /* 只在每个计划上给「推进这个计划」：原来还有一个「立即推进到点步骤」跑全部计划，
     两个按钮做同一件事，用户还得先猜哪个更安全。 */
  items.forEach(function (plan) {
    const status = textOf(plan.status);
    const box = el('div', 'anomaly');
    const head = el('div', 'anomaly-head');
    head.appendChild(el('span', 'anomaly-title', textOf(plan.title)));
    head.appendChild(el('span',
      status === 'completed' ? 'tag ok' : (status === 'failed' || status === 'cancelled' ? 'tag bad' : (status === 'paused' ? 'tag warn' : 'tag info')),
      BX_PLAN_STATUS[status] || status));
    box.appendChild(head);
    box.appendChild(el('div', 'card-note',
      '进度 ' + bxCount(plan.finished_steps) + ' / ' + bxCount(plan.step_count) + ' 步 · 计划号 ' + textOf(plan.plan_id)));
    /* 生日计划：预算在 meta 里，付款入口直接挂在对应步骤上，不用回聊天里找。 */
    const planMeta = bxIsObj(plan.meta) ? plan.meta : {};
    const isBirthday = textOf(planMeta.kind) === 'birthday';
    const payHost = isBirthday ? el('div', '') : null;
    if (isBirthday) {
      box.appendChild(el('div', 'card-note',
        '生日 ' + (textOf(planMeta.birthday) || '—') +
        ' · 预算 ' + (textOf(planMeta.budget_yuan) || '—') +
        ' 元 · 到点 ' + (textOf(planMeta.trigger_text) || '—') +
        '（预算已占用可支配金额，余额没动）'));
    }
    bxArr(plan.steps).forEach(function (step) {
      const row = el('div', 'step-row');
      const left = el('div', 'step-main');
      left.appendChild(el('span', 'step-seq', String(bxNum(step.seq))));
      left.appendChild(el('span', 'step-title', textOf(step.title)));
      left.appendChild(el('span', bxStepClass(textOf(step.state)), BX_STEP_STATE[textOf(step.state)] || textOf(step.state)));
      row.appendChild(left);
      row.appendChild(el('span', 'card-note',
        (BX_STEP_KIND[textOf(step.kind)] || textOf(step.kind)) + ' · 到点 ' + textOf(step.due_text)));
      box.appendChild(row);
      if (bxNum(step.attempts) > 0) {
        box.appendChild(el('div', 'card-note',
          '已检查 ' + bxCount(step.attempts) + ' / ' + bxCount(step.max_attempts) + ' 次'));
      }
      if (textOf(step.last_error)) {
        box.appendChild(el('div', 'card-note', textOf(step.last_error)));
      }
      if (isBirthday && textOf(step.kind) === 'action') {
        const payload = bxIsObj(step.payload) ? step.payload : {};
        const key = textOf(payload.item_key);
        if (key && textOf(step.state) === 'done') {
          box.appendChild(el('div', 'card-note', '这一项已付款，预算转为已用。'));
        } else if (key) {
          const payBar = el('div', 'factor-actions');
          const pay = el('button', 'btn btn-mini btn-green', '现在付「' + (textOf(payload.label) || key) + '」');
          pay.type = 'button';
          pay.addEventListener('click', function () {
            bxPayBirthdayItem(textOf(plan.plan_id), key, pay, payHost);
          });
          payBar.appendChild(pay);
          box.appendChild(payBar);
        }
      }
    });
    if (payHost) { box.appendChild(payHost); }
    const runOne = el('button', 'btn btn-mini', '推进这个计划');
    runOne.type = 'button';
    runOne.title = '只推进已经到点的步骤；资金动作仍然要你确认。';
    runOne.addEventListener('click', function () { bxRunPlans(runOne, plan.plan_id); });
    const acts = [runOne];
    if (status !== 'cancelled' && status !== 'completed') {
      const cancel = el('button', 'btn btn-mini', '取消计划');
      cancel.type = 'button';
      cancel.addEventListener('click', function () {
        cancel.disabled = true;
        bxSend('/api/plans/' + encodeURIComponent(plan.plan_id), 'PATCH', { status: 'cancelled' })
          .then(function () { appendBubble('system', '计划已取消：' + textOf(plan.title)); bxRefresh(); })
          .catch(function (err) { cancel.disabled = false; appendBubble('system', '取消失败：' + errorText(err)); });
      });
      acts.push(cancel);
    }
    box.appendChild(bxMore(acts.length > 1 ? '推进 / 取消这个计划' : '推进这个计划', acts));
    host.appendChild(box);
  });
}

function bxRenderUpcoming(host, payload) {
  if (!host) { return; }
  const data = bxIsObj(payload) ? payload : {};
  const items = bxArr(data.items);
  setText($('plan-upcoming-tag'), bxCount(data.count, ' 笔'));
  if (!items.length) {
    bxEmpty(host, '近 7 天没有已知扣费', textOf(data.note) || '后端没有返回窗口内的扣费。');
    host.appendChild(el('div', 'card-note', textOf(data.action)));
    return;
  }
  host.replaceChildren();
  const list = el('div', 'list');
  items.forEach(function (item) {
    list.appendChild(bxRow(
      textOf(item.merchant),
      '预计 ' + textOf(item.next_charge_text) + ' · 还有 ' + bxCount(item.days_left, ' 天') + ' · ' + textOf(item.period_label),
      textOf(item.amount_yuan) + ' 元'
    ));
  });
  host.appendChild(list);
  host.appendChild(el('div', 'card-note', textOf(data.note) + ' ' + textOf(data.action)));
}

function bxRenderMonitorRuns(payload) {
  const host = $('plan-runs');
  const data = bxIsObj(payload) ? payload : {};
  const items = bxArr(data.items);
  /* 这个徽标挂在「调度与监测」的折叠标题上，只写「0 次」看不出是什么的次数。 */
  setText($('plan-run-tag'), bxCount(data.count, ' 次监测'));
  if (host) {
    if (!items.length) {
      bxEmpty(host, '还没有监测记录', '打开目标的后台监测，或手动点一次「立即运行监测」。');
    } else {
      host.replaceChildren();
      const list = el('div', 'list');
      items.forEach(function (run) {
        const ok = textOf(run.status) === 'ok';
        list.appendChild(bxRow(
          (textOf(run.trigger_label) || textOf(run.trigger)) + (ok ? ' · 完成' : ' · 失败'),
          textOf(run.started_text || run.detail || ''),
          '新增提醒 ' + bxCount(run.alerts_created) + ' 条'
        ));
      });
      host.appendChild(list);
    }
    if (data.note) { setText($('plan-run-note'), textOf(data.note)); }
  }
}

function bxCancelGoal(goalId, btn) {
  if (!goalId) { return Promise.resolve(null); }
  if (btn && btn.getAttribute('data-armed') !== '1') {
    if (btn) {
      btn.setAttribute('data-armed', '1');
      btn.textContent = '再点一次确认停止';
      btn.className = 'btn btn-mini btn-red';
    }
    return Promise.resolve(null);
  }
  return pfRequest('/api/savings/goals/' + encodeURIComponent(goalId), {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ status: 'cancelled' })
  }).then(function () {
    appendBubble('sys', '计划已停止（后端状态已改为 cancelled）。已经生成的提醒不受影响，可以单独处理。');
    bxRefresh();
  }).catch(function (err) {
    pfFail(err, '停止计划失败');
  });
}
