# -*- coding: utf-8 -*-
"""A16 浏览器扩展接管覆盖率（对标 IDM）：Playwright Chromium（有头，Xvfb :77）加载 extension/ + 真内核（ui_smoke_core.py）。

用法：source /workspace/work/env/setup.sh && source /workspace/work/env/v3env.sh && sh tests/host_xvfb.sh
      DISPLAY=:77 python3 tests/ext_takeover_test.py
本地起一个 HTTP 服务器，做 21 个场景页，逐个验证「该接管的进 SniffDL 且文件 MD5 一致、浏览器没自己存；
不该接管的浏览器自己下、内核没任务」，最后输出接管覆盖率 x/y。

场景：
  S01 直链 .zip                      S02 302 两跳（/go → /hop → .7z，点的链接没有后缀）
  S03 Content-Disposition 无后缀地址   S04 需要 Cookie（无 Cookie 403）
  S05 需要 Referer（无 Referer 403）   S06 JS 生成的游离 <a download> 程序化 click
  S07 window.location 跳下载           S08 iframe 下载
  S09 表单 GET 下载（/export.php + CD）S10 Alt 点击绕过 → 浏览器自己下
  S11 扩展站点黑名单 → 不接管          S12 内核离线 → 放行给浏览器（不吞）
  S13 中文文件名（filename*=UTF-8''）   S14 只靠 Content-Type（application/x-msdownload，无后缀无 CD）
  S15 IDM 清单里、内核默认没有的 .deb  S16 右键「用 SniffDL 下载」链接
  S17 右键「下载全部链接」→ 待确认 3 条 S18 普通网页链接不误伤
  S19 设置里删掉 deb → .deb 不再接管   S20 页面内 <a download="x.epub" href=无后缀>
  S21 302 两跳 + 终点需要 Cookie（走 onDeterminingFilename 兜底，Cookie 从真实请求头带过去）
"""
import hashlib
import http.server
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXT = os.path.join(HERE, "extension")
DATA = "/tmp/sdlext_a16"
TOKEN = "ext-a16-token"
CHECKS = []     # 附加检查（不计入覆盖率）
SCEN = []       # (id, 说明, ok, detail)
FILES = {}
CN_NAME = "测试文档_s13.pdf"
FINAL = {"s02": "/f/s02_redirect.7z", "s12": "/f/s12_offline.zip", "s21": "/need/s21_chain.zip"}
SEEN = []


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok), detail))
    print(("  PASS " if ok else "  FAIL ") + name + ("" if ok else "  ← " + str(detail)[:2000]), flush=True)
    return ok


def scen(sid, desc, ok, detail=""):
    SCEN.append((sid, desc, bool(ok), detail))
    print(("  PASS " if ok else "  FAIL ") + sid + " " + desc + ("" if ok else "  ← " + str(detail)[:1500]), flush=True)


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def wait(fn, timeout=10, step=0.2):
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


def mk(key, size=150_000):
    r = random.Random(key)
    FILES[key] = bytes(r.getrandbits(8) for _ in range(size + len(key)))
    return FILES[key]


def page(title, body):
    return ("<!doctype html><meta charset=utf-8><title>%s</title><body><h3>%s</h3>%s</body>" % (title, title, body)).encode()


PAGES = {
    "s01": '<a id=go href="/f/s01_direct.zip">直链 zip</a>',
    "s02": '<a id=go href="/go/s02">两跳下载（无后缀）</a>',
    "s03": '<a id=go href="/dl?id=s03">附件（无后缀）</a>',
    "s04": '<a id=go href="/need/s04_cookie.zip">要 Cookie</a>',
    "s05": '<a id=go href="/needref/s05_referer.zip">要 Referer</a>',
    "s06": '<button id=go onclick="const a=document.createElement(\'a\');a.href=\'/gen/s06\';a.download=\'s06_jsgen.zip\';a.click();">JS 生成下载</button>',
    "s07": '<button id=go onclick="location.href=\'/f/s07_location.rar\'">location 跳下载</button>',
    "s08": '<button id=go onclick="const f=document.createElement(\'iframe\');f.style.display=\'none\';f.src=\'/f/s08_iframe.iso\';document.body.appendChild(f);">iframe 下载</button>',
    "s09": '<form action="/export.php" method="get"><input name=id value=s09><input name=fmt value=xlsx><button id=go>导出</button></form>',
    "s10": '<a id=go href="/f/s10_alt.zip">Alt 点我</a>',
    "s11": '<a id=go href="/f/s11_black.zip">黑名单站点</a>',
    "s12": '<a id=go href="/go/s12">内核离线</a>',
    "s13": '<a id=go href="/dl?id=s13">中文名附件</a>',
    "s14": '<a id=go href="/bin/s14">只有 Content-Type</a>',
    "s15": '<a id=go href="/f/s15_pkg.deb">deb 包</a>',
    "s16": '<a id=go href="/gen/s16">右键我</a>',
    "s17": '<a href="/f/s17_a.zip">a</a> <a href="/f/s17_b.zip">b</a> <a href="/f/s17_c.zip">c</a> <a href="/f/s17_a.zip#x">重复</a>',
    "s18": '<a id=go href="/s/plain.html">普通网页</a>',
    "s19": '<a id=go href="/f/s19_pkg2.deb">deb 包（类型已删）</a>',
    "s20": '<a id=go href="/gen/s20" download="s20_attr.epub">download 属性</a>',
    "s21": '<a id=go href="/go/s21">两跳 + Cookie</a>',
}


