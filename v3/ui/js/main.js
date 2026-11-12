// 外壳：侧栏导航 + 计数、速度卡 + 乌龟、状态栏、主题、快捷键、SSE 事件接入、占位视图（待确认 / 队列 / 设置）
import { $, h, esc, size, speed, when, basename } from './util.js';
import { icon } from './icons.js';
import * as api from './api.js';
import { S, sub, changed, loadAll, setSetting, tasksArr } from './store.js';
import { toast } from './ui/toast.js';
import { drawSpeed } from './ui/chart.js';
import { applyBg } from './ui/background.js';
import { mountAdder, setText, focusAdder } from './views/adder.js';
import { mountTasks, FILTERS, focusSearch, focusList } from './views/tasks.js';
import { openDetail, closeDetail, detailOpen } from './views/detail.js';
import { mountSettings, renderSettings, echoSettings } from './views/settings.js';
import { mountQueues, renderQueues } from './views/queues.js';
import { mountPending, renderPending } from './views/pending.js';
import { mountStats, renderStats, mountLogs, renderLogs, paintLogs } from './views/insight.js';

// ---------------- 导航
const NAV = [
  { id: 'all', label: '全部', icon: 'list', view: 'downloads', filter: 'all', kind: 'all', count: () => S.tasks.size },
  { id: 'active', label: '下载中', icon: 'download', view: 'downloads', filter: 'active', kind: 'all', count: () => tasksArr().filter(FILTERS.active.test).length },
  { id: 'done', label: '已完成', icon: 'checkc', view: 'downloads', filter: 'done', kind: 'all', count: () => tasksArr().filter(FILTERS.done.test).length },
  { id: 'bt', label: 'BT', icon: 'magnet', view: 'downloads', filter: 'all', kind: 'bt', count: () => tasksArr().filter((t) => t.kind === 'bt').length },
  { grp: '工具' },
  { id: 'pending', label: '待确认', icon: 'inbox', view: 'pending', badge: () => (S.pending || []).length + (S.grabber?.size || 0) },
  { id: 'queues', label: '队列', icon: 'layers', view: 'queues', count: () => (S.queues || []).length },
  { id: 'stats', label: '统计', icon: 'clock', view: 'stats' },
  { id: 'logs', label: '日志', icon: 'doc', view: 'logs' },
  { id: 'settings', label: '设置', icon: 'gear', view: 'settings' },
];
const VIEW_EL = { downloads: 'vDownloads', pending: 'vPending', queues: 'vQueues', stats: 'vStats', logs: 'vLogs', settings: 'vSettings' };
let navBtns = new Map();

function currentNav() {
  if (S.view !== 'downloads') return S.view;
  if (S.kind === 'bt' && S.filter === 'all') return 'bt';
  return NAV.find((n) => n.view === 'downloads' && n.filter === S.filter && n.kind === 'all')?.id || 'all';
}

function buildNav() {
  const nav = $('#nav'); nav.innerHTML = '';
  NAV.forEach((n, i) => {
    if (n.grp) { nav.append(h('div.grp', n.grp)); return; }
    const b = h('button', { 'data-nav': n.id, title: `${n.label}（Alt+${navIndex(n.id)}）`, on: { click: () => go(n.id) } },
      h('span', { html: icon(n.icon) }), h('span', n.label), h('span.' + (n.badge ? 'badge' : 'cnt')));
    navBtns.set(n.id, b); nav.append(b);
  });
}
const navIndex = (id) => NAV.filter((n) => !n.grp).findIndex((n) => n.id === id) + 1;

export function go(id) {
  const n = NAV.find((x) => x.id === id);
  if (!n) return;
  S.view = n.view;
  if (n.view === 'downloads') { S.filter = n.filter; S.kind = n.kind; }
  showView(); changed('view');
}

function showView() {
  for (const [v, elId] of Object.entries(VIEW_EL)) $('#' + elId).classList.toggle('hidden', S.view !== v);
  if (S.view === 'pending') renderPending();
  if (S.view === 'queues') renderQueues();
  if (S.view === 'settings') renderSettings();
  if (S.view === 'stats') renderStats();
  if (S.view === 'logs') renderLogs();
  paintNav();
}

