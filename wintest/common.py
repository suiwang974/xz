# -*- coding: utf-8 -*-
"""Windows 实测公共工具：结果记录、RPC、慢速本地文件服务器、进程查询（psutil）。"""
import hashlib
import http.server
import json
import os
import socket
import sys
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
V3 = os.path.join(ROOT, "v3")
OUT = os.environ.get("WINTEST_OUT") or os.path.join(ROOT, "wintest_out")
os.makedirs(OUT, exist_ok=True)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


class Results:
    def __init__(self, suite):
        self.suite = suite
        self.items = []

    def ok(self, name, cond, detail=""):
        self.items.append({"name": name, "ok": bool(cond), "detail": str(detail)[:1500]})
        print(("PASS " if cond else "FAIL ") + name + ("  " + str(detail)[:600] if detail else ""), flush=True)
        return cond

    def skip(self, name, detail=""):
        """不计失败，但在报告里明确标 SKIP 和原因（不冒充通过）。"""
        self.items.append({"name": name, "ok": True, "skip": True, "detail": str(detail)[:1500]})
        print("SKIP " + name + ("  " + str(detail)[:600] if detail else ""), flush=True)

    def save(self):
        p = os.path.join(OUT, "result_%s.json" % self.suite)
        json.dump({"suite": self.suite, "items": self.items}, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        bad = [i for i in self.items if not i["ok"]]
        print("\n[%s] %d/%d 通过" % (self.suite, len(self.items) - len(bad), len(self.items)), flush=True)
        return 1 if bad else 0


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


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


def rpc(port, method, *params, token=""):
    req = urllib.request.Request("http://127.0.0.1:%d/jsonrpc" % int(port),
                                 data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": list(params)}).encode(),
                                 headers={"Content-Type": "application/json", "X-SniffDL-Token": token})
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return json.loads(op.open(req, timeout=8).read().decode("utf-8"))


def make_file_server(size, delay=0.02, name="sample.bin"):
    blob = (hashlib.sha256(b"sniffdl").digest() * (size // 32 + 1))[:size]

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _body(self, a, b):
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(b - a))
            self.send_header("Content-Disposition", 'attachment; filename="%s"' % name)
            self.end_headers()
            if self.command == "HEAD":
                return
            pos = a
            try:
                while pos < b:
                    n = min(65536, b - pos)
                    self.wfile.write(blob[pos:pos + n])
                    pos += n
                    if delay:
                        time.sleep(delay)
            except Exception:
                pass

        def do_GET(self):
            rg = self.headers.get("Range")
            if rg and rg.startswith("bytes="):
                s, _, e = rg[6:].partition("-")
                a = int(s or 0)
                b = int(e) + 1 if e else size
                self.send_response(206)
                self.send_header("Content-Range", "bytes %d-%d/%d" % (a, b - 1, size))
                self._body(a, b)
            else:
                self.send_response(200)
                self._body(0, size)

        do_HEAD = do_GET

    port = free_port()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port, hashlib.sha256(blob).hexdigest()


def procs(name=None, cmd_sub=None):
    import psutil
    out = []
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if name and (p.info["name"] or "").lower() != name.lower():
                continue
            if cmd_sub and cmd_sub.lower() not in " ".join(p.info["cmdline"] or []).lower():
                continue
            out.append(p.info["pid"])
        except Exception:
            pass
    return out


def alive(pid):
    import psutil
    try:
        return psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except Exception:
        return False


def screenshot_full(path):
    """整屏截图（含透明窗口真实合成效果）。"""
    try:
        from PIL import ImageGrab
        im = ImageGrab.grab(all_screens=True)
        im.save(path)
        return os.path.getsize(path)
    except Exception as e:
        print("screenshot_full err", e)
        return 0


# ---------------- 录屏（ffmpeg gdigrab，10fps h264 crf30） ----------------
def ffmpeg_exe():
    import shutil
    p = os.environ.get("FFMPEG") or shutil.which("ffmpeg")
    if p:
        return p
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def start_rec(path):
    import subprocess
    exe = ffmpeg_exe()
    if not exe:
        print("录屏：找不到 ffmpeg")
        return None
    log = open(path + ".ffmpeg.log", "w", encoding="utf-8", errors="replace")
    t0 = time.time()
    p = subprocess.Popen([exe, "-y", "-f", "gdigrab", "-framerate", "30", "-draw_mouse", "1", "-i", "desktop",
                          "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26", "-pix_fmt", "yuv420p",
                          "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-movflags", "+faststart", path],
                         stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT)
    p.t0 = t0 + 0.6          # ffmpeg 起抓大约有 0.5s 启动开销
    time.sleep(1.5)
    print("录屏开始", path, "pid", p.pid, flush=True)
    return p


def stop_rec(p):
    if not p:
        return
    try:
        p.stdin.write(b"q")
        p.stdin.flush()
        p.stdin.close()
    except Exception:
        pass
    try:
        p.wait(30)
    except Exception:
        p.kill()


# ---------------- 键盘（菜单导航） ----------------
def key(vk, times=1):
    import ctypes
    for _ in range(times):
        ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
        time.sleep(0.05)
        ctypes.windll.user32.keybd_event(vk, 0, 2, 0)
        time.sleep(0.25)


VK_UP, VK_DOWN, VK_RETURN, VK_ESCAPE = 0x26, 0x28, 0x0D, 0x1B


# ---------------- Electron 主进程 Node 调试口（拿 Tray 对象弹托盘菜单） ----------------
class NodeInspector:
    def __init__(self, port):
        import websocket
        lst = json.loads(urllib.request.build_opener(urllib.request.ProxyHandler({}))
                         .open("http://127.0.0.1:%d/json/list" % port, timeout=5).read())
        self.ws = websocket.create_connection(lst[0]["webSocketDebuggerUrl"], timeout=15, suppress_origin=True)
        self.i = 0

    def call(self, method, **params):
        self.i += 1
        self.ws.send(json.dumps({"id": self.i, "method": method, "params": params}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == self.i:
                if "error" in m:
                    raise RuntimeError(m["error"])
                return m["result"]

    def on_tray(self, fn_decl):
        """对主进程里唯一的 Tray 实例执行 function(){...}（this=tray），返回值按 JSON 带回。"""
        r = self.call("Runtime.evaluate", expression="(process.mainModule || module).require('electron').Tray.prototype")
        proto = r["result"]["objectId"]
        objs = self.call("Runtime.queryObjects", prototypeObjectId=proto)["objects"]["objectId"]
        r = self.call("Runtime.callFunctionOn", objectId=objs, returnByValue=True,
                      functionDeclaration="function(){ const t=this[0]; if(!t) return {err:'no tray'}; return (" + fn_decl + ").call(t); }")
        return r["result"].get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass
