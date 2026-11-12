# -*- coding: utf-8 -*-
"""本地 mock「网盘 CDN」（网盘直链验收用，零外网）。

模拟夸克 CDN 的鉴权行为（规则来自 LinkSwift 对夸克的错误码注释 + alist quark 驱动）：
  · 没带登录 Cookie（__pus=…）              → 412 Precondition Failed（LinkSwift：「需要登录才能下载」）
  · Cookie 对、但 UA 不是推送方给的那个      → 403（模拟"签名绑定 UA/Cookie"）
  · auth_key 第 1 段（到期时间戳）已过        → 403（LinkSwift：「链接已过期」）
  · 都对 → 200/206，支持 Range（多连接分段 + 断点续传）

    python tools/mock_netdisk_site.py --port 8798 [--mb 8] [--ua quark-cloud-drive] [--cookie __pus=OK]

GET /__stats  → 每个请求的 UA / Cookie 名 / Range / 状态码 + 历史最大并发连接数
GET /__reset  → 清统计
文件：/ngTX6F13/<任意>/file?auth_key=<exp>-<rand>-<ttl>-<hash>&filename=xxx.zip
"""
import argparse
import hashlib
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

CFG = {"ua": "quark-cloud-drive", "cookie": "__pus=OK", "size": 8 * 1024 * 1024,
       "conn_kbps": 0, "slow_every": 0, "slow_kbps": 32}
# slow_every=N>0：每第 N 条数据连接被"拖后腿"成 slow_kbps（模拟 CDN 个别节点慢，测分段调度/尾段拖延）   # conn_kbps>0：每条连接限速（模拟网盘/CDN 单连接限速，测多连接收益）
STAT = {"reqs": [], "max_conc": 0, "conc": 0, "bytes": 0}
LOCK = threading.Lock()
_BODY = {}


def body(size):
    b = _BODY.get(size)
    if b is not None:
        return b
    out = bytearray()
    i = 0
    while len(out) < size:
        out.extend(hashlib.sha256(b"nd:%d" % i).digest() * 64)
        i += 1
    b = bytes(out[:size])
    _BODY[size] = b
    return b


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        return

    def _json(self, obj, code=200):
        blob = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def _deny(self, code, why, rec):
        rec["status"] = code
        rec["why"] = why
        blob = ("<html><body>%d %s</body></html>" % (code, why)).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(blob)

    def _serve(self):
        pr = urlparse(self.path)
        if pr.path == "/__stats":
            with LOCK:
                return self._json(STAT)
        if pr.path == "/__reset":
            with LOCK:
                STAT.update({"reqs": [], "max_conc": 0, "conc": 0, "bytes": 0, "nconn": 0, "nreq_data": 0})
            return self._json({"ok": True})
        q = parse_qs(pr.query)
        ua = self.headers.get("User-Agent") or ""
        ck = self.headers.get("Cookie") or ""
        rng = self.headers.get("Range") or ""
        rec = {"method": self.command, "path": pr.path, "ua": ua,
               "cookie_names": sorted({kv.split("=")[0].strip() for kv in ck.split(";") if kv.strip()}),
               "cookie": ck, "referer": self.headers.get("Referer") or "",
               "range": rng, "ts": time.time()}
        with LOCK:
            STAT["reqs"].append(rec)
        ak = (q.get("auth_key") or [""])[0]
        m = re.match(r"^(\d{10})-", ak)
        if CFG["cookie"] not in ck:
            return self._deny(412, "Precondition Failed (no login cookie)", rec)
        if m and int(m.group(1)) < time.time():
            return self._deny(403, "Forbidden (expired)", rec)
        if CFG["ua"] and CFG["ua"] not in ua:
            return self._deny(403, "Forbidden (ua mismatch)", rec)
        with LOCK:
            STAT["nreq_data"] = STAT.get("nreq_data", 0) + 1
            flaky = STAT["nreq_data"] <= CFG.get("fail_first", 0)
        if flaky:   # 模拟 CDN 瞬时抖动：开头 N 个请求回 503（测重试是否稳健、会不会白白掉回单连接）
            return self._deny(503, "Service Unavailable (flaky)", rec)
        data = body(CFG["size"])
        total = len(data)
        start, end = 0, total - 1
        code = 200
        mm = re.match(r"bytes=(\d+)-(\d*)", rng)
        if mm:
            start = int(mm.group(1))
            end = int(mm.group(2)) if mm.group(2) else total - 1
            end = min(end, total - 1)
            code = 206
        rec["status"] = code
        fn = (q.get("filename") or ["file.bin"])[0]
        self.send_response(code)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % fn)
        self.send_header("Content-Length", str(end - start + 1))
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, total))
        self.end_headers()
        if self.command == "HEAD":
            return
        with LOCK:
            STAT["conc"] += 1
            STAT["max_conc"] = max(STAT["max_conc"], STAT["conc"])
            STAT["nconn"] = STAT.get("nconn", 0) + 1
            if CFG.get("slow_random"):
                import random as _rnd
                slow = CFG["slow_every"] > 0 and _rnd.random() < 1.0 / CFG["slow_every"]
            else:
                slow = CFG["slow_every"] > 0 and STAT["nconn"] % CFG["slow_every"] == 0
        rate = CFG["slow_kbps"] if slow else CFG["conn_kbps"]
        try:
            pos = start
            while pos <= end:
                chunk = data[pos:min(end + 1, pos + 64 * 1024)]
                self.wfile.write(chunk)
                pos += len(chunk)
                with LOCK:
                    STAT["bytes"] += len(chunk)
                if rate > 0:
                    time.sleep(len(chunk) / (rate * 1024.0))
                else:
                    time.sleep(0.002)   # 稍微拖慢，让多连接真的并发起来
        except Exception:
            pass
        finally:
            with LOCK:
                STAT["conc"] -= 1

    def do_GET(self):
        self._serve()

    def do_HEAD(self):
        self._serve()


def serve(port, size=None, ua=None, cookie=None, conn_kbps=None):
    if conn_kbps is not None:
        CFG["conn_kbps"] = conn_kbps
    if size:
        CFG["size"] = size
    if ua is not None:
        CFG["ua"] = ua
    if cookie is not None:
        CFG["cookie"] = cookie
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    return srv


def expected_sha256(size=None):
    return hashlib.sha256(body(size or CFG["size"])).hexdigest()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8798)
    ap.add_argument("--mb", type=int, default=8)
    ap.add_argument("--ua", default="quark-cloud-drive")
    ap.add_argument("--cookie", default="__pus=OK")
    ap.add_argument("--conn-kbps", type=int, default=0, help="每条连接限速 KB/s（0=不限）")
    a = ap.parse_args()
    serve(a.port, a.mb * 1024 * 1024, a.ua, a.cookie, a.conn_kbps)
    print("mock netdisk CDN on http://127.0.0.1:%d  (Ctrl+C 退出)" % a.port)
    while True:
        time.sleep(3600)
