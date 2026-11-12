// 顶部「一个框」：粘贴/拖入 → 识别 → 识别结果卡（选格式/选文件/高级选项）→ 开始下载
import { h, esc, size, debounce, basename, wild } from '../util.js';
import { icon } from '../icons.js';
import * as api from '../api.js';
import { S } from '../store.js';
import { toast } from '../ui/toast.js';

const KIND = {
  direct: ['file', '文件直链'], netdisk: ['shield', '网盘直链'], video: ['video', '视频站'], page: ['sniff', '网页'],
  m3u8: ['live', 'HLS 流'], torrent: ['magnet', '种子文件'], magnet: ['magnet', '磁力链接'], batch: ['layers', '批量链接'],
  empty: ['link', ''], unsupported: ['alert', '无法识别'],
};
const FORMATS = ['最佳', '2160p', '1440p', '1080p', '720p', '480p', '仅音频 MP3', '仅音频 M4A', 'MP4（兼容）', 'MKV'];

let root, ta, recoBox, cur = null, seq = 0;

export function mountAdder(el) {
  root = el;
  ta = h('textarea#addInput', { rows: 1, spellcheck: false, 'aria-label': '下载地址',
    placeholder: '粘贴链接、磁力、种子哈希，或把 .torrent 拖进来…' });
  const box = h('div.addbox',
    h('div.lead', { html: icon('link', 's20') }), ta,
    h('div.acts',
      h('button.btn.ghost.icon#btnTorrent', { title: '打开种子文件', 'aria-label': '打开种子文件', html: icon('upload'), on: { click: openTorrent } }),
      h('button.btn.pri.lg#btnGo', { on: { click: () => go(false) } }, h('span', { html: icon('download') }), '下载')));
  const hint = h('div.hintline',
    h('span', { html: icon('bolt', 's14') + ' 自动识别：直链 / 视频站 / m3u8 / 磁力 / 种子 / 网盘 / 批量 <code>file[001-100].jpg</code>' }),
    h('span', h('kbd', 'Enter'), ' 识别'), h('span', h('kbd', 'Ctrl'), '+', h('kbd', 'Enter'), ' 直接下载'));
  recoBox = h('div#reco');
  root.append(box, hint, recoBox);
  const auto = debounce(() => inspect(), 450);
  ta.addEventListener('input', () => { fit(); cur = null; if (!ta.value.trim()) clearReco(); else auto(); });
  ta.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); go(e.ctrlKey || e.metaKey); }
    if (e.key === 'Escape') { if (ta.value) { ta.value = ''; fit(); clearReco(); } else ta.blur(); }
  });
  ta.addEventListener('paste', () => setTimeout(() => { fit(); inspect(); }, 0));
  // 拖拽：.torrent 文件 / 链接文本
  const dz = document.body;
  dz.addEventListener('dragover', (e) => { e.preventDefault(); box.classList.add('drag'); });
  dz.addEventListener('dragleave', (e) => { if (!e.relatedTarget) box.classList.remove('drag'); });
  dz.addEventListener('drop', async (e) => {
    e.preventDefault(); box.classList.remove('drag');
    const f = [...(e.dataTransfer.files || [])].find((x) => /\.torrent$/i.test(x.name));
    if (f) return uploadTorrent(f);
    const t = e.dataTransfer.getData('text/uri-list') || e.dataTransfer.getData('text/plain');
    if (t) { setText(t); }
  });
}

function fit() { ta.style.height = 'auto'; ta.style.height = Math.min(140, ta.scrollHeight) + 'px'; }
export function setText(t, run = true) { ta.value = t; fit(); ta.focus(); if (run) inspect(); }
export function focusAdder() { ta.focus(); ta.select(); }
function clearReco() { cur = null; recoBox.innerHTML = ''; }

async function openTorrent() {
  const p = await api.host.pickFile([{ name: '种子', extensions: ['torrent'] }]);
  if (p) return setText(p);
  const inp = h('input', { type: 'file', accept: '.torrent', style: { display: 'none' } });
  inp.onchange = () => inp.files[0] && uploadTorrent(inp.files[0]);
  document.body.append(inp); inp.click(); setTimeout(() => inp.remove(), 60000);
}
async function uploadTorrent(file) {
  const b64 = await new Promise((r) => { const fr = new FileReader(); fr.onload = () => r(String(fr.result).split(',')[1]); fr.readAsDataURL(file); });
  try { const r = await api.rpc('sniffdl.torrent.upload', b64, file.name); setText(r.path); }
  catch (e) { toast({ kind: 'err', title: '种子文件读不了', body: e.message }); }
}

