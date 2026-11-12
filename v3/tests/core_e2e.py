# -*- coding: utf-8 -*-
"""WP1 提速引擎接线端到端：起真内核（tests/ui_smoke_core.py，隔离数据目录）+ 本地 HTTP 文件服务器，
经 /jsonrpc 真实添加任务，验证：

  modes    speed_engine = auto / aria2 / sdl 三种模式真实下载（sha256 校验、任务 engine 字段、日志）
  fallback auto：动态分片被拒（首个请求 400）→ 回退 aria2 成功；sdl：同情况直接失败、不走单连接兜底
  limit    全局限速（动态分片）与单任务限速（aria2）
  pause    暂停 → .sdl/.sdlpart 保留 → 继续只补剩下的；resume_policy=restart 从头；
           resume_policy=always 遇到服务器不再认 Range → 报错且保留进度；auto 同情况从头下完
  cancel   cancel_cleanup = always / never / ask(第3参 true/false)，sdl 与 aria2 两种引擎的临时文件
  diskfull 磁盘满（4MB tmpfs，unshare -rm 重入）：预检不开始；aria2/sdl/auto 写满后停下、提示磁盘、不回退单连接、不刷重试
  incdir   incomplete_dir：下载中临时文件在未完成目录、保存位置没有；完成后移到保存位置、未完成目录无残留

用法（v3 目录，先 source env/setup.sh、env/v3env.sh，PATH 加 /workspace/work/sdl/speed/tools）：
  python3 tests/core_e2e.py                 # 全部组
  python3 tests/core_e2e.py modes limit     # 只跑指定组
  python3 tests/core_e2e.py --out r.json    # 结果写 JSON（默认 /tmp/sdlv3_e2e/result.json）
全量约 3–4 分钟：沙箱单条命令 120 s 上限时放后台（nohup … &）再轮询输出。退出码 0 = 全过。
"""
import hashlib
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = "/tmp/sdlv3_e2e"
OUT = os.path.join(DATA, "out")
INC = os.path.join(DATA, "inc")
TOKEN = "e2etoken789"
MB = 1024 * 1024
RESULTS = []
TIMES = {}


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  ← %s" % str(detail)[:500]), flush=True)
    return ok


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ------------------------------------------------------------------ 本地文件服务器
class Files:
    """name → bytes；每个名字的计数器（请求数、发出字节）与开关（norange、fail_first、每连接限速）。"""
    data = {}
    sha = {}
    stat = {}
    flags = {}
    lock = threading.Lock()

    @classmethod
    def add(cls, name, size, **flags):
        b = random.Random(hash(name) & 0xffff).randbytes(size)
        cls.data[name] = b
        cls.sha[name] = hashlib.sha256(b).hexdigest()
        cls.stat[name] = {"req": 0, "sent": 0, "range_req": 0, "asked": 0}
        cls.flags[name] = dict(flags)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _name(self):
        return urllib.parse.unquote(urllib.parse.urlparse(self.path).path.rsplit("/", 1)[-1])

    def do_HEAD(self):
        self._serve(head=True)

    def do_GET(self):
        self._serve(head=False)

    def _serve(self, head):
        name = self._name()
        b = Files.data.get(name)
        if b is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        fl = Files.flags[name]
        st = Files.stat[name]
        with Files.lock:
            st["req"] += 1
            n = st["req"]
            st.setdefault("hdrs", []).append({k.lower(): v for k, v in self.headers.items()})
            if self.headers.get("Range"):
                st["range_req"] += 1
        if fl.get("fail_first") and n <= fl["fail_first"]:
            self.send_response(400)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        total = len(b)
        a, z = 0, total - 1
        rng = self.headers.get("Range")
        partial = False
        if rng and not fl.get("norange") and rng.startswith("bytes="):
            s, _, e = rng[6:].split(",")[0].partition("-")
            try:
                a = int(s) if s else 0
                z = min(int(e), total - 1) if e else total - 1
                partial = True
            except ValueError:
                a, z = 0, total - 1
            if a >= total:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % total)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        with Files.lock:
            st["asked"] += z - a + 1          # 客户端要的字节数（不受服务器预先塞进 socket 缓冲区的影响）
            st.setdefault("starts", []).append(a)
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(z - a + 1))
        if not fl.get("norange"):
            self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (a, z, total))
        self.send_header("ETag", '"%s"' % Files.sha[name][:16])
        self.end_headers()
        if head:
            return
        rate = int(fl.get("rate") or 0)            # 每连接 B/s
        chunk = 64 * 1024
        pos = a
        t0 = time.monotonic()
        sent = 0
        try:
            while pos <= z:
                piece = b[pos:min(z + 1, pos + chunk)]
                self.wfile.write(piece)
                pos += len(piece)
                sent += len(piece)
                with Files.lock:
                    st["sent"] += len(piece)
                if rate:
                    ahead = sent / float(rate) - (time.monotonic() - t0)
                    if ahead > 0:
                        time.sleep(ahead)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            self.close_connection = True


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


