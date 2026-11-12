// 0.6.0 网盘接管回归：加载真实 extension/background.js（chrome.* 打桩），验证
//  1) 浏览器发往网盘下载 CDN 的真实请求头（webRequest extraHeaders）被捕获；
//  2) 网盘下载（无手势、文件名未定）也必接管，且交给页面下载卡的 info 带着捕获到的 UA/Cookie/Referer；
//  3) Chrome 开机自动续传的旧网盘下载（startTime 很早）不接管（防洪水）；
//  4) 非网盘、无手势、无明确文件名的 .zip 仍不接管（老规则不回退）；
//  5) 网盘 Cookie 同步：cookies.onChanged → sniffdl.syncCookies 推给桌面（含 httpOnly）。
//  node tools/ext_netdisk_regression.js
const fs = require('fs');
const vm = require('vm');
const path = require('path');
const src = fs.readFileSync(path.join(__dirname, '..', 'extension', 'background.js'), 'utf8');

const ev = {};
function evt(name) {
  return { addListener: (fn) => { (ev[name] = ev[name] || []).push(fn); }, removeListener() {}, hasListener() { return false; } };
}
const session = {};
const calls = { cancel: [], sendMessage: [], rpc: [] };
const chrome = {
  runtime: { onInstalled: evt('onInstalled'), onStartup: evt('onStartup'), onMessage: evt('onMessage'), lastError: null },
  downloads: { onCreated: evt('onCreated'), onChanged: evt('dlChanged'),
               cancel: async (id) => { calls.cancel.push(id); }, removeFile() {}, erase() {} },
  webRequest: { onBeforeSendHeaders: evt('onBeforeSendHeaders') },
  cookies: { onChanged: evt('cookieChanged'),
             getAll: async (q) => (q.domain === 'quark.cn' ? [
               { name: '__pus', value: 'P', domain: '.quark.cn', hostOnly: false, path: '/', secure: true, httpOnly: true },
               { name: '__puus', value: 'PU', domain: '.quark.cn', hostOnly: false, path: '/', secure: true, httpOnly: true }] : []) },
  storage: { local: { get: async (d) => Object.assign({}, d), set: async () => {} },
             session: { get: async (k) => ({ [k]: session[k] }), set: async (o) => Object.assign(session, o), remove: async (k) => { delete session[k]; } } },
  tabs: { get: async (id) => ({ id, url: 'https://pan.quark.cn/list#/list/all' }),
          sendMessage: async (id, msg) => { calls.sendMessage.push(msg); return { ok: true }; }, query: async () => [] },
  contextMenus: { create() {}, onClicked: evt('menu') },
  scripting: { executeScript: async () => {} },
  notifications: { create() {}, clear() {} },
  action: { setBadgeText() {}, setBadgeBackgroundColor() {} },
};
const fetchStub = async (url, opt) => {
  const body = JSON.parse(opt.body);
  calls.rpc.push(body);
  return { json: async () => ({ result: body.method === 'aria2.addUri' ? 'g1' : { ok: true } }) };
};
const sandbox = { chrome, console: { log() {}, warn() {}, error: console.error }, setTimeout, clearTimeout, Promise, URL, fetch: fetchStub, navigator: { userAgent: 'Mozilla/5.0 Chrome/131' } };
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(src, sandbox);

