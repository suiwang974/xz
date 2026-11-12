# -*- coding: utf-8 -*-
"""事件总线 + SSE。

界面经 GET /events?token=<ui_token> 订阅（EventSource），内核主动推：
  hello / task / task.removed / stats / sniff / clip / intercept / notify / log / settings / queue / bt
每条事件带自增 id；断线重连时 Last-Event-ID 之后的事件从环形缓冲补发（最多 2000 条）。
"""
import collections
import json
import threading
import time


class EventBus:
    def __init__(self, keep=2000):
        self._lock = threading.Condition()
        self._buf = collections.deque(maxlen=keep)
        self._seq = 0
        self.subscribers = 0

    def emit(self, kind, data=None):
        with self._lock:
            self._seq += 1
            ev = (self._seq, kind, data if data is not None else {}, time.time())
            self._buf.append(ev)
            self._lock.notify_all()
            return self._seq

    @property
    def last_id(self):
        return self._seq

    def since(self, last_id, timeout=15.0):
        """阻塞等待 last_id 之后的事件，返回列表（可能为空=心跳）。"""
        deadline = time.time() + timeout
        with self._lock:
            while True:
                out = [e for e in self._buf if e[0] > last_id]
                if out:
                    return out
                left = deadline - time.time()
                if left <= 0:
                    return []
                self._lock.wait(timeout=min(left, 1.0))

    def recent(self, kind=None, n=200):
        with self._lock:
            evs = [e for e in self._buf if kind is None or e[1] == kind]
        return evs[-n:]


def sse_format(ev):
    eid, kind, data, ts = ev
    payload = json.dumps({"type": kind, "data": data, "ts": ts}, ensure_ascii=False)
    return ("id: %d\nevent: %s\ndata: %s\n\n" % (eid, kind, payload)).encode("utf-8")


def serve_sse(handler, bus, last_id=0, on_open=None, should_stop=None):
    """在 BaseHTTPRequestHandler 线程里持续推事件，直到客户端断开。"""
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "keep-alive")
    handler.send_header("X-Accel-Buffering", "no")
    handler.end_headers()
    handler.close_connection = True
    with bus._lock:
        bus.subscribers += 1
    try:
        handler.wfile.write(b"retry: 1500\n\n")
        if on_open:
            for ev in on_open():
                handler.wfile.write(sse_format(ev))
        handler.wfile.flush()
        cur = last_id or bus.last_id
        while not (should_stop and should_stop()):
            evs = bus.since(cur, timeout=12.0)
            if not evs:
                handler.wfile.write(b": ping\n\n")
            for ev in evs:
                cur = ev[0]
                handler.wfile.write(sse_format(ev))
            handler.wfile.flush()
    except Exception:
        pass
    finally:
        with bus._lock:
            bus.subscribers -= 1
