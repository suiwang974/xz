# -*- coding: utf-8 -*-
"""WP2 提速引擎端到端测试：core/speed.py（run_http / run_direct）+ sdl_speed，对本地 HTTP 服务器实测，
并与 aria2c 命令行（-x16 -s16 -k1M，与内核 aria2 直链参数同档）对比速度。

用法（在 v3 目录）：
  python3 tests/speed_e2e.py                 # 全部用例
  python3 tests/speed_e2e.py perconn slow    # 只跑指定用例
  python3 tests/speed_e2e.py --no-aria2      # 不跑 aria2c 对比
  python3 tests/speed_e2e.py --out r.json    # 结果合并写进 JSON（默认 /tmp/sdl_speed_e2e.json）
aria2c：环境变量 ARIA2C，否则 PATH，否则 /workspace/work/sdl/speed/tools/aria2c。
不加载 Tk 内核：用 FakeEng 提供 core/speed.py 用到的 Engine 属性（cfg/gbucket/_buckets/log/_rpc_lock…）。
"""
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost,::1"

from core import speed as SP          # noqa: E402
from core import ratelimit as RL      # noqa: E402

MB = 1024 * 1024
RESULTS = {}
FAILS = []
PASSES = [0]


def check(cond, what):
    if cond:
        PASSES[0] += 1
        print("  ✓", what)
    else:
        FAILS.append(what)
        print("  ✗", what)


# ------------------------------------------------------------------ 本地测试服务器
class _QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass      # 客户端取消/踢慢连接时的 reset 是预期行为


class Server:
    """cfg: size, rate(每连接 B/s，0 不限), norange, slow_every/slow_rate(每 N 个请求有 1 个慢),
    require({头:值}), redirect_cookie(302 时下发的 Cookie)。路径：/f 文件，/r 302→/f，/gone 404，/bad 400。"""

    def __init__(self, **cfg):
        self.cfg = cfg
        self.data = random.Random(cfg.get("seed", 7)).randbytes(cfg.get("size", 8 * MB))
        self.sha = hashlib.sha256(self.data).hexdigest()
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.requests = 0
        self.file_gets = 0
        self.sent = 0
        self.seen = []
        srv = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                with srv.lock:
                    srv.requests += 1
                    srv.seen.append(dict(self.headers.items()))
                p = self.path.split("?")[0]
                if p == "/gone":
                    return self._simple(404)
                if p == "/bad":
                    return self._simple(400)
                if p == "/r":
                    self.send_response(302)
                    self.send_header("Location", "/f")
                    ck = srv.cfg.get("redirect_cookie")
                    if ck:
                        self.send_header("Set-Cookie", ck + "; Path=/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if p != "/f":
                    return self._simple(404)
                for k, v in (srv.cfg.get("require") or {}).items():
                    got = self.headers.get(k) or ""
                    if v not in got:
                        return self._simple(403)
                with srv.lock:
                    srv.file_gets += 1
                    n = srv.file_gets
                    srv.active += 1
                    srv.max_active = max(srv.max_active, srv.active)
                try:
                    self._serve(n)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    with srv.lock:
                        srv.active -= 1

            def _simple(self, code):
                body = b"x"
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _serve(self, n):
                data = srv.data
                size = len(data)
                a, b = 0, size - 1
                rng = self.headers.get("Range")
                partial = False
                if rng and not srv.cfg.get("norange") and rng.startswith("bytes="):
                    s, _, e = rng[6:].partition("-")
                    a = int(s or 0)
                    b = int(e) if e else size - 1
                    b = min(b, size - 1)
                    partial = True
                self.send_response(206 if partial else 200)
                if partial:
                    self.send_header("Content-Range", "bytes %d-%d/%d" % (a, b, size))
                if not srv.cfg.get("norange"):
                    self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(b - a + 1))
                self.send_header("ETag", '"e2e-%d"' % size)
                self.end_headers()
                rate = srv.cfg.get("rate") or 0
                se = srv.cfg.get("slow_every")
                if se and n % se == se - 1:
                    rate = srv.cfg.get("slow_rate", 32 * 1024)
                pos, t0, sent = a, time.monotonic(), 0
                blk = 16 * 1024 if rate else 256 * 1024
                while pos <= b:
                    chunk = data[pos:min(b + 1, pos + blk)]
                    self.wfile.write(chunk)
                    pos += len(chunk)
                    sent += len(chunk)
                    with srv.lock:
                        srv.sent += len(chunk)
                    if rate:
                        lag = sent / rate - (time.monotonic() - t0)
                        if lag > 0:
                            time.sleep(lag)

        self.httpd = _QuietServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.base = "http://127.0.0.1:%d" % self.port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class FakeEng:
    def __init__(self, cfg=None):
        self.cfg = dict(cfg or {})
        self.gbucket = RL.TokenBucket(0)
        self._buckets = {}
        self._rpc_lock = threading.RLock()
        self.logs = []

    def log(self, m):
        self.logs.append(m)

    def _rpc_is_waf_host(self, url):
        return False

    def _auto_cookie_for(self, url, prof=None):
        return "", ""

    def _rpc_referer_guess(self, url):
        return None

    def _rpc_query_filename(self, url):
        return ""


