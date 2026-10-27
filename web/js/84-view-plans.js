/* 84-view-plans.js — 计划视图：待办、持久化计划、步骤与取消。 */
'use strict';

/* 新建跨场景计划（POST /api/plans）。
   后台同一套落库：页面表单与聊天自然语言进同一张 plans 表，只是入口不同。
   这里只放「一句话说清的一步」，多步 DAG 仍建议在对话里说——表单填不出分支。 */
function bxPlanCreateBar() {
  const note = $('plan-persist-note');
  if (!note || !note.parentNode) { return; }
  if (note.parentNode.querySelector('[data-bar="plan-create"]')) { return; }
  const bar = el('div', 'cap-chips');
  bar.setAttribute('data-bar', 'plan-create');
  const btn = el('button', 'btn btn-mini', '新建一个计划');
  btn.type = 'button';
  btn.addEventListener('click', function () {
    bxFormModal({
      title: '新建计划',
      sub: '只登记「到点提醒你做什么」，不替你动钱；真要动钱的那一步，到期会给你确认卡片。',
      okText: '建好它',
      fields: [
        { name: 'title', label: '计划叫什么', hint: '最多 60 字', value: '' },
        { name: 'step', label: '这一步做什么', hint: '例如「提醒我把 2000 元转进储蓄」', value: '' },
        { name: 'minutes', label: '多少分钟后提醒', hint: '留空就是不定时', type: 'number', value: '' }
      ],
      validate: function (v) {
        if (!v.title) { return '计划名不能空。'; }
        if (!v.step) { return '这一步做什么得写一句。'; }
        if (v.minutes && Number(v.minutes) < 0) { return '分钟数不能是负数。'; }
        return '';
      },
      onSubmit: function (v) {
        const step = { kind: 'reminder', title: v.step, payload: {} };
        if (v.minutes) { step.due_in_ms = Math.round(Number(v.minutes) * 60000); }
        bxSend('/api/plans', 'POST', { title: v.title, steps: [step], meta: { from: 'plans_page' } })
          .then(function () {
            if (typeof bxToast === 'function') { bxToast('计划建好了，到点会在这里提醒你。', 'ok'); }
            bxLoad('plans', true);
          })
          .catch(function (err) {
            if (typeof bxToast === 'function') { bxToast('没建成：' + (err && err.message ? err.message : '未知原因'), 'bad'); }
          });
      }
    });
  });
  bar.appendChild(btn);
  bar.appendChild(el('span', 'card-note', '多步骤、带分支的事建议直接在对话里说，助手会拆成步骤排好。'));
  note.parentNode.insertBefore(bar, note);
}

function bxRenderPlans(data) {
  data = bxIsObj(data) ? data : {};
  /* 助手提醒的正文与反馈按钮住在这页：进计划页取一次就够，只读不生成
     （生成要用户自己点「现在想一想」，免得每次开页面都多出消息）。 */
  if (typeof bxLoadMessages === 'function') { bxLoadMessages(); }
  bxPlanCreateBar();
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

  /* 储蓄计划整块搬去 85-view-savings.js：它要回答「钱真的动了没有」，
     跟下面这些只看状态的块不是一回事。 */
  if (typeof bxRenderSavings === 'function') { bxRenderSavings(data); }

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
        const resolve = el('button', 'btn btn-mini btn-green', '标记已处理');
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
