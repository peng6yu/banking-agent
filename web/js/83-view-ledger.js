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
  return '/api/transactions?' + parts.join('&');
}

function bxLedgerMetaText(data) {
  const lines = [
    data.range && data.range.label ? textOf(data.range.label) : '',
    '共 ' + textOf(data.total) + ' 笔',
    '支出 ' + bxMoney(data, 'out'),
    '收入 ' + bxMoney(data, 'in'),
    '结余 ' + bxMoney(data, 'net')
  ];
  const head = lines.filter(function (line) { return !!line; }).join(' · ');
  const internal = bxNum(data.internal_out_fen);
  if (!internal) { return head; }
  return head + '；另有 ' + bxMoney(data, 'internal_out') +
    '，是转给自己另一张卡或买理财，不算收支。';
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

function bxRenderLedger(data, append) {
  const host = $('ledger-tx');
  const meta = $('ledger-meta');
  const note = $('ledger-note');
  const more = $('ledger-more');
  const key = bxLedgerValue('ledger-q') || bxLedgerValue('ledger-category') ||
    bxLedgerValue('ledger-direction');
  const items = bxArr(data.items);
  if (append && host && host.querySelector('table')) {
    const table = host.querySelector('table');
    const body = table.querySelector('tbody');
    items.forEach(function (tx) {
      const tr = document.createElement('tr');
      bxLedgerRow(tx).forEach(function (cell, index) {
        const td = document.createElement('td');
        if (index === 4) { td.className = 'num'; }
        td.textContent = cell;
        tr.appendChild(td);
      });
      body.appendChild(tr);
    });
  } else if (host) {
    if (!items.length) {
      bxEmpty(host, key ? '这个条件下没有流水' : '这段时间没有流水',
        key ? '换个关键词或清空筛选再看。' : '可以手动补录一笔，或者切到别的月份。');
    } else {
      host.replaceChildren();
      host.appendChild(bxTable(
        ['时间', '对手方 / 商户', '分类', '方向', '金额', '备注'],
        items.map(bxLedgerRow),
        [4]
      ));
    }
  }
  if (meta) { meta.textContent = bxLedgerMetaText(data); }
  if (more) {
    more.disabled = !data.has_more;
    more.textContent = data.has_more
      ? '加载更多（已显示 ' + textOf(data.count) + ' / ' + textOf(data.total) + ' 笔）'
      : '已经到底了';
  }
  if (note) { note.textContent = textOf(data.note); }
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
  return bxLoadLedger(true);
}

function bxLedgerInit() {
  const more = $('ledger-more');
  const reset = $('ledger-reset');
  if (more) { more.addEventListener('click', function () { bxLoadLedger(false); }); }
  if (reset) { reset.addEventListener('click', bxLedgerReset); }
  const direction = $('ledger-direction');
  if (direction) { direction.addEventListener('change', function () { bxLoadLedger(true); }); }
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
