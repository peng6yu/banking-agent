/* 74-view-messages.js — 「计划与提醒」里的「助手提醒」卡片（首页只显示待看条数）。
   读：GET /api/messages（不生成）；想现在就算一轮：POST /api/messages/refresh；
   采纳/忽略/静音：POST /api/messages/{id}/feedback。生成是写动作，所以刷新必须由人点。 */
'use strict';

function bxMessagesTag(data) {
  const tag = $('plan-messages-tag');
  if (!tag) { return; }
  const daily = bxIsObj(data && data.daily) ? data.daily : {};
  const used = Number(daily.used || 0);
  const cap = Number(daily.cap || 3);
  tag.textContent = '今天已说 ' + used + ' 条 · 上限 ' + cap + ' 条';
  tag.className = 'tag ' + (used >= cap ? 'warn' : 'info');
}

function bxMessagesFacts(facts) {
  const box = el('div', 'field-label');
  const keys = facts && typeof facts === 'object' ? Object.keys(facts) : [];
  if (!keys.length) { return null; }
  box.textContent = '依据：' + keys.map(function (k) {
    return k + ' = ' + textOf(facts[k]);
  }).join(' · ');
  return box;
}

function bxMessageAction(btnKey, msg) {
  bxArr(msg.actions).forEach(function (act) {
    if (!bxIsObj(act) || !act.label) { return; }
    const btn = el('button', 'btn btn-mini', textOf(act.label));
    btn.type = 'button';
    btn.addEventListener('click', function () {
      if (act.kind === 'ask' && act.prompt) { bxAsk(act.prompt); return; }
      if (act.kind === 'goto' && act.view) { bxSetView(act.view); return; }
      bxToast('这条提醒没有可执行的下一步，可以先忽略。', 'bad');
    });
    btnKey.appendChild(btn);
  });
}

/* 一条提醒只露一个主按钮，「这次不用 / 这类别再提醒」收进「不用了」里：
   同一屏三条提醒时按钮从九个降到六个，主次也不打架了。 */
function bxMessageFeedback(btnKey, msg) {
  const more = el('div', 'cap-chips');
  more.hidden = true;
  let extra = 0;
  bxArr(msg.feedback_actions).forEach(function (fb) {
    if (!bxIsObj(fb) || !fb.action) { return; }
    const primary = !!fb.primary;
    const btn = el('button', 'btn btn-mini' + (primary ? ' btn-green' : ' btn-ghost'), textOf(fb.label));
    btn.type = 'button';
    btn.addEventListener('click', function () {
      if (btn.disabled) { return; }
      btn.disabled = true;
      bxSend('/api/messages/' + encodeURIComponent(textOf(msg.message_id)) + '/feedback', 'POST', { action: textOf(fb.action) })
        .then(function (data) {
          bxToast('已记下：「' + textOf(fb.label) + '」', 'ok');
          bxRenderMessages(bxIsObj(data) && data.messages ? data.messages : null);
        })
        .catch(function (err) {
          btn.disabled = false;
          bxToast('没记上：' + errorText(err), 'bad');
        });
    });
    if (primary) { btnKey.appendChild(btn); } else { more.appendChild(btn); extra += 1; }
  });
  if (!extra) { return; }
  const toggle = el('button', 'btn btn-mini btn-ghost', '不用了');
  toggle.type = 'button';
  toggle.setAttribute('aria-expanded', 'false');
  toggle.addEventListener('click', function () {
    more.hidden = !more.hidden;
    toggle.setAttribute('aria-expanded', more.hidden ? 'false' : 'true');
  });
  btnKey.appendChild(toggle);
  btnKey.appendChild(more);
}

/* 顶部导航的待看徽标：不点开总览也知道助手有几条话要说。计数就是 status=new 的条数，
   点了「我去处理 / 这次不用 / 这类别再提醒」之后自然减少，不做「已读」这种假状态。 */
function bxMessagesBadge(count) {
  const badge = $('nav-messages-badge');
  if (!badge) { return; }
  const n = Math.max(0, Number(count) || 0);
  badge.textContent = n > 9 ? '9+' : String(n);
  badge.hidden = n === 0;
  badge.setAttribute('aria-label', n ? ('助手提醒待看 ' + n + ' 条') : '没有待看的助手提醒');
}