let fail = 0;
function check(cond, label, extra) { console.log((cond ? 'PASS ' : 'FAIL ') + label + (extra ? '  ' + extra : '')); if (!cond) fail++; }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  check(sandbox.isNetdiskDl('https://dl-pc-sz.drive.quark.cn/x?filename=a.zip'), '夸克下载 CDN 识别为网盘');
  check(sandbox.isNetdiskDl('https://d.pcs.baidu.com/file/x'), '百度 PCS 识别为网盘');
  check(!sandbox.isNetdiskDl('https://github.com/a.zip'), 'github 不是网盘');
  check(!sandbox.isNetdiskDl('https://evilquark.cn.example.com/a'), '后缀匹配不被伪造域骗');

  const QURL = 'https://dl-pc-sz.drive.quark.cn/ngTX6F13/a/b?auth_key=1793990469-1-21600-x&filename=hanshuang.zip';
  const reqHeaders = [
    { name: 'User-Agent', value: 'Mozilla/5.0 Chrome/131 RealBrowser' },
    { name: 'Cookie', value: '__pus=P; __puus=PU' },
    { name: 'Referer', value: 'https://pan.quark.cn/' },
    { name: 'Accept-Encoding', value: 'gzip, br' },
    { name: 'sec-ch-ua', value: '"Chromium";v="131"' },
    { name: 'Range', value: 'bytes=0-' },
  ];
  check((ev.onBeforeSendHeaders || []).length === 1, 'webRequest.onBeforeSendHeaders 已注册');
  ev.onBeforeSendHeaders[0]({ url: QURL, method: 'GET', requestHeaders: reqHeaders });
  await sleep(20);
  const cap = await sandbox.capGet(QURL);
  check(cap && cap.Cookie === '__pus=P; __puus=PU' && cap['User-Agent'].includes('RealBrowser'), '捕获到真实 Cookie/UA', JSON.stringify(cap));
  check(cap && !cap['Accept-Encoding'] && !cap['sec-ch-ua'] && !cap.Range, '传输层头/sec-* 不转发');

  // 网盘下载：无手势、filename 还没定、刚创建 → 必接管 + 卡片带真实头
  calls.cancel.length = 0; calls.sendMessage.length = 0;
  await ev.onCreated[0]({ id: 11, url: QURL, finalUrl: QURL, tabId: 5, filename: '', mime: 'application/zip',
                          totalBytes: 5e6, startTime: new Date().toISOString(), referrer: 'https://pan.quark.cn/' });
  await sleep(30);
  check(calls.cancel.includes(11), '网盘下载无手势也接管（取消浏览器下载）');
  const card = calls.sendMessage.find((m) => m.type === 'dl_card');
  check(card && card.info.headers && card.info.headers.Cookie === '__pus=P; __puus=PU', '下载卡 info 带捕获的 Cookie', card && JSON.stringify(card.info).slice(0, 160));
  check(card && card.info.ua.includes('RealBrowser') && card.info.referer === 'https://pan.quark.cn/', '下载卡 info 的 UA/Referer=浏览器真实值');

  // 开机自动续传的旧网盘下载 → 不接管
  calls.cancel.length = 0;
  await ev.onCreated[0]({ id: 12, url: QURL, finalUrl: QURL, tabId: 5, filename: '', mime: 'application/zip',
                          totalBytes: 5e6, startTime: new Date(Date.now() - 3600e3).toISOString() });
  await sleep(30);
  check(!calls.cancel.includes(12), '开机续传的旧网盘下载不接管（防洪水）');

  // 普通站 zip，无手势、无文件名 → 不接管（老规则）
  calls.cancel.length = 0;
  await ev.onCreated[0]({ id: 13, url: 'https://example.com/dl?id=1', tabId: 5, filename: '', mime: 'application/zip',
                          totalBytes: 5e6, startTime: new Date().toISOString() });
  await sleep(30);
  check(!calls.cancel.includes(13), '非网盘无手势下载仍不接管');

  // 网页卡点「开始下载」→ sniffdl.add 带捕获头
  calls.rpc.length = 0;
  await new Promise((res) => ev.onMessage[0]({ type: 'rpc', method: 'sniffdl.add', params: [{ url: QURL, filename: 'hanshuang.zip', cookie: '', ua: 'Mozilla/5.0 Chrome/131' }] },
                                             { tab: { id: 5, url: 'https://pan.quark.cn/' } }, res));
  const add = calls.rpc.find((c) => c.method === 'sniffdl.add');
  check(add && add.params[0].headers && add.params[0].cookie === '__pus=P; __puus=PU' && add.params[0].ua.includes('RealBrowser'),
        'sniffdl.add 转发的是浏览器真实 Cookie/UA', add && JSON.stringify(add.params[0]).slice(0, 160));

  // Cookie 同步
  calls.rpc.length = 0;
  await sandbox.syncNetdiskCookies();
  const sync = calls.rpc.find((c) => c.method === 'sniffdl.syncCookies');
  check(sync && sync.params[0].length === 2 && sync.params[0][0].name === '__pus', '网盘 Cookie 同步给桌面（含 httpOnly）');

  console.log(fail ? `\n===> ${fail} FAIL` : '\n===> EXT NETDISK ALL GREEN');
  process.exit(fail ? 1 : 0);
})();