# ------------------------------------------------------------------ RPC
PORT = [0]
FPORT = [0]


def raw_rpc(method, params, token=TOKEN, timeout=20):
    h = {"Content-Type": "application/json"}
    if token:
        h["X-SniffDL-Token"] = token
    req = urllib.request.Request("http://127.0.0.1:%d/jsonrpc" % PORT[0],
                                 data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                                                  "params": params}).encode(), headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"error": {"code": e.code, "message": str(e)}}


def rpc(method, *params):
    j = raw_rpc(method, list(params))
    if "error" in j:
        raise RuntimeError("%s → %s" % (method, j["error"]))
    return j["result"]


def wait(fn, timeout, step=0.25):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = fn()
            if v:
                return v
        except Exception:
            pass
        time.sleep(step)
    return None


def setv(**kv):
    r = rpc("sniffdl.settings.set", kv)
    if isinstance(r, dict) and r.get("ok") is False:
        raise RuntimeError("settings.set 失败：%s" % r)
    return r


def url_of(name):
    return "http://127.0.0.1:%d/f/%s" % (FPORT[0], urllib.parse.quote(name))


def add(name, **extra):
    info = {"url": url_of(name), "name": name, "dir": OUT}
    info.update(extra)
    r = rpc("sniffdl.task.add", info)
    gid = r.get("gid") if isinstance(r, dict) else r
    if not gid:
        raise RuntimeError("task.add 没返回 gid：%s" % r)
    return gid


def view(gid):
    for t in rpc("sniffdl.tasks"):
        if t["gid"] == gid:
            return t
    return None


def wait_status(gid, sts, timeout):
    return wait(lambda: (lambda v: v if v and v["status"] in sts else None)(view(gid)), timeout)


def wait_done_bytes(gid, nbytes, timeout):
    return wait(lambda: (lambda v: v if v and v["done"] >= nbytes else None)(view(gid)), timeout, 0.15)


def logs_since(mark):
    lines = rpc("sniffdl.logs", 2000) or []
    txt = [x if isinstance(x, str) else json.dumps(x, ensure_ascii=False) for x in lines]
    return txt[mark:] if mark < len(txt) else []


def log_mark():
    return len(rpc("sniffdl.logs", 2000) or [])


def sha_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def leftovers(d, name):
    return sorted(x for x in os.listdir(d) if x.startswith(name) and x != name) if os.path.isdir(d) else []


def finish_ok(tag, gid, name, timeout=60, d=OUT):
    v = wait_status(gid, ("complete", "error"), timeout)
    p = os.path.join(d, name)
    ok = bool(v and v["status"] == "complete" and os.path.isfile(p) and sha_file(p) == Files.sha[name])
    check("%s：完成且 sha256 一致" % tag, ok, v)
    return v


def remove(gid, delete_files=False, cleanup=None):
    args = [[gid], delete_files] + ([cleanup] if cleanup is not None else [])
    return rpc("sniffdl.task.remove", *args)


def base_settings():
    setv(speed_engine="auto", speed_limit_kbps=0, resume_policy="auto", cancel_cleanup="ask",
         incomplete_dir="", incomplete_suffix=True, retry_count=0)


# ------------------------------------------------------------------ 用例组
def g_modes():
    for mode in ("auto", "aria2", "sdl"):
        base_settings()
        setv(speed_engine=mode)
        name = "modes_%s.bin" % mode
        Files.add(name, 16 * MB)
        mk = log_mark()
        t0 = time.time()
        gid = add(name)
        v = finish_ok("模式 %s 16MB" % mode, gid, name)
        TIMES["modes_" + mode] = round(time.time() - t0, 2)
        lg = "\n".join(logs_since(mk))
        want = "aria2" if mode == "aria2" else "sdl"
        check("模式 %s：任务 engine=%s" % (mode, want), v and v.get("engine") == want, (v or {}).get("engine"))
        if mode == "aria2":
            check("模式 aria2：没启动动态分片、日志有 aria2c 完成", "动态分片引擎启动" not in lg and "aria2c 多连接完成" in lg,
                  lg[-600:])
        else:
            check("模式 %s：日志有「动态分片完成」且没走 aria2" % mode, "动态分片完成" in lg and "aria2c 多连接完成" not in lg,
                  lg[-600:])
        check("模式 %s：保存位置无临时文件残留" % mode, leftovers(OUT, name) == [], leftovers(OUT, name))
    # 不认 Range 的服务器：动态分片单流也能下
    base_settings()
    name = "modes_norange.bin"
    Files.add(name, 4 * MB, norange=True)
    gid = add(name)
    v = finish_ok("auto 服务器不认 Range 4MB", gid, name)
    check("auto 不认 Range：仍由动态分片单流完成", v and v.get("engine") == "sdl", (v or {}).get("engine"))


