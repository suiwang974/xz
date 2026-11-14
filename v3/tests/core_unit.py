# -*- coding: utf-8 -*-
"""WP1 内核接口单测：起真内核（隔离数据目录、SNIFFDL_DRYRUN_OS=1）→ HTTP /jsonrpc 带 X-SniffDL-Token 逐个真实调用。

覆盖：令牌校验、WP1a 新设置键（sidecar_* / cancel_cleanup / resume_policy / after=重启 / rt_* 密钥掩码 / bt_upload_limit_kbps）、
cookie.*、components.*、rt.test/models（本地假网关）、stats.history/reset（n/count 两种账本键）、
os.openOutdir/openLog（dry-run 断言）、sniff.slow*（开始/状态/取消）、selftest、torrent.upload（落内核数据目录）、
bt.pauseAll/resumeAll（真 BT 任务）、platform_util 「重启」命令。

用法：source env/setup.sh && source env/v3env.sh && PATH=$PATH:/workspace/work/sdl/speed/tools python3 tests/core_unit.py
退出码 0 = 全过。
"""
import base64
import hashlib
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
DATA = "/tmp/sdlv3_unit"
TOKEN = "unittoken456"
RT_KEY = "sk-unit-test-not-real"
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  ← %s" % str(detail)[:400]), flush=True)
    return ok


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def raw_rpc(port, method, params, token=TOKEN):
    h = {"Content-Type": "application/json"}
    if token:
        h["X-SniffDL-Token"] = token
    req = urllib.request.Request("http://127.0.0.1:%d/jsonrpc" % port,
                                 data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
                                 headers=h)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"error": {"code": e.code, "message": str(e)}}


def rpc(port, method, *params):
    j = raw_rpc(port, method, list(params))
    if "error" in j:
        raise RuntimeError("%s → %s" % (method, j["error"]))
    return j["result"]


def wait(fn, timeout, step=0.3):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = fn()
            if v:
                return v
        except Exception:
            pass
        time.sleep(step)
    return None


# ---------- 本地假网关（OpenAI 兼容 /models、/responses，校验 Bearer 密钥）
class FakeGateway(http.server.BaseHTTPRequestHandler):
    seen_auth = []

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _authed(self):
        a = self.headers.get("Authorization", "")
        FakeGateway.seen_auth.append(a)
        return a == "Bearer " + RT_KEY

    def do_GET(self):
        if not self._authed():
            return self._json(401, {"error": "bad key"})
        if self.path.endswith("/models"):
            return self._json(200, {"data": [{"id": "gpt-rt-a"}, {"id": "gpt-rt-b"}]})
        self._json(404, {})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if not self._authed():
            return self._json(401, {"error": "bad key"})
        if self.path.endswith("/responses"):
            return self._json(200, {"id": "r1", "model": body.get("model"), "output_text": "pong"})
        self._json(404, {})


