// 待确认页：浏览器接管时等你点头的下载（intercept）+ 扩展嗅探到的资源（grabber），合成一张可批量处理的表。
// 勾选（单击 / Shift 区间 / 表头全选）、按类型 / 大小 / 文件名筛选；有勾选时按钮作用于所选，否则作用于当前筛选出的全部。
import { h, size, basename } from '../util.js';
import { icon } from '../icons.js';
import * as api from '../api.js';
import { S, changed } from '../store.js';
import { toast } from '../ui/toast.js';
import { confirmBox } from '../ui/modal.js';

const TYPES = {
  视频: 'mp4,mkv,webm,m4v,mov,flv,ts,avi,wmv,m3u8,mpd', 音频: 'mp3,flac,wav,aac,ogg,m4a,opus,wma',
  压缩包: 'zip,rar,7z,tar,gz,tgz,bz2,xz,iso', 程序: 'exe,msi,apk,dmg,pkg,deb,rpm,appimage', 文档: 'pdf,epub,doc,docx,xls,xlsx,ppt,pptx,txt',
  图片: 'png,jpg,jpeg,gif,webp,bmp,svg', 种子: 'torrent',
};
const EXT2T = {}; for (const [k, v] of Object.entries(TYPES)) v.split(',').forEach((e) => (EXT2T[e] = k));
const SIZES = [['任意大小', () => true], ['未知大小', (n) => !n], ['< 10 MB', (n) => n > 0 && n < 10 << 20], ['10 – 100 MB', (n) => n >= 10 << 20 && n < 100 << 20],
  ['100 MB – 1 GB', (n) => n >= 100 << 20 && n < 1 << 30], ['> 1 GB', (n) => n >= 1 << 30]];

let root = null;
const F = { type: '全部类型', size: 0, q: '' };
const sel = new Set();
let lastIdx = -1;
export function mountPending(el) { root = el; }

