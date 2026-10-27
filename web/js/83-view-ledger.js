/* 流水明细：按当前卡与时间段翻全量流水（合计 + 方向/分类/关键词过滤 + 分页）。
   账单页的「异常相关流水」只列命中规则的证据流水，不是对账单；用户想找某一笔
   或者确认「这个月到底花了哪些」时，需要的是这一块。口径与上方统计区一致：
   支出/收入/结余都不含账户互转与理财，内部搬运单独交代一句。 */

let bxLedgerOffset = 0;
const bxLedgerLimit = 30;
let bxLedgerLoading = false;

function bxLedgerValue(id) {
  const node = $(id);
  return node ? String(node.value || '').trim() : '';
}

function bxLedgerUrl(offset) {
  const parts = [
    'period=' + encodeURIComponent(bxBillPeriod),
    'limit=' + bxLedgerLimit,
    'offset=' + offset
  ];
  if (bxBillAccount) { parts.push('account_id=' + encodeURIComponent(bxBillAccount)); }
  const direction = bxLedgerValue('ledger-direction');
  if (direction) { parts.push('direction=' + encodeURIComponent(direction)); }
  const category = bxLedgerValue('ledger-category');
  if (category) { parts.push('category=' + encodeURIComponent(category)); }
  const keyword = bxLedgerValue('ledger-q');
  if (keyword) { parts.push('q=' + encodeURIComponent(keyword)); }
  const flagged = $('ledger-flagged');
  if (flagged && flagged.checked) { parts.push('flagged=1'); }
  return '/api/transactions?' + parts.join('&');
}

const BX_LEDGER_HEADERS = ['时间', '对手方 / 商户', '分类', '方向', '金额', '备注'];

function bxLedgerMetaText(data) {
  const lines = [
    data.range && data.range.label ? textOf(data.range.label) : '',
    '共 ' + textOf(data.total) + ' 笔',
    '支出 ' + bxMoney(data, 'out'),
    '收入 ' + bxMoney(data, 'in'),
    '结余 ' + bxMoney(data, 'net')
  ];
  const only = bxIsObj(data.filters) && data.filters.flagged ? '只看命中账单规则的笔 · ' : '';
  const head = only + lines.filter(function (line) { return !!line; }).join(' · ');
  let text = '';
  if (bxNum(data.internal_out_fen)) {
    text += '；另有 ' + bxMoney(data, 'internal_out') +
      '，是转给自己另一张卡或买理财，不算收支';
  }
  if (bxNum(data.anomaly_count)) {
    text += '；标红的 ' + textOf(data.anomaly_count) + ' 笔命中了账单规则，点开看是哪条';
  }
  return text ? head + text + '。' : head;
}

function bxLedgerRow(tx) {
  return [
    textOf(tx.ts_text),
    textOf(tx.counterparty),
    textOf(tx.category),
    textOf(tx.direction) === 'in' ? '收入' : '支出',
    bxMoney(tx, 'amount'),
    textOf(tx.note)
  ];
}

function bxLedgerLine(box, text, className) {
  if (!text) { return; }
  const line = document.createElement('div');
  if (className) { line.className = className; }
  line.textContent = text;
  box.appendChild(line);
}

function bxLedgerDetail(tx) {
  const anomaly = tx.anomaly || {};
  const box = document.createElement('div');
  box.className = 'notice warn';
  const head = document.createElement('div');
  const title = document.createElement('strong');
  title.textContent = textOf(anomaly.title) || '命中一条账单规则';
  head.appendChild(title);
  const label = [
    textOf(anomaly.rule_id),
    anomaly.confidence_label ? '命中把握 ' + textOf(anomaly.confidence_label) : ''
  ].filter(function (part) { return !!part; }).join(' · ');
  if (label) {
    head.appendChild(document.createTextNode(' '));
    const tag = document.createElement('span');
    tag.className = 'tag warn';
    tag.textContent = label;
    head.appendChild(tag);
  }
  box.appendChild(head);
  bxLedgerLine(box, textOf(anomaly.detail));
  if (bxNum(anomaly.tx_count) > 1) {
    bxLedgerLine(box, '这一组共 ' + textOf(anomaly.tx_count) + ' 笔，表格里标红的就是它们。');
  }
  bxLedgerLine(box, anomaly.possible_false_positive
    ? '可能的误报：' + textOf(anomaly.possible_false_positive) : '');
  bxLedgerLine(box, '规则命中只说明值得核实，不是欺诈认定。', 'field-label');
  return box;
}

function bxLedgerTr(tx) {
  const flagged = !!(tx && tx.anomaly);
  const tr = document.createElement('tr');
  tr.className = 'tx-row' + (flagged ? ' is-flagged' : '');
  bxLedgerRow(tx).forEach(function (cell, index) {
    const td = document.createElement('td');
    if (index === 4) { td.className = 'num'; }
    td.textContent = cell;
    tr.appendChild(td);
  });
  if (flagged) {
    tr.tabIndex = 0;
    tr.setAttribute('aria-expanded', 'false');
    tr.title = '点开看这条属于哪条异常';
  }
  return tr;
}

