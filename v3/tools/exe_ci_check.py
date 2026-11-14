# -*- coding: utf-8 -*-
"""打包产物验收（CI build_exe job 用；沙箱可用 `--exe video_downloader.py` 走源码代跑同一流程）。

  1) 产物目录体积 / 文件数
  2) `SniffDL.exe --selftest --selftest-out …`：退出码 0 且 SELFTEST PASS x/y 里 x==y、关键项齐全
  3) `SniffDL.exe --core …`（无界面内核）：冷启动到 READY（runtime.json 写出 + ping 通）计时 →
     本地 HTTP 真下载（中文文件名、中文目标目录）→ sha256 比对 → RPC 关停 → 进程退出码 0、runtime.json 已删
  4) 结果写 $GITHUB_STEP_SUMMARY（markdown）和 <out>/exe_ci_result.json

用法：
  python tools/exe_ci_check.py --exe dist_exe/SniffDL/SniffDL.exe [--dist dist_exe/SniffDL] [--out exe_ci_out]
  python tools/exe_ci_check.py --exe video_downloader.py --dist .      # 沙箱：源码代跑
退出码 0 = 全过；任何一项不过 = 1；exe 不存在 = 2。
"""
import argparse
import hashlib
import json
import os
import random
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 自检输出里必须出现的条目（名字前缀）：少一条说明 selftest 入口被改坏/没走到
REQUIRED_SELFTEST_ITEMS = ("模块可导入", "aria2c", "中文路径可写", "托盘图标", "浏览器扩展",
                           "RPC 启动并 ping 通", "RPC 停止")
MIN_SELFTEST_TOTAL = 10
DL_NAME = "打包验收_样本 文件.bin"
DL_SIZE = 6 * 1024 * 1024
TOKEN = "execi" + "%08x" % random.getrandbits(32)

RESULTS = []


def chk(name, ok, detail=""):
    RESULTS.append({"name": name, "ok": bool(ok), "detail": str(detail)[:500]})
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else "  ← " + str(detail)[:400]), flush=True)
    return bool(ok)


# ------------------------------------------------------------------ 产物
def dir_size(path):
    tot = n = 0
    for root, _d, files in os.walk(path):
        for f in files:
            try:
                tot += os.path.getsize(os.path.join(root, f))
                n += 1
            except OSError:
                pass
    return tot, n


def exe_cmd(exe):
    """真 exe 直接跑；.py 用当前解释器代跑（沙箱验流程用）。"""
    return [sys.executable, exe] if exe.lower().endswith(".py") else [exe]


def clean_env(exe):
    env = dict(os.environ, NO_PROXY="127.0.0.1,localhost,::1", no_proxy="127.0.0.1,localhost,::1",
               PYTHONIOENCODING="utf-8")
    if not exe.lower().endswith(".py"):
        for k in ("PYTHONPATH", "PYTHONHOME"):      # 产物必须自带依赖，不能蹭 CI 的 Python
            env.pop(k, None)
    env.pop("SNIFFDL_SELFTEST_OUT", None)
    return env


# ------------------------------------------------------------------ selftest 解析
SELFTEST_RE = re.compile(r"^SELFTEST (PASS|FAIL) (\d+)/(\d+)\s*$", re.M)


def parse_selftest(text):
    """返回 {verdict, x, y, items:[(tag,name)], json}；没找到 SELFTEST 行 → verdict=None。"""
    m = None
    for m in SELFTEST_RE.finditer(text or ""):
        pass
    res = {"verdict": None, "x": 0, "y": 0, "items": [], "json": None}
    if m:
        res.update(verdict=m.group(1), x=int(m.group(2)), y=int(m.group(3)))
    for line in (text or "").splitlines():
        if line[:5] in ("OK   ", "FAIL ", "WARN "):
            res["items"].append((line[:4].strip(), line[5:].split("  ")[0]))
        elif line.startswith("SELFTEST_JSON "):
            try:
                res["json"] = json.loads(line[len("SELFTEST_JSON "):])
            except ValueError:
                pass
    return res


