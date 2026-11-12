# -*- coding: utf-8 -*-
"""A15 连接数上限提升（2026-11-12）：自研引擎 sdl_speed 64/128 路实测 + 设置「最大连接数」接线。

本地起 Range 服务器（每条 TCP 连接限速 200 KB/s，可选「并发 >N 回 503」和「总带宽封顶」），验证：
  1. 固定 32 / 64 / 128 路各下一遍：64 路稳态速度 ≥ 32 路 × 1.8；128 路比 64 路更快；sha256 全对。
  2. 自动模式上限 128：从 8 路自动涨到 128 且不越过；给 999 也被夹到 128；默认自动上限 = 64。
  3. 服务器并发 >20 回 503：引擎自动降（throttles>0、学到上限 ≤ 服务器允许值附近）且最终下完、sha256 对。
  4. 总带宽封顶（4 MB/s = 20 路）：加一批提升 <5% 就停并退回上一档，不会一路开到 128。
  5. core/speed 设置接线：max_conns=自动/8/…/128 → 上限；网盘覆盖 >16 只给自研引擎；aria2 侧仍夹 16；
     needs_sdl 只在明确要求 >16 时为真；设置表里有中文「最大连接数」高级项。
  6. 经 core.speed.run_http（真内核同一入口）在 max_conns=128 下真下一遍，conns_eff=128、sha256 对。

用法：source env/setup.sh && source env/v3env.sh && python3 tests/conn_cap_test.py   退出码 0 = 全过。
结果数字写 /tmp/sdl_conn_cap.json。
"""
import hashlib
import json
import os
import random
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost,::1"

import sdl_speed as SPEED                 # noqa: E402
from sdl_speed import profiles as PR      # noqa: E402
from sdl_speed import engine as SE        # noqa: E402
from core import speed as SP              # noqa: E402
from core import ratelimit as RL          # noqa: E402
from core import settings as S            # noqa: E402
import netdisk_rules as ND                # noqa: E402

MB = 1024 * 1024
KB = 1024
PER_CONN = 200 * KB
RESULTS = {}
FAILS = []
PASSES = [0]


def check(cond, what, detail=""):
    if cond:
        PASSES[0] += 1
        print("  PASS", what, flush=True)
    else:
        FAILS.append(what)
        print("  FAIL", what, ("← %s" % detail) if detail else "", flush=True)
    return cond


# ------------------------------------------------------------------ 服务器
class _Srv(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 512
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        pass


class RangeServer:
    """per_conn：每条 TCP 连接 B/s；max_active：同时在传的请求 > 它就回 503（Retry-After: 1）；
    total_rate：所有连接共享的总带宽 B/s（0 = 不封顶）。"""

    def __init__(self, size, per_conn=PER_CONN, max_active=0, total_rate=0, seed=15):
        self.data = random.Random(seed).randbytes(size)
        self.sha = hashlib.sha256(self.data).hexdigest()
        self.per_conn = per_conn
        self.max_active = max_active
        self.total_rate = total_rate
        self.lk = threading.Lock()
        self.active = 0
        self.peak = 0
        self.n503 = 0
        self.conns = 0
        self._g_next = time.monotonic()
        srv = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self):
                super().setup()
                self._next = time.monotonic()
                with srv.lk:
                    srv.conns += 1

            def log_message(self, *a):
                pass

            def _pace(self, n):
                now = time.monotonic()
                if self._next < now - 0.2:
                    self._next = now
                self._next += n / float(srv.per_conn)
                wait = self._next - now
                if srv.total_rate:
                    with srv.lk:
                        if srv._g_next < now - 0.2:
                            srv._g_next = now
                        srv._g_next += n / float(srv.total_rate)
                        wait = max(wait, srv._g_next - now)
                if wait > 0:
                    time.sleep(wait)

            def do_GET(self):
                with srv.lk:
                    srv.active += 1
                    busy = srv.max_active and srv.active > srv.max_active
                    if busy:
                        srv.n503 += 1
                    else:
                        srv.peak = max(srv.peak, srv.active)
                try:
                    if busy:
                        self.send_response(503)
                        self.send_header("Retry-After", "1")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    total = len(srv.data)
                    rg = self.headers.get("Range")
                    a, b = 0, total - 1
                    if rg and rg.startswith("bytes="):
                        x, _, y = rg[6:].partition("-")
                        a = int(x or 0)
                        b = min(total - 1, int(y)) if y else total - 1
                        self.send_response(206)
                        self.send_header("Content-Range", "bytes %d-%d/%d" % (a, b, total))
                    else:
                        self.send_response(200)
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Length", str(b - a + 1))
                    self.send_header("Content-Type", "application/octet-stream")
                    self.end_headers()
                    pos = a
                    blk = 16 * KB
                    while pos <= b:
                        n = min(blk, b - pos + 1)
                        self._pace(n)
                        self.wfile.write(srv.data[pos:pos + n])
                        pos += n
                except (BrokenPipeError, ConnectionResetError, OSError):
                    self.close_connection = True
                finally:
                    with srv.lk:
                        srv.active -= 1

        self.httpd = _Srv(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.url = "http://127.0.0.1:%d/a15.bin" % self.port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def sha_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(4 * MB), b""):
            h.update(b)
    return h.hexdigest()


