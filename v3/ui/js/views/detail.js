// 任务详情抽屉：概况（统计 + 速度曲线 + 错误一键处理 + 常规信息）/ 来源 / BT 文件（可勾选）/ 节点 / 日志
import { h, esc, size, speed, dur, when, pct, basename, copyText } from '../util.js';
import { icon } from '../icons.js';
import * as api from '../api.js';
import { S, sub } from '../store.js';
import { toast } from '../ui/toast.js';
import { drawSpeed } from '../ui/chart.js';
import { act, refreshUrl } from './tasks.js';

const PHASE_LABEL = { active: '下载中', queued: '排队中', hold: '稍后下载', retry: '等待重试', verifying: '校验中', seeding: '做种中',
  paused: '已暂停', complete: '已完成', error: '失败', waiting: '等待中', removed: '已移除' };
const KIND_LABEL = { file: '文件', video: '视频', bt: 'BT', live: '直播' };
const ICON_OF = { file: 'file', video: 'video', bt: 'magnet', live: 'live' };

// 每个任务的速度采样（打开抽屉前就开始记，打开时曲线不是空的）
const hist = new Map();
let lastSample = 0;
sub((d) => {
  if (!d.has('tasks')) return;
  const now = Date.now();
  if (now - lastSample < 900) return;
  lastSample = now;
  for (const t of S.tasks.values()) {
    if (t.status !== 'active' && !hist.has(t.gid)) continue;
    const a = hist.get(t.gid) || [];
    a.push({ t: now, down: t.speed || 0, up: t.up || 0 });
    if (a.length > 120) a.shift();
    hist.set(t.gid, a);
  }
  for (const g of hist.keys()) if (!S.tasks.has(g)) hist.delete(g);
});

let D = null;   // 当前抽屉 {gid, root, scrim, tab, detail, bt, timer, unsub, selDirty}

export function closeDetail() {
  if (!D) return;
  clearInterval(D.timer); D.unsub(); document.removeEventListener('keydown', D.key, true);
  D.scrim.remove(); D.root.remove(); D.prev?.focus?.();
  D = null;
}
export const detailOpen = () => !!D;

export function openDetail(gid) {
  if (D) closeDetail();
  const t = S.tasks.get(gid);
  if (!t) return;
  const scrim = h('div.scrim', { on: { click: closeDetail } });
  const root = h('div.drawer#detail', { role: 'dialog', 'aria-modal': 'true', 'aria-label': '任务详情' });
  D = { gid, root, scrim, tab: 'info', detail: null, bt: null, selDirty: false, prev: document.activeElement };
  D.key = (e) => { if (e.key === 'Escape' && !document.querySelector('.modal')) { e.preventDefault(); e.stopPropagation(); closeDetail(); } };
  document.addEventListener('keydown', D.key, true);
  document.body.append(scrim, root);
  build();
  D.unsub = sub((d) => { if (d.has('tasks') && D) { if (!S.tasks.has(D.gid)) { closeDetail(); return; } paint(); } });
  D.timer = setInterval(() => fetchAll(), 2000);
  fetchAll();
  root.tabIndex = -1; root.style.outline = 'none'; setTimeout(() => root.focus(), 30);
}

async function fetchAll() {
  if (!D) return;
  const gid = D.gid, t = S.tasks.get(gid);
  // 已完成/失败的非 BT 任务不必反复拉
  if (D.detail && t && !['active', 'verifying'].includes(t.status) && t.kind !== 'bt' && D._fetched) return;
  try {
    const r = await api.rpc('sniffdl.task.detail', gid);
    if (D && D.gid === gid && r.ok) D.detail = r.task;
    if (t?.kind === 'bt') {
      const b = await api.rpc('sniffdl.bt.detail', gid);
      if (D && D.gid === gid && b.ok) D.bt = b;
    }
    if (D) { D._fetched = true; paint(); }
  } catch (e) { /* 断线时静默，状态栏会提示 */ }
}

