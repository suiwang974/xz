// 统计页（今日 / 累计 KPI + 近 16 周热力图）与日志页（实时、关键字 / 级别筛选、复制、打开日志文件）。数据来自内核 sniffdl.stats.history / sniffdl.logs。
import { h, size } from '../util.js';
import * as api from '../api.js';
import { S } from '../store.js';
import { toast } from '../ui/toast.js';
import { copyText } from '../util.js';

let sRoot = null, lRoot = null;
export function mountStats(el) { sRoot = el; }
export function mountLogs(el) { lRoot = el; }

// ---------- 统计
export async function renderStats() {
  if (!sRoot) return;
  let d;
  try { d = await api.rpc('sniffdl.stats.history', 112); } catch (e) { sRoot.replaceChildren(h('div.ins-empty', '读取统计失败：' + e.message)); return; }
  const days = d.days || [];
  const mx = Math.max(1, ...days.map((x) => x.bytes));
  const lv = (b) => (b <= 0 ? 0 : b / mx < .25 ? 1 : b / mx < .5 ? 2 : b / mx < .8 ? 3 : 4);
  const first = new Date(days[0]?.date + 'T00:00:00');
  const pad = (first.getDay() + 6) % 7;           // 周一为一周起点
  const cells = [];
  for (let i = 0; i < pad; i++) cells.push(h('i.blank'));
  for (const x of days) cells.push(h('i', { class: 'l' + lv(x.bytes), title: `${x.date}　${size(x.bytes)} · ${x.count} 个`, 'data-date': x.date }));
  const kpi = (k, v, s) => h('div.kpi', h('small', k), h('b', v), h('span', s));
  sRoot.replaceChildren(
    h('div.ins-head', h('h2', '统计'), h('span.ins-sub', '数据只存在本机'),
      h('button.btn', { id: 'statsReset', on: { click: async () => { if (!confirm('清空全部统计记录？下载文件不受影响。')) return; await api.rpc('sniffdl.stats.reset'); toast({ kind: 'ok', title: '统计已重置' }); renderStats(); } } }, '重置统计')),
    h('div.kpis', kpi('今日下载', size(d.today_bytes), `${d.today_count} 个文件`), kpi('累计下载', size(d.total_bytes), `${d.total_count} 个文件`),
      kpi('活跃天数', String(d.active_days), '有下载的天数'), kpi('日均（活跃日）', d.active_days ? size(d.total_bytes / d.active_days) : '—', '累计 ÷ 活跃天数')),
    h('div.ins-card', h('h3', '下载活动 · 近 16 周'), h('div.heat', { id: 'heatmap' }, cells),
      h('div.heat-lg', h('span', '少'), ...[0, 1, 2, 3, 4].map((n) => h('i', { class: 'l' + n })), h('span', '多'))));
}

// ---------- 日志
const LF = { q: '', lv: '' };
const BAD = /失败|错误|异常|error|fail/i, WARN = /警告|重试|超时|warn/i;
function rows() {
  const q = LF.q.toLowerCase();
  return (S.logs || []).filter((r) => {
    const m = r.msg || '';
    if (q && !m.toLowerCase().includes(q)) return false;
    if (LF.lv === 'err') return BAD.test(m);
    if (LF.lv === 'warn') return BAD.test(m) || WARN.test(m);
    return true;
  });
}
const hms = (t) => { const d = new Date(t * 1000), p = (n) => String(n).padStart(2, '0'); return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`; };
export function paintLogs() {
  const box = lRoot?.querySelector('#logBox'); if (!box) return;
  const r = rows().slice(-600);
  const stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
  box.replaceChildren(...(r.length ? r.map((x) => h('div.ll', { class: 'll ' + (BAD.test(x.msg) ? 'bad' : WARN.test(x.msg) ? 'warn' : '') }, h('span.t', hms(x.ts)), h('span', x.msg)))
    : [h('div.ins-empty', '没有匹配的日志')]));
  lRoot.querySelector('#logCount').textContent = `共 ${(S.logs || []).length} 条 · 显示 ${r.length} 条`;
  if (stick) box.scrollTop = box.scrollHeight;
}
export function renderLogs() {
  if (!lRoot) return;
  lRoot.replaceChildren(
    h('div.ins-head', h('h2', '日志'), h('span.ins-sub', { id: 'logCount' }, ''),
      h('input.in', { id: 'logQ', placeholder: '筛选关键字', value: LF.q, on: { input: (e) => { LF.q = e.target.value; paintLogs(); } } }),
      h('select', { id: 'logLv', on: { change: (e) => { LF.lv = e.target.value; paintLogs(); } } },
        h('option', { value: '' }, '全部'), h('option', { value: 'warn' }, '警告及以上'), h('option', { value: 'err' }, '仅错误')),
      h('button.btn', { id: 'logCopy', on: { click: () => { copyText(rows().map((x) => `${hms(x.ts)} ${x.msg}`).join('\n')); toast({ kind: 'ok', title: '已复制 ' + rows().length + ' 行' }); } } }, '复制'),
      h('button.btn', { id: 'logOpen', on: { click: async () => { const r = await api.rpc('sniffdl.os.openLog'); if (!r?.ok) toast({ kind: 'err', title: '打开失败', body: r?.error }); } } }, '打开日志文件')),
    h('div.logbox', { id: 'logBox' }));
  paintLogs();
  const box = lRoot.querySelector('#logBox'); box.scrollTop = box.scrollHeight;
}
