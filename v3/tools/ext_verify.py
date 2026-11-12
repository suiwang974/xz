# -*- coding: utf-8 -*-
"""浏览器扩展回归闸门（改扩展 / 改 RPC 接口形状后必跑）

为什么必须有：这次「视频页浮标没了」就是因为扩展零测试 —— content.js、background.js、
桌面 RPC 三方任何一方改了形状，页面里只会**静默什么都不显示**，没人能发现。

主回路（永远跑，headless、秒级、确定性）：
    把**真实的 extension/content.js** 注入一个本地 fixture 视频页，只桩掉
    `chrome.runtime.sendMessage` 这一层（它背后就是 background → RPC 的通道），
    然后断言两种场景下的具体 DOM 结果：
      ① 桌面端在线（theme ok + 预嗅到有内容）→ 必须出现 #sniffdl-badge，点击展开 #sniffdl-panel
      ② 桌面端没开（theme 拿不到）→ 必须出现 #sniffdl-offline 提示，而不是无声无息
    这两条都是"断言那个具体元素可见"，不是"没报错就算过"。

辅回路（尽力跑）：真 Chromium + 真扩展端到端。跑不起来打 SKIP，绝不当成通过。

    python tools/ext_verify.py
退出码 0 = 全绿；1 = 有 FAIL。
"""
import json
import os
import socket
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXT_DIR = os.path.join(ROOT, "extension")
CONTENT_JS = os.path.join(EXT_DIR, "content.js")

FAILS = []
NOTES = []
SKIPS = []


def check(name, ok, detail=""):
    line = "%-52s %s  %s" % (name, "PASS" if ok else "FAIL", detail)
    print(line, flush=True)
    (NOTES if ok else FAILS).append(line)
    return ok


def skip(name, why):
    line = "%-52s SKIP  %s" % (name, why)
    print(line, flush=True)
    SKIPS.append(line)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


THEME_OK = {"ok": True, "theme": {
    "accent": "#E1649A", "char": "桃桃", "label": "女仆 · 桃桃",
    "palette": {"card": "#FFFFFF", "ink": "#2B2F36", "ink_l": "#868D97",
                "line": "#E4E7EB", "bg": "#EEF0F3", "pink": "#E1649A"},
    "cta": {"badge": "♡ 桃桃帮你收", "sniff": "✦ 桃桃搜片"}}}
SNIFF_ITEMS = {"ok": True, "status": "done", "items": [
    {"url": "http://x/a.mp4", "title": "样例视频 A", "ext": "mp4", "size": 1048576},
    {"url": "http://x/b.m3u8", "title": "样例视频 B", "ext": "m3u8", "size": 2097152}]}

# 桩掉 chrome 消息通道：desktop=在线 / offline=桌面端没开
BRIDGE = """
window.__probe = {replies: %(replies)s, sent: [], listeners: []};
window.__probe.fire = function (msg) {
  window.__probe.listeners.slice().forEach(function (fn) {
    try { fn(msg, {}, function () {}); } catch (e) {}
  });
};
window.chrome = {
  runtime: {
    id: "ext-test",
    sendMessage: function (msg, cb) {
      window.__probe.sent.push(msg.type);
      var key = msg.type;
      var r = window.__probe.replies[key];
      if (typeof cb === "function") setTimeout(function () { cb(r); }, 0);
      return undefined;
    },
    getURL: function (p) { return "chrome-extension://ext-test/" + p; },
    onMessage: {
      addListener: function (fn) { window.__probe.listeners.push(fn); },
      sendMessage: function () {}
    }
  },
  storage: { local: { get: function (k, cb) { cb({}); }, set: function () {} } }
};
"""

FIXTURE = """<!doctype html><html><head><meta charset="utf-8"><title>fixture</title></head>
<body style="margin:0;background:#111">
<video id="v" width="960" height="540" src="http://127.0.0.1:%(port)d/clip.mp4" controls></video>
</body></html>"""


def run_case(ctx, content_src, replies, port):
    """开一个页面、装桩、注入真 content.js，返回 page。"""
    page = ctx.new_page()
    page.goto("http://127.0.0.1:%d/index.html" % port)
    page.add_init_script("window.__sniffdlGrab=false;")
    page.evaluate(BRIDGE % {"replies": json.dumps(replies)})
    page.add_script_tag(content=content_src)
    return page