class PageServer(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        b = b"<html><body><p>no player here</p></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


def benc(x):
    if isinstance(x, int):
        return b"i%de" % x
    if isinstance(x, str):
        x = x.encode()
    if isinstance(x, bytes):
        return b"%d:%s" % (len(x), x)
    if isinstance(x, list):
        return b"l" + b"".join(benc(i) for i in x) + b"e"
    if isinstance(x, dict):
        return b"d" + b"".join(benc(k) + benc(x[k]) for k in sorted(x)) + b"e"
    raise TypeError(x)


def make_torrent_bytes(announce):
    data = os.urandom(300 * 1024)
    plen = 64 * 1024
    pieces = b"".join(hashlib.sha1(data[i:i + plen]).digest() for i in range(0, len(data), plen))
    info = {"name": "unit_pack.bin", "piece length": plen, "pieces": pieces, "length": len(data)}
    return benc({"announce": announce, "info": info, "comment": "sniffdl core_unit"})


def serve(handler):
    port = free_port()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


# ---------- 进程内小单测
def t_platform_util():
    os.environ["SNIFFDL_DRYRUN_OS"] = "1"
    from core import platform_util as PU
    check("platform_util 有「重启」命令（Win / Linux）",
          PU.POWER_CMDS_WIN.get("重启") == ["shutdown", "/r", "/t", "0"] and PU.POWER_CMDS_LINUX.get("重启"))
    n = len(PU.ACTIONS)
    check("power('重启') dry-run 记录且不执行", PU.power("重启") and len(PU.ACTIONS) == n + 1 and "重启" in PU.ACTIONS[-1]["target"],
          PU.ACTIONS[-3:])
    from core import settings as S
    for k in ("speed_engine", "sidecar_m3u8", "sidecar_bili", "sidecar_paths", "cancel_cleanup", "resume_policy",
              "incomplete_dir", "bt_upload_limit_kbps", "rt_enable", "rt_base_url", "rt_api_key", "rt_model", "rt_instructions"):
        if not S.spec(k):
            check("设置键存在 %s" % k, False)
    check("设置表含 WP1a 全部新键", all(S.spec(k) for k in ("speed_engine", "sidecar_m3u8", "sidecar_bili", "sidecar_paths",
                                                          "cancel_cleanup", "resume_policy", "incomplete_dir",
                                                          "bt_upload_limit_kbps", "rt_api_key")))
    check("after 选项含「重启」", "重启" in S.spec("after")["options"])
    check("rt_api_key 标为 secret，public_schema 不回默认值", S.spec("rt_api_key").get("secret")
          and all(d.get("default") == "" for d in S.public_schema() if d.get("secret")))


def t_bt_exclude():
    """bt_exclude 通配排除（torrent 与磁力共用 _bt_auto_select）+ 磁力元数据后的 _bt_on_followed（假 aria2 RPC）。"""
    os.environ["SNIFFDL_DRYRUN_OS"] = "1"
    import types
    sys.path.insert(0, os.path.join(HERE, "tests"))
    import ui_smoke_core
    ui_smoke_core._stub_tk()                 # 沙箱没有 libtk：进程内导入引擎前先换成桩
    from core import engine as E
    cls = [v for v in vars(E).values() if isinstance(v, type) and hasattr(v, "_bt_on_followed")][0]
    fs = [{"index": 1, "path": "a/movie.mkv"}, {"index": 2, "path": "a/广告.url"}, {"index": 3, "path": "a/Readme.HTML"},
          {"index": 4, "path": "a/_____padding_file_1"}]

    def mk(ex, status="paused", files=None):
        calls = []
        o = types.SimpleNamespace(cfg={"bt_exclude": ex}, calls=calls, logs=[])
        o._bt_auto_select = lambda f: cls._bt_auto_select(o, f)
        o.log = lambda m: o.logs.append(m)

        def rpc_(m, a):
            calls.append((m, a))
            if m == "aria2.tellStatus":
                return {"status": status, "dir": "/d/x", "files": [{"index": str(f["index"]), "path": "/d/x/" + f["path"]}
                                                                     for f in (files or fs)]}
            return "OK"
        o._bt_rpc = rpc_
        return o
    o = mk("*.url\n*.html\n_____padding_file*")
    t = {}
    cls._bt_on_followed(o, t, "g2")
    chg = [c for c in o.calls if c[0] == "aria2.changeOption"]
    check("bt_exclude 磁力元数据后：select-file 只留正片（1）", chg and chg[0][1] == ["g2", {"select-file": "1"}], o.calls)
    check("bt_exclude 磁力：选完后 unpause 放行，且在 changeOption 之后",
          [c[0] for c in o.calls][-2:] == ["aria2.changeOption", "aria2.unpause"], o.calls)
    check("bt_exclude 磁力：任务记下 bt_select=[1] 且有日志", t.get("bt_select") == [1] and o.logs, (t, o.logs))
    o = mk("")
    cls._bt_on_followed(o, {}, "g3")
    check("bt_exclude 为空：不改 select-file，只 unpause", [c[0] for c in o.calls][1:] == ["aria2.unpause"], o.calls)
    o = mk("*")
    cls._bt_on_followed(o, {}, "g4")
    check("bt_exclude 全排除：回退全选，不改 select-file",
          not [c for c in o.calls if c[0] == "aria2.changeOption"], o.calls)
    o = mk("*.url", status="active")
    cls._bt_on_followed(o, {}, "g5")
    check("非 paused（没带 pause-metadata）也能改选，但不乱 unpause",
          any(c[0] == "aria2.changeOption" for c in o.calls) and not any(c[0] == "aria2.unpause" for c in o.calls), o.calls)
    o = mk("*.url")
    cls._bt_on_followed(o, {"bt_select": [2, 3]}, "g6")
    check("用户已手选文件（bt_select）：不覆盖，仍 unpause",
          not any(c[0] == "aria2.changeOption" for c in o.calls) and o.calls[-1][0] == "aria2.unpause", o.calls)
    o = mk("a/sub/*", files=[{"index": 1, "path": "a/m.mkv"}, {"index": 2, "path": "a/sub/x.mp4"}])
    cls._bt_on_followed(o, {}, "g7")
    chg = [c for c in o.calls if c[0] == "aria2.changeOption"]
    check("路径通配按种子内相对路径匹配（去掉保存目录前缀）", chg and chg[0][1][1]["select-file"] == "1", o.calls)


def t_kill_aria2c_under():
    """video_downloader.kill_aria2c_under：只杀命令行带该临时目录的 aria2c（yt-dlp 外部下载器），别的不碰。"""
    import tempfile
    sys.path.insert(0, os.path.join(HERE, "tests"))
    import ui_smoke_core
    ui_smoke_core._stub_tk()
    import video_downloader as V
    d = tempfile.mkdtemp(prefix="sdl_k_")
    fake = os.path.join(d, "aria2c")
    os.symlink(sys.executable, fake)               # 进程名叫 aria2c 的 python（命令行带 --dir）
    tmp_a, tmp_b = os.path.join(d, ".snifftmp", "aaa"), os.path.join(d, ".snifftmp", "bbb")
    pa = subprocess.Popen([fake, "-c", "import time;time.sleep(300)", "--dir", tmp_a + "/"])
    pb = subprocess.Popen([fake, "-c", "import time;time.sleep(300)", "--dir", tmp_b + "/"])
    time.sleep(0.5)
    n = V.kill_aria2c_under(tmp_a)
    try:
        pa.wait(5)
    except Exception:
        pass
    check("kill_aria2c_under：杀掉带该目录的 aria2c，返回 1", n == 1 and pa.poll() is not None, (n, pa.poll()))
    check("kill_aria2c_under：别的目录的 aria2c 不受影响", pb.poll() is None, pb.poll())
    check("kill_aria2c_under：目录没有对应进程返回 0", V.kill_aria2c_under(os.path.join(d, "nonexist")) == 0)
    pb.kill()
    pb.wait()
    shutil.rmtree(d, ignore_errors=True)


def t_bucket_refund():
    """令牌桶：被取消打断的 consume 退还没睡完的透支，否则分片线程被取消后欠的债由后来的任务偿还。"""
    from core import ratelimit as RL
    b = RL.TokenBucket(1000)
    stop = threading.Event()
    ths = [threading.Thread(target=lambda: b.consume(200000, cancel=stop.is_set), daemon=True) for _ in range(8)]
    for th in ths:
        th.start()
    time.sleep(0.4)
    stop.set()
    for th in ths:
        th.join(3)
    check("令牌桶：取消后分片线程全部退出", not any(th.is_alive() for th in ths))
    t0 = time.time()
    b.consume(500)
    el = time.time() - t0
    check("令牌桶：被取消线程的透支已退还，新消费 500B@1000B/s 在 2s 内返回（实 %.2fs）" % el, el < 2.0, el)


def t_token_auth():
    """R109：界面令牌缺失被拒、aria2 rpc_token 缺失/错误被拒、正确放行并剥离。"""
    from core import api as A
    import rpc_server as R
    try:
        A.dispatch(None, "sniffdl.snapshot", [], {})
        ok = False
    except Exception as e:
        ok = getattr(e, "code", None) == -32001
    check("接口：无界面令牌调用受保护方法被拒(-32001)", ok)
    check("接口：未知方法返回 NotImplemented", A.dispatch(None, "sniffdl.nope", [], {"ui": True}) is NotImplemented)
    check("接口：ping 无需令牌可注册", "sniffdl.ping" in A.methods())
    srv = R.RpcServer.__new__(R.RpcServer)
    srv.token = "abc"
    for name, prm, msg in (("缺失", [["u"]], "missing"), ("错误", ["token:bad", ["u"]], "wrong")):
        try:
            srv._check_token(prm)
            got = ""
        except R.RpcError as e:
            got = e.message
        check("rpc_token %s 被拒" % name, msg in got, got)
    check("rpc_token 正确则剥离首位", srv._check_token(["token:abc", ["u"]]) == [["u"]])
    srv.token = ""
    check("无 rpc_token 时剥离客户端习惯性 token:xxx", srv._check_token(["token:zzz", ["u"]]) == [["u"]])
    check("无 rpc_token 时普通参数原样", srv._check_token([["u"]]) == [["u"]])


def t_path_traversal():
    """R112：压缩包越界路径被拒、解压不写出目标目录；safe_name 清洗分隔符。"""
    import zipfile, tempfile
    from core import postactions as PA
    d = tempfile.mkdtemp(prefix="trav_")
    zp = os.path.join(d, "evil.zip")
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("ok.txt", "x")
        z.writestr("../escape.txt", "boom")
    ok, dest, msg = PA.extract(zp, log=lambda *a: None)
    check("越界 zip 解压被拒", not ok, msg)
    check("越界文件没写到目标目录之外", not os.path.exists(os.path.join(d, "escape.txt")))
    zp2 = os.path.join(d, "good.zip")
    with zipfile.ZipFile(zp2, "w") as z:
        z.writestr("sub/a.txt", "x")
    ok2, dest2, _ = PA.extract(zp2, log=lambda *a: None)
    check("正常 zip 仍可解压", ok2 and os.path.exists(os.path.join(dest2, "sub", "a.txt")))
    import video_downloader as V
    n = V.safe_name("..\\..\\a/b:c*?.txt")
    check("safe_name 去掉路径分隔符与非法字符: %r" % n, not any(c in n for c in '/\\:*?'), n)


def main():
    t_path_traversal()
    t_token_auth()
    t_platform_util()
    t_bt_exclude()
    t_kill_aria2c_under()
    t_bucket_refund()

    shutil.rmtree(DATA, ignore_errors=True)
    os.makedirs(os.path.join(DATA, "out"), exist_ok=True)
    # 账本：一天用旧键 n（record_download 写的），一天用 count（兼容）
    import datetime
    today = datetime.date.today()
    y = (today - datetime.timedelta(days=1)).isoformat()
    old = (today - datetime.timedelta(days=400)).isoformat()
    with open(os.path.join(DATA, "stats.json"), "w", encoding="utf-8") as f:
        json.dump({today.isoformat(): {"n": 3, "bytes": 3000}, y: {"count": 2, "bytes": 500},
                   old: {"n": 1, "bytes": 7}}, f)

    gw, gport = serve(FakeGateway)
    pg, pport = serve(PageServer)
    cport = free_port()
    env = dict(os.environ, SNIFFDL_DRYRUN_OS="1")
    env.pop("DISPLAY", None)       # cookie.login 的有头浏览器在无显示环境下应快速失败，而不是真弹窗
    core = subprocess.Popen([sys.executable, os.path.join(HERE, "tests", "ui_smoke_core.py"), "--data-dir", DATA,
                             "--port", str(cport), "--runtime", os.path.join(DATA, "runtime.json"), "--ui-token", TOKEN],
                            cwd=HERE, env=env, stdout=open(os.path.join(DATA, "core.out"), "w"), stderr=subprocess.STDOUT)
    try:
        if not wait(lambda: raw_rpc(cport, "sniffdl.ping", [], token=None).get("result"), 25):
            check("内核启动", False, "见 %s/core.out" % DATA)
            return
        check("内核启动", True)
        rpc(cport, "sniffdl.settings.set", {"outdir": os.path.join(DATA, "out"), "clip_watch": False, "notify": False,
                                            "bt_tracker_auto": False, "bt_dht": False, "bt_lpd": False, "bt_pex": False})

        # ---- 令牌
        j = raw_rpc(cport, "sniffdl.selftest", [], token=None)
        check("无令牌调 ui 接口被拒（-32001）", (j.get("error") or {}).get("code") == -32001, j)
        j = raw_rpc(cport, "sniffdl.stats.reset", [], token="wrong")
        check("错误令牌被拒", "error" in j, j)
        ms = rpc(cport, "sniffdl.methods")
        need = ["sniffdl.cookie.info", "sniffdl.cookie.login", "sniffdl.cookie.loginStatus", "sniffdl.cookie.import",
                "sniffdl.components.list", "sniffdl.components.check", "sniffdl.components.update", "sniffdl.rt.test",
                "sniffdl.rt.models", "sniffdl.stats.history", "sniffdl.stats.reset", "sniffdl.os.openOutdir",
                "sniffdl.os.openLog", "sniffdl.sniff.slow", "sniffdl.sniff.slowStatus", "sniffdl.sniff.slowCancel",
                "sniffdl.selftest", "sniffdl.bt.pauseAll", "sniffdl.bt.resumeAll", "sniffdl.torrent.upload"]
        check("methods 列出全部 WP1a 接口", all(m in ms for m in need), [m for m in need if m not in ms])

        # ---- 设置键
        r = rpc(cport, "sniffdl.settings.set", {"after": "重启", "sidecar_m3u8": False, "sidecar_bili": True,
                                                "sidecar_paths": "BBDown=/opt/BBDown", "cancel_cleanup": "always",
                                                "resume_policy": "restart", "speed_engine": "aria2"})
        v = rpc(cport, "sniffdl.settings.get")["values"]
        check("新键可保存并读回", v["after"] == "重启" and v["sidecar_m3u8"] is False and v["sidecar_bili"] is True
              and v["sidecar_paths"] == "BBDown=/opt/BBDown" and v["cancel_cleanup"] == "always"
              and v["resume_policy"] == "restart" and v["speed_engine"] == "aria2", (r, {k: v.get(k) for k in ("after", "cancel_cleanup")}))
        j = raw_rpc(cport, "sniffdl.settings.set", [{"resume_policy": "乱写"}])
        bad = j.get("error") or (j.get("result") or {}).get("errors") or ((j.get("result") or {}).get("ok") is False)
        v2 = rpc(cport, "sniffdl.settings.get")["values"]
        check("非法 select 值被拒且不落盘", bad and v2["resume_policy"] == "restart", j)
        cfg = json.load(open(os.path.join(DATA, "config.json"), encoding="utf-8"))
        check("新键写进数据目录 config.json", cfg.get("cancel_cleanup") == "always" and cfg.get("after") == "重启",
              {k: cfg.get(k) for k in ("cancel_cleanup", "after")})
        rpc(cport, "sniffdl.settings.set", {"after": "无"})

        # ---- 实时语音网关（密钥掩码 + 假网关）
        rpc(cport, "sniffdl.settings.set", {"rt_base_url": "http://127.0.0.1:%d/v1" % gport, "rt_api_key": RT_KEY,
                                            "rt_model": "gpt-rt-a"})
        v = rpc(cport, "sniffdl.settings.get")["values"]
        check("rt_api_key 读回是掩码不是明文", v["rt_api_key"] == "********", v["rt_api_key"])
        rpc(cport, "sniffdl.settings.set", {"rt_api_key": "********"})
        cfg = json.load(open(os.path.join(DATA, "config.json"), encoding="utf-8"))
        check("回传掩码不覆盖已存密钥", cfg.get("rt_api_key") == RT_KEY)
        r = rpc(cport, "sniffdl.rt.test", "", "********", "")
        check("rt.test 掩码 → 用已存密钥打通 /models + /responses", r.get("ok") and r.get("models_count") == 2
              and r.get("model_exists") is True and r.get("responses_http") == 200 and "key" not in r, r)
        check("rt.test 返回里不含密钥明文", RT_KEY not in json.dumps(r, ensure_ascii=False), r)
        r = rpc(cport, "sniffdl.rt.models", "http://127.0.0.1:%d/v1" % gport, "********")
        check("rt.models 列出模型", r.get("ok") and r.get("models") == ["gpt-rt-a", "gpt-rt-b"], r)
        r = rpc(cport, "sniffdl.rt.models", "http://127.0.0.1:%d/v1" % gport, "sk-wrong")
        check("rt.models 错密钥 → ok=false", r.get("ok") is False and "401" in r.get("error", ""), r)
        r = rpc(cport, "sniffdl.rt.test", "http://127.0.0.1:1/v1", "x", "m")
        check("rt.test 网关不通 → ok=false 带原因", r.get("ok") is False and (r.get("models_err") or r.get("responses_err")), r)

        # ---- 统计
        h = rpc(cport, "sniffdl.stats.history", 7)
        check("stats.history 7 天、末项是今天", len(h["days"]) == 7 and h["days"][-1]["date"] == today.isoformat(), h["days"][-2:])
        check("stats.history 今日 n→count=3、昨天 count=2", h["today_count"] == 3 and h["today_bytes"] == 3000
              and h["days"][-2]["count"] == 2, h)
        check("stats.history 累计含 7 天外记录", h["total_count"] == 6 and h["total_bytes"] == 3507 and h["active_days"] == 3, h)
        h112 = rpc(cport, "sniffdl.stats.history")
        check("stats.history 默认 112 天（16 周热力图）", len(h112["days"]) == 112)
        r = rpc(cport, "sniffdl.stats.reset")
        h = rpc(cport, "sniffdl.stats.history", 7)
        st = json.load(open(os.path.join(DATA, "stats.json"), encoding="utf-8"))
        check("stats.reset 清空账本", r.get("cleared_days") == 3 and h["total_count"] == 0 and st == {}, (r, st))

        # ---- 系统：打开目录 / 日志（dry-run 断言实际要打开的路径）
        r = rpc(cport, "sniffdl.os.openOutdir")
        acts = rpc(cport, "sniffdl.debug.osActions", 5)
        check("os.openOutdir 打开设置的保存目录", r.get("ok") and r["path"] == os.path.join(DATA, "out")
              and acts[-1]["kind"] == "open" and acts[-1]["target"] == r["path"], (r, acts[-1:]))
        sub = os.path.join(DATA, "out", "新建子目录")
        r = rpc(cport, "sniffdl.os.openOutdir", sub)
        check("os.openOutdir 指定目录不存在时自动创建", r.get("ok") and os.path.isdir(sub), r)
        r = rpc(cport, "sniffdl.os.openLog")
        acts = rpc(cport, "sniffdl.debug.osActions", 5)
        check("os.openLog 打开数据目录里的 sniffdl.log", r.get("ok") and r["path"] == os.path.join(DATA, "sniffdl.log")
              and os.path.exists(r["path"]) and acts[-1]["target"] == r["path"], (r, acts[-1:]))

        # ---- Cookie
        r = rpc(cport, "sniffdl.cookie.info")
        check("cookie.info 返回模式/已存数/浏览器列表", r.get("ok") and "stored" in r and isinstance(r.get("browsers"), list), r)
        r = rpc(cport, "sniffdl.cookie.import", "")
        check("cookie.import 空内容 → ok=false", r.get("ok") is False, r)
        exp = int(time.time()) + 86400 * 30
        txt = ("# Netscape HTTP Cookie File\n"
               ".example.com\tTRUE\t/\tFALSE\t%d\tSESSDATA\tabc123\n"
               ".example.com\tTRUE\t/\tFALSE\t%d\tbili_jct\tdef456\n") % (exp, exp)
        r = rpc(cport, "sniffdl.cookie.import", txt)
        check("cookie.import 导入 2 条", r.get("ok") and r.get("stored", 0) >= 2, r)
        check("cookie.import 落到内核数据目录 cookies.txt", os.path.exists(os.path.join(DATA, "cookies.txt"))
             , os.listdir(DATA))
        v = rpc(cport, "sniffdl.settings.get")["values"]
        r = rpc(cport, "sniffdl.cookie.info")
        check("导入后来源切到「已登录Cookie」且 info 计数更新", v["cookie_browser"] == "已登录Cookie" and r.get("stored", 0) >= 2
              and r.get("has_cookie_file"), (v["cookie_browser"], r))
        r = rpc(cport, "sniffdl.cookie.login", "https://127.0.0.1:%d/login" % pport)
        check("cookie.login 立即返回 started", r.get("ok") and (r.get("started") or r.get("already")), r)
        r2 = rpc(cport, "sniffdl.cookie.login", "")
        s = rpc(cport, "sniffdl.cookie.loginStatus")
        check("cookie.loginStatus 有 running/done/msg 字段", all(k in s for k in ("running", "done", "saved", "msg")), s)
        s = wait(lambda: (lambda x: x if x.get("done") else None)(rpc(cport, "sniffdl.cookie.loginStatus")), 40, 0.5)
        check("cookie.login 无显示环境下结束并给出原因（不卡死）", s and s["running"] is False and s["msg"], s)

        # ---- 组件
        r = rpc(cport, "sniffdl.components.list")
        check("components.list 有当前版本", r.get("ok") and isinstance(r.get("cur"), dict) and r["cur"], r)
        r = rpc(cport, "sniffdl.components.check")
        check("components.check 后台开始", r.get("ok") and (r.get("started") or r.get("already")), r)
        r2 = rpc(cport, "sniffdl.components.check")
        check("components.check 检查中重复调 → already", r2.get("ok") and (r2.get("already") or r2.get("started")), r2)
        done = wait(lambda: rpc(cport, "sniffdl.components.list").get("checking") is False, 60, 1)
        check("components.check 结束（checking=false）", done)
        r = rpc(cport, "sniffdl.components.update", "not-a-component")
        check("components.update 不支持的组件 → ok=false", r.get("ok") is False and "不支持" in r.get("error", ""), r)

        # ---- 自检
        r = rpc(cport, "sniffdl.selftest")
        names = {i["name"]: i for i in r.get("items", [])}
        check("selftest 返回逐项结果", {"aria2c", "ffmpeg", "yt-dlp", "数据目录可写", "保存目录可写", "内核端口"} <= set(names), list(names))
        check("selftest 内核端口/数据目录/保存目录 通过", names["内核端口"]["ok"] and names["数据目录可写"]["ok"]
              and names["保存目录可写"]["ok"] and DATA in names["数据目录可写"]["detail"], names)
        check("selftest aria2c 找到（PATH 已加 speed/tools）", names["aria2c"]["ok"], names["aria2c"])
        a2 = [k for k in names if k.startswith("aria2 兼容端口")]
        want = rpc(cport, "sniffdl.settings.get")["rpc"]["configured_port"]
        check("selftest 报告 aria2 兼容端口（设定 %s / 实际 %s）" % (want, cport),
              a2 and a2[0].endswith(str(want)) and names[a2[0]]["ok"] == (int(want) == cport), names.get(a2[0] if a2 else ""))
        check("selftest 总体 ok 与各项一致", r["ok"] == all(i["ok"] for i in r["items"]))

        # ---- 慢速取片
        j = raw_rpc(cport, "sniffdl.sniff.slow", ["ftp://x"])
        check("sniff.slow 非 http 地址 → -32602", (j.get("error") or {}).get("code") == -32602, j)
        r = rpc(cport, "sniffdl.sniff.slow", "http://127.0.0.1:%d/page" % pport)
        jid = r.get("id")
        check("sniff.slow 立即返回 id", r.get("ok") and jid, r)
        s = rpc(cport, "sniffdl.sniff.slowStatus", jid)
        check("sniff.slowStatus 返回状态", s["id"] == jid and s["state"] in ("running", "error", "cancelled", "done"), s)
        lst = rpc(cport, "sniffdl.sniff.slowStatus")
        check("sniff.slowStatus 无参 → 列表", isinstance(lst, list) and any(x["id"] == jid for x in lst), lst)
        c = rpc(cport, "sniffdl.sniff.slowCancel", jid)
        check("sniff.slowCancel ok", c.get("ok"), c)
        fin = wait(lambda: (lambda x: x if x["state"] in ("cancelled", "error", "done") else None)(
            rpc(cport, "sniffdl.sniff.slowStatus", jid)), 90, 1)
        check("sniff.slow 取消后进入终态（cancelled/error）", fin and fin["state"] in ("cancelled", "error") and fin["finished"], fin)
        j = raw_rpc(cport, "sniffdl.sniff.slowCancel", ["nope"])
        check("sniff.slowCancel 不存在的 id → -32602", (j.get("error") or {}).get("code") == -32602, j)

        # ---- torrent.upload + BT 批量暂停/继续
        tb = make_torrent_bytes("http://127.0.0.1:%d/announce" % pport)
        j = raw_rpc(cport, "sniffdl.torrent.upload", [base64.b64encode(b"not torrent").decode(), "x.torrent"])
        check("torrent.upload 非种子 → -32602", (j.get("error") or {}).get("code") == -32602, j)
        r = rpc(cport, "sniffdl.torrent.upload", base64.b64encode(tb).decode().rstrip("="), "单元/测试:种子")
        path = r.get("path", "")
        check("torrent.upload 落到 内核数据目录/torrents/ 且文件名清洗", r.get("ok") and path.startswith(os.path.join(DATA, "torrents") + os.sep)
              and path.endswith(".torrent") and open(path, "rb").read() == tb and "/" not in os.path.basename(path)[:-8].replace("_", ""), r)
        rpc(cport, "sniffdl.settings.set", {"bt_upload_limit_kbps": 64})
        gid = rpc(cport, "sniffdl.task.add", {"url": path}).get("gid")
        check("种子任务已添加", gid)
        bt = wait(lambda: (lambda t: t if t and t[0]["status"] in ("active", "waiting") else None)(
            [t for t in rpc(cport, "sniffdl.tasks") if t["gid"] == gid and t["kind"] == "bt"]), 20)
        check("任务识别为 bt 且在下载/排队", bt, [t for t in rpc(cport, "sniffdl.tasks") if t["gid"] == gid])
        r = rpc(cport, "sniffdl.bt.pauseAll")
        st = wait(lambda: [t for t in rpc(cport, "sniffdl.tasks") if t["gid"] == gid][0]["status"] == "paused", 10)
        check("bt.pauseAll → 暂停", st, (r, [t["status"] for t in rpc(cport, "sniffdl.tasks") if t["gid"] == gid]))
        r = rpc(cport, "sniffdl.bt.resumeAll")
        st = wait(lambda: [t for t in rpc(cport, "sniffdl.tasks") if t["gid"] == gid][0]["status"] in ("active", "waiting"), 10)
        check("bt.resumeAll → 继续", st, (r, [t["status"] for t in rpc(cport, "sniffdl.tasks") if t["gid"] == gid]))
        logs = "\n".join(str(x) for x in rpc(cport, "sniffdl.logs", 500))
        rpc(cport, "sniffdl.settings.set", {"bt_upload_limit_kbps": 32})
        logs2 = "\n".join(str(x) for x in rpc(cport, "sniffdl.logs", 500))
        check("bt_upload_limit_kbps 热更新到 BT 守护", "BT 上传 32 KB/s" in logs2, logs2[-600:])
        ps = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
        check("BT aria2 启动参数带上传限速", "--max-overall-upload-limit=64K" in ps or "--max-overall-upload-limit=32K" in ps,
              [l for l in ps.splitlines() if "aria2c" in l][:3])
        rpc(cport, "sniffdl.task.remove", [gid], True)
    finally:
        try:
            rpc(cport, "sniffdl.core.shutdown")
            core.wait(timeout=10)
        except Exception:
            core.kill()
        gw.shutdown()
        pg.shutdown()
    n_ok = sum(1 for _, ok in RESULTS if ok)
    print("\ncore_unit: %d/%d 通过" % (n_ok, len(RESULTS)))
    if n_ok != len(RESULTS):
        print("失败：", [n for n, ok in RESULTS if not ok])


if __name__ == "__main__":
    main()
    sys.exit(0 if RESULTS and all(ok for _, ok in RESULTS) else 1)
