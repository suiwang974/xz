# -*- coding: utf-8 -*-
"""Windows 真机：Electron 33 宿主 + 真内核（真 tkinter，不打桩）+ 真 aria2c.exe 端到端，带拟人操作录屏。

运行 A（录屏）：启动 → 内核握手 → 主窗截图 → 拟人粘贴链接下载完成 → 打开详情 → 设置里切深色 → 大肥鱼出现/说话
               → 二次启动单实例 → 杀内核自动重启 → 托盘菜单弹出截图 → 托盘「退出 SniffDL」→ 全部进程退出 + runtime.json 清理
运行 B：启动 → 开慢速下载（aria2c 在跑）→ taskkill /F 强杀 electron.exe 主进程 → 内核与 aria2c.exe 数秒内自退
数据目录全部在 C:\\测试用户\\ 下（中文路径）。
环境：SNIFFDL_ELECTRON=electron.exe 路径  SNIFFDL_ARIA2C=aria2c.exe 路径
"""
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (OUT, V3, Results, NodeInspector, free_port, make_file_server, procs, alive, rpc, wait,  # noqa: E402
                    screenshot_full, start_rec, stop_rec)
import humanize as HU  # noqa: E402

R = Results("host")
ELECTRON = os.environ["SNIFFDL_ELECTRON"]
ARIA2C = os.environ.get("SNIFFDL_ARIA2C", "")
APP = os.path.join(V3, "pet_electron", "app")
BASE = "C:\\测试用户\\sdlhost"
SHOTS = os.path.join(OUT, "shots")
os.makedirs(SHOTS, exist_ok=True)
CORE_SUB = "sniffdl_core.py"


def shot_page(pg, name):
    p = os.path.join(SHOTS, name)
    try:
        pg.screenshot(path=p)
        return os.path.getsize(p)
    except Exception as e:
        print("shot err", name, e)
        return 0


def launch(T, extra=(), inspect=None, cdp=None):
    env = dict(os.environ)
    env["SNIFFDL_PYTHON"] = sys.executable
    if ARIA2C:
        env["SNIFFDL_ARIA2C"] = ARIA2C
        env["PATH"] = os.path.dirname(ARIA2C) + os.pathsep + env.get("PATH", "")
    args = [ELECTRON]
    if inspect:
        args.append("--inspect=%d" % inspect)
    if cdp:                                  # Chromium 开关放在应用路径之前，Windows 上位置在后可能被当成应用参数
        args += ["--remote-debugging-port=%d" % cdp, "--remote-allow-origins=*"]
    args += [APP]
    args += ["--user-data", os.path.join(T, "ud"), "--runtime", os.path.join(T, "runtime.json"),
             "--data-dir", os.path.join(T, "data"), "--state", os.path.join(T, "pet", "pet_state.json"),
             "--port", str(free_port())] + list(extra)
    out = open(os.path.join(T, "electron_%d.out" % int(time.time() * 1000)), "w", encoding="utf-8", errors="replace")
    return subprocess.Popen(args, cwd=V3, env=env, stdout=out, stderr=subprocess.STDOUT)


def read_rt(T):
    try:
        return json.load(open(os.path.join(T, "runtime.json"), encoding="utf-8"))
    except Exception:
        return None


def prep(T, outdir):
    shutil.rmtree(T, ignore_errors=True)
    for d in ("data", "pet", outdir):
        os.makedirs(os.path.join(T, d), exist_ok=True)
    json.dump({"outdir": os.path.join(T, outdir), "clip_watch": False, "pet_live": True, "notify": True,
               "motw": True, "keep_awake": True},
              open(os.path.join(T, "data", "config.json"), "w", encoding="utf-8"), ensure_ascii=False)


def handshake(T, timeout=90, not_pid=None):
    def f():
        r = read_rt(T)
        if r and r.get("pid") != not_pid and rpc(r["port"], "sniffdl.ping").get("result"):
            return r
    return wait(f, timeout, 0.5)


