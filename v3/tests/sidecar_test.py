# -*- coding: utf-8 -*-
"""外挂引擎接线（设置 sidecar_m3u8 / sidecar_bili / sidecar_paths，core/sidecar.py）。

单测：sidecar_paths 解析、找程序顺序、N_m3u8DL-RE 命令行（头、线程、保存名）、B 站链接识别。
真内核：假 N_m3u8DL-RE / BBDown 脚本（记录参数、写成品）——成功交给外挂并把请求头原样带上、外挂失败回落内置分段下载、
暂停时杀外挂进程（无孤儿）、sidecar_m3u8=关 不启动外挂、找不到程序时开关开着也照常下载、B 站链接交给 BBDown。
用法：source env/setup.sh && source env/v3env.sh && python3 tests/sidecar_test.py     退出码 0 = 全过
"""
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
import time

os.environ.setdefault("SDL_E2E_DATA", "/tmp/sdlv3_sidecar")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core_e2e as E   # noqa: E402
import hls_e2e as HE   # noqa: E402
from core_e2e import check, wait  # noqa: E402
from core import sidecar as SC  # noqa: E402


def unit(tmp):
    t = SC.parse_paths("# 注释\nN_m3u8DL-RE = /opt/x/N_m3u8DL-RE \n\nbbdown=\"/opt/y/BBDown\"\n坏行\n")
    check("U1 sidecar_paths 解析：忽略注释/空行/坏行，去引号，名称小写", t == {"n_m3u8dl-re": "/opt/x/N_m3u8DL-RE", "bbdown": "/opt/y/BBDown"}, t)
    fake = os.path.join(tmp, "N_m3u8DL-RE")
    open(fake, "w").write("#!/bin/sh\n")
    os.chmod(fake, 0o755)
    check("U2 find：sidecar_paths 里写名称=路径可找到", SC.find("n_m3u8dl", "N_m3u8DL-RE=%s" % fake) == fake)
    check("U2b find：路径无效且不在 PATH/vendor → None（开关开着也不报错）",
          SC.find("bbdown", "bbdown=/no/such/file") is None)
    old = os.environ["PATH"]
    os.environ["PATH"] = tmp + ":" + old
    try:
        check("U2c find：回落到 PATH", SC.find("n_m3u8dl", "") == fake)
    finally:
        os.environ["PATH"] = old
    cmd = SC.build_m3u8_cmd("/x/N", "http://h/a.m3u8", "/out", "mv", {"Cookie": "a=1", "Referer": "http://r/", "Host": "bad"}, 8)
    flat = " ".join(cmd)
    check("U3 命令行：保存目录/名/线程数，头逐条 -H，Host 不转发",
          "--save-dir /out" in flat and "--save-name mv" in flat and "--thread-count 8" in flat
          and "-H Cookie: a=1" in flat and "-H Referer: http://r/" in flat and "Host" not in flat, flat)
    check("U4 is_bili：bilibili.com / b23.tv 是，其他不是",
          SC.is_bili("https://www.bilibili.com/video/BV1xx") and SC.is_bili("https://b23.tv/abc")
          and not SC.is_bili("https://notbilibili.com.evil/x") and not SC.is_bili("https://example.com/"))


