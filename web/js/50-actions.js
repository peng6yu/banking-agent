/* 50-actions.js — 发送、确认、取消、审计链与工具清单。 */
'use strict';

async function confirmAction(confirmationId, approve, factor, proofs, sid) {
  if (!confirmationId) {
    appendBubble('sys', '这条确认没有确认号，无法提交。');
    return;
  }
  if (!sessionId) {
    appendBubble('sys', '会话还没建立，无法提交确认。');
    return;
  }
  const body = {
    session_id: sid || sessionId,
    confirmation_id: confirmationId,
    approve: approve === true,
    factor: factor ? String(factor) : null
  };
  if (proofs && proofs.length) { body.proofs = proofs; }
  let result = null;
  try {
    const response = await fetch(API_CONFIRM, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    if (!response.ok) {
      await reportHttpError(response);
      return;
    }
    result = await response.json();
  } catch (err) {
    appendBubble('sys', '提交确认失败：' + errorText(err) + '。请重试。');
    return;
  }

  /* 服务端只回传这一次确认的处理结果，执行进度要另外查：不然用户只会看到「已提交」而看不到结果 */
  const status = textOf(result && result.status);
  if (result && result.idempotent) {
    appendBubble('sys', '这条确认后端已经处理过了（当前状态：' + (BX_TASK_LABEL[status] || status) + '）。重复提交不会产生第二次执行。');
  } else if (status === 'waiting_factor') {
    const need = asList(result && result.required).map(function (key) {
      return BX_FACTOR_TEXT[textOf(key)] || textOf(key);
    }).join('、');
    appendBubble('sys', '这一步还需要验证：' + (need || '后端没有说明具体步骤') + '。右边「多因子验证」里填。');
    renderFactor({ confirmation_id: confirmationId, required: result ? result.required : [] });
    return;
  } else if (status === 'rejected') {
    appendBubble('sys', '已经拒绝，这一轮不会继续执行，后面没有执行的节点会标成跳过。');
  } else if (status === 'approved') {
    appendBubble('sys', approve ? '后端已批准这次授权，正在执行。执行结果以服务端会话状态为准。' : '已提交。');
  } else if (status) {
    appendBubble('sys', '后端返回状态：' + (BX_TASK_LABEL[status] || status) + '。');
  }

  panelBadge('panel-factor', '已提交');
  if (!approve) {
    bxCacheDrop();
    if (bxView !== 'assistant') { bxLoad(bxView, true); }
    return;
  }
  if (streaming) {
    /* 这一轮的 SSE 还开着：确认一落地，执行事件会继续从那条流里推过来。
       这里如果再轮询一次，同一个节点结果会在聊天区出现两遍。 */
    appendBubble('sys', '授权已通过，执行进度会继续从这条流里推过来。');
    return;
  }
  appendBubble('sys', '这条流已经断了（可能刷新过页面），改为直接查服务端会话拿结果。');
  await bxPollSession(sessionId, 8);
}

/* 首页/管理页的「就地验证并批准」：把多因子验证码输入框直接摆在待确认卡下面。
   以前这类卡只有一个「去完成验证」，点了只是往输入框塞一句没有指代的话，
   用户既看不到输入框、也不知道该验证哪一条。验证码仍然由服务端签发与校验。 */
function bxInlineFactor(box, cid, sid, onDone) {
  if (!box) { return; }
  box.replaceChildren();
  box.appendChild(el('div', 'card-note', '正在向服务端申请验证码…'));
  const rowHost = el('div', 'factor-codes');
  const actionBar = el('div', 'factor-actions');
  let challenges = [];

  function paint(items) {
    challenges = bxArr(items);
    rowHost.replaceChildren();
    if (!challenges.length) {
      rowHost.appendChild(el('div', 'card-note', '服务端没有可用的验证挑战，点「重新获取验证码」再试。'));
      return;
    }
    challenges.forEach(function (item) {
      const row = el('div', 'factor-code-row');
      const label = BX_FACTOR_TEXT[textOf(item.factor)] || textOf(item.factor);
      row.appendChild(el('span', 'factor-code-label', label + (item.target ? '（' + textOf(item.target) + '）' : '')));
      const input = document.createElement('input');
      input.type = 'text';
      input.className = 'factor-input';
      input.placeholder = '6 位验证码';
      input.setAttribute('data-challenge', textOf(item.challenge_id));
      if (item.code) { input.value = textOf(item.code); }
      row.appendChild(input);
      if (item.code) { row.appendChild(el('span', 'card-note', '演示验证码已自动填好')); }
      rowHost.appendChild(row);
    });
  }

  function load(btn) {
    if (btn) { btn.disabled = true; }
    box.replaceChildren(el('div', 'card-note', '正在向服务端申请验证码…'));
    box.appendChild(rowHost);
    box.appendChild(actionBar);
    const reload = el('button', 'btn btn-ghost btn-mini', '重新获取验证码');
    reload.type = 'button';
    reload.addEventListener('click', function () { load(reload); });
    actionBar.replaceChildren(reload);
    return bxSend('/api/factor/challenge', 'POST', { session_id: sid, confirmation_id: cid })
      .then(function (res) {
        paint(res && res.challenges);
        if (res && res.note) { rowHost.appendChild(el('div', 'card-note', textOf(res.note))); }
        const okBtn = el('button', 'btn btn-mini btn-green', '提交验证并批准');
        okBtn.type = 'button';
        okBtn.addEventListener('click', function () {
          if (!challenges.length) {
            appendBubble('sys', '还没有可用的验证码，先点「重新获取验证码」。');
            return;
          }
          const proofs = [];
          let missing = 0;
          challenges.forEach(function (item) {
            const input = rowHost.querySelector('input[data-challenge="' + textOf(item.challenge_id) + '"]');
            const code = input ? textOf(input.value).trim() : '';
            if (!code) { missing += 1; return; }
            proofs.push({ factor: textOf(item.factor), challenge_id: textOf(item.challenge_id), code: code });
          });
          if (missing) {
            appendBubble('sys', '还有 ' + missing + ' 个验证码没填，服务端会拒绝这次提交。');
            return;
          }
          okBtn.disabled = true;
          Promise.resolve(confirmAction(cid, true, null, proofs, sid)).then(function () {
            box.replaceChildren();
            if (typeof onDone === 'function') { onDone(); }
          }, function () { okBtn.disabled = false; });
        });
        const noBtn = el('button', 'btn btn-mini btn-red', '拒绝这次操作');
        noBtn.type = 'button';
        noBtn.addEventListener('click', function () {
          noBtn.disabled = true;
          Promise.resolve(confirmAction(cid, false, null, null, sid)).then(function () {
            box.replaceChildren();
            if (typeof onDone === 'function') { onDone(); }
          }, function () { noBtn.disabled = false; });
        });
        actionBar.appendChild(okBtn);
        actionBar.appendChild(noBtn);
      })
      .catch(function (err) {
        box.replaceChildren(el('div', 'card-note', '获取验证码失败：' + pfErrText(err)));
        if (btn) { btn.disabled = false; }
      });
  }

  load(null);
}

async function cancelSession() {
  if (!sessionId) {
    appendBubble('sys', '当前没有进行中的会话。');
    return;
  }
  try {
    const response = await fetch(API_CANCEL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId })
    });
    if (!response.ok) {
      await reportHttpError(response);
      return;
    }
    appendBubble('sys', '已发出中断请求，后端会释放未完成的占位。');
  } catch (err) {
    appendBubble('sys', '中断失败：' + errorText(err) + '。请重试。');
  }
}

