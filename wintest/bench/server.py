# -*- coding: utf-8 -*-
"""SniffDL 对比测速用的本地 HTTP 服务器（支持 Range / HEAD / keep-alive）。

一个进程同时提供全部场景，按路径区分：
  /A/big.bin           A 大文件，不限速
  /B/big.bin           B 单连接限速（按 TCP 连接计，默认 2 MB/s，模拟网盘）
  /C/big.bin           C 高延迟：每个请求首字节前 +200 ms，之后每发 64 KB 再加 0~jitter ms 随机抖动
  /D/f0001.bin …       D 大量小文件（默认 500 × 200 KB）
  /E/big.bin           E 最多 N 个并发传输（默认 4），超出直接 503（可选 Retry-After）
  /manifest.json       每个文件的 size / sha256（测速脚本拿它校验）
  /stats.json          各场景请求数 / 503 次数 / 峰值并发
  /D/list.txt          D 场景全部 URL（一行一个，给 aria2 -i 用）
查询串（如 ?run=3）会被忽略，测速脚本用它让 IDM 每次看到不同 URL。

用法：
  python server.py --data DIR --port 0 [--scale full|small] [--a-size 1G] [--b-rate 2M] ...
  启动完成后首行打印：READY <port>
文件只在 DIR 里不存在或大小不符时生成（随机数据，固定种子），生成后算 sha256 存 DIR/manifest.json。
"""
import argparse
import hashlib
import json
import os
import random
import re
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

KB, MB, GB = 1024, 1024 ** 2, 1024 ** 3
BUF = 256 * KB

SCALES = {
    "full": dict(a_size=1 * GB, b_size=512 * MB, c_size=512 * MB, d_count=500, d_size=200 * KB, e_size=256 * MB),
    "small": dict(a_size=100 * MB, b_size=100 * MB, c_size=100 * MB, d_count=100, d_size=200 * KB, e_size=100 * MB),
}


def parse_size(v):
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip().upper().rstrip("B")
    mul = 1
    for suf, m in (("K", KB), ("M", MB), ("G", GB)):
        if s.endswith(suf):
            s, mul = s[:-1], m
            break
    return int(float(s) * mul)


def _gen(path, size, seed):
    """生成随机数据文件并返回 sha256；已存在且大小一致就只算哈希。"""
    h = hashlib.sha256()
    if os.path.exists(path) and os.path.getsize(path) == size:
        with open(path, "rb") as f:
            for b in iter(lambda: f.read(4 * MB), b""):
                h.update(b)
        return h.hexdigest()
    rnd = random.Random(seed)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        left = size
        while left > 0:
            n = min(4 * MB, left)
            b = rnd.randbytes(n)
            h.update(b)
            f.write(b)
            left -= n
    os.replace(tmp, path)
    return h.hexdigest()


def build_data(root, cfg):
    os.makedirs(root, exist_ok=True)
    man_path = os.path.join(root, "manifest.json")
    old = {}
    if os.path.exists(man_path):
        try:
            old = json.load(open(man_path, encoding="utf-8"))
        except Exception:
            old = {}
    files = {}
    want = [("A/big.bin", cfg["a_size"], 101), ("B/big.bin", cfg["b_size"], 102),
            ("C/big.bin", cfg["c_size"], 103), ("E/big.bin", cfg["e_size"], 105)]
    for i in range(1, cfg["d_count"] + 1):
        want.append(("D/f%04d.bin" % i, cfg["d_size"], 10000 + i))
    for rel, size, seed in want:
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        o = old.get(rel)
        if o and o.get("size") == size and os.path.exists(p) and os.path.getsize(p) == size and o.get("seed") == seed:
            files[rel] = o
            continue
        files[rel] = {"size": size, "sha256": _gen(p, size, seed), "seed": seed}
    with open(man_path, "w", encoding="utf-8") as f:
        json.dump(files, f, indent=0)
    return files


class Stats:
    def __init__(self):
        self.lk = threading.Lock()
        self.d = {}

    def bump(self, sc, key, n=1):
        with self.lk:
            s = self.d.setdefault(sc, {"requests": 0, "rejected_503": 0, "bytes": 0, "peak_active": 0, "active": 0,
                                        "connections": 0})
            s[key] = s.get(key, 0) + n
            if key == "active" and s["active"] > s["peak_active"]:
                s["peak_active"] = s["active"]
            return s

    def snapshot(self):
        with self.lk:
            return json.loads(json.dumps(self.d))

    def reset(self):
        with self.lk:
            self.d = {}