def g_fallback():
    base_settings()
    name = "fb_auto.bin"
    Files.add(name, 8 * MB, fail_first=1)
    mk = log_mark()
    gid = add(name)
    v = finish_ok("auto 首请求 400 → 回退", gid, name)
    lg = "\n".join(logs_since(mk))
    check("auto 回退：日志「回退 aria2c」+ engine=aria2", "回退 aria2c" in lg and v and v.get("engine") == "aria2",
          (lg[-600:], (v or {}).get("engine")))
    setv(speed_engine="sdl")
    name = "fb_sdl.bin"
    Files.add(name, 8 * MB, fail_first=1)
    mk = log_mark()
    gid = add(name)
    v = wait_status(gid, ("complete", "error"), 40)
    time.sleep(1.0)
    lg = "\n".join(logs_since(mk))
    check("sdl 模式同情况：任务失败（不回退 aria2）", v and v["status"] == "error", v)
    check("sdl 模式失败原因含「动态分片失败」", v and "动态分片失败" in json.dumps(v, ensure_ascii=False), v)
    check("sdl 模式不走单连接兜底（服务器只收到 1 个请求）", Files.stat[name]["req"] == 1, Files.stat[name])
    check("sdl 模式失败后不留 .sdl/.sdlpart", leftovers(OUT, name) == [], leftovers(OUT, name))
    check("sdl 失败日志没有 aria2", "aria2c 多连接" not in lg, lg[-400:])


def g_limit():
    base_settings()
    setv(speed_limit_kbps=2048)
    name = "lim_global.bin"
    Files.add(name, 8 * MB)
    t0 = time.time()
    gid = add(name)
    v = finish_ok("全局限速 2048KB/s 8MB（动态分片）", gid, name, 40)
    el = time.time() - t0
    TIMES["limit_global_8MB_2MBps"] = round(el, 2)
    check("全局限速生效：用时 %.2fs（理论 3.0–4.0s：令牌桶空闲时最多先放 1 秒额度，允许 2.8–6s）" % el, 2.8 <= el <= 6.0 and v and v.get("engine") == "sdl",
          (el, (v or {}).get("engine")))
    # 单任务限速 + aria2
    setv(speed_limit_kbps=0, speed_engine="aria2")
    name = "lim_task_a2.bin"
    Files.add(name, 6 * MB)
    t0 = time.time()
    gid = add(name, limit_kbps=1536)
    v = finish_ok("单任务限速 1536KB/s 6MB（aria2）", gid, name, 40)
    el = time.time() - t0
    TIMES["limit_task_aria2_6MB_1.5MBps"] = round(el, 2)
    check("单任务限速对 aria2 生效：用时 %.2fs（理论 4.0s，允许 3.0–7s）" % el, 3.0 <= el <= 7.0, el)
    # 单任务限速 + 动态分片，运行中改限速
    setv(speed_engine="auto")
    name = "lim_task_sdl.bin"
    Files.add(name, 12 * MB)
    t0 = time.time()
    gid = add(name, limit_kbps=1024)
    wait_done_bytes(gid, 2 * MB, 15)
    t_mid = time.time() - t0
    rpc("sniffdl.task.setLimit", gid, 0)
    v = finish_ok("单任务限速 1024KB/s 后中途解除 12MB（动态分片）", gid, name, 40)
    el = time.time() - t0
    TIMES["limit_task_sdl_unlimit"] = round(el, 2)
    check("单任务限速生效（前 2MB 用 %.2fs，1.5–4s）且解除后很快下完（总 %.2fs <8s，若不解除需 12s）" % (t_mid, el),
          1.5 <= t_mid <= 4.0 and el < 8.0, (t_mid, el))


def g_pause():
    base_settings()
    setv(speed_limit_kbps=2048)
    name = "pause_resume.bin"
    size = 16 * MB
    Files.add(name, size)
    mk = log_mark()
    gid = add(name)
    wait_done_bytes(gid, 5 * MB, 20)
    rpc("sniffdl.task.pause", [gid])
    v = wait_status(gid, ("paused",), 15)
    time.sleep(0.8)
    lo = leftovers(OUT, name)
    check("暂停：状态 paused 且 .sdl+.sdlpart 保留", v and name + ".sdl" in lo and name + ".sdlpart" in lo, (v, lo))
    try:
        holes = [tuple(r) for r in json.load(open(os.path.join(OUT, name + ".sdlpart"), encoding="utf-8"))["ranges"]]
    except Exception:
        holes = []
    need = sum(b - a for a, b in holes)
    n_before = len(Files.stat[name].get("starts") or [])
    setv(speed_limit_kbps=0)
    rpc("sniffdl.task.resume", [gid])
    finish_ok("暂停后继续 16MB", gid, name, 40)
    lg = "\n".join(logs_since(mk))
    check("继续时从 .sdl 续传（日志）", "从 .sdl 续传" in lg, lg[-500:])
    # 按侧车记的空洞判：暂停时还缺 need 字节（应 < 13MB），继续后每个请求的起点都落在空洞里（没重下已有部分）。
    # 以前按服务器「发出字节」判：多连接时服务器会先把数据塞满 socket 缓冲区，随时序在 2–16MB 间抖，误报。
    starts = (Files.stat[name].get("starts") or [])[n_before:]
    bad = [a for a in starts if not any(h0 <= a < h1 for h0, h1 in holes)]
    check("续传只补剩余部分：暂停时缺 %.1f MB（< 13 MB），继续后 %d 个请求起点全在空洞内" % (need / MB, len(starts)),
          holes and need < 13 * MB and starts and not bad, (holes[:6], bad[:6]))
    check("续传完成后无残留", leftovers(OUT, name) == [], leftovers(OUT, name))

    # resume_policy=restart
    setv(speed_limit_kbps=2048, resume_policy="restart")
    name = "pause_restart.bin"
    Files.add(name, 12 * MB)
    mk = log_mark()
    gid = add(name)
    wait_done_bytes(gid, 4 * MB, 20)
    rpc("sniffdl.task.pause", [gid])
    wait_status(gid, ("paused",), 15)
    time.sleep(0.8)
    sent_before = Files.stat[name]["asked"]
    setv(speed_limit_kbps=0)
    rpc("sniffdl.task.resume", [gid])
    finish_ok("restart 策略 暂停后继续", gid, name, 40)
    lg = "\n".join(logs_since(mk))
    extra = Files.stat[name]["asked"] - sent_before   # 按「继续后客户端请求的字节」算：服务器发出字节含暂停前塞进 socket 缓冲区的，随时序抖
    check("restart：日志「总是从头」且没有「从 .sdl 续传」", "总是从头" in lg and "从 .sdl 续传" not in lg, lg[-500:])
    check("restart：继续后服务器重新发出整个文件（%.1f MB ≥ 12 MB）" % (extra / MB), extra >= 12 * MB, extra)

    # resume_policy=always，服务器后来不认 Range → 报错保留
    setv(speed_limit_kbps=2048, resume_policy="always")
    name = "pause_always.bin"
    Files.add(name, 12 * MB)
    gid = add(name)
    wait_done_bytes(gid, 4 * MB, 20)
    rpc("sniffdl.task.pause", [gid])
    wait_status(gid, ("paused",), 15)
    time.sleep(0.8)
    Files.flags[name]["norange"] = True
    sdl_size = os.path.getsize(os.path.join(OUT, name + ".sdl")) if os.path.exists(os.path.join(OUT, name + ".sdl")) else -1
    rpc("sniffdl.task.resume", [gid])
    v = wait_status(gid, ("error", "complete"), 30)
    time.sleep(0.5)
    lo = leftovers(OUT, name)
    check("always：服务器不认 Range → 任务报错", v and v["status"] == "error" and "断点续传" in json.dumps(v, ensure_ascii=False), v)
    check("always：进度文件保留（.sdl+.sdlpart）", name + ".sdl" in lo and name + ".sdlpart" in lo and sdl_size > 0, (lo, sdl_size))
    # 同一个半成品改成 auto → 从头下完
    setv(speed_limit_kbps=0, resume_policy="auto")
    rpc("sniffdl.task.resume", [gid])
    finish_ok("auto：服务器不认 Range 时丢旧进度从头下完", gid, name, 40)
    check("auto 从头下完后无残留", leftovers(OUT, name) == [], leftovers(OUT, name))


def _start_slow(name, size=16 * MB):
    Files.add(name, size)
    gid = add(name)
    wait_done_bytes(gid, 3 * MB, 20)
    return gid


def g_cancel():
    cases = [("always", None, True), ("never", None, False), ("ask", True, True), ("ask", False, False)]
    for eng in ("auto", "aria2"):
        for pol, a2, cleaned in cases:
            base_settings()
            setv(speed_limit_kbps=2048, cancel_cleanup=pol, speed_engine=eng)
            name = "cancel_%s_%s_%s.bin" % (eng, pol, {None: "x", True: "y", False: "n"}[a2])
            gid = _start_slow(name)
            time.sleep(0.4)
            before = leftovers(OUT, name)
            remove(gid, False, a2)
            gone = wait(lambda: view(gid) is None, 10)
            time.sleep(4.5)                  # 等引擎线程收尾 + 迟到清扫（1s/3.5s）
            after = leftovers(OUT, name)
            tag = "删除任务 引擎=%s cancel_cleanup=%s%s" % (eng, pol, "" if a2 is None else " 第3参=%s" % a2)
            has_tmp = bool(before)
            if cleaned:
                check(tag + "：临时文件已清（删前 %s）" % before, gone and has_tmp and after == [], (before, after))
            else:
                check(tag + "：临时文件保留（%s）" % after, gone and has_tmp and len(after) >= 1, (before, after))
            for x in after:
                try:
                    os.remove(os.path.join(OUT, x))
                except OSError:
                    pass
    # ask + 不传第3参 + 勾删文件 → 全清
    base_settings()
    setv(speed_limit_kbps=2048, cancel_cleanup="ask")
    name = "cancel_ask_delfiles.bin"
    gid = _start_slow(name)
    remove(gid, True)
    wait(lambda: view(gid) is None, 10)
    time.sleep(4.5)
    check("删除任务 ask 未传第3参 + 勾删文件：临时文件已清", leftovers(OUT, name) == [], leftovers(OUT, name))


def g_incdir():
    for eng in ("auto", "aria2"):
        base_settings()
        shutil.rmtree(INC, ignore_errors=True)
        setv(incomplete_dir=INC, speed_limit_kbps=2048, speed_engine=eng)
        name = "incdir_%s.bin" % eng
        Files.add(name, 10 * MB)
        mk = log_mark()
        gid = add(name)
        wait_done_bytes(gid, 3 * MB, 20)
        mid_inc, mid_out = leftovers(INC, name), leftovers(OUT, name) + ([name] if os.path.exists(os.path.join(OUT, name)) else [])
        check("incomplete_dir 引擎=%s：下载中临时文件在未完成目录（%s），保存位置没有（%s）" % (eng, mid_inc, mid_out),
              len(mid_inc) >= 1 and mid_out == [], (mid_inc, mid_out))
        setv(speed_limit_kbps=0)
        v = finish_ok("incomplete_dir 引擎=%s 完成" % eng, gid, name, 40)
        time.sleep(0.5)
        inc_left = os.listdir(INC) if os.path.isdir(INC) else []
        lg = "\n".join(logs_since(mk))
        check("incomplete_dir 引擎=%s：任务路径在保存位置、未完成目录无残留（%s）" % (eng, inc_left),
              v and os.path.dirname(v.get("path") or "") == OUT and not [x for x in inc_left if x.startswith(name)],
              ((v or {}).get("path"), inc_left))
        check("incomplete_dir 引擎=%s：日志「已从未完成目录移到保存位置」" % eng, "已从未完成目录移到保存位置" in lg, lg[-400:])
    # 取消清理也会去未完成目录找
    base_settings()
    setv(incomplete_dir=INC, speed_limit_kbps=2048, cancel_cleanup="always")
    name = "incdir_cancel.bin"
    gid = _start_slow(name)
    before = leftovers(INC, name)
    remove(gid, False)
    wait(lambda: view(gid) is None, 10)
    time.sleep(4.5)
    check("incomplete_dir + cancel_cleanup=always：未完成目录里的临时文件也清掉（删前 %s）" % before,
          before and leftovers(INC, name) == [], (before, leftovers(INC, name)))
    base_settings()


