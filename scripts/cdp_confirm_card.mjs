#!/usr/bin/env node
/* 用 CDP 直接驱动本机 Chromium：打开真页面 → 在页面里喂一帧假确认卡 → 截图。
   用途：确认卡这类「只在有动作时才出现」的界面，没有 playwright 也能看成什么样。
   用法：node scripts/cdp_confirm_card.mjs <base_url> <token> <out_png> */
import { spawn } from 'node:child_process';
import { writeFileSync } from 'node:fs';

const BASE = process.argv[2] || 'http://127.0.0.1:8123';
const TOKEN = process.argv[3] || '';
const OUT = process.argv[4] || 'tmp/confirm_card.png';
const CHROME = process.env.CHROME_BIN
  || '/home/peng/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome';

const child = spawn(CHROME, [
  '--headless=new', '--disable-gpu', '--no-sandbox', '--hide-scrollbars',
  '--remote-debugging-port=9333', '--window-size=1200,900', 'about:blank'
], { stdio: 'ignore' });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function targets() {
  for (let i = 0; i < 40; i += 1) {
    try {
      const res = await fetch('http://127.0.0.1:9333/json/list');
      const list = await res.json();
      const page = list.find((t) => t.type === 'page');
      if (page) return page;
    } catch (err) { /* 还没起来 */ }
    await sleep(250);
  }
  throw new Error('CDP 没起来');
}

const page = await targets();
const ws = new WebSocket(page.webSocketDebuggerUrl);
let seq = 0;
const pending = new Map();
ws.addEventListener('message', (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
});
await new Promise((r) => ws.addEventListener('open', r));
const send = (method, params = {}) => new Promise((resolve, reject) => {
  seq += 1;
  const id = seq;
  pending.set(id, (msg) => (msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result)));
  ws.send(JSON.stringify({ id, method, params }));
});

await send('Page.enable');
await send('Runtime.enable');
await send('Page.navigate', { url: `${BASE}/#token=${encodeURIComponent(TOKEN)}` });
await sleep(2500);

const FAKE = {
  confirmation_id: 'cf_demo_0001',
  tool: 'transfer',
  tool_name: '转账',
  action_text: '500.00 元（转账，张三）',
  amount_fen: 50000,
  payee_name: '张三（民生银行 · 尾号 6620 · 同事B · 139****8004）',
  expires_in_s: 8,
  level: 'yellow',
  reason_text: '即将向张三 500.00 元，需要你确认',
  rule_id: 'R06_transfer_yellow'
};

const evaluate = async (expression) => {
  const r = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
  return r.result && r.result.value;
};

const injected = await evaluate(
  `(function(){ if (typeof renderConfirm !== 'function') { return 'renderConfirm 不存在'; }
     renderConfirm(${JSON.stringify(FAKE)}); return 'ok'; })()`
);
console.log('[注入]', injected);
await sleep(1200);

const texts = await evaluate(
  `(function(){ var c = document.querySelector('.confirm-card'); if (!c) { return null; }
     return { head: c.querySelector('.confirm-head') ? c.querySelector('.confirm-head').textContent : '',
              keys: Array.from(c.querySelectorAll('.confirm-key')).map(function(n){return n.textContent;}),
              btns: Array.from(c.querySelectorAll('button')).map(function(b){return b.textContent + (b.disabled ? ' [灰]' : '');}),
              timer: c.querySelector('.confirm-timer') ? c.querySelector('.confirm-timer').textContent : '',
              cls: c.className }; })()`
);
console.log('[卡面]', JSON.stringify(texts, null, 1));

await sleep(8000);
const expired = await evaluate(
  `(function(){ var c = document.querySelector('.confirm-card');
     return { timer: c.querySelector('.confirm-timer').textContent,
              btns: Array.from(c.querySelectorAll('button')).map(function(b){return b.textContent + (b.disabled ? ' [灰]' : '');}),
              notes: Array.from(c.querySelectorAll('.card-note')).map(function(n){return n.textContent;}).slice(-2),
              cls: c.className }; })()`
);
console.log('[8 秒后]', JSON.stringify(expired, null, 1));

const shot = await send('Page.captureScreenshot', { format: 'png' });
writeFileSync(OUT, Buffer.from(shot.data, 'base64'));
console.log('[截图]', OUT);
ws.close();
child.kill('SIGKILL');
process.exit(0);
