"""SniffDL 动态分片下载引擎（IDM 式）。

工作方式（一句话）：先用一个连接 `Range: bytes=0-` 把整个文件拿下来当作第 0 段，
随后新连接不断从「预计剩余时间最长」的段里“偷”后半截，空闲连接永远有活干，
慢连接被踢下去把剩余区间还回队列；连接数按总吞吐自动增减。

公开 API：
    task = start(url, headers, out_path, on_progress=None, **opts)
    task.pause() / task.resume() / task.cancel() / task.wait() / task.progress()
    download_blocking(url, headers, out_path, prog=None, cancel=None, **opts) -> 路径
"""
import hashlib
import email.utils
import os
import random
import statistics
import threading
import time
from collections import deque

from . import profiles
from .backends import Http, Sidecar, Storage, clean_headers, parse_content_range, read1
from .errors import CancelledError, SpeedError, http_error, network_error

CHUNK = 256 * 1024
TICK = 0.25
SAVE_EVERY = 1.0
PROGRESS_EVERY = 0.5
GROW_EVAL = 1.5          # 每次改连接数后等多久再评估吞吐
GROW_GAIN = 1.05         # 吞吐提升 ≥5% 才继续加（A15：<5% 就停）
SLOW_RATIO = 0.3         # 低于中位速度 0.3× 算慢
SLOW_HOLD = 1.5          # 持续这么久才踢
SLOW_MIN_AGE = 2.0       # 新连接先给 2 秒热身
ENDGAME_MIN = 64 * 1024  # 收尾阶段：慢段可以切到 64K
REF_WINDOW = 10.0
THROTTLE_FACTOR = 0.7
FREEZE_AFTER_THROTTLE = 10.0
FREEZE_AFTER_RESET = 8.0
ALIGN = 64 * 1024
COALESCE = 1024 * 1024

STATES = ("probing", "downloading", "paused", "verifying", "done", "error", "cancelled")


def _retry_after(resp):
    v = resp.headers.get("Retry-After") if resp is not None else None
    if not v:
        return None
    v = v.strip()
    try:
        return max(0.0, float(v))
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(v)
        return max(0.0, dt.timestamp() - time.time())
    except Exception:
        return None


def backoff_delay(attempt, retry_after=None, base=0.5, cap=15.0):
    """服务器给了 Retry-After 就照它等（最多 120 秒，加 0~10% 抖动防止所有连接同一刻扑上去）；
    没给就指数退避 + 抖动。"""
    if retry_after is not None:
        ra = min(120.0, retry_after)
        return ra + random.random() * max(0.05, ra * 0.1)
    d = min(cap, base * (2 ** max(0, attempt)))
    return d * (0.5 + random.random() * 0.5)


class _Segment:
    __slots__ = ("start", "pos", "reserved", "end")

    def __init__(self, start, end):
        self.start = start
        self.pos = start          # 已写盘的位置（侧车按它记）
        self.reserved = start     # 已读到、正在写的位置（切分按它算）
        self.end = end            # 不含；只会被缩短

    @property
    def remaining(self):
        return self.end - self.reserved


class _Worker:
    _ids = 0

    def __init__(self, task):
        _Worker._ids += 1
        self.id = _Worker._ids
        self.task = task
        self.http = task._new_http()
        self.seg = None
        self.resp = None
        self.bytes = 0                  # 当前连接累计字节
        self.conn_start = time.monotonic()
        self.hist = deque()             # (t, bytes)
        self.speed = 0.0
        self.slow_since = None
        self.demoted = False
        self.attempt = 0
        self.last_data = time.monotonic()
        self.thread = None
        self.open_ended = False         # 正在读探测时那条开放区间响应（一直读到文件尾）

    def new_connection(self):
        self.http.close()
        self.http = self.task._new_http()
        self.bytes = 0
        self.conn_start = time.monotonic()
        self.hist.clear()
        self.speed = 0.0
        self.slow_since = None
        self.demoted = False


