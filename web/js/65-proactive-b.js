/* 65-proactive-b.js — 主动财务：目标卡、预算、预警、监测与模拟支出的渲染与提交。 */
'use strict';

function pfGoalBody(values, editing) {
  const body = { name: values.name, target_fen: values.targetFen, target_date: values.dateText };
  if (values.savedFen !== null) {
    body.saved_fen = values.savedFen;
  } else if (!editing) {
    body.saved_fen = 0;
  }
  if (values.budgetFen !== null) { body.budget_fen = values.budgetFen; }
  if (values.tolerance !== null) { body.progress_tolerance = values.tolerance; }
  if (!editing) { body.period = values.period; }
  return body;
}

function pfResetGoalForm() {
  pfEditingGoalId = '';
  ['pf-name', 'pf-target-yuan', 'pf-saved-yuan', 'pf-budget-yuan', 'pf-tolerance'].forEach(function (id) {
    pfSetValue(id, '');
  });
  const dateInput = $('pf-target-date');
  if (dateInput && !dateInput.value) { dateInput.value = pfDefaultTargetDate(); }
  const periodSelect = $('pf-period');
  if (periodSelect) { periodSelect.disabled = false; periodSelect.value = 'monthly'; }
  const cancel = $('pf-goal-cancel');
  if (cancel) { cancel.hidden = true; }
  const submit = $('pf-goal-submit');
  if (submit) { submit.textContent = '创建目标'; }
}

function pfStartEdit(goal) {
  if (!goal) { return; }
  pfEditingGoalId = textOf(goal.goal_id);
  pfSelectedGoalId = pfEditingGoalId;
  const rules = Array.isArray(goal.monitor_rules) ? goal.monitor_rules : [];
  let budgetFen = null;
  let tolerance = null;
  let period = 'monthly';
  if (goal.budget && goal.budget.budget_fen !== undefined && goal.budget.budget_fen !== null) {
    budgetFen = Number(goal.budget.budget_fen);
  }
  rules.forEach(function (rule) {
    const kind = textOf(rule && rule.kind);
    if (kind === 'budget' && budgetFen === null && rule.threshold_fen !== null) {
      budgetFen = Number(rule.threshold_fen);
    }
    if (kind === 'progress') {
      tolerance = rule.threshold_pp === null ? null : Number(rule.threshold_pp);
      period = textOf(rule.period) || 'monthly';
    }
  });
  pfSetValue('pf-name', textOf(goal.name));
  pfSetValue('pf-target-yuan', pfFenToInput(goal.target_fen));
  pfSetValue('pf-saved-yuan', pfFenToInput(goal.saved_fen));
  pfSetValue('pf-target-date', textOf(goal.target_date));
  pfSetValue('pf-budget-yuan', budgetFen === null ? '' : pfFenToInput(budgetFen));
  pfSetValue('pf-tolerance', tolerance === null ? '' : String(tolerance));
  pfSetValue('pf-period', period === 'weekly' ? 'weekly' : 'monthly');
  const periodSelect = $('pf-period');
  if (periodSelect) { periodSelect.disabled = true; }
  const cancel = $('pf-goal-cancel');
  if (cancel) { cancel.hidden = false; }
  const submit = $('pf-goal-submit');
  if (submit) { submit.textContent = '保存修改'; }
  pfHint('pf-goal-hint', '编辑中：已存/预算留空表示不改动；保存后会重新拉取并展示重算后的计划。', '');
  pfRenderGoals();
  pfRenderPlan();
}

