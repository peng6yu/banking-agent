#!/usr/bin/env node
/* 用 CDP 真点一遍：强确认弹窗、新建计划、手动跑储蓄。
   验证的是「人真点得动」，不是静态扫描。
   用法：node scripts/cdp_modal_check.mjs <base_url> <token> <out_png> */
import { spawn } from 'node:child_process';
import { writeFileSync } from 'node:fs';

const BASE = process.argv[2] || 'http://127.0.0.1:8125';
const TOKEN = process.argv[3] || 'modal-probe';
const OUT = process.argv[4] || 'tmp/modal_check.png';
const PORT = 9337;
const CHROME = process.env.CHROME_BIN
  || '/home/peng/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome';

const child = spawn(CHROME, ['--headless=new', '--disable-gpu', '--no-sandbox', '--hide-scrollbars',
  `--remote-debugging-port=${PORT}`, '--window-size=1280,1500', 'about:blank'], { stdio: 'ignore' });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let page;
for (let i = 0; i < 40; i += 1) {
  try {
    const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
    page = list.find((t) => t.type === 'page');
    if (page) break;
  } catch (err) { /* 等 CDP */ }
  await sleep(250);
}
const ws = new WebSocket(page.webSocketDebuggerUrl);
let seq = 0;
const pending = new Map();
ws.addEventListener('message', (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
});
await new Promise((r) => ws.addEventListener('open', r));
const send = (method, params = {}) => new Promise((res, rej) => {
  seq += 1;
  const id = seq;
  pending.set(id, (m) => (m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result)));
  ws.send(JSON.stringify({ id, method, params }));
});
const ev = async (x) => {
  const r = await send('Runtime.evaluate', { expression: x, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) { throw new Error(JSON.stringify(r.exceptionDetails).slice(0, 400)); }
  return r.result && r.result.value;
};
const shot = async (path) => {
  const png = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
  writeFileSync(path, Buffer.from(png.data, 'base64'));
};

await send('Page.enable');
await send('Runtime.enable');
await send('Page.navigate', { url: `${BASE}/#token=${encodeURIComponent(TOKEN)}` });
await sleep(2400);

/* ---- 1. 强确认弹窗：必须按字确认才放行 ---- */
await ev("bxSetView('security')");
await sleep(1200);
console.log('[点重置]', await ev(`(function(){
  var b = Array.prototype.slice.call(document.querySelectorAll('#sec-demo button'))
    .filter(function(x){ return x.textContent.indexOf('重置演示数据') >= 0; })[0];
  if (!b) { return '按钮没找到'; }
  b.click(); return 'clicked';
})()`));
await sleep(700);
console.log('[弹窗内容]', JSON.stringify(await ev(`(function(){
  var box = document.querySelector('div[role="dialog"]');
  if (!box) { return { error: '弹窗没出现' }; }
  var input = box.querySelector('input');
  var buttons = Array.prototype.slice.call(box.querySelectorAll('button')).map(function(b){
    return b.textContent + (b.disabled ? '(灰)' : '(可点)');
  });
  return { text: box.innerText.replace(/\\n{2,}/g, '\\n').slice(0, 600), buttons: buttons,
           typedGate: Boolean(input) };
})()`), null, 1));
await shot(`${OUT.replace(/\.png$/, '')}_1_modal.png`);

console.log('[输入错字]', await ev(`(function(){
  var box = document.querySelector('div[role="dialog"]');
  var input = box.querySelector('input');
  input.value = '重置数据';
  input.dispatchEvent(new Event('input', { bubbles: true }));
  var ok = Array.prototype.slice.call(box.querySelectorAll('button')).pop();
  return '错字时确认键 disabled=' + ok.disabled;
})()`));
console.log('[输入正确]', await ev(`(function(){
  var box = document.querySelector('div[role="dialog"]');
  var input = box.querySelector('input');
  input.value = '重置';
  input.dispatchEvent(new Event('input', { bubbles: true }));
  var ok = Array.prototype.slice.call(box.querySelectorAll('button')).pop();
  var state = '正确时确认键 disabled=' + ok.disabled;
  ok.click();
  return state + ' / 已点执行';
})()`));
await sleep(3200);
console.log('[重置结果]', JSON.stringify(await ev(`(function(){
  return { out: (document.getElementById('sec-demo-out')||{}).innerText,
           tag: (document.getElementById('sec-demo-tag')||{}).textContent,
           modalLeft: document.querySelectorAll('div[role="dialog"]').length };
})()`), null, 1));

/* ---- 2. 新建计划（表单弹窗）---- */
await ev("bxSetView('plans')");
await sleep(1600);
console.log('[点新建]', await ev(`(function(){
  var b = Array.prototype.slice.call(document.querySelectorAll('[data-bar="plan-create"] button'))[0];
  if (!b) { return '按钮没找到'; }
  b.click(); return 'clicked';
})()`));
await sleep(600);
console.log('[填表]', await ev(`(function(){
  var box = document.querySelector('div[role="dialog"]');
  if (!box) { return '弹窗没出现'; }
  var inputs = Array.prototype.slice.call(box.querySelectorAll('input'));
  inputs[0].value = '下月房租';
  inputs[1].value = '提醒我留出 3000 元付房租';
  inputs[2].value = '60';
  var ok = Array.prototype.slice.call(box.querySelectorAll('button')).pop();
  ok.click();
  return '已提交 · 字段数=' + inputs.length;
})()`));
await sleep(2500);
console.log('[建计划结果]', JSON.stringify(await ev(`(function(){
  var bar = document.querySelector('[data-bar="plan-create"]');
  var list = document.getElementById('plan-persist');
  return { toast: (document.querySelector('.toast')||{}).innerText || null,
           listHasTitle: list ? list.innerText.indexOf('下月房租') >= 0 : null,
           modalLeft: document.querySelectorAll('div[role="dialog"]').length };
})()`), null, 1));

/* ---- 3. 手动跑一次到期储蓄计划 ---- */
console.log('[点跑批]', await ev(`(function(){
  var b = Array.prototype.slice.call(document.querySelectorAll('[data-bar="savings-run"] button'))[0];
  if (!b) { return '按钮没找到'; }
  b.click(); return 'clicked';
})()`));
await sleep(2500);
console.log('[跑批结果]', JSON.stringify(await ev(`(function(){
  var bar = document.querySelector('[data-bar="savings-run"]');
  return { barText: bar ? bar.innerText.replace(/\\n{2,}/g, '\\n').slice(0, 300) : null,
           toast: (document.querySelector('.toast')||{}).innerText || null };
})()`), null, 1));
await shot(`${OUT.replace(/\.png$/, '')}_2_plans.png`);

ws.close();
child.kill('SIGKILL');
process.exit(0);
