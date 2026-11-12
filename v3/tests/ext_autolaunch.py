# -*- coding: utf-8 -*-
"""A3：内核没开时，扩展里的主动动作（嗅探条一键推送）经 Native Messaging 自动拉起内核再推送。
有头 Chromium + 真 native_host.py（注册到 profile/NativeMessagingHosts）。先 sh tests/host_xvfb.sh，DISPLAY=:77。
内核由 native host 按 SNIFFDL_LAUNCH_CMD 拉起（这里指向 tests/ui_smoke_core.py）。"""
import http.server, json, os, shutil, socket, subprocess, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ext_e2e as E
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import native_host as NH

PAGE = b"<!doctype html><meta charset=utf-8><title>al</title><video src='/clip.mp4' preload=auto muted></video>"


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/a.html":
            b, ct = PAGE, "text/html"
        elif p == "/clip.mp4":
            b, ct = b"\0" * 150000, "video/mp4"
        else:
            self.send_response(404); self.end_headers(); return
        self.send_response(200); self.send_header("Content-Type", ct); self.send_header("Content-Length", str(len(b))); self.end_headers()
        try: self.wfile.write(b)
        except Exception: pass


def main():
    shutil.rmtree(E.DATA, ignore_errors=True)
    os.makedirs(os.path.join(E.DATA, "out"), exist_ok=True)
    fport = E.free_port(); cport = E.free_port()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", fport), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % fport
    prof = os.path.join(E.DATA, "profile")
    os.makedirs(os.path.join(prof, "NativeMessagingHosts"), exist_ok=True)
    os.chmod(os.path.join(E.HERE, "native_host.py"), 0o755)
    launch_cmd = "%s %s --data-dir %s --port %d --runtime %s --ui-token %s" % (
        sys.executable, os.path.join(E.HERE, "tests", "ui_smoke_core.py"), E.DATA, cport, os.path.join(E.DATA, "runtime.json"), E.TOKEN)
    env = dict(os.environ, SNIFFDL_LAUNCH_CMD=launch_cmd, SNIFFDL_DRYRUN_OS="1")
    env.pop("HTTP_PROXY", None); env.pop("HTTPS_PROXY", None)
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(prof, headless=False, bypass_csp=True, env=env,
                args=["--no-sandbox", "--disable-extensions-except=" + E.EXT, "--load-extension=" + E.EXT, "--no-first-run"])
            sw = E.wait(lambda: ctx.service_workers and ctx.service_workers[0], 15)
            ext_id = sw.url.split("/")[2]
            NH.install([ext_id], [os.path.join(prof, "NativeMessagingHosts")])
            sw.evaluate("(port) => chrome.storage.local.set({ port, token: '' })", cport)
            E.check("起点：内核端口未开", not NH.port_open(cport))
            pg = ctx.pages[0] if ctx.pages else ctx.new_page()
            pg.goto(base + "/a.html"); time.sleep(2.5)
            tid = sw.evaluate("chrome.tabs.query({active:true,currentWindow:true}).then(t=>t[0].id)")
            pop = ctx.new_page(); pop.goto("chrome-extension://%s/popup.html" % ext_id); time.sleep(0.5)
            call = lambda msg: pop.evaluate("(m)=>new Promise(r=>chrome.runtime.sendMessage(m,r))", msg)
            items = E.wait(lambda: (lambda x: x if x and x["items"] else None)(call({"type": "sniff_list", "tabId": tid})), 8)
            E.check("内核没开也能嗅探到 mp4", bool(items), items)
            url = items["items"][0]["url"]
            # 1) 内核没开 → 推送触发自动拉起
            t0 = time.time()
            r = call({"type": "sniff_push", "tabId": tid, "url": url})
            E.check("内核没开时点推送：自动拉起后推送成功（用时 %.1fs）" % (time.time() - t0), r and r.get("ok"), r)
            E.check("内核端口已被 native host 拉起", NH.port_open(cport))
            pend = E.wait(lambda: [x for x in E.rpc(cport, "sniffdl.snapshot")["pending"] if "clip.mp4" in (x.get("url") or "")], 8)
            E.check("拉起后的内核待确认区收到该条且带页面 Referer", bool(pend) and "/a.html" in pend[0].get("referer", ""), pend)
            # 2) 关掉内核 + autoLaunch=false → 不拉起、报离线
            try: E.rpc(cport, "sniffdl.core.shutdown")
            except Exception: pass
            E.check("手动关掉内核", E.wait(lambda: not NH.port_open(cport), 15))
            sw.evaluate("() => chrome.storage.local.set({ autoLaunch: false })")
            r = call({"type": "sniff_push", "tabId": tid, "url": url})
            E.check("autoLaunch=关：返回离线、不推送", r and not r.get("ok") and r.get("offline"), r)
            time.sleep(1.5)
            E.check("autoLaunch=关：内核没被拉起", not NH.port_open(cport))
            # 3) 后台探活不触发拉起：popup 打开状态检查（ping）后端口仍关；再开 autoLaunch 也一样
            sw.evaluate("() => chrome.storage.local.set({ autoLaunch: true })")
            call({"type": "ping"}); time.sleep(1.5)
            E.check("探活/读规则这类后台调用不拉起内核", not NH.port_open(cport))
            ctx.close()
    finally:
        try: E.rpc(cport, "sniffdl.core.shutdown")
        except Exception: pass
        subprocess.run(["pkill", "-f", "ui_smoke_core.py --data-dir " + E.DATA])
    bad = [r for r in E.RESULTS if not r[1]]
    print("%d/%d 通过" % (len(E.RESULTS) - len(bad), len(E.RESULTS)))
    sys.exit(1 if bad else 0)


main()