def g_rpcopt():
    """aria2 RPC changeGlobalOption / changeOption 真生效。"""
    base_settings()
    r = rpc("aria2.changeGlobalOption", {"max-overall-download-limit": "2M", "max-concurrent-downloads": "2"})
    check("changeGlobalOption 返回 OK", r == "OK", r)
    go = rpc("aria2.getGlobalOption")
    check("getGlobalOption 读回限速 2048K", go.get("max-overall-download-limit") == "2048K", go)
    check("getGlobalOption 读回并发 2", go.get("max-concurrent-downloads") == "2", go)
    st = rpc("sniffdl.settings.get")
    flat = json.dumps(st)
    check("设置里 speed_limit_kbps=2048 同步", "2048" in flat, flat[:200])
    name = "rpcopt_g.bin"
    Files.add(name, 8 * MB)
    t0 = time.time()
    gid = add(name)
    finish_ok("RPC 设限速 2MB/s 后 8MB 下载", gid, name, 40)
    el = time.time() - t0
    check("RPC 全局限速真生效：用时 %.2fs（2.8–7s）" % el, 2.8 <= el <= 7.0, el)
    rpc("aria2.changeGlobalOption", {"max-overall-download-limit": "0"})
    check("RPC 取消限速后读回 0", rpc("aria2.getGlobalOption").get("max-overall-download-limit") == "0")
    name = "rpcopt_t.bin"
    Files.add(name, 12 * MB)
    t0 = time.time()
    gid = add(name, limit_kbps=512)
    wait_done_bytes(gid, 1 * MB, 15)
    r = rpc("aria2.changeOption", gid, {"max-download-limit": "0"})
    check("changeOption 返回 OK", r == "OK", r)
    finish_ok("changeOption 解除单任务限速后下完", gid, name, 40)
    el = time.time() - t0
    check("changeOption 真生效：总 %.2fs <10s（不解除需 24s）" % el, el < 10, el)
    j = raw_rpc("aria2.changeOption", ["0000000000000000", {"max-download-limit": "1M"}])
    check("changeOption 不存在的 GID 报错", "error" in j, j)
    j = raw_rpc("aria2.changeGlobalOption", [{"max-concurrent-downloads": "abc"}])
    check("非法并发值报错", "error" in j, j)
    rpc("aria2.changeGlobalOption", {"max-concurrent-downloads": "3"})


def g_linkswift():
    """LinkSwift 式推送：无 token 的 aria2.addUri，带 header/referer/user-agent，v3 内核必须接受并带头下载。"""
    base_settings()
    name = "ls_a.bin"
    Files.add(name, 6 * MB)
    ua = "quark-cloud-drive/2.5.20 Electron/18 Safari/537.36"
    ck = "__pus=OK; _t=1"
    ver = raw_rpc("aria2.getVersion", [], token=None)
    check("无 token getVersion（脚本测试连接）返回版本", "version" in (ver.get("result") or {}), ver)
    j = raw_rpc("aria2.addUri", [[url_of(name)], {
        "out": name, "dir": OUT, "referer": "https://pan.quark.cn/", "user-agent": ua,
        "header": ["Accept: */*", "User-Agent: " + ua, "Referer: https://pan.quark.cn/", "Cookie: " + ck]}],
        token=None)
    gid = j.get("result")
    check("无 token addUri 返回 gid", isinstance(gid, str) and len(gid) >= 8, j)
    if not gid:
        return
    finish_ok("LinkSwift 推送任务", gid, name, 40)
    hs = Files.stat[name].get("hdrs", [])
    check("服务端每个请求都带推送的 UA（%d 个请求）" % len(hs), hs and all(h.get("user-agent") == ua for h in hs), hs[:2])
    check("服务端每个请求都带 Cookie", hs and all(h.get("cookie") == ck for h in hs), hs[:2])
    check("服务端每个请求都带 Referer", hs and all(h.get("referer") == "https://pan.quark.cn/" for h in hs), hs[:2])
    j = raw_rpc("aria2.tellStatus", [gid], token=None)
    check("无 token tellStatus 可查", (j.get("result") or {}).get("status") in ("complete", "active"), j)
    j = raw_rpc("aria2.tellStopped", [0, 1000], token=None)
    check("无 token tellStopped 含该任务", any(x.get("gid") == gid for x in (j.get("result") or [])), j)


def _mk_torrent(tag, announce):
    sys.path.insert(0, os.path.join(HERE, "tests"))
    import core_unit as CU                 # 复用其 bencode / 造种子（模块无副作用，main 有 __name__ 守卫）
    data = random.Random(tag).randbytes(300 * 1024)
    plen = 64 * 1024
    pieces = b"".join(hashlib.sha1(data[i:i + plen]).digest() for i in range(0, len(data), plen))
    name = "bt_%s.bin" % tag
    tb = CU.benc({"announce": announce, "info": {"name": name, "piece length": plen, "pieces": pieces,
                                                   "length": len(data)}})
    p = os.path.join(DATA, "bt_%s.torrent" % tag)
    with open(p, "wb") as f:
        f.write(tb)
    return p, name


