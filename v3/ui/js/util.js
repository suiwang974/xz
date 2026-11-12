// 小工具：DOM 构建、格式化、转义
export const $ = (s, r = document) => r.querySelector(s);
export const $$ = (s, r = document) => [...r.querySelectorAll(s)];

export function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
// h('div.cls#id', {attrs, on:{click}}, ...children)
export function h(tag, props, ...kids) {
  const m = /^([a-z0-9]+)?((?:[.#][\w-]+)*)$/i.exec(tag) || [];
  const el = document.createElement(m[1] || 'div');
  (m[2] || '').replace(/([.#])([\w-]+)/g, (_, t, v) => { if (t === '.') el.classList.add(v); else el.id = v; });
  if (props && (typeof props !== 'object' || props instanceof Node || Array.isArray(props))) { kids.unshift(props); props = null; }
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === 'on') for (const [ev, fn] of Object.entries(v)) el.addEventListener(ev, fn);
    else if (k === 'html') el.innerHTML = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k in el && typeof v !== 'string') el[k] = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const c of kids.flat(Infinity)) if (c != null && c !== false) el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return el;
}

const U = ['B', 'KB', 'MB', 'GB', 'TB'];
export function size(n, d = 1) {
  n = Number(n) || 0;
  if (n <= 0) return '0 B';
  let i = 0;
  while (n >= 1024 && i < U.length - 1) { n /= 1024; i++; }
  return (i === 0 ? n.toFixed(0) : n.toFixed(n >= 100 ? 0 : d)) + ' ' + U[i];
}
export const speed = (n) => (n > 0 ? size(n) + '/s' : '—');
export function dur(s) {
  s = Math.round(Number(s) || 0);
  if (s < 0) return '—';
  if (s < 60) return s + ' 秒';
  if (s < 3600) return Math.floor(s / 60) + ' 分 ' + (s % 60) + ' 秒';
  if (s < 86400) return Math.floor(s / 3600) + ' 时 ' + Math.floor((s % 3600) / 60) + ' 分';
  return Math.floor(s / 86400) + ' 天 ' + Math.floor((s % 86400) / 3600) + ' 时';
}
export function when(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000), now = new Date();
  const p = (x) => String(x).padStart(2, '0');
  const hm = p(d.getHours()) + ':' + p(d.getMinutes());
  if (d.toDateString() === now.toDateString()) return '今天 ' + hm;
  const y = new Date(now); y.setDate(now.getDate() - 1);
  if (d.toDateString() === y.toDateString()) return '昨天 ' + hm;
  return (d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + hm;
}
export const pct = (done, total) => (total > 0 ? Math.min(100, (100 * done) / total) : 0);
export function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
export function extOf(name) { const m = /\.([a-z0-9]{1,8})$/i.exec(name || ''); return m ? m[1].toLowerCase() : ''; }
export function basename(p) { return String(p || '').split(/[\\/]/).filter(Boolean).pop() || ''; }
export function copyText(t) {
  if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(t).catch(() => fallbackCopy(t));
  fallbackCopy(t); return Promise.resolve();
}
function fallbackCopy(t) {
  const ta = h('textarea', { style: { position: 'fixed', left: '-9999px' } }, t);
  document.body.append(ta); ta.select(); try { document.execCommand('copy'); } catch (e) {} ta.remove();
}
export function wild(pat) {   // 通配符 → 正则（* ?）
  const s = String(pat || '').trim();
  if (!s) return null;
  return new RegExp('^' + s.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*').replace(/\?/g, '.') + '$', 'i');
}