function paintNav() {
  const cur = currentNav();
  for (const n of NAV) {
    if (n.grp) continue;
    const b = navBtns.get(n.id);
    b.classList.toggle('on', n.id === cur);
    b.setAttribute('aria-current', n.id === cur ? 'page' : 'false');
    const c = b.lastChild;
    if (n.badge) { const v = n.badge(); c.textContent = v > 99 ? '99+' : String(v); c.classList.toggle('hidden', !v); }
    else if (n.count) c.textContent = String(n.count());
    else c.textContent = '';
  }
}

// ---------------- 速度卡 / 乌龟 / 状态栏
function paintSpeed() {
  const st = S.stats || {};
  $('#spdDown').textContent = '↓ ' + speed(st.down || 0);
  $('#spdUp').textContent = '↑ ' + speed(st.up || 0);
  $('#spdLimit').textContent = st.limit_kbps ? `限速 ${st.limit_kbps >= 1024 ? (st.limit_kbps / 1024).toFixed(1) + ' MB/s' : st.limit_kbps + ' KB/s'}` : '不限速';
  const tb = $('#btnTurtle');
  tb.classList.toggle('on', !!st.turtle);
  tb.setAttribute('aria-pressed', st.turtle ? 'true' : 'false');
  tb.innerHTML = icon('turtle', 's14') + (st.turtle ? '乌龟中' : '乌龟');
  drawSpeed($('#spdChart'), S.hist);
}

async function toggleTurtle() {
  try {
    const r = await api.rpc('sniffdl.turtle');
    const on = r?.changed?.turtle ?? !S.stats.turtle;
    S.stats = { ...S.stats, turtle: on }; changed('stats');
    try { S.stats = await api.rpc('sniffdl.stats.now'); changed('stats'); } catch (e) {}
    toast({ kind: on ? 'warn' : 'ok', title: on ? '乌龟模式已开' : '乌龟模式已关', body: on ? `下载速度临时压到 ${S.settings?.turtle_kbps || 512} KB/s，给别的事让路` : '恢复正常速度' });
  } catch (e) { toast({ kind: 'err', title: '切换失败', body: e.message }); }
}

const CONN_TXT = { online: '内核在线', connecting: '连接中…', reconnecting: '重连中…', error: '未连接', quit: '内核已退出' };
function paintStatus() {
  const st = S.stats || {};
  const cs = api.connState();
  const ci = api.connInfo();
  const el = $('#status');
  el.innerHTML = '';
  const sep = () => h('span.sep');
  el.append(...[
    h('span', h('span.dot' + (cs === 'online' ? '.on' : cs === 'error' || cs === 'quit' ? '.bad' : '')), CONN_TXT[cs] || cs),
    ci?.port ? h('span', '端口 ', h('b', String(ci.port))) : null, sep(),
    h('span', '下载中 ', h('b', String(st.active || 0)), '  排队 ', h('b', String(st.waiting || 0)), st.seeding ? h('span', '  做种 ', h('b', String(st.seeding))) : null,
      st.error ? h('span', '  失败 ', h('b', { style: { color: 'var(--err)' } }, String(st.error))) : null), sep(),
    h('span', '剩余磁盘 ', h('b#stDisk', st.disk_free ? size(st.disk_free) : '—')),
    h('div.grow'),
    st.power?.state === 'countdown' ? h('span', { style: { color: 'var(--warn)' } }, `全部完成后将${st.power.action}`) : null,
    st.turtle ? h('span', { style: { color: 'var(--warn)' } }, h('span', { html: icon('turtle', 's14') }), ' 乌龟模式') : null,
    h('span', '全局 ', h('b#stDown', '↓ ' + speed(st.down || 0)), '  ', h('b', '↑ ' + speed(st.up || 0))),
    st.limit_kbps ? h('span', '限速 ', h('b', st.limit_kbps + ' KB/s')) : null].filter(Boolean));
  // 任务栏进度（Electron）
  const act = tasksArr().filter((t) => t.status === 'active' && t.total);
  const tot = act.reduce((a, t) => a + t.total, 0), dn = act.reduce((a, t) => a + t.done, 0);
  api.host.setProgress(tot ? dn / tot : -1);
}

