# -*- coding: utf-8 -*-
"""试用版冒烟：在解压目录（中文+空格路径）里像用户一样「双击」启动SniffDL.vbs（ShellExecute），
内核握手 → 主窗截图 → 拟人粘贴链接下载完成 → 大肥鱼 → 退出 → 进程清理；再用 .bat 启动一次验证握手。
    python trial_smoke.py <解压后的 SniffDL_v3_trial_win64 目录>
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import OUT, Results, free_port, make_file_server, procs, rpc, wait, screenshot_full, start_rec, stop_rec  # noqa: E402
import humanize as HU  # noqa: E402

R = Results("trial")
D = os.path.abspath(sys.argv[1])
SHOTS = os.path.join(OUT, "shots")
os.makedirs(SHOTS, exist_ok=True)
RT = os.path.join(D, "data", "runtime.json")


def read_rt():
    try:
        return json.load(open(RT, encoding="utf-8"))
    except Exception:
        return None


def handshake(timeout=120):
    def f():
        r = read_rt()
        if r and rpc(r["port"], "sniffdl.ping").get("result"):
            return r
    return wait(f, timeout, 0.5)


def cleanup():
    for n in ("electron.exe", "aria2c.exe"):
        for pid in procs(n):
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
    for pid in procs(cmd_sub="sniffdl_core.py"):
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)


def find_page(br, sub):
    for c in br.contexts:
        for pg in c.pages:
            if sub in pg.url:
                return pg
    return None


def main():
    R.ok("T0 解压目录结构", all(os.path.exists(os.path.join(D, x)) for x in
                             ("启动SniffDL.vbs", "启动SniffDL.bat", "python\\python.exe", "electron\\electron.exe",
                              "aria2\\aria2c.exe", "v3\\sniffdl_core.py", "v3\\pet_electron\\app\\main.js")), D)
    r = subprocess.run([os.path.join(D, "python", "python.exe"), "-c",
                        "import sys,tkinter,yt_dlp,requests,PIL;print(sys.version, yt_dlp.version.__version__)"],
                       capture_output=True, text=True, errors="replace", env={k: v for k, v in os.environ.items() if not k.startswith("PYTHON")} | {"PYTHONNOUSERSITE": "1", "SystemRoot": os.environ.get("SystemRoot", "C:\\Windows")})
    R.ok("T1 自带 Python 可独立运行且依赖齐（tkinter/yt_dlp/requests/PIL）", r.returncode == 0, (r.stdout + r.stderr)[-400:])
    cdp = free_port()
    os.environ["SNIFFDL_EXTRA"] = "--remote-debugging-port=%d" % cdp
    srv, fport, _ = make_file_server(10 * 1024 * 1024, delay=0.03, name="试用样例.bin")
    tl = HU.Timeline(os.path.join(OUT, "trial_timeline.json"))
    rec = start_rec(os.path.join(OUT, "trial_raw.mp4"))
    tl.start(rec)
    tl.seg("双击启动SniffDL.vbs（等待）", 6)
    try:
        os.startfile(os.path.join(D, "启动SniffDL.vbs"))           # 等同双击
        t0 = time.time()
        info = handshake()
        R.ok("T2 双击 .vbs → 内核握手（data\\runtime.json + ping）", info, info and {"pid": info["pid"], "用时s": round(time.time() - t0, 1)})
        if not info:
            log = os.path.join(D, "data", "pet", "pet_electron.log")
            print(open(log, encoding="utf-8", errors="replace").read()[-3000:] if os.path.exists(log) else "no pet log")
            return R.save()
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            br = wait(lambda: p.chromium.connect_over_cdp("http://127.0.0.1:%d" % cdp), 40, 0.5)
            R.ok("T3 CDP 连上", br)
            ui = wait(lambda: find_page(br, "ui/index.html"), 40)
            # 界面有 CSP（script-src 'self'，无 unsafe-eval）：Playwright wait_for_function 的字符串谓词走 eval 会被拒，
            # 改成 Python 侧轮询 evaluate（CDP Runtime.evaluate 不受页面 CSP 限制）
            if not wait(lambda: ui.evaluate("window.__sdlReady === true"), 40, 0.5):
                raise RuntimeError("主窗 40s 内未 __sdlReady")
            tl.seg("主窗出现")
            HU.pause(0.8, 1.2)
            ui.screenshot(path=os.path.join(SHOTS, "trial_main.png"))
            screenshot_full(os.path.join(SHOTS, "trial_desktop.png"))
            R.ok("T4 主窗截图 trial_main.png", os.path.getsize(os.path.join(SHOTS, "trial_main.png")) > 15000)
            url = "http://127.0.0.1:%d/试用/sample.bin" % fport
            url = "http://127.0.0.1:%d/trial/sample.bin" % fport
            xy = HU.page_xy(ui, "#addInput")
            if not (xy and HU.click(*xy)):
                ui.click("#addInput")
            if not HU.type_text(url):
                ui.keyboard.insert_text(url)
            if ui.evaluate("document.querySelector('#addInput').value") != url:
                ui.fill("#addInput", url)
            HU.hotkey("ctrl", "enter")
            if not wait(lambda: ui.query_selector(".task"), 6):
                ui.focus("#addInput"); ui.keyboard.press("Control+Enter")
            tl.seg("下载中（等待）", 6)
            done = wait(lambda: ui.evaluate("[...window.__sdl.S.tasks.values()].some(t => t.status === 'complete')"), 90, 0.5)
            tl.seg("下载完成")
            R.ok("T5 试用版里下载完成（自带 aria2c）", done, ui.evaluate("JSON.stringify([...window.__sdl.S.tasks.values()].map(t=>[t.status,t.done,t.total,t.err]))"))
            HU.pause(1.0, 1.5)
            ui.screenshot(path=os.path.join(SHOTS, "trial_main_done.png"))
            pet = wait(lambda: find_page(br, "desk.html"), 20)
            ready = wait(lambda: pet.evaluate("!!window.__petModelReady"), 60, 0.5) if pet else None
            R.ok("T6 大肥鱼桌宠就绪", ready)
            try:
                rpc(info["port"], "sniffdl.pet", "say", "试用版启动成功！", token=info["ui_token"])
            except Exception:
                pass
            time.sleep(2.5)
            screenshot_full(os.path.join(SHOTS, "trial_desktop_pet.png"))
            tl.seg("退出（等待）", 4)
            try:
                ui.evaluate("window.sniffdlHost.quit()")
            except Exception:
                pass
            try:
                br.close()
            except Exception:
                pass
        R.ok("T7 退出后 electron/内核/aria2c 全清", wait(lambda: not procs("electron.exe") and not procs("aria2c.exe")
                                                     and not procs(cmd_sub="sniffdl_core.py"), 25),
             {"electron": procs("electron.exe"), "aria2c": procs("aria2c.exe")})
    finally:
        tl.end()
        stop_rec(rec)
        cleanup()
    # .bat 直接启动一次
    subprocess.Popen(["cmd", "/c", os.path.join(D, "启动SniffDL.bat")], cwd=D)
    info = handshake(90)
    R.ok("T8 .bat 启动 → 内核握手", info)
    time.sleep(3)
    screenshot_full(os.path.join(SHOTS, "trial_bat_desktop.png"))
    cleanup()
    srv.shutdown()
    log = os.path.join(D, "data", "pet", "pet_electron.log")
    if os.path.exists(log):
        import shutil
        shutil.copy(log, os.path.join(OUT, "trial_pet_electron.log"))
    for f in ("sniffdl.log",):
        p = os.path.join(D, "data", f)
        if os.path.exists(p):
            import shutil
            shutil.copy(p, os.path.join(OUT, "trial_" + f))
    return R.save()


if __name__ == "__main__":
    sys.exit(main())
