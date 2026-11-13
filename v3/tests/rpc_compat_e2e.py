# -*- coding: utf-8 -*-
"""aria2 RPC 客户端方法清单对照（计划7）：AriaNg / Motrix / LinkSwift 常用方法必须可用、返回形状正确。
起真内核，经 HTTP /jsonrpc 调用。退出码 0 = 全过。"""
import os, sys, threading, subprocess, shutil
os.environ.setdefault("SDL_E2E_DATA", "/tmp/sdlv3_rpccompat")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core_e2e as E
from core_e2e import check, wait

# AriaNg 与 Motrix 实际调用的方法（来自两者公开源码的方法清单）
CLIENT_METHODS = """aria2.addUri aria2.addTorrent aria2.addMetalink aria2.remove aria2.forceRemove aria2.pause aria2.pauseAll
aria2.forcePause aria2.forcePauseAll aria2.unpause aria2.unpauseAll aria2.tellStatus aria2.getUris aria2.getFiles
aria2.getPeers aria2.getServers aria2.tellActive aria2.tellWaiting aria2.tellStopped aria2.changePosition
aria2.changeOption aria2.getOption aria2.changeGlobalOption aria2.getGlobalOption aria2.getGlobalStat
aria2.purgeDownloadResult aria2.removeDownloadResult aria2.getVersion aria2.getSessionInfo aria2.shutdown
aria2.forceShutdown aria2.saveSession system.listMethods system.listNotifications""".split()

def main():
    E.Files.add("f16.bin", 1 << 20)
    shutil.rmtree(E.DATA, ignore_errors=True); os.makedirs(E.OUT, exist_ok=True)
    srv = E.QuietServer(("127.0.0.1", 0), E.Handler); E.FPORT[0] = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    E.PORT[0] = E.free_port()
    env = dict(os.environ, SNIFFDL_DRYRUN_OS="1", NO_PROXY="127.0.0.1,localhost,::1")
    for k in ("HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","http_proxy","https_proxy","all_proxy","DISPLAY"): env.pop(k, None)
    core = subprocess.Popen([sys.executable, os.path.join(E.HERE,"tests","ui_smoke_core.py"),"--data-dir",E.DATA,
        "--port",str(E.PORT[0]),"--runtime",os.path.join(E.DATA,"runtime.json"),"--ui-token",E.TOKEN],
        cwd=E.HERE, env=env, stdout=open(os.path.join(E.DATA,"core.out"),"w"), stderr=subprocess.STDOUT)
    try:
        if not wait(lambda: E.raw_rpc("sniffdl.ping", [], token=None).get("result"), 30):
            check("内核启动", False); return 1
        E.setv(outdir=E.OUT, clip_watch=False, notify=False, conflict="重命名", bt_tracker_auto=False,
               bt_dht=False, bt_lpd=False, bt_pex=False, max_concurrent=3)
        R = E.raw_rpc
        lm = R("system.listMethods", []).get("result") or []
        miss = [m for m in CLIENT_METHODS if m not in lm]
        check("listMethods 覆盖 AriaNg/Motrix 全部 %d 个方法" % len(CLIENT_METHODS), not miss, miss)
        v = (R("aria2.getVersion", []).get("result") or {})
        check("getVersion 含 version 与 enabledFeatures 列表", bool(v.get("version")) and isinstance(v.get("enabledFeatures"), list), v)
        g = (R("aria2.getGlobalStat", []).get("result") or {})
        need = ["downloadSpeed","uploadSpeed","numActive","numWaiting","numStopped","numStoppedTotal"]
        check("getGlobalStat 六字段齐全且为字符串", all(isinstance(g.get(k), str) for k in need), g)
        gid = (R("aria2.addUri", [["http://127.0.0.1:%d/f16.bin" % E.FPORT[0]], {"dir": E.OUT}]).get("result"))
        check("addUri 返回 16 位十六进制 gid", isinstance(gid, str) and len(gid) == 16 and int(gid, 16) >= 0, gid)
        st = {}
        ok = wait(lambda: (R("aria2.tellStatus", [gid]).get("result") or {}).get("status") in ("complete","active","error"), 20)
        st = R("aria2.tellStatus", [gid]).get("result") or {}
        check("tellStatus 含 gid/status/totalLength/completedLength/files", all(k in st for k in ("gid","status","totalLength","completedLength","files")), list(st))
        check("tellStatus 数值字段为字符串", isinstance(st.get("totalLength"), str) and isinstance(st.get("completedLength"), str), st.get("totalLength"))
        st2 = R("aria2.tellStatus", [gid, ["gid","status"]]).get("result") or {}
        check("tellStatus 支持 keys 过滤", set(st2.keys()) == {"gid","status"}, st2)
        wait(lambda: (R("aria2.tellStatus", [gid]).get("result") or {}).get("status") == "complete", 30)
        ts = R("aria2.tellStopped", [0, 10]).get("result")
        check("tellStopped(0,10) 返回列表且含该 gid", isinstance(ts, list) and any(x.get("gid") == gid for x in ts), ts)
        ta = R("aria2.tellActive", []).get("result"); tw = R("aria2.tellWaiting", [0, 10]).get("result")
        check("tellActive/tellWaiting 返回列表", isinstance(ta, list) and isinstance(tw, list), (ta, tw))
        bad = R("aria2.tellStatus", ["ffffffffffffffff"])
        check("不存在的 gid 返回 error 对象(code 1)", "error" in bad and bad["error"].get("code") == 1 and "result" not in bad, bad)
        un = R("aria2.noSuchMethod", [])
        check("未知方法返回 -32601", (un.get("error") or {}).get("code") == -32601, un)
        gf = R("aria2.getFiles", [gid]).get("result")
        check("getFiles 返回含 path/length/selected 的列表", isinstance(gf, list) and gf and all(k in gf[0] for k in ("path","length","selected")), gf)
        go = R("aria2.getGlobalOption", []).get("result")
        check("getGlobalOption 为字典", isinstance(go, dict), type(go))
        cg = R("aria2.changeGlobalOption", [{"max-overall-download-limit": "0"}])
        check("changeGlobalOption 返回 OK", cg.get("result") == "OK", cg)
        pr = R("aria2.removeDownloadResult", [gid])
        check("removeDownloadResult 返回 OK", pr.get("result") == "OK", pr)
        pd = R("aria2.purgeDownloadResult", [])
        check("purgeDownloadResult 返回 OK", pd.get("result") == "OK", pd)
        si = R("aria2.getSessionInfo", []).get("result") or {}
        check("getSessionInfo 含 sessionId", bool(si.get("sessionId")), si)
        nt = R("system.listNotifications", []).get("result") or []
        check("listNotifications 含 6 个 onDownload* 通知", sum(1 for n in nt if n.startswith("aria2.on")) >= 6, nt)
        # 批量 system.multicall
        mc = R("system.multicall", [[{"methodName":"aria2.getVersion","params":[]},{"methodName":"aria2.getGlobalStat","params":[]}]])
        r = mc.get("result")
        check("system.multicall 返回两个结果", isinstance(r, list) and len(r) == 2, mc)
    finally:
        core.terminate()
    return 0

if __name__ == "__main__":
    rc = main()
    bad = [n for n, ok in E.RESULTS if not ok]
    print("%d/%d 通过" % (len(E.RESULTS) - len(bad), len(E.RESULTS)))
    sys.exit(rc or (1 if bad else 0))
