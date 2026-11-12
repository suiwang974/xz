// SniffDL v3 统一宿主（Electron 33）—— 下载器主窗 + 大肥鱼 Live2D 桌宠 + 托盘，一个进程
// 结构：
//   - 拉起 Python 内核（sniffdl_core.py / SniffDL.exe --core），读 stdout「SNIFFDL_READY {json}」拿 port + ui_token；
//     已有内核在跑（runtime.json 的 pid 活着且 ping 通）→ 直接复用（复用的内核退出时不关，不归本宿主管）。
//     内核带 --parent-pid / --watch-stdin，宿主被强杀也会跟着退；内核意外退出 → 自动重启一次。
//   - 主窗加载 v3/ui/index.html（file://，界面通过 preload 的 window.sniffdlHost.conn() 拿连接）。
//   - 桌宠窗：铺满主屏工作区的透明置顶窗口，行为与 v2 桌宠完全一致（命中门控/控制 API/sniffpet:// 协议）。
//     内核经环境变量 SNIFFDL_PET_PORT/SNIFFDL_PET_TOKEN 连桌宠控制端口（pet_bridge attach 模式）。
//   - 托盘：显示主窗、开始/停止全部、乌龟模式、从剪贴板导入、显示/隐藏桌宠、退出。关闭主窗 = 进托盘。
// 命令行（均可省略）：--port 桌宠控制端口(6801) --token 桌宠令牌 --hidden 1(桌宠初始隐藏) --state 桌宠状态文件
//   --runtime 内核 runtime.json --data-dir 内核数据目录 --core-port 内核端口 --user-data Electron 数据目录(测试隔离)
//   --no-main 1(启动时不弹主窗，只进托盘) --no-pet 1(不建桌宠窗)
// 环境：SNIFFDL_CORE_CMD='["python","x.py"]'（覆盖内核启动命令，测试打桩用）SNIFFDL_PYTHON（开发态 python 路径）
const { app, BrowserWindow, Menu, Tray, nativeImage, screen, ipcMain, globalShortcut, protocol, session,
  dialog, clipboard, Notification, shell } = require('electron');
const http = require('http');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { spawn } = require('child_process');

function arg(name, dft) {
  const i = process.argv.indexOf('--' + name);
  return i >= 0 && process.argv[i + 1] !== undefined ? process.argv[i + 1] : dft;
}
const HERE = __dirname;                                  // pet_electron/app
const ROOT = path.join(HERE, '..');                      // pet_electron
const V3 = path.resolve(arg('v3-dir', process.env.SNIFFDL_V3_DIR || path.join(ROOT, '..')));   // 程序目录（ui/、sniffdl_core.py）
const UI_INDEX = path.join(V3, 'ui', 'index.html');
const RUNTIME = path.resolve(arg('runtime', path.join(V3, 'runtime.json')));
const DATA_DIR = arg('data-dir', '');
const CORE_PORT = parseInt(arg('core-port', '0'), 10) || 0;
const STATE = path.resolve(arg('state', path.join(ROOT, 'pet_state.json')));
const LOGF = path.join(path.dirname(STATE), 'pet_electron.log');
let CTRL_PORT = parseInt(arg('port', '6801'), 10);
const TOKEN = arg('token', crypto.randomBytes(12).toString('hex'));
const HIDDEN = arg('hidden', '') === '1';
const NO_MAIN = arg('no-main', '') === '1';
const NO_PET = arg('no-pet', '') === '1';
const SCALE_STEP = [0.6, 0.75, 0.9, 1.0, 1.15, 1.3, 1.5, 1.75];
let scaleIdx = 3;
let skin = 'da-feiyu';

// 数据目录沿用 %APPDATA%\SniffDLPet（小本本/好感度/成就在 Partitions\sniffpet 里），换新版本解压也不丢；单实例锁也挂在这里
try {
  app.setName('SniffDL');
  app.setPath('userData', path.resolve(arg('user-data', path.join(app.getPath('appData'), 'SniffDLPet'))));
} catch (e) {}
try { if (process.platform === 'win32') app.setAppUserModelId('SniffDL'); } catch (e) {}   // Windows 通知需要
const GOT_LOCK = app.requestSingleInstanceLock();
if (!GOT_LOCK) { app.exit(0); }