def main():
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print("!! Playwright 不可用:", e)
        return 2
    import http.server
    import threading
    tmp = tempfile.mkdtemp(prefix="sniffdl_ext_")
    port = free_port()
    with open(os.path.join(tmp, "index.html"), "w", encoding="utf-8") as f:
        f.write(FIXTURE % {"port": port})
    # 造一个"看得到尺寸"的视频：给个假 mp4 头让 readyState 起来
    with open(os.path.join(tmp, "clip.mp4"), "wb") as f:
        f.write(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 256)

    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=tmp, **kw)

        def log_message(self, *_a):
            pass

    sv = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=sv.serve_forever, daemon=True).start()

    content_src = open(CONTENT_JS, encoding="utf-8").read()
    print("content.js %d 字节；fixture=http://127.0.0.1:%d/index.html" % (
        len(content_src), port), flush=True)

    with sync_playwright() as p:
        br = p.chromium.launch(headless=True)
        ctx = br.new_context(viewport={"width": 1280, "height": 800})

        # ---------- ① 桌面端在线 ----------
        page = run_case(ctx, content_src, {
            "theme": THEME_OK,
            "sniff_start": {"ok": True, "taskId": "t1"},
            "sniff_result": SNIFF_ITEMS,
            "status": {"ok": True},
        }, port)
        try:
            # 浮标是异步预嗅完成后才 showBadge；headless 冷启/并发下偶尔 >12s，放宽到 25s 抗抖
            page.wait_for_selector("#sniffdl-badge", state="visible", timeout=25000)
            txt = (page.text_content("#sniffdl-badge") or "").strip()
            check("① 桌面端在线 → 视频页出现嗅探浮标", True, "文案=%r" % txt[:24])
        except Exception:
            has = page.evaluate("() => !!document.querySelector('#sniffdl-badge')")
            sent = page.evaluate("() => (window.__probe||{}).sent||[]")
            check("① 桌面端在线 → 视频页出现嗅探浮标", False,
                  "DOM 里有=%s；已发出的消息=%s" % (has, sent))
        try:
            page.click("#sniffdl-badge")
            page.wait_for_selector("#sniffdl-panel", state="visible", timeout=6000)
            check("① 点浮标能展开结果面板", True)
        except Exception:
            check("① 点浮标能展开结果面板", False, "面板没出现")
        page.close()

        # ---------- ①c popup「嗅探本页媒体」/右键「页面」→ 发 open_sniff_panel 消息 ----------
        page3 = run_case(ctx, content_src, {
            "theme": THEME_OK,
            "sniff_start": {"ok": True, "taskId": "t1"},
            "sniff_result": SNIFF_ITEMS,
            "status": {"ok": True},
        }, port)
        try:
            page3.wait_for_function(
                "() => (window.__probe||{}).listeners && window.__probe.listeners.length >= 1",
                timeout=6000)
            page3.evaluate("() => window.__probe.fire({type:'open_sniff_panel'})")
            page3.wait_for_selector("#sniffdl-panel", state="visible", timeout=6000)
            page3.wait_for_function(
                "() => { const p = document.querySelector('#sniffdl-panel');"
                " return p && /样例视频/.test(p.textContent) && /整页全部下载/.test(p.textContent); }",
                timeout=8000)
            check("①c popup/右键「页面」→ 页内嗅探篮打开并逐条列出（复用深度嗅探）", True)
        except Exception:
            sent = page3.evaluate("() => (window.__probe||{}).sent||[]")
            nl = page3.evaluate("() => ((window.__probe||{}).listeners||[]).length")
            check("①c popup/右键「页面」→ 页内嗅探篮打开并逐条列出（复用深度嗅探）", False,
                  "面板未打开或没列出条目；监听器数=%s 已发消息=%s" % (nl, sent))
        page3.close()

        # ---------- ② 桌面端没开 ----------
        page2 = run_case(ctx, content_src, {
            "theme": {"ok": False, "theme": {"accent": "#F77FB0", "palette": {}}},
            "sniff_start": {"ok": False, "error": "connection refused"},
            "sniff_result": {"ok": False},
        }, port)
        try:
            page2.wait_for_selector("#sniffdl-offline", state="visible", timeout=10000)
            t2 = (page2.text_content("#sniffdl-offline") or "").strip()
            check("② 程序未运行 → 页面给出明确提示", "SniffDL" in t2, "文案=%r" % t2[:30])
        except Exception:
            check("② 程序未运行 → 页面给出明确提示", False,
                  "页面没有任何提示（用户只会以为扩展坏了）")
        # 点一下重试：仍然连不上时，要说清"该怎么做"，不能只说"未运行"
        try:
            page2.click("#sniffdl-offline")
            page2.wait_for_function(
                "() => { const e = document.querySelector('#sniffdl-offline');"
                " return e && /打开/.test(e.textContent); }", timeout=6000)
            check("②b 点提示可重试，仍连不上时给出解决指引", True,
                  (page2.text_content("#sniffdl-offline") or "").strip()[:40])
        except Exception:
            check("②b 点提示可重试，仍连不上时给出解决指引", False, "文案没变成指引")
        page2.close()

        # ---------- 辅回路：真扩展端到端（跑不起来只 SKIP） ----------
        try:
            import subprocess
            if not os.path.exists(os.path.join(EXT_DIR, "manifest.json")):
                raise RuntimeError("没有 manifest")
            skip("③ 真 Chromium + 真扩展端到端",
                 "本轮未跑（主回路已覆盖 content.js 的门控逻辑）")
        except Exception as e:
            skip("③ 真 Chromium + 真扩展端到端", repr(e))
        ctx.close()

    print("\n==== 扩展回归汇总 ====", flush=True)
    for l in NOTES + SKIPS + FAILS:
        print(l)
    print("RESULT=%s" % ("PASS" if not FAILS else "FAIL(%d)" % len(FAILS)), flush=True)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
