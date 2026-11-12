# -*- coding: utf-8 -*-
"""tray_close 宿主测试：开(默认)→关窗缩托盘、Electron 仍在；关→关窗即退出（含内核）。"""
import json, os, shutil, sys, signal, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import host_electron_test as H
from playwright.sync_api import sync_playwright


def run(tray_close):
    shutil.rmtree(H.T, ignore_errors=True)
    for d in ("data/out", "home", "pet"):
        os.makedirs(os.path.join(H.T, d), exist_ok=True)
    json.dump({"outdir": os.path.join(H.T, "data", "out"), "clip_watch": False, "pet_live": False, "tray_close": tray_close},
              open(os.path.join(H.T, "data", "config.json"), "w"))
    el = H.launch()
    tag = "tray_close=%s " % tray_close
    try:
        info = H.wait(lambda: (lambda r: r if r and H.rpc(r, "sniffdl.ping").get("result") else None)(H.read_rt()), 60)
        H.ok(tag + "内核握手", info)
        if not info:
            return
        H.rpc(info, "sniffdl.settings.set", {"tray_close": tray_close})
        got = H.rpc(info, "sniffdl.settings.get").get("result") or {}
        v = got.get("values") or got
        H.ok(tag + "设置已生效", v.get("tray_close") == tray_close, v.get("tray_close"))
        with sync_playwright() as p:
            br = H.wait(lambda: p.chromium.connect_over_cdp("http://127.0.0.1:%d" % H.CDP), 30, 0.5)
            ui = H.wait(lambda: next((pg for c in br.contexts for pg in c.pages if "ui/index.html" in pg.url), None), 30)
            ui.wait_for_function("window.__sdlReady === true", timeout=30000)
            try:
                ui.evaluate("window.close()")
            except Exception:
                pass
            time.sleep(3)
            if tray_close:
                H.ok(tag + "关窗后 Electron 仍在（缩托盘）", el.poll() is None)
                H.ok(tag + "内核仍在", H.alive(info["pid"]))
                try:
                    ui.evaluate("window.sniffdlHost.quit()")
                except Exception:
                    pass
            else:
                rc = H.wait(lambda: el.poll() is not None, 20)
                H.ok(tag + "关窗即退出 Electron", rc, el.poll())
                H.ok(tag + "内核随之退出", H.wait(lambda: not H.alive(info["pid"]), 15))
            try:
                br.close()
            except Exception:
                pass
        H.wait(lambda: el.poll() is not None, 20)
    finally:
        try:
            os.killpg(el.pid, signal.SIGKILL)
        except Exception:
            pass


run(True)
run(False)
bad = [r for r in H.RES if not r[1]]
print("%d/%d 通过" % (len(H.RES) - len(bad), len(H.RES)))
sys.exit(1 if bad else 0)