def judge_selftest(rc, parsed):
    """→ (ok, why)。退出码、PASS、x==y、条目数下限、关键条目齐全且都 OK。"""
    if parsed["verdict"] is None:
        return False, "输出里没有 SELFTEST 行（rc=%s）" % rc
    if rc != 0:
        return False, "退出码 %s" % rc
    if parsed["verdict"] != "PASS" or parsed["x"] != parsed["y"]:
        return False, "SELFTEST %s %d/%d" % (parsed["verdict"], parsed["x"], parsed["y"])
    if parsed["y"] < MIN_SELFTEST_TOTAL:
        return False, "致命项只有 %d 条（<%d），自检被削弱了" % (parsed["y"], MIN_SELFTEST_TOTAL)
    names = [(t, n) for t, n in parsed["items"]]
    for want in REQUIRED_SELFTEST_ITEMS:
        hit = [t for t, n in names if n.startswith(want)]
        if not hit:
            return False, "缺自检项「%s」" % want
        if "FAIL" in hit:
            return False, "自检项「%s」FAIL" % want
    return True, "SELFTEST PASS %d/%d" % (parsed["x"], parsed["y"])


def run_selftest(exe, out_dir, timeout=180):
    of = os.path.join(out_dir, "selftest.txt")
    if os.path.exists(of):
        os.remove(of)
    t0 = time.time()
    try:
        p = subprocess.run(exe_cmd(exe) + ["--selftest", "--selftest-out", of], capture_output=True,
                           timeout=timeout, env=clean_env(exe), cwd=out_dir)
        rc, so = p.returncode, (p.stdout or b"").decode("utf-8", "replace") + (p.stderr or b"").decode("utf-8", "replace")
    except subprocess.TimeoutExpired as ex:
        rc, so = "timeout", ((ex.stdout or b"").decode("utf-8", "replace") if isinstance(ex.stdout, bytes) else "")
    dt = time.time() - t0
    text = open(of, encoding="utf-8").read() if os.path.isfile(of) else so   # 窗口版 exe 没 stdout，以文件为准
    with open(os.path.join(out_dir, "selftest_stdout.txt"), "w", encoding="utf-8") as f:
        f.write(so)
    parsed = parse_selftest(text)
    ok, why = judge_selftest(rc, parsed)
    chk("exe --selftest：%s" % why, ok, text[-1500:] if not ok else "")
    return {"rc": rc, "secs": round(dt, 2), "x": parsed["x"], "y": parsed["y"], "verdict": parsed["verdict"],
            "ok": ok, "why": why, "ready_s": (parsed["json"] or {}).get("ready_s"),
            "frozen": (parsed["json"] or {}).get("frozen"),
            "items": [("%s %s" % (t, n)) for t, n in parsed["items"]]}


# ------------------------------------------------------------------ 本地 HTTP 源
class _Src:
    data = b""
    sha = ""
    name = DL_NAME


class _H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self._serve(True)

    def do_GET(self):
        self._serve(False)

    def _serve(self, head):
        name = urllib.parse.unquote(urllib.parse.urlparse(self.path).path.rsplit("/", 1)[-1])
        if name != _Src.name:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        b, total = _Src.data, len(_Src.data)
        a, z, part = 0, total - 1, False
        rng = self.headers.get("Range") or ""
        if rng.startswith("bytes="):
            s, _, e = rng[6:].split(",")[0].partition("-")
            try:
                a = int(s) if s else 0
                z = min(int(e), total - 1) if e else total - 1
                part = True
            except ValueError:
                a, z = 0, total - 1
        self.send_response(206 if part else 200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(z - a + 1))
        self.send_header("Accept-Ranges", "bytes")
        if part:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (a, z, total))
        self.end_headers()
        if not head:
            try:
                self.wfile.write(b[a:z + 1])
            except OSError:
                self.close_connection = True


