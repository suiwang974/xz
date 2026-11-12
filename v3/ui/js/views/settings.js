// 设置页：按内核 schema（sniffdl.settings.get）自动生成完整表单。
// 「常用」直接展示；group=advanced 按 section 分卡折叠；顶部搜索（标签/说明/键名）。
// 改动即时保存（开关/下拉立即，文本/数字在失焦或回车时）；其它窗口改了设置 → SSE settings 事件回显到控件。
import { $, h } from '../util.js';
import { icon } from '../icons.js';
import * as api from '../api.js';
import { S, setSetting } from '../store.js';
import { toast } from '../ui/toast.js';
import { modal } from '../ui/modal.js';
import { BUILTIN, pathToUrl, previewBg, clearBgFail } from '../ui/background.js';

const IS_WIN = /win/i.test(navigator.userAgentData?.platform || navigator.platform || navigator.userAgent || '');
// 平台限制：显示在对应设置项旁边
const PLATFORM = {   // tag 显示在名称旁；off = 非 Windows 时额外提示一行
  motw: { tag: '仅 Windows', note: '「来自网络」标记是 Windows 的 Zone.Identifier', off: '当前系统不是 Windows，此项不生效' },
  keep_awake: { tag: '仅 Windows', note: '防睡眠调用 Windows 电源接口', off: '当前系统不是 Windows，此项不生效' },
  av_scan: { tag: '仅 Windows', note: '默认扫描用 Windows Defender', off: '当前系统没有 Defender，请填下面的「自定义扫描命令」' },
  auto_extract: { tag: '7-Zip 仅 Windows', note: '7z / rar / 分卷需要 Windows 上的 7-Zip', off: '当前系统只能自动解 zip / tar' },
  bt_port: { tag: '不支持 UPnP', note: 'aria2 不做 UPnP 端口映射，需要时在路由器里手动映射' },
};

let root = null, ctl = new Map(), query = '', openSecs = new Set();

export function mountSettings(el) { root = el; }

export function renderSettings() {
  if (!root) return;
  const keepScroll = root.querySelector('.vbody')?.scrollTop || 0;
  const hadFocus = document.activeElement?.closest?.('#vSettings') ? document.activeElement.id : null;
  root.innerHTML = '';
  ctl = new Map();
  const schema = (S.schema || []).filter((s) => s.group !== 'hidden');
  const search = h('input.input#setSearch', { type: 'search', placeholder: '搜索设置（名称、说明）…', value: query, 'aria-label': '搜索设置',
    on: { input: (e) => { query = e.target.value; applyFilter(); const b = root.querySelector('.vbody'); if (b) b.scrollTop = 0; } } });
  const head = h('div.vhead', h('h1', '设置'), h('span.sub', '改动即时生效，自动保存'), h('div.grow'),
    h('div.search', { style: { position: 'relative', width: '260px' } }, h('span', { html: icon('search', 's14'), style: { position: 'absolute', left: '9px', top: '9px', color: 'var(--ink3)' } }), search));
  search.style.paddingLeft = '30px'; search.style.width = '100%';

  const common = schema.filter((s) => s.group === 'common' && s.section !== '外观');
  const look = schema.filter((s) => s.group === 'common' && s.section === '外观');
  const secs = [];
  for (const s of schema.filter((x) => x.group !== 'common')) { let g = secs.find((x) => x.name === s.section); if (!g) secs.push(g = { name: s.section, items: [] }); g.items.push(s); }

  const cards = h('div.cards');
  cards.append(h('div.card', { 'data-sec': '常用' }, h('div.ch', h('h3', '常用')), h('div.cb', common.map(row))));
  if (look.length) cards.append(lookCard(look));
  cards.append(h('div.advh', h('span', { html: icon('gear', 's14') }), '高级', h('span.sub', `${secs.reduce((a, g) => a + g.items.length, 0)} 项 · 点开分组查看`)));
  for (const g of secs) {
    const d = h('details.card', { 'data-sec': g.name, open: openSecs.has(g.name) },
      h('summary', h('span.svg.chev', { html: icon('chev', 's14') }), h('h3', g.name), h('span.sub', `${g.items.length} 项`)),
      h('div.cb', g.items.map(row)));
    d.addEventListener('toggle', () => { if (query) return; d.open ? openSecs.add(g.name) : openSecs.delete(g.name); });
    cards.append(d);
  }
  const rpc = S.rpcInfo || {};
  cards.append(h('div.card', h('div.ch', h('h3', 'RPC / 接入')), h('div.cb',
    h('div.set', h('div.l', h('b', '接口地址'), h('small', 'aria2 兼容 JSON-RPC，浏览器扩展 / 第三方工具用')), h('div.c', h('span.mono', rpc.url || '—'))),
    h('div.set', h('div.l', h('b', '令牌')), h('div.c', h('span', rpc.token_set ? '已设置' : '未设置'))))));
  cards.append(toolsCard());
  cards.append(h('div.muted#setNone.hidden', { style: { padding: '20px 4px' } }, '没有匹配的设置项。'));
  const body = h('div.vbody', cards);
  root.append(head, body);
  applyFilter();
  body.scrollTop = keepScroll;
  if (hadFocus) document.getElementById(hadFocus)?.focus();
}