def g_btclean():
    """BT 任务删除时按 cancel_cleanup 清理：没下完的 payload 与 .aria2 控制文件（无做种者 → 永远下不完）。"""
    dead = "http://127.0.0.1:%d/announce" % free_port()
    for pol, a3, cleaned in (("always", None, True), ("never", None, False), ("ask", True, True), ("ask", False, False)):
        base_settings()
        setv(cancel_cleanup=pol)
        tag = "%s_%s" % (pol, {None: "x", True: "y", False: "n"}[a3])
        tp, name = _mk_torrent(tag, dead)
        r = rpc("sniffdl.task.add", {"url": tp, "dir": OUT})
        gid = r.get("gid") if isinstance(r, dict) else r
        v = wait(lambda: (lambda x: x if x and x.get("kind") == "bt" and x["status"] in ("active", "waiting") else None)(view(gid)), 20)
        have = wait(lambda: [x for x in os.listdir(OUT) if x.startswith(name)], 15)
        tag2 = "BT 删除 cancel_cleanup=%s%s" % (pol, "" if a3 is None else " 第3参=%s" % a3)
        check(tag2 + "：任务是 bt 且已落临时文件（%s）" % have, v and have, (v, have))
        remove(gid, False, a3)
        wait(lambda: view(gid) is None, 10)
        time.sleep(4.5)
        left = sorted(x for x in os.listdir(OUT) if x.startswith(name))
        if cleaned:
            check(tag2 + "：未完成 payload 与 .aria2 已清（剩 %s）" % left, left == [], left)
        else:
            check(tag2 + "：临时文件保留（%s）" % left, len(left) >= 1, left)
        for x in left:
            try:
                os.remove(os.path.join(OUT, x))
            except OSError:
                pass
    base_settings()


DFMNT = "/tmp/sdlv3_dfmnt"          # 4MB tmpfs（unshare -rm 用户命名空间内挂载，见 main 里的重入）
DF_SIZE_MB = 4


def _df_settle(gid, timeout=40):
    """等任务落到终态（磁盘满后内核把 error 改成 paused + 提示），再等 2s 看有无迟到的回退请求。"""
    v = wait(lambda: (lambda x: x if x and x["status"] in ("error", "paused", "complete")
                      and (x["status"] != "paused" or "磁盘" in (x.get("note") or "")) else None)(view(gid)), timeout)
    return v


def g_diskfull():
    """磁盘满：真内核 + 真 tmpfs（4MB）写满 → 任务停（暂停/失败并提示磁盘），不回退单连接重下、不无限重试。"""
    if not os.path.ismount(DFMNT):
        print("SKIP diskfull：%s 不是挂载点（需 unshare -rm，Linux 专用）" % DFMNT, flush=True)
        return
    for f in os.listdir(DFMNT):
        try:
            os.remove(os.path.join(DFMNT, f))
        except OSError:
            pass
    # 1) 开始前预检：服务器给了 Content-Length 且 > 剩余空间 → 不开始
    base_settings()
    setv(disk_check=True, speed_engine="auto")
    name = "df_precheck.bin"
    Files.add(name, 16 * MB)
    gid = add(name, dir=DFMNT, size_hint=16 * MB)
    v = wait(lambda: (lambda x: x if x and x["status"] in ("paused", "error", "complete") else None)(view(gid)), 20)
    check("磁盘满 预检：16MB 任务在 4MB 盘上不开始（暂停 + 提示磁盘空间不足）",
          v and v["status"] == "paused" and "磁盘空间不足" in (v.get("note") or ""), v)
    check("磁盘满 预检：服务器没收到下载请求", Files.stat[name]["asked"] == 0, Files.stat[name])
    remove(gid, True)
    wait(lambda: view(gid) is None, 10)
    # 2) 关预检，三种引擎真写满
    for eng in ("aria2", "sdl", "auto"):
        base_settings()
        setv(disk_check=False, speed_engine=eng, retry_count=2, retry_wait=1)
        for f in os.listdir(DFMNT):
            try:
                os.remove(os.path.join(DFMNT, f))
            except OSError:
                pass
        name = "df_%s.bin" % eng
        Files.add(name, 16 * MB)
        mk = log_mark()
        gid = add(name, dir=DFMNT)
        v = _df_settle(gid)
        time.sleep(3.0)
        req1 = Files.stat[name]["req"]
        time.sleep(3.0)
        req2 = Files.stat[name]["req"]
        v = view(gid)
        lg = "\n".join(logs_since(mk))
        blob = json.dumps(v, ensure_ascii=False)
        check("磁盘满 引擎=%s：任务停下（暂停/失败，不是完成）" % eng, v and v["status"] in ("paused", "error"), v)
        check("磁盘满 引擎=%s：提示含「磁盘」" % eng, v and "磁盘" in blob, blob[:400])
        check("磁盘满 引擎=%s：不回退单连接重下（日志无「回退单连接」）" % eng, "回退单连接…" not in lg, lg[-600:])
        check("磁盘满 引擎=%s：停下后服务器不再收到请求（不自动重试刷盘，%d→%d）" % (eng, req1, req2),
              req1 == req2, (req1, req2))
        noring = sum(1 for h in Files.stat[name].get("hdrs", []) if "range" not in h)
        check("磁盘满 引擎=%s：不带 Range 的整文件请求 ≤1（没被单连接从头重下，实际 %d）" % (eng, noring),
              noring <= 1, Files.stat[name])
        remove(gid, True, True)
        wait(lambda: view(gid) is None, 10)
        time.sleep(1.0)
    base_settings()
    setv(disk_check=True)