function bxRenderMessages(data) {
  const host = $('plan-messages');
  if (!host) { return; }
  data = bxIsObj(data) ? data : {};
  bxMessagesTag(data);
  const items = bxArr(data.items);
  const counts = bxIsObj(data.counts) ? data.counts : {};
  bxMessagesBadge(counts.new);
  host.replaceChildren();
  if (!items.length) {
    bxEmpty(host, '现在没有要提醒你的事',
      textOf(data.empty_reason) || '助手每天最多说三条，探不到依据就不说。');
    return;
  }
  const list = el('div', 'list');
  items.forEach(function (msg) {
    if (!bxIsObj(msg)) { return; }
    const box = el('div', 'notice ' + (msg.severity === 'warning' ? 'warn' : 'info'));
    const head = el('div', 'cap-chips');
    head.appendChild(el('strong', '', textOf(msg.title)));
    head.appendChild(el('span', 'tag ' + (msg.severity === 'warning' ? 'warn' : 'info'), textOf(msg.severity === 'warning' ? '值得核实' : '提示')));
    head.appendChild(el('span', 'tag', textOf(msg.status_label)));
    box.appendChild(head);
    box.appendChild(el('div', '', textOf(msg.body)));
    const facts = bxMessagesFacts(msg.facts);
    if (facts) { box.appendChild(facts); }
    const acts = el('div', 'cap-chips');
    bxMessageAction(acts, msg);
    bxMessageFeedback(acts, msg);
    box.appendChild(acts);
    box.appendChild(el('div', 'field-label', textOf(msg.created_text)));
    list.appendChild(box);
  });
  host.appendChild(list);
  const muted = bxArr(data.muted_insights);
  if (muted.length) {
    host.appendChild(el('div', 'card-note', '已经对 ' + muted.length + ' 类提醒说过「别再提醒」，它们不会再出现。'));
  }
}

function bxLoadMessages() {
  const host = $('plan-messages');
  if (!host) { return Promise.resolve(null); }
  bxLoading(host, 2);
  return bxFetch('/api/messages?limit=10').then(function (data) {
    bxRenderMessages(data);
    return data;
  }).catch(function (err) {
    bxError(host, err);
    return null;
  });
}

/* 首页收件箱只需要「有几条没看」，不要把整张卡片拉过来重渲染一遍：
   提醒的正文与反馈按钮只属于「计划与提醒」那一页。 */
function bxMessagesSummary(cb) {
  return bxFetch('/api/messages?limit=1').then(function (data) {
    const counts = bxIsObj(data) && bxIsObj(data.counts) ? data.counts : {};
    if (typeof cb === 'function') { cb(counts); }
    if (typeof bxMessagesBadge === 'function') { bxMessagesBadge(counts.new); }
    return counts;
  }).catch(function () {
    /* 取不到就什么也不显示：收件箱不能因为提醒接口失败而整块空掉。 */
    return null;
  });
}

function bxMessagesRefresh(btn) {
  if (!btn) { return; }
  if (btn.disabled) { return; }
  btn.disabled = true;
  const label = btn.textContent;
  btn.textContent = '正在想…';
  bxSend('/api/messages/refresh', 'POST', {})
    .then(function (data) {
      const created = Number((bxIsObj(data) && data.created) || 0);
      bxToast(created > 0 ? ('新想到 ' + created + ' 条') : '这一轮没有新的可说（同类 7 天内不重复、每天最多三条）', created > 0 ? 'ok' : '');
      bxRenderMessages(bxIsObj(data) && data.messages ? data.messages : null);
    })
    .catch(function (err) {
      bxToast('没想出来：' + errorText(err), 'bad');
    })
    .then(function () {
      btn.disabled = false;
      btn.textContent = label;
    });
}

(0, function () {
  document.addEventListener('DOMContentLoaded', function () {
    const btn = $('plan-messages-refresh');
    if (btn) { btn.addEventListener('click', function () { bxMessagesRefresh(btn); }); }
  });
})();