def run_engine(srv, out, profile, timeout=150):
    """直接用 sdl_speed 下一遍。返回 dict(sec, MBps 全程, steady_MBps 10%→90% 稳态, stats, sha, task)。"""
    marks = []

    def cb(p):
        if p["state"] == "downloading" and p.get("total"):
            marks.append((time.monotonic(), p["bytes"], p.get("connections") or 0))

    t0 = time.monotonic()
    task = SPEED.start(srv.url, {}, out, cb, profile=profile, verify=False, trust_env=False)
    task.wait(timeout)
    el = time.monotonic() - t0
    st = task.state
    if st != "done":
        task.cancel(discard=True)
    size = len(srv.data)
    steady = None
    lo = [m for m in marks if m[1] >= size * 0.10]
    hi = [m for m in marks if m[1] >= size * 0.90]
    if lo and hi and hi[0][0] > lo[0][0]:
        steady = (hi[0][1] - lo[0][1]) / (hi[0][0] - lo[0][0]) / MB
    return {"state": st, "error": str(task.error) if task.error else None, "sec": round(el, 2),
            "MBps": round(size / MB / el, 2), "steady_MBps": round(steady, 2) if steady else None,
            "stats": dict(task.stats), "target": task.target,
            "sha_ok": st == "done" and os.path.exists(out) and sha_file(out) == srv.sha,
            "peak_conns": max([m[2] for m in marks] or [0]), "task": task}


def fixed(n):
    return {"name": "测试%d" % n, "start_conn": n, "max_conn": n, "min_split": 256 * KB}


# ------------------------------------------------------------------ 用例
def case_fixed_speeds(d):
    print("[speed] 96 MB，每连接 200 KB/s：固定 32 / 64 / 128 路", flush=True)
    size = 96 * MB
    res = {}
    for n in (32, 64, 128):
        srv = RangeServer(size)
        try:
            r = run_engine(srv, os.path.join(d, "f%d.bin" % n), fixed(n))
        finally:
            srv.close()
        res[n] = r
        print("    %3d 路：%s  全程 %.2f MB/s  稳态 %s MB/s  用时 %.1fs  服务器峰值并发 %d  引擎最多 %s 路" % (
            n, r["state"], r["MBps"], r["steady_MBps"], r["sec"], srv.peak, r["stats"]["max_conn"]), flush=True)
        check(r["state"] == "done" and r["sha_ok"], "%d 路下完且 sha256 一致" % n, r["error"])
        check(srv.peak <= n, "%d 路：服务器看到的并发 ≤ %d（实际 %d）" % (n, n, srv.peak))
        RESULTS["fixed_%d" % n] = {k: r[k] for k in ("sec", "MBps", "steady_MBps")}
        RESULTS["fixed_%d" % n]["server_peak"] = srv.peak
        try:
            os.remove(os.path.join(d, "f%d.bin" % n))
        except OSError:
            pass
    s32, s64, s128 = (res[n]["steady_MBps"] or res[n]["MBps"] for n in (32, 64, 128))
    check(s64 >= 1.8 * s32, "64 路稳态速度 ≥ 32 路 × 1.8（%.2f vs %.2f，%.2f×）" % (s64, s32, s64 / max(s32, 1e-9)))
    check(res[64]["MBps"] >= 1.6 * res[32]["MBps"], "64 路全程速度 ≥ 32 路 × 1.6（%.2f vs %.2f，%.2f×，含握手/收尾）" % (
        res[64]["MBps"], res[32]["MBps"], res[64]["MBps"] / max(res[32]["MBps"], 1e-9)))
    check(s128 >= 1.5 * s64, "128 路稳态速度 ≥ 64 路 × 1.5（%.2f vs %.2f）" % (s128, s64))
    RESULTS["ratio_64_32"] = round(s64 / max(s32, 1e-9), 2)
    RESULTS["ratio_128_64"] = round(s128 / max(s64, 1e-9), 2)


