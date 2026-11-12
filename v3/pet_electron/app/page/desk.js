// 桌面版胶水：主进程控制 API ↔ 大肥鱼页面（window.__pet）
// - SniffDLPet.react/bubble/expr/... 给 main.js 的控制服务器调用
// - 命中测试 hitAt(x,y)：只有身体与菜单/气泡按钮等区域吃点击，其余由主进程设为穿透
(function () {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const pet = () => window.__pet;
  const ready = () => !!(window.__pet && window.__petModelReady);
  const cmd = (a) => { try { window.petCmd && window.petCmd(a); } catch (e) {} };

  // ---- 右键菜单追加：隐藏 / 退出（Q版/正常版 原菜单已有）----
  const menu = $('menu');
  if (menu) {
    const add = (a, t) => { const d = document.createElement('div'); d.dataset.a = a; d.textContent = t; menu.appendChild(d); };
    add('hide', '隐藏（Ctrl+Alt+H 唤回）');
    add('quit', '退出桌宠');
    menu.addEventListener('pointerup', (e) => {
      const a = e.target && e.target.dataset ? e.target.dataset.a : '';
      if (a === 'hide') { menu.style.display = 'none'; cmd('hide'); }
      if (a === 'quit') { menu.style.display = 'none'; cmd('quit'); }
    });
  }

  // ---- 下载状态反应（busy 会一直举着手，直到 done/fail/idle）----
  // busy → 举手 + 开心 + 头顶加载圈(key8)；done → 爱心眼 + 脸红；fail → 流泪，再流冷汗；idle → 常态
  let state = 'idle', keepT = 0;
  const SHOWING = new Set(['doneLove', 'cry', 'sweat']);
  const EXPR = { '加载': 'key8', '爱心眼': 'key4', '脸红': 'key7', '流泪': 'key2', '流冷汗': 'key13',
    '黑脸': 'key1', '圈圈眼': 'key3', '＞＜': 'key5', '屑屑眼': 'key6', '兔牙': 'key9', '钢盆': 'key10',
    '黑框眼镜': 'key11', '单片眼镜': 'key12' };
  const FACE_OF = { '流泪': 'cry', '流冷汗': 'sweat', '爱心眼': 'doneLove', '脸红': 'shy', '黑脸': 'dark',
    '圈圈眼': 'dizzy', '＞＜': 'xx', '屑屑眼': 'smug' };
  function setState(s) {
    state = s; clearInterval(keepT);
    if (s === 'busy') {
      keepT = setInterval(() => {   // 被别的互动打断后，空闲时把举手+加载圈续上
        const p = pet(); if (!p || state !== 'busy') return;
        if (SHOWING.has(p.faceName())) return;   // 上一个任务的完成/失败表演还没演完：先让它演完
        p.ACC.add('key8');
        // 下载中就一直举着手：除了被拎着/吃饭/睡觉，别的表情盖掉了就续上
        if (p.faceName() !== 'busyhand' && !/^(drag|eat|sleep|air)$/.test(p.P.mode)) p.face('busyhand', 2500);
      }, 600);
    }
  }
  // 举手：模型的「默认手切举手」动作末帧参数（Param56/57 交给 Idle 动作）。下载中把权重缓到 1，结束缓回 0。
  const HAND = { Movement2: 1, Param59: .476, Param60: .86, Param61: .002, Param62: .034 };
  let handW = 0, handHooked = false, handT = performance.now();
  function hookHand() {
    if (handHooked || !ready()) return;
    const im = pet().L2D.model && pet().L2D.model.internalModel; if (!im) return;
    const core = im.coreModel, ps = core._model.parameters, idx = {};
    for (const k in HAND) { const i = ps.ids.indexOf(k); if (i >= 0) idx[k] = [i, ps.defaultValues[i]]; }
    im.on('afterMotionUpdate', () => {
      const t = performance.now(), dt = Math.min(.05, (t - handT) / 1000); handT = t;
      const want = state === 'busy' && !SHOWING.has(pet().faceName()) && !/^(drag|sleep|eat)$/.test(pet().P.mode) ? 1 : 0;
      handW += Math.sign(want - handW) * Math.min(Math.abs(want - handW), dt * 2.5);
      if (handW <= 0) return;
      for (const k in idx) { const [i, d] = idx[k]; const cur = core.getParameterValueByIndex(i);
        core.setParameterValueByIndex(i, cur + (d + (HAND[k] - d) * handW - cur) * handW); }
    });
    handHooked = true;
  }
  let pending = null;
  function react(name) {
    hookHand();
    if (!ready()) {   // 宿主刚启动、模型还在加载：记住最后一条，加载完补上
      if (!pending) { const iv = setInterval(() => { if (ready()) { clearInterval(iv); const n = pending; pending = null; setTimeout(() => react(n), 1200); } }, 300); }
      pending = name; return false;
    }
    const p = pet(); name = String(name || 'idle');
    if (p.P.mode === 'sleep') { try { p.toIdle(); } catch (e) {} }
    switch (name) {
      case 'busy':
        if (SHOWING.has(p.faceName())) { setState('busy'); break; }   // 紧接着下一个任务：完成/失败表演演完再举手
        setState('busy'); p.ACC.add('key8'); p.pose('busyhand', 2600, p.toIdle); p.particle('✨', 1); break;
      case 'done':
        setState('idle'); p.ACC.delete('key8'); p.pose('doneLove', 3200, p.toIdle); p.particle('💗', 3); break;
      case 'fail':
        setState('idle'); p.ACC.delete('key8');
        p.pose('cry', 2200, () => p.pose('sweat', 2200, p.toIdle)); p.particle('💧', 2); break;
      case 'happy': p.pose('happy', 1800, p.toIdle); break;
      case 'surprise': p.pose('surprised', 1200, p.toIdle); p.particle('❗', 1); break;
      case 'shy': p.pose('shy', 1800, p.toIdle); break;
      case 'pout': p.pose('pout', 1800, p.toIdle); break;
      case 'idle': default:
        setState('idle'); p.ACC.delete('key8'); p.face('neutral', 10); p.toIdle(); break;
    }
    return true;
  }
  function expr(name) {
    if (!ready()) return false;
    const p = pet(); name = String(name || '');
    if (name in FACE_OF) { p.pose(FACE_OF[name], 2500, p.toIdle); return true; }
    if (name in EXPR) { const k = EXPR[name]; if (p.ACC.has(k)) p.ACC.delete(k); else p.ACC.add(k); return true; }
    if (name) { p.pose(name, 2000, p.toIdle); return true; }   // 直接给 FACES 名（happy/cry/...）
    return false;
  }
  function bubble(text, ms) {
    if (!ready()) return false;
    pet().say(String(text || ''), true);
    if (ms) { const b = $('bubble'); clearTimeout(bubble.t); bubble.t = setTimeout(() => b.classList.remove('show'), Math.max(1200, ms)); }
    return true;
  }
  function setChibi(on) {
    if (!ready()) return false;
    const p = pet(); const isChibi = p.L2D.chibiLevel() >= .5;
    if (!!on !== isChibi) p.toggleSize();
    return true;
  }
  function setScale(k) { window.__petScale = Math.max(.3, Math.min(3, +k || 1)); if (ready()) pet().relayout(true); return window.__petScale; }
  function moveTo(x, y) {   // 窗口内坐标（脚底中心）
    if (!ready()) return false;
    const p = pet(); p.P.x = x; p.P.y = Math.min(y, document.getElementById('stage').clientHeight); p.P.mode = 'air'; p.P.vy = 0; p.P.vx = 0;
    return true;
  }
  // 命中：身体区域 + 可交互浮层。拖拽/菜单打开期间整窗都吃点击。
  const HOT = '#menu,#chips,#cmd,#sheet,#book,.toast,#bowl,#cup,#link';
  function hitAt(x, y) {
    if (!ready()) return false;
    const p = pet();
    if (p.P.mode === 'drag') return true;
    if (menu && menu.style.display === 'block') return true;
    if ($('sheet').style.display === 'flex' || $('book').style.display === 'flex') return true;
    if ($('cmd').style.display === 'block') { const r = $('cmd').getBoundingClientRect(); if (x >= r.left && x <= r.right && y >= r.top && y <= r.bottom) return true; }
    if (p.region(x, y)) return true;
    const el = document.elementFromPoint(x, y);
    return !!(el && el.closest && el.closest(HOT));
  }
  // 穿透时页面收不到 mousemove（Linux 没有 forward）：主进程把光标喂进来，合成 pointermove 让视线/悬停照常工作
  let lastFeed = 0;
  function feedCursor(x, y) {
    const t = performance.now(); if (t - lastFeed < 30) return; lastFeed = t;
    try { document.getElementById('stage').dispatchEvent(new PointerEvent('pointermove', { clientX: x, clientY: y, bubbles: true })); } catch (e) {}
  }
  // Windows：穿透窗也会收到 forward 的真实 mousemove → 直接报命中，比主进程轮询更跟手
  let lastHit = null;
  document.addEventListener('mousemove', (e) => {
    if (!e.isTrusted) return;
    const h = hitAt(e.clientX, e.clientY);
    if (h !== lastHit) { lastHit = h; cmd(h ? 'hit:1' : 'hit:0'); }
  }, true);
  window.SniffDLPet = {
    isReady: ready, react, expr, bubble, setChibi, setScale, moveTo, hitAt, feedCursor,
    state: () => state,
    info: () => { const p = pet(); return p ? { hand: +handW.toFixed(2), mode: p.P.mode, x: Math.round(p.P.x), y: Math.round(p.P.y), chibi: p.L2D.chibiLevel(), face: p.faceName(), acc: [...p.ACC], state, scale: window.__petScale || 1, nick: p.M.nick || '', aff: p.M.aff, visible: !document.hidden, err: window.__err || '' } : { err: window.__err || 'not ready' }; },
  };
})();