GROUPS = [("modes", g_modes), ("fallback", g_fallback), ("limit", g_limit), ("pause", g_pause),
          ("cancel", g_cancel), ("incdir", g_incdir), ("rpcopt", g_rpcopt), ("linkswift", g_linkswift), ("diskfull", g_diskfull), ("btclean", g_btclean)]


def main(argv):
    out = os.path.join(DATA, "result.json")
    if "--out" in argv:
        out = argv[argv.index("--out") + 1]
        argv = [a for a in argv if a not in ("--out", out)]
    want = [a for a in argv if not a.startswith("-")] or [g for g, _ in GROUPS]
    if "diskfull" in want and not os.environ.get("SDL_DF_NS") and shutil.which("unshare") and sys.platform.startswith("linux"):
        # 重入：在用户+挂载命名空间里挂一个 4MB tmpfs 再跑（无需 root）；失败则不重入，diskfull 组记 SKIP
        cmd = "mkdir -p %s && mount -t tmpfs -o size=%dm tmpfs %s && exec %s %s %s" % (
            DFMNT, DF_SIZE_MB, DFMNT, sys.executable, os.path.abspath(__file__),
            " ".join("'%s'" % a.replace("'", "") for a in sys.argv[1:]))
        r = subprocess.call(["unshare", "-rm", "sh", "-c", cmd], env=dict(os.environ, SDL_DF_NS="1"))
        if r != 126 and r != 127:
            return r
    shutil.rmtree(DATA, ignore_errors=True)
    os.makedirs(OUT, exist_ok=True)
    srv = QuietServer(("127.0.0.1", 0), Handler)
    FPORT[0] = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    PORT[0] = free_port()
    env = dict(os.environ, SNIFFDL_DRYRUN_OS="1", NO_PROXY="127.0.0.1,localhost,::1", no_proxy="127.0.0.1,localhost,::1")
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(k, None)              # 本地服务器测试：aria2c 不能被系统代理带走
    env.pop("DISPLAY", None)
    core = subprocess.Popen([sys.executable, os.path.join(HERE, "tests", "ui_smoke_core.py"), "--data-dir", DATA,
                             "--port", str(PORT[0]), "--runtime", os.path.join(DATA, "runtime.json"),
                             "--ui-token", TOKEN],
                            cwd=HERE, env=env, stdout=open(os.path.join(DATA, "core.out"), "w"),
                            stderr=subprocess.STDOUT)
    t_all = time.time()
    try:
        if not wait(lambda: raw_rpc("sniffdl.ping", [], token=None).get("result"), 30):
            check("内核启动", False, "见 %s/core.out" % DATA)
            return 1
        check("内核启动", True)
        setv(outdir=OUT, clip_watch=False, notify=False, conflict="重命名", bt_tracker_auto=False, bt_dht=False,
             bt_lpd=False, bt_pex=False, max_concurrent=3)
        for g, fn in GROUPS:
            if g not in want:
                continue
            print("== %s" % g, flush=True)
            t0 = time.time()
            try:
                fn()
            except Exception as e:
                import traceback
                traceback.print_exc()
                check("%s 组异常" % g, False, "%s: %s" % (type(e).__name__, e))
            TIMES["group_" + g] = round(time.time() - t0, 1)
    finally:
        try:
            raw_rpc("sniffdl.core.shutdown", [], timeout=5)
        except Exception:
            pass
        try:
            core.wait(8)
        except Exception:
            core.kill()
        srv.shutdown()
    npass = sum(1 for _, ok in RESULTS if ok)
    print("\n结果：%d/%d 通过（%.0f s）  用时：%s" % (npass, len(RESULTS), time.time() - t_all,
                                                json.dumps(TIMES, ensure_ascii=False)), flush=True)
    try:
        old = json.load(open(out, encoding="utf-8")) if os.path.exists(out) else {}
    except Exception:
        old = {}
    old.update({"groups": want, "pass": npass, "total": len(RESULTS), "times": TIMES,
                "fails": [n for n, ok in RESULTS if not ok], "at": time.strftime("%Y-%m-%d %H:%M:%S")})
    with open(out, "w", encoding="utf-8") as f:
        json.dump(old, f, ensure_ascii=False, indent=1)
    return 0 if npass == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
