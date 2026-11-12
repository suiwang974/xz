/* SniffDL 接管弹窗：读当前世界主题自动换肤 + 连接状态 + 设置 + 实时任务 */

const MOTIF_EMOJI = {
  heart: "💗", sakura: "🌸", star: "⭐", moon: "🌙", torii: "⛩️",
  gear: "⚙️", bat: "🦇", cross: "🕯️", snow: "❄️", leaf: "🍃",
  bell: "🔔", wand: "🪄", crystal: "🔮", flame: "🔥", cloud: "☁️",
};

const $ = (id) => document.getElementById(id);

const MOTIF_OK = { heart: 1, star: 1, blossom: 1, diamond: 1 };

function applyTheme(th) {
  if (!th) return;
  const p = th.palette || {};
  const root = document.documentElement.style;
  const set = (v, c) => { if (c) root.setProperty(v, c); };
  set("--bg", p.bg); set("--card", p.card); set("--ink", p.ink);
  set("--ink-l", p.ink_l); set("--line", p.line);
  set("--accent", th.accent || p.pink || p.purple || p.gold);
  set("--accent2", p.pink2 || p.pink_d || p.plum || p.navy || th.accent);
  $("charName").textContent = th.char ? (th.char + " · SniffDL") : "SniffDL";
  $("charRole").textContent = th.role || th.label || "看板娘下载接管";

  // 背景纹样：仅认四种真实 SVG symbol，其余回落心形
  const motif = MOTIF_OK[th.motif] ? th.motif : "heart";
  ["motifUseA", "motifUseB"].forEach((id) => {
    const u = $(id);
    if (u) u.setAttribute("href", "#m-" + motif);
  });

  // 头像：桌面端传来的 base64 立绘优先，加载成功就隐藏 emoji 占位
  const img = $("avatarImg");
  const ph = $("avatarMotif");
  if (img && ph) {
    if (th.avatar) {
      ph.style.display = "none";
      img.style.display = "block";
      img.onerror = () => { img.style.display = "none"; ph.style.display = ""; ph.textContent = MOTIF_EMOJI[motif] || "✦"; };
      img.src = "data:image/png;base64," + th.avatar;
    } else {
      img.removeAttribute("src");
      img.style.display = "none";
      ph.style.display = "";
      ph.textContent = MOTIF_EMOJI[motif] || "✦";
    }
  } else if (ph) {
    ph.textContent = MOTIF_EMOJI[motif] || "✦";
  }
}

function setConn(on, text) {
  $("connDot").className = "dot " + (on ? "on" : "off");
  $("connText").textContent = text;
}