// ---------------- 主题
const mq = window.matchMedia ? matchMedia('(prefers-color-scheme: dark)') : null;
function applyTheme() {
  const pref = S.settings?.ui_theme || '跟随系统';
  const dark = pref === '深色' || (pref === '跟随系统' && mq?.matches);
  S.theme = dark ? 'dark' : 'light';
  document.documentElement.dataset.theme = S.theme;
  applyBg();
  paintSpeed();
}
mq?.addEventListener?.('change', applyTheme);
applyTheme();

// ---------------- 快捷键
function typing(e) { const t = e.target; return t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable); }
document.addEventListener('keydown', (e) => {
  const k = e.key.toLowerCase();
  const mod = e.ctrlKey || e.metaKey;
  if (document.querySelector('.modal')) return;
  if (mod && (k === 'n' || k === 'l')) { e.preventDefault(); closeDetail(); go(S.view === 'downloads' ? currentNav() : 'all'); focusAdder(); }
  else if (mod && k === 'f') { e.preventDefault(); if (S.view !== 'downloads') go('all'); focusSearch(); }
  else if (mod && k === 't') { e.preventDefault(); toggleTurtle(); }
  else if (mod && k === ',') { e.preventDefault(); go('settings'); }
  else if (e.altKey && /^[1-9]$/.test(e.key)) { e.preventDefault(); const n = NAV.filter((x) => !x.grp)[Number(e.key) - 1]; if (n) { closeDetail(); go(n.id); } }
  else if (k === '/' && !typing(e)) { e.preventDefault(); go(S.view === 'downloads' ? currentNav() : 'all'); focusSearch(); }
  else if (k === 'f1') { e.preventDefault(); showKeys(); }
});
// 在非输入区 Ctrl+V：直接进添加框识别
document.addEventListener('paste', (e) => {
  if (typing(e) || detailOpen() || document.querySelector('.modal')) return;
  const t = e.clipboardData?.getData('text') || '';
  if (!t.trim()) return;
  e.preventDefault();
  if (S.view !== 'downloads') go('all');
  setText(t.trim());
});
function showKeys() {
  toast({ title: '快捷键', ms: 9000, body: 'Ctrl+N 添加 · Ctrl+F 搜索 · Ctrl+T 乌龟 · Alt+1~9 切换页面 · 空格 开始/暂停 · Enter 详情 · Del 删除 · Ctrl+, 设置' });
}

// ---------------- SSE 事件接入
document.addEventListener('sdl:nav', (e) => { const v = e.detail?.view; const n = NAV.find((x) => x.id === v || x.view === v); if (n) go(n.id); });
api.on('notify', (d) => {
  if (!d) return;
  const kind = d.kind === 'error' ? 'err' : d.kind === 'done' ? 'ok' : 'info';
  if (d.kind === 'started') return;   // 添加时 adder 已经提示过
  const actions = d.gid && S.tasks.has(d.gid) ? [{ label: '详情', fn: () => openDetail(d.gid) }] : undefined;
  if (d.kind === 'done' && d.gid) actions?.unshift({ label: '打开', pri: true, fn: () => api.rpc('sniffdl.task.open', d.gid) });
  toast({ kind, title: d.title || '', body: d.body || '', actions });
  if (document.hidden && S.settings?.notify !== false) api.host.notify(d.title || 'SniffDL', d.body || '');
});
api.on('clip', (d) => {
  if (!d?.text) return;
  toast({ title: `剪贴板里有${d.count > 1 ? ' ' + d.count + ' 个' : ''}可下载的链接`, body: (d.items?.[0]?.url || d.items?.[0] || d.text).toString().slice(0, 140),
    actions: [{ label: '识别下载', pri: true, fn: () => { api.host.showWindow(); go('all'); setText(d.text); } }] });
});
api.on('intercept', (d) => {
  api.host.showWindow();
  toast({ kind: 'warn', title: '浏览器有个下载等你确认', body: (d?.filename || d?.url || '') + (d?.size ? ' · ' + size(d.size) : ''),
    actions: [{ label: '去确认', pri: true, fn: () => go('pending') }] });
});
let powerToast = null;
api.on('power', (d) => {
  S.stats = { ...S.stats, power: d }; changed('stats');
  powerToast?.close(); powerToast = null;
  if (d?.state === 'countdown') {
    powerToast = toast({ kind: 'warn', title: `全部下载完成，60 秒后${d.action}`, ms: 60000,
      actions: [{ label: '取消', pri: true, fn: () => api.rpc('sniffdl.power.cancel').then(() => toast({ title: '已取消' })) }] });
  }
});
api.on('limit', () => api.rpc('sniffdl.stats.now').then((s) => { S.stats = s; changed('stats'); }).catch(() => {}));
api.on('settings', (d) => { if (d?.changed && ('ui_theme' in d.changed)) applyTheme(); if (d?.changed && ('turtle' in d.changed || 'speed_limit_kbps' in d.changed)) api.rpc('sniffdl.stats.now').then((s) => { S.stats = s; changed('stats'); }).catch(() => {}); if (S.view === 'settings') echoSettings(d?.changed); });
api.on('quit', () => { bootShow('内核已退出', '关闭本窗口，或从 SniffDL 启动器重新打开', true); });
let wasOnline = false;
api.on('conn', (c) => {
  paintStatus();
  if (c.state === 'online') {
    bootHide();
    if (wasOnline) loadAll().catch(() => {});   // 断线重连：整表重拉，防止漏事件
    wasOnline = true;
  } else if (c.state === 'error') bootShow('连不上下载内核', c.detail || '', true);
});

