# -*- coding: utf-8 -*-
"""Windows 侧本地转发桥：把 0.0.0.0:3066 转发到 127.0.0.1:3065（网关）。
让 WSL 里的 s2s 能通过宿主机 IP 访问只监听 Windows 回环的网关。支持流式(SSE)。
用法: python bridge_gateway.py   （Ctrl+C 停）"""
import http.server, socketserver, http.client, threading

LISTEN_PORT = 3066
TARGET_HOST, TARGET_PORT = "127.0.0.1", 3065
DROP_REQ = {"host", "content-length", "connection", "accept-encoding"}
DROP_RESP = {"transfer-encoding", "connection", "content-length"}
import json, os
CFG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.json")
def _current_model():
    try:
        with open(CFG_PATH, encoding="utf-8") as f:
            return (json.load(f).get("rt_model") or "").strip()
    except Exception:
        return ""


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _proxy(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else None
        if body and self.command == "POST" and ("/responses" in self.path or "/chat/completions" in self.path):
            try:
                obj = json.loads(body.decode("utf-8"))
                m = _current_model()
                if m and obj.get("model") != m:
                    obj["model"] = m
                    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            except Exception:
                pass
        headers = {k: v for k, v in self.headers.items() if k.lower() not in DROP_REQ}
        headers["Host"] = "%s:%d" % (TARGET_HOST, TARGET_PORT)
        try:
            conn = http.client.HTTPConnection(TARGET_HOST, TARGET_PORT, timeout=180)
            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()
            self.send_response(resp.status, resp.reason)
            for k, v in resp.getheaders():
                if k.lower() in DROP_RESP:
                    continue
                self.send_header(k, v)
            self.send_header("Connection", "close")
            self.end_headers()
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)
                try:
                    self.wfile.flush()
                except Exception:
                    break
            conn.close()
        except Exception as e:
            try:
                self.send_error(502, str(e)[:120])
            except Exception:
                pass

    def do_GET(self):    self._proxy()
    def do_POST(self):   self._proxy()
    def do_PUT(self):    self._proxy()
    def do_OPTIONS(self): self._proxy()
    def do_DELETE(self): self._proxy()

    def log_message(self, *a):
        pass


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    srv = ThreadingServer(("0.0.0.0", LISTEN_PORT), Handler)
    print("gateway bridge listening on 0.0.0.0:%d -> %s:%d" % (LISTEN_PORT, TARGET_HOST, TARGET_PORT))
    srv.serve_forever()