async function inspect() {
  const text = ta.value.trim();
  if (!text) return clearReco();
  const my = ++seq;
  recoBox.innerHTML = '';
  recoBox.append(h('div.reco', h('div.reco-head', h('div.spin'), h('div', h('div.t', '正在识别…'), h('div.s', '探测文件名、大小、能否续传')))));
  let res;
  try { res = await api.rpc('sniffdl.inspect', text, true); } catch (e) {
    if (my !== seq) return;
    recoBox.innerHTML = ''; recoBox.append(h('div.reco', h('div.reco-head', h('div.kind', { html: icon('alert') }), h('div', h('div.t', '识别失败'), h('div.s', e.message)))));
    return;
  }
  if (my !== seq) return;
  cur = res;
  render();
}

function dirDefault(cat) {
  const b = S.settings?.outdir || S.defaults?.outdir || '';
  return cat && S.settings?.auto_category ? joinPath(b, cat) : b;
}
function joinPath(a, b) { if (!a) return b; const sep = a.includes('\\') ? '\\' : '/'; return a.replace(/[\\/]+$/, '') + sep + b; }

function dirField(init) {
  const inp = h('input.input#recoDir', { value: init || '', placeholder: '默认保存位置' });
  const pick = h('button.btn', { on: { click: async () => { const d = await api.host.pickDir('选择保存位置'); if (d) inp.value = d; } } }, h('span', { html: icon('folder', 's14') }), '浏览');
  return { el: h('div.field', h('label', '保存到'), h('div.ctl', inp, pick)), get: () => inp.value.trim() };
}

function advFields(kind) {
  const hdr = h('textarea.input#advHeaders', { rows: 2, placeholder: 'Referer: https://…\nCookie: …（每行一个，只作用于这次）' });
  const sum = h('input.input#advChecksum', { placeholder: 'sha256:… / md5:…（下完自动核对）' });
  const lim = h('input.input.num#advLimit', { type: 'number', min: 0, placeholder: '0', style: { width: '110px' } });
  const conns = h('select.select#advConns', ['自动', '1', '2', '4', '8', '16'].map((v) => h('option', { value: v }, v)));
  const qsel = h('select.select#advQueue', (S.queues || []).map((q) => h('option', { value: q.id }, q.name)));
  const after = h('select.select#advAfter', [['', '无'], ['open', '打开文件'], ['folder', '打开所在文件夹']].map(([v, l]) => h('option', { value: v }, l)));
  const el = h('details.more', h('summary', h('span', { html: icon('chev', 's14') }), '高级选项'),
    h('div.inner',
      kind !== 'torrent' && kind !== 'magnet' ? h('div.field', h('label', '请求头'), h('div.ctl', hdr)) : null,
      kind === 'direct' || kind === 'netdisk' ? h('div.field', h('label', '校验和'), h('div.ctl', sum)) : null,
      h('div.field', h('label', '限速'), h('div.ctl', lim, h('span.muted', 'KB/s（0 = 跟随全局）'),
        kind === 'direct' || kind === 'netdisk' || kind === 'batch' ? [h('span.muted', { style: { marginLeft: '12px' } }, '连接数'), conns] : null)),
      h('div.field', h('label', '加入队列'), h('div.ctl', qsel, h('span.muted', '下完后'), after))));
  return {
    el, get: () => ({ headers: hdr.value.trim() || undefined, checksum: sum.value.trim() || undefined,
      limit_kbps: Number(lim.value) || undefined, conns: conns.value !== '自动' ? conns.value : undefined,
      queue: qsel.value || undefined, after: after.value || undefined }),
  };
}

function head(kind, title, sub, chips = []) {
  const [ic, label] = KIND[kind] || KIND.direct;
  return h('div.reco-head', h('div.kind', { html: icon(ic, 's18') }),
    h('div.grow', h('div.t.ellipsis', title || label), h('div.s.row', h('span', label), sub ? h('span.ellipsis', '· ' + sub) : null, chips)),
    h('button.btn.ghost.icon.sm', { title: '关闭（Esc）', 'aria-label': '关闭', html: icon('x', 's14'), on: { click: () => { ta.value = ''; fit(); clearReco(); } } }));
}

function render() {
  recoBox.innerHTML = '';
  const k = cur.kind, it = (cur.items || [])[0] || {};
  if (k === 'empty') return;
  if (k === 'unsupported' || !it.kind) {
    recoBox.append(h('div.reco', head('unsupported', '这不像可下载的东西', it.reason || '贴一个 http(s) 链接、磁力或种子文件试试')));
    return;
  }
  if (k === 'batch') return renderBatch();
  if (k === 'torrent' || k === 'magnet') return renderBT(it);
  if (k === 'page') return renderPage(it);
  return renderSingle(it);
}