function dlog(s) { try { fs.appendFileSync(LOGF, '[' + new Date().toISOString().slice(11, 19) + '] ' + s + '\n'); } catch (e) {} }
if (GOT_LOCK) { try { fs.writeFileSync(LOGF, 'boot pid=' + process.pid + ' port=' + CTRL_PORT + ' state=' + STATE + ' v3=' + V3 + '\n'); } catch (e) {} }

try { app.commandLine.appendSwitch('enable-transparent-visuals'); } catch (e) {}
try { app.commandLine.appendSwitch('autoplay-policy', 'no-user-gesture-required'); } catch (e) {}
if (process.platform === 'linux') { try { app.commandLine.appendSwitch('enable-unsafe-swiftshader'); } catch (e) {} }   // 仅 Linux 验收（Xvfb 无 GPU）：允许软件 WebGL

protocol.registerSchemesAsPrivileged([{ scheme: 'sniffpet',
  privileges: { standard: true, secure: true, supportFetchAPI: true, corsEnabled: true, stream: true } }]);

const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css',
  '.json': 'application/json', '.png': 'image/png', '.jpg': 'image/jpeg', '.webp': 'image/webp',
  '.moc3': 'application/octet-stream', '.wasm': 'application/wasm', '.txt': 'text/plain; charset=utf-8' };
