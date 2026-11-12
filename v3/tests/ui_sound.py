# -*- coding: utf-8 -*-
"""完成提示音（设置 sound）：真内核 + 真下载完成，页面里 AudioContext 换成计数桩，断言响几声。

  · sound=开 + 任务完成 → 两声（两个振荡器 start）
  · sound=关 → 完成不响
  · 下载出错 / 开始添加 → 不响
  · 没有 AudioContext（老环境）→ 不抛 JS 错误
用法：source env/setup.sh && source env/v3env.sh && python3 tests/ui_sound.py     退出码 0 = 全过
"""
import http.server
import os
import shutil
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ui_pages as P  # noqa: E402
from ui_pages import check, wait, rpc  # noqa: E402

STUB = """
window.__chimes = 0; window.__osc = 0;
class FakeAC { constructor(){ this.state='running'; this.currentTime=0; this.destination={}; }
  resume(){} createGain(){ return {gain:{setValueAtTime(){},exponentialRampToValueAtTime(){}}, connect(){}}; }
  createOscillator(){ window.__osc++; return {frequency:{}, connect(){}, start(){ window.__chimes++; }, stop(){}}; } }
window.AudioContext = FakeAC;
"""


def main():
    P.DATA = "/tmp/sdlv3_sound"
    shutil.rmtree(P.DATA, ignore_errors=True)
    os.makedirs(os.path.join(P.DATA, "out"), exist_ok=True)
    fport = P.free_port()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", fport), P.Files)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cport = P.free_port()
    core = P.start_core(cport)
    rpc(cport, "sniffdl.settings.set", {"outdir": os.path.join(P.DATA, "out"), "clip_watch": False, "notify": False,
                                        "sound": True, "bt_tracker_auto": False, "retry_count": 0})
    errors = []
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as p:
            br = p.chromium.launch(args=["--no-sandbox"])
            ctx = br.new_context(viewport={"width": 1200, "height": 800}, bypass_csp=True)
            ctx.add_init_script(STUB)
            pg = ctx.new_page()
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.goto("http://127.0.0.1:%d/ui/index.html#port=%d&token=%s" % (cport, cport, P.TOKEN))
            pg.wait_for_function("window.__sdlReady === true", timeout=20000)
            n = lambda: pg.evaluate("window.__chimes")   # noqa: E731

            check("S0 初始 0 声", n() == 0, n())
            g = rpc(cport, "sniffdl.task.add", {"url": "http://127.0.0.1:%d/files/s1.bin" % fport, "name": "s1.bin"})
            gid = g.get("gid") if isinstance(g, dict) else g
            ok = wait(lambda: any(t["gid"] == gid and t["status"] == "complete" for t in P.tasks(cport)), 40)
            check("S1 sound=开：下载完成响两声", ok and wait(lambda: n() == 2, 8), (ok, n()))
            before = n()
            rpc(cport, "sniffdl.settings.set", {"sound": False})
            wait(lambda: rpc(cport, "sniffdl.settings.get")["values"]["sound"] is False, 5)
            pg.wait_for_timeout(600)
            g = rpc(cport, "sniffdl.task.add", {"url": "http://127.0.0.1:%d/files/s2.bin" % fport, "name": "s2.bin"})
            gid = g.get("gid") if isinstance(g, dict) else g
            ok = wait(lambda: any(t["gid"] == gid and t["status"] == "complete" for t in P.tasks(cport)), 40)
            pg.wait_for_timeout(1000)
            check("S2 sound=关：下载完成不响", ok and n() == before, (ok, n(), before))
            rpc(cport, "sniffdl.settings.set", {"sound": True})
            wait(lambda: rpc(cport, "sniffdl.settings.get")["values"]["sound"] is True, 5)
            pg.wait_for_timeout(600)
            before = n()
            g = rpc(cport, "sniffdl.task.add", {"url": "http://127.0.0.1:%d/404/bad.bin" % fport, "name": "bad.bin"})
            gid = g.get("gid") if isinstance(g, dict) else g
            ok = wait(lambda: any(t["gid"] == gid and t["status"] == "error" for t in P.tasks(cport)), 40)
            pg.wait_for_timeout(800)
            check("S3 下载失败不响提示音", ok and n() == before, (ok, n(), before))
            # 没有 AudioContext
            ctx2 = br.new_context(viewport={"width": 1000, "height": 700}, bypass_csp=True)
            ctx2.add_init_script("delete window.AudioContext; delete window.webkitAudioContext;")
            pg2 = ctx2.new_page()
            e2 = []
            pg2.on("pageerror", lambda e: e2.append(str(e)))
            pg2.goto("http://127.0.0.1:%d/ui/index.html#port=%d&token=%s" % (cport, cport, P.TOKEN))
            pg2.wait_for_function("window.__sdlReady === true", timeout=20000)
            pg2.evaluate("window.__sdlPlayChime()")
            check("S4 没有 AudioContext：调用提示音不抛错", not e2, e2)
            check("S5 页面全程无 JS 报错", not errors, errors[:3])
            br.close()
    finally:
        P.stop_core(cport, core)
        srv.shutdown()
    bad = [r for r in P.RESULTS if not r[1]]
    print("\n%d/%d 通过" % (len(P.RESULTS) - len(bad), len(P.RESULTS)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
