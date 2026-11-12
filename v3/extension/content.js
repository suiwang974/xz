/* SniffDL 页面小询问框（content script）
 * 目标：右键「复制视频地址 / 复制链接」那种场合，别弹居中大窗打断浏览；
 * 改为在鼠标旁浮一个小小的询问卡「下载此链接 / 下载此视频 / 下载此图片」，
 * 一键直连交给桌面看板娘（只弹进度·完成），不想下就点 × 或点别处，几秒自动消失。
 * 定位刻意朝鼠标「左上方」展开，避开系统原生右键菜单，两者不打架。
 */

/* ---- 主动截击器：抢在浏览器弹「另存为」之前劫走下载点击（对标 IDM 的关键）----
 * 反应式 chrome.downloads.onCreated 太慢——浏览器原生「另存为」模态框一出来扩展就压不住。
 * 这里在捕获阶段用事件委托监听 click：命中"指向可下载文件 / 带 download 属性"的链接即
 * preventDefault 掐掉默认下载，交给网页中央下载卡（弹不出则静默后台 aria2 下）。 */
(() => {
  if (window.__sniffdlGrab) return;
  window.__sniffdlGrab = true;

  // 与后台 DEFAULTS.enabled 对齐的全局开关（默认开）；关掉就不再劫持任何点击
  let enabled = true;
  try {
    chrome.storage.local.get(["enabled"], (r) => { if (r && typeof r.enabled === "boolean") enabled = r.enabled; });
    chrome.storage.onChanged.addListener((ch, area) => {
      if (area === "local" && ch.enabled) enabled = !!ch.enabled.newValue;
    });
  } catch (e) {}

  const DL_EXT = /\.(exe|msi|msp|apk|ipa|deb|rpm|dmg|pkg|appimage|jar|zip|rar|7z|tar|gz|tgz|bz2|xz|iso|img|bin|cab|pdf|epub|mobi|azw3|xlsx?|docx?|pptx?|mp4|mkv|mov|avi|webm|flv|wmv|m4v|ts|mp3|flac|m4a|wav|opus|ogg)(\?|#|$)/i;

  function anchorOf(node) {
    try { return (node && node.closest) ? node.closest("a[href],area[href]") : null; } catch (e) { return null; }
  }
  function absUrl(a) {
    try { return a.href ? new URL(a.href, location.href).href : ""; } catch (e) { return ""; }
  }
  function isDownload(a) {
    if (!a) return false;
    const u = absUrl(a);
    if (!/^(https?|ftp):/i.test(u)) return false;
    if (a.hasAttribute && a.hasAttribute("download")) return true;   // 显式 download 属性
    return DL_EXT.test(u);                                           // 或 URL 带可下载扩展名
  }
  function isOurs(node) {                                            // 点我们自己的浮标/卡片不劫持
    try { return !!(node && node.closest && node.closest("#sniffdl-chip,#sniffdl-badge,#sniffdl-panel")); } catch (e) { return false; }
  }

  // 0.7.0：v3 内核的接管规则（扩展名/站点白黑名单/Alt 放行），由后台从内核读来缓存；拿不到时沿用 0.6.0 判定
  let R = { mode: "", rules: null, t: 0 };
  function loadRules(force) {
    try {
      chrome.runtime.sendMessage({ type: "rules", force: !!force }, (r) => {
        void chrome.runtime.lastError;
        if (r && r.ok) R = { mode: r.mode, rules: r.rules, t: Date.now() };
      });
    } catch (e) {}
  }
  loadRules(true);
  try { document.addEventListener("visibilitychange", () => { if (!document.hidden) loadRules(true); }); } catch (e) {}
  try { window.addEventListener("focus", () => { if (Date.now() - R.t > 3000) loadRules(true); }); } catch (e) {}
  function hostIn(h, list) {
    h = (h || "").toLowerCase();
    return !!h && (list || []).some((d) => { d = String(d || "").toLowerCase().replace(/^https?:\/\//, "").replace(/^\*\./, "").split("/")[0]; return !!d && (h === d || h.endsWith("." + d)); });
  }
  function extOf(u) { try { const m = new URL(u).pathname.match(/\.([a-z0-9]{1,8})$/i); return m ? m[1].toLowerCase() : ""; } catch (e) { return ""; } }
  // v3 规则下：这个链接要不要由我们接管（返回 null = 规则未就绪，按旧判定）
  function v3Wants(a, url) {
    const r = R.rules;
    if (R.mode !== "v3" || !r) return null;
    if (!r.enabled) return false;
    let lh = ""; try { lh = new URL(url).hostname; } catch (e) {}
    const hosts = [location.hostname, lh];
    if (hosts.some((h) => hostIn(h, r.blacklist))) return false;
    const white = hosts.some((h) => hostIn(h, r.whitelist));
    const ext = extOf(url);
    if (white) return (a.hasAttribute && a.hasAttribute("download")) || DL_EXT.test(url) || (!!ext && (r.exts || []).includes(ext));
    if ((r.exts || []).length) return (r.exts || []).includes(ext);
    return (a.hasAttribute && a.hasAttribute("download")) || DL_EXT.test(url);
  }

  let _lastGestureSent = 0;
  document.addEventListener("click", (e) => {
    if (!enabled) return;
    if (e.defaultPrevented || e.button !== 0) return;
    // 0.7.0：按住 Alt 点击 = 不接管（内核设置「按住 Alt 点击时不接管」，旧桌面端默认也是放行），
    // 告诉后台这次浏览器自己的下载（Alt+点击 = 浏览器另存）别在 onCreated 里再抢
    if (e.altKey && (!R.rules || R.rules.alt_bypass !== false)) {
      try { const a0 = anchorOf(e.target); chrome.runtime.sendMessage({ type: "gesture", bypass: true, url: a0 ? absUrl(a0) : "" }, () => { void chrome.runtime.lastError; }); } catch (_) {}
      return;
    }
    // 按住 Ctrl/Shift/Meta = 用户想自己新开标签/另存，放行不劫持
    if (e.ctrlKey || e.shiftKey || e.metaKey) return;
    // 记录"用户刚在本页点了一下"，供后台 onCreated 判定"这是不是用户主动发起的下载"（防洪水：无手势不抢）
    const _now = Date.now();
    if (_now - _lastGestureSent > 250) {
      _lastGestureSent = _now;
      try { chrome.runtime.sendMessage({ type: "gesture" }, () => { void chrome.runtime.lastError; }); } catch (_) {}
    }
    if (isOurs(e.target)) return;
    const a = anchorOf(e.target);
    if (!a) return;
    const url0 = absUrl(a);
    if (!/^(https?|ftp):/i.test(url0)) return;
    const want = v3Wants(a, url0);
    if (want === false) return;                       // v3 规则：不接管（黑名单/扩展名不在列表/接管关闭）→ 浏览器照常
    if (want === null && !isDownload(a)) return;      // 规则未就绪：0.6.0 判定
    const url = url0;
    e.preventDefault();
    e.stopPropagation();
    try {
      chrome.runtime.sendMessage(
        // click_dl：后台现取规则——v3 交内核、离线交回浏览器并提示、旧桌面端回落 0.6.0 网页中央卡（规则还没到也不会走错路）
        { type: "click_dl", url, filename: "", referer: location.href, size: 0 },
        () => { void chrome.runtime.lastError; }
      );
    } catch (_) {}
  }, true);
})();

(() => {
  if (window.__sniffdlChip) return;      // 防重复注入
  window.__sniffdlChip = true;

  const isHttp = (u) => !!u && /^https?:/i.test(u);
  const VIDEO_EXT = /\.(mp4|mkv|webm|m4v|mov|flv|ts|m3u8|f4v)(\?|#|$)/i;
  const FILE_EXT = /\.(exe|msi|apk|zip|rar|7z|tar|gz|iso|pdf|docx?|xlsx?|pptx?|epub|apk|dmg|whl|jar)(\?|#|$)/i;

  let theme = { accent: "#3E6FD0", card: "#FFFFFF", ink: "#1F2A44", inkL: "#5C6880", line: "#D7E0F0" };
  try {
    chrome.runtime.sendMessage({ type: "theme" }, (r) => {
      const ok = !!(r && r.ok && r.theme);
      window.__sniffdlRpcOk = ok;
      if (ok) {
        const p = r.theme.palette || {};
        theme = {
          accent: r.theme.accent || p.pink || theme.accent,
          card: p.card || theme.card,
          ink: p.ink || theme.ink,
          inkL: p.ink_l || theme.inkL,
          line: p.line || theme.line,
        };
        window.__sniffdlTheme = r.theme;
        if (chip) paint(chip);
      }
      try { document.dispatchEvent(new CustomEvent("sniffdl-theme-ready", { detail: { ok } })); } catch (e) {}
    });
  } catch (e) {}

  let chip = null, hideTimer = null, last = null;

  function ensure() {
    if (chip) return chip;
    chip = document.createElement("div");
    chip.id = "sniffdl-chip";
    paint(chip);
    chip.addEventListener("mousedown", (e) => e.stopPropagation());
    document.documentElement.appendChild(chip);
    return chip;
  }

  function paint(el) {
    Object.assign(el.style, {
      position: "fixed", zIndex: "2147483646", display: "none",
      maxWidth: "280px", padding: "0", border: "1px solid " + theme.line,
      borderRadius: "12px", background: theme.card, color: theme.ink,
      boxShadow: "0 8px 26px -8px rgba(0,0,0,.35)", font:
        '13px/1.5 "Microsoft YaHei UI","Segoe UI",system-ui,sans-serif',
      userSelect: "none", transition: "opacity .18s ease, transform .18s ease",
      opacity: "0", transform: "translateY(4px)",
    });
  }

  function dismiss() {
    if (!chip) return;
    chip.style.opacity = "0";
    chip.style.transform = "translateY(4px)";
    const el = chip;
    setTimeout(() => { if (el.style.opacity === "0") el.style.display = "none"; }, 200);
    if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
  }

  function armAutoHide(ms) {
    if (hideTimer) clearTimeout(hideTimer);
    hideTimer = setTimeout(dismiss, ms || 6000);
  }

  function resolveTarget(node) {
    // 从右键命中的元素往上找可下载对象
    const a = node.closest && node.closest("a[href]");
    const vid = node.closest && node.closest("video");
    const src = node.closest && node.closest("source");
    const img = node.closest && node.closest("img");

    if (vid) {
      const u = vid.currentSrc || vid.src || (vid.querySelector("source") || {}).src || "";
      if (isHttp(u)) return { url: u, kind: "video" };
    }
    if (src && isHttp(src.src)) return { url: src.src, kind: "video" };
    if (a) {
      const href = a.href;
      if (isHttp(href)) {
        if (VIDEO_EXT.test(href)) return { url: href, kind: "video", filename: a.getAttribute("download") || "" };
        if (FILE_EXT.test(href)) return { url: href, kind: "file", filename: a.getAttribute("download") || "" };
        return { url: href, kind: "link", filename: a.getAttribute("download") || "" };
      }
    }
    if (img && isHttp(img.currentSrc || img.src)) return { url: img.currentSrc || img.src, kind: "image" };
    return null;
  }

  const LABEL = { video: "下载此视频", image: "下载此图片", file: "下载此文件", link: "下载此链接" };

  function shortUrl(u) {
    try {
      const x = new URL(u);
      let s = x.hostname + x.pathname;
      return s.length > 34 ? s.slice(0, 33) + "…" : s;
    } catch (e) { return u.length > 34 ? u.slice(0, 33) + "…" : u; }
  }

  function build(t) {
    const el = ensure();
    el.innerHTML = "";
    const wrap = document.createElement("div");
    wrap.style.cssText = "padding:9px 10px 10px;";

    const head = document.createElement("div");
    head.style.cssText = "display:flex;align-items:center;gap:6px;margin-bottom:7px;";
    const dot = document.createElement("span");
    dot.style.cssText = "width:8px;height:8px;border-radius:50%;background:" + theme.accent + ";flex:none;";
    const title = document.createElement("span");
    title.textContent = "要交给看板娘吗？";
    title.style.cssText = "font-weight:700;font-size:12.5px;";
    const close = document.createElement("span");
    close.textContent = "×";
    close.title = "不用，我只是复制一下";
    close.style.cssText = "margin-left:auto;cursor:pointer;color:" + theme.inkL + ";font-size:16px;line-height:1;padding:0 3px;";
    close.addEventListener("click", dismiss);
    head.appendChild(dot); head.appendChild(title); head.appendChild(close);

    const urlLine = document.createElement("div");
    urlLine.textContent = shortUrl(t.url);
    urlLine.title = t.url;
    urlLine.style.cssText = "font-size:11px;color:" + theme.inkL + ";margin:0 0 8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:260px;";

    const go = document.createElement("button");
    go.textContent = LABEL[t.kind] || "下载";
    go.style.cssText = "width:100%;cursor:pointer;border:none;border-radius:9px;padding:8px 10px;" +
      "font-weight:700;font-size:13px;color:#fff;font-family:inherit;" +
      "background:linear-gradient(135deg," + theme.accent + "," + theme.accent + "cc);";
    go.addEventListener("click", () => doDownload(t, go));

    wrap.appendChild(head); wrap.appendChild(urlLine); wrap.appendChild(go);
    el.appendChild(wrap);
    return el;
  }

  function doDownload(t, btn) {
    btn.disabled = true;
    btn.textContent = "打开下载卡…";
    btn.style.opacity = ".8";
    dismiss();
    // 优先在网页正中央弹 IDM 式确认卡（不再跳回桌面软件）；拿不到卡片再回落快捷直连
    if (window.__sniffdlOpenCard) {
      try {
        window.__sniffdlOpenCard({
          url: t.url, filename: t.filename || "", referer: location.href,
          size: 0, ext: "", source: "chip",
        });
        return;
      } catch (e) {}
    }
    try {
      chrome.runtime.sendMessage({
        type: "chip_dl", url: t.url, filename: t.filename || "", referer: location.href,
      }, () => {});
    } catch (e) {}
  }

  function show(t, x, y) {
    const el = build(t);
    el.style.display = "block";
    // 先量尺寸再定位：朝鼠标「左上方」展开，避开原生右键菜单（原生菜单往右下长）
    const r = el.getBoundingClientRect();
    const w = r.width || 260, h = r.height || 96;
    const vw = innerWidth, vh = innerHeight;
    let left = x - w - 10;      // 默认放左边
    let top = y - h - 6;        // 略偏上
    if (left < 8) left = x + 12;          // 左边放不下→放右边（仍可能压菜单，尽量靠下错开）
    if (left + w > vw - 8) left = Math.max(8, vw - w - 8);
    if (top < 8) top = y + 18;            // 上方不够→往下挪一点并左移，躲菜单主体
    if (top + h > vh - 8) top = Math.max(8, vh - h - 8);
    el.style.left = left + "px";
    el.style.top = top + "px";
    requestAnimationFrame(() => { el.style.opacity = "1"; el.style.transform = "translateY(0)"; });
    armAutoHide(6000);
  }

  document.addEventListener("contextmenu", (e) => {
    if (chip && chip.contains(e.target)) return;   // 点在小框上不算
    const node = e.target;
    if (!node || !node.closest) { dismiss(); return; }
    const t = resolveTarget(node);
    if (!t) { dismiss(); return; }
    last = t;
    // 让原生菜单先弹出，稍后再浮小框，视觉更自然
    setTimeout(() => show(t, e.clientX, e.clientY), 120);
  }, true);

  // 任何滚动 / 点击别处 / 按 Esc / 切窗 → 收起，绝不挡路
  window.addEventListener("scroll", dismiss, true);
  window.addEventListener("resize", dismiss, true);
  window.addEventListener("blur", dismiss);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") dismiss(); }, true);
  document.addEventListener("pointerdown", (e) => {
    if (chip && chip.style.display !== "none" && !chip.contains(e.target)) dismiss();
  }, true);
})();

/* SniffDL 自动浮标（content script）
 * 一进页面/一放视频，若嗅到可下载媒体，就在播放器右上角（无播放器则视口右上角）
 * 自动浮一个小按钮；点开→把整页 URL 交给桌面端猫爪深度嗅探→列出多清晰度/格式，
 * 点某条走 IDM 式确认窗下载，或整页批量。面板实时回显软件端嗅探/下载状态。
 * 比 IDM 强在：即便页面没有视频直链，只要有页面 URL，桌面端也能把视频嗅出来。
 */
(() => {
  if (window.__sniffdlBadge) return;      // 防重复注入
  window.__sniffdlBadge = true;

  const FB = { accent: "#3E6FD0", card: "#FFFFFF", ink: "#1F2A44", inkL: "#5C6880", line: "#D7E0F0", bg: "#EEF3FB" };
  let th = Object.assign({}, FB);
  let badge = null, panel = null, opened = false;
  let sniffTask = null, sniffTimer = null, statusTimer = null, scanTimer = null;
  let items = [], sniffed = false, placedVideo = null, countEl = null;
  // 后台预嗅探：浮标只在"桌面端真嗅到可下载内容"时才出现，绝不凭 <video> 元素或站点名硬显
  let curUrl = "";
  let preState = "idle", preItems = [], preTask = null, preTimer = null, preTriedUrl = "";

  const isHttp = (u) => !!u && /^https?:/i.test(u);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  function applyTheme() {
    const t = window.__sniffdlTheme || {};
    const p = t.palette || {};
    th = {
      accent: t.accent || p.pink || FB.accent,
      card: p.card || FB.card,
      ink: p.ink || FB.ink,
      inkL: p.ink_l || FB.inkL,
      line: p.line || FB.line,
      bg: p.bg || FB.bg,
      char: t.char || "",
      cta: t.cta || {},
    };
  }

  // ---- 媒体探测（只判断"值不值得让桌面端嗅"，绝不直接决定浮标显隐）----
  // 播放页 URL 特征
  function isWatchUrl(u) {
    return /\/watch|v=[\w-]{6,}|\/shorts\/|\/live\/|\/embed\/|\/video\/|video\.html|\/p\/\d|\/part\/|\/av\d+|\/bv[0-9A-Za-z]+|\/ep\d+|\/mo\/|bangumi\/(av|ep|ss)|\/detail\/|\/show\/|\/movie\/|\/anime\/|\/tv\/|\/play\//i.test(u || "");
  }
  // 列表 / 搜索 / 首页 / 频道 等"非单一视频"页特征——这类页面不自动嗅探、不弹浮标
  function isListingUrl(u) {
    u = u || "";
    return /\/results|search_query|\/search(\/|\?|#|$)|\/explore|\/feed\/|\/home(\/|\?|#|$)|\/browse\/|\/channel\/|\/c\/[\w-]+|\/user\/[\w-]+|\/hashtag\/|\/tag\/|\/topic\/|\/list\/|list=[\w-]+|page=\d+|\/ranking|\/category|\/collection|\/space\/\d+|\/fav(list)?\.?$/i.test(u) ||
      /bilibili\.com\/(video\/)?$/.test(u) || /douyin\.com\/?$/.test(u) || /www\.(youtube\.com|bilibili\.com)\/?$/.test(u);
  }
  // 真正在播（或已就绪）的主播放器：<video> 够大、可见，且正在播放 / 有真实媒体源
  function primaryPlayer() {
    let best = null, bestArea = 0;
    try {
      const vids = Array.prototype.slice.call(document.querySelectorAll("video"));
      for (const v of vids) {
        const r = v.getBoundingClientRect();
        if (r.width < 1 || r.top >= innerHeight || r.bottom <= 0) continue;
        const vw = Math.min(r.width, innerWidth), vh = Math.min(r.height, innerHeight);
        const area = vw * vh;
        const bigEnough = r.width >= 360 && r.height >= 200 && area >= 0.10 * innerWidth * innerHeight;
        if (!bigEnough) continue;
        let src = "";
        try { src = v.currentSrc || v.src || ""; } catch (_) {}
        const playing = !v.paused && !v.ended;
        const realSrc = !!src && !/^blob:/i.test(src) && /\.(m3u8|mpd|mp4|webm|flv|ts|m4s|mov|mkv)(\?|#|$)/i.test(src);
        // MSE 站点（YouTube 等）src 是 blob，用 readyState 判断是否真在加载播放
        const mseReady = /^blob:/i.test(src) && v.readyState >= 2;
        if (!(playing || realSrc || mseReady)) continue;
        if (area > bestArea) { bestArea = area; best = v; }
      }
    } catch (_) { best = null; }
    return best;
  }
  // 是否值得让桌面端后台嗅一次：有真实主播放器，或是明确的播放页；但列表/搜索页一律排除
  function isCandidate() {
    const u = location.href;
    if (isListingUrl(u)) return false;
    if (primaryPlayer()) return true;
    if (isWatchUrl(u)) return true;
    // 非列表页里出现真正的流媒体资源请求，也算候选
    try {
      return performance.getEntriesByType("resource").some((e) =>
        /\.(m3u8|mpd|webm|flv|mkv|ts)(\?|#|$)/i.test(e.name) || /playlist\.m3u8|\.mpd(\?|$)/i.test(e.name));
    } catch (_) { return false; }
  }

  function largestVideo() { return primaryPlayer(); }

  // ---- 浮标 ----
  function makeBadge() {
    if (badge) return;
    badge = document.createElement("div");
    badge.id = "sniffdl-badge";
    badge.style.cssText =
      "position:fixed;z-index:2147483645;display:none;align-items:center;gap:6px;" +
      "padding:7px 12px;border-radius:22px;cursor:pointer;user-select:none;" +
      "font:600 12.5px/1 'Microsoft YaHei UI','Segoe UI',system-ui,sans-serif;color:#fff;" +
      "box-shadow:0 6px 18px -6px rgba(0,0,0,.4);border:1px solid rgba(255,255,255,.35);" +
      "transition:transform .12s ease, box-shadow .12s ease;";
    badge.innerHTML =
      '<span style="font-size:14px;line-height:1">⬇</span>' +
      '<span class="sd-label">嗅探·下载</span>' +
      '<span class="sd-cnt" style="display:none;min-width:16px;height:16px;line-height:16px;' +
      'text-align:center;border-radius:9px;background:rgba(255,255,255,.9);padding:0 4px;font-size:11px"></span>';
    countEl = badge.querySelector(".sd-cnt");
    badge.addEventListener("mouseenter", () => { badge.style.transform = "translateY(-1px)"; });
    badge.addEventListener("mouseleave", () => { badge.style.transform = "translateY(0)"; });
    badge.addEventListener("click", (e) => { e.stopPropagation(); toggle(); });
    document.documentElement.appendChild(badge);
  }

  function positionBadge() {
    if (!badge) return;
    const r = badge.getBoundingClientRect();
    const w = r.width || 120, h = r.height || 34;
    placedVideo = largestVideo();
    let left, top;
    if (placedVideo) {
      const vr = placedVideo.getBoundingClientRect();
      left = vr.right - w - 10;
      top = vr.top + 10;
      if (top + h > innerHeight - 8) top = innerHeight - h - 8;
      if (top < 8) top = 8;
    } else {
      left = innerWidth - w - 14;
      top = 14;
    }
    if (left < 8) left = 8;
    if (left + w > innerWidth - 8) left = Math.max(8, innerWidth - w - 8);
    badge.style.left = left + "px";
    badge.style.top = top + "px";
  }

  function showBadge() {
    makeBadge();
    badge.style.background = "linear-gradient(135deg," + th.accent + "," + th.accent + "dd)";
    const lab = badge.querySelector(".sd-label");
    if (lab) lab.textContent = (th.cta && th.cta.badge) || "嗅探·下载";
    positionBadge();
    badge.style.display = "flex";
  }
  function hideBadge() { if (badge) badge.style.display = "none"; }

  function setCount(n) {
    if (!countEl) return;
    if (n > 0) { countEl.textContent = n > 99 ? "99+" : n; countEl.style.display = "inline-block"; }
    else countEl.style.display = "none";
  }

  // ---- 面板 ----
  function panelCss() {
    return "position:fixed;z-index:2147483645;display:none;width:344px;max-width:94vw;" +
      "border-radius:16px;overflow:hidden;font:13px/1.5 'Microsoft YaHei UI','Segoe UI',system-ui,sans-serif;" +
      "color:" + th.ink + ";background:" + th.card + ";border:1px solid " + th.line + ";" +
      "box-shadow:0 18px 48px -12px rgba(0,0,0,.42);";
  }

  function buildPanel() {
    if (panel) return panel;
    panel = document.createElement("div");
    panel.id = "sniffdl-panel";
    panel.style.cssText = panelCss();
    panel.addEventListener("mousedown", (e) => e.stopPropagation());
    document.documentElement.appendChild(panel);
    return panel;
  }

  function renderPanel(state, msg) {
    const p = buildPanel();
    const head =
      `<div style="display:flex;align-items:center;gap:8px;padding:12px 14px;` +
      `background:linear-gradient(135deg,${th.accent},${th.accent}cc);color:#fff;">` +
      `<span style="font-size:16px;line-height:1">⬇</span>` +
      `<div style="font-weight:700;flex:1">${esc(th.char ? th.char + " 的嗅探篮" : "嗅探篮")}</div>` +
      `<span class="sd-x" title="收起" style="cursor:pointer;font-size:18px;line-height:1;padding:0 2px;opacity:.9">×</span>` +
      `</div>`;
    let body = "";
    if (state === "sniffing") {
      body = '<div style="padding:22px 16px;text-align:center;color:' + th.inkL + '">' +
        '<div style="font-size:26px;margin-bottom:8px">🔍</div>看板娘正在深度嗅探本页…<br>' +
        '<span style="font-size:11.5px">' + esc(msg || "") + "</span></div>";
    } else if (state === "error") {
      body = '<div style="padding:22px 16px;text-align:center;color:#c0455f">' +
        "嗅探失败：" + esc(msg || "") + "</div>";
    } else if (state === "empty") {
      body = '<div style="padding:22px 16px;text-align:center;color:' + th.inkL + '">' +
        '<div style="font-size:26px;margin-bottom:8px">🐾</div>这一页没嗅到可下载的视频。<br>' +
        '<span style="font-size:11.5px">试试播放一下、或换到视频播放页再点。</span></div>';
    } else if (state === "list") {
      const rows = items.map((it, i) =>
        '<div class="sd-row" data-i="' + i + '" style="display:flex;align-items:center;gap:8px;' +
        "padding:9px 14px;border-top:1px solid " + th.line + '">' +
        '<div style="flex:1;min-width:0">' +
        '<div style="font-weight:600;font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + esc(it.title || ("媒体 " + (i + 1))) + "</div>" +
        '<div style="font-size:11px;color:' + th.inkL + ';white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' +
        [it.res && it.res !== "-" ? it.res : "", it.ext && it.ext !== "-" ? it.ext : "", it.size_s && it.size_s !== "—" ? it.size_s : "", it.how].filter(Boolean).map(esc).join(" · ") +
        "</div></div>" +
        '<button class="sd-dl" data-i="' + i + '" style="flex:none;cursor:pointer;border:none;border-radius:8px;' +
        "padding:6px 10px;font:600 12px inherit;color:#fff;font-family:inherit;" +
        "background:linear-gradient(135deg," + th.accent + "," + th.accent + "cc)\">下载</button>" +
        "</div>").join("");
      body = '<div style="max-height:300px;overflow:auto">' + rows + "</div>" +
        '<div style="display:flex;gap:8px;padding:10px 14px;border-top:1px solid ' + th.line + '">' +
        '<button class="sd-all" style="flex:1;cursor:pointer;border:none;border-radius:9px;padding:8px;' +
        "font:700 12.5px inherit;color:#fff;font-family:inherit;background:linear-gradient(135deg," + th.accent + "," + th.accent + "cc)\">整页全部下载（" + items.length + "）</button>" +
        '<button class="sd-re" title="重新嗅探" style="flex:none;cursor:pointer;border:1px solid ' + th.line + ";background:" + th.bg + ";color:" + th.ink + ";border-radius:9px;padding:8px 10px;font:600 12.5px inherit;font-family:inherit\">↻</button>" +
        "</div>";
    }
    const strip = '<div class="sd-live" style="padding:8px 14px;font-size:11.5px;color:' + th.inkL + ';border-top:1px solid ' + th.line + '">' +
      "看板娘待命中～</div>";
    p.innerHTML = head + body + strip;
    p.querySelector(".sd-x").addEventListener("click", closePanel);
    if (state === "list") {
      p.querySelectorAll(".sd-dl").forEach((b) => b.addEventListener("click", () => dlOne(+b.getAttribute("data-i"), b)));
      const all = p.querySelector(".sd-all");
      if (all) all.addEventListener("click", () => dlAll(all));
      const re = p.querySelector(".sd-re");
      if (re) re.addEventListener("click", () => startSniff(true));
    }
  }

  function placePanel() {
    if (!panel) return;
    const r = panel.getBoundingClientRect();
    const w = r.width || 344, h = r.height || 200;
    let left = innerWidth - w - 14, top = 54;
    if (placedVideo) {
      const vr = placedVideo.getBoundingClientRect();
      left = vr.right - w - 10; top = vr.top + 44;
    }
    if (left < 8) left = 8;
    if (left + w > innerWidth - 8) left = Math.max(8, innerWidth - w - 8);
    if (top + h > innerHeight - 8) top = Math.max(8, innerHeight - h - 8);
    panel.style.left = left + "px";
    panel.style.top = top + "px";
  }

  function openPanel() {
    opened = true;
    if (preState === "done") { items = preItems.slice(); sniffed = true; }
    else if (preState === "sniffing" && preTask) { items = []; sniffed = false; sniffTask = preTask; }
    else { items = []; sniffed = false; }
    renderPanel(sniffed ? (items.length ? "list" : "empty") : "sniffing",
                sniffed ? "" : "交给桌面端猫爪引擎…");
    panel.style.display = "block";
    placePanel();
    if (!sniffed) { if (sniffTask) pollSniff(0); else startSniff(false); }
    startLive();
  }
  function closePanel() {
    opened = false;
    if (panel) panel.style.display = "none";
    stopSniff(); stopLive();
  }
  function toggle() { if (opened) closePanel(); else openPanel(); }

  // ---- 嗅探流程 ----
  function startSniff(force) {
    sniffed = false;
    renderPanel("sniffing", "交给桌面端猫爪引擎…");
    placePanel();
    try {
      chrome.runtime.sendMessage({ type: "sniff_start", url: location.href, scope: "all" }, (resp) => {
        if (resp && resp.ok && resp.taskId) { sniffTask = resp.taskId; pollSniff(0); }
        else { renderPanel("error", (resp && resp.error) || "无法启动嗅探"); placePanel(); }
      });
    } catch (e) { renderPanel("error", e.message || String(e)); placePanel(); }
  }

  function pollSniff(n) {
    if (!opened || !sniffTask) return;
    try {
      chrome.runtime.sendMessage({ type: "sniff_result", taskId: sniffTask }, (resp) => {
        if (!opened) return;
        const r = resp || {};
        if (r.status === "done") {
          items = r.items || [];
          sniffed = true;
          // 回填后台预嗅探缓存，保证浮标与面板数据一致
          preState = "done"; preItems = items.slice(); preTriedUrl = location.href;
          setCount(items.length);
          if (items.length) { showBadge(); } else { hideBadge(); }
          renderPanel(items.length ? "list" : "empty");
          placePanel();
          return;
        }
        if (r.status === "error") {
          renderPanel("error", r.error || "嗅探出错"); placePanel(); return;
        }
        // running / not_found → 继续轮询
        const sec = Math.round((r.elapsed || (n * 1.5)));
        renderPanel("sniffing", "已嗅探 " + sec + " 秒，请稍候 ♪");
        placePanel();
        sniffTimer = setTimeout(() => pollSniff(n + 1), 1500);
      });
    } catch (e) { renderPanel("error", e.message || String(e)); placePanel(); }
  }
  function stopSniff() { if (sniffTimer) { clearTimeout(sniffTimer); sniffTimer = null; } }

  // ---- 下载 ----
  function dlOne(i, btn) {
    const it = items[i]; if (!it) return;
    if (btn) { btn.disabled = true; btn.textContent = "打开中…"; btn.style.opacity = ".75"; }
    // 单条：网页正中央弹 IDM 式确认卡（分类/另存为/保存到/浏览…），桌面不再弹原生窗
    if (window.__sniffdlOpenCard) {
      try {
        window.__sniffdlOpenCard({
          url: it.url, filename: it.title || "", referer: it.page || location.href,
          size: it.size || 0, ext: it.ext || "", source: "badge",
        });
        return;
      } catch (e) {}
    }
    try {
      chrome.runtime.sendMessage({
        type: "badge_dl", url: it.url, filename: it.title || "", referer: it.page || location.href, size: it.size || 0,
      }, () => {});
    } catch (e) {}
  }
  function dlAll(btn) {
    if (btn) { btn.disabled = true; btn.textContent = "整页已交给看板娘 ♪"; btn.style.opacity = ".8"; }
    // 批量：不逐个弹网页卡（会刷屏），静默转交桌面后台下，完成通知一声
    try {
      chrome.runtime.sendMessage({
        type: "badge_batch",
        items: items.map((it) => ({ url: it.url, filename: it.title || "", referer: it.page || location.href })),
      }, (resp) => {
        const n = (resp && resp.n) || items.length;
        try { if (window.__sniffdlToast) window.__sniffdlToast("已交给看板娘批量下载（" + n + " 项），完成后通知你 ♪", true); } catch (e) {}
      });
    } catch (e) {}
  }

  // ---- 实时下载状态条 ----
  function startLive() {
    stopLive();
    const tick = () => {
      if (!opened) return;
      try {
        chrome.runtime.sendMessage({ type: "tasks" }, (resp) => {
          if (!opened) return;
          const el = panel && panel.querySelector(".sd-live");
          if (!el) return;
          const act = (resp && resp.active) || [];
          if (!act.length) { el.textContent = "看板娘待命中～"; return; }
          const a = act[0];
          let pct = "";
          if (a.totalLength && a.totalLength !== "0") pct = " " + Math.floor((+a.downloadedLength / +a.totalLength) * 100) + "%";
          const nm = (a.name || a.gid || "").slice(0, 16);
          el.textContent = "看板娘正在搬 " + act.length + " 项：" + nm + pct;
        });
      } catch (e) {}
    };
    tick();
    statusTimer = setInterval(tick, 2000);
  }
  function stopLive() { if (statusTimer) { clearInterval(statusTimer); statusTimer = null; } }

  // ---- 后台预嗅探（浮标显隐的唯一依据：桌面端真嗅到东西才亮）----
  function resetPre(reason) {
    if (preTimer) { clearTimeout(preTimer); preTimer = null; }
    preState = "idle"; preItems = []; preTask = null; preTriedUrl = "";
  }
  function ensurePreSniff() {
    if (preTriedUrl === location.href && (preState === "sniffing" || preState === "done")) return;
    preTriedUrl = location.href;
    preState = "sniffing"; preItems = [];
    try {
      chrome.runtime.sendMessage({ type: "sniff_start", url: location.href, scope: "all" }, (resp) => {
        if (resp && resp.ok && resp.taskId) { preTask = resp.taskId; pollPre(0); }
        else { preState = "done"; preItems = []; }
      });
    } catch (e) { preState = "done"; }
  }
  function pollPre(n) {
    if (preState !== "sniffing" || !preTask || preTriedUrl !== location.href) return;
    try {
      chrome.runtime.sendMessage({ type: "sniff_result", taskId: preTask }, (resp) => {
        if (preTriedUrl !== location.href) return;   // 页面已切换，丢弃旧结果
        const r = resp || {};
        if (r.status === "done") {
          preItems = r.items || []; preState = "done";
          if (preItems.length && !opened) { showBadge(); setCount(preItems.length); }
          else if (!preItems.length && !opened) { hideBadge(); }
          return;
        }
        if (r.status === "error") { preState = "done"; preItems = []; if (!opened) hideBadge(); return; }
        preTimer = setTimeout(() => pollPre(n + 1), 1600);
      });
    } catch (e) { preState = "done"; }
  }

  // ---- 扫描调度 ----
  let scanBusy = false;
  function scan() {
    if (scanBusy) return;
    scanBusy = true;
    try {
      // URL 变化（SPA 导航，如 YouTube 搜索页↔播放页）→ 重置，重新判定
      if (location.href !== curUrl) { curUrl = location.href; resetPre("nav"); if (!opened) hideBadge(); }
      if (opened) { if (placedVideo) placePanel(); return; }   // 面板开着时不打断
      // 只有"真嗅到可下载内容"才显示浮标；候选页先静默后台嗅一次
      if (!isCandidate()) { hideBadge(); return; }
      ensurePreSniff();
      if (preState === "done" && preItems.length) { showBadge(); setCount(preItems.length); }
      else hideBadge();
    } finally { scanBusy = false; }
  }
  let posRaf = 0;
  function onMove() {
    if (posRaf) return;
    posRaf = requestAnimationFrame(() => {
      posRaf = 0;
      if (badge && badge.style.display !== "none") positionBadge();
      if (opened) placePanel();
    });
  }

  function init() {
    applyTheme();
    if (badge) { badge.style.background = "linear-gradient(135deg," + th.accent + "," + th.accent + "dd)"; }
    scan();
    if (scanTimer) clearInterval(scanTimer);
    scanTimer = setInterval(scan, 3000);
  }

  // ---- 桌面端没开时给个明确提示 ----
  // IDM 装的是后台常驻服务，浏览器永远连得上；SniffDL 不是 —— 程序一关，浮标就静默消失，
  // 用户只会以为"扩展坏了"（真出过这回事）。所以探不到 RPC 时必须说出来，且只在候选页说，
  // 不到处盖章。
  let offChip = null, offBusy = false;
  function ensureOffline() {
    if (offChip) return offChip;
    offChip = document.createElement("div");
    offChip.id = "sniffdl-offline";
    offChip.style.cssText =
      "position:fixed;z-index:2147483644;top:12px;right:12px;display:flex;align-items:center;" +
      "gap:7px;padding:7px 13px;border-radius:20px;cursor:pointer;user-select:none;" +
      "font:600 12.5px/1 'Microsoft YaHei UI','Segoe UI',system-ui,sans-serif;" +
      "color:#868D97;background:rgba(255,255,255,.92);border:1px solid #E4E7EB;" +
      "box-shadow:0 6px 20px rgba(20,25,35,.10);transition:opacity .25s;";
    offChip.innerHTML = '<span style="width:7px;height:7px;border-radius:50%;background:#C2C8D0;' +
      'display:inline-block"></span><span class="sd-off-t">SniffDL 未运行 · 点此重试</span>';
    offChip.addEventListener("click", retryOffline);
    document.documentElement.appendChild(offChip);
    setTimeout(() => { if (offChip) offChip.style.opacity = "0.4"; }, 6000);
    offChip.addEventListener("mouseenter", () => { if (offChip) offChip.style.opacity = "1"; });
    return offChip;
  }
  function offText(t) {
    const el = offChip && offChip.querySelector(".sd-off-t");
    if (el) el.textContent = t;
  }
  function hideOffline() { if (offChip) offChip.style.display = "none"; }
  function retryOffline() {
    if (offBusy) return;
    offBusy = true;
    offText("正在重试…");
    try {
      chrome.runtime.sendMessage({ type: "theme" }, (r) => {
        offBusy = false;
        const ok = !!(r && r.ok && r.theme);
        window.__sniffdlRpcOk = ok;
        if (ok) {
          window.__sniffdlTheme = r.theme;
          hideOffline();
          try { document.dispatchEvent(new CustomEvent("sniffdl-theme-ready", { detail: { ok: true } })); } catch (_) {}
          init();
        } else {
          offText("还是没连上：请先打开桌面版 SniffDL（设置 → 本地下载服务 RPC）");
          if (offChip) offChip.style.opacity = "1";
        }
      });
    } catch (e) {
      offBusy = false;
      offText("连不上桌面端：请先打开 SniffDL");
    }
  }

  document.addEventListener("sniffdl-theme-ready", (e) => {
    const ok = !!(e && e.detail && e.detail.ok);
    if (ok) {
      hideOffline();
      init();
    } else if (isCandidate()) {
      ensureOffline();
    }
  });

  // 供 popup「嗅探本页媒体」/ 右键「页面」调用：在本页正中央打开嗅探篮并（重）跑桌面端深度嗅探
  try {
    chrome.runtime.onMessage.addListener((msg, _s, sendResp) => {
      if (msg && msg.type === "open_sniff_panel") {
        applyTheme();
        if (!opened) openPanel();          // 未开 → 打开（内部若未嗅会自动 startSniff）
        else startSniff(true);             // 已开着 → 强制重嗅
        sendResp({ ok: true });
        return true;
      }
      return false;
    });
  } catch (e) {}

  window.addEventListener("scroll", onMove, true);
  window.addEventListener("resize", onMove, true);
  try {
    const mo = new MutationObserver(() => { /* 结构变化时下一轮 scan 会捕捉 */ });
    mo.observe(document.documentElement, { childList: true, subtree: true });
  } catch (e) {}

  // 若主题早已就绪（事件已发过），补一次
  if (window.__sniffdlRpcOk) init();
})();

/* SniffDL 网页中央「下载文件信息 / 下载进度」卡（content script）
 * 用户明确要：接管下载别再跳回桌面软件，全部在网页正中央完成——
 *   · 单条：先弹 IDM 式「下载文件信息」卡（URL/分类＋新建/让该分类用此路径/另存为/保存到＋浏览/描述/右侧文件图标+大号大小），
 *           点「开始下载」→ 桌面静默起下载（不弹任何桌面窗）→ 原地换成实时进度卡（进度条/速度/已下载/剩余/续传/暂停/取消），
 *           完成出 ✓ +「打开文件夹」(定位高亮文件)+「打开文件」，失败出 ✗ + 原因 + 重试。
 *   · 批量：不走这里（避免刷屏），由浮标 badge_batch 静默转交，桌面后台下，完成通知一声。
 * 桌面真搬运，页面只负责呈现 + 轮询 aria2.getDownloadResult。所有 RPC 经 background 的 {type:"rpc"} 透传。
 */
(() => {
  if (window.__sniffdlCard) return;
  window.__sniffdlCard = true;

  function T() {
    const t = window.__sniffdlTheme || {};
    const p = t.palette || {};
    return {
      accent: t.accent || p.pink || p.purple || p.gold || "#3E6FD0",
      accent2: p.pink2 || p.plum || p.navy || t.accent || "#38C6D9",
      card: p.card || "#FFFFFF", ink: p.ink || "#1F2A44", inkL: p.ink_l || "#5C6880",
      line: p.line || "#D7E0F0", bg: p.bg || "#EEF3FB",
      char: t.char || "", role: t.role || "", motif: t.motif || "", avatar: t.avatar || "",
    };
  }
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  function rpc(method, params) {
    return new Promise((res) => {
      try {
        chrome.runtime.sendMessage({ type: "rpc", method, params: params || [] }, (r) => {
          res(r || { ok: false, error: "no response" });
        });
      } catch (e) { res({ ok: false, error: String(e) }); }
    });
  }
  function fmtB(n) {
    n = Number(n) || 0; if (n <= 0) return "未知";
    const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n + " B" : n.toFixed(1) + " " + u[i]);
  }
  function fmtSpd(n) { n = Number(n) || 0; return n <= 0 ? "—" : fmtB(n) + "/s"; }
  function fmtEta(sec) {
    sec = Math.max(0, Math.round(Number(sec) || 0));
    if (!isFinite(sec) || sec <= 0) return "—";
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    const z = (x) => (x < 10 ? "0" + x : "" + x);
    return h > 0 ? (h + ":" + z(m) + ":" + z(s)) : (m + ":" + z(s));
  }
  function guessExt(name, url) {
    let e = (String(name || "").match(/\.([A-Za-z0-9]{1,8})$/) || [])[1];
    if (!e && url) e = (String(url).match(/\.([A-Za-z0-9]{1,8})(\?|#|$)/i) || [])[1];
    return (e || "").toLowerCase();
  }
  function nameFromUrl(url) {
    try {
      let p = String(url || "").split(/[?#]/)[0];
      p = decodeURIComponent(p.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || "");
      return p.trim();
    } catch (e) { return ""; }
  }

  // ---- 通用模态容器（遮罩 + 居中卡）----
  let host = null;
  function mount() {
    unmount();
    host = document.createElement("div");
    host.style.cssText = "position:fixed;inset:0;z-index:2147483647;display:flex;" +
      "align-items:center;justify-content:center;background:rgba(20,10,16,.42);" +
      "font:13px/1.5 'Microsoft YaHei UI','Segoe UI',system-ui,sans-serif;";
    document.documentElement.appendChild(host);
    return host;
  }
  function unmount() {
    if (host) { try { host.remove(); } catch (e) {} host = null; }
  }
  function cardShell(th) {
    const wrap = document.createElement("div");
    wrap.style.cssText = "width:468px;max-width:94vw;border-radius:16px;overflow:hidden;" +
      "background:" + th.card + ";color:" + th.ink + ";border:1px solid " + th.line + ";" +
      "box-shadow:0 24px 70px -14px rgba(0,0,0,.5);";
    wrap.addEventListener("mousedown", (e) => e.stopPropagation());
    return wrap;
  }
  function headBar(th, title, sub, onClose, onMin) {
    const h = document.createElement("div");
    h.style.cssText = "display:flex;align-items:center;gap:10px;padding:13px 16px;" +
      "background:linear-gradient(135deg," + th.accent + "," + th.accent + "cc);color:#fff;";
    let avHtml = th.avatar
      ? '<img src="data:image/png;base64,' + th.avatar + '" style="width:38px;height:38px;border-radius:50%;background:rgba(255,255,255,.25);flex:none">'
      : '<span style="font-size:22px;flex:none">🐾</span>';
    const minBtn = onMin
      ? '<span class="sd-mn" title="最小化（下载继续在后台）" style="cursor:pointer;font-size:18px;line-height:1;padding:0 4px;opacity:.92">–</span>'
      : "";
    h.innerHTML = avHtml +
      '<div style="flex:1;min-width:0"><div class="sd-hd-t" style="font-weight:700;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' +
      esc(title) + '</div><div class="sd-hd-s" style="font-size:11.5px;opacity:.9;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' +
      esc(sub || "") + "</div></div>" +
      minBtn +
      '<span class="sd-cx" title="关闭" style="cursor:pointer;font-size:20px;line-height:1;padding:0 2px;opacity:.92">×</span>';
    if (onMin) h.querySelector(".sd-mn").addEventListener("click", onMin);
    h.querySelector(".sd-cx").addEventListener("click", onClose);
    return h;
  }
  function fieldRow(th, label, ctrlEl, hint) {
    const row = document.createElement("div");
    row.style.cssText = "display:flex;align-items:center;gap:10px;margin:9px 0;";
    const lab = document.createElement("div");
    lab.textContent = label;
    lab.style.cssText = "flex:none;width:64px;font-weight:700;font-size:12.5px;color:" + th.ink + ";";
    row.appendChild(lab);
    row.appendChild(ctrlEl);
    if (hint) { const hh = document.createElement("div"); hh.textContent = hint; hh.style.cssText = "flex:none;font-size:11px;color:" + th.inkL + ";"; row.appendChild(hh); }
    return row;
  }
  function inputEl(th, val, readonly) {
    const i = document.createElement("input");
    i.value = val == null ? "" : val;
    i.readOnly = !!readonly;
    i.style.cssText = "flex:1;min-width:0;box-sizing:border-box;border:1px solid " + th.line +
      ";border-radius:8px;padding:6px 9px;font:13px inherit;font-family:inherit;color:" + th.ink +
      ";background:" + (readonly ? th.bg : th.card) + ";outline:none;";
    return i;
  }
  function btnEl(th, text, kind) {
    const b = document.createElement("button");
    b.textContent = text;
    const prim = kind === "primary";
    b.style.cssText = "cursor:pointer;border:" + (prim ? "none" : "1px solid " + th.line) +
      ";border-radius:9px;padding:9px 14px;font:700 13px inherit;font-family:inherit;" +
      "color:" + (prim ? "#fff" : th.ink) + ";" +
      (prim ? "background:linear-gradient(135deg," + th.accent + "," + th.accent + "cc);" : "background:" + th.bg + ";");
    return b;
  }
  function glyphEl(th, ext, sizeTxt) {
    const box = document.createElement("div");
    box.style.cssText = "flex:none;width:132px;border-left:1px solid " + th.line + ";padding:16px 12px;" +
      "display:flex;flex-direction:column;align-items:center;gap:4px;background:" + th.bg + ";";
    const g = document.createElement("div");
    const lab = (ext || "?").toUpperCase().slice(0, 5);
    g.style.cssText = "position:relative;width:74px;height:92px;border:2px solid " + th.accent +
      ";border-radius:8px;background:" + th.card + ";display:flex;align-items:center;justify-content:center;";
    g.innerHTML = '<span style="color:' + th.accent + ';font-weight:800;font-size:15px">' + esc(lab) + "</span>" +
      '<span style="position:absolute;top:-2px;right:-2px;width:16px;height:16px;background:' + th.bg + ';border-top:2px solid ' + th.accent + ';border-right:2px solid ' + th.accent + ';border-bottom-left-radius:6px"></span>';
    const sz = document.createElement("div"); sz.textContent = sizeTxt;
    sz.style.cssText = "font-weight:800;font-size:16px;color:" + th.ink + ";margin-top:6px;";
    const ty = document.createElement("div"); ty.textContent = ext ? ("类型 ." + ext) : "类型 未知";
    ty.style.cssText = "font-size:11px;color:" + th.inkL + ";";
    box.appendChild(g); box.appendChild(sz); box.appendChild(ty);
    box._sz = sz; box._ty = ty;
    return box;
  }

  function toast(th, msg, ok) {
    const t = document.createElement("div");
    t.textContent = msg;
    t.style.cssText = "position:fixed;left:50%;top:22px;transform:translateX(-50%);z-index:2147483647;" +
      "padding:10px 16px;border-radius:22px;color:#fff;font:600 13px 'Microsoft YaHei UI',system-ui,sans-serif;" +
      "background:linear-gradient(135deg," + (ok === false ? "#c0455f" : th.accent) + "," + (ok === false ? "#c0455f" : th.accent) + "cc);" +
      "box-shadow:0 8px 22px -8px rgba(0,0,0,.4);";
    document.documentElement.appendChild(t);
    setTimeout(() => { try { t.remove(); } catch (e) {} }, 2600);
  }
  window.__sniffdlToast = (m, ok) => toast(T(), m, ok);

  // ---- 网页内文件夹浏览器（不切窗口，逐层选保存位置）----
  function openDirBrowser(startPath, onPick) {
    const th = T();
    function miniBtn(text) {
      const b = document.createElement("button");
      b.textContent = text;
      b.style.cssText = "cursor:pointer;border:1px solid " + th.line + ";border-radius:8px;" +
        "padding:6px 10px;font:600 12px inherit;font-family:inherit;color:" + th.ink +
        ";background:" + th.bg + ";white-space:nowrap;";
      return b;
    }
    const ov = document.createElement("div");
    ov.style.cssText = "position:fixed;inset:0;z-index:2147483647;background:rgba(20,10,20,.45);" +
      "display:flex;align-items:center;justify-content:center;font-family:'Microsoft YaHei UI',system-ui,sans-serif;";
    const box = document.createElement("div");
    box.style.cssText = "width:520px;max-width:94vw;height:62vh;max-height:80vh;display:flex;flex-direction:column;" +
      "background:" + th.card + ";color:" + th.ink + ";border-radius:14px;overflow:hidden;border:1px solid " + th.line +
      ";box-shadow:0 24px 70px -14px rgba(0,0,0,.5);";
    const hd = document.createElement("div");
    hd.style.cssText = "display:flex;align-items:center;gap:8px;padding:12px 14px;color:#fff;font-weight:700;" +
      "background:linear-gradient(135deg," + th.accent + "," + th.accent + "cc);";
    hd.innerHTML = "<span style='flex:1'>📁 选择保存位置</span>";
    const x = document.createElement("span");
    x.textContent = "×"; x.style.cssText = "cursor:pointer;font-size:20px;line-height:1;padding:0 4px";
    hd.appendChild(x);
    const tb = document.createElement("div");
    tb.style.cssText = "display:flex;gap:6px;align-items:center;padding:8px 12px;border-bottom:1px solid " + th.line + ";";
    const upBtn = miniBtn("⬆ 上级"), rootBtn = miniBtn("💽 我的电脑");
    const pathIn = document.createElement("input");
    pathIn.style.cssText = "flex:1;min-width:0;border:1px solid " + th.line + ";border-radius:8px;padding:6px 8px;" +
      "font:12.5px inherit;font-family:inherit;background:" + th.bg + ";color:" + th.ink + ";outline:none;";
    tb.appendChild(upBtn); tb.appendChild(rootBtn); tb.appendChild(pathIn);
    const list = document.createElement("div");
    list.style.cssText = "flex:1;overflow:auto;padding:6px;";
    const ft = document.createElement("div");
    ft.style.cssText = "display:flex;gap:8px;align-items:center;padding:10px 12px;border-top:1px solid " + th.line + ";";
    const nativeBtn = miniBtn("用系统选择器…");
    const spacer = document.createElement("span"); spacer.style.cssText = "flex:1";
    const pickBtn = btnEl(th, "保存到此处", "primary");
    ft.appendChild(nativeBtn); ft.appendChild(spacer); ft.appendChild(pickBtn);
    box.appendChild(hd); box.appendChild(tb); box.appendChild(list); box.appendChild(ft);
    ov.appendChild(box);
    document.documentElement.appendChild(ov);

    let cur = { path: "", parent: "", dirs: [], drives: [] };
    function close() { try { ov.remove(); } catch (e) {} }
    function render() {
      pathIn.value = cur.path || "";
      list.innerHTML = "";
      const atRoot = cur.path === "";
      const items = atRoot ? cur.drives : cur.dirs;
      const cap = document.createElement("div");
      cap.style.cssText = "padding:8px 10px;font-size:12px;color:" + th.inkL + ";";
      cap.textContent = atRoot ? "选择盘符：" : ("当前目录：" + cur.path);
      list.appendChild(cap);
      if (!items.length) {
        const e = document.createElement("div");
        e.style.cssText = "padding:14px;color:" + th.inkL + ";";
        e.textContent = atRoot ? "未发现可用盘符" : "（此目录没有子文件夹）";
        list.appendChild(e);
      }
      items.forEach((nm) => {
        const full = atRoot ? nm : (cur.path.replace(/[\\/]+$/, "") + "\\" + nm);
        const row = document.createElement("div");
        row.style.cssText = "display:flex;align-items:center;gap:8px;padding:9px 10px;border-radius:8px;cursor:pointer;font-size:13px;";
        row.onmouseenter = () => { row.style.background = th.bg; };
        row.onmouseleave = () => { row.style.background = ""; };
        row.innerHTML = "<span>📁</span><span style='flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap'>" +
          esc(nm) + "</span><span style='color:" + th.inkL + ";font-size:12px'>›</span>";
        row.onclick = () => go(full);
        list.appendChild(row);
      });
      pickBtn.style.opacity = cur.path ? "1" : ".45";
    }
    function go(path) {
      list.innerHTML = "<div style='padding:14px;color:" + th.inkL + "'>加载中…</div>";
      rpc("sniffdl.listDirs", [path]).then((r) => {
        const d = (r && r.result) || {};
        cur = { path: d.path || "", parent: d.parent || "", dirs: d.dirs || [], drives: d.drives || [] };
        render();
      }).catch(() => { list.innerHTML = "<div style='padding:14px;color:#c0455f'>读取目录失败</div>"; });
    }
    x.onclick = close;
    upBtn.onclick = () => { if (cur.path !== "") go(cur.parent || ""); };
    rootBtn.onclick = () => go("");
    pathIn.onkeydown = (e) => { if (e.key === "Enter") { const v = pathIn.value.trim(); if (v) go(v); } };
    nativeBtn.onclick = () => {
      nativeBtn.disabled = true; nativeBtn.textContent = "在软件里选…";
      rpc("sniffdl.pickDir", ["选择保存位置"]).then((r) => {
        nativeBtn.disabled = false; nativeBtn.textContent = "用系统选择器…";
        const p = (r && r.result && r.result.path) || "";
        if (p) { close(); onPick(p); }
      });
    };
    pickBtn.onclick = () => { if (cur.path) { close(); onPick(cur.path); } };
    ov.addEventListener("click", (e) => { if (e.target === ov) close(); });
    go(startPath || "");
  }

  // ---- 「下载文件信息」确认卡 ----
  function openCard(info) {
    info = info || {};
    const th = T();
    rpc("sniffdl.dlDefaults", [location.href]).then((d) => {
      const dd = (d && d.ok && d.result) || {};
      buildCard(info, dd, th);
    });
  }

  function buildCard(info, dd, th) {
    unmount();
    const cats = (dd.categories && dd.categories.length) ? dd.categories.slice() : ["常规"];
    const base = dd.base_dir || "";
    const sep = "\\";
    const name0 = info.filename || nameFromUrl(info.url) || dd.suggested_name || "download";
    const ext0 = (info.ext || guessExt(name0, info.url) || "").toLowerCase();
    const lastCat = (dd.last_cat && cats.indexOf(dd.last_cat) >= 0) ? dd.last_cat : cats[0];
    const st = {
      cats, base, cat: lastCat, bind: true,
      dir: base ? (base + sep + lastCat) : "",
      name: name0, desc: info.description || "", browsed: false,
    };

    const hostEl = mount();
    const card = cardShell(th);
    hostEl.appendChild(card);

    const hd = headBar(th, (th.char ? th.char + " · " : "") + "下载文件信息",
      "收到浏览器交来的下载，确认一下就搬～", () => unmount());
    card.appendChild(hd);

    const bodyWrap = document.createElement("div");
    bodyWrap.style.cssText = "display:flex;gap:0;align-items:stretch;";
    const form = document.createElement("div");
    form.style.cssText = "flex:1;min-width:0;padding:12px 16px;";
    bodyWrap.appendChild(form);
    card.appendChild(bodyWrap);

    // URL（只读）
    const urlIn = inputEl(th, info.url || "", true); urlIn.title = info.url || "";
    form.appendChild(fieldRow(th, "URL", urlIn));

    // 分类 + ＋新建
    const catWrap = document.createElement("div");
    catWrap.style.cssText = "flex:1;display:flex;gap:6px;align-items:center;";
    const catSel = document.createElement("select");
    catSel.style.cssText = "flex:1;min-width:0;border:1px solid " + th.line + ";border-radius:8px;padding:6px 8px;" +
      "font:13px inherit;font-family:inherit;color:" + th.ink + ";background:" + th.card + ";outline:none;";
    function fillCats() {
      catSel.innerHTML = "";
      st.cats.forEach((c) => { const o = document.createElement("option"); o.value = c; o.textContent = c; catSel.appendChild(o); });
      catSel.value = st.cat;
    }
    fillCats();
    const addBtn = btnEl(th, "＋", "ghost"); addBtn.style.padding = "6px 10px"; addBtn.title = "新建分类";
    addBtn.addEventListener("click", () => {
      const nm = window.prompt("给新分类起个名字：", "");
      const v = (nm || "").trim();
      if (!v || st.cats.indexOf(v) >= 0) return;
      rpc("sniffdl.addCategory", [v]).then((r) => {
        const list = (r && r.ok && r.result && r.result.categories) || null;
        st.cats = list && list.length ? list : st.cats.concat(v);
        st.cat = v; fillCats(); onCatChange();
      });
    });
    catWrap.appendChild(catSel); catWrap.appendChild(addBtn);
    form.appendChild(fieldRow(th, "分类", catWrap));

    // 让该分类使用该路径
    const bindWrap = document.createElement("div");
    bindWrap.style.cssText = "flex:1;display:flex;align-items:center;gap:6px;";
    const bindCb = document.createElement("input"); bindCb.type = "checkbox"; bindCb.checked = true;
    const bindLab = document.createElement("span"); bindLab.textContent = "让该分类使用该路径";
    bindLab.style.cssText = "font-size:12.5px;color:" + th.inkL + ";";
    bindWrap.appendChild(bindCb); bindWrap.appendChild(bindLab);
    form.appendChild(fieldRow(th, "", bindWrap));

    // 另存为
    const nameIn = inputEl(th, st.name, false);
    nameIn.addEventListener("input", () => {
      st.name = nameIn.value;
      const e = guessExt(st.name, info.url);
      glyph._sz.textContent = fmtB(info.size || 0);
      glyph._ty.textContent = e ? ("类型 ." + e) : "类型 未知";
    });
    form.appendChild(fieldRow(th, "另存为", nameIn));

    // 保存到：常用位置(网页内快选，免切桌面) + 可编辑路径 + 其他位置(原生兜底)
    const dirWrap = document.createElement("div");
    dirWrap.style.cssText = "flex:1;min-width:0;display:flex;flex-wrap:wrap;gap:6px;align-items:center;";
    const dirIn = inputEl(th, st.dir, false);
    dirIn.style.minWidth = "140px";
    const posSel = document.createElement("select");
    posSel.style.cssText = "flex:1 1 42%;min-width:120px;border:1px solid " + th.line + ";border-radius:8px;padding:6px 8px;" +
      "font:12.5px inherit;font-family:inherit;color:" + th.inkL + ";background:" + th.card + ";outline:none;" +
      "text-overflow:ellipsis;";
    const posOpts = [];
    (dd.recent_dirs || []).forEach((p) => { if (p && posOpts.indexOf(p) < 0) posOpts.push(p); });
    const op0 = document.createElement("option"); op0.value = ""; op0.textContent = "常用位置…";
    posSel.appendChild(op0);
    posOpts.forEach((p) => { const o = document.createElement("option"); o.value = p; o.textContent = p; o.title = p; posSel.appendChild(o); });
    posSel.addEventListener("change", () => {
      const p = posSel.value;
      if (p) { st.dir = p; dirIn.value = p; st.browsed = true; bindCb.checked = false; st.bind = false; }
    });
    const browse = btnEl(th, "📁 浏览", "ghost");
    browse.style.cssText += "flex:0 0 auto;padding:6px 12px;white-space:nowrap;font-weight:700;";
    browse.title = "在网页里逐层选择保存文件夹（无需切换到软件窗口）";
    browse.addEventListener("click", () => {
      openDirBrowser(st.dir || "", (p) => {
        if (p) { st.dir = p; dirIn.value = p; st.browsed = true; bindCb.checked = false; st.bind = false; }
      });
    });
    dirWrap.appendChild(posSel); dirWrap.appendChild(dirIn); dirWrap.appendChild(browse);
    form.appendChild(fieldRow(th, "保存到", dirWrap));

    // 描述
    const descIn = inputEl(th, st.desc, false);
    form.appendChild(fieldRow(th, "描述", descIn));

    function onCatChange() {
      st.cat = catSel.value;
      if (bindCb.checked) { st.dir = st.base ? (st.base + sep + st.cat) : ""; dirIn.value = st.dir; st.browsed = false; }
    }
    catSel.addEventListener("change", onCatChange);
    bindCb.addEventListener("change", () => { st.bind = bindCb.checked; if (st.bind) onCatChange(); });

    // 右侧预览
    const glyph = glyphEl(th, ext0, info.size ? fmtB(info.size) : "…");
    bodyWrap.appendChild(glyph);

    // 底部按钮
    const foot = document.createElement("div");
    foot.style.cssText = "display:flex;gap:8px;justify-content:flex-end;padding:12px 16px;border-top:1px solid " + th.line + ";";
    const later = btnEl(th, "稍后下载", "ghost");
    const start = btnEl(th, "开始下载", "primary");
    const cancel = btnEl(th, "取消", "ghost");
    foot.appendChild(later); foot.appendChild(start); foot.appendChild(cancel);
    card.appendChild(foot);

    function payload(defer) {
      return {
        url: info.url, filename: st.name || "download", referer: info.referer || location.href,
        cookie: info.cookie || "", ua: navigator.userAgent || "", size: info.size || 0,
        category: st.cat, use_cat_dir: bindCb.checked, outdir: st.browsed ? st.dir : "",
        description: (descIn.value || "").trim(), _defer: !!defer,
      };
    }
    start.addEventListener("click", () => {
      start.disabled = true; start.textContent = "正在交给看板娘…";
      rpc("sniffdl.add", [payload(false)]).then((r) => {
        const gid = (r && r.ok && r.result) || "";
        if (!gid) { start.disabled = false; start.textContent = "开始下载"; toast(th, "起下载失败，请确认 SniffDL 已开启本地下载服务", false); return; }
        openProgress(gid, { name: st.name || "download", dir: st.dir, size: info.size || 0, url: info.url, referer: info.referer || location.href });
      });
    });
    later.addEventListener("click", () => {
      rpc("sniffdl.add", [payload(true)]).then((r) => {
        const gid = (r && r.ok && r.result) || "";
        unmount();
        toast(th, gid ? ("已存入稍后队列：" + (st.name || "download")) : "登记稍后失败", !!gid);
      });
    });
    cancel.addEventListener("click", () => unmount());
    hostEl.addEventListener("mousedown", (e) => { if (e.target === hostEl) unmount(); });
  }

  // ---- 实时进度：可最小化成角落浮标，下载在桌面后台跑，页面只轮询呈现 ----
  const watchers = {};          // gid -> {gid, meta, s, done, pillEl, view, paused}
  let focusedGid = null;
  let dockEl = null;
  let sharedTimer = null;

  function ensureDock() {
    if (dockEl && document.documentElement.contains(dockEl)) return dockEl;
    dockEl = document.createElement("div");
    dockEl.style.cssText = "position:fixed;right:14px;bottom:14px;z-index:2147483646;" +
      "display:flex;flex-direction:column;gap:8px;align-items:flex-end;" +
      "font:12.5px/1.4 'Microsoft YaHei UI','Segoe UI',system-ui,sans-serif;";
    document.documentElement.appendChild(dockEl);
    return dockEl;
  }
  function startShared() { if (!sharedTimer) sharedTimer = setInterval(pollAll, 800); }
  function maybeStopShared() {
    if (sharedTimer && !Object.keys(watchers).length) { clearInterval(sharedTimer); sharedTimer = null; }
  }
  function upsertWatcher(gid, meta) {
    let w = watchers[gid];
    if (!w) w = watchers[gid] = { gid, meta: meta || {}, s: null, done: false, paused: false };
    else if (meta) w.meta = Object.assign(w.meta, meta);
    return w;
  }

  function removePill(w) { if (w.pillEl) { try { w.pillEl.remove(); } catch (e) {} w.pillEl = null; } }

  function renderPill(w) {
    const th = T();
    let pill = w.pillEl;
    if (!pill) {
      pill = w.pillEl = document.createElement("div");
      pill.style.cssText = "width:252px;border-radius:12px;overflow:hidden;cursor:pointer;" +
        "background:" + th.card + ";color:" + th.ink + ";border:1px solid " + th.line + ";" +
        "box-shadow:0 12px 34px -12px rgba(0,0,0,.45);";
      pill.innerHTML =
        '<div style="display:flex;align-items:center;gap:8px;padding:8px 10px;background:linear-gradient(135deg,' + th.accent + ',' + th.accent + 'cc);color:#fff">' +
          '<span class="pk-t" style="flex:1;min-width:0;font-weight:700;font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis"></span>' +
          '<span class="pk-x" title="取消并删除该下载" style="cursor:pointer;font-size:15px;line-height:1;padding:0 3px;opacity:.9">×</span>' +
        '</div>' +
        '<div style="padding:8px 10px">' +
          '<div style="height:8px;border-radius:5px;background:' + th.bg + ';border:1px solid ' + th.line + ';overflow:hidden"><div class="pk-fill" style="height:100%;width:0%;background:linear-gradient(90deg,' + th.accent + ',' + th.accent + 'bb);transition:width .3s ease"></div></div>' +
          '<div class="pk-sub" style="margin-top:6px;font-size:11.5px;color:' + th.inkL + '"></div>' +
        '</div>';
      pill.addEventListener("click", (e) => { if (e.target.classList.contains("pk-x")) return; focusGid(w.gid); });
      pill.querySelector(".pk-x").addEventListener("click", (e) => { e.stopPropagation(); cancelDelete(w); });
      ensureDock().appendChild(pill);
    }
    const s = w.s || {};
    const total = Number(s.totalLength) || 0, done = Number(s.completedLength || s.downloadedLength) || 0;
    const spd = Number(s.downloadSpeed) || 0;
    const pct = total > 0 ? Math.min(100, Math.floor(done / total * 100)) : 0;
    pill.querySelector(".pk-t").textContent = w.meta.name || "下载中";
    const fill = pill.querySelector(".pk-fill"), sub = pill.querySelector(".pk-sub");
    if (w.done) {
      const ok = s.status === "complete";
      fill.style.width = "100%"; fill.style.background = ok ? th.accent : "#c0455f";
      sub.textContent = ok ? ("✓ 已完成 · " + fmtB(total)) : ("✗ " + (s.errorMessage || "失败"));
    } else {
      fill.style.width = (total > 0 ? pct : 4) + "%";
      sub.textContent = (total > 0 ? (pct + "% · ") : "") + fmtSpd(spd) + (w.paused ? " · 已暂停" : "");
    }
  }

  function dismissWatcher(gid) {
    const w = watchers[gid]; if (!w) return;
    removePill(w);
    if (focusedGid === gid) { focusedGid = null; unmount(); }
    delete watchers[gid];
    maybeStopShared();
  }

  function minimize() {
    if (!focusedGid) return;
    const gid = focusedGid; focusedGid = null;
    const w = watchers[gid];
    if (w) { w.view = null; }
    unmount();
    if (w) renderPill(w);
  }

  function focusGid(gid) {
    const w = watchers[gid]; if (!w) return;
    if (focusedGid && focusedGid !== gid) {
      const prev = watchers[focusedGid];
      if (prev) { prev.view = null; renderPill(prev); }
    }
    focusedGid = gid;
    removePill(w);
    buildProgressModal(w);
    paintWatcher(w);
  }

  function openProgress(gid, meta) {
    upsertWatcher(gid, meta);
    startShared();
    focusGid(gid);
    pollAll();
  }

  function cancelDelete(w) {
    // × / 取消：停线程 + 删本次残留（后端 rpc_remove 已 purge .part/.snifftmp），
    // 但已下完的成品文件仍保留（IDM 语义：取消进行中的下载＝丢弃半成品）。
    try { rpc("aria2.remove", [w.gid]); } catch (e) {}
    dismissWatcher(w.gid);
  }

  function buildProgressModal(w) {
    unmount();
    const th = T();
    const hostEl = mount();
    const card = cardShell(th);
    hostEl.appendChild(card);
    const hd = headBar(th, "看板娘正在搬 ♪", w.meta.name || "", () => cancelDelete(w), () => minimize());
    card.appendChild(hd);

    const body = document.createElement("div");
    body.style.cssText = "padding:16px;";
    card.appendChild(body);

    const pctBig = document.createElement("div");
    pctBig.style.cssText = "text-align:center;font-weight:800;font-size:30px;color:" + th.accent + ";margin:2px 0 10px;";
    pctBig.textContent = "0%";
    body.appendChild(pctBig);

    const track = document.createElement("div");
    track.style.cssText = "height:12px;border-radius:6px;background:" + th.bg + ";border:1px solid " + th.line + ";overflow:hidden;";
    const bar = document.createElement("div");
    bar.style.cssText = "height:100%;width:0%;border-radius:6px;background:linear-gradient(90deg," + th.accent + "," + th.accent + "bb);transition:width .3s ease;";
    track.appendChild(bar); body.appendChild(track);

    const rows = document.createElement("div");
    rows.style.cssText = "display:grid;grid-template-columns:auto 1fr;gap:4px 14px;margin-top:14px;font-size:12.5px;";
    function kv(k) {
      const a = document.createElement("div"); a.textContent = k; a.style.cssText = "color:" + th.inkL + ";";
      const b = document.createElement("div"); b.textContent = "—"; b.style.cssText = "color:" + th.ink + ";word-break:break-all;";
      rows.appendChild(a); rows.appendChild(b); return b;
    }
    const vStatus = kv("状态"), vTotal = kv("文件大小"), vDone = kv("已下载"),
      vSpd = kv("传输速度"), vEta = kv("剩余时间"), vRes = kv("能否续传"), vDst = kv("保存到");
    vRes.textContent = "是（断点续传）";
    body.appendChild(rows);

    const foot = document.createElement("div");
    foot.style.cssText = "display:flex;gap:8px;justify-content:flex-end;padding:12px 16px;border-top:1px solid " + th.line + ";";
    card.appendChild(foot);
    const minBtn = btnEl(th, "最小化", "ghost");
    const pauseBtn = btnEl(th, "暂停", "ghost");
    const cancelBtn = btnEl(th, "取消", "ghost");
    foot.appendChild(minBtn); foot.appendChild(pauseBtn); foot.appendChild(cancelBtn);
    minBtn.addEventListener("click", () => minimize());
    pauseBtn.addEventListener("click", () => {
      rpc(w.paused ? "aria2.unpause" : "aria2.pause", [w.gid]);
      w.paused = !w.paused; pauseBtn.textContent = w.paused ? "继续" : "暂停";
    });
    cancelBtn.addEventListener("click", () => cancelDelete(w));

    w.view = { hd, pctBig, bar, vStatus, vTotal, vDone, vSpd, vEta, vDst, foot, th, pauseBtn };
    hostEl.addEventListener("mousedown", (e) => { if (e.target === hostEl) minimize(); });
  }

  function showFinish(w) {
    const v = w.view; if (!v || v._finished) return;
    v._finished = true;
    const th = v.th, s = w.s || {}, ok = s.status === "complete";
    const t = v.hd.querySelector(".sd-hd-t"); if (t) t.textContent = ok ? "✓ 下载完成" : "✗ 下载失败";
    v.foot.innerHTML = "";
    if (ok) {
      const open = btnEl(th, "打开文件夹", "primary");
      const play = btnEl(th, "打开文件", "ghost");
      const close = btnEl(th, "关闭", "ghost");
      open.addEventListener("click", () => rpc("sniffdl.reveal", [w.gid]).then((r) => {
        if (!(r && r.ok && r.result && r.result.ok)) toast(th, "文件可能还没落盘，稍后再试", false);
      }));
      play.addEventListener("click", () => rpc("sniffdl.openFile", [w.gid]));
      close.addEventListener("click", () => dismissWatcher(w.gid));
      v.foot.appendChild(open); v.foot.appendChild(play); v.foot.appendChild(close);
    } else {
      const retry = btnEl(th, "重试", "primary");
      const close = btnEl(th, "关闭", "ghost");
      retry.addEventListener("click", () => {
        rpc("sniffdl.add", [{ url: w.meta.url, filename: w.meta.name, referer: w.meta.referer,
          outdir: w.meta.dir, use_cat_dir: false }]).then((r) => {
          const g2 = (r && r.ok && r.result) || "";
          if (g2) { const old = w.gid; dismissWatcher(old); openProgress(g2, w.meta); }
          else toast(th, "重试失败", false);
        });
      });
      close.addEventListener("click", () => dismissWatcher(w.gid));
      v.foot.appendChild(retry); v.foot.appendChild(close);
    }
  }

  function paintWatcher(w) {
    if (w.pillEl) renderPill(w);
    const v = w.view; if (!v) return;
    const th = v.th, s = w.s || {};
    const total = Number(s.totalLength) || 0, done = Number(s.completedLength || s.downloadedLength) || 0;
    const spd = Number(s.downloadSpeed) || 0;
    const pct = total > 0 ? Math.min(100, Math.floor(done / total * 100)) : 0;
    const ok = s.status === "complete";
    v.pctBig.textContent = w.done ? (ok ? "100%" : "失败") : (total > 0 ? (pct + "%") : "嗅探中…");
    v.pctBig.style.color = (w.done && !ok) ? "#c0455f" : th.accent;
    v.bar.style.width = (w.done && ok) ? "100%" : (total > 0 ? pct + "%" : "3%");
    v.vTotal.textContent = total ? fmtB(total) : "计算中…";
    v.vDone.textContent = fmtB(done) + (total ? (" / " + fmtB(total)) : "");
    v.vSpd.textContent = fmtSpd(spd);
    v.vEta.textContent = (spd > 0 && total > 0) ? fmtEta((total - done) / spd) : "—";
    v.vDst.textContent = (s.dir || w.meta.dir || "") + (s.name ? ("\\" + s.name) : "");
    if (w.done) {
      v.vStatus.textContent = ok ? "已完成" : ("失败：" + (s.errorMessage || "未知原因"));
      showFinish(w);
    } else {
      v.vStatus.textContent = s.status === "active" ? (s.statusMessage || "下载中") : (s.status === "waiting" ? "排队中" :
        (s.status === "paused" ? "已暂停" : (s.status || "—")));
    }
  }

  function pollAll() {
    const gids = Object.keys(watchers);
    if (!gids.length) { maybeStopShared(); return; }
    gids.forEach((gid) => {
      const w = watchers[gid]; if (!w || w.done) return;
      rpc("aria2.getDownloadResult", [gid]).then((r) => {
        const s = (r && r.ok && r.result) || null;
        if (!s || !watchers[gid]) return;
        w.s = s;
        if (s.status === "complete" || s.status === "error") {
          w.done = true;
          const ok = s.status === "complete";
          toast(T(), "「" + (w.meta.name || "文件") + "」" + (ok ? "已下载完成 ♪" : "下载失败"), ok);
        }
        paintWatcher(w);
      });
    });
  }

  window.__sniffdlOpenCard = openCard;

  // 供 background 把「右键 / 浏览器自动接管」也拉回网页中央弹卡
  try {
    chrome.runtime.onMessage.addListener((msg, _s, sendResp) => {
      if (msg && msg.type === "dl_card" && msg.info) {
        openCard(msg.info); sendResp({ ok: true }); return true;
      }
      if (msg && msg.type === "dl_progress" && msg.gid) {
        openProgress(msg.gid, msg.meta || {}); sendResp({ ok: true }); return true;
      }
      return false;
    });
  } catch (e) {}
})();

// ── 0.7.1：视频悬浮下载按钮（IDM 式）─────────────────────────────
// 鼠标移到够大的 <video> 上 → 播放器右上角浮出「下载视频」小钮；点它弹网页中央下载卡。
// 真实地址（mp4/m3u8…）直接交地址；MSE/blob 播放器交页面地址，由内核走 yt-dlp 解析。
// 关闭：弹窗设置 hoverBtn=false 全局关；点小钮上的 × 本站本次会话不再出现。
(function () {
  "use strict";
  if (window.__sniffdlHoverBtn) return;
  window.__sniffdlHoverBtn = true;
  const KEY = "__sniffdl_hover_off";
  let enabled = true;
  try { if (sessionStorage.getItem(KEY) === "1") enabled = false; } catch (_) {}
  try {
    chrome.storage && chrome.storage.local.get({ hoverBtn: true }, (r) => { if (r && r.hoverBtn === false) enabled = false; });
  } catch (_) {}

  const MIN_W = 240, MIN_H = 135;
  let btn = null, cur = null, hideT = null;

  function accent() {
    const t = window.__sniffdlTheme;
    return (t && (t.accent || (t.palette && t.palette.pink))) || "#3E6FD0";
  }
  function srcOf(v) {
    let u = "";
    try { u = v.currentSrc || v.src || ((v.querySelector("source") || {}).src) || ""; } catch (_) {}
    return u;
  }
  function target(v) {
    const u = srcOf(v);
    if (/^https?:/i.test(u)) {
      let name = "";
      try { name = decodeURIComponent(new URL(u).pathname.split("/").pop() || ""); } catch (_) {}
      return { url: u, filename: /\.\w{2,4}$/.test(name) ? name : "", page: false };
    }
    return { url: location.href, filename: "", page: true };   // blob/MSE：交页面给 yt-dlp
  }

  function ensure() {
    if (btn && document.documentElement.contains(btn)) return btn;
    btn = document.createElement("div");
    btn.setAttribute("data-sniffdl", "hover-btn");
    btn.style.cssText = "position:fixed;z-index:2147483646;display:none;align-items:center;gap:0;" +
      "border-radius:999px;overflow:hidden;box-shadow:0 4px 14px rgba(20,40,90,.28);" +
      "font:600 12.5px/1 system-ui,-apple-system,'Microsoft YaHei',sans-serif;user-select:none;" +
      "opacity:0;transition:opacity .15s ease;";
    const go = document.createElement("span");
    go.setAttribute("data-role", "go");
    go.textContent = "⬇ 下载视频";
    go.style.cssText = "padding:7px 11px 7px 12px;color:#fff;cursor:pointer;";
    const x = document.createElement("span");
    x.setAttribute("data-role", "close");
    x.textContent = "×";
    x.title = "本站这次不再显示";
    x.style.cssText = "padding:7px 10px 7px 6px;color:rgba(255,255,255,.85);cursor:pointer;font-size:14px;";
    btn.appendChild(go); btn.appendChild(x);
    go.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); fire(); });
    x.addEventListener("click", (e) => {
      e.preventDefault(); e.stopPropagation();
      enabled = false; hide(true);
      try { sessionStorage.setItem(KEY, "1"); } catch (_) {}
    });
    btn.addEventListener("mouseenter", () => { if (hideT) { clearTimeout(hideT); hideT = null; } });
    btn.addEventListener("mouseleave", () => armHide());
    (document.body || document.documentElement).appendChild(btn);
    return btn;
  }

  function place(v) {
    const r = v.getBoundingClientRect();
    const b = ensure();
    b.style.background = accent();
    b.style.display = "flex";
    const bw = b.offsetWidth || 110;
    b.style.left = Math.max(4, Math.min(innerWidth - bw - 4, r.right - bw - 12)) + "px";
    b.style.top = Math.max(4, r.top + 12) + "px";
    requestAnimationFrame(() => { b.style.opacity = "1"; });
  }
  function hide(now) {
    if (!btn) return;
    btn.style.opacity = "0";
    if (now) btn.style.display = "none";
    else setTimeout(() => { if (btn && btn.style.opacity === "0") btn.style.display = "none"; }, 160);
    cur = null;
  }
  function armHide() {
    if (hideT) clearTimeout(hideT);
    hideT = setTimeout(() => hide(false), 1200);
  }

  function fire() {
    if (!cur) return;
    const t = target(cur);
    const info = { url: t.url, filename: t.filename, referer: location.href, size: 0, ext: "", source: "hover" };
    hide(true);
    if (window === window.top && window.__sniffdlOpenCard) {
      try { window.__sniffdlOpenCard(info); return; } catch (_) {}
    }
    try {
      chrome.runtime.sendMessage({ type: "chip_dl", url: info.url, filename: info.filename, referer: info.referer }, () => { void chrome.runtime.lastError; });
    } catch (_) {}
  }

  function videoAt(x, y) {
    const vids = document.getElementsByTagName("video");
    for (let i = 0; i < vids.length; i++) {
      const v = vids[i];
      const r = v.getBoundingClientRect();
      if (r.width < MIN_W || r.height < MIN_H) continue;
      if (x >= r.left && x <= r.right && y >= r.top && y <= r.bottom) {
        const s = srcOf(v);
        if (!s && v.readyState < 1) continue;          // 空壳播放器不打扰
        return v;
      }
    }
    return null;
  }

  // 播放器常盖着一层控件，不能只靠 video 的 mouseover → 用坐标命中
  let raf = 0;
  document.addEventListener("mousemove", (e) => {
    if (!enabled || raf) return;
    const x = e.clientX, y = e.clientY;
    raf = requestAnimationFrame(() => {
      raf = 0;
      if (btn && btn.contains(e.target)) return;
      if (document.fullscreenElement) { hide(true); return; }   // 全屏时不挡画面
      const v = videoAt(x, y);
      if (v) {
        if (hideT) { clearTimeout(hideT); hideT = null; }
        if (v !== cur) { cur = v; place(v); }
      } else if (cur) armHide();
    });
  }, { passive: true, capture: true });
  addEventListener("scroll", () => { if (cur) place(cur); }, { passive: true, capture: true });
  addEventListener("resize", () => { if (cur) place(cur); }, { passive: true });
})();
