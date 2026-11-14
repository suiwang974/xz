# -*- coding: utf-8 -*-
"""WP4 桌宠宿主联动：下载事件 → 大肥鱼反应、气泡、点击交互（真 Electron + 真内核）。
    source /workspace/work/env/v3env.sh && sh tests/host_xvfb.sh && DISPLAY=:77 python3 tests/host_pet_link_test.py
"""
import json, os, shutil, signal, sys, threading, time, http.server
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import host_electron_test as H
from host_electron_test import ok, rpc, wait, T, SlowFile


def main():
    shutil.rmtree(T, ignore_errors=True)
    for d in ("data/out", "home", "pet"):
        os.makedirs(os.path.join(T, d), exist_ok=True)
    os.makedirs(H.SHOTS, exist_ok=True)
    json.dump({"outdir": os.path.join(T, "data", "out"), "clip_watch": False, "pet_live": True},
              open(os.path.join(T, "data", "config.json"), "w"))
    fport = H.free_port()
    SlowFile.size = 6 * 1024 * 1024
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", fport), SlowFile)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    el = H.launch()
    try:
        info = wait(lambda: (lambda r: r if r and rpc(r, "sniffdl.ping").get("result") else None)(H.read_rt()), 60)
        ok("内核握手", info)
        if not info:
            return 2
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            br = wait(lambda: p.chromium.connect_over_cdp("http://127.0.0.1:%d" % H.CDP), 30, 0.5)

            def find(sub):
                for c in br.contexts:
                    for pg in c.pages:
                        if sub in pg.url:
                            return pg
            ui = wait(lambda: find("ui/index.html"), 30)
            pet = wait(lambda: find("desk.html"), 30)
            ok("主窗与桌宠窗都在", ui and pet)
            ui.wait_for_function("window.__sdlReady === true", timeout=30000)
            ok("桌宠模型就绪", wait(lambda: pet.evaluate("!!window.__petModelReady"), 40, 0.5))
            ps = lambda: pet.evaluate("SniffDLPet.state()")
            # 内核 → 桌宠 控制通道
            ok("react busy → 桌宠 state=busy", (rpc(info, "sniffdl.pet", "react", "busy").get("result") or {}).get("ok") and wait(lambda: ps() == "busy", 5), ps())
            rpc(info, "sniffdl.pet", "react", "done")
            ok("react done → state 回 idle", wait(lambda: ps() == "idle", 5), ps())
            rpc(info, "sniffdl.pet", "react", "fail")
            ok("react fail → 不报错且 state=idle", wait(lambda: ps() == "idle", 5) and not pet.evaluate("window.__err||''"))
            rpc(info, "sniffdl.pet", "say", "联动测试气泡")
            ok("say → 桌宠气泡显示文字", wait(lambda: pet.evaluate("(document.getElementById('bubble')||{}).textContent||''").find("联动测试气泡") >= 0, 5))
            rpc(info, "sniffdl.pet", "visible", False); time.sleep(1); rpc(info, "sniffdl.pet", "visible", True); time.sleep(1)
            ok("visible 开关后桌宠仍可用", pet.evaluate("SniffDLPet.isReady()"))
            # 真实下载：开始 → busy，完成 → done 表演
            ui.click("#addInput"); ui.keyboard.insert_text("http://127.0.0.1:%d/files/link_sample.bin" % fport)
            ui.keyboard.press("Control+Enter")
            ui.wait_for_selector(".task", timeout=15000)
            ok("下载开始 → 桌宠 busy", wait(lambda: ps() == "busy", 15), ps())
            ok("下载完成 → 桌宠回 idle", wait(lambda: ps() == "idle", 40), ps())
            ok("下载文件落盘", wait(lambda: any(f.startswith("link_sample") for _r, _d, fs in os.walk(os.path.join(T, "data", "out")) for f in fs) and sum(os.path.getsize(os.path.join(r, f)) for r, _d, fs in os.walk(os.path.join(T, "data", "out")) for f in fs if f.startswith("link_sample")) == SlowFile.size, 20))
            # 失败：不存在端口
            ui.click("#addInput"); ui.keyboard.insert_text("http://127.0.0.1:1/nope.bin")
            ui.keyboard.press("Control+Enter")
            ok("失败任务后桌宠不卡 busy", wait(lambda: ps() == "idle", 60), ps())
            # 点击交互：点身体 → 模式/表情变化或出菜单且无报错
            pet.screenshot(path=os.path.join(H.SHOTS, "pet_link.png"))
            info0 = json.loads(pet.evaluate("JSON.stringify(SniffDLPet.info())"))
            hit = pet.evaluate("SniffDLPet.hitAt(%d,%d)" % (info0["x"], info0["y"] - 120))
            ok("身体区域命中判定为真", hit, info0)
            pet.mouse.click(info0["x"], info0["y"] - 120)
            time.sleep(1)
            ok("点击后无脚本错误", not pet.evaluate("window.__err||''"))
            ok("Q版切换 setChibi 生效", pet.evaluate("SniffDLPet.setChibi(true)") and wait(lambda: json.loads(pet.evaluate("JSON.stringify(SniffDLPet.info())"))["chibi"] >= .5, 8))
            try:
                ui.evaluate("window.sniffdlHost.quit()")
            except Exception:
                pass
            try: br.close()
            except Exception: pass
        try: el.wait(20)
        except Exception: pass
    finally:
        try: os.killpg(el.pid, signal.SIGKILL)
        except Exception: pass
        srv.shutdown()
    bad = [r for r in H.RES if not r[1]]
    print("\n%d/%d 通过" % (len(H.RES) - len(bad), len(H.RES)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