// sniffpet://app/<相对 pet_electron/app 的路径>  →  磁盘文件（越界/不存在返回 null）
function resolveFile(url) {
  try {
    const u = new URL(url);
    let rel = decodeURIComponent(u.pathname).replace(/^\/+/, '');
    // ASCII 别名：页面用 model/desktop.model3.json 指向「大肥鱼.desktop.model3.json」
    if (rel === 'model/desktop.model3.json') rel = 'model/大肥鱼.desktop.model3.json';
    const f = path.normalize(path.join(HERE, rel));
    if (u.host !== 'app' || !f.startsWith(HERE + path.sep) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return null;
    return f;
  } catch (e) { return null; }
}
// 用 registerBufferProtocol 而不是 protocol.handle：Electron 33 的 handle 在把请求头转成 undici Headers 时
// 遇到非 Latin-1 字符（中文文件名的动作/表情 URL 带出的头）会直接抛 ByteString 错，整页加载失败（Xvfb 实测）。
function registerScheme(ses) {
  const p = ses.protocol;
  if (typeof p.registerBufferProtocol === 'function') {
    p.registerBufferProtocol('sniffpet', (req, cb) => {
      const f = resolveFile(req.url);
      if (!f) { dlog('404 ' + req.url); cb({ statusCode: 404, mimeType: 'text/plain', data: Buffer.from('not found') }); return; }
      const mt = (MIME[path.extname(f).toLowerCase()] || 'application/octet-stream').split(';')[0];
      cb({ statusCode: 200, mimeType: mt, charset: /^text|json/.test(mt) ? 'utf-8' : undefined,
        headers: { 'Access-Control-Allow-Origin': '*' }, data: fs.readFileSync(f) });
    });
  } else {
    p.handle('sniffpet', (req) => {
      const f = resolveFile(req.url);
      if (!f) return new Response('not found', { status: 404 });
      return new Response(fs.readFileSync(f), { headers: { 'content-type': MIME[path.extname(f).toLowerCase()] || 'application/octet-stream' } });
    });
  }
}

let petWin = null, tray = null, bound = false;
let clickThrough = false;     // /api/clickthrough：强制整窗穿透（她不吃任何点击）
let interactive = null;
function evalJs(code) {
  return (petWin && !petWin.isDestroyed()) ? petWin.webContents.executeJavaScript(code).catch(() => null) : Promise.resolve(null);
}
function setIgnore(ignore) {
  if (!petWin || petWin.isDestroyed()) return;
  if (interactive === !ignore) return;
  interactive = !ignore;
  try { petWin.setIgnoreMouseEvents(ignore, ignore ? { forward: true } : undefined); } catch (e) {}
}
function showPet(on) {
  if (!petWin || petWin.isDestroyed()) return;
  if (on) { petWin.showInactive(); petWin.setAlwaysOnTop(true, 'screen-saver'); } else petWin.hide();
  rebuildTray();
}
function toggleChibi() { evalJs('window.SniffDLPet&&SniffDLPet.setChibi(!(window.__pet&&__pet.L2D.chibiLevel()>=.5))'); }

function rebuildTray() { buildTray(); }

// ======================================================================= 桌宠（v2 行为原样保留）
// ---- 控制 API（与旧版路由一致）----
function handleApi(req, res, body) {
  const send = (o, code) => { res.writeHead(code || 200, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(o)); };
  if ((req.headers['authorization'] || '') !== 'Bearer ' + TOKEN) return send({ ok: false, error: 'auth' }, 401);
  const p = (req.url || '').split('?')[0];
  const S = (code) => evalJs('window.SniffDLPet&&' + code);
  if (p === '/api/ping') return send({ ok: true, skin, fps: 0, bound, pid: process.pid, engine: 'electron', model: 'da-feiyu' });
  if (p === '/api/probe') {   // 验收用：按窗口内坐标走一遍命中门控（Linux/Xvfb 下 getCursorScreenPoint 不随 XTEST 更新）
    gate(parseInt(body.x, 10), parseInt(body.y, 10), true).then((h) => send({ ok: true, hit: h, interactive })); return;
  }
  if (p === '/api/debug') { S('JSON.stringify(SniffDLPet.info())').then((r) => send({ ok: true, js: r, interactive, visible: !!(petWin && petWin.isVisible()), cursor: screen.getCursorScreenPoint(), bounds: petWin && petWin.getBounds(), clickThrough })); return; }
  if (p === '/api/bubble') { S(`SniffDLPet.bubble(${JSON.stringify(String(body.text || ''))},${parseInt(body.ms || 0, 10) || 0})`); return send({ ok: true }); }
  if (p === '/api/react') { S(`SniffDLPet.react(${JSON.stringify(String(body.name || 'idle'))})`); return send({ ok: true }); }
  if (p === '/api/expr') { S(`SniffDLPet.expr(${JSON.stringify(String(body.name || ''))})`); return send({ ok: true }); }
  if (p === '/api/visible') { showPet(!!body.on); return send({ ok: true }); }
  if (p === '/api/skin') {
    // 只有一个角色；skin=chibi/q → Q版，normal/full → 正常版，其余照旧回 ok（兼容旧桥）
    const k = String(body.key || body.skin || '').toLowerCase();
    if (/^(chibi|q|qban|q版)$/.test(k)) S('SniffDLPet.setChibi(true)');
    else if (/^(normal|full|正常版?)$/.test(k)) S('SniffDLPet.setChibi(false)');
    return send({ ok: true, single: true, model: 'da-feiyu' });
  }
  if (p === '/api/pos') {
    // 屏幕坐标（她的脚底中心）→ 窗口内坐标
    if (petWin) { const b = petWin.getBounds(); S(`SniffDLPet.moveTo(${parseInt(body.x, 10) - b.x},${parseInt(body.y, 10) - b.y})`); }
    return send({ ok: true });
  }
  if (p === '/api/size') {
    scaleIdx = Math.max(0, Math.min(SCALE_STEP.length - 1, parseInt(body.idx, 10) || 0));
    S(`SniffDLPet.setScale(${SCALE_STEP[scaleIdx]})`); return send({ ok: true, idx: scaleIdx });
  }
  if (p === '/api/clickthrough') { clickThrough = !!body.on; if (clickThrough) setIgnore(true); return send({ ok: true }); }
  if (p === '/api/quit') { send({ ok: true, hidden: true }); showPet(false); return; }   // 统一宿主：桌宠「退出」= 收起桌宠，下载器照常
  return send({ ok: false, error: 'no route' }, 404);
}

function createWindow() {
  const wa = require('./screens').unionBounds(screen.getAllDisplays()) || screen.getPrimaryDisplay().workArea;
  const ses = session.fromPartition('persist:sniffpet');
  registerScheme(ses);
  petWin = new BrowserWindow({
    x: wa.x, y: wa.y, width: wa.width, height: wa.height,
    frame: false, transparent: true, resizable: false, movable: false, minimizable: false, maximizable: false,
    fullscreenable: false, alwaysOnTop: true, skipTaskbar: true, hasShadow: false, focusable: true, show: false,
    backgroundColor: '#00000000',
    webPreferences: { partition: 'persist:sniffpet', contextIsolation: false, nodeIntegration: false,
      backgroundThrottling: false, preload: path.join(HERE, 'preload.js') },
  });
  petWin.setAlwaysOnTop(true, 'screen-saver');
  setIgnore(true);
  petWin.webContents.on('console-message', (e, level, msg) => dlog('console L' + level + ' ' + String(msg).slice(0, 300)));
  petWin.webContents.on('did-fail-load', (e, code, desc, u) => dlog('did-fail-load ' + code + ' ' + desc + ' ' + u));
  petWin.webContents.on('render-process-gone', (e, d) => dlog('render-gone ' + JSON.stringify(d)));
  petWin.webContents.on('did-finish-load', () => {
    let n = 0;
    const iv = setInterval(() => {
      evalJs('window.__petModelReady?1:(window.__err?-1:0)').then((r) => {
        if (r === 1) { clearInterval(iv); if (!bound) dlog('model ready'); bound = true; }
        else if (r === -1 || ++n > 240) { clearInterval(iv); evalJs('window.__err||""').then((e) => dlog('model load failed: ' + e)); }
      });
    }, 250);
  });
  // 工作区变了（任务栏挪位置/分辨率变化）→ 跟着铺满
  const refit = () => { if (!petWin || petWin.isDestroyed()) return; const w2 = require('./screens').unionBounds(screen.getAllDisplays()) || screen.getPrimaryDisplay().workArea; petWin.setBounds(w2); };
  screen.on('display-metrics-changed', refit);
  screen.on('display-added', refit); screen.on('display-removed', refit);
  petWin.loadURL('sniffpet://app/page/desk.html');
  petWin.once('ready-to-show', () => { if (!HIDDEN) showPet(true); else rebuildTray(); });

  // 光标轮询：命中身体/菜单才接管点击，其它地方整窗穿透（并把光标喂给视线）
  setInterval(() => {
    if (!petWin || petWin.isDestroyed() || !petWin.isVisible() || !bound) return;
    const c = screen.getCursorScreenPoint(), b = petWin.getBounds();
    gate(c.x - b.x, c.y - b.y);
  }, 33);
}
// 命中门控：窗口内坐标 (x,y) → 问页面是否命中她/菜单 → 设置穿透。返回 Promise<bool 是否接管点击>
let gateBusy = false;
function gate(x, y, force) {
  if (!petWin || petWin.isDestroyed()) return Promise.resolve(false);
  const b = petWin.getBounds();
  if (x < 0 || y < 0 || x >= b.width || y >= b.height) { setIgnore(true); return Promise.resolve(false); }
  if (clickThrough) { setIgnore(true); evalJs(`window.SniffDLPet&&SniffDLPet.feedCursor(${x},${y})`); return Promise.resolve(false); }
  if (gateBusy && !force) return Promise.resolve(interactive);
  gateBusy = true;
  return evalJs(`(function(){var S=window.SniffDLPet;if(!S)return 0;var h=S.hitAt(${x},${y});if(!h)S.feedCursor(${x},${y});return h?1:0})()`)
    .then((h) => { gateBusy = false; setIgnore(!h); return !!h; });
}

// ======================================================================= 内核
let core = null;              // { proc, info:{port, ui_token, pid}, attached }
let coreReady = null, coreReadyResolve = null;
let quitting = false, coreRestarts = 0;
function resetReady() { coreReady = new Promise((r) => { coreReadyResolve = r; }); }
resetReady();

function coreCmd() {
  try { const c = JSON.parse(process.env.SNIFFDL_CORE_CMD || 'null'); if (Array.isArray(c) && c.length) return c; } catch (e) {}
  for (const exe of [path.join(V3, 'SniffDL.exe'), path.join(process.resourcesPath || V3, '..', 'SniffDL.exe')]) {
    if (process.platform === 'win32' && fs.existsSync(exe)) return [exe, '--core'];
  }
  const py = process.env.SNIFFDL_PYTHON || (process.platform === 'win32' ? 'python' : 'python3');
  return [py, path.join(V3, 'sniffdl_core.py')];
}
function readRuntime() {
  try {
    const j = JSON.parse(fs.readFileSync(RUNTIME, 'utf8'));
    if (!j.port || !j.pid) return null;
    try { process.kill(j.pid, 0); } catch (e) { return null; }
    return j;
  } catch (e) { return null; }
}
function rpcTo(info, method, params, timeout) {
  return new Promise((resolve, reject) => {
    const data = Buffer.from(JSON.stringify({ jsonrpc: '2.0', id: Date.now(), method, params: params || [] }));
    const req = http.request({ host: '127.0.0.1', port: info.port, path: '/jsonrpc', method: 'POST', timeout: timeout || 4000,
      headers: { 'Content-Type': 'application/json', 'Content-Length': data.length, 'X-SniffDL-Token': info.ui_token || '' } }, (res) => {
      let b = ''; res.setEncoding('utf8'); res.on('data', (c) => { b += c; });
      res.on('end', () => { try { const j = JSON.parse(b); if (j.error) reject(new Error(j.error.message || 'rpc')); else resolve(j.result); } catch (e) { reject(e); } });
    });
    req.on('timeout', () => req.destroy(new Error('timeout')));
    req.on('error', reject); req.end(data);
  });
}
function rpc(method, ...params) { return coreReady.then((info) => rpcTo(info, method, params)); }

async function startCore() {
  const old = readRuntime();
  if (old) {
    try { const r = await rpcTo(old, 'sniffdl.ping', [], 2000); if (r && r.ok) { setCore({ proc: null, info: old, attached: true }); return; } } catch (e) {}
  }
  const cmd = coreCmd();
  const args = cmd.slice(1).concat(['--runtime', RUNTIME, '--ui-token', crypto.randomBytes(16).toString('hex'), '--host', 'electron',
    '--ui-dir', path.join(V3, 'ui'), '--parent-pid', String(process.pid), '--watch-stdin']);
  if (CORE_PORT) args.push('--port', String(CORE_PORT));
  if (DATA_DIR) args.push('--data-dir', path.resolve(DATA_DIR));
  const env = Object.assign({}, process.env, { PYTHONIOENCODING: 'utf-8', PYTHONUNBUFFERED: '1', SNIFFDL_HOST: 'electron',
    SNIFFDL_PET_PORT: petListening ? String(CTRL_PORT) : '', SNIFFDL_PET_TOKEN: petListening ? TOKEN : '' });
  if (!petListening) { delete env.SNIFFDL_PET_PORT; delete env.SNIFFDL_PET_TOKEN; }
  dlog('core spawn ' + JSON.stringify([cmd[0]].concat(args.map((a) => (/^[0-9a-f]{32}$/.test(a) ? '***' : a)))));
  let proc;
  try { proc = spawn(cmd[0], args, { cwd: V3, env, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true }); }
  catch (e) { dlog('core spawn err ' + e); return; }
  const me = { proc, info: null, attached: false };
  core = me;
  let buf = '';
  proc.stdout.setEncoding('utf8');
  proc.stdout.on('data', (c) => {
    buf += c; let i;
    while ((i = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1);
      if (line.startsWith('SNIFFDL_READY ')) {
        try {
          const info = JSON.parse(line.slice(14));
          if (info.attached) { dlog('core: 已有内核在跑，复用 pid=' + info.pid); setCore({ proc: null, info, attached: true }); }
          else setCore(Object.assign(me, { info }));
        } catch (e) { dlog('bad READY ' + line.slice(0, 200)); }
      }
    }
  });
  proc.stderr.setEncoding('utf8');
  proc.stderr.on('data', (c) => dlog('core! ' + String(c).trim().slice(0, 400)));
  proc.on('error', (e) => dlog('core proc error ' + e));
  proc.on('exit', (code, sig) => {
    dlog('core exit code=' + code + ' sig=' + sig);
    if (core !== me) return;                 // 已转为复用别的内核 / 已被替换
    core = null;
    if (quitting) return;
    resetReady();
    if (coreRestarts < 1) { coreRestarts++; dlog('core 意外退出 → 自动重启一次'); setTimeout(startCore, 800); }
    else { notify('SniffDL 内核已退出', '自动重启后又退出了，请从托盘「退出」后重新打开 SniffDL。'); buildTray(); }
  });
}
function setCore(c) {
  core = c;
  dlog('core ready port=' + c.info.port + ' pid=' + c.info.pid + (c.attached ? ' (attached)' : ''));
  coreReadyResolve(c.info);
  refreshTurtle(); buildTray();
}
function stopCore() {
  return new Promise((resolve) => {
    const c = core;
    if (!c || c.attached || !c.proc) { resolve(); return; }
    const p = c.proc;
    if (p.exitCode !== null) { resolve(); return; }
    let done = false;
    const fin = () => { if (!done) { done = true; resolve(); } };
    p.once('exit', fin);
    if (c.info) rpcTo(c.info, 'sniffdl.core.shutdown', [], 1500).catch(() => {});
    try { p.stdin.end(); } catch (e) {}
    setTimeout(() => { if (p.exitCode === null) { try { p.kill(); } catch (e) {} } }, 4000);
    setTimeout(() => { if (p.exitCode === null) { try { p.kill('SIGKILL'); } catch (e) {} } fin(); }, 6500);
  });
}

// ======================================================================= 主窗
let mainWin = null;
function createMain(show) {
  mainWin = new BrowserWindow({
    width: 1320, height: 860, minWidth: 980, minHeight: 640, show: false, title: 'SniffDL',
    backgroundColor: '#f6f7fb', icon: path.join(HERE, 'icon.png'), autoHideMenuBar: true,
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: false,
      preload: path.join(HERE, 'preload.js'), backgroundThrottling: false },
  });
  mainWin.setMenuBarVisibility(false);
  mainWin.webContents.on('console-message', (e, level, msg) => { if (level >= 2) dlog('ui L' + level + ' ' + String(msg).slice(0, 300)); });
  mainWin.webContents.on('did-fail-load', (e, code, desc, u) => dlog('ui did-fail-load ' + code + ' ' + desc + ' ' + u));
  mainWin.webContents.on('render-process-gone', (e, d) => dlog('ui render-gone ' + JSON.stringify(d)));
  mainWin.webContents.setWindowOpenHandler(({ url }) => { if (/^https?:/.test(url)) shell.openExternal(url); return { action: 'deny' }; });
  mainWin.webContents.on('will-navigate', (e, url) => { if (!url.startsWith('file:')) { e.preventDefault(); if (/^https?:/.test(url)) shell.openExternal(url); } });
  mainWin.on('close', (e) => { if (!quitting) { e.preventDefault(); mainWin.hide(); buildTray(); } });
  mainWin.on('show', buildTray); mainWin.on('hide', buildTray);
  mainWin.loadFile(UI_INDEX);
  if (show) mainWin.once('ready-to-show', () => showMain());
}
function showMain() {
  if (!mainWin || mainWin.isDestroyed()) return;
  if (mainWin.isMinimized()) mainWin.restore();
  mainWin.show(); mainWin.focus();
}

