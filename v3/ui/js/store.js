// 界面状态：任务表、统计、设置、队列、待确认、待确认区；变化时通知订阅者
import * as api from './api.js';

export const S = {
  tasks: new Map(), order: [], stats: {}, settings: null, schema: [], rpcInfo: {}, queues: [],
  pending: [], grabber: new Map(), logs: [], hist: [], view: 'downloads', filter: 'all', kind: 'all',
  q: '', sort: { key: 'added', dir: -1 }, sel: new Set(), focus: null, theme: 'light',
};
const subs = new Set();
let raf = 0;
export function sub(fn) { subs.add(fn); return () => subs.delete(fn); }
export function changed(what) {
  S._dirty = S._dirty || new Set(); S._dirty.add(what);
  if (raf) return;
  raf = requestAnimationFrame(() => { raf = 0; const d = S._dirty; S._dirty = new Set(); subs.forEach((fn) => fn(d)); });
}

function upsert(list) {
  for (const t of list || []) { if (!S.tasks.has(t.gid)) S.order.push(t.gid); S.tasks.set(t.gid, t); }
}
api.on('snapshot', (d) => {
  S.tasks.clear(); S.order = [];
  upsert(d.tasks); S.stats = d.stats || {}; S.pending = d.pending || [];
  S.grabber = new Map((d.grabber || []).map((g) => [g.url, g]));
  changed('tasks'); changed('stats'); changed('pending'); changed('grabber');
});
api.on('tasks', (d) => {
  upsert(d.upsert);
  for (const g of d.removed || []) { S.tasks.delete(g); S.sel.delete(g); }
  if ((d.removed || []).length) S.order = S.order.filter((g) => S.tasks.has(g));
  changed('tasks');
});
api.on('stats', (d) => {
  S.stats = d; S.hist.push({ t: Date.now(), down: d.down || 0, up: d.up || 0 }); if (S.hist.length > 180) S.hist.shift();
  changed('stats');
});
api.on('log', (d) => { S.logs.push(d); if (S.logs.length > 2000) S.logs.splice(0, S.logs.length - 2000); changed('log'); });
api.on('intercept', (d) => { S.pending.push(d); changed('pending'); });
api.on('grabber', (d) => {
  if (d.reset) S.grabber = new Map((d.items || []).map((g) => [g.url, g]));
  for (const g of d.added || []) S.grabber.set(g.url, g);
  for (const g of d.updated || []) S.grabber.set(g.url, g);
  changed('grabber');
});
api.on('queues', (d) => { if (d.queues) S.queues = d.queues; S.pausedQueues = d.paused || S.pausedQueues || []; changed('queues'); });
api.on('settings', (d) => { if (S.settings) Object.assign(S.settings, d.changed || {}); changed('settings'); });

export async function loadAll() {
  const [snap, st, hist, logs] = await Promise.all([
    api.rpc('sniffdl.snapshot'), api.rpc('sniffdl.settings.get'), api.rpc('sniffdl.stats.speedHistory'), api.rpc('sniffdl.logs', 500),
  ]);
  S.tasks.clear(); S.order = []; upsert(snap.tasks);
  S.stats = snap.stats; S.queues = snap.queues; S.pending = snap.pending; S.grabber = new Map(snap.grabber.map((g) => [g.url, g]));
  S.settings = st.values; S.schema = st.schema; S.rpcInfo = st.rpc; S.defaults = st.defaults;
  S.hist = (hist || []).slice(-180).map((x) => ({ t: x.t * 1000, down: x.down, up: x.up }));
  S.logs = logs || [];
  ['tasks', 'stats', 'settings', 'queues', 'pending', 'grabber', 'log'].forEach(changed);
}
export async function setSetting(patch) {
  const r = await api.rpc('sniffdl.settings.set', patch);
  if (r.changed) Object.assign(S.settings, r.changed);
  changed('settings');
  return r;
}
export function tasksArr() { return S.order.map((g) => S.tasks.get(g)).filter(Boolean); }
