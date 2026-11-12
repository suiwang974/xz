# -*- coding: utf-8 -*-
"""令牌桶限速：全局一个桶 + 每任务一个桶。速率单位 字节/秒，0 = 不限。"""
import threading
import time


class TokenBucket:
    def __init__(self, rate=0):
        self.rate = float(rate or 0)
        self._tokens = 0.0
        self._t = time.monotonic()
        self._lock = threading.Lock()

    def set_rate(self, rate):
        with self._lock:
            self.rate = float(rate or 0)
            self._tokens = min(self._tokens, self.rate)

    def consume(self, n, cancel=None):
        """消耗 n 字节配额；配额透支就睡到补齐（可被 cancel() 打断）。长期平均速率 = rate。"""
        if n <= 0:
            return
        with self._lock:
            if self.rate <= 0:
                return
            now = time.monotonic()
            self._tokens = min(self.rate, self._tokens + (now - self._t) * self.rate)
            self._t = now
            self._tokens -= n
            wait = (-self._tokens / self.rate) if self._tokens < 0 else 0.0
        end = time.monotonic() + wait
        while True:
            left = end - time.monotonic()
            if left <= 0 or (cancel and cancel()):
                return
            time.sleep(min(0.2, left))
