// Regression harness for the auto-takeover flood bug.
// Loads the REAL shouldTake/isJunkAutolink/extFromUrl from extension/background.js
// (chrome.* stubbed) and asserts: media -> take; software/model/doc -> DON'T take.
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(require('path').join(__dirname, '..', 'extension', 'background.js'), 'utf8');

// permissive chrome stub: any property access returns a no-op function/promise
function makeChrome() {
  const noop = () => {};
  const handler = { get: () => new Proxy(function(){}, { get: () => new Proxy(function(){}, handler), apply: () => undefined }) };
  return new Proxy({}, handler);
}
const sandbox = { chrome: makeChrome(), console, setTimeout, clearTimeout, Promise, URL, fetch: () => Promise.resolve() };
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(src, sandbox);   // defines shouldTake, isJunkAutolink, extFromUrl, DEFAULTS...

const CFG = { onlyExtensions: "", minSizeMB: 0, skipInline: true, confirm: false };
function shouldTake(item){ return sandbox.shouldTake(item, CFG); }

const cases = [
  // [url, mime, expectTake, label]
  ['https://issuepcdn.baidupcs.com/issue/netdisk/yunguanjia/BaiduNetdisk_8.8.8.101.exe', 'application/x-msdownload', false, '百度网盘 exe'],
  ['https://github.com/DockerDesktop-CN/releases/download/4.93.0/DockerDesktop-4.93.0.exe', 'application/octet-stream', false, 'Docker exe'],
  ['https://huggingface.co/x/hermes-3-llama-3.2-3b-q4_k_m.gguf', 'application/octet-stream', false, 'gguf 模型'],
  ['https://dl/x/smartautoclicker-arm64.apk', 'application/vnd.android.package-archive', false, 'apk'],
  ['https://x/strata-feed.json', 'application/json', false, 'json'],
  ['https://x/cat-catch.crx', 'application/x-chrome-extension', false, 'crx'],
  ['https://x/Setup.msi', 'application/x-msi', false, 'msi'],
  ['https://live.x/video_1234.mp4', 'video/mp4', true, 'mp4 视频'],
  ['https://live.x/master.m3u8', 'application/vnd.apple.mpegurl', true, 'm3u8 流'],
  ['https://x/audio.mp3', 'audio/mpeg', true, 'mp3'],
  ['https://x/clip', 'video/quicktime', false, '无扩展名 video mime + skipInline → 交浏览器预览'],
];

let fail = 0;
for (const [url, mime, expect, label] of cases) {
  const item = { url, mime, totalBytes: 50 * 1024 * 1024, filename: url.split('/').pop() };
  let got;
  try { got = shouldTake(item); } catch (e) { console.log('THREW', label, e.message); got = 'ERR'; }
  const ok = got === expect;
  if (!ok) fail++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  expect=${expect} got=${got}  ${label}`);
}
// explicit whitelist still honored
const c2 = Object.assign({}, CFG, { onlyExtensions: 'exe,msi' });
const exeTaken = sandbox.shouldTake({ url: 'https://x/a.exe', mime: 'application/x-msdownload', totalBytes: 5e7, filename:'a.exe' }, c2);
console.log(`${exeTaken === true ? 'PASS' : 'FAIL'}  显式白名单 exe,msi 时 exe 应被接管 got=${exeTaken}`);
if (exeTaken !== true) fail++;

console.log(fail === 0 ? '\n===> ALL GREEN (auto-takeover = media-only by default)' : `\n===> ${fail} FAIL`);
process.exit(fail === 0 ? 0 : 1);
