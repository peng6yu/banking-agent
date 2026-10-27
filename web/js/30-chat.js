/* 30-chat.js — SSE 收流与剧场三栏（意图/计划/策略）渲染。 */
'use strict';

/* ------------------------------------------------------- SSE 手工解析 */
/* /api/chat 是 POST，浏览器内置的事件流对象不支持 POST，所以用 fetch 拿 ReadableStream 自己切包 */
async function pumpSse(response) {
  if (!response.body || typeof response.body.getReader !== 'function') {
    appendBubble('sys', '这个浏览器不支持流式响应（ReadableStream），请换 Chrome 或 Edge 再试。');
    return;
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';
  let carry = '';
  for (;;) {
    const chunk = await reader.read();
    if (chunk.done) { break; }
    carry += decoder.decode(chunk.value, { stream: true });
    let piece = carry.split('\r\n').join('\n');
    if (piece.charAt(piece.length - 1) === '\r') {
      /* 末尾的 \r 可能是 \r\n 的前半截，留到下一块再归一化 */
      carry = '\r';
      piece = piece.slice(0, -1);
    } else {
      carry = '';
    }
    buffer += piece;
    let cut = buffer.indexOf('\n\n');
    while (cut !== -1) {
      dispatchBlock(buffer.slice(0, cut));
      buffer = buffer.slice(cut + 2);
      cut = buffer.indexOf('\n\n');
    }
  }
  carry += decoder.decode();
  buffer += carry.split('\r\n').join('\n');
  if (buffer.trim()) { dispatchBlock(buffer); }
}

/* 一个 SSE 块：event 行给事件名，data 行给 JSON */
function dispatchBlock(block) {
  if (!block) { return; }
  let name = '';
  let rawData = '';
  const lines = block.split('\n');
  let i;
  for (i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    if (!line || line.charAt(0) === ':') { continue; }
    const at = line.indexOf(':');
    const field = at === -1 ? line : line.slice(0, at);
    let value = at === -1 ? '' : line.slice(at + 1);
    if (value.charAt(0) === ' ') { value = value.slice(1); }
    if (field === 'event') {
      name = value.trim();
    } else if (field === 'data') {
      rawData = rawData === '' ? value : rawData + '\n' + value;
    }
  }
  if (!name && !rawData) { return; }
  let data = {};
  if (rawData) {
    try {
      data = JSON.parse(rawData);
    } catch (err) {
      data = { user_text: '收到一段无法解析的数据：' + rawData };
    }
  }
  handleEvent(name, data);
}

function handleEvent(name, data) {
  switch (name) {
    case 'start': onStart(data); break;
    case 'intent': renderIntent(data); break;
    case 'trace': renderPlan(data); renderPolicy(data); renderAudit(fromEvent('policy.decided', data)); break;
    case 'confirm': renderConfirm(data); renderAudit(fromEvent('confirm.requested', data)); break;
    /* 红灯也要先给确认卡片：用户得先看清收款人/金额/转出账户，再去右侧做验证。
       只弹验证面板、不给卡片，等于让人在看不见交易内容的情况下签字。 */
    case 'factor': renderConfirm(data); renderFactor(data); renderAudit(fromEvent('factor.required', data)); break;
    case 'tool': renderToolResult(data); renderAudit(fromEvent('tool.done', data)); break;
    case 'done': renderDone(data); renderAudit(fromEvent('plan.done', data)); break;
    case 'error':
      renderError(data);
      renderAudit(fromEvent('request.failed', data));
      /* 上一轮还没走完：不自动替用户选，只把那张卡重新拉出来给人看。
         服务端已经把 run_id 与两条出口回过来了，这里只负责让卡重新可见。 */
      if (fieldVal(data, ['code']) === 'SESSION_RUN_CONFLICT') { bxRestoreSession(); }
      break;
    default: renderAudit(fromEvent('unknown.' + textOf(name), data)); break;
  }
}

/* 事件统一入账：seq 与短哈希前 8 位，能拿到就显示，拿不到显示占位符 */
function fromEvent(eventType, data) {
  const box = data && typeof data === 'object' ? data : {};
  let seq = box.seq;
  if (seq === undefined || seq === null || seq === '') { seq = box.audit_seq; }
  if (seq === undefined || seq === null || seq === '') { seq = ++localSeq; }
  return {
    seq: textOf(seq),
    event_type: eventType,
    hash: fieldVal(box, ['hash', 'entry_hash', 'audit_hash']),
    ts_ms: fieldVal(box, ['ts_ms'])
  };
}

function shortHash(hash) {
  const text = textOf(hash);
  return text ? text.slice(0, 8) : '—';
}

function renderAudit(row) {
  const host = panelBody('panel-audit');
  if (!host) { return; }
  if (!host.querySelector('.audit-list')) {
    host.replaceChildren();
    host.appendChild(el('div', 'audit-list'));
  }
  const list = host.querySelector('.audit-list');
  const key = row.seq + '|' + row.event_type;
  const known = auditIndex.get(key);
  if (known) {
    if (row.hash && !known.dataset.hash) {
      known.dataset.hash = '1';
      setText(known.querySelector('.audit-hash'), shortHash(row.hash));
      known.querySelector('.audit-hash').title = textOf(row.hash);
    }
    return;
  }
  const node = el('div', 'audit-row');
  if (row.hash) { node.dataset.hash = '1'; } else { node.dataset.hash = ''; }
  /* 结构用 innerHTML，每个后端值都过 escapeHtml */
  node.innerHTML =
    '<span class="audit-seq">#' + escapeHtml(row.seq) + '</span>' +
    '<span class="audit-type">' + escapeHtml(row.event_type) + '</span>' +
    '<span class="audit-hash" title="' + escapeHtml(row.hash) + '">' + escapeHtml(shortHash(row.hash)) + '</span>';
  list.appendChild(node);
  list.scrollTop = list.scrollHeight;
  auditIndex.set(key, node);
  auditCount += 1;
  panelBadge('panel-audit', auditCount + ' 条');
  flash('panel-audit');
}

/* ------------------------------------------------------------ 各面板渲染 */
function renderIntent(data) {
  const host = panelBody('panel-intent');
  if (!host) { return; }
  host.replaceChildren();
  const intent = fieldVal(data, ['intent']);
  host.appendChild(kvRow('意图', (INTENT_LABEL[intent] || '未识别') + '（' + intent + '）'));
  const confidence = fieldVal(data, ['confidence']);
  if (confidence) { host.appendChild(kvRow('置信度', confidence, true)); }
  const missing = asList(data && data.missing);
  if (missing.length) {
    const row = kvRow('缺什么', missing.join('、'), false);
    row.querySelector('.kv-val').className = 'kv-val warn';
    host.appendChild(row);
  }
  const slots = data && data.slots && typeof data.slots === 'object' ? data.slots : null;
  if (slots) {
    const grid = el('div', 'slot-grid');
    Object.keys(slots).forEach(function (key) {
      const chip = el('span', 'slot');
      chip.appendChild(el('b', null, key + '='));
      chip.appendChild(document.createTextNode(textOf(slots[key])));
      grid.appendChild(chip);
    });
    host.appendChild(grid);
  }
  panelBadge('panel-intent', intent || '已解析');
  setPanelLevel('panel-intent', missing.length ? 'yellow' : 'green');
  flash('panel-intent');
}

function planUpsert(data) {
  const nodeId = fieldVal(data, ['node_id', 'id']) || ('n' + (planNodes.length + 1));
  const tool = fieldVal(data, ['tool', 'tool_name']);
  let node = null;
  let i;
  for (i = 0; i < planNodes.length; i += 1) {
    if (planNodes[i].nodeId === nodeId) { node = planNodes[i]; }
  }
  if (!node) {
    node = { nodeId: nodeId, tool: tool, status: '待执行', level: '' };
    planNodes.push(node);
  }
  if (tool) { node.tool = tool; }
  const level = fieldVal(data, ['level']);
  if (level) { node.level = level; }
  const ok = data && data.ok !== undefined ? data.ok : undefined;
  if (ok === true) { node.status = '成功'; }
  else if (ok === false) { node.status = '失败'; }
  else if (data && typeof data.summary === 'string' && data.summary) { node.status = '已执行'; }
  return node;
}

function renderPlan(data) {
  planUpsert(data);
  const host = panelBody('panel-plan');
  if (!host) { return; }
  host.replaceChildren();
  const list = el('ul', 'plan-list');
  planNodes.forEach(function (node) {
    const item = el('li', 'plan-item ' + levelClass(node.level));
    item.appendChild(el('span', 'plan-node', node.nodeId));
    item.appendChild(el('span', 'plan-tool', node.tool));
    item.appendChild(el('span', 'plan-status', node.status));
    list.appendChild(item);
  });
  host.appendChild(list);
  panelBadge('panel-plan', planNodes.length + ' 个节点');
  const last = planNodes[planNodes.length - 1];
  setPanelLevel('panel-plan', last ? last.level : '');
  flash('panel-plan');
}

function renderPolicy(data) {
  const host = panelBody('panel-policy');
  if (!host) { return; }
  const level = fieldVal(data, ['level', 'decision']);
  host.replaceChildren();
  const lamp = el('div', 'lamp-big');
  lamp.appendChild(el('span', 'bulb'));
  lamp.appendChild(document.createTextNode(levelText(level)));
  host.appendChild(lamp);
  host.appendChild(kvRow('节点', fieldVal(data, ['node_id', 'id']), true));
  host.appendChild(kvRow('工具', fieldVal(data, ['tool', 'tool_name']), true));
  const ruleId = fieldVal(data, ['rule_id', 'rule']);
  if (ruleId) {
    const human = ruleText(ruleId);
    host.appendChild(kvRow('依据', (human ? human + '（' : '') + ruleId + (human ? '）' : ''), !human));
  }
  const reason = fieldVal(data, ['reason_text', 'reason']);
  if (reason) { host.appendChild(kvRow('理由', reason)); }
  /* 后端 policy.engine.explain() 每轮都算好了一句「为什么这么判」，此前前端一个字都没用。
     这是最硬的证据：判决不是模型说的，是策略引擎按规则算的。 */
  const explain = fieldVal(data, ['explain']);
  if (explain) { host.appendChild(kvRow('怎么判的', explain)); }
  panelBadge('panel-policy', LEVEL_DOT[textOf(level).toLowerCase()] || '已裁决');
  setPanelLevel('panel-policy', level);
  flash('panel-policy');
  /* 常驻说明条同步一句：切页回来也知道上一轮是怎么判的 */
  const humanRule = ruleId ? (ruleText(ruleId) || ruleId) : '';
  noteTheater(levelText(level) + (humanRule ? ' · ' + humanRule : '') + (reason ? ' · ' + reason : ''));
}

function factorValue() {
  const input = document.querySelector('#panel-factor .factor-input');
  return input ? input.value.trim() : '';
}
