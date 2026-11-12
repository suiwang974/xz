# -*- coding: utf-8 -*-
"""无 Tk 调度器：给沿用的引擎代码提供 root.after / after_cancel 等最小接口。"""
import heapq
import itertools
import threading
import time
import logging

LOG = logging.getLogger("sniffdl.core")


class HeadlessRoot:
    """一个后台线程按时间顺序执行回调；接口与 tk 根窗口的 after 系列一致。"""

    def __init__(self):
        self._q = []
        self._seq = itertools.count(1)
        self._cancel = set()
        self._cv = threading.Condition()
        self._stop = False
        self._th = threading.Thread(target=self._run, name="Core-Scheduler", daemon=True)
        self._th.start()

    def after(self, ms, fn=None, *args):
        if fn is None:
            time.sleep(max(0, ms) / 1000.0)
            return None
        sid = "after#%d" % next(self._seq)
        with self._cv:
            heapq.heappush(self._q, (time.time() + max(0, ms) / 1000.0, sid, fn, args))
            self._cv.notify()
        return sid

    def after_idle(self, fn, *args):
        return self.after(0, fn, *args)

    def after_cancel(self, sid):
        with self._cv:
            self._cancel.add(sid)

    def _run(self):
        while True:
            with self._cv:
                while not self._stop and (not self._q or self._q[0][0] > time.time()):
                    wait = (self._q[0][0] - time.time()) if self._q else 1.0
                    self._cv.wait(timeout=max(0.005, min(wait, 1.0)))
                if self._stop:
                    return
                _ts, sid, fn, args = heapq.heappop(self._q)
                if sid in self._cancel:
                    self._cancel.discard(sid)
                    continue
            try:
                fn(*args)
            except Exception:
                LOG.exception("scheduled callback failed")

    def stop(self):
        with self._cv:
            self._stop = True
            self._cv.notify()

    # ---- tk 根窗口的只读查询：内核没有窗口 ----
    def winfo_exists(self):
        return True

    def winfo_viewable(self):
        return False

    def state(self):
        return "withdrawn"

    def focus_set(self):
        return None

    def title(self, *a):
        return "SniffDL Core"

    def quit(self):
        self.stop()

    def destroy(self):
        self.stop()


class Var:
    """tk.StringVar / BooleanVar 的线程安全替身。"""

    def __init__(self, value=None):
        self._v = value
        self._cbs = []

    def get(self):
        return self._v

    def set(self, v):
        self._v = v
        for cb in list(self._cbs):
            try:
                cb()
            except Exception:
                pass

    def trace_add(self, _mode, cb):
        self._cbs.append(lambda: cb("", "", "write"))
