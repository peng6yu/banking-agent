/* 45-modal.js — 弹窗外壳、强确认、表单弹窗。
   单独成文件是因为 40-confirm.js 已经贴着 600 行上限，而这三样是通用件：
   任何不可撤销的动作、任何两三个字段的小表单都走这里，不再用原生 confirm。
   样式全部内联，不占 app.css 与 index.html 的行数预算。 */
'use strict';

/* ---------------------------------------------------------- 弹窗与强确认 */
/* 原生 confirm 太轻：不可撤销的动作（演示重置）要人看清后果、并按字确认。
   样式全部内联，不占 app.css 与 index.html 的行数预算。 */
function bxModalShell(titleText, subText) {
  const mask = document.createElement('div');
  mask.style.cssText = 'position:fixed;inset:0;background:rgba(16,24,40,.45);display:flex;align-items:center;'
    + 'justify-content:center;z-index:9999;padding:24px';
  const box = document.createElement('div');
  box.setAttribute('role', 'dialog');
  box.setAttribute('aria-modal', 'true');
  box.style.cssText = 'background:#fff;border-radius:14px;max-width:520px;width:100%;box-shadow:0 18px 48px rgba(16,24,40,.28);'
    + 'font-size:14px;color:#1f2937;overflow:hidden';
  const head = document.createElement('div');
  head.style.cssText = 'padding:18px 20px 6px;font-size:16px;font-weight:600';
  head.textContent = titleText;
  box.appendChild(head);
  if (subText) {
    const sub = document.createElement('div');
    sub.style.cssText = 'padding:0 20px 12px;color:#5b6577;font-size:12.5px;line-height:1.6';
    sub.textContent = subText;
    box.appendChild(sub);
  }
  const body = document.createElement('div');
  body.style.cssText = 'padding:0 20px 4px;max-height:52vh;overflow:auto';
  box.appendChild(body);
  const foot = document.createElement('div');
  foot.style.cssText = 'display:flex;gap:10px;justify-content:flex-end;padding:14px 20px 18px;border-top:1px solid #eef1f5;margin-top:12px';
  box.appendChild(foot);
  mask.appendChild(box);
  document.body.appendChild(mask);
  document.body.style.overflow = 'hidden';
  const close = function () {
    if (mask.parentNode) { mask.parentNode.removeChild(mask); }
    document.body.style.overflow = '';
    document.removeEventListener('keydown', onKey);
  };
  const onKey = function (ev) { if (ev.key === 'Escape') { close(); } };
  document.addEventListener('keydown', onKey);
  mask.addEventListener('click', function (ev) { if (ev.target === mask) { close(); } });
  return { mask: mask, box: box, body: body, foot: foot, close: close };
}

/* 一行「标题 + 说明」，用于弹窗里陈列后果清单。 */
function bxModalLine(parent, text, tone) {
  const row = document.createElement('div');
  row.style.cssText = 'display:flex;gap:8px;padding:4px 0;line-height:1.6;color:'
    + (tone === 'bad' ? '#b42318' : '#374151');
  const dot = document.createElement('span');
  dot.textContent = tone === 'bad' ? '✕' : '·';
  dot.style.cssText = 'flex:0 0 12px;color:' + (tone === 'bad' ? '#b42318' : '#9aa4b2');
  const txt = document.createElement('span');
  txt.textContent = text;
  row.appendChild(dot);
  row.appendChild(txt);
  parent.appendChild(row);
  return row;
}