class _Srv(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, *a):
        pass


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def rpc(port, method, params=None, token=TOKEN, timeout=10):
    h = {"Content-Type": "application/json"}
    if token:
        h["X-SniffDL-Token"] = token
    req = urllib.request.Request("http://127.0.0.1:%d/jsonrpc" % port, headers=h,
                                 data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                                                  "params": params or []}).encode())
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        r = json.loads(op.open(req, timeout=timeout).read())
    except urllib.error.HTTPError as e:
        r = json.loads(e.read() or b"{}")
    if "error" in r:
        raise RuntimeError("%s → %s" % (method, r["error"]))
    return r.get("result")


def wait_for(fn, timeout, step=0.2):
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


def sha_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def run_core_download(exe, out_dir, ready_timeout=90, dl_timeout=120):
    res = {"ready_s": None, "dl_ok": False, "exit_rc": None, "engine": None}
    _Src.data = random.Random(20261114).randbytes(DL_SIZE)
    _Src.sha = hashlib.sha256(_Src.data).hexdigest()
    srv = _Srv(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = tempfile.mkdtemp(prefix="sdl_execi_")
    data = os.path.join(base, "内核 数据")
    dest = os.path.join(base, "下载 目录")
    os.makedirs(dest)
    rt = os.path.join(data, "runtime.json")
    port = free_port()
    logf = open(os.path.join(out_dir, "core_stdout.txt"), "wb")
    t0 = time.time()
    proc = subprocess.Popen(exe_cmd(exe) + ["--core", "--data-dir", data, "--port", str(port), "--runtime", rt,
                                            "--ui-token", TOKEN, "--host", "none"],
                            stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            env=clean_env(exe), cwd=out_dir)
    try:
        def _ready():
            if proc.poll() is not None:
                raise SystemExit
            if not os.path.isfile(rt):
                return None
            info = json.load(open(rt, encoding="utf-8"))
            return info if (rpc(int(info["port"]), "sniffdl.ping", token=None) or {}).get("service") == "SniffDL" else None
        try:
            info = wait_for(_ready, ready_timeout, 0.1)
        except SystemExit:
            info = None
        if info:
            res["ready_s"] = round(time.time() - t0, 2)
            port = int(info["port"])
        if not chk("exe --core 冷启动到 READY（runtime.json + ping）", info,
                   "rc=%s 超时 %ss" % (proc.poll(), ready_timeout)):
            return res
        chk("runtime.json pid = 产物进程", int(info.get("pid") or 0) == proc.pid or exe.lower().endswith(".py"),
            info)
        rpc(port, "sniffdl.settings.set", [{"outdir": dest, "clip_watch": False, "notify": False,
                                            "bt_tracker_auto": False, "bt_dht": False, "bt_lpd": False,
                                            "bt_pex": False, "auto_category": False, "retry_count": 1}])
        url = "http://127.0.0.1:%d/f/%s" % (srv.server_address[1], urllib.parse.quote(DL_NAME))
        r = rpc(port, "sniffdl.task.add", [{"url": url, "name": DL_NAME, "dir": dest}])
        gid = r.get("gid") if isinstance(r, dict) else r
        if not chk("task.add 返回 gid", gid, r):
            return res

        def _done():
            for t in rpc(port, "sniffdl.tasks") or []:
                if t.get("gid") == gid and t.get("status") in ("complete", "error", "removed"):
                    return t
            return None
        t = wait_for(_done, dl_timeout, 0.3)
        res["engine"] = (t or {}).get("engine")
        fp = os.path.join(dest, DL_NAME)
        got = sha_file(fp) if os.path.isfile(fp) else None
        res["dl_ok"] = chk("本地 HTTP 真下载 6MB（中文名/中文目录）完成且 sha256 一致",
                           t and t.get("status") == "complete" and got == _Src.sha,
                           "status=%s sha=%s want=%s" % ((t or {}).get("status"), got, _Src.sha))
        rpc(port, "sniffdl.core.shutdown")
        try:
            rc = proc.wait(30)
        except subprocess.TimeoutExpired:
            rc = "timeout"
        res["exit_rc"] = rc
        chk("RPC 关停后进程正常退出（rc=0）", rc == 0, "rc=%s" % rc)
        chk("退出后 runtime.json 已删", not os.path.exists(rt), rt)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)
        srv.shutdown()
        logf.close()
    return res


