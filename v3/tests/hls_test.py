# -*- coding: utf-8 -*-
"""A17 第一段：HLS / DASH 分段下载合并（core/hls.py）。本地假服务器，不联网。

覆盖：明文 HLS 合并 sha 一致、主列表挑最高码率 / 限高、AES-128（显式 IV 与按序号 IV）、带 Cookie/Referer（缺头 403 报错）、
fMP4 #EXT-X-MAP、#EXT-X-BYTERANGE、分片 500 自动重试、取消 + 断点续上（只补缺的分片）、直播 / SAMPLE-AES / DRM 回落信号、
限速回调、DASH（SegmentTimeline / Number 模板 / SegmentList / 音视频合并）、取消杀 ffmpeg 子进程、真 ffmpeg 造的 HLS 能播。
用法：source env/setup.sh && source env/v3env.sh && python3 tests/hls_test.py     退出码 0 = 全过
"""
import hashlib
import http.server
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time

import requests

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from core import hls  # noqa: E402

RESULTS = []
COOKIE = "sess=abc123"
REFERER = "http://video.example/page"


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  ← %s" % str(detail)[:300]), flush=True)
    return ok


ROUTES = {}        # path -> bytes | callable(handler) -> (status, bytes)
HITS = []          # (path, headers-ok)
REJECT = []
FAIL_ONCE = set()
SLOW = {"delay": 0.0}


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    def do_GET(self):
        p = self.path.split("?")[0]
        ok = self.headers.get("Cookie") == COOKIE and self.headers.get("Referer") == REFERER
        if not ok and not p.startswith("/open/"):
            REJECT.append(p)
            self.send_response(403)
            self.end_headers()
            return
        HITS.append(p)
        if SLOW["delay"] and p.endswith(".seg"):
            time.sleep(SLOW["delay"])
        if p in FAIL_ONCE:
            FAIL_ONCE.discard(p)
            self.send_response(500)
            self.end_headers()
            return
        v = ROUTES.get(p)
        if v is None:
            self.send_response(404)
            self.end_headers()
            return
        body = v(self) if callable(v) else v
        st = 200
        rg = self.headers.get("Range")
        if rg and rg.startswith("bytes="):
            a, _, b = rg[6:].partition("-")
            body = body[int(a):int(b) + 1]
            st = 206
        self.send_response(st)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve():
    s = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s, "http://127.0.0.1:%d" % s.server_address[1]


def sha(b):
    return hashlib.sha256(b).hexdigest()


def sess():
    return requests.Session()


HDR = {"Cookie": COOKIE, "Referer": REFERER}