async function submit(list, startLater) {
  let ok = 0, fail = 0, lastErr = '';
  for (const info of list) {
    try { const r = await api.rpc('sniffdl.task.add', { ...info, hold: startLater || undefined }); if (r.ok) ok++; else { fail++; lastErr = r.error; } }
    catch (e) { fail++; lastErr = e.message; }
  }
  if (ok) {
    toast({ kind: 'ok', title: startLater ? `已加入「稍后下载」${ok} 个` : `已开始 ${ok} 个任务`, body: fail ? `另有 ${fail} 个失败：${lastErr}` : '' });
    ta.value = ''; fit(); clearReco();
    document.dispatchEvent(new CustomEvent('sdl:added'));
  } else toast({ kind: 'err', title: '没加进去', body: lastErr || '未知原因' });
}

function footer(onGo, extra) {
  return h('div.reco-foot', extra || h('span.muted', { style: { fontSize: '12px' } }, '回车开始 · Esc 关闭'), h('div.grow'),
    h('button.btn#recoLater', { on: { click: () => onGo(true) } }, h('span', { html: icon('clock', 's14') }), '稍后下载'),
    h('button.btn.pri#recoStart', { on: { click: () => onGo(false) } }, h('span', { html: icon('download', 's14') }), '开始下载'));
}

function renderSingle(it) {
  const k = it.kind;
  const nameInp = h('input.input#recoName', { value: it.name || basename((it.url || '').split('?')[0]) || '', placeholder: k === 'video' ? '自动（用视频标题）' : '文件名' });
  if (k === 'video') nameInp.value = '';
  const d = dirField(dirDefault(it.category));
  const adv = advFields(k);
  const chips = [];
  if (it.size) chips.push(h('span.chip', size(it.size)));
  if (it.resumable === true) chips.push(h('span.chip.ok', '可续传'));
  if (it.resumable === false) chips.push(h('span.chip.warn', { title: '服务器不支持断点续传：暂停后要从头下' }, '不可续传'));
  if (it.online === false) chips.push(h('span.chip.err', '可能已失效' + (it.status ? ' · ' + it.status : '')));
  if (it.live) chips.push(h('span.chip.err', '● 直播'));
  const body = h('div.reco-body');
  if (it.cert_error) body.append(h('div.warnbox', h('span', { html: icon('shield', 's14') }), h('div', h('b', 'HTTPS 证书异常 · '), it.cert_error, '。确认来源可信再下载。')));
  if (it.expired) body.append(h('div.warnbox.err', h('span', { html: icon('alert', 's14') }), '这条网盘直链已过期：回网盘页面重新获取链接再推送'));
  if (it.encrypted) body.append(h('div.warnbox.err', h('span', { html: icon('alert', 's14') }), '这个流是加密的（#EXT-X-KEY），可能无法下载；不支持 DRM'));
  body.append(h('div.field', h('label', '文件名'), h('div.ctl', nameInp)));
  let fmt, subs, plist, liveLim, liveIdle;
  if (k === 'video' || (k === 'm3u8' && !it.live)) {
    fmt = h('select.select#recoFormat', FORMATS.map((f) => h('option', { value: f, selected: f === (S.settings?.format_preset || '最佳') }, f)));
    subs = h('input#recoSubs', { type: 'checkbox', checked: !!S.settings?.subs });
    body.append(h('div.field', h('label', '格式'), h('div.ctl', fmt, h('label.check', subs, '带字幕'))));
    if (k === 'video') {
      plist = h('input.input#recoPlaylist', { placeholder: '如 1-10 或 1,3,5（只对合集/播放列表）', style: { maxWidth: '320px' } });
      body.append(h('div.field', h('label', '列表范围'), h('div.ctl', plist)));
    }
  }
  if (k === 'm3u8' && it.live) {
    liveLim = h('input.input.num#recoLiveLimit', { type: 'number', min: 0, value: S.settings?.live_limit_min ?? 0, style: { width: '100px' } });
    liveIdle = h('input.input.num#recoLiveIdle', { type: 'number', min: 5, value: S.settings?.live_idle_sec ?? 60, style: { width: '100px' } });
    body.append(h('div.field', h('label', '直播录制'), h('div.ctl', '最长', liveLim, '分钟（0 = 不限）·', '无新画面', liveIdle, '秒后停')));
  }
  body.append(d.el, adv.el);
  const go = (later) => {
    const info = { url: it.url, name: nameInp.value.trim() || undefined, dir: d.get() || undefined, size_hint: it.size || undefined,
      resumable: it.resumable, ...adv.get() };
    if (fmt) info.format = fmt.value;
    if (subs) info.subs = subs.checked;
    if (plist && plist.value.trim()) info.playlist_items = plist.value.trim();
    if (liveLim) info.live = { limit_min: Number(liveLim.value) || 0, idle_sec: Number(liveIdle.value) || 60 };
    submit([info], later);
  };
  recoBox.append(h('div.reco', head(k, it.name || it.url, it.netdisk || it.site || (it.mime || ''), chips), body, footer(go)));
  cur._go = go;
}

