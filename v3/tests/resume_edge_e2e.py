# -*- coding: utf-8 -*-
"""断点续传异常场景 e2e：norange/drop/flaky503/etag 四组。python3 tests/resume_edge_e2e.py"""
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core_e2e as C  # noqa: E402  复用 rpc/wait/check 等


DATA = "/tmp/sdlv3_resume"
OUT = os.path.join(DATA, "out")
MB = 1024 * 1024
FILES = {}
LK = threading.Lock()
STATE = {}
LOG = []


def mk(name, size, seed=0):
    b = random.Random(len(name) * 7919 + size + seed).randbytes(size)
    FILES[name] = (b, hashlib.sha256(b).hexdigest())


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        kind, _, name = path.strip("/").partition("/")
        with LK:
            LOG.append((kind, name, self.headers.get("Range")))
            n = STATE[name] = STATE.get(name, 0) + 1
        if name not in FILES:
            self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers(); return
        if kind == "flaky" and n == 1:
            self.send_response(503); self.send_header("Content-Length", "0"); self.end_headers(); return
        b = FILES[name][0]
        total = len(b)
        a, z, part = 0, total - 1, False
        rng = self.headers.get("Range") or ""
        if rng.startswith("bytes=") and kind != "norange":
            s, _, e = rng[6:].partition("-")
            a = int(s or 0)
            z = min(int(e), total - 1) if e else total - 1
            part = True
        self.send_response(206 if part else 200)
        self.send_header("Content-Type", "application/octet-stream")
        if kind != "norange":
            self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", '"%s"' % FILES[name][1][:16])
        self.send_header("Content-Length", str(z - a + 1))
        if part:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (a, z, total))
        self.end_headers()
        rate = 2 * MB if kind in ("etag", "norange") else 8 * MB
        pos, t0, sent = a, time.monotonic(), 0
        try:
            while pos <= z:
                if kind == "drop" and n == 1 and sent >= 3 * MB:
                    self.close_connection = True
                    self.connection.shutdown(2)
                    return
                piece = b[pos:min(z + 1, pos + 65536)]
                self.wfile.write(piece)
                pos += len(piece)
                sent += len(piece)
                ahead = sent / float(rate) - (time.monotonic() - t0)
                if ahead > 0:
                    time.sleep(ahead)
        except OSError:
            self.close_connection = True


class Srv(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, *a):
        pass


def add(kind, name):
    r = C.rpc("sniffdl.task.add", {"url": "http://127.0.0.1:%d/%s/%s" % (C.FPORT[0], kind, name), "name": name, "dir": OUT})
    return r.get("gid") if isinstance(r, dict) else r


def fin(tag, gid, name, timeout=60):
    v = C.wait_status(gid, ("complete", "error"), timeout)
    p = os.path.join(OUT, name)
    C.check("%s：完成" % tag, bool(v and v["status"] == "complete"), v)
    C.check("%s：sha256 一致" % tag, os.path.isfile(p) and C.sha_file(p) == FILES[name][1])
    return v


def t_norange():
    mk("nr.bin", 12 * MB)
    g = add("norange", "nr.bin")
    fin("norange", g, "nr.bin")
    C.check("norange：没有残留 .sdl/.sdlpart", not [f for f in os.listdir(OUT) if f.startswith("nr.bin.")], os.listdir(OUT))
    C.check("norange：确有请求发出", STATE.get("nr.bin", 0) >= 1, STATE)


def t_drop():
    mk("dr.bin", 10 * MB)
    g = add("drop", "dr.bin")
    fin("drop", g, "dr.bin")
    C.check("drop：断线后确实重连（≥2 次请求）", STATE.get("dr.bin", 0) >= 2, STATE.get("dr.bin"))


def t_flaky():
    mk("fl.bin", 4 * MB)
    C.setv(retry_count=3)
    g = add("flaky", "fl.bin")
    fin("flaky503", g, "fl.bin")
    C.check("flaky503：重试过（≥2 次请求）", STATE.get("fl.bin", 0) >= 2, STATE.get("fl.bin"))
    C.setv(retry_count=0)


def t_etag():
    mk("et.bin", 16 * MB)
    C.setv(speed_limit_kbps=2048)
    g = add("etag", "et.bin")
    C.wait_done_bytes(g, 2 * MB, 30)
    C.rpc("sniffdl.task.pause", [g])
    v = C.wait_status(g, ("paused",), 15)
    C.check("etag：已暂停", bool(v), v)
    mk("et.bin", 16 * MB, seed=1)
    time.sleep(1.5)          # 等引擎真停稳再解限速：否则解限速的瞬间还在飞的分片会把整个文件抢着下完
    C.setv(speed_limit_kbps=0)
    C.rpc("sniffdl.task.resume", [g])
    v = C.wait_status(g, ("complete", "error"), 90)
    p = os.path.join(OUT, "et.bin")
    if v and v["status"] == "complete":
        ok = C.wait(lambda: os.path.isfile(p) and C.sha_file(p) == FILES["et.bin"][1], 5)
        C.check("etag：换文件后结果等于新内容（无混合坏文件）", ok, "isfile=%s" % os.path.isfile(p))
    else:
        C.check("etag：换文件后报错且不留成品坏文件", bool(v) and not os.path.isfile(p), v)
    C.check("etag：任务有终态", bool(v), v)


