// 任务列表：筛选（状态 / 类型 / 搜索）、排序、多选、批量操作、右键菜单、键盘
import { h, esc, size, speed, dur, when, pct, basename, copyText } from '../util.js';
import { icon } from '../icons.js';
import * as api from '../api.js';
import { S, sub, changed, tasksArr } from '../store.js';
import { toast } from '../ui/toast.js';
import { openMenu } from '../ui/menu.js';
import { modal, confirmBox, promptBox } from '../ui/modal.js';
import { openDetail } from './detail.js';

export const FILTERS = {
  all: { label: '全部任务', test: () => true },
  active: { label: '下载中', test: (t) => ['active', 'queued', 'retry', 'verifying', 'seeding'].includes(t.phase) },
  waiting: { label: '排队 / 稍后', test: (t) => ['queued', 'hold', 'retry'].includes(t.phase) },
  done: { label: '已完成', test: (t) => t.status === 'complete' },
  unfinished: { label: '未完成', test: (t) => ['paused', 'error', 'hold'].includes(t.phase) },
};
const KINDS = [['all', '全部'], ['file', '文件'], ['video', '视频'], ['bt', 'BT'], ['live', '直播']];
const ICON_OF = { file: 'file', video: 'video', bt: 'magnet', live: 'live' };
const PHASE_LABEL = { active: '下载中', queued: '排队中', hold: '稍后下载', retry: '等待重试', verifying: '校验中', seeding: '做种中',
  paused: '已暂停', complete: '已完成', error: '失败', waiting: '等待中', removed: '已移除' };

let el, listEl, rowsEl, theadEl, bulkEl, rows = new Map(), lastClick = null, kindSeg, searchInp, sortSel;

export function mountTasks(container) {
  el = container;
  kindSeg = h('div.seg#kindSeg', KINDS.map(([k, l]) => h('button', { 'data-k': k, class: S.kind === k ? 'on' : '', on: { click: () => { S.kind = k; render(); } } }, l)));
  searchInp = h('input.input#taskSearch', { type: 'search', placeholder: '搜索任务（Ctrl+F）', on: { input: () => { S.q = searchInp.value.trim().toLowerCase(); render(); } } });
  sortSel = h('select.select#taskSort', { title: '排序', on: { change: () => { const [k, d] = sortSel.value.split(':'); S.sort = { key: k, dir: Number(d) }; render(); } } },
    [['added:-1', '最近添加'], ['added:1', '最早添加'], ['name:1', '名称'], ['total:-1', '大小'], ['speed:-1', '速度'], ['finished:-1', '完成时间']].map(([v, l]) => h('option', { value: v }, l)));
  bulkEl = h('div.bulk.hidden#bulk');
  const tb = h('div.toolbar', kindSeg, h('div.search', h('span', { html: icon('search', 's14') }), searchInp), bulkEl, h('div.grow'), sortSel,
    h('button.btn.sm#btnStartAll', { title: '开始全部暂停的任务', on: { click: () => api.rpc('sniffdl.all.resume').then(() => toast({ kind: 'ok', title: '已全部开始' })) } }, h('span', { html: icon('play', 's14') }), '全部开始'),
    h('button.btn.sm#btnPauseAll', { title: '暂停全部', on: { click: () => api.rpc('sniffdl.all.pause').then(() => toast({ title: '已全部暂停' })) } }, h('span', { html: icon('pause', 's14') }), '全部暂停'),
    h('button.btn.sm.icon#btnListMore', { title: '更多', 'aria-label': '更多', html: icon('more'), on: { click: (e) => listMore(e) } }));
  theadEl = h('div.thead', h('span'), h('span', { 'data-s': 'name' }, '名称'), h('span', { 'data-s': 'done' }, '进度'), h('span', { 'data-s': 'total' }, '大小'),
    h('span', { 'data-s': 'speed' }, '速度'), h('span.col-eta', '剩余'), h('span', '状态'));
  theadEl.addEventListener('click', (e) => { const k = e.target.dataset?.s; if (!k) return; S.sort = { key: k, dir: S.sort.key === k ? -S.sort.dir : -1 }; render(); });
  rowsEl = h('div.tasks#taskRows', { role: 'listbox', 'aria-multiselectable': 'true' });
  listEl = h('div.list#taskList', { tabindex: 0 }, theadEl, rowsEl);
  el.append(tb, listEl);
  listEl.addEventListener('keydown', keys);
  listEl.addEventListener('mousedown', (e) => { if (e.target === listEl || e.target === rowsEl) { S.sel.clear(); render(); } });
  sub((d) => { if (d.has('tasks') || d.has('view')) render(); });
  document.addEventListener('sdl:added', () => { if (!['all', 'active', 'waiting'].includes(S.filter)) { S.filter = 'all'; changed('view'); } });
}

