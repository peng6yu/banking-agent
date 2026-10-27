/* 92-view-security.js — 执行记录与安全中心。 */
'use strict';

/* --------------------------------------------------- 执行记录与安全中心 */
function bxRenderSecurity(data) {
  data = bxIsObj(data) ? data : {};
  const sessions = bxArr(data.sessions);
  const denials = bxArr(data.denials);
  const audit = bxIsObj(data.audit) ? data.audit : {};

  const host = $('sec-summary');
  if (host) {
    host.replaceChildren();
    host.appendChild(bxStatCard('最近会话', bxCount(sessions.length, ' 个'), '来自服务端会话表'));
    host.appendChild(bxStatCard('被拒绝的操作', bxCount(denials.length, ' 次'), '授权缺失、策略拦截、注入命中'));
    host.appendChild(bxStatCard('审计链', audit.chain_ok === true ? '校验通过' : (audit.chain_ok === false ? '校验失败' : BX_NA),
      '共 ' + bxCount(audit.count, ' 条') + (audit.head ? ' · 链头 ' + shortHash(audit.head) : '')));
  }

  setText($('sec-session-tag'), bxCount(sessions.length, ' 个'));

  const sessionHost = $('sec-sessions');
  if (sessionHost) {
    if (!sessions.length) {
      bxEmpty(sessionHost, '还没有执行记录', textOf(data.empty_reason));
    } else {
      sessionHost.replaceChildren();
      sessions.forEach(function (session) {
        const box = el('div', 'anomaly');
        const head = el('div', 'anomaly-head');
        head.appendChild(el('span', 'anomaly-title', textOf(session.title) || textOf(session.session_id)));
        head.appendChild(el('span', 'tag ' + (textOf(session.status) === 'done' ? 'ok' : (textOf(session.status) === 'cancelled' ? 'bad' : 'info')), textOf(session.status_label) || textOf(session.status)));
        box.appendChild(head);
        box.appendChild(kvRow('会话号', textOf(session.session_id), true));
        box.appendChild(kvRow('时间', textOf(session.created_text) + ' → ' + textOf(session.last_text)));
        const nodes = bxArr(session.nodes);
        if (nodes.length) {
          /* 同一个会话里可能执行过好几轮：以前所有轮的节点平铺成一张表，
             看到的是「n1 成功 / n3 等待 / n2 等待 / n2 成功 / n3 成功」这种
             时间倒错的混排，根本分不清哪几行属于同一次请求。按 run_id 分组，
             每组标上轮次与时间，顺序才是执行顺序。 */
          const groups = [];
          const groupIndex = {};
          nodes.forEach(function (node) {
            const rid = textOf(node.run_id) || '未知轮次';
            if (!(rid in groupIndex)) {
              groupIndex[rid] = groups.length;
              groups.push({ run_id: rid, nodes: [] });
            }
            groups[groupIndex[rid]].nodes.push(node);
          });
          groups.forEach(function (group, order) {
            const first = group.nodes[0] || {};
            box.appendChild(el('div', 'field-label',
              '第 ' + (groups.length - order) + ' 轮 · ' + group.run_id +
              (first.ts_text ? ' · ' + textOf(first.ts_text) : '') +
              ' · 共 ' + group.nodes.length + ' 个节点'));
            box.appendChild(bxTable(
              ['节点', '工具', '状态', '裁决', '结果'],
              group.nodes.map(function (node) {
                return [
                  textOf(node.node_id),
                  textOf(node.tool),
                  textOf(node.state_label) || BX_TASK_LABEL[textOf(node.state)] || textOf(node.state),
                  node.level ? (levelText(node.level) + (node.rule_id ? ' · ' + textOf(node.rule_id) : '')) : '',
                  textOf(node.summary)
                ];
              })
            ));
          });
          box.appendChild(el('div', 'card-note', '同一会话里执行过几轮就分几段列，每段的顺序就是这一轮的执行顺序。'));
        } else {
          box.appendChild(el('div', 'card-note', '这条会话没有节点记录。'));
        }
        const confs = bxArr(session.confirmations);
        if (confs.length) {
          box.appendChild(el('div', 'field-label', '确认卡片'));
          const list = el('div', 'list');
          confs.forEach(function (conf) {
            list.appendChild(bxRow(
              textOf(conf.node_id) + ' · ' + (textOf(conf.status_label) || textOf(conf.status)),
              '创建 ' + textOf(conf.created_text) + ' · 到期 ' + textOf(conf.expires_text) + (conf.resolved_text ? ' · 处理 ' + textOf(conf.resolved_text) : ''),
              ''
            ));
          });
          box.appendChild(list);
        }
        const bar = el('div', 'anomaly-actions');
        const auditBtn = el('button', 'btn btn-mini', '看这条会话的审计');
        auditBtn.type = 'button';
        auditBtn.addEventListener('click', function () { bxAuditFor(session.session_id); });
        bar.appendChild(auditBtn);
        box.appendChild(bar);
        sessionHost.appendChild(box);
      });
      const labels = bxIsObj(data.state_labels) ? data.state_labels : {};
      const legend = Object.keys(labels).map(function (key) { return textOf(key) + '=' + textOf(labels[key]); }).join(' / ');
      if (legend) { sessionHost.appendChild(el('div', 'card-note', '任务状态口径：' + legend)); }
    }
  }

  setText($('sec-denial-tag'), bxCount(denials.length, ' 次'));
  const denialHost = $('sec-denials');
  if (denialHost) {
    if (!denials.length) {
      bxEmpty(denialHost, '没有被拒绝的操作', '这个区间里没有触发防线。');
    } else {
      denialHost.replaceChildren();
      denialHost.appendChild(bxTable(
        ['时间', '原因', '会话'],
        denials.map(function (item) { return [textOf(item.ts_text), textOf(item.reason), textOf(item.session_id)]; })
      ));
    }
  }

  const note = $('sec-note');
  if (note) {
    note.replaceChildren();
    const lines = [
      textOf(audit.append_only),
      '所以链断了是能被发现的：任何人改了历史事件，后面的哈希都对不上。'
    ];
    lines.forEach(function (line) {
      if (line) { note.appendChild(el('div', 'card-note', line)); }
    });
    note.appendChild(el('div', 'card-note',
      '确认卡片与会话记录落在服务端表里，刷新页面不会丢；正在推进的执行状态保存在进程内存里，' +
      '服务重启后页面会先试着把它接回来（POST /api/session/{会话号}/recover），接不回来才需要重新发起。'));
  }

  /* 这三块各自独立取数：自检和工单队列不该跟「执行记录」绑在一次刷新里失败。 */
  bxLoadHealth();
  bxLoadHandoffs();
  bxRenderDemoConsole();
}

