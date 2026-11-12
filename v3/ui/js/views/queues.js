// 队列页：新建 / 改名 / 删除队列，同时下载数，时间段计划（星期 + 时段），自动开始，启停；
// 任务可在队列卡之间拖动（也可多选后右键「移到队列…」或在这里每行的下拉里改）。
import { h, size } from '../util.js';
import { icon } from '../icons.js';
import * as api from '../api.js';
import { S, tasksArr, changed } from '../store.js';
import { toast } from '../ui/toast.js';
import { confirmBox, promptBox } from '../ui/modal.js';

const DAYS = ['一', '二', '三', '四', '五', '六', '日'];
let root = null, editing = null;
export function mountQueues(el) { root = el; }

async function save(q, msg) {
  try {
    const r = await api.rpc('sniffdl.queue.save', q);
    upd(r.queue);
    if (msg) toast({ kind: 'ok', title: msg });
    return r.queue;
  } catch (e) { toast({ kind: 'err', title: '保存失败', body: e.message }); renderQueues(); return null; }
}
function upd(q) { const i = (S.queues || []).findIndex((x) => x.id === q.id); if (i >= 0) S.queues[i] = q; else S.queues.push(q); changed('queues'); }

async function addQueue() {
  const name = await promptBox('新建队列', '队列名称', '新队列');
  if (!name) return;
  const q = await save({ name, max: 0, auto: true, schedule: { enabled: false, start: '02:00', stop: '07:00', days: [0, 1, 2, 3, 4, 5, 6] } }, '已新建队列');
  if (q) { editing = q.id; renderQueues(); }
}
async function renameQueue(q) {
  const name = await promptBox('重命名队列', '队列名称', q.name);
  if (name && name !== q.name) save({ ...q, name }, '已重命名');
}
async function delQueue(q) {
  const n = tasksArr().filter((t) => (t.queue || 'main') === q.id).length;
  if (!(await confirmBox(`删除队列「${q.name}」`, n ? `队列里的 ${n} 个任务会移回主队列，任务本身不删除。` : '队列里没有任务。', '删除', true))) return;
  try { await api.rpc('sniffdl.queue.remove', q.id); S.queues = S.queues.filter((x) => x.id !== q.id); changed('queues'); toast({ kind: 'ok', title: '已删除队列' }); }
  catch (e) { toast({ kind: 'err', title: '删除失败', body: e.message }); }
}
async function startStop(q, run) {
  try {
    const r = await api.rpc('sniffdl.queue.startStop', q.id, run);
    S.pausedQueues = r.paused || []; changed('queues');
    toast({ kind: 'ok', title: run ? `「${q.name}」已开始` : `「${q.name}」已停止`, body: run ? '队列里排队的任务会按同时下载数陆续开始' : '在跑的任务已暂停，开始后自动续上' });
  } catch (e) { toast({ kind: 'err', title: '操作失败', body: e.message }); }
}
async function moveTo(gids, qid) {
  let ok = 0;
  for (const g of gids) { const r = await api.rpc('sniffdl.task.update', g, { queue: qid }).catch((e) => ({ ok: false, error: e.message })); if (r.ok) { ok++; const t = S.tasks.get(g); if (t) t.queue = qid; } }
  changed('tasks'); changed('queues');
  const q = (S.queues || []).find((x) => x.id === qid);
  toast({ kind: ok ? 'ok' : 'warn', title: ok ? `已移到「${q?.name || qid}」` : '没有移动' });
}

function schedText(q) {
  const s = q.schedule || {};
  if (!s.enabled) return null;
  const d = (s.days || []).length === 7 ? '每天' : '周' + (s.days || []).map((i) => DAYS[i]).join('、');
  return `${d} ${s.start}–${s.stop}`;
}

const PH = { active: ['下载中', 'acc'], queued: ['排队', ''], hold: ['稍后', ''], paused: ['已暂停', 'warn'], error: ['失败', 'err'], done: ['完成', 'ok'], seeding: ['做种', 'seed'], retry: ['等重试', 'warn'] };