def case_cap128(d):
    print("[cap128] 自动模式上限 128：8 路起自动涨，验证到 128 且不越过；999 夹到 128；默认上限 64", flush=True)
    check(PR.DEFAULT_MAX == 64, "sdl_speed 默认自动上限 = 64（实际 %s）" % PR.DEFAULT_MAX)
    check(PR.HARD_MAX == 128, "sdl_speed 绝对上限 = 128")
    check(PR.resolve("http://127.0.0.1/x").max_conn == 64, "普通站点 resolve 上限 64")
    check(PR.resolve("http://127.0.0.1/x", {"max_conn": 999}).max_conn == 128, "override 999 → 夹到 128")
    check(PR.resolve("https://objects.githubusercontent.com/x").max_conn == 128, "快主机自动上限 128")
    check(abs(SE.GROW_GAIN - 1.05) < 1e-9, "自动加连接阈值：提升 <5% 就停（GROW_GAIN=1.05）")
    srv = RangeServer(160 * MB)
    try:
        prof = {"name": "上限999", "start_conn": 8, "max_conn": 999, "min_split": 256 * KB}
        r = run_engine(srv, os.path.join(d, "cap.bin"), prof)
    finally:
        srv.close()
    t = r["task"]
    print("    自动：%s 用时 %.1fs 全程 %.2f MB/s 稳态 %s MB/s，引擎最多 %s 路，服务器峰值 %d" % (
        r["state"], r["sec"], r["MBps"], r["steady_MBps"], r["stats"]["max_conn"], srv.peak), flush=True)
    check(t.profile.max_conn == 128, "任务 profile 上限被夹到 128（实际 %s）" % t.profile.max_conn)
    check(r["state"] == "done" and r["sha_ok"], "自动 8→128 下完且 sha256 一致", r["error"])
    check(r["stats"]["max_conn"] == 128, "自动模式真涨到 128 路（引擎记录最多 %s 路）" % r["stats"]["max_conn"])
    check(srv.peak <= 128, "服务器从没看到 >128 并发（峰值 %d）" % srv.peak)
    RESULTS["auto_128"] = {"sec": r["sec"], "MBps": r["MBps"], "steady_MBps": r["steady_MBps"],
                           "max_conn": r["stats"]["max_conn"], "server_peak": srv.peak}


def case_503(d):
    print("[503] 服务器并发 >20 回 503（Retry-After: 1）：自动降并最终下完", flush=True)
    srv = RangeServer(32 * MB, max_active=20)
    try:
        prof = {"name": "防封测试", "start_conn": 8, "max_conn": 64, "min_split": 256 * KB}
        r = run_engine(srv, os.path.join(d, "t503.bin"), prof, timeout=180)
    finally:
        srv.close()
    stt = r["stats"]
    print("    %s 用时 %.1fs %.2f MB/s，throttles=%s，学到上限=%s，最终目标 %s 路，服务器回 503 %d 次、峰值 %d" % (
        r["state"], r["sec"], r["MBps"], stt.get("throttles"), stt.get("ceiling"), r["target"], srv.n503,
        srv.peak), flush=True)
    check(srv.n503 > 0, "服务器确实回过 503（%d 次）" % srv.n503)
    check(stt.get("throttles", 0) > 0, "引擎记了 throttles（%s）" % stt.get("throttles"))
    check(0 < (stt.get("ceiling") or 0) <= 24, "引擎自动降：学到的上限 %s ≤ 24（服务器允许 20）" % stt.get("ceiling"))
    check(r["target"] <= 24, "最终连接目标 %s ≤ 24" % r["target"])
    check(r["state"] == "done" and r["sha_ok"], "503 限流下最终下完且 sha256 一致", r["error"])
    check(r["MBps"] >= 0.5 * 20 * PER_CONN / MB, "限流后仍保持 ≥ 服务器允许带宽的一半（%.2f MB/s）" % r["MBps"])
    RESULTS["throttle_503"] = {"sec": r["sec"], "MBps": r["MBps"], "throttles": stt.get("throttles"),
                               "ceiling": stt.get("ceiling"), "server_503": srv.n503, "server_peak": srv.peak}


