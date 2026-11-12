# -*- coding: utf-8 -*-
"""A17 内核端到端：经 /jsonrpc 真实添加 m3u8 / mpd 任务（本地假 HLS/DASH 服务器，要求 Cookie+Referer）。

验证：带头推送 → 内置分段下载合并（sha 一致）、AES-128、缺头报 403 不卡死、暂停→继续只补缺片、
限速生效、删除任务清 .parts、直播列表回落原路径（日志有回落记录）、hls_native=关 时不走内置、DASH(.mpd) 成品为 mp4。
用法：source env/setup.sh && source env/v3env.sh && python3 tests/hls_e2e.py     退出码 0 = 全过
"""
import hashlib
import http.server
import os
import shutil
import subprocess
import sys
import threading
import time

os.environ.setdefault("SDL_E2E_DATA", "/tmp/sdlv3_hls")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core_e2e as E   # noqa: E402
from core_e2e import check, wait  # noqa: E402

COOKIE = "sess=e2e-hls"
REFERER = "http://video.example/e2e"
ROUTES = {}
HITS = []
SLOW = {"delay": 0.0}


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    def do_GET(self):
        p = self.path.split("?")[0]
        if self.headers.get("Cookie") != COOKIE or self.headers.get("Referer") != REFERER:
            HITS.append(("403", p))
            self.send_response(403)
            self.end_headers()
            return
        HITS.append(("200", p))
        if SLOW["delay"] and p.endswith(".seg"):
            time.sleep(SLOW["delay"])
        body = ROUTES.get(p)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass


