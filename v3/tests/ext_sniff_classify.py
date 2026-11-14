# -*- coding: utf-8 -*-
"""嗅探分类器与去重（有头 Chromium）：sniffClassify 表驱动 + 同一地址重复请求只记一条。先 sh tests/host_xvfb.sh，DISPLAY=:77。"""
import http.server, os, shutil, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ext_e2e as E

PAGE = b"""<!doctype html><meta charset=utf-8><body>
<script>for (let i=0;i<3;i++) fetch('/dup.mp4').catch(()=>{}); fetch('/dup.mp4?x=1').catch(()=>{}); fetch('/a.ts').catch(()=>{});</script>"""


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/favicon.ico":
            self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers(); return
        b, ct = (PAGE, "text/html") if p == "/s.html" else (b"\0" * 4096, "video/mp4" if p.endswith(".mp4") else "application/octet-stream")
        self.send_response(200); self.send_header("Content-Type", ct); self.send_header("Content-Length", str(len(b))); self.end_headers()
        try: self.wfile.write(b)
        except Exception: pass


CASES = [
    ("http://h/a.mp4", "", "video"), ("http://h/a.MP4?token=1", "", "video"), ("http://h/a.m3u8", "", "m3u8"),
    ("http://h/x", "application/vnd.apple.mpegurl", "m3u8"), ("http://h/a.mpd", "", "mpd"),
    ("http://h/x", "application/dash+xml", "mpd"), ("http://h/a.mp3", "", "audio"), ("http://h/x", "audio/mpeg", "audio"),
    ("http://h/x", "video/webm; codecs=vp9", "video"), ("http://h/a.png", "image/png", ""), ("http://h/a.mp4", "image/jpeg", ""),
    ("http://h/a.js", "application/javascript", ""), ("http://h/a.json", "application/json", ""), ("http://h/", "text/html", ""),
    ("not a url", "", ""), ("http://h/a.mp4", "text/plain", ""),
]


def main():
    shutil.rmtree(E.DATA, ignore_errors=True)
    os.makedirs(E.DATA, exist_ok=True)
    fport = E.free_port()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", fport), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(os.path.join(E.DATA, "profile"), headless=False, bypass_csp=True,
            args=["--no-sandbox", "--disable-extensions-except=" + E.EXT, "--load-extension=" + E.EXT, "--no-first-run"])
        sw = E.wait(lambda: ctx.service_workers and ctx.service_workers[0], 15)
        for u, ct, want in CASES:
            got = sw.evaluate("(a)=>self.__sdlSniffClassify(a[0],a[1])", [u, ct])
            E.check("分类 %s [%s] → %r" % (u, ct, want), got == want, got)
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        pg.goto("http://127.0.0.1:%d/s.html" % fport); time.sleep(2.5)
        tid = sw.evaluate("chrome.tabs.query({active:true,currentWindow:true}).then(t=>t[0].id)")
        pop = ctx.new_page(); pop.goto(sw.url.rsplit("/", 1)[0] + "/popup.html"); time.sleep(0.5)
        r = pop.evaluate("(id)=>new Promise(r=>chrome.runtime.sendMessage({type:'sniff_list',tabId:id},r))", tid)
        urls = [i["url"] for i in (r or {"items": []})["items"]]
        E.check("同地址请求 3 次只记 1 条", sum(1 for u in urls if u.endswith("/dup.mp4")) == 1, urls)
        E.check("带不同查询串算另一条", sum(1 for u in urls if "dup.mp4?x=1" in u) == 1, urls)
        E.check("octet-stream 的 .ts 分片不记（避免刷屏），总数恰为 2", len(urls) == 2, urls)
        ctx.close()
    srv.shutdown()
    R = E.RESULTS
    np_ = sum(1 for r in R if r[-1] is True or (len(r) > 1 and r[1] is True))
    print("ext_sniff_classify: %d/%d" % (np_, len(R)))
    return 0 if np_ == len(R) else 1


if __name__ == "__main__":
    sys.exit(main())