RANGE_RE = re.compile(r"^\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*$", re.I)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "sdl-bench"
    sys_version = ""

    def setup(self):
        super().setup()
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass
        # B：按 TCP 连接限速 —— 令牌桶挂在 handler 上（一个 handler = 一条连接）
        self.tb_t = time.monotonic()
        self.tb_tokens = 0.0
        self.counted = set()

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self._serve(True)

    def do_GET(self):
        self._serve(False)

    # ------------------------------------------------------------------
    def _simple(self, code, body, ctype="application/json", extra=()):
        b = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(b)

    def _throttle(self, n, rate):
        """连接级令牌桶：发 n 字节前调用。"""
        if rate <= 0:
            return
        now = time.monotonic()
        self.tb_tokens = min(rate * 0.25, self.tb_tokens + (now - self.tb_t) * rate)
        self.tb_t = now
        self.tb_tokens -= n
        if self.tb_tokens < 0:
            time.sleep(-self.tb_tokens / rate)

    def _serve(self, head):
        S = self.server
        path = urlsplit(self.path).path
        if path == "/manifest.json":
            return self._simple(200, json.dumps(S.files))
        if path == "/stats.json":
            return self._simple(200, json.dumps(S.stats.snapshot()))
        if path == "/stats/reset":
            S.stats.reset()
            return self._simple(200, "{}")
        if path == "/D/list.txt":
            host = self.headers.get("Host") or "127.0.0.1:%d" % S.server_address[1]
            lines = ["http://%s/%s" % (host, k) for k in sorted(S.files) if k.startswith("D/")]
            return self._simple(200, "\n".join(lines) + "\n", "text/plain; charset=utf-8")
        rel = path.lstrip("/")
        meta = S.files.get(rel)
        if not meta:
            return self._simple(404, '{"error":"not found"}')
        sc = rel.split("/", 1)[0]
        cfg = S.cfg
        st = S.stats
        st.bump(sc, "requests")
        if sc not in self.counted:
            self.counted.add(sc)
            st.bump(sc, "connections")

        active = False
        if sc == "E" and not head:
            with S.e_lock:
                if S.e_active >= cfg["e_max_conn"]:
                    st.bump(sc, "rejected_503")
                    extra = [("Connection", "close")]
                    if cfg.get("e_retry_after") is not None:
                        extra.append(("Retry-After", str(cfg["e_retry_after"])))
                    self.close_connection = True
                    return self._simple(503, '{"error":"too many connections"}', extra=extra)
                S.e_active += 1
                active = True
        try:
            self._send_file(rel, meta, sc, head)
        finally:
            if active:
                with S.e_lock:
                    S.e_active -= 1

    def _send_file(self, rel, meta, sc, head):
        S = self.server
        cfg = S.cfg
        size = meta["size"]
        etag = '"%s"' % meta["sha256"][:16]
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng:
            m = RANGE_RE.match(rng)
            if not m or (not m.group(1) and not m.group(2)):
                return self._simple(416, "{}", extra=[("Content-Range", "bytes */%d" % size)])
            a, b = m.group(1), m.group(2)
            if a == "":
                n = int(b)
                start, end = max(0, size - n), size - 1
            else:
                start = int(a)
                end = min(size - 1, int(b)) if b else size - 1
            if start >= size or start > end:
                return self._simple(416, "{}", extra=[("Content-Range", "bytes */%d" % size)])
            code = 206
        # C：首字节延迟
        if sc == "C" and cfg["c_latency"] > 0:
            time.sleep(cfg["c_latency"])
        elif sc == "D" and cfg["d_latency"] > 0:
            time.sleep(cfg["d_latency"])
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", "Thu, 12 Nov 2026 00:00:00 GMT")
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        if head:
            return
        st = S.stats
        st.bump(sc, "active")
        rate = cfg["b_rate"] if sc == "B" else (cfg["e_rate"] if sc == "E" else 0)
        step = BUF
        if rate:
            step = max(16 * KB, min(BUF, int(rate / 16)))
        if sc == "C":
            step = 64 * KB
        sent = 0
        try:
            with open(os.path.join(S.root, rel), "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    n = min(step, left)
                    b = f.read(n)
                    if not b:
                        break
                    if rate:
                        self._throttle(len(b), rate)
                    if sc == "C" and cfg["c_jitter"] > 0:
                        time.sleep(random.random() * cfg["c_jitter"])
                    self.wfile.write(b)
                    left -= len(b)
                    sent += len(b)
        except (ConnectionError, BrokenPipeError, socket.timeout, OSError):
            self.close_connection = True
        finally:
            st.bump(sc, "active", -1)
            st.bump(sc, "bytes", sent)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 512


def make_server(root, cfg, host="127.0.0.1", port=0):
    files = build_data(root, cfg)
    srv = Server((host, port), Handler)
    srv.root = root
    srv.cfg = cfg
    srv.files = files
    srv.stats = Stats()
    srv.e_lock = threading.Lock()
    srv.e_active = 0
    return srv


def main(argv=None):
    ap = argparse.ArgumentParser(description="SniffDL 对比测速本地服务器")
    ap.add_argument("--data", required=True, help="数据目录（生成测试文件）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--scale", choices=sorted(SCALES), default="full")
    ap.add_argument("--a-size"), ap.add_argument("--b-size"), ap.add_argument("--c-size"), ap.add_argument("--e-size")
    ap.add_argument("--d-count", type=int), ap.add_argument("--d-size")
    ap.add_argument("--b-rate", default="2M", help="B 每连接限速 B/s")
    ap.add_argument("--c-latency", type=float, default=0.2, help="C 每请求首字节延迟（秒）")
    ap.add_argument("--c-jitter", type=float, default=0.02, help="C 每 64KB 块随机抖动上限（秒）")
    ap.add_argument("--d-latency", type=float, default=0.0, help="D 每请求首字节延迟（秒）")
    ap.add_argument("--e-max-conn", type=int, default=4)
    ap.add_argument("--e-rate", default="5M", help="E 每连接限速 B/s（0 不限）")
    ap.add_argument("--e-retry-after", type=int, default=None, help="E 503 带的 Retry-After 秒数（默认不带）")
    a = ap.parse_args(argv)
    cfg = dict(SCALES[a.scale])
    for k in ("a_size", "b_size", "c_size", "e_size", "d_size"):
        v = getattr(a, k)
        if v:
            cfg[k] = parse_size(v)
    if a.d_count:
        cfg["d_count"] = a.d_count
    cfg.update(b_rate=parse_size(a.b_rate), c_latency=a.c_latency, c_jitter=a.c_jitter, d_latency=a.d_latency,
               e_max_conn=a.e_max_conn, e_rate=parse_size(a.e_rate), e_retry_after=a.e_retry_after)
    srv = make_server(a.data, cfg, a.host, a.port)
    with open(os.path.join(a.data, "server_cfg.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    print("READY %d" % srv.server_address[1], flush=True)
    try:
        srv.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