def sha_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(4 * MB), b""):
            h.update(b)
    return h.hexdigest()


def aria2c_bin():
    for c in (os.environ.get("ARIA2C"), shutil.which("aria2c"), "/workspace/work/sdl/speed/tools/aria2c"):
        if c and os.path.isfile(c):
            return c
    return None


def run_aria2(url, d, name, headers=None, timeout=110):
    a2 = aria2c_bin()
    if not a2 or NO_ARIA2:
        return None
    cmd = [a2, "--no-conf", "-x16", "-s16", "-k1M", "--file-allocation=none", "--allow-overwrite=true",
           "--auto-file-renaming=false", "--summary-interval=0", "--console-log-level=error",
           "--retry-wait=1", "-d", d, "-o", name, url]
    for k, v in (headers or {}).items():
        cmd[1:1] = ["--header=%s: %s" % (k, v)]
    t0 = time.monotonic()
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    el = time.monotonic() - t0
    p = os.path.join(d, name)
    return {"rc": r.returncode, "sec": round(el, 2), "path": p if os.path.exists(p) else None}


def sdl(eng, url, d, name, headers=None, task=None, prog=None, cancel=None):
    t0 = time.monotonic()
    p = SP.run_http(eng, url, d, name, headers or {"User-Agent": "e2e"}, prog=prog, cancel=cancel, task=task)
    return p, time.monotonic() - t0


def rec(case, **kw):
    RESULTS.setdefault(case, {}).update(kw)


def mbps(size, sec):
    return round(size / MB / max(sec, 1e-6), 2)


# ------------------------------------------------------------------ 用例
def case_perconn(d):
    print("[perconn] 48 MB，服务器每连接限 512 KB/s")
    s = Server(size=48 * MB, rate=512 * 1024)
    try:
        eng = FakeEng()
        p, el = sdl(eng, s.base + "/f", d, "a.bin")
        check(sha_file(p) == s.sha, "sha256 一致")
        check(not os.path.exists(p + ".sdl") and not os.path.exists(p + ".sdlpart"), "完成后无 .sdl/.sdlpart 残留")
        check(s.max_active > 16, "连接数自动加到 16 以上（最多 %d）" % s.max_active)
        rec("perconn", sdl_sec=round(el, 2), sdl_MBps=mbps(48 * MB, el), sdl_max_conn=s.max_active)
    finally:
        s.stop()
    s = Server(size=48 * MB, rate=512 * 1024)
    try:
        r = run_aria2(s.base + "/f", d, "a2.bin")
        if r:
            check(r["rc"] == 0 and r["path"] and sha_file(r["path"]) == s.sha, "aria2c 对照完成且 sha 一致")
            rec("perconn", aria2_sec=r["sec"], aria2_MBps=mbps(48 * MB, r["sec"]), aria2_max_conn=s.max_active)
    finally:
        s.stop()
    print("   ", RESULTS.get("perconn"))


