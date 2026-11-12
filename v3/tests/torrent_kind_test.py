# -*- coding: utf-8 -*-
"""A5：.torrent 推送归类 BT。起真内核（隔离数据目录），经 /jsonrpc sniffdl.task.add 推送，看任务 kind：
  无后缀种子直链（attachment.php?aid=）+ 推送名 *.torrent → bt；同 URL 形态 + 名 .zip → http；
  路径 .torrent → bt；磁力 → bt；Content-Type 判定函数；任务恢复后（只剩 uri+name）仍认 bt。
用法：source env/setup.sh && source env/v3env.sh && python3 tests/torrent_kind_test.py；退出码 0 = 全过。"""
import os, sys, time, shutil, subprocess, threading
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "tests"))
import core_e2e as E                       # 复用其 RPC 帮手与本地文件服务器

R = []
def chk(n, c, d=""):
    R.append(bool(c)); print(("PASS " if c else "FAIL ") + n + ("" if c else "  ← %s" % str(d)[:300]), flush=True)

def kind_of(gid):
    for t in E.rpc("sniffdl.tasks"):
        if t["gid"] == gid:
            return t.get("kind"), t
    return None, None

def main():
    E.DATA = "/tmp/sdlv3_tkind"; E.OUT = os.path.join(E.DATA, "out")
    shutil.rmtree(E.DATA, ignore_errors=True); os.makedirs(E.OUT)
    srv = E.QuietServer(("127.0.0.1", 0), E.Handler); E.FPORT[0] = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    E.PORT[0] = E.free_port()
    env = dict(os.environ, SNIFFDL_DRYRUN_OS="1", NO_PROXY="127.0.0.1,localhost")
    env.pop("DISPLAY", None)
    core = subprocess.Popen([sys.executable, os.path.join(HERE, "tests", "ui_smoke_core.py"), "--data-dir", E.DATA,
                             "--port", str(E.PORT[0]), "--runtime", os.path.join(E.DATA, "runtime.json"),
                             "--ui-token", E.TOKEN], cwd=HERE, env=env,
                            stdout=open(os.path.join(E.DATA, "core.out"), "w"), stderr=subprocess.STDOUT)
    try:
        chk("内核启动", E.wait(lambda: E.raw_rpc("sniffdl.ping", [], token=None).get("result"), 30))
        E.setv(outdir=E.OUT, clip_watch=False, notify=False, bt_tracker_auto=False, bt_dht=False, bt_lpd=False,
               bt_pex=False, auto_category=False)
        base = "http://127.0.0.1:%d" % E.FPORT[0]
        def add(url, name):
            E._last = E.rpc("sniffdl.task.add", {"url": url, "name": name, "dir": E.OUT, "defer": True})
            r = E._last
            return r.get("gid") if isinstance(r, dict) else r
        g1 = add(base + "/forum/attachment.php?aid=12345", "电影合集.torrent")
        k1, t1 = kind_of(g1)
        chk("无后缀种子直链 + 推送名 .torrent → kind=bt", k1 == "bt", t1)
        g2 = add(base + "/forum/attachment.php?aid=777", "资料.zip")
        k2, t2 = kind_of(g2)
        chk("同形态 URL + 推送名 .zip → 不归 BT（kind=%s）" % k2, k2 in ("file", "http"), t2)
        g3 = add(base + "/x/abc.torrent", "")
        chk("路径 .torrent → kind=bt（原规则不回退）", kind_of(g3)[0] == "bt", kind_of(g3)[1])
        g4 = add("magnet:?xt=urn:btih:" + "a" * 40, "")
        chk("磁力 → kind=bt", kind_of(g4)[0] == "bt", kind_of(g4)[1])
        # 扩展接管路径：只有 mime（浏览器 DownloadItem.mime），没文件名线索
        before = {t["gid"] for t in E.rpc("sniffdl.tasks")}
        u5 = base + "/dl/get?id=31337"
        r5 = E.raw_rpc("sniffdl.intercept", [{"url": u5, "filename": "", "mime": "application/x-bittorrent",
                                              "source": "browser", "size": 1234}])
        new5 = E.wait(lambda: [t for t in E.rpc("sniffdl.tasks") if t["gid"] not in before and t.get("uri") == u5], 10)
        chk("扩展接管 mime=application/x-bittorrent 无后缀链接 → kind=bt", new5 and new5[0].get("kind") == "bt",
            (r5, new5))
    finally:
        try: E.raw_rpc("sniffdl.core.shutdown", [], timeout=5)
        except Exception: pass
        try: core.wait(8)
        except Exception: core.kill()
        srv.shutdown()
    # 进程内：判定函数 + 恢复场景（注册表清空，只剩任务的 uri+name）
    sys.path.insert(0, HERE)
    import ui_smoke_core; ui_smoke_core._stub_tk()
    from core import engine as CE
    cls = [v for v in vars(CE).values() if isinstance(v, type) and "_is_torrent_hint" in v.__dict__][0]
    chk("Content-Type application/x-bittorrent → 种子", cls._is_torrent_hint("", "application/x-bittorrent; charset=x"))
    chk("名 a.TORRENT 大小写 → 种子", cls._is_torrent_hint("a.TORRENT"))
    chk("名 a.zip + octet-stream → 不是种子", not cls._is_torrent_hint("a.zip", "application/octet-stream"))
    e = cls.__new__(cls)
    u = "http://h/attachment.php?aid=9"
    e._rpc_tasks = {"g": {"uri": u, "name": "x.torrent"}}
    chk("恢复后（只剩 uri+name）_rpc_is_bt 仍认 bt", e._rpc_is_bt(u))
    chk("恢复后无关 URL 不误判", not e._rpc_is_bt("http://h/attachment.php?aid=10"))
    print("%d/%d" % (sum(R), len(R)))
    return 0 if all(R) else 1

if __name__ == "__main__":
    sys.exit(main())
