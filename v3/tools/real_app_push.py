# -*- coding: utf-8 -*-
"""对【正在运行的真 SniffDL】（python video_downloader.py / SniffDL.exe）做外部推送端到端验收。

前提：SniffDL 已启动且 RPC 开着（config.json rpc_enable=true）。本脚本：
  1. 起本地 mock 网盘 CDN（缺 Cookie→412 / UA 不对→403 / 过期→403，见 mock_netdisk_site.py）；
  2. 按 LinkSwift sendTo.aria2() 一字不差组包 → aria2.addUri 推给 SniffDL；
  3. aria2.tellStatus 盯到完成 → 在 SniffDL 下载目录找到文件，核对大小 + sha256 + 服务端看到的头 + 并发连接数；
  4. 模拟扩展接管：sniffdl.intercept（source=browser 静默接管）同样核对；
  5. 失败路径：缺 Cookie 必须 error 且原因可见；
  6. 可选 --public URL：真实公网大文件测速。

    python tools/real_app_push.py --rpc http://127.0.0.1:6800/jsonrpc --outdir <SniffDL 下载目录> [--token xxx]
                                   [--public https://…/100MB.bin]
"""
import argparse
import hashlib
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mock_netdisk_site as M  # noqa: E402
from netdisk_e2e import LINKSWIFT_QUARK_UA, linkswift_addUri, mock_url  # noqa: E402

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def rpc(url, method, params, token=""):
    if token:
        params = ["token:" + token] + list(params)
    body = json.dumps({"id": int(time.time() * 1000), "jsonrpc": "2.0", "method": method,
                       "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json;charset=UTF-8",
                                                           "Accept": "application/json"})
    with OPENER.open(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def wait(url, gid, token, timeout):
    t0 = time.time()
    st = {}
    while time.time() - t0 < timeout:
        r = rpc(url, "aria2.tellStatus", [gid], token)
        st = r.get("result") or {"status": "?", "err": r.get("error")}
        if st.get("status") in ("complete", "error", "removed"):
            break
        time.sleep(0.5)
    return st, time.time() - t0


def find_file(outdir, name, newer_than):
    for d, _, fs in os.walk(outdir):
        for f in fs:
            p = os.path.join(d, f)
            if f == name and os.path.getmtime(p) >= newer_than - 2:
                return p
    return ""


def stats(mport):
    return json.loads(OPENER.open("http://127.0.0.1:%d/__stats" % mport).read())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", default="http://127.0.0.1:6800/jsonrpc")
    ap.add_argument("--token", default="")
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--mport", type=int, default=18797)
    ap.add_argument("--mb", type=int, default=48)
    ap.add_argument("--public", default="")
    ap.add_argument("--public-sha256", default="", help="可选：公网文件官方 sha256（如 Alpine latest-releases.yaml 给的），下完逐字节核对")
    a = ap.parse_args()
    res = []

    def ok(n, c, d=""):
        res.append((n, bool(c)))
        print("%s %-52s %s" % ("PASS" if c else "FAIL", n, d), flush=True)

    M.serve(a.mport, a.mb * 1024 * 1024, "quark-cloud-drive", "__pus=OK", conn_kbps=1024)
    want = M.expected_sha256()
    r = rpc(a.rpc, "aria2.getVersion", ["token:" + a.token])
    ok("握手 aria2.getVersion（LinkSwift 测试连接）", "result" in r, json.dumps(r)[:80])

    # ---- 1) LinkSwift 推送 ----
    fn = "real_push_%d.zip" % int(time.time())
    t0 = time.time()
    hdr = {"User-Agent": LINKSWIFT_QUARK_UA, "Cookie": "__pus=OK; __puus=zz"}
    r = rpc(a.rpc, "aria2.addUri", linkswift_addUri(mock_url(a.mport, fn=fn, tag="real1"), fn, hdr, token=a.token))
    gid = r.get("result")
    ok("aria2.addUri 返回 gid", bool(gid), str(r)[:80])
    st, dt = wait(a.rpc, gid, a.token, 180)
    p = find_file(a.outdir, fn, t0)
    size = os.path.getsize(p) if p else 0
    sha = hashlib.sha256(open(p, "rb").read()).hexdigest() if p else ""
    s = stats(a.mport)
    reqs = [q for q in s["reqs"] if q["path"].startswith("/real1")]
    ok("任务 complete", st.get("status") == "complete", "status=%s err=%s 用时 %.1fs" % (
        st.get("status"), st.get("errorMessage"), dt))
    ok("文件落盘 大小=%dMB" % a.mb, size == a.mb * 1024 * 1024, "%s %d B" % (p, size))
    ok("sha256 一致", sha == want, sha[:16])
    ok("每个请求 UA/Cookie 与推送方一字不差",
       reqs and all(q["ua"] == LINKSWIFT_QUARK_UA and q["cookie"] == hdr["Cookie"] for q in reqs),
       "%d 个请求" % len(reqs))
    ok("多连接（mock 单连接限速 1MB/s）", s["max_conc"] > 1,
       "服务端最大并发=%d 平均 %.2f MB/s" % (s["max_conc"], a.mb / max(dt, 0.01)))

    # ---- 2) 扩展接管：sniffdl.intercept（浏览器下载被接管，静默入队）----
    OPENER.open("http://127.0.0.1:%d/__reset" % a.mport).read()
    fn2 = "real_intercept_%d.zip" % int(time.time())
    t0 = time.time()
    info = {"url": mock_url(a.mport, fn=fn2, tag="real2"), "filename": fn2,
            "referer": "https://pan.quark.cn/", "cookie": "__pus=OK", "ua": LINKSWIFT_QUARK_UA,
            "headers": {"User-Agent": LINKSWIFT_QUARK_UA, "Cookie": "__pus=OK; captured=1",
                        "Referer": "https://pan.quark.cn/"},
            "size": a.mb * 1024 * 1024, "source": "browser", "confirm": True}
    r = rpc(a.rpc, "sniffdl.intercept", [info], a.token)
    gid2 = r.get("result")
    ok("sniffdl.intercept(source=browser) 入队", bool(gid2), str(r)[:100])
    if gid2:
        st, dt = wait(a.rpc, gid2, a.token, 180)
        p = find_file(a.outdir, fn2, t0)
        s = stats(a.mport)
        reqs = [q for q in s["reqs"] if q["path"].startswith("/real2")]
        ok("扩展接管任务 complete + 落盘完整", st.get("status") == "complete" and p and
           os.path.getsize(p) == a.mb * 1024 * 1024, "status=%s %s 用时 %.1fs" % (st.get("status"), p, dt))
        ok("扩展捕获的真实头原样使用（Cookie 含 captured=1）",
           reqs and all(q["cookie"] == "__pus=OK; captured=1" for q in reqs), "%d 请求" % len(reqs))

    # ---- 2b) 网页卡「开始下载」：sniffdl.add ----
    fn3 = "real_add_%d.zip" % int(time.time())
    t0 = time.time()
    r = rpc(a.rpc, "sniffdl.add", [{"url": mock_url(a.mport, fn=fn3, tag="real3"), "filename": fn3,
                                     "referer": "https://pan.quark.cn/", "cookie": "__pus=OK",
                                     "ua": LINKSWIFT_QUARK_UA, "category": "", "use_cat_dir": False}], a.token)
    gid3 = r.get("result")
    st, dt = wait(a.rpc, gid3, a.token, 180) if gid3 else ({}, 0)
    p = find_file(a.outdir, fn3, t0)
    ok("网页卡 sniffdl.add → complete + 落盘", st.get("status") == "complete" and p and
       os.path.getsize(p) == a.mb * 1024 * 1024, "status=%s 用时 %.1fs" % (st.get("status"), dt))

    # ---- 3) 失败可见：缺 Cookie ----
    fn4 = "real_nocookie_%d.zip" % int(time.time())
    r = rpc(a.rpc, "aria2.addUri", linkswift_addUri(mock_url(a.mport, fn=fn4, tag="real4"), fn4,
                                                     {"User-Agent": LINKSWIFT_QUARK_UA}, token=a.token))
    st, dt = wait(a.rpc, r.get("result"), a.token, 60)
    ok("缺 Cookie → error 且原因可见", st.get("status") == "error" and "Cookie" in (st.get("errorMessage") or ""),
       (st.get("errorMessage") or "")[:90])

    # ---- 4) 公网大文件 ----
    if a.public:
        t0 = time.time()
        name = a.public.rsplit("/", 1)[-1].split("?")[0] or "public.bin"
        r = rpc(a.rpc, "aria2.addUri", [[a.public], {"out": name}], a.token)
        st, dt = wait(a.rpc, r.get("result"), a.token, 600)
        p = find_file(a.outdir, name, t0)
        size = os.path.getsize(p) if p else 0
        ok("公网大文件下载完成", st.get("status") == "complete" and size > 0,
           "status=%s %s %.1fMB 用时 %.1fs 平均 %.2f MB/s err=%s" % (
               st.get("status"), p, size / 1048576.0, dt, size / 1048576.0 / max(dt, 0.01),
               (st.get("errorMessage") or "")[:80]))
        try:
            total = int(st.get("totalLength") or 0)
            ok("公网文件大小与服务器 Content-Length 一致", total and total == size, "%d vs %d" % (size, total))
        except Exception:
            pass
        if a.public_sha256 and p:
            h = hashlib.sha256()
            with open(p, "rb") as f:
                for b in iter(lambda: f.read(1 << 20), b""):
                    h.update(b)
            ok("公网文件 sha256 与官方一致", h.hexdigest().lower() == a.public_sha256.lower(), h.hexdigest())
        print("公网任务 connections=%s downloadSpeed(末次)=%s" % (st.get("connections"), st.get("downloadSpeed")))

    bad = [n for n, c in res if not c]
    print("REAL APP RESULT=%s (%d/%d PASS)" % ("PASS" if not bad else "FAIL", len(res) - len(bad), len(res)))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
