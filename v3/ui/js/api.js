// 内核连接：拿真实端口 + 界面令牌 → JSON-RPC 调用 + SSE 事件流（断线自动重连、补发）
// 连接来源优先级：Electron 宿主 preload（window.sniffdlHost）> pywebview 桥 > 地址栏 #port=&token= > 同源（内核直接托管 /ui/）
const listeners = new Map();
let conn = null, es = null, lastId = 0, state = 'connecting', retry = 0;

export function on(type, fn) { if (!listeners.has(type)) listeners.set(type, new Set()); listeners.get(type).add(fn); return () => listeners.get(type).delete(fn); }
function emit(type, data) { (listeners.get(type) || []).forEach((fn) => { try { fn(data); } catch (e) { console.error(type, e); } }); (listeners.get('*') || []).forEach((fn) => { try { fn(type, data); } catch (e) {} }); }
export const connState = () => state;
export const connInfo = () => conn;
function setState(s, detail) { state = s; emit('conn', { state: s, detail, conn }); }

export const host = {
  kind: window.sniffdlHost ? 'electron' : (window.pywebview ? 'webview' : 'browser'),
  async pickDir(title) {
    if (window.sniffdlHost?.pickDir) return window.sniffdlHost.pickDir(title);
    if (window.pywebview?.api?.pick_dir) return window.pywebview.api.pick_dir(title);
    return null;
  },
  async pickFile(filters) {
    if (window.sniffdlHost?.pickFile) return window.sniffdlHost.pickFile(filters);
    if (window.pywebview?.api?.pick_file) return window.pywebview.api.pick_file();
    return null;
  },
  async saveFile(name, text) {
    if (window.sniffdlHost?.saveFile) return window.sniffdlHost.saveFile(name, text);
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([text], { type: 'application/json' }));
    a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 2000);
    return name;
  },
  notify(title, body) {
    if (window.sniffdlHost?.notify) return window.sniffdlHost.notify(title, body);
    try { if ('Notification' in window && Notification.permission === 'granted') new Notification(title, { body }); } catch (e) {}
  },
  showWindow() { window.sniffdlHost?.show?.(); },
  setProgress(p) { window.sniffdlHost?.setProgress?.(p); },
  quit() { window.sniffdlHost?.quit?.(); },
};

async function discover() {
  if (window.sniffdlHost?.conn) { const c = await window.sniffdlHost.conn(); if (c?.port) return c; }
  if (window.pywebview?.api?.conn) { const c = await window.pywebview.api.conn(); if (c?.port) return typeof c === 'string' ? JSON.parse(c) : c; }
  const hp = new URLSearchParams(location.hash.slice(1) || location.search.slice(1));
  if (hp.get('token')) return { port: Number(hp.get('port') || location.port || 6800), token: hp.get('token') };
  return null;
}
async function waitPywebview() {
  if (window.pywebview?.api) return;
  await new Promise((r) => { window.addEventListener('pywebviewready', r, { once: true }); setTimeout(r, 1500); });
}

export async function connect() {
  setState('connecting');
  if (!window.sniffdlHost && /pywebview/i.test(navigator.userAgent + '') ) await waitPywebview();
  for (let i = 0; ; i++) {
    try { conn = await discover(); } catch (e) { conn = null; }
    if (conn) {
      try { await rpc('sniffdl.ping'); break; } catch (e) { setState('connecting', '内核还在启动…'); }
    } else if (i > 6) { setState('error', '没拿到内核地址：请从 SniffDL 启动器打开本界面'); }
    await new Promise((r) => setTimeout(r, Math.min(2000, 300 + i * 200)));
  }
  openEvents();
  return conn;
}

const base = () => `http://127.0.0.1:${conn.port}`;
let rid = 0;
export async function rpc(method, ...params) {
  if (!conn) throw new Error('未连接内核');
  const r = await fetch(base() + '/jsonrpc', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-SniffDL-Token': conn.token },
    body: JSON.stringify({ jsonrpc: '2.0', id: ++rid, method, params }),
  });
  const j = await r.json();
  if (j.error) { const e = new Error(j.error.message || 'RPC 错误'); e.code = j.error.code; throw e; }
  return j.result;
}

function openEvents() {
  try { es?.close(); } catch (e) {}
  es = new EventSource(`${base()}/events?t=${encodeURIComponent(conn.token)}&since=${lastId}`);
  es.onopen = () => { retry = 0; setState('online'); };
  es.onerror = async () => {
    try { es.close(); } catch (e) {}
    setState('reconnecting');
    retry++;
    await new Promise((r) => setTimeout(r, Math.min(5000, 400 * retry)));
    // 端口可能变了（设置里改端口 / 内核被宿主重启）：重新问宿主
    try { const c = await discover(); if (c?.port) conn = c; } catch (e) {}
    openEvents();
  };
  const types = ['snapshot', 'tasks', 'stats', 'log', 'notify', 'clip', 'intercept', 'grabber', 'settings', 'queues',
    'power', 'limit', 'port', 'hello', 'say', 'quit'];
  for (const t of types) {
    es.addEventListener(t, (ev) => {
      lastId = Number(ev.lastEventId) || lastId;
      let msg = {};
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (t === 'port' && msg.data?.port) { conn = { ...conn, port: msg.data.port }; setTimeout(openEvents, 200); }
      emit(t, msg.data);
    });
  }
}
