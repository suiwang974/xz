# -*- coding: utf-8 -*-
"""v3 界面冒烟：起真内核（隔离数据目录）→ /ui/ 托管 → Playwright 粘贴本地 http 文件链接 → 下载完成 → 详情抽屉 → 浅/深色截图。

用法（先 source env/setup.sh 与 env/v3env.sh）：
    python3 tests/ui_smoke.py            # 截图写到 docs/ui_shots/
退出码 0 = 通过。
"""
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = "/tmp/sdlv3"
SHOTS = os.path.join(HERE, "docs", "ui_shots")
TOKEN = "smoketoken123"
FILE_SIZE = 6 * 1024 * 1024


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class SlowFile(http.server.BaseHTTPRequestHandler):
    """慢速文件服务器（约 1.5 MB/s），让速度曲线有东西可画；支持 Range。"""
    blob = os.urandom(FILE_SIZE)

    def log_message(self, *a):
        pass

    def _send(self, body_range):
        a, b = body_range
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(b - a))
        self.send_header("Content-Disposition", 'attachment; filename="smoke_sample.bin"')
        self.end_headers()
        if self.command == "HEAD":
            return
        pos = a
        try:
            while pos < b:
                n = min(64 * 1024, b - pos)
                self.wfile.write(self.blob[pos:pos + n])
                pos += n
                time.sleep(0.04)
        except Exception:
            pass

    def do_GET(self):
        rg = self.headers.get("Range")
        if rg and rg.startswith("bytes="):
            s, _, e = rg[6:].partition("-")
            a = int(s or 0)
            b = int(e) + 1 if e else FILE_SIZE
            self.send_response(206)
            self.send_header("Content-Range", "bytes %d-%d/%d" % (a, b - 1, FILE_SIZE))
            self._send((a, b))
        else:
            self.send_response(200)
            self._send((0, FILE_SIZE))

    do_HEAD = do_GET


