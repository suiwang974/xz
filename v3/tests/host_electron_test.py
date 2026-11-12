# -*- coding: utf-8 -*-
"""v3 统一宿主（Electron）端到端：Linux + Xvfb + 真 Electron 33.4.11 + 真内核（tkinter 打桩）。

    source /workspace/work/env/v3env.sh && sh tests/host_xvfb.sh && python3 tests/host_electron_test.py
检查：Electron 启动 → 内核握手(runtime.json/ping) → 主窗加载界面(__sdlReady, 1320×860, 最小 980×640) → 截图
     → 界面经 sniffdlHost 连上、粘贴本地 http 链接下载（aria2c 被拉起）→ 桌宠窗出现(desk.html, 模型就绪)
     → 二次启动不起第二个实例 → kill -9 内核 → 自动重启一次（新 pid、界面重连）→ sniffdlHost.quit()
     → Electron / 内核 / aria2c 全部退出。退出码 0 = 全过。
"""
import http.server
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request

V3 = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(V3, "pet_electron", "app")
T = "/tmp/sdlhost"
SHOTS = os.path.join(V3, "docs", "host_shots")
ELECTRON = os.environ.get("SNIFFDL_ELECTRON") or "electron"
ARIA2C = os.environ.get("SNIFFDL_ARIA2C") or "/workspace/work/sdl/speed/tools/aria2c"
CDP = 9333
RES = []
CORE_PAT = "ui_smoke_core.py --runtime " + T


def ok(name, cond, detail=""):
    RES.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + ("  " + str(detail) if detail else ""), flush=True)
    return cond


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


class SlowFile(http.server.BaseHTTPRequestHandler):
    size = 24 * 1024 * 1024

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self.send_response(200); self.send_header("Content-Length", str(self.size))
        self.send_header("Accept-Ranges", "bytes"); self.send_header("Content-Type", "application/octet-stream"); self.end_headers()

    def do_GET(self):
        start, end = 0, self.size - 1
        rg = self.headers.get("Range")
        if rg and rg.startswith("bytes="):
            a, b = rg[6:].split("-")
            start = int(a or 0); end = int(b) if b else end
            self.send_response(206); self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, self.size))
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(end - start + 1)); self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", "application/octet-stream"); self.end_headers()
        chunk = b"\0" * 65536
        n = end - start + 1
        try:
            while n > 0:
                k = min(n, len(chunk)); self.wfile.write(chunk[:k]); n -= k; time.sleep(0.08)   # ~800 KB/s
        except Exception:
            pass


def rpc(info, method, *params):
    req = urllib.request.Request("http://127.0.0.1:%d/jsonrpc" % int(info["port"]),
                                 data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": list(params)}).encode(),
                                 headers={"Content-Type": "application/json", "X-SniffDL-Token": info.get("ui_token", "")})
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return json.loads(op.open(req, timeout=4).read().decode())


def alive(pid):
    try:
        os.kill(pid, 0)
    except Exception:
        return False
    try:
        with open("/proc/%d/stat" % pid) as f:
            return f.read().split(")")[-1].split()[0] != "Z"
    except Exception:
        return False


def pids(pattern):
    if pattern == "aria2c":       # 按进程名精确匹配（-f 会匹配到命令行里带 aria2c 字样的 shell）
        out = subprocess.run(["pgrep", "-x", "aria2c"], capture_output=True, text=True).stdout.split()
        return [int(x) for x in out]
    out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout.split()
    return [int(x) for x in out if int(x) != os.getpid()]


def read_rt():
    try:
        return json.load(open(os.path.join(T, "runtime.json")))
    except Exception:
        return None


def wait(fn, timeout, step=0.25):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            r = fn()
            if r:
                return r
        except Exception:
            pass
        time.sleep(step)
    return None


