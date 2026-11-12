/* SniffDL 下载接管 · 后台 service worker（0.7.0：v3 内核时接管规则从内核 sniffdl.takeoverRules 读）
 * 两条入口，全部交给桌面端 SniffDL 真正搬运（多连接/断点续传/看板娘反馈）：
 *   1) 自动接管：监听浏览器所有下载 → 命中规则就 cancel 掉浏览器下载 →
 *      把 {url, 文件名, Referer, Cookie, 大小} 推给 sniffdl.intercept → 桌面弹 IDM 式确认窗。
 *   2) 右键一项「用 SniffDL 下载」（link/image/selection/page 自适应）：
 *      链接=下载此链接、图片=下载图片、选中文本=批量其中的链接、页面=嗅探本页媒体。
 * 桌面端凭 rpc_confirm_dialog 决定弹确认窗还是静默直下；插件不做这个决策。
 */

const DEFAULTS = {
  enabled: true,        // 接管总开关
  port: 6800,           // SniffDL 本地 RPC 端口
  token: "",            // 可选鉴权 token
  minSizeMB: 0,         // 只接管 ≥ 此大小的文件（0=不限）
  skipInline: true,     // 跳过浏览器内联预览（图片/视频直接打开的那种）
  onlyExtensions: "",   // 逗号分隔的白名单扩展名（空=全部类型都接管）
  syncNetdiskCookies: true, // 0.6.0：把网盘登录 Cookie 同步给桌面端（只进内存），手动粘贴直链也能下
};

async function cfg() {
  const stored = await chrome.storage.local.get(DEFAULTS);
  // 0.6.0 迁移：旧测试期存过 6820 + "tk-test"，桌面端已回归 aria2 默认 6800/无 token → 一起改回，免得两边对不上
  if (String(stored.port) === "6820" && String(stored.token || "") === "tk-test") {
    stored.port = 6800; stored.token = "";
    try { await chrome.storage.local.set({ port: 6800, token: "" }); } catch (e) {}
  }
  return { ...DEFAULTS, ...stored };
}

// 我们主动取消的下载 id，避免取消事件里再触发别的逻辑
const cancelling = new Set();

function rpcUrl(c) {
  return `http://127.0.0.1:${c.port}/jsonrpc`;
}

async function callRpc(method, params) {
  const c = await cfg();
  const p = Array.isArray(params) ? params.slice() : [];
  if (c.token) p.unshift("token:" + c.token);
  let res;
  try {
    res = await fetch(rpcUrl(c), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", method, params: p, id: Date.now() }),
    });
  } catch (e) {
    // 0.7.0：连不上 = 内核没运行（或端口不对），给一句人话而不是 "Failed to fetch"
    const er = new Error("SniffDL 没有运行（连不上 127.0.0.1:" + c.port + "）：请先打开 SniffDL，或在扩展弹窗里核对端口");
    er.offline = true;
    throw er;
  }
  const j = await res.json();
  if (j.error) throw new Error(j.error.message || "RPC error");
  return j.result;
}

// 取该 URL 会带上的 Cookie（含 httpOnly），拼成 Cookie 头
async function cookieHeader(url) {
  try {
    const cookies = await chrome.cookies.getAll({ url });
    if (!cookies || !cookies.length) return "";
    return cookies.map((k) => `${k.name}=${k.value}`).join("; ");
  } catch (e) {
    return "";
  }
}

function extFromUrl(u) {
  try {
    const path = new URL(u).pathname;
    const m = path.match(/\.([A-Za-z0-9]{1,6})$/);
    return m ? m[1].toLowerCase() : "";
  } catch (e) {
    return "";
  }
}

function baseName(item, url) {
  let name = "";
  try {
    const fn = decodeURIComponent((item.filename || "").split(/[\\/]/).pop());
    if (fn) name = fn;
  } catch (e) {}
  if (!name) {
    // 网盘直链：真名在 query（夸克 filename= / 百度 fin= / 123 filename= …），path 末段只是哈希
    try {
      const sp = new URL(url).searchParams;
      for (const k of ["filename", "fname", "fn", "fin", "file_name", "name"]) {
        const v = sp.get(k);
        if (v && /\.[A-Za-z0-9]{1,8}$/.test(v)) { name = v; break; }
      }
    } catch (e) {}
  }
  if (!name) {
    try { name = decodeURIComponent(new URL(url).pathname.split("/").pop()); } catch (e) {}
  }
  return name || "download";
}

// 自动接管里"用户没主动点就别碰"的垃圾：更新器重定向(fwlink)、网页/广告碎片、极小碎屑
function isJunkAutolink(item, url) {
  try {
    const u = new URL(url);
    const host = (u.hostname || "").toLowerCase();
    const path = (u.pathname || "").toLowerCase();
    if (host === "go.microsoft.com" || path.startsWith("/fwlink") || path.indexOf("/fwlink/") >= 0) return true;
    const ext = extFromUrl(url) || extFromUrl(item.finalUrl || "");
    const TEXTY = ["htm", "html", "asp", "aspx", "jsp", "jspx", "php", "cfm", "shtml", "xhtml"];
    const sz = item.totalBytes || 0;
    if (TEXTY.indexOf(ext) >= 0) return !(sz >= 256 * 1024);   // 网页/碎片：≥256KB 才当真实附件
    if (sz > 0 && sz < 1024) return true;
    const mt = (item.mime || "").toLowerCase().split(";")[0].trim();
    if (!ext && (mt === "text/html" || mt === "application/xhtml+xml")) return true;
    return false;
  } catch (e) {
    return false;
  }
}