// soft = 任务变化引起的刷新：只换每张卡的标题行和任务列表，正在编辑的计划表单不动（不丢焦点）
export function renderQueues(soft) {
  if (!root) return;
  if (soft && root.querySelector('.qbox')) {
    const ids = [...root.querySelectorAll('.qbox')].map((c) => c.dataset.q).join(',');
    if (ids === (S.queues || []).map((q) => q.id).join(',')) {
      const fresh = build();
      fresh.querySelectorAll('.qbox').forEach((nc) => {
        const oc = root.querySelector(`.qbox[data-q="${nc.dataset.q}"]`);
        oc.querySelector('.qcard').replaceWith(nc.querySelector('.qcard'));
        const ot = oc.querySelector('.qtasks'), sc = ot.scrollTop; const nt = nc.querySelector('.qtasks'); ot.replaceWith(nt); nt.scrollTop = sc;
      });
      return;
    }
  }
  const sc = root.querySelector('.vbody')?.scrollTop || 0;
  root.innerHTML = '';
  const cards = build();
  const head = h('div.vhead', h('h1', '队列'), h('span.sub', '分组排队、限制同时下载数、只在某个时段下载'), h('div.grow'),
    h('button.btn.pri#qAdd', { on: { click: addQueue } }, h('span', { html: icon('plus', 's14') }), '新建队列'));
  const body = h('div.vbody', cards);
  root.append(head, body);
  body.scrollTop = sc;
}

function build() {
  const qs = S.queues || [];
  const paused = new Set(S.pausedQueues || []);
  const cards = h('div.cards');
  for (const q of qs) {
    const tasks = tasksArr().filter((t) => (t.queue || 'main') === q.id);
    const live = tasks.filter((t) => t.status !== 'complete');
    const run = tasks.filter((t) => t.status === 'active').length;
    const st = paused.has(q.id) ? h('span.chip.warn', '已停止') : run ? h('span.chip.acc', `${run} 个在下`) : null;
    const sch = schedText(q);
    const bits = [q.max ? `同时 ${q.max} 个` : `同时数跟随全局（${S.settings?.max_concurrent || 5}）`, q.auto === false ? '不自动开始' : '加入即开始', sch ? '计划 ' + sch : null].filter(Boolean);
    const isEdit = editing === q.id;
    const card = h('div.card.qbox', { 'data-q': q.id,
      on: {
        dragover: (e) => { if (e.dataTransfer.types.includes('application/x-sdl-gids')) { e.preventDefault(); card.classList.add('drop'); } },
        dragleave: () => card.classList.remove('drop'),
        drop: (e) => { e.preventDefault(); card.classList.remove('drop'); try { const g = JSON.parse(e.dataTransfer.getData('application/x-sdl-gids')); moveTo(g.filter((x) => (S.tasks.get(x)?.queue || 'main') !== q.id), q.id); } catch (er) {} },
      } },
      h('div.qcard',
        h('div', h('div.n', h('span', { html: icon('layers', 's14') }), h('span.qn', q.name), q.id === 'main' ? h('span.chip', '默认') : null, st),
          h('div.d', bits.join(' · '), ` · ${live.length} 个未完成 / 共 ${tasks.length}`)),
        h('div.row',
          paused.has(q.id)
            ? h('button.btn.sm.pri', { 'data-act': 'start', on: { click: () => startStop(q, true) } }, h('span', { html: icon('play', 's14') }), '开始')
            : h('button.btn.sm', { 'data-act': 'stop', on: { click: () => startStop(q, false) } }, h('span', { html: icon('pause', 's14') }), '停止'),
          h('button.btn.sm' + (isEdit ? '.on' : ''), { 'data-act': 'edit', 'aria-expanded': isEdit ? 'true' : 'false', on: { click: () => { editing = isEdit ? null : q.id; renderQueues(); } } }, h('span', { html: icon('gear', 's14') }), '设置'),
          h('button.btn.sm.ghost', { title: '重命名队列', 'data-act': 'rename', on: { click: () => renameQueue(q) } }, '改名'),
          q.id !== 'main' ? h('button.btn.sm.icon.ghost.danger', { title: '删除队列', 'data-act': 'delete', on: { click: () => delQueue(q) } }, h('span', { html: icon('trash', 's14') })) : null)),
      isEdit ? editor(q) : null,
      h('div.qtasks', live.length ? live.slice(0, 50).map((t) => taskLine(t, q)) : h('div.qempty', '把任务拖到这里，或在任务上右键「移到队列…」'),
        live.length > 50 ? h('div.qempty', `还有 ${live.length - 50} 个…`) : null));
    cards.append(card);
  }
  return cards;
}

