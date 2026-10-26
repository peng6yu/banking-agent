/* 10-core.js — 常量、访问令牌与 fetch 补头、平铺状态。这一块必须最先执行：后面的模块都靠它。 */
'use strict';

/* ------------------------------------------------------------------ 常量 */
const API_CHAT = '/api/chat';
const API_CONFIRM = '/api/confirm';
const API_CANCEL = '/api/cancel';
const API_AUDIT = '/api/audit';
const API_AUDIT_VERIFY = '/api/audit/verify';
const API_TOOLS = '/api/tools';

/* ------------------------------------------------------------------ 访问令牌
   服务端除首页与 /api/health 外都要令牌。用启动日志里带 #token= 的地址打开一次，
   这里读进来存进 sessionStorage，再把 token 从地址栏抹掉。
   令牌放 fragment 而不是 ?token=：fragment 不随 HTTP 请求发出，不会进
   服务端访问日志或代理日志；服务端也只从请求头认令牌。 */
const TOKEN_KEY = 'banking_api_token';
const tokenStore = window.sessionStorage;

(function captureToken() {
  let fromUrl = '';
  if (location.hash.indexOf('#token=') === 0) {
    fromUrl = location.hash.slice('#token='.length);
    try { fromUrl = decodeURIComponent(fromUrl); } catch (err) { /* 保持原样 */ }
  }
  if (!fromUrl) { return; }
  tokenStore.setItem(TOKEN_KEY, fromUrl);
  window.history.replaceState(null, '', location.pathname + location.search);
}());

const rawFetch = window.fetch.bind(window);
window.fetch = function (input, init) {
  const options = Object.assign({}, init || {});
  const headers = Object.assign({}, options.headers || {});
  const token = tokenStore.getItem(TOKEN_KEY);
  if (token) { headers.Authorization = 'Bearer ' + token; }
  options.headers = headers;
  return rawFetch(input, options);
};

/* 颜色由后端的 decision.level 决定，前端只做「等级 → 样式类」映射，不自己判色 */
const LEVEL_CLASS = { green: 'lv-green', yellow: 'lv-amber', red: 'lv-red' };
const LEVEL_LABEL = { green: '绿灯：直接放行', yellow: '黄灯：需要你确认', red: '红灯：需要多因子' };
const LEVEL_DOT = { green: '绿灯', yellow: '黄灯', red: '红灯' };
const LEVEL_DEFAULT = 'lv-unknown';

/* 仅用于把后端给的意图代号显示成人话，判定逻辑全在后端 */
const INTENT_LABEL = {
  query_balance: '查余额',
  transfer: '转账',
  bill_analysis: '账单分析',
  subscription: '订阅管理',
  birthday_plan: '生日理财计划',
  unknown: '未识别'
};

const AMOUNT_KEYS = ['amount_text', 'amount_yuan', 'amount', 'amount_fen'];
const PAYEE_KEYS = ['payee_name', 'payee_text', 'payee', 'recipient_name', 'counterparty'];
const NESTED_KEYS = ['action', 'params', 'detail', 'action_json', 'confirmation', 'context', 'slots'];

/* -------------------------------------------------------------- 运行状态 */
let sessionId = null;
let streaming = false;
let localSeq = 0;
let planNodes = [];
const auditIndex = new Map();
let auditCount = 0;

/* -------------------------------------------------------------- 基础工具 */
function $(id) {
  return document.getElementById(id);
}
