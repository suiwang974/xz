# -*- coding: utf-8 -*-
"""A11：设置 bt_meta_timeout 生效：无人做种的磁力在超时后失败并提示「无人做种」（不是无限等）。
用法：source env/setup.sh && source env/v3env.sh && PATH=$PATH:.../speed/tools python3 tests/bt_meta_timeout_test.py"""
import os, sys, time, shutil, subprocess, threading
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "tests"))
import core_e2e as E
R = []
def chk(n, c, d=""):
    R.append(bool(c)); print(("PASS " if c else "FAIL ") + n + ("" if c else "  ← %s" % str(d)[:300]), flush=True)
def task(gid):
    for t in E.rpc("sniffdl.tasks"):
        if t["gid"] == gid:
            return t
def main():
    E.DATA = "/tmp/sdlv3_btmeta"; E.OUT = os.path.join(E.DATA, "out")
    shutil.rmtree(E.DATA, ignore_errors=True); os.makedirs(E.OUT)
    E.PORT[0] = E.free_port()
    env = dict(os.environ, SNIFFDL_DRYRUN_OS="1", NO_PROXY="127.0.0.1,localhost"); env.pop("DISPLAY", None)
    core = subprocess.Popen([sys.executable, os.path.join(HERE, "tests", "ui_smoke_core.py"), "--data-dir", E.DATA,
                             "--port", str(E.PORT[0]), "--runtime", os.path.join(E.DATA, "runtime.json"),
                             "--ui-token", E.TOKEN], cwd=HERE, env=env,
                            stdout=open(os.path.join(E.DATA, "core.out"), "w"), stderr=subprocess.STDOUT)
    try:
        chk("内核启动", E.wait(lambda: E.raw_rpc("sniffdl.ping", [], token=None).get("result"), 30))
        E.setv(outdir=E.OUT, clip_watch=False, notify=False, bt_tracker_auto=False, bt_dht=False, bt_lpd=False,
               bt_pex=False, auto_category=False, bt_meta_timeout=6, retry_count=0)
        t0 = time.time()
        r = E.rpc("sniffdl.task.add", {"url": "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=nobody",
                                      "dir": E.OUT})
        gid = r if isinstance(r, str) else (r or {}).get("gid")
        chk("磁力任务已建", gid, r)
        seen_msg = [False]
        def done():
            t = task(gid)
            if t and "正在找人要种子信息" in str(t.get("statusMessage") or t.get("message") or ""):
                seen_msg[0] = True
            return t and t.get("status") == "error"
        ok = E.wait(done, 40, 0.5)
        dt = time.time() - t0
        t = task(gid) or {}
        chk("超时（6 秒设置）后任务失败，而非无限等（用时 %.1fs ≤ 30）" % dt, ok and dt <= 30, t)
        chk("错误提示含「无人做种」", "无人做种" in str(t), t)
        E.setv(bt_meta_timeout=0)
        gid2 = E.rpc("sniffdl.task.add", {"url": "magnet:?xt=urn:btih:abcdef0123456789abcdef0123456789abcdef01&dn=wait", "dir": E.OUT})
        gid2 = gid2 if isinstance(gid2, str) else (gid2 or {}).get("gid")
        time.sleep(12)
        t2 = task(gid2) or {}
        chk("bt_meta_timeout=0：12 秒后仍在等（不报失败）", t2.get("status") != "error", t2)
    finally:
        core.terminate()
    print("%d/%d 通过" % (sum(R), len(R))); return 0 if all(R) else 1
sys.exit(main())