def case_plateau(d):
    print("[plateau] 总带宽封顶 4 MB/s（=20 路×200K）：提升 <5% 就停并退回上一档", flush=True)
    srv = RangeServer(48 * MB, total_rate=4 * MB)
    try:
        prof = {"name": "封顶测试", "start_conn": 8, "max_conn": 128, "min_split": 256 * KB}
        r = run_engine(srv, os.path.join(d, "plateau.bin"), prof)
    finally:
        srv.close()
    stt = r["stats"]
    print("    %s 用时 %.1fs %.2f MB/s，最多 %s 路，自动停在 %s 路" % (
        r["state"], r["sec"], r["MBps"], stt.get("max_conn"), stt.get("auto_stop")), flush=True)
    check(r["state"] == "done" and r["sha_ok"], "封顶服务器下完且 sha256 一致", r["error"])
    check(stt.get("max_conn", 0) < 128, "没有一路开到 128（最多 %s 路）" % stt.get("max_conn"))
    check(stt.get("auto_stop") is not None and stt["auto_stop"] <= 32,
          "吞吐不再涨时停下并退回（停在 %s 路 ≤ 32）" % stt.get("auto_stop"))
    RESULTS["plateau"] = {"MBps": r["MBps"], "max_conn": stt.get("max_conn"), "auto_stop": stt.get("auto_stop")}


class FakeEng:
    def __init__(self, cfg=None):
        self.cfg = dict(cfg or {})
        self.gbucket = RL.TokenBucket(0)
        self._buckets = {}
        self._rpc_lock = threading.RLock()
        self.logs = []

    def log(self, m):
        self.logs.append(m)


def case_settings():
    print("[settings] 设置「最大连接数」接线 / 网盘与 aria2 上限", flush=True)
    sp = S.spec("max_conns")
    check(sp is not None and sp["label"] == "最大连接数" and sp["group"] == "advanced"
          and sp["options"] == ["自动", "8", "16", "32", "64", "128"] and sp["default"] == "自动",
          "设置表：max_conns「最大连接数」高级项，档位 自动/8/16/32/64/128", sp)
    check(S.validate("max_conns", "128") == "128", "validate 接受 128")
    try:
        S.validate("max_conns", "256")
        check(False, "validate 拒绝 256")
    except ValueError:
        check(True, "validate 拒绝 256")
    url = "http://127.0.0.1:1/x.bin"
    p, _ = SP._profile_for(FakeEng(), url, 16, "1M", None)
    check(p is None, "最大连接数=自动（未设）→ 交给自动 8→64")
    p, _ = SP._profile_for(FakeEng({"max_conns": "自动"}), url, 16, "1M", None)
    check(p is None, "最大连接数=自动 → 自动")
    for n in (8, 16, 32, 64, 128):
        p, nm = SP._profile_for(FakeEng({"max_conns": str(n)}), url, 16, "1M", None)
        check(p and p["max_conn"] == n and p["start_conn"] == min(8, n), "最大连接数=%d → 上限 %d" % (n, n), p)
    p, _ = SP._profile_for(FakeEng({"max_conns": "128"}), url, 16, "1M", {"a2opts": {"split": "16"}})
    check(p["max_conn"] == 16, "推送方 split=16 仍当上限（LinkSwift 兼容）")
    p, _ = SP._profile_for(FakeEng({"max_conns": "128"}), url, 16, "1M", {"conns": "4"})
    check(p["max_conn"] == 4, "任务手动 4 路仍当上限")
    check(SP.planned_cap(FakeEng(), url) == 64, "自动模式计划上限 64")
    # 网盘
    bd = next(x for x in ND.PROFILES if x["key"] == "baidu")
    burl = "https://d.pcs.baidu.com/file/x?fid=1"
    if ND.match_profile(burl) is None:
        burl = "https://%s/file/x" % bd["dl_hosts"][0]
    p, _ = SP._profile_for(FakeEng(), burl, 16, "1M", None)
    check(p and p["max_conn"] == min(16, bd["conn"]), "网盘默认仍按防封上限（%s）" % (p and p["max_conn"]))
    p, _ = SP._profile_for(FakeEng({"netdisk_conn": {"baidu": 48}}), burl, 16, "1M", None)
    check(p and p["max_conn"] == 48, "网盘覆盖 48 → 自研引擎 48 路（不再夹 16）", p)
    p, _ = SP._profile_for(FakeEng({"netdisk_conn": {"baidu": 48}, "max_conns": "32"}), burl, 16, "1M", None)
    check(p and p["max_conn"] == 32, "网盘覆盖 48 ∧ 最大连接数 32 → 32")
    check(ND.profile_conn(bd, {"netdisk_conn": {"baidu": 48}}, 16) == 16, "aria2 侧 profile_conn 仍夹 16")
    check(ND.profile_conn(bd, {"netdisk_conn": {"baidu": 48}}, None, hard_max=128) == 48, "hard_max=128 时 48")
    a = ND.aria2_http_task_opts(64)
    check(str(a.get("split")) == "16" and str(a.get("max-connection-per-server")) == "16",
          "aria2 回退参数：要 64 路也夹到 16", a)
    # needs_sdl：只在明确要求 >16 时
    check(SP.needs_sdl(FakeEng(), url) == 0, "自动模式不算明确要求 >16（speed_engine=aria2 仍尊重 aria2）")
    check(SP.needs_sdl(FakeEng({"max_conns": "16"}), url) == 0, "最大连接数=16 → 不需要自研引擎")
    check(SP.needs_sdl(FakeEng({"max_conns": "64"}), url) == 64, "最大连接数=64 → 需要自研引擎（64 路）")
    check(SP.needs_sdl(FakeEng({"netdisk_conn": {"baidu": 48}}), burl) == 48, "网盘覆盖 48 → 需要自研引擎")
    check(SP.needs_sdl(FakeEng({"max_conns": "64"}), url, task={"a2opts": {"split": "16"}}) == 0,
          "推送方 split=16 → 不需要（照推送方）")