// ======================================================================= 通知 / 托盘
function notify(title, body) {
  try {
    if (!Notification.isSupported()) return false;
    const n = new Notification({ title: String(title || 'SniffDL'), body: String(body || ''), icon: path.join(HERE, 'icon.png') });
    n.on('click', showMain); n.show(); return true;
  } catch (e) { dlog('notify err ' + e); return false; }
}
let turtleOn = false;
function refreshTurtle() { rpc('sniffdl.settings.get').then((s) => { turtleOn = !!(s && ((s.values && s.values.turtle) || s.turtle)); buildTray(); }).catch(() => {}); }
async function importClipboard() {
  const text = (clipboard.readText() || '').trim();
  if (!text) { notify('剪贴板是空的', '先复制下载链接再点「从剪贴板导入」。'); return; }
  try {
    const r = await rpc('sniffdl.clip.offer', text);
    if (r && r.taken) showMain(); else notify('剪贴板里没找到可下载的链接', text.slice(0, 80));
  } catch (e) { notify('导入失败', String(e.message || e)); }
}
function buildTray() {
  if (!tray) return;
  const petVis = !!(petWin && !petWin.isDestroyed() && petWin.isVisible());
  const ready = !!(core && core.info);
  const call = (m, ...p) => () => rpc(m, ...p).catch((e) => notify('操作失败', String(e.message || e)));
  try {
    tray.setToolTip('SniffDL' + (ready ? '' : '（内核启动中…）'));
    tray.setContextMenu(Menu.buildFromTemplate([
      { label: '显示主窗口', click: showMain },
      { type: 'separator' },
      { label: '开始全部', enabled: ready, click: call('sniffdl.all.resume') },
      { label: '停止全部', enabled: ready, click: call('sniffdl.all.pause') },
      { label: '乌龟模式', type: 'checkbox', checked: turtleOn, enabled: ready,
        click: (it) => rpc('sniffdl.turtle', !!it.checked).then(() => { turtleOn = !!it.checked; buildTray(); }).catch(() => refreshTurtle()) },
      { label: '从剪贴板导入', enabled: ready, click: importClipboard },
      { type: 'separator' },
      { label: petVis ? '隐藏大肥鱼' : '显示大肥鱼', enabled: !!petWin, click: () => showPet(!petVis) },
      { label: '切换 Q版 / 正常版', enabled: !!petWin, click: toggleChibi },
      { type: 'separator' },
      { label: '退出 SniffDL', click: quit },
    ]));
  } catch (e) { dlog('tray menu err ' + e); }
}