async function pfSubmitGoal(ev) {
  if (ev) { ev.preventDefault(); }
  const editing = !!pfEditingGoalId;
  const values = pfGoalFormValues();
  if (values.error) { pfHint('pf-goal-hint', values.error, 'bad'); return; }
  const submit = $('pf-goal-submit');
  if (submit) { submit.disabled = true; }
  pfHint('pf-goal-hint', editing ? '正在保存修改…' : '正在创建目标…', '');
  try {
    const body = pfGoalBody(values, editing);
    let json;
    if (editing) {
      json = await pfRequest(API_PF_GOALS + '/' + encodeURIComponent(pfEditingGoalId), {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
    } else {
      json = await pfRequest(API_PF_GOALS, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
    }
    const goal = json && json.goal ? json.goal : null;
    if (goal && goal.goal_id) { pfSelectedGoalId = textOf(goal.goal_id); }
    pfResetGoalForm();
    pfHint('pf-goal-hint',
      (editing ? '已保存修改：' : '已创建目标：') + textOf(goal && goal.name) +
      '（audit_seq=' + textOf(json && json.audit_seq) + '）', 'good');
    appendBubble('sys', (editing ? '已更新储蓄目标 ' : '已创建储蓄目标 ') + textOf(goal && goal.name) +
      '，audit_seq=' + textOf(json && json.audit_seq) + '。');
    await loadProactive();
  } catch (err) {
    pfHint('pf-goal-hint', pfFail(err, editing ? '保存目标失败' : '创建目标失败'), 'bad');
  } finally {
    if (submit) { submit.disabled = false; }
  }
}

/* ------------------------------------------------------------ 目标列表 */
function pfSelectGoal(goalId) {
  pfSelectedGoalId = textOf(goalId);
  pfRenderGoals();
  pfRenderPlan();
}

function pfGoalCard(goal) {
  const resolvedId = textOf(goal.goal_id);
  const card = el('div', 'pf-card' + (resolvedId === pfSelectedGoalId ? ' sel' : ''));
  const head = el('div', 'pf-card-head');
  head.appendChild(el('span', 'pf-card-name', textOf(goal.name) || '(未命名目标)'));
  const feasibility = pfFeasibilityMeta(goal.feasibility);
  head.appendChild(pfChip(feasibility.text, feasibility.cls));
  const openCount = Number(goal.open_alert_count) || 0;
  if (openCount > 0) { head.appendChild(pfChip('未处理提醒 ' + openCount, 'warn')); }
  card.appendChild(head);

  const pct = Math.max(0, Math.min(100, Number(goal.progress_pct) || 0));
  const bar = el('div', 'pf-bar');
  const fill = el('div', 'pf-bar-fill' + (textOf(goal.feasibility) === 'infeasible' || textOf(goal.feasibility) === 'expired' ? ' bad' : ''));
  fill.style.width = pct + '%';
  bar.appendChild(fill);
  card.appendChild(bar);

  card.appendChild(kvRow('进度', pct + '%'));
  card.appendChild(kvRow('已存 / 目标', textOf(goal.saved_yuan) + ' / ' + textOf(goal.target_yuan) + ' 元'));
  card.appendChild(kvRow('距目标差额', textOf(goal.remaining_yuan) + ' 元'));
  card.appendChild(kvRow('剩余时间', textOf(goal.days_remaining) + ' 天 · ' + textOf(goal.periods_remaining) + ' 个' + pfPeriodUnit(goal.plan && goal.plan.period_unit) + '期'));
  card.appendChild(kvRow('阶段要求', (textOf(goal.required_per_period_yuan) || '—') + ' 元 / 期'));
  if (goal.budget && goal.budget.budget_fen !== undefined && goal.budget.budget_fen !== null) {
    card.appendChild(kvRow('本期预算',
      textOf(goal.budget.budget_yuan) + ' 元（' + (textOf(goal.budget.period_label) || '本期') +
      '），已用 ' + textOf(goal.budget.usage_pct) + '%'));
  }

  const actions = el('div', 'pf-actions-row');
  actions.appendChild(pfButton('查看计算依据', 'btn btn-mini btn-ghost', function () { pfSelectGoal(resolvedId); }));
  actions.appendChild(pfButton('编辑目标', 'btn btn-mini', function () { pfStartEdit(goal); }));
  card.appendChild(actions);
  return card;
}

function pfRenderGoals() {
  const host = $('pf-goal-list');
  if (!host) { return; }
  host.replaceChildren();
  setText($('pf-goal-count'), pfGoals.length + ' 个');
  if (!pfGoals.length) {
    host.appendChild(el('div', 'pf-empty', '还没有储蓄目标。用上面的表单建一个，再点「立即运行监测（手动触发）」。'));
    return;
  }
  pfGoals.forEach(function (goal) { host.appendChild(pfGoalCard(goal)); });
}

/* ------------------------------------------------ 计划 / 计算依据 / 预算偏差 */
function pfEvidenceForGoal(goalId) {
  for (let i = 0; i < pfAlerts.length; i += 1) {
    const alert = pfAlerts[i];
    if (textOf(alert && alert.goal_id) !== textOf(goalId)) { continue; }
    if (alert.evidence && typeof alert.evidence === 'object') { return alert.evidence; }
  }
  return null;
}

function pfPlanFor(goal) {
  if (goal && goal.plan && typeof goal.plan === 'object') { return goal.plan; }
  return pfEvidenceForGoal(goal ? goal.goal_id : '') || {};
}

function pfRenderBudget(goal) {
  const box = el('div', 'pf-block');
  box.appendChild(el('div', 'pf-title', '预算偏差'));
  const budget = goal && goal.budget && typeof goal.budget === 'object' ? goal.budget : null;
  if (!budget) {
    box.appendChild(el('div', 'pf-empty', '这个目标没有每期预算：在表单里填「每期预算（元）」后保存，就会生成预算监测规则。'));
    return box;
  }
  const over = budget.over_budget === true;
  box.appendChild(kvRow('账期', textOf(budget.period_label) + '（' + pfPeriodUnit(budget.period) + '）'));
  box.appendChild(kvRow('预算', textOf(budget.budget_yuan) + ' 元'));
  box.appendChild(kvRow('实际支出', textOf(budget.actual_yuan) + ' 元'));
  box.appendChild(kvRow('偏差', textOf(budget.deviation_yuan) + ' 元 · ' + (over ? '超支' : '结余')));
  if (over) {
    box.appendChild(kvRow('超支金额', pfYuanFromFen(budget.over_fen) + ' 元'));
  } else {
    box.appendChild(kvRow('剩余额度', pfYuanFromFen(budget.remaining_fen) + ' 元'));
  }
  box.appendChild(kvRow('预算使用率', textOf(budget.usage_pct) + '%'));
  return box;
}

function pfRenderPlan() {
  const host = $('pf-plan');
  if (!host) { return; }
  host.replaceChildren();
  const goal = pfGoalById(pfSelectedGoalId) || pfGoals[0] || null;
  if (!goal) {
    host.appendChild(el('div', 'pf-empty', '还没有目标，先建一个再看计算依据。'));
    return;
  }
  const plan = pfPlanFor(goal);
  host.appendChild(el('div', 'pf-card-name', '目标：' + textOf(goal.name)));
  host.appendChild(el('div', 'pf-note', '金额由确定性代码计算，模型只做解释。下面每一项都直接取自后端返回字段（*fen 为整数分，括号里是换算后的元）。'));
  host.appendChild(kvRow('币种', textOf(plan.currency) || 'CNY'));
  host.appendChild(kvRow('周期单位', pfPeriodUnit(plan.period_unit || (goal.budget && goal.budget.period))));
  host.appendChild(kvRow('剩余周期', textOf(plan.periods_remaining !== undefined ? plan.periods_remaining : goal.periods_remaining) + ' 个'));
  host.appendChild(kvRow('每期需存', pfYuanFromFen(plan.required_per_period_fen !== undefined ? plan.required_per_period_fen : goal.required_per_period_fen) + ' 元'));
  host.appendChild(kvRow('历史月均收入', pfYuanFromFen(plan.avg_income_fen) + ' 元'));
  host.appendChild(kvRow('历史月均必要支出', pfYuanFromFen(plan.avg_essential_fen) + ' 元'));
  host.appendChild(kvRow('历史月均总支出', pfYuanFromFen(plan.avg_spend_fen) + ' 元'));
  host.appendChild(kvRow('预计每期结余', pfYuanFromFen(plan.projected_period_balance_fen) + ' 元'));
  host.appendChild(kvRow('每期缺口', pfYuanFromFen(plan.period_gap_fen) + ' 元' +
    (Number(plan.period_gap_fen) < 0 ? '（负数表示当期有结余）' : '')));
  host.appendChild(kvRow('总缺口', pfYuanFromFen(plan.total_gap_fen) + ' 元' +
    (Number(plan.total_gap_fen) < 0 ? '（负数表示总账有结余）' : '')));
  host.appendChild(kvRow('历史样本',
    Number(plan.months_analyzed) ? textOf(plan.months_analyzed) + ' 个完整月' : '—'));
  host.appendChild(kvRow('可行性', pfFeasibilityMeta(plan.feasibility || goal.feasibility).text));
  host.appendChild(kvRow('距目标差额', (textOf(goal.remaining_yuan) || pfYuanFromFen(goal.remaining_fen)) + ' 元'));

  const warnings = Array.isArray(plan.warnings) ? plan.warnings : [];
  if (warnings.length) {
    const list = el('ul', 'pf-list-tight pf-warnlist');
    warnings.forEach(function (code) {
      list.appendChild(el('li', '', PF_WARNING[textOf(code)] || textOf(code)));
    });
    host.appendChild(el('div', 'pf-sub', '后端 warnings'));
    host.appendChild(list);
  }
  const assumptions = Array.isArray(plan.assumptions) ? plan.assumptions : [];
  if (assumptions.length) {
    const list = el('ul', 'pf-list-tight pf-assume');
    assumptions.forEach(function (line) { list.appendChild(el('li', '', textOf(line))); });
    host.appendChild(el('div', 'pf-sub', '后端 assumptions（计算口径）'));
    host.appendChild(list);
  }
  host.appendChild(el('hr', 'pf-divider'));
  host.appendChild(pfRenderBudget(goal));
}

/* ------------------------------------------------------------ 提醒列表 */
function pfEvidenceValue(key, value) {
  if (/_fen$/.test(key)) { return pfYuanFromFen(value) + ' 元（' + textOf(value) + ' 分）'; }
  if (/_ms$/.test(key)) { return pfTimeText(value); }
  if (key === 'feasibility') { return pfFeasibilityMeta(value).text; }
  if (value && typeof value === 'object') { return JSON.stringify(value); }
  return textOf(value);
}

function pfAlertCard(alert) {
  const card = el('div', 'pf-card');
  const head = el('div', 'pf-card-head');
  const reason = textOf(alert.reason);
  head.appendChild(pfChip(PF_REASON[reason] || reason || '提醒',
    reason === 'progress_behind' ? 'warn' : 'bad'));
  head.appendChild(pfChip('目标：' + pfGoalName(alert.goal_id), ''));
  card.appendChild(head);
  card.appendChild(el('div', 'pf-note', textOf(alert.suggestion) || '（后端没有返回建议）'));
  card.appendChild(kvRow('提醒 ID', textOf(alert.alert_id), true));
  if (alert.rule_id) { card.appendChild(kvRow('规则 ID', textOf(alert.rule_id), true)); }
  card.appendChild(kvRow('创建时间', pfTimeText(alert.created_ms)));
  card.appendChild(kvRow('状态', textOf(alert.status)));
  if (alert.resolved_ms) { card.appendChild(kvRow('处理时间', pfTimeText(alert.resolved_ms))); }

  const evidence = alert.evidence && typeof alert.evidence === 'object' ? alert.evidence : null;
  if (evidence) {
    const box = el('div', 'pf-evidence');
    box.appendChild(el('div', 'pf-sub', '证据（后端 evidence 原始字段）'));
    Object.keys(evidence).forEach(function (key) {
      box.appendChild(kvRow(PF_EVIDENCE_LABEL[key] || key, pfEvidenceValue(key, evidence[key])));
    });
    card.appendChild(box);
  } else {
    card.appendChild(el('div', 'pf-note', '这条提醒没有 evidence 字段。'));
  }

  const actions = el('div', 'pf-actions-row');
  actions.appendChild(pfButton('忽略', 'btn btn-mini', function () { pfResolveAlert(alert.alert_id, 'ignore'); }));
  actions.appendChild(pfButton('标记已解决', 'btn btn-mini btn-green', function () { pfResolveAlert(alert.alert_id, 'resolve'); }));
  card.appendChild(actions);
  return card;
}

function pfRenderAlerts() {
  const host = $('pf-alert-list');
  if (!host) { return; }
  host.replaceChildren();
  setText($('pf-alert-count'), pfAlerts.length + ' 条待处理');
  if (!pfAlerts.length) {
    host.appendChild(el('div', 'pf-empty', '没有待处理提醒。注入一笔模拟支出后点「立即运行监测（手动触发）」，就能走通「注入支出 → 重跑监测 → 出现提醒」。'));
    return;
  }
  pfAlerts.forEach(function (alert) { host.appendChild(pfAlertCard(alert)); });
}

async function pfResolveAlert(alertId, action) {
  const id = textOf(alertId);
  if (!id) { return; }
  const act = action === 'ignore' ? 'ignore' : 'resolve';
  try {
    const json = await pfRequest(API_PF_ALERTS + '/' + encodeURIComponent(id) + '/resolve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: act })
    });
    const idempotent = json && json.idempotent === true;
    /* 反馈必须落在用户当下看的这一页：以前只往 AI 助手页写一条 sys 气泡，
       计划页上点了「忽略」当场什么都看不到，卡片要整页刷新才消失。 */
    if (typeof bxToast === 'function') {
      bxToast((act === 'ignore' ? '已忽略这条提醒' : '已标记为已处理') +
        (idempotent ? '（之前已经处理过，这次没有新变更）' : ''), 'ok');
    }
    await loadProactive();
    if (typeof bxRefresh === 'function') { bxRefresh(); }
  } catch (err) {
    const message = pfFail(err, act === 'ignore' ? '忽略提醒失败' : '标记提醒失败');
    if (typeof bxToast === 'function') { bxToast(message, 'bad'); }
  }
}

/* ------------------------------------------------------ 手动监测 / 运行结果 */
function pfRunCost(run) {
  const cost = (Number(run && run.finished_ms) || 0) - (Number(run && run.started_ms) || 0);
  return cost >= 0 ? String(cost) : '0';
}

function pfRenderRun(run) {
  const host = $('pf-run');
  if (!host) { return; }
  host.replaceChildren();
  if (!run) {
    host.appendChild(el('div', 'pf-empty', '本页面还没有手动运行过监测。点右上角「立即运行监测（手动触发）」。'));
    return;
  }
  const ok = textOf(run.status) === 'ok';
  const card = el('div', 'pf-card');
  const head = el('div', 'pf-card-head');
  head.appendChild(pfChip('status=' + (textOf(run.status) || 'unknown'), ok ? 'ok' : 'bad'));
  head.appendChild(el('span', 'pf-sub', 'run_id=' + textOf(run.run_id)));
  card.appendChild(head);
  card.appendChild(kvRow('扫描目标数', textOf(run.goals_checked)));
  card.appendChild(kvRow('检查规则数', textOf(run.rules_checked)));
  card.appendChild(kvRow('生成提醒数', textOf(run.alerts_created)));
  card.appendChild(kvRow('耗时', pfRunCost(run) + ' ms'));
  card.appendChild(kvRow('运行明细（失败原因）', textOf(run.detail) || '—'));
  card.appendChild(kvRow('audit_seq', textOf(run.audit_seq)));
  host.appendChild(card);

  if (Array.isArray(run.alerts) && run.alerts.length) {
    const list = el('div', 'pf-list');
    run.alerts.forEach(function (item) {
      const row = el('div', 'pf-card');
      const rowHead = el('div', 'pf-card-head');
      rowHead.appendChild(pfChip(PF_REASON[textOf(item.reason)] || textOf(item.reason), 'warn'));
      rowHead.appendChild(el('span', 'pf-sub', '目标：' + pfGoalName(item.goal_id)));
      row.appendChild(rowHead);
      row.appendChild(el('div', 'pf-note', textOf(item.suggestion)));
      list.appendChild(row);
    });
    host.appendChild(list);
  }
}

async function pfRunMonitor() {
  const button = $('btn-pf-monitor');
  if (button) { button.disabled = true; }
  const host = $('pf-run');
  if (host) { host.replaceChildren(el('div', 'pf-empty', '正在手动运行一次监测…')); }
  try {
    const json = await pfRequest(API_PF_MONITOR, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}'
    });
    pfRenderRun(json);
    appendBubble('sys', '手动监测完成：status=' + textOf(json && json.status) +
      '，扫描目标 ' + textOf(json && json.goals_checked) +
      ' 个，生成提醒 ' + textOf(json && json.alerts_created) +
      ' 条，耗时 ' + pfRunCost(json) + ' ms。');
    await loadProactive();
  } catch (err) {
    if (host) { host.replaceChildren(el('div', 'pf-empty', '监测没有跑通：' + pfErrText(err))); }
    pfFail(err, '手动运行监测失败');
  } finally {
    if (button) { button.disabled = false; }
  }
}

