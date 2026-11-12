(() => {
// =====================================================================
//  大肥鱼 桌宠 · 预览版
//  结构：几何/位置 → 表情层(Face) → 说话(Speech) → 状态机(update) → 交互 → 道具 → 日常行为 → 关心系统(Life) → 下载助手 → 指令 → UI
// =====================================================================
const $ = id => document.getElementById(id);
const rnd = (a, b) => a + Math.random() * (b - a), pick = a => a[Math.random() * a.length | 0];
const clamp = (v, a, b) => Math.max(a, Math.min(b, v)), now = () => performance.now();
const lerp = (a, b, t) => a + (b - a) * t;
const stage = $('stage'), petEl = $('pet'), bub = $('bubble'), win = $('win'), shadow = $('shadow'), menu = $('menu');

// ---------------- 记忆存储（本地） ----------------
const KEY = 'dfy_pet_v1';
const today = () => { const d = new Date(); return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0'); };
const yesterday = () => { const d = new Date(Date.now() - 864e5); return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0'); };
let M = null;
function loadMem() {
  try { M = JSON.parse(localStorage.getItem(KEY)) || null; } catch (e) { M = null; }
  if (!M) M = { nick: '', firstSeen: Date.now(), lastSeen: 0, aff: 30, birthday: '08-15', chibi: 1,
    stats: { pokes: 0, pats: 0, fed: 0, cups: 0, dl: 0, pomo: 0, chats: 0, dizzy: 0, facts: 0, basin: 0, rps: 0 },
    ach: {}, days: {}, remind: { water: true, meal: true, sleep: true, stand: true, eye: true }, outfit: { long: 0, chest: 0, tail: 1 } };
  M.stats = Object.assign({ pokes: 0, pats: 0, fed: 0, cups: 0, dl: 0, pomo: 0, chats: 0, dizzy: 0, facts: 0, basin: 0, rps: 0 }, M.stats);
  M.remind = Object.assign({ water: true, meal: true, sleep: true, stand: true, eye: true }, M.remind);
  M.outfit = Object.assign({ long: 0, chest: 0, tail: 1 }, M.outfit);
}
function day(k) { k = k || today(); if (!M.days[k]) M.days[k] = { water: 0, meals: {}, sleep: '', mood: '', sport: '', asked: {}, pokes: 0, pats: 0, fed: 0, dl: 0, pomo: 0, first: Date.now() }; return M.days[k]; }
let saveT = 0; function save() { clearTimeout(saveT); saveT = setTimeout(() => { try { const ks = Object.keys(M.days).sort(); while (ks.length > 30) delete M.days[ks.shift()]; localStorage.setItem(KEY, JSON.stringify(M)); } catch (e) {} }, 300); }
function fmt(s) {
  const d = day(); const h = new Date().getHours(), mi = new Date().getMinutes();
  return s.replace(/^\{n\}[，,]/, M.nick ? '{n}，' : '').replace(/\{n\}/g, M.nick || '').replace(/\{cups\}/g, d.water).replace(/\{pokes\}/g, pokeChain)
    .replace(/\{time\}/g, h + ':' + String(mi).padStart(2, '0'))
    .replace(/\{tcomment\}/g, h < 5 ? '这是睡觉的时间！' : h < 9 ? '早上好～' : h < 12 ? '上午效率最高！' : h < 14 ? '该吃午饭啦' : h < 18 ? '下午加油～' : h < 22 ? '晚上放松一下吧' : '该准备睡觉啦');
}
function line(k) { const a = LINES[k]; if (!a) return k; let s, i = 0; do { s = pick(a); i++; } while (a.length > 1 && s === line.last[k] && i < 6); line.last[k] = s; return fmt(s); }
line.last = {};

// ---------------- 全局状态 ----------------
const P = { x: 0, y: -400, vx: 0, vy: 0, mode: 'air', until: 0, next: null, walkTarget: null, walkV: 0, walkPhase: 0, dir: 1,
  onLedge: false, rot: 0, rotV: 0, sx: 1, sy: 1, angryUntil: 0, dragCount: 0, shake: 0, dizzy: false, wobble: 0 };
let mood = 65, energy = 80, fast = false, quiet = false, lastAct = now(), lastMouse = 0, neglect = 0, routineAt = 0, pomo = null;
let pokeChain = 0, pokeAt = 0, basinHits = 0;
const ACC = new Set();           // 常驻配件：key8 加载 / key11 眼镜 / key12 单片眼镜 / key10 钢盆 / Clothes4 脱鞋
const STATE_CN = { work: '下载中', paused: '已暂停', sniff: '嗅探中', air: '飞行中', idle: '待机', walk: '散步', sleep: '睡觉', drag: '被拎着', react: '互动中', eat: '吃饭', prep: '准备起跳', pomo: '陪你专注', land: '落地', sit: '在窗台' };
function setMood(d) { mood = clamp(mood + d, 0, 100); $('mood').firstElementChild.style.width = mood + '%'; }
function aff(d) { M.aff = clamp((M.aff || 30) + d, 0, 100); save(); }
function T(ms) { return fast ? ms * .1 : ms; }
function floorY() { return stage.clientHeight; }
function ledge() { return { y: -1e9, l: 1e9, r: -1e9 }; }   // 桌面版禁用窗台

// ---------------- 几何：盒子大小随 Q版/正常版 平滑变化 ----------------
let box = { w: 200, h: 260 }, lastLayoutK = -1;
function boxFor(k) {
  const H = stage.clientHeight;
  const ch = clamp(H * .3, 190, 290), nh = clamp(H * .62, 330, 600);
  const S = window.__petScale || 1;
  const h = lerp(nh, ch, k) * S, w = h * lerp(.62, .9, k);
  return { w: Math.round(w), h: Math.round(h) };
}
function relayout(force) {
  const k = L2D.chibiLevel();
  if (!force && Math.abs(k - lastLayoutK) < .004) return;
  lastLayoutK = k; box = boxFor(k); L2D.layout(box.w, box.h);
  petEl.style.width = box.w + 'px'; petEl.style.height = box.h + 'px';
}
// 人物在盒子里的实际范围（用于点击判定）
function charRect() {
  const k = L2D.chibiLevel(), aspect = lerp(.462, .707, k);
  const ch = box.h * .9, cw = Math.min(box.w * .86, ch * aspect);
  const left = P.x - cw / 2, top = P.y - box.h * .025 - ch;
  return { left, top, w: cw, h: ch, headFrac: lerp(.2, .5, k) };
}
function headPoint() { const r = charRect(); return { x: P.x, y: r.top + r.h * r.headFrac * .55 }; }
function region(cx, cy) {
  const r = charRect(); if (cx < r.left - 6 || cx > r.left + r.w + 6 || cy < r.top - 6 || cy > r.top + r.h + 6) return null;
  const fy = (cy - r.top) / r.h, fx = (cx - r.left) / r.w;
  if (fy < .07 && Math.abs(fx - .5) < .2) return 'ahoge';
  if (fy < r.headFrac) { if (fy > r.headFrac * .55 && Math.abs(fx - .5) < .22) return 'cheek'; return 'head'; }
  if (Math.abs(fx - .5) > .38) return null;
  return 'body';
}

// ===================== 表情层 =====================
// 预设只描述“偏离默认多少”，最终值 = 默认 + 情绪底色 + 当前表情 + 程序化（眨眼/说话/视线/呼吸/身体）
const FACES = {
  neutral: {},
  smile: { EyeSmile: .5, MouthSmile_L: .6, MouthSmile_R: .6, ParamMouthForm: .5 },
  happy: { EyeSmile: 1, MouthSmile_L: 1, MouthSmile_R: 1, ParamMouthForm: 1, ParamBrowLY: .4, ParamBrowRY: .4 },
  joy: { EyeSmile: 1, MouthSmile_L: 1, MouthSmile_R: 1, ParamMouthForm: 1, ParamMouthOpenY: .55, ParamA: .6, ParamBrowLY: .6, ParamBrowRY: .6, key9: 1 },
  surprised: { eyeOpen: 1.3, ParamMouthOpenY: .5, ParamO: .7, ShockL: 1, ShockR: 1, ParamBrowLY: .9, ParamBrowRY: .9 },
  shy: { key7: 1, eyeOpen: .75, ParamMouthForm: .2, Mouthpucker: .3, lookY: -.5, lookX: .5 },
  love: { key4: 1, key7: 1, EyeSmile: .4, MouthSmile_L: 1, MouthSmile_R: 1, ParamMouthForm: 1 },
  pout: { CheeckPuff: 1, Mouthpucker: .5, ParamBrowLAngle: -.7, ParamBrowRAngle: -.7 },
  angry: { CheeckPuff: 1, ParamBrowLAngle: -1, ParamBrowRAngle: -1, ParamBrowLY: -.5, ParamBrowRY: -.5, ParamMouthForm: -.6, eyeOpen: .9 },
  dark: { key1: 1, ParamBrowLAngle: -1, ParamBrowRAngle: -1, ParamMouthForm: -1, eyeOpen: .8 },
  sad: { BrowInnerUp: 1, ParamMouthForm: -.8, eyeOpen: .75, lookY: -.4 },
  cry: { key2: 1, BrowInnerUp: 1, ParamMouthForm: -1, ParamMouthOpenY: .3, eyeOpen: .7 },
  dizzy: { key3: 1, ParamMouthOpenY: .3, ParamMouthForm: -.3 },
  xx: { key5: 1, ParamMouthOpenY: .4, ParamMouthForm: -.4 },
  smug: { key6: 1, MouthSmile_L: .8, ParamMouthForm: .4 },
  sweat: { key13: 1, BrowInnerUp: .6, ParamMouthForm: -.3 },
  sleepy: { eyeOpen: .45, ParamMouthForm: 0, lookY: -.3 },
  sleep: { eyeOpen: 0, ParamMouthOpenY: .12, lookY: -.6 },
  yawn: { eyeOpen: .1, ParamMouthOpenY: 1, JawOpen: .8, ParamO: 1, BrowInnerUp: .5 },
  eat: { EyeSmile: 1, MouthSmile_L: .7, MouthSmile_R: .7 },
  think: { lookX: -.6, lookY: .6, MouthX: .4, Movement3: 1 },
  grass: { Movement1: 1, EyeSmile: .6, MouthSmile_L: .8, MouthSmile_R: .8 },
  relief: { Movement2: 1, eyeOpen: .6, ParamMouthOpenY: .2, ParamO: .4 },
  finger: { Movement3: 1, EyeSmile: .3, ParamBrowLY: .5, ParamBrowRY: .5, ParamMouthForm: .5 },
  wink: { EyeSmile: .6, winkL: 1, MouthSmile_L: 1, MouthSmile_R: 1, TongueOut: .4 },
  tongue: { TongueOut: 1, winkL: 1, EyeSmile: .4 },
  busyhand: { Movement2: 1, EyeSmile: 1, MouthSmile_L: 1, MouthSmile_R: 1, ParamMouthForm: 1, ParamBrowLY: .4, ParamBrowRY: .4 },
  doneLove: { key4: 1, key7: 1, EyeSmile: .4, MouthSmile_L: 1, MouthSmile_R: 1, ParamMouthForm: 1 },
  focus: { eyeOpen: 1.05, ParamBrowLY: -.2, ParamBrowRY: -.2, MouthX: .1 },
};
let faceNow = { name: 'neutral', until: 0 };
function face(name, ms) { faceNow = { name, until: now() + (ms || 1500), t0: now() }; if (Math.random() < .5) blink(); }
function faceName() { return now() < faceNow.until ? faceNow.name : null; }

// ---- 眨眼（自然间隔，偶尔双眨，视线大跳时也会眨） ----
let blinkT = 0, blinkPhase = 0, nextBlink = now() + 2000, dblBlink = false;
function blink() { if (blinkPhase === 0) { blinkPhase = 1; blinkT = now(); } }
function blinkFactor(t) {
  if (blinkPhase === 0) { if (t > nextBlink) { blink(); dblBlink = Math.random() < .18; nextBlink = t + rnd(2200, 6000) * (energy < 35 ? .6 : 1); } return 1; }
  const e = t - blinkT;
  if (e < 70) return 1 - e / 70;
  if (e < 110) return 0;
  if (e < 230) return (e - 110) / 120;
  blinkPhase = 0; if (dblBlink) { dblBlink = false; setTimeout(blink, 90); } return 1;
}

// ---- 视线：眼睛先到，头慢半拍跟上（靠参数平滑速度不同实现） ----
const gaze = { x: 0, y: 0, until: 0, tx: null, ty: null, wander: 0 };
let mouse = { x: -1, y: -1, t: 0 };
function lookAt(x, y, ms) { gaze.tx = x; gaze.ty = y; gaze.until = now() + (ms || 1500); if (Math.random() < .4) blink(); }
function gazeTarget(t) {
  if (gaze.tx != null && t < gaze.until) return { x: gaze.tx, y: gaze.ty };
  if (t - mouse.t < 3500 && mouse.x >= 0) return { x: mouse.x, y: mouse.y };
  if (t > gaze.wander) { gaze.wander = t + rnd(1800, 5000); const h = headPoint(); gaze.wx = h.x + rnd(-260, 260); gaze.wy = h.y + rnd(-160, 120); if (Math.random() < .25) { gaze.wx = h.x; gaze.wy = h.y + 30; } if (Math.random() < .35) blink(); }
  return { x: gaze.wx, y: gaze.wy };
}

// ---- 说话：嘴型跟着文字动（元音随机、标点停顿） ----
let talk = { text: '', t0: 0, dur: 0 };
function mouthTalk(t) {
  if (t - talk.t0 > talk.dur) return null;
  const i = Math.floor((t - talk.t0) / 85), ch = talk.text[i] || '';
  if (/[，。！？…、,.!?～\s]/.test(ch)) return { open: .05, v: 'A' };
  const k = (i * 7919) % 5, v = 'AIUEO'[k];
  const o = .35 + .5 * Math.abs(Math.sin((t - talk.t0) / 85 * Math.PI));
  return { open: o, v };
}

// ---- 每帧合成所有参数 ----
let breathT = 0, noiseT = rnd(0, 100);
function faceUpdate(t, dt) {
  const v = {}; // 目标值
  const fnm = faceName();
  // 状态决定的持续表情
  let base = 'neutral';
  if (P.mode === 'sleep' || P.mode === 'paused') base = 'sleep';
  else if (P.mode === 'eat') base = 'eat';
  else if (P.mode === 'pomo') base = 'focus';
  else if (P.mode === 'work') base = DL.slowUntil > t ? 'sweat' : 'focus';
  else if (P.mode === 'drag') base = P.dizzy ? 'dizzy' : 'surprised';
  else if (t < P.angryUntil) base = 'pout';
  else if (energy < 25) base = 'sleepy';
  else if (mood > 78) base = 'smile';
  else if (mood < 25) base = 'sad';
  const F = Object.assign({}, FACES[base] || {}, fnm ? FACES[fnm] || {} : {});
  // 情绪底色（轻微）
  const moodSmile = clamp((mood - 50) / 50, -1, 1) * .25;
  v.ParamMouthForm = (F.ParamMouthForm != null ? F.ParamMouthForm : moodSmile);
  v.EyeSmile = F.EyeSmile || 0; v.MouthSmile_L = F.MouthSmile_L || Math.max(0, moodSmile); v.MouthSmile_R = F.MouthSmile_R || Math.max(0, moodSmile);
  for (const k of ['ParamBrowLY', 'ParamBrowRY', 'ParamBrowLAngle', 'ParamBrowRAngle', 'BrowInnerUp', 'Mouthpucker', 'MouthFunnel', 'CheeckPuff', 'TongueOut', 'MouthX', 'ShockL', 'ShockR', 'JawOpen', 'Movement1', 'Movement2', 'Movement3'])
    v[k] = F[k] || 0;
  for (let i = 1; i <= 13; i++) v['key' + i] = F['key' + i] || 0;
  v.Clothes4 = 0; for (const a of ACC) v[a] = 1;
  // 眼睛开合 = 表情 × 眨眼 × 困意
  let eo = F.eyeOpen != null ? F.eyeOpen : 1;
  if (energy < 40 && F.eyeOpen == null) eo *= lerp(.55, 1, energy / 40);
  const bf = blinkFactor(t);
  const eyeL = eo * (F.winkL ? 0 : 1) * (eo > .05 ? bf : 1), eyeR = eo * (eo > .05 ? bf : 1);
  v.ParamEyeLOpen = eyeL; v.ParamEyeROpen = eyeR;
  // 嘴：说话 > 表情
  const mt = mouthTalk(t);
  v.ParamMouthOpenY = F.ParamMouthOpenY || 0; for (const c of 'AIUEO') v['Param' + c] = F['Param' + c] || 0;
  if (mt && !F.ParamMouthOpenY) { v.ParamMouthOpenY = mt.open * .8; v['Param' + mt.v] = Math.max(v['Param' + mt.v], mt.open); }
  if (P.mode === 'eat') { const c = Math.abs(Math.sin(t / 120)); v.ParamMouthOpenY = c * .55; v.ParamA = c * .5; }
  // 视线
  const h = headPoint();
  let lx = 0, ly = 0;
  if (P.mode !== 'sleep' && P.mode !== 'paused' && !(fnm === 'sleep')) {
    const g = gazeTarget(t); lx = clamp((g.x - h.x) / 260, -1, 1); ly = clamp(-(g.y - h.y) / 220, -1, 1);
  }
  if (F.lookX != null) lx = F.lookX; if (F.lookY != null) ly = F.lookY;
  v.ParamEyeBallX = lx; v.ParamEyeBallY = ly;
  // 头/身体（追踪输入 → 物理）
  noiseT += dt;
  const nz = (a, b, c) => Math.sin(noiseT * a + b) * .6 + Math.sin(noiseT * c + b * 2) * .4;
  let ax = lx * 16 + nz(.37, 1, .91) * 3, ay = ly * 12 + nz(.29, 2, .73) * 2.5, az = nz(.23, 3, .61) * 3;
  let px = nz(.19, 4, .53) * 2.5, py = 0, pz = nz(.17, 5, .47) * 2;
  // 呼吸
  const bspd = (P.mode === 'sleep') ? .22 : .32; breathT += dt * bspd; const br = (Math.sin(breathT * Math.PI * 2) + 1) / 2;
  v.ParamBreath = br;
  // 状态动作
  if (P.mode === 'walk') {
    const s = Math.sin(P.walkPhase * Math.PI * 2), sp = Math.abs(P.walkV) / 70;
    ax += P.dir * 14 * sp; px += P.dir * 9 * sp; az += -P.dir * 2 * sp + s * 2.5 * sp; py += Math.abs(s) * 7 * sp;
  }
  if (P.mode === 'air') { py += clamp(-P.vy / 60, -12, 12); ax += clamp(P.vx / 40, -15, 15); }
  if (P.mode === 'drag') { az += clamp(P.rot * .9, -25, 25); px += clamp(P.rotV / 20, -25, 25); ay += 8; py -= 6; }
  if (P.mode === 'sleep' || P.mode === 'paused') { ay = -16; az = 12 + Math.sin(t / 2600) * 2; px = 0; }
  if (fnm === 'shy') { ay -= 8; az += 6; }
  if (fnm === 'sad' || fnm === 'cry') ay -= 8;
  if (fnm === 'yawn') { ay += 12; py += 4; }
  if (fnm === 'think') { ay += 6; az -= 8; }
  if (fnm === 'joy') py += Math.abs(Math.sin(t / 140)) * 8;
  if (fnm === 'grass') { az += Math.sin(t / 300) * 6; px += Math.sin(t / 300) * 5; }
  if (P.mode === 'pomo' || P.mode === 'work') { ay = -6 + nz(.5, 7, 1.3) * 2; ax = nz(.2, 8, .7) * 6; }
  if (dance.on) { const b = t / 1000 * 2.2 * Math.PI; ax += Math.sin(b) * 14; az += Math.sin(b) * 10; px += Math.sin(b) * 14; py += Math.abs(Math.cos(b)) * 10; }
  if (P.wobble > 0) az += Math.sin(t / 60) * 10 * P.wobble;
  v.AngleX = ax; v.AngleY = ay; v.AngleZ = az; v.PositionX = px; v.PositionY = py; v.PositionZ = pz;
  // 送进模型：不同参数不同“反应速度”
  const fastIds = /EyeLOpen|EyeROpen|MouthOpenY|Param[AIUEO]$/;
  for (const k in v) {
    let rate = 9;
    if (fastIds.test(k)) rate = 28; else if (/EyeBall/.test(k)) rate = 16; else if (/^Angle/.test(k)) rate = 5; else if (/^Position/.test(k)) rate = 4.5; else if (/^key|^Movement/.test(k)) rate = 7;
    L2D.def(k, v[k], rate);
  }
}

// ===================== 气泡 + 粒子 =====================
function say(k, force) {
  if (quiet && !force) return;
  const s = typeof k === 'string' && LINES[k] ? line(k) : fmt(String(k)); if (!s) return;
  bub.textContent = s; bub.classList.add('show');
  talk = { text: s, t0: now(), dur: Math.min(4000, s.length * 85) };
  clearTimeout(say.t); say.t = setTimeout(() => bub.classList.remove('show'), Math.max(2600, s.length * 190));
  M.stats.chats++;
}
function particle(ch, n = 1) {
  const r = charRect();
  for (let i = 0; i < n; i++) {
    const e = document.createElement('div'); e.className = 'pt'; e.textContent = ch;
    e.style.left = (P.x + rnd(-r.w * .35, r.w * .35)) + 'px'; e.style.top = (r.top + rnd(0, r.h * .3)) + 'px';
    e.style.setProperty('--dx', rnd(-12, 12) + 'px'); e.style.animationDelay = (i * .12) + 's';
    stage.appendChild(e); setTimeout(() => e.remove(), 1500 + i * 120);
  }
}
function squash(sx, sy) { P.sx = sx; P.sy = sy; }
function toast(html, btns, ms) {
  const el = document.createElement('div'); el.className = 'toast'; el.innerHTML = html;
  (btns || []).forEach(([t, f]) => { const b = document.createElement('button'); b.textContent = t; b.onclick = e => { e.stopPropagation(); f(); el.remove(); }; el.appendChild(b); });
  const bx = $('toasts'); bx.appendChild(el); while (bx.children.length > 3) bx.firstChild.remove(); setTimeout(() => el.remove(), ms || 7000);
  const r = el.getBoundingClientRect(); lookAt(r.left + r.width / 2, r.top + r.height / 2, 1200);
}
let chipsOpen = false;
function chips(list, ms) {
  const c = $('chips'); c.innerHTML = '';
  list.forEach(([t, f, ghost]) => { const b = document.createElement('button'); b.textContent = t; if (ghost) b.className = 'ghost';
    b.onpointerdown = e => e.stopPropagation(); b.onclick = e => { e.stopPropagation(); c.style.display = 'none'; chipsOpen = false; touch(); f(); }; c.appendChild(b); });
  c.style.display = 'flex'; chipsOpen = true; clearTimeout(chips.t);
  chips.t = setTimeout(() => { c.style.display = 'none'; chipsOpen = false; }, ms || 15000);
}
function ask(q, opts, fc, ms) { if (P.mode === 'sleep') wake(true); if (fc) face(fc, 2500); say(q, true); chips(opts, ms); }

// ===================== 动作 =====================
function pose(fc, ms, then) { P.mode = 'react'; face(fc, ms); P.until = now() + ms; P.next = then || null; }
function toIdle() {
  if (pomo && !dlActive() && P.mode !== 'drag') { P.mode = 'pomo'; return; }
  if (dlActive() && !DL.paused) { P.mode = 'work'; return; }
  if (dlActive() && DL.paused) { P.mode = 'paused'; return; }
  P.mode = P.onLedge ? 'sit' : 'idle';
}
function hop(h = 140, dx = 0) { P.onLedge = false; P.mode = 'prep'; squash(1.06, .9); P.until = now() + 160; P.next = () => { P.mode = 'air'; P.vy = -Math.sqrt(2 * 2200 * h); P.vx = dx; }; }
function jumpToLedge() { toIdle(); return;   // 桌面版禁用窗台
  const L = ledge(); const tx = clamp(P.x, L.l + 20, L.r - 20); const h = P.y - L.y + 70; if (h < 40) { toIdle(); return; }
  P.mode = 'prep'; squash(1.08, .86); P.until = now() + 220; lookAt(tx, L.y - 40, 800);
  P.next = () => { P.mode = 'air'; const vy = -Math.sqrt(2 * 2200 * h); const tUp = -vy / 2200, tDown = Math.sqrt(2 * 70 / 2200); P.vy = vy; P.vx = (tx - P.x) / (tUp + tDown); };
}
function walkTo(x) { P.mode = 'walk'; P.walkTarget = clamp(x, box.w * .3, stage.clientWidth - box.w * .3); P.dir = P.walkTarget > P.x ? 1 : -1; lookAt(P.walkTarget, P.y - 40, 900); }
function sleep() { P.mode = 'sleep'; const h = new Date().getHours(); if (h >= 21 || h < 7) ACC.add('Clothes4'); }
function wake(silent) {
  say(pick(['唔…我没睡着！', '被发现了…', '嗯？天亮了吗', '呼啊…你回来啦']), !!silent);
  pose('surprised', 900, toIdle); squash(.92, 1.08); particle('❗'); neglect = 0;
  const h = new Date().getHours(); if (!(h >= 22 || h < 7)) ACC.delete('Clothes4');
}
const dance = { on: false, until: 0 };
function doDance(ms) { dance.on = true; dance.until = now() + (ms || 4000); face('happy', ms || 4000); P.mode = 'react'; P.until = now() + (ms || 4000); P.next = () => { dance.on = false; toIdle(); }; particle('🎵', 3); }

// ===================== 输入 =====================
let ptr = null;
function touch() { lastAct = now(); neglect = 0; }
function activity() { touch(); if (P.mode === 'sleep') wake(); }
let circ = { a: 0, last: null, sum: 0, t: 0 };
let hoverT = 0;
stage.addEventListener('pointermove', e => {
  const t = now(); mouse = { x: e.clientX, y: e.clientY, t }; lastMouse = t;
  if (!ptr) { if (P.mode === 'sleep' && Math.hypot(e.movementX || 0, e.movementY || 0) > 6 && Math.random() < .03) activity(); else touch(); }
  // 鼠标在她身边快速画圈 → 跟着转晕
  const h = headPoint(); const a = Math.atan2(e.clientY - h.y, e.clientX - h.x), d = Math.hypot(e.clientY - h.y, e.clientX - h.x);
  if (d < 220 && d > 30 && circ.last != null) { let da = a - circ.last; if (da > Math.PI) da -= 2 * Math.PI; if (da < -Math.PI) da += 2 * Math.PI; circ.sum += da; if (t - circ.t > 1600) { circ.sum = 0; circ.t = t; } if (Math.abs(circ.sum) > Math.PI * 6 && P.mode === 'idle') { circ.sum = 0; circ.t = t; pose('dizzy', 2200, toIdle); say('cursor_circle'); P.wobble = 1; particle('💫', 2); } }
  circ.last = a;
  // 鼠标停在她身上很久 → 害羞
  if (region(e.clientX, e.clientY) && !ptr) { if (!hoverT) hoverT = t; if (t - hoverT > 3500 && (P.mode === 'idle' || P.mode === 'sit')) { hoverT = t + 15000; if (M.aff > 60) { pose('love', 2000, toIdle); say(pick(['嘿嘿，你在看我～', '一直看着我…是喜欢我吗？'])); } else { pose('shy', 2000, toIdle); say('hover_long'); } } }
  else hoverT = 0;
});
stage.addEventListener('contextmenu', e => { if (region(e.clientX, e.clientY)) e.preventDefault(); });
stage.addEventListener('pointerdown', e => {
  if (e.target.closest('#fabs,#hud,#help-btn,#menu,#chips,#cmd,#sheet,#book,.toast,#bowl,#cup,#link,#win .bar')) return;
  const rg = region(e.clientX, e.clientY); closeMenu(); if (!rg) return;
  if (e.button === 2) { e.preventDefault(); openMenu(e.clientX, e.clientY); return; }
  e.preventDefault(); stage.setPointerCapture(e.pointerId);
  ptr = { id: e.pointerId, x0: e.clientX, y0: e.clientY, x: e.clientX, y: e.clientY, t0: now(), rg, dirs: 0, lastDx: 0, patStart: 0, moved: 0, hist: [], mode: null,
    long: setTimeout(() => { if (ptr && ptr.moved < 8 && !ptr.mode) { ptr.mode = 'menu'; openMenu(ptr.x, ptr.y); } }, 520) };
  touch();
});
stage.addEventListener('pointermove', e => {
  if (!ptr || e.pointerId !== ptr.id) return; const dx = e.clientX - ptr.x, dy = e.clientY - ptr.y; ptr.x = e.clientX; ptr.y = e.clientY; ptr.moved += Math.hypot(dx, dy); touch();
  ptr.hist.push({ t: now(), x: e.clientX, y: e.clientY }); if (ptr.hist.length > 8) ptr.hist.shift();
  if (ptr.mode === 'menu') return;
  if (ptr.mode === 'drag') { dragMove(e, dx); return; }
  if ((ptr.rg === 'head' || ptr.rg === 'ahoge') && ptr.mode !== 'drag') {
    const tdx = e.clientX - ptr.x0, tdy = e.clientY - ptr.y0;
    if (Math.abs(tdy) > 60 && ptr.mode !== 'pat') { startDrag(e); return; }
    if (Math.abs(dx) > 1.5) { const s = Math.sign(dx); if (ptr.lastDx && s !== ptr.lastDx) ptr.dirs++; ptr.lastDx = s; }
    if (ptr.dirs >= 2 && ptr.mode !== 'pat') { ptr.mode = 'pat'; ptr.patStart = now(); startPat(); }
    if (ptr.mode === 'pat') { patTick(); return; }
    if (Math.abs(tdx) > 90 && ptr.dirs === 0) startDrag(e);
    return;
  }
  if (ptr.moved > 8 && ptr.mode !== 'pat') startDrag(e);
});
function endPtr(e) {
  if (!ptr || e.pointerId !== ptr.id) return; clearTimeout(ptr.long); const p = ptr; ptr = null;
  if (p.mode === 'drag') { release(p); return; } if (p.mode === 'pat') { endPat(p); return; } if (p.mode === 'menu') return;
  if (now() - p.t0 < 380 && p.moved < 10) onTap(p);
}
stage.addEventListener('pointerup', endPtr); stage.addEventListener('pointercancel', endPtr);

let lastTap = 0, tapTimer = null;
function onTap(p) {
  if (pomo) { face('finger', 1200); say(pick(['嘘，专心中～', '还没到时间哦，加油！', '专注专注！']), true); return; }
  const t = now();
  if (dlActive()) { if (t - lastTap < 300) { clearTimeout(tapTimer); lastTap = 0; squash(.95, 1.05); openCmd(); return; } lastTap = t; tapTimer = setTimeout(dlTap, 300); return; }
  if (t - lastTap < 300) { clearTimeout(tapTimer); lastTap = 0; doubleTap(); return; }
  lastTap = t; tapTimer = setTimeout(() => singleTap(p.rg), 300);
}
// ---- 戳戳彩蛋：一直戳会怎样？ ----
function singleTap(rg) {
  const t = now();
  if (P.mode === 'sleep') { wake(); return; }
  if (t - pokeAt > 12000) { pokeChain = 0; basinHits = 0; if (ACC.has('key10')) { ACC.delete('key10'); } }
  pokeAt = t; pokeChain++; M.stats.pokes++; day().pokes++; save();
  squash(.94, 1.06);
  if (ACC.has('key10')) {           // 钢盆模式：戳到的都是盆
    basinHits++; M.stats.basin++; particle('💥'); stage.classList.remove('shake'); void stage.offsetWidth; stage.classList.add('shake');
    if (basinHits === 15) { say('poke_basin_peek', true); face('sweat', 2500); }
    else if (basinHits >= 30) { ACC.delete('key10'); pose('dizzy', 2600, () => { toIdle(); say('poke_end', true); }); unlock('iron', '铁头娃', '对着钢盆戳了 30 下'); pokeChain = 0; basinHits = 0; P.wobble = 1; }
    else say('poke_basin_hit', true);
    return;
  }
  if (rg === 'ahoge' && pokeChain < 5) { pose('surprised', 900, toIdle); say('ahoge'); P.wobble = .8; particle('📡'); return; }
  if (rg === 'cheek' && pokeChain < 5) { pose('pout', 1000, () => { say(pick(['噗——', '哼'])); toIdle(); }); say('cheek'); return; }
  const c = pokeChain;
  if (c < 3) { pose('surprised', 700, toIdle); say('poke1'); particle('❗'); if (P.mode !== 'sit' && !P.onLedge) { P.vy = -320; P.air = true; } setMood(+1); return; }
  if (c < 5) { pose('pout', 1100, toIdle); say('poke2'); return; }
  if (c === 5 || c === 6) { P.angryUntil = t + 7000; setMood(-6); aff(-1); pose('angry', 2200, toIdle); say('poke_angry'); particle('💢'); return; }
  if (c < 10) { pose('dark', 2200, toIdle); say(c === 7 ? 'poke_dark' : pick(['……', '（无视你）', '哼！'])); return; }
  if (c < 14) { pose('cry', 2600, toIdle); say(c === 10 ? 'poke_cry' : pick(['呜…', '呜呜呜', '坏人…'])); particle('💧'); setMood(-3); return; }
  if (c < 18) { pose('dizzy', 2200, toIdle); say(c === 14 ? 'poke_dizzy' : pick(['@_@', '晕…', '星星…'])); P.wobble = 1; return; }
  if (c === 18) { ACC.add('key10'); pose('smug', 1800, toIdle); say('poke_basin', true); particle('🛡'); basinHits = 0; return; }
}
function doubleTap() {
  if (now() < P.angryUntil) { say('哼！双击也没用！', true); return; }
  if (P.mode === 'sleep') { wake(); return; }
  face('joy', 1400); say(pick(['耶！', '在这里！', '叫我干嘛呀～', '嘿嘿！'])); particle('✨', 4); setMood(+3);
  P.onLedge = false; P.mode = 'air'; P.vy = -650; P.vx = 0; setTimeout(() => { if (P.mode !== 'drag') openCmd(); }, 650);
}
// ---- 摸头 ----
function startPat() {
  if (P.mode === 'sleep') wake(true);
  if (now() < P.angryUntil || pokeChain >= 5) { P.angryUntil = 0; pokeChain = 0; ACC.delete('key10'); say('forgive', true); setMood(+6); aff(+1); }
  else if (DL.failStreak >= 2) say('下次一定行…谢谢你', true);
  else if (DL.done > 0 && Math.random() < .3) say('今天帮你下了 ' + DL.done + ' 个、' + fmtSize(DL.bytes) + ' 哦，夸我！', true);
  else say('pat');
  P.preMode = P.mode; P.mode = 'react'; P.until = now() + 99999; face('happy', 99999); P.patting = true;
  M.stats.pats++; day().pats++; save();
}
function patTick() {
  if (!P.patting) return; const e = now() - ptr.patStart;
  if (Math.random() < .07) particle('💗'); setMood(+.12);
  if (e > 4000 && faceNow.name === 'happy') { face(M.aff > 55 ? 'love' : 'shy', 99999); say('pat_long'); particle('💗', 2); }
  if (e > 16000 && P.mode !== 'sleep') { P.patting = false; face('sleep', 1); say('pat_sleep', true); setTimeout(() => { sleep(); }, 600); unlock('patsleep', '摸睡了', '一直摸头把她摸睡着了'); }
}
function endPat(p) {
  if (!P.patting) return; P.patting = false; aff(+1);
  if (M.stats.pats >= 100) unlock('pat100', '摸头上瘾', '累计摸头 100 次');
  if (dlActive()) { faceNow.until = 0; toIdle(); return; }
  face(faceNow.name === 'love' ? 'love' : 'happy', 900); P.until = now() + 700; P.next = () => { toIdle(); if (Math.random() < .5) say(pick(['还要摸吗？', '嘿嘿～', '头发乱了…不过没关系'])); };
}
// ---- 拎起来 / 甩 / 扔 ----
function startDrag(e) {
  if (pomo) pomoStop(false);
  ptr.mode = 'drag'; P.mode = 'drag'; P.onLedge = false; P.dragCount++; P.shake = 0; P.dizzy = false; petEl.classList.add('dragging');
  P.rot = 0; P.rotV = 0; P.sx = P.sy = 1; P.patting = false;
  say(dlActive() ? '别闹，在下载呢！' : (P.dragCount % 3 === 1 ? 'drag1' : 'drag2')); setMood(-1);
  const r = charRect(); P.grabDy = clamp(e.clientY - r.top, 10, r.h * .4);
  P.x = e.clientX; P.y = e.clientY - P.grabDy + r.h + box.h * .025;
}
function dragMove(e, dx) {
  const r = charRect(); P.x = e.clientX; P.y = e.clientY - P.grabDy + r.h + box.h * .025; P.rotV += -dx * .9;
  P.shake = P.shake * .92 + Math.abs(dx);
  if (!P.dizzy && (P.shake > 280 || P.dragCount >= 4 && P.shake > 140)) { P.dizzy = true; say('dizzy'); particle('💫', 2); M.stats.dizzy++; if (M.stats.dizzy >= 5) unlock('dizzy5', '甩甩乐', '把她甩晕 5 次'); }
}
function release(p) {
  petEl.classList.remove('dragging');
  const h = p.hist; let vx = 0, vy = 0; if (h.length >= 2) { const a = h[0], b = h[h.length - 1], dt = Math.max(16, b.t - a.t) / 1000; vx = (b.x - a.x) / dt; vy = (b.y - a.y) / dt; }
  P.mode = 'air'; P.vx = clamp(vx, -1700, 1700); P.vy = clamp(vy, -1900, 1700); P.wasDizzy = P.dizzy; P.hard = Math.hypot(vx, vy) > 1300;
  if (vy < -1200) say('throw_high');
}

// ===================== 菜单 =====================
function openMenu(x, y) { menu.style.display = 'block'; const w = menu.offsetWidth, h = menu.offsetHeight; menu.style.left = clamp(x, 6, stage.clientWidth - w - 6) + 'px'; menu.style.top = clamp(y - h - 10, 6, stage.clientHeight - h - 50) + 'px'; }
function closeMenu() { menu.style.display = 'none'; }
menu.addEventListener('pointerup', e => {
  const a = e.target.dataset.a; if (!a) return; closeMenu(); touch();
  if (a === 'pat') { startPat(); setTimeout(() => particle('💗', 3), 50); setTimeout(() => endPat(), 1800); }
  if (a === 'feed') spawnBowl();
  if (a === 'water') drinkTogether();
  if (a === 'ledge') { if (L2D.chibiLevel() < .5) say('正常版太高啦，变成 Q 版再上窗台吧', true); else jumpToLedge(); }
  if (a === 'down' && P.onLedge) { P.onLedge = false; P.mode = 'air'; P.vy = -250; P.vx = rnd(-100, 100); P.dropThrough = true; }
  if (a === 'size') toggleSize();
  if (a === 'dress') dressMenu();
  if (a === 'book') openBook();
  if (a === 'quiet') toggleQuiet();
  if (a === 'pause') dlTogglePause();
  if (a === 'cmd') openCmd();
  if (a === 'sleep') { say('那我睡一会儿…晚安～', true); face('yawn', 1200); setTimeout(sleep, 1200); }
});
document.addEventListener('pointerdown', e => { if (!menu.contains(e.target)) closeMenu(); }, true);

// ===================== 道具：饭碗 / 水杯 / 链接 =====================
function makeItem(el, onTake, opts) {
  const it = { el, st: null };
  it.spawn = (x, y) => { el.style.display = 'block'; it.st = { x, y, t: now(), drag: null }; place(); lookAt(x + 20, y + 20, 1500); };
  function place() { el.style.left = it.st.x + 'px'; el.style.top = it.st.y + 'px'; }
  it.hide = () => { el.style.display = 'none'; it.st = null; };
  el.addEventListener('pointerdown', e => { if (!it.st) return; e.preventDefault(); e.stopPropagation(); el.setPointerCapture(e.pointerId); it.st.drag = { dx: e.clientX - it.st.x, dy: e.clientY - it.st.y }; touch(); });
  el.addEventListener('pointermove', e => { if (!it.st || !it.st.drag) return; it.st.x = e.clientX - it.st.drag.dx; it.st.y = e.clientY - it.st.drag.dy; place(); it.st.t = now(); lookAt(e.clientX, e.clientY, 600);
    if (region(e.clientX, e.clientY)) { it.st.drag = null; it.hide(); onTake(); } });
  el.addEventListener('pointerup', () => { if (it.st) { it.st.drag = null; if (opts && opts.floor) { it.st.y = floorY() - 46; } place(); } });
  return it;
}
const bowl = makeItem($('bowl'), () => eatNow(), { floor: true });
const cup = makeItem($('cup'), () => drinkNow(), { floor: true });
const link = makeItem($('link'), () => feedLink('https://www.bilibili.com/video/BV1xx'));
function spawnBowl() { if (bowl.st) return; const bx = clamp(P.x + (P.x > stage.clientWidth / 2 ? -150 : 150), 30, stage.clientWidth - 70); bowl.spawn(bx, floorY() - 46); face('surprised', 800); say(pick(['…是饭的味道！', '米饭！是米饭吗！', '闻到香味了！'])); }
let fedRecent = [];
function eatNow() {
  if (P.mode === 'sleep') { P.mode = 'idle'; say('…是饭的味道！'); }
  const t = now(); fedRecent = fedRecent.filter(x => t - x < T(600000)); fedRecent.push(t);
  if (fedRecent.length >= 4) { pose('sweat', 2200, toIdle); say('too_full'); particle('💦'); unlock('full', '撑撑的', '短时间内喂了她 4 次'); return; }
  P.mode = 'eat'; say('eat'); P.until = t + 3000; M.stats.fed++; day().fed++; aff(+2); save();
  P.next = () => { setMood(+15); energy = clamp(energy + 15, 0, 100); pose('happy', 2200, toIdle); say(fedRecent.length >= 3 ? 'too_full' : 'full'); particle('💙', 3); if (M.stats.fed >= 10) unlock('fed10', '投喂达人', '累计喂饭 10 次'); };
  const tick = setInterval(() => { if (P.mode !== 'eat') return clearInterval(tick); particle('🍚'); }, 450);
}
function drinkTogether() { if (cup.st) return; cup.spawn(clamp(P.x + (P.x > stage.clientWidth / 2 ? -130 : 130), 30, stage.clientWidth - 60), floorY() - 46); say(pick(['一起喝水！把杯子给我～', '咕嘟咕嘟时间！'])); }
function drinkNow() { logWater(true); }
function logWater(withPet) {
  const d = day(); d.water++; M.stats.cups++; aff(+1); save();
  pose('happy', 2000, toIdle); particle('💧', 3);
  const msg = d.water === 1 ? '今天第一杯水！好的开始～' : d.water === 4 ? '已经 4 杯啦，过半了！' : d.water === 8 ? '8 杯达成！你是喝水冠军！' : d.water > 8 ? '喝了 ' + d.water + ' 杯，也别喝太多哦～' : '第 ' + d.water + ' 杯！' + pick(LINES.drink_with);
  say(msg, true); if (d.water === 8) { unlock('water8', '喝水冠军', '一天喝满 8 杯水'); particle('🏆'); }
  waterNext = now() + T(45 * 60000);
}

// ===================== 状态机（每帧） =====================
let last = now();
function update(dt) {
  const t = now(), W = stage.clientWidth, F = floorY();
  P.sx += (1 - P.sx) * Math.min(1, dt * 10); P.sy += (1 - P.sy) * Math.min(1, dt * 10);
  if (P.wobble > 0) P.wobble = Math.max(0, P.wobble - dt * .8);
  if (dance.on && t > dance.until) dance.on = false;
  energy = clamp(energy - dt * (fast ? .08 : .008) + (P.mode === 'sleep' ? dt * (fast ? .6 : .06) : 0), 0, 100);
  if (P.mode === 'drag') { P.rotV += (-P.rot * 60 - P.rotV * 7) * dt; P.rot += P.rotV * dt; P.rot = clamp(P.rot, -28, 28); P.shake *= Math.pow(.2, dt); return; }
  P.rot += (0 - P.rot) * Math.min(1, dt * 8);
  const hw = box.w * .28;
  // 任何状态下只要脚下悬空就受重力（防止在空中被别的动作“冻住”）
  if (P.mode !== 'air' && !P.air && !P.onLedge && P.y < F - 2) { P.air = true; P.vy = Math.max(P.vy, 0); }
  if (P.onLedge && P.mode !== 'air') { const L0 = ledge(); if (P.x < L0.l - 30 || P.x > L0.r + 30) { P.onLedge = false; P.air = true; P.vy = 0; } }
  if (P.mode === 'air' || P.air) {
    P.vy += 2200 * dt; P.x += P.vx * dt; P.y += P.vy * dt;
    if (P.x < hw) { P.x = hw; P.vx = -P.vx * .45; squash(.85, 1.1); if (Math.abs(P.vx) > 300) { say(pick(['咚！', '撞墙了…'])); particle('💫'); } }
    if (P.x > W - hw) { P.x = W - hw; P.vx = -P.vx * .45; squash(.85, 1.1); if (Math.abs(P.vx) > 300) { say(pick(['咚！', '撞墙了…'])); particle('💫'); } }
    if (P.y - box.h * .9 < 0 && P.vy < 0) P.vy = -P.vy * .3;
    const L = ledge();
    if (P.dropThrough && P.y > L.y + 8) P.dropThrough = false;
    if (!P.dropThrough && P.vy > 0 && P.x > L.l && P.x < L.r && P.y >= L.y && P.y - P.vy * dt <= L.y + 2) {
      P.y = L.y; P.vx = 0; P.vy = 0; P.onLedge = true; squash(1.1, .86);
      if (P.air) { P.air = false; return; }
      if (P.wasDizzy) { P.wasDizzy = false; pose('dizzy', 1400, () => { say('land_hard'); toIdle(); }); }
      else { P.mode = 'sit'; if (Math.random() < .5) say(pick(['这里视野最好～', '窗台是我的！', '站得高看得远！'])); }
      return;
    }
    if (P.y >= F) {
      P.y = F;
      if (P.air) { P.air = false; P.vy = 0; squash(1.08, .9); return; }
      const hard = P.hard || P.vy > 1500; P.vy = 0; P.vx = 0; const dz = P.wasDizzy; P.wasDizzy = false; P.hard = false;
      P.mode = 'land'; squash(1.14, .82); P.until = t + (dz ? 260 : 200);
      P.next = dz ? () => { pose('dizzy', 1800, () => { pose('relief', 1300, toIdle); say('呼…终于停了'); }); P.wobble = 1; say('dizzy'); }
        : hard ? () => { pose('cry', 1600, toIdle); say('land_hard'); aff(-1); particle('💧'); } : () => { toIdle(); if (Math.random() < .3) say('land_soft'); };
    }
    return;
  }
  if (P.onLedge && P.mode !== 'air') { const L = ledge(); P.y = L.y; P.x = clamp(P.x, L.l, L.r); }
  if ((P.mode === 'react' || P.mode === 'land' || P.mode === 'prep' || P.mode === 'eat') && t >= P.until) { const n = P.next; P.next = null; if (n) n(); else toIdle(); }
  if (P.mode === 'walk') {
    if (quiet) { toIdle(); return; }
    const d = P.walkTarget - P.x, vmax = 70, acc = 160;
    const want = Math.sign(d) * Math.min(vmax, Math.sqrt(2 * acc * Math.abs(d)));
    P.walkV += clamp(want - P.walkV, -acc * dt, acc * dt);
    P.x += P.walkV * dt; P.walkPhase += Math.abs(P.walkV) * dt / 46;
    if (Math.abs(d) < 2 && Math.abs(P.walkV) < 8) { P.walkV = 0; toIdle(); }
    if (bowl.st && !bowl.st.drag && Math.abs(bowl.st.x + 20 - P.x) < 22) { bowl.hide(); eatNow(); }
    if (cup.st && !cup.st.drag && Math.abs(cup.st.x + 18 - P.x) < 22) { cup.hide(); drinkNow(); }
  } else P.walkV = 0;
  if (P.mode === 'sleep' || P.mode === 'paused') { P.zT = (P.zT || 0) + dt; if (P.zT > 1.5) { P.zT = 0; particle('💤'); } }
  // 道具放着不管 → 自己走过去
  for (const it of [bowl, cup]) if (it.st && !it.st.drag && (P.mode === 'idle' || P.mode === 'sit') && t - it.st.t > T(30000) + 2500) {
    if (P.onLedge) { P.onLedge = false; P.mode = 'air'; P.vy = -200; P.vx = 0; P.dropThrough = true; } else walkTo(it.st.x + 20); break; }
  dlUpdate(dt, t);
  if (pomo) pomoUpdate(t);
  // 久不理：叫你 → 委屈 → 无聊玩狗尾草 → 打瞌睡
  const idleFor = t - lastAct;
  if (!quiet && !pomo && !dlActive() && (P.mode === 'idle' || P.mode === 'sit') && !chipsOpen) {
    const k = fast ? .1 : 1;
    if (neglect === 0 && idleFor > 180000 * k) { neglect = 1; face('finger', 2000); say('call'); lookAt(stage.clientWidth / 2, stage.clientHeight * .4, 2000); }
    else if (neglect === 1 && idleFor > 360000 * k) { neglect = 2; pose('sad', 2400, toIdle); say('lonely'); setMood(-3); }
    else if (neglect === 2 && idleFor > 480000 * k) { neglect = 3; pose('grass', 5000, toIdle); say('bored'); }
    else if (neglect === 3 && idleFor > 600000 * k) { neglect = 4; pose('yawn', 1600, () => sleep()); say('yawn'); }
  }
  if (t > routineAt) { routineAt = t + (fast ? rnd(3500, 6000) : rnd(9000, 18000)); if (neglect < 4) routine(); }
  life(t);
}

// ===================== 日常小动作（像真人一样的“闲不住”） =====================
function routine() {
  if (quiet || pomo || dlActive() || chipsOpen || (P.mode !== 'idle' && P.mode !== 'sit')) return;
  const h = new Date().getHours(), night = h >= 23 || h < 6;
  const W = [
    [P.onLedge ? 0 : 22, () => walkTo(P.x + rnd(-280, 280))],
    [0, () => jumpToLedge()],
    [P.onLedge ? 16 : 0, () => { P.onLedge = false; P.mode = 'air'; P.vy = -300; P.vx = rnd(-120, 120); P.dropThrough = true; }],
    [10, () => { const tb = { x: stage.clientWidth - 40, y: stage.clientHeight - 20 }; lookAt(tb.x, tb.y, 1400); setTimeout(() => { if (Math.random() < .5) say('现在 {time} 了'); }, 900); }],
    [10, () => { const r = win.getBoundingClientRect(); lookAt(r.left + r.width / 2, r.top + 60, 2200); if (Math.random() < .4) { ACC.add('key11'); setTimeout(() => ACC.delete('key11'), 3200); say(pick(['让我看看你在写什么…', '嗯嗯…', '这个待办好像还没做完？'])); } }],
    [8, () => { pose('smile', 1600, toIdle); lookAt(P.x, P.y - box.h * .4 + 30, 1600); say(pick(['嘿嘿', '（看着你）', '你今天也很努力呢'])); }],
    [6, () => { pose('smile', 3200, toIdle); say('hum'); const n = setInterval(() => particle('🎵'), 700); setTimeout(() => clearInterval(n), 3000); }],
    [energy < 50 || night ? 10 : 3, () => { pose('yawn', 1700, toIdle); say('yawn'); particle('💧'); }],
    [5, () => { pose('relief', 1500, () => pose('happy', 900, toIdle)); say('stretch'); squash(.92, 1.1); }],
    [mood > 50 ? 6 : 2, () => { pose('grass', 4200, toIdle); say('grass'); }],
    [5, () => { pose('think', 2600, () => { if (Math.random() < .5) { face('finger', 1400); particle('💡'); } toIdle(); }); say('think'); }],
    [7, () => { pose('smile', 2200, toIdle); say(night ? 'chat_night' : Math.random() < .3 ? 'chat_code' : 'chat'); }],
    [3, () => { pose('finger', 2600, toIdle); say('whale_fact'); M.stats.facts++; if (M.stats.facts >= 5) unlock('scholar', '鲸鱼学者', '听她讲了 5 个鲸鱼冷知识'); }],
    [mood > 70 ? 4 : 1, () => { doDance(3600); say(pick(['跳个舞给你看！', '♪ 扭一扭～'])); }],
    [1.5, () => { pose('xx', 900, toIdle); squash(1.1, .88); say('sneeze'); }],
    [2, () => { pose('wink', 1400, toIdle); say(pick(['嘿嘿～', '（眨眼）', '偷偷看你一眼'])); }],
    [M.aff > 60 ? 3 : 0, () => { pose('love', 1800, toIdle); say(pick(['最喜欢{n}了！', '有你在真好～'])); particle('💗', 2); }],
  ];
  let sum = W.reduce((a, w) => a + w[0], 0), r = Math.random() * sum;
  for (const [w, f] of W) { if ((r -= w) <= 0) { f(); return; } }
}

// ===================== 关心系统 Life =====================
// 真实时钟驱动：早安/睡眠 → 三餐（问吃了没、吃的什么）→ 喝水 → 久坐 → 护眼 → 午休/下午茶 → 心情 → 运动 → 夜里催睡
let lifeAt = 0, lastAsk = 0, waterNext = 0, standNext = 0, eyeNext = 0, sessionStart = Date.now(), mealRetry = {};
function canAsk(t) { return !quiet && !chipsOpen && !pomo && P.mode !== 'drag' && P.mode !== 'air' && P.mode !== 'eat' && t - lastAsk > (fast ? 9000 : 150000) && t - lastAct < (fast ? 60000 : 600000); }
function asked(k) { const d = day(); if (d.asked[k]) return true; d.asked[k] = Date.now(); save(); return false; }
function life(t) {
  if (t < lifeAt || !M.nick) return; lifeAt = t + 1000;
  const d = day(), hh = new Date().getHours() + new Date().getMinutes() / 60, wd = new Date().getDay();
  // 夜里自动脱鞋/早上穿上，季节长短裙
  if (hh >= 22 || hh < 6.5) { if (P.mode === 'sleep') ACC.add('Clothes4'); } else if (P.mode !== 'sleep') ACC.delete('Clothes4');
  if (!canAsk(t)) return;
  const go = () => { lastAsk = t; return true; };
  // 1) 新的一天 → 睡眠
  if (hh >= 5 && hh < 14 && !d.asked.sleep && go()) { asked('sleep'); askSleep(); return; }
  // 2) 三餐
  if (M.remind.meal) {
    const meals = [['b', '早饭', 6, 10], ['l', '午饭', 11, 14], ['d', '晚饭', 17, 20.5]];
    for (const [k, nm, a, b] of meals) {
      if (hh >= a && hh < b && !d.meals[k] && (!mealRetry[k] || t > mealRetry[k]) && !d.asked['meal_' + k + '_no'] && go()) { mealRetry[k] = t + T(30 * 60000) + 15000; askMeal(k, nm); return; }
      if (hh >= b && hh < b + 1.5 && d.meals[k] === '' && !d.asked['late_' + k] && go()) { asked('late_' + k); pose('sad', 2000, toIdle); say('remind_meal_late'); return; }
    }
  }
  // 3) 喝水
  if (M.remind.water) { if (!waterNext) waterNext = t + T(40 * 60000) + 20000; if (t > waterNext && hh >= 7 && hh < 23.5 && go()) { waterNext = t + T(45 * 60000) + 20000; askWater(); return; } }
  // 4) 久坐 / 5) 护眼
  if (M.remind.stand) { if (!standNext) standNext = t + T(50 * 60000) + 30000; if (t > standNext && go()) { standNext = t + T(50 * 60000) + 30000; askStand(); return; } }
  if (M.remind.eye) { if (!eyeNext) eyeNext = t + T(25 * 60000) + 40000; if (t > eyeNext && go()) { eyeNext = t + T(30 * 60000) + 40000; eyeBreak(); return; } }
  // 6) 午休 / 下午茶
  if (hh >= 13 && hh < 14.5 && !d.asked.nap && go()) { asked('nap'); face(d.sleep === 'bad' ? 'sad' : 'yawn', 1600); say(d.sleep === 'bad' ? '早上你说没睡好…中午眯一会儿吧？' : 'remind_nap'); return; }
  if (hh >= 15 && hh < 16.5 && !d.asked.fruit && go()) { asked('fruit'); face('finger', 1600); say('remind_fruit'); return; }
  // 7) 心情 / 8) 运动
  if (hh >= 16 && hh < 22 && !d.asked.mood && go()) { asked('mood'); askMood(); return; }
  if (hh >= 19 && hh < 22.5 && !d.asked.sport && go()) { asked('sport'); askSport(); return; }
  // 9) 节日 / 周末
  const fest = festival(); if (fest && !d.asked.fest && go()) { asked('fest'); fest(); return; }
  if (wd === 5 && hh >= 17 && !d.asked.weekend && go()) { asked('weekend'); doDance(3000); say('周五啦！周末快乐～有什么计划吗？', true); return; }
  if (wd === 1 && hh < 12 && !d.asked.monday && go()) { asked('monday'); pose('finger', 2000, toIdle); say('周一加油！这周也一起努力吧', true); return; }
  // 10) 夜里催睡（递进）
  if (M.remind.sleep) {
    if (hh >= 23 && hh < 24 && !d.asked.sleep1 && go()) { asked('sleep1'); pose('finger', 2200, toIdle); say('remind_sleep1'); setTimeout(() => say('remind_brush'), 3200); return; }
    if ((hh >= 0 && hh < 1) && !d.asked.sleep2 && go()) { asked('sleep2'); pose('pout', 2200, toIdle); say('remind_sleep2'); return; }
    if (hh >= 1 && hh < 5 && (!d.asked.sleep3 || Date.now() - d.asked.sleep3 > (fast ? 60000 : 1800000)) && go()) { d.asked.sleep3 = Date.now(); save(); pose('angry', 2400, toIdle); say('remind_sleep3'); if (hh >= 2) unlock('owl', '夜猫子', '凌晨两点后还在陪她'); return; }
  }
}
function askSleep() {
  const y = M.days[yesterday()];
  const pre = y && y.sleep === 'bad' ? '昨天你说没睡好，' : '';
  ask(pre + (new Date().getHours() < 11 ? '早安！' : '') + '昨晚睡得好吗？', [
    ['很好！', () => { day().sleep = 'good'; save(); pose('happy', 1800, toIdle); say(pick(['太好了！精神满满的一天～', '睡得好心情就好！'])); aff(+2); goodSleepAch(); }],
    ['还行', () => { day().sleep = 'ok'; save(); pose('smile', 1500, toIdle); say('还行就好～今晚争取更好！'); aff(+1); }],
    ['没睡好', () => { day().sleep = 'bad'; save(); pose('sad', 2200, toIdle); say(pick(['抱抱…今天别太累了，中午记得眯一会儿', '那今晚我早点催你睡！'])); aff(+2); }],
    ['熬夜了', () => { day().sleep = 'late'; save(); pose('pout', 2200, toIdle); say(pick(['又熬夜！今晚 11 点我来抓你睡觉', '熬夜伤身体…今天多喝水'])); aff(+1); }],
  ], 'smile');
}
function goodSleepAch() { const ks = Object.keys(M.days).filter(k => M.days[k].sleep === 'good'); if (ks.length >= 3) unlock('sleepgood', '早睡宝宝', '有 3 天告诉她睡得很好'); }
const MEAL_OPTS = { b: ['包子', '面包', '粥', '鸡蛋', '米饭', '其他'], l: ['米饭', '面', '外卖', '食堂', '轻食', '零食', '其他'], d: ['米饭', '面', '火锅', '外卖', '饺子', '轻食', '其他'] };
function askMeal(k, nm) {
  const y = M.days[yesterday()]; const yf = y && y.meals && y.meals[k];
  ask(pick([nm + '吃了吗？', '{n}，' + nm + '时间到啦！吃了没？', '肚子饿不饿？' + nm + '吃了吗']), [
    ['吃了', () => askFood(k, nm, yf)],
    ['还没', () => { pose('finger', 2000, toIdle); say(pick(['那快去吃！我过会儿再来问你', '不许饿肚子！吃完回来告诉我吃了什么'])); }],
    ['不想吃', () => { day().asked['meal_' + k + '_no'] = 1; day().meals[k] = ''; save(); pose('sad', 2200, toIdle); say(pick(['不吃饭会没力气的…至少喝杯牛奶？', '是不舒服吗？那喝点热水，饿了一定要吃'])); }],
  ], 'smile');
}
function askFood(k, nm, yf) {
  ask(pick(['吃的什么呀？', '好耶！吃了什么好吃的？']), MEAL_OPTS[k].map(f => [f, () => {
    if (f === '其他') { openCmd('我吃了…'); P.awaitFood = k; return; }
    logMeal(k, f, yf);
  }]), 'happy');
}
function logMeal(k, f, yf) {
  day().meals[k] = f; save(); aff(+2);
  const key = Object.keys(FOOD_REACT).find(x => f.includes(x)) || '其他';
  if (key === '米饭') { doDance(2200); particle('🍚', 3); } else if (key === '零食') pose('pout', 1800, toIdle); else pose('happy', 1800, toIdle);
  let s = pick(FOOD_REACT[key]);
  if (yf && yf === f && key !== '米饭') s += '（昨天也吃的' + f + '，明天换换口味？）';
  say(s, true);
}
function askWater() {
  ask(line('remind_water'), [
    ['喝了！', () => logWater()],
    ['等会儿', () => { waterNext = now() + T(10 * 60000) + 15000; pose('pout', 1200, toIdle); say(pick(['说好的哦，我 10 分钟后再来！', '那我待会儿来查岗'])); }],
    ['不渴', () => { waterNext = now() + T(30 * 60000) + 20000; face('finger', 1500); say('不渴也要小口喝一点，身体缺水时是不会马上觉得渴的'); }],
  ], 'finger');
}
function askStand() {
  ask(line('remind_stand'), [
    ['站起来了！', () => { pose('joy', 1800, toIdle); say(pick(['好棒！顺便扭扭脖子转转手腕～', '伸——懒——腰！'])); squash(.9, 1.12); aff(+1); }],
    ['等一下', () => { standNext = now() + T(15 * 60000) + 15000; pose('pout', 1200, toIdle); say('那 15 分钟后一定要起来哦'); }],
  ], 'finger');
}
function eyeBreak() {
  if (Math.random() < .35) return;
  pose('finger', 1500, toIdle); say('remind_eye'); let n = 20;
  setTimeout(() => { const iv = setInterval(() => { if (chipsOpen || P.mode === 'drag') { clearInterval(iv); return; } n -= fast ? 4 : 1; if (n <= 0) { clearInterval(iv); pose('happy', 1200, toIdle); say('好啦！眼睛休息好了～', true); return; } if (!quiet) { bub.textContent = '看远处… ' + n; bub.classList.add('show'); clearTimeout(say.t); say.t = setTimeout(() => bub.classList.remove('show'), 1500); } }, 1000); }, 2600);
}
function askMood() {
  ask(pick(['今天心情怎么样？', '{n}，今天过得开心吗？']), [
    ['开心', () => { day().mood = 'happy'; save(); doDance(2600); say('happy'); aff(+2); }],
    ['一般', () => { day().mood = 'ok'; save(); pose('smile', 1600, toIdle); say(pick(['平平淡淡也很好～', '那我给你讲个笑话？'])); aff(+1); }],
    ['有点累', () => { day().mood = 'tired'; save(); pose('sad', 2000, () => { pose('love', 1600, toIdle); }); say('tired'); aff(+2); }],
    ['难过', () => { day().mood = 'sad'; save(); pose('sad', 2200, toIdle); say('sad'); aff(+2);
      setTimeout(() => chips([['讲个笑话', () => { pose('wink', 2000, toIdle); say('joke', true); }], ['陪我安静待会', () => { if (!quiet) toggleQuiet(); }], ['听我说', () => openCmd('')]]), 2600); }],
  ], 'smile');
}
function askSport() {
  ask('今天运动了吗？', [
    ['运动了', () => { day().sport = 'yes'; save(); pose('joy', 1800, toIdle); say(pick(['好厉害！记得拉伸哦', '运动完要补水！'])); aff(+2); }],
    ['没有', () => { day().sport = 'no'; save(); pose('smile', 1600, toIdle); say(pick(['那睡前做几个深蹲？我数数！', '散步十分钟也算哦～'])); }],
    ['明天一定', () => { day().sport = 'tmr'; save(); pose('smug', 1600, toIdle); say('我记下了！明天来查岗'); }],
  ], 'smile');
}
function festival() {
  const d = new Date(), md = String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
  if (md === (M.birthday || '08-15')) return () => { doDance(5000); say('{n}生日快乐！！🎂 今天你最大！', true); particle('🎂', 2); particle('🎉', 4); unlock('bday', '生日快乐', '和她一起过了生日'); };
  const F = { '01-01': '新年快乐！新的一年也请多指教～', '02-14': '情人节快乐！…才不是特意跟你说的！', '04-01': '我要离家出走了！……骗你的，愚人节快乐～', '05-01': '劳动节快乐！今天就别劳动啦', '06-01': '儿童节快乐！今天你可以当小朋友', '10-01': '国庆快乐！出去玩记得带上我（的祝福）', '10-31': 'Trick or treat！不给米饭就捣蛋！', '12-24': '平安夜快乐～吃苹果了吗？', '12-25': '圣诞快乐！我想要的礼物是…一碗米饭！' };
  if (F[md]) return () => { pose(md === '04-01' ? 'smug' : 'joy', 2600, toIdle); say(F[md], true); particle('🎉', 3); };
  return null;
}
// 新的一天打招呼 + 引用昨天的记忆
function greet() {
  const h = new Date().getHours(), dd = day();
  const gap = M.lastSeen ? Date.now() - M.lastSeen : 0;
  let k = h >= 23 || h < 5 ? 'hello_late' : h < 11 ? 'hello_morning' : h < 14 ? 'hello_noon' : h < 18 ? 'hello_afternoon' : 'hello_evening';
  if (gap > 3 * 864e5) k = 'back_long';
  pose(k === 'hello_late' ? 'pout' : k === 'back_long' ? 'cry' : 'happy', 2200, toIdle); say(k, true);
  // 引用昨天
  const y = M.days[yesterday()];
  if (y && k !== 'back_long') {
    const notes = [];
    if (y.water && y.water < 4) notes.push('昨天只喝了 ' + y.water + ' 杯水，今天要多喝点哦');
    if (y.sleep === 'late') notes.push('昨天熬夜了，今天早点睡好不好？');
    if (y.mood === 'sad') notes.push('昨天你有点难过…今天好些了吗？');
    if (y.sport === 'tmr') notes.push('昨天你说今天一定运动！我记着呢');
    if (y.meals && y.meals.d) notes.push('昨晚吃的是' + y.meals.d + '，好吃吗？');
    if (notes.length) setTimeout(() => { if (!chipsOpen) { face('finger', 2000); say(pick(notes), true); } }, 3600);
  }
  M.lastSeen = Date.now(); save();
}
function firstMeet() {
  pose('joy', 2500, toIdle); say(LINES.first_meet[0], true);
  setTimeout(() => { say(LINES.first_meet[1], true); }, 3400);
  setTimeout(() => ask('对了，我该怎么称呼你呀？', [['主人', () => setNick('主人')], ['哥哥', () => setNick('哥哥')], ['姐姐', () => setNick('姐姐')], ['同学', () => setNick('同学')], ['叫我名字', () => { P.awaitNick = true; openCmd('我叫…'); }]], 'smile', 60000), 7200);
}
function setNick(n) { M.nick = n; save(); pose('love', 2000, toIdle); say('好的，' + n + '！以后请多多关照～', true); unlock('meet', '初次见面', '和大肥鱼成为了朋友'); lifeAt = now() + 8000; lastAsk = now(); }

// ---- 成就 ----
const ACH = { meet: '初次见面', iron: '铁头娃', patsleep: '摸睡了', pat100: '摸头上瘾', full: '撑撑的', fed10: '投喂达人', water8: '喝水冠军', sleepgood: '早睡宝宝', owl: '夜猫子', pomo5: '专注大师', dl10: '下载狂人', dizzy5: '甩甩乐', bday: '生日快乐', scholar: '鲸鱼学者', rps: '猜拳赢家', konami: '秘籍玩家', chat50: '话痨' };
function unlock(id, nm, desc) { if (M.ach[id]) return; M.ach[id] = Date.now(); save(); toast('<b>🏅 解锁成就：' + nm + '</b>' + desc, null, 6000); particle('🏅'); }

// ===================== 下载助手 =====================
const DL = { tasks: [], done: 0, bytes: 0, failDemo: false, paused: false, failStreak: 0, slowUntil: 0, slowSaid: false, nextSlow: 0, speedShown: 0, audioOnly: false };
const FAIL = { net: '网络断了…要我再试试吗？', dead: '链接失效了，换一个吧…', rate: '被限速了…等一会儿再来', merge: '合并失败了，呜…', disk: '磁盘满啦，清理一下再继续？' };
const VNAMES = ['猫猫合集', '鲸鱼纪录片', '编程教程第3集', '演唱会现场', '旅行Vlog', '番剧OP', '游戏实况', '白米饭的一百种吃法'];
function dlActive() { return DL.tasks.some(t => t.state === 'run' || t.state === 'merge'); }
function fmtSize(b) { return b >= 1024 ? (b / 1024).toFixed(1) + ' GB' : Math.round(b) + ' MB'; }
function fmtEta(s) { if (!isFinite(s) || s < 0) return '--'; s = Math.round(s); return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'); }
function dsay(k) { if (!quiet) say(k, true); }
function feedLink(url) {
  touch(); if (P.mode === 'sleep' || (P.mode === 'paused' && !dlActive())) P.mode = 'idle';
  P.mode = 'react'; face('eat', 900); dsay('嗷呜！链接收到～'); particle('🔗'); P.until = now() + 900;
  P.next = () => { P.mode = 'sniff'; ACC.add('key12'); ACC.add('key8'); dsay('嗅嗅…在找视频'); P.sniffEnd = now() + 1900; P.sniffUrl = url; };
}
function sniffDone(url) {
  ACC.delete('key12');
  const none = /none|404/.test(url) || Math.random() < .08;
  if (none) { ACC.delete('key8'); pose('sad', 2000, toIdle); say('这页没嗅到视频…换个链接试试？', true); setMood(-2); return; }
  const n = 1 + (Math.random() * 3 | 0);
  pose('joy', 1300, () => startTasks(n)); dsay('找到 ' + n + ' 个视频！开工～'); particle('✨', 3); squash(.94, 1.06);
}
function startTasks(n) {
  for (let i = 0; i < n; i++) { const size = rnd(60, 420); DL.tasks.push({ name: pick(VNAMES) + (DL.audioOnly ? '.m4a' : '.mp4'), size: DL.audioOnly ? size * .12 : size, got: 0, state: 'run', speed: 0, failAt: DL.failDemo && (i === 0 || Math.random() < .4) ? rnd(.2, .85) : 2 }); }
  DL.paused = false; DL.nextSlow = now() + rnd(6000, 12000); ACC.add('key8'); toIdle(); updBadge();
}
function updBadge() { const n = DL.tasks.filter(t => t.state === 'run' || t.state === 'merge').length; const b = $('badge'); b.style.display = n > 1 ? 'block' : 'none'; b.textContent = n; if (!n) ACC.delete('key8'); }
function dlTap() {
  if (DL.paused) { dlTogglePause(); return; }
  const r = DL.tasks.filter(t => t.state === 'run' || t.state === 'merge'); squash(.95, 1.05);
  const cur = r.find(t => t.state === 'run') || r[0]; const cp = cur ? Math.floor(cur.got / cur.size * 100) : 0;
  face('finger', 1200); say(r.length > 1 ? '还有 ' + r.length + ' 个，这个到 ' + cp + '% 啦～' : (cp > 80 ? '马上就好！再等一下下' : '已经 ' + cp + '% 啦，别急～'), true);
}
function dlTogglePause() {
  if (!dlActive() && !DL.paused) { say('现在没有在下载哦', true); return; }
  DL.paused = !DL.paused;
  if (DL.paused) { pose('yawn', 1300, toIdle); say('那我先眯一会儿…点我继续', true); }
  else { pose('surprised', 700, toIdle); say('继续干活！', true); particle('❗'); }
}
function taskFail(t, reason) {
  t.state = 'fail'; t.reason = reason; DL.failStreak++; setMood(-4); updBadge();
  if (P.mode !== 'drag' && P.mode !== 'air') pose(reason === 'disk' ? 'sweat' : 'cry', 2200, toIdle); dsay(FAIL[reason]);
  const btn = reason === 'dead' ? [['换个链接', () => spawnLink()]] : [['重试', () => { t.state = 'run'; t.failAt = 2; DL.paused = false; ACC.add('key8'); say('再来一次！', true); toIdle(); updBadge(); }]];
  toast('<b>❌ ' + t.name + '</b>' + FAIL[reason], btn, 12000);
}
function taskDone(t) {
  t.state = 'done'; DL.done++; DL.bytes += t.size; DL.failStreak = 0; setMood(+5); updBadge(); M.stats.dl++; day().dl++; save();
  if (M.stats.dl >= 10) unlock('dl10', '下载狂人', '帮你下载了 10 个文件');
  toast('<b>✅ ' + t.name + '</b>' + fmtSize(t.size), [['打开文件夹', () => say('预览里打不开文件夹，接入 SniffDL 后会直接打开～', true)]]);
  const left = dlActive();
  if (P.mode !== 'drag' && P.mode !== 'air') { face('love', 1400); P.mode = 'air'; P.vy = -620; P.vx = 0; particle('✨', 4); }
  dsay(left ? '下好一个啦！' : '下载好啦！');
  if (!left) setTimeout(() => { if (!dlActive()) { pose('happy', 2600, toIdle); dsay('今天也帮你下了 ' + DL.done + ' 个～'); particle('💙', 3); setMood(+8); } }, 1500);
}
function dlUpdate(dt, t) {
  if (P.mode === 'sniff' && t > P.sniffEnd) { P.mode = 'react'; P.until = t; P.next = null; sniffDone(P.sniffUrl); return; }
  if (P.mode === 'sniff' && Math.random() < dt * 2.5) particle('🔍');
  const run = DL.tasks.filter(x => x.state === 'run' || x.state === 'merge');
  if (!run.length) { DL.tasks = DL.tasks.filter(x => x.state === 'fail'); return; }
  const slow = t < DL.slowUntil;
  if (!DL.paused && !slow && t > DL.nextSlow && !DL.slowUntil) { DL.slowUntil = t + rnd(3500, 5500); DL.slowSaid = false; }
  if (DL.slowUntil && t > DL.slowUntil) { DL.slowUntil = 0; DL.nextSlow = t + rnd(12000, 22000); if (DL.slowSaid) { dsay('速度回来啦！'); if (P.mode === 'react' && faceNow.name === 'angry') P.until = t; } }
  if (slow && !DL.slowSaid && !DL.paused && P.mode === 'work') { DL.slowSaid = true; pose('angry', 1800, toIdle); dsay('网络好慢…快点快点！'); particle('💢'); }
  let tot = 0, got = 0, spd = 0;
  for (const x of run) {
    if (x.state === 'merge') { x.mergeT -= dt; if (x.mergeT <= 0) { if (x.failAt === -1) taskFail(x, 'merge'); else taskDone(x); } tot += x.size; got += x.size; continue; }
    if (!DL.paused) { x.speed += ((slow ? rnd(.05, .3) : rnd(2, 9)) - x.speed) * Math.min(1, dt * 1.5); x.got = Math.min(x.size, x.got + x.speed / run.length * (fast ? 3 : 1) * dt); } else x.speed *= .9;
    spd += DL.paused ? 0 : x.speed / run.length * (fast ? 3 : 1);
    if (x.got / x.size >= x.failAt) { const r = pick(Object.keys(FAIL)); if (r === 'merge') { x.got = x.size; x.failAt = -1; } else { taskFail(x, r); continue; } }
    if (x.got >= x.size) { x.state = 'merge'; x.mergeT = 1.4; if (x.failAt !== -1) x.failAt = 2; if (P.mode === 'work') { pose('joy', 1200, toIdle); dsay('合并中…快好了快好了'); } }
    tot += x.size; got += x.got;
  }
  const pc = tot ? got / tot : 0; $('dringc').setAttribute('stroke-dashoffset', 113.1 * (1 - pc)); $('dringc').setAttribute('stroke', DL.paused ? '#adb5bd' : slow ? '#f59f00' : '#3b5bdb');
  $('dringt').textContent = DL.paused ? '⏸' : Math.floor(pc * 100) + '%';
  if (t - DL.speedShown > 400) { DL.speedShown = t; $('dspeed').textContent = DL.paused ? '已暂停\n点她继续' : run.every(x => x.state === 'merge') ? '合并中…' : '↓ ' + spd.toFixed(1) + ' MB/s\n剩 ' + fmtEta((tot - got) / Math.max(.05, spd)); }
}
function spawnLink() { if (link.st) return; touch(); $('link').style.display = 'block'; const lw = $('link').offsetWidth || 190; link.spawn(P.x > stage.clientWidth / 2 ? 12 : stage.clientWidth - lw - 12, floorY() - box.h * .8); face('surprised', 900); dsay('那是…链接的味道！'); }

// ===================== 番茄钟 =====================
function pomoStart() { pomo = { end: now() + (fast ? 30000 : 25 * 60000), dur: fast ? 30000 : 25 * 60000 }; $('b-pomo').classList.add('on'); $('ring').style.display = 'block'; ACC.add('key11'); if (P.onLedge) { P.onLedge = false; P.dropThrough = true; } P.mode = 'pomo'; say(fast ? '戴上眼镜，认真模式！一起专注 30 秒' : '戴上眼镜，认真模式！一起专注 25 分钟', true); P.y = floorY(); }
function pomoStop(done) {
  if (!pomo) return; pomo = null; touch(); $('b-pomo').classList.remove('on'); $('ring').style.display = 'none'; ACC.delete('key11');
  if (done) { M.stats.pomo++; day().pomo++; save(); if (M.stats.pomo >= 5) unlock('pomo5', '专注大师', '完成 5 个番茄钟'); P.mode = 'air'; P.vy = -700; P.vx = 0; face('joy', 1600); say('完成啦！奖励自己喝口水、伸个懒腰吧', true); particle('✨', 5); setMood(+10); }
  else toIdle();
}
function pomoUpdate(t) {
  const left = Math.max(0, pomo.end - t); $('ringc').setAttribute('stroke-dashoffset', 119.4 * (1 - left / pomo.dur));
  const s = Math.ceil(left / 1000); $('ringt').textContent = s > 99 ? Math.ceil(s / 60) + 'm' : s + 's';
  const r = charRect(); $('ring').style.left = clamp(r.left - 30, 4, stage.clientWidth - 50) + 'px'; $('ring').style.top = Math.max(4, r.top - 8) + 'px';
  if (left <= 0) pomoStop(true);
}

// ===================== 指令 / 聊天 =====================
let cmdT = 0;
function closeCmd() { $('cmd').style.display = 'none'; clearTimeout(cmdT); }
function openCmd(prefill) {
  const c = $('cmd'), r = charRect(); c.style.display = 'block'; const w = c.offsetWidth;
  c.style.left = clamp(P.x - w / 2, 8, stage.clientWidth - w - 8) + 'px'; c.style.top = Math.max(8, r.top - c.offsetHeight - 60) + 'px';
  const i = $('cmdin'); i.value = ''; i.placeholder = prefill || '想跟她说什么？'; setTimeout(() => i.focus(), 50);
  clearTimeout(cmdT); cmdT = setTimeout(() => { if (!i.value) closeCmd(); }, 15000);
}
document.addEventListener('pointerdown', e => { if ($('cmd').style.display !== 'none' && !$('cmd').contains(e.target)) closeCmd(); }, true);
$('cmdin').addEventListener('keydown', e => { if (e.key === 'Escape') closeCmd(); });
$('cmdin').addEventListener('input', () => clearTimeout(cmdT));
$('cmdin').addEventListener('keydown', e => { if (e.key !== 'Enter') return; const s = e.target.value.trim(); closeCmd(); touch(); runCmd(s); });
function mealKeyNow() { const h = new Date().getHours(); return h < 10.5 ? 'b' : h < 16 ? 'l' : 'd'; }
function runCmd(s) {
  if (!s) return; M.stats.chats++; if (M.stats.chats >= 50) unlock('chat50', '话痨', '和她聊了 50 句');
  if (P.mode === 'sleep') wake(true);
  if (P.awaitNick) { P.awaitNick = false; setNick(s.replace(/^我叫|^叫我/, '').slice(0, 8) || '主人'); return; }
  if (P.awaitFood) { const k = P.awaitFood; P.awaitFood = null; logMeal(k, s.replace(/^我?吃了?/, '').slice(0, 10) || '好吃的'); return; }
  const has = re => re.test(s);
  // 下载
  if (has(/https?:|www\.|\.com|\.cn|bv1/i)) return feedLink(s);
  if (has(/暂停/)) { if (dlActive() && !DL.paused) dlTogglePause(); else say('现在没什么可暂停的～', true); return; }
  if (has(/^继续|继续下/)) { if (DL.paused) dlTogglePause(); else say('一直在努力呢！', true); return; }
  if (has(/取消|清空/)) { const n = DL.tasks.filter(t => t.state === 'run' || t.state === 'merge').length; DL.tasks = []; DL.paused = false; updBadge(); toIdle(); say(n ? '好，' + n + ' 个任务都取消了' : '本来就没有任务哦', true); return; }
  if (has(/只要音频|音频|mp3/i)) { DL.audioOnly = true; pose('smug', 1500, toIdle); say('好，之后只下音频～', true); return; }
  if (has(/要视频|恢复视频/)) { DL.audioOnly = false; say('好，恢复下视频～', true); return; }
  if (has(/文件夹/)) { say('预览里打不开文件夹，接入 SniffDL 后会直接打开～', true); return; }
  // 生活记录
  const fm = s.match(/(?:我)?(?:吃了|吃的是|在吃)(.{1,10})/);
  if (fm && !has(/吗|没/)) { logMeal(mealKeyNow(), fm[1].replace(/[。！!，,]$/, ''), null); return; }
  if (has(/喝(了|过)?水|喝了/)) { logWater(); return; }
  if (has(/没睡好|失眠|睡不着/)) { day().sleep = 'bad'; save(); pose('sad', 2000, toIdle); say('抱抱…今天别太勉强，晚上我早点催你睡', true); return; }
  if (has(/睡得(很|挺)?好|睡饱/)) { day().sleep = 'good'; save(); pose('happy', 1800, toIdle); say('太好啦！今天一定很有精神', true); goodSleepAch(); return; }
  if (has(/运动|跑步|健身/)) { day().sport = 'yes'; save(); pose('joy', 1800, toIdle); say('好厉害！记得拉伸和补水～', true); return; }
  if (has(/饿/)) { pose('finger', 1800, toIdle); say(pick(['饿了就快去吃饭！要不要我帮你决定吃什么？', '我也饿了…一起吃饭吧！']), true); spawnBowl(); return; }
  if (has(/吃什么/)) { const f = pick(['米饭', '米饭', '面条', '饺子', '盖浇饭', '麻辣烫', '黄焖鸡米饭', '寿司', '炒饭']); doDance(1600); say('我帮你抽签：' + f + '！' + (f.includes('饭') ? '（米饭万岁）' : ''), true); return; }
  // 情绪
  if (has(/累|困|疲/)) { pose('sad', 1600, () => pose('love', 1600, toIdle)); say('tired', true); day().mood = day().mood || 'tired'; save(); return; }
  if (has(/难过|伤心|不开心|哭|烦|emo/)) { pose('sad', 2400, toIdle); say('sad', true); day().mood = 'sad'; save(); return; }
  if (has(/开心|高兴|快乐|好耶/)) { doDance(2600); say('happy', true); day().mood = 'happy'; save(); return; }
  if (has(/爱你|喜欢你|最喜欢/)) { pose('love', 2600, toIdle); say('love', true); particle('💗', 3); aff(+3); return; }
  if (has(/笨蛋|傻|蠢/)) { pose('smug', 2000, toIdle); say('baka', true); return; }
  if (has(/谢谢|感谢|辛苦/)) { pose('happy', 1600, toIdle); say('thanks', true); aff(+1); return; }
  if (has(/抱抱|抱一下/)) { pose('love', 2400, toIdle); say(pick(['抱抱！（张开小短手）', '来，抱一个～']), true); particle('💗', 3); return; }
  if (has(/晚安|睡觉了|去睡了/)) { pose('yawn', 1300, () => { sleep(); }); say(pick(['晚安{n}！明天见～', '晚安，做个好梦！我也要睡啦']), true); return; }
  if (has(/早安|早上好|早$/)) { pose('happy', 1600, toIdle); say('hello_morning', true); return; }
  // 玩
  if (has(/笑话|段子/)) { pose('wink', 2600, toIdle); say('joke', true); return; }
  if (has(/冷知识|鲸鱼|科普/)) { pose('finger', 2400, toIdle); say('whale_fact', true); M.stats.facts++; return; }
  if (has(/唱歌|唱首/)) { pose('smile', 3000, toIdle); say('hum', true); const n = setInterval(() => particle('🎵'), 600); setTimeout(() => clearInterval(n), 3000); return; }
  if (has(/跳舞|跳个/)) { doDance(4000); say('看我的！', true); return; }
  if (has(/猜拳|石头|剪刀|布/)) { rps(); return; }
  if (has(/硬币/)) { const r = Math.random() < .5 ? '正面' : '反面'; face('surprised', 800); say('叮——是' + r + '！', true); return; }
  if (has(/你是谁|介绍/)) { pose('happy', 2000, toIdle); say('who', true); return; }
  if (has(/几点|时间/)) { lookAt(stage.clientWidth - 40, stage.clientHeight - 20, 1200); say('time', true); return; }
  // 换装 / 配件
  if (has(/单片眼镜|侦探/)) { toggleAcc('key12', '侦探模式！'); return; }
  if (has(/眼镜/)) { toggleAcc('key11', '戴上眼镜，看起来聪明吧？'); return; }
  if (has(/钢盆|盆/)) { toggleAcc('key10', '钢盆装备！'); return; }
  if (has(/长裙|短裙|裙子/)) { M.outfit.long = M.outfit.long ? 0 : 1; applyOutfit(); save(); say(M.outfit.long ? '换上长裙～优雅！' : '换回短裙！', true); return; }
  if (has(/脱鞋|光脚/)) { ACC.add('Clothes4'); say('鞋子脱掉好舒服～', true); return; }
  if (has(/穿鞋/)) { ACC.delete('Clothes4'); say('穿好鞋子啦', true); return; }
  if (has(/尾巴/)) { M.outfit.tail = M.outfit.tail ? 0 : 1; applyOutfit(); save(); say(M.outfit.tail ? '尾巴放出来～' : '尾巴收起来了！', true); return; }
  if (has(/q版|Q版|变小|缩小/)) { if (L2D.chibiLevel() < .5) toggleSize(); else say('我已经是 Q 版啦～', true); return; }
  if (has(/正常|变大|长大/)) { if (L2D.chibiLevel() > .5) toggleSize(); else say('已经是正常版啦', true); return; }
  if (has(/摸|乖/)) { startPat(); setTimeout(endPat, 1600); return; }
  if (has(/饭|米/)) { spawnBowl(); return; }
  if (has(/水/)) { drinkTogether(); return; }
  if (has(/番茄|专注/)) { if (!pomo) pomoStart(); return; }
  if (has(/安静|闭嘴|别说话/)) { if (!quiet) toggleQuiet(); return; }
  if (has(/小本本|记录|今天/)) { openBook(); return; }
  pose('think', 1600, toIdle); say('dont_know', true);
}
function toggleAcc(k, msg) { if (ACC.has(k)) { ACC.delete(k); say('收起来～', true); } else { ACC.add(k); pose('smug', 1500, toIdle); say(msg, true); } }
function rps() {
  ask('石头剪刀布！你出什么？', ['石头', '剪刀', '布'].map(x => [x, () => {
    const me = pick(['石头', '剪刀', '布']); const win = { 石头: '剪刀', 剪刀: '布', 布: '石头' };
    if (me === x) { pose('surprised', 1400, toIdle); say('我出' + me + '！平局！再来？', true); }
    else if (win[x] === me) { pose('cry', 1600, toIdle); say('我出' + me + '…呜，你赢了！', true); M.stats.rps++; if (M.stats.rps >= 3) unlock('rps', '猜拳赢家', '猜拳赢了她 3 次'); }
    else { doDance(1800); say('我出' + me + '！我赢啦～', true); }
  }]), 'smug');
}

// ===================== 外观 / 开关 =====================
function applyOutfit() { L2D.def('Clothes1', M.outfit.long ? 0 : 1, 6); L2D.def('Clothes3', M.outfit.chest ? 1 : 0, 6); L2D.def('Clothes5', M.outfit.tail ? 1 : 0, 6); }
// Clothes* 不在表情层里每帧覆盖，单独维护
function dressMenu() {
  chips([['长裙/短裙', () => { M.outfit.long = M.outfit.long ? 0 : 1; applyOutfit(); save(); say(M.outfit.long ? '长裙～' : '短裙！', true); }],
    ['尾巴', () => { M.outfit.tail = M.outfit.tail ? 0 : 1; applyOutfit(); save(); say(M.outfit.tail ? '尾巴出来啦' : '尾巴藏起来', true); }],
    ['领口', () => { M.outfit.chest = M.outfit.chest ? 0 : 1; applyOutfit(); save(); say('换好啦', true); }],
    ['眼镜', () => toggleAcc('key11', '戴上眼镜！')], ['单片眼镜', () => toggleAcc('key12', '侦探模式！')],
    ['鞋子', () => { if (ACC.has('Clothes4')) { ACC.delete('Clothes4'); say('穿好鞋子', true); } else { ACC.add('Clothes4'); say('脱鞋！', true); } }]]);
}
function toggleSize() {
  const toChibi = L2D.chibiLevel() < .5; M.chibi = toChibi ? 1 : 0; save();
  L2D.setChibi(toChibi); particle('✨', 5); face('surprised', 900);
  if (!toChibi && P.onLedge) { P.onLedge = false; P.air = true; P.vy = 0; P.dropThrough = true; }
  $('b-size').textContent = toChibi ? '🔄 正常版' : '🔄 Q版';
  setTimeout(() => say(toChibi ? '变小啦！Q 版大肥鱼～' : '变回来啦！', true), 500);
}
function toggleQuiet() { quiet = !quiet; $('b-quiet').classList.toggle('on', quiet); if (quiet) { bub.classList.remove('show'); say('那我安静陪着你～', true); } else { pose('happy', 1500, toIdle); say('我回来啦', true); } }

// ===================== 小本本（记录面板） =====================
function openBook() {
  const d = day(), y = M.days[yesterday()];
  const FOOD = k => d.meals[k] ? d.meals[k] : d.meals[k] === '' ? '没吃…' : '—';
  const SL = { good: '很好 😊', ok: '还行', bad: '没睡好 😢', late: '熬夜了 🦉' }, MD = { happy: '开心', ok: '一般', tired: '有点累', sad: '难过' };
  const lv = M.aff >= 80 ? '最喜欢你 💗' : M.aff >= 50 ? '亲密' : M.aff >= 20 ? '熟悉' : '有点拘谨';
  const cups = Array.from({ length: 8 }, (_, i) => '<span class="' + (i < d.water ? 'f' : '') + '"></span>').join('');
  const ach = Object.entries(ACH).map(([k, n]) => '<span class="ach ' + (M.ach[k] ? '' : 'off') + '">' + (M.ach[k] ? '🏅 ' + n : '？？？') + '</span>').join('');
  $('bookcard').innerHTML = `<h3>📒 大肥鱼的小本本 · ${today()}</h3><small>她会记住这些，第二天会关心你（数据只存在你自己的浏览器里）</small>
  <div class="sec"><b>💧 喝水</b> ${d.water} / 8 杯<br><div class="cups">${cups}</div></div>
  <div class="sec"><b>🍚 三餐</b><br>早饭：${FOOD('b')}　午饭：${FOOD('l')}　晚饭：${FOOD('d')}</div>
  <div class="sec"><b>😴 昨晚睡眠</b> ${SL[d.sleep] || '还没问'}　<b>🙂 心情</b> ${MD[d.mood] || '还没问'}　<b>🏃 运动</b> ${{ yes: '运动了', no: '没有', tmr: '明天一定' }[d.sport] || '—'}</div>
  <div class="sec"><b>🎮 今天的互动</b><br>摸头 ${d.pats} 次 · 戳了她 ${d.pokes} 下 · 喂饭 ${d.fed} 次 · 下载 ${d.dl} 个 · 番茄钟 ${d.pomo} 个</div>
  <div class="sec"><b>💗 好感度</b> ${Math.round(M.aff)} / 100 · ${lv}<div class="bar2"><i style="width:${M.aff}%"></i></div></div>
  ${y ? `<div class="sec"><b>📅 昨天</b><br>喝水 ${y.water} 杯 · 早 ${y.meals.b || '—'} / 午 ${y.meals.l || '—'} / 晚 ${y.meals.d || '—'} · 睡眠 ${SL[y.sleep] || '—'}</div>` : ''}
  <div class="sec"><b>🏅 成就</b> ${Object.keys(M.ach).length} / ${Object.keys(ACH).length}<br>${ach}</div>
  <div class="sec"><b>⚙️ 设置</b><br>称呼：<b>${M.nick || '—'}</b>　生日：<b>${M.birthday}</b><br>
   提醒：${['water:喝水', 'meal:吃饭', 'sleep:睡觉', 'stand:久坐', 'eye:护眼'].map(x => { const [k, n] = x.split(':'); return `<label style="margin-right:8px"><input type="checkbox" data-r="${k}" ${M.remind[k] ? 'checked' : ''}> ${n}</label>`; }).join('')}
   <br><button id="bk-nick" style="margin-top:6px">改称呼</button> <button id="bk-bday">改生日</button> <button id="bk-reset">清空记忆</button></div>
  <div style="text-align:center;margin-top:10px"><button id="bk-close" style="border:0;background:#3b5bdb;color:#fff;border-radius:14px;padding:8px 22px">关闭</button></div>`;
  $('book').style.display = 'flex';
  $('bookcard').querySelectorAll('input[data-r]').forEach(i => i.onchange = () => { M.remind[i.dataset.r] = i.checked; save(); });
  $('bk-close').onclick = () => $('book').style.display = 'none';
  $('bk-nick').onclick = () => { $('book').style.display = 'none'; P.awaitNick = true; openCmd('想让我怎么叫你？'); };
  $('bk-bday').onclick = () => { const v = prompt('你的生日（月-日，例如 08-15）', M.birthday); if (v && /^\d{2}-\d{2}$/.test(v)) { M.birthday = v; save(); openBook(); } };
  $('bk-reset').onclick = () => { if (confirm('确定清空她记住的所有东西吗？')) { localStorage.removeItem(KEY); location.reload(); } };
  face('finger', 1500);
}
$('book').addEventListener('pointerdown', e => { if (e.target.id === 'book') $('book').style.display = 'none'; });

// ===================== 日常演示（不用等真实时间，按顺序把关心场景过一遍） =====================
let careIdx = 0;
const CARE_DEMO = [
  ['早上：问睡眠', () => askSleep()], ['早饭', () => askMeal('b', '早饭')], ['喝水提醒', () => askWater()], ['护眼', () => { const r = Math.random; eyeBreakForce(); }],
  ['午饭', () => askMeal('l', '午饭')], ['午休建议', () => { face('yawn', 1600); say('remind_nap', true); }], ['久坐提醒', () => askStand()], ['下午茶', () => { face('finger', 1600); say('remind_fruit', true); }],
  ['心情', () => askMood()], ['晚饭', () => askMeal('d', '晚饭')], ['运动', () => askSport()], ['23点：催睡+刷牙', () => { pose('finger', 2200, toIdle); say('remind_sleep1', true); setTimeout(() => say('remind_brush', true), 3000); }],
  ['凌晨：生气催睡', () => { pose('angry', 2400, toIdle); say('remind_sleep3', true); }], ['第二天：引用昨天', () => { const y = day(); face('finger', 2000); say(y.water < 4 ? '昨天只喝了 ' + y.water + ' 杯水，今天要多喝点哦' : y.meals.d ? '昨晚吃的是' + y.meals.d + '，好吃吗？' : '昨天你说要早点睡，做到了吗？', true); }],
  ['生日惊喜', () => { doDance(5000); say('{n}生日快乐！！🎂 今天你最大！', true); particle('🎂', 2); particle('🎉', 4); }],
];
function eyeBreakForce() { pose('finger', 1500, toIdle); say('remind_eye', true); let n = 20; setTimeout(() => { const iv = setInterval(() => { n -= 4; if (n <= 0) { clearInterval(iv); pose('happy', 1200, toIdle); say('好啦！眼睛休息好了～', true); return; } bub.textContent = '看远处… ' + n; bub.classList.add('show'); clearTimeout(say.t); say.t = setTimeout(() => bub.classList.remove('show'), 1500); }, 1000); }, 2600); }
$('b-care').onclick = () => { touch(); const [nm, f] = CARE_DEMO[careIdx % CARE_DEMO.length]; careIdx++; toast('<b>🕐 日常演示 ' + careIdx + '/' + CARE_DEMO.length + '</b>' + nm, null, 2500); if (P.mode === 'sleep') wake(true); setTimeout(f, 300); };

// ===================== 按钮 =====================
$('b-size').onclick = toggleSize; $('b-book').onclick = openBook;
$('b-feed').onclick = () => spawnBowl(); $('b-water').onclick = () => logWater();
$('b-quiet').onclick = toggleQuiet; $('b-fast').onclick = () => { fast = !fast; $('b-fast').classList.toggle('on', fast); };
$('b-pomo').onclick = () => { if (pomo) pomoStop(false); else pomoStart(); };
$('b-link').onclick = () => spawnLink();
$('b-fail').onclick = () => { DL.failDemo = !DL.failDemo; $('b-fail').classList.toggle('on', DL.failDemo); say(DL.failDemo ? '失败演示开了，下载可能会出各种状况' : '失败演示关了', true); };
$('b-clip').onclick = () => { touch(); setTimeout(() => { if (P.mode === 'sleep') wake(true); face('surprised', 1200); dsay('要下载这个吗？'); chips([['下吧', () => feedLink('https://www.youtube.com/watch?v=demo')], ['不用', () => { pose('smug', 1500, toIdle); say('好吧～那我继续待机', true); }, true]]); }, 600); };
$('help-btn').onclick = () => { $('sheet').style.display = 'flex'; };
$('sheet').onclick = () => { $('sheet').style.display = 'none'; };
// 秘籍：↑↑↓↓←→←→BA
const KONAMI = ['ArrowUp', 'ArrowUp', 'ArrowDown', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'ArrowLeft', 'ArrowRight', 'b', 'a']; let kq = [];
document.addEventListener('keydown', e => { if (document.activeElement === $('cmdin')) return; kq.push(e.key); kq = kq.slice(-10); if (kq.join() === KONAMI.join()) { kq = []; konami(); } });
function konami() {
  unlock('konami', '秘籍玩家', '输入了上上下下左右左右BA');
  const seq = ['joy', 'love', 'xx', 'smug', 'dizzy', 'dark', 'cry', 'surprised', 'wink', 'happy']; let i = 0; P.mode = 'react'; P.until = now() + 99999;
  say('隐藏表情大放送！', true); const iv = setInterval(() => { if (i >= seq.length) { clearInterval(iv); P.until = now(); return; } face(seq[i], 700); i++; }, 650);
  ACC.add('key11'); setTimeout(() => { ACC.delete('key11'); ACC.add('key12'); }, 2200); setTimeout(() => { ACC.delete('key12'); ACC.add('key10'); }, 4400); setTimeout(() => ACC.delete('key10'), 6200);
}

// ===================== 窗口拖动 / 时钟 =====================
(() => { const bar = win.querySelector('.bar'); let d = null;
  bar.addEventListener('pointerdown', e => { e.preventDefault(); bar.setPointerCapture(e.pointerId); const r = win.getBoundingClientRect(); d = { dx: e.clientX - r.left, dy: e.clientY - r.top }; win.style.left = r.left + 'px'; win.style.top = r.top + 'px'; });
  bar.addEventListener('pointermove', e => { if (!d) return; const r = win.getBoundingClientRect(); const nx = clamp(e.clientX - d.dx, -r.width + 80, stage.clientWidth - 80), ny = clamp(e.clientY - d.dy, box.h * .9 + 20, floorY() - 60);
    if (P.onLedge) { P.x += nx - r.left; P.rotV += (nx - r.left) * 2; } win.style.left = nx + 'px'; win.style.top = ny + 'px'; lastAct = now(); });
  bar.addEventListener('pointerup', () => d = null); })();
function clock() { const d = new Date(); $('clock').textContent = d.getHours().toString().padStart(2, '0') + ':' + d.getMinutes().toString().padStart(2, '0'); }
setInterval(clock, 10000); clock();

// ===================== 渲染 =====================
function render(t) {
  relayout();
  const left = P.x - box.w / 2, top = P.y - box.h;
  let sx = P.sx, sy = P.sy, rot = P.rot, ty = 0;
  if (P.mode === 'walk') ty = -Math.abs(Math.sin(P.walkPhase * Math.PI)) * 3 * Math.min(1, Math.abs(P.walkV) / 50);
  if (P.mode === 'drag') { petEl.style.transformOrigin = '50% ' + ((box.h - (charRect().h + box.h * .025) + P.grabDy) / box.h * 100) + '%'; } else petEl.style.transformOrigin = '50% 97%';
  petEl.style.transform = `translate(${left}px,${top + ty}px) rotate(${rot}deg) scale(${sx},${sy})`;
  // 影子
  const gy = P.onLedge ? ledge().y : floorY(); const dist = Math.max(0, gy - P.y); const r = charRect();
  const sw = r.w * .7 * Math.max(.35, 1 - dist / 400); shadow.style.width = sw + 'px'; shadow.style.left = (P.x - sw / 2) + 'px'; shadow.style.top = (gy - 6) + 'px'; shadow.style.opacity = Math.max(.12, 1 - dist / 350);
  // 下载进度环
  const tb = $('toasts'), hi = (P.y - box.h * .5) < stage.clientHeight * .5; if (tb.dataset.hi !== String(hi)) { tb.dataset.hi = hi; if (hi) { tb.style.top = 'auto'; tb.style.bottom = '60px'; } else { tb.style.bottom = 'auto'; tb.style.top = '118px'; } }
  const dr = $('dring'); if (dlActive()) { dr.style.display = 'block'; const rw = dr.offsetWidth || 60; dr.style.left = clamp(r.left + r.w * .82, 4, stage.clientWidth - rw - 4) + 'px'; dr.style.top = Math.max(4, r.top + (P.mode === 'drag' ? 30 : -6)) + 'px'; } else dr.style.display = 'none';
  const ch = $('chips'); if (ch.style.display === 'flex') { const cw = ch.offsetWidth; ch.style.left = clamp(P.x - cw / 2, 6, stage.clientWidth - cw - 6) + 'px'; let cy = r.top - (bub.classList.contains('show') ? bub.offsetHeight + 16 : 0) - ch.offsetHeight - 8; const fb = $('fabs').getBoundingClientRect().bottom + 6; if (cy < fb) cy = Math.min(P.y + 6, stage.clientHeight - 50 - ch.offsetHeight); ch.style.top = cy + 'px'; }
  if (bub.classList.contains('show')) { const bw = bub.offsetWidth, bh = bub.offsetHeight; bub.style.left = clamp(P.x - bw / 2, 6, stage.clientWidth - bw - 6) + 'px'; bub.style.top = Math.max(6, r.top - bh - 8) + 'px'; }
  $('state').textContent = '状态：' + (STATE_CN[P.mode] || P.mode) + (quiet ? ' · 安静' : '') + (now() < P.angryUntil ? ' · 生气中' : '') + (L2D.chibiLevel() > .5 ? ' · Q版' : '');
}
function loop() { const t = now(); const dt = Math.min(.033, (t - last) / 1000); last = t; update(dt); faceUpdate(t, dt); render(t); requestAnimationFrame(loop); }

// ===================== 帮助 =====================
$('helpcard').innerHTML = `<h3>和大肥鱼玩</h3>
👋 <b>摸头</b>：在她头上左右来回滑，越摸越害羞，摸久了会睡着<br>
👉 <b>戳一下</b>：点她身体；一直戳会怎样？自己试试（不止生气哦）<br>
😗 <b>戳脸 / 戳呆毛</b>：反应不一样<br>
✨ <b>双击</b>：跳起来，并弹出聊天框<br>
🫳 <b>拎起来</b>：按住拖动，甩太猛会晕；松手会被甩飞，能扔到窗台上<br>
🖱 <b>鼠标</b>：她会用眼睛追着你的鼠标；在她身边画圈圈会把她转晕；一直停在她身上她会害羞<br>
🍚 <b>喂饭 / 🥛 喝水</b>：把碗或杯子拖给她，不管的话她会自己走过去<br>
💬 <b>跟她说话</b>：双击或长按菜单。“我吃了米饭”“喝水了”“我好累”“讲个笑话”“猜拳”“晚安”…她都听得懂<br>
📋 <b>长按 / 右键</b>：菜单（换装、Q版/正常版、哄睡、小本本…）<br><br>
<b>她会关心你</b><br>
😴 早上问你睡得好不好 · 🍚 三餐问你吃了没、吃的什么 · 💧 定时提醒喝水（会记杯数）· 🧍 久坐提醒 · 👀 护眼 20 秒 · 🛌 午休/下午茶 · 🙂 问你心情 · 🏃 问你运动 · 🌙 23 点后递进催睡<br>
📒 <b>小本本</b>：记下你今天喝了几杯水、吃了什么、睡得怎样，第二天她会提起<br>
🕐 <b>日常演示</b>：不用等真实时间，按一下过一个关心场景<br><br>
<b>下载助手</b><br>
🔗 把链接拖到她身上 → 戴上单片眼镜嗅探 → 头顶转圈下载；进度环、速度、剩余时间；网慢了会流冷汗生气；暂停就打瞌睡；完成爱心眼；失败会哭并告诉你原因<br><br>
🔇 安静模式：不说话不乱跑　⏩ 加速预览：把等待时间缩短 10 倍<br>
<small>还有很多彩蛋：深夜来看她、输入奇怪的话、键盘秘籍、节日…<br>模型：DeepSeek-chan（大肥鱼）· 立绘与拆分 Kamiya Ryoko · Live2D 霓虹猫_NeonNeko · 仅个人使用</small>`;

// ===================== 启动 =====================
async function start() {
  loadMem();
  L2D.setChibi(M.chibi !== 0);
  const src = window.__DFY_MODEL__ || 'model/dfy.model3.json';
  await L2D.load(petEl, typeof src === 'function' ? await src() : src);
  L2D.snap('Clothes2', M.chibi !== 0 ? 1 : 0);
  // 衣服默认值（Clothes1 默认 1=短裙，Clothes5 默认 1=有尾巴）
  applyOutfit();
  $('b-size').textContent = M.chibi !== 0 ? '🔄 正常版' : '🔄 Q版';
  relayout(true);
  $('loading').style.display = 'none';
  window.__petModelReady = true;
  P.x = stage.clientWidth * .5; P.y = -40; P.vy = 200; P.mode = 'air'; face('surprised', 1200);
  routineAt = now() + 8000; setMood(0); lastAsk = now();
  const chk = setInterval(() => { if (P.mode === 'idle' || P.mode === 'sit') { clearInterval(chk); if (!M.nick) firstMeet(); else greet(); } }, 60);
  requestAnimationFrame(loop);
  window.__pet = { pose, toIdle, particle, faceName, T, relayout, closeMenu, chips, ask, rect: charRect, region, box: () => box, P, M, L2D, say, face, ACC, DL, runCmd, askSleep, askMeal, askWater, askMood, toggleSize, setNick, life: () => ({ lastAsk, waterNext }), unlock };
}
window.addEventListener('resize', () => relayout(true));
start().catch(e => { window.__err = String(e); $('loading').textContent = '加载失败：' + e; console.error(e); });
})();
