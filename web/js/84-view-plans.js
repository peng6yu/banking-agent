/* 84-view-plans.js — 计划视图：待办、持久化计划、步骤与取消。 */
'use strict';

function bxRenderPlans(data) {
  data = bxIsObj(data) ? data : {};
  /* 助手提醒的正文与反馈按钮住在这页：进计划页取一次就够，只读不生成
     （生成要用户自己点「现在想一想」，免得每次开页面都多出消息）。 */
  if (typeof bxLoadMessages === 'function') { bxLoadMessages(); }
  const banner = $('plan-banner');
  if (banner) {
    bxRenderScheduler(banner, data.scheduler, '「计划已保存」只是存了参数；只有调度器在跑、并且任务状态是 scheduled，才算「任务已安排」。');
    const btn = el('button', 'btn btn-mini', '立即执行到期任务');
    btn.type = 'button';
    btn.id = 'plan-run-now';
    btn.addEventListener('click', bxRunScheduledNow);
    banner.appendChild(btn);
    /* 五个状态标签是「调度怎么认这些状态」的说明，归技术细节，不占储蓄目标卡的位置。 */
    const legend = bxArr(data.state_legend);
    if (legend.length) {
      const row = el('div', 'cap-chips');
      legend.forEach(function (item) {
        const tag = el('span', 'tag ' + (textOf(item.state) === 'done' ? 'ok' : (textOf(item.state) === 'failed' ? 'bad' : 'info')), textOf(item.label));
        tag.title = textOf(item.meaning);
        row.appendChild(tag);
      });
      banner.appendChild(row);
      banner.appendChild(el('div', 'card-note', '任务状态就这么几种，图标颜色跟右上角的红黄绿灯是两套东西。'));
    }
  }

  const goalHost = $('plan-goals');
  const savings = bxIsObj(data.saved_plans) ? data.saved_plans : {};
  const goals = bxArr(savings.items);
  setText($('plan-goal-tag'), bxCount(savings.count, ' 个'));
  if (goalHost) {
    goalHost.replaceChildren();
    if (!goals.length) {
      bxEmpty(goalHost, '还没有保存的计划', textOf(savings.empty_reason));
    } else {
      goals.forEach(function (goal) {
        const plan = bxIsObj(goal.plan) ? goal.plan : null;
        const box = el('div', 'anomaly');
        const head = el('div', 'anomaly-head');
        head.appendChild(el('span', 'anomaly-title', textOf(goal.name)));
        head.appendChild(el('span', 'tag ' + (textOf(goal.status) === 'achieved' ? 'ok' : (textOf(goal.status) === 'cancelled' ? 'bad' : 'info')), BX_GOAL_STATUS[textOf(goal.status)] || textOf(goal.status)));
        box.appendChild(head);
        box.appendChild(el('div', 'card-note',
          '目标 ' + textOf(goal.target_yuan) + ' 元 · 已存 ' + textOf(goal.saved_yuan) + ' 元 · 目标日 ' + textOf(goal.target_date)));
        if (plan) {
          box.appendChild(bxBar(plan.progress_pct, bxNum(plan.progress_pct) >= 100 ? 'ok' : ''));
          const unitText = BX_PERIOD_UNIT[textOf(plan.period_unit)] || textOf(plan.period_unit);
          box.appendChild(kvRow('每期需要', bxMoney(plan, 'required_per_period') + '（' + unitText + ' × ' + bxCount(plan.periods_remaining) + ' 期）', true));
          box.appendChild(kvRow('每期结余', bxMoney(plan, 'period_surplus')));
          box.appendChild(kvRow('缺口', bxMoney(plan, 'total_gap')));
          box.appendChild(kvRow('可行性', BX_FEASIBILITY[textOf(plan.feasibility)] || textOf(plan.feasibility)));
          /* assumptions 是后端给的字符串数组，原来整段 JSON 直接糊在页面上，
             用户看到的是一堆方括号和引号；这里拆成列表逐条渲染，并默认收起——
             它回答「这个数是怎么算的」，不是第一眼要看的结论。 */
          const assumptions = bxArr(plan.assumptions);
          if (assumptions.length) {
            const fold = el('details', 'goal-why');
            fold.appendChild(el('summary', 'caps-title', '这个数是怎么算的（' + assumptions.length + ' 条假设）'));
            const ul = el('ul', 'assumptions');
            assumptions.forEach(function (line) { ul.appendChild(el('li', '', textOf(line))); });
            fold.appendChild(ul);
            box.appendChild(fold);
          }
          bxArr(plan.warnings).forEach(function (warn) {
            box.appendChild(el('div', 'notice warn', textOf(warn)));
          });
        } else if (goal.plan_error) {
          box.appendChild(el('div', 'notice bad', textOf(goal.plan_error)));
        } else {
          box.appendChild(el('div', 'card-note', '后端没有返回计划计算结果。'));
        }
        const edit = el('button', 'btn btn-mini', '修改');
        edit.type = 'button';
        edit.addEventListener('click', function () {
          const panel = $('panel-proactive');
          if (panel) { panel.open = true; }
          pfStartEdit(goal);
        });
        const cancel = el('button', 'btn btn-mini', '停止计划');
        cancel.type = 'button';
        cancel.addEventListener('click', function () { bxCancelGoal(goal.goal_id, cancel); });
        const monitorOn = goal.monitor_enabled === true;
        const monitor = el('button', 'btn btn-mini' + (monitorOn ? '' : ' btn-green'),
          monitorOn ? '关闭后台监测' : '开启后台监测');
        monitor.type = 'button';
        monitor.title = '打开后，节拍器会按固定间隔检查这个目标并生成提醒；只写提醒，不动钱。';
        monitor.addEventListener('click', function () { bxToggleMonitor(goal.goal_id, !monitorOn, monitor); });
        box.appendChild(bxMore('改目标 / 停止 / 后台监测', [edit, cancel, monitor],
          monitorOn
            ? '后台监测：已开启。节拍器会按间隔检查这个目标，提醒写进「提醒」，不会动钱。'
            : '后台监测：未开启。保存目标不会让它自动跑；需要持续监测时在上面打开。'));
        goalHost.appendChild(box);
      });
    }
  }

  const taskHost = $('plan-tasks');
  const scheduled = bxIsObj(data.scheduled_tasks) ? data.scheduled_tasks : {};
  const tasks = bxArr(scheduled.items);
  if (taskHost) {
    if (!tasks.length) {
      bxEmpty(taskHost, '没有预约任务', textOf(scheduled.empty_reason));
    } else {
      taskHost.replaceChildren();
      const list = el('div', 'list');
      tasks.forEach(function (task) {
        const cls = textOf(task.status) === 'scheduled' ? (task.overdue ? 'warn' : 'info') : (textOf(task.status) === 'failed' ? 'bad' : 'ok');
        list.appendChild(bxRow(
          (textOf(task.payee_name) || '预约任务') + (task.overdue ? ' · 已过期未执行' : ''),
          textOf(task.due_text) + ' · ' + (BX_RECURRENCE[textOf(task.recurrence)] || textOf(task.recurrence)) + ' · ' + textOf(task.state_label),
          textOf(task.amount_yuan) + ' 元'
        ));
        if (task.needs_verification === true) {
          // 到点被判红了：状态仍是 scheduled，不补验证就永远不会执行。
          // 这一档必须能在这里验，否则页面看着「已安排」，钱却一直不动。
          const vbar = el('div', 'anomaly-actions');
          vbar.appendChild(el('span', 'tag warn', '等待你补验证'));
          const vbtn = el('button', 'btn btn-mini btn-green', '补验证并放行');
          vbtn.type = 'button';
          if (task.can_verify_here !== true) { vbtn.disabled = true; }
          vbtn.addEventListener('click', function () {
            bxScheduledVerify(textOf(task.scheduled_id), bxArr(task.required_factors), vbar);
          });
          vbar.appendChild(vbtn);
          list.appendChild(vbar);
        }
        if (textOf(task.status) === 'scheduled') {
          const bar = el('div', 'anomaly-actions');
          bar.appendChild(bxLink('让助手撤销这笔预约', function () {
            bxAsk('帮我撤销 ' + textOf(task.due_text) + ' 给 ' + (textOf(task.payee_name) || '收款人') + ' 转 ' + textOf(task.amount_yuan) + ' 元的那笔预约');
          }));
          list.appendChild(bar);
        }
      });
      taskHost.appendChild(list);
    }
  }

  const alertHost = $('plan-alerts');
  const alerts = bxIsObj(data.alerts) ? data.alerts : {};
  const alertItems = bxArr(alerts.items);
  setText($('plan-alert-tag'), bxCount(alerts.count, ' 条'));
  if (alertHost) {
    if (!alertItems.length) {
      bxEmpty(alertHost, '没有需要处理的提醒', textOf(alerts.empty_reason));
    } else {
      alertHost.replaceChildren();
      alertItems.forEach(function (alert) {
        const box = el('div', 'anomaly');
        const head = el('div', 'anomaly-head');
        head.appendChild(el('span', 'anomaly-title', textOf(alert.reason_text) || textOf(alert.reason)));
        head.appendChild(el('span', 'tag ' + (textOf(alert.status) === 'open' ? 'warn' : 'info'), BX_ALERT_STATUS[textOf(alert.status)] || textOf(alert.status)));
        box.appendChild(head);
        box.appendChild(el('div', 'card-note', textOf(alert.suggestion)));
        box.appendChild(kvRow('相关计划', textOf(alert.goal_name)));
        box.appendChild(kvRow('触发时间', textOf(alert.created_text)));
        const ignore = el('button', 'btn btn-mini', '先不管');
        ignore.type = 'button';
        ignore.title = '只把这条移出待办，不动账；审计里记的是「忽略」。';
        ignore.addEventListener('click', function () {
          pfResolveAlert(alert.alert_id, 'ignore');
        });
        const resolve = el('button', 'btn btn-mini btn-green', '已处理完');
        resolve.type = 'button';
        resolve.title = '你已经自己处理或核实过；审计里记的是「已处理」。';
        resolve.addEventListener('click', function () {
          pfResolveAlert(alert.alert_id, 'resolve');
        });
        box.appendChild(bxMore('处理这条提醒', [ignore, resolve]));
        alertHost.appendChild(box);
      });
    }
  }

  bxRenderUpcoming($('plan-upcoming'), data.upcoming_charges);
  bxRenderPersistedPlans(data.plans);
  bxRenderMonitorRuns(data.monitor_runs);
  bxBindBirthdayForm();
  bxBindReminderForm();
  bxRenderReminders(data.reminders);
}