export function visible() {
  const f = FILTERS[S.filter] || FILTERS.all;
  let arr = tasksArr().filter((t) => f.test(t) && (S.kind === 'all' || t.kind === S.kind));
  if (S.q) arr = arr.filter((t) => (t.name + ' ' + t.uri + ' ' + t.dir).toLowerCase().includes(S.q));
  const { key, dir } = S.sort;
  arr.sort((a, b) => {
    const x = a[key], y = b[key];
    if (typeof x === 'string') return dir * String(x).localeCompare(String(y), 'zh');
    return dir * ((x || 0) - (y || 0));
  });
  return arr;
}

function render() {
  if (!rowsEl) return;
  kindSeg.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset.k === S.kind));
  theadEl.querySelectorAll('[data-s]').forEach((s) => s.classList.toggle('sorted', s.dataset.s === S.sort.key));
  const arr = visible();
  const keep = new Set(arr.map((t) => t.gid));
  for (const [g, r] of rows) if (!keep.has(g)) { r.remove(); rows.delete(g); }
  let prev = null;
  for (const t of arr) {
    let r = rows.get(t.gid);
    if (!r) { r = makeRow(t.gid); rows.set(t.gid, r); }
    fill(r, t);
    const want = prev ? prev.nextSibling : rowsEl.firstChild;
    if (want !== r) rowsEl.insertBefore(r, want);
    prev = r;
  }
  let empty = rowsEl.querySelector('.empty');
  if (!arr.length) {
    if (!empty) {
      const any = S.tasks.size > 0;
      empty = h('div.empty', h('img', { src: 'assets/feiyu_chibi.png', alt: '' }),
        h('b', any ? '这里没有符合条件的任务' : '还没有下载任务'),
        h('div', any ? '换个筛选条件，或清空搜索看看' : '把链接、磁力或种子贴到上面的框里，大肥鱼帮你搬回来～'),
        any ? null : h('div.keys', h('span', h('kbd', 'Ctrl'), '+', h('kbd', 'V'), ' 粘贴即识别'), h('span', '拖入 .torrent'), h('span', '浏览器扩展一键接管')));
      rowsEl.append(empty);
    }
  } else if (empty) empty.remove();
  theadEl.style.display = arr.length ? '' : 'none';
  for (const g of [...S.sel]) if (!S.tasks.has(g)) S.sel.delete(g);
  updBulk();
}

function makeRow(gid) {
  const r = h('div.task', { role: 'option', 'data-gid': gid });
  r.innerHTML = `<div class="ico"></div><div class="nm"><div class="t"></div><div class="meta"></div></div>
    <div class="prog"><div class="bar"><i></i></div><div class="p"><span class="pp"></span><span class="pd"></span></div></div>
    <div class="num sz"></div><div class="num spd"></div><div class="num eta col-eta"></div><div class="st"></div>`;
  const acts = h('div.acts');
  r.querySelector('.st').append(h('span.pillw'), acts);
  r.querySelector('.st').style.cssText = 'display:flex;align-items:center;justify-content:space-between;gap:4px;min-width:0';
  r.addEventListener('click', (e) => { if (e.target.closest('.acts')) return; clickSel(gid, e); });
  r.addEventListener('dblclick', (e) => { if (e.target.closest('.acts')) return; const t = S.tasks.get(gid); if (t?.status === 'complete') api.rpc('sniffdl.task.open', gid).then(okOrErr); else openDetail(gid); });
  r.addEventListener('contextmenu', (e) => { e.preventDefault(); if (!S.sel.has(gid)) { S.sel = new Set([gid]); S.focus = gid; render(); } ctxMenu(e.clientX, e.clientY); });
  return r;
}

