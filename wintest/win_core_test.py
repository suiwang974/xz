# -*- coding: utf-8 -*-
"""Windows 真机内核专项：中文用户名路径 data-dir、MOTW（Zone.Identifier ADS）、防睡眠 API、
--parent-pid 看门狗、真 tasklist/PowerShell/taskkill 的 BT 孤儿 aria2c 收割。"""
import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import V3, Results, free_port, make_file_server, procs, alive, rpc, wait  # noqa: E402

R = Results("core")
ARIA2C = os.environ.get("SNIFFDL_ARIA2C") or shutil.which("aria2c")
BASE = "C:\\测试用户"
TOKEN = "wintesttoken0123456789abcdef0000"


def start_core(dd, parent=None):
    port = free_port()
    args = [sys.executable, os.path.join(V3, "sniffdl_core.py"), "--data-dir", dd, "--port", str(port),
            "--runtime", os.path.join(dd, "runtime.json"), "--ui-token", TOKEN]
    if parent:
        args += ["--parent-pid", str(parent)]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    if ARIA2C:
        env["PATH"] = os.path.dirname(ARIA2C) + os.pathsep + env["PATH"]
    p = subprocess.Popen(args, cwd=V3, env=env, stdout=open(os.path.join(dd, "core.out"), "w", encoding="utf-8"),
                         stderr=subprocess.STDOUT)
    ok = wait(lambda: rpc(port, "sniffdl.ping").get("result"), 60, 0.5)
    return p, port, ok