def case_slow(d):
    print("[slow] 16 MB，每连接 1 MB/s，每 4 个连接有 1 个只有 32 KB/s（慢速动态再分片）")
    s = Server(size=16 * MB, rate=1 * MB, slow_every=4, slow_rate=32 * 1024)
    try:
        eng = FakeEng()
        p, el = sdl(eng, s.base + "/f", d, "s.bin")
        check(sha_file(p) == s.sha, "sha256 一致")
        dem = [x for x in eng.logs if "踢慢连接" in x]
        check(el < 20, "慢连接没拖到最后（%.1fs < 20s（朴素不踢慢连接需 500s+，偶发 17s 属负载抖动））" % el)
        rec("slow", sdl_sec=round(el, 2), sdl_MBps=mbps(16 * MB, el), sdl_log=(dem[-1] if dem else ""))
    finally:
        s.stop()
    s = Server(size=16 * MB, rate=1 * MB, slow_every=4, slow_rate=32 * 1024)
    try:
        r = run_aria2(s.base + "/f", d, "s2.bin")
        if r:
            check(r["rc"] == 0 and r["path"] and sha_file(r["path"]) == s.sha, "aria2c 对照完成且 sha 一致")
            rec("slow", aria2_sec=r["sec"], aria2_MBps=mbps(16 * MB, r["sec"]))
    finally:
        s.stop()
    print("   ", RESULTS.get("slow"))


def case_resume(d):
    print("[resume] 16 MB 每连接 256 KB/s：下到 ~35% 取消 → .sdl/.sdlpart 保留 → 再跑续传")
    s = Server(size=16 * MB, rate=256 * 1024)
    try:
        eng = FakeEng()
        got = {"n": 0}

        def prog(x):
            got["n"] = x["downloaded_bytes"]
        flag = {"c": False}

        def watcher():
            while got["n"] < 0.35 * 16 * MB:
                time.sleep(0.05)
            flag["c"] = True
        threading.Thread(target=watcher, daemon=True).start()
        out = os.path.join(d, "r.bin")
        try:
            sdl(eng, s.base + "/f", d, "r.bin", prog=prog, cancel=lambda: flag["c"])
            check(False, "取消应抛中止异常")
        except SP.abort_exc():
            check(True, "取消抛中止异常（_AbortSignal 同类）")
        check(os.path.exists(out + ".sdl") and os.path.exists(out + ".sdlpart"), ".sdl + .sdlpart 保留")
        first = s.sent
        time.sleep(0.3)
        first = s.sent
        p, el = sdl(eng, s.base + "/f", d, "r.bin")
        check(sha_file(p) == s.sha, "续传后 sha256 一致")
        extra = s.sent - first
        check(extra < 0.8 * 16 * MB, "续传只补剩余部分（第二次下载 %.1f MB / 共 16 MB）" % (extra / MB))
        check(any("续传" in x for x in eng.logs), "日志标明从 .sdl 续传")
        rec("resume", first_MB=round(first / MB, 1), second_MB=round(extra / MB, 1), second_sec=round(el, 2))
    finally:
        s.stop()
    print("   ", RESULTS.get("resume"))


def case_norange(d):
    print("[norange] 8 MB，服务器不认 Range（只回 200），每连接 4 MB/s")
    s = Server(size=8 * MB, rate=4 * MB, norange=True)
    try:
        eng = FakeEng()
        p, el = sdl(eng, s.base + "/f", d, "n.bin")
        check(sha_file(p) == s.sha, "sha256 一致")
        check(s.file_gets == 1 and s.max_active == 1, "单流下载（文件请求 %d 次）" % s.file_gets)
        rec("norange", sdl_sec=round(el, 2), sdl_MBps=mbps(8 * MB, el))
    finally:
        s.stop()
    s = Server(size=8 * MB, rate=4 * MB, norange=True)
    try:
        r = run_aria2(s.base + "/f", d, "n2.bin")
        if r:
            check(r["rc"] == 0 and r["path"] and sha_file(r["path"]) == s.sha, "aria2c 对照完成且 sha 一致")
            rec("norange", aria2_sec=r["sec"], aria2_MBps=mbps(8 * MB, r["sec"]))
    finally:
        s.stop()
    print("   ", RESULTS.get("norange"))


def case_redirect(d):
    print("[302] /r 302→/f，302 下发 Cookie，终点要求 X-Token + Cookie + Referer")
    s = Server(size=8 * MB, rate=1 * MB, redirect_cookie="sid=abc",
               require={"X-Token": "t0k", "Cookie": "sid=abc", "Referer": "https://ref.example/"})
    try:
        eng = FakeEng()
        h = {"User-Agent": "e2e-UA", "X-Token": "t0k", "Referer": "https://ref.example/"}
        p, el = sdl(eng, s.base + "/r", d, "x.bin", headers=h)
        check(sha_file(p) == s.sha, "sha256 一致")
        fh = [x for x in s.seen if x.get("Range")]
        check(fh and all(x.get("User-Agent") == "e2e-UA" for x in fh), "所有分段都带原 UA")
        check(s.max_active > 1, "302 后仍多连接（最多 %d）" % s.max_active)
        rec("redirect", sdl_sec=round(el, 2), sdl_MBps=mbps(8 * MB, el), max_conn=s.max_active)
    finally:
        s.stop()
    print("   ", RESULTS.get("redirect"))


