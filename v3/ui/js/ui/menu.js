import { h } from '../util.js';
import { icon } from '../icons.js';
let cur = null;
export function closeMenu() { if (cur) { cur.remove(); cur = null; document.removeEventListener('mousedown', outside, true); document.removeEventListener('keydown', key, true); } }
function outside(e) { if (cur && !cur.contains(e.target)) closeMenu(); }
function key(e) {
  if (!cur) return;
  const items = [...cur.querySelectorAll('button:not([disabled])')];
  const i = items.indexOf(document.activeElement);
  if (e.key === 'Escape') { e.preventDefault(); closeMenu(); }
  else if (e.key === 'ArrowDown') { e.preventDefault(); items[(i + 1) % items.length]?.focus(); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); items[(i - 1 + items.length) % items.length]?.focus(); }
}
// items: [{label, icon, k, danger, disabled, fn} | '-']
export function openMenu(x, y, items) {
  closeMenu();
  cur = h('div.menu', { role: 'menu' }, items.map((it) => it === '-' ? h('hr') :
    h('button' + (it.danger ? '.danger' : ''), { role: 'menuitem', disabled: !!it.disabled, on: { click: () => { closeMenu(); it.fn && it.fn(); } } },
      h('span', { html: icon(it.icon || 'chev', 's14') }), it.label, it.k ? h('span.k', it.k) : null)));
  document.body.append(cur);
  const r = cur.getBoundingClientRect();
  cur.style.left = Math.max(6, Math.min(x, innerWidth - r.width - 6)) + 'px';
  cur.style.top = Math.max(6, Math.min(y, innerHeight - r.height - 6)) + 'px';
  setTimeout(() => { document.addEventListener('mousedown', outside, true); document.addEventListener('keydown', key, true); cur?.querySelector('button')?.focus(); }, 0);
}