function tabsFor(t) {
  const tabs = [['info', '概况'], ['src', '来源']];
  if (t.kind === 'bt') tabs.push(['files', '文件'], ['peers', '节点']);
  tabs.push(['log', '日志']);
  return tabs;
}

function build() {
  const t = S.tasks.get(D.gid);
  D.root.innerHTML = '';
  D.head = h('div.dh',
    h('div.ico', { html: icon(ICON_OF[t.kind] || 'file', 's18'), style: { width: '34px', height: '34px', borderRadius: '10px', display: 'grid', placeItems: 'center', flex: 'none', background: 'var(--acc-soft)', color: 'var(--acc)' } }),
    h('div.grow', h('div.t#detailTitle'), h('div.row', { style: { marginTop: '6px', flexWrap: 'wrap' } }, h('span#detailPill'), h('span.chip', KIND_LABEL[t.kind] || t.kind), h('span.muted#detailSub', { style: { fontSize: '12px' } }))),
    h('button.btn.icon.sm.ghost', { title: '关闭（Esc）', 'aria-label': '关闭', html: icon('x'), on: { click: closeDetail } }));
  D.tabsEl = h('div.tabs', { role: 'tablist' });
  D.body = h('div.db');
  D.foot = h('div.reco-foot');
  D.root.append(D.head, D.tabsEl, D.body, D.foot);
  renderTabs();
  paint(true);
}

function renderTabs() {
  const t = S.tasks.get(D.gid);
  D.tabsEl.innerHTML = '';
  for (const [k, l] of tabsFor(t)) {
    D.tabsEl.append(h('button', { role: 'tab', class: D.tab === k ? 'on' : '', 'aria-selected': D.tab === k ? 'true' : 'false', 'data-tab': k,
      on: { click: () => { D.tab = k; renderTabs(); paint(true); } } }, l));
  }
}

function paint(full) {
  if (!D) return;
  const t = S.tasks.get(D.gid);
  if (!t) return;
  const x = Object.assign({}, D.detail || {}, t);   // 列表数据更新更勤，覆盖详情里的同名字段
  const nm = t.name || basename((t.uri || '').split('?')[0]) || t.uri;
  const tt = D.root.querySelector('#detailTitle'); if (tt.textContent !== nm) tt.textContent = nm;
  const pl = D.root.querySelector('#detailPill'); const ph = t.phase;
  if (pl._p !== ph) { pl._p = ph; pl.innerHTML = `<span class="pill ${ph}">${PHASE_LABEL[ph] || ph}</span>`; }
  D.root.querySelector('#detailSub').textContent = t.total ? `${size(t.done)} / ${size(t.total)}` : (t.done ? size(t.done) : '');
  paintFoot(t);
  const fn = { info: paintInfo, src: paintSrc, files: paintFiles, peers: paintPeers, log: paintLog }[D.tab] || paintInfo;
  fn(x, full);
}

function paintFoot(t) {
  const key = t.phase + '|' + (t.status === 'complete' ? 1 : 0);
  if (D.foot._k === key) return;
  D.foot._k = key; D.foot.innerHTML = '';
  const B = (ic, label, fn, cls = '') => h('button.btn.sm' + cls, { on: { click: fn } }, h('span', { html: icon(ic, 's14') }), label);
  const ph = t.phase;
  if (['active', 'queued', 'retry', 'seeding'].includes(ph)) D.foot.append(B('pause', ph === 'seeding' ? '停止做种' : '暂停', () => act('pause', [t.gid])));
  else if (['paused', 'error', 'hold'].includes(ph)) D.foot.append(B('play', ph === 'error' ? '重试' : '开始', () => act('resume', [t.gid]), '.pri'));
  if (t.status === 'complete') D.foot.append(B('file', '打开文件', () => api.rpc('sniffdl.task.open', t.gid).then(okOrErr)));
  D.foot.append(B('folder', '打开文件夹', () => api.rpc('sniffdl.task.reveal', t.gid).then(okOrErr)));
  D.foot.append(h('div.grow'));
  D.foot.append(B('copy', '复制链接', () => copyText(t.uri).then(() => toast({ kind: 'ok', title: '链接已复制' }))));
}
function okOrErr(r) { if (r && r.ok === false) toast({ kind: 'err', title: '打不开', body: r.error || r.path || '' }); }