def rpc(port, method, *params):
    req = urllib.request.Request("http://127.0.0.1:%d/jsonrpc" % port,
                                 data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": list(params)}).encode(),
                                 headers={"Content-Type": "application/json", "X-SniffDL-Token": TOKEN})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def main():
    shutil.rmtree(DATA, ignore_errors=True)
    os.makedirs(os.path.join(DATA, "out"), exist_ok=True)
    os.makedirs(SHOTS, exist_ok=True)
    fport = free_port()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", fport), SlowFile)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    cport = free_port()
    core = subprocess.Popen([sys.executable, os.path.join(HERE, "tests", "ui_smoke_core.py"), "--data-dir", DATA, "--port", str(cport),
                             "--runtime", os.path.join(DATA, "runtime.json"), "--ui-token", TOKEN],
                            cwd=HERE, stdout=open(os.path.join(DATA, "core.out"), "w"), stderr=subprocess.STDOUT)
    fails = []
    try:
        for _ in range(80):
            try:
                if rpc(cport, "sniffdl.ping").get("result"):
                    break
            except Exception:
                time.sleep(0.25)
        else:
            raise SystemExit("内核没起来，见 %s/core.out" % DATA)
        r = rpc(cport, "sniffdl.settings.set", {"outdir": os.path.join(DATA, "out"), "clip_watch": False, "notify": False,
                                                "ui_theme": "跟随系统"})
        print("settings.set:", "ok" if "result" in r else r)

        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            br = p.chromium.launch(args=["--no-sandbox"])
            errors = []
            for scheme in ("light", "dark"):
                ctx = br.new_context(viewport={"width": 1320, "height": 860}, color_scheme=scheme, device_scale_factor=1, bypass_csp=True)
                pg = ctx.new_page()
                pg.on("pageerror", lambda e: errors.append("pageerror: %s" % e))
                pg.on("console", lambda m: errors.append("console.%s: %s" % (m.type, m.text)) if m.type == "error" else None)
                pg.goto("http://127.0.0.1:%d/ui/index.html#port=%d&token=%s" % (cport, cport, TOKEN))
                pg.wait_for_function("window.__sdlReady === true", timeout=20000)
                theme = pg.evaluate("document.documentElement.dataset.theme")
                print("loaded", scheme, theme, flush=True)
                if theme != scheme:
                    fails.append("主题没跟随系统：期望 %s 实际 %s" % (scheme, theme))
                if scheme == "light":
                    url = "http://127.0.0.1:%d/files/smoke_sample.bin" % fport
                    pg.click("#addInput")
                    pg.keyboard.insert_text(url)
                    pg.keyboard.press("Control+Enter")
                    pg.wait_for_selector(".task", timeout=15000)
                    time.sleep(2.5)
                    pg.screenshot(path=os.path.join(SHOTS, "light_downloading.png"))
                    gid = pg.get_attribute(".task", "data-gid")
                    pg.evaluate("g => window.__sdl.openDetail(g)", gid)
                    pg.wait_for_selector("#detail #dStats", timeout=5000)
                    time.sleep(1.2)
                    pg.screenshot(path=os.path.join(SHOTS, "light_detail_active.png"))
                    pg.keyboard.press("Escape")
                    try:
                        pg.wait_for_function("[...window.__sdl.S.tasks.values()].some(t => t.status === 'complete')", timeout=40000)
                    except Exception:
                        fails.append("40 秒内没下完：%s" % pg.evaluate("JSON.stringify([...window.__sdl.S.tasks.values()].map(t=>[t.status,t.done,t.total,t.err]))"))
                    time.sleep(0.8)
                    out = os.path.join(DATA, "out", "smoke_sample.bin")
                    if not (os.path.isfile(out) and os.path.getsize(out) == FILE_SIZE):
                        hits = [os.path.join(d, f) for d, _, fs in os.walk(DATA) for f in fs if f.startswith("smoke_sample")]
                        if not any(os.path.getsize(h) == FILE_SIZE for h in hits):
                            fails.append("没找到完整的下载文件：%s" % hits)
                    # 导航计数
                    cnt = pg.inner_text('[data-nav="done"] .cnt')
                    if cnt.strip() != "1":
                        fails.append("「已完成」计数不对：%r" % cnt)
                # 两种主题都截：列表、详情、三个占位页
                pg.evaluate("window.__sdl.go('all')")
                time.sleep(0.5)
                pg.mouse.move(5, 5)
                pg.screenshot(path=os.path.join(SHOTS, "%s_main.png" % scheme))
                gid = pg.get_attribute(".task", "data-gid")
                if gid:
                    pg.evaluate("g => window.__sdl.openDetail(g)", gid)
                    pg.wait_for_selector("#detail #dStats", timeout=5000)
                    time.sleep(0.8)
                    pg.screenshot(path=os.path.join(SHOTS, "%s_detail.png" % scheme))
                    pg.click('#detail [data-tab="src"]')
                    time.sleep(0.3)
                    if "smoke_sample.bin" not in pg.inner_text("#detail .db"):
                        fails.append("来源页没显示下载地址")
                    pg.keyboard.press("Escape")
                    if pg.query_selector("#detail"):
                        fails.append("Esc 没关掉详情")
                for v in ("pending", "queues", "settings"):
                    pg.keyboard.press("Alt+%d" % {"pending": 5, "queues": 6, "settings": 9}[v])
                    time.sleep(0.4)
                    vis = pg.evaluate("v => !document.getElementById(v).classList.contains('hidden')",
                                      {"pending": "vPending", "queues": "vQueues", "settings": "vSettings"}[v])
                    if not vis:
                        fails.append("Alt 快捷键没切到 %s" % v)
                    if scheme == "light" or v == "settings":
                        pg.screenshot(path=os.path.join(SHOTS, "%s_%s.png" % (scheme, v)))
                # 乌龟按钮
                if scheme == "light":
                    pg.click("#btnTurtle")
                    pg.wait_for_function("window.__sdl.S.stats.turtle === true", timeout=5000)
                    pg.click("#btnTurtle")
                    pg.wait_for_function("window.__sdl.S.stats.turtle === false", timeout=5000)
                ctx.close()
            br.close()
            errs = [e for e in errors if "favicon" not in e]
            if errs:
                fails.append("页面报错：\n  " + "\n  ".join(errs[:10]))
    finally:
        try:
            rpc(cport, "sniffdl.core.shutdown")
        except Exception:
            pass
        time.sleep(0.8)
        if core.poll() is None:
            core.terminate()
        srv.shutdown()
    if fails:
        print("FAIL")
        for f in fails:
            print(" -", f)
        return 1
    print("PASS  截图：", SHOTS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