function bxAuditFor(sessionIdValue) {
  if (!sessionIdValue) { return Promise.resolve(null); }
  return bxFetch('/api/audit?session_id=' + encodeURIComponent(sessionIdValue)).then(function (json) {
    const events = bxArr(json && json.events);
    events.forEach(function (row) {
      renderAudit({
        seq: row.seq,
        event_type: row.event_type,
        hash: row.hash || (row.payload && row.payload.hash),
        ts_ms: row.ts_ms
      });
    });
    panelBadge('panel-audit', events.length + ' 条');
    const panel = $('panel-audit');
    if (panel) { panel.open = true; }
    if (!events.length) { appendBubble('sys', '这条会话没有审计条目。'); }
    return json;
  }).catch(function (err) {
    appendBubble('sys', '拉这条会话的审计失败：' + errorText(err));
  });
}


/* ------------------------------------------------- 系统自检（GET /api/health） */
/* 赛题现场最怕「跑的不是真身」：模型名、工具数、库状态、内存会话数、服务端时间
   全在这一屏，评委一眼能核。取不到就直说取不到，不拿上一次的值顶着。 */
async function bxLoadHealth() {
  const host = $('sec-health');
  if (!host) { return; }
  const tag = $('sec-health-tag');
  try {
    const res = await bxFetch('/api/health');
    const rows = [
      ['探活', textOf(res.status) || 'ok'],
      ['模型', textOf(res.model) || BX_NA],
      ['可用工具', bxCount(res.tools, ' 个')],
      ['数据库', res.db === true ? '连得上' : '连不上'],
      ['内存里的会话', bxCount(res.sessions, ' 个')],
      ['服务端时间', textOf(res.now_text) || BX_NA]
    ];
    host.replaceChildren();
    rows.forEach(function (row) { host.appendChild(kvRow(row[0], row[1])); });
    if (tag) { tag.textContent = res.ok === true ? '正常' : '异常'; }
  } catch (err) {
    bxEmpty(host, '自检没通过', errorText(err));
    if (tag) { tag.textContent = '取不到'; }
  }
}