// ---------- 概况
function paintInfo(x, full) {
  if (full || !D.body.querySelector('#dInfo')) {
    D.body.innerHTML = '';
    D.body.append(h('div#dInfo',
      h('div#dErr'),
      h('div.statgrid#dStats'),
      h('div.card', { style: { padding: '10px 12px 6px', marginBottom: '14px' } },
        h('div.row', { style: { fontSize: '12px', color: 'var(--ink3)' } }, h('span', '速度曲线'), h('div.grow'), h('span#dSpdNow.mono')),
        h('div', { style: { position: 'relative' } },
          h('canvas#dChart', { style: { width: '100%', height: '90px', display: 'block', marginTop: '6px' } }),
          h('div.muted#dChartEmpty', { style: { position: 'absolute', inset: '0', display: 'grid', placeItems: 'center', fontSize: '12px' } }, '还没有速度记录（界面打开期间的下载才会画曲线）'))),
      h('dl.kv#dKv')));
  }
  // 错误与一键处理
  const errEl = D.body.querySelector('#dErr');
  const ek = x.status === 'error' && x.err ? JSON.stringify(x.err) : (x.cert_error ? 'cert' + x.cert_error : '');
  if (errEl._k !== ek) {
    errEl._k = ek; errEl.innerHTML = '';
    if (x.status === 'error' && x.err) errEl.append(errBox(x));
    else if (x.cert_error) errEl.append(h('div.warnbox', { style: { marginBottom: '12px' } }, h('span', { html: icon('shield', 's14') }), h('div', '证书异常：' + x.cert_error)));
  }
  // 统计格
  const isBt = x.kind === 'bt';
  const p = x.total ? pct(x.done, x.total) : (x.status === 'complete' ? 100 : 0);
  const cells = [
    ['进度', x.total ? p.toFixed(1) + '%' : (x.status === 'complete' ? '100%' : '—')],
    ['速度', x.phase === 'seeding' ? '↑ ' + speed(x.up || 0) : (x.status === 'active' ? speed(x.speed) : '—')],
    ['剩余', x.status === 'active' && x.eta >= 0 ? dur(x.eta) : (x.phase === 'retry' && x.retry_at ? dur(x.retry_at - Date.now() / 1000) + '后重试' : '—')],
    ['已下载', size(x.done)],
    ['总大小', x.total ? size(x.total) : '未知'],
    isBt ? ['分享率', (D.bt?.ratio ?? x.ratio ?? 0).toFixed(2)] : ['连接数', x.connections ? String(x.connections) : (x.conns || '—')],
  ];
  const sg = D.body.querySelector('#dStats');
  const sk = JSON.stringify(cells);
  if (sg._k !== sk) { sg._k = sk; sg.innerHTML = cells.map(([k, v]) => `<div><small>${k}</small><b>${esc(v)}</b></div>`).join(''); }
  // 速度曲线
  const cv = D.body.querySelector('#dChart');
  const hs = hist.get(x.gid) || [];
  D.body.querySelector('#dSpdNow').textContent = x.status === 'active' ? speed(x.speed) : (hs.length ? '峰值 ' + speed(Math.max(...hs.map((s) => s.down))) : '');
  if (hs.length >= 2) drawSpeed(cv, hs);
  else if (!cv._empty) { cv._empty = 1; drawSpeed(cv, []); }
  if (hs.length >= 2) cv._empty = 0;
  D.body.querySelector('#dChartEmpty').classList.toggle('hidden', hs.length >= 2);
  // 常规信息
  const rows = [
    ['保存到', x.path || x.dir || '—'],
    ['添加于', when(x.added)],
    x.started ? ['开始于', when(x.started)] : null,
    x.finished ? ['完成于', when(x.finished)] : null,
    ['队列', (S.queues || []).find((q) => q.id === x.queue)?.name || x.queue || '—'],
    ['来源', x.src === 'browser' ? '浏览器接管' : x.src === 'clip' ? '剪贴板' : '手动添加'],
    x.format ? ['格式', x.format] : null,
    x.limit_kbps ? ['单任务限速', x.limit_kbps + ' KB/s'] : null,
    x.resumable === false ? ['断点续传', '不支持（暂停后从头下）'] : x.resumable ? ['断点续传', '支持'] : null,
    x.checksum ? ['校验', x.checksum + (x.checksum_ok === true ? '  ✓ 通过' : x.checksum_ok === false ? '  ✗ 不一致' : '')] : null,
    x.retries ? ['重试次数', String(x.retries)] : null,
    x.note ? ['备注', x.note] : null,
    x.msg && x.status === 'active' ? ['状态信息', x.msg] : null,
    isBt && D.bt?.infohash ? ['InfoHash', D.bt.infohash] : null,
    isBt && D.bt ? ['已上传', size(D.bt.uploaded)] : null,
    ['任务 ID', x.gid],
  ].filter(Boolean);
  const kv = D.body.querySelector('#dKv'); const kk = JSON.stringify(rows);
  if (kv._k !== kk) {
    kv._k = kk; kv.innerHTML = '';
    for (const [k, v] of rows) kv.append(h('dt', k), h('dd', { class: /^(InfoHash|任务 ID|保存到|校验)$/.test(k) ? 'mono' : '' }, v));
  }
}