function bootShow(msg, err, bad) {
  const b = $('#boot'); b.classList.remove('hidden');
  $('#bootMsg').textContent = msg; $('#bootErr').textContent = err || '';
  b.querySelector('.spin')?.classList.toggle('hidden', !!bad);
}
function bootHide() { $('#boot').classList.add('hidden'); }

// 队列页随任务变化刷新，节流到 1 秒，避免拖动 / 下拉操作时被频繁重绘打断
let qT = 0;
function queuesSoon() { if (qT) return; qT = setTimeout(() => { qT = 0; if (S.view === 'queues' && !document.querySelector('#vQueues .dragging, #vQueues .qtasks select:focus')) renderQueues(true); }, 1000); }

// ---------------- 订阅渲染
sub((d) => {
  if (d.has('tasks') || d.has('pending') || d.has('grabber') || d.has('queues') || d.has('view')) paintNav();
  if (d.has('stats')) { paintSpeed(); paintStatus(); }
  if (d.has('log') && S.view === 'logs') paintLogs();
  if (d.has('settings')) applyTheme();
  if (d.has('pending') || d.has('grabber')) { if (S.view === 'pending') renderPending(); }
  if (d.has('queues') && S.view === 'queues') renderQueues(!!document.querySelector('#vQueues .qedit :focus'));
  else if (d.has('tasks') && S.view === 'queues') queuesSoon();
  if (d.has('settings') && S.view === 'settings' && !document.querySelector('#vSettings .set')) renderSettings();
  if (d.has('view')) showView();
});
window.addEventListener('resize', () => paintSpeed());

// ---------------- 启动
async function boot() {
  buildNav();
  mountAdder($('#adder'));
  mountTasks($('#tasksHost'));
  mountSettings($('#vSettings')); mountQueues($('#vQueues')); mountPending($('#vPending')); mountStats($('#vStats')); mountLogs($('#vLogs'));
  $('#btnTurtle').addEventListener('click', toggleTurtle);
  paintSpeed(); paintStatus(); paintNav();
  await api.connect();
  try {
    await loadAll();
    const ver = (await api.rpc('sniffdl.snapshot').catch(() => null))?.version;
    if (ver) $('#brandSub').textContent = '大肥鱼下载器 · v' + ver;
  } catch (e) { toast({ kind: 'err', title: '读取数据失败', body: e.message }); }
  applyTheme(); showView(); paintStatus();
  bootHide();
  focusAdder();
  window.__sdlReady = true;
}
boot();
// 调试 / 测试用
window.__sdl = { S, go, openDetail, closeDetail };