function row(s) {
  const v = S.settings?.[s.key];
  const id = 'set_' + s.key;
  let input;
  const save = s.key === 'ui_bg_custom'
    ? (val) => { clearBgFail(); return commit(s, val, r, String(val || '').trim() ? { ui_bg: 'custom' } : null); }
    : (val) => commit(s, val, r);
  switch (s.type) {
    case 'bool':
      input = h('label.switch', h('input#' + id, { type: 'checkbox', checked: !!v, on: { change: (e) => save(e.target.checked) } }), h('span'));
      break;
    case 'select':
      if (s.key === 'ui_theme') {
        input = h('div.seg#' + id, s.options.map((o) => h('button', { type: 'button', class: (v || s.default) === o ? 'on' : '', 'data-v': o, on: { click: () => save(o) } }, o)));
      } else input = h('select.select#' + id, { on: { change: (e) => save(e.target.value) } }, s.options.map((o) => h('option', { value: o, selected: String(v) === o }, o)));
      break;
    case 'int': case 'float':
      if (s.slider) {
        const rv = h('span.rv', `${v ?? s.default}${s.unit || ''}`);
        input = [h('input#' + id, { type: 'range', min: s.min, max: s.max, step: 1, value: v ?? s.default, 'aria-label': s.label,
          on: { input: (e) => { rv.textContent = e.target.value + (s.unit || ''); previewBg({ [s.key]: Number(e.target.value) }); },
                change: async (e) => { await save(Number(e.target.value)); previewBg(null); } } }), rv];
        break;
      }
      input = h('input.input.num#' + id, { type: 'number', value: v ?? '', min: s.min, max: s.max, step: s.type === 'float' ? 'any' : 1,
        on: { change: (e) => save(e.target.value), keydown: (e) => { if (e.key === 'Enter') e.target.blur(); } } });
      break;
    case 'time':
      input = h('input.input.num#' + id, { type: 'time', value: v || '', on: { change: (e) => e.target.value && save(e.target.value) } });
      break;
    case 'text':
      input = h('textarea.input#' + id, { rows: Math.min(8, Math.max(3, String(v || '').split('\n').length)), spellcheck: false, on: { change: (e) => save(e.target.value) } });
      input.value = v ?? '';
      break;
    default:
      input = h('input.input#' + id, { type: 'text', value: v ?? '', spellcheck: false,
        placeholder: s.key === 'outdir' ? (S.defaults?.outdir || '默认') : '',
        on: { change: (e) => save(e.target.value), keydown: (e) => { if (e.key === 'Enter') e.target.blur(); } } });
  }
  const extra = [];
  if (s.unit && !s.slider) extra.push(h('span.unit', s.unit));
  if (s.key === 'ui_bg_custom' && api.host.kind !== 'browser') {
    extra.push(h('button.btn.sm', { type: 'button', on: { click: pickCustom } }, '选择图片…'));
  }
  if ((s.type === 'path') && api.host.kind !== 'browser') {
    extra.push(h('button.btn.sm', { type: 'button', on: { click: async () => { const p = await api.host.pickDir(s.label); if (p) { $('#' + id).value = p; save(p); } } } }, '选择…'));
  }
  const pf = PLATFORM[s.key];
  const r = h('div.set', { 'data-key': s.key, 'data-find': [s.label, s.help, s.key, s.section, pf?.tag, pf?.note].filter(Boolean).join(' ').toLowerCase() },
    h('div.l', h('label', { for: id }, h('b', s.label, pf ? h('span.chip.warn.pf', { title: pf.note, style: { marginLeft: '6px', verticalAlign: '1px' } }, pf.tag) : null)),
      s.help ? h('small', s.help) : null,
      pf && pf.off && !IS_WIN ? h('small.pfnote', pf.off) : null),
    h('div.c', input, extra, h('span.err')));
  ctl.set(s.key, { s, r });
  return r;
}

