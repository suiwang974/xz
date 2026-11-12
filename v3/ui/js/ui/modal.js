import { h } from '../util.js';
import { icon } from '../icons.js';
// modal({title, icon, body: Node|string, actions:[{label, pri, danger, value}], onKey}) → Promise<value|null>
export function modal(o) {
  return new Promise((resolve) => {
    const prevFocus = document.activeElement;
    const scrim = h('div.scrim.m');
    const btns = (o.actions || [{ label: '确定', pri: true, value: true }]).map((a) =>
      h('button.btn' + (a.pri ? '.pri' : '') + (a.danger ? '.danger.solid' : ''), { on: { click: () => done(typeof a.value === 'function' ? a.value() : a.value) } }, a.label));
    const box = h('div.modal', { role: 'dialog', 'aria-modal': 'true', 'aria-label': o.title || '' },
      h('div.mh', o.icon ? h('span', { html: icon(o.icon, 's18'), style: { color: o.iconColor || 'var(--acc)' } }) : null, o.title || ''),
      h('div.mb', typeof o.body === 'string' ? h('div', o.body) : o.body),
      h('div.mf', btns));
    function done(v) {
      if (v === undefined) return;
      document.removeEventListener('keydown', key, true);
      scrim.remove(); box.remove(); prevFocus?.focus?.(); resolve(v);
    }
    function key(e) {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); done(null); }
      if (e.key === 'Enter' && !(e.target.tagName === 'TEXTAREA')) {
        const pri = (o.actions || []).find((a) => a.pri);
        if (pri) { e.preventDefault(); e.stopPropagation(); done(typeof pri.value === 'function' ? pri.value() : pri.value); }
      }
    }
    scrim.addEventListener('click', () => done(null));
    document.addEventListener('keydown', key, true);
    document.body.append(scrim, box);
    setTimeout(() => (box.querySelector('input,textarea,select') || btns[btns.length - 1])?.focus(), 30);
  });
}
export async function confirmBox(title, body, okLabel = '确定', danger = false) {
  return !!(await modal({ title, body, icon: danger ? 'alert' : 'info', iconColor: danger ? 'var(--err)' : undefined,
    actions: [{ label: '取消', value: null }, { label: okLabel, pri: !danger, danger, value: true }] }));
}
export async function promptBox(title, label, value = '', placeholder = '') {
  const inp = h('input.input', { value, placeholder, style: { width: '100%' } });
  const r = await modal({ title, body: h('div', { style: { display: 'grid', gap: '8px' } }, label ? h('div', label) : null, inp),
    actions: [{ label: '取消', value: null }, { label: '确定', pri: true, value: () => inp.value }] });
  return r;
}