def seg_bytes(tag, i, n=3000):
    return hashlib.sha256(("%s-%d" % (tag, i)).encode()).digest() * (n // 32)


def hls_text(uris, extra="", ended=True, seq=0, durs=None):
    out = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:4", "#EXT-X-MEDIA-SEQUENCE:%d" % seq]
    if extra:
        out.append(extra)
    for i, u in enumerate(uris):
        out.append("#EXTINF:%s," % ((durs or [4.0] * len(uris))[i]))
        out.append(u)
    if ended:
        out.append("#EXT-X-ENDLIST")
    return "\n".join(out).encode()


def openssl_enc(key, iv, data):
    return subprocess.run(["openssl", "enc", "-aes-128-cbc", "-K", key.hex(), "-iv", iv.hex()], input=data,
                          capture_output=True, check=True).stdout


def main():
    srv, base = serve()
    tmp = tempfile.mkdtemp(prefix="hlstest_")
    S = sess()

    def out(n):
        return os.path.join(tmp, n)

    # ---- 1 明文 HLS，相对地址，合并 sha 一致
    segs = [seg_bytes("p", i) for i in range(12)]
    for i, b in enumerate(segs):
        ROUTES["/vod/a/seg%d.seg" % i] = b
    ROUTES["/vod/a/index.m3u8"] = hls_text(["seg%d.seg" % i for i in range(12)])
    r = hls.download(base + "/vod/a/index.m3u8", out("plain.ts"), S, headers=HDR, conns=6, log=lambda *a: None)
    got = open(r["path"], "rb").read()
    check("1 明文 HLS 12 片合并 sha 一致", sha(got) == sha(b"".join(segs)) and r["segments"] == 12 and r["path"].endswith(".ts"),
          (r, len(got)))
    check("1b 合并后不留 .parts 临时目录", not os.path.exists(r["path"] + ".parts"))

    # ---- 2 主列表：最高码率 / 限高
    for h, bw in ((360, 800000), (480, 1400000), (1080, 5000000)):
        for i in range(3):
            ROUTES["/vod/m/%d/s%d.seg" % (h, i)] = seg_bytes("m%d" % h, i)
        ROUTES["/vod/m/%d/i.m3u8" % h] = hls_text(["s%d.seg" % i for i in range(3)])
    ROUTES["/vod/m/master.m3u8"] = (
        "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\n360/i.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080\n1080/i.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=1400000,RESOLUTION=854x480\n480/i.m3u8\n").encode()
    HITS.clear()
    r = hls.download(base + "/vod/m/master.m3u8", out("best.ts"), S, headers=HDR, log=lambda *a: None)
    check("2 主列表默认挑 1080p，不碰其他码率",
          sha(open(r["path"], "rb").read()) == sha(b"".join(seg_bytes("m1080", i) for i in range(3)))
          and not any("/360/" in h or "/480/" in h for h in HITS), (r, HITS))
    r = hls.download(base + "/vod/m/master.m3u8", out("h480.ts"), S, headers=HDR, max_height=720, log=lambda *a: None)
    check("2b 限高 720 → 选 480p",
          sha(open(r["path"], "rb").read()) == sha(b"".join(seg_bytes("m480", i) for i in range(3))), r["variant"])
    r = hls.download(base + "/vod/m/master.m3u8", out("w.ts"), S, headers=HDR, prefer="worst", log=lambda *a: None)
    check("2c prefer=worst → 360p", "640x360" in r["variant"], r["variant"])

    # ---- 3 AES-128：显式 IV + 按序号 IV（MEDIA-SEQUENCE=5）
    key = os.urandom(16)
    iv = os.urandom(16)
    plain = [seg_bytes("e", i, 4000) + b"tail%d" % i for i in range(6)]
    ROUTES["/vod/enc/key.bin"] = key
    for i, p in enumerate(plain):
        ROUTES["/vod/enc/s%d.seg" % i] = openssl_enc(key, iv, p)
    ROUTES["/vod/enc/index.m3u8"] = hls_text(["s%d.seg" % i for i in range(6)],
                                              '#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x%s' % iv.hex())
    r = hls.download(base + "/vod/enc/index.m3u8", out("enc.ts"), S, headers=HDR, log=lambda *a: None)
    check("3 AES-128 显式 IV 解密后 sha 一致（填充已去掉）", sha(open(r["path"], "rb").read()) == sha(b"".join(plain)), r)
    plain2 = [seg_bytes("q", i, 2000) for i in range(4)]
    for i, p in enumerate(plain2):
        sq = (5 + i).to_bytes(16, "big")
        ROUTES["/vod/enc2/s%d.seg" % i] = openssl_enc(key, sq, p)
    ROUTES["/vod/enc2/key.bin"] = key
    ROUTES["/vod/enc2/index.m3u8"] = hls_text(["s%d.seg" % i for i in range(4)], '#EXT-X-KEY:METHOD=AES-128,URI="key.bin"', seq=5)
    r = hls.download(base + "/vod/enc2/index.m3u8", out("enc2.ts"), S, headers=HDR, log=lambda *a: None)
    check("3b AES-128 无 IV → 按分片序号（起点 5）", sha(open(r["path"], "rb").read()) == sha(b"".join(plain2)), r)
    check("3c 内置 AES 与 openssl 逐字节一致（含大分片走 openssl 路径之外的纯 Python）",
          hls._aes_cbc_py(key, iv, openssl_enc(key, iv, b"z" * 5000))[:5000] == b"z" * 5000)

    # ---- 4 缺头 403
    REJECT.clear()
    try:
        hls.download(base + "/vod/a/index.m3u8", out("nohdr.ts"), S, headers={}, log=lambda *a: None)
        check("4 不带 Cookie/Referer → 报 HTTP 403", False, "没报错")
    except Exception as e:
        check("4 不带 Cookie/Referer → 报 HTTP 403", "403" in str(e) and REJECT, str(e))
    HITS.clear()
    REJECT.clear()
    hls.download(base + "/vod/enc/index.m3u8", out("hdr2.ts"), S, headers=HDR, log=lambda *a: None)
    check("4b 带头：列表、分片、密钥请求全部带 Cookie+Referer（0 次被拒）", not REJECT and any("key.bin" in h for h in HITS), REJECT)

    # ---- 5 fMP4 MAP
    init = b"\x00\x00\x00\x18ftypiso5" + os.urandom(40)
    ROUTES["/vod/f/init.mp4"] = init
    fs = [seg_bytes("f", i) for i in range(4)]
    for i, b in enumerate(fs):
        ROUTES["/vod/f/s%d.seg" % i] = b
    ROUTES["/vod/f/index.m3u8"] = hls_text(["s%d.seg" % i for i in range(4)], '#EXT-X-MAP:URI="init.mp4"')
    r = hls.download(base + "/vod/f/index.m3u8", out("fmp4.ts"), S, headers=HDR, log=lambda *a: None)
    check("5 fMP4：初始化段在最前，输出改 .mp4", r["path"].endswith(".mp4")
          and open(r["path"], "rb").read() == init + b"".join(fs), r["path"])

    # ---- 6 BYTERANGE
    big = os.urandom(9000)
    ROUTES["/vod/br/all.bin"] = big
    ROUTES["/vod/br/index.m3u8"] = (
        "#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4,\n#EXT-X-BYTERANGE:3000@0\nall.bin\n"
        "#EXTINF:4,\n#EXT-X-BYTERANGE:3000\nall.bin\n#EXTINF:4,\n#EXT-X-BYTERANGE:3000\nall.bin\n#EXT-X-ENDLIST\n").encode()
    r = hls.download(base + "/vod/br/index.m3u8", out("br.ts"), S, headers=HDR, log=lambda *a: None)
    check("6 BYTERANGE 三段拼回原文件", open(r["path"], "rb").read() == big, r)

    # ---- 7 分片 500 → 自动重试
    FAIL_ONCE.add("/vod/a/seg3.seg")
    FAIL_ONCE.add("/vod/a/seg7.seg")
    r = hls.download(base + "/vod/a/index.m3u8", out("retry.ts"), S, headers=HDR, log=lambda *a: None)
    check("7 两个分片首次 500，重试后合并 sha 仍一致", sha(open(r["path"], "rb").read()) == sha(b"".join(segs)))

    # ---- 8 取消 + 续上
    SLOW["delay"] = 0.25
    cnt = {"n": 0}
    stop = {"v": False}

    def prog(d):
        cnt["n"] = d["downloaded_bytes"]
        if d["downloaded_bytes"] >= 3 * 3000:
            stop["v"] = True
    HITS.clear()
    th_before = threading.active_count()
    try:
        hls.download(base + "/vod/a/index.m3u8", out("cancel.ts"), S, headers=HDR, conns=2, cancel=lambda: stop["v"],
                     prog=prog, log=lambda *a: None)
        check("8 取消 → 抛 Cancelled", False)
    except hls.Cancelled:
        check("8 取消 → 抛 Cancelled", True)
    time.sleep(0.6)
    check("8b 取消后工作线程全部退出", threading.active_count() <= th_before + 1, threading.active_count() - th_before)
    pd = out("cancel.ts") + ".parts"
    have = len([f for f in os.listdir(pd) if f.endswith(".seg")]) if os.path.isdir(pd) else -1
    check("8c 取消后保留已下分片（0<已下<12）", 0 < have < 12, have)
    HITS.clear()
    SLOW["delay"] = 0
    r = hls.download(base + "/vod/a/index.m3u8", out("cancel.ts"), S, headers=HDR, log=lambda *a: None)
    segreq = [h for h in HITS if h.endswith(".seg")]
    check("8d 续上：只补请求缺的分片，合并 sha 一致", sha(open(r["path"], "rb").read()) == sha(b"".join(segs))
          and len(segreq) == 12 - have, (len(segreq), have))

    # ---- 9 回落信号
    ROUTES["/vod/live.m3u8"] = hls_text(["/vod/a/seg0.seg", "/vod/a/seg1.seg"], ended=False)
    ROUTES["/vod/sample.m3u8"] = hls_text(["/vod/a/seg0.seg"], '#EXT-X-KEY:METHOD=SAMPLE-AES,URI="k"')
    ROUTES["/vod/drm.m3u8"] = hls_text(["/vod/a/seg0.seg"], '#EXT-X-KEY:METHOD=AES-128,URI="skd://x",KEYFORMAT="com.apple.streamingkeydelivery"')
    for n, label in (("live", "直播"), ("sample", "SAMPLE-AES"), ("drm", "FairPlay DRM")):
        try:
            hls.download(base + "/vod/%s.m3u8" % n, out(n + ".ts"), S, headers=HDR, log=lambda *a: None)
            check("9 %s → Unsupported（调用方回落 yt-dlp/直播录制）" % label, False, "没抛")
        except hls.Unsupported:
            check("9 %s → Unsupported（调用方回落 yt-dlp/直播录制）" % label, True)

    # ---- 10 限速回调
    tot = {"n": 0}
    hls.download(base + "/vod/a/index.m3u8", out("thr.ts"), S, headers=HDR, throttle=lambda n: tot.__setitem__("n", tot["n"] + n),
                 log=lambda *a: None)
    check("10 throttle 回调累计 = 全部分片字节", tot["n"] == sum(len(x) for x in segs), tot)

    # ---- 11 DASH
    vsegs = [seg_bytes("dv", i) for i in range(5)]
    asegs = [seg_bytes("da", i) for i in range(4)]
    vinit, ainit = b"VINIT" * 10, b"AINIT" * 10
    ROUTES["/dash/v/init.mp4"] = vinit
    ROUTES["/dash/a/init.mp4"] = ainit
    t = 0
    times = []
    for d in (4000, 4000, 4000, 2000, 2000):       # 前三段 4s（r=2），后两段 2s
        times.append(t)
        t += d
    for i, tm in enumerate(times):
        ROUTES["/dash/v/%d.m4s" % tm] = vsegs[i]
    for i in range(4):
        ROUTES["/dash/a/%05d.m4s" % (i + 1)] = asegs[i]
    ROUTES["/dash/lo/init.mp4"] = b"LOWINIT"
    ROUTES["/dash/lo/0.m4s"] = b"low"
    mpd = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT16S">
 <BaseURL>/dash/</BaseURL>
 <Period>
  <AdaptationSet contentType="video" mimeType="video/mp4">
   <Representation id="lo" bandwidth="300000" width="320" height="180">
    <SegmentTemplate timescale="1000" initialization="lo/init.mp4" media="lo/$Time$.m4s"><SegmentTimeline><S t="0" d="4000"/></SegmentTimeline></SegmentTemplate>
   </Representation>
   <Representation id="v" bandwidth="2000000" width="1280" height="720">
    <SegmentTemplate timescale="1000" initialization="v/init.mp4" media="v/$Time$.m4s">
     <SegmentTimeline><S t="0" d="4000" r="2"/><S d="2000" r="1"/></SegmentTimeline></SegmentTemplate>
   </Representation>
  </AdaptationSet>
  <AdaptationSet contentType="audio" mimeType="audio/mp4">
   <Representation id="a" bandwidth="128000">
    <SegmentTemplate timescale="1" duration="4" startNumber="1" initialization="a/init.mp4" media="a/$Number%05d$.m4s"/>
   </Representation>
  </AdaptationSet>
 </Period>
</MPD>""".encode()
    ROUTES["/mpd/main.mpd"] = mpd
    # 假 ffmpeg：把两个输入原样拼接成输出（验证调用参数与合并顺序，不依赖真编码）
    fake = os.path.join(tmp, "bin")
    os.makedirs(fake)
    fp = os.path.join(fake, "ffmpeg")
    open(fp, "w").write('#!/bin/sh\nin1=$5; in2=$7; out=${10}\ncat "$in1" "$in2" > "$out"\n')
    os.chmod(fp, 0o755)
    oldpath = os.environ["PATH"]
    os.environ["PATH"] = fake + ":" + oldpath
    r = hls.download(base + "/mpd/main.mpd", out("dash.mp4"), S, headers=HDR, log=lambda *a: None)
    exp = vinit + b"".join(vsegs) + ainit + b"".join(asegs)
    got = open(r["path"], "rb").read()
    check("11 DASH：挑最高码率视频 + 音频，SegmentTimeline($Time$,r 展开) + $Number%05d$，合并后字节一致",
          got == exp and r["kind"] == "dash" and r["segments"] == 9 and r["muxed"], (r, len(got), len(exp)))
    check("11b DASH 合并后中间轨道文件已清", not [f for f in os.listdir(tmp) if f.startswith("dash.") and f != "dash.mp4"],
          os.listdir(tmp))
    os.environ["PATH"] = oldpath
    # 没有 ffmpeg：两条轨道都保留且如实返回
    os.environ["PATH"] = "/nonexistent"
    try:
        r = hls.download(base + "/mpd/main.mpd", out("dash2.mp4"), S, headers=HDR, log=lambda *a: None)
    finally:
        os.environ["PATH"] = oldpath
    check("11c 无 ffmpeg：视频轨/音轨分文件保留，muxed=False",
          not r["muxed"] and r["audio_path"] and os.path.isfile(r["path"]) and os.path.isfile(r["audio_path"])
          and open(r["audio_path"], "rb").read() == ainit + b"".join(asegs), r)

    # SegmentList + 单 Representation 无音频
    ROUTES["/dash2/i.mp4"] = b"INI"
    for i in range(3):
        ROUTES["/dash2/c%d.m4s" % i] = seg_bytes("sl", i)
    ROUTES["/mpd/list.mpd"] = b"""<?xml version="1.0"?><MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT9S">
<Period><AdaptationSet mimeType="video/mp4"><Representation id="x" bandwidth="1000" height="360"><BaseURL>/dash2/</BaseURL>
<SegmentList duration="3"><Initialization sourceURL="i.mp4"/><SegmentURL media="c0.m4s"/><SegmentURL media="c1.m4s"/><SegmentURL media="c2.m4s"/></SegmentList>
</Representation></AdaptationSet></Period></MPD>"""
    r = hls.download(base + "/mpd/list.mpd", out("dl.mp4"), S, headers=HDR, log=lambda *a: None)
    check("11d DASH SegmentList + BaseURL", open(r["path"], "rb").read() == b"INI" + b"".join(seg_bytes("sl", i) for i in range(3)), r)
    ROUTES["/mpd/dyn.mpd"] = b'<?xml version="1.0"?><MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="dynamic"><Period/></MPD>'
    ROUTES["/mpd/drm.mpd"] = (b'<?xml version="1.0"?><MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT4S"><Period>'
                              b'<AdaptationSet mimeType="video/mp4"><ContentProtection schemeIdUri="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"/>'
                              b'<Representation id="x" bandwidth="1"><SegmentTemplate duration="4" media="$Number$.m4s"/></Representation></AdaptationSet></Period></MPD>')
    for n, label in (("dyn", "直播 MPD"), ("drm", "带 ContentProtection 的 MPD")):
        try:
            hls.download(base + "/mpd/%s.mpd" % n, out(n + ".mp4"), S, headers=HDR, log=lambda *a: None)
            check("11e %s → Unsupported" % label, False, "没抛")
        except hls.Unsupported:
            check("11e %s → Unsupported" % label, True)

    # ---- 12 取消杀 ffmpeg 子进程（假 ffmpeg 睡 60 秒）
    pidf = os.path.join(tmp, "ff.pid")
    open(fp, "w").write('#!/bin/sh\necho $$ > %s\nexec sleep 60\n' % pidf)
    os.chmod(fp, 0o755)
    os.environ["PATH"] = fake + ":" + oldpath
    flag = {"v": False}

    def killer():
        for _ in range(100):
            if os.path.exists(pidf):
                time.sleep(0.3)
                flag["v"] = True
                return
            time.sleep(0.1)
    threading.Thread(target=killer, daemon=True).start()
    try:
        hls.download(base + "/mpd/main.mpd", out("kill.mp4"), S, headers=HDR, cancel=lambda: flag["v"], log=lambda *a: None)
        check("12 合并阶段取消 → 抛 Cancelled", False)
    except hls.Cancelled:
        check("12 合并阶段取消 → 抛 Cancelled", True)
    finally:
        os.environ["PATH"] = oldpath
    time.sleep(0.3)
    pid = int(open(pidf).read().strip()) if os.path.exists(pidf) else 0
    alive = False
    if pid:
        try:
            os.kill(pid, 0)
            alive = open("/proc/%d/stat" % pid).read().split()[2] != "Z"
        except (OSError, FileNotFoundError):
            alive = False
    check("12b 取消后 ffmpeg 子进程已被杀（无孤儿）", pid and not alive, (pid, alive))

    # ---- 13 真 ffmpeg 造 HLS：下载合并后能被 ffprobe 读出时长
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        d = os.path.join(tmp, "real")
        os.makedirs(d)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10",
                        "-f", "lavfi", "-i", "sine=frequency=440", "-t", "6", "-c:v", "libx264", "-c:a", "aac",
                        "-hls_time", "2", "-hls_playlist_type", "vod", "-hls_segment_filename", os.path.join(d, "r%d.ts"),
                        os.path.join(d, "real.m3u8")], check=True)
        for f in os.listdir(d):
            ROUTES["/real/" + f] = open(os.path.join(d, f), "rb").read()
        r = hls.download(base + "/real/real.m3u8", out("real.ts"), S, headers=HDR, log=lambda *a: None)
        pr = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", r["path"]],
                            capture_output=True, text=True)
        try:
            dur = float(pr.stdout.strip())
        except ValueError:
            dur = 0
        check("13 真 ffmpeg 造的 3 片 HLS：合并文件 ffprobe 时长≈6s", 5.0 <= dur <= 7.5, (pr.stdout, pr.stderr[:100]))
    else:
        print("SKIP 13 无 ffmpeg/ffprobe")

    shutil.rmtree(tmp, ignore_errors=True)
    bad = [n for n, ok in RESULTS if not ok]
    print("\nhls_test: %d/%d 通过" % (len(RESULTS) - len(bad), len(RESULTS)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
