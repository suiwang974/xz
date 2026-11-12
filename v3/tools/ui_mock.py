# -*- coding: utf-8 -*-
"""生成 SniffDL Web UI（路线B 前端）——大肥鱼单一蓝主题 · 真数据 webview。

产出的 ui_web/index.html 由 ui_web.py（pywebview 普通窗）加载，页面通过
`window.pywebview.api.call(method, params_json)` 桥调用本机 SniffDL JSON-RPC，
把下载/队列/值班室/日志/设置五视图接真数据、每控件真能用；主题恒为大肥鱼蓝
（运行时再由 sniffdl.theme 兜底覆盖，与插件/桌宠三处统一）。

不再是静态视觉稿：假数据全部换成空状态 + 真数据渲染函数。
用法: python tools/ui_mock.py
"""
import base64, json, os, sys
sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---- 大肥鱼头像（裸 base64；运行时也会被 sniffdl.theme 覆盖）----
def feiyu_uri():
    for p in (os.path.join(ROOT, "assets", "feiyu_avatar.b64"),
              os.path.join(ROOT, "assets", "feiyu_avatar.png")):
        if os.path.exists(p):
            if p.endswith(".b64"):
                s = open(p, encoding="utf-8").read().strip()
                return s if s.startswith("data:") else "data:image/png;base64," + s
            return "data:image/png;base64," + base64.b64encode(open(p, "rb").read()).decode()
    return ""

FEIYU = feiyu_uri()