function fill(r, t) {
  const ph = t.phase;
  r.classList.toggle('sel', S.sel.has(t.gid));
  r.classList.toggle('focus', S.focus === t.gid && document.activeElement === listEl);
  r.setAttribute('aria-selected', S.sel.has(t.gid) ? 'true' : 'false');
  const ico = r.querySelector('.ico');
  if (ico.dataset.k !== t.kind) { ico.dataset.k = t.kind; ico.className = 'ico k-' + t.kind; ico.innerHTML = icon(ICON_OF[t.kind] || 'file', 's18'); }
  const nm = t.name || basename((t.uri || '').split('?')[0]) || t.uri;
  const tt = r.querySelector('.t'); if (tt.textContent !== nm) { tt.textContent = nm; tt.title = nm; }
  // meta 行：错误 / 备注 / 阶段说明 / 来源
  let meta = '';
  if (t.status === 'error' && t.err) meta = `<span class="em" title="${esc(t.err.raw)}">${esc(t.err.title)} · ${esc(t.err.hint)}</span>`;
  else {
    const bits = [];
    if (t.note) bits.push(`<span class="note" title="${esc(t.note)}">${esc(t.note)}</span>`);
    else if (t.msg && t.status === 'active') bits.push(`<span class="note">${esc(t.msg)}</span>`);
    if (t.src === 'browser') bits.push('<span class="chip">浏览器接管</span>');
    if (t.resumable === false) bits.push('<span class="chip warn" title="服务器不支持断点续传，暂停后会从头下">不可续传</span>');
    if (t.limit_kbps) bits.push(`<span class="chip">限速 ${t.limit_kbps} KB/s</span>`);
    if (t.checksum_ok === true) bits.push('<span class="chip ok">校验通过</span>');
    if (t.kind === 'bt' && (t.seeders || t.connections)) bits.push(`<span>${t.connections} 节点 · ${t.seeders} 做种</span>`);
    if (ph === 'seeding') bits.push(`<span>分享率 ${(t.ratio || 0).toFixed(2)}</span>`);
    if (!bits.length) bits.push(`<span class="note">${esc(t.status === 'complete' ? '完成于 ' + when(t.finished) : basename(t.dir) || t.dir || '')}</span>`);
    meta = bits.join('');
  }
  const me = r.querySelector('.meta'); if (me._h !== meta) { me.innerHTML = meta; me._h = meta; }
  const bar = r.querySelector('.bar'); const p = t.total ? pct(t.done, t.total) : (t.status === 'complete' ? 100 : 0);
  bar.className = 'bar ' + ({ complete: 'ok', error: 'err', paused: 'paused', seeding: 'seed', hold: 'paused', queued: 'paused' }[ph] || '') + ((t.status === 'active' && !t.total) ? ' indet' : '');
  bar.firstChild.style.width = p.toFixed(1) + '%';
  r.querySelector('.pp').textContent = t.total ? p.toFixed(p >= 99.95 || p === 0 ? 0 : 1) + '%' : (t.status === 'active' ? '获取大小…' : '');
  r.querySelector('.pd').textContent = t.total ? size(t.done) : (t.done ? size(t.done) : '');
  r.querySelector('.sz').textContent = t.total ? size(t.total) : '—';
  const spd = ph === 'seeding' ? '↑ ' + speed(t.up || 0) : (t.status === 'active' ? speed(t.speed) : '—');
  r.querySelector('.spd').textContent = spd;
  r.querySelector('.eta').textContent = t.status === 'active' && t.eta >= 0 ? dur(t.eta) : (ph === 'retry' && t.retry_at ? dur(t.retry_at - Date.now() / 1000) + '后' : '—');
  const pw = r.querySelector('.pillw'); const pl = PHASE_LABEL[ph] || ph;
  if (pw._p !== ph) { pw._p = ph; pw.innerHTML = `<span class="pill ${ph}">${pl}</span>`; }
  const acts = r.querySelector('.acts'); const key = ph + (t.path ? 1 : 0);
  if (acts._k !== key) {
    acts._k = key; acts.innerHTML = '';
    const B = (ic, title, fn) => h('button.btn', { title, 'aria-label': title, html: icon(ic, 's14'), on: { click: (e) => { e.stopPropagation(); fn(); } } });
    if (['active', 'queued', 'retry', 'seeding'].includes(ph)) acts.append(B('pause', ph === 'seeding' ? '停止做种' : '暂停（空格）', () => act('pause', [t.gid])));
    else if (['paused', 'error', 'hold'].includes(ph)) acts.append(B('play', ph === 'error' ? '重试' : '开始（空格）', () => act('resume', [t.gid])));
    if (t.status === 'complete') acts.append(B('file', '打开文件', () => api.rpc('sniffdl.task.open', t.gid).then(okOrErr)));
    acts.append(B('folder', '打开所在文件夹', () => api.rpc('sniffdl.task.reveal', t.gid).then(okOrErr)));
    acts.append(B('more', '更多（右键）', () => { S.sel = new Set([t.gid]); S.focus = t.gid; render(); const b = acts.lastChild.getBoundingClientRect(); ctxMenu(b.left - 160, b.bottom + 4); }));
  }
}

