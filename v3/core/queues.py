# -*- coding: utf-8 -*-
"""多队列 + 同时下载数 + 时间段计划（IDM「队列与计划任务」/ AB DM 多队列）。

数据（存在 cfg["queues"]，列表）：
  {"id": "main", "name": "主队列", "max": 0, "auto": true,
   "schedule": {"enabled": false, "start": "02:00", "stop": "07:00", "days": [0..6]}}
  · max = 该队列同时下载数，0 = 跟随全局「同时下载数」
  · auto = 新任务加入后是否自动开始（false = 「稍后下载」队列，只有手动开始或到点才跑）
  · schedule.enabled：只在 start–stop 时段内跑（跨午夜可，如 23:00–07:00）；到 stop 自动暂停本队列在跑的任务
内核每秒 tick 一次：按队列先后、任务加入先后挑 waiting 任务开跑。做种中的 BT 任务不占名额。
"""
import datetime
import threading
import uuid

MAIN = "main"


def default_queues():
    return [
        {"id": MAIN, "name": "主队列", "max": 0, "auto": True,
         "schedule": {"enabled": False, "start": "02:00", "stop": "07:00", "days": [0, 1, 2, 3, 4, 5, 6]}},
        {"id": "later", "name": "稍后下载", "max": 0, "auto": False,
         "schedule": {"enabled": False, "start": "02:00", "stop": "07:00", "days": [0, 1, 2, 3, 4, 5, 6]}},
    ]


def _hm(s):
    h, m = str(s or "00:00").split(":")[:2]
    return int(h) * 60 + int(m)


def in_window(start, stop, now=None, days=None):
    """now 是否落在 [start, stop) 时段内（支持跨午夜）。days: 允许的星期（0=周一）。"""
    now = now or datetime.datetime.now()
    a, b = _hm(start), _hm(stop)
    cur = now.hour * 60 + now.minute
    if a == b:
        inside = True
        day = now.weekday()
    elif a < b:
        inside = a <= cur < b
        day = now.weekday()
    else:                                   # 跨午夜：23:00–07:00
        inside = cur >= a or cur < b
        day = now.weekday() if cur >= a else (now.weekday() - 1) % 7
    if days is not None and len(days) and day not in days:
        return False
    return inside


class QueueManager:
    def __init__(self, cfg, save_cfg):
        self.cfg = cfg
        self._save = save_cfg
        self.lock = threading.RLock()
        if not isinstance(cfg.get("queues"), list) or not cfg["queues"]:
            cfg["queues"] = default_queues()
        if not any(q.get("id") == MAIN for q in cfg["queues"]):
            cfg["queues"].insert(0, default_queues()[0])
        self.paused_queues = set()          # 用户手动「停止队列」
        self._was_open = {}                 # 计划窗口上一拍的开关状态（检测 stop 边沿）

    def list(self):
        with self.lock:
            return [dict(q) for q in self.cfg["queues"]]

    def get(self, qid):
        for q in self.cfg["queues"]:
            if q.get("id") == qid:
                return q
        return None

    def upsert(self, q):
        with self.lock:
            q = dict(q or {})
            qid = q.get("id") or uuid.uuid4().hex[:8]
            q["id"] = qid
            q["name"] = (str(q.get("name") or "新队列").strip() or "新队列")[:40]
            q["max"] = max(0, min(32, int(q.get("max") or 0)))
            q["auto"] = bool(q.get("auto", True))
            sch = dict(q.get("schedule") or {})
            sch["enabled"] = bool(sch.get("enabled", False))
            for k, dft in (("start", "02:00"), ("stop", "07:00")):
                v = str(sch.get(k) or dft)
                _hm(v)                     # 格式不对直接抛
                sch[k] = "%02d:%02d" % divmod(_hm(v), 60)
            days = sch.get("days")
            sch["days"] = sorted({int(d) for d in (days if isinstance(days, list) else range(7)) if 0 <= int(d) <= 6})
            q["schedule"] = sch
            old = self.get(qid)
            if old is not None:
                old.clear()
                old.update(q)
            else:
                self.cfg["queues"].append(q)
            self._save()
            return dict(q)

    def remove(self, qid):
        with self.lock:
            if qid == MAIN:
                raise ValueError("主队列不能删除")
            self.cfg["queues"] = [q for q in self.cfg["queues"] if q.get("id") != qid]
            self._save()

    def is_open(self, q, now=None):
        """队列此刻是否允许开新任务。"""
        if q.get("id") in self.paused_queues:
            return False
        sch = q.get("schedule") or {}
        if sch.get("enabled"):
            return in_window(sch.get("start"), sch.get("stop"), now, sch.get("days"))
        return True

    def limit_of(self, q, global_max):
        m = int(q.get("max") or 0)
        return m if m > 0 else max(1, int(global_max or 1))

    def plan(self, tasks, global_max, now=None):
        """tasks: [{gid, queue, status, running(bool), seeding(bool), hold(bool), retry_at, added}]
        返回 (to_start:[gid], to_pause:[gid])。
        · 全局同时下载数 + 每队列上限，两道闸都要过
        · 计划窗口刚关上（stop 边沿）→ 把该队列在跑的任务暂停（标记 sched_paused，窗口再开时自动续上）
        """
        import time as _t
        nowts = _t.time()
        with self.lock:
            queues = list(self.cfg["queues"])
        running = [t for t in tasks if t["running"] and not t.get("seeding")]
        total_run = len(running)
        to_start, to_pause = [], []
        for q in queues:
            qid = q["id"]
            open_ = self.is_open(q, now)
            was = self._was_open.get(qid)
            self._was_open[qid] = open_
            sch = q.get("schedule") or {}
            if sch.get("enabled") and was is True and open_ is False:
                to_pause += [t["gid"] for t in running if (t.get("queue") or MAIN) == qid]
                continue
            if not open_:
                continue
            q_run = sum(1 for t in running if (t.get("queue") or MAIN) == qid)
            cap = self.limit_of(q, global_max)
            for t in sorted((x for x in tasks if (x.get("queue") or MAIN) == qid), key=lambda x: x.get("added") or 0):
                if total_run >= max(1, int(global_max or 1)) or q_run >= cap:
                    break
                if t["running"] or t["status"] != "waiting":
                    continue
                if t.get("hold") and not (sch.get("enabled") or t.get("sched_paused")):
                    continue                  # 「稍后下载」：不自动跑（计划窗口打开时例外）
                if not q.get("auto", True) and not sch.get("enabled") and not t.get("sched_paused"):
                    continue
                if t.get("retry_at") and t["retry_at"] > nowts:
                    continue
                to_start.append(t["gid"])
                total_run += 1
                q_run += 1
        return to_start, to_pause
