// 界面背景图：整幅插画透出 + 玻璃面板 + 左重右轻遮罩。
// 设置项（core/settings.py）：ui_bg = none|bg1..bg4|custom，ui_bg_custom（路径/URL），ui_bg_fit，ui_bg_pos，
// ui_bg_opacity / ui_bg_blur / ui_bg_dim。内置 4 张已抠掉黑底（assets/bg/bgN.webp 带透明通道），
// 浅色 / 深色主题下都落在主题底色上；自定义图片按原样铺满。
// html[data-bg="on"] 打开 CSS 里的玻璃变量（app.css「背景图」段）。
import { S } from '../store.js';

export const BUILTIN = [
  { id: 'bg1', title: '大肥鱼 · 立绘', src: 'assets/bg/bg1.webp', thumb: 'assets/bg/bg1_t.webp', fit: 'contain', pos: '100% 100%' },
  { id: 'bg2', title: '大肥鱼 · 抬手', src: 'assets/bg/bg2.webp', thumb: 'assets/bg/bg2_t.webp', fit: 'contain', pos: '100% 100%' },
  { id: 'bg3', title: '大肥鱼 · 伸手', src: 'assets/bg/bg3.webp', thumb: 'assets/bg/bg3_t.webp', fit: 'contain', pos: '100% 100%' },
  { id: 'bg4', title: '蓝发女仆', src: 'assets/bg/bg4.webp', thumb: 'assets/bg/bg4_t.webp', fit: 'contain', pos: '100% 100%' },
];
const POS = { '右': '100% 50%', '居中': '50% 50%', '左': '0% 50%', '顶部': '50% 0%', '底部': '50% 100%' };

let layer = null, img = null, lastSrc = '', failSrc = '';
let preview = null;   // 拖动滑块时的临时值（不写盘）

// 本机路径 → file:// URL（Electron 主窗走 file://，能直接读本机图片）；http(s)/data/file: 原样
export function pathToUrl(p) {
  p = String(p || '').trim();
  if (!p) return '';
  if (/^(https?:|data:|blob:|file:)/i.test(p)) return p;
  let s = p.replace(/\\/g, '/');
  if (/^[a-z]:\//i.test(s)) s = '/' + s;            // C:/x → /C:/x
  else if (s.startsWith('//')) return 'file:' + encodeURI(s);   // UNC
  return 'file://' + encodeURI(s).replace(/#/g, '%23').replace(/\?/g, '%3F');
}

function ensureLayer() {
  if (layer) return;
  layer = document.createElement('div');
  layer.id = 'bgLayer'; layer.setAttribute('aria-hidden', 'true');
  layer.innerHTML = '<div class="bg-base"></div><div class="bg-img"></div><div class="bg-veil"></div>';
  document.body.prepend(layer);
  img = layer.querySelector('.bg-img');
}

function val(k, d) {
  if (preview && k in preview) return preview[k];
  const v = S.settings?.[k];
  return v == null || v === '' ? d : v;
}

export function currentBg() {
  const id = val('ui_bg', 'none');
  if (id === 'custom') {
    const url = pathToUrl(val('ui_bg_custom', ''));
    return url ? { id, src: url, fit: 'cover', pos: '50% 50%', custom: true } : null;
  }
  return BUILTIN.find((b) => b.id === id) || null;
}

export function applyBg() {
  ensureLayer();
  const root = document.documentElement;
  const b = currentBg();
  if (!b || b.src === failSrc) {
    delete root.dataset.bg;
    layer.classList.remove('on');
    return;
  }
  const fitPref = val('ui_bg_fit', '自动');
  const fit = fitPref === '铺满' ? 'cover' : fitPref === '完整' ? 'contain' : b.fit;
  const posPref = val('ui_bg_pos', '自动');
  const pos = POS[posPref] || b.pos;
  const op = Math.max(10, Math.min(100, Number(val('ui_bg_opacity', 100)))) / 100;
  const blur = Math.max(0, Math.min(24, Number(val('ui_bg_blur', 0))));
  const dim = Math.max(0, Math.min(100, Number(val('ui_bg_dim', 60)))) / 100;
  if (b.src !== lastSrc) {
    lastSrc = b.src;
    img.style.backgroundImage = `url("${b.src.replace(/"/g, '%22')}")`;
    // 自定义图片读不到（路径错 / 纯浏览器模式读不了本机文件）→ 退回无背景并通知设置页
    const probe = new Image();
    probe.onload = () => { if (failSrc === b.src) failSrc = ''; window.dispatchEvent(new CustomEvent('sdl-bg', { detail: { ok: true, src: b.src } })); };
    probe.onerror = () => {
      if (lastSrc !== b.src) return;
      failSrc = b.src; lastSrc = '';
      applyBg();
      window.dispatchEvent(new CustomEvent('sdl-bg', { detail: { ok: false, src: b.src, custom: !!b.custom } }));
    };
    probe.src = b.src;
  }
  img.style.backgroundSize = fit === 'contain' ? 'contain' : 'cover';
  img.style.backgroundPosition = pos;
  img.style.opacity = String(op);
  img.style.filter = blur ? `blur(${blur}px)` : '';
  img.style.transform = blur ? `scale(${1 + blur / 300})` : '';
  layer.style.setProperty('--dim', String(dim));
  root.style.setProperty('--dim', String(dim));
  root.dataset.bg = 'on';
  root.dataset.bgKind = b.custom ? 'photo' : 'art';
  layer.classList.add('on');
}

// 设置页滑块拖动时实时预览（松手才保存）
export function previewBg(patch) { preview = patch ? { ...(preview || {}), ...patch } : null; applyBg(); }
export function clearBgFail() { failSrc = ''; }
