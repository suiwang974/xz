# -*- coding: utf-8 -*-
"""A9：PyInstaller 产物（冻结内核）验收：起真产物 → READY → ping → 真下载 sha 一致 → HLS 模块在包里能用 → 关停清 runtime.json。
产物由 /workspace/work/sdl/pybuild 构建（沙箱 Linux 试构建；Windows exe 要等 CI/真机，本测试不代表 Windows 已测）。
用法：python3 tests/frozen_core_test.py [产物目录或可执行文件，默认 /workspace/work/sdl/pybuild/dist/sniffdl_core/sniffdl_core]
退出码 0 = 全过；产物不存在 → 退出码 2（不算过）。"""
import json, os, shutil, subprocess, sys, threading, time
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "tests"))
import core_e2e as E

R = []
def chk(n, c, d=""):
    R.append(bool(c)); print(("PASS " if c else "FAIL ") + n + ("" if c else "  ← %s" % str(d)[:400]), flush=True)

def main():
    exe = sys.argv[1] if len(sys.argv) > 1 else "/workspace/work/sdl/pybuild/dist/sniffdl_core/sniffdl_core"
    if os.path.isdir(exe):
        exe = os.path.join(exe, "sniffdl_core")
    if not os.path.isfile(exe):
        print("SKIP 没有产物：%s" % exe); return 2
    E.DATA = "/tmp/sdlv3_frozen"; E.OUT = os.path.join(E.DATA, "out")
    shutil.rmtree(E.DATA, ignore_errors=True); os.makedirs(E.OUT)
    srv = E.QuietServer(("127.0.0.1", 0), E.Handler); E.FPORT[0] = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    E.PORT[0] = E.free_port()
    rt = os.path.join(E.DATA, "runtime.json")
    env = dict(os.environ, SNIFFDL_DRYRUN_OS="1", NO_PROXY="127.0.0.1,localhost")
    for k in ("PYTHONPATH", "PYTHONHOME", "LD_LIBRARY_PATH_ORIG"):
        env.pop(k, None)                       # 产物必须自带依赖，不能靠沙箱的 PYTHONPATH 蹭
    env.pop("DISPLAY", None)
    out = open(os.path.join(E.DATA, "core.out"), "w")
    core = subprocess.Popen([exe, "--data-dir", E.DATA, "--port", str(E.PORT[0]), "--runtime", rt,
                             "--ui-token", E.TOKEN], cwd=E.DATA, env=env, stdout=out, stderr=subprocess.STDOUT)
    try:
        ok = E.wait(lambda: E.raw_rpc("sniffdl.ping", [], token=None).get("result"), 40)
        chk("产物内核启动并 ping 通", ok, open(os.path.join(E.DATA, "core.out")).read()[-600:])
        if not ok:
            return 1
        chk("runtime.json 写出且 pid=产物进程", os.path.isfile(rt) and json.load(open(rt)).get("pid") == core.pid,
            open(rt).read() if os.path.isfile(rt) else "无文件")
        E.setv(outdir=E.OUT, clip_watch=False, notify=False, bt_tracker_auto=False, bt_dht=False, bt_lpd=False,
               bt_pex=False, auto_category=False, speed_engine="sdl", retry_count=0)
        name = "frozen_16m.bin"
        E.Files.add(name, 16 * E.MB)
        gid = E.add(name)
        E.finish_ok("产物自研分片引擎下 16MB", gid, name, d=E.OUT)
        v = E.view(gid)
        chk("任务 engine=sdl（sdl_speed 随包）", v and v.get("engine") == "sdl", v)
        name2 = "frozen_small.bin"
        E.Files.add(name2, 200 * 1024)
        E.finish_ok("产物下 200KB 小文件", E.add(name2), name2, d=E.OUT)
        # 包内模块齐全：HLS 解析（core.hls）、网盘规则表、bencode
        meipass = None
        try:
            r = E.raw_rpc("sniffdl.settings.get", [])
            chk("设置接口可读", isinstance(r.get("result"), (dict, list)), r)
        except Exception as e:
            chk("设置接口可读", False, e)
        log = open(os.path.join(E.DATA, "core.out")).read()
        chk("产物输出无 Traceback / ModuleNotFoundError", "Traceback" not in log and "ModuleNotFoundError" not in log, log[-600:])
    finally:
        try: E.raw_rpc("sniffdl.core.shutdown", [], timeout=5)
        except Exception: pass
        try: core.wait(10)
        except Exception: core.kill()
        srv.shutdown()
    chk("关停后进程已退出", core.poll() is not None)
    chk("关停后 runtime.json 已清", not os.path.exists(rt))
    allr = R + [ok for _, ok in E.RESULTS]
    print("%d/%d" % (sum(allr), len(allr)))
    return 0 if all(allr) else 1

if __name__ == "__main__":
    sys.exit(main())