function taskLine(t, q) {
  const [lab, cls] = PH[t.phase] || [t.status, ''];
  const pct = t.total ? Math.floor((t.done / t.total) * 100) + '%' : '';
  const line = h('div.qt', { draggable: 'true', 'data-gid': t.gid, title: '拖到别的队列',
    on: { dragstart: (e) => { const g = S.sel.has(t.gid) ? [...S.sel] : [t.gid]; e.dataTransfer.setData('application/x-sdl-gids', JSON.stringify(g)); e.dataTransfer.effectAllowed = 'move'; line.classList.add('dragging'); },
      dragend: () => line.classList.remove('dragging') } },
    h('span.grip', { html: icon('grip', 's14') }), h('span.ellipsis.qtn', t.name || t.uri), h('span.muted.num', [t.total ? size(t.total) : '', pct].filter(Boolean).join(' · ')),
    h('span.chip' + (cls ? '.' + cls : ''), lab),
    h('select.select.sm', { title: '移到队列', 'aria-label': '移到队列', on: { change: (e) => moveTo([t.gid], e.target.value) } },
      (S.queues || []).map((x) => h('option', { value: x.id, selected: x.id === q.id }, x.name))));
  return line;
}

function editor(q) {
  const s = { enabled: false, start: '02:00', stop: '07:00', days: [0, 1, 2, 3, 4, 5, 6], ...(q.schedule || {}) };
  const max = h('input.input.num#qMax', { type: 'number', min: 0, max: 32, value: q.max || 0 });
  const auto = h('input#qAuto', { type: 'checkbox', checked: q.auto !== false });
  const en = h('input#qSch', { type: 'checkbox', checked: !!s.enabled });
  const st = h('input.input.num#qStart', { type: 'time', value: s.start });
  const sp = h('input.input.num#qStop', { type: 'time', value: s.stop });
  const days = h('div.days', DAYS.map((d, i) => h('label', h('input', { type: 'checkbox', value: i, checked: (s.days || []).includes(i) }), h('span', d))));
  const apply = () => save({ ...q, max: Number(max.value) || 0, auto: auto.checked,
    schedule: { enabled: en.checked, start: st.value || '02:00', stop: sp.value || '07:00', days: [...days.querySelectorAll('input:checked')].map((x) => Number(x.value)) } }, '队列设置已保存');
  [max, auto, en, st, sp].forEach((x) => x.addEventListener('change', apply));
  days.addEventListener('change', apply);
  return h('div.qedit',
    h('div.set', h('div.l', h('b', '同时下载数'), h('small', '0 = 跟随全局「同时下载数」；两道限制都要满足')), h('div.c', max, h('span.unit', '个'))),
    h('div.set', h('div.l', h('b', '加入后自动开始'), h('small', '关掉 = 「稍后下载」式队列，只在手动开始或计划时段内跑')), h('div.c', h('label.switch', auto, h('span')))),
    h('div.set', h('div.l', h('b', '只在时段内下载'), h('small', '到结束时间自动暂停本队列任务，下个时段自动继续；可跨午夜')), h('div.c', h('label.switch', en, h('span')))),
    h('div.set', h('div.l', h('b', '时段')), h('div.c', st, h('span.unit', '至'), sp)),
    h('div.set', h('div.l', h('b', '星期')), h('div.c', days)));
}
