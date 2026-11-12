# -*- coding: utf-8 -*-
"""删除弹窗 × cancel_cleanup（真内核 + 真界面 + 慢速文件服务器）。
用法：source /workspace/work/env/setup.sh && source /workspace/work/env/v3env.sh && python3 tests/ui_remove.py
覆盖：ask 时弹窗出现「清理半截文件」勾选；不勾 → 半截文件保留；勾 → 清掉；always/never 时不出勾选、只出说明文字。
"""
import os, sys, time, shutil, subprocess, threading, http.server
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ui_smoke as U

DATA = "/tmp/sdlv3_rm"; TOKEN = U.TOKEN; R = []
def check(n, ok, d=""):
    R.append(ok); print(("  PASS " if ok else "  FAIL ") + n + ("" if ok else "  <- " + str(d)[:300]), flush=True)

def parts(out, name):
    return sorted(f for f in os.listdir(out) if f.startswith(name) and f != name)

def main():
    shutil.rmtree(DATA, ignore_errors=True); out = os.path.join(DATA, "out"); os.makedirs(out)
    fport = U.free_port(); srv = http.server.ThreadingHTTPServer(("127.0.0.1", fport), U.SlowFile)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cport = U.free_port()
    core = subprocess.Popen([sys.executable, os.path.join(U.HERE, "tests", "ui_smoke_core.py"), "--data-dir", DATA, "--port", str(cport),
                             "--runtime", os.path.join(DATA, "runtime.json"), "--ui-token", TOKEN],
                            cwd=U.HERE, stdout=open(os.path.join(DATA, "core.out"), "w"), stderr=subprocess.STDOUT)
    rpc = lambda m, *a: U.rpc(cport, m, *a).get("result")
    try:
        for _ in range(80):
            try:
                if rpc("sniffdl.ping"): break
            except Exception: time.sleep(0.25)
        rpc("sniffdl.settings.set", {"outdir": out, "clip_watch": False, "notify": False, "speed_limit_kbps": 512})
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            br = p.chromium.launch(args=["--no-sandbox"])
            pg = br.new_page(viewport={"width": 1280, "height": 820}, bypass_csp=True)
            errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto("http://127.0.0.1:%d/ui/index.html#port=%d&token=%s" % (cport, cport, TOKEN))
            pg.wait_for_function("window.__sdlReady === true", timeout=20000)

            def case(pol, tick, shot=None):
                rpc("sniffdl.settings.set", {"cancel_cleanup": pol})
                name = "rm_%s_%s.bin" % (pol, tick)
                gid = rpc("sniffdl.task.add", {"url": "http://127.0.0.1:%d/f/%s" % (fport, name), "name": name, "dir": out})
                gid = gid.get("gid") if isinstance(gid, dict) else gid
                t0 = time.time()
                while time.time() - t0 < 15 and not parts(out, name): time.sleep(0.2)
                time.sleep(1.0)
                rpc("sniffdl.task.pause", [gid]); time.sleep(1.5)
                before = parts(out, name)
                pg.wait_for_function("(g)=>import('/ui/js/store.js').then(m=>m.S.tasks.has(g) && m.S.settings && m.S.settings.cancel_cleanup===%r)" % pol, arg=gid, timeout=8000)
                pg.evaluate("(g)=>{import('/ui/js/views/tasks.js').then(m=>m.removeSel([g]))}", gid)
                pg.wait_for_selector(".modal", timeout=5000)
                has_cb = pg.locator("#rmPart").count() > 0
                txt = pg.locator(".modal").inner_text()
                if shot: pg.screenshot(path=os.path.join(U.SHOTS, shot))
                if pol == "ask":
                    check("ask：弹窗有「清理半截文件」勾选（默认勾上）", has_cb and pg.locator("#rmPart").is_checked(), txt)
                    if not tick: pg.locator("#rmPart").uncheck()
                else:
                    check("%s：弹窗不出勾选、有说明文字" % pol, (not has_cb) and ("保留" if pol == "never" else "自动清理") in txt, txt)
                pg.locator(".modal button.danger").click()
                time.sleep(4.5)
                after = parts(out, name)
                keep = (pol == "never") or (pol == "ask" and not tick)
                check("%s 勾=%s：删前有半截 %s，删后 %s（期望%s）" % (pol, tick, before, after, "保留" if keep else "清掉"),
                      bool(before) and (bool(after) if keep else not after), (before, after))

            case("ask", False, "ui_remove_ask.png")
            case("ask", True)
            case("always", True)
            case("never", True)
            check("界面无 JS 报错", not errs, errs)
            br.close()
    finally:
        try: rpc("sniffdl.core.shutdown")
        except Exception: pass
        time.sleep(0.5); core.kill()
    print("RESULT %d/%d" % (sum(R), len(R))); sys.exit(0 if all(R) else 1)

if __name__ == "__main__":
    main()
