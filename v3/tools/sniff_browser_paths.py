# -*- coding: utf-8 -*-
"""「嗅探用浏览器」两条路径（Edge / Chrome）+ 自动选择 的真跑验收。
    python tools/sniff_browser_paths.py
Linux 验收机没有真 Edge/Chrome（无 arm64 版）：/opt/microsoft/msedge/msedge 与 /opt/google/chrome/chrome
放的是转发到系统 Chromium 的壳脚本，并把自己的路径写进 /tmp/brlaunch.log —— 用来证明 Playwright
确实按 channel 拉起了对应路径。真 Windows 上 Playwright 找的是
  Edge: %ProgramFiles(x86)%\\Microsoft\\Edge\\Application\\msedge.exe
  Chrome: %ProgramFiles%\\Google\\Chrome\\Application\\chrome.exe
"""
import functools, http.server, os, shutil, sys, threading
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); os.chdir(ROOT)
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
import video_downloader as V  # noqa: E402

PAGE = b"""<html><body><video src="/media/test.mp4" autoplay muted></video>
<script>fetch('/media/list.m3u8').catch(()=>{})</script></body></html>"""


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/":
            body, ct = PAGE, "text/html"
        elif self.path.endswith(".m3u8"):
            body, ct = b"#EXTM3U\n#EXT-X-ENDLIST\n", "application/vnd.apple.mpegurl"
        else:
            body, ct = b"\x00" * 2048, "video/mp4"
        self.send_response(200); self.send_header("Content-Type", ct); self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = "http://127.0.0.1:%d/" % srv.server_address[1]
LOG = "/tmp/brlaunch.log"
R = []


def run(pref, expect, note=""):
    V.SNIFF_BROWSER[0] = pref
    V._BR_RUN_CACHE.clear()
    if os.path.exists(LOG):
        os.remove(LOG)
    lines = []
    order = [V._browser_label(c) for c in V._browser_channel_order()]
    res = V.run_sniff(URL, lines.append)
    launched = open(LOG).read().split() if os.path.exists(LOG) else []
    first = launched[0] if launched else ""
    okb = expect in first and V._LAST_BROWSER[0] == {"msedge": "Edge", "chrome": "Chrome"}[expect]
    okm = any("test.mp4" in l or "list.m3u8" in l for l in lines)
    R.append(okb and okm)
    print(("PASS" if okb and okm else "FAIL"), "设置=%s %s 顺序=%s 实际拉起=%s 用=%s 嗅到媒体=%s" % (pref, note, "→".join(order), first, V._LAST_BROWSER[0], okm))


EDGE, CHROME = "/opt/microsoft/msedge/msedge", "/opt/google/chrome/chrome"
run("自动", "msedge", "(两者都装、都没在跑 → Edge 优先)")
run("Edge", "msedge")
run("Chrome", "chrome")
# 自动 + 只有 Chrome 在跑 → Chrome 优先（模拟用户正在用 Chrome）
orig = V._browser_running
V._browser_running = lambda ch: ch == "chrome"
run("自动", "chrome", "(Chrome 正在运行 → Chrome 优先)")
V._browser_running = orig
# 自动 + 没装 Edge → Chrome
os.rename(EDGE, EDGE + ".off") if os.access(os.path.dirname(EDGE), os.W_OK) else None
moved = os.path.exists(EDGE + ".off")
if not moved:
    orig_inst = V._browser_installed
    V._browser_installed = lambda ch: ch == "chrome"
run("自动", "chrome", "(未安装 Edge → Chrome)")
if moved:
    os.rename(EDGE + ".off", EDGE)
else:
    V._browser_installed = orig_inst
print("RESULT %d/%d" % (sum(R), len(R)))
sys.exit(0 if all(R) else 1)