class DownloadTask:
    def __init__(self, url, headers, out_path, on_progress=None, *, expected_hash=None,
                 proxies=None, verify=True, timeout=(15, 30), profile=None,
                 stall_timeout=60.0, max_probe_retries=8, chunk=CHUNK, limiter=None,
                 tmp_suffix=".sdltmp", side_suffix=".sdlpart", trust_env=True, method="GET", data=None):
        self.url = url
        # POST 下载：每条连接重发同一请求体 + Range（Gopeed 做法）；首个响应就是第 0 段、一直读着不丢，
        # 一次性链接也不怕。服务器不认 Range 时自然落回单流。
        self.method = (method or "GET").upper()
        self.data = data if self.method != "GET" else None
        if isinstance(self.data, str):
            self.data = self.data.encode("utf-8")
        self.f_method, self.f_data = self.method, self.data
        self.headers = clean_headers(headers or {})
        self.out_path = os.path.abspath(out_path)
        self.tmp_path = self.out_path + (tmp_suffix or ".sdltmp")
        self.sidecar = Sidecar(self.out_path + (side_suffix or ".sdlpart"))
        # limiter：限速器（可选）。要有 consume(n, cancel) —— 读完 n 字节后调用，配额不够就睡；
        # 可选 rate() -> 当前限速 B/s（0 = 不限）。在每条连接自己的线程里调用，限速时读块会变小，
        # 让 TCP 背压把服务器也压下来，而不是读一大块再长睡。
        self.limiter = limiter
        self.trust_env = trust_env
        self._lim_rate = 0
        self.on_progress = on_progress
        self.expected_hash = expected_hash
        self.proxies = proxies
        self.verify = verify
        self.timeout = timeout
        self.profile = profile if isinstance(profile, profiles.HostProfile) else \
            profiles.resolve(url, profile if isinstance(profile, dict) else None)
        self.stall_timeout = stall_timeout
        self.max_probe_retries = max_probe_retries
        self.chunk = chunk

        self._lk = threading.RLock()
        self._ctl = None
        self._stop = threading.Event()
        self._user_action = None        # "pause" / "cancel"
        self._done_evt = threading.Event()
        self.state = "probing"
        self.error = None
        self.total = None
        self.done_bytes = 0
        self.final_url = url
        self.etag = None
        self.last_modified = None
        self.single = False
        self.speed = 0.0
        self.stats = {"steals": 0, "demotions": 0, "throttles": 0, "resets": 0, "max_conn": 0,
                      "requests": 0, "resumed_from": 0, "cap": self.profile.max_conn, "ceiling": 0}
        self._reset_runtime()

    # ------------------------------------------------------------------ 公开 API
    def start(self):
        with self._lk:
            if self._ctl and self._ctl.is_alive():
                return self
            self._stop = threading.Event()
            self._user_action = None
            self._done_evt.clear()
            self.error = None
            self._set_state("probing")
            self._ctl = threading.Thread(target=self._controller, name="sdl-speed-ctl", daemon=True)
            self._ctl.start()
        return self

    def pause(self):
        """暂停：所有连接停下，剩余区间写进侧车；resume() 或下次 start() 从这里接着下。"""
        self._user_action = "pause"
        self._stop.set()
        self._join()
        return self.progress()

    def resume(self):
        if self.state in ("paused", "error", "cancelled"):
            self._reset_runtime()
            self.start()
        return self

    def cancel(self, discard=False):
        """取消。默认保留 .sdltmp/.sdlpart（以后还能续传）；discard=True 删掉。"""
        self._user_action = "cancel"
        self._stop.set()
        self._join()
        if discard:
            self.sidecar.remove()
            try:
                os.remove(self.tmp_path)
            except OSError:
                pass
        return self.progress()

    def wait(self, timeout=None):
        self._done_evt.wait(timeout)
        return self.progress()

    def result(self, timeout=None):
        """阻塞到结束；成功返回最终路径，失败抛 SpeedError。"""
        self.wait(timeout)
        if self.state == "done":
            return self.out_path
        if self.state == "cancelled":
            raise CancelledError()
        if self.error:
            raise self.error
        raise SpeedError("http", "下载未完成（状态 %s）" % self.state)

    def progress(self):
        with self._lk:
            total = self.total
            b = self.done_bytes
            spd = self.speed
            conns = sum(1 for w in self._workers if w.seg is not None)
            if self.single and self.state == "downloading":
                conns = 1
        eta = None
        if total is not None and spd > 0:
            eta = max(0.0, (total - b) / spd)
        e = self.error
        return {"state": self.state, "bytes": b, "total": total, "speed": spd,
                "connections": conns, "eta": eta,
                "error": str(e) if e else None, "error_kind": e.kind if e else None,
                "status": e.status if e else None, "path": self.out_path}

    # ------------------------------------------------------------------ 内部：通用
    def _reset_runtime(self):
        self._pending = []              # [(a, b)]，按 a 排序
        self._workers = []
        self._fatal = None
        self.target = 1
        self._cap = self.profile.max_conn
        self._last_change = time.monotonic()
        self._base_tp = None
        self._prev_target = None
        self._grow_done = False
        self._split_rejected = False    # 额外连接被 401/403 拒（一次性链接）→ 不再切开放流
        self._frozen_until = 0.0
        self._last_throttle = 0.0
        self._cooled = False
        self._gbytes = deque()          # (t, done_bytes) 给总速度和增长评估用
        self._speed_hist = deque()      # (t, 单连接速度) 给“中位速度”用
        self._last_progress = time.monotonic()
        self._last_err = None
        self._storage = None
        self.single = False

    def _new_http(self):
        return Http(proxies=self.proxies, verify=self.verify, timeout=self.timeout,
                    trust_env=self.trust_env)

    # ---- 限速
    def _limit_rate(self):
        lm = self.limiter
        if lm is None:
            return 0
        fn = getattr(lm, "rate", None)
        if fn is None:
            return 1 << 40          # 只有 consume：当作“有限速但速率未知”，读块用默认
        try:
            r = fn() if callable(fn) else fn
            return max(0, int(r or 0))
        except Exception:
            return 0

    def _read_sizes(self):
        """返回 (单次 read1 上限, 攒批上限)。不限速：256K / 1M；限速时按每连接份额缩小，
        让一次扣的配额只够约 1/4~1/2 秒，限速曲线平滑、取消也快。"""
        r = self._lim_rate
        if r <= 0 or r >= 1 << 40:
            return self.chunk, COALESCE
        n = max(1, sum(1 for w in self._workers if w.seg is not None)) if not self.single else 1
        per = r / n
        blk = int(max(16 * 1024, min(self.chunk, per / 4)))
        co = int(max(blk, min(COALESCE, per / 2)))
        return blk, co

    def _throttle(self, n):
        lm = self.limiter
        if lm is None or n <= 0:
            return
        try:
            lm.consume(n, cancel=self._stop.is_set)
        except TypeError:
            lm.consume(n)
        except Exception:
            pass

    def _join(self):
        t = self._ctl
        if t and t.is_alive() and t is not threading.current_thread():
            t.join()

    def _set_state(self, st):
        self.state = st
        self._emit(force=True)

    def _emit(self, force=False):
        now = time.monotonic()
        if not force and now - getattr(self, "_last_emit", 0) < PROGRESS_EVERY:
            return
        self._last_emit = now
        cb = self.on_progress
        if cb:
            try:
                cb(self.progress())
            except Exception:
                pass

    def _remaining_ranges(self):
        with self._lk:
            rs = list(self._pending)
            for w in self._workers:
                s = w.seg
                if s is not None and s.end > s.pos:
                    rs.append((s.pos, s.end))
        rs.sort()
        merged = []
        for a, b in rs:
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        return merged

    def _save_sidecar(self):
        if self.single or self.total is None:
            return
        self.sidecar.save(self.url, self.total, self.etag, self.last_modified, self._remaining_ranges())

    def _remaining_bytes(self):
        with self._lk:
            n = sum(b - a for a, b in self._pending)
            for w in self._workers:
                s = w.seg
                if s is not None:
                    n += max(0, s.end - s.pos)
        return n

    # ------------------------------------------------------------------ 控制线程
    def _controller(self):
        try:
            self._probe_and_run()
        except SpeedError as e:
            self.error = e
        except Exception as e:      # 兜底：任何意外都变成中文错误，不让线程静默死掉
            self.error = SpeedError("http", "下载引擎内部错误", detail=repr(e))
        finally:
            self._stop.set()
            for w in list(self._workers):
                if w.thread:
                    w.thread.join(timeout=5)
            storage = self._storage
            if storage is not None and self.state != "done":
                try:
                    storage.sync()
                except Exception:
                    pass
                storage.close()
            if self.state != "done":
                if self.error and self.error.kind in ("changed", "verify"):
                    self.sidecar.remove()
                else:
                    self._save_sidecar()
                if self._user_action == "pause" and not self.error:
                    self._set_state("paused")
                elif self._user_action == "cancel" and not self.error:
                    self._set_state("cancelled")
                else:
                    if self.error is None:
                        self.error = SpeedError("http", "下载中断")
                    self._set_state("error")
            self._done_evt.set()

    def _check_user_stop(self):
        if self._stop.is_set() and self._user_action:
            raise _UserStop()

    def _probe_and_run(self):
        side = self.sidecar.load()
        resume = None
        if side and side.get("url") == self.url and side.get("ranges") and os.path.exists(self.tmp_path):
            resume = side
        elif side:
            self.sidecar.remove()
        start_at = resume["ranges"][0][0] if resume else 0

        http = self._new_http()
        attempt = 0
        while True:
            try:
                self._check_user_stop()
            except _UserStop:
                http.close()
                return None
            self.stats["requests"] += 1
            try:
                resp, final, hdrs = http.get(self.url, self.headers, start=start_at, method=self.method, data=self.data)
            except SpeedError as e:
                if e.kind != "network" or attempt >= self.max_probe_retries:
                    raise
                attempt += 1
                self._stop.wait(backoff_delay(attempt))
                continue
            st = resp.status_code
            if st in (429, 503) or (500 <= st < 600 and st not in (501, 505)):
                ra = _retry_after(resp)
                resp.close()
                if attempt >= self.max_probe_retries:
                    raise http_error(st)
                attempt += 1
                self.stats["throttles"] += 1 if st in (429, 503) else 0
                self._stop.wait(backoff_delay(attempt - 1, ra))
                continue
            if st == 416 and resume:
                resp.close()                 # 侧车对不上（文件变短了）：从头来
                self.sidecar.remove()
                resume, start_at = None, 0
                continue
            if st == 416:
                cr = parse_content_range(resp.headers.get("Content-Range", "").replace("*", "0-0", 1)) \
                    if resp.headers.get("Content-Range", "").startswith("bytes */") else None
                resp.close()
                if cr and cr[2] == 0:          # 空文件
                    self.total = 0
                    return self._finish_empty()
                raise http_error(416)
            if st not in (200, 206):
                resp.close()
                raise http_error(st)
            break

        self.final_url = final
        self.headers = hdrs                  # 含重定向时拿到的 Cookie
        self.f_method = getattr(http, "final_method", self.method)
        self.f_data = getattr(http, "final_data", self.data)
        self.cdisp = resp.headers.get("Content-Disposition") or ""
        self.etag = resp.headers.get("ETag")
        self.last_modified = resp.headers.get("Last-Modified")

        cr = parse_content_range(resp.headers.get("Content-Range")) if st == 206 else None
        if st == 206 and cr and cr[2] is not None and cr[0] == start_at:
            total = cr[2]
            if resume:
                same = resume.get("total") == total
                if same and resume.get("etag") and self.etag and resume["etag"] != self.etag:
                    same = False
                if same and resume.get("last_modified") and self.last_modified and \
                        resume["last_modified"] != self.last_modified:
                    same = False
                if not same:                 # 服务器上的文件换了：丢掉旧进度重新开始
                    resp.close()
                    self.sidecar.remove()
                    self.done_bytes = 0
                    return self._restart_fresh()
            self.total = total
            return self._run_multi(http, resp, resume)
        # 200（不支持 Range）或 206 但总长未知：单流
        if resume:
            self.sidecar.remove()
            if start_at != 0:
                resp.close()
                return self._restart_fresh()
        self.total = None
        if st == 200:
            cl = resp.headers.get("Content-Length")
            if cl and cl.isdigit():
                self.total = int(cl)
        elif cr and cr[2] is not None:
            self.total = cr[2]
        return self._run_single(http, resp)

    def _restart_fresh(self):
        self.sidecar.remove()
        try:
            os.remove(self.tmp_path)
        except OSError:
            pass
        return self._probe_and_run()

    def _finish_empty(self):
        st = Storage(self.tmp_path, 0)
        st.close()
        self._finalize(None)
        return None

    # ------------------------------------------------------------------ 单流
    def _run_single(self, http, resp):
        self.single = True
        self._poll_limit()
        self.done_bytes = 0
        storage = Storage(self.tmp_path, None)
        self._storage = storage
        self._set_state("downloading")
        tries = 0
        while True:
            off = 0
            try:
                while True:
                    if self._stop.is_set():
                        resp.close()
                        http.close()
                        return storage
                    data = read1(resp, self._read_sizes()[1] if self._lim_rate else 1024 * 1024)
                    if not data:
                        break
                    storage.write(off, data)
                    self._throttle(len(data))
                    off += len(data)
                    with self._lk:
                        self.done_bytes = off
                    self._poll_limit()
                    self._tick_speed()
                    self._emit()
                resp.close()
                if self.total is not None and off != self.total:
                    raise SpeedError("network", "连接提前结束（数据不完整）",
                                     detail="got %d of %d" % (off, self.total))
                break
            except SpeedError as e:
                if e.kind not in ("network",) or tries >= 3:
                    raise
            except Exception as e:
                if tries >= 3:
                    raise network_error(e)
            # 不支持 Range 的服务器只能从头再来
            tries += 1
            try:
                resp.close()
            except Exception:
                pass
            self._stop.wait(backoff_delay(tries))
            storage.truncate(0)
            with self._lk:
                self.done_bytes = 0
            resp, _, _ = http.get(self.final_url, self.headers, start=0, method=self.f_method, data=self.f_data)
            if resp.status_code not in (200, 206):
                st = resp.status_code
                resp.close()
                raise http_error(st)
        http.close()
        if self.total is None:
            self.total = off
        self._finalize(storage)
        return storage

    # ------------------------------------------------------------------ 多连接
    def _run_multi(self, http, resp, resume):
        total = self.total
        keep = bool(resume)
        storage = Storage(self.tmp_path, total, keep=keep)
        self._storage = storage
        if total == 0:
            resp.close()
            self._finalize(storage)
            return storage
        if resume:
            self._pending = sorted(resume["ranges"])
            self.done_bytes = total - sum(b - a for a, b in self._pending)
            self.stats["resumed_from"] = self.done_bytes
        else:
            self._pending = [(0, total)]
            self.done_bytes = 0
        self._poll_limit()
        prof = self.profile
        self.min_split = prof.min_split
        self.target = max(1, min(prof.start_conn, self._work_slots()))
        self._last_change = time.monotonic()
        self._last_progress = time.monotonic()
        self._set_state("downloading")

        # worker0 直接用探测响应
        w0 = _Worker(self)
        w0.http.close()
        w0.http = http
        a, b = self._pending.pop(0)
        seg = _Segment(a, b)
        w0.seg = seg
        self._workers.append(w0)
        self._spawn(w0, (seg, resp))

        last_save = time.monotonic()
        while True:
            if self._stop.wait(TICK):
                break
            now = time.monotonic()
            self._poll_limit(now)
            self._tick_speed()
            self._detect_slow(now)
            self._adapt(now)
            self._maybe_spawn()
            if now - last_save >= SAVE_EVERY:
                self._save_sidecar()
                last_save = now
            self._emit()
            if self._remaining_bytes() == 0:
                break
            if now - self._last_progress > self.stall_timeout:
                self._fatal = self._fatal or self._last_err or \
                    SpeedError("network", "长时间没有任何进度")
                break
            if self._fatal:
                break
        if self._fatal and not self._user_action:
            self._stop.set()
            raise self._fatal
        if self._user_action:
            self._stop.set()
            return storage
        # 全部下完：等 worker 退出
        self._stop.set()
        for w in list(self._workers):
            if w.thread:
                w.thread.join(timeout=10)
        self._finalize(storage)
        return storage

    def _work_slots(self):
        rem = self._remaining_bytes() if self._workers else sum(b - a for a, b in self._pending)
        return max(1, rem // max(1, self.profile.min_split))

    def _spawn(self, w, first=None):
        t = threading.Thread(target=self._worker_main, args=(w, first), name="sdl-speed-w%d" % w.id,
                             daemon=True)
        w.thread = t
        t.start()
        with self._lk:
            self.stats["max_conn"] = max(self.stats["max_conn"], len(self._workers))

    def _maybe_spawn(self):
        while True:
            with self._lk:
                if self._stop.is_set() or len(self._workers) >= self.target:
                    return
                if not self._pending and self._pick_victim() is None:
                    return
                w = _Worker(self)
                self._workers.append(w)
            self._spawn(w)

    # ---- 分配与窃取（都在锁内）
    def _ref_speed(self):
        now = time.monotonic()
        while self._speed_hist and now - self._speed_hist[0][0] > REF_WINDOW:
            self._speed_hist.popleft()
        vals = [s for _, s in self._speed_hist]
        return statistics.median(vals) if vals else 0.0

    def _pick_victim(self):
        """返回 (受害 worker, 本次最小切分)。先按 min_split 找；找不到再进收尾模式：
        明显偏慢（< 0.5× 中位速度）且还要 3 秒以上的段，可以切到 64K。"""
        ms = self.min_split
        ref = self._ref_speed()
        refv = ref or 1.0
        best, best_eta = None, -1.0
        for w in self._workers:
            s = w.seg
            if s is None or w.demoted or (self._split_rejected and getattr(w, "open_ended", False)):
                continue
            rem = s.remaining
            if rem < 2 * ms:
                continue
            spd = w.speed if w.speed > 0 else refv
            eta = rem / max(spd, 1.0)
            if eta > best_eta:
                best, best_eta = w, eta
        if best is not None:
            return best, ms
        if ref <= 0:
            return None
        for w in self._workers:
            s = w.seg
            if s is None or w.demoted or w.speed <= 0 or (self._split_rejected and getattr(w, "open_ended", False)):
                continue
            rem = s.remaining
            if rem < 2 * ENDGAME_MIN or w.speed >= 0.5 * ref:
                continue
            eta = rem / w.speed
            if eta > 3.0 and eta > best_eta:
                best, best_eta = w, eta
        return (best, ENDGAME_MIN) if best is not None else None

    def _acquire(self, w):
        with self._lk:
            if self._stop.is_set():
                return None
            if len(self._workers) > self.target:
                return None
            if self._pending:
                a, b = self._pending.pop(0)
                seg = _Segment(a, b)
                w.seg = seg
                return seg
            pv = self._pick_victim()
            if pv is None:
                return None
            v, ms = pv
            s = v.seg
            ref = self._ref_speed()
            sv = v.speed if v.speed > 0 else (ref or 1.0)
            sn = w.speed if w.speed > 0 else (ref or sv)
            rem = s.remaining
            keep = int(rem * sv / (sv + sn))
            keep = max(ms, min(rem - ms, keep))
            cut = s.reserved + keep
            al = ALIGN if ms >= ALIGN * 2 else 4096
            cut = max(s.reserved + ms, (cut // al) * al)
            if s.end - cut < ms:
                return None
            seg = _Segment(cut, s.end)
            s.end = cut
            w.seg = seg
            self.stats["steals"] += 1
            return seg

    def _give_back(self, w, rejected=False):
        """把 worker 手里没下完的区间还回队列。rejected=True（分片请求被 401/403 拒）且这段一个字节没下、
        正好接在开放区间流后面 → 直接并回那条流（它本来就会一路读到文件尾），一次性链接也能单流下完。"""
        with self._lk:
            s = w.seg
            w.seg = None
            if s is None or s.end <= s.pos:
                return
            if rejected:
                for o in self._workers:
                    os_ = o.seg
                    if o is not w and getattr(o, "open_ended", False) and os_ is not None and os_.end == s.pos:
                        os_.end = s.end
                        self._split_rejected = True
                        return
            self._pending.append((s.pos, s.end))
            self._pending.sort()

    # ---- worker
    def _worker_main(self, w, first):
        try:
            while not self._stop.is_set():
                if first is not None:
                    seg, resp = first
                    first = None
                else:
                    seg = self._acquire(w)
                    if seg is None:
                        break
                    resp = None
                try:
                    self._run_segment(w, seg, resp)
                    w.attempt = 0
                except _Retry as r:
                    self._give_back(w, rejected=r.rejected)
                    if r.new_conn:
                        w.new_connection()
                    w.attempt += 1
                    if self._stop.wait(r.delay if r.delay is not None else backoff_delay(w.attempt)):
                        break
                except SpeedError as e:
                    self._give_back(w)
                    self._fatal = self._fatal or e
                    self._stop.set()
                    break
                except Exception as e:      # 意外异常：当网络错处理
                    self._give_back(w)
                    self._last_err = network_error(e)
                    w.new_connection()
                    w.attempt += 1
                    if self._stop.wait(backoff_delay(w.attempt)):
                        break
                if w.demoted:
                    w.new_connection()      # 慢连接：换一条新 TCP 再来
        finally:
            self._give_back(w)
            w.http.close()
            with self._lk:
                if w in self._workers:
                    self._workers.remove(w)

    def _run_segment(self, w, seg, resp):
        if resp is None:
            with self._lk:
                a, e = seg.pos, seg.end
            self.stats["requests"] += 1
            try:
                resp, _, _ = w.http.get(self.final_url, self.headers, start=a, end=e - 1,
                                         method=self.f_method, data=self.f_data)
            except SpeedError as err:
                self._last_err = err
                self._on_reset()
                raise _Retry(new_conn=True)
            st = resp.status_code
            if st != 206:
                ra = _retry_after(resp)
                resp.close()
                self._handle_bad_status(w, st, ra)
            cr = parse_content_range(resp.headers.get("Content-Range"))
            if not cr or cr[0] != a:
                resp.close()
                self._last_err = SpeedError("http", "服务器返回的分段位置不对")
                raise _Retry(new_conn=True)
            if cr[2] is not None and cr[2] != self.total:
                resp.close()
                raise SpeedError("changed", "服务器上的文件已变化（大小不一致）")
            req_end = e
        else:
            req_end = None              # 探测响应：开放区间
            w.open_ended = True
        w.resp = resp
        drained = False
        try:
            while True:
                if self._stop.is_set():
                    return
                try:
                    data, eof = self._gather(resp, seg)
                except Exception as err:
                    self._last_err = network_error(err)
                    self._on_reset()
                    raise _Retry(new_conn=True)
                if data:
                    with self._lk:
                        allowed = min(len(data), seg.end - seg.reserved)
                        off = seg.reserved
                        seg.reserved += max(0, allowed)
                    if allowed > 0:
                        self._storage.write(off, data[:allowed] if allowed < len(data) else data)
                    self._throttle(len(data))
                    now = time.monotonic()
                    with self._lk:
                        if allowed > 0:
                            seg.pos += allowed
                            w.bytes += allowed
                            self.done_bytes += allowed
                            w.last_data = now
                            self._last_progress = now
                        finished = seg.pos >= seg.end
                        shrunk = req_end is None or seg.end != req_end
                    if finished:
                        drained = (not shrunk) and allowed == len(data)
                        break
                if eof:
                    with self._lk:
                        short = seg.pos < seg.end
                    if short:
                        self._last_err = SpeedError("network", "连接提前结束")
                        self._on_reset()
                        raise _Retry(new_conn=True)
                    drained = True
                    break
        finally:
            w.resp = None
            w.open_ended = False
            if drained and req_end is not None:
                try:
                    resp.raw.release_conn()   # 读满了：连接放回池子，下一段复用
                except Exception:
                    resp.close()
            else:
                resp.close()
            with self._lk:
                if w.seg is seg and seg.pos >= seg.end:
                    w.seg = None

    def _gather(self, resp, seg):
        """攒一批再写：read1 一次只给一个 TLS 记录（≤16K），攒到 COALESCE 或 50ms 再落盘，
        少拿锁、少 pwrite。返回 (bytes, 是否到流尾)。"""
        blk, co = self._read_sizes()
        first = read1(resp, blk)
        if not first:
            return b"", True
        want = min(co, seg.end - seg.reserved)   # 无锁读，只用来提前停
        if len(first) >= want:
            return first, False
        parts = [first]
        n = len(first)
        t_end = time.monotonic() + 0.05
        while n < want and time.monotonic() < t_end:
            b = read1(resp, min(blk, want - n))
            if not b:
                return b"".join(parts), True
            parts.append(b)
            n += len(b)
        return b"".join(parts), False

    def _handle_bad_status(self, w, st, ra):
        if st in (404, 410, 416):
            raise http_error(st)
        if st == 200:
            # 服务器这次不认 Range 了：少开一条连接再试
            self._last_err = SpeedError("http", "服务器拒绝分段请求")
            self._on_reset()
            raise _Retry(new_conn=True)
        if st in (429, 503, 401, 403):
            # 401/403：探测成功后才出现，多半是连接数太多被网盘拒；只剩一条连接还被拒才算致命
            if st in (401, 403):
                with self._lk:
                    alone = len(self._workers) <= 1
                if alone and w.attempt >= 2:
                    raise http_error(st)
            self._last_err = http_error(st)
            self._on_throttle(st)
            raise _Retry(delay=backoff_delay(w.attempt, ra), new_conn=False, rejected=st in (401, 403))
        if 500 <= st < 600:
            self._last_err = http_error(st)
            raise _Retry(new_conn=True)
        raise http_error(st)

    # ---- 速度、慢连接、自适应
    def _tick_speed(self):
        now = time.monotonic()
        with self._lk:
            self._gbytes.append((now, self.done_bytes))
            while len(self._gbytes) > 2 and now - self._gbytes[0][0] > 3.0:
                self._gbytes.popleft()
            t0, b0 = self._gbytes[0]
            win = now - t0
            self.speed = (self.done_bytes - b0) / win if win > 0.2 else self.speed
            for w in self._workers:
                w.hist.append((now, w.bytes))
                while len(w.hist) > 2 and now - w.hist[0][0] > 2.0:
                    w.hist.popleft()
                ht, hb = w.hist[0]
                if now - ht >= 1.0:
                    w.speed = (w.bytes - hb) / (now - ht)
                    if w.seg is not None:
                        self._speed_hist.append((now, w.speed))

    def _throughput(self, window=1.0):
        now = time.monotonic()
        with self._lk:
            pts = [p for p in self._gbytes if now - p[0] <= window + 0.01]
            if len(pts) < 2:
                return 0.0
            return (pts[-1][1] - pts[0][1]) / max(0.01, pts[-1][0] - pts[0][0])

    def _poll_limit(self, now=None):
        """每个控制周期读一次限速。速率变了：重新评估连接数（限速解除后还能继续加连接）。"""
        r = self._limit_rate()
        if r != self._lim_rate:
            with self._lk:
                self._lim_rate = r
                self._grow_done = False
                self._base_tp = None
                self._last_change = now or time.monotonic()

    def _detect_slow(self, now):
        if 0 < self._lim_rate < 1 << 40:
            return      # 限速时各连接轮流等令牌，速度高低不代表服务器快慢：不踢慢连接
        with self._lk:
            ref = self._ref_speed()
            if ref <= 0:
                return
            for w in self._workers:
                s = w.seg
                if s is None or w.demoted or now - w.conn_start < SLOW_MIN_AGE:
                    w.slow_since = None
                    continue
                if w.speed < SLOW_RATIO * ref:
                    if w.slow_since is None:
                        w.slow_since = now
                    elif now - w.slow_since >= SLOW_HOLD and s.end > s.reserved and \
                            (s.end - s.reserved) / max(w.speed, 1.0) > 1.0:
                        # 剩下的按它现在的速度还要 1 秒以上才值得踢（新请求本身也有开销）
                        # 慢连接：end 截到当前位置，剩下的放回队列给快连接
                        self._pending.append((s.reserved, s.end))
                        self._pending.sort()
                        s.end = s.reserved
                        w.demoted = True
                        self.stats["demotions"] += 1
                        if now - w.last_data > 8.0 and w.resp is not None:
                            try:
                                w.resp.close()   # 完全卡死的连接：直接掐断
                            except Exception:
                                pass
                else:
                    w.slow_since = None

    def _adapt(self, now):
        with self._lk:
            if self._grow_done or now < self._frozen_until:
                return
            if len(self._workers) < self.target or now - self._last_change < GROW_EVAL:
                return
            tp = self._throughput(1.0)
            if self._base_tp is None or tp >= self._base_tp * GROW_GAIN:
                slots = self._work_slots()
                if self.target >= self._cap or len(self._workers) >= slots:
                    self._grow_done = True
                    return
                # 上一步提升明显（≥40%）就翻倍，否则只加一半
                # 被限流/断连过就只走半步
                gain = tp / self._base_tp if self._base_tp else (1.0 if self._cooled else 2.0)
                step = self.target if gain >= 1.4 else max(2, self.target // 2)
                self._base_tp = tp
                self._prev_target = self.target
                self.target = min(self._cap, self.target + step, max(1, slots))
                self._last_change = now
            else:
                # 这批连接带来的提升 <5%：停止加，并退回上一档（多开的连接白占服务器、还招限流）
                self._grow_done = True
                pt = getattr(self, "_prev_target", None)
                if pt and pt < self.target:
                    self.target = pt
                self.stats["auto_stop"] = self.target

    def _on_throttle(self, status=None):
        now = time.monotonic()
        with self._lk:
            self.stats["throttles"] += 1
            if now - self._last_throttle < 1.0:   # 同一波 429 只降一次
                return
            self._last_throttle = now
            n = max(len(self._workers), self.target)
            tgt = max(1, int(round(n * THROTTLE_FACTOR)))
            # 正在出数据的连接数 ≈ 服务器肯给的并发（多出来的那批正吃 429/503）：学成本任务的上限，
            # 之后再加连接也不越过它，避免「降→加→又被拒」来回撞墙（A15）
            live = sum(1 for w in self._workers if w.resp is not None and w.seg is not None)
            # 只认 429/503（明确的「太多了」）；401/403 多半是一次性链接拒分片，交给 _give_back 并回开放流
            if status in (429, 503) and live >= 1 and live < n:
                tgt = min(tgt, live)
                self._cap = max(1, min(self._cap, live))
                self.stats["ceiling"] = self._cap
            self.target = tgt
            self._frozen_until = now + FREEZE_AFTER_THROTTLE
            self._cooled = True
            self._base_tp = None
            self._grow_done = False
            self._last_change = now

    def _on_reset(self):
        now = time.monotonic()
        with self._lk:
            self.stats["resets"] += 1
            self.target = max(1, self.target - 1)
            self._frozen_until = now + FREEZE_AFTER_RESET
            self._cooled = True
            self._base_tp = None
            self._grow_done = False
            self._last_change = now

    # ------------------------------------------------------------------ 收尾
    def _finalize(self, storage):
        self._set_state("verifying")
        if storage is not None:
            storage.sync()
            size = storage.size()
            storage.close()
        else:
            size = os.path.getsize(self.tmp_path)
        if self.total is not None and size != self.total:
            raise SpeedError("verify", "文件大小校验失败（%d / %d）" % (size, self.total))
        if self.expected_hash:
            algo, _, want = str(self.expected_hash).partition(":")
            if not want:
                algo, want = "sha256", algo
            got = file_hash(self.tmp_path, algo)
            if got.lower() != want.lower():
                try:
                    os.remove(self.tmp_path)
                except OSError:
                    pass
                raise SpeedError("verify", "%s 校验失败（文件已损坏）" % algo.upper())
        os.replace(self.tmp_path, self.out_path)
        self.sidecar.remove()
        with self._lk:
            self.done_bytes = self.total if self.total is not None else size
        self._set_state("done")


class _Retry(Exception):
    def __init__(self, delay=None, new_conn=False, rejected=False):
        super().__init__("retry")
        self.delay = delay
        self.new_conn = new_conn
        self.rejected = rejected


class _UserStop(Exception):
    pass


def file_hash(path, algo="sha256"):
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        while True:
            b = f.read(4 * 1024 * 1024)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def start(url, headers=None, out_path=None, on_progress=None, **opts):
    """开始下载，立即返回 DownloadTask。"""
    if not out_path:
        raise ValueError("out_path 必填")
    d = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(d, exist_ok=True)
    return DownloadTask(url, headers, out_path, on_progress, **opts).start()


def download_blocking(url, headers, out_path, prog=None, cancel=None, poll=0.25, status_cb=None, **opts):
    """SniffDL 用的阻塞版本。

    prog(dict) 收到 {"status": "downloading", "downloaded_bytes", "total_bytes", "speed"}（和
    yt-dlp 进度钩子同形），cancel() 返回真就取消。成功返回路径；失败抛 SpeedError
    （.kind / .status / 中文 str），取消抛 CancelledError。
    status_cb(str)（可选）收到中文阶段文字，例如「多连接下载中（12 路）」。
    """
    last = [0.0]
    phase = [None]

    def _cb(p):
        if status_cb is not None:
            st = p["state"]
            msg = {"probing": "连接服务器中…", "verifying": "校验文件中…", "paused": "已暂停"}.get(st)
            if st == "downloading":
                msg = "多连接下载中（%d 路）" % max(1, p["connections"] or 1)
            if msg and msg != phase[0]:
                phase[0] = msg
                try:
                    status_cb(msg)
                except Exception:
                    pass
        if prog is None or p["state"] not in ("downloading", "verifying", "done"):
            return
        now = time.monotonic()
        if p["state"] == "downloading" and now - last[0] < 0.4:
            return
        last[0] = now
        try:
            prog({"status": "downloading", "downloaded_bytes": p["bytes"],
                  "total_bytes": p["total"], "speed": p["speed"], "eta": p["eta"],
                  "connections": p["connections"]})
        except Exception:
            pass

    task = start(url, headers, out_path, _cb, **opts)
    while not task._done_evt.wait(poll):
        if cancel is not None:
            try:
                c = cancel()
            except Exception:
                c = False
            if c:
                task.cancel()
                raise CancelledError()
    return task.result()
