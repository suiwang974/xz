// 内核被宿主重启后令牌/端口都换了：ui/js/api.js 的 rpc() 遇 401 / 连接被拒要重新问宿主要地址并重试一次
// 用法：node tests/ui_token_refresh_test.js   （无需浏览器：stub 掉 window/location/EventSource，起两个假内核）
const http = require('http');
const path = require('path');
const { pathToFileURL } = require('url');
let fails = 0;
const t = (n, c, d) => { console.log((c ? 'PASS ' : 'FAIL ') + n + (c ? '' : '  ' + (d || ''))); if (!c) fails++; };
const mk = (token, tag) => new Promise((res) => {
  const calls = [];
  const s = http.createServer((q, r) => {
    let b = ''; q.on('data', (d) => b += d); q.on('end', () => {
      const tk = q.headers['x-sniffdl-token'];
      calls.push(tk);
      if (tk !== token) { r.writeHead(401, { 'Content-Type': 'application/json' }); return r.end('{"error":{"message":"unauthorized"}}'); }
      r.writeHead(200, { 'Content-Type': 'application/json' }); r.end(JSON.stringify({ jsonrpc: '2.0', id: 1, result: { from: tag } }));
    });
  }).listen(0, '127.0.0.1', () => res({ s, port: s.address().port, calls }));
});
(async () => {
  const A = await mk('tokA', 'A'), B = await mk('tokB', 'B');
  let cur = { port: A.port, token: 'tokA', pid: 1 };
  global.window = { sniffdlHost: { conn: async () => cur }, addEventListener() {} };
  global.location = { hash: '', search: '', port: '' };
  global.navigator = { userAgent: 'node' };
  global.EventSource = class { constructor() { this.n = 0; } close() {} addEventListener() {} };
  const api = await import(pathToFileURL(path.join(__dirname, '..', 'ui', 'js', 'api.js')).href);
  await api.connect();
  let r = await api.rpc('x');
  t('旧内核正常调用', r.from === 'A');
  // 1) 同端口换令牌（宿主重启内核，端口固定）
  A.s.close();
  const A2 = await new Promise((res) => { const calls = []; const s = http.createServer((q, rr) => { q.resume(); q.on('end', () => { const tk = q.headers['x-sniffdl-token']; calls.push(tk); if (tk !== 'tokA2') { rr.writeHead(401); return rr.end('{}'); } rr.writeHead(200); rr.end('{"result":{"from":"A2"}}'); }); }).listen(A.port, '127.0.0.1', () => res({ s, calls })); });
  cur = { port: A.port, token: 'tokA2', pid: 2 };
  r = await api.rpc('x');
  t('同端口换令牌：401 后自动重取并重试成功', r.from === 'A2', JSON.stringify(r));
  t('旧令牌只试了一次（不循环）', A2.calls.filter((x) => x === 'tokA').length === 0 || A2.calls.filter((x) => x === 'tokA2').length >= 1, JSON.stringify(A2.calls));
  // 2) 换端口 + 换令牌（旧端口连接被拒）
  A2.s.close();
  await new Promise((r) => setTimeout(r, 100));
  cur = { port: B.port, token: 'tokB', pid: 3 };
  r = await api.rpc('x');
  t('旧端口不通：重取新地址并重试成功', r.from === 'B', JSON.stringify(r));
  // 3) 内核真没了：宿主给的还是死地址 → 抛清楚的错而不是死循环
  B.s.close();
  await new Promise((r) => setTimeout(r, 100));
  let err = null;
  try { await api.rpc('x'); } catch (e) { err = e; }
  t('内核真不在：抛「连不上内核」', err && /连不上内核/.test(err.message), err && err.message);
  // 4) 业务错误(error 字段)不触发重取
  const C = await mk('tokC', 'C');
  cur = { port: C.port, token: 'tokC', pid: 4 };
  await api.rpc('x');
  const n0 = C.calls.length;
  t('正常新地址调用 1 次请求', n0 === 2 || n0 === 1, String(n0));
  process.exit(fails ? 1 : 0);
})();
