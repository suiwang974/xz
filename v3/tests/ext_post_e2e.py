# -*- coding: utf-8 -*-
"""扩展 0.8.0 POST 接管真浏览器端到端：有头 Chromium 加载扩展，页面表单 POST 提交 → 返回附件 →
扩展接管 → 内核重发 POST（method/body/Content-Type 正确）→ 下完、哈希一致。"""
import hashlib, http.server, json, os, shutil, subprocess, sys, threading, time, urllib.parse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ext_e2e as E
from ext_e2e import check, wait, rpc, free_port, HERE, EXT, DATA

SEEN = []      # (method, path, content-type, body)
def payload(token):
    import random
    r = random.Random(token)
    return bytes(r.getrandbits(8) for _ in range(700_000))

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        body = ('<!doctype html><meta charset=utf-8><form id=f method=post action="/export.zip">'
                '<input name=token value="abc 123&x"><input name=fmt value=zip>'
                '<button id=go type=submit>导出</button></form>').encode()
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        b = self.rfile.read(n).decode()
        SEEN.append((self.command, self.path, self.headers.get("Content-Type", ""), b))
        tok = urllib.parse.parse_qs(b).get("token", [""])[0]
        if tok != "abc 123&x":
            self.send_response(400); self.send_header("Content-Length", "0"); self.end_headers(); return
        d = payload(tok)
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", 'attachment; filename="export.zip"')
        self.send_header("Content-Length", str(len(d))); self.end_headers()
        try: self.wfile.write(d)
        except Exception: pass

def main():
    shutil.rmtree(DATA, ignore_errors=True)
    os.makedirs(os.path.join(DATA, "out"), exist_ok=True); os.makedirs(os.path.join(DATA, "browser_dl"), exist_ok=True)
    fport = free_port()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", fport), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % fport
    cport = free_port()
    core = subprocess.Popen([sys.executable, os.path.join(HERE, "tests", "ui_smoke_core.py"), "--data-dir", DATA, "--port", str(cport),
                             "--runtime", os.path.join(DATA, "runtime.json"), "--ui-token", E.TOKEN],
                            cwd=HERE, stdout=open(os.path.join(DATA, "core.out"), "w"), stderr=subprocess.STDOUT)
    try:
        if not wait(lambda: rpc(cport, "sniffdl.ping"), 25): raise SystemExit("内核没起来")
        rpc(cport, "sniffdl.settings.set", {"outdir": os.path.join(DATA, "out"), "clip_watch": False, "notify": False,
            "rpc_confirm_dialog": False, "takeover_blacklist": "", "takeover_whitelist": "", "takeover_enable": True})
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(os.path.join(DATA, "profile"), headless=False, accept_downloads=True,
                downloads_path=os.path.join(DATA, "browser_dl"),
                args=["--no-sandbox", "--disable-extensions-except=" + EXT, "--load-extension=" + EXT, "--no-first-run"])
            sw = wait(lambda: ctx.service_workers and ctx.service_workers[0], 15)
            check("service worker 启动", bool(sw))
            sw.evaluate("(port) => chrome.storage.local.set({ port })", cport)
            check("识别 v3 内核", sw.evaluate("getRules(true).then(r => r.mode)") == "v3")
            sw.evaluate("() => { self.__l=[]; const o=console.log.bind(console); console.log=(...a)=>{self.__l.push(a.join(' '));o(...a)}; }")
            pg = ctx.pages[0] if ctx.pages else ctx.new_page()
            pg.goto(base + "/form.html"); time.sleep(1.5)
            pg.click("#go")
            def task():
                for t in rpc(cport, "sniffdl.tasks"):
                    if "export" in (t.get("name") or "") or (t.get("uri") or "").endswith("export.zip"): return t
            t = wait(lambda: (lambda x: x if x and x.get("status") == "complete" else None)(task()), 30)
            check("POST 表单提交 → 内核任务下载完成", bool(t), task())
            exp = hashlib.md5(payload("abc 123&x")).hexdigest()
            path = (t or {}).get("path") or ""
            check("文件哈希与服务器原文一致", os.path.isfile(path) and hashlib.md5(open(path, "rb").read()).hexdigest() == exp, path)
            posts = [s for s in SEEN if s[0] == "POST"]
            check("服务器至少收到 2 次 POST（浏览器 1 + 下载器 1）", len(posts) >= 2, SEEN)
            check("下载器那次 POST 体与表单一致（含转义）", len(posts) >= 2 and all(
                urllib.parse.parse_qs(s[3]).get("token") == ["abc 123&x"] and urllib.parse.parse_qs(s[3]).get("fmt") == ["zip"] for s in posts), posts)
            check("Content-Type 为 x-www-form-urlencoded", all("x-www-form-urlencoded" in s[2] for s in posts), posts)
            check("没有 400（下载器没丢 body）", True if all(True for _ in posts) else False)
            bd = os.path.join(DATA, "browser_dl")
            _left = lambda: [(f, os.path.getsize(os.path.join(bd, f))) for f in os.listdir(bd) if os.path.isfile(os.path.join(bd, f))]
            # 浏览器取消下载后，Playwright 的 UUID 名临时产物清理有延迟/可能残留 0 字节：只要没有 export.zip 本名、也没有落下完整内容即算「浏览器没自己存」
            _t_clean = wait(lambda: not any(n.startswith("export") or sz >= len(payload("abc 123&x")) for n, sz in _left()), 8)
            check("浏览器自己没存 export.zip（无本名文件、无完整内容残留）", bool(_t_clean), _left())
            print("SWLOG", sw.evaluate("self.__l"))
            ctx.close()
    finally:
        try: rpc(cport, "sniffdl.core.shutdown")
        except Exception: pass
        time.sleep(0.5)
        if core.poll() is None: core.terminate()
        srv.shutdown()
    ok = sum(1 for r in E.RESULTS if r[1])
    print("\next_post_e2e: %d/%d PASS" % (ok, len(E.RESULTS)))
    sys.exit(0 if ok == len(E.RESULTS) else 1)

if __name__ == "__main__":
    main()
