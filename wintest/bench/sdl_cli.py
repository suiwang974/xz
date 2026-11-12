# -*- coding: utf-8 -*-
"""SniffDL 测速命令行：走 v3 内核真实入口 core.speed.run_http（= 直链任务交给自研多连接引擎 sdl_speed 的那一步）。

用法：
  python sdl_cli.py --v3 PATH --out DIR [--parallel 8] [--via core|engine] URL [URL ...]
  python sdl_cli.py --v3 PATH --out DIR --list urls.txt
结束时 stdout 最后一行打印 JSON：{"ok": n, "fail": n, "elapsed": 秒, "bytes": n, "errors": [...], "logs": [...]}

--via core（默认）：core.speed.run_http(eng, url, outdir, name, headers)，eng 是最小桩（设置全默认：
  最大连接数=自动、不限速），连接策略/网盘规则/代理判断/临时文件后缀与 SniffDL 实际下载一致。
  内核抛 Fallback（真实 App 里会回退 aria2）在这里记为失败，不偷偷换别的工具。
--via engine：直接 sdl_speed.download_blocking（不经内核，用来区分内核开销）。
多个 URL 时按 --parallel 个任务同时下载（对应设置「同时下载数」，测速时三家工具设成同一个值）。
"""
import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit, unquote


class _StubEng:
    """core.speed 需要的最小 Engine 接口：cfg / gbucket / _buckets / log。"""

    def __init__(self):
        self.cfg = {"max_conns": "自动", "speed_engine": "auto"}
        self.gbucket = None
        self._buckets = {}
        self.logs = []
        self._lk = threading.Lock()

    def log(self, msg):
        with self._lk:
            if len(self.logs) < 400:
                self.logs.append(str(msg))


def _name_of(url):
    p = unquote(urlsplit(url).path)
    return os.path.basename(p) or "download.bin"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--v3", required=True, help="SniffDL v3 目录（含 core/ sdl_speed/）")
    ap.add_argument("--out", required=True)
    ap.add_argument("--parallel", type=int, default=8)
    ap.add_argument("--via", choices=("core", "engine"), default="core")
    ap.add_argument("--list")
    ap.add_argument("--ua", default="Mozilla/5.0 (Windows NT 10.0; Win64; x64) SniffDL-bench")
    ap.add_argument("urls", nargs="*")
    a = ap.parse_args(argv)
    v3 = os.path.abspath(a.v3)
    sys.path.insert(0, v3)
    urls = list(a.urls)
    if a.list:
        with open(a.list, encoding="utf-8") as f:
            urls += [ln.strip() for ln in f if ln.strip()]
    os.makedirs(a.out, exist_ok=True)

    import sdl_speed  # noqa: F401
    eng = _StubEng()
    if a.via == "core":
        from core import speed as CS
        if not CS.available():
            print(json.dumps({"ok": 0, "fail": len(urls), "elapsed": 0, "bytes": 0,
                              "errors": ["core.speed: sdl_speed 不可用"]}, ensure_ascii=False))
            return 2
    headers = {"User-Agent": a.ua}
    errors = []
    ok = [0]
    nbytes = [0]
    lk = threading.Lock()

    def one(url):
        name = _name_of(url)
        try:
            if a.via == "core":
                path = CS.run_http(eng, url, a.out, name, dict(headers), task={})
            else:
                path = sdl_speed.download_blocking(url, dict(headers), os.path.join(a.out, name))
            sz = os.path.getsize(path)
            with lk:
                ok[0] += 1
                nbytes[0] += sz
        except BaseException as e:  # noqa: BLE001  Fallback / RuntimeError / SpeedError 都记下
            with lk:
                errors.append("%s: %s: %s" % (name, type(e).__name__, str(e)[:200]))

    t0 = time.perf_counter()
    if len(urls) == 1:
        one(urls[0])
    else:
        with ThreadPoolExecutor(max_workers=max(1, a.parallel)) as ex:
            list(ex.map(one, urls))
    el = time.perf_counter() - t0
    out = {"ok": ok[0], "fail": len(urls) - ok[0], "elapsed": round(el, 4), "bytes": nbytes[0],
           "errors": errors[:20], "logs": [l for l in eng.logs if "完成" in l][:5],
           "via": a.via}
    print(json.dumps(out, ensure_ascii=False))
    return 0 if ok[0] == len(urls) else 1


if __name__ == "__main__":
    sys.exit(main())
