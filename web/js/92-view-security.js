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
      '确认卡片与会话记录落在服务端表里，刷新页面不会丢；但正在推进的执行状态保存在进程内存里，服务重启后旧的确认无法继续，需要重新发起。'));
  }
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