def _proc_table():
    try:
        import psutil
        out = []
        for p in psutil.process_iter(["pid", "ppid", "name", "cmdline", "cwd"]):
            n = (p.info["name"] or "").lower()
            if n.startswith(("electron", "python", "aria2c", "sniffdl")):
                out.append((p.info["pid"], p.info["ppid"], n, " ".join(p.info["cmdline"] or [])[-220:]))
        return out
    except Exception as e:
        return ["psutil err %r" % e]


def _diag(T, el, tag):
    """失败时把真机证据打出来：aria2c 在哪、内核怎么找 aria2c、宿主/内核日志尾部、进程表、端口占用。"""
    import glob
    print("==== 诊断 %s ====" % tag, flush=True)
    print("SNIFFDL_ARIA2C=%r exists=%s  which(aria2c)=%r  electron poll=%r" % (
        ARIA2C, bool(ARIA2C) and os.path.isfile(ARIA2C), shutil.which("aria2c"), el.poll() if el else None), flush=True)
    print("runtime.json:", read_rt(T), flush=True)
    for row in _proc_table():
        print("  proc", row, flush=True)
    try:
        import psutil
        ls = [(c.laddr.port, c.pid) for c in psutil.net_connections("inet") if c.status == "LISTEN" and c.laddr.port in (6800, 6801)]
        print("LISTEN 6800/6801:", ls, flush=True)
    except Exception as e:
        print("net err", repr(e), flush=True)
    files = glob.glob(os.path.join(T, "electron_*.out")) + glob.glob(os.path.join(T, "pet", "*.log")) + \
        glob.glob(os.path.join(T, "data", "*.log")) + glob.glob(os.path.join(T, "data", "logs", "*.log"))
    for f in files:
        try:
            t = open(f, encoding="utf-8", errors="replace").read()
            print("---- %s (%d bytes) 末 2500 ----\n%s" % (f, len(t), t[-2500:]), flush=True)
        except Exception as e:
            print("read", f, repr(e), flush=True)
    try:     # 内核找 aria2c 的结果（与宿主同环境：PATH 已前置 aria2c 目录）
        r = subprocess.run([sys.executable, "-c", "import sys,os;sys.path.insert(0,%r);os.chdir(%r);import video_downloader as V;"
                            "print('resolve_aria2c=',V.resolve_aria2c({}),'ARIA2C=',V.ARIA2C)" % (V3, V3)],
                           capture_output=True, text=True, timeout=60, errors="replace",
                           env=dict(os.environ, PATH=(os.path.dirname(ARIA2C) + os.pathsep if ARIA2C else "") + os.environ.get("PATH", "")))
        print("内核找 aria2c：", (r.stdout + r.stderr).strip()[-600:], flush=True)
    except Exception as e:
        print("resolve err", repr(e), flush=True)


def _clean_stray(tag):
    """前面步骤残留的 SniffDL（electron 宿主 / 内核 / aria2c）会和本测试抢 6800、单实例，先打印再清掉。"""
    rows = [r for r in _proc_table() if isinstance(r, tuple) and (r[2].startswith(("electron", "aria2c"))
                                                                    or CORE_SUB in r[3] or "--core" in r[3])]
    rows = [r for r in rows if r[0] != os.getpid()]
    if rows:
        print("[%s] 清掉残留进程：" % tag, flush=True)
        for r in rows:
            print("   ", r, flush=True)
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(r[0])], capture_output=True)
        time.sleep(1.5)


def find_page(br, sub):
    for c in br.contexts:
        for pg in c.pages:
            if sub in pg.url:
                return pg
    return None