def main():
    tmp = "/tmp/sdlv3_sidecar_bin"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    unit(tmp)

    shutil.rmtree(E.DATA, ignore_errors=True)
    os.makedirs(E.OUT, exist_ok=True)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), HE.H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    E.FPORT[0] = base.rsplit(":", 1)[1]
    E.PORT[0] = E.free_port()
    args_log = os.path.join(tmp, "args.log")
    pidf = os.path.join(tmp, "pid")
    mode = os.path.join(tmp, "mode")      # ok / fail / hang
    open(mode, "w").write("ok")
    fake_m = os.path.join(tmp, "N_m3u8DL-RE")
    open(fake_m, "w").write(r'''#!/bin/sh
echo "$@" >> %(args)s
m=$(cat %(mode)s)
dir=""; name=""
while [ $# -gt 0 ]; do case "$1" in --save-dir) dir="$2"; shift;; --save-name) name="$2"; shift;; esac; shift; done
echo $$ > %(pid)s
if [ "$m" = hang ]; then echo "10%%"; exec sleep 60; fi
if [ "$m" = fail ]; then echo "boom"; exit 3; fi
echo "50%%"; printf SIDECAR > "$dir/$name.mp4"; echo "100%%"
''' % {"args": args_log, "mode": mode, "pid": pidf})
    fake_b = os.path.join(tmp, "BBDown")
    open(fake_b, "w").write(r'''#!/bin/sh
echo "BB $@" >> %(args)s
url="$1"; shift
dir=""
while [ $# -gt 0 ]; do case "$1" in --work-dir) dir="$2"; shift;; esac; shift; done
printf BILI > "$dir/bili_video.mp4"
''' % {"args": args_log})
    os.chmod(fake_m, 0o755)
    os.chmod(fake_b, 0o755)
    env = dict(os.environ, SNIFFDL_DRYRUN_OS="1", NO_PROXY="127.0.0.1,localhost,::1")
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "DISPLAY"):
        env.pop(k, None)
    core = subprocess.Popen([sys.executable, os.path.join(E.HERE, "tests", "ui_smoke_core.py"), "--data-dir", E.DATA,
                             "--port", str(E.PORT[0]), "--runtime", os.path.join(E.DATA, "runtime.json"),
                             "--ui-token", E.TOKEN],
                            cwd=E.HERE, env=env, stdout=open(os.path.join(E.DATA, "core.out"), "w"),
                            stderr=subprocess.STDOUT)
    HDR = {"Cookie": HE.COOKIE, "Referer": HE.REFERER}

    def addm(name, url, headers=HDR, **kw):
        info = {"url": url, "name": name, "dir": E.OUT, "headers": headers}
        info.update(kw)
        r = E.rpc("sniffdl.task.add", info)
        return r.get("gid") if isinstance(r, dict) else r

    try:
        if not wait(lambda: E.raw_rpc("sniffdl.ping", [], token=None).get("result"), 30):
            check("内核启动", False, "见 %s/core.out" % E.DATA)
            return 1
        segs = [HE.seg("sc", i, 20000) for i in range(5)]
        for i, b in enumerate(segs):
            HE.ROUTES["/v/sc/s%d.seg" % i] = b
        HE.ROUTES["/v/sc/index.m3u8"] = HE.m3u8(["s%d.seg" % i for i in range(5)])
        E.setv(outdir=E.OUT, clip_watch=False, notify=False, conflict="重命名", retry_count=0, max_concurrent=4,
               hls_native=True, sidecar_m3u8=True, sidecar_bili=False, sidecar_paths="")

        # S1 找不到程序：开关开着也走内置
        g = addm("nofind.m3u8", base + "/v/sc/index.m3u8?x=1")
        v = E.wait_status(g, ("complete", "error"), 30)
        p = os.path.join(E.OUT, "nofind.ts")
        check("S1 sidecar_m3u8 开但没装程序：照常用内置分段下载完成", bool(v and v["status"] == "complete" and os.path.isfile(p)
              and open(p, "rb").read() == b"".join(segs)), v)

        # S2 设了路径 → 交给外挂，头原样带上
        E.setv(sidecar_paths="N_m3u8DL-RE=%s\nBBDown=%s" % (fake_m, fake_b))
        open(args_log, "w").close()
        g = addm("viasc.m3u8", base + "/v/sc/index.m3u8?x=2")
        v = E.wait_status(g, ("complete", "error"), 30)
        p = os.path.join(E.OUT, "viasc.mp4")
        a = open(args_log).read()
        check("S2 装了 N_m3u8DL-RE：交给外挂，成品 viasc.mp4 由外挂产出", bool(v and v["status"] == "complete"
              and os.path.isfile(p) and open(p).read() == "SIDECAR" and v["name"] == "viasc.mp4"), (v, a))
        check("S2b 外挂命令行带 Cookie/Referer 头与保存名", "Cookie: %s" % HE.COOKIE in a and "Referer: %s" % HE.REFERER in a
              and "--save-name viasc" in a, a)

        # S3 外挂失败 → 回落内置
        open(mode, "w").write("fail")
        g = addm("fallb.m3u8", base + "/v/sc/index.m3u8?x=3")
        v = E.wait_status(g, ("complete", "error"), 30)
        p = os.path.join(E.OUT, "fallb.ts")
        check("S3 外挂退出码 3：回落内置分段下载，成品 sha 正确", bool(v and v["status"] == "complete" and os.path.isfile(p)
              and open(p, "rb").read() == b"".join(segs)), v)

        # S4 暂停杀外挂进程
        open(mode, "w").write("hang")
        if os.path.exists(pidf):
            os.remove(pidf)
        g = addm("hang.m3u8", base + "/v/sc/index.m3u8?x=4")
        ok = wait(lambda: os.path.exists(pidf) and open(pidf).read().strip(), 15)
        pid = int(ok) if ok else 0
        E.rpc("sniffdl.task.pause", [g])
        v = E.wait_status(g, ("paused",), 15)
        time.sleep(0.5)
        alive = False
        if pid:
            try:
                os.kill(pid, 0)
                alive = open("/proc/%d/stat" % pid).read().split()[2] != "Z"
            except (OSError, FileNotFoundError):
                alive = False
        check("S4 外挂运行中暂停：任务 paused，外挂进程已被杀（无孤儿）", bool(pid and v and not alive), (pid, alive, v))
        try:
            E.remove(g, True)
        except Exception:
            pass

        # S5 关开关 → 不启动外挂
        open(mode, "w").write("ok")
        E.setv(sidecar_m3u8=False)
        open(args_log, "w").close()
        g = addm("swoff.m3u8", base + "/v/sc/index.m3u8?x=5")
        v = E.wait_status(g, ("complete", "error"), 30)
        check("S5 sidecar_m3u8=关：不启动外挂（参数日志为空），内置完成", bool(v and v["status"] == "complete"
              and os.path.getsize(args_log) == 0 and v["name"] == "swoff.ts"), v)

        # S6 B 站链接 → BBDown
        E.setv(sidecar_bili=True)
        open(args_log, "w").close()
        g = addm("bili.mp4", "https://www.bilibili.com/video/BV1abcdefghi")
        v = E.wait_status(g, ("complete", "error"), 30)
        a = open(args_log).read()
        check("S6 sidecar_bili 开 + B 站链接：交给 BBDown，成品 bili_video.mp4", bool(v and v["status"] == "complete"
              and a.startswith("BB https://www.bilibili.com/video/BV1abcdefghi") and v["name"] == "bili_video.mp4"), (v, a))
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