function errBox(x) {
  const e = x.err;
  const ACT = {
    refresh: ['refresh', '刷新下载地址', () => refreshUrl(x.gid)],
    retry: ['refresh', '重试', () => act('resume', [x.gid])],
    space: ['folder', '打开保存目录清理', () => api.rpc('sniffdl.task.reveal', x.gid).then(okOrErr)],
    proxy: ['gear', '去设置代理', () => nav('settings', '在「网络」里填代理，保存后回来点重试')],
    login: ['key', '去设置 Cookie', () => nav('settings', '在「网站登录 / Cookie」里导入浏览器 Cookie，保存后回来点重试')],
  };
  const a = ACT[e.action];
  return h('div.warnbox.err', { style: { marginBottom: '12px', alignItems: 'center' } },
    h('span', { html: icon('alert', 's18') }),
    h('div.grow', h('b', e.title), h('div', { style: { fontSize: '12px', opacity: .9 } }, e.hint),
      e.raw ? h('details.more', { style: { marginTop: '4px' } }, h('summary', h('span', { html: icon('chev', 's14') }), '原始报错'), h('div.mono', { style: { userSelect: 'text', wordBreak: 'break-all' } }, e.raw)) : null),
    a ? h('button.btn.sm.danger.solid#dFix', { on: { click: a[2] } }, h('span', { html: icon(a[0], 's14') }), a[1]) : null,
    e.action !== 'retry' && a ? h('button.btn.sm', { on: { click: () => act('resume', [x.gid]) } }, '重试') : null);
}

function nav(view, hint) {
  closeDetail();
  document.dispatchEvent(new CustomEvent('sdl:nav', { detail: { view } }));
  if (hint) toast({ title: '下一步', body: hint });
}

