# -*- coding: utf-8 -*-
"""POST 下载端到端：真内核（tests/ui_smoke_core.py）+ 本地服务器，经 sniffdl.task.add 带 method/post_data/headers。

  multi   只认 POST（GET 回 405）+ 校验请求体与 Content-Type + 支持 Range、每连接限速 → 多连接下完 sha 一致，
          每个请求都带同一请求体，没有任何 GET
  once    一次性 token（请求体里的 token 只能用一次，之后 403）→ 首个响应一直读完，单流下完 sha 一致
  r303    POST → 303 → GET 真文件（按浏览器规矩改 GET、丢请求体）
  r307    POST → 307 → 仍 POST 且带原请求体
  aria2   speed_engine=aria2 时 POST 任务仍走动态分片（aria2 只会 GET）
  fail    服务器 500 → 任务失败、错误提示「POST 下载失败」，且没有任何 GET 回退请求

用法：source env 两个脚本；python3 tests/post_e2e.py。退出码 0 = 全过。
"""
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

DATA = "/tmp/sdlv3_post"
OUT = os.path.join(DATA, "out")
MB = 1024 * 1024
BODY = "export=1&fmt=bin&k=%E4%B8%AD"
LOG = []           # (path, method, has_range, body)
LK = threading.Lock()
FILES = {}
USED = set()


def mk(name, size):
    b = random.Random(len(name) * 7919 + size).randbytes(size)
    FILES[name] = (b, hashlib.sha256(b).hexdigest())


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _empty(self, code, extra=None):
        self.send_response(code)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self._h("GET")

    def do_POST(self):
        self._h("POST")

    def _h(self, meth):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        path = urllib.parse.urlparse(self.path).path
        with LK:
            LOG.append((path, meth, bool(self.headers.get("Range")), body))
        kind, _, name = path.strip("/").partition("/")
        if kind == "r303":
            return self._empty(303, {"Location": "/g/" + name})
        if kind == "r307":
            return self._empty(307, {"Location": "/p/" + name})
        if kind == "err":
            return self._empty(500)
        if name not in FILES:
            return self._empty(404)
        if kind == "g":
            if meth != "GET":
                return self._empty(405)
        elif kind in ("p", "once", "pa", "pu", "pn"):
            if meth != "POST":
                return self._empty(405)
            if body != BODY or "x-www-form-urlencoded" not in (self.headers.get("Content-Type") or ""):
                return self._empty(400)
            if kind == "once":
                with LK:
                    if name in USED:
                        return self._empty(403)
                    USED.add(name)
        else:
            return self._empty(404)
        b, sha = FILES[name]
        total = len(b)
        a, z, part = 0, total - 1, False
        rng = self.headers.get("Range") or ""
        if rng.startswith("bytes="):
            s, _, e = rng[6:].partition("-")
            a = int(s or 0)
            z = min(int(e), total - 1) if e else total - 1
            part = True
        self.send_response(206 if part else 200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        if kind == "pa":
            self.send_header("Content-Disposition", 'attachment; filename="report_ascii.csv"')
        elif kind == "pu":
            self.send_header("Content-Disposition", "attachment; filename=\"fallback.xlsx\"; filename*=UTF-8''%E6%8A%A5%E8%A1%A8%E5%AF%BC%E5%87%BA.xlsx")
        self.send_header("Content-Length", str(z - a + 1))
        if part:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (a, z, total))
        self.end_headers()
        rate = 1 * MB            # 每连接 1MB/s，逼出多连接
        pos, t0, sent = a, time.monotonic(), 0
        try:
            while pos <= z:
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


def url(kind, name):
    return "http://127.0.0.1:%d/%s/%s" % (C.FPORT[0], kind, name)


def addp(kind, name, method="POST"):
    info = {"url": url(kind, name), "name": name, "dir": OUT, "method": method, "post_data": BODY,
            "headers": {"Content-Type": "application/x-www-form-urlencoded", "Referer": "http://127.0.0.1/page"}}
    r = C.rpc("sniffdl.task.add", info)
    return r.get("gid") if isinstance(r, dict) else r


def reqs(path_sub):
    with LK:
        return [x for x in LOG if path_sub in x[0]]


def done_ok(tag, gid, name, timeout=60):
    v = C.wait_status(gid, ("complete", "error"), timeout)
    p = os.path.join(OUT, name)
    ok = bool(v and v["status"] == "complete" and os.path.isfile(p)
              and C.sha_file(p) == FILES[name][1])
    C.check("%s：完成且 sha256 一致" % tag, ok, v)
    return v