def launch(extra=()):
    env = dict(os.environ)
    env["SNIFFDL_CORE_CMD"] = json.dumps([sys.executable, os.path.join(V3, "tests", "ui_smoke_core.py")])
    env["SNIFFDL_ARIA2C"] = ARIA2C
    env["PATH"] = os.path.dirname(ARIA2C) + os.pathsep + env.get("PATH", "")
    env["HOME"] = os.path.join(T, "home")
    args = [ELECTRON, APP, "--no-sandbox", "--remote-debugging-port=%d" % CDP,
            "--user-data", os.path.join(T, "ud"), "--runtime", os.path.join(T, "runtime.json"),
            "--data-dir", os.path.join(T, "data"), "--state", os.path.join(T, "pet", "pet_state.json"),
            "--port", str(free_port())] + list(extra)
    return subprocess.Popen(args, cwd=V3, env=env, stdout=open(os.path.join(T, "electron%d.out" % len(RES)), "w"),
                            stderr=subprocess.STDOUT, start_new_session=True)


def main():
    shutil.rmtree(T, ignore_errors=True)
    for d in ("data/out", "home", "pet"):
        os.makedirs(os.path.join(T, d), exist_ok=True)
    os.makedirs(SHOTS, exist_ok=True)
    json.dump({"outdir": os.path.join(T, "data", "out"), "clip_watch": False, "pet_live": True},
              open(os.path.join(T, "data", "config.json"), "w"))
    fport = free_port()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", fport), SlowFile)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    el = launch()
    log = os.path.join(T, "pet", "pet_electron.log")
    try:
        info = wait(lambda: (lambda r: r if r and rpc(r, "sniffdl.ping").get("result") else None)(read_rt()), 60)
        ok("内核握手（runtime.json + ping）", info, info and {"port": info["port"], "pid": info["pid"]})
        if not info:
            raise SystemExit(2)
        core_pid = info["pid"]
        lg = open(log, encoding="utf-8").read()
        ok("宿主读到 SNIFFDL_READY", "core ready port=%d" % info["port"] in lg)
        ok("内核带 --parent-pid", ("--parent-pid" in open("/proc/%d/cmdline" % core_pid).read().replace("\0", " ")))
        rpc(info, "sniffdl.settings.set", {"outdir": os.path.join(T, "data", "out"), "clip_watch": False, "notify": True})

        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            br = wait(lambda: p.chromium.connect_over_cdp("http://127.0.0.1:%d" % CDP), 30, 0.5)
            ok("CDP 连上 Electron", br)

            def find(sub):
                for c in br.contexts:
                    for pg in c.pages:
                        if sub in pg.url:
                            return pg
                return None
            ui = wait(lambda: find("ui/index.html"), 30)
            ok("主窗加载 v3/ui/index.html", ui, ui and ui.url)
            errs = []
            ui.on("pageerror", lambda e: errs.append(str(e)))
            ui.wait_for_function("window.__sdlReady === true", timeout=30000)
            c = ui.evaluate("window.sniffdlHost.conn()")
            ok("sniffdlHost.conn() = 内核端口/令牌", c and c.get("port") == info["port"] and c.get("token") == info["ui_token"], {"port": c and c.get("port")})
            keys = ui.evaluate("Object.keys(window.sniffdlHost).sort()")
            ok("sniffdlHost 接口齐全", all(k in keys for k in ("conn", "pickDir", "pickFile", "saveFile", "notify")), keys)
            ok("界面 host.kind = electron", ui.evaluate("import('./js/api.js').then(m=>m.host.kind)") == "electron")
            sz = ui.evaluate("[window.outerWidth, window.outerHeight]")
            ok("主窗 1320×860", abs(sz[0] - 1320) <= 2 and abs(sz[1] - 860) <= 40, sz)
            ui.evaluate("window.sniffdlHost.notify('SniffDL 宿主测试', '系统通知通道')")
            time.sleep(0.5)
            ok("notify 不报错（Linux 无通知守护，仅验通道）", "notify err" not in open(log, encoding="utf-8").read())
            # 下载：界面粘贴本地 http 链接 → aria2c 被拉起
            ui.click("#addInput"); ui.keyboard.insert_text("http://127.0.0.1:%d/files/host_sample.bin" % fport)
            ui.keyboard.press("Control+Enter")
            ui.wait_for_selector(".task", timeout=15000)
            ar = wait(lambda: pids("aria2c"), 20)
            ok("下载中 aria2c 已拉起", ar, ar)
            time.sleep(2.5)
            ui.screenshot(path=os.path.join(SHOTS, "main_downloading.png"))
            ok("截图 main_downloading.png", os.path.getsize(os.path.join(SHOTS, "main_downloading.png")) > 20000)
            pet = wait(lambda: find("desk.html"), 20)
            ok("桌宠窗出现（desk.html）", pet, pet and pet.url)
            ready = wait(lambda: pet.evaluate("!!window.__petModelReady"), 40, 0.5) if pet else None
            ok("桌宠模型就绪", ready)
            if pet:
                time.sleep(1)
                pet.screenshot(path=os.path.join(SHOTS, "pet_window.png"))
            r = rpc(info, "sniffdl.pet", "say", "宿主测试：内核 → 桌宠")
            ok("内核连上桌宠控制端口（sniffdl.pet say）", (r.get("result") or {}).get("ok"), r)

            # 二次启动
            n0 = len(pids("--user-data " + os.path.join(T, "ud")))
            e2 = launch(["--remote-debugging-port=%d" % (CDP + 1)])
            try:
                rc = e2.wait(15)
            except subprocess.TimeoutExpired:
                rc = None
            ok("二次启动自行退出", rc is not None, "rc=%s" % rc)
            time.sleep(1)
            ok("仍只有一个内核", len(pids(CORE_PAT)) == 1, pids(CORE_PAT))
            ok("原实例收到 second-instance", "second-instance" in open(log, encoding="utf-8").read())

            # 内核意外退出 → 自动重启一次
            os.kill(core_pid, signal.SIGKILL)
            info2 = wait(lambda: (lambda r: r if r and r.get("pid") != core_pid and rpc(r, "sniffdl.ping").get("result") else None)(read_rt()), 60)
            ok("内核被 kill -9 后自动重启", info2, info2 and {"port": info2["port"], "pid": info2["pid"]})
            if info2:
                okc = wait(lambda: ui.evaluate("window.sniffdlHost.conn()").get("pid") == info2["pid"], 15)
                ok("conn() 返回新内核", okc)
                time.sleep(4)
                ui.screenshot(path=os.path.join(SHOTS, "main_after_core_restart.png"))
            ok("界面无 pageerror", not errs, errs[:3])

            # 退出
            core_pid2 = (info2 or info)["pid"]
            try:
                ui.evaluate("window.sniffdlHost.quit()")
            except Exception:
                pass
            try:
                br.close()
            except Exception:
                pass
        rc = None
        try:
            rc = el.wait(20)
        except subprocess.TimeoutExpired:
            pass
        ok("Electron 退出", rc is not None, "rc=%s" % rc)
        gone = wait(lambda: not alive(core_pid2) and not pids(CORE_PAT), 10)
        ok("内核进程已退出", gone)
        gone2 = wait(lambda: not pids("aria2c"), 10)
        ok("aria2c 进程已退出", gone2, pids("aria2c"))
        ok("没有残留 Electron", wait(lambda: not pids("--user-data " + os.path.join(T, "ud")), 10))
        ok("runtime.json 已清理", not os.path.exists(os.path.join(T, "runtime.json")))
    finally:
        try:
            os.killpg(el.pid, signal.SIGKILL)
        except Exception:
            pass
        srv.shutdown()
    bad = [r for r in RES if not r[1]]
    print("\n%d/%d 通过" % (len(RES) - len(bad), len(RES)))
    json.dump([{"name": n, "ok": o, "detail": str(d)} for n, o, d in RES], open(os.path.join(T, "result.json"), "w"), ensure_ascii=False, indent=1)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
