# -*- coding: utf-8 -*-
"""浏览器扩展 0.7.0 端到端：Playwright Chromium（有头，跑在 Xvfb :77）--load-extension 加载 extension/ + 真内核。

用法：source env/setup.sh && source env/v3env.sh && sh tests/host_xvfb.sh && python3 tests/ext_e2e.py
需要完整 chromium（不是 headless shell）：python3 -m playwright install chromium（已装在 env/pw/chromium-1148）。

覆盖：
  A 点 .zip 链接 → 扩展按内核规则接管（rpc_confirm_dialog=关 → 直接开下）→ 内核下载完成、大小一致、浏览器没有自己下
  B 按住 Alt 点 → 不接管（内核无新任务；浏览器自己下）
  C 规则改黑名单站点 → 不接管（内容脚本放行 + onCreated 也按规则不抢）
  D rpc_confirm_dialog=开 → 进待确认 → intercept.answer → 下载完成
  E 右键「下载全部链接」（经 SW 测试入口 self.__sdlMenu 触发，原生菜单点不到）→ 待确认区新增 N 条 → 界面「全部确认」→ N 个下载完成
  F 右键「分析本页」→ 本页地址进待确认区
  G 内核不在（扩展端口指向空端口）→ 不吞下载：浏览器自己下 + 图标 "!" + popup 能拿到「没有运行」提示
  H 3 个右键菜单已注册；service worker 无报错（错误收集器 + console.error 包装）
"""
import hashlib
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
EXT = os.path.join(HERE, "extension")
DATA = "/tmp/sdlext"
TOKEN = "ext-e2e-token"
RESULTS = []
FILES = {}          # name -> bytes
LINKS_N = 4


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("  PASS " if ok else "  FAIL ") + name + ("" if ok else "  ← " + str(detail)[:300]), flush=True)
    return ok


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait(fn, timeout=10, step=0.25):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = fn()
            if v:
                return v
        except Exception:
            pass
        time.sleep(step)
    return None


def mkfile(name, size):
    import random
    r = random.Random(name)
    FILES[name] = bytes(r.getrandbits(8) for _ in range(size))


PAGE = """<!doctype html><meta charset=utf-8><title>ext e2e</title>
<a id=za href="/files/a.zip">a.zip</a><br><a id=zb href="/files/b.zip">b.zip</a><br>
<a id=zc href="/files/c.zip">c.zip</a><br><a id=zd href="/files/d.zip">d.zip</a><br>
<a id=ze href="/files/e.zip">e.zip</a><br><a id=html href="/other.html">普通网页</a>"""


def links_page():
    xs = "".join('<a href="/files/batch_%d.zip">包 %d</a><br>' % (i, i) for i in range(LINKS_N))
    return ("<!doctype html><meta charset=utf-8><title>links</title>" + xs +
            '<a href="/files/batch_0.zip#dup">重复</a><a href="mailto:x@y.z">mail</a><a href="javascript:void(0)">js</a>'
            '<a href="#top">锚点</a>')


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/page.html", "/links.html", "/other.html"):
            body = (PAGE if path == "/page.html" else links_page() if path == "/links.html" else "<p>other</p>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return
        name = path.rsplit("/", 1)[-1]
        data = FILES.get(name)
        if data is None:
            self.send_response(404)
            self.end_headers()
            return
        a, b = 0, len(data)
        rg = self.headers.get("Range")
        if rg and rg.startswith("bytes="):
            s, _, e = rg[6:].partition("-")
            a = int(s or 0)
            b = int(e) + 1 if e else len(data)
            self.send_response(206)
            self.send_header("Content-Range", "bytes %d-%d/%d" % (a, b - 1, len(data)))
        else:
            self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(b - a))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(data[a:b])
            except Exception:
                pass

    do_HEAD = do_GET


def rpc(port, method, *params):
    req = urllib.request.Request("http://127.0.0.1:%d/jsonrpc" % port,
                                 data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": list(params)}).encode(),
                                 headers={"Content-Type": "application/json", "X-SniffDL-Token": TOKEN})
    with urllib.request.urlopen(req, timeout=10) as r:
        j = json.loads(r.read())
    if "error" in j and j["error"]:
        raise RuntimeError(j["error"])
    return j.get("result")


def task_by(cport, name):
    for t in rpc(cport, "sniffdl.tasks"):
        if (t.get("name") or "") == name or (t.get("uri") or "").endswith("/" + name):
            return t
    return None


def done_ok(cport, name):
    t = task_by(cport, name)
    p = (t or {}).get("path") or os.path.join(DATA, "out", name)     # 内核可能按类型归到子目录（压缩文件/）
    return t and t.get("status") == "complete" and os.path.isfile(p) and \
        hashlib.md5(open(p, "rb").read()).hexdigest() == hashlib.md5(FILES[name]).hexdigest()