def case_core_run_http(d):
    print("[core] core.speed.run_http（内核同一入口）max_conns=128 实下", flush=True)
    srv = RangeServer(96 * MB)
    eng = FakeEng({"max_conns": "128"})
    task = {"gid": "a15", "conns": ""}
    t0 = time.monotonic()
    err = None
    try:
        res = SP.run_http(eng, srv.url, d, "core128.bin", {}, prog=None, cancel=lambda: False,
                          conn=16, min_split="256K", task=task)
    except Exception as e:
        res, err = None, e
    finally:
        srv.close()
    el = time.monotonic() - t0
    ok = res and sha_file(res) == srv.sha
    print("    %s 用时 %.1fs %.2f MB/s conns_eff=%s 服务器峰值 %d" % (
        "完成" if ok else "失败 %r" % err, el, 96 / el, task.get("conns_eff"), srv.peak), flush=True)
    check(ok, "run_http 下完且 sha256 一致", err)
    check(task.get("conns_eff") == "128" and task.get("speed_engine") == "sdl",
          "conns_eff=128、speed_engine=sdl（%s/%s）" % (task.get("conns_eff"), task.get("speed_engine")))
    check(any("上限 128" in m for m in eng.logs), "日志写了上限 128", eng.logs[:2])
    RESULTS["core_run_http_128"] = {"sec": round(el, 2), "MBps": round(96 / el, 2), "server_peak": srv.peak}


def main(argv):
    only = [a for a in argv if not a.startswith("-")]
    d = tempfile.mkdtemp(prefix="sdl_a15_")
    cases = [("settings", lambda: case_settings()), ("speed", lambda: case_fixed_speeds(d)),
             ("cap128", lambda: case_cap128(d)), ("503", lambda: case_503(d)),
             ("plateau", lambda: case_plateau(d)), ("core", lambda: case_core_run_http(d))]
    t0 = time.monotonic()
    try:
        for name, fn in cases:
            if only and name not in only:
                continue
            try:
                fn()
            except Exception as e:
                import traceback
                traceback.print_exc()
                check(False, "用例 %s 异常：%r" % (name, e))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    RESULTS["total_sec"] = round(time.monotonic() - t0, 1)
    with open("/tmp/sdl_conn_cap.json", "w", encoding="utf-8") as f:
        json.dump(RESULTS, f, ensure_ascii=False, indent=1)
    print("\nconn_cap_test: %d 通过, %d 失败%s" % (PASSES[0], len(FAILS), "" if not FAILS else "：" + "；".join(FAILS)))
    print(json.dumps(RESULTS, ensure_ascii=False))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