// ================= 0.7.0：接管规则从内核读（v3 sniffdl.takeoverRules）=================
// mode: "v3" = 新内核，按内核规则（扩展名/最小大小/站点白黑名单/Alt 放行/是否确认）判定，接管走 sniffdl.intercept；
//       "legacy" = 旧桌面端（没有 takeoverRules），完全沿用 0.6.0 行为；
//       "offline" = 连不上内核：不取消浏览器下载，给出提示。
let rulesCache = { t: 0, mode: "", rules: null, error: "" };
async function getRules(force) {
  if (!force && rulesCache.mode && Date.now() - rulesCache.t < 1500) return rulesCache;
  let mode = "legacy", rules = null, error = "";
  try {
    const r = await callRpc("sniffdl.takeoverRules", []);
    if (r && r.ok) { mode = "v3"; rules = r; }
  } catch (e) {
    if (e && e.offline) { mode = "offline"; error = e.message; }
  }
  rulesCache = { t: Date.now(), mode, rules, error };
  try {
    chrome.action.setBadgeText({ text: mode === "offline" ? "!" : "" });
    if (mode === "offline") chrome.action.setBadgeBackgroundColor({ color: "#D04A3E" });
    chrome.action.setTitle({ title: mode === "offline" ? "SniffDL 没有运行：下载暂由浏览器自己处理" : "SniffDL 下载接管" });
  } catch (e) {}
  return rulesCache;
}
function hostOf(u) { try { return new URL(u).hostname.toLowerCase(); } catch (e) { return ""; } }
function ruleHostIn(host, list) {
  host = (host || "").toLowerCase();
  if (!host) return false;
  return (list || []).some((d) => {
    d = String(d || "").trim().toLowerCase().replace(/^https?:\/\//, "").replace(/^\*\./, "").split("/")[0];
    return !!d && (host === d || host.endsWith("." + d));
  });
}
// 按内核规则判定：{take, why}
function judgeByRules(r, url, pageUrl, size) {
  if (!r || !r.enabled) return { take: false, why: "内核设置里关掉了接管" };
  const hosts = [hostOf(url), hostOf(pageUrl)];
  if (hosts.some((h) => ruleHostIn(h, r.blacklist))) return { take: false, why: "站点在「从不接管」名单" };
  const white = hosts.some((h) => ruleHostIn(h, r.whitelist));
  if (white) return { take: true, why: "站点在「总是接管」名单" };
  const ext = extFromUrl(url);
  if ((r.exts || []).length && !(r.exts || []).includes(ext)) return { take: false, why: "扩展名 ." + (ext || "?") + " 不在接管列表" };
  if (r.min_kb > 0 && size > 0 && size < r.min_kb * 1024) return { take: false, why: "小于 " + r.min_kb + " KB" };
  return { take: true, why: "" };
}
// 用户按 Alt 点 / 内核离线交回浏览器的下载：onCreated 不再抢（按标签 5 秒 + 按 URL）
const bypassUrls = new Map();
async function markBypass(tabId, url) {
  if (url) bypassUrls.set(url, Date.now());
  if (tabId != null && tabId >= 0) { try { await chrome.storage.session.set({ ['b' + tabId]: Date.now() }); } catch (e) {} }
}
async function isBypassed(item) {
  const now = Date.now();
  for (const [u, t] of bypassUrls) if (now - t > 30000) bypassUrls.delete(u);
  if (bypassUrls.has(item.url) || bypassUrls.has(item.finalUrl)) return true;
  if (item.tabId == null || item.tabId < 0) return false;
  try { const o = await chrome.storage.session.get('b' + item.tabId); return now - (o['b' + item.tabId] || 0) <= 5000; } catch (e) { return false; }
}
let offlineNotified = 0;
function notifyOffline(msg) {
  if (Date.now() - offlineNotified < 15000) return;
  offlineNotified = Date.now();
  try {
    chrome.notifications.create("sdl-offline-" + Date.now(), {
      type: "basic", iconUrl: "icons/icon128.png", title: "SniffDL 没有运行",
      message: (msg || "连不上本机 SniffDL") + "\n这次下载已交回浏览器自己处理。",
    });
  } catch (e) {}
}
// v3：交给内核。user=true → 内核按「主动点下载时先确认」决定进待确认区还是直接开下
async function v3Take(info, user, rules) {
  info = await withCaptured(info);
  const cookie = info.cookie != null ? info.cookie : await cookieHeader(info.url);
  const pd = postGet(info.url);
  const r = await callRpc("sniffdl.intercept", [{
    headers: info.headers || null, url: info.url, filename: info.filename || "", referer: info.referer || "",
    cookie: cookie || "", ua: (typeof navigator !== "undefined" && navigator.userAgent) || "",
    size: info.size || 0, source: user ? "chip" : "browser", confirm: !!(rules && rules.confirm),
    mime: info.mime || "",
    ...(pd ? { method: "POST", post_data: pd,
               headers: Object.assign({}, info.headers || {}, { "Content-Type": "application/x-www-form-urlencoded" }) } : {}),
  }]);
  const name = info.filename || (info.url || "").split("?")[0].split("/").pop() || info.url;
  if (r === "pending") notifyTaken("已进 SniffDL「待确认」：" + name, "icpt-" + Date.now());
  else if (r) notifyTaken("SniffDL 已开始下载：" + name, "icpt-" + r);
  return r;
}
// 内容脚本点击接管（v3）：规则判定 → 交内核；离线 → 交回浏览器下载并提示
async function handleClickDl(msg, tab) {
  const url = (msg.url || "").trim();
  const tabId = (tab && tab.id != null) ? tab.id : -1;
  const pageUrl = msg.referer || (tab && tab.url) || "";
  const rc = await getRules(true);
  const backToBrowser = async () => { await markBypass(tabId, url); try { await chrome.downloads.download({ url }); } catch (e) {} };
  if (rc.mode === "offline") { notifyOffline(rc.error); await backToBrowser(); return { ok: false, offline: true, error: rc.error }; }
  if (rc.mode !== "v3") return null;   // 旧桌面端：调用方走 0.6.0 网页卡
  let size = 0;
  const j = judgeByRules(rc.rules, url, pageUrl, 0);
  if (j.take && rc.rules.min_kb > 0 && !ruleHostIn(hostOf(url), rc.rules.whitelist) && !ruleHostIn(hostOf(pageUrl), rc.rules.whitelist)) {
    try { const h = await fetch(url, { method: "HEAD" }); size = Number(h.headers.get("content-length") || 0); } catch (e) {}
  }
  const j2 = size ? judgeByRules(rc.rules, url, pageUrl, size) : j;
  if (!j2.take) { await backToBrowser(); return { ok: true, taken: false, why: j2.why }; }
  try {
    const r = await v3Take({ url, filename: msg.filename || "", referer: pageUrl, size, source: "chip" }, true, rc.rules);
    return { ok: true, taken: true, result: r };
  } catch (e) {
    if (e.offline) { notifyOffline(e.message); await backToBrowser(); return { ok: false, offline: true, error: e.message }; }
    notifyFail(url, e.message || String(e));
    return { ok: false, error: e.message || String(e) };
  }
}

// 是否应该接管这一条下载
function shouldTake(item, c) {
  const url = item.url || "";
  if (!/^(https?|ftp):/i.test(url)) return false;      // 只接管 http/https/ftp
  if (isJunkAutolink(item, url)) return false;         // 更新器/广告碎片/极小碎屑：不接管也不下载
  const ext = extFromUrl(url) || extFromUrl(item.finalUrl || "");
  // 白名单扩展名
  const allow = (c.onlyExtensions || "").split(",").map((s) => s.trim().toLowerCase()).filter(Boolean);
  if (allow.length) {
    if (!allow.includes(ext)) return false;            // 用户显式白名单：只接管列出的扩展名
  } else {
    // 默认（自动接管、用户没主动点）：只碰媒体(视频/音频)。软件/模型/文档/压缩包等一律不劫持，留给浏览器——
    // 否则打开一个"工具合集"页就会把几十个 .exe/.gguf/.apk 全静默灌进 SniffDL（历史上反复烧流量的根因）。
    const MEDIA = ["mp4","mkv","webm","m4v","mov","flv","ts","m3u8","mpd","mp3","flac","wav","aac","ogg","oga","m4a","opus","weba","3gp","f4v"];
    const mt = (item.mime || "").toLowerCase().split(";")[0].trim();
    const isMedia = (ext && MEDIA.indexOf(ext) >= 0) ||
                    (/^(video|audio)\//.test(mt)) ||
                    (!ext && /\.(m3u8|mpd)(\?|#|$)/i.test(url));
    if (!isMedia) return false;
  }
  // 大小阈值（若已知）
  if (c.minSizeMB > 0 && item.totalBytes && item.totalBytes > 0) {
    if (item.totalBytes < c.minSizeMB * 1024 * 1024) return false;
  }
  // 跳过内联预览：没有明确文件名扩展、且 MIME 是图片/视频/文本预览的，交给浏览器看
  if (c.skipInline && !ext) {
    const mt = (item.mime || "").toLowerCase();
    if (/^(image|video|audio|text)\//.test(mt)) return false;
  }
  return true;
}

// 交桌面端。confirm 缺省 true → 桌面弹“下载文件信息”大窗（选保存位置）；
// confirm=false → 快捷直连，桌面只弹进度·完成窗，不打断浏览。
// ---- POST 下载：浏览器发出 POST 导航（表单提交导出/下载）时记下请求体，被接管时一并交给桌面 ----
// 只收 application/x-www-form-urlencoded 表单（formData）与 ≤64KB 的文本 raw；multipart 不收。
const postMem = new Map();
try {
  chrome.webRequest.onBeforeRequest.addListener((d) => {
    try {
      if (!d || d.method !== "POST" || !d.requestBody) return;
      let body = "";
      if (d.requestBody.formData) {
        const sp = new URLSearchParams();
        for (const k of Object.keys(d.requestBody.formData)) for (const v of d.requestBody.formData[k]) sp.append(k, v);
        body = sp.toString();
      } else if (d.requestBody.raw && d.requestBody.raw.length) {
        const parts = d.requestBody.raw.filter((r) => r && r.bytes).map((r) => new Uint8Array(r.bytes));
        const n = parts.reduce((a, b) => a + b.length, 0);
        if (!n || n > 65536) return;
        const all = new Uint8Array(n); let o = 0;
        for (const p of parts) { all.set(p, o); o += p.length; }
        body = new TextDecoder("utf-8").decode(all);
      }
      if (!body) return;
      postMem.set(d.url, { body, ts: Date.now(), tabId: d.tabId });
      if (postMem.size > 20) postMem.delete(postMem.keys().next().value);
    } catch (e) {}
  }, { urls: ["<all_urls>"], types: ["main_frame", "sub_frame"] }, ["requestBody"]);
} catch (e) {}
function postTab(url) {
  const h = postMem.get(url);
  return h && Date.now() - h.ts < 5 * 60 * 1000 && h.tabId >= 0 ? h.tabId : null;
}
function postGet(url) {
  const h = postMem.get(url);
  return h && Date.now() - h.ts < 5 * 60 * 1000 ? h.body : null;
}

async function interceptOne(info) {
  info = await withCaptured(info);
  const pd = postGet(info.url);
  const cookie = info.cookie != null ? info.cookie : await cookieHeader(info.url);
  return callRpc("sniffdl.intercept", [{
    headers: info.headers || null,
    url: info.url,
    filename: info.filename || "",
    referer: info.referer || "",
    cookie: cookie || "",
    ua: info.ua || ((typeof navigator !== "undefined" && navigator.userAgent) ? navigator.userAgent : ""),
    size: info.size || 0,
    source: info.source || "",
    confirm: info.confirm !== false,
    ...(pd ? { method: "POST", post_data: pd,
               headers: Object.assign({}, info.headers || {}, { "Content-Type": "application/x-www-form-urlencoded" }) } : {}),
  }]);
}

// 批量/嗅探：直接静默转交，不逐个弹确认窗（避免几十条把桌面刷屏）
async function autoAdd(url, referer, out) {
  const cap = isNetdiskDl(url) ? await capGet(url) : null;
  const header = [];
  if (cap) {
    // 浏览器真实发出的头（网盘直链与 UA/Cookie 绑定，原样转交）
    for (const k of Object.keys(cap)) header.push(k + ": " + cap[k]);
    const has = (n) => Object.keys(cap).some((k) => k.toLowerCase() === n);
    if (referer && !has("referer")) header.push("Referer: " + referer);
    const options = { header };
    if (out) options.out = out;
    return callRpc("aria2.addUri", [[url], options]);
  }
  const cookie = await cookieHeader(url);
  if (referer) header.push("Referer: " + referer);
  if (cookie) header.push("Cookie: " + cookie);
  const options = { header };
  if (out) options.out = out;
  return callRpc("aria2.addUri", [[url], options]);
}

// 兜底：网页中央卡弹不出来（该页无法注入 content script，如 chrome:// / 商店页）时，
// 一律静默交给桌面后台下载（aria2.addUri 全程无窗），绝不再弹桌面「下载文件信息」确认框刷屏。
// 只在浏览器里轻提示一声，不打断浏览。
async function silentFallback(info) {
  const url = (info && info.url) || "";
  if (!url) return "";
  try {
    const gid = await autoAdd(url, (info && info.referer) || "", (info && info.filename) || "");
    notifyTaken("已交给看板娘后台下载（此页无法弹出网页下载卡）", "fb-" + (gid || Date.now()));
    return gid;
  } catch (e) {
    notifyFail((info && info.filename) || url, e.message || String(e));
    return "";
  }
}

// ================= 0.6.0 网盘直链：真实请求头捕获 + 必接管 + Cookie 同步 =================
// 与桌面端 netdisk_rules.py 的 dl_hosts 保持一致（只列"下载 CDN"域，网盘网页本身不在内）。
const NETDISK_DL_HOSTS = [
  "drive.quark.cn", "quark.cn", "drive.uc.cn",
  "d.pcs.baidu.com", "pcs.baidu.com", "baidupcs.com", "bcebos.com",
  "aliyundrive.net", "alicloudccp.com",
  "123pan.com", "123pan.cn", "123957.com", "123295.com", "123684.com", "123865.com", "123912.com", "123952.com",
  "xunlei.com", "xlcdn.com",
  "lanzoug.com", "lanzouc.com", "lanzoux.com", "lanzoui.com", "lanzouo.com", "lanzouw.com", "lanzoue.com", "lanzouj.com",
  "115cdn.net", "115cdn.com",
  "ctyunxs.cn",
];
// 网盘网页（这些页面上发起的下载 = 网盘下载，也必接管）
const NETDISK_PAGE_HOSTS = ["pan.quark.cn", "drive.uc.cn", "pan.baidu.com", "yun.baidu.com", "www.alipan.com",
  "www.aliyundrive.com", "www.123pan.com", "www.123912.com", "pan.xunlei.com", "115.com", "cloud.189.cn"];
// 需要登录 Cookie 才能下的网盘 Cookie 域：同步给桌面端，手动粘贴直链 / 外部推送缺 Cookie 时按域补齐
const NETDISK_COOKIE_DOMAINS = ["quark.cn", "uc.cn", "baidu.com", "115.com"];

function hostMatch(host, list) {
  host = (host || "").toLowerCase();
  return list.some((s) => host === s || host.endsWith("." + s));
}
function isNetdiskDl(url) {
  try { return hostMatch(new URL(url).hostname, NETDISK_DL_HOSTS); } catch (e) { return false; }
}
function isNetdiskPage(url) {
  try { return hostMatch(new URL(url).hostname, NETDISK_PAGE_HOSTS); } catch (e) { return false; }
}

// ---- 真实请求头捕获：浏览器发往网盘下载 CDN 的那一刻，把 UA/Cookie/Referer… 原样记下 ----
// 需要 "extraHeaders" 才看得到 Cookie/Referer；只观察不拦截（MV3 允许非阻塞 webRequest）。
// 存 storage.session（SW 被回收也不丢），按 URL 键，最多留 60 条、10 分钟。
const CAPTURE_TTL = 10 * 60 * 1000;
const SKIP_CAPTURE = /^(host|content-length|connection|range|accept-encoding|if-range|if-none-match|if-modified-since|upgrade-insecure-requests|priority)$/i;
const capMem = new Map();
async function capPut(url, headers) {
  capMem.set(url, { h: headers, ts: Date.now() });
  if (capMem.size > 60) capMem.delete(capMem.keys().next().value);
  try {
    const o = await chrome.storage.session.get("ndcap");
    const m = (o && o.ndcap) || {};
    m[url] = { h: headers, ts: Date.now() };
    const keys = Object.keys(m).sort((a, b) => m[a].ts - m[b].ts);
    while (keys.length > 60) delete m[keys.shift()];
    await chrome.storage.session.set({ ndcap: m });
  } catch (e) {}
}
async function capGet(url) {
  if (!url) return null;
  const now = Date.now();
  const hit = capMem.get(url);
  if (hit && now - hit.ts < CAPTURE_TTL) return hit.h;
  try {
    const o = await chrome.storage.session.get("ndcap");
    const m = (o && o.ndcap) || {};
    if (m[url] && now - m[url].ts < CAPTURE_TTL) return m[url].h;
    // 退一步：同 host+path（query 里的签名/时间戳可能被重定向改写）
    const u = new URL(url);
    for (const k of Object.keys(m)) {
      try {
        const v = new URL(k);
        if (v.host === u.host && v.pathname === u.pathname && now - m[k].ts < CAPTURE_TTL) return m[k].h;
      } catch (e) {}
    }
  } catch (e) {}
  return null;
}
try {
  const urls = [];
  for (const h of NETDISK_DL_HOSTS) { urls.push("*://" + h + "/*"); urls.push("*://*." + h + "/*"); }
  chrome.webRequest.onBeforeSendHeaders.addListener((d) => {
    try {
      if (!d || !d.url || !d.requestHeaders) return;
      if (d.method && d.method !== "GET") return;
      const h = {};
      for (const x of d.requestHeaders) {
        if (!x || !x.name || x.value == null) continue;
        if (SKIP_CAPTURE.test(x.name)) continue;
        if (/^sec-/i.test(x.name)) continue;           // sec-fetch-*/sec-ch-ua 不转发（aria2 用不上，且会和 UA 打架）
        h[x.name] = x.value;
      }
      capPut(d.url, h);
    } catch (e) {}
  }, { urls }, ["requestHeaders", "extraHeaders"]);
} catch (e) { try { console.warn("[SniffDL] webRequest 捕获注册失败", e); } catch (_) {} }

// 把捕获到的真实请求头并进交给桌面的 info（UA/Cookie/Referer 以浏览器实际发出的为准）
async function withCaptured(info) {
  if (!info || !info.url || !isNetdiskDl(info.url)) return info;
  const cap = await capGet(info.url);
  if (!cap) return info;
  const out = Object.assign({}, info, { headers: cap, hdr_captured: true });
  for (const k of Object.keys(cap)) {
    const kl = k.toLowerCase();
    if (kl === "cookie") out.cookie = cap[k];
    else if (kl === "user-agent") out.ua = cap[k];
    else if (kl === "referer") out.referer = cap[k];
  }
  return out;
}

// ---- Cookie 同步：网盘登录 Cookie（含 httpOnly）同步给桌面端（只进内存，不落盘）----
let _ckTimer = null;
async function syncNetdiskCookies() {
  try {
    const c = await cfg();
    if (!c.enabled || c.syncNetdiskCookies === false) return;
    const all = [];
    for (const d of NETDISK_COOKIE_DOMAINS) {
      try {
        const cs = await chrome.cookies.getAll({ domain: d });
        for (const k of cs || []) {
          all.push({ name: k.name, value: k.value, domain: k.domain, hostOnly: !!k.hostOnly,
                     path: k.path || "/", secure: !!k.secure, expirationDate: k.expirationDate || null });
        }
      } catch (e) {}
    }
    if (all.length) await callRpc("sniffdl.syncCookies", [all]);
  } catch (e) {}   // 桌面没开就算了，下次再同步
}
function scheduleCookieSync(delay) {
  if (_ckTimer) clearTimeout(_ckTimer);
  _ckTimer = setTimeout(() => { _ckTimer = null; syncNetdiskCookies(); }, delay || 2000);
}
try {
  chrome.cookies.onChanged.addListener((ch) => {
    try {
      const d = ((ch && ch.cookie && ch.cookie.domain) || "").replace(/^\./, "").toLowerCase();
      if (NETDISK_COOKIE_DOMAINS.some((x) => d === x || d.endsWith("." + x))) scheduleCookieSync(2500);
    } catch (e) {}
  });
} catch (e) {}
try { chrome.runtime.onStartup.addListener(() => scheduleCookieSync(4000)); } catch (e) {}
scheduleCookieSync(3000);   // SW 每次被唤醒都顺手同步一次（节流 3s）

// ---- 入口 1（0.5.12 重新引入，但加了"用户手势闸门"防洪水）----
// 教训留档：当年无差别监听 onCreated → 后台/自动续传/别的下载器/油猴触发的下载全被静默劫持 = 洪水。
// 本版只在"该标签页 4 秒内刚有用户真实点击"时才接管浏览器下载事件，命中后取消浏览器下载并弹
// 网页中央「下载文件信息」确认卡，用户点"开始下载"才真下——绝不静默、绝不在你没点时抢。
// 这样能抓到 content.js 点击截击器漏掉的"程序化下载"（网盘 JS 算出 CDN 直链再触发那种，IDM 同款能力）。
// 手势时间戳存 chrome.storage.session：跨 service worker 重启也不丢（MV3 的 SW 会被回收，
// 纯内存 Map 会在"点击→下载"之间被清掉，导致 onCreated 误判无手势而漏抢——这是之前没弹卡的根因）。
async function setGesture(tabId) { try { await chrome.storage.session.set({ ['g' + tabId]: Date.now() }); } catch (e) {} }
async function getGesture(tabId) { try { const o = await chrome.storage.session.get('g' + tabId); return o['g' + tabId] || 0; } catch (e) { return 0; } }
async function clearGesture(tabId) { try { await chrome.storage.session.remove('g' + tabId); } catch (e) {} }

// 手势门控下的接管判定：确认是用户刚点的之后，只要是"真实文件下载"(http/有扩展名或有大小/非内联预览/非碎屑)就抢，
// 不再限制"仅媒体"——因为已确认用户主动发起，抢 .exe/.zip 也符合预期。
function shouldTakeGesture(item) {
  const url = item.finalUrl || item.url || "";
  if (!/^(https?|ftp):/i.test(url)) return false;          // blob:/data: 交给浏览器（aria2 拿不到）
  if (isJunkAutolink(item, url)) return false;             // fwlink/碎屑/文本预览：不抢
  const ext = extFromUrl(url) || extFromUrl(item.finalUrl || "");
  if (!ext) {
    const mt = (item.mime || "").toLowerCase().split(";")[0].trim();
    if (/^(image|video|audio|text)\//.test(mt)) return false;   // 无扩展名的内联预览放行浏览器
  }
  return true;
}

// 下载项带明确文件名 + 真实文件扩展名（网盘直链那种）——即使手势信号没抓到也应当接管
function hasRealFileExt(item) {
  try {
    const fn = decodeURIComponent((item.filename || "").split(/[\\/]/).pop());
    return /\.(exe|msi|msp|apk|ipa|deb|rpm|dmg|pkg|jar|zip|rar|7z|tar|gz|tgz|bz2|xz|iso|img|bin|cab|wim|esd|gho|pdf|epub|mobi|docx?|xlsx?|pptx?|mp4|mkv|mov|avi|webm|flv|wmv|m4v|ts|mp3|flac|m4a|wav|opus|ogg|whl)$/i.test(fn);
  } catch (e) { return false; }
}

chrome.downloads.onCreated.addListener(async (item) => {
  try {
    try { console.log('[SniffDL] onCreated', item.url, 'tab=' + item.tabId, 'state=' + item.state, 'size=' + item.totalBytes, 'mime=' + item.mime); } catch (e) {}
    const c = await cfg();
    if (!c.enabled) return;
    let tabId = item.tabId;
    // 表单 POST 触发的下载：DownloadItem 不带 tabId → 用 webRequest 记下的发起标签
    if ((tabId == null || tabId < 0) && postGet(item.url)) { tabId = postTab(item.url); if (tabId != null) await setGesture(tabId); }
    if (tabId == null || tabId < 0) return;
    if (await isBypassed(item)) { try { console.log('[SniffDL] onCreated skip(Alt/交回浏览器)', item.url); } catch (e) {} return; }
    // 0.7.0：v3 内核 → 先按内核规则过一遍；离线 → 不抢，提示一声
    const rc = await getRules(false);
    if (rc.mode === "offline") { notifyOffline(rc.error); return; }
    let _pg = "";
    if (rc.mode === "v3") {
      try { const tb0 = await chrome.tabs.get(tabId); _pg = (tb0 && tb0.url) || ""; } catch (e) {}
      const jr = judgeByRules(rc.rules, item.finalUrl || item.url || "", _pg || item.referrer || "", item.totalBytes || 0);
      if (!jr.take) { try { console.log('[SniffDL] onCreated skip(内核规则)', jr.why, item.url); } catch (e) {} return; }
    }
    const g = await getGesture(tabId);
    const hasGesture = (Date.now() - g) <= 4000;
    // 0.6.0：网盘下载（下载 CDN 域，或在网盘网页上发起）→ 任何文件类型都接管。
    // 防洪水护栏：只接"刚刚新建"的下载（startTime 20 秒内）——Chrome 开机自动续传的旧下载 startTime 很早，不碰。
    const _u0 = item.finalUrl || item.url || "";
    let _ref0 = item.referrer || "";
    const _fresh = (() => { try { return (Date.now() - Date.parse(item.startTime)) < 20000; } catch (e) { return false; } })();
    const isNd = _fresh && (isNetdiskDl(_u0) || isNetdiskDl(item.url || "") || isNetdiskPage(_ref0));
    // 有近期手势，或下载项带明确文件名+真实扩展名（网盘直链那种），或网盘下载 → 才抢；都不是 = 后台噪声，放行（防洪水）
    if (!hasGesture && !hasRealFileExt(item) && !isNd) { try { console.log('[SniffDL] onCreated skip(无手势且非明确文件)', item.url, item.filename); } catch (e) {} return; }
    if (!shouldTakeGesture(item)) { try { console.log('[SniffDL] onCreated skip(过滤)', item.url); } catch (e) {} return; }
    if (hasGesture) await clearGesture(tabId);             // 一次点击只接管一次
    const url = item.finalUrl || item.url || "";
    let referer = "";
    try { const tb = await chrome.tabs.get(tabId); referer = (tb && tb.url) || ""; } catch (e) {}
    let info = { url, filename: baseName(item, url), referer: referer || item.referrer || "", size: item.totalBytes || 0, source: "browser", mime: item.mime || "" };
    info = await withCaptured(info);     // 网盘：带上浏览器真实发出的 UA/Cookie/Referer
    if (isNd) scheduleCookieSync(200);
    cancelling.add(item.id);                               // 先取消浏览器自己的下载，再交给看板娘
    try { await chrome.downloads.cancel(item.id); } catch (e) {}
    try { chrome.downloads.removeFile(item.id); } catch (e) {}
    try { chrome.downloads.erase(item.id); } catch (e) {}
    if (rc.mode === "v3") {
      try { await v3Take(info, hasGesture, rc.rules); }
      catch (e) {
        if (e.offline) { notifyOffline(e.message); await markBypass(tabId, url); try { await chrome.downloads.download({ url }); } catch (_) {} }
        else notifyFail(info.filename || url, e.message || String(e));
      }
      return;
    }
    if (!(await pushCard(tabId, info))) await silentFallback(info);
  } catch (e) {}
});

// 取消是我们主动做的 → 把那条 .crdownload 从下载记录里抹掉，保持干净
chrome.downloads.onChanged.addListener((delta) => {
  if (cancelling.has(delta.id)) {
    cancelling.delete(delta.id);
    try { chrome.downloads.removeFile(delta.id); } catch (e) {}
    try { chrome.downloads.erase(delta.id); } catch (e) {}
  }
});

// ---- 入口 2：右键单项（自适应 link / image / selection / page）----
const MENUS = [
  { id: "sniffdl", title: "用 SniffDL 下载", contexts: ["link", "image", "selection", "page"] },
  { id: "sniffdl-all", title: "用 SniffDL 下载全部链接", contexts: ["page", "link", "selection", "image"] },   // 0.7.0
  { id: "sniffdl-analyze", title: "用 SniffDL 分析本页", contexts: ["page"] },                                // 0.7.0
];
self.__sdlMenuIds = [];
chrome.runtime.onInstalled.addListener(() => {
  try {
    chrome.contextMenus.removeAll(() => {
      for (const m of MENUS) {
        try { chrome.contextMenus.create(m, () => { void chrome.runtime.lastError; self.__sdlMenuIds.push(m.id); }); } catch (e) {}
      }
    });
  } catch (e) {}
  getRules(true);
});

// 0.7.0：收集页面全部链接（去重、去 #锚点、只要 http/https/ftp），推到内核「待确认」
async function collectLinks(tabId) {
  const res = await chrome.scripting.executeScript({
    target: { tabId, allFrames: true },
    func: () => Array.from(document.querySelectorAll("a[href],area[href]")).map((a) => ({
      url: a.href, text: ((a.getAttribute("download") || a.textContent || a.title || "") + "").trim().slice(0, 120),
    })),
  });
  const seen = new Set(), out = [];
  for (const fr of res || []) for (const it of (fr && fr.result) || []) {
    let u = (it.url || "").trim();
    if (!/^(https?|ftp):/i.test(u)) continue;
    try { const x = new URL(u); x.hash = ""; u = x.href; } catch (e) { continue; }
    if (seen.has(u)) continue;
    seen.add(u); out.push({ url: u, name: it.text || "" });
  }
  return out;
}
async function menuAllLinks(tab) {
  const rc = await getRules(true);
  if (rc.mode === "offline") throw Object.assign(new Error(rc.error), { offline: true });
  if (rc.mode !== "v3") throw new Error("需要 SniffDL v3（待确认区）才能批量收集链接");
  const pageUrl = tab.url || "";
  let items = await collectLinks(tab.id);
  items = items.filter((x) => x.url !== pageUrl).map((x) => ({ ...x, referer: pageUrl }));
  if (!items.length) { notifyFail("下载全部链接", "本页没有可下载的链接"); return { ok: true, added: 0 }; }
  const r = await callRpc("sniffdl.grabber.push", [items, "ext-page"]);
  notifyTaken("已推到 SniffDL「待确认」：新增 " + ((r && r.added) || 0) + " 条（本页共 " + items.length + " 个链接）", "grab-" + Date.now());
  return r;
}
async function menuAnalyze(tab) {
  const rc = await getRules(true);
  if (rc.mode === "offline") throw Object.assign(new Error(rc.error), { offline: true });
  if (rc.mode !== "v3") {          // 旧桌面端：老的本页嗅探篮
    if (!(await openSniffPanel(tab.id))) await sniffTab(tab.id);
    return { ok: true, legacy: true };
  }
  const r = await callRpc("sniffdl.grabber.push", [[{ url: tab.url, name: tab.title || "", referer: tab.url }], "ext-analyze"]);
  notifyTaken("已把本页交给 SniffDL 分析，结果在「待确认」里识别下载", "ana-" + Date.now());
  return r;
}
async function handleMenu(info, tab) {
  if (info && (info.menuItemId === "sniffdl-all" || info.menuItemId === "sniffdl-analyze")) {
    try {
      if (!tab || tab.id == null) throw new Error("没拿到当前标签页");
      return info.menuItemId === "sniffdl-all" ? await menuAllLinks(tab) : await menuAnalyze(tab);
    } catch (e) {
      if (e.offline) notifyOffline(e.message); else notifyFail(info.menuItemId === "sniffdl-all" ? "下载全部链接" : "分析本页", e.message || String(e));
      return { ok: false, error: e.message || String(e) };
    }
  }
  return legacyMenu(info, tab);
}
self.__sdlMenu = handleMenu;   // 测试入口（原生右键菜单 Playwright 点不到）
chrome.contextMenus.onClicked.addListener((info, tab) => { handleMenu(info, tab); });

async function legacyMenu(info, tab) {
  if (!info || info.menuItemId !== "sniffdl") return;
  const referer = info.pageUrl || (tab && tab.url) || "";
  const tabId = (tab && tab.id != null) ? tab.id : -1;
  try {
    if (info.srcUrl) {
      // 图片：下载这张图 → 网页中央确认卡
      if (!(await pushCard(tabId, { url: info.srcUrl, filename: "", referer, size: 0, source: "menu-image" })))
        await silentFallback({ url: info.srcUrl, filename: "", referer });
    } else if (info.linkUrl) {
      // 链接：下载此链接 → 网页中央确认卡
      if (!(await pushCard(tabId, { url: info.linkUrl, filename: "", referer, size: 0, source: "menu-link" })))
        await silentFallback({ url: info.linkUrl, filename: "", referer });
    } else if (info.selectionText) {
      // 选中文本：批量其中的链接；单链走网页中央卡
      const urls = extractUrls(info.selectionText);
      if (urls.length === 1) {
        if (!(await pushCard(tabId, { url: urls[0], filename: "", referer, size: 0, source: "menu-selection" })))
          await silentFallback({ url: urls[0], filename: "", referer });
      } else if (urls.length > 1) {
        const gids = [];
        for (const u of urls) { try { gids.push(await autoAdd(u, referer)); } catch (e) {} }
        notifyTaken("已交给看板娘批量下载：" + gids.length + " 个链接", "sel-" + Date.now());
        watchBatch(gids, "选中链接");
      } else {
        const t = (info.selectionText || "").trim();
        if (/^https?:/i.test(t)) {
          if (!(await pushCard(tabId, { url: t, filename: "", referer, size: 0, source: "menu-selection" })))
            await silentFallback({ url: t, filename: "", referer });
        }
      }
    } else if (tab && tab.id != null) {
      // 页面：在本页正中央打开嗅探篮（桌面端深度嗅探）；打不开面板（受限页）才回落老的一次性 DOM 扫
      if (!(await openSniffPanel(tab.id))) await sniffTab(tab.id);
    }
  } catch (e) {
    notifyFail("右键下载", e.message || String(e));
  }
}

function extractUrls(text) {
  const m = (text || "").match(/https?:\/\/[^\s"'<>（）()，,、]+/g) || [];
  return Array.from(new Set(m));
}

async function sniffTab(tabId) {
  let urls = [];
  try {
    const injected = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => {
        const set = new Set();
        document.querySelectorAll("video,audio").forEach((v) => {
          if (v.src) set.add(v.src);
          v.querySelectorAll && v.querySelectorAll("source").forEach((s) => { if (s.src) set.add(s.src); });
        });
        document.querySelectorAll("a[href]").forEach((a) => {
          const h = a.href || "";
          if (/\.(mp4|mkv|webm|m4v|mov|flv|ts|m3u8|mp3|flac|zip|rar|7z|pdf|exe|msi|apk|iso)(\?|#|$)/i.test(h)) set.add(h);
        });
        try {
          performance.getEntriesByType("resource").forEach((e) => {
            if (/\.(mp4|m3u8|webm|ts|mp3|flv|mkv|f4v)(\?|$)/i.test(e.name)) set.add(e.name);
          });
        } catch (_) {}
        return Array.from(set).filter((u) => /^https?:/i.test(u)).slice(0, 100);
      },
    });
    urls = (injected && injected[0] && injected[0].result) || [];
  } catch (e) {
    notifyFail("页面嗅探", "无法在此页注入脚本：" + (e.message || String(e)));
    return;
  }
  if (!urls.length) { notifyTaken("本页没嗅到可下载的媒体", "sniff-empty"); return; }
  let referer = "";
  try { referer = (await chrome.tabs.get(tabId)).url || ""; } catch (e) {}
  if (urls.length === 1) {
    if (!(await pushCard(tabId, { url: urls[0], filename: "", referer, size: 0, source: "sniff" })))
      await silentFallback({ url: urls[0], filename: "", referer });
  } else {
    const gids = [];
    for (const u of urls) { try { gids.push(await autoAdd(u, referer)); } catch (e) {} }
    notifyTaken("已交给看板娘批量下载：" + gids.length + " 个媒体", "sniff-" + Date.now());
    watchBatch(gids, "本页媒体");
  }
}

function notifyTaken(name, gid) {
  try {
    chrome.notifications.create("sniff-taken-" + gid, {
      type: "basic",
      title: "已交给 SniffDL ♪",
      message: String(name),
    });
  } catch (e) {}
}
function notifyFail(name, msg) {
  try {
    chrome.notifications.create("sniff-fail-" + Date.now(), {
      type: "basic",
      title: "接管失败",
      message: name + "：" + msg + "（请确认 SniffDL 已开启本地下载服务）",
    });
  } catch (e) {}
}

// 把「下载文件信息」确认卡拉回网页正中央弹（右键 / 浏览器自动接管也走网页卡，不再跳桌面窗）
async function pushCard(tabId, info) {
  if (tabId == null || tabId < 0) return false;
  const send = async () => {
    try {
      const r = await chrome.tabs.sendMessage(tabId, { type: "dl_card", info });
      return !!(r && r.ok);
    } catch (e) { return false; }
  };
  if (await send()) return true;
  // 该页没注入 content script（多为插件重载后标签页没刷新）→ 现场注入再试一次
  try {
    await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] });
  } catch (e) { return false; }
  await new Promise((res) => setTimeout(res, 150));
  return await send();
}

// 在本页正中央打开「嗅探篮」并跑桌面端深度嗅探（复用浮标面板，替代老的 sniffTab 一次性 DOM 扫）
async function openSniffPanel(tabId) {
  if (tabId == null || tabId < 0) return false;
  const send = async () => {
    try {
      const r = await chrome.tabs.sendMessage(tabId, { type: "open_sniff_panel" });
      return !!(r && r.ok);
    } catch (e) { return false; }
  };
  if (await send()) return true;
  // 该页没注入 content script（多为插件重载后标签页没刷新）→ 现场注入再试一次
  try {
    await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] });
  } catch (e) { return false; }
  await new Promise((res) => setTimeout(res, 150));
  return await send();
}

// 批量：桌面后台下，轮询到全部结束后通知一声（SW 若被回收则尽力而为，桌面看板娘也会各自反馈）
function notifyBatch(label, okN, failN) {
  try {
    chrome.notifications.create("sniff-batch-" + Date.now(), {
      type: "basic",
      title: failN ? "整页下载结束（有失败）" : "整页下载完成 ♪",
      message: label + "：成功 " + okN + " 项" + (failN ? ("，失败 " + failN + " 项") : ""),
    });
  } catch (e) {}
}
function watchBatch(gids, label) {
  gids = (gids || []).filter(Boolean);
  if (!gids.length) return;
  const total = gids.length;
  const iv = setInterval(async () => {
    let ok = 0, fail = 0;
    try {
      const res = await Promise.all(gids.map((g) => callRpc("aria2.getDownloadResult", [g]).catch(() => null)));
      res.forEach((s) => { if (!s) return; if (s.status === "complete") ok++; else if (s.status === "error") fail++; });
    } catch (e) { return; }
    if (ok + fail >= total) { clearInterval(iv); notifyBatch(label, ok, fail); }
  }, 2500);
  setTimeout(() => { try { clearInterval(iv); } catch (e) {} }, 60 * 60 * 1000);
}

// 供弹窗调用：探活 + 换肤 + 任务 + 手动推送 / 嗅探
chrome.runtime.onMessage.addListener((msg, _sender, sendResp) => {
  if (msg && (msg.type === "sniff_list" || msg.type === "sniff_push")) return;   // 0.8.0 嗅探由下方监听器处理
  (async () => {
    try {
      if (msg && msg.type === "gesture") {
        if (_sender && _sender.tab && _sender.tab.id != null) {
          if (msg.bypass) await markBypass(_sender.tab.id, msg.url || "");   // 0.7.0 Alt 点击：放行浏览器
          else await setGesture(_sender.tab.id);
        }
        sendResp({ ok: true }); return;
      }
      if (msg && msg.type === "rules") {        // 0.7.0：内容脚本取接管规则
        const rc = await getRules(!!msg.force);
        sendResp({ ok: true, mode: rc.mode, rules: rc.rules, error: rc.error }); return;
      }
      if (msg && msg.type === "click_dl") {     // 0.7.0：点击接管（v3 内核）
        const r = await handleClickDl(msg, _sender && _sender.tab);
        if (r) { sendResp(r); return; }
        // 旧桌面端：与 chip_dl 相同（网页中央卡）
        const cTab = (_sender && _sender.tab && _sender.tab.id != null) ? _sender.tab.id : -1;
        const cardInfo = { url: msg.url, filename: "", referer: msg.referer || "", size: 0, source: "chip" };
        const pushed = await pushCard(cTab, cardInfo);
        sendResp({ ok: true, gid: pushed ? "card" : await silentFallback(cardInfo) }); return;
      }
      if (msg && msg.type === "ping") {
        const rc = await getRules(true);
        if (rc.mode === "offline") { sendResp({ ok: false, offline: true, error: rc.error }); return; }
        let st = {}, th = null;
        try { st = await callRpc("sniffdl.status", []); } catch (e) {
          try { const pg = await callRpc("sniffdl.ping", []); st = { rpc_port: pg && pg.port, version: pg && pg.version }; } catch (_) {}
        }
        try { th = await callRpc("sniffdl.theme", []); } catch (e) {}
        sendResp({ ok: true, status: st || {}, theme: th, mode: rc.mode, rules: rc.rules });
      } else if (msg && msg.type === "save") {
        await chrome.storage.local.set(msg.data || {});
        sendResp({ ok: true });
      } else if (msg && msg.type === "load") {
        sendResp({ ok: true, data: await chrome.storage.local.get(DEFAULTS) });
      } else if (msg && msg.type === "tasks") {
        const active = await callRpc("aria2.tellActive", []);
        const waiting = await callRpc("aria2.tellWaiting", [0, 8]);
        const stopped = await callRpc("aria2.tellStopped", [0, 8]);
        sendResp({ ok: true, active, waiting, stopped });
      } else if (msg && msg.type === "dl") {
        const url = (msg.url || "").trim();
        if (!url) { sendResp({ ok: false, error: "empty url" }); return; }
        const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
        const pTab = (tabs && tabs[0] && tabs[0].id != null) ? tabs[0].id : -1;
        const cardInfo = { url, filename: msg.filename || "", referer: msg.referer || "", size: msg.size || 0, source: "popup" };
        const pushed = await pushCard(pTab, cardInfo);
        const gid = pushed ? "card" : await silentFallback(cardInfo);
        sendResp({ ok: true, gid });
      } else if (msg && msg.type === "sniff") {
        const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
        const tab = tabs && tabs[0];
        if (tab && tab.id != null) {
          const ok = await openSniffPanel(tab.id);
          if (!ok) await sniffTab(tab.id);   // 打不开面板（如受限页）才回落老逻辑
          sendResp({ ok: true });
        }
        else sendResp({ ok: false, error: "no active tab" });
      } else if (msg && msg.type === "chip_dl") {
        // 页面小框点「下载此链接/此视频」→ 优先在本页正中央弹网页下载卡；弹不出才静默后台下，绝不跳桌面
        const url = (msg.url || "").trim();
        if (!url) { sendResp({ ok: false, error: "empty url" }); return; }
        const cTab = (_sender && _sender.tab && _sender.tab.id != null) ? _sender.tab.id : -1;
        const cardInfo = { url, filename: msg.filename || "", referer: msg.referer || "", size: msg.size || 0, source: "chip" };
        const pushed = await pushCard(cTab, cardInfo);
        const gid = pushed ? "card" : await silentFallback(cardInfo);
        sendResp({ ok: true, gid });
      } else if (msg && msg.type === "sniff_start") {
        let url = (msg.url || "").trim();
        if (!url) {
          const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
          url = (tabs && tabs[0] && tabs[0].url) || "";
        }
        if (!/^https?:/i.test(url)) { sendResp({ ok: false, error: "no page url" }); return; }
        const scope = msg.scope === "page" ? "page" : "all";
        const taskId = await callRpc("sniffdl.sniff", [url, scope]);
        sendResp({ ok: true, taskId });
      } else if (msg && msg.type === "sniff_result") {
        const r = await callRpc("sniffdl.sniffResult", [msg.taskId || ""]);
        sendResp({ ok: true, ...(r || {}) });
      } else if (msg && msg.type === "badge_dl") {
        // 浮标点某条 → 优先在本标签页正中央弹网页下载卡；弹不出才静默后台下，绝不弹桌面确认框
        const url = (msg.url || "").trim();
        if (!url) { sendResp({ ok: false, error: "empty url" }); return; }
        const bTab = (_sender && _sender.tab && _sender.tab.id != null) ? _sender.tab.id : -1;
        const cardInfo = { url, filename: msg.filename || "", referer: msg.referer || "", size: msg.size || 0, source: "badge" };
        const pushed = await pushCard(bTab, cardInfo);
        const gid = pushed ? "card" : await silentFallback(cardInfo);
        sendResp({ ok: true, gid });
      } else if (msg && msg.type === "badge_batch") {
        const list = Array.isArray(msg.items) ? msg.items : [];
        const gids = [];
        for (const it of list) {
          if (!it || !it.url) continue;
          try { gids.push(await autoAdd(it.url, it.referer || "", it.filename || "")); } catch (e) {}
        }
        notifyTaken("整页已交给看板娘：" + gids.length + " 项", "badge-batch-" + Date.now());
        watchBatch(gids, "整页视频");
        sendResp({ ok: true, n: gids.length });
      } else if (msg && msg.type === "rpc") {
        // 网页中央卡透传：分类/浏览/起下载/进度/打开文件夹 等，全部经桌面 aria2 兼容 RPC
        const method = msg.method || "";
        const allowed = /^(aria2\.(getDownloadResult|tellActive|tellWaiting|tellStopped|pause|unpause|remove|forceRemove)|sniffdl\.(dlDefaults|addCategory|pickDir|listDirs|add|reveal|openFile|recentSniff|clearRecentSniff|theme|status))$/;
        if (!allowed.test(method)) { sendResp({ ok: false, error: "method not allowed" }); return; }
        let params = Array.isArray(msg.params) ? msg.params : [];
        if (method === "sniffdl.add") {
          // 内容脚本读不到 httpOnly Cookie（B站"无可用格式"就是缺登录 Cookie）→ 后台补齐 Cookie+Referer 再转发
          let info = (params[0] && typeof params[0] === "object") ? Object.assign({}, params[0]) : {};
          const u = (info.url || "").trim();
          if (u && isNetdiskDl(u)) info = await withCaptured(info);   // 0.6.0：网盘直链用浏览器真实请求头
          if (u) {
            if (!info.cookie) { try { info.cookie = await cookieHeader(u); } catch (e) {} }
            if (!info.referer) {
              let ref = "";
              try { ref = (_sender && _sender.tab && _sender.tab.url) || ""; } catch (e) {}
              if (!ref) { try { ref = new URL(u).origin; } catch (e) {} }
              info.referer = ref;
            }
          }
          params = [info];
        }
        const result = await callRpc(method, params);
        sendResp({ ok: true, result });
      } else if (msg && msg.type === "theme") {
        // 供页面小框取当前世界配色（拿不到就回落粉）
        try {
          const th = await callRpc("sniffdl.theme", []);
          sendResp({ ok: true, theme: th });
        } catch (e) {
          sendResp({ ok: false, theme: { accent: "#3E6FD0", palette: {} } });
        }
      } else {
        sendResp({ ok: false, error: "unknown" });
      }
    } catch (e) {
      sendResp({ ok: false, error: e.message || String(e) });
    }
  })();
  return true; // 异步响应
});

// ================= 0.8.0：webRequest 媒体嗅探（clean-room：只看响应头 Content-Type / 扩展名）=================
// 每个标签页最多留 50 条；只观察不拦截。popup 经 sniff_list 取、sniff_push 一键推给内核（带 Cookie/UA/Referer）。
const SNIFF_EXT = /\.(m3u8|mpd|mp4|m4v|webm|flv|mkv|mov|mp3|m4a|aac|flac|ogg|wav)(?:$|[?#])/i;
const SNIFF_CT = /^(video\/|audio\/|application\/(vnd\.apple\.mpegurl|x-mpegurl|dash\+xml|x-flv)|application\/ogg)/i;
function sniffClassify(url, ct) {
  ct = String(ct || "").split(";")[0].trim().toLowerCase();
  if (/^image\//.test(ct) || /^(text|font)\//.test(ct) || /javascript|json|css|html/.test(ct)) return "";
  let path = "";
  try { path = new URL(url).pathname + (new URL(url).search || ""); } catch (e) { return ""; }
  const m = path.match(SNIFF_EXT);
  if (/mpegurl/.test(ct) || (m && m[1].toLowerCase() === "m3u8")) return "m3u8";
  if (/dash\+xml/.test(ct) || (m && m[1].toLowerCase() === "mpd")) return "mpd";
  if (SNIFF_CT.test(ct)) return ct.startsWith("audio/") ? "audio" : "video";
  if (m) return /^(mp3|m4a|aac|flac|ogg|wav)$/i.test(m[1]) ? "audio" : "video";
  return "";
}
const sniffMem = new Map();   // tabId -> [{url,kind,size,ts,type,headers}]
const sniffReq = new Map();   // requestId -> request headers
function sniffAdd(tabId, item) {
  const l = sniffMem.get(tabId) || [];
  if (l.some((x) => x.url === item.url)) return;
  l.push(item);
  while (l.length > 50) l.shift();
  sniffMem.set(tabId, l);
  try { chrome.action.setBadgeText({ tabId, text: String(l.length) }); chrome.action.setBadgeBackgroundColor({ tabId, color: "#1E90FF" }); } catch (e) {}
}
try {
  const all = { urls: ["<all_urls>"] };
  chrome.webRequest.onBeforeSendHeaders.addListener((d) => {
    try {
      if (!d || d.tabId < 0 || !d.requestHeaders) return;
      const h = {};
      for (const x of d.requestHeaders) {
        if (!x || !x.name || x.value == null || SKIP_CAPTURE.test(x.name) || /^sec-/i.test(x.name)) continue;
        h[x.name] = x.value;
      }
      sniffReq.set(d.requestId, h);
      if (sniffReq.size > 200) sniffReq.delete(sniffReq.keys().next().value);
    } catch (e) {}
  }, all, ["requestHeaders", "extraHeaders"]);
  chrome.webRequest.onHeadersReceived.addListener((d) => {
    try {
      if (!d || d.tabId < 0 || !d.url || !/^https?:/i.test(d.url)) return;
      if (d.statusCode && d.statusCode >= 400) return;
      let ct = "", cl = 0;
      for (const x of (d.responseHeaders || [])) {
        const n = (x.name || "").toLowerCase();
        if (n === "content-type") ct = x.value || "";
        else if (n === "content-length") cl = parseInt(x.value, 10) || 0;
        else if (n === "content-range") { const m = /\/(\d+)$/.exec(x.value || ""); if (m) cl = parseInt(m[1], 10) || cl; }
      }
      const kind = sniffClassify(d.url, ct);
      if (!kind) return;
      sniffAdd(d.tabId, { url: d.url, kind, size: cl, ts: Date.now(), type: ct, headers: sniffReq.get(d.requestId) || {} });
      sniffReq.delete(d.requestId);
    } catch (e) {}
  }, all, ["responseHeaders", "extraHeaders"]);
  chrome.tabs.onRemoved.addListener((id) => sniffMem.delete(id));
  chrome.webNavigation.onBeforeNavigate.addListener((d) => {
    if (d.frameId === 0) { sniffMem.delete(d.tabId); try { chrome.action.setBadgeText({ tabId: d.tabId, text: "" }); } catch (e) {} }
  });
} catch (e) { try { console.warn("[SniffDL] 嗅探注册失败", e); } catch (_) {} }

chrome.runtime.onMessage.addListener((msg, _s, sendResp) => {
  if (!msg || (msg.type !== "sniff_list" && msg.type !== "sniff_push")) return;
  (async () => {
    try {
      const tabId = msg.tabId;
      const l = (sniffMem.get(tabId) || []).slice();
      if (msg.type === "sniff_list") { sendResp({ ok: true, items: l.map((x) => ({ url: x.url, kind: x.kind, size: x.size, type: x.type })) }); return; }
      const it = l.find((x) => x.url === msg.url);
      if (!it) { sendResp({ ok: false, error: "该条已过期" }); return; }
      let tab = null; try { tab = await chrome.tabs.get(tabId); } catch (e) {}
      const hd = Object.assign({}, it.headers);
      if (!hd.Referer && !hd.referer && tab && tab.url) hd.Referer = tab.url;
      const ck = await cookieHeader(it.url).catch(() => "");
      if (ck && !hd.Cookie && !hd.cookie) hd.Cookie = ck;
      const r = await callRpc("sniffdl.intercept", [{ url: it.url, filename: "", referer: hd.Referer || hd.referer || "", cookie: hd.Cookie || hd.cookie || "", ua: hd["User-Agent"] || navigator.userAgent, headers: hd, size: it.size || 0, source: "chip", confirm: true }]);
      sendResp({ ok: true, result: r });
    } catch (e) { sendResp({ ok: false, error: e.message || String(e), offline: !!e.offline }); }
  })();
  return true;
});
self.__sdlSniffClassify = sniffClassify;


// 内核不在线时经 Native Messaging（com.sniffdl.host）一键拉起 SniffDL
async function launchSniffDL() {
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendNativeMessage("com.sniffdl.host", { action: "launch", port: 6800 }, (r) => {
        if (chrome.runtime.lastError) resolve({ ok: false, error: chrome.runtime.lastError.message });
        else resolve(r || { ok: false });
      });
    } catch (e) { resolve({ ok: false, error: String(e) }); }
  });
}