def case_limit(d):
    print("[limit] 全局限速 / 单任务限速 / 运行中解除限速（服务器不限速）")
    s = Server(size=12 * MB)
    try:
        eng = FakeEng()
        eng.gbucket.set_rate(3 * MB)
        p, el = sdl(eng, s.base + "/f", d, "g.bin")     # 不给 prog：限速不依赖进度回调
        check(sha_file(p) == s.sha, "全局限速 sha256 一致")
        check(3.2 <= el <= 5.6, "全局 3 MB/s：12 MB 用时 %.2fs（理论 4.0s）" % el)
        rec("limit", global_3MBps_sec=round(el, 2), global_MBps=mbps(12 * MB, el))
    finally:
        s.stop()
    s = Server(size=8 * MB)
    try:
        eng = FakeEng()
        t = {"gid": "g1", "limit_kbps": 2048}
        p, el = sdl(eng, s.base + "/f", d, "t.bin", task=t)
        check(sha_file(p) == s.sha, "单任务限速 sha256 一致")
        check(3.2 <= el <= 5.6, "单任务 2 MB/s：8 MB 用时 %.2fs（理论 4.0s）" % el)
        check("g1" in eng._buckets, "单任务桶与内核共用 eng._buckets[gid]")
        rec("limit", task_2MBps_sec=round(el, 2), task_MBps=mbps(8 * MB, el))
    finally:
        s.stop()
    s = Server(size=24 * MB)
    try:
        eng = FakeEng()
        eng.gbucket.set_rate(1 * MB)
        threading.Timer(2.0, lambda: eng.gbucket.set_rate(0)).start()
        p, el = sdl(eng, s.base + "/f", d, "u.bin")
        check(sha_file(p) == s.sha, "运行中解除限速 sha256 一致")
        check(el < 6, "2s 后解除 1 MB/s 限速：24 MB 用时 %.2fs（不解除需 24s）" % el)
        rec("limit", unlimit_after_2s_sec=round(el, 2))
    finally:
        s.stop()
    print("   ", RESULTS.get("limit"))


def case_cancel(d):
    print("[cancel] 取消响应时间（限速中也要快）")
    s = Server(size=16 * MB, rate=256 * 1024)
    try:
        eng = FakeEng()
        eng.gbucket.set_rate(512 * 1024)
        flag = {"c": False, "at": None}

        def go():
            flag["at"] = time.monotonic()
            flag["c"] = True
        threading.Timer(1.5, go).start()
        try:
            sdl(eng, s.base + "/f", d, "c.bin", cancel=lambda: flag["c"])
            check(False, "取消应抛中止异常")
        except SP.abort_exc():
            lat = time.monotonic() - flag["at"]
            check(lat < 1.0, "取消 %.2fs 内停下" % lat)
            rec("cancel", latency_sec=round(lat, 2))
        time.sleep(0.5)
        check(s.active == 0, "取消后服务器端连接全部断开")
        check(os.path.exists(os.path.join(d, "c.bin.sdl")), "取消保留 .sdl 以便续传")
    finally:
        s.stop()
    print("   ", RESULTS.get("cancel"))


def case_errors(d):
    print("[errors] 404 直接失败 / 400 交回 aria2 / 过期直链 / aria2 进度在 / 回环直连 / 网盘上限")
    s = Server(size=4 * MB, rate=2 * MB)
    try:
        eng = FakeEng()
        t0 = time.monotonic()
        try:
            sdl(eng, s.base + "/gone", d, "g.bin")
            check(False, "404 应失败")
        except SP.Fallback:
            check(False, "404 不应 Fallback")
        except RuntimeError as e:
            check(time.monotonic() - t0 < 3, "404 直接失败（%.2fs）：%s" % (time.monotonic() - t0, str(e)[:40]))
        check(not os.path.exists(os.path.join(d, "g.bin.sdl")), "404 不留 .sdl")
        try:
            sdl(eng, s.base + "/bad", d, "b.bin")
            check(False, "400 应 Fallback")
        except SP.Fallback as e:
            check(True, "400 → Fallback（%s）" % str(e)[:40])
        check(not os.path.exists(os.path.join(d, "b.bin.sdl")), "Fallback 前清掉 .sdl")
        # 过期直链：run_direct 预检，不发请求
        n0 = s.requests
        t = {"uri": s.base + "/f?Expires=1600000000", "dir": d, "name": "e.bin", "gid": "e"}
        try:
            SP.run_direct(eng, t, None, {})
            check(False, "过期应失败")
        except RuntimeError as e:
            check(s.requests == n0 and "过期" in str(e), "过期直链预检失败且没发请求")
        # aria2 进度在 → 交回
        open(os.path.join(d, "a.bin.aria2"), "w").close()
        try:
            sdl(eng, s.base + "/f", d, "a.bin")
            check(False, "有 .aria2 应 Fallback")
        except SP.Fallback:
            check(True, "已有 aria2 续传进度 → Fallback")
        # 回环直连：环境变量设个不存在的代理，回环地址仍直连
        old = {k: os.environ.get(k) for k in ("HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy")}
        os.environ["HTTP_PROXY"] = os.environ["http_proxy"] = "http://127.0.0.1:9"
        os.environ.pop("NO_PROXY", None)
        os.environ.pop("no_proxy", None)
        try:
            p, _ = sdl(eng, s.base + "/f", d, "lo.bin")
            check(sha_file(p) == s.sha, "回环地址无视环境代理直连")
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    finally:
        s.stop()
    # 网盘规则：伪装 127.0.0.1 命中蓝奏云规则（conn=4）→ 连接上限 4 + 自动补 Referer
    s = Server(size=12 * MB, rate=256 * 1024, require={"Referer": "woozooo.com"})
    nd = SP.ND
    orig = nd.match_profile
    lz = [x for x in nd.PROFILES if x["key"] == "lanzou"][0]
    nd.match_profile = lambda u: lz if "127.0.0.1" in (u or "") else orig(u)
    try:
        eng = FakeEng()
        t = {"uri": s.base + "/f", "dir": d, "name": "nd.bin", "gid": "nd"}
        p = SP.run_direct(eng, t, None, {"User-Agent": "e2e"})
        check(sha_file(p) == s.sha, "网盘规则 sha256 一致")
        check(s.max_active <= 4, "遵守网盘连接上限 4（实际最多 %d）" % s.max_active)
        check(t.get("conns_eff") and int(t["conns_eff"]) <= 4, "任务记录有效连接数 conns_eff=%s" % t.get("conns_eff"))
        rec("netdisk", max_conn=s.max_active)
    finally:
        nd.match_profile = orig
        s.stop()


CASES = {"perconn": case_perconn, "slow": case_slow, "resume": case_resume, "norange": case_norange,
         "redirect": case_redirect, "limit": case_limit, "cancel": case_cancel, "errors": case_errors}
NO_ARIA2 = False


def main(argv):
    global NO_ARIA2
    out = "/tmp/sdl_speed_e2e.json"
    names = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--no-aria2":
            NO_ARIA2 = True
        elif a == "--out":
            i += 1
            out = argv[i]
        else:
            names.append(a)
        i += 1
    names = names or list(CASES)
    if not SP.available():
        print("sdl_speed 不可用")
        return 2
    for n in names:
        d = tempfile.mkdtemp(prefix="sdle2e_")
        try:
            CASES[n](d)
        except Exception as e:
            import traceback
            traceback.print_exc()
            FAILS.append("%s 异常：%s" % (n, e))
        finally:
            shutil.rmtree(d, ignore_errors=True)
    old = {}
    if os.path.exists(out):
        try:
            old = json.load(open(out, encoding="utf-8"))
        except Exception:
            old = {}
    old.update(RESULTS)
    old["_env"] = {"cpus": os.cpu_count(), "aria2c": aria2c_bin(), "time": time.strftime("%Y-%m-%d %H:%M")}
    json.dump(old, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n结果 %d 通过 / %d 失败 → %s" % (PASSES[0], len(FAILS), out))
    for f in FAILS:
        print("  FAIL:", f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
