import { h } from '../util.js';
import { icon } from '../icons.js';
let box;
const ICON = { ok: 'check', err: 'alert', info: 'info', warn: 'alert' };
// toast({kind, title, body, actions:[{label, pri, fn}], ms})
export function toast(o) {
  if (typeof o === 'string') o = { title: o };
  box = box || document.body.appendChild(h('div.toasts', { role: 'status', 'aria-live': 'polite' }));
  const kind = o.kind || 'info';
  const el = h('div.toast.' + kind,
    h('div.ti', { html: icon(ICON[kind] || 'info', 's14') }),
    h('div.tb', h('div.tt', o.title || ''), o.body ? h('div.td', o.body) : null,
      (o.actions || []).length ? h('div.ta', (o.actions || []).map((a) => h('button.btn.sm' + (a.pri ? '.pri' : ''),
        { on: { click: () => { close(); a.fn && a.fn(); } } }, a.label))) : null),
    h('button.x', { title: '关闭', 'aria-label': '关闭', html: icon('x', 's14'), on: { click: () => close() } }));
  box.append(el);
  while (box.children.length > 4) box.firstChild.remove();
  let tm = setTimeout(close, o.ms ?? (o.actions ? 12000 : 3600));
  el.addEventListener('mouseenter', () => clearTimeout(tm));
  el.addEventListener('mouseleave', () => { tm = setTimeout(close, 2500); });
  function close() { clearTimeout(tm); el.classList.add('out'); setTimeout(() => el.remove(), 200); }
  return { close, el };
}