async function quit() {
  if (quitting) return;
  quitting = true;
  dlog('quit');
  try { globalShortcut.unregisterAll(); } catch (e) {}
  try { if (tray) { const t = tray; tray = null; t.destroy(); } } catch (e) {}
  try { if (mainWin && !mainWin.isDestroyed()) mainWin.hide(); } catch (e) {}
  try { if (petWin && !petWin.isDestroyed()) petWin.hide(); } catch (e) {}
  await stopCore();
  dlog('bye');
  app.exit(0);
}

// ======================================================================= IPC（preload → window.sniffdlHost）
function senderWin(e) { return BrowserWindow.fromWebContents(e.sender) || mainWin; }
function registerHostIpc() {
  ipcMain.handle('host:conn', async () => {
    const info = await Promise.race([coreReady, new Promise((r) => setTimeout(() => r(null), 20000))]);
    return info ? { port: info.port, token: info.ui_token, pid: info.pid, version: info.version } : null;
  });
  ipcMain.handle('host:pickDir', async (e, title) => {
    const r = await dialog.showOpenDialog(senderWin(e), { title: title || '选择文件夹', properties: ['openDirectory', 'createDirectory'] });
    return r.canceled ? null : r.filePaths[0];
  });
  ipcMain.handle('host:pickFile', async (e, filters) => {
    const f = Array.isArray(filters) ? filters : (filters ? [filters] : []);
    const r = await dialog.showOpenDialog(senderWin(e), { title: '选择文件', properties: ['openFile'], filters: f.length ? f : undefined });
    return r.canceled ? null : r.filePaths[0];
  });
  ipcMain.handle('host:saveFile', async (e, name, text) => {
    const r = await dialog.showSaveDialog(senderWin(e), { title: '保存', defaultPath: String(name || 'sniffdl.json') });
    if (r.canceled || !r.filePath) return null;
    if (text !== undefined && text !== null) fs.writeFileSync(r.filePath, typeof text === 'string' ? text : JSON.stringify(text, null, 2), 'utf8');
    return r.filePath;
  });
  ipcMain.handle('host:notify', (e, title, body) => notify(title, body));
  ipcMain.on('host:show', showMain);
  ipcMain.on('host:quit', () => quit());
  ipcMain.on('host:progress', (e, p) => { try { if (mainWin) mainWin.setProgressBar(typeof p === 'number' && p >= 0 ? Math.min(1, p) : -1); } catch (x) {} });
}

