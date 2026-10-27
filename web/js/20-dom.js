/* 20-dom.js — DOM 工具：转义、建节点、面板与气泡、滚动。只做渲染，不发请求。 */
'use strict';

/* 所有来自后端的文本，进 innerHTML 模板前必过这里 */
function escapeHtml(value) {
  if (value === null || value === undefined) { return ''; }
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/* textContent 路径：浏览器不解析 HTML，纯字符串直出 */
function textOf(value) {
  if (value === null || value === undefined) { return ''; }
  if (typeof value === 'object') { return JSON.stringify(value); }
  return String(value);
}

function setText(node, value) {
  if (node) { node.textContent = textOf(value); }
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) { node.className = className; }
  if (text !== undefined) { node.textContent = textOf(text); }
  return node;
}

/* 从事件 data 里取字段，命中返回 {key, value}，未命中返回空 */
function fieldInfo(data, keys) {
  if (!data || typeof data !== 'object') { return { key: '', value: '' }; }
  let i;
  for (i = 0; i < keys.length; i += 1) {
    const hit = data[keys[i]];
    if (hit !== undefined && hit !== null && hit !== '') { return { key: keys[i], value: textOf(hit) }; }
  }
  for (let c = 0; c < NESTED_KEYS.length; c += 1) {
    let box = data[NESTED_KEYS[c]];
    if (typeof box === 'string') {
      try { box = JSON.parse(box); } catch (err) { box = null; }
    }
    if (box && typeof box === 'object') {
      for (i = 0; i < keys.length; i += 1) {
        const nested = box[keys[i]];
        if (nested !== undefined && nested !== null && nested !== '') {
          return { key: keys[i], value: textOf(nested) };
        }
      }
    }
  }
  return { key: '', value: '' };
}

function fieldVal(data, keys) {
  return fieldInfo(data, keys).value;
}

function asList(value) {
  if (value === null || value === undefined || value === '') { return []; }
  if (Array.isArray(value)) { return value; }
  return [textOf(value)];
}

function levelClass(level) {
  const key = textOf(level).toLowerCase();
  return LEVEL_CLASS[key] || LEVEL_DEFAULT;
}

function levelText(level) {
  const key = textOf(level).toLowerCase();
  return LEVEL_LABEL[key] || '未裁决';
}

function errorText(err) {
  if (!err) { return '未知错误'; }
  if (typeof err === 'string') { return err; }
  return err.message ? err.message : String(err);
}

function panelOf(panelId) {
  return $(panelId);
}

function panelBody(panelId) {
  const panel = panelOf(panelId);
  return panel ? panel.querySelector('[data-role="body"]') : null;
}

function panelBadge(panelId, text) {
  const panel = panelOf(panelId);
  if (!panel) { return; }
  setText(panel.querySelector('[data-role="badge"]'), text);
}

function setPanelLevel(panelId, level) {
  const panel = panelOf(panelId);
  if (!panel) { return; }
  panel.className = 'panel ' + levelClass(level);
}

/* 事件到达即刻渲染，100ms 内完成；这里用一次短闪回提示「这行刚被更新」 */
function flash(panelId) {
  const panel = panelOf(panelId);
  if (!panel) { return; }
  if (panel.tagName === 'DETAILS') { panel.open = true; }
  panel.classList.remove('flash');
  void panel.offsetWidth;
  panel.classList.add('flash');
}

function kvRow(key, value, mono) {
  const row = el('div', 'kv');
  row.appendChild(el('span', 'kv-key', key));
  row.appendChild(el('span', 'kv-val' + (mono ? ' mono' : ''), value));
  return row;
}

/* ------------------------------------------------------------ 左栏：对话 */
function scrollChat() {
  const log = $('chat-log');
  if (log) { log.scrollTop = log.scrollHeight; }
}

function appendBubble(role, text, tag) {
  const log = $('chat-log');
  if (!log) { return null; }
  const wrap = el('div', 'bubble bubble-' + role);
  wrap.appendChild(el('div', 'bubble-role', role === 'user' ? '你' : (role === 'agent' ? '智能体' : '系统')));
  wrap.appendChild(el('div', 'bubble-body', text));
  if (tag) { wrap.appendChild(el('div', 'bubble-tag', tag)); }
  log.appendChild(wrap);
  scrollChat();
  return wrap;
}

function appendNode(node) {
  const log = $('chat-log');
  if (!log) { return; }
  log.appendChild(node);
  scrollChat();
}

function resetTheater() {
  planNodes = [];
  ['panel-intent', 'panel-plan', 'panel-policy', 'panel-factor', 'panel-audit'].forEach(function (id) {
    setPanelLevel(id, '');
    const body = panelBody(id);
    if (body) { body.replaceChildren(); }
    panelBadge(id, '等待');
  });
  panelBadge('panel-plan', '0 个节点');
  const tag = $('theater-idle-tag');
  if (tag) { tag.textContent = '还没有操作'; }
  const last = $('theater-idle-last');
  if (last) { last.textContent = ''; }
}

/* 每轮结束把「这一轮到底怎么判的」写一句到常驻说明条上，切页回来也还看得见。 */
function noteTheater(text) {
  const last = $('theater-idle-last');
  if (last && text) { last.textContent = text; }
  const tag = $('theater-idle-tag');
  if (tag && text) { tag.textContent = '已更新'; }
}

function setBusy(busy) {
  streaming = busy;
  const send = $('chat-send');
  const cancel = $('chat-cancel');
  if (send) { send.disabled = busy; }
  if (cancel) { cancel.disabled = !(busy && sessionId); }
}
