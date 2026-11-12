// ===================== Live2D 控制层 =====================
// 思路：不靠“播放动画帧”，而是每帧给模型的追踪参数（AngleX/Y/Z、PositionX/Y/Z、眼、嘴、眉…）喂目标值，
// 由模型自带的物理（头发、尾巴、裙摆、耳鳍）自然带出二次运动。这就是 VTube Studio 里“活着”的感觉。
const L2D = (() => {
  const CHIBI_BB = [.333, .114, .667, .344];   // Q版在模型里的范围（相对模型宽高）
  const FULL_BB  = [.15, .099, .96, .953];     // 正常版（含尾巴）
  let app = null, model = null, core = null, ready = false;
  const P = {};         // id -> {cur, tgt, rate, min, max}
  const parts = {};     // part id -> opacity override
  let chibi = 1, chibiT = 1;        // 当前/目标 Q版程度（Clothes2）
  let boxW = 200, boxH = 260;

  const TRACK = ['AngleX','AngleY','AngleZ','PositionX','PositionY','PositionZ','LeanInput','ParamBreath',
    'ParamEyeLOpen','ParamEyeROpen','EyeSmile','ParamEyeBallX','ParamEyeBallY','ParamBrowLY','ParamBrowRY',
    'ParamBrowLAngle','ParamBrowRAngle','BrowInnerUp','ParamMouthForm','ParamMouthOpenY','JawOpen',
    'MouthSmile_L','MouthSmile_R','Mouthpucker','MouthFunnel','CheeckPuff','TongueOut','MouthX',
    'ParamA','ParamI','ParamU','ParamE','ParamO','ShockL','ShockR','LEyeSquint','REyeSquint','Press',
    'key1','key2','key3','key4','key5','key6','key7','key8','key9','key10','key11','key12','key13',
    'Movement1','Movement2','Movement3','Clothes1','Clothes3','Clothes4','Clothes5'];

  function def(id, v, rate) { const p = P[id]; if (!p) return; p.tgt = v; if (rate) p.rate = rate; }
  function snap(id, v) { const p = P[id]; if (!p) return; p.tgt = p.cur = v; }
  function get(id) { return P[id] ? P[id].cur : 0; }

  async function load(container, src) {
    app = new PIXI.Application({ width: 10, height: 10, backgroundAlpha: 0, antialias: true,
      resolution: Math.min(2, window.devicePixelRatio || 1), autoDensity: true });
    container.appendChild(app.view);
    model = await PIXI.live2d.Live2DModel.from(src, { autoInteract: false, autoUpdate: true, idleMotionGroup: 'Idle' });
    app.stage.addChild(model);
    const im = model.internalModel; core = im.coreModel;
    // 关掉库自带的眨眼/呼吸/视线，全部自己控制，这样表情不会和自动眨眼打架
    im.eyeBlink = undefined; im.breath = undefined;
    if (im.focusController) im.focusController.focus = () => {};
    const m = core._model, ps = m.parameters;
    for (const id of TRACK.concat(['Clothes2'])) {
      const i = ps.ids.indexOf(id); if (i < 0) continue;
      const d = ps.defaultValues[i];
      P[id] = { cur: d, tgt: d, rate: 10, min: ps.minimumValues[i], max: ps.maximumValues[i], i };
    }
    P.Clothes2.cur = P.Clothes2.tgt = chibi;
    im.on('afterMotionUpdate', applyParams);
    im.on('beforeModelUpdate', applyParts);
    try { model.motion('Idle', 0, 1); } catch (e) {}
    ready = true;
    return model;
  }

  let lastT = performance.now();
  function applyParams() {
    const t = performance.now(), dt = Math.min(.05, (t - lastT) / 1000); lastT = t;
    for (const id in P) {
      const p = P[id];
      if (id === 'Clothes2') { const d = chibiT - p.cur; p.cur += Math.sign(d) * Math.min(Math.abs(d), dt * 2.2); chibi = p.cur; }
      else p.cur += (p.tgt - p.cur) * Math.min(1, dt * p.rate);
      const v = Math.max(p.min, Math.min(p.max, p.cur));
      core.setParameterValueByIndex ? core.setParameterValueByIndex(p.i, v) : core.setParameterValueById(id, v);
    }
  }
  function applyParts() {
    const m = core._model;
    for (const id in parts) { const i = m.parts.ids.indexOf(id); if (i >= 0) m.parts.opacities[i] = parts[id]; }
  }

  // 根据 Q版程度，算出模型在画布中的缩放和位置（两种形态之间平滑过渡）
  function layout(w, h) {
    boxW = w; boxH = h;
    app.renderer.resize(w, h);
    frame();
  }
  function lerp(a, b, t) { return a + (b - a) * t; }
  function frame() {
    if (!model) return;
    const k = Math.max(0, Math.min(1, chibi));
    const bb = [0, 1, 2, 3].map(i => lerp(FULL_BB[i], CHIBI_BB[i], k));
    const iw = model.internalModel.width, ih = model.internalModel.height;
    const bw = (bb[2] - bb[0]) * iw, bh = (bb[3] - bb[1]) * ih;
    const s = Math.min(boxW * .86 / bw, boxH * .9 / bh);
    model.scale.set(s);
    model.x = boxW / 2 - ((bb[0] + bb[2]) / 2) * iw * s;
    model.y = boxH * .975 - bb[3] * ih * s;
  }
  function setChibi(v) { chibiT = v ? 1 : 0; }
  function chibiLevel() { return chibi; }
  function motion(group) { try { return model.motion(group, 0, 3); } catch (e) {} }

  return { load, def, snap, get, layout, frame, setChibi, chibiLevel, motion, parts, P,
    get ready() { return ready; }, get model() { return model; }, get app() { return app; } };
})();