def _dump_cdp_diag(T, el, cdp, lg):
    print("---- CDP 诊断 ----  electron poll=%r cdp=%d" % (el.poll(), cdp), flush=True)
    try:
        import psutil
        pr = psutil.Process(el.pid)
        print("children:", [(c.pid, c.name()) for c in pr.children(recursive=True)][:20], flush=True)
        print("listen:", sorted({c.laddr.port for c in psutil.net_connections("inet") if c.status == "LISTEN"
                                 and c.pid in [el.pid] + [x.pid for x in pr.children(recursive=True)]}), flush=True)
    except Exception as e:
        print("psutil diag err", repr(e), flush=True)
    import glob
    for f in glob.glob(os.path.join(T, "electron_*.out")) + [lg]:
        try:
            t = open(f, encoding="utf-8", errors="replace").read()
            print("---- %s (%d bytes) 末 3000 ----\n%s" % (f, len(t), t[-3000:]), flush=True)
        except Exception as e:
            print("read", f, repr(e), flush=True)


def run_a():
    _clean_stray("A 开始前")
    T = os.path.join(BASE, "A")
    prep(T, "下载 输出")
    srv, fport, sha = make_file_server(8 * 1024 * 1024, delay=0.03, name="大肥鱼_样例.bin")
    cdp, insp = free_port(), free_port()
    tl = HU.Timeline(os.path.join(OUT, "host_timeline.json"))
    rec = start_rec(os.path.join(OUT, "host_raw.mp4"))
    tl.start(rec)
    tl.seg("启动 Electron（等待）", 6)
    el = launch(T, inspect=insp, cdp=cdp)
    try:
        info = handshake(T)
        R.ok("A1 内核握手（runtime.json + ping）", info, info and {"port": info["port"], "pid": info["pid"]})
        if not info:
            _diag(T, el, "A1")
            return None
        core_pid = info["pid"]
        lg = os.path.join(T, "pet", "pet_electron.log")
        R.ok("A2 宿主日志有 core ready", wait(lambda: "core ready port=%d" % info["port"] in open(lg, encoding="utf-8").read(), 10), "")
        import psutil
        cmdl = " ".join(psutil.Process(core_pid).cmdline())
        R.ok("A3 内核带 --parent-pid（= electron 主进程）", "--parent-pid %d" % el.pid in cmdl, cmdl[-200:])

        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            def _cdp_up():
                try:
                    import urllib.request
                    return b"webSocketDebuggerUrl" in urllib.request.urlopen(
                        "http://127.0.0.1:%d/json/version" % cdp, timeout=3).read()
                except Exception:
                    return False
            up = wait(_cdp_up, 40, 1)
            if not up:                      # 诊断：Electron 还活着吗、它的输出、端口监听、子进程、宿主日志
                _dump_cdp_diag(T, el, cdp, lg)
            br = p.chromium.connect_over_cdp("http://127.0.0.1:%d" % cdp, timeout=20000) if up else None
            R.ok("A4 CDP 连上 Electron", br, "" if up else "端口 %d 40s 内没起来" % cdp)
            if not br:
                return None
            ui = wait(lambda: find_page(br, "ui/index.html"), 40)
            R.ok("A5 主窗加载 v3/ui/index.html", ui, ui and ui.url)
            errs = []
            ui.on("pageerror", lambda e: errs.append(str(e)))
            killed = [False]       # 主动杀内核之后，界面对旧端口的请求被重置/旧令牌 401 都是预期的，不算界面错误
            def _con(m):
                if m.type != "error":
                    return
                try:
                    u = (m.location or {}).get("url", "")
                except Exception:
                    u = ""
                (late_errs if killed[0] else errs).append("console.error: %s [%s]" % (m.text, u))
            late_errs = []
            ui.on("console", _con)
            # 界面有 CSP（script-src 'self'，无 unsafe-eval）：Playwright wait_for_function 的字符串谓词走 eval 会被拒，
            # 改成 Python 侧轮询 evaluate（CDP Runtime.evaluate 不受页面 CSP 限制）
            if not wait(lambda: ui.evaluate("window.__sdlReady === true"), 40, 0.5):
                raise RuntimeError("主窗 40s 内未 __sdlReady")
            tl.seg("主窗出现")
            ui.bring_to_front()
            HU.pause(0.8, 1.2)
            R.ok("A6 主窗截图 main_ready.png", shot_page(ui, "main_ready.png") > 15000)
            screenshot_full(os.path.join(SHOTS, "desktop_main_ready.png"))
            c = ui.evaluate("window.sniffdlHost.conn()")
            R.ok("A7 sniffdlHost.conn() 与 runtime.json 一致", c and c.get("port") == info["port"], c)
            sz = ui.evaluate("[window.outerWidth, window.outerHeight, screen.width, screen.height]")
            R.ok("A8 主窗尺寸（期望 1320×860，屏幕小则受限）", sz[0] >= 900, sz)

            # —— 拟人：点输入框、逐字打链接、Ctrl+Enter
            url = "http://127.0.0.1:%d/files/dfy_sample.bin" % fport
            xy = HU.page_xy(ui, "#addInput")
            if not (xy and HU.click(*xy)):
                ui.click("#addInput")
            if not HU.type_text(url):
                ui.keyboard.insert_text(url)
            HU.pause()
            if ui.evaluate("document.querySelector('#addInput').value") != url:      # 坐标偏了 → 兜底
                ui.fill("#addInput", url)
            HU.hotkey("ctrl", "enter")
            if not wait(lambda: ui.query_selector(".task"), 6):
                ui.focus("#addInput"); ui.keyboard.press("Control+Enter")
            ok_task = wait(lambda: ui.query_selector(".task"), 15)
            R.ok("A9 拟人粘贴链接 → 出现任务行", ok_task)
            # 直链默认走自研动态分片引擎（speed_engine=auto），不会起 aria2c.exe；有进度即证明引擎在跑
            ar = wait(lambda: procs("aria2c.exe") or ui.evaluate("[...window.__sdl.S.tasks.values()].some(t => t.done > 0 || t.status === 'complete')"), 20)
            R.ok("A10 下载中引擎在跑（自研引擎有进度，或 aria2c.exe 已拉起）", ar, ar)
            if not ar:
                _diag(T, el, "A10")
            tl.seg("下载中（等待）", 6)
            time.sleep(2)
            shot_page(ui, "main_downloading.png")
            done = wait(lambda: ui.evaluate("[...window.__sdl.S.tasks.values()].some(t => t.status === 'complete')"), 90, 0.5)
            tl.seg("下载完成")
            R.ok("A11 下载完成", done, ui.evaluate("JSON.stringify([...window.__sdl.S.tasks.values()].map(t=>[t.status,t.done,t.total,t.err]))"))
            if not done:
                _diag(T, el, "A11")
            outd = os.path.join(T, "下载 输出")
            files = [os.path.join(d, f) for d, _, fs in os.walk(outd) for f in fs]
            a12 = any(os.path.getsize(f) == 8 * 1024 * 1024 for f in files)
            R.ok("A12 文件落在中文路径且大小正确", a12, files)
            if not a12:
                print("A12 诊断：outdir=%r 存在=%s  任务=%s" % (outd, os.path.isdir(outd), ui.evaluate(
                    "JSON.stringify([...window.__sdl.S.tasks.values()].map(t=>[t.name,t.dir,t.path,t.status]))")), flush=True)
                for d, _, fs in os.walk(T):
                    for f in fs:
                        if not f.endswith((".log", ".out", ".json")):
                            print("   file", os.path.join(d, f), flush=True)
            if files:
                import hashlib
                f0 = max(files, key=os.path.getsize)
                R.ok("A13 sha256 一致", hashlib.sha256(open(f0, "rb").read()).hexdigest() == sha)
                ads = None
                try:
                    ads = open(f0 + ":Zone.Identifier", encoding="utf-8").read()
                except Exception as e:
                    ads = "ERR " + str(e)
                R.ok("A14 MOTW：Zone.Identifier ZoneId=3（宿主路径）", "ZoneId=3" in (ads or ""), ads)
            HU.pause(0.6, 1.0)
            shot_page(ui, "main_done.png")

            # —— 打开详情
            xy = HU.page_xy(ui, ".task")
            if xy:
                HU.click(*xy)          # 单击选中（双击已完成任务会直接打开文件，不用）
            if not wait(lambda: ui.query_selector("#detail #dStats"), 3):
                gid = ui.get_attribute(".task", "data-gid")
                ui.evaluate("g => window.__sdl.openDetail(g)", gid)
            R.ok("A15 打开详情抽屉", wait(lambda: ui.query_selector("#detail"), 5))
            HU.pause(1.0, 1.4)
            shot_page(ui, "main_detail.png")
            HU.press("esc")
            if ui.query_selector("#detail"):
                ui.keyboard.press("Escape")
            HU.pause()

            # —— 设置 → 深色
            tl.seg("切深色")
            xy = HU.page_xy(ui, '[data-nav="settings"]')
            if xy:
                HU.click(*xy)
            if ui.evaluate("document.getElementById('vSettings').classList.contains('hidden')"):
                ui.keyboard.press("Alt+7")
            btn = '#set_ui_theme button[data-v="深色"]'
            wait(lambda: ui.query_selector(btn), 5)
            xy = HU.page_xy(ui, btn)
            if xy:
                ui.locator(btn).scroll_into_view_if_needed()
                xy = HU.page_xy(ui, btn)
                HU.click(*xy)
            if ui.evaluate("document.documentElement.dataset.theme") != "dark":
                ui.click(btn)
            R.ok("A16 设置里切到深色", wait(lambda: ui.evaluate("document.documentElement.dataset.theme") == "dark", 5))
            HU.pause(0.8, 1.2)
            shot_page(ui, "main_dark_settings.png")
            xy = HU.page_xy(ui, '[data-nav="all"]') or HU.page_xy(ui, '[data-nav="downloads"]')
            if xy:
                HU.click(*xy)
            else:
                ui.evaluate("window.__sdl.go('all')")
            HU.pause(0.8, 1.2)
            shot_page(ui, "main_dark_list.png")

            # —— 大肥鱼
            tl.seg("大肥鱼")
            pet = wait(lambda: find_page(br, "desk.html"), 20)
            R.ok("A17 桌宠窗出现（desk.html）", pet, pet and pet.url)
            ready = wait(lambda: pet.evaluate("!!window.__petModelReady"), 60, 0.5) if pet else None
            R.ok("A18 桌宠 Live2D 模型就绪（Windows 无 GPU 走软件渲染）", ready)
            r = rpc(info["port"], "sniffdl.pet", "say", "Windows 实测：本鱼来啦！", token=info["ui_token"])
            R.ok("A19 内核 → 桌宠 say", (r.get("result") or {}).get("ok"), r)
            time.sleep(2.5)
            if pet:
                shot_page(pet, "pet_window_cdp.png")
            screenshot_full(os.path.join(SHOTS, "desktop_pet_transparent.png"))
            R.ok("A20 整屏截图（透明桌宠合成效果）", os.path.exists(os.path.join(SHOTS, "desktop_pet_transparent.png")))

            # —— 二次启动
            tl.seg("二次启动（等待）", 4)
            e2 = launch(T)
            try:
                rc = e2.wait(25)
            except subprocess.TimeoutExpired:
                rc = None
                e2.kill()
            R.ok("A21 二次启动自行退出（单实例）", rc is not None, "rc=%s" % rc)
            time.sleep(1)
            R.ok("A22 仍只有一个内核", len(procs(cmd_sub=CORE_SUB)) == 1, procs(cmd_sub=CORE_SUB))
            R.ok("A23 原实例收到 second-instance", "second-instance" in open(lg, encoding="utf-8").read())

            # —— 杀内核 → 自动重启
            killed[0] = True
            subprocess.run(["taskkill", "/F", "/PID", str(core_pid)], capture_output=True)
            info2 = handshake(T, 60, not_pid=core_pid)
            R.ok("A24 内核被 taskkill /F 后自动重启", info2, info2 and {"pid": info2["pid"]})
            if info2:
                R.ok("A25 conn() 返回新内核", wait(lambda: ui.evaluate("window.sniffdlHost.conn()").get("pid") == info2["pid"], 20))
            core_pid2 = (info2 or info)["pid"]
            R.ok("A26 界面无 pageerror/console.error（杀内核之前）", not errs, errs[:5])
            print("A26 info：杀内核之后的预期报错 %d 条 %s" % (len(late_errs), late_errs[:3]))

            # —— 托盘菜单
            tl.seg("托盘菜单")
            tray_ok = None
            try:
                ni = NodeInspector(insp)
                b = ni.on_tray("function(){ const b=this.getBounds(); setTimeout(()=>this.popUpContextMenu(), 300); return b; }")
                tray_ok = b
                time.sleep(1.5)
                screenshot_full(os.path.join(SHOTS, "desktop_tray_menu.png"))
                R.ok("A27 托盘图标存在并弹出菜单（截图 desktop_tray_menu.png）", b and "width" in b, b)
                if b and b.get("width"):
                    HU.move(b["x"] + b["width"] / 2, b["y"] - 60)
                    HU.pause()
                # 菜单最后一项是「退出 SniffDL」：↑ 选中，回车
                HU.press("up")
                HU.pause(0.6, 0.9)
                screenshot_full(os.path.join(SHOTS, "desktop_tray_quit_hover.png"))
                HU.press("enter")
                ni.close()
            except Exception as e:
                R.ok("A27 托盘图标存在并弹出菜单", False, repr(e))
            tl.seg("退出（等待）", 4)
            rc = None
            try:
                rc = el.wait(25)
            except subprocess.TimeoutExpired:
                pass
            if rc is None:       # 真菜单键盘选择没生效 → 先直接 click 菜单项（同一个 click 处理函数），再兜底 sniffdlHost.quit()
                R.ok("A28 托盘「退出 SniffDL」让 Electron 退出（键盘选菜单）", False, "25s 内未退出，改用菜单项 click()")
                try:
                    ni2 = NodeInspector(insp)
                    lab = ni2.eval("(()=>{const m=globalThis.__sdlTrayMenu; const it=m&&m.items.find(i=>String(i.label).includes('退出')); if(!it) return null; it.click(); return it.label})()")
                    ni2.close()
                    try:
                        rc = el.wait(20)
                    except subprocess.TimeoutExpired:
                        rc = None
                    R.ok("A28b 托盘菜单项「退出 SniffDL」click() 让 Electron 退出", rc is not None, "label=%s rc=%s" % (lab, rc))
                except Exception as e:
                    R.ok("A28b 托盘菜单项 click()", False, repr(e))
            if rc is None:
                try:
                    ui.evaluate("window.sniffdlHost.quit()")
                except Exception:
                    pass
                try:
                    rc = el.wait(20)
                except subprocess.TimeoutExpired:
                    pass
            else:
                R.ok("A28 托盘「退出 SniffDL」让 Electron 退出", True, "rc=%s tray=%s" % (rc, tray_ok))
            try:
                br.close()
            except Exception:
                pass
        R.ok("A29 Electron 已退出", rc is not None, "rc=%s" % rc)
        R.ok("A30 内核进程已退出", wait(lambda: not alive(core_pid2) and not procs(cmd_sub=CORE_SUB), 12))
        R.ok("A31 aria2c.exe 已退出", wait(lambda: not procs("aria2c.exe"), 12), procs("aria2c.exe"))
        R.ok("A32 无残留 electron.exe", wait(lambda: not procs("electron.exe"), 12), procs("electron.exe"))
        R.ok("A33 runtime.json 已清理", not os.path.exists(os.path.join(T, "runtime.json")))
        time.sleep(1)
    finally:
        tl.end()
        stop_rec(rec)
        for pid in procs("electron.exe"):
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
        srv.shutdown()