// ======================================================================= 启动
let petListening = false;
function startPetCtrl() {
  return new Promise((resolve) => {
    const ctrl = http.createServer((req, res) => {
      let data = '';
      req.on('data', (c) => { data += c; if (data.length > 1e6) req.destroy(); });
      req.on('end', () => { let body = {}; try { body = JSON.parse(data || '{}'); } catch (e) {} handleApi(req, res, body); });
    });
    let tried0 = false;
    ctrl.on('error', (e) => {
      dlog('ctrl listen error ' + e);
      if (!tried0 && e.code === 'EADDRINUSE') { tried0 = true; ctrl.listen(0, '127.0.0.1'); } else resolve(false);
    });
    ctrl.on('listening', () => {
      CTRL_PORT = ctrl.address().port; petListening = true;
      try {
        fs.mkdirSync(path.dirname(STATE), { recursive: true });
        fs.writeFileSync(STATE, JSON.stringify({ port: CTRL_PORT, token: TOKEN, skin, pid: process.pid,
          ts: Math.floor(Date.now() / 1000), engine: 'electron' }));
      } catch (e) { dlog('state write err ' + e); }
      dlog('ctrl listening ' + CTRL_PORT);
      resolve(true);
    });
    ctrl.listen(CTRL_PORT, '127.0.0.1');
  });
}

