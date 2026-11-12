# -*- coding: utf-8 -*-
"""扩展 0.7.1 视频悬浮下载按钮：Playwright Chromium（有头，Xvfb :77）加载 extension/，不需要内核。
用法：source /workspace/work/env/setup.sh && sh tests/host_xvfb.sh && DISPLAY=:77 python3 tests/ext_hover.py
覆盖：大视频悬停出钮、小视频不出、移开消失、点击弹网页下载卡（卡内含视频地址）、blob 视频交页面地址、× 后本站不再出、popup 开关关掉后不出。
"""
import http.server, os, shutil, socketserver, sys, threading, time, json

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXT = os.path.join(HERE, "extension")
R = []

def check(n, ok, d=""):
    R.append((n, bool(ok), d)); print(("  PASS " if ok else "  FAIL ") + n + ("" if ok else "  <- " + str(d)[:200]), flush=True)

PAGE = b"""<!doctype html><meta charset=utf-8><body style="margin:0">
<video id=big src="/clip/movie_01.mp4" style="position:absolute;left:40px;top:40px;width:640px;height:360px;background:#000"></video>
<video id=small src="/clip/tiny.mp4" style="position:absolute;left:760px;top:40px;width:160px;height:90px;background:#000"></video>
<div id=cover style="position:absolute;left:40px;top:40px;width:640px;height:360px"></div>
</body>"""
BLOB = b"""<!doctype html><meta charset=utf-8><body style="margin:0">
<video id=v style="position:absolute;left:40px;top:40px;width:640px;height:360px;background:#000"></video>
<script>document.getElementById('v').src=URL.createObjectURL(new Blob([new Uint8Array(64)],{type:'video/mp4'}));</script></body>"""

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        body = PAGE if self.path.startswith("/page") else BLOB if self.path.startswith("/blob") else b"\0" * 1024
        ct = "text/html; charset=utf-8" if self.path.startswith(("/page", "/blob")) else "video/mp4"
        self.send_response(200); self.send_header("Content-Type", ct); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

def main():
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H); port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % port
    prof = "/tmp/sdlhover_prof"; shutil.rmtree(prof, ignore_errors=True)
    from playwright.sync_api import sync_playwright
    BTN = "[data-sniffdl=hover-btn]"
    vis = "(()=>{const b=document.querySelector('%s');return !!b&&b.style.display!=='none'&&b.style.opacity==='1'})()" % BTN
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(prof, headless=False, viewport={"width": 1200, "height": 800},
            args=["--no-sandbox", "--disable-extensions-except=" + EXT, "--load-extension=" + EXT, "--no-first-run"])
        t0 = time.time()
        while not ctx.service_workers and time.time() - t0 < 15: time.sleep(0.3)
        sw = ctx.service_workers[0]
        _want = json.load(open(os.path.join(HERE, "extension", "manifest.json"), encoding="utf-8"))["version"]
        check("扩展版本 = manifest %s" % _want, sw.evaluate("chrome.runtime.getManifest().version") == _want)
        pg = ctx.new_page(); pg.goto(base + "/page"); pg.wait_for_timeout(800)
        pg.mouse.move(300, 200); pg.mouse.move(320, 210); pg.wait_for_timeout(400)
        check("大视频（被控件层盖住）悬停出钮", pg.evaluate(vis))
        pg.screenshot(path=os.path.join(HERE, "docs", "ui_shots", "ext_hover_btn.png"))
        box = pg.evaluate("(()=>{const r=document.querySelector('%s').getBoundingClientRect();return [r.right,r.top]})()" % BTN)
        check("按钮在播放器右上角", 560 < box[0] <= 680 and 40 <= box[1] < 70, box)
        pg.mouse.move(840, 80); pg.wait_for_timeout(1700)
        check("小视频不出钮 + 移开 1.2s 后隐藏", not pg.evaluate(vis))
        pg.mouse.move(300, 200); pg.mouse.move(310, 205); pg.wait_for_timeout(400)
        pg.click(BTN + " [data-role=go]"); pg.wait_for_timeout(1500)
        txt = pg.evaluate("document.documentElement.innerText+'|'+[...document.querySelectorAll('input')].map(i=>i.value).join('|')")
        check("点击弹网页下载卡", "下载文件信息" in txt, txt[:200])
        check("卡片带视频文件名", "movie_01" in txt, txt[:200])
        # blob
        pg2 = ctx.new_page(); pg2.goto(base + "/blob"); pg2.wait_for_timeout(800)
        pg2.mouse.move(300, 200); pg2.mouse.move(305, 205); pg2.wait_for_timeout(400)
        check("blob/MSE 视频也出钮", pg2.evaluate(vis))
        pg2.click(BTN + " [data-role=go]"); pg2.wait_for_timeout(1500)
        txt2 = pg2.evaluate("document.documentElement.innerText+'|'+[...document.querySelectorAll('input')].map(i=>i.value).join('|')")
        check("blob 视频交页面地址（卡里不是 blob:）", "下载文件信息" in txt2 and "blob:" not in txt2, txt2[:200])
        # × 关闭
        pg3 = ctx.new_page(); pg3.goto(base + "/page"); pg3.wait_for_timeout(800)
        pg3.mouse.move(300, 200); pg3.mouse.move(306, 206); pg3.wait_for_timeout(400)
        pg3.click(BTN + " [data-role=close]"); pg3.wait_for_timeout(300)
        pg3.reload(); pg3.wait_for_timeout(800); pg3.mouse.move(300, 200); pg3.mouse.move(307, 207); pg3.wait_for_timeout(400)
        check("× 后本站本会话不再出", not pg3.evaluate(vis))
        # popup 开关
        sw.evaluate("chrome.storage.local.set({hoverBtn:false})")
        pg4 = ctx.new_page(); pg4.goto(base.replace("127.0.0.1", "localhost") + "/page"); pg4.wait_for_timeout(900)
        pg4.mouse.move(300, 200); pg4.mouse.move(308, 208); pg4.wait_for_timeout(400)
        check("设置 hoverBtn=false 后不出", not pg4.evaluate(vis))
        ext_id = sw.url.split("/")[2]
        pp = ctx.new_page(); pp.goto("chrome-extension://%s/popup.html" % ext_id); pp.wait_for_timeout(600)
        check("popup 开关反映关闭状态", pp.evaluate("document.getElementById('hoverBtn').checked") is False)
        pp.evaluate("document.getElementById('hoverBtn').click()"); pp.wait_for_timeout(300)
        check("popup 打开开关写回存储", sw.evaluate("new Promise(r=>chrome.storage.local.get({hoverBtn:true},x=>r(x.hoverBtn)))") is True)
        pg.bring_to_front(); pg.wait_for_timeout(300); pg.screenshot(path=os.path.join(HERE, "docs", "ui_shots", "ext_hover_card.png"))
        ctx.close()
    n = sum(1 for r in R if r[1]); print("RESULT %d/%d" % (n, len(R)))
    sys.exit(0 if n == len(R) else 1)

if __name__ == "__main__":
    main()