CSS = """
:root{
  --bg:#EEF3FB; --panel:#FFFFFF; --side:#F6F8FC; --ink:#1F2A44; --ink2:#5C6880;
  --ink3:#93A0B8; --line:#D7E0F0; --line2:#EAF0F8; --acc:#3E6FD0; --acc2:#38C6D9;
  --acc-soft:#3E6FD01a; --deep:#16223F; --gold:#C9A24B; --blush:#F3D7D0;
  --ok:#3BA55D; --warn:#D98A29; --err:#D14343;
  --r:12px; --shadow:0 1px 2px rgba(20,30,60,.05),0 6px 20px rgba(20,30,60,.06);
}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{background:var(--bg);color:var(--ink);
  font:14px/1.5 "Segoe UI","Microsoft YaHei UI",system-ui,sans-serif;display:flex;overflow:hidden}
#bg{position:fixed;inset:0;z-index:0;background:center/cover no-repeat;opacity:0;
  pointer-events:none;transition:opacity .35s ease}
.side{width:236px;flex:0 0 236px;background:var(--side);backdrop-filter:blur(7px);
  border-right:1px solid var(--line);position:relative;z-index:1;
  display:flex;flex-direction:column;padding:16px 14px}
.brand{display:flex;align-items:center;gap:9px;padding:4px 6px 16px}
.brand .logo{width:26px;height:26px;border-radius:8px;background:linear-gradient(135deg,var(--acc),var(--acc2));
  display:grid;place-items:center;color:#fff;font-weight:800;font-size:15px}
.brand b{font-size:16px;letter-spacing:.3px}
.me{display:flex;align-items:center;justify-content:center;gap:11px;padding:10px;border-radius:var(--r);
  background:var(--panel);border:1px solid var(--line);box-shadow:var(--shadow);margin-bottom:14px}
.me img{width:46px;height:46px;border-radius:50%;object-fit:cover;object-position:top;box-shadow:0 0 0 2px var(--acc)}
.metext{display:none}
.me .nm{font-weight:700;font-size:14px}
.me .rl{font-size:12px;color:var(--ink3)}
nav{display:flex;flex-direction:column;gap:3px}
.nav{display:flex;align-items:center;gap:11px;padding:9px 12px;border-radius:10px;color:var(--ink2);
  cursor:pointer;font-weight:600;border:none;background:none;text-align:left;width:100%;font:inherit}
.nav svg{width:18px;height:18px;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round;flex:0 0 18px}
.nav:hover{background:#fff;color:var(--ink)}
.nav.on{background:var(--acc-soft);color:var(--acc)}
.side .sp{flex:1}
.ver{font-size:11px;color:var(--ink3);padding:10px 6px 0}
.main{flex:1;display:flex;flex-direction:column;min-width:0;position:relative;z-index:1;background:transparent}
.top{height:60px;flex:0 0 60px;display:flex;align-items:center;gap:12px;padding:0 26px;border-bottom:1px solid var(--line);background:transparent}
.top h1{font-size:19px;margin:0;font-weight:800}
.chip{font-size:12px;color:var(--ink2);background:#fff;border:1px solid var(--line);border-radius:20px;padding:2px 10px}
.top .r{margin-left:auto;display:flex;gap:8px;align-items:center}
.ico{width:34px;height:34px;border-radius:9px;border:1px solid var(--line);background:#fff;display:grid;place-items:center;color:var(--ink2);cursor:pointer}
.ico svg{width:17px;height:17px;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.view{flex:1;overflow:auto;padding:22px 26px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);box-shadow:var(--shadow)}
.hide{display:none!important}
.urlbar{display:flex;gap:10px;align-items:center;padding:12px 14px;margin-bottom:16px}
.urlbar .tag{font-size:12px;color:var(--acc);font-weight:700;padding:0 4px;white-space:nowrap}
.urlbar input{flex:1;border:1px solid var(--line);border-radius:9px;padding:10px 12px;font:inherit;background:#fbfcfd}
.urlbar input:focus{outline:none;border-color:var(--acc);background:#fff}
.seg{display:flex;background:var(--line2);border-radius:9px;padding:3px}
.seg span{padding:6px 12px;border-radius:7px;font-size:13px;color:var(--ink2);cursor:pointer;white-space:nowrap}
.seg span.on{background:#fff;color:var(--ink);box-shadow:var(--shadow);font-weight:600}
.btn{border:none;border-radius:9px;padding:9px 15px;font:inherit;font-weight:700;cursor:pointer;white-space:nowrap}
.btn.pri{background:linear-gradient(135deg,var(--acc),var(--acc2));color:#fff}
.btn.gho{background:#fff;color:var(--ink);border:1px solid var(--line)}
.btn:disabled{opacity:.5;cursor:default}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--ink3);font-weight:600;padding:11px 12px;border-bottom:1px solid var(--line);font-size:12px}
td{padding:10px 12px;border-bottom:1px solid var(--line2);color:var(--ink2)}
td.name{color:var(--ink);font-weight:600}
.pbar{display:flex;align-items:center;gap:8px;white-space:nowrap}
.track{width:88px;height:6px;border-radius:4px;background:var(--line2);overflow:hidden}
.track i{display:block;height:100%;border-radius:4px;background:var(--acc)}
.track.full i{background:var(--ok)}
.pct{font-size:12px;color:var(--ink2);min-width:34px}
.pill{display:inline-flex;align-items:center;gap:5px;font-size:12px;font-weight:700;border-radius:20px;padding:2px 10px;white-space:nowrap}
.pill.ok{color:var(--ok);background:#3BA55D14}.pill.run{color:var(--acc);background:var(--acc-soft)}
.pill.wait{color:var(--ink3);background:#93A0B814}.pill.err{color:var(--err);background:#D1434314}
.empty{padding:56px;text-align:center;color:var(--ink3)}
.empty b{display:block;color:var(--ink2);font-size:15px;margin-bottom:6px}
.actbar{display:flex;gap:10px;align-items:center;padding:12px 16px;margin-top:16px}
.actbar .path{margin-left:auto;font-size:12px;color:var(--ink3);display:flex;gap:8px;align-items:center}
.qhead{display:flex;align-items:center;padding:10px 16px;color:var(--ink3);font-size:12px;font-weight:600;border-bottom:1px solid var(--line)}
.qrow{display:flex;align-items:center;gap:12px;padding:12px 16px;border-bottom:1px solid var(--line2)}
.qrow:hover{background:#fafbfc}
.qrow .nm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--ink);font-weight:600}
.qrow .sz{width:84px;text-align:right;color:var(--ink2);font-size:13px}
.qrow .pg{width:150px}.qrow .st{width:96px;text-align:center}.qrow .sp2{width:120px;text-align:right;color:var(--ink2);font-size:12px}
.qhead .h1{flex:1}.qhead .h2{width:84px;text-align:right}.qhead .h3{width:150px}.qhead .h4{width:96px;text-align:center}.qhead .h5{width:120px;text-align:right}
@keyframes beat{0%,100%{opacity:1}50%{opacity:.55}}.beat{animation:beat 1.4s ease-in-out infinite}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:14px}
.kpi{padding:16px 18px;position:relative;overflow:hidden}
.kpi .lb{font-size:12px;color:var(--ink3)}.kpi .v{font-size:26px;font-weight:800;margin-top:4px;letter-spacing:.3px}
.kpi .v small{font-size:12px;color:var(--ink3);font-weight:600}
.kpi .sub{font-size:12px;color:var(--ink3);margin-top:8px;display:flex;align-items:center;gap:6px}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block}.dot.g{background:var(--ok)}.dot.a{background:var(--acc)}
.kpi .bar{position:absolute;left:0;right:0;bottom:0;height:3px;background:var(--acc)}
.kpi.k3 .bar{background:var(--acc2)}.kpi.k4 .bar{background:var(--ok)}
.grid2{display:grid;grid-template-columns:1.6fr 1fr;gap:14px;margin-bottom:14px}
.panel{padding:16px 18px}.panel h3{margin:0 0 12px;font-size:14px;display:flex;align-items:center;gap:8px}
.panel h3 .more{margin-left:auto;font-size:12px;color:var(--ink3);font-weight:400}
.hm{display:grid;grid-template-rows:repeat(7,12px);grid-auto-flow:column;grid-auto-columns:12px;gap:3px}
.hm i{width:12px;height:12px;border-radius:3px;background:var(--line2)}
.hm i.l1{background:var(--acc-soft)}.hm i.l2{background:var(--acc)66}.hm i.l3{background:var(--acc)aa}.hm i.l4{background:var(--acc)}
.hmlb{display:flex;justify-content:space-between;font-size:11px;color:var(--ink3);margin-top:8px}
.statline{display:flex;align-items:center;justify-content:space-between;padding:8px 0;border-top:1px solid var(--line2);font-size:13px}
.statline:first-of-type{border-top:none}.statline b{font-weight:700}
.mee{display:flex;gap:12px;align-items:flex-start}
.mee img{width:52px;height:52px;border-radius:50%;object-fit:cover;object-position:top;box-shadow:0 0 0 2px var(--acc)}
.bubble{background:var(--acc-soft);border-radius:12px;padding:10px 12px;font-size:13px;color:var(--ink)}
.bubble .who{font-size:11px;color:var(--acc);font-weight:700;margin-bottom:2px}
.log{font:12.5px/1.7 Consolas,monospace;color:#cdd3dc;background:#1c2027;border-radius:var(--r);padding:16px 18px;height:100%;overflow:auto;border:1px solid #262b34;white-space:pre-wrap}
.log .g{color:#7fd1cb}.log .y{color:#e6c07a}.log .d{color:#8a9099}
.setgrid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;max-width:1060px}
.scard{padding:18px;cursor:pointer;transition:box-shadow .15s}
.scard:hover{box-shadow:0 2px 4px rgba(20,30,60,.07),0 10px 28px rgba(20,30,60,.09)}
.scard .sic{width:44px;height:44px;border-radius:12px;display:grid;place-items:center;margin-bottom:14px;background:var(--acc-soft);color:var(--acc)}
.scard .sic svg{width:22px;height:22px;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.scard h4{margin:0 0 4px;font-size:14px}.scard p{margin:0;font-size:12px;color:var(--ink3);line-height:1.5}
.scard .body{display:none;margin-top:14px;border-top:1px solid var(--line2);padding-top:6px}
.scard.open{grid-column:1/-1;cursor:default}.scard.open .body{display:block}
.field{display:flex;align-items:center;justify-content:space-between;padding:9px 0;border-top:1px solid var(--line2)}
.field:first-of-type{border-top:none}.field label{color:var(--ink);font-weight:600}
.field .ctl{display:flex;gap:8px;align-items:center}
.sel,.in{border:1px solid var(--line);border-radius:8px;padding:7px 10px;font:inherit;background:#fbfcfd;color:var(--ink)}
.sw{width:40px;height:23px;border-radius:20px;background:var(--acc);position:relative;cursor:pointer;flex:none}
.sw i{position:absolute;top:3px;right:3px;width:17px;height:17px;border-radius:50%;background:#fff}
.sw.off{background:#cfd4db}.sw.off i{left:3px;right:auto}
.statusbar{flex:0 0 38px;display:flex;align-items:center;gap:10px;padding:0 18px;background:var(--panel);border-top:1px solid var(--line);font-size:12px;color:var(--ink2)}
.statusbar .eng{display:flex;align-items:center;gap:6px;font-weight:700}
.statusbar .sep{width:1px;height:16px;background:var(--line)}
.statusbar .cur{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--ink3)}
.sppill{display:inline-flex;align-items:center;gap:5px;border:1px solid var(--line);border-radius:20px;padding:2px 10px;background:#fbfcfd;font-weight:700;color:var(--ink2);white-space:nowrap}
.sppill b{color:var(--acc);font-weight:800}.sppill.live{border-color:var(--acc);background:var(--acc-soft)}
.toast{position:fixed;left:50%;bottom:52px;transform:translateX(-50%);font-size:12.5px;color:#fff;background:var(--deep);border-radius:20px;padding:7px 16px;z-index:20;opacity:0;transition:opacity .2s;pointer-events:none}
.toast.on{opacity:.95}
/* 剪贴板检测到链接：可操作下载条 */
.clipbar{position:fixed;left:50%;bottom:56px;transform:translateX(-50%) translateY(8px);z-index:30;display:none;
  align-items:center;gap:12px;background:linear-gradient(135deg,var(--acc),var(--acc2));color:#fff;
  padding:11px 16px;border-radius:14px;box-shadow:0 14px 34px rgba(22,34,63,.3);max-width:82%;font-size:13px;
  opacity:0;transition:opacity .25s ease, transform .25s ease}
.clipbar.on{display:flex;opacity:1;transform:translateX(-50%) translateY(0)}
.clipbar .lk{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:360px;opacity:.95;font-weight:600}
.clipbar button{border:none;border-radius:9px;padding:7px 13px;font-weight:700;cursor:pointer;font:inherit;white-space:nowrap}
.clipbar .go{background:#fff;color:var(--acc)}
.clipbar .go:hover{filter:brightness(1.05)}
.clipbar .no{background:rgba(255,255,255,.2);color:#fff}
/* ---------- 背景缩略图选择器（不用下拉，像主题市场那样点缩略图） ---------- */
.field.col{display:flex;flex-direction:column;align-items:flex-start;gap:4px}
.bgpick{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
.bgtile{width:78px;height:56px;padding:0;border-radius:10px;overflow:hidden;cursor:pointer;position:relative;
  border:2px solid var(--line);background:#0e1a30 center/cover no-repeat;transition:transform .12s, box-shadow .12s, border-color .12s}
.bgtile img{width:100%;height:100%;object-fit:cover;display:block}
.bgtile:hover{transform:translateY(-2px);box-shadow:0 6px 16px rgba(20,30,60,.25)}
.bgtile.sel{border-color:var(--acc);box-shadow:0 0 0 3px var(--acc-soft),0 6px 16px rgba(20,30,60,.25)}
.bgtile .bgnone{position:absolute;inset:0;display:grid;place-items:center;color:var(--ink2);font-weight:700;font-size:13px;background:repeating-linear-gradient(45deg,#f2f5fa,#f2f5fa 8px,#e9eef6 8px,#e9eef6 16px)}
.bgtile .tick{position:absolute;right:4px;top:4px;width:16px;height:16px;border-radius:50%;background:var(--acc);color:#fff;font-size:11px;display:none;place-items:center;box-shadow:0 1px 3px rgba(0,0,0,.3)}
.bgtile.sel .tick{display:grid}
/* ---------- 沉浸式背景：整幅插画透出 + 玻璃面板 + 左重右轻暗遮罩保证文字清晰 ---------- */
body.immersive{--bg:#0b1224; --panel:rgba(16,26,50,.74); --side:rgba(9,15,32,.82); --ink:#F1F5FB;
  --ink2:#D4DEF0; --ink3:#A9B8D0; --line:rgba(255,255,255,.18); --line2:rgba(255,255,255,.10);
  --acc-soft:rgba(120,160,230,.24)}
body.immersive #bg{opacity:1}
#bg::after{content:"";position:fixed;inset:0;opacity:0;transition:opacity .35s;
  background:linear-gradient(90deg, rgba(6,10,24,.90) 0%, rgba(6,10,24,.72) 22%, rgba(6,10,24,.50) 55%, rgba(6,10,24,.60) 100%)}
body.immersive #bg::after{opacity:1}
body.immersive .nav{color:var(--ink2)}
body.immersive .nav.on{color:var(--acc)}
body.immersive .side,body.immersive .top,body.immersive .statusbar,body.immersive .card,
body.immersive .me,body.immersive .chip,body.immersive .pill,body.immersive .in,body.immersive .sel,
body.immersive .sppill{backdrop-filter:blur(12px) saturate(1.25);-webkit-backdrop-filter:blur(12px) saturate(1.25)}
body.immersive .card{border-color:rgba(255,255,255,.14)}
body.immersive .me{background:transparent;border-color:transparent;box-shadow:none}
body.immersive .urlbar input,body.immersive .in,body.immersive .sel{background:rgba(255,255,255,.10);color:var(--ink);border-color:rgba(255,255,255,.18)}
body.immersive .urlbar input::placeholder{color:var(--ink3)}
body.immersive .btn.gho{background:rgba(255,255,255,.10);color:var(--ink);border-color:rgba(255,255,255,.20)}
body.immersive .nav:hover,body.immersive .scard:hover{background:rgba(255,255,255,.08)}
body.immersive .seg{background:rgba(255,255,255,.10)}
body.immersive .seg span.on{background:rgba(255,255,255,.9);color:#1F2A44}
body.immersive th,body.immersive td{border-color:rgba(255,255,255,.10)}
body.immersive .empty{color:var(--ink3)}
"""