# ------------------------------------------------------------------ summary
def fmt_mb(n):
    return "%.1f MB" % (n / 1048576.0)


def render_summary(info):
    ok = all(r["ok"] for r in RESULTS)
    st, core = info.get("selftest") or {}, info.get("core") or {}
    L = ["## SniffDL.exe 打包验收 %s" % ("✅" if ok else "❌"), "",
         "| 项 | 结果 |", "|---|---|",
         "| 产物 | `%s` |" % info.get("exe"),
         "| 目录大小 | %s（%s 个文件） |" % (fmt_mb(info.get("dist_bytes") or 0), info.get("dist_files")),
         "| --selftest | %s（rc=%s，%ss） |" % (st.get("why"), st.get("rc"), st.get("secs")),
         "| 冷启动到 READY（--core） | %s |" % (("%ss" % core["ready_s"]) if core.get("ready_s") is not None else "未就绪"),
         "| 真实下载 + sha256 | %s（引擎 %s） |" % ("通过" if core.get("dl_ok") else "失败", core.get("engine")),
         "| 内核退出码 | %s |" % core.get("exit_rc"),
         "", "<details><summary>全部断言 %d/%d</summary>" % (sum(r["ok"] for r in RESULTS), len(RESULTS)), ""]
    for r in RESULTS:
        L.append("- %s %s%s" % ("✅" if r["ok"] else "❌", r["name"], "" if r["ok"] else " — `%s`" % r["detail"][:200].replace("`", "'")))
    if st.get("items"):
        L += ["", "selftest 条目：", ""] + ["- %s" % x for x in st["items"]]
    L += ["", "</details>", ""]
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", required=True)
    ap.add_argument("--dist", default="")
    ap.add_argument("--out", default="exe_ci_out")
    ap.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY", ""))
    ap.add_argument("--skip-core", action="store_true")
    a = ap.parse_args(argv)
    del RESULTS[:]
    exe = os.path.abspath(a.exe)
    if not os.path.isfile(exe):
        print("没有产物：%s" % exe)
        if a.summary:
            with open(a.summary, "a", encoding="utf-8") as f:
                f.write("## SniffDL.exe 打包验收 ❌\n\n产物不存在：`%s`\n" % exe)
        return 2
    out = os.path.abspath(a.out)
    os.makedirs(out, exist_ok=True)
    dist = os.path.abspath(a.dist or os.path.dirname(exe))
    nbytes, nfiles = dir_size(dist)
    info = {"exe": exe, "dist": dist, "dist_bytes": nbytes, "dist_files": nfiles}
    print("产物目录 %s：%s，%d 个文件" % (dist, fmt_mb(nbytes), nfiles), flush=True)
    info["selftest"] = run_selftest(exe, out)
    if not a.skip_core:
        info["core"] = run_core_download(exe, out)
    info["results"] = RESULTS
    info["ok"] = all(r["ok"] for r in RESULTS) and bool(RESULTS)
    with open(os.path.join(out, "exe_ci_result.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=1)
    md = render_summary(info)
    with open(os.path.join(out, "summary.md"), "w", encoding="utf-8") as f:
        f.write(md)
    if a.summary:
        with open(a.summary, "a", encoding="utf-8") as f:
            f.write(md + "\n")
    print("exe_ci_check: %d/%d 通过" % (sum(r["ok"] for r in RESULTS), len(RESULTS)), flush=True)
    return 0 if info["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
