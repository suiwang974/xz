# -*- coding: utf-8 -*-
"""aria2 WebSocket 接口端到端（A1）：ws://127.0.0.1:<port>/jsonrpc

起真内核（tests/ui_smoke_core.py）+ 本地文件服务器（复用 core_e2e 的 Files/Handler），用裸 socket 手写
RFC 6455 客户端（不依赖第三方库）验证：
  握手（RFC 示例 Accept 值）、请求/响应、批量请求、ping→pong、分片消息、非法 Origin 拒绝、
  未掩码帧断连、aria2 官方格式通知（{"jsonrpc":"2.0","method":"aria2.onDownloadStart","params":[{"gid":..}]}）
  onDownloadStart / Pause / Complete / Stop，getPeers/getServers 经 WebSocket。
用法：python3 tests/rpc_ws_e2e.py      退出码 0 = 全过。
"""
import base64
import hashlib
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time

os.environ.setdefault("SDL_E2E_DATA", "/tmp/sdlv3_ws")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core_e2e as E   # noqa: E402
from core_e2e import check, wait, MB  # noqa: E402

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WS:
    def __init__(self, port, path="/jsonrpc", origin=None, key=None, headers=None):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=15)
        key = key or base64.b64encode(os.urandom(16)).decode()
        self.key = key
        hh = dict(headers or {})
        host = hh.pop("Host", "127.0.0.1:%d" % port)
        req = ("GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n") % (path, host, key)
        if origin:
            req += "Origin: %s\r\n" % origin
        req += "".join("%s: %s\r\n" % kv for kv in hh.items()) + "\r\n"
        self.s.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            c = self.s.recv(4096)
            if not c:
                break
            buf += c
        head, _, self.rest = buf.partition(b"\r\n\r\n")
        lines = head.decode("latin1").split("\r\n")
        self.status = int(lines[0].split()[1]) if lines and len(lines[0].split()) > 1 else 0
        self.hdr = {l.split(":", 1)[0].strip().lower(): l.split(":", 1)[1].strip() for l in lines[1:] if ":" in l}
        self.q = []
        self.lock = threading.Lock()

    def _recv(self, n):
        while len(self.rest) < n:
            c = self.s.recv(65536)
            if not c:
                raise EOFError
            self.rest += c
        out, self.rest = self.rest[:n], self.rest[n:]
        return out

    def send(self, payload, opcode=1, fin=True, mask=True):
        if isinstance(payload, str):
            payload = payload.encode()
        n = len(payload)
        h = bytes([(0x80 if fin else 0) | opcode])
        mb = 0x80 if mask else 0
        if n < 126:
            h += bytes([mb | n])
        elif n < 65536:
            h += bytes([mb | 126]) + struct.pack(">H", n)
        else:
            h += bytes([mb | 127]) + struct.pack(">Q", n)
        if mask:
            m = os.urandom(4)
            payload = bytes(b ^ m[i & 3] for i, b in enumerate(payload))
            h += m
        self.s.sendall(h + payload)

    def recv(self, timeout=10):
        """-> (opcode, bytes)；超时 None，对端关闭 'closed'。"""
        self.s.settimeout(timeout)
        try:
            h = self._recv(2)
            op, n = h[0] & 0x0F, h[1] & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._recv(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._recv(8))[0]
            return op, self._recv(n)
        except socket.timeout:
            return None
        except (EOFError, ConnectionError, OSError):
            return "closed"

    def call(self, method, params=None, rid="1", timeout=10):
        self.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or []}))
        end = time.time() + timeout
        while time.time() < end:
            r = self.recv(max(0.1, end - time.time()))
            if r is None or r == "closed":
                return r
            if r[0] == 1:
                j = json.loads(r[1])
                if j.get("id") == rid:
                    return j
        return None

    def notes(self, want, timeout=15):
        """收集通知直到出现 want 个（或超时）：返回 [(method, gid)]。"""
        got = []
        end = time.time() + timeout
        while time.time() < end and len(got) < want:
            r = self.recv(max(0.1, end - time.time()))
            if r is None or r == "closed":
                break
            if r[0] == 1:
                j = json.loads(r[1])
                if "method" in j and "id" not in j:
                    got.append((j["method"], (j["params"][0] or {}).get("gid"), j))
        return got

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass


