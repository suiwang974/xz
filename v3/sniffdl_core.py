# -*- coding: utf-8 -*-
"""SniffDL v3 内核入口（无界面后台服务）。

    python sniffdl_core.py [--port 6800] [--runtime runtime.json] [--ui-token TOKEN]
                           [--host electron|webview|none] [--ui-dir ui] [--watch-stdin] [--parent-pid PID]
打包后：SniffDL.exe --core …（同参数）。

启动成功后 stdout 打一行 SNIFFDL_READY {json}，并写 runtime.json（端口/界面令牌/pid）。
已有内核在跑（runtime.json 里的 pid 还活着且能 ping 通）→ 直接把那份信息打出来并退出，宿主连那个即可。
"""
import argparse
import json
import os
import sys
import threading
import time
import urllib.request

T0 = time.time()   # 冷启动计时起点（--selftest 报「到 READY 用时」）
HERE = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))


def _default_ui_dir():
    """PyInstaller 6 onedir 的数据在 _internal（=_MEIPASS），不在 exe 旁边：两处都找。"""
    for base in (getattr(sys, "_MEIPASS", None), HERE):
        if base and os.path.isdir(os.path.join(base, "ui")):
            return os.path.join(base, "ui")
    return os.path.join(HERE, "ui")


def _ping(info):
    try:
        req = urllib.request.Request("http://127.0.0.1:%d/jsonrpc" % int(info["port"]),
                                     data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "sniffdl.ping",
                                                      "params": []}).encode(),
                                     headers={"Content-Type": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return "SniffDL" in opener.open(req, timeout=2).read().decode("utf-8", "ignore")
    except Exception:
        return False


def _run_selftest(a, eng, V, tmp):
    import shutil
    import pack_selftest as PS
    ready_s = time.time() - T0
    items = []
    try:
        items += PS.check_modules()
        try:
            a2 = V.resolve_aria2c(eng.cfg) or V.ARIA2C
        except Exception:
            a2 = getattr(V, "ARIA2C", None)
        items.append(PS.check_aria2c(a2))
        items.append(PS.check_cn_dir(V.BASE_DIR))
        items += PS.check_resources()
        items.append(PS.check_rpc_up(eng.port))
        from core import api as _api
        try:
            ent = getattr(_api, "_REG", {}).get("sniffdl.selftest")
            fn = ent[0] if ent else _api._selftest
            res = fn(eng, [])
        except Exception as ex:
            res = {"items": [{"name": "自检执行", "ok": False, "detail": str(ex)[:200]}]}
        items += PS.merge_api_items(res.get("items"), strict=a.strict)
    except Exception as ex:   # 自检本身炸了也要落一条 FAIL，不能静默 0
        items.append(PS._item("自检执行", False, "%s: %s" % (type(ex).__name__, str(ex)[:200])))
    port = eng.port
    eng.shutdown()
    items.append(PS.check_rpc_down(port, eng.runtime_path))
    summ = PS.summarize(items)
    extra = {"frozen": PS.is_frozen(), "ready_s": round(ready_s, 2), "data_dir": V.BASE_DIR,
             "resource_dirs": PS.resource_dirs()}
    PS.emit(PS.format_lines(items, summ, extra), a.selftest_out or None)
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0 if summ["ok"] else 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sniffdl-core")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--runtime", default=os.path.join(HERE, "runtime.json"))
    ap.add_argument("--ui-token", default="")
    ap.add_argument("--host", default="none")
    ap.add_argument("--ui-dir", default=_default_ui_dir())
    ap.add_argument("--watch-stdin", action="store_true", help="stdin 关闭（宿主退出）时内核跟着退")
    ap.add_argument("--parent-pid", type=int, default=0)
    ap.add_argument("--data-dir", default=os.environ.get("SNIFFDL_DATA_DIR", ""),
                    help="配置/任务库/日志放这里（默认程序目录；测试用临时目录隔离）")
    ap.add_argument("--selftest", action="store_true",
                    help="自检：隔离的中文数据目录起内核→打包清单+sniffdl.selftest→起停 RPC→打印→退出（0=全过）")
    ap.add_argument("--selftest-out", default=os.environ.get("SNIFFDL_SELFTEST_OUT", ""),
                    help="自检结果另写一份到这个文件（窗口版 exe 没有控制台，CI 读它）")
    ap.add_argument("--strict", action="store_true", help="自检里 ffmpeg/6800 端口/保存目录 也算致命项")
    a = ap.parse_args(argv)
    tmp = None
    if a.selftest:
        import tempfile
        tmp = tempfile.mkdtemp(prefix="sdl_selftest_")
        # 数据目录故意用「中文 + 空格」：整条内核链路都在这种路径上跑一遍
        a.data_dir = a.data_dir or os.path.join(tmp, "数据 目录")
        a.runtime = os.path.join(tmp, "runtime.json")

    from core import runtime as RT
    old = RT.read(a.runtime)
    if old and _ping(old):
        old["attached"] = True
        sys.stdout.write("SNIFFDL_READY " + json.dumps(old, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        return 0

    sys.path.insert(0, HERE)
    os.chdir(HERE)
    import video_downloader as V
    if a.data_dir:
        dd = os.path.abspath(a.data_dir)
        os.makedirs(dd, exist_ok=True)
        V.BASE_DIR = dd
        V.CONFIG_FILE = os.path.join(dd, "config.json")
        V.HISTORY_FILE = os.path.join(dd, "downloaded.json")
        V.QUEUE_FILE = os.path.join(dd, "queue.json")
        V.STATS_FILE = os.path.join(dd, "stats.json")
        V.LOG_FILE = os.path.join(dd, "sniffdl.log")
    V.setup_logging()
    try:
        V.install_crash_hooks()
    except Exception:
        pass
    from core.engine import Engine
    eng = Engine(runtime_path=a.runtime, ui_token=a.ui_token or None, host=a.host,
                 ui_dir=a.ui_dir if os.path.isdir(a.ui_dir) else None)
    eng.start(port=a.port or None)

    if a.selftest:
        return _run_selftest(a, eng, V, tmp)

    stop = threading.Event()

    def _drop_rt():
        # Windows 实测 B8：宿主被强杀后内核走看门狗自退，后面的完整 shutdown 里若被谁掐断，runtime.json 就留下了
        # → 下次启动被误认成「内核还活着」。退出信号一到就先把自己的 runtime.json 删掉（只删 pid 是自己的）。
        try:
            info = RT.read(eng.runtime_path)
            if not info or int(info.get("pid") or 0) == os.getpid():
                RT.remove(eng.runtime_path)
        except Exception:
            pass

    def _bye(*_):
        _drop_rt()
        stop.set()
    if a.watch_stdin:
        def _w():
            try:
                while sys.stdin.read(1):
                    pass
            except Exception:
                pass
            _bye()
        threading.Thread(target=_w, daemon=True).start()
    if a.parent_pid:
        def _pw():
            while not stop.is_set():
                if not RT._alive(a.parent_pid):
                    eng.log("[内核] 宿主进程已退出，内核随之退出")
                    _bye()
                    return
                time.sleep(2)
        threading.Thread(target=_pw, daemon=True).start()
    try:
        import signal
        signal.signal(signal.SIGTERM, _bye)
        signal.signal(signal.SIGINT, _bye)
    except Exception:
        pass
    while not stop.is_set() and not eng._stop.is_set():
        stop.wait(0.5)
    eng.shutdown()
    _drop_rt()
    return 0


if __name__ == "__main__":
    sys.exit(main())