NAV_ICONS = {
    "dl": '<svg viewBox="0 0 24 24"><path d="M12 3v11m0 0l-4-4m4 4l4-4"/><path d="M4 17v3a1 1 0 001 1h14a1 1 0 001-1v-3"/></svg>',
    "queue": '<svg viewBox="0 0 24 24"><path d="M9 6h11M9 12h11M9 18h11"/><path d="M4 6h.01M4 12h.01M4 18h.01"/></svg>',
    "dash": '<svg viewBox="0 0 24 24"><rect x="4" y="4" width="7" height="7" rx="1.5"/><rect x="13" y="4" width="7" height="7" rx="1.5"/><rect x="4" y="13" width="7" height="7" rx="1.5"/><rect x="13" y="13" width="7" height="7" rx="1.5"/></svg>',
    "log": '<svg viewBox="0 0 24 24"><path d="M5 4h14v16H5z"/><path d="M8 8h8M8 12h8M8 16h5"/></svg>',
    "set": '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3.2"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M19 5l-2 2M7 17l-2 2"/></svg>',
}
ICONS = {
    "store": '<svg viewBox="0 0 24 24"><path d="M3 8l9-5 9 5v11a1 1 0 01-1 1H4a1 1 0 01-1-1z"/><path d="M9 20v-7h6v7"/></svg>',
    "dl": '<svg viewBox="0 0 24 24"><path d="M12 3v11m0 0l-4-4m4 4l4-4"/><path d="M4 17v3a1 1 0 001 1h14a1 1 0 001-1v-3"/></svg>',
    "cookie": '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><circle cx="10" cy="10" r=".9" fill="currentColor" stroke="none"/><circle cx="14.5" cy="13" r=".9" fill="currentColor" stroke="none"/><circle cx="10.5" cy="15.5" r=".9" fill="currentColor" stroke="none"/></svg>',
    "globe": '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.8 2.6 2.8 15.4 0 18M12 3c-2.8 2.6-2.8 15.4 0 18"/></svg>',
    "pet": '<svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="4.2"/><path d="M4.5 20a7.5 7.5 0 0115 0"/></svg>',
    "info": '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8v.1"/></svg>',
}