function okOrErr(r) { if (r && r.ok === false) toast({ kind: 'err', title: '打不开', body: r.error || r.path || '' }); }

function clickSel(gid, e) {
  const vis = visible().map((t) => t.gid);
  if (e.shiftKey && lastClick) {
    const a = vis.indexOf(lastClick), b = vis.indexOf(gid);
    if (a >= 0 && b >= 0) { if (!e.ctrlKey) S.sel.clear(); vis.slice(Math.min(a, b), Math.max(a, b) + 1).forEach((g) => S.sel.add(g)); }
  } else if (e.ctrlKey || e.metaKey) { S.sel.has(gid) ? S.sel.delete(gid) : S.sel.add(gid); lastClick = gid; }
  else { S.sel = new Set([gid]); lastClick = gid; }
  S.focus = gid; listEl.focus({ preventScroll: true }); render();
}

function updBulk() {
  const n = S.sel.size;
  bulkEl.classList.toggle('hidden', n < 2);
  if (n < 2) return;
  const tot = [...S.sel].reduce((a, g) => a + (S.tasks.get(g)?.total || 0), 0);
  bulkEl.innerHTML = '';
  bulkEl.append(h('b', `已选 ${n} 项 · ${size(tot)}`),
    h('button.btn.sm.ghost', { on: { click: () => act('resume', [...S.sel]) } }, h('span', { html: icon('play', 's14') }), '开始'),
    h('button.btn.sm.ghost', { on: { click: () => act('pause', [...S.sel]) } }, h('span', { html: icon('pause', 's14') }), '暂停'),
    h('button.btn.sm.ghost', { on: { click: () => removeSel() } }, h('span', { html: icon('trash', 's14') }), '删除'),
    h('button.btn.sm.ghost.icon', { title: '取消选择', html: icon('x', 's14'), on: { click: () => { S.sel.clear(); render(); } } }));
}

export async function act(what, gids) {
  if (!gids.length) return;
  try { await api.rpc('sniffdl.task.' + what, gids); } catch (e) { toast({ kind: 'err', title: '操作失败', body: e.message }); }
}

export async function removeSel(gids) {
  gids = gids || [...S.sel];
  if (!gids.length) return;
  const isDone = (g) => S.tasks.get(g)?.status === 'complete';   // 注意 .done 是已下字节数，不是布尔
  const anyFile = gids.some(isDone);
  const anyPart = gids.some((g) => !isDone(g));
  const pol = S.settings?.cancel_cleanup || 'ask';   // always / never / ask（设置 › 下载 › 取消时清理半截文件）
  const cb = h('input#rmFiles', { type: 'checkbox' });
  const cbPart = h('input#rmPart', { type: 'checkbox', checked: true });
  const partLine = !anyPart ? null
    : pol === 'ask' ? h('label.check', cbPart, '同时清理未下完的半截文件（不勾则保留，可稍后接着下）')
    : h('div.muted', { style: { fontSize: '12px' } }, pol === 'never' ? '未下完的半截文件会保留在原处（可在设置里改）。' : '未下完的半截文件会自动清理。');
  const body = h('div', { style: { display: 'grid', gap: '10px' } },
    h('div', gids.length === 1 ? `删除「${S.tasks.get(gids[0])?.name || '任务'}」？` : `删除选中的 ${gids.length} 个任务？`),
    anyFile ? h('label.check', cb, '同时删除已下载的文件（移到回收站，可找回）') : null,
    partLine);
  const ok = await modal({ title: '删除任务', icon: 'trash', iconColor: 'var(--err)', body, actions: [{ label: '取消', value: null }, { label: '删除', danger: true, value: true }] });
  if (!ok) return;
  const args = [gids, !!cb.checked];
  if (pol === 'ask' && anyPart) args.push(!!cbPart.checked);
  const r = await api.rpc('sniffdl.task.remove', ...args);
  S.sel.clear();
  toast({ kind: 'ok', title: `已删除 ${gids.length} 个任务`, body: r.trashed?.length ? `${r.trashed.length} 个文件已移到回收站` : '' });
}

