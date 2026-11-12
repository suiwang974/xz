# -*- coding: utf-8 -*-
"""A11：设置 remote_time（保留服务器文件时间）生效：开→文件 mtime = Last-Modified；关→mtime 是现在。
用法同 bt_meta_timeout_test.py"""
import os, sys, time, shutil, subprocess, threading, http.server
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "tests"))
import core_e2e as E
R = []
def chk(n, c, d=""):
    R.append(bool(c)); print(("PASS " if c else "FAIL ") + n + ("" if c else "  ← %s" % str(d)[:300]), flush=True)
LM = "Wed, 21 Oct 2015 07:28:00 GMT"; LM_TS = 1445412480
BODY = b"x" * (3 * 1024 * 1024)
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _h(self, code=200, rng=None):
        self.send_response(code); self.send_header("Last-Modified", LM); self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", "application/octet-stream")
        if rng: self.send_header("Content-Range", "bytes %d-%d/%d" % (rng[0], rng[1], len(BODY)))
        n = (rng[1] - rng[0] + 1) if rng else len(BODY)
        self.send_header("Content-Length", str(n)); self.end_headers()
    def do_HEAD(self): self._h()
    def do_GET(self):
        rg = self.headers.get("Range"); a, b = 0, len(BODY) - 1
        if rg and rg.startswith("bytes="):
            x, y = rg[6:].split("-"); a = int(x or 0); b = int(y) if y else b
            self._h(206, (a, b))
        else: self._h()
        try: self.wfile.write(BODY[a:b + 1])
        except Exception: pass
def main():
    E.DATA = "/tmp/sdlv3_rtime"; E.OUT = os.path.join(E.DATA, "out")
    shutil.rmtree(E.DATA, ignore_errors=True); os.makedirs(E.OUT)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H); fp = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    E.PORT[0] = E.free_port()
    env = dict(os.environ, SNIFFDL_DRYRUN_OS="1", NO_PROXY="127.0.0.1,localhost"); env.pop("DISPLAY", None)
    core = subprocess.Popen([sys.executable, os.path.join(HERE, "tests", "ui_smoke_core.py"), "--data-dir", E.DATA,
                             "--port", str(E.PORT[0]), "--runtime", os.path.join(E.DATA, "runtime.json"),
                             "--ui-token", E.TOKEN], cwd=HERE, env=env,
                            stdout=open(os.path.join(E.DATA, "core.out"), "w"), stderr=subprocess.STDOUT)
    try:
        chk("内核启动", E.wait(lambda: E.raw_rpc("sniffdl.ping", [], token=None).get("result"), 30))
        for eng in ("aria2", "sdl"):
            for on in (True, False):
                E.setv(outdir=E.OUT, clip_watch=False, notify=False, auto_category=False, remote_time=on,
                       speed_engine=eng, conflict="覆盖")
                name = "rt_%s_%d.bin" % (eng, on)
                r = E.rpc("sniffdl.task.add", {"url": "http://127.0.0.1:%d/%s" % (fp, name), "name": name, "dir": E.OUT})
                gid = r if isinstance(r, str) else (r or {}).get("gid")
                def fin():
                    for t in E.rpc("sniffdl.tasks"):
                        if t["gid"] == gid: return t["status"] in ("complete", "error")
                E.wait(fin, 40, 0.5)
                p = os.path.join(E.OUT, name)
                ok = os.path.isfile(p) and os.path.getsize(p) == len(BODY)
                mt = os.path.getmtime(p) if ok else 0
                if on: chk("引擎=%s remote_time=开：mtime=服务器 Last-Modified" % eng, ok and abs(mt - LM_TS) <= 2, mt)
                else: chk("引擎=%s remote_time=关：mtime 是下载时刻" % eng, ok and time.time() - mt < 120, mt)
    finally:
        core.terminate()
    print("%d/%d 通过" % (sum(R), len(R))); return 0 if all(R) else 1
sys.exit(main())