SW_HOOK = """() => {
  if (self.__errs) return 'already';
  self.__errs = [];
  self.addEventListener('error', (e) => self.__errs.push('error: ' + (e.message || e)));
  self.addEventListener('unhandledrejection', (e) => self.__errs.push('rejection: ' + (e.reason && e.reason.message || e.reason)));
  const ce = console.error.bind(console);
  console.error = (...a) => { self.__errs.push('console.error: ' + a.join(' ')); ce(...a); };
  return 'hooked';
}"""


def main():
    shutil.rmtree(DATA, ignore_errors=True)
    os.makedirs(os.path.join(DATA, "out"), exist_ok=True)
    os.makedirs(os.path.join(DATA, "browser_dl"), exist_ok=True)
    for n in "abcde":
        mkfile(n + ".zip", 900_000 + ord(n))
    for i in range(LINKS_N):
        mkfile("batch_%d.zip" % i, 300_000 + i)
    fport = free_port()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", fport), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % fport

    cport = free_port()
    core = subprocess.Popen([sys.executable, os.path.join(HERE, "tests", "ui_smoke_core.py"), "--data-dir", DATA, "--port", str(cport),
                             "--runtime", os.path.join(DATA, "runtime.json"), "--ui-token", TOKEN],
                            cwd=HERE, stdout=open(os.path.join(DATA, "core.out"), "w"), stderr=subprocess.STDOUT)
    try:
        if not wait(lambda: rpc(cport, "sniffdl.ping"), 25):
            raise SystemExit("内核没起来，见 %s/core.out" % DATA)
        rpc(cport, "sniffdl.settings.set", {"outdir": os.path.join(DATA, "out"), "clip_watch": False, "notify": False,
                                            "rpc_confirm_dialog": False, "takeover_blacklist": "", "takeover_whitelist": "",
                                            "takeover_enable": True, "takeover_alt_bypass": True})
        rules = rpc(cport, "sniffdl.takeoverRules")
        check("内核 takeoverRules 可用且 zip 在接管列表", rules.get("ok") and "zip" in rules.get("exts", []), rules)

        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            prof = os.path.join(DATA, "profile")
            ctx = p.chromium.launch_persistent_context(
                prof, headless=False, accept_downloads=True, bypass_csp=True, downloads_path=os.path.join(DATA, "browser_dl"),
                args=["--no-sandbox", "--disable-extensions-except=" + EXT, "--load-extension=" + EXT, "--no-first-run"])
            sw = wait(lambda: ctx.service_workers and ctx.service_workers[0], 15)
            check("service worker 启动", bool(sw))
            sw.evaluate(SW_HOOK)
            ver = sw.evaluate("chrome.runtime.getManifest().version")
            check("扩展版本 0.8.0", ver == "0.8.0", ver)
            sw.evaluate("(port) => chrome.storage.local.set({ port })", cport)
            mode = sw.evaluate("getRules(true).then(r => r.mode)")
            check("扩展识别到 v3 内核（读到接管规则）", mode == "v3", mode)
            menus = wait(lambda: len(sw.evaluate("self.__sdlMenuIds || []")) >= 3 and sw.evaluate("self.__sdlMenuIds"), 5)
            check("右键菜单 3 项已注册", menus and {"sniffdl", "sniffdl-all", "sniffdl-analyze"} <= set(menus), menus)

            browser_dls = []
            pg = ctx.pages[0] if ctx.pages else ctx.new_page()
            pg.on("download", lambda d: browser_dls.append(d.suggested_filename))

            def bdl(name):     # 浏览器自己的下载记录（chrome.downloads，比 Playwright download 事件可靠）
                return any((x or "").split("/")[-1].split("?")[0] == name
                           for x in sw.evaluate("chrome.downloads.search({}).then(a => a.map(d => d.finalUrl || d.url))"))
            pg.goto(base + "/page.html")
            time.sleep(1.2)     # 内容脚本取规则

            # A
            pg.click("#za")
            check("A 点 .zip → 进 SniffDL 并下载完成（文件 MD5 一致）", wait(lambda: done_ok(cport, "a.zip"), 20), task_by(cport, "a.zip"))
            check("A 浏览器没有自己下 a.zip", not bdl("a.zip") and "a.zip" not in browser_dls, browser_dls)
            check("A 页面没有跳走", pg.url.endswith("/page.html"), pg.url)

            # B
            pg.click("#zb", modifiers=["Alt"])
            time.sleep(3)
            check("B Alt+点击 → 内核没有任务", task_by(cport, "b.zip") is None, task_by(cport, "b.zip"))
            check("B 浏览器自己下了 b.zip（Alt 点击交回浏览器）", wait(lambda: bdl("b.zip"), 5), browser_dls)

            # C
            rpc(cport, "sniffdl.settings.set", {"takeover_blacklist": "127.0.0.1"})
            pg.reload()
            time.sleep(1.2)
            pg.click("#zc")
            time.sleep(3)
            check("C 黑名单站点 → 内核没有任务", task_by(cport, "c.zip") is None, task_by(cport, "c.zip"))
            check("C 浏览器自己下了 c.zip（没被吞）", wait(lambda: bdl("c.zip"), 6), browser_dls)
            rpc(cport, "sniffdl.settings.set", {"takeover_blacklist": ""})

            # D
            rpc(cport, "sniffdl.settings.set", {"rpc_confirm_dialog": True})
            pg.reload()
            time.sleep(1.2)
            pg.click("#zd")
            pend = wait(lambda: [x for x in rpc(cport, "sniffdl.snapshot")["pending"] if (x.get("url") or "").endswith("/d.zip")], 8)
            check("D 先确认开 → d.zip 进待确认", bool(pend), rpc(cport, "sniffdl.snapshot")["pending"])
            if pend:
                rpc(cport, "sniffdl.intercept.answer", pend[0]["id"], True, {})
                check("D 确认后下载完成", wait(lambda: done_ok(cport, "d.zip"), 20), task_by(cport, "d.zip"))
            rpc(cport, "sniffdl.settings.set", {"rpc_confirm_dialog": False})

            # E
            pg.goto(base + "/links.html")
            time.sleep(0.8)
            r = sw.evaluate("""async () => {
              const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
              return await self.__sdlMenu({ menuItemId: 'sniffdl-all', pageUrl: tab.url }, tab);
            }""")
            grab = lambda: [g for g in rpc(cport, "sniffdl.grabber.list") if g.get("source") == "ext-page"]
            check("E 下载全部链接 → 待确认区新增 %d 条（去重/去 mailto/js/锚点）" % LINKS_N,
                  wait(lambda: len(grab()) == LINKS_N, 5) and (r or {}).get("added") == LINKS_N, (r, [g["url"] for g in grab()]))
            ui = ctx.new_page()
            uerr = []
            ui.on("pageerror", lambda e: uerr.append(str(e)))
            ui.goto("http://127.0.0.1:%d/ui/index.html#port=%d&token=%s" % (cport, cport, TOKEN))
            ui.wait_for_function("window.__sdlReady === true", timeout=20000)
            ui.keyboard.press("Alt+5")
            ui.wait_for_selector("#vPending .prow[data-key]", timeout=8000)
            rows = ui.locator("#vPending .prow[data-key]").count()
            check("E 界面待确认页列出 %d 条" % LINKS_N, rows == LINKS_N, rows)
            ui.click("#pendYes")
            allok = wait(lambda: all(done_ok(cport, "batch_%d.zip" % i) for i in range(LINKS_N)), 30)
            check("E 界面「全部确认」→ %d 个下载完成" % LINKS_N, allok,
                  [(t["name"], t["status"]) for t in rpc(cport, "sniffdl.tasks")])
            check("E 界面无 JS 报错", not uerr, uerr)
            ui.close()

            # F
            pg.bring_to_front()
            r = sw.evaluate("""async () => {
              const [tab] = await chrome.tabs.query({ url: '*://127.0.0.1/links.html' });
              return await self.__sdlMenu({ menuItemId: 'sniffdl-analyze', pageUrl: tab.url }, tab);
            }""")
            check("F 分析本页 → 本页地址进待确认区", wait(lambda: any(g.get("source") == "ext-analyze" and g["url"].endswith("/links.html")
                                                       for g in rpc(cport, "sniffdl.grabber.list")), 5), r)

            # G
            dead = free_port()
            sw.evaluate("(port) => chrome.storage.local.set({ port })", dead)
            pg.goto(base + "/page.html")
            time.sleep(1.5)
            pg.click("#ze")
            check("G 内核不在 → 浏览器自己下 e.zip（不吞下载）", wait(lambda: bdl("e.zip"), 8), browser_dls)
            badge = sw.evaluate("chrome.action.getBadgeText({})")
            check("G 扩展图标显示 \"!\"", badge == "!", badge)
            ping = sw.evaluate("getRules(true).then(r => ({ mode: r.mode, error: r.error }))")
            check("G 提示文字：SniffDL 没有运行", ping["mode"] == "offline" and "没有运行" in ping["error"], ping)
            check("G 内核没收到 e.zip", task_by(cport, "e.zip") is None)
            sw.evaluate("(port) => chrome.storage.local.set({ port })", cport)

            # H
            errs = sw.evaluate("self.__errs || []")
            check("H service worker 无报错", not errs, errs)
            ctx.close()
    finally:
        try:
            rpc(cport, "sniffdl.core.shutdown")
        except Exception:
            pass
        time.sleep(0.5)
        if core.poll() is None:
            core.terminate()
        srv.shutdown()
    n_ok = sum(1 for r in RESULTS if r[1])
    print("\next_e2e: %d/%d PASS" % (n_ok, len(RESULTS)))
    sys.exit(0 if n_ok == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