export async function refreshUrl(gid) {
  const t = S.tasks.get(gid);
  const v = await promptBox('刷新下载地址', h('div', '链接过期时，把从网页上重新拿到的新链接贴进来，已下载的部分会保留、接着下。'), '', '新的下载链接 https://…');
  if (!v) return;
  const r = await api.rpc('sniffdl.task.refreshUrl', gid, v.trim());
  if (r.ok) toast({ kind: 'ok', title: '已换新地址，继续下载', body: t?.name || '' }); else toast({ kind: 'err', title: '换地址失败', body: r.error });
}

async function setLimit(gids) {
  const cur = S.tasks.get(gids[0])?.limit_kbps || 0;
  const v = await promptBox('单任务限速', 'KB/s，0 = 不单独限速（仍受全局限速约束）', String(cur));
  if (v == null) return;
  for (const g of gids) await api.rpc('sniffdl.task.setLimit', g, Number(v) || 0);
  toast({ kind: 'ok', title: Number(v) ? `已限速 ${v} KB/s` : '已取消单任务限速' });
}

function ctxMenu(x, y) {
  const gids = [...S.sel];
  const t = S.tasks.get(gids[0]); if (!t) return;
  const one = gids.length === 1;
  const items = [
    { label: '开始 / 继续', icon: 'play', k: '空格', fn: () => act('resume', gids), disabled: !gids.some((g) => ['paused', 'error', 'hold'].includes(S.tasks.get(g)?.phase)) },
    { label: '暂停', icon: 'pause', k: '空格', fn: () => act('pause', gids), disabled: !gids.some((g) => ['active', 'queued', 'retry', 'seeding'].includes(S.tasks.get(g)?.phase)) },
    { label: '稍后下载', icon: 'clock', fn: () => act('later', gids) },
    '-',
    { label: '打开文件', icon: 'file', fn: () => api.rpc('sniffdl.task.open', t.gid).then(okOrErr), disabled: !one || t.status !== 'complete' },
    { label: '打开所在文件夹', icon: 'folder', fn: () => api.rpc('sniffdl.task.reveal', t.gid).then(okOrErr), disabled: !one },
    { label: '详情', icon: 'info', k: 'Enter', fn: () => openDetail(t.gid), disabled: !one },
    '-',
    { label: '刷新下载地址…', icon: 'refresh', fn: () => refreshUrl(t.gid), disabled: !one || t.kind === 'bt' },
    { label: '单任务限速…', icon: 'turtle', fn: () => setLimit(gids) },
    { label: '移到队列…', icon: 'list', fn: () => moveQueue(gids) },
    { label: '复制下载链接', icon: 'copy', fn: () => copyText(gids.map((g) => S.tasks.get(g)?.uri).join('\n')).then(() => toast({ kind: 'ok', title: '链接已复制' })) },
    t.kind === 'bt' && one ? { label: '导出 .torrent', icon: 'magnet', fn: () => api.rpc('sniffdl.bt.exportTorrent', t.gid).then((r) => r.ok ? toast({ kind: 'ok', title: '已在文件夹里选中种子文件' }) : toast({ kind: 'warn', title: r.error })) } : null,
    '-',
    { label: '删除…', icon: 'trash', k: 'Del', danger: true, fn: () => removeSel(gids) },
  ].filter(Boolean);
  openMenu(x, y, items);
}

async function moveQueue(gids) {
  const sel = h('select.select', { style: { width: '100%' } }, (S.queues || []).map((q) => h('option', { value: q.id }, q.name)));
  const v = await modal({ title: '移到队列', body: sel, actions: [{ label: '取消', value: null }, { label: '移动', pri: true, value: () => sel.value }] });
  if (!v) return;
  for (const g of gids) await api.rpc('sniffdl.task.update', g, { queue: v });
  toast({ kind: 'ok', title: '已移动' });
}