function renderBT(it) {
  const d = dirField(dirDefault(''));
  const adv = advFields(it.kind);
  const seq = h('input#recoSeq', { type: 'checkbox' });
  const body = h('div.reco-body');
  let files = it.files || [];
  const sel = new Set(files.filter((f) => f.selected !== false).map((f) => f.index));
  const sumEl = h('span');
  const upd = () => { const tot = files.filter((f) => sel.has(f.index)).reduce((a, f) => a + f.length, 0); sumEl.textContent = `已选 ${sel.size}/${files.length} 个 · ${size(tot)}`; };
  if (files.length) {
    const list = h('div.reco-list');
    const rows = files.map((f) => {
      const cb = h('input', { type: 'checkbox', checked: sel.has(f.index), on: { change: () => { cb.checked ? sel.add(f.index) : sel.delete(f.index); upd(); } } });
      return { f, cb, el: h('label.it', cb, h('span.ellipsis', { title: f.path }, f.path), h('span.sz', size(f.length)), h('span')) };
    });
    list.append(h('div.tools', h('label.check', h('input', { type: 'checkbox', checked: true, on: { change: (e) => { rows.forEach((r) => { r.cb.checked = e.target.checked; e.target.checked ? sel.add(r.f.index) : sel.delete(r.f.index); }); upd(); } } }), '全选'),
      h('span.muted', '广告 / 填充文件已默认不勾'), h('div.grow'), sumEl), rows.map((r) => r.el));
    body.append(list);
    upd();
  } else {
    body.append(h('div.warnbox.info', h('span', { html: icon('info', 's14') }), '磁力要先从网络拿到种子信息，拿到后可在任务详情里勾选文件。没人做种时会在超时后提示「无人做种」。'));
  }
  body.append(h('div.field', h('label', '播放'), h('div.ctl', h('label.check', seq, '首尾块优先（边下边播 / 快速预览）'))), d.el, adv.el);
  const go = (later) => {
    if (files.length && !sel.size) return toast({ kind: 'warn', title: '至少勾一个文件' });
    const info = { url: it.url, dir: d.get() || undefined, bt_seq: seq.checked || undefined, ...adv.get(), size_hint: it.size || undefined };
    if (files.length && sel.size < files.length) info.bt_select = [...sel].sort((a, b) => a - b);
    submit([info], later);
  };
  const chips = [it.size ? h('span.chip', size(it.size)) : null, it.infohash ? h('span.chip.mono', it.infohash.slice(0, 12) + '…') : null];
  recoBox.append(h('div.reco', head(it.kind, it.name || '磁力任务', it.from_hash ? '由哈希补全' : '', chips), body, footer(go)));
  cur._go = go;
}

function renderBatch() {
  const items = cur.items || [];
  const d = dirField(dirDefault(''));
  const adv = advFields('batch');
  const sel = new Set(items.map((_, i) => i));
  const flt = h('input.input#batchFilter', { placeholder: '筛选：*.jpg / 关键字', style: { maxWidth: '200px' } });
  const sumEl = h('span');
  const list = h('div.reco-list');
  const rows = items.map((it, i) => {
    const cb = h('input', { type: 'checkbox', checked: true, on: { change: () => { cb.checked ? sel.add(i) : sel.delete(i); upd(); } } });
    return { it, cb, i, el: h('label.it', cb, h('span.ellipsis', { title: it.url }, it.name || it.url),
      h('span.chip' + (it.online === false ? '.err' : ''), (KIND[it.kind] || KIND.direct)[1]), h('span.sz', it.size ? size(it.size) : '')) };
  });
  const upd = () => { sumEl.textContent = `已选 ${sel.size}/${items.length}`; };
  flt.addEventListener('input', () => {
    const re = wild(flt.value.includes('*') || flt.value.includes('?') ? flt.value : '*' + flt.value + '*');
    rows.forEach((r) => { const show = !re || re.test(r.it.name || r.it.url) || re.test(r.it.url); r.el.style.display = show ? '' : 'none'; r.cb.checked = show; show ? sel.add(r.i) : sel.delete(r.i); });
    upd();
  });
  list.append(h('div.tools', h('label.check', h('input', { type: 'checkbox', checked: true, on: { change: (e) => { rows.forEach((r) => { if (r.el.style.display === 'none') return; r.cb.checked = e.target.checked; e.target.checked ? sel.add(r.i) : sel.delete(r.i); }); upd(); } } }), '全选'),
    flt, h('div.grow'), sumEl), rows.map((r) => r.el));
  upd();
  const body = h('div.reco-body', cur.truncated ? h('div.warnbox', '只展开了前 5000 条') : null, list, d.el, adv.el);
  const go = (later) => {
    const a = adv.get();
    const chosen = rows.filter((r) => sel.has(r.i)).map((r) => ({ url: r.it.url, dir: d.get() || undefined, ...a }));
    if (!chosen.length) return toast({ kind: 'warn', title: '一个都没选' });
    submit(chosen, later);
  };
  recoBox.append(h('div.reco', head('batch', `${items.length} 个链接`, '可筛选后批量加入'), body, footer(go)));
  cur._go = go;
}

