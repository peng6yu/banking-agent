#!/usr/bin/env node
/* 用 CDP 驱动本机 Chromium 打开真页面、切到指定视图、把该视图的文字读回来并截图。
   没有 playwright 也能做视觉与内容验证。
   用法：node scripts/cdp_view_check.mjs <base_url> <token> <view> <out_png> [wait_ms] */
import { spawn } from 'node:child_process';
import { writeFileSync } from 'node:fs';

const BASE = process.argv[2] || 'http://127.0.0.1:8123';
const TOKEN = process.argv[3] || '';
const VIEW = process.argv[4] || 'security';
const OUT = process.argv[5] || 'tmp/view.png';
const WAIT = Number(process.argv[6] || 2500);
const CHROME = process.env.CHROME_BIN
  || '/home/peng/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome';

const child = spawn(CHROME, [
  '--headless=new', '--disable-gpu', '--no-sandbox', '--hide-scrollbars',
  '--remote-debugging-port=9334', '--window-size=1280,1400', 'about:blank'
], { stdio: 'ignore' });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function target() {
  for (let i = 0; i < 40; i += 1) {
    try {
      const list = await (await fetch('http://127.0.0.1:9334/json/list')).json();
      const page = list.find((t) => t.type === 'page');
      if (page) return page;
    } catch (err) { /* 等 CDP 起来 */ }
    await sleep(250);
  }
  throw new Error('CDP 没起来');
}

const page = await target();
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
  pending.set(id, (m) => (m.error ? reject(new Error(JSON.stringify(m.error))) : resolve(m.result)));
  ws.send(JSON.stringify({ id, method, params }));
});
const evaluate = async (expression) => {
  const r = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) { throw new Error(JSON.stringify(r.exceptionDetails).slice(0, 400)); }
  return r.result && r.result.value;
};

await send('Page.enable');
await send('Runtime.enable');
await send('Page.navigate', { url: `${BASE}/#token=${encodeURIComponent(TOKEN)}` });
await sleep(2200);

const switched = await evaluate(`(function(){ if (typeof bxSetView !== 'function') { return 'no bxSetView'; } bxSetView(${JSON.stringify(VIEW)}); return 'ok'; })()`);
console.log('[切视图]', switched, '→', VIEW);
await sleep(WAIT);

const dump = await evaluate(`(function(){
  var v = document.querySelector('#view-${VIEW}');
  if (!v) { return { error: '视图不存在' }; }
  var out = {};
  ['sec-health', 'sec-health-tag', 'sec-demo', 'sec-demo-out', 'sec-handoff-list', 'sec-handoff-tag', 'sec-handoff-detail'].forEach(function (id) {
    var n = document.getElementById(id);
    if (n) { out[id] = n.innerText.replace(/\\n{2,}/g, '\\n').trim().slice(0, 600); }
  });
  out._viewText = v.innerText.replace(/\\n{2,}/g, '\\n').slice(0, 1500);
  return out;
})()`);
console.log(JSON.stringify(dump, null, 1));

const shot = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
writeFileSync(OUT, Buffer.from(shot.data, 'base64'));
console.log('[截图]', OUT);
ws.close();
child.kill('SIGKILL');
process.exit(0);