/* 不同状态下能做什么：还没人认领只能认领或拒绝；认领之后才谈得上继续/补充/取消。
   后端也会再校验一次状态机，这里只是别把做不到的按钮摆在人眼前。 */
function handoffActionsFor(status) {
  if (status === 'pending') { return [['claim', '我来处理'], ['reject', '拒绝这单']]; }
  if (status === 'claimed' || status === 'in_progress') {
    return [['resume', '处理完，继续跑'], ['need-info', '需要本人补充'], ['cancel', '取消这单']];
  }
  return [];
}

/* 要写备注的动作走表单弹窗；认领是一步动作，直接发。 */
function bxHandoffDo(item, action, btn) {
  const hid = textOf(item && item.handoff_id);
  if (!hid) { return; }
  const run = function (note) {
    if (btn) { btn.disabled = true; }
    bxHandoffPost(item, action, note || '').then(function () {
      if (typeof bxToast === 'function') { bxToast('做完了：' + hid, 'ok'); }
      bxLoadHandoffs();
      bxLoad('security', true);
    }).catch(function (err) {
      if (typeof bxToast === 'function') { bxToast('没做成：' + errorText(err), 'bad'); }
      if (btn) { btn.disabled = false; }
    });
  };
  if (action === 'claim') { run(''); return; }
  bxFormModal({
    title: '这一单要写一句处理说明',
    sub: '说明会记在工单上，用户与审计都能看到；空着也能提交，但写清楚更省事。',
    okText: '提交',
    fields: [{ name: 'note', label: '处理说明', hint: '例如「收款人不唯一，已跟客户确认是同事张三」', value: '' }],
    onSubmit: function (values) { run(values.note); }
  });
}

/* ---------------------------------------- 人工接管队列（GET /api/handoffs） */
/* 这是服务端工单表的原始列表，和「最近的执行」里的会话视角是同一批数据。
   以前只渲染会话视角：一条都没跑过的时候，队列看起来是空的，其实库里可能还挂着单。 */
