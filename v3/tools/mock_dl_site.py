# -*- coding: utf-8 -*-
"""本地 mock 下载站（记忆系统端到端验收用，零外网流量、不受站点限流/被墙干扰）。

    python tools/mock_dl_site.py --port 8799 [--mb 16] [--kbps 4000]

三个文件，全部支持 Range（断点续传），内容确定性生成（同参数下 sha256 恒定）：
  /fast.mp4   2MB            → 验「已完成不重下」
  /slow.mp4   --mb 大小      → 验「进行中被叉掉 → 重启只显示不重下，点继续才下完」
  /auto.mp4   --mb 大小      → 验「browser 来源重启不恢复」
GET /__stats → JSON：每个文件已发出字节 / 请求数 / 各请求的 Range 起点。
E2E 靠它硬断言「重启后零流量（没有自动重放）」与「点继续之后才真的续传」——
比看本地文件大小可靠得多（aria2c 稀疏预分配会让 getsize 骗人）。
限速是【全站共享带宽】：SniffDL 会把一条直链拆成几十上百个并发分段，按请求限速
等于没限速，所以这里用全局令牌桶（PACE）把总带宽钉死。
"""
import argparse
import hashlib
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

CFG = {"kbps": 4000}
STAT = {"bytes": {}, "reqs": {}, "starts": {}}
LOCK = threading.Lock()
PACE = {"next": 0.0}
_BODIES = {}


def body_for(name, size):
    """确定性内容（缓存复用：aria2c 会几十路并发，每路重算一遍会把自己跑死）。"""
    with LOCK:
        b = _BODIES.get(name)
        if b is not None and len(b) == size:
            return b
    out = bytearray()
    idx = 0
    while len(out) < size:
        block = (hashlib.sha256(("%s:%d" % (name, idx)).encode()).hexdigest() * 4)[:65536]
        out.extend(block.encode("ascii"))
        idx += 1
    b = bytes(out[:size])
    with LOCK:
        _BODIES[name] = b
    return b


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    sizes = {}

    def log_message(self, *a):
        return

    def _stats(self):
        with LOCK:
            blob = json.dumps({"kbps": CFG["kbps"], "stats": STAT}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        try:
            self.wfile.write(blob)
        except Exception:
            pass

    def _handle(self, method):
        pr = urlparse(self.path)
        name = pr.path.lstrip("/")
        if name == "__stats":
            return self._stats()
        q = parse_qs(pr.query)
        try:
            kbps = int((q.get("kbps") or [0])[0]) or CFG["kbps"]
        except Exception:
            kbps = CFG["kbps"]
        size = self.sizes.get(name)
        if size is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = body_for(name, size)
        start, end = 0, len(data) - 1
        partial = False
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d+)-(\d*)", rng)
            if m:
                start = int(m.group(1))
                end = min(int(m.group(2)) if m.group(2) else len(data) - 1, len(data) - 1)
                partial = start <= end
        chunk = data[start:end + 1] if partial else data
        with LOCK:
            STAT["reqs"][name] = STAT["reqs"].get(name, 0) + 1
            STAT["starts"].setdefault(name, []).append(start)
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(chunk)))
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, len(data)))
        self.end_headers()
        if method == "HEAD" or not chunk:
            return
        step = 64 * 1024
        bps = (kbps * 1024 / 8.0) or 0.0
        sent = 0
        for i in range(0, len(chunk), step):
            piece = chunk[i:i + step]
            if bps:
                # 全站共享令牌桶：所有并发分段排队过同一个时刻
                with LOCK:
                    now = time.time()
                    target = max(now, PACE["next"])
                    PACE["next"] = target + len(piece) / bps
                wait = target - time.time()
                if wait > 0:
                    time.sleep(min(wait, 30.0))
            try:
                self.wfile.write(piece)
                self.wfile.flush()
                sent += len(piece)
                with LOCK:      # 实时累计：几十个并发分段"进行中"也要看得见字节
                    STAT["bytes"][name] = STAT["bytes"].get(name, 0) + len(piece)
            except Exception:
                return

    def do_GET(self):
        self._handle("GET")

    def do_HEAD(self):
        self._handle("HEAD")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--mb", type=int, default=16)
    ap.add_argument("--kbps", type=int, default=4000)
    a = ap.parse_args()
    CFG["kbps"] = a.kbps
    Handler.sizes = {"fast.mp4": 2 * 1024 * 1024,
                     "auto.mp4": a.mb * 1024 * 1024,
                     "slow.mp4": a.mb * 1024 * 1024}
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print("MOCK READY http://127.0.0.1:%d  fast.mp4=2MB  slow.mp4=auto.mp4=%dMB  "
          "全站限速 %dkbps（可按请求 ?kbps= 覆盖）" % (a.port, a.mb, a.kbps), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
