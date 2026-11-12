# -*- coding: utf-8 -*-
"""扩展 webRequest 嗅探端到端（有头 Chromium + 真内核）。先 sh tests/host_xvfb.sh，DISPLAY=:77。"""
import http.server, json, os, shutil, subprocess, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ext_e2e as E

SEEN = []   # (path, headers)
BODY = {"/clip.mp4": ("video/mp4", b"\0" * 200000), "/live.m3u8": ("application/vnd.apple.mpegurl", b"#EXTM3U\n#EXT-X-ENDLIST\n"),
        "/pic.png": ("image/png", b"\x89PNG" + b"\0" * 100), "/song.mp3": ("audio/mpeg", b"\0" * 50000),
        "/api.json": ("application/json", b"{}"), "/blob": ("video/webm", b"\0" * 30000)}
PAGE = b"""<!doctype html><meta charset=utf-8><title>sniff</title><body>
<video id=v src="/clip.mp4" preload=auto muted></video>
<script>for (const u of ['/live.m3u8','/pic.png','/song.mp3','/api.json','/blob']) fetch(u).catch(()=>{});</script>"""


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/s.html":
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.send_header("Content-Length", str(len(PAGE))); self.end_headers(); self.wfile.write(PAGE); return
        if p in BODY:
            SEEN.append((p, dict(self.headers)))
            ct, b = BODY[p]
            self.send_response(200); self.send_header("Content-Type", ct); self.send_header("Content-Length", str(len(b))); self.send_header("Set-Cookie", "k=v"); self.end_headers()
            try: self.wfile.write(b)
            except Exception: pass
            return
        self.send_response(404); self.end_headers()


