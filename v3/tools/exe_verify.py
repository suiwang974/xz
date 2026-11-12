# -*- coding: utf-8 -*-
"""打包后验收：用 **exe 本体** 跑，不看"进程活着"，只看断言。

1) dist/SniffDL.exe --uicheck     → 证明 PIL/ImageTk 冻结后没静默回落纯色
2) dist/SniffDL.exe --probe <本地 mock 站> → 证明打包后的嗅探内核真能出条目
   （本地 mock 不受被墙/限流干扰，归因干净）
3) dist/SniffDL.exe --diagnose    → 自检报告里 Chromium 内核必须存在

用法：python tools/exe_verify.py
退出码 0 = 全绿。
"""
import http.server
import os
import re
import socket
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "dist", "SniffDL.exe")

FAILS = []
BASE_MTIME = float(sys.argv[1]) if len(sys.argv) > 1 else 0


def check(name, ok, detail=""):
    print("%-44s %s  %s" % (name, "PASS" if ok else "FAIL", detail), flush=True)
    if not ok:
        FAILS.append(name)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


MOCK = """<!doctype html><meta charset="utf-8"><title>mock</title>
<body><h3>mock 直链页</h3>
<video src="http://127.0.0.1:%(p)d/a.mp4" width="960" height="540" controls></video>
<a href="http://127.0.0.1:%(p)d/a.mp4">a.mp4</a>
<a href="http://127.0.0.1:%(p)d/b.mp4">b.mp4</a>
</body>"""


def serve_mock(port, stop):
    html = MOCK % {"p": port}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_a):
            pass

        def do_GET(self):
            if self.path.endswith(".mp4"):
                body = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 4096
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                body = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

    sv = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=sv.serve_forever, daemon=True).start()
    return sv


def run(cmd, timeout):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "(超时 %ds)" % timeout


def main():
    if not os.path.exists(EXE):
        check("exe 存在", False, EXE)
        return 1
    mt = os.path.getmtime(EXE)
    print("exe mtime = %s  大小 = %.1f MB" % (
        time.strftime("%m-%d %H:%M:%S", time.localtime(mt)),
        os.path.getsize(EXE) / 1048576), flush=True)
    if BASE_MTIME:
        check("exe 是本次新构建（时间戳已更新）", mt > BASE_MTIME,
              "新 %s vs 基线 %s" % (time.strftime("%H:%M:%S", time.localtime(mt)),
                                    time.strftime("%H:%M:%S", time.localtime(BASE_MTIME))))

    # ① 冻结环境的自绘管线自检
    rc, out = run([EXE, "--uicheck"], 180)
    for line in out.splitlines():
        if "PASS" in line or "FAIL" in line or "RESULT" in line:
            print("   ", line, flush=True)
    check("exe --uicheck 全绿（PIL 冻结生效）", rc == 0 and "RESULT=PASS" in out,
          "exit=%s" % rc)

    # ② 自检报告里 Chromium 必须在
    rc2, out2 = run([EXE, "--diagnose"], 180)
    low = out2.lower()
    has_browser = ("chromium" in low) or ("内核" in out2) or ("playwright" in low)
    check("exe --diagnose 有输出且报告内核", rc2 == 0 and has_browser,
          "exit=%s 片段=%r" % (rc2, out2[-160:].replace("\n", " | ")))

    # ③ 本地 mock 站嗅探（不依赖外网）
    port = free_port()
    serve_mock(port, None)
    url = "http://127.0.0.1:%d/" % port
    rc3, out3 = run([EXE, "--probe", url], 300)
    nums = [int(x) for x in re.findall(r"(\d+)\s*(?:条|个)", out3)]
    # ⚠ exe 写控制台的中文在管道里是 GBK 乱码，用中文量词做正则会稳定漏匹配（把成功判成
    #   失败）。改判 ASCII 信号：结果表行里的媒体扩展名 / "(N)" 计数。
    row = re.search(r"\(\s*(\d+)\s*\)\s*\|", out3) or re.search(
        r"\b(mp4|m3u8|webm|flv|mkv|ts)\b", out3)
    print("   probe exit=%s 计数线索=%s ASCII命中=%s" % (rc3, nums, bool(row)), flush=True)
    for line in out3.splitlines()[-14:]:
        print("   ", line, flush=True)
    check("exe --probe 本地 mock 站能嗅到条目",
          rc3 == 0 and (bool(nums) or bool(row)),
          "exit=%s nums=%s ascii=%s" % (rc3, nums, bool(row)))

    print("\nRESULT=%s" % ("PASS" if not FAILS else "FAIL(%d): %s" % (
        len(FAILS), " / ".join(FAILS))), flush=True)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
