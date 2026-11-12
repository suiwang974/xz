# -*- coding: utf-8 -*-
"""记忆系统（queue.json 持久化下载列表）端到端验收 —— 真 exe + 本地 mock 站，零外网。

跑法：
    1) python tools/mock_dl_site.py --port 8799 --mb 16 --kbps 6000      （另开一个窗口）
    2) python tools/qmem_e2e.py

验的是交接文件 §1 的两条验收判据 + 用户选定的「只恢复显示、点继续才下」：
  ① 下几个用户任务 → 叉掉 → 重开，队列还在；已完成的显示完成且【没有】重新下载；
  ② 进行中/排队的恢复成「⏸ 待继续」，重启后【零流量】（服务端字节计数不涨 = 没自动重放）；
  ③ 来源=browser 自动接管的一律不恢复（洪水红线）；
  ④ 点「继续」（aria2.unpause）之后才真的把剩下的下完，成品 sha256 与 mock 内容一致；
  ⑤ 「清除已完成」只清列表，本地成品文件一个都不动。
退出码 0=全绿。
"""
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXE = os.path.join(ROOT, "dist", "SniffDL", "SniffDL.exe")
EXE_DIR = os.path.dirname(EXE)
QFILE = os.path.join(EXE_DIR, "queue.json")
LOGF = os.path.join(EXE_DIR, "sniffdl.log")
MOCK = "http://127.0.0.1:8799"
RPC = "http://127.0.0.1:6800/jsonrpc"
OUT = os.path.join(os.environ.get("TEMP", r"C:\Temp"), "qmem_e2e", time.strftime("run_%H%M%S"))

sys.path.insert(0, HERE)
from mock_dl_site import body_for          # 内容确定性生成 → 可硬比对 sha256

fails = []


def chk(name, ok, hint=""):
    print("%-52s %s%s" % (name, "PASS" if ok else "FAIL", "" if ok else ("  " + str(hint))),
          flush=True)
    if not ok:
        fails.append(name)
    return ok


def rpc(method, *params):
    body = json.dumps({"jsonrpc": "2.0", "method": method, "params": list(params),
                       "id": "1"}).encode("utf-8")
    req = urllib.request.Request(RPC, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        j = json.loads(r.read().decode("utf-8", "ignore"))
    if isinstance(j, dict) and j.get("error"):
        raise RuntimeError((j["error"] or {}).get("message", "rpc error"))
    return (j or {}).get("result")


def http_get(url, timeout=20):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def server_bytes(name):
    try:
        d = json.loads(http_get(MOCK + "/__stats", timeout=5).decode("utf-8", "ignore"))
        return int((d.get("stats", {}).get("bytes", {}) or {}).get(name, 0)), d
    except Exception:
        return -1, {}


def log_size():
    try:
        return os.path.getsize(LOGF)
    except Exception:
        return 0


def log_new(from_size):
    try:
        with open(LOGF, "rb") as f:
            f.seek(from_size)
            return f.read().decode("utf-8", "ignore")
    except Exception:
        return ""


def wait_rpc(sec=90):
    t0 = time.time()
    while time.time() - t0 < sec:
        try:
            if rpc("sniffdl.status").get("ok"):
                return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


def start_exe():
    flags = 0x00000008 if os.name == "nt" else 0     # DETACHED_PROCESS
    subprocess.Popen([EXE, "--tray"], cwd=EXE_DIR, creationflags=flags,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


def our_aria2c_pids():
    """只挑命令行里带着我们 .snifftdl 数据目录的 aria2c（Motrix 之类的一律不碰）。"""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='aria2c.exe'\" | "
          "Where-Object { $_.CommandLine -match '\\.snifftdl' } | "
          "ForEach-Object { $_.ProcessId }")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=25)
        return [int(x) for x in (r.stdout or "").splitlines() if x.strip().isdigit()]
    except Exception:
        return []


def kill_exe():
    subprocess.run(["taskkill", "/IM", "SniffDL.exe", "/F"], capture_output=True)
    for pid in our_aria2c_pids():
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    time.sleep(2.0)


def qrows():
    try:
        with open(QFILE, encoding="utf-8") as f:
            d = json.load(f)
        return d.get("tasks") or []
    except Exception:
        return []


def by_gid(g):
    for r in qrows():
        if str(r.get("gid")) == str(g):
            return r
    return {}


def tell_all():
    act = rpc("aria2.tellActive") or []
    wait = rpc("aria2.tellWaiting", 0, 64) or []
    stop = rpc("aria2.tellStopped", 0, 200) or []
    return act, wait, stop