function renderTasks(active, waiting, stopped) {
  const ul = $("taskList");
  const rows = [];
  (active || []).forEach((t) => rows.push({ name: t.name || t.gid, st: "active",
    extra: t.totalLength && t.totalLength !== "0"
      ? Math.floor((+t.downloadedLength / +t.totalLength) * 100) + "%" : "…", }));
  (waiting || []).forEach((t) => rows.push({ name: t.name || t.gid, st: "waiting", extra: "稍后" }));
  (stopped || []).forEach((t) => rows.push({ name: t.name || t.gid, st: t.status,
    extra: t.status === "complete" ? "✓" : (t.errorCode !== "0" ? "✗" : t.status) }));
  if (!rows.length) { ul.innerHTML = '<li class="empty">暂无任务</li>'; return; }
  ul.innerHTML = rows.slice(0, 12).map((r) =>
    `<li><span class="nm" title="${esc(r.name)}">${esc(r.name)}</span>` +
    `<span class="st ${r.st}">${esc(String(r.extra))}</span></li>`).join("");
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function loadSettingsIntoForm(data) {
  $("enabled").checked = !!data.enabled;
  $("port").value = data.port || 6800;
  $("token").value = data.token || "";
  $("minSizeMB").value = data.minSizeMB || 0;
  $("onlyExtensions").value = data.onlyExtensions || "";
}

function refreshPing() {
  chrome.runtime.sendMessage({ type: "ping" }, (resp) => {
    if (resp && resp.ok) {
      applyTheme(resp.theme);
      setConn(true, "已连接 " + (resp.status && resp.status.rpc_port ? ":" + resp.status.rpc_port : ""));
      if (resp.mode === "v3") {   // 0.7.0：新内核下接管规则以 SniffDL 设置为准
        const m = $("statusMsg");
        const r = resp.rules || {};
        m.textContent = "接管规则来自 SniffDL 设置 › 高级 › 浏览器接管" + (r.enabled === false ? "（当前已关闭接管）" : "") +
          (r.alt_bypass !== false ? "；按住 Alt 点击不接管" : "");
        m.className = "statusMsg ok";
      }
    } else {
      setConn(false, resp && resp.offline ? "SniffDL 没有运行" : "未连接");
      const m = $("statusMsg");
      m.textContent = (resp && resp.offline) ? (resp.error + "。内核没运行时，下载由浏览器自己处理。")
        : "连不上本机 SniffDL：" + ((resp && resp.error) || "请在 SniffDL 设置里开启「本地下载服务(RPC)」");
      m.className = "statusMsg bad";
    }
  });
}

function refreshTasks() {
  chrome.runtime.sendMessage({ type: "tasks" }, (resp) => {
    if (resp && resp.ok) renderTasks(resp.active, resp.waiting, resp.stopped);
  });
}

function renderRecent(items) {
  const ul = $("recentSniff");
  if (!ul) return;
  if (!items || !items.length) {
    ul.innerHTML = '<li class="empty">还没有记录，去视频页嗅探一次就有了</li>';
    return;
  }
  ul.innerHTML = items.map((r, i) => {
    const t = r.title || r.url || "";
    return `<li class="sniffItem" data-url="${esc(r.url || "")}" data-i="${i}">` +
      `<span class="nm" title="${esc(r.url || "")}">${esc(t)}</span>` +
      `<span class="st active">${(r.count || 0) + " 项"}</span></li>`;
  }).join("");
  ul.querySelectorAll(".sniffItem").forEach((el) => {
    el.addEventListener("click", () => {
      const u = el.getAttribute("data-url");
      if (u) chrome.tabs.create({ url: u });
    });
  });
}

function refreshRecent() {
  chrome.runtime.sendMessage({ type: "rpc", method: "sniffdl.recentSniff", params: [] }, (resp) => {
    const list = (resp && resp.ok && resp.result && resp.result.items) || [];
    renderRecent(list);
  });
}

document.addEventListener("DOMContentLoaded", () => {
  chrome.runtime.sendMessage({ type: "load" }, (resp) => {
    if (resp && resp.ok) loadSettingsIntoForm(resp.data);
  });
  refreshPing();
  refreshTasks();
  refreshRecent();
  setInterval(refreshTasks, 1500);

  const cs = $("clearSniff");
  if (cs) cs.addEventListener("click", () => {
    chrome.runtime.sendMessage({ type: "rpc", method: "sniffdl.clearRecentSniff", params: [] }, () => refreshRecent());
  });

  $("save").addEventListener("click", () => {
    const data = {
      enabled: $("enabled").checked,
      port: parseInt($("port").value, 10) || 6800,
      token: $("token").value.trim(),
      minSizeMB: parseInt($("minSizeMB").value, 10) || 0,
      onlyExtensions: $("onlyExtensions").value.trim(),
    };
    chrome.runtime.sendMessage({ type: "save", data }, (resp) => {
      const m = $("statusMsg");
      m.textContent = (resp && resp.ok) ? "已保存 ✓" : "保存失败";
      m.className = "statusMsg " + ((resp && resp.ok) ? "ok" : "bad");
      refreshPing();
    });
  });

  $("test").addEventListener("click", () => {
    const m = $("statusMsg");
    m.textContent = "测试中…"; m.className = "statusMsg";
    refreshPing();
  });

  // 开关即时保存，所见即所得
  try {
    chrome.storage.local.get({ hoverBtn: true }, (r) => { $("hoverBtn").checked = !(r && r.hoverBtn === false); });
    $("hoverBtn").addEventListener("change", () => { chrome.storage.local.set({ hoverBtn: $("hoverBtn").checked }); });
  } catch (_) {}
  $("enabled").addEventListener("change", () => {
    chrome.runtime.sendMessage({ type: "save", data: { enabled: $("enabled").checked } });
  });

  // 嗅探本页媒体 → 在本页正中央打开嗅探篮（桌面端深度嗅探），不再走老的一次性 DOM 扫
  $("sniff").addEventListener("click", () => {
    const m = $("statusMsg");
    m.textContent = "正在本页打开嗅探篮…"; m.className = "statusMsg";
    chrome.runtime.sendMessage({ type: "sniff" }, (resp) => {
      const ok = resp && resp.ok;
      m.textContent = ok ? "已在本页正中央打开嗅探篮，逐条勾选即可下载 ♪" : ("打开失败：" + ((resp && resp.error) || ""));
      m.className = "statusMsg " + (ok ? "ok" : "bad");
      setTimeout(refreshTasks, 600);
      if (ok) setTimeout(() => { try { window.close(); } catch (e) {} }, 500);  // 关弹窗，让用户看页面里的清单
    });
  });

  // 粘贴链接手动转交
  const doPush = () => {
    const url = ($("pushUrl").value || "").trim();
    const m = $("statusMsg");
    if (!url) { m.textContent = "先粘贴一条链接"; m.className = "statusMsg bad"; return; }
    m.textContent = "转交中…"; m.className = "statusMsg";
    chrome.runtime.sendMessage({ type: "dl", url }, (resp) => {
      if (resp && resp.ok) {
        m.textContent = "已交给看板娘 ♪"; m.className = "statusMsg ok";
        $("pushUrl").value = "";
        setTimeout(refreshTasks, 600);
      } else {
        m.textContent = "转交失败：" + ((resp && resp.error) || ""); m.className = "statusMsg bad";
      }
    });
  };
  $("pushGo").addEventListener("click", doPush);
  $("pushUrl").addEventListener("keydown", (e) => { if (e.key === "Enter") doPush(); });
});