def main():
    shutil.rmtree(E.DATA, ignore_errors=True)
    os.makedirs(os.path.join(E.DATA, "out"), exist_ok=True)
    fport = E.free_port()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", fport), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % fport
    cport = E.free_port()
    core = subprocess.Popen([sys.executable, os.path.join(E.HERE, "tests", "ui_smoke_core.py"), "--data-dir", E.DATA, "--port", str(cport),
                             "--runtime", os.path.join(E.DATA, "runtime.json"), "--ui-token", E.TOKEN],
                            cwd=E.HERE, stdout=open(os.path.join(E.DATA, "core.out"), "w"), stderr=subprocess.STDOUT)
    try:
        if not E.wait(lambda: E.rpc(cport, "sniffdl.ping"), 25): raise SystemExit("内核没起来")
        E.rpc(cport, "sniffdl.settings.set", {"outdir": os.path.join(E.DATA, "out"), "clip_watch": False, "notify": False, "rpc_confirm_dialog": True})
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(os.path.join(E.DATA, "profile"), headless=False, bypass_csp=True,
                args=["--no-sandbox", "--disable-extensions-except=" + E.EXT, "--load-extension=" + E.EXT, "--no-first-run"])
            sw = E.wait(lambda: ctx.service_workers and ctx.service_workers[0], 15)
            sw.evaluate("(port) => chrome.storage.local.set({ port })", cport)
            E.check("分类器已暴露", sw.evaluate("typeof self.__sdlSniffClassify") == "function")
            pg = ctx.pages[0] if ctx.pages else ctx.new_page()
            pg.goto(base + "/s.html"); time.sleep(2.5)
            tid = sw.evaluate("chrome.tabs.query({active:true,currentWindow:true}).then(t=>t[0].id)")
            pop = ctx.new_page(); pop.goto(sw.url.rsplit("/", 1)[0] + "/popup.html"); time.sleep(0.5)
            lst = lambda: pop.evaluate("(id)=>new Promise(r=>chrome.runtime.sendMessage({type:'sniff_list',tabId:id},r))", tid)
            items = E.wait(lambda: (lambda x: x if x and len(x["items"]) >= 3 else None)(lst()), 8)
            urls = [i["url"] for i in (items or {"items": []})["items"]]
            kinds = {u.rsplit("/", 1)[-1]: i["kind"] for u, i in zip(urls, items["items"])} if items else {}
            E.check("收到 mp4（video）", kinds.get("clip.mp4") == "video", kinds)
            E.check("收到 m3u8", kinds.get("live.m3u8") == "m3u8", kinds)
            E.check("收到 mp3（audio）", kinds.get("song.mp3") == "audio", kinds)
            E.check("按 Content-Type 收到无扩展名 video/webm", kinds.get("blob") == "video", kinds)
            E.check("图片不误收", "pic.png" not in kinds, kinds)
            E.check("json 不误收", "api.json" not in kinds, kinds)
            E.check("徽标显示条数", sw.evaluate("(id)=>chrome.action.getBadgeText({tabId:id})", tid) == str(len(urls)), urls)
            mark = len(SEEN)
            r = pop.evaluate("(a)=>new Promise(r=>chrome.runtime.sendMessage({type:'sniff_push',tabId:a[0],url:a[1]},r))", [tid, [u for u in urls if u.endswith("clip.mp4")][0]])
            E.check("一键推送成功", r and r.get("ok"), r)
            pend = E.wait(lambda: [x for x in E.rpc(cport, "sniffdl.snapshot")["pending"] if "clip.mp4" in (x.get("url") or "")], 8)
            E.check("内核待确认区收到该条且带页面 Referer", bool(pend) and "/s.html" in pend[0].get("referer", ""), pend)
            if pend:
                E.rpc(cport, "sniffdl.intercept.answer", pend[0]["id"], True, {})
            got = E.wait(lambda: [h for pth, h in SEEN[mark:] if pth == "/clip.mp4"], 15)
            hh = {k.lower(): v for k, v in (got[0] if got else {}).items()}
            E.check("内核真下载请求带 Referer", "/s.html" in hh.get("referer", ""), hh)
            E.check("内核真下载请求带浏览器 User-Agent", "Chrome" in hh.get("user-agent", ""), hh)
            # ---- popup 嗅探列表 UI 组（A2）----
            ui = ctx.new_page(); ui.set_viewport_size({"width": 420, "height": 900})
            ui.goto(sw.url.rsplit("/", 1)[0] + "/popup.html?tab=%s" % tid)
            E.wait(lambda: ui.query_selector("#pageSniff .row"), 8)
            grp = ui.eval_on_selector_all("#pageSniff .grp", "e=>e.map(x=>x.dataset.kind)")
            E.check("UI：按类型分组（视频/音频/流媒体 m3u8）", grp == ["video", "audio", "m3u8"], grp)
            rows = ui.eval_on_selector_all("#pageSniff .row", "e=>e.map(x=>[x.querySelector('.nm').textContent,x.querySelector('.sz').textContent])")
            E.check("UI：行数 = 嗅探条数（图片/json 不在内）", len(rows) == len(urls) and not any(n in ("pic.png", "api.json") for n, _ in rows), rows)
            sz = dict(rows).get("clip.mp4", "")
            E.check("UI：显示人类可读大小（200000 B → 195 KB）", sz == "195 KB", sz)
            E.check("UI：每行有「下载」按钮", ui.eval_on_selector_all("#pageSniff .row button.pushOne", "e=>e.length") == len(rows))
            try:
                os.makedirs(os.path.join(E.HERE, "docs", "ui_shots"), exist_ok=True)
                ui.query_selector("#pageSniffCard").screenshot(path=os.path.join(E.HERE, "docs", "ui_shots", "ext_popup_sniff.png"))
            except Exception as e:
                print("截图失败", e)
            pend0 = len(E.rpc(cport, "sniffdl.snapshot")["pending"])
            ui.click("#pageSniff .row:has-text('song.mp3') button.pushOne")
            okb = E.wait(lambda: ui.inner_text("#pageSniff .row:has-text('song.mp3') button").startswith("已推送"), 8)
            E.check("UI：点「下载」→ 按钮变「已推送 ✓」", okb)
            pend1 = E.wait(lambda: [x for x in E.rpc(cport, "sniffdl.snapshot")["pending"] if "song.mp3" in (x.get("url") or "")], 8)
            E.check("UI：一键推送后内核待确认区出现 song.mp3", bool(pend1), pend1)
            ui.click("#pushAllSniff")
            E.wait(lambda: ui.eval_on_selector_all("#pageSniff .row button:not([disabled])", "e=>e.length") == 0, 12)
            allp = E.wait(lambda: (lambda l: l if len(l) >= len(rows) - 1 else None)(
                [x for x in E.rpc(cport, "sniffdl.snapshot")["pending"] if any(k in (x.get("url") or "") for k in ("clip.mp4", "live.m3u8", "blob"))]), 10)
            E.check("UI：「全部下载」把其余条目都推给内核", bool(allp), E.rpc(cport, "sniffdl.snapshot")["pending"])
            ui.click("#clearPageSniff")
            E.wait(lambda: ui.query_selector("#pageSniff .empty"), 6)
            E.check("UI：「清空」后列表为空且显示提示", ui.query_selector("#pageSniff .row") is None and ui.query_selector("#pageSniff .empty") is not None)
            E.check("UI：清空后徽标消失", sw.evaluate("(id)=>chrome.action.getBadgeText({tabId:id})", tid) == "")
            ctx.close()
    finally:
        core.terminate()
    bad = [r for r in E.RESULTS if not r[1]]
    print("%d/%d 通过" % (len(E.RESULTS) - len(bad), len(E.RESULTS)))
    sys.exit(1 if bad else 0)

main()