def main():
    print("=== SniffDL 记忆系统 E2E（真 exe + mock 站）===", flush=True)
    print("exe   = %s" % EXE, flush=True)
    print("outdir= %s" % OUT, flush=True)
    if not os.path.isfile(EXE):
        print("没找到打包好的 exe，先重打包再跑本测试"); return 2
    os.makedirs(OUT, exist_ok=True)
    try:
        server_bytes("slow.mp4")
    except Exception:
        pass
    if server_bytes("slow.mp4")[0] < 0:
        print("mock 站没起来（%s/__stats 读不到），先跑 tools/mock_dl_site.py" % MOCK)
        return 2

    kill_exe()
    start_exe()
    if not wait_rpc():
        print("exe 的 RPC 服务没起来"); return 2
    print("RPC 在线。", flush=True)

    # ① 一条会「完成」的用户任务
    u_fast = "%s/fast.mp4?kbps=6000" % MOCK
    gid_a = rpc("aria2.addUri", [u_fast], {"dir": OUT, "out": "fast_done.mp4",
                                           "header": ["Referer: http://127.0.0.1/"]})
    t0 = time.time()
    st_a = ""
    while time.time() - t0 < 120:
        try:
            st_a = rpc("aria2.getDownloadResult", str(gid_a)).get("status")
        except Exception:
            st_a = ""
        if st_a in ("complete", "error"):
            break
        time.sleep(1.0)
    fA = os.path.join(OUT, "fast_done.mp4")
    sha_fast = hashlib.sha256(body_for("fast.mp4", 2 * 1024 * 1024)).hexdigest()
    got_fast = hashlib.sha256(open(fA, "rb").read()).hexdigest() if os.path.isfile(fA) else ""
    chk("任务A 下载完成且内容与 mock 一致", st_a == "complete" and got_fast == sha_fast,
        "status=%s size=%s" % (st_a, os.path.getsize(fA) if os.path.isfile(fA) else "无文件"))
    mtime_a = os.path.getmtime(fA) if os.path.isfile(fA) else 0

    # ② 一条「用户主动、正在下」+ 一条「浏览器自动接管、正在下」
    u_slow = "%s/slow.mp4?kbps=6000" % MOCK
    u_auto = "%s/auto.mp4?kbps=6000" % MOCK
    gid_b = rpc("aria2.addUri", [u_slow], {"dir": OUT, "out": "slow_user.mp4"})
    gid_c = rpc("sniffdl.intercept", {"url": u_auto, "filename": "auto_browser.mp4",
                                      "outdir": OUT, "source": "", "confirm": False})
    time.sleep(8.0)
    b_slow1, _ = server_bytes("slow.mp4")
    b_auto1, _ = server_bytes("auto.mp4")
    chk("任务B/C 在杀掉前确实在下（服务端有字节）", b_slow1 > 0 and b_auto1 > 0,
        "slow=%d auto=%d" % (b_slow1, b_auto1))
    pre = qrows()
    chk("queue.json 已记录 3 条任务", len([r for r in pre if r.get("gid") in
        (str(gid_a), str(gid_b), str(gid_c))]) == 3, "%d 行" % len(pre))
    chk("C 的来源被记成 browser（自动接管）", by_gid(gid_c).get("src") == "browser",
        repr(by_gid(gid_c).get("src")))
    print("     [文件证据] 杀掉前 queue.json=%d 行 %s" % (
        len(pre), sorted(str(r.get("gid")) for r in pre)), flush=True)

    # ③ 叉掉程序（连同我们的 BT 守护进程），模拟"用户直接关了"
    kill_exe()
    if os.path.isfile(QFILE):
        b0, _ = server_bytes("slow.mp4")
        time.sleep(3.0)
        b1, _ = server_bytes("slow.mp4")
        chk("进程被杀后没有残留 aria2c 继续偷跑", b0 == b1, "%d → %d" % (b0, b1))

    # ④ 重开：只恢复显示，绝不自动重放
    log_at = log_size()
    start_exe()
    if not wait_rpc():
        print("重开后 RPC 没起来"); return 2
    time.sleep(4.0)
    post = qrows()
    print("     [文件证据] 重开后 queue.json=%d 行 %s" % (
        len(post), sorted(str(r.get("gid")) for r in post)), flush=True)
    chk("重开后 queue.json 文件本身还留着 3 条", len(post) == 3, "%d 行" % len(post))
    act, wait, stop = tell_all()
    idx = {str(t.get("gid")): t for t in (act + wait + stop)}
    chk("重开后队列 3 条都在（像 IDM 的历史列表）",
        all(str(g) in idx for g in (gid_a, gid_b, gid_c)),
        "拿到 %d 条：%r" % (len(idx), sorted(idx)))
    tb, tc = idx.get(str(gid_b), {}), idx.get(str(gid_c), {})
    chk("已完成的仍显示完成（没变成续传）",
        idx.get(str(gid_a), {}).get("status") == "complete",
        repr(idx.get(str(gid_a), {}).get("status")))
    chk("进行中的恢复成「待继续」（paused）", tb.get("status") == "paused", repr(tb.get("status")))
    chk("待继续行带「点继续」提示", "点继续" in (tb.get("note") or ""), repr(tb.get("note")))
    chk("浏览器自动接管的不恢复（洪水红线）",
        tc.get("status") == "paused" and "不恢复" in (tc.get("note") or ""),
        repr([tc.get("status"), tc.get("note")]))
    gs = rpc("aria2.getGlobalStat")
    chk("重启后没有任何任务在跑（numActive=0）",
        int(gs.get("numActive") or 0) == 0 and int(gs.get("numWaiting") or 0) == 0, repr(gs))

    b2, _ = server_bytes("slow.mp4")
    a2, _ = server_bytes("auto.mp4")
    time.sleep(20.0)
    b3, _ = server_bytes("slow.mp4")
    a3, _ = server_bytes("auto.mp4")
    chk("重启后 20 秒零流量：没有自动重放下载",
        b2 == b3 and a2 == a3, "slow %d→%d；auto %d→%d" % (b2, b3, a2, a3))
    chk("已完成的成品文件没被覆盖重下",
        os.path.getmtime(fA) == mtime_a and os.path.getsize(fA) == 2 * 1024 * 1024,
        "mtime %s→%s" % (mtime_a, os.path.getmtime(fA)))
    newlog = log_new(log_at)
    bad = [ln for ln in newlog.splitlines()
           if ("接收任务" in ln or "接管任务" in ln)
           and ("slow.mp4" in ln or "auto.mp4" in ln)]
    chk("日志里没有对旧任务的重新投递", not bad, bad[:1])
    chk("恢复动作本身有留痕（[记忆] …恢复进队列…）",
        "恢复进队列" in newlog and "[记忆]" in newlog, newlog[-160:])

    # ⑤ 点「继续」→ 才真的把剩下的下完
    bs0, _ = server_bytes("slow.mp4")
    rpc("aria2.unpause", str(gid_b))
    t0 = time.time()
    st_b = ""
    while time.time() - t0 < 300:
        try:
            st_b = rpc("aria2.getDownloadResult", str(gid_b)).get("status")
        except Exception:
            st_b = ""
        if st_b in ("complete", "error"):
            break
        time.sleep(2.0)
    fB = os.path.join(OUT, "slow_user.mp4")
    sha_slow = hashlib.sha256(body_for("slow.mp4", 16 * 1024 * 1024)).hexdigest()
    got_slow = hashlib.sha256(open(fB, "rb").read()).hexdigest() if os.path.isfile(fB) else ""
    bs1, sd = server_bytes("slow.mp4")
    chk("点「继续」后把剩下的下完了（16MB，sha256 一致）",
        st_b == "complete" and got_slow == sha_slow,
        "status=%s size=%s" % (st_b, os.path.getsize(fB) if os.path.isfile(fB) else "无文件"))
    chk("继续之后服务端才出现流量（证明是手动触发）", bs1 > bs0, "%d → %d" % (bs0, bs1))
    rng_starts = (sd.get("stats", {}).get("starts", {}) or {}).get("slow.mp4") or []
    print("     [证据] 续传请求起点：%s" % sorted(set(x for x in rng_starts if x))[:6], flush=True)
    chk("恢复后队列条目状态写回盘（已完成+无「待继续」备注）",
        by_gid(gid_b).get("status") == "complete" and "点继续" not in (by_gid(gid_b).get("note") or ""),
        repr([by_gid(gid_b).get("status"), by_gid(gid_b).get("note")]))

    # ⑥ 清除已完成：只清列表，成品一个都不动
    n = rpc("sniffdl.clearQueue", True)
    left = qrows()
    chk("「清除已完成」清掉完成/失败条目",
        int(n or 0) >= 2 and all(r.get("status") != "complete" for r in left),
        "清了 %s，剩 %d 行" % (n, len(left)))
    chk("清除已完成没有动本地成品文件",
        os.path.isfile(fA) and os.path.isfile(fB), "%s / %s" % (fA, fB))
    chk("待继续（浏览器接管）那条仍在，等主人自己决定",
        any(str(r.get("gid")) == str(gid_c) for r in left), repr(left))

    # 收尾：给用户一个干净的队列（测试条目不留在他的真实队列里），程序留在桌面继续用
    rpc("sniffdl.clearQueue", False)
    rpc("aria2.forceRemove", str(gid_c))
    time.sleep(1.0)
    chk("收尾清空后队列为空（真实使用不受测试污染）", len(qrows()) <= 1, "%d 行" % len(qrows()))

    # 自净：本次 mock 链接会进用户真实的 downloaded.json 完成账本，留着的话下次再跑
    # 会被判成"已下过、零流量跳过"→ 端到端测试假通过。跑完必须从账本里摘掉自己。
    led = os.path.join(EXE_DIR, "downloaded.json")
    try:
        with open(led, encoding="utf-8") as f:
            h = json.load(f)
        mine = [k for k in list(h) if "127.0.0.1:8799/" in k]
        for k in mine:
            h.pop(k, None)
        if mine:
            with open(led, "w", encoding="utf-8") as f:
                json.dump(h, f, ensure_ascii=False, indent=1)
        print("     [自净] 从完成账本摘掉测试链接 %d 条" % len(mine), flush=True)
    except Exception as e:
        print("     [自净] 账本清理失败（不影响结论）：%s" % str(e)[:80], flush=True)
    print("\n产物目录（自己留着看）：%s" % OUT, flush=True)
    print("E2E RESULT=%s" % ("PASS" if not fails else "FAIL(%d): %s" % (len(fails), " / ".join(fails))),
          flush=True)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