BODY = """
<div id="bg"></div>
<aside class="side">
  <div class="brand"><div class="logo">S</div><b>SniffDL</b></div>
  <div class="me"><img src="__FEIYU__" id="meimg"><div class="metext"><div class="nm" id="menm">大肥鱼 · DeepSeek娘</div><div class="rl" id="merl">吃白饭的 AI 鲸鱼娘</div></div></div>
  <nav>
    <button class="nav on" data-v="dl">__N_dl__下载</button>
    <button class="nav" data-v="queue">__N_queue__队列</button>
    <button class="nav" data-v="dash">__N_dash__值班室</button>
    <button class="nav" data-v="log">__N_log__日志</button>
    <button class="nav" data-v="set">__N_set__设置</button>
  </nav>
  <div class="sp"></div>
  <div class="ver" id="ver">连接中…</div>
</aside>

<div class="main">
  <section id="v-dl" style="display:flex;flex-direction:column;flex:1;min-height:0">
    <div class="top"><h1>下载</h1><span class="chip" id="dlcount">0 条结果</span>
      <div class="r"><div class="ico" id="refresh" title="刷新"><svg viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-3-6.7M21 4v5h-5"/></svg></div></div>
    </div>
    <div class="view">
      <div class="card urlbar"><span class="tag">网址</span>
        <input id="url" placeholder="粘贴视频页 / 列表页 / 直链，回车即嗅探">
        <div class="seg" id="scope"><span class="on" data-s="page">仅本页</span><span data-s="all">全站列表</span></div>
        <button class="btn pri" id="sniffbtn">✦ 开捞！</button>
        <button class="btn gho" id="magnetbtn">种子 / 磁力</button>
      </div>
      <div class="card">
        <table><thead><tr><th style="width:32px">#</th><th>文件名 / 标题</th><th style="width:56px">格式</th><th style="width:80px">分辨率</th><th style="width:60px">时长</th><th style="width:78px">大小</th><th style="width:132px">进度</th><th style="width:92px">状态</th><th style="width:88px">来源</th></tr></thead>
        <tbody id="dlrows"></tbody></table>
        <div class="empty" id="dlempty"><b id="dlemptyb">这片…还空着</b><span id="dlemptys">贴个网址给本鱼，我这就去翻个底朝天。</span></div>
      </div>
      <div class="card actbar">
        <button class="btn gho" id="dlsel">♡ 下载选中</button>
        <button class="btn pri" id="dlall">✦ 全部下载</button>
        <button class="btn gho" id="dlclear">清空列表</button>
        <span class="path">保存到 <b id="outpath" style="color:var(--ink2)">…</b> · <a href="#" id="openout" style="color:var(--acc);text-decoration:none">打开文件夹</a></span>
      </div>
    </div>
  </section>

  <section id="v-queue" class="hide" style="display:flex;flex-direction:column;flex:1;min-height:0">
    <div class="top"><h1>队列</h1><span class="chip" id="qcount">0 进行中 · 0 排队 · 0 完成</span></div>
    <div class="view">
      <div class="card" style="padding:14px 16px;margin-bottom:14px">
        <div style="display:flex;align-items:center;gap:12px;margin-bottom:10px"><b>总进度</b><span class="chip" id="qtot">–</span></div>
        <div class="track" style="width:100%;height:8px"><i id="qbar" style="width:0%"></i></div>
      </div>
      <div class="card"><div class="qhead"><span class="h1">名称</span><span class="h2">大小</span><span class="h3">进度</span><span class="h4">状态</span><span class="h5">速度 · 剩余</span></div>
        <div id="qrows"></div>
        <div class="empty" id="qempty"><b>暂时没有任务</b><span>去下载视图贴个网址，嗅到宝贝就能开工啦。</span></div>
      </div>
    </div>
  </section>

  <section id="v-dash" class="hide" style="display:flex;flex-direction:column;flex:1;min-height:0">
    <div class="top"><h1>值班室</h1><span class="chip" id="dashchip">今日 0 件</span>
      <div class="r"><button class="btn gho" id="resetstats">重置统计</button></div></div>
    <div class="view">
      <div class="kpis">
        <div class="card kpi k1"><div class="lb">引擎</div><div class="v" id="kEng">–</div><div class="sub" id="kEngSub"><span class="dot g"></span>连接中…</div><div class="bar"></div></div>
        <div class="card kpi k2"><div class="lb">今日战绩</div><div class="v" id="kToday">–</div><div class="sub" id="kTodaySub">今天还没有收获</div><div class="bar"></div></div>
        <div class="card kpi k3"><div class="lb">当前速度</div><div class="v" id="kSpeed">–</div><div class="sub" id="kSpeedSub"><span class="dot a"></span>空闲</div><div class="bar"></div></div>
        <div class="card kpi k4"><div class="lb">累计传输</div><div class="v" id="kTotal">–</div><div class="sub" id="kTotalSub">–</div><div class="bar"></div></div>
      </div>
      <div class="grid2">
        <div class="card panel"><h3>下载活动<span class="more">近 16 周 · 按天字节量</span></h3><div class="hm" id="heat"></div><div class="hmlb"><span>少</span><span>多</span></div></div>
        <div class="card panel"><h3>任务概况</h3>
          <div class="statline"><span>下载中</span><b id="sAct" style="color:var(--acc)">0</b></div>
          <div class="statline"><span>排队</span><b id="sWait">0</b></div>
          <div class="statline"><span>今日完成</span><b id="sToday" style="color:var(--ok)">0</b></div>
          <div class="statline"><span>失败 / 冷却</span><b id="sErr" style="color:var(--err)">0</b></div>
          <div class="statline"><span>保存到</span><b id="sOut" style="font-weight:600;font-size:12px;color:var(--ink2)">…</b></div>
          <div class="statline"><span>磁盘剩余</span><b id="sDisk">–</b></div>
        </div>
      </div>
      <div class="card panel" style="max-width:640px"><div class="mee">
        <img id="dashav" src="__FEIYU__">
        <div class="bubble"><div class="who" id="dashwho">大肥鱼 · DeepSeek娘</div><span id="dashsay">今天想捞点什么？</span></div>
      </div></div>
    </div>
  </section>

  <section id="v-log" class="hide" style="display:flex;flex-direction:column;flex:1;min-height:0">
    <div class="top"><h1>日志</h1><span class="chip">运行状态摘要</span>
      <div class="r"><button class="btn gho" id="logrefresh">刷新</button></div></div>
    <div class="view" style="padding:16px 26px"><div class="log" id="log"></div></div>
  </section>

  <section id="v-set" class="hide" style="display:flex;flex-direction:column;flex:1;min-height:0">
    <div class="top"><h1>设置</h1><span class="chip">改动即时保存</span></div>
    <div class="view">
      <div class="setgrid">
        <div class="card scard"><div class="sic">__I_store__</div><h4>存储</h4><p>下载保存目录与快捷打开</p>
          <div class="body"><div class="field"><label>保存到</label><div class="ctl"><input class="in" id="s_outdir" style="width:320px"><button class="btn gho" id="s_pick">浏览</button><button class="btn gho" id="s_open">打开文件夹</button></div></div>
          <div class="field col"><label>界面背景</label>
            <div class="bgpick" id="bgpick">
              <button class="bgtile" data-bg="none" title="无（纯净）"><span class="bgnone">无</span><span class="tick">✓</span></button>
              <button class="bgtile" data-bg="bg1" title="大肥鱼 · 立绘"><img src="bg/bg1.jpg"><span class="tick">✓</span></button>
              <button class="bgtile" data-bg="bg2" title="大肥鱼 · 抬手"><img src="bg/bg2.jpg"><span class="tick">✓</span></button>
              <button class="bgtile" data-bg="bg3" title="大肥鱼 · 伸手"><img src="bg/bg3.jpg"><span class="tick">✓</span></button>
              <button class="bgtile" data-bg="bg4" title="蓝发女仆"><img src="bg/bg4.webp"><span class="tick">✓</span></button>
            </div></div></div></div>
        <div class="card scard"><div class="sic">__I_dl__</div><h4>下载</h4><p>画质 · 并发 · 网络 · 保险丝</p>
          <div class="body">
            <div class="field"><label>画质上限</label><select class="sel" id="s_quality"><option>最佳</option><option>2160</option><option>1080</option><option>720</option><option>480</option></select></div>
            <div class="field"><label>加速档位</label><select class="sel" id="s_threads"><option>自动</option><option>16</option><option>32</option><option>64</option><option>128</option><option>256</option></select></div>
            <div class="field"><label>大站单文件也多线程</label><div class="sw off" id="s_dash"><i></i></div></div>
            <div class="field"><label>代理</label><select class="sel" id="s_proxy"><option>跟随系统</option><option>不使用</option><option>手动</option></select></div>
            <div class="field"><label>流量保险丝</label><div class="ctl"><input class="in" id="s_rxcap" style="width:70px"><span style="color:var(--ink3);font-size:12px">GB（0=关闭）</span></div></div>
            <div class="field"><label>完成后</label><select class="sel" id="s_after"><option>无</option><option>关机</option><option>退出程序</option></select></div>
          </div></div>
        <div class="card scard"><div class="sic">__I_cookie__</div><h4>登录与 Cookie</h4><p>需登录站点的高画质：读本机浏览器 Cookie，或开登录窗抓取记住</p>
          <div class="body">
            <div class="field"><label>Cookie 来源</label><select class="sel" id="s_cookie"><option>自动</option><option>浏览器</option><option>Chrome</option><option>Edge</option><option>Firefox</option><option>Brave</option><option>已登录Cookie</option><option>不使用</option></select></div>
            <div class="field"><label>本机浏览器</label><span id="ck_browsers" style="color:var(--ink2)">检测中…</span></div>
            <div class="field"><label>已存 Cookie</label><span id="ck_stored" style="color:var(--ink2)">–</span></div>
            <div class="field"><label>登录取 Cookie</label><div class="ctl"><input class="in" id="ck_url" placeholder="站点网址，如 instagram.com" style="width:230px"><button class="btn pri" id="ck_login">打开登录窗</button></div></div>
            <div class="field"><label></label><span id="ck_msg" style="color:var(--ink3);font-size:12px">点「打开登录窗」→ 在弹出浏览器里登录该站 → 自动记住 Cookie（下次下载直接带上）。</span></div>
            <div class="field"><label>导入 cookies.txt</label><div class="ctl"><textarea class="in" id="ck_paste" rows="2" style="width:300px;font-size:11px" placeholder="粘贴 Netscape 格式 cookies.txt 内容"></textarea><button class="btn gho" id="ck_import">导入</button></div></div>
          </div></div>
        <div class="card scard"><div class="sic">__I_globe__</div><h4>本地下载服务 (RPC)</h4><p>浏览器插件 / 油猴把 SniffDL 当 aria2 用</p>
          <div class="body">
            <div class="field"><label>启用</label><div class="sw off" id="s_rpcen"><i></i></div></div>
            <div class="field"><label>端口</label><input class="in" id="s_port" style="width:90px"></div>
            <div class="field"><label>token</label><input class="in" id="s_token" style="width:150px"></div>
            <div class="field"><label>开机自启</label><div class="sw off" id="s_autostart"><i></i></div></div>
          </div></div>
        <div class="card scard"><div class="sic">__I_pet__</div><h4>助手与桌宠</h4><p>实时桌宠「大肥鱼」显示与快捷键</p>
          <div class="body">
            <div class="field"><label>显示桌宠</label><div class="sw off" id="s_pet"><i></i></div></div>
            <div class="field"><label>当前看板娘</label><span style="color:var(--ink2)">大肥鱼 · DeepSeek娘（Live2D）</span></div>
            <div class="field"><label>唤醒 / 收起</label><span style="color:var(--ink2)"><b>Ctrl + Alt + H</b>（全局热键）</span></div>
            <div class="field"><label>互动</label><span style="color:var(--ink3);font-size:12px">单击戳她 · 拖拽移动 · 右键圆盘菜单（换表情/大小/穿透/隐藏）· 双击逗她</span></div>
          </div></div>
        <div class="card scard"><div class="sic">__I_globe__</div><h4>实时对话 · 大脑网关</h4><p>大肥鱼实时对话接的 OpenAI 兼容大脑（本地网关 / OpenRouter 等）</p>
          <div class="body">
            <div class="field"><label>启用实时大脑</label><div class="sw off" id="s_rten"><i></i></div></div>
            <div class="field"><label>接口地址</label><input class="in" id="s_rtbase" style="width:320px" placeholder="http://127.0.0.1:3065/v1"></div>
            <div class="field"><label>API Key</label><input class="in" id="s_rtkey" type="password" style="width:320px" placeholder="sk-..."></div>
            <div class="field"><label>模型</label><div class="ctl"><select class="sel" id="s_rtmodel" style="width:230px"></select><button class="btn gho" id="rt_refresh">刷新模型</button></div></div>
            <div class="field col"><label>人设（留空用内置默认）</label><textarea class="in" id="s_rtins" rows="4" style="width:100%;font-size:12px" placeholder="把大肥鱼的人设贴这里，实时对话会带上"></textarea></div>
            <div class="field"><label>连通测试</label><div class="ctl"><button class="btn pri" id="rt_test">测试连接</button><span id="rt_msg" style="color:var(--ink3);font-size:12px">验证网关可达 + /responses 能否用</span></div></div>
          </div></div>
        <div class="card scard"><div class="sic">__I_info__</div><h4>关于与自检</h4><p>版本 · 组件自动更新 · 运行状态</p>
          <div class="body">
            <div class="field"><label>版本</label><span id="s_ver" style="color:var(--ink2)">–</span></div>
            <div class="field"><label>连接</label><span id="s_conn" style="color:var(--ink2)">–</span></div>
            <div class="field"><label>yt-dlp</label><span id="cp_ytdlp" style="color:var(--ink2)">–</span></div>
            <div class="field"><label>ffmpeg</label><span id="cp_ffmpeg" style="color:var(--ink2)">–</span></div>
            <div class="field"><label>组件更新</label><div class="ctl"><span id="cp_msg" style="color:var(--ink3);font-size:12px">自动检查并升级依赖组件</span><button class="btn gho" id="cp_check">检查更新</button><button class="btn pri" id="cp_upd" style="display:none">立即升级</button></div></div>
          </div></div>
      </div>
    </div>
  </section>

  <div class="statusbar">
    <span class="eng"><span class="dot g" id="sbDot"></span><span id="sbEng">连接中…</span></span>
    <span class="sep"></span><span id="sbRpc">RPC –</span>
    <span class="sep"></span>
    <span class="cur" id="sbCur">大肥鱼待命中～</span>
    <span class="sppill" id="sbDown">↓ <b>0 B/s</b></span>
    <span class="sppill">任务 <b id="sbTask">0</b></span>
  </div>
</div>
<div class="toast" id="toast"></div>
<div class="clipbar" id="clipbar"><span>🔗 剪贴板检测到：</span><span class="lk" id="clklk"></span><button class="go" id="clkgo">✦ 下载这条</button><button class="no" id="clkno">忽略</button></div>
"""

