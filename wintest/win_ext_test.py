# -*- coding: utf-8 -*-
"""真 Edge / 真 Chrome（+ Playwright Chromium 兜底）加载 v3/extension，检查 service worker 起来、无运行时/清单报错、
content script 注入。错误读取：chrome://extensions 页里的 chrome.developerPrivate.getExtensionsInfo()。"""
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import OUT, V3, Results, make_file_server, free_port  # noqa: E402
import http.server  # noqa: E402
import threading  # noqa: E402

R = Results("ext")
EXT = os.path.join(V3, "extension")
with open(os.path.join(EXT, "manifest.json"), encoding="utf-8") as _f:
    EXT_VER = json.load(_f)["version"]


def _shot(page, path):
    try:
        page.screenshot(path=path, timeout=10000)
    except Exception as e:
        print("screenshot skipped", path, repr(e)[:120])


def make_html_server(dl_port):
    """真 HTML 测试页（带一个指向下载服务器的链接），给 content script 注入用。"""
    html = ("<!doctype html><meta charset=utf-8><title>SniffDL 扩展测试</title>"
            "<h1>测试页</h1><a id=dl href='http://127.0.0.1:%d/x.bin'>下载 x.bin</a>"
            "<video id=v src='http://127.0.0.1:%d/v.mp4' width=320 height=180></video>" % (dl_port, dl_port)).encode("utf-8")

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

    port = free_port()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port
SHOTS = os.path.join(OUT, "shots")
os.makedirs(SHOTS, exist_ok=True)


def one(p, label, channel):
    ud = tempfile.mkdtemp(prefix="ext_%s_" % label)
    args = ["--disable-extensions-except=" + EXT, "--load-extension=" + EXT,
            "--disable-features=DisableLoadExtensionCommandLineSwitch,ExtensionManifestV2Disabled",
            "--no-first-run", "--no-default-browser-check"]
    kw = dict(headless=False, args=args)
    if channel:
        kw["channel"] = channel
    try:
        ctx = p.chromium.launch_persistent_context(ud, **kw)
    except Exception as e:
        R.ok("%s 启动浏览器" % label, False, repr(e)[:400])
        return
    try:
        ver = ctx.browser.version if ctx.browser else ""
        sw = ctx.service_workers[0] if ctx.service_workers else None
        if not sw:
            try:
                sw = ctx.wait_for_event("serviceworker", timeout=15000)
            except Exception:
                sw = None
        if not sw and channel == "chrome":
            # Chrome 正式版 137 起移除了 --load-extension 命令行加载，只能走 Chrome for Testing / Playwright Chromium
            try:
                major = int((ver or "0").split(".")[0])
            except ValueError:
                major = 0
            if major >= 137:
                R.skip("%s 正式版 %s 禁用命令行加载扩展（Chrome 137+ 策略），由 pwchromium 覆盖" % (label, ver))
                return
        R.ok("%s 扩展 service worker 起来" % label, sw, sw and sw.url)
        if not sw:
            pg = ctx.new_page()
            pg.goto("chrome://extensions" if channel != "msedge" else "edge://extensions")
            time.sleep(2)
            _shot(pg, os.path.join(SHOTS, "ext_%s_extensions_page.png" % label))
            return
        ext_id = sw.url.split("/")[2]
        man = sw.evaluate("chrome.runtime.getManifest().version")
        R.ok("%s SW 内 chrome.runtime 可用（manifest %s）" % (label, man), man == EXT_VER, man)
        # 打开 extensions 页 → 开发者模式（才收集错误）→ 重载扩展 → 跑一个网页触发 content script
        scheme = "edge" if channel == "msedge" else "chrome"
        mg = ctx.new_page()
        mg.goto("%s://extensions/?id=%s" % (scheme, ext_id))
        time.sleep(1.5)
        try:
            mg.evaluate("chrome.developerPrivate.updateProfileConfiguration({inDeveloperMode:true})")
            # 不再 reload：命令行加载的扩展 reload 后会卡在 disableReasons.reloading=true（沙箱与 Windows CI 均复现）
        except Exception as e:
            print("devmode/reload err", e)
        time.sleep(3)
        srv, fport, _ = make_file_server(1024, delay=0, name="x.bin")
        hsrv, hport = make_html_server(fport)
        pg = ctx.new_page()
        cerr = []
        pg.on("pageerror", lambda e: cerr.append(str(e)))
        pg.goto("http://127.0.0.1:%d/page.html" % hport)
        time.sleep(3)
        R.ok("%s 测试页正常打开（非下载）" % label, pg.title() == "SniffDL 扩展测试", pg.title())
        _shot(pg, os.path.join(SHOTS, "ext_%s_testpage.png" % label))
        srv.shutdown()
        hsrv.shutdown()
        info = mg.evaluate("chrome.developerPrivate.getExtensionsInfo()")
        me = [x for x in info if x.get("id") == ext_id]
        me = me[0] if me else {}
        rt = [e.get("message") for e in me.get("runtimeErrors") or []]
        mf = [e.get("message") for e in me.get("manifestErrors") or []]
        R.ok("%s 扩展无 manifest 报错" % label, not mf, mf)
        R.ok("%s 扩展无 runtime 报错（SW/content）" % label, not rt, rt[:8])
        st = me.get("state")
        for _ in range(20):  # 开发者模式 reload 后状态会短暂变化，轮询到稳定
            if st == "ENABLED":
                break
            time.sleep(0.5)
            info = mg.evaluate("chrome.developerPrivate.getExtensionsInfo()")
            st = next((x.get("state") for x in info if x.get("id") == ext_id), None)
        R.ok("%s 扩展状态 ENABLED" % label, st == "ENABLED", "%s %s" % (st, json.dumps({k: me.get(k) for k in ("disableReasons", "blacklistText", "location", "controlledInfo")}, ensure_ascii=False)))
        mg.goto("%s://extensions/?errors=%s" % (scheme, ext_id))
        time.sleep(1.5)
        _shot(mg, os.path.join(SHOTS, "ext_%s_errors_page.png" % label))
        R.ok("%s 页面无 pageerror（content script）" % label, not cerr, cerr[:5])
        print(label, "browser version", ver)
    finally:
        try:
            ctx.close()
        except Exception:
            pass
        shutil.rmtree(ud, ignore_errors=True)


def main():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        for label, ch in (("edge", "msedge"), ("chrome", "chrome"), ("pwchromium", None)):
            try:
                one(p, label, ch)
            except Exception as e:
                R.ok("%s 异常" % label, False, repr(e)[:400])
    return R.save()


if __name__ == "__main__":
    sys.exit(main())