function renderPage(it) {
  const body = h('div.reco-body', h('div.warnbox.info', h('span', { html: icon('info', 's14') }),
    '这是普通网页。可以嗅探页面里的视频 / 音频，或者把它当文件直接下载。'));
  const res = h('div');
  body.append(res);
  const sniffBtn = h('button.btn.pri#recoSniff', { on: { click: () => sniff(it.url, res, sniffBtn) } }, h('span', { html: icon('sniff', 's14') }), '嗅探页面里的视频');
  const foot = h('div.reco-foot', h('span.muted', { style: { fontSize: '12px' } }, '嗅探会在后台用浏览器打开页面'), h('div.grow'),
    h('button.btn', { on: { click: () => submit([{ url: it.url }], false) } }, '当文件下载'), sniffBtn);
  recoBox.append(h('div.reco', head('page', it.url, ''), body, foot));
  cur._go = () => sniff(it.url, res, sniffBtn);
}

async function sniff(url, box, btn) {
  btn.disabled = true;
  box.innerHTML = '';
  const st = h('div.row', h('div.spin'), h('span', '嗅探中… 打开页面、等待播放器加载（通常 5–30 秒）'));
  box.append(st);
  let tid;
  try { tid = (await api.rpc('sniffdl.sniff.start', url, 'page')).task; } catch (e) { st.replaceWith(h('div.warnbox.err', e.message)); btn.disabled = false; return; }
  const t0 = Date.now();
  while (true) {
    await new Promise((r) => setTimeout(r, 1200));
    let r;
    try { r = await api.rpc('sniffdl.sniff.result', tid); } catch (e) { continue; }
    if (r.status === 'running') { st.lastChild.textContent = `嗅探中… 已用 ${Math.round((Date.now() - t0) / 1000)} 秒`; continue; }
    btn.disabled = false;
    if (r.status === 'error' || !(r.items || []).length) { st.replaceWith(h('div.warnbox', h('span', { html: icon('info', 's14') }), r.error || '没找到可下载的视频。可以试试把视频播放页地址直接贴进来。')); return; }
    const items = r.items;
    const sel = new Set(items.map((x) => x.idx));
    const list = h('div.reco-list', h('div.tools', h('b', `找到 ${items.length} 个`), h('div.grow'),
      h('button.btn.pri.sm', { on: { click: () => submit(items.filter((x) => sel.has(x.idx)).map((x) => ({ url: x.url, headers: x.page ? 'Referer: ' + x.page : undefined })), false) } }, '下载选中')),
      items.map((x) => {
        const cb = h('input', { type: 'checkbox', checked: true, on: { change: () => (cb.checked ? sel.add(x.idx) : sel.delete(x.idx)) } });
        return h('label.it', cb, h('span.ellipsis', { title: x.url }, x.title), h('span.chip', [x.ext, x.res].filter((v) => v && v !== '-').join(' · ') || x.how || '媒体'), h('span.sz', x.size ? size(x.size) : ''));
      }));
    st.replaceWith(list);
    return;
  }
}

async function go(direct) {
  const text = ta.value.trim();
  if (!text) { ta.focus(); return; }
  if (direct && cur && cur._go && cur.kind !== 'page') return cur._go(false);
  if (!cur) { await inspect(); if (direct && cur?._go && cur.kind !== 'page') cur._go(false); return; }
  if (cur._go) cur._go(false);
}