def t_motw_keepawake():
    dd = os.path.join(BASE, "王伟的数据", "core")
    outd = os.path.join(BASE, "王伟的数据", "下载 输出")
    shutil.rmtree(os.path.join(BASE, "王伟的数据"), ignore_errors=True)
    os.makedirs(dd)
    os.makedirs(outd)
    srv, fport, sha = make_file_server(6 * 1024 * 1024, delay=0.03, name="motw.bin")
    p, port, ok = start_core(dd)
    try:
        R.ok("C1 内核在中文 data-dir 启动（C:\\测试用户\\王伟的数据\\core）", ok, open(os.path.join(dd, "core.out"), encoding="utf-8", errors="replace").read()[-800:] if not ok else port)
        if not ok:
            return
        R.ok("C2 runtime.json 写在中文路径", os.path.isfile(os.path.join(dd, "runtime.json")))
        r = rpc(port, "sniffdl.settings.set", {"outdir": outd, "clip_watch": False, "notify": False, "motw": True, "keep_awake": True}, token=TOKEN)
        R.ok("C3 settings.set（中文 outdir）", "result" in r, r)
        url = "http://127.0.0.1:%d/dl/motw.bin" % fport
        r = rpc(port, "sniffdl.task.add", {"url": url, "start_now": True}, token=TOKEN)
        R.ok("C4 task.add", (r.get("result") or {}).get("ok"), r)
        gid = (r.get("result") or {}).get("gid")
        st = wait(lambda: [t for t in rpc(port, "sniffdl.tasks", token=TOKEN)["result"] if t["gid"] == gid and t["status"] in ("complete", "error")], 90, 0.5)
        R.ok("C5 下载完成", st and st[0]["status"] == "complete", st)
        files = [os.path.join(d, f) for d, _, fs in os.walk(outd) for f in fs]
        R.ok("C6 文件在中文 outdir", files, files)
        if files:
            f0 = max(files, key=os.path.getsize)
            R.ok("C7 sha256 一致", hashlib.sha256(open(f0, "rb").read()).hexdigest() == sha)
            time.sleep(1.5)
            try:
                ads = open(f0 + ":Zone.Identifier", encoding="utf-8").read()
            except Exception as e:
                ads = "ERR " + repr(e)
            R.ok("C8 MOTW：Zone.Identifier 存在且 ZoneId=3", "ZoneId=3" in ads, ads)
            R.ok("C9 MOTW：HostUrl 记录下载地址", ("HostUrl=" + url) in ads, ads)
            ps = subprocess.run(["powershell", "-NoProfile", "-Command",
                                 "Get-Content -LiteralPath '%s' -Stream Zone.Identifier" % f0.replace("'", "''")],
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
            R.ok("C10 PowerShell Get-Content -Stream 也读到 ADS", "ZoneId=3" in ps.stdout, (ps.stdout + ps.stderr)[-300:])
        acts = rpc(port, "sniffdl.debug.osActions", 200, token=TOKEN).get("result") or []
        ka = [a["target"] for a in acts if a.get("kind") == "keepawake"]
        R.ok("C11 防睡眠：下载时 on、结束后 off", "on" in ka and ka[-1] == "off", ka)
        R.ok("C12 osActions 记录 motw", any(a.get("kind") == "motw" for a in acts))
        k = ctypes.windll.kernel32
        k.SetThreadExecutionState.restype = ctypes.c_uint
        prev = k.SetThreadExecutionState(ctypes.c_uint(0x80000000 | 0x00000001))
        k.SetThreadExecutionState(ctypes.c_uint(0x80000000))
        R.ok("C13 SetThreadExecutionState 直调返回非 0（API 可用）", prev != 0, hex(prev))
        pc = subprocess.run(["powercfg", "/requests"], capture_output=True, text=True, errors="replace")
        print("powercfg /requests:\n", pc.stdout[:800])
    finally:
        try:
            rpc(port, "sniffdl.core.shutdown", token=TOKEN)
        except Exception:
            pass
        try:
            p.wait(15)
        except Exception:
            p.kill()
        R.ok("C14 core.shutdown 后进程退出、runtime.json 删除", p.poll() is not None and not os.path.exists(os.path.join(dd, "runtime.json")))
        srv.shutdown()


def t_parent_pid():
    dd = os.path.join(BASE, "父进程看门狗")
    shutil.rmtree(dd, ignore_errors=True)
    os.makedirs(dd)
    dummy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    p, port, ok = start_core(dd, parent=dummy.pid)
    R.ok("P1 内核带 --parent-pid 启动", ok)
    subprocess.run(["taskkill", "/F", "/PID", str(dummy.pid)], capture_output=True)
    t0 = time.time()
    gone = wait(lambda: p.poll() is not None, 15)
    R.ok("P2 父进程被 taskkill /F 后内核自退", gone, "用时 %.1fs" % (time.time() - t0))
    if p.poll() is None:
        p.kill()


def t_bt_orphan_real():
    if not ARIA2C:
        R.ok("BT0 有 aria2c.exe", False)
        return
    root = os.path.join(BASE, "王伟", ".snifftdl", "bt")
    other = os.path.join(BASE, "Motrix下载")
    for d in (root, other):
        os.makedirs(d, exist_ok=True)
    orphan = subprocess.Popen([ARIA2C, "--enable-rpc", "--rpc-listen-port=%d" % free_port(), "--dir=" + root, "--quiet"])
    decoy = subprocess.Popen([ARIA2C, "--enable-rpc", "--rpc-listen-port=%d" % free_port(), "--dir=" + other,
                              "--bt-max-peers=55", "--bt-enable-lpd=true", "--quiet"])
    time.sleep(2)
    sys.path.insert(0, V3)
    os.chdir(V3)
    import video_downloader as V
    # 前面步骤（ui_smoke 等）可能留着别的 SniffDL 内核在跑，它们会被当成「主人」→ 孤儿不成立。先清场并打印
    for pid, n, cl in V.list_processes():
        if pid != os.getpid() and n.lower().startswith(("python", "electron")) and V.is_sniffdl_owner_cmdline(cl):
            print("清掉残留 SniffDL 进程 pid=%d %s" % (pid, cl[-160:]), flush=True)
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
    time.sleep(1)
    app = V.App.__new__(V.App)
    logs = []
    app.log = lambda m: logs.append(m)
    app._bt = {"proc": None, "port": 0, "rpc_id": 0}
    app._h2 = {"proc": None}
    try:
        got = app._bt_orphan_pids(root)
        diag = {"got": got, "orphan": orphan.pid, "decoy": decoy.pid}
        if got != [orphan.pid]:            # 只在失败时附诊断：进程表 / 每个 aria2c 的判定原因 / PowerShell 返回
            try:
                from core.runtime import _alive as _al
                pl = V.list_processes()
                diag["ps"] = dict(getattr(V, "_LIST_PROC_DIAG", {}))
                diag["aria2c"] = [(pid, cl[-120:], V.aria2_orphan_verdict(cl, _al, 0, [], [], pid))
                                  for pid, n, cl in pl if n.lower().startswith("aria2c")]
                diag["owners"] = [(pid, cl[-120:]) for pid, n, cl in pl if V.is_sniffdl_owner_cmdline(cl)]
            except Exception as e:
                diag["diag_err"] = repr(e)
        R.ok("BT1 真 PowerShell/CIM 只认出带 .snifftdl 的孤儿（不误伤 Motrix --bt-*）", got == [orphan.pid], diag)
        gone = app._bt_reap_orphans(root)
        time.sleep(1)
        R.ok("BT2 真 taskkill 收掉孤儿", gone == [orphan.pid] and orphan.poll() is not None, {"gone": gone, "logs": logs[-2:]})
        R.ok("BT3 Motrix 式 aria2c 仍存活", decoy.poll() is None)
        names = app._bt_pids_named("python.exe") if hasattr(app, "_bt_pids_named") else None
        R.ok("BT4 真 tasklist 解析（python.exe 含自身 pid）", names and os.getpid() in names, names)
    finally:
        for x in (orphan, decoy):
            if x.poll() is None:
                x.kill()


def main():
    os.makedirs(BASE, exist_ok=True)
    for fn in (t_motw_keepawake, t_parent_pid, t_bt_orphan_real):
        try:
            fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
            R.ok(fn.__name__ + " 异常", False, repr(e))
    return R.save()


if __name__ == "__main__":
    sys.exit(main())
