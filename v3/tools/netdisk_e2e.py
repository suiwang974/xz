# -*- coding: utf-8 -*-
"""网盘直链 / 外部脚本推送 端到端验收（零外网，Linux/Windows 都能跑）。

复现主人报的 bug：「LinkSwift / 网盘直链下载助手 推到 SniffDL 显示『分发成功』，但 SniffDL 根本没下」。
做法：真 RpcServer + 真 App 下载后端（无界面：用桩替掉 tkinter）+ 真 aria2c + 本地 mock 网盘 CDN
（tools/mock_netdisk_site.py：缺 Cookie→412、UA 不对/过期→403），然后发和 LinkSwift 一字不差的 JSON-RPC：
  ① aria2.getVersion  params=["token:"]               （脚本「测试连接」）
  ② aria2.addUri      params=[[link], {referer, user-agent, out, header:[Accept, User-Agent, Origin, Referer, Cookie]}]
再用 aria2.tellStatus / tellActive / tellStopped 盯任务直到完成或失败。

    python tools/netdisk_e2e.py [--aria2c 路径] [--only A,B,...] [--bench]

场景：
  A 正常推送（Cookie+UA 齐）→ 必须真下完 + sha256 对 + 服务端每个请求都带推送方给的 UA/Cookie（一字不改）+ 真多连接
  B 推送缺 Cookie          → 必须进「失败」且原因可见（缺登录 Cookie / 412），不能静默
  C 链接已过期              → 必须进「失败」且原因写明「过期」
  D 同一文件有个「待继续(paused)」旧任务（记忆系统恢复出来的）→ 再推新链接必须真下，不能返回旧 gid 就完事
  E 共享 aria2 守护进程队列已满（BT/别的任务占满 max-concurrent-downloads）→ 推送必须照样开下，不能无限排队
  F 带 token 的推送 / 错 token 必须报错（不能假成功）
  --bench：单连接限速 mock 上测吞吐（多连接参数前后对比用）
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import types
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

LINKSWIFT_QUARK_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                      "quark-cloud-drive/2.5.20 Chrome/100.0.4896.160 Electron/18.3.5.4-b478491100 "
                      "Safari/537.36 Channel/pckk_other_ch")


def _stub_tk():
    """无图形环境：给 tkinter 打桩，只为能 import video_downloader 拿后端方法。"""
    try:
        import tkinter  # noqa: F401
        return
    except Exception:
        pass

    class _Any:
        def __init__(self, *a, **k):
            pass

        def __getattr__(self, n):
            return _Any()

        def __call__(self, *a, **k):
            return _Any()

    for m in ("tkinter", "tkinter.font", "tkinter.ttk", "tkinter.filedialog", "tkinter.messagebox"):
        mod = types.ModuleType(m)
        mod.__getattr__ = lambda n: _Any
        sys.modules[m] = mod
    sys.modules["tkinter"].font = sys.modules["tkinter.font"]


def make_backend(tmp, aria2c, logs):
    _stub_tk()
    import video_downloader as V
    V.HISTORY_FILE = os.path.join(tmp, "downloaded.json")     # 别碰真实账本
    V.STATS_FILE = os.path.join(tmp, "stats.json")
    V.BASE_DIR = tmp
    app = V.App.__new__(V.App)
    app.cfg = {"outdir": os.path.join(tmp, "out"), "aria2c_path": aria2c, "dl_threads": "自动"}
    app.log = lambda m: logs.append("%.2f %s" % (time.time(), m))
    app._rpc_tasks, app._rpc_order = {}, []
    app._rpc_lock = threading.Lock()
    app._bt = {"proc": None, "port": 0, "rpc_id": 0}
    app._bt_lock = threading.Lock()
    app._rx_bytes, app._rx_blocked = 0, False
    app._qmem_mark = lambda force=False: None
    app._rpc_fire_mascot = lambda ok: None
    app._rx_account = lambda path: None
    app._bt_datadir = lambda: os.makedirs(os.path.join(tmp, "bt"), exist_ok=True) or os.path.join(tmp, "bt")
    return V, app


class Rpc:
    def __init__(self, port):
        self.url = "http://127.0.0.1:%d/jsonrpc" % port

    def call(self, method, params, origin=None):
        body = json.dumps({"id": int(time.time() * 1000), "jsonrpc": "2.0",
                           "method": method, "params": params}).encode()
        h = {"Accept": "application/json", "Content-Type": "application/json;charset=UTF-8"}
        if origin:
            h["Origin"] = origin
        req = urllib.request.Request(self.url, data=body, headers=h)
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))


def linkswift_addUri(link, filename, headers, token="", dir_=None):
    """逐字照抄 LinkSwift sendTo.aria2() 的组包（standHeaders 默认头 + user-agent/referer 选项）。"""
    std = {"Accept": "*/*", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0.0.0",
           "Origin": "https://pan.quark.cn", "Referer": "https://pan.quark.cn/"}
    std.update(headers)
    parts = ["%s: %s" % (k, v) for k, v in std.items()]
    opt = {}
    if dir_:
        opt["dir"] = dir_
    if std.get("User-Agent"):
        opt["user-agent"] = std["User-Agent"]
    opt["referer"] = std.get("Referer") or "https://pan.quark.cn/"
    opt["out"] = filename
    opt["header"] = parts
    params = [[link], opt]
    if token:
        params.insert(0, "token:" + token)
    return params


def wait_task(rpc, gid, timeout=60, token=""):
    t0 = time.time()
    last = None
    pre = ["token:" + token] if token else []
    while time.time() - t0 < timeout:
        r = rpc.call("aria2.tellStatus", pre + [gid])
        if "error" in r:
            # 老版本没实现 tellStatus：退回 tellActive/tellStopped 里找
            got = None
            for m, p in (("aria2.tellActive", []), ("aria2.tellWaiting", [0, 100]),
                         ("aria2.tellStopped", [0, 100])):
                rr = rpc.call(m, pre + p)
                for it in rr.get("result") or []:
                    if it.get("gid") == gid:
                        got = it
            last = got or {"status": "?missing", "_tellStatus_err": r["error"]}
        else:
            last = r["result"]
        if last.get("status") in ("complete", "error", "removed"):
            return last, time.time() - t0
        time.sleep(0.3)
    return last, time.time() - t0


def mock_url(port, exp_delta=3600, fn="hanshuang-pojia-2.0.2.zip", tag="ngTX6F13"):
    exp = int(time.time() + exp_delta)
    return ("http://127.0.0.1:%d/%s/abc/def?abt=8_0_&auth_key=%d-351891-21600-0123abcd&sp=100"
            "&token=x&filename=%s" % (port, tag, exp, fn))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--aria2c", default=shutil.which("aria2c") or os.path.expanduser("~/bin/aria2c"))
    ap.add_argument("--only", default="A,B,C,D,E,F,G,H")
    ap.add_argument("--bench", action="store_true")
    ap.add_argument("--mb", type=int, default=8)
    ap.add_argument("--bench-mb", type=int, default=64)
    ap.add_argument("--bench-kbps", type=int, default=512, help="mock 单连接限速 KB/s")
    ap.add_argument("--bench-slow-every", type=int, default=0, help="每第 N 条连接拖成 32KB/s（0=不拖）")
    ap.add_argument("--bench-random", action="store_true", help="拖后腿连接按 1/N 概率随机抽（更像真实 CDN）")
    ap.add_argument("--bench-flaky", type=int, default=0, help="开头 N 个请求回 503（CDN 瞬时抖动）")
    a = ap.parse_args()
    only = set(a.only.split(","))
    import mock_netdisk_site as M
    import rpc_server as RS
    tmp = tempfile.mkdtemp(prefix="sdl_nd_")
    logs = []
    V, app = make_backend(tmp, a.aria2c, logs)
    mport = 18798
    M.serve(mport, a.mb * 1024 * 1024, "quark-cloud-drive", "__pus=OK")
    M.CFG["slow_random"] = bool(a.bench_random)
    M.CFG["fail_first"] = 0
    BENCH_FLAKY[0] = a.bench_flaky
    srv = RS.RpcServer(app, port=0, token="")
    rport = srv.start()
    rpc = Rpc(rport)
    want = M.expected_sha256()
    res = []

    def ok(name, cond, detail=""):
        res.append((name, bool(cond), detail))
        print("%s %-58s %s" % ("PASS" if cond else "FAIL", name, detail))

    good_hdr = {"User-Agent": LINKSWIFT_QUARK_UA, "Cookie": "__pus=OK; __puus=zz; b-user-id=1"}
    try:
        r = rpc.call("aria2.getVersion", ["token:"])
        ok("0 getVersion(token:空) 握手成功", "result" in r, json.dumps(r)[:90])

        if "A" in only:
            urllib.request.urlopen("http://127.0.0.1:%d/__reset" % mport).read()
            link = mock_url(mport)
            r = rpc.call("aria2.addUri", linkswift_addUri(link, "hanshuang-pojia-2.0.2.zip", good_hdr))
            gid = r.get("result")
            ok("A1 addUri 返回 gid", bool(gid), str(r)[:80])
            st, dt = wait_task(rpc, gid, 90)
            ok("A2 任务完成(complete)", st and st.get("status") == "complete",
               "status=%s err=%s 用时%.1fs" % (st and st.get("status"), st and st.get("errorMessage"), dt))
            p = os.path.join(app.cfg["outdir"])
            fp = ""
            for d, _, fs in os.walk(p):
                for f in fs:
                    if f == "hanshuang-pojia-2.0.2.zip":
                        fp = os.path.join(d, f)
            sha = hashlib.sha256(open(fp, "rb").read()).hexdigest() if fp else ""
            ok("A3 文件内容 sha256 正确", sha == want, fp)
            stt = json.loads(urllib.request.urlopen("http://127.0.0.1:%d/__stats" % mport).read())
            reqs = [q for q in stt["reqs"] if q["path"].startswith("/ngTX6F13")]
            ua_ok = reqs and all(q["ua"] == LINKSWIFT_QUARK_UA for q in reqs)
            ck_ok = reqs and all(q["cookie"] == good_hdr["Cookie"] for q in reqs)
            ok("A4 每个请求 UA 与推送方一字不差", ua_ok, "%d 个请求; 例 UA=%s" % (len(reqs), reqs[0]["ua"][:50] if reqs else "-"))
            ok("A5 每个请求 Cookie 与推送方一字不差", ck_ok, (reqs[0]["cookie"] if reqs else "-"))
            ok("A6 多连接（服务端最大并发>1）", stt["max_conc"] > 1, "max_conc=%d" % stt["max_conc"])

        if "B" in only:
            link = mock_url(mport, fn="nocookie.zip", tag="ngTX6F13b")
            hdr = {"User-Agent": LINKSWIFT_QUARK_UA}
            r = rpc.call("aria2.addUri", linkswift_addUri(link, "nocookie.zip", hdr))
            st, dt = wait_task(rpc, r.get("result"), 60)
            em = (st or {}).get("errorMessage") or ""
            ok("B1 缺 Cookie → 状态=error（不静默）", st and st.get("status") == "error",
               "status=%s 用时%.1fs" % (st and st.get("status"), dt))
            ok("B2 失败原因写明缺登录 Cookie / 412", ("Cookie" in em and "412" in em), em[:120])

        if "C" in only:
            link = mock_url(mport, exp_delta=-3600, fn="expired.zip", tag="ngTX6F13c")
            r = rpc.call("aria2.addUri", linkswift_addUri(link, "expired.zip", good_hdr))
            st, dt = wait_task(rpc, r.get("result"), 60)
            em = (st or {}).get("errorMessage") or ""
            ok("C1 过期链接 → 状态=error", st and st.get("status") == "error", "用时%.1fs" % dt)
            ok("C2 失败原因写明「过期」", "过期" in em, em[:120])

        if "D" in only:
            # 模拟记忆系统恢复出来的「待继续」旧任务（旧签名），然后脚本推同一文件的新链接
            old = mock_url(mport, exp_delta=-3600, fn="again.zip", tag="ngTX6F13d")
            gid_old = app.rpc_add_uri(old, {"out": "again.zip"}, defer=True, src="user")
            with app._rpc_lock:
                app._rpc_tasks[gid_old]["status"] = "paused"
                app._rpc_tasks[gid_old]["note"] = "重启未自动重下·点继续才跑"
            new = mock_url(mport, fn="again.zip", tag="ngTX6F13d")
            r = rpc.call("aria2.addUri", linkswift_addUri(new, "again.zip", good_hdr))
            gid = r.get("result")
            st, dt = wait_task(rpc, gid, 60)
            ok("D1 已有「待继续」同名旧任务时再推送 → 真下完",
               st and st.get("status") == "complete",
               "返回gid=%s(旧=%s) status=%s err=%s" % (gid, gid_old, st and st.get("status"),
                                                       (st or {}).get("errorMessage", "")[:60]))

        if "E" in only:
            # 共享 aria2(BT) 守护进程被占满：max-concurrent-downloads=1 + 一条永远连不上的任务。
            # 修复前直链借用这个守护进程 → 无限排队；修复后直链走独立 HTTP 引擎，不受影响
            try:
                app._bt_ensure()
                app._bt_rpc("aria2.changeGlobalOption", [{"max-concurrent-downloads": "1"}])
                slow = "http://127.0.0.1:%d/ngTX6F13slow/x?filename=slow.bin&auth_key=%d-1-1-1" % (
                    mport, int(time.time() + 3600))
                M.CFG["conn_kbps"] = 0
                blocker = app._bt_rpc("aria2.addUri", [["http://10.255.255.1:9/never.bin"],
                                                       {"dir": tmp, "connect-timeout": "600",
                                                        "timeout": "600", "max-tries": "0"}])
                time.sleep(0.5)
                link = mock_url(mport, fn="busy.zip", tag="ngTX6F13e")
                r = rpc.call("aria2.addUri", linkswift_addUri(link, "busy.zip", good_hdr))
                st, dt = wait_task(rpc, r.get("result"), 45)
                ok("E1 共享 aria2 队列被占满时推送仍能下完", st and st.get("status") == "complete",
                   "status=%s 用时%.1fs err=%s" % (st and st.get("status"), dt,
                                                   (st or {}).get("errorMessage", "")[:80]))
                try:
                    app._bt_rpc("aria2.forceRemove", [blocker])
                    app._bt_rpc("aria2.changeGlobalOption", [{"max-concurrent-downloads": "5"}])
                except Exception:
                    pass
            except Exception as e:
                ok("E1 共享 aria2 队列被占满时推送仍能下完", False, "异常 %s" % e)

        if "F" in only:
            srv2 = RS.RpcServer(app, port=0, token="s3cret")
            p2 = srv2.start()
            rpc2 = Rpc(p2)
            r = rpc2.call("aria2.getVersion", ["token:s3cret"])
            ok("F1 带 token 握手成功", "result" in r)
            r = rpc2.call("aria2.addUri", linkswift_addUri(mock_url(mport, fn="tok.zip", tag="t"), "tok.zip",
                                                            good_hdr, token="wrong"))
            ok("F2 错 token → 报错（不假成功）", "error" in r, json.dumps(r)[:90])
            srv2.stop()

        if "G" in only:
            # 网盘直链下载助手(panlinker) 原版组包：params 首位恒为 "token:"（即使没设 token）、dir="D:"（裸盘符），
            # header 用小写键；另一类脚本只给 aria2 的 user-agent 选项、header 里不放 UA
            urllib.request.urlopen("http://127.0.0.1:%d/__reset" % mport).read()
            link = mock_url(mport, fn="panlinker.zip", tag="ngTX6F13g")
            params = ["token:", [link], {"dir": "D:", "out": "panlinker.zip",
                                         "header": ["user-agent: " + LINKSWIFT_QUARK_UA,
                                                    "cookie: __pus=OK; BDUSS=x"]}]
            r = rpc.call("aria2.addUri", params)
            st, dt = wait_task(rpc, r.get("result"), 60)
            stt = json.loads(urllib.request.urlopen("http://127.0.0.1:%d/__stats" % mport).read())
            reqs = [q for q in stt["reqs"] if q["path"].startswith("/ngTX6F13g")]
            ok("G1 panlinker 组包(token:空 + dir=D: + 小写头) → 下完", st and st.get("status") == "complete",
               "status=%s dir=%s err=%s" % (st and st.get("status"), (st or {}).get("dir"),
                                            (st or {}).get("errorMessage", "")[:60]))
            ok("G2 小写 user-agent 头原样送达（没被程序 Chrome UA 顶掉/变双 UA）",
               reqs and all(q["ua"] == LINKSWIFT_QUARK_UA for q in reqs), "%d 请求" % len(reqs))
            ok("G3 裸盘符 D: 不当保存目录（落到 SniffDL 下载目录）",
               st and not str((st or {}).get("dir", "")).startswith("D:"), str((st or {}).get("dir")))
            link = mock_url(mport, fn="optua.zip", tag="ngTX6F13h")
            params = [[link], {"out": "optua.zip", "user-agent": LINKSWIFT_QUARK_UA,
                               "header": ["Cookie: __pus=OK"]}]
            urllib.request.urlopen("http://127.0.0.1:%d/__reset" % mport).read()
            r = rpc.call("aria2.addUri", params)
            st, dt = wait_task(rpc, r.get("result"), 60)
            stt = json.loads(urllib.request.urlopen("http://127.0.0.1:%d/__stats" % mport).read())
            reqs = [q for q in stt["reqs"] if q["path"].startswith("/ngTX6F13h")]
            ok("G4 只给 aria2「user-agent」选项（header 无 UA）→ 照样用推送方 UA 下完",
               st and st.get("status") == "complete" and reqs and all(q["ua"] == LINKSWIFT_QUARK_UA for q in reqs),
               "status=%s" % (st and st.get("status")))
            r = rpc.call("aria2.tellStatus", [r.get("result"), ["gid", "status", "totalLength"]])
            ok("G5 tellStatus(keys 过滤) 按 aria2 语义返回", set((r.get("result") or {}).keys()) ==
               {"gid", "status", "totalLength"}, json.dumps(r.get("result"))[:90])
            r = rpc.call("system.multicall", [[{"methodName": "aria2.getVersion", "params": ["token:"]},
                                               {"methodName": "aria2.getGlobalStat", "params": ["token:"]}]])
            ok("G6 system.multicall（子调用各带 token:）", isinstance(r.get("result"), list) and
               all(isinstance(x, list) for x in r["result"]), json.dumps(r)[:90])

        if "H" in only:
            # 手动粘贴直链（界面 sniffdl.addDirect）：① 扩展同步来的浏览器 Cookie 按域自动补齐；② 自定义请求头框
            import netdisk_rules as ND
            ND.PROFILES.insert(0, {"key": "mock", "name": "mock网盘", "dl_hosts": ("127.0.0.1",),
                                   "cookie_domain": "127.0.0.1", "referer": "https://pan.quark.cn/",
                                   "default_ua": None, "conn": 6, "min_split": "1M", "use_aria2": True,
                                   "need_cookie": True, "expiry": "auth_key", "tips": "mock"})
            try:
                r = rpc.call("sniffdl.syncCookies", [[
                    {"name": "__pus", "value": "OK", "domain": "127.0.0.1", "hostOnly": True, "path": "/",
                     "secure": False, "expirationDate": time.time() + 3600},
                    {"name": "other", "value": "X", "domain": ".example.com", "path": "/"}]])
                ok("H1 sniffdl.syncCookies 收下浏览器 Cookie", (r.get("result") or {}).get("n") == 2, json.dumps(r)[:80])
                M.CFG["ua"] = ""
                urllib.request.urlopen("http://127.0.0.1:%d/__reset" % mport).read()
                link = mock_url(mport, fn="paste.zip", tag="ngTX6F13p")
                r = rpc.call("sniffdl.addDirect", [{"url": link, "headers_text": ""}])
                gid = (r.get("result") or {}).get("gid")
                st, dt = wait_task(rpc, gid, 60)
                stt = json.loads(urllib.request.urlopen("http://127.0.0.1:%d/__stats" % mport).read())
                reqs = [q for q in stt["reqs"] if q["path"].startswith("/ngTX6F13p")]
                ok("H2 粘贴网盘直链(无头) → 自动带上同步来的 Cookie 下完",
                   st and st.get("status") == "complete" and reqs and all("__pus" in q["cookie_names"] for q in reqs)
                   and not any("other" in q["cookie_names"] for q in reqs),
                   "status=%s cookies=%s" % (st and st.get("status"), reqs[0]["cookie_names"] if reqs else "-"))
                # aria2 首个探测连接被它中途弃用时 mock 侧还在吐数据，会多计 1 条 → 上限按 规则+1 判
                ok("H3 网盘规则连接数生效（mock 规则=6，aria2 split=6）", 1 < stt["max_conc"] <= 7,
                   "max_conc=%d" % stt["max_conc"])
                M.CFG["ua"] = "quark-cloud-drive"
                urllib.request.urlopen("http://127.0.0.1:%d/__reset" % mport).read()
                link = mock_url(mport, fn="curl.zip", tag="ngTX6F13q")
                curl = "curl -L '%s' -A 'quark-cloud-drive/2.5.20 X' -b '__pus=OK; z=1' -H 'Referer: https://pan.quark.cn/'" % link
                r = rpc.call("sniffdl.addDirect", [{"url": link, "headers_text": curl}])
                st, dt = wait_task(rpc, (r.get("result") or {}).get("gid"), 60)
                stt = json.loads(urllib.request.urlopen("http://127.0.0.1:%d/__stats" % mport).read())
                reqs = [q for q in stt["reqs"] if q["path"].startswith("/ngTX6F13q")]
                ok("H4 自定义请求头框粘 curl 命令（-A/-b/-H）→ 原样使用并下完",
                   st and st.get("status") == "complete" and reqs and all(q["ua"] == "quark-cloud-drive/2.5.20 X" and
                                                                          q["cookie"] == "__pus=OK; z=1" for q in reqs),
                   "status=%s" % (st and st.get("status")))
                r = rpc.call("sniffdl.addDirect", [{"url": "https://www.bilibili.com/video/BV1xx", "headers_text": ""}])
                ok("H5 非网盘且没填头 → skip（界面照常嗅探）", (r.get("result") or {}).get("skip") is True)
            finally:
                ND.PROFILES.pop(0)
                M.CFG["ua"] = "quark-cloud-drive"

        if a.bench:
            bench(app, rpc, M, mport, a.bench_mb, a.bench_kbps, a.bench_slow_every)
    finally:
        srv.stop()
        try:
            if app._bt.get("proc"):
                app._bt["proc"].kill()
        except Exception:
            pass
        try:
            if getattr(app, "_h2", None) and app._h2.get("proc"):
                app._h2["proc"].kill()
        except Exception:
            pass
        with open(os.path.join(tmp, "app.log"), "w", encoding="utf-8") as f:
            f.write("\n".join(logs))
        print("---- app 日志（最后 40 行）: %s" % os.path.join(tmp, "app.log"))
        for ln in logs[-40:]:
            print("   ", ln[:220])
    bad = [n for n, c, _ in res if not c]
    print("NETDISK E2E RESULT=%s  (%d/%d PASS)" % ("PASS" if not bad else "FAIL", len(res) - len(bad), len(res)))
    return 0 if not bad else 1


BENCH_FLAKY = [0]


def bench(app, rpc, M, mport, mb=64, kbps=512, slow_every=0):
    """单连接限速 kbps KB/s 的 mock 上下 mb MB，报吞吐。slow_every>0 时每第 N 条连接只有 32KB/s。"""
    urllib.request.urlopen("http://127.0.0.1:%d/__reset" % mport).read()
    M.CFG["conn_kbps"] = kbps
    M.CFG["slow_every"] = slow_every
    M.CFG["fail_first"] = BENCH_FLAKY[0]
    M.CFG["size"] = mb * 1024 * 1024
    M._BODY.clear()
    link = mock_url(mport, fn="bench_%d.bin" % int(time.time()), tag="bench")
    t0 = time.time()
    r = rpc.call("aria2.addUri", linkswift_addUri(link, link.rsplit("=", 1)[-1],
                                                  {"User-Agent": LINKSWIFT_QUARK_UA, "Cookie": "__pus=OK"}))
    st, dt = wait_task(rpc, r.get("result"), 600)
    stt = json.loads(urllib.request.urlopen("http://127.0.0.1:%d/__stats" % mport).read())
    sp = mb / max(dt, 0.01)
    M.CFG["fail_first"] = 0
    print("BENCH(抖动503前%d个) " % BENCH_FLAKY[0] + "%dMB 单连接限速 %dKB/s 拖后腿=每%s条 → 用时 %.1fs 平均 %.2f MB/s  服务端最大并发=%d 连接总数=%d  status=%s" % (
        mb, kbps, slow_every or "-", dt, sp, stt["max_conc"], stt.get("nconn", 0), st and st.get("status")))
    M.CFG["conn_kbps"] = 0
    M.CFG["slow_every"] = 0
    return dt


if __name__ == "__main__":
    sys.exit(main())