JS = r"""
(function(){
  'use strict';
  var $=function(id){return document.getElementById(id);};
  var scope='page', sniffTask=null, sniffTimer=null, lastItems=[];
  var esc=function(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});};
  function toast(t){var el=$('toast');el.textContent=t;el.classList.add('on');clearTimeout(el._t);el._t=setTimeout(function(){el.classList.remove('on');},2600);}

  // ---- RPC 桥（pywebview: window.pywebview.api.call(method, params_json) 返回 JSON 字符串）----
  function rpc(method){
    var params=Array.prototype.slice.call(arguments,1);
    var api=(window.pywebview&&window.pywebview.api&&window.pywebview.api.call)?window.pywebview.api:null;
    if(!api){return Promise.resolve({ok:false,error:'no-bridge'});}
    return Promise.resolve(api.call(method, JSON.stringify(params))).then(function(raw){
      try{ return JSON.parse(raw); }catch(e){ return {ok:false,error:String(e)}; }
    },function(e){ return {ok:false,error:String(e)}; });
  }
  function fmtBytes(n){n=+n||0;if(n<=0)return '0 B';var u=['B','KB','MB','GB','TB'],i=0;while(n>=1024&&i<u.length-1){n/=1024;i++;}return (i?n.toFixed(1):Math.round(n))+' '+u[i];}
  function fmtSpeed(n){n=+n||0;return n<=0?'0 B/s':fmtBytes(n)+'/s';}

  // ---- 主题 ----
  function applyTheme(t){
    var p=(t&&t.palette)||{};
    var rs=document.documentElement.style;
    var map={'--acc':t&&t.accent,'--acc2':p.pink2,'--bg':p.bg,'--panel':p.card,'--ink':p.ink,'--ink2':p.ink2,'--line':p.line};
    for(var k in map){ if(map[k]) rs.setProperty(k,map[k]); }
    if(p['--deep']) rs.setProperty('--deep',p['--deep']);
    var av=t&&t.avatar?('data:image/png;base64,'+t.avatar):null;
    if(av){$('meimg').src=av;$('dashav').src=av;}
    if(t.char||t.label){$('menm').textContent=(t.label||t.char)+'';$('dashwho').textContent=(t.label||t.char)+'';}
    if(t.role)$('merl').textContent=t.role;
    var L=(t&&t.lines)||{};
    if(L.idle)$('dlemptys').textContent=L.idle;
    if(L.greet){$('dashsay').textContent=L.greet;$('sbCur').textContent=L.greet;}
    if(t.cta&&t.cta.sniff)$('sniffbtn').textContent='✦ '+t.cta.sniff.replace(/^[^ ]+ /,'');
  }

  // ---- 下载视图 ----
  function renderDl(items){
    lastItems=items||[];var tb=$('dlrows');
    if(!items||!items.length){tb.innerHTML='';$('dlempty').style.display='';$('dlcount').textContent='0 条结果';return;}
    $('dlempty').style.display='none';$('dlcount').textContent=items.length+' 条结果';
    tb.innerHTML=items.map(function(it,i){
      var name=it.name||it.title||it.url||('条目 '+(i+1));
      var size=it.size?fmtBytes(it.size):(it.sizeText||'–');
      var pct=it.progress!=null?Math.round(it.progress):null;
      var st=it.status||'待下载';
      return '<tr><td>'+(i+1)+'</td><td class="name" title="'+esc(it.url||'')+'"><input type="checkbox" data-i="'+i+'" '+(it.checked?'checked':'')+'> '+esc(name)+'</td>'+
        '<td>'+esc(it.ext||it.format||'–')+'</td><td>'+esc(it.resolution||'–')+'</td><td>'+esc(it.duration||'–')+'</td><td>'+esc(size)+'</td>'+
        '<td><div class="pbar"><div class="track'+(pct>=100?' full':'')+'"><i style="width:'+(pct||0)+'%"></i></div><span class="pct">'+(pct!=null?pct+'%':'–')+'</span></div></td>'+
        '<td><span class="pill '+(it.done?'ok':'wait')+'">'+esc(st)+'</span></td><td>'+esc(it.source||it.via||'–')+'</td></tr>';
    }).join('');
  }
  function startSniff(){
    var url=($('url').value||'').trim();
    if(!url){toast('先贴一条网址嘛～');return;}
    $('sniffbtn').disabled=true;$('sniffbtn').textContent='推理中…';
    rpc('sniffdl.sniff',url,scope).then(function(r){
      var tid=(r&&r.result)||r;
      if(typeof tid!=='string'||!tid){$('sniffbtn').disabled=false;$('sniffbtn').textContent='✦ 开捞！';toast('嗅探启动失败');return;}
      sniffTask=tid;var tries=0;
      clearInterval(sniffTimer);
      sniffTimer=setInterval(function(){
        tries++;
        rpc('sniffdl.sniffResult',sniffTask).then(function(rr){
          var res=(rr&&rr.result)||{};
          if(res.items&&res.items.length){renderDl(res.items);}
          if(res.status==='done'||res.status==='error'||tries>120){
            clearInterval(sniffTimer);$('sniffbtn').disabled=false;$('sniffbtn').textContent='✦ 开捞！';
            if(res.status==='error')toast('这一片没捞到，或出错了');
            else if(res.items&&res.items.length)toast('捞上来啦！'+res.items.length+' 条');
          }
        });
      },1200);
    });
  }
  function addSelected(all){
    var idxs=[];
    if(all){for(var i=0;i<lastItems.length;i++)idxs.push(i);}
    else{document.querySelectorAll('#dlrows input[type=checkbox]:checked').forEach(function(c){idxs.push(+c.getAttribute('data-i'));});}
    if(!idxs.length){toast('先勾选要搬的条目～');return;}
    var done=0;
    idxs.forEach(function(i){
      var it=lastItems[i];if(!it)return;
      rpc('sniffdl.add',it).then(function(){done++;if(done===idxs.length)toast('交给大肥鱼啦，'+done+' 条开始搬 ♪');});
    });
    setTimeout(refreshQueue,800);
  }

  // ---- 队列 ----
  function renderQueue(act,wait,stop){
    var rows=[];
    function pctOf(t){var tot=+t.totalLength||0,done=+t.completedLength||+t.downloadedLength||0;return tot?Math.floor(done/tot*100):0;}
    function pill(st){if(st==='active')return '<span class="pill run">下载中</span>';if(st==='complete')return '<span class="pill ok">✓ 完成</span>';if(st==='error')return '<span class="pill err">✗ '+(t_err||'')+'</span>';return '<span class="pill wait">排队</span>';}
    (act||[]).forEach(function(t){var p=pctOf(t);rows.push(qrow(t,'active',p,fmtSpeed(t.downloadSpeed)));});
    (wait||[]).forEach(function(t){rows.push(qrow(t,'waiting',pctOf(t),'–'));});
    (stop||[]).slice(0,20).forEach(function(t){rows.push(qrow(t,t.status,pctOf(t),t.status==='complete'?'✓':'✗'));});
    var host=$('qrows');
    $('qcount').textContent=(act||[]).length+' 进行中 · '+(wait||[]).length+' 排队 · '+(stop||[]).length+' 完成';
    if(!rows.length){host.innerHTML='';$('qempty').style.display='';$('qtot').textContent='–';$('qbar').style.width='0%';return;}
    $('qempty').style.display='none';host.innerHTML=rows.join('');
    var tot=0,done=0;(act||[]).concat(wait||[]).forEach(function(t){tot+=+t.totalLength||0;done+=+t.completedLength||+t.downloadedLength||0;});
    var pp=tot?Math.floor(done/tot*100):0;$('qtot').textContent=pp+'% 已完成';$('qbar').style.width=pp+'%';
  }
  var t_err='';
  function qrow(t,st,p,sp){
    t_err=(t.errorCode&&t.errorCode!=='0')?t.errorCode:'';
    var name=t.name||(t.files&&t.files[0]&&t.files[0].path)||t.gid||'任务';
    name=String(name).split(/[\\/]/).pop();
    var pillHtml=st==='active'?'<span class="pill run">下载中</span>':st==='complete'?'<span class="pill ok">✓ 完成</span>':st==='error'?'<span class="pill err">✗ '+esc(t_err)+'</span>':'<span class="pill wait">排队</span>';
    return '<div class="qrow"><span class="nm" title="'+esc(name)+'">'+esc(name)+'</span>'+
      '<span class="sz">'+fmtBytes(t.totalLength)+'</span>'+
      '<span class="pg"><div class="pbar"><div class="track'+(p>=100?' full':'')+'"><i'+(st==='active'?' class="beat"':'')+' style="width:'+p+'%'+(st==='error'?';background:var(--err)':'')+'"></i></div></div></span>'+
      '<span class="st">'+pillHtml+'</span><span class="sp2">'+esc(sp)+'</span></div>';
  }
  function refreshQueue(){
    Promise.all([rpc('aria2.tellActive'),rpc('aria2.tellWaiting',0,64),rpc('aria2.tellStopped',0,64)]).then(function(a){
      renderQueue((a[0]&&a[0].result)||[],(a[1]&&a[1].result)||[],(a[2]&&a[2].result)||[]);
    });
  }

  // ---- 值班室 ----
  function renderDash(st,gs){
    var s=(st&&st.stats)||{};
    var na=+((gs&&gs.numActive)||0),nw=+((gs&&gs.numWaiting)||0),ns=+((gs&&gs.numStopped)||0);
    $('kToday').innerHTML=(s.today_count||0)+' <small>件</small> · '+fmtBytes(s.today_bytes);
    if((s.today_count||0)>0){$('kTodaySub').textContent='今天已捞 '+(s.today_count)+' 件 · '+fmtBytes(s.today_bytes);}
    else{$('kTodaySub').textContent='今天还没有收获，去嗅一条试试～';}
    $('kTotal').innerHTML=(s.total_bytes? (s.total_bytes/1073741824).toFixed(1):'0')+' <small>GB</small>';
    $('kTotalSub').textContent=(s.total_count||0)+' 件 · 磁盘剩余 '+(s.disk_free_gb||0)+' GB';
    $('sToday').textContent=s.today_count||0;$('sDisk').textContent=(s.disk_free_gb||0)+' GB';
    var sp=(gs&&gs.downloadSpeed)||0;$('kSpeed').innerHTML=(sp/1048576).toFixed(1)+' <small>MB/s</small>';
    $('kSpeedSub').innerHTML='<span class="dot a'+(na>0?' beat':'')+'"></span>'+(na>0?(na+' 个任务搬运中'):'空闲');
    $('sAct').textContent=na;$('sWait').textContent=nw;$('sErr').textContent=Math.max(0,ns-((s.days&&Object.keys(s.days).length)?0:0));
    $('dashchip').textContent='今日 '+(s.today_count||0)+' 件 · '+fmtBytes(s.today_bytes);
    // 热力图（近 112 天）
    var days=s.days||{},cells='',now=new Date();
    var vals=[];for(var i=111;i>=0;i--){var d=new Date(now-i*864e5);var v=(days[d.toISOString().slice(0,10)]||{}).bytes||0;vals.push(v);}
    var mx=Math.max.apply(null,vals)||1;
    vals.forEach(function(v){var lv=v<=0?0:(v/mx<.25?1:v/mx<.5?2:v/mx<.8?3:4);cells+='<i'+(lv?' class="l'+lv+'"':'')+'></i>';});
    $('heat').innerHTML=cells;
  }

  // ---- 状态条 / 概况 ----
  function renderStatus(info,gs){
    var ok=info&&info.ok;
    $('sbEng').textContent=ok?'aria2 就绪':'未连接';
    $('sbDot').className='dot '+(ok?'g':'');
    $('sbRpc').textContent='RPC '+(info&&info.rpc_port?info.rpc_port:'–');
    var na=+((gs&&gs.numActive)||0);$('sbTask').textContent=na;
    $('sbDown').innerHTML='↓ <b>'+fmtSpeed((gs&&gs.downloadSpeed)||0)+'</b>';
    $('sbDown').className='sppill'+(na>0?' live':'');
    $('ver').textContent=(info&&info.version?('v'+String(info.version).replace(/^v/,'')):'SniffDL')+' · 大肥鱼';
    $('s_ver').textContent=(info&&info.version)||'–';
    $('s_conn').textContent=ok?('已连接 :'+info.rpc_port):'未连接';
    var ke=$('kEng'),kes=$('kEngSub');
    if(ke){ke.textContent=ok?'就绪':'离线';}
    if(kes){kes.innerHTML='<span class="dot '+(ok?'g':'')+'"></span>aria2 · RPC :'+((info&&info.rpc_port)||'–')+(ok?' 运行中':' 未连接');}
  }

  // ---- 设置 ----
  function applyBg(v){
    var el=$('bg'); if(!el) return;
    if(!v||v==='none'){ el.style.opacity='0'; el.style.backgroundImage=''; document.body.classList.remove('immersive'); }
    else { var ext=(v==='bg4')?'.webp':'.jpg'; el.style.backgroundImage='url("bg/'+v+ext+'")'; document.body.classList.add('immersive'); }
    document.querySelectorAll('#bgpick .bgtile').forEach(function(t){ t.classList.toggle('sel', t.getAttribute('data-bg')===(v||'none')); });
  }
  function rtRefreshModels(cur){
    rpc('sniffdl.listModels').then(function(r){
      var d=(r&&r.result)||r||{}; var sel=$('s_rtmodel'); if(!sel) return;
      var models=d.models||[]; sel.innerHTML='';
      if(!models.length && cur){ var o0=document.createElement('option'); o0.value=cur; o0.textContent=cur; sel.appendChild(o0); }
      models.forEach(function(m){ var o=document.createElement('option'); o.value=m; o.textContent=m; sel.appendChild(o); });
      if(cur){ for(var i=0;i<sel.options.length;i++){ if(sel.options[i].value===cur){ sel.selectedIndex=i; break; } } }
    });
  }
  function loadSettings(){
    rpc('sniffdl.getSettings').then(function(r){
      var s=(r&&r.settings)||(r&&r.result&&r.result.settings)||{};
      if(r&&r.result&&r.result.settings)s=r.result.settings;
      $('s_outdir').value=s.outdir||'';$('outpath').textContent=s.outdir||'…';$('sOut').textContent=s.outdir||'…';
      setSel('s_quality',s.quality);setSel('s_threads',s.dl_threads);setSel('s_proxy',s.proxy_mode);setSel('s_after',s.after);setSel('s_cookie',s.cookie_browser);
      $('s_rxcap').value=(s.rx_cap_gb!=null?s.rx_cap_gb:0);
      sw('s_dash',s.dash_aria2);sw('s_rpcen',s.rpc_enable);sw('s_autostart',s.autostart);sw('s_pet',s.pet_live);
      $('s_port').value=s.rpc_port||6800;$('s_token').value=s.rpc_token||'';
      sw('s_rten',s.rt_enable);$('s_rtbase').value=s.rt_base_url||'';$('s_rtkey').value=s.rt_api_key||'';$('s_rtins').value=s.rt_instructions||'';rtRefreshModels(s.rt_model||'');
      applyBg(s.ui_bg||'none');
    });
  }
  // ---- Cookie 登录/抓取/导入 ----
  function loadCookieInfo(){
    rpc('sniffdl.cookieInfo').then(function(r){
      var c=(r&&r.result)||r||{};
      var bs=c.browsers||[];
      $('ck_browsers').textContent=bs.length?bs.join('、'):'未检测到（Chrome/Edge/Firefox…）';
      $('ck_stored').textContent=(c.stored||0)+' 条'+(c.has_cookie_file?'（已登录Cookie 可用）':'');
    });
  }
  var loginTimer=null;
  function startLoginCookie(){
    var u=($('ck_url').value||'').trim()||($('url').value||'').trim();
    $('ck_login').disabled=true;$('ck_msg').textContent='正在打开登录窗…（在弹出的浏览器里登录该站点）';
    rpc('sniffdl.loginCookie',u).then(function(r){
      var res=(r&&r.result)||r||{};
      if(!res.ok&&!res.started){$('ck_login').disabled=false;$('ck_msg').textContent='打开失败：'+(res.error||'');return;}
      clearInterval(loginTimer);
      loginTimer=setInterval(function(){
        rpc('sniffdl.loginStatus').then(function(rr){
          var st=(rr&&rr.result)||rr||{};
          $('ck_msg').textContent=st.msg||'登录窗进行中…';
          if(st.done){clearInterval(loginTimer);$('ck_login').disabled=false;loadCookieInfo();loadSettings();}
        });
      },2000);
    });
  }
  function importCookieText(){
    var t=($('ck_paste').value||'').trim();
    if(!t){$('ck_msg').textContent='先粘贴 cookies.txt 内容';return;}
    rpc('sniffdl.importCookie',t).then(function(r){var res=(r&&r.result)||r||{};
      $('ck_msg').textContent=res.ok?(res.msg||'导入成功'):('导入失败：'+(res.error||''));
      if(res.ok){loadCookieInfo();loadSettings();}});
  }
  // ---- 组件自动更新 ----
  var compTimer=null;
  function loadComponents(){
    rpc('sniffdl.components').then(function(r){
      var c=(r&&r.result)||r||{};var cur=c.cur||{};var info=c.info||{};
      $('cp_ytdlp').textContent=(cur['yt-dlp']||'?')+((info['yt-dlp']&&info['yt-dlp'].latest)?('  → 最新 '+info['yt-dlp'].latest):'');
      $('cp_ffmpeg').textContent=cur['ffmpeg']||'?';
      var need=info['yt-dlp']&&info['yt-dlp'].need;
      $('cp_upd').style.display=need?'':'none';
      if(need)$('cp_msg').textContent='发现新版本，可一键升级';
      else if(info['yt-dlp'])$('cp_msg').textContent='组件均为最新';
    });
  }
  function checkUpdates(){
    $('cp_check').disabled=true;$('cp_msg').textContent='检查中…';
    rpc('sniffdl.checkUpdates').then(function(){pollComponents(15);});
  }
  function pollComponents(n){
    clearInterval(compTimer);var left=n;
    compTimer=setInterval(function(){
      rpc('sniffdl.components').then(function(r){
        var c=(r&&r.result)||r||{};var info=c.info||{};var cur=c.cur||{};
        $('cp_ytdlp').textContent=(cur['yt-dlp']||'?')+((info['yt-dlp']&&info['yt-dlp'].latest)?('  → 最新 '+info['yt-dlp'].latest):'');
        var need=info['yt-dlp']&&info['yt-dlp'].need;
        $('cp_upd').style.display=need?'':'none';
        if(!c.checking){$('cp_check').disabled=false;$('cp_msg').textContent=need?'发现新版本，可一键升级':'已是最新';}
      });
      if(--left<=0)clearInterval(compTimer);
    },1500);
  }
  // ---- 剪贴板接活：检测到链接弹可操作下载条 ----
  var lastClip='';
  function pollClip(){
    rpc('sniffdl.clipHint').then(function(r){
      var u=(r&&r.result&&r.result.url)||'';
      if(u && u!==lastClip){ lastClip=u; $('clklk').textContent=u; $('clklk').title=u; $('clipbar').classList.add('on'); }
      else if(!u && lastClip){ $('clipbar').classList.remove('on'); lastClip=''; }
    });
  }
  function setSel(id,v){if(v==null)return;var el=$(id);for(var i=0;i<el.options.length;i++){if(el.options[i].value===String(v)||el.options[i].text===String(v)){el.selectedIndex=i;break;}}}
  function sw(id,on){var el=$(id);if(!el)return;el.classList.toggle('off',!on);}
  function bindSet(id,key,kind){
    var el=$(id);if(!el)return;
    var push=function(v){rpc('sniffdl.setSetting',key,v).then(function(r){if(r&&r.ok)toast('已保存：'+key);else toast('保存失败');});};
    if(kind==='sel')el.addEventListener('change',function(){push(el.value);});
    else if(kind==='text')el.addEventListener('change',function(){push(el.value);});
    else if(kind==='sw')el.addEventListener('click',function(){var on=el.classList.contains('off');el.classList.toggle('off',!on);push(on);});
  }

  // ---- 日志（运行摘要）----
  function renderLog(info,gs,s){
    var L=[];
    L.push('<span class="d">[大肥鱼]</span> 本鱼在线，随时为你开捞！');
    L.push('<span class="g">[引擎]</span> '+(info&&info.ok?('aria2 就绪 · RPC :'+info.rpc_port):'未连接本机服务'));
    L.push('<span class="g">[并发]</span> 加速档位='+(s.dl_threads||'自动')+' · 大站多线程='+(s.dash_aria2?'开':'关'));
    L.push('<span class="g">[代理]</span> '+(s.proxy_mode||'跟随系统'));
    L.push('<span class="g">[Cookie]</span> 来源='+(s.cookie_browser||'自动'));
    L.push('<span class="y">[流量]</span> 保险丝='+(s.rx_cap_gb||0)+' GB（0=关闭）');
    L.push('<span class="g">[队列]</span> 下载中 '+(gs.numActive||0)+' · 排队 '+(gs.numWaiting||0)+' · 已停 '+(gs.numStopped||0)+' · 速度 '+fmtSpeed(gs.downloadSpeed||0));
    $('log').innerHTML=L.join('<br>');
  }

  // ---- 轮询 ----
  function tick(){
    Promise.all([rpc('sniffdl.status'),rpc('aria2.getGlobalStat'),rpc('sniffdl.stats')]).then(function(a){
      var info=(a[0]&&a[0].result)||a[0]||{};var gs=(a[1]&&a[1].result)||{};var st=(a[2]&&a[2].result)||{};
      renderStatus(info,gs);renderDash(st,gs);
    });
    refreshQueue();
  }

  // ---- 初始化 ----
  function init(){
    // 导航
    document.querySelectorAll('.nav').forEach(function(b){b.onclick=function(){
      document.querySelectorAll('.nav').forEach(function(x){x.classList.remove('on');});b.classList.add('on');
      ['dl','queue','dash','log','set'].forEach(function(v){var s=$('v-'+v);if(!s)return;s.classList.toggle('hide',v!==b.dataset.v);s.style.display=(v===b.dataset.v)?'flex':'none';});
    };});
    // 设置卡展开
    document.querySelectorAll('.scard').forEach(function(c){c.onclick=function(e){
      if(e.target.closest('.body'))return;var on=c.classList.contains('open');
      document.querySelectorAll('.scard').forEach(function(x){x.classList.remove('open');});if(!on)c.classList.add('open');
    };});
    // 作用域
    document.querySelectorAll('#scope span').forEach(function(sp){sp.onclick=function(){
      document.querySelectorAll('#scope span').forEach(function(x){x.classList.remove('on');});sp.classList.add('on');scope=sp.dataset.s;};});
    // 下载视图动作
    $('sniffbtn').onclick=startSniff;$('url').addEventListener('keydown',function(e){if(e.key==='Enter')startSniff();});
    $('dlall').onclick=function(){addSelected(true);};$('dlsel').onclick=function(){addSelected(false);};
    $('dlclear').onclick=function(){renderDl([]);};
    $('magnetbtn').onclick=function(){toast('把磁力链接贴进网址框即可');$('url').focus();};
    $('openout').onclick=function(e){e.preventDefault();rpc('sniffdl.openOutDir');};
    $('refresh').onclick=tick;
    // 设置绑定
    bindSet('s_quality','quality','sel');bindSet('s_threads','dl_threads','sel');bindSet('s_proxy','proxy_mode','sel');
    bindSet('s_after','after','sel');bindSet('s_cookie','cookie_browser','sel');bindSet('s_rxcap','rx_cap_gb','text');
    bindSet('s_dash','dash_aria2','sw');bindSet('s_rpcen','rpc_enable','sw');bindSet('s_autostart','autostart','sw');bindSet('s_pet','pet_live','sw');
    bindSet('s_port','rpc_port','text');bindSet('s_token','rpc_token','text');bindSet('s_outdir','outdir','text');
    bindSet('s_rten','rt_enable','sw');bindSet('s_rtbase','rt_base_url','text');bindSet('s_rtkey','rt_api_key','text');bindSet('s_rtmodel','rt_model','sel');bindSet('s_rtins','rt_instructions','text');
    $('rt_refresh').onclick=function(){ rtRefreshModels($('s_rtmodel').value); toast('已刷新模型列表'); };
    $('rt_test').onclick=function(){
      var b=$('s_rtbase').value,k=$('s_rtkey').value,m=$('s_rtmodel').value;
      $('rt_msg').textContent='测试中…';$('rt_test').disabled=true;
      rpc('sniffdl.testGateway',b,k,m).then(function(r){
        $('rt_test').disabled=false;
        var d=(r&&r.result)||r||{};
        if(d.ok){$('rt_msg').textContent='✅ 连通：/responses 可用（模型 '+d.model+' · 网关 '+d.models_count+' 个模型）';}
        else{
          var msg='';
          if(d.models_err) msg+='models:'+d.models_err+' ';
          if(d.responses_http) msg+='/responses HTTP '+d.responses_http+' ';
          if(d.responses_err) msg+=String(d.responses_err).slice(0,80);
          if(d.model_exists===false) msg+='（所选模型不在列表里）';
          $('rt_msg').textContent='❌ '+(msg||'未通');
        }
      });
    };
    document.querySelectorAll('#bgpick .bgtile').forEach(function(t){ t.onclick=function(){
      var v=t.getAttribute('data-bg'); applyBg(v); rpc('sniffdl.setSetting','ui_bg',v);
      toast(v==='none'?'已切回纯净界面':'背景已切换'); }; });
    $('s_pick').onclick=function(){rpc('sniffdl.pickDir','选择保存目录').then(function(r){var p=(r&&r.result&&r.result.path)||r&&r.path||r&&r.result;if(p&&typeof p==='string'){$('s_outdir').value=p;rpc('sniffdl.setSetting','outdir',p);}});};
    $('s_open').onclick=function(){rpc('sniffdl.openOutDir');};
    $('resetstats').onclick=function(){toast('统计重置请在桌面端确认');};
    $('logrefresh').onclick=function(){Promise.all([rpc('sniffdl.status'),rpc('aria2.getGlobalStat'),rpc('sniffdl.getSettings')]).then(function(a){renderLog((a[0]&&a[0].result)||{},(a[1]&&a[1].result)||{},((a[2]&&a[2].result)||{}).settings||{});});};
    // Cookie：登录窗抓取 + 导入 + 状态
    $('ck_login').onclick=startLoginCookie;
    $('ck_import').onclick=importCookieText;
    // 组件自动更新
    $('cp_check').onclick=checkUpdates;
    $('cp_upd').onclick=function(){rpc('sniffdl.updateOne','yt-dlp').then(function(){toast('开始升级 yt-dlp…');pollComponents(20);});};
    // 剪贴板下载条
    $('clkgo').onclick=function(){var u=$('clklk').textContent;$('url').value=u;$('clipbar').classList.remove('on');lastClip='';rpc('sniffdl.clipDismiss');startSniff();};
    $('clkno').onclick=function(){rpc('sniffdl.clipDismiss');$('clipbar').classList.remove('on');lastClip='';};

    // 主题 + 设置 + 状态 + Cookie/组件
    rpc('sniffdl.theme').then(function(r){var t=(r&&r.result)||r||{};if(t&&t.theme)applyTheme(t);});
    loadSettings();loadCookieInfo();loadComponents();
    Promise.all([rpc('sniffdl.status'),rpc('aria2.getGlobalStat'),rpc('sniffdl.getSettings')]).then(function(a){
      renderStatus((a[0]&&a[0].result)||{},(a[1]&&a[1].result)||{});
      renderLog((a[0]&&a[0].result)||{},(a[1]&&a[1].result)||{},((a[2]&&a[2].result)||{}).settings||{});
    });
    tick();setInterval(tick,2000);pollClip();setInterval(pollClip,2500);
  }

  if(window.pywebview&&window.pywebview.api&&window.pywebview.api.call){ init(); }
  else { window.addEventListener('pywebviewready',init,{once:true});
         // 无桥环境（纯浏览器预览）也初始化，rpc 会返回 no-bridge 优雅降级
         setTimeout(function(){ if(!window.__inited){window.__inited=1;init();} },300); }
  window.__inited=1;
})();
"""

html = ('<!doctype html><html lang="zh"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        '<title>SniffDL · 大肥鱼</title><style>' + CSS + '</style></head><body>'
        + BODY + '<script>' + JS + '</script></body></html>')

repl = {"__FEIYU__": FEIYU}
for k, v in ICONS.items():
    repl["__I_%s__" % k] = v
for k, v in NAV_ICONS.items():
    repl["__N_%s__" % k] = v
for k, v in repl.items():
    html = html.replace(k, v)

OUT = os.path.join(ROOT, "ui_web")
os.makedirs(OUT, exist_ok=True)
open(os.path.join(OUT, "index.html"), "w", encoding="utf-8").write(html)
print("wrote", os.path.join(OUT, "index.html"), len(html), "bytes")