// ---------- 来源
function paintSrc(x, full) {
  if (!full && D.body._srcK === JSON.stringify([x.uri, x.referer, x.servers, x.headers])) return;
  D.body._srcK = JSON.stringify([x.uri, x.referer, x.servers, x.headers]);
  D.body.innerHTML = '';
  const line = (label, v, open) => h('div', { style: { marginBottom: '12px' } },
    h('div.muted', { style: { fontSize: '12px', marginBottom: '4px' } }, label),
    h('div.copyline', h('span.mono.grow', { style: { wordBreak: 'break-all', userSelect: 'text' } }, v || '—'),
      v ? h('button.btn.sm.ghost.icon', { title: '复制', 'aria-label': '复制' + label, html: icon('copy', 's14'), on: { click: () => copyText(v).then(() => toast({ kind: 'ok', title: '已复制' })) } }) : null,
      v && open && /^https?:/i.test(v) ? h('a.btn.sm.ghost.icon', { href: v, target: '_blank', rel: 'noreferrer', title: '在浏览器打开', html: icon('link', 's14') }) : null));
  D.body.append(line('下载地址', x.uri, false), line('来源页面', x.referer || x.page || '', true));
  if (x.kind !== 'bt') D.body.append(h('div.row', { style: { marginBottom: '14px' } },
    h('button.btn.sm#dRefresh', { on: { click: () => refreshUrl(x.gid) } }, h('span', { html: icon('refresh', 's14') }), '刷新下载地址…'),
    h('span.muted', { style: { fontSize: '12px' } }, '链接过期时换新地址，已下载部分保留')));
  const hd = Object.entries(x.headers || {});
  if (hd.length) {
    D.body.append(h('div.muted', { style: { fontSize: '12px', margin: '4px 0' } }, '请求头'),
      h('table.tbl', h('tbody', hd.map(([k, v]) => h('tr', h('td.mono', k), h('td.mono', { style: { wordBreak: 'break-all' } }, String(v)))))));
  }
  if ((x.servers || []).length) {
    D.body.append(h('div.muted', { style: { fontSize: '12px', margin: '14px 0 4px' } }, `连接（${x.servers.length}）`),
      h('table.tbl', h('thead', h('tr', h('th', '服务器'), h('th.r', '速度'))),
        h('tbody', x.servers.map((s) => h('tr', h('td.mono.ellipsis', { style: { maxWidth: '320px' }, title: s.uri }, s.uri), h('td.r', speed(s.speed)))))));
  }
}

// ---------- BT 文件
function paintFiles(x, full) {
  const b = D.bt;
  if (!b) { if (full) { D.body.innerHTML = ''; D.body.append(h('div.empty', h('div.spin'), '正在读取文件列表…')); } return; }
  const files = b.files || [];
  if (!files.length) { D.body.innerHTML = ''; D.body.append(h('div.empty', h('b', '还没拿到文件列表'), h('div', '磁力链接要先从节点拿到元数据，稍等片刻'))); return; }
  // 用户正在勾选时不重建，只刷新进度
  if (!full && D.body.querySelector('#dFiles') && files.length === D.body._nf) {
    files.forEach((f) => { const c = D.body.querySelector(`[data-fi="${f.index}"] .fp`); if (c) c.textContent = f.length ? pct(f.done, f.length).toFixed(0) + '%' : '—'; });
    return;
  }
  D.body._nf = files.length;
  D.body.innerHTML = '';
  const boxes = [];
  const sumEl = h('span');
  const applyBtn = h('button.btn.sm.pri#dApplySel', { disabled: true, on: { click: apply } }, '应用选择');
  const upd = () => {
    const sel = boxes.filter((c) => c.checked);
    sumEl.textContent = `已选 ${sel.length}/${files.length} · ${size(sel.reduce((a, c) => a + c._len, 0))}`;
    const orig = files.filter((f) => f.selected).map((f) => f.index).join(',');
    D.selDirty = sel.map((c) => c._idx).join(',') !== orig;
    applyBtn.disabled = !D.selDirty || !sel.length;
  };
  const all = h('input', { type: 'checkbox', 'aria-label': '全选', on: { change: () => { boxes.forEach((c) => (c.checked = all.checked)); upd(); } } });
  const seq = h('input', { type: 'checkbox', checked: !!x.bt_seq, on: { change: async () => { await api.rpc('sniffdl.bt.sequential', x.gid, seq.checked); toast({ kind: 'ok', title: seq.checked ? '已开启顺序下载（可边下边看）' : '已关闭顺序下载' }); } } });
  const rows = files.map((f) => {
    const c = h('input', { type: 'checkbox', checked: !!f.selected, on: { change: upd } }); c._idx = f.index; c._len = f.length; boxes.push(c);
    const nm = f.path ? basename(f.path) : '#' + f.index;
    return h('tr', { 'data-fi': f.index }, h('td', { style: { width: '24px' } }, c), h('td.ellipsis', { style: { maxWidth: '260px' }, title: f.path }, nm),
      h('td.r', size(f.length)), h('td.r.fp', f.length ? pct(f.done, f.length).toFixed(0) + '%' : '—'));
  });
  all.checked = files.every((f) => f.selected);
  D.body.append(h('div#dFiles',
    h('div.row', { style: { marginBottom: '8px', fontSize: '12px', color: 'var(--ink2)' } }, h('label.check', all, '全选'), sumEl, h('div.grow'), h('label.check', seq, '顺序下载'), applyBtn),
    h('table.tbl', h('thead', h('tr', h('th'), h('th', '文件'), h('th.r', '大小'), h('th.r', '进度'))), h('tbody', rows))));
  upd();
  async function apply() {
    const idx = boxes.filter((c) => c.checked).map((c) => c._idx);
    const r = await api.rpc('sniffdl.bt.select', x.gid, idx);
    if (r.ok) { toast({ kind: 'ok', title: `只下载选中的 ${idx.length} 个文件` }); D.selDirty = false; files.forEach((f) => (f.selected = idx.includes(f.index))); upd(); }
    else toast({ kind: 'err', title: '改选失败', body: r.error });
  }
}