/* 强确认：后果清单 + 倒计时感 + 必须按字确认 + 明说「点了会发生什么」。 */
function bxStrongConfirm(opts) {
  const o = opts || {};
  const need = String(o.typed || '').trim();
  const shell = bxModalShell(o.title || '请确认', o.sub || '');
  bxArr(o.lines).forEach(function (line) { bxModalLine(shell.body, line, o.bad ? 'bad' : ''); });
  const warn = document.createElement('div');
  warn.style.cssText = 'margin-top:10px;padding:8px 10px;border-radius:8px;background:#fff7ed;border:1px solid #fed7aa;'
    + 'color:#92400e;font-size:12.5px;line-height:1.6';
  warn.textContent = o.warn || '这个动作不能撤销。';
  shell.body.appendChild(warn);

  let input = null;
  if (need) {
    const label = document.createElement('div');
    label.style.cssText = 'margin-top:12px;font-size:12.5px;color:#374151';
    label.textContent = '要真做，请在下面原样输入「' + need + '」';
    shell.body.appendChild(label);
    input = document.createElement('input');
    input.type = 'text';
    input.setAttribute('aria-label', '按字确认');
    input.style.cssText = 'margin-top:6px;width:100%;padding:8px 10px;border:1px solid #d6dbe3;border-radius:8px;font-size:14px';
    shell.body.appendChild(input);
  }

  const cancel = document.createElement('button');
  cancel.type = 'button';
  cancel.className = 'btn btn-ghost';
  cancel.textContent = o.cancelText || '先不做了';
  cancel.addEventListener('click', shell.close);
  const ok = document.createElement('button');
  ok.type = 'button';
  ok.className = o.bad ? 'btn btn-danger' : 'btn';
  ok.textContent = o.okText || '确认执行';
  ok.disabled = Boolean(need);
  if (need && input) {
    input.addEventListener('input', function () { ok.disabled = input.value.trim() !== need; });
    input.addEventListener('keydown', function (ev) { if (ev.key === 'Enter' && !ok.disabled) { ok.click(); } });
  }
  ok.addEventListener('click', function () {
    shell.close();
    if (typeof o.onOk === 'function') { o.onOk(); }
  });
  shell.foot.appendChild(cancel);
  shell.foot.appendChild(ok);
  if (input) { input.focus(); } else { ok.focus(); }
  return shell;
}

/* 表单弹窗：字段声明式给，提交时把值交回来（步骤表单与其它小表单共用）。 */
function bxFormModal(opts) {
  const o = opts || {};
  const shell = bxModalShell(o.title || '填写', o.sub || '');
  const inputs = {};
  bxArr(o.fields).forEach(function (field) {
    const wrap = document.createElement('label');
    wrap.style.cssText = 'display:block;margin:10px 0 0;font-size:12.5px;color:#374151';
    const cap = document.createElement('div');
    cap.textContent = field.label + (field.hint ? '（' + field.hint + '）' : '');
    wrap.appendChild(cap);
    let control;
    if (field.type === 'select') {
      control = document.createElement('select');
      bxArr(field.options).forEach(function (opt) {
        const node = document.createElement('option');
        node.value = opt.value;
        node.textContent = opt.label;
        control.appendChild(node);
      });
    } else {
      control = document.createElement('input');
      control.type = field.type === 'number' ? 'number' : 'text';
      if (field.value !== undefined) { control.value = String(field.value); }
    }
    control.style.cssText = 'margin-top:4px;width:100%;padding:8px 10px;border:1px solid #d6dbe3;border-radius:8px;font-size:14px';
    wrap.appendChild(control);
    inputs[field.name] = control;
    shell.body.appendChild(wrap);
  });
  const err = document.createElement('div');
  err.style.cssText = 'margin-top:10px;color:#b42318;font-size:12.5px;min-height:16px';
  shell.body.appendChild(err);

  const cancel = document.createElement('button');
  cancel.type = 'button';
  cancel.className = 'btn btn-ghost';
  cancel.textContent = '取消';
  cancel.addEventListener('click', shell.close);
  const ok = document.createElement('button');
  ok.type = 'button';
  ok.className = 'btn';
  ok.textContent = o.okText || '提交';
  ok.addEventListener('click', function () {
    const values = {};
    Object.keys(inputs).forEach(function (k) { values[k] = inputs[k].value.trim(); });
    const problem = typeof o.validate === 'function' ? o.validate(values) : '';
    if (problem) { err.textContent = problem; return; }
    shell.close();
    if (typeof o.onSubmit === 'function') { o.onSubmit(values); }
  });
  shell.foot.appendChild(cancel);
  shell.foot.appendChild(ok);
  return shell;
}