/* ------------------------------------------------------------ 模拟支出 */
function pfRenderSimResult(json) {
  const host = $('pf-sim-result');
  if (!host) { return; }
  host.replaceChildren();
  const transaction = json && json.transaction && typeof json.transaction === 'object' ? json.transaction : {};
  host.className = 'pf-card';
  host.style.marginTop = '6px';
  host.appendChild(kvRow('模拟流水号', textOf(transaction.transaction_id || transaction.id), true));
  host.appendChild(kvRow('金额', textOf(transaction.amount_yuan) + ' 元'));
  host.appendChild(kvRow('商户 / 分类', textOf(transaction.merchant) + ' / ' + textOf(transaction.category)));
  host.appendChild(kvRow('演示余额', pfYuanFromFen(json && json.balance_after_fen) + ' 元'));
  host.appendChild(kvRow('幂等', json && json.idempotent === true ? '是（当天同参数已注入过，复用原流水，不重复扣演示余额）' : '否（新插入一条模拟流水）'));
  host.appendChild(kvRow('notice', textOf(json && json.notice)));
  host.appendChild(kvRow('audit_seq', textOf(json && json.audit_seq)));
}

async function pfSubmitSimulate(ev) {
  if (ev) { ev.preventDefault(); }
  const amountFen = pfParseYuanToFen(pfValue('pf-sim-amount'));
  const merchant = textOf(pfValue('pf-sim-merchant')).trim();
  const category = pfValue('pf-sim-category') || '餐饮';
  const note = textOf(pfValue('pf-sim-note')).trim();
  if (amountFen === null) { pfHint('pf-sim-hint', '金额请填正数、最多两位小数（例如 88 或 88.50）。', 'bad'); return; }
  if (!merchant) { pfHint('pf-sim-hint', '商户不能为空。', 'bad'); return; }
  if (PF_CATEGORIES.indexOf(category) < 0) { pfHint('pf-sim-hint', '分类不在后端允许的 7 类里。', 'bad'); return; }
  const body = { amount_fen: amountFen, merchant: merchant, category: category };
  if (note) { body.note = note; }
  const button = $('pf-sim-submit');
  if (button) { button.disabled = true; }
  pfHint('pf-sim-hint', '正在注入模拟支出…', '');
  try {
    const json = await pfRequest(API_PF_SIMULATE, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    pfRenderSimResult(json);
    appendBubble('sys', '已注入模拟支出 ' + textOf(json && json.transaction && json.transaction.amount_yuan) +
      ' 元（商户 ' + merchant + '），演示余额 ' + pfYuanFromFen(json && json.balance_after_fen) + ' 元。' +
      (json && json.idempotent === true ? ' 幂等：当天同参数已注入过，本次复用原流水。' : ''));
    pfHint('pf-sim-hint', '已重新拉取目标与提醒。如果要让新支出触发提醒，再点一次「立即运行监测（手动触发）」。', 'good');
    await loadProactive();
  } catch (err) {
    if (err && err.status === 404) {
      pfHint('pf-sim-hint', '后端该接口尚未就绪（HTTP 404），请等后端部署后再试；这一步没有写入任何数据，也不会假装成功。', 'bad');
      appendBubble('sys', '模拟支出接口还未就绪，本次没有写入数据。');
    } else {
      pfHint('pf-sim-hint', pfFail(err, '注入模拟支出失败'), 'bad');
    }
  } finally {
    if (button) { button.disabled = false; }
  }
}

/* ------------------------------------------------------------ 加载 / 绑定 */
async function loadProactive() {
  try {
    const results = await Promise.all([
      pfRequest(API_PF_GOALS + '?status=active'),
      pfRequest(API_PF_ALERTS + '?status=open')
    ]);
    const goalsJson = results[0] || {};
    const alertsJson = results[1] || {};
    pfGoals = Array.isArray(goalsJson.goals) ? goalsJson.goals : [];
    pfAlerts = Array.isArray(alertsJson.alerts) ? alertsJson.alerts : [];
    if (!pfSelectedGoalId || !pfGoalById(pfSelectedGoalId)) {
      pfSelectedGoalId = pfGoals.length ? textOf(pfGoals[0].goal_id) : '';
    }
    setPanelLevel('panel-proactive', pfAlerts.length ? 'yellow' : (pfGoals.length ? 'green' : 'unknown'));
    panelBadge('panel-proactive', pfGoals.length + ' 个目标 · ' + pfAlerts.length + ' 条待处理');
    pfRenderGoals();
    pfRenderPlan();
    pfRenderAlerts();
  } catch (err) {
    setPanelLevel('panel-proactive', 'red');
    panelBadge('panel-proactive', '加载失败');
    const goalHost = $('pf-goal-list');
    if (goalHost) { goalHost.replaceChildren(el('div', 'pf-empty', '目标加载失败：' + pfErrText(err))); }
    const alertHost = $('pf-alert-list');
    if (alertHost) { alertHost.replaceChildren(el('div', 'pf-empty', '提醒加载失败：' + pfErrText(err))); }
    appendBubble('sys', '主动财务面板加载失败：' + pfErrText(err));
  }
}

function pfBind() {
  const goalForm = $('pf-goal-form');
  if (goalForm) { goalForm.addEventListener('submit', pfSubmitGoal); }
  const simForm = $('pf-sim-form');
  if (simForm) { simForm.addEventListener('submit', pfSubmitSimulate); }
  const cancel = $('pf-goal-cancel');
  if (cancel) {
    cancel.addEventListener('click', function () {
      pfResetGoalForm();
      pfHint('pf-goal-hint', '已取消编辑。', '');
    });
  }
  const refresh = $('btn-pf-refresh');
  if (refresh) { refresh.addEventListener('click', function () { loadProactive(); }); }
  const monitor = $('btn-pf-monitor');
  if (monitor) { monitor.addEventListener('click', function () { pfRunMonitor(); }); }
  const dateInput = $('pf-target-date');
  if (dateInput && !dateInput.value) { dateInput.value = pfDefaultTargetDate(); }
  pfRenderGoals();
  pfRenderPlan();
  pfRenderAlerts();
  pfRenderRun(null);
}

/* ------------------------------------------------------------------ 启动 */
function boot() {
  const form = $('chat-form');
  if (form) {
    form.addEventListener('submit', function (ev) {
      ev.preventDefault();
      sendMessage();
    });
  }
  const cancel = $('chat-cancel');
  if (cancel) {
    cancel.addEventListener('click', function () { cancelSession(); });
  }
  const pull = $('btn-audit-pull');
  if (pull) {
    pull.addEventListener('click', function () { pullAudit(); });
  }
  const verify = $('btn-audit-verify');
  if (verify) {
    verify.addEventListener('click', function () { verifyAudit(); });
  }
  appendBubble('sys', '我在。说一句人话就行，比如「帮我给张三转 800 元」，右栏会同步亮灯。');
  loadTools();
  pfBind();
  loadProactive();
}

/* ============================================================ 六区业务视图
   只读数据全部来自 /api/business/*；前端不做任何金额推算，
   后端没给的字段一律渲染成「暂不可用」，绝不填样例值。 */