// ---------- BT 节点
function paintPeers(x, full) {
  const b = D.bt;
  D.body.innerHTML = '';
  if (!b) { D.body.append(h('div.empty', h('div.spin'), '正在读取节点…')); return; }
  D.body.append(h('div.statgrid',
    h('div', h('small', '分享率'), h('b', (b.ratio || 0).toFixed(2))),
    h('div', h('small', '已上传'), h('b', size(b.uploaded))),
    h('div', h('small', '连接 / 做种'), h('b', `${b.connections || 0} / ${b.seeders || 0}`))));
  const ps = b.peers || [];
  if (!ps.length) { D.body.append(h('div.empty', h('b', '暂时没有连上的节点'), h('div', b.error ? '引擎：' + b.error : '冷门资源可能要等一会儿'))); return; }
  D.body.append(h('table.tbl', h('thead', h('tr', h('th', '地址'), h('th.r', '对方进度'), h('th.r', '↓'), h('th.r', '↑'))),
    h('tbody', ps.map((p) => h('tr', h('td.mono', `${p.ip}:${p.port}`, p.seeder ? h('span.chip.seed', { style: { marginLeft: '6px' } }, '种子') : null),
      h('td.r', (p.progress || 0).toFixed(0) + '%'), h('td.r', speed(p.down)), h('td.r', speed(p.up)))))));
}

// ---------- 日志
function paintLog(x, full) {
  const logs = x.log || [];
  const k = logs.length + ':' + (logs[logs.length - 1]?.ts || '');
  if (!full && D.body._lk === k) return;
  D.body._lk = k;
  D.body.innerHTML = '';
  if (!logs.length) { D.body.append(h('div.empty', h('b', '这个任务还没有日志'))); return; }
  D.body.append(h('div.logbox', logs.map((r) => {
    const m = r.msg || '';
    const c = /失败|错误|error/i.test(m) ? 'e' : /警告|重试|warn/i.test(m) ? 'w' : /完成|成功/.test(m) ? 'o' : '';
    const ts = r.ts ? new Date(r.ts * 1000).toTimeString().slice(0, 8) : '';
    return h('div.ln' + (c ? '.' + c : ''), h('span.ts', ts), m);
  })));
}
