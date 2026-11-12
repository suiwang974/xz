# -*- coding: utf-8 -*-
"""B8（Windows 实测）：宿主被强杀后，内核经 --parent-pid 看门狗 / --watch-stdin / SIGTERM 退出时，runtime.json 必须被清掉；
即便后面的完整 shutdown 被卡住或被掐断，也要在收到退出信号的第一时间先删。退出码 0 = 全过。"""
import json, os, shutil, signal, subprocess, sys, time
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
R = []
def chk(n, c, d=""):
    R.append(bool(c)); print(("PASS " if c else "FAIL ") + n + ("" if c else "  ← %s" % str(d)[:300]), flush=True)

WRAP = r'''
import sys, time
sys.path.insert(0, %r)
sys.path.insert(0, %r)
import ui_smoke_core; ui_smoke_core._stub_tk()   # 无 tk 环境打桩（Windows 真机无需）
import sniffdl_core
HANG = %r
if HANG:
    import core.engine as E
    _orig = E.Engine.shutdown
    def slow(self):
        time.sleep(60)       # 模拟 shutdown 卡住（BT/aria2 停不下来）
        _orig(self)
    E.Engine.shutdown = slow
sys.exit(sniffdl_core.main(sys.argv[1:]))
'''

def wait(f, t):
    e = time.time() + t
    while time.time() < e:
        if f(): return True
        time.sleep(0.1)
    return False

def start(tag, extra, hang=False, stdin=False):
    d = "/tmp/sdlv3_exitrt_" + tag
    shutil.rmtree(d, ignore_errors=True); os.makedirs(d)
    rt = os.path.join(d, "runtime.json")
    code = WRAP % (HERE, os.path.join(HERE, 'tests'), hang)
    env = dict(os.environ, SNIFFDL_DRYRUN_OS="1")
    p = subprocess.Popen([sys.executable, "-c", code, "--runtime", rt, "--data-dir", d, "--host", "none",
                          "--ui-token", "tt"] + extra, stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, cwd=HERE)
    ok = wait(lambda: os.path.exists(rt), 40)
    return p, rt, ok

def main():
    # 1) 父进程死 → 看门狗 → 内核自退并删 runtime.json
    par = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    p, rt, ok = start("a", ["--parent-pid", str(par.pid)])
    chk("a 启动后 runtime.json 存在", ok)
    par.kill(); par.wait()
    chk("a 父进程被杀 → 内核 12s 内自退", wait(lambda: p.poll() is not None, 12), p.poll())
    chk("a 自退后 runtime.json 已删", not os.path.exists(rt))
    if p.poll() is None: p.kill()
    # 2) 父进程死 + shutdown 卡住：信号一到 runtime.json 就已删（旧代码会残留）
    par = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    p, rt, ok = start("b", ["--parent-pid", str(par.pid)], hang=True)
    chk("b 启动后 runtime.json 存在", ok)
    par.kill(); par.wait()
    chk("b shutdown 卡住时 runtime.json 仍在 6s 内被删", wait(lambda: not os.path.exists(rt), 6))
    p.kill(); p.wait()
    # 3) stdin 关闭
    p, rt, ok = start("c", ["--watch-stdin"], hang=True, stdin=True)
    chk("c 启动后 runtime.json 存在", ok)
    p.stdin.close()
    chk("c stdin 关闭 → runtime.json 4s 内被删", wait(lambda: not os.path.exists(rt), 4))
    p.kill(); p.wait()
    # 4) SIGTERM（POSIX）
    if os.name != "nt":
        p, rt, ok = start("d", [], hang=True)
        chk("d 启动后 runtime.json 存在", ok)
        p.send_signal(signal.SIGTERM)
        chk("d SIGTERM → runtime.json 4s 内被删", wait(lambda: not os.path.exists(rt), 4))
        p.kill(); p.wait()
    ok = all(R); print("%d/%d PASS" % (sum(R), len(R)), flush=True)
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