class Srv(http.server.ThreadingHTTPServer):
    def handle_error(self, request, client_address):     # 浏览器被取消的下载会 reset 连接，不刷屏
        pass


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", ct="text/html; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ct)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except Exception:
                pass

    def _file(self, data, ct, cd=None):
        a, b = 0, len(data)
        rg = self.headers.get("Range")
        hdr = {"Accept-Ranges": "bytes"}
        if cd:
            hdr["Content-Disposition"] = cd
        code = 200
        if rg and rg.startswith("bytes="):
            s, _, e = rg[6:].partition("-")
            a = int(s or 0); b = int(e) + 1 if e else len(data)
            code = 206
            hdr["Content-Range"] = "bytes %d-%d/%d" % (a, b - 1, len(data))
        self._send(code, data[a:b], ct, hdr)

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        path, q = u.path, urllib.parse.parse_qs(u.query)
        SEEN.append((path, self.headers.get("Cookie") or "", self.headers.get("Referer") or "", self.headers.get("User-Agent") or ""))
        if path.startswith("/s/"):
            sid = path[3:].split(".")[0]
            if sid == "plain":
                return self._send(200, page("plain", "<p>只是网页</p>"))
            extra = {"Set-Cookie": "sdltok=abc123; Path=/"} if sid in ("s04", "s21") else None
            return self._send(200, page(sid, PAGES.get(sid, "")), extra=extra)
        if path.startswith("/go/"):
            return self._send(302, b"", extra={"Location": "/hop/" + path[4:]})
        if path.startswith("/hop/"):
            return self._send(302, b"", extra={"Location": FINAL[path[5:]]})
        if path.startswith("/f/"):
            name = path[3:]
            ct = "application/zip" if name.endswith(".zip") else "application/octet-stream"
            return self._file(FILES[name], ct)
        if path.startswith("/need/"):
            if "sdltok=abc123" not in (self.headers.get("Cookie") or ""):
                return self._send(403, b"need cookie", "text/plain")
            return self._file(FILES[path[6:]], "application/zip")
        if path.startswith("/needref/"):
            if "/s/s05" not in (self.headers.get("Referer") or ""):
                return self._send(403, b"need referer", "text/plain")
            return self._file(FILES[path[9:]], "application/zip")
        if path == "/dl":
            i = (q.get("id") or [""])[0]
            if i == "s03":
                return self._file(FILES["s03"], "application/octet-stream", 'attachment; filename="s03_cdfile.dat"')
            if i == "s13":
                return self._file(FILES["s13"], "application/pdf",
                                  "attachment; filename=\"s13.pdf\"; filename*=UTF-8''" + urllib.parse.quote(CN_NAME))
        if path == "/export.php":
            return self._file(FILES["s09"], "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                              'attachment; filename="s09_form.xlsx"')
        if path.startswith("/gen/"):
            return self._file(FILES[path[5:]], "application/zip")
        if path.startswith("/bin/"):
            return self._file(FILES[path[5:]], "application/x-msdownload")
        return self._send(404, b"nf", "text/plain")

    do_HEAD = do_GET


def rpc(port, method, *params):
    req = urllib.request.Request("http://127.0.0.1:%d/jsonrpc" % port,
                                 data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": list(params)}).encode(),
                                 headers={"Content-Type": "application/json", "X-SniffDL-Token": TOKEN})
    with urllib.request.urlopen(req, timeout=10) as r:
        j = json.loads(r.read())
    if j.get("error"):
        raise RuntimeError(j["error"])
    return j.get("result")


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
    for name in ["s01_direct.zip", "s02_redirect.7z", "s04_cookie.zip", "s05_referer.zip", "s07_location.rar", "s08_iframe.iso",
                 "s10_alt.zip", "s11_black.zip", "s12_offline.zip", "s15_pkg.deb", "s17_a.zip", "s17_b.zip", "s17_c.zip",
                 "s19_pkg2.deb", "s21_chain.zip"]:
        mk(name)
    for k in ["s03", "s06", "s09", "s13", "s14", "s16", "s20"]:
        mk(k)
    TOK2KEY = {k.split("_")[0]: k for k in FILES}          # s01 -> s01_direct.zip / s03 -> s03

    fport = free_port()
    srv = Srv(("127.0.0.1", fport), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % fport
    cport = free_port()
    core = subprocess.Popen([sys.executable, os.path.join(HERE, "tests", "ui_smoke_core.py"), "--data-dir", DATA, "--port", str(cport),
                             "--runtime", os.path.join(DATA, "runtime.json"), "--ui-token", TOKEN],
                            cwd=HERE, stdout=open(os.path.join(DATA, "core.out"), "w"), stderr=subprocess.STDOUT)

    def tasks():
        return rpc(cport, "sniffdl.tasks") or []

    def ktask(tok):
        for t in tasks():
            if tok in (t.get("name") or "") or tok in (t.get("uri") or ""):
                return t
        return None

    def kdone(tok, key=None):
        t = ktask(tok)
        if not t or t.get("status") != "complete":
            return None
        p = t.get("path") or ""
        if not os.path.isfile(p):
            return None
        return t if hashlib.md5(open(p, "rb").read()).hexdigest() == hashlib.md5(FILES[key or TOK2KEY[tok]]).hexdigest() else None

    try:
        if not wait(lambda: rpc(cport, "sniffdl.ping"), 25):
            raise SystemExit("内核没起来，见 %s/core.out" % DATA)
        rpc(cport, "sniffdl.settings.set", {"outdir": os.path.join(DATA, "out"), "clip_watch": False, "notify": False,
                                            "rpc_confirm_dialog": False, "takeover_blacklist": "", "takeover_whitelist": "",
                                            "takeover_enable": True, "takeover_alt_bypass": True, "takeover_min_kb": 0})
        kr = rpc(cport, "sniffdl.takeoverRules")
        check("内核默认接管类型里没有 deb/7z/dat（用来验证扩展 IDM 清单补上）", "deb" not in kr["exts"] and "dat" not in kr["exts"], kr["exts"])

        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                os.path.join(DATA, "profile"), headless=False, accept_downloads=True, downloads_path=os.path.join(DATA, "browser_dl"),
                args=["--no-sandbox", "--disable-extensions-except=" + EXT, "--load-extension=" + EXT, "--no-first-run"])
            sw = wait(lambda: ctx.service_workers and ctx.service_workers[0], 15)
            check("service worker 启动", bool(sw))
            sw.evaluate(SW_HOOK)
            sw.evaluate("(port) => chrome.storage.local.set({ port })", cport)
            check("扩展识别到 v3 内核", sw.evaluate("getRules(true).then(r => r.mode)") == "v3")
            n_idm = sw.evaluate("self.__sdlIdmExts.length")
            check("默认接管类型清单 ≥100 种（对标 IDM）：%d" % n_idm, n_idm >= 100, n_idm)
            eff = sw.evaluate("getRules(true).then(r => r.rules.exts)")
            check("生效规则 = 内核 ∪ 扩展清单（含 deb/7z/iso/epub）", all(x in eff for x in ["deb", "7z", "iso", "epub", "zip"]), len(eff))
            check("CD 解析：filename*=UTF-8'' 中文", sw.evaluate("self.__sdlCdFilename(%s)" % json.dumps(
                "attachment; filename=\"a.pdf\"; filename*=UTF-8''" + urllib.parse.quote(CN_NAME))) == CN_NAME)

            # popup：高级折叠区（中文）
            ext_id = sw.url.split("/")[2]
            pp = ctx.new_page()
            pp.goto("chrome-extension://%s/popup.html" % ext_id)
            pp.wait_for_function("document.getElementById('takeoverExts') && document.getElementById('takeoverExts').value.length > 100", timeout=8000)
            st = pp.evaluate("""() => ({ open: document.getElementById('advTakeover').open,
                 summary: document.querySelector('#advTakeover summary').textContent,
                 n: new Set(document.getElementById('takeoverExts').value.split(/\\s+/).filter(Boolean)).size,
                 cnt: document.getElementById('extCount').textContent, hj: document.getElementById('headerJudge').checked })""")
            check("弹窗「高级」默认折叠、中文、清单 ≥100 种、按响应头判定默认开", (not st["open"]) and "高级" in st["summary"] and st["n"] >= 100 and st["hj"], st)
            pp.close()

            pg = ctx.pages[0] if ctx.pages else ctx.new_page()
            js_dl = "chrome.downloads.search({}).then(a => a.map(d => ({u: (d.finalUrl||'') + ' ' + (d.url||''), s: d.state, f: d.filename})))"

            def bdone(tok):    # 浏览器自己把它下完了
                return any(tok in x["u"] and x["s"] == "complete" for x in sw.evaluate(js_dl))

            def go(sid):
                pg.goto(base + "/s/%s.html" % sid)
                time.sleep(0.9)       # 内容脚本取规则

            def expect_take(sid, desc, tok=None, name=None, act=None, key=None):
                tok = tok or sid
                go(sid)
                (act or (lambda: pg.click("#go")))()
                t = wait(lambda: kdone(tok, key), 20)
                det = {"task": ktask(tok), "decide": sw.evaluate("self.__sdlLastDecision.slice(-2)")}
                ok = bool(t) and not bdone(tok)
                if ok and name:
                    ok = (t.get("name") == name or os.path.basename(t.get("path") or "") == name)
                scen(sid, desc, ok, det)

            def expect_pass(sid, desc, tok=None, act=None, browser=True):
                tok = tok or sid
                go(sid)
                (act or (lambda: pg.click("#go")))()
                bok = wait(lambda: bdone(tok), 10) if browser else True
                time.sleep(1.0)
                scen(sid, desc, bool(bok) and ktask(tok) is None,
                     {"browser": bok, "task": ktask(tok), "decide": sw.evaluate("self.__sdlLastDecision.slice(-2)")})

            expect_take("s01", "直链 .zip 接管")
            expect_take("s02", "302 两跳（点的链接无后缀）接管")
            expect_take("s03", "Content-Disposition: attachment 无后缀地址接管（.dat 不在清单，按响应头）", name="s03_cdfile.dat")
            expect_take("s04", "需要 Cookie 的链接接管（内核带 Cookie 下完）")
            expect_take("s05", "需要 Referer 的链接接管（内核带 Referer 下完）")
            expect_take("s06", "JS 生成的游离 <a download> 程序化点击接管", name="s06_jsgen.zip")
            expect_take("s07", "window.location 跳下载接管")
            expect_take("s08", "iframe 下载接管")
            expect_take("s09", "表单 GET 下载（/export.php + 附件头）接管", name="s09_form.xlsx")
            expect_pass("s10", "按住 Alt 点击 → 不接管，浏览器自己下", act=lambda: pg.click("#go", modifiers=["Alt"]))

            sw.evaluate("chrome.storage.local.set({ siteBlacklist: '127.0.0.1' })")
            expect_pass("s11", "扩展设置「从不接管」站点 → 不接管，浏览器自己下")
            sw.evaluate("chrome.storage.local.set({ siteBlacklist: '' })")

            dead = free_port()
            sw.evaluate("(port) => chrome.storage.local.set({ port })", dead)
            expect_pass("s12", "内核离线 → 放行给浏览器（302 无后缀链接走兜底路径也不吞）")
            sw.evaluate("(port) => chrome.storage.local.set({ port })", cport)
            sw.evaluate("getRules(true)")

            expect_take("s13", "中文文件名（filename*=UTF-8''）接管且名字正确", tok="s13", name=CN_NAME)
            expect_take("s14", "只靠 Content-Type: application/x-msdownload（无后缀无 CD）接管")
            expect_take("s15", "IDM 清单里的 .deb（内核默认清单没有）接管")

            # S16 右键「用 SniffDL 下载」（原生菜单 Playwright 点不到 → SW 测试入口）
            go("s16")
            r16 = sw.evaluate("""async (u) => { const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
                return await self.__sdlMenu({ menuItemId: 'sniffdl', linkUrl: u, pageUrl: tab.url }, tab); }""", base + "/gen/s16")
            t16 = wait(lambda: kdone("s16"), 20)
            scen("s16", "右键「用 SniffDL 下载」链接 → 内核下完", bool(t16), {"r": r16, "task": ktask("s16")})

            go("s17")
            r17 = sw.evaluate("""async () => { const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
                return await self.__sdlMenu({ menuItemId: 'sniffdl-all', pageUrl: tab.url }, tab); }""")
            g17 = wait(lambda: len([g for g in rpc(cport, "sniffdl.grabber.list") if "/s17_" in g["url"]]) == 3, 6)
            scen("s17", "右键「下载全部链接」→ 待确认区 3 条（去重）", bool(g17) and (r17 or {}).get("added") == 3, r17)

            go("s18")
            pg.click("#go")
            nav_ok = wait(lambda: pg.url.endswith("/s/plain.html"), 6)
            time.sleep(0.8)
            scen("s18", "普通网页链接不误伤（照常打开、无任务）", bool(nav_ok) and ktask("plain") is None, pg.url)

            cur = sw.evaluate("chrome.storage.local.get('takeoverExts').then(o => o.takeoverExts)") or " ".join(sw.evaluate("self.__sdlIdmExts"))
            sw.evaluate("(v) => chrome.storage.local.set({ takeoverExts: v })", " ".join(x for x in cur.split() if x != "deb"))
            expect_pass("s19", "设置里删掉 deb → .deb 不再接管（浏览器自己下）")
            sw.evaluate("(v) => chrome.storage.local.set({ takeoverExts: v })", cur)

            expect_take("s20", "页面内 <a download=\"x.epub\" href=无后缀> 接管", name="s20_attr.epub")
            expect_take("s21", "302 两跳 + 终点要 Cookie（兜底路径带真实 Cookie）接管")

            c21 = [s for s in SEEN if s[0] == "/need/s21_chain.zip" and "sdltok=abc123" in s[1]]
            check("S21 服务器至少收到 2 次带 Cookie 的终点请求（浏览器 1 + 内核 ≥1）", len(c21) >= 2, c21)
            time.sleep(1.0)
            bd = os.path.join(DATA, "browser_dl")
            sizes = set(len(v) for v in FILES.values())
            # Playwright 给每个下载都建 GUID 文件；被取消的是空/残片，只数「完整大小」的
            bfiles = sorted(f for f in os.listdir(bd) if os.path.getsize(os.path.join(bd, f)) in sizes)
            bitems = [x for x in sw.evaluate(js_dl) if x["s"] == "complete"]
            check("浏览器自己存下的文件 = 4 个（只有 S10/S11/S12/S19 放行场景）", len(bfiles) == 4 and len(bitems) == 4,
                  {"n_dir": len(bfiles), "all": [(f, os.path.getsize(os.path.join(bd, f))) for f in os.listdir(bd)], "items": [x["u"] for x in bitems]})
            errs = sw.evaluate("self.__errs || []")
            check("service worker 无报错", not errs, errs)
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

    take_ids = {"s01", "s02", "s03", "s04", "s05", "s06", "s07", "s08", "s09", "s13", "s14", "s15", "s16", "s17", "s20", "s21"}
    tk = [s for s in SCEN if s[0] in take_ids]
    ps = [s for s in SCEN if s[0] not in take_ids]
    n_ok = sum(1 for s in SCEN if s[2])
    print("\n接管场景：%d/%d 接管成功；放行场景：%d/%d 正确放行" % (sum(1 for s in tk if s[2]), len(tk), sum(1 for s in ps if s[2]), len(ps)))
    print("ext_takeover 场景覆盖率：%d/%d" % (n_ok, len(SCEN)))
    c_ok = sum(1 for c in CHECKS if c[1])
    print("附加检查：%d/%d PASS" % (c_ok, len(CHECKS)))
    with open(os.path.join(DATA, "result.json"), "w", encoding="utf-8") as f:
        json.dump({"scenarios": [s[:3] for s in SCEN], "checks": [c[:2] for c in CHECKS]}, f, ensure_ascii=False, indent=1)
    sys.exit(0 if n_ok == len(SCEN) and len(SCEN) >= 15 and c_ok == len(CHECKS) else 1)


if __name__ == "__main__":
    main()