async function commit(s, val, r, also) {
  const errEl = r.querySelector('.err'); errEl.textContent = '';
  r.querySelector('.input')?.classList.remove('bad');
  try {
    const res = await setSetting({ [s.key]: val, ...(also || {}) });
    if (res.errors && res.errors[s.key]) {
      errEl.textContent = res.errors[s.key];
      r.querySelector('.input')?.classList.add('bad');
      return;
    }
    flash(s.key);
    if (s.key === 'rpc_port' || s.key === 'rpc_token') toast({ title: '接口设置已更新', body: '浏览器扩展会自动重连新端口' });
  } catch (e) {
    errEl.textContent = e.message;
    toast({ kind: 'err', title: '保存失败', body: e.message });
  }
}

function flash(key) {
  const c = ctl.get(key); if (!c) return;
  c.r.classList.remove('saved'); void c.r.offsetWidth; c.r.classList.add('saved');
  clearTimeout(c.t); c.t = setTimeout(() => c.r.classList.remove('saved'), 1600);
}

// SSE / 其它入口改了设置 → 回显到控件（正在编辑的那个不动）
export function echoSettings(changedMap) {
  for (const [k, v] of Object.entries(changedMap || {})) {
    if (k.startsWith('ui_bg')) paintLook();
    const c = ctl.get(k); if (!c) continue;
    const el = c.r.querySelector('#set_' + k);
    if (!el || el === document.activeElement) continue;
    if (k.startsWith('ui_bg')) paintLook();
    if (c.s.slider) { el.value = v; const rv = c.r.querySelector('.rv'); if (rv) rv.textContent = v + (c.s.unit || ''); }
    else if (c.s.type === 'bool') el.checked = !!v;
    else if (k === 'ui_theme') el.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset.v === v));
    else el.value = v ?? '';
    flash(k);
  }
}

function applyFilter() {
  if (!root) return;
  const q = query.trim().toLowerCase();
  let any = 0;
  root.querySelectorAll('.set[data-find]').forEach((r) => { const hit = !q || q.split(/\s+/).every((w) => r.dataset.find.includes(w)); r.classList.toggle('hidden', !hit); if (hit) any++; });
  root.querySelectorAll('[data-sec]').forEach((c) => {
    const n = c.querySelectorAll('.set[data-find]:not(.hidden)').length;
    c.classList.toggle('hidden', !!q && !n);
    if (c.tagName === 'DETAILS') c.open = q ? n > 0 : openSecs.has(c.dataset.sec);
  });
  root.querySelector('.advh')?.classList.toggle('hidden', !!q);
  $('#setNone', root)?.classList.toggle('hidden', !q || !!any);
}

// ---------------- 外观卡：主题 + 背景缩略图 + 背景参数
const IMG_EXT = ['jpg', 'jpeg', 'png', 'webp', 'gif', 'bmp', 'avif'];
function lookCard(items) {
  const by = Object.fromEntries(items.map((x) => [x.key, x]));
  const rows = [];
  if (by.ui_theme) rows.push(row(by.ui_theme));
  if (by.ui_bg) rows.push(bgPickRow(by.ui_bg));
  for (const k of ['ui_bg_custom', 'ui_bg_fit', 'ui_bg_pos', 'ui_bg_opacity', 'ui_bg_blur', 'ui_bg_dim']) {
    if (!by[k]) continue;
    const r = row(by[k]); r.classList.add('bgopt'); if (k === 'ui_bg_custom') r.classList.add('bgcustom');
    rows.push(r);
  }
  const card = h('div.card#lookCard', { 'data-sec': '外观' }, h('div.ch', h('h3', '外观'), h('span.sub', '主题与背景插画')), h('div.cb', rows));
  setTimeout(paintLook, 0);
  return card;
}