if (GOT_LOCK) app.whenReady().then(async () => {
  ipcMain.on('pet-cmd', (e, a) => {
    if (a === 'hide') showPet(false);
    else if (a === 'quit') showPet(false);              // 桌宠菜单「退出」只收起桌宠；退出整个 SniffDL 走托盘
    else if (a === 'chibi') toggleChibi();
    else if (a === 'hit:1') setIgnore(false);           // 页面收到真实 mousemove（Windows forward）时直接报命中
    else if (a === 'hit:0' && !clickThrough) setIgnore(true);
    else if (a === 'main') showMain();
  });
  registerHostIpc();
  try {
    const ico = path.join(HERE, 'icon.png');
    tray = new Tray(fs.existsSync(ico) ? nativeImage.createFromPath(ico) : nativeImage.createEmpty());
    tray.on('click', showMain);
    tray.on('double-click', showMain);
  } catch (e) { dlog('tray err ' + e); }
  if (!NO_PET) await startPetCtrl();
  startCore();
  createMain(!NO_MAIN);
  if (!NO_PET) createWindow();
  buildTray();
  setInterval(() => { if (core && core.info && !quitting) refreshTurtle(); }, 15000);   // 界面里切了乌龟模式 → 托盘勾选跟上
  try {
    const ok = globalShortcut.register('Control+Alt+H', () => showPet(!(petWin && petWin.isVisible())));
    dlog('hotkey Control+Alt+H register=' + ok);
  } catch (e) { dlog('hotkey err ' + e); }
});
app.on('second-instance', () => { dlog('second-instance → 显示主窗'); showMain(); });
app.on('window-all-closed', () => { /* 托盘常驻：关窗不退 */ });
app.on('before-quit', (e) => { if (!quitting) { e.preventDefault(); quit(); } });
app.on('will-quit', () => { try { globalShortcut.unregisterAll(); } catch (e) {} });
process.on('SIGTERM', () => quit());
process.on('SIGINT', () => quit());