def t_multi():
    mk("multi.bin", 12 * MB)
    gid = addp("p", "multi.bin")
    v = done_ok("multi", gid, "multi.bin")
    rs = reqs("/multi.bin")
    C.check("multi：全是 POST、无 GET", rs and all(r[1] == "POST" for r in rs), rs[:5])
    C.check("multi：每个请求都带原请求体", all(r[3] == BODY for r in rs), [r[3] for r in rs][:5])
    C.check("multi：多连接（Range POST ≥3 个）", sum(1 for r in rs if r[2]) >= 3, len(rs))
    C.check("multi：任务 engine=sdl", v and v.get("engine") == "sdl", v and v.get("engine"))
    C.check("multi：无临时残留", not C.leftovers(OUT, "multi.bin"), C.leftovers(OUT, "multi.bin"))


def t_once():
    mk("once.bin", 4 * MB)
    gid = addp("once", "once.bin")
    done_ok("once", gid, "once.bin", 60)
    rs = reqs("/once.bin")
    C.check("once：首个 POST 用了 token，无 GET", rs and rs[0][1] == "POST" and all(r[1] == "POST" for r in rs), rs[:4])


def t_r303():
    mk("r303.bin", 4 * MB)
    gid = addp("r303", "r303.bin")
    done_ok("r303", gid, "r303.bin")
    g = reqs("/g/r303.bin")
    C.check("r303：跳转后改 GET、不带请求体", g and all(r[1] == "GET" and r[3] == "" for r in g), g[:4])


def t_r307():
    mk("r307.bin", 4 * MB)
    gid = addp("r307", "r307.bin")
    done_ok("r307", gid, "r307.bin")
    p = reqs("/p/r307.bin")
    C.check("r307：跳转后仍 POST 且带请求体", p and all(r[1] == "POST" and r[3] == BODY for r in p), p[:4])


def t_aria2():
    C.setv(speed_engine="aria2")
    try:
        mk("a2.bin", 4 * MB)
        gid = addp("p", "a2.bin")
        v = done_ok("aria2 模式", gid, "a2.bin")
        C.check("aria2 模式：POST 仍走动态分片", v and v.get("engine") == "sdl", v and v.get("engine"))
        C.check("aria2 模式：无 GET", all(r[1] == "POST" for r in reqs("/a2.bin")), reqs("/a2.bin")[:4])
    finally:
        C.setv(speed_engine="auto")


def addn(kind, name):
    info = {"url": url(kind, name), "dir": OUT, "method": "POST", "post_data": BODY,
            "headers": {"Content-Type": "application/x-www-form-urlencoded"}}
    r = C.rpc("sniffdl.task.add", info)
    return r.get("gid") if isinstance(r, dict) else r


def t_name():
    for kind, key, want in (("pa", "nameA", "report_ascii.csv"), ("pu", "nameU", "报表导出.xlsx"), ("pn", "nameN", None)):
        mk(key, 3 * MB)
        gid = addn(kind, key)
        v = C.wait_status(gid, ("complete", "error"), 60)
        want = want or (key + ".bin")
        p = os.path.join(OUT, want)
        C.check("name %s：文件名 %s 且 sha 一致" % (kind, want),
                bool(v and v["status"] == "complete" and os.path.isfile(p) and C.sha_file(p) == FILES[key][1]),
                (v, os.listdir(OUT)))


def t_fail():
    gid = addp("err", "bad.bin")
    v = C.wait_status(gid, ("complete", "error"), 40)
    C.check("fail：任务失败", v and v["status"] == "error", v)
    msg = json.dumps(v, ensure_ascii=False) if v else ""
    C.check("fail：提示 POST 下载失败", "POST" in msg, msg[:300])
    rs = reqs("/bad.bin")
    C.check("fail：没有 GET 回退", rs and all(r[1] == "POST" for r in rs), rs)


def t_icpt():
    """扩展接管路径：sniffdl.intercept 带 method/post_data/headers（快捷直连）"""
    mk("icpt.bin", 5 * MB)
    gid = C.rpc("sniffdl.intercept", {"url": url("p", "icpt.bin"), "filename": "icpt.bin", "outdir": OUT,
                                      "source": "popup", "confirm": False, "method": "POST", "post_data": BODY,
                                      "headers": {"Content-Type": "application/x-www-form-urlencoded"}})
    C.check("icpt：返回 gid", bool(gid) and gid != "pending", gid)
    done_ok("icpt", gid, "icpt.bin")
    rs = reqs("/icpt.bin")
    C.check("icpt：全 POST 且带请求体、无 GET", rs and all(r[1] == "POST" and r[3] == BODY for r in rs), rs[:4])


TESTS = [("multi", t_multi), ("once", t_once), ("r303", t_r303), ("r307", t_r307), ("aria2", t_aria2), ("icpt", t_icpt), ("name", t_name),
         ("fail", t_fail)]


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
