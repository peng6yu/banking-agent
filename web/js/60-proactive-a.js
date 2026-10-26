/* 60-proactive-a.js — 主动财务（目标/预警/监测）：常量与表单求值。 */
'use strict';

const API_PF_GOALS = '/api/savings/goals';
const API_PF_ALERTS = '/api/proactive/alerts';
const API_PF_MONITOR = '/api/proactive/monitor/run';
const API_PF_SIMULATE = '/api/proactive/simulate/expense';
/* 与后端 MAX_TARGET_FEN = 100_000_000_00 分保持一致 */
const PF_MAX_FEN = 10000000000;
const PF_FEASIBILITY = {
  feasible: { text: '可行', cls: 'ok' },
  infeasible: { text: '不可行', cls: 'bad' },
  unknown: { text: '数据不足', cls: 'warn' },
  achieved: { text: '已达成', cls: 'ok' },
  expired: { text: '已过期', cls: 'bad' }
};
const PF_WARNING = {
  GOAL_ACHIEVED: '目标已达成。',
  TARGET_DATE_PASSED: '目标日期已经过去，计划无法成立，建议改日期。',
  NO_TRANSACTION_HISTORY: '历史流水不足，无法判断可行性。',
  CASHFLOW_NOT_POSITIVE: '历史预计结余不为正，储蓄计划不可行。',
  REQUIRED_EXCEEDS_SURPLUS: '每期需存金额超过预计结余，计划不可行。'
};
const PF_REASON = {
  progress_behind: '进度落后',
  budget_overspent: '预算超支',
  cashflow_insufficient: '现金流不足'
};
const PF_EVIDENCE_LABEL = {
  kind: '证据类型',
  period: '周期',
  period_label: '账期',
  budget_fen: '预算（分）',
  actual_fen: '实际支出（分）',
  deviation_fen: '偏差（分）',
  over_fen: '超支（分）',
  usage_pct: '预算使用率',
  avg_income_fen: '历史月均收入（分）',
  avg_essential_fen: '历史月均必要支出（分）',
  projected_period_balance_fen: '预计每期结余（分）',
  required_per_period_fen: '每期需存（分）',
  period_gap_fen: '每期缺口（分）',
  remaining_fen: '距目标差额（分）',
  expected_pct: '应有进度',
  actual_pct: '实际进度',
  lag_pp: '落后（百分点）',
  threshold_pp: '容差（百分点）',
  expected_saved_fen: '应有已存（分）',
  saved_fen: '已存（分）',
  target_fen: '目标（分）',
  periods_remaining: '剩余周期',
  feasibility: '可行性',
  evaluated_ms: '评估时间',
  dedupe_key: '去重键'
};
/* 与后端 SIMULATED_EXPENSE_CATEGORIES 保持一致 */
const PF_CATEGORIES = ['餐饮', '交通', '购物', '生活缴费', '娱乐', '医疗', '订阅'];

let pfGoals = [];
let pfAlerts = [];
let pfSelectedGoalId = '';
let pfEditingGoalId = '';

/* 金额展示：整数分 -> 千分位元字符串（纯展示换算，不做业务计算） */
function pfYuanFromFen(fen) {
  const num = Number(fen);
  if (!Number.isFinite(num)) { return '—'; }
  const negative = num < 0;
  const abs = Math.abs(Math.round(num));
  const cents = abs % 100;
  const whole = String(Math.floor(abs / 100)).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  return (negative ? '-' : '') + whole + '.' + (cents < 10 ? '0' : '') + String(cents);
}

/* 元 -> 分：BigInt 整数运算，避免 0.1 + 0.2 这类浮点误差 */
function pfParseYuanToFen(raw) {
  const text = textOf(raw).trim().replace(/,/g, '');
  if (!/^\d+(\.\d{1,2})?$/.test(text)) { return null; }
  const parts = text.split('.');
  const whole = parts[0].replace(/^0+(?=\d)/, '');
  const cents = ((parts[1] || '') + '00').slice(0, 2);
  try {
    const fen = BigInt(whole) * BigInt(100) + BigInt(cents);
    if (fen < BigInt(1) || fen > BigInt(PF_MAX_FEN)) { return null; }
    return Number(fen);
  } catch (err) {
    return null;
  }
}

function pfFenToInput(fen) {
  const text = pfYuanFromFen(fen);
  return text === '—' ? '' : text.replace(/,/g, '');
}

function pfTimeText(ms) {
  const num = Number(ms);
  if (!Number.isFinite(num) || num <= 0) { return '—'; }
  const date = new Date(num);
  function p2(value) { return value < 10 ? '0' + value : String(value); }
  return date.getFullYear() + '-' + p2(date.getMonth() + 1) + '-' + p2(date.getDate()) +
    ' ' + p2(date.getHours()) + ':' + p2(date.getMinutes()) + ':' + p2(date.getSeconds());
}

function pfDefaultTargetDate() {
  const date = new Date();
  date.setDate(date.getDate() + 365);
  const month = date.getMonth() + 1;
  const day = date.getDate();
  return String(date.getFullYear()) + '-' + (month < 10 ? '0' + month : String(month)) +
    '-' + (day < 10 ? '0' + day : String(day));
}

function pfValue(id) {
  const node = $(id);
  return node ? node.value : '';
}