async function bxLoadHandoffs() {
  const host = $('sec-handoff-list');
  if (!host) { return; }
  const tag = $('sec-handoff-tag');
  try {
    const res = await bxFetch('/api/handoffs?limit=20');
    const items = bxArr(res && res.handoffs);
    host.replaceChildren();
    if (tag) { tag.textContent = bxCount(res && res.total, ' 张') + (items.length < Number(res && res.total) ? '（显示最近 20）' : ''); }
    if (!items.length) {
      bxEmpty(host, '队列里没有工单', '需要人工接管的活会出现在这里；智能体能自己办完的不会挂单。');
      return;
    }
    items.forEach(function (item) {
      const box = el('div', 'anomaly');
      const head = el('div', 'anomaly-head');
      head.appendChild(el('span', 'anomaly-title', textOf(item.reason_label) || textOf(item.handoff_id)));
      head.appendChild(el('span', 'tag ' + (textOf(item.status) === 'pending' ? 'warn' : 'info'),
        textOf(item.status_label) || textOf(item.status)));
      box.appendChild(head);
      box.appendChild(kvRow('用户原话', textOf(item.request_text) || '（没留原话）'));
      if (textOf(item.failure_text)) { box.appendChild(kvRow('卡在哪', textOf(item.failure_text))); }
      box.appendChild(kvRow('单号', textOf(item.handoff_id), true));
      box.appendChild(kvRow('时间', textOf(item.created_text) || BX_NA));
      if (textOf(item.operator)) { box.appendChild(kvRow('处理人', textOf(item.operator))); }
      if (textOf(item.note)) { box.appendChild(kvRow('备注', textOf(item.note))); }
      /* 动作就地做完：以前队列只能看，真要处理还得回会话页找那张卡。
         「谁在办」也写清楚——claim 只有在没人认领时才成功，并发只会有一个赢。 */
      const bar = el('div', 'cap-chips');
      const more = el('button', 'btn btn-ghost btn-mini', '看这张单的详情');
      more.type = 'button';
      more.addEventListener('click', function () { bxHandoffDetail(textOf(item.handoff_id), more); });
      bar.appendChild(more);
      bxArr(handoffActionsFor(textOf(item.status))).forEach(function (pair) {
        const btn = el('button', 'btn btn-mini' + (pair[0] === 'cancel' || pair[0] === 'reject' ? ' btn-red' : ''), pair[1]);
        btn.type = 'button';
        btn.addEventListener('click', function () { bxHandoffDo(item, pair[0], btn); });
        bar.appendChild(btn);
      });
      box.appendChild(bar);
      host.appendChild(box);
    });
  } catch (err) {
    bxEmpty(host, '队列取不到', errorText(err));
    if (tag) { tag.textContent = '取不到'; }
  }
}

/* 单张工单详情：DAG、失败节点、裁决、授权快照都在单张里，列表上不给。 */
async function bxHandoffDetail(handoffId, btn) {
  const host = $('sec-handoff-detail');
  if (!host || !handoffId) { return; }
  if (btn) { btn.disabled = true; }
  try {
    const res = await bxFetch('/api/handoffs/' + encodeURIComponent(handoffId));
    const one = bxIsObj(res && res.handoff) ? res.handoff : {};
    host.replaceChildren();
    const box = el('div', 'anomaly');
    box.appendChild(el('div', 'anomaly-head')); /* 占位，下面按字段填 */
    box.firstChild.appendChild(el('span', 'anomaly-title', '工单 ' + handoffId));
    box.firstChild.appendChild(el('span', 'tag info', textOf(one.status_label) || textOf(one.status) || BX_NA));
    const nodes = bxArr(one.nodes);
    if (nodes.length) {
      box.appendChild(bxTable(['节点', '工具', '状态', '裁决'], nodes.map(function (node) {
        return [textOf(node.node_id), textOf(node.tool), textOf(node.state_label) || textOf(node.state),
          textOf(node.level) + (textOf(node.rule_id) ? ' · ' + textOf(node.rule_id) : '')];
      })));
    }
    const fields = [
      ['用户原话', textOf(one.request_text)],
      ['需要的操作', textOf(one.reason_label) || textOf(one.reason)],
      ['会话号', textOf(one.session_id)],
      ['轮次', textOf(one.run_id)],
      ['处理人', textOf(one.operator)],
      ['备注', textOf(one.note)]
    ];
    fields.forEach(function (row) { if (row[1]) { box.appendChild(kvRow(row[0], row[1], row[0] === '会话号' || row[0] === '轮次')); } });
    box.appendChild(el('div', 'card-note',
      '这张单本身不动钱：领取、拒绝、要补充信息、取消都要走操作员动作，钱只在用户本人确认后才动。'));
    host.replaceChildren(box);
  } catch (err) {
    bxEmpty(host, '详情取不到', errorText(err));
  } finally {
    if (btn) { btn.disabled = false; }
  }
}

