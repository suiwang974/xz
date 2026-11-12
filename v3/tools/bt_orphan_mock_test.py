# -*- coding: utf-8 -*-
"""_bt_orphan_pids / _bt_reap_orphans 的 Windows 分支逻辑测试（Linux 上无 tasklist/PowerShell/taskkill，
用假 subprocess.run 冒充其输出（B9 起 PowerShell/CIM 返回 JSON 进程表）；只验判定逻辑，真 Windows 仍需实机验）。
    python tools/bt_orphan_mock_test.py
"""
import json
import os
import subprocess
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from netdisk_e2e import _stub_tk  # noqa: E402

_stub_tk()
import video_downloader as V  # noqa: E402

ME = os.getpid()
calls = []
SCEN = {}
DD = r"C:\Users\王伟\.snifftdl\bt"
ARIA = r"C:\Program Files\SniffDL\aria2c.exe"


def _row(pid, name, cl):
    return {"ProcessId": pid, "Name": name, "CommandLine": cl}


def fake_run(cmd, **kw):
    """冒充 Windows：powershell（CIM，B9 起返回 JSON 进程表、UTF-8 字节）/ taskkill / tasklist。"""
    calls.append(cmd)
    out = ""
    if cmd[0] == "powershell":
        SCEN["ps_cmd"] = cmd[-1]
        rows = []
        for pid in SCEN["aria_pids"]:
            rows.append(_row(pid, "aria2c.exe", '"%s" --enable-rpc=true --dir=%s --rpc-listen-port=1%d' % (ARIA, DD, pid % 10000)))
        # Motrix 式 aria2c：带 --bt-* 参数、目录不是 .snifftdl → 绝不能被认成孤儿
        rows.append(_row(8800, "aria2c.exe", r'C:\Motrix\aria2c.exe --enable-rpc --dir=C:\Users\王伟\Downloads --bt-max-peers=55 --bt-enable-lpd=true'))
        for pid in SCEN["sniffdl_pids"]:
            rows.append(_row(pid, "python.exe", r'python.exe C:\SniffDL\sniffdl_core.py --host electron'))
        out = json.dumps(rows, ensure_ascii=False)
        if kw.get("text") or kw.get("encoding"):
            return types.SimpleNamespace(stdout=out, stderr="", returncode=0)
        return types.SimpleNamespace(stdout=out.encode("utf-8"), stderr=b"", returncode=0)
    elif cmd[0] == "tasklist":
        out = "".join('"SniffDL.exe","%d","Console","1","100,000 K"\n' % p for p in SCEN["sniffdl_pids"])
    return types.SimpleNamespace(stdout=out, stderr="", returncode=0)


def mk():
    app = V.App.__new__(V.App)
    app.log = lambda m: calls.append(("log", m))
    app.runtime_path = os.path.join(HERE, "__no_such_runtime__.json")   # 不读开发目录里可能残留的 runtime.json
    app._bt = {"proc": types.SimpleNamespace(pid=501), "port": 0, "rpc_id": 0}
    app._h2 = {"proc": types.SimpleNamespace(pid=502)}
    return app


import core.runtime as RTM  # noqa: E402
res = []
real_name, real_run, real_alive = os.name, subprocess.run, RTM._alive
try:
    os.name = "nt"
    subprocess.run = fake_run
    RTM._alive = lambda pid: pid in SCEN.get("alive", ())      # 假 Windows 下 ctypes.windll 不存在，存活表由场景给
    # A：没有别的 SniffDL；aria2c 列表含 本进程拉起的 BT(501)/直链(502) + 两个孤儿 + Motrix
    SCEN.update(aria_pids=[501, 502, 7001, 7002], sniffdl_pids=[], alive=())
    a = mk()
    got = a._bt_orphan_pids(DD)
    res.append(("A 只认孤儿、不杀自己的 BT/直链引擎", got == [7001, 7002], got))
    res.append(("A 按 .snifftdl 目录匹配（Motrix 的 --bt-* aria2c 不算孤儿）",
                8800 not in got and "Win32_Process" in SCEN.get("ps_cmd", ""), (got, SCEN.get("ps_cmd", "")[-80:])))
    # B：还有另一个 SniffDL 活着 → 它的 aria2c 是合法守护进程，不算孤儿
    SCEN.update(aria_pids=[7001], sniffdl_pids=[9999], alive=(9999,))
    got = mk()._bt_orphan_pids(DD)
    res.append(("B 另有 SniffDL 在跑 → 不收", got == [], got))
    # C：reap 调 taskkill /F /PID
    calls.clear()
    SCEN.update(aria_pids=[7003], sniffdl_pids=[], alive=())
    gone = mk()._bt_reap_orphans(DD)
    tk = [c for c in calls if isinstance(c, list) and c[0] == "taskkill"]
    res.append(("C reap 用 taskkill 收掉孤儿", gone == [7003] and tk == [["taskkill", "/F", "/PID", "7003"]], tk))
finally:
    os.name = real_name
    subprocess.run = real_run
    RTM._alive = real_alive
# D：非 Windows：走 /proc，本机没有带这个假目录的 aria2c → 空
res.append(("D 非 Windows 返回空", mk()._bt_orphan_pids("/x/.snifftdl/bt") == [], ""))
for n, ok, d in res:
    print(("PASS " if ok else "FAIL ") + n, "" if ok else d)
print("%d/%d PASS" % (sum(1 for r in res if r[1]), len(res)))
sys.exit(0 if all(r[1] for r in res) else 1)