function pfSetValue(id, value) {
  const node = $(id);
  if (node) { node.value = value; }
}

function pfHint(id, text, kind) {
  const node = $(id);
  if (!node) { return; }
  node.textContent = textOf(text);
  node.className = 'pf-hint' + (kind ? ' ' + kind : '');
}

function pfChip(text, cls) {
  return el('span', 'pf-chip' + (cls ? ' ' + cls : ''), text);
}

function pfButton(text, className, handler) {
  const button = el('button', className || 'btn btn-mini', text);
  button.type = 'button';
  if (handler) { button.addEventListener('click', handler); }
  return button;
}

function pfFeasibilityMeta(level) {
  return PF_FEASIBILITY[textOf(level)] || { text: textOf(level) || '未知', cls: '' };
}

function pfPeriodUnit(unit) {
  return textOf(unit) === 'week' ? '周' : '月';
}

function pfGoalById(goalId) {
  for (let i = 0; i < pfGoals.length; i += 1) {
    if (textOf(pfGoals[i].goal_id) === textOf(goalId)) { return pfGoals[i]; }
  }
  return null;
}

function pfGoalName(goalId) {
  const goal = pfGoalById(goalId);
  return goal ? (textOf(goal.name) || textOf(goalId)) : (textOf(goalId) || '—');
}

/* ---------------------------------------------------- 错误：非 2xx 一律取出可读原因 */
async function pfErrorFromResponse(response) {
  let code = '';
  let reason = '';
  try {
    const raw = await response.text();
    try {
      const json = JSON.parse(raw);
      code = textOf(json.code);
      reason = textOf(json.user_text);
      if (!reason && json.detail !== undefined && json.detail !== null) {
        if (Array.isArray(json.detail)) {
          reason = json.detail.map(function (item) {
            const loc = item && Array.isArray(item.loc) ? item.loc.join('.') : '';
            const msg = item && item.msg ? textOf(item.msg) : textOf(item);
            return loc ? (loc + '：' + msg) : msg;
          }).join('；');
        } else if (typeof json.detail === 'object') {
          reason = JSON.stringify(json.detail);
        } else {
          reason = textOf(json.detail);
        }
      }
    } catch (err) {
      reason = raw;
    }
  } catch (err) {
    reason = '';
  }
  if (!reason) {
    if (response.status === 401) {
      reason = '令牌不对或已失效，请用带 #token= 的地址重新打开页面。';
    } else if (response.status === 404) {
      reason = '后端没有这个接口或这条记录。';
    } else if (response.status === 422) {
      reason = '提交的参数没通过后端校验。';
    } else if (response.status >= 500) {
      reason = '后端处理失败，请稍后重试。';
    } else {
      reason = '请求没通过（HTTP ' + response.status + '）。';
    }
  }
  return { pfError: true, status: response.status, code: code, reason: reason };
}

async function pfRequest(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) { throw await pfErrorFromResponse(response); }
  return response.json();
}

function pfErrText(err) {
  if (err && err.pfError) {
    return err.reason + (err.code ? '（code=' + err.code + '）' : '') + ' [HTTP ' + err.status + ']';
  }
  return errorText(err);
}

function pfFail(err, where) {
  const message = (where ? where + '：' : '') + pfErrText(err);
  appendBubble('sys', message);
  return message;
}

/* ------------------------------------------------------------ 目标表单 */
function pfGoalFormValues() {
  const name = textOf(pfValue('pf-name')).trim();
  const targetText = textOf(pfValue('pf-target-yuan')).trim();
  const savedText = textOf(pfValue('pf-saved-yuan')).trim();
  const dateText = textOf(pfValue('pf-target-date')).trim();
  const budgetText = textOf(pfValue('pf-budget-yuan')).trim();
  const toleranceText = textOf(pfValue('pf-tolerance')).trim();
  const period = pfValue('pf-period') || 'monthly';
  if (!name) { return { error: '请先填目标名。' }; }
  const targetFen = pfParseYuanToFen(targetText);
  if (targetFen === null) {
    return { error: '目标金额请填正数、最多两位小数（例如 30000 或 30000.50）。' };
  }
  if (!/^\d{4}-\d{2}-\d{2}$/.test(dateText)) { return { error: '目标日期请按 YYYY-MM-DD 填。' }; }
  let savedFen = null;
  if (savedText) {
    savedFen = pfParseYuanToFen(savedText);
    if (savedFen === null) { return { error: '已存金额请填 0 或正数，最多两位小数。' }; }
    if (savedFen > targetFen) { return { error: '已存金额不能超过目标金额（后端也会拒绝）。' }; }
  }
  let budgetFen = null;
  if (budgetText) {
    budgetFen = pfParseYuanToFen(budgetText);
    if (budgetFen === null) { return { error: '每期预算请填正数，最多两位小数；留空表示不设预算。' }; }
  }
  let tolerance = null;
  if (toleranceText) {
    if (!/^\d+$/.test(toleranceText) || Number(toleranceText) < 1 || Number(toleranceText) > 100) {
      return { error: '进度容差请填 1–100 的整数，留空用后端默认值。' };
    }
    tolerance = Number(toleranceText);
  }
  return {
    name: name,
    targetFen: targetFen,
    savedFen: savedFen,
    dateText: dateText,
    budgetFen: budgetFen,
    tolerance: tolerance,
    period: period
  };
}