async function pullAudit() {
  if (!sessionId) {
    appendBubble('sys', '还没有会话号，先发一句话再拉审计链。');
    return;
  }
  try {
    const response = await fetch(API_AUDIT + '?session_id=' + encodeURIComponent(sessionId));
    if (!response.ok) {
      await reportHttpError(response);
      return;
    }
    const json = await response.json();
    let rows = [];
    if (Array.isArray(json)) { rows = json; }
    else if (json && Array.isArray(json.items)) { rows = json.items; }
    else if (json && Array.isArray(json.rows)) { rows = json.rows; }
    rows.forEach(function (row) {
      const box = row && typeof row === 'object' ? row : { event_type: textOf(row) };
      renderAudit({
        seq: fieldVal(box, ['seq']),
        event_type: fieldVal(box, ['event_type', 'type', 'event']),
        hash: fieldVal(box, ['hash', 'entry_hash']),
        ts_ms: fieldVal(box, ['ts_ms'])
      });
    });
    if (!rows.length) {
      appendBubble('sys', '后端没有返回审计条目。');
    }
  } catch (err) {
    appendBubble('sys', '拉取审计链失败：' + errorText(err) + '。请重试。');
  }
}

async function verifyAudit() {
  try {
    const response = await fetch(API_AUDIT_VERIFY);
    if (!response.ok) {
      await reportHttpError(response);
      return;
    }
    const json = await response.json();
    const ok = json && json.ok === true;
    appendBubble('sys',
      (ok ? '审计链校验通过。' : '审计链校验没通过。') +
      ' 条目数 ' + textOf(json && json.count) +
      '，链头 ' + shortHash(json && json.head));
  } catch (err) {
    appendBubble('sys', '校验失败：' + errorText(err) + '。请重试。');
  }
}

async function loadTools() {
  const host = $('cap-list');
  if (!host) { return; }
  try {
    const response = await fetch(API_TOOLS);
    if (!response.ok) {
      host.replaceChildren(el('span', 'cap-chip', '工具清单暂时拿不到'));
      return;
    }
    const json = await response.json();
    let rows = [];
    if (Array.isArray(json)) { rows = json; }
    else if (json && Array.isArray(json.tools)) { rows = json.tools; }
    host.replaceChildren();
    if (!rows.length) {
      host.appendChild(el('span', 'cap-chip', '工具清单为空'));
      return;
    }
    rows.forEach(function (row) {
      const box = row && typeof row === 'object' ? row : { name: textOf(row) };
      const name = fieldVal(box, ['name', 'tool', 'title']);
      const desc = fieldVal(box, ['description', 'desc', 'summary']);
      // 折叠区里只放工具名：展开时也不该把 33 段说明书铺满整屏，说明放 title。
      const chip = el('span', 'cap-chip');
      chip.title = desc || name;
      chip.textContent = name;
      host.appendChild(chip);
    });
  } catch (err) {
    host.replaceChildren(el('span', 'cap-chip', '工具清单暂时拿不到'));
  }
}

/* ------------------------------------------------------ 主动财务（模拟数据）
   全部真实调用后端：储蓄目标 CRUD、手动监测、提醒处理、模拟支出注入。
   金额一律用后端的 *fen 整数，前端只做展示换算，不重算业务口径。 */