def _win(offset_min_start, offset_min_stop):
    import datetime as dt
    n = dt.datetime.now()
    f = lambda d: (n + dt.timedelta(minutes=d)).strftime("%H:%M")
    return {"enabled": True, "start": f(offset_min_start), "stop": f(offset_min_stop), "days": []}


def t_sched():
    """真内核计划任务：窗口外不开工 → 窗口开启自动下载 → 窗口关闭暂停（sched_paused）→ 再开自动续完 sha 一致"""
    mk("sc.bin", 14 * MB)
    q = C.rpc("sniffdl.queue.save", {"name": "夜间计划", "schedule": _win(60, 120)})["queue"]
    qid = q["id"]
    r = C.rpc("sniffdl.task.add", {"url": "http://127.0.0.1:%d/etag/sc.bin" % C.FPORT[0], "name": "sc.bin", "dir": OUT, "queue": qid})
    g = r.get("gid") if isinstance(r, dict) else r
    C.check("sched：任务建在计划队列里", bool(g), r)
    time.sleep(3)
    v = C.view(g)
    C.check("sched：窗口外保持等待、没有开工", v and v["status"] in ("waiting", "paused") and not v["done"], v)
    C.check("sched：窗口外服务器没收到请求", STATE.get("sc.bin", 0) == 0, STATE.get("sc.bin"))
    # 打开窗口（现在在 [-1, +60] 分钟内）
    C.rpc("sniffdl.queue.save", dict(q, schedule=_win(-1, 60)))
    v = C.wait_done_bytes(g, 2 * MB, 30)
    C.check("sched：窗口开启后自动开始下载", bool(v), C.view(g))
    # 关闭窗口 → 暂停
    C.rpc("sniffdl.queue.save", dict(q, schedule=_win(60, 120)))
    v = C.wait(lambda: (lambda x: x if x and x["status"] in ("waiting", "paused") else None)(C.view(g)), 20)
    C.check("sched：窗口关闭后暂停", bool(v), C.view(g))
    d0 = (C.view(g) or {}).get("done", 0)
    time.sleep(2.5)
    d1 = (C.view(g) or {}).get("done", 0)
    C.check("sched：暂停后不再增长（%d→%d）" % (d0, d1), d1 - d0 < 256 * 1024, (d0, d1))
    # 再打开 → 续完
    C.rpc("sniffdl.queue.save", dict(q, schedule=_win(-1, 60)))
    fin("sched 续传", g, "sc.bin", 90)
    C.rpc("sniffdl.queue.remove", qid)


TESTS = [("norange", t_norange), ("drop", t_drop), ("flaky503", t_flaky), ("etag", t_etag), ("sched", t_sched)]


def main(argv):
    want = [a for a in argv if not a.startswith("-")] or [n for n, _ in TESTS]
    shutil.rmtree(DATA, ignore_errors=True)
    os.makedirs(OUT, exist_ok=True)
    C.OUT = OUT
    srv = Srv(("127.0.0.1", 0), H)
    C.FPORT[0] = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    C.PORT[0] = C.free_port()
    env = dict(os.environ, SNIFFDL_DRYRUN_OS="1", NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "DISPLAY"):
        env.pop(k, None)
    core = subprocess.Popen([sys.executable, os.path.join(C.HERE, "tests", "ui_smoke_core.py"), "--data-dir", DATA,
                             "--port", str(C.PORT[0]), "--runtime", os.path.join(DATA, "runtime.json"),
                             "--ui-token", C.TOKEN], cwd=C.HERE, env=env,
                            stdout=open(os.path.join(DATA, "core.out"), "w"), stderr=subprocess.STDOUT)
    t0 = time.time()
    try:
        if not C.wait(lambda: C.raw_rpc("sniffdl.ping", [], token=None).get("result"), 30):
            C.check("内核启动", False, DATA + "/core.out")
            return 1
        C.setv(outdir=OUT, clip_watch=False, notify=False, conflict="重命名", bt_dht=False, bt_lpd=False,
               bt_pex=False, bt_tracker_auto=False, max_concurrent=3, speed_engine="auto", retry_count=0,
               speed_limit_kbps=0, incomplete_dir="")
        for n, fn in TESTS:
            if n in want:
                print("== " + n, flush=True)
                try:
                    fn()
                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    C.check(n + " 异常", False, e)
    finally:
        try:
            C.raw_rpc("sniffdl.core.shutdown", [], timeout=5)
        except Exception:
            pass
        try:
            core.wait(8)
        except Exception:
            core.kill()
        srv.shutdown()
    np_ = sum(1 for _, ok in C.RESULTS if ok)
    print("\n结果：%d/%d 通过（%.0f s）" % (np_, len(C.RESULTS), time.time() - t0), flush=True)
    return 0 if np_ == len(C.RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