function bgPickRow(s) {
  const tiles = [
    h('button.bgtile.none', { type: 'button', 'data-bg': 'none', title: '无背景（纯净界面）', on: { click: () => setBg('none') } },
      h('div', h('span', { html: icon('x', 's14') }), '无背景'), h('span.tk', { html: icon('check', 's14') })),
    ...BUILTIN.map((b) => h('button.bgtile', { type: 'button', 'data-bg': b.id, title: b.title, on: { click: () => setBg(b.id) } },
      h('img', { src: b.thumb, alt: b.title, loading: 'lazy' }), h('span.lb', b.title), h('span.tk', { html: icon('check', 's14') }))),
    h('button.bgtile.add#bgCustomTile', { type: 'button', 'data-bg': 'custom', title: api.host.kind === 'browser' ? '自定义：填写图片路径或网址' : '自定义：选择一张本机图片', on: { click: customTile } },
      h('div', h('span', { html: icon('plus', 's14') }), '自定义'), h('span.tk', { html: icon('check', 's14') })),
  ];
  const r = h('div.set', { 'data-key': 'ui_bg', 'data-find': [s.label, s.help, 'ui_bg 背景 壁纸 插画 自定义 图片 外观'].join(' ').toLowerCase() },
    h('div.l', h('b', s.label), h('small', s.help || '')),
    h('div.c', { style: { flexDirection: 'column', alignItems: 'flex-start' } }, h('div.bgpick#bgPick', { role: 'radiogroup', 'aria-label': '界面背景' }, tiles), h('div.bgmsg#bgMsg')));
  ctl.set('ui_bg', { s, r });
  return r;
}

async function setBg(id, extra) {
  clearBgFail();
  const m = $('#bgMsg'); if (m) m.textContent = '';
  try { await setSetting({ ui_bg: id, ...(extra || {}) }); paintLook(); }
  catch (e) { toast({ kind: 'err', title: '切换背景失败', body: e.message }); }
}

async function pickCustom() {
  const p = await api.host.pickFile([{ name: '图片', extensions: IMG_EXT }]);
  if (p) await setBg('custom', { ui_bg_custom: String(p) });
  return p;
}

async function customTile() {
  if (api.host.kind !== 'browser') {
    const p = await pickCustom();
    if (!p && S.settings?.ui_bg_custom) await setBg('custom');
    return;
  }
  // 纯浏览器拿不到本机文件对话框：展开路径输入框
  if (S.settings?.ui_bg_custom) await setBg('custom');
  const r = root?.querySelector('.set.bgcustom');
  if (r) { r.classList.remove('off'); r.querySelector('input')?.focus(); }
}

export function paintLook() {
  if (!root) return;
  const cur = S.settings?.ui_bg || 'none';
  root.querySelectorAll('#bgPick .bgtile').forEach((t) => { const on = t.dataset.bg === cur; t.classList.toggle('on', on); t.setAttribute('aria-checked', on ? 'true' : 'false'); t.setAttribute('role', 'radio'); });
  const ct = root.querySelector('#bgCustomTile');
  const cu = S.settings?.ui_bg_custom || '';
  if (ct) {
    let im = ct.querySelector('img');
    if (cu) {
      if (!im) { im = h('img.photo', { alt: '' }); im.onerror = () => im.remove(); ct.prepend(im); }
      const u = pathToUrl(cu); if (im.getAttribute('src') !== u) im.src = u;
      ct.classList.remove('add');
    } else { im?.remove(); ct.classList.add('add'); }
    ct.title = cu ? '自定义：' + cu : ct.title;
  }
  root.querySelectorAll('.set.bgopt').forEach((r) => {
    const off = cur === 'none' || (r.classList.contains('bgcustom') && cur !== 'custom' && !(api.host.kind === 'browser' && document.activeElement && r.contains(document.activeElement)));
    r.classList.toggle('off', off);
  });
}

window.addEventListener('sdl-bg', (e) => {
  const m = root?.querySelector('#bgMsg');
  if (e.detail.ok) { if (m) m.textContent = ''; return; }
  if (!e.detail.custom) return;
  const msg = api.host.kind === 'browser'
    ? '读不到这张图片：纯浏览器模式下网页读不了本机文件，请填 http(s) 图片地址，或在桌面版里「选择图片…」'
    : '读不到这张图片：文件不存在或格式不支持（jpg / png / webp）';
  if (m) m.textContent = msg;
  toast({ kind: 'warn', title: '背景图片加载失败', body: msg });
});