function bxLedgerPair(tx) {
  const row = bxLedgerTr(tx);
  const detail = document.createElement('tr');
  detail.className = 'tx-detail';
  detail.hidden = true;
  detail.setAttribute('aria-hidden', 'true');
  const td = document.createElement('td');
  td.colSpan = BX_LEDGER_HEADERS.length;
  td.appendChild(bxLedgerDetail(tx));
  detail.appendChild(td);
  return { row: row, detail: detail };
}

function bxLedgerTable(items) {
  const table = document.createElement('table');
  table.className = 'table';
  const headRow = document.createElement('tr');
  BX_LEDGER_HEADERS.forEach(function (text) {
    const th = document.createElement('th');
    th.textContent = text;
    headRow.appendChild(th);
  });
  const thead = document.createElement('thead');
  thead.appendChild(headRow);
  const body = document.createElement('tbody');
  items.forEach(function (tx) {
    const pair = bxLedgerPair(tx);
    body.appendChild(pair.row);
    body.appendChild(pair.detail);
  });
  table.appendChild(thead);
  table.appendChild(body);
  return table;
}

function bxLedgerToggle(tr) {
  const detail = tr.nextElementSibling;
  if (!detail || !detail.classList.contains('tx-detail')) { return; }
  const open = detail.hidden;
  detail.hidden = !open;
  detail.setAttribute('aria-hidden', open ? 'false' : 'true');
  tr.setAttribute('aria-expanded', open ? 'true' : 'false');
}

function bxRenderLedger(data, append) {
  const host = $('ledger-tx');
  const meta = $('ledger-meta');
  const note = $('ledger-note');
  const more = $('ledger-more');
  const key = bxLedgerValue('ledger-q') || bxLedgerValue('ledger-category') ||
    bxLedgerValue('ledger-direction');
  const items = bxArr(data.items);
  const body = append && host ? host.querySelector('table tbody') : null;
  if (body) {
    items.forEach(function (tx) {
      const pair = bxLedgerPair(tx);
      body.appendChild(pair.row);
      body.appendChild(pair.detail);
    });
  } else if (host) {
    if (!items.length) {
      bxEmpty(host, key ? '这个条件下没有流水' : '这段时间没有流水',
        key ? '换个关键词或清空筛选再看。' : '可以手动补录一笔，或者切到别的月份。');
    } else {
      host.replaceChildren();
      host.appendChild(bxLedgerTable(items));
    }
  }
  if (meta) { meta.textContent = bxLedgerMetaText(data); }
  if (more) {
    more.disabled = !data.has_more;
    more.textContent = data.has_more
      ? '加载更多（已显示 ' + textOf(data.count) + ' / ' + textOf(data.total) + ' 笔）'
      : '已经到底了';
  }
  if (note) {
    note.textContent = [textOf(data.note), textOf(data.anomaly_note)]
      .filter(function (part) { return !!part; }).join(' ');
  }
}

function bxLoadLedger(reset) {
  if (bxLedgerLoading) { return Promise.resolve(null); }
  const offset = reset ? 0 : bxLedgerOffset;
  const host = $('ledger-tx');
  if (reset && host) { host.replaceChildren(); }
  bxLedgerLoading = true;
  return bxFetch(bxLedgerUrl(offset)).then(function (data) {
    bxLedgerLoading = false;
    bxLedgerOffset = offset + bxArr(data.items).length;
    bxRenderLedger(data, !reset);
    return data;
  }).catch(function (err) {
    bxLedgerLoading = false;
    if (host) { bxError(host, err); }
    if (reset && $('ledger-meta')) { $('ledger-meta').textContent = '流水没取到。'; }
    return null;
  });
}

function bxLedgerReset() {
  ['ledger-direction', 'ledger-category', 'ledger-q'].forEach(function (id) {
    const node = $(id);
    if (node) { node.value = ''; }
  });
  const flagged = $('ledger-flagged');
  if (flagged) { flagged.checked = false; }
  return bxLoadLedger(true);
}

function bxLedgerInit() {
  const more = $('ledger-more');
  const reset = $('ledger-reset');
  if (more) { more.addEventListener('click', function () { bxLoadLedger(false); }); }
  if (reset) { reset.addEventListener('click', bxLedgerReset); }
  const host = $('ledger-tx');
  if (host) {
    host.addEventListener('click', function (event) {
      const tr = event.target.closest('tr.tx-row.is-flagged');
      if (tr) { bxLedgerToggle(tr); }
    });
    host.addEventListener('keydown', function (event) {
      if (event.key !== 'Enter' && event.key !== ' ') { return; }
      const tr = event.target.closest('tr.tx-row.is-flagged');
      if (!tr) { return; }
      event.preventDefault();
      bxLedgerToggle(tr);
    });
  }
  const direction = $('ledger-direction');
  if (direction) { direction.addEventListener('change', function () { bxLoadLedger(true); }); }
  const flagged = $('ledger-flagged');
  if (flagged) { flagged.addEventListener('change', function () { bxLoadLedger(true); }); }
  ['ledger-category', 'ledger-q'].forEach(function (id) {
    const node = $(id);
    if (!node) { return; }
    node.addEventListener('keydown', function (event) {
      if (event.key === 'Enter') { event.preventDefault(); bxLoadLedger(true); }
    });
    node.addEventListener('change', function () { bxLoadLedger(true); });
  });
}

(0, function () {
  document.addEventListener('DOMContentLoaded', bxLedgerInit);
})();