def run_b():
    _clean_stray("B 开始前")
    T = os.path.join(BASE, "B")
    prep(T, "out")
    srv, fport, _ = make_file_server(64 * 1024 * 1024, delay=0.08, name="slow.bin")
    cdp = free_port()
    el = launch(T, cdp=cdp)
    try:
        info = handshake(T)
        R.ok("B1 内核握手", info)
        if not info:
            _diag(T, el, "B1")
            return
        core_pid = info["pid"]
        r = rpc(info["port"], "sniffdl.task.add", {"url": "http://127.0.0.1:%d/slow.bin" % fport, "start_now": True}, token=info["ui_token"])
        R.ok("B2 RPC 添加慢速下载", (r.get("result") or {}).get("ok"), r)
        def _prog():
            try:
                ts = rpc(info["port"], "sniffdl.tasks", token=info["ui_token"]).get("result") or []
                return any((t.get("done") or 0) > 0 for t in ts) or procs("aria2c.exe")
            except Exception:
                return None
        ar = wait(_prog, 25)
        R.ok("B3 下载引擎在跑（任务有进度，或 aria2c.exe 已拉起）", ar, ar)
        if not ar:
            _diag(T, el, "B3")
        time.sleep(3)
        kill = subprocess.run(["taskkill", "/F", "/PID", str(el.pid)], capture_output=True, text=True, errors="replace")
        t0 = time.time()
        R.ok("B4 taskkill /F 强杀 electron.exe 主进程", kill.returncode == 0, (kill.stdout + kill.stderr).strip())
        g1 = wait(lambda: not alive(core_pid), 15)
        R.ok("B5 内核在 15s 内自退（--parent-pid 看门狗）", g1, "用时 %.1fs" % (time.time() - t0))
        g2 = wait(lambda: not procs("aria2c.exe"), 15)
        R.ok("B6 aria2c.exe 在 15s 内自退（--stop-with-process）", g2, "用时 %.1fs 残留=%s" % (time.time() - t0, procs("aria2c.exe")))
        g3 = wait(lambda: not procs("electron.exe"), 15)
        R.ok("B7 electron 子进程（renderer/gpu）全退", g3, procs("electron.exe"))
        # 宿主被 taskkill /F 时 Windows 把内核一并硬杀（B5 仅 0.3s），内核没机会删 runtime.json——这是无法避免的。
        # 要求的是「残留文件不害人」：要么已删，要么里面的 pid 已死（RT.read 会当作没有内核），并且同路径能再起一个内核。
        rtp = os.path.join(T, "runtime.json")
        def _stale_ok():
            if not os.path.exists(rtp):
                return True
            r = read_rt(T)
            return bool(r) and not alive(int(r.get("pid") or 0))
        R.ok("B8 runtime.json 已删或只剩死 pid 的残留", wait(_stale_ok, 5), read_rt(T))
        try:
            core2 = subprocess.Popen([sys.executable, os.path.join(V3, "sniffdl_core.py"), "--runtime", rtp, "--ui-token", "b8",
                                      "--host", "none", "--data-dir", os.path.join(T, "data")], cwd=V3,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            h2 = handshake(T, 60, not_pid=core_pid)
            R.ok("B8b 残留 runtime.json 不影响同路径重新起内核", h2, h2 and {"pid": h2["pid"]})
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(core2.pid)], capture_output=True)
        except Exception as e:
            R.ok("B8b 残留 runtime.json 不影响同路径重新起内核", False, repr(e))
    finally:
        for pid in procs("electron.exe"):
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
        for pid in procs("aria2c.exe"):
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        srv.shutdown()


def main():
    os.makedirs(BASE, exist_ok=True)
    try:
        run_a()
    except Exception as e:
        import traceback
        traceback.print_exc()
        R.ok("A 运行异常", False, repr(e))
    try:
        run_b()
    except Exception as e:
        import traceback
        traceback.print_exc()
        R.ok("B 运行异常", False, repr(e))
    # 收集日志
    for d, _, fs in os.walk(BASE):
        for f in fs:
            if f.endswith((".log", ".out")) or f == "result.json":
                rel = os.path.relpath(os.path.join(d, f), BASE).replace(os.sep, "__")
                try:
                    shutil.copy(os.path.join(d, f), os.path.join(OUT, "host_logs__" + rel))
                except Exception:
                    pass
    return R.save()


if __name__ == "__main__":
    sys.exit(main())