def main():
    shutil_rm = __import__("shutil").rmtree
    shutil_rm(E.DATA, ignore_errors=True)
    os.makedirs(E.OUT, exist_ok=True)
    srv = E.QuietServer(("127.0.0.1", 0), E.Handler)
    E.FPORT[0] = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    E.PORT[0] = E.free_port()
    env = dict(os.environ, SNIFFDL_DRYRUN_OS="1", NO_PROXY="127.0.0.1,localhost,::1")
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "DISPLAY"):
        env.pop(k, None)
    core = subprocess.Popen([sys.executable, os.path.join(E.HERE, "tests", "ui_smoke_core.py"), "--data-dir", E.DATA,
                             "--port", str(E.PORT[0]), "--runtime", os.path.join(E.DATA, "runtime.json"),
                             "--ui-token", E.TOKEN],
                            cwd=E.HERE, env=env, stdout=open(os.path.join(E.DATA, "core.out"), "w"),
                            stderr=subprocess.STDOUT)
    port = E.PORT[0]
    try:
        if not wait(lambda: E.raw_rpc("sniffdl.ping", [], token=None).get("result"), 30):
            check("内核启动", False, "见 %s/core.out" % E.DATA)
            return 1
        E.setv(outdir=E.OUT, clip_watch=False, notify=False, conflict="重命名", bt_tracker_auto=False, bt_dht=False,
               bt_lpd=False, bt_pex=False, max_concurrent=3)

        # 1 握手：RFC 6455 示例 key → 固定 Accept
        w = WS(port, key="dGhlIHNhbXBsZSBub25jZQ==")
        check("ws 握手 101", w.status == 101, w.status)
        check("Accept 值符合 RFC 6455 示例", w.hdr.get("sec-websocket-accept") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
              w.hdr.get("sec-websocket-accept"))
        check("Upgrade/Connection 头", w.hdr.get("upgrade", "").lower() == "websocket"
              and "upgrade" in w.hdr.get("connection", "").lower())
        w.close()

        # 2 请求/响应（无 token 的 aria2 风格客户端：AriaNg 直连）
        w = WS(port)
        j = w.call("aria2.getVersion")
        check("ws aria2.getVersion 有 version 与 enabledFeatures",
              bool(j) and "version" in (j.get("result") or {}) and "enabledFeatures" in j["result"], j)
        j = w.call("aria2.nope")
        check("ws 未知方法 → -32601 且 id 对应", bool(j) and j.get("error", {}).get("code") == -32601 and j.get("id") == "1", j)
        w.send(json.dumps([{"jsonrpc": "2.0", "id": "a", "method": "aria2.getGlobalStat"},
                           {"jsonrpc": "2.0", "id": "b", "method": "aria2.getVersion"}]))
        r = w.recv()
        arr = json.loads(r[1]) if r and r != "closed" and r[0] == 1 else None
        check("ws 批量请求返回数组且 id 配对", isinstance(arr, list) and [x["id"] for x in arr] == ["a", "b"]
              and "numActive" in arr[0]["result"], arr)
        w.send(b"{bad json", 1)
        r = w.recv()
        j = json.loads(r[1]) if r and r != "closed" else {}
        check("ws 坏 JSON → -32700 且连接保持", j.get("error", {}).get("code") == -32700
              and (w.call("aria2.getVersion") or {}).get("result") is not None, j)

        # 3 控制帧与分片
        w.send(b"hi-ping", 0x9)
        r = w.recv()
        check("ping → pong 原样回显", r and r != "closed" and r[0] == 0xA and r[1] == b"hi-ping", r)
        msg = json.dumps({"jsonrpc": "2.0", "id": "frag", "method": "aria2.getVersion", "params": []}).encode()
        w.send(msg[:20], 1, fin=False)
        w.send(msg[20:40], 0, fin=False)
        w.send(msg[40:], 0, fin=True)
        r = w.recv()
        j = json.loads(r[1]) if r and r != "closed" else {}
        check("分片文本消息重组后正常应答", j.get("id") == "frag" and "result" in j, j)
        big = "x" * 70000
        j = w.call("aria2.getVersion", [], rid="big") if False else None
        w.send(json.dumps({"jsonrpc": "2.0", "id": "big", "method": "aria2.getOption", "params": ["zz" + big]}))
        r = w.recv()
        j = json.loads(r[1]) if r and r != "closed" else {}
        check("64KB+ 长消息（8 字节长度帧）可收", j.get("id") == "big" and "error" in j, str(j)[:200])
        w.close()

        # 4 安全：Origin、未掩码
        w = WS(port, origin="https://evil.example.com")
        check("陌生网页 Origin 被拒（403，不升级）", w.status == 403, w.status)
        w.close()
        w = WS(port, origin="chrome-extension://abcdefghijklmnopabcdefghijklmnop")
        check("扩展 Origin 放行", w.status == 101, w.status)
        w.close()
        w = WS(port, origin="null")
        check("file:// 页面（Origin: null）放行", w.status == 101, w.status)
        w.close()
        w = WS(port)
        w.send(json.dumps({"jsonrpc": "2.0", "id": "u", "method": "aria2.getVersion"}), mask=False)
        r = w.recv(5)
        check("客户端未掩码帧 → 服务端断开", r == "closed", r)
        w.close()
        w = WS(port, headers={"Host": "evil.example.com"})
        check("Host 非本机（DNS 重绑定）拒绝", w.status == 403, w.status)
        w.close()

        # 5 通知：addUri → Start → Complete
        E.Files.add("ws_a.bin", 2 * MB)
        w = WS(port)
        j = w.call("aria2.addUri", [[E.url_of("ws_a.bin")], {"dir": E.OUT}])
        gid = (j or {}).get("result")
        check("ws aria2.addUri 返回 gid", bool(gid), j)
        ns = w.notes(2, 25)
        names = [n[0] for n in ns if n[1] == gid]
        check("收到 onDownloadStart", "aria2.onDownloadStart" in names, names)
        check("收到 onDownloadComplete", "aria2.onDownloadComplete" in names, names)
        if names:
            ex = ns[0][2]
            check("通知格式 = aria2 官方（jsonrpc/method/params[{gid}]，无 id）",
                  ex.get("jsonrpc") == "2.0" and "id" not in ex and list(ex["params"][0].keys()) == ["gid"], ex)
        check("下载文件落盘", os.path.isfile(os.path.join(E.OUT, "ws_a.bin"))
              and E.sha_file(os.path.join(E.OUT, "ws_a.bin")) == E.Files.sha["ws_a.bin"])

        # 6 暂停 / 删除 → Pause / Stop（慢速大文件）
        E.Files.add("ws_slow.bin", 24 * MB, rate=256 * 1024)
        j = w.call("aria2.addUri", [[E.url_of("ws_slow.bin")], {"dir": E.OUT}])
        g2 = (j or {}).get("result")
        got = [n for n in w.notes(1, 15) if n[1] == g2]
        check("慢速任务 onDownloadStart", got and got[0][0] == "aria2.onDownloadStart", got)
        w.call("aria2.pause", [g2], rid="p")
        got = [n[0] for n in w.notes(2, 15) if n[1] == g2]
        check("pause → onDownloadPause", "aria2.onDownloadPause" in got, got)
        w.call("aria2.forceRemove", [g2], rid="r")
        got = [n[0] for n in w.notes(3, 15) if n[1] == g2]
        check("remove → onDownloadStop", "aria2.onDownloadStop" in got, got)

        # 7 第二个客户端同时收到；断开后广播不报错
        w2 = WS(port)
        E.Files.add("ws_b.bin", 1 * MB)
        j = w.call("aria2.addUri", [[E.url_of("ws_b.bin")], {"dir": E.OUT}])
        g3 = (j or {}).get("result")
        a = [n[0] for n in w.notes(3, 20) if n[1] == g3]
        b = [n[0] for n in w2.notes(3, 20) if n[1] == g3]
        check("两个客户端都收到同一任务的 Complete", "aria2.onDownloadComplete" in a and "aria2.onDownloadComplete" in b, (a, b))
        w2.close()
        j = w.call("aria2.getPeers", [g3], rid="pe")
        check("ws getPeers（HTTP 任务）返回空数组", j and j.get("result") == [], j)
        j = w.call("aria2.getServers", [g3], rid="se")
        check("ws getServers（已完成任务）返回空数组", j and j.get("result") == [], j)
        w.close()

        # 8 带界面令牌的 ws（?t=）能用 sniffdl.* 私有方法
        w = WS(port, path="/jsonrpc?t=" + E.TOKEN)
        j = w.call("sniffdl.ping")
        check("ws 带界面令牌 ?t= 可调 sniffdl.ping", bool(j) and j.get("result"), j)
        w.close()
    finally:
        try:
            E.raw_rpc("sniffdl.core.shutdown", [], timeout=5)
        except Exception:
            pass
        try:
            core.wait(8)
        except Exception:
            core.kill()
        srv.shutdown()
    npass = sum(1 for _, ok in E.RESULTS if ok)
    print("\n结果：%d/%d 通过" % (npass, len(E.RESULTS)))
    return 0 if npass == len(E.RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