// ---------------- 工具与诊断：打开目录/日志、提示音试听、运行自检、Cookie 登录（高级，默认折叠）
function toolBtn(id, label, fn) {
  return h('button.btn#' + id, { type: 'button', on: { click: fn } }, label);
}
async function runSelftest() {
  let r;
  try { r = await api.rpc('sniffdl.selftest'); } catch (e) { toast({ kind: 'err', title: '自检失败', body: String(e.message || e) }); return; }
  const items = r?.items || [];
  const list = h('div#selftestList', { style: { display: 'grid', gap: '8px', minWidth: '420px' } },
    items.map((i) => h('div.stitem', { 'data-ok': i.ok ? '1' : '0', style: { display: 'grid', gridTemplateColumns: '22px 1fr', gap: '8px', alignItems: 'start' } },
      h('span', { style: { color: i.ok ? 'var(--ok)' : 'var(--err)', fontWeight: '700' } }, i.ok ? '✓' : '✗'),
      h('div', h('b', i.name), h('div.muted', { style: { fontSize: '12px', wordBreak: 'break-all' } }, i.detail || ''),
        i.ok || !i.fix ? null : h('div', { style: { fontSize: '12px', color: 'var(--warn)' } }, '怎么办：' + i.fix)))));
  await modal({ title: r?.ok ? '自检通过' : '自检发现问题', icon: r?.ok ? 'checkc' : 'alert', iconColor: r?.ok ? 'var(--ok)' : 'var(--err)',
    body: list, actions: [{ label: '知道了', pri: true, value: true }] });
}
function toolsCard() {
  const cookieLine = h('span.muted#ckInfo', '读取中…');
  const site = h('input.input#ckSite', { placeholder: '登录页地址，留空 = B 站', style: { width: '260px' } });
  api.rpc('sniffdl.cookie.info').then((c) => { cookieLine.textContent = `已保存 ${c?.stored ?? 0} 条 Cookie · 当前来源：${c?.mode || '自动'}`; }).catch(() => { cookieLine.textContent = '内核未返回 Cookie 信息'; });
  const d = h('details.card#toolsCard', { 'data-sec': '工具与诊断', open: openSecs.has('工具与诊断') },
    h('summary', h('span.svg.chev', { html: icon('chev', 's14') }), h('h3', '工具与诊断'), h('span.sub', '打开目录 · 自检 · 登录')),
    h('div.cb',
      h('div.set', h('div.l', h('b', '常用位置'), h('small', '在系统文件管理器里打开')),
        h('div.c', { style: { display: 'flex', gap: '8px', flexWrap: 'wrap' } },
          toolBtn('btnOpenDir', '打开保存目录', async () => { const r = await api.rpc('sniffdl.os.openOutdir').catch((e) => ({ ok: false, error: String(e.message || e) })); toast(r?.ok === false ? { kind: 'err', title: '打不开', body: r.error || '' } : { kind: 'ok', title: '已打开保存目录' }); }),
          toolBtn('btnOpenLog', '打开日志文件', async () => { const r = await api.rpc('sniffdl.os.openLog').catch((e) => ({ ok: false, error: String(e.message || e) })); toast(r?.ok === false ? { kind: 'err', title: '打不开', body: r.error || '' } : { kind: 'ok', title: '已打开日志' }); }))),
      h('div.set', h('div.l', h('b', '完成提示音'), h('small', '试听一下现在的提示音（上面的开关管是否播放）')),
        h('div.c', toolBtn('btnChime', '试听', () => window.__sdlPlayChime?.()))),
      h('div.set', h('div.l', h('b', '运行自检'), h('small', '检查 aria2、ffmpeg、yt-dlp、保存目录、端口，几秒完成，不联网')),
        h('div.c', toolBtn('btnSelftest', '开始自检', runSelftest))),
      h('div.set', h('div.l', h('b', '网站登录（Cookie）'), h('small', '需要登录才能下的站点：弹出浏览器登录一次，之后自动带上；也可在「Cookie 来源」里选直接读浏览器')),
        h('div.c', { style: { display: 'grid', gap: '8px' } }, cookieLine,
          h('div', { style: { display: 'flex', gap: '8px', flexWrap: 'wrap' } }, site,
            toolBtn('btnCkLogin', '打开登录窗', async () => {
              const r = await api.rpc('sniffdl.cookie.login', site.value.trim()).catch((e) => ({ ok: false, msg: String(e.message || e) }));
              toast({ kind: r?.ok === false ? 'err' : 'info', title: '网站登录', body: r?.msg || r?.error || '已打开登录窗' });
            }))))));
  d.addEventListener('toggle', () => { if (query) return; d.open ? openSecs.add('工具与诊断') : openSecs.delete('工具与诊断'); });
  return d;
}