/* --------------------------------------------------- 演示台（POST /api/demo/reset） */
/* 两场演示之间必须能复位：收回智能体动过的钱、清掉预授权占位与额度账、收掉进行中的会话。
   这是不可撤销动作，所以按钮写清后果、点一次问一次，不提供「以后都不问」。 */
function bxRenderDemoConsole() {
  const host = $('sec-demo');
  if (!host) { return; }
  host.replaceChildren();
  host.appendChild(el('div', 'card-note',
    '两场演示之间用它复位：收回智能体动过的钱、清掉预授权占位与当日额度账、收掉进行中的会话，再重新播种子数据。' +
    '人工编辑过的数据不会被改写；执行记录与审计链保留（审计本来就该留痕）。'));
  const btn = el('button', 'btn btn-red btn-mini', '重置演示数据（不可撤销）');
  btn.type = 'button';
  btn.addEventListener('click', function () {
    if (btn.disabled) { return; }
    /* 原生 confirm 太轻：这是删掉演示现场状态的动作，要人按字确认。
       文案只列后端真会做的事（收回资金 / 清占位与额度账 / 收会话 / 重新播种），不夸大。 */
    bxStrongConfirm({
      title: '重置演示数据？',
      sub: '两场演示之间的复位键。做完就回不到刚才的样子。',
      lines: [
        '收回智能体动过的钱，账户余额回到种子值',
        '清掉预授权占位与当日额度账（1000 元分档重算）',
        '收掉进行中的会话：正在等的确认与验证一并作废',
        '重新播种子数据；执行记录与审计链保留（审计本来就该留痕）'
      ],
      warn: '人工改过的数据不会被改写，但这个动作本身不能撤销。',
      typed: '重置',
      bad: true,
      okText: '按字确认，执行重置',
      cancelText: '先不做了',
      onOk: function () { bxDemoReset(btn); }
    });
  });
  host.appendChild(btn);
}

async function bxDemoReset(btn) {
  const out = $('sec-demo-out');
  const tag = $('sec-demo-tag');
  if (btn) { btn.disabled = true; }
  if (tag) { tag.textContent = '重置中…'; }
  try {
    const res = await bxSend('/api/demo/reset', 'POST', { reason: '界面演示台手动重置' });
    /* 后端只回 reason / account_id / balance_fen：钱冲回到哪个账户、回到多少，
       照它说，不编「释放了几条占位」这种没返回的数字。 */
    const parts = [];
    if (res && textOf(res.account_id)) {
      parts.push('账户 ' + textOf(res.account_id) + ' 余额回到 ' + pfYuanFromFen(res.balance_fen) + ' 元');
    }
    if (res && textOf(res.reason)) { parts.push('原因：' + textOf(res.reason)); }
    if (out) {
      out.replaceChildren();
      out.appendChild(el('div', 'card-note', '已重置：' + (parts.length ? parts.join(' · ') : '服务端已执行重置') + '。'));
    }
    if (tag) { tag.textContent = '已重置'; }
    bxCacheDrop();
    sessionId = '';
    try { window.sessionStorage.removeItem(BX_SESSION_KEY); } catch (err) { /* 隐私模式下没有 sessionStorage */ }
    appendBubble('sys', '演示数据已重置：' + (parts.length ? parts.join(' · ') : '服务端已执行重置') + '。前面的对话记录还在，但服务端的执行状态已经清空，接着说一句话就是新的一轮。', 'demo-reset');
    bxLoad('security', true);
  } catch (err) {
    if (out) { out.replaceChildren(); out.appendChild(el('div', 'card-note', '重置失败：' + errorText(err))); }
    if (tag) { tag.textContent = '重置失败'; }
  } finally {
    if (btn) { btn.disabled = false; }
  }
}