function items() {
  const out = [];
  for (const p of S.pending || []) out.push({ key: 'p:' + p.id, src: 'icpt', id: p.id, url: p.url, name: p.filename || nameOf(p.url), size: p.size || 0, from: p.referer || '' });
  for (const g of S.grabber?.values() || []) out.push({ key: 'g:' + g.url, src: 'grab', url: g.url, name: g.name || nameOf(g.url), size: g.size || 0, from: g.referer || '', kind: g.kind, online: g.online });
  for (const it of out) { const ext = (/\.([a-z0-9]{1,8})$/i.exec(it.name) || /\.([a-z0-9]{1,8})(?:$|[?#])/i.exec(it.url || '') || [])[1]?.toLowerCase() || ''; it.ext = ext; it.type = EXT2T[ext] || (it.kind === 'video' || it.kind === 'site' ? '视频' : '其他'); }
  return out;
}
const nameOf = (u) => { try { return decodeURIComponent(basename((u || '').split(/[?#]/)[0])) || u; } catch (e) { return u; } };
function filtered(all) {
  const q = F.q.trim().toLowerCase();
  return all.filter((it) => (F.type === '全部类型' || it.type === F.type) && SIZES[F.size][1](it.size) && (!q || (it.name + ' ' + it.url).toLowerCase().includes(q)));
}

export function renderPending() {
  if (!root) return;
  const all = items();
  for (const k of [...sel]) if (!all.some((x) => x.key === k)) sel.delete(k);
  const vis = filtered(all);
  const types = ['全部类型', ...Object.keys(TYPES), '其他'].filter((t) => t === '全部类型' || all.some((x) => x.type === t));
  if (!types.includes(F.type)) F.type = '全部类型';
  const selVis = vis.filter((x) => sel.has(x.key));
  const target = selVis.length ? selVis : vis;
  const hadQ = document.activeElement?.id === 'pendQ';
  root.innerHTML = '';
  const head = h('div.vhead', h('h1', '待确认'), h('span.sub', '浏览器接管时等你点头的下载，和扩展嗅探到的资源'));
  const qInp = h('input.input#pendQ', { type: 'search', placeholder: '按文件名 / 链接筛选', value: F.q, style: { width: '220px' }, on: { input: (e) => { F.q = e.target.value; renderPending(); } } });
  const bar = h('div.grab-filters',
    h('select.select#pendType', { 'aria-label': '按类型筛选', on: { change: (e) => { F.type = e.target.value; renderPending(); } } }, types.map((t) => h('option', { value: t, selected: t === F.type }, t === '全部类型' ? t : `${t}（${all.filter((x) => x.type === t).length}）`))),
    h('select.select#pendSize', { 'aria-label': '按大小筛选', on: { change: (e) => { F.size = Number(e.target.value); renderPending(); } } }, SIZES.map(([l], i) => h('option', { value: i, selected: i === F.size }, l))),
    qInp, h('div.grow'),
    h('button.btn#pendNo', { disabled: !target.length, on: { click: () => bulk(target, false) } }, h('span', { html: icon('x', 's14') }), selVis.length ? `取消所选 ${selVis.length}` : `全部取消${vis.length ? ' ' + vis.length : ''}`),
    h('button.btn.pri#pendYes', { disabled: !target.length, on: { click: () => bulk(target, true) } }, h('span', { html: icon('download', 's14') }), selVis.length ? `确认所选 ${selVis.length}` : `全部确认${vis.length ? ' ' + vis.length : ''}`));
  const allChk = h('input#pendAll', { type: 'checkbox', 'aria-label': '全选', checked: vis.length > 0 && selVis.length === vis.length,
    on: { change: (e) => { vis.forEach((x) => (e.target.checked ? sel.add(x.key) : sel.delete(x.key))); renderPending(); } } });
  allChk.indeterminate = selVis.length > 0 && selVis.length < vis.length;
  const list = h('div.plist',
    h('div.prow.phead', h('label.check', allChk), h('span', '文件'), h('span', '类型'), h('span.num', '大小'), h('span', '来源'), h('span')));
  if (!all.length) list.append(h('div.qempty', '没有等待确认的下载。浏览器扩展接管或嗅探到资源时会出现在这里。'));
  else if (!vis.length) list.append(h('div.qempty', '没有符合筛选条件的项。'));
  vis.slice(0, 500).forEach((it, i) => {
    const chk = h('input', { type: 'checkbox', checked: sel.has(it.key), 'aria-label': '选择 ' + it.name });
    const r = h('div.prow' + (sel.has(it.key) ? '.sel' : ''), { 'data-key': it.key,
      on: { click: (e) => {
        if (e.target.closest('button')) return;
        if (e.shiftKey && lastIdx >= 0) { const [a, b] = [Math.min(lastIdx, i), Math.max(lastIdx, i)]; vis.slice(a, b + 1).forEach((x) => sel.add(x.key)); }
        else if (e.target === chk) chk.checked ? sel.add(it.key) : sel.delete(it.key);
        else sel.has(it.key) ? sel.delete(it.key) : sel.add(it.key);
        lastIdx = i; renderPending();
      } } },
      h('label.check', chk),
      h('div.pn', h('div.ellipsis', { title: it.name }, it.name), h('div.d.ellipsis', { title: it.url }, it.url)),
      h('span', h('span.chip' + (it.src === 'icpt' ? '.warn' : ''), it.src === 'icpt' ? '等确认' : it.type), it.src === 'icpt' ? h('span.muted', ' ' + it.type) : null),
      h('span.num', it.size ? size(it.size) : (it.online === false ? h('span', { style: { color: 'var(--err)' } }, '失效') : '—')),
      h('span.d.ellipsis', { title: it.from }, it.from ? hostOf(it.from) : (it.src === 'icpt' ? '浏览器接管' : '嗅探')),
      h('div.row', h('button.btn.sm.icon.ghost', { title: '取消', on: { click: () => bulk([it], false) } }, h('span', { html: icon('x', 's14') })),
        h('button.btn.sm', { title: '下载', on: { click: () => bulk([it], true) } }, h('span', { html: icon('download', 's14') }), '下载')));
    list.append(r);
  });
  root.append(head, h('div.vbody', h('div.cards', { style: { maxWidth: '1100px' } }, h('div.card', h('div.cb', { style: { paddingTop: '14px' } }, bar, list)))));
  if (hadQ) { qInp.focus(); qInp.setSelectionRange(qInp.value.length, qInp.value.length); }
}
const hostOf = (u) => { try { return new URL(u).host; } catch (e) { return u; } };

async function bulk(list, ok) {
  if (list.length > 1 && !ok && !(await confirmBox(`取消 ${list.length} 项`, '这些下载不会开始，嗅探到的资源会从列表移除。', '全部取消', true))) return;
  let n = 0, bad = 0;
  const grabDone = [];
  for (const it of list) {
    try {
      if (it.src === 'icpt') {
        const r = await api.rpc('sniffdl.intercept.answer', it.id, ok, {});
        if (r.ok === false) bad++; else n++;
        S.pending = (S.pending || []).filter((x) => x.id !== it.id);
      } else {
        if (ok) { const r = await api.rpc('sniffdl.task.add', { url: it.url, headers: it.from ? { Referer: it.from } : undefined, name: it.name && it.name !== it.url ? it.name : undefined }); if (r && r.ok === false) { bad++; continue; } }
        grabDone.push(it.url); n++;
      }
    } catch (e) { bad++; }
    sel.delete(it.key);
  }
  if (grabDone.length) { await api.rpc('sniffdl.grabber.remove', grabDone).catch(() => {}); grabDone.forEach((u) => S.grabber.delete(u)); }
  changed('pending'); changed('grabber');
  toast({ kind: bad ? 'warn' : 'ok', title: ok ? `已开始 ${n} 个下载` : `已取消 ${n} 项`, body: bad ? `${bad} 项处理失败（可能已过期）` : '' });
}