def seg(tag, i, n):
    return (hashlib.sha256(("%s%d" % (tag, i)).encode()).digest() * (n // 32 + 1))[:n]


def m3u8(uris, extra="", ended=True):
    t = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:4", "#EXT-X-MEDIA-SEQUENCE:0"]
    if extra:
        t.append(extra)
    for u in uris:
        t += ["#EXTINF:4.0,", u]
    if ended:
        t.append("#EXT-X-ENDLIST")
    return "\n".join(t).encode()


def main():
    shutil.rmtree(E.DATA, ignore_errors=True)
    os.makedirs(E.OUT, exist_ok=True)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    E.FPORT[0] = base.rsplit(":", 1)[1]
    E.PORT[0] = E.free_port()
    env = dict(os.environ, SNIFFDL_DRYRUN_OS="1", NO_PROXY="127.0.0.1,localhost,::1")
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "DISPLAY"):
        env.pop(k, None)
    # SDL_CORE_EXE=冻结产物路径时，改对 PyInstaller 产物跑同一套用例（不借沙箱 PYTHONPATH）
    _exe = os.environ.get("SDL_CORE_EXE")
    if _exe:
        env.pop("PYTHONPATH", None)
    core = subprocess.Popen(([_exe] if _exe else [sys.executable, os.path.join(E.HERE, "tests", "ui_smoke_core.py")]) + ["--data-dir", E.DATA,
                             "--port", str(E.PORT[0]), "--runtime", os.path.join(E.DATA, "runtime.json"),
                             "--ui-token", E.TOKEN],
                            cwd=E.HERE, env=env, stdout=open(os.path.join(E.DATA, "core.out"), "w"),
                            stderr=subprocess.STDOUT)
    HDR = {"Cookie": COOKIE, "Referer": REFERER}

    def addm(name, path, headers=HDR, **kw):
        info = {"url": base + path + "?x=" + name, "name": name, "dir": E.OUT, "headers": headers}
        info.update(kw)
        r = E.rpc("sniffdl.task.add", info)
        return r.get("gid") if isinstance(r, dict) else r

    try:
        if not wait(lambda: E.raw_rpc("sniffdl.ping", [], token=None).get("result"), 30):
            check("内核启动", False, "见 %s/core.out" % E.DATA)
            return 1
        E.setv(outdir=E.OUT, clip_watch=False, notify=False, conflict="重命名", retry_count=0, max_concurrent=4,
               speed_limit_kbps=0, hls_native=True)

        # 1 明文 HLS，带头
        segs = [seg("a", i, 40000) for i in range(10)]
        for i, b in enumerate(segs):
            ROUTES["/v/a/s%d.seg" % i] = b
        ROUTES["/v/a/index.m3u8"] = m3u8(["s%d.seg" % i for i in range(10)])
        g = addm("movie1.m3u8", "/v/a/index.m3u8")
        v = E.wait_status(g, ("complete", "error"), 40)
        p = os.path.join(E.OUT, "movie1.ts")
        check("1 带 Cookie/Referer 推送的 m3u8：完成，成品 sha 与 10 片拼接一致",
              bool(v and v["status"] == "complete" and os.path.isfile(p)
                   and hashlib.sha256(open(p, "rb").read()).hexdigest() == hashlib.sha256(b"".join(segs)).hexdigest()), v)
        check("1b 任务名/路径已改成真实成品 .ts，大小 = 400000", bool(v and v["name"] == "movie1.ts"
                                                                       and os.path.getsize(p) == 400000), v)
        check("1c 全程 0 次被拒（每个请求都带了头）", not [h for h in HITS if h[0] == "403"], HITS[:3])
        check("1d 没留 .parts 临时目录", not [x for x in os.listdir(E.OUT) if x.endswith(".parts")], os.listdir(E.OUT))

        # 2 AES-128
        key = os.urandom(16)
        iv = os.urandom(16)
        plain = [seg("e", i, 20000) for i in range(6)]
        ROUTES["/v/e/key.bin"] = key
        for i, b in enumerate(plain):
            ROUTES["/v/e/s%d.seg" % i] = subprocess.run(
                ["openssl", "enc", "-aes-128-cbc", "-K", key.hex(), "-iv", iv.hex()], input=b, capture_output=True,
                check=True).stdout
        ROUTES["/v/e/index.m3u8"] = m3u8(["s%d.seg" % i for i in range(6)],
                                          '#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x%s' % iv.hex())
        g = addm("enc.m3u8", "/v/e/index.m3u8")
        v = E.wait_status(g, ("complete", "error"), 40)
        p = os.path.join(E.OUT, "enc.ts")
        check("2 AES-128 加密流：内核端解密，成品等于明文拼接",
              bool(v and v["status"] == "complete" and os.path.isfile(p) and open(p, "rb").read() == b"".join(plain)), v)

        # 3 缺头 → 报错，不卡死
        g = addm("nohdr.m3u8", "/v/a/index.m3u8", headers={})
        v = E.wait_status(g, ("complete", "error"), 20)
        check("3 不带头推送：很快报错，信息含 403", bool(v and v["status"] == "error" and "403" in str(v)), v)

        # 4 限速：10 片 400KB，限 100KB/s，至少 ~3s
        E.setv(speed_limit_kbps=0)
        t0 = time.time()
        g = addm("lim.m3u8", "/v/a/index.m3u8", limit_kbps=100)
        v = E.wait_status(g, ("complete", "error"), 60)
        el = time.time() - t0
        check("4 单任务限速 100KB/s：400KB 用时 ≥2.5s 且完成", bool(v and v["status"] == "complete" and el >= 2.5), (el, v))

        # 5 暂停 → 继续只补缺片
        SLOW["delay"] = 0.4
        g = addm("pause.m3u8", "/v/a/index.m3u8", conns="2")
        E.wait_done_bytes(g, 80000, 20)
        E.rpc("sniffdl.task.pause", [g])
        v = E.wait_status(g, ("paused",), 15)
        check("5 暂停 → 状态 paused，已下分片保留在 .parts", bool(v) and bool([x for x in os.listdir(E.OUT) if x.startswith("pause") and x.endswith(".parts")]), v)
        SLOW["delay"] = 0
        HITS.clear()
        E.rpc("sniffdl.task.resume", [g])
        v = E.wait_status(g, ("complete", "error"), 40)
        p = os.path.join(E.OUT, "pause.ts")
        done_segs = [h for h in HITS if h[1].endswith(".seg")]
        check("5b 继续 → 完成且 sha 正确，只补请求了缺的分片（<10 次）",
              bool(v and v["status"] == "complete" and os.path.isfile(p) and open(p, "rb").read() == b"".join(segs)
                   and 0 < len(done_segs) < 10), (v, len(done_segs)))

        # 6 删除任务（下载中）→ 清 .parts
        SLOW["delay"] = 0.5
        g = addm("rm.m3u8", "/v/a/index.m3u8", conns="2")
        E.wait_done_bytes(g, 40000, 20)
        E.remove(g, True)
        time.sleep(2.5)
        SLOW["delay"] = 0
        left = [x for x in os.listdir(E.OUT) if x.startswith("rm")]
        check("6 下载中删除任务（连文件）：.parts 与成品都不留", not left, left)

        # 7 直播列表 → 回落原路径（日志记录）
        ROUTES["/v/live.m3u8"] = m3u8(["a/s0.seg", "a/s1.seg"], ended=False)
        mark = E.log_mark()
        g = addm("livey.m3u8", "/v/live.m3u8")
        okl = wait(lambda: any("回落原路径" in x for x in E.logs_since(mark)), 20)
        check("7 没有 ENDLIST 的直播列表：内置下载让出，日志有「回落原路径」", bool(okl), E.logs_since(mark)[-5:])
        try:
            E.remove(g, True)
        except Exception:
            pass

        # 8 hls_native 关闭 → 不走内置
        E.setv(hls_native=False)
        mark = E.log_mark()
        HITS.clear()
        g = addm("off.m3u8", "/v/a/index.m3u8")
        time.sleep(4)
        used_native = any("分段下载" in x for x in E.logs_since(mark)) or \
            (E.view(g) or {}).get("msg", "").startswith("已合并")
        check("8b hls_native=关：任务没有被内置模块合并（statusMessage 不是「已合并 N 个分片」）", not used_native, E.view(g))
        try:
            E.remove(g, True)
        except Exception:
            pass
        E.setv(hls_native=True)

        # 9 DASH 单视频轨 → mp4
        vi = b"DASHINIT"
        vs = [seg("d", i, 30000) for i in range(4)]
        ROUTES["/v/d/init.mp4"] = vi
        for i, b in enumerate(vs):
            ROUTES["/v/d/%d.m4s" % (i + 1)] = b
        ROUTES["/v/d/m.mpd"] = b"""<?xml version="1.0"?><MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT16S">
<Period><AdaptationSet contentType="video" mimeType="video/mp4"><Representation id="1" bandwidth="900000" width="640" height="360">
<SegmentTemplate timescale="1" duration="4" startNumber="1" initialization="init.mp4" media="$Number$.m4s"/></Representation></AdaptationSet></Period></MPD>"""
        g = addm("dash1.mpd", "/v/d/m.mpd")
        v = E.wait_status(g, ("complete", "error"), 40)
        p = os.path.join(E.OUT, "dash1.mp4")
        check("9 DASH(.mpd)：成品 dash1.mp4 = 初始化段 + 4 片",
              bool(v and v["status"] == "complete" and os.path.isfile(p) and open(p, "rb").read() == vi + b"".join(vs)), v)

        # 10 主列表多码率：默认最高；任务 format=480 → 不超过 480p
        for h, bw in ((360, 800000), (480, 1400000), (1080, 5000000)):
            for i in range(3):
                ROUTES["/v/m/%d/s%d.seg" % (h, i)] = seg("m%d" % h, i, 10000)
            ROUTES["/v/m/%d/i.m3u8" % h] = m3u8(["s%d.seg" % i for i in range(3)])
        ROUTES["/v/m/master.m3u8"] = (
            "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\n360/i.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080\n1080/i.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=1400000,RESOLUTION=854x480\n480/i.m3u8\n").encode()
        g = addm("best.m3u8", "/v/m/master.m3u8")
        v = E.wait_status(g, ("complete", "error"), 30)
        p = os.path.join(E.OUT, "best.ts")
        check("10 主列表默认选 1080p 码率", bool(v and v["status"] == "complete" and os.path.isfile(p)
              and open(p, "rb").read() == b"".join(seg("m1080", i, 10000) for i in range(3))), v)
        g = addm("cap.m3u8", "/v/m/master.m3u8", format="480")
        v = E.wait_status(g, ("complete", "error"), 30)
        p = os.path.join(E.OUT, "cap.ts")
        check("10b 任务 format=480 → 选 480p 码率", bool(v and v["status"] == "complete" and os.path.isfile(p)
              and open(p, "rb").read() == b"".join(seg("m480", i, 10000) for i in range(3))), v)
    finally:
        try:
            E.raw_rpc("sniffdl.core.shutdown", [], timeout=5)
        except Exception:
            pass
        try:
            core.wait(8)
        except Exception:
            core.kill()
        srv.shutdown()
    npass = sum(1 for _, ok in E.RESULTS if ok)
    print("\n结果：%d/%d 通过" % (npass, len(E.RESULTS)))
    return 0 if npass == len(E.RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