function listMore(e) {
  const b = e.currentTarget.getBoundingClientRect();
  openMenu(b.right - 200, b.bottom + 4, [
    { label: '导入任务列表…', icon: 'upload', fn: importTasks },
    { label: '导出任务列表', icon: 'download', fn: exportTasks },
    '-',
    { label: '清除已完成（不删文件）', icon: 'checkc', fn: () => api.rpc('sniffdl.task.clear', 'done').then((r) => toast({ kind: 'ok', title: `已清除 ${r.removed} 条` })) },
    { label: '清除全部记录（不删文件）', icon: 'trash', danger: true, fn: async () => { if (await confirmBox('清除全部记录', '正在下载的任务不受影响，本地文件不会被删除。', '清除', true)) { const r = await api.rpc('sniffdl.task.clear', 'all'); toast({ kind: 'ok', title: `已清除 ${r.removed} 条` }); } } },
  ]);
}

async function exportTasks() {
  const r = await api.rpc('sniffdl.tasks.export', true);
  const p = await api.host.saveFile('SniffDL任务列表.json', JSON.stringify(r, null, 2));
  if (p) toast({ kind: 'ok', title: `已导出 ${r.tasks.length} 个任务`, body: typeof p === 'string' ? p : '' });
}
async function importTasks() {
  const ta = h('textarea.input#importText', { rows: 8, placeholder: '粘贴导出的 JSON，或每行一个链接', style: { width: '100%' } });
  const fileBtn = h('button.btn.sm', { on: { click: () => { const i = h('input', { type: 'file', accept: '.json,.txt' }); i.onchange = async () => { ta.value = await i.files[0].text(); }; i.click(); } } }, '选择文件…');
  const start = h('input', { type: 'checkbox' });
  const v = await modal({ title: '导入任务', icon: 'upload', body: h('div', { style: { display: 'grid', gap: '8px' } }, ta, h('div.row', fileBtn, h('label.check', start, '导入后立即开始'))),
    actions: [{ label: '取消', value: null }, { label: '导入', pri: true, value: () => ta.value }] });
  if (!v || !v.trim()) return;
  const r = await api.rpc('sniffdl.tasks.import', v, start.checked);
  toast({ kind: 'ok', title: `已导入 ${r.count} 个任务`, body: start.checked ? '' : '在「排队 / 稍后」里，点开始即可' });
}

function keys(e) {
  const vis = visible().map((t) => t.gid);
  if (!vis.length) return;
  const i = vis.indexOf(S.focus);
  const move = (j) => { j = Math.max(0, Math.min(vis.length - 1, j)); S.focus = vis[j]; if (e.shiftKey) S.sel.add(vis[j]); else S.sel = new Set([vis[j]]); render(); rows.get(vis[j])?.scrollIntoView({ block: 'nearest' }); };
  if (e.key === 'ArrowDown') { e.preventDefault(); move(i + 1); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); move(i < 0 ? 0 : i - 1); }
  else if (e.key === 'Home') { e.preventDefault(); move(0); }
  else if (e.key === 'End') { e.preventDefault(); move(vis.length - 1); }
  else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'a') { e.preventDefault(); S.sel = new Set(vis); render(); }
  else if (e.key === 'Delete') { e.preventDefault(); removeSel(); }
  else if (e.key === ' ') {
    e.preventDefault();
    const gs = [...S.sel]; if (!gs.length) return;
    const anyRun = gs.some((g) => ['active', 'queued', 'retry', 'seeding'].includes(S.tasks.get(g)?.phase));
    act(anyRun ? 'pause' : 'resume', gs);
  } else if (e.key === 'Enter' && S.focus) { e.preventDefault(); openDetail(S.focus); }
  else if (e.key === 'Escape') { S.sel.clear(); render(); }
  else if (e.key === 'ContextMenu' && S.focus) { e.preventDefault(); const b = rows.get(S.focus)?.getBoundingClientRect(); if (b) ctxMenu(b.left + 60, b.bottom); }
}
export function focusList() { listEl?.focus(); }
export function focusSearch() { searchInp?.focus(); searchInp?.select(); }
