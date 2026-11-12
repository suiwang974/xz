# -*- coding: utf-8 -*-
"""SniffDL 本地下载 RPC 服务（aria2-JSON-RPC 兼容子集）。

设计：本模块只做"传输 + aria2 语义"，不碰下载引擎本身。真正的下载/排队/进度
由宿主（App）通过一个 backend 对象提供（鸭子类型，见下方方法约定）。这样：
  · 浏览器插件、油猴脚本、网页"推送到 Aria2"按钮都能直接把我们当 aria2 用；
  · 只监听 127.0.0.1，避免 Windows 防火墙弹窗与外部访问；
  · 可选 token 鉴权（params[0] == "token:SECRET"），防本地被网页跨源滥用。

backend 需实现（gid 均为字符串）：
  rpc_add_uri(uri:str, options:dict) -> gid
  rpc_start(gid) / rpc_pause(gid) / rpc_resume(gid) -> None
  rpc_remove(gid) -> None
  rpc_struct(gid) -> dict | None            # 返回 aria2 风格任务结构（含 status 等）
  rpc_list(segments) -> list[dict]          # segments 为 {"active","waiting","stopped"} 的可迭代集合
"""
import base64
import hashlib
import json
import socket
import struct
import os
import re
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# 无 token 时也允许跨源调用的来源：浏览器扩展 + 各网盘页面（网页里的"推送到 Aria2"按钮用页面 fetch，
# 需要 CORS 预检；油猴 GM_xmlhttpRequest 本身不受 CORS 限制）。其它网页一律不放行，防本地被滥用。
_CORS_ORIGIN_RE = re.compile(
    r"^(chrome-extension|moz-extension|edge-extension|safari-web-extension)://[^/]+$|"
    r"^https?://([a-z0-9-]+\.)*(baidu\.com|quark\.cn|uc\.cn|alipan\.com|aliyundrive\.com|"
    r"123pan\.com|123pan\.cn|123912\.com|123865\.com|xunlei\.com|115\.com|lanzou[a-z]?\.com|"
    r"woozooo\.com|189\.cn|139\.com|10086\.cn|guangyapan\.com)(:\d+)?$|"
    r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$", re.I)

# aria2 status 取值
ST_ACTIVE, ST_WAITING, ST_PAUSED, ST_COMPLETE, ST_ERROR = (
    "active", "waiting", "paused", "complete", "error")


class RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
WS_NOTIFICATIONS = ["aria2.onDownloadStart", "aria2.onDownloadPause", "aria2.onDownloadStop",
                    "aria2.onDownloadComplete", "aria2.onDownloadError", "aria2.onBtDownloadComplete"]


def parse_size(v):
    """aria2 尺寸写法 → 字节：'1M' / '500K' / '1048576' / '1.5m' / '0'；K=1024，M=1024²，G=1024³。非法抛 ValueError。"""
    if isinstance(v, bool):
        raise ValueError("bad size")
    if isinstance(v, (int, float)):
        n = float(v)
    else:
        m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kKmMgG]?)(?:[iI]?[bB])?\s*", str(v or ""))
        if not m:
            raise ValueError("bad size: %r" % (v,))
        n = float(m.group(1)) * {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[m.group(2).lower()]
    if n < 0:
        raise ValueError("negative size")
    return int(n)


def size_to_kbps(v):
    """aria2 限速值 → KB/s（设置里的单位）；>0 但不足 1K 的按 1 KB/s，0=不限。"""
    b = parse_size(v)
    if b <= 0:
        return 0
    return max(1, int(round(b / 1024.0)))


def conns_option(n):
    """aria2 split / max-connection-per-server 数 → 设置 task_conns 的档位（自动/1/2/4/8/16，取不超过它的最大档）。"""
    n = int(str(n).strip())
    if n <= 0:
        return "自动"
    best = "1"
    for o in (1, 2, 4, 8, 16):
        if o <= n:
            best = str(o)
    return best


def ws_origin_ok(origin, token_set):
    """WebSocket 不受 CORS 保护，浏览器里任何网页都能连 ws://127.0.0.1:6800 —— 只放：
    无 Origin（AriaNg 桌面版/脚本/原生客户端）、null（file:// 打开的 AriaNg）、扩展 + 网盘页面 + 本机白名单；
    设了 token 时全放（鉴权在 RPC 层，每条消息都要带 token）。"""
    o = (origin or "").strip()
    if not o or o.lower() == "null" or token_set:
        return True
    return bool(_CORS_ORIGIN_RE.match(o))


def _norm_struct(t):
    """把宿主返回的任务字典补全成 aria2 常见字段，缺省填安全值。"""
    gid = str(t.get("gid", ""))
    total = int(t.get("totalLength") or t.get("total") or 0)
    done = int(t.get("downloadedLength") or t.get("done") or 0)
    speed = int(t.get("downloadSpeed") or t.get("speed") or 0)
    status = t.get("status") or ST_WAITING
    err_code = t.get("errorCode") or (t.get("error_code") or ("0" if status != ST_ERROR else "1"))
    err_msg = t.get("errorMessage") if t.get("errorMessage") is not None else t.get("error_message", "")
    name = t.get("name") or ""
    path = t.get("dir") or t.get("path") or ""
    base = {
        "gid": gid,
        "status": status,
        "totalLength": str(total),
        "completedLength": str(done),
        "downloadedLength": str(done),
        "uploadLength": str(t.get("uploadLength") or 0),
        "downloadSpeed": str(speed),
        "maxDownloadLimit": str(t.get("maxDownloadLimit") or 0),
        "bitfield": t.get("bitfield") or "",
        "numSeeders": int(t.get("numSeeders") or 0),
        "connections": int(t.get("connections") or 1),
        "dir": path,
        "incompletePathName": "",
        "name": name,
        "belongTo": gid,
        "belongsTo": gid,
        "index": "1",
        "errorCode": str(err_code),
        "errorMessage": err_msg or "",
        "statusMessage": t.get("statusMessage") or "",
        "verifiedLength": str(t.get("verifiedLength") or 0),
        "verifyIntegrityPending": t.get("verifyIntegrityPending") or "false",
        # ---- 记忆系统扩展字段（SniffDL 自己的 webview/插件用，外部 aria2 客户端会忽略）----
        "src": t.get("src") or "user",
        "note": t.get("note") or "",
        "addedTs": str(t.get("added_ts") or ""),
        "uri": t.get("uri") or "",
        "files": [{
            "index": "1",
            "path": t.get("path") or name or gid,
            "length": str(total),
            "selected": "true",
            "uris": [{"uri": t.get("uri") or "", "status":
                      ("complete" if status == ST_COMPLETE else "active"),
                      "currentUri": t.get("uri") or ""}],
        }],
    }
    if status == ST_COMPLETE:
        base["followedBy"] = []
    return base


class _Handler(BaseHTTPRequestHandler):
    server_version = "SniffDL-RPC/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):        # 静音默认 stderr 访问日志
        return

    # ---- CORS / 预检 ----
    def _cors(self):
        srv = self.server
        origin = (self.headers.get("Origin") or "").strip()
        allow = ""
        _rh = (self.headers.get("Access-Control-Request-Headers") or "").lower()
        if "x-sniffdl-token" in _rh or self._ctx().get("ui"):
            allow = origin or "*"             # v3 界面（file:// 的 Origin 是 "null"）：带界面令牌的请求放行，鉴权在 RPC 层
        elif getattr(srv, "token", ""):
            allow = "*"                       # 有 token：鉴权在 RPC 层做，跨源全放
        elif origin and _CORS_ORIGIN_RE.match(origin):
            allow = origin                    # 无 token：只放扩展 + 网盘页面 + 本机
        if allow:
            self.send_header("Access-Control-Allow-Origin", allow)
            if allow != "*":
                self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers",
                             "Content-Type, Authorization, Accept, X-Requested-With, X-SniffDL-Token, Last-Event-ID")
            self.send_header("Access-Control-Max-Age", "600")
            # Chrome「专用网络访问(PNA)」：公网页面访问 127.0.0.1 的预检必须回这个头
            if (self.headers.get("Access-Control-Request-Private-Network") or "").lower() == "true":
                self.send_header("Access-Control-Allow-Private-Network", "true")

    # ---- v3：界面令牌 + 防 DNS 重绑定 ----
    def _ctx(self):
        srv = self.server
        tok = getattr(srv.rpc.backend, "ui_token", "") or ""
        got = (self.headers.get("X-SniffDL-Token") or "").strip()
        auth = (self.headers.get("Authorization") or "").strip()
        if not got and auth.lower().startswith("bearer "):
            got = auth[7:].strip()
        if not got:
            try:
                got = (parse_qs(urlparse(self.path).query).get("t") or [""])[0]
            except Exception:
                got = ""
        return {"ui": bool(tok) and got == tok}

    def _host_ok(self):
        """只认本机 Host（127.0.0.1 / localhost / [::1]），挡住 DNS 重绑定攻击（恶意域名解析到 127.0.0.1）。"""
        h = (self.headers.get("Host") or "").strip().lower()
        if not h:
            return True
        host = h.rsplit(":", 1)[0] if not h.startswith("[") else h.split("]")[0] + "]"
        return host in ("127.0.0.1", "localhost", "[::1]", "::1")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if not self._host_ok():
            self._reply(403, {"error": "bad host"})
            return
        try:
            _path = urlparse(self.path).path
        except Exception:
            _path = "/"
        b = self.server.rpc.backend
        if _path == "/events" and hasattr(b, "serve_events"):
            if not self._ctx()["ui"]:
                self._reply(401, {"error": "需要界面令牌"})
                return
            try:
                lid = int(self.headers.get("Last-Event-ID") or
                          (parse_qs(urlparse(self.path).query).get("since") or ["0"])[0] or 0)
            except Exception:
                lid = 0
            b.serve_events(self, lid)
            return
        if _path.startswith("/ui/") and getattr(b, "ui_dir", None):
            self._static(b.ui_dir, _path[4:])
            return
        if _path == "/ui":
            self.send_response(302)
            self.send_header("Location", "/ui/index.html")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        # aria2 规范的 GET 形式：/jsonrpc?method=M&id=I&params=<base64(JSON 数组)>（少数网页组件用）
        try:
            pr = urlparse(self.path)
            q = parse_qs(pr.query)
        except Exception:
            q = {}
        if q.get("method"):
            try:
                params = []
                if q.get("params"):
                    raw = q["params"][0]
                    raw += "=" * (-len(raw) % 4)
                    params = json.loads(base64.b64decode(raw).decode("utf-8", "ignore"))
                req = {"method": q["method"][0], "id": (q.get("id") or [None])[0], "params": params}
            except Exception:
                self._reply(400, {"jsonrpc": "2.0", "error":
                                  {"code": -32700, "message": "parse error"}, "id": None})
                return
            resp = self.server.rpc.handle(req, {"ui": False})
            cb = (q.get("jsoncallback") or [""])[0]
            if cb and re.fullmatch(r"[A-Za-z0-9_.$]{1,64}", cb):
                body = ("%s(%s)" % (cb, json.dumps(resp, ensure_ascii=False))).encode("utf-8")
                self.send_response(200)
                self._cors()
                self.send_header("Content-Type", "text/javascript; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except Exception:
                    pass
                return
            self._reply(200, resp)
            return
        # 轻量健康检查：GET /  返回服务名，方便插件探活
        body = b'{"service":"SniffDL","compatible":"aria2-json-rpc"}'
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    _MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
             ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
             ".svg": "image/svg+xml", ".png": "image/png", ".webp": "image/webp", ".json": "application/json",
             ".woff2": "font/woff2", ".ico": "image/x-icon", ".wav": "audio/wav", ".mp3": "audio/mpeg"}

    def _static(self, root, rel):
        """开发/测试用：把 ui/ 目录当静态站点（Electron/pywebview 正式走 file://，不依赖这里）。"""
        rel = (rel or "index.html").split("?")[0]
        base = os.path.realpath(root)
        f = os.path.realpath(os.path.join(base, rel.replace("/", os.sep)))
        if not f.startswith(base + os.sep) or not os.path.isfile(f):
            self._reply(404, {"error": "not found"})
            return
        with open(f, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", self._MIME.get(os.path.splitext(f)[1].lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_POST(self):
        if not self._host_ok():
            self._reply(403, {"error": "bad host"})
            return
        ctx = self._ctx()
        if (self.headers.get("X-SniffDL-Token") is not None or
                (self.headers.get("Authorization") or "").lower().startswith("bearer ")) and not ctx["ui"]:
            # 带了界面令牌头却不对：整条拒绝（预检对任何 Origin 放行了这个头，必须在这里把关，防网页借它绕过 CORS）
            self._reply(401, {"jsonrpc": "2.0", "error": {"code": -32001, "message": "界面令牌无效"}, "id": None})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            req = json.loads(raw.decode("utf-8", "ignore") or "{}")
        except Exception:
            self._reply(400, {"jsonrpc": "2.0", "error":
                              {"code": -32700, "message": "parse error"}, "id": None})
            return
        if isinstance(req, list):              # JSON-RPC 批量请求（AriaNg 等会用）
            resp = [self.server.rpc.handle(r if isinstance(r, dict) else {}, ctx) for r in req]
        else:
            resp = self.server.rpc.handle(req if isinstance(req, dict) else {}, ctx)
        self._reply(200, resp)

    def _reply(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass


class RpcServer:
    """在后台线程里跑的 aria2 兼容 JSON-RPC 服务。"""

    def __init__(self, backend, host="127.0.0.1", port=6800, token=""):
        self.backend = backend
        self._recent = {}          # 最近被移除的 gid（tellStatus 查已移除任务时回 removed 而不是报错）
        self.host = host
        self.port = port
        self.token = token or ""
        self._httpd = None
        self._thread = None
        self.bound_port = 0
        self._ctx_local = threading.local()

    # ---- 生命周期 ----
    def start(self):
        self._httpd = ThreadingHTTPServer((self.host, self.port), _Handler)
        self._httpd.backend = self.backend
        self._httpd.token = self.token
        self._httpd.rpc = self
        self.bound_port = self._httpd.server_address[1]
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="SniffDL-RPC", daemon=True)
        self._thread.start()
        return self.bound_port

    def stop(self):
        try:
            if self._httpd:
                self._httpd.shutdown()
                self._httpd.server_close()
        except Exception:
            pass

    # ---- JSON-RPC 分发 ----
    def handle(self, req, ctx=None):
        rid = req.get("id")
        method = req.get("method") or ""
        params = req.get("params") or []
        if isinstance(params, dict):
            params = [params]
        ctx = ctx or {}
        try:
            if ctx.get("ui") and params and isinstance(params[0], str) and params[0].startswith("token:"):
                params = params[1:]                      # 界面令牌已鉴权，顺手剥掉 aria2 token 前缀
            elif not ctx.get("ui"):
                params = self._check_token(params)
            self._ctx_local.ctx = ctx
            result = self._dispatch(method, params)
            return {"jsonrpc": "2.0", "result": result, "id": rid}
        except RpcError as e:
            return {"jsonrpc": "2.0", "error": {"code": e.code, "message": e.message}, "id": rid}
        except Exception as e:  # 兜底
            if e.__class__.__name__ == "ApiError":
                return {"jsonrpc": "2.0", "error": {"code": getattr(e, "code", -32000),
                                                    "message": getattr(e, "msg", str(e))}, "id": rid}   # 兜底：绝不让 RPC 线程因异常崩掉
            return {"jsonrpc": "2.0", "error": {"code": -32603,
                    "message": "internal error: %s" % str(e)[:200]}, "id": rid}

    def _check_token(self, params):
        if not self.token:
            # 我们没设密钥：外部 aria2 客户端仍可能习惯性带一个 "token:xxx" 首位参数，
            # 这里把它剥掉，否则它会污染真实 params（如 addUri 的 uris 列表）。
            if params and isinstance(params[0], str) and params[0].startswith("token:"):
                return params[1:]
            return params
        if params and isinstance(params[0], str) and params[0].startswith("token:"):
            if params[0][6:] == self.token:
                return params[1:]
            raise RpcError(-32000, "Unauthorized; wrong token")
        raise RpcError(-32000, "Unauthorized; missing token")

    def _dispatch(self, method, params):
        b = self.backend
        if method == "aria2.addUri":
            uris = params[0] if params else []
            uri = uris[0] if isinstance(uris, (list, tuple)) and uris else (uris if isinstance(uris, str) else "")
            if not uri:
                raise RpcError(-32602, "No URI to download")
            options = params[1] if len(params) > 1 and isinstance(params[1], dict) else {}
            return str(b.rpc_add_uri(uri, options))

        if method in ("aria2.addTorrent", "aria2.addMetalink"):
            # 种子以 base64 推来（AriaNg / 各种"推送到 aria2"）：落成临时 .torrent 交给内置 BT 引擎
            if method == "aria2.addMetalink":
                raise RpcError(1, "SniffDL 暂不支持 Metalink")
            raw = params[0] if params else ""
            options = {}
            for x in params[1:]:
                if isinstance(x, dict):
                    options = x
            try:
                data = base64.b64decode(raw + "=" * (-len(raw) % 4))
            except Exception:
                raise RpcError(-32602, "torrent 不是合法 base64")
            d = os.path.join(os.path.expanduser("~"), ".snifftdl", "torrents")
            os.makedirs(d, exist_ok=True)
            fp = os.path.join(d, "push_%d.torrent" % int(time.time() * 1000))
            with open(fp, "wb") as f:
                f.write(data)
            return str(b.rpc_add_uri(fp, options))

        if method in ("aria2.remove", "aria2.forceRemove"):
            gid = str(params[0]) if params else ""
            b.rpc_remove(gid)          # 不存在的 gid 也回 gid（旧行为，扩展网页卡依赖它）
            self._recent[gid] = time.time()
            return gid
        if method in ("aria2.pause", "aria2.forcePause"):
            gid = str(params[0]) if params else ""
            b.rpc_pause(gid)
            return gid
        if method == "aria2.pauseAll":
            for t in b.rpc_list({"active"}):
                b.rpc_pause(str(t.get("gid")))
            return "OK"
        if method in ("aria2.unpause", "aria2.forceUnpause"):
            gid = str(params[0]) if params else ""
            b.rpc_resume(gid)
            return gid

        if method == "aria2.getStatus":
            gid = str(params[0]) if params else ""
            t = b.rpc_struct(gid)
            if t is None:
                raise RpcError(-32001, "No such download for gid: %s" % gid)
            s = _norm_struct(t)
            # getStatus 返回精简版（含 numStatus）
            return {"numStatus": "1", "status": s["status"],
                    "downloadedLength": s["downloadedLength"],
                    "uploadLength": s["uploadLength"],
                    "error_message": s["errorMessage"], "errorCode": s["errorCode"]}

        if method == "aria2.tellStatus":
            # 绝大多数 aria2 前端（AriaNg / Motrix 插件 / 脚本「查看下载任务」）靠它盯单个任务
            gid = str(params[0]) if params else ""
            keys = params[1] if len(params) > 1 and isinstance(params[1], list) else None
            t = b.rpc_struct(gid)
            if t is None:
                if gid in self._recent:
                    s = {"gid": gid, "status": "removed", "errorCode": "0", "errorMessage": ""}
                    return {k: s.get(k, "") for k in keys} if keys else s
                raise RpcError(1, "GID %s is not found" % gid)
            s = _norm_struct(t)
            return {k: s[k] for k in keys if k in s} if keys else s
        if method == "aria2.getUris":
            t = b.rpc_struct(str(params[0]) if params else "")
            if t is None:
                raise RpcError(1, "GID not found")
            return _norm_struct(t)["files"][0]["uris"]
        if method == "aria2.getFiles":
            t = b.rpc_struct(str(params[0]) if params else "")
            if t is None:
                raise RpcError(1, "GID not found")
            return _norm_struct(t)["files"]
        if method in ("aria2.getPeers", "aria2.getServers"):
            return []
        if method == "aria2.getOption":
            t = b.rpc_struct(str(params[0]) if params else "")
            if t is None:
                raise RpcError(1, "GID not found")
            o = {"dir": t.get("dir") or "", "out": t.get("name") or ""}
            for k, v in (t.get("a2opts") or {}).items():
                o[k] = str(v)
            return o
        if method == "aria2.changePosition":
            return 0
        if method in ("aria2.unpauseAll",):
            for t in b.rpc_list({ST_PAUSED}):
                b.rpc_resume(str(t.get("gid")))
            return "OK"
        if method in ("aria2.forcePauseAll",):
            for t in b.rpc_list({"active", "waiting"}):
                b.rpc_pause(str(t.get("gid")))
            return "OK"
        if method in ("aria2.purgeDownloadResult", "aria2.saveSession"):
            return "OK"
        if method in ("aria2.shutdown", "aria2.forceShutdown"):
            return "OK"            # 外部前端无权关掉 SniffDL 本体
        if method == "system.listNotifications":
            return []

        if method == "aria2.tellActive":
            return [_norm_struct(t) for t in b.rpc_list({"active"})]
        if method == "aria2.tellWaiting":
            off = int(params[0]) if params else 0
            num = int(params[1]) if len(params) > 1 else 1024
            lst = [_norm_struct(t) for t in b.rpc_list({"waiting"})]
            return lst[off:off + num]
        if method == "aria2.tellStopped":
            off = int(params[0]) if params else 0
            num = int(params[1]) if len(params) > 1 else 1024
            # aria2 的 "stopped" = 暂停 + 完成 + 失败。记忆系统重启后把待恢复任务标成
            # paused，若这里不含 paused，重启恢复出来的队列就在界面上凭空消失。
            lst = [_norm_struct(t) for t in
                   b.rpc_list({ST_COMPLETE, ST_ERROR, ST_PAUSED})]
            return lst[off:off + num]

        if method == "aria2.getDownloadResult":
            gid = str(params[0]) if params else ""
            t = b.rpc_struct(gid)
            if t is None:
                raise RpcError(-32001, "No such download for gid: %s" % gid)
            return _norm_struct(t)

        # ---- aria2 握手/全局方法：外部 aria2 GUI 与网页「推送到 Aria2」组件
        #      第一步几乎都是调 aria2.getVersion 验证 RPC 是否在线，缺它就判定「连不上」。----
        if method == "aria2.getVersion":
            return {"version": "1.37.0",
                    "enabledFeatures": ["HTTPS", "Message-Checksum", "IPv6", "Metalink"]}
        if method == "aria2.getSessionInfo":
            return {"sessionId": "snifftdl"}
        if method == "aria2.getGlobalOption":
            try:
                info = b.rpc_global_option() or {}
            except Exception:
                info = {}
            return info
        if method == "aria2.getGlobalStat":
            def _sum(tasks, key):
                s = 0
                for t in tasks:
                    try:
                        s += int(t.get(key) or 0)
                    except Exception:
                        pass
                return str(s)
            act = b.rpc_list({"active"})
            wait = b.rpc_list({"waiting"})
            stop = b.rpc_list({ST_COMPLETE, ST_ERROR, ST_PAUSED})
            return {"numActive": str(len(act)), "numWaiting": str(len(wait)),
                    "numStopped": str(len(stop)),
                    "downloadSpeed": _sum(act, "downloadSpeed") or "0",
                    "uploadSpeed": _sum(act, "uploadSpeed") or "0",
                    "totalDownloadLength": "0", "totalUploadLength": "0"}
        if method == "aria2.changeGlobalOption":
            o = params[0] if params and isinstance(params[0], dict) else {}
            try:
                if hasattr(b, "rpc_change_global_option"):
                    b.rpc_change_global_option(o)
            except (ValueError, TypeError) as e:
                raise RpcError(1, "bad option: %s" % e)
            return "OK"
        if method == "aria2.changeOption":
            o = params[1] if len(params) > 1 and isinstance(params[1], dict) else {}
            if hasattr(b, "rpc_change_option"):
                try:
                    ok = b.rpc_change_option(str(params[0]) if params else "", o)
                except (ValueError, TypeError) as e:
                    raise RpcError(1, "bad option: %s" % e)
                if not ok:
                    raise RpcError(1, "GID not found")
            return "OK"
        if method == "aria2.removeDownloadResult":
            return "OK"

        if method == "sniffdl.theme":
            info = b.rpc_theme_info()
            if not isinstance(info, dict):
                raise RpcError(-32603, "bad theme info")
            return info
        if method == "sniffdl.status":
            try:
                return b.rpc_status_info()
            except Exception:
                return {"ok": True, "service": "SniffDL"}

        if method == "sniffdl.syncCookies":
            # 扩展同步浏览器 Cookie（手动粘贴/外部推送缺 Cookie 时按域补齐，IDM 浏览器集成同款）
            ck = params[0] if params and isinstance(params[0], list) else []
            return b.rpc_sync_cookies(ck)
        if method == "sniffdl.addDirect":
            info = params[0] if params and isinstance(params[0], dict) else {}
            return b.rpc_add_direct(info)
        if method == "sniffdl.netdiskRules":
            return b.rpc_netdisk_rules()

        if method == "sniffdl.intercept":
            info = params[0] if params and isinstance(params[0], dict) else {}
            return b.rpc_intercept(info)

        if method == "sniffdl.sniff":
            url = str(params[0]) if params else ""
            scope = str(params[1]) if len(params) > 1 and params[1] else "all"
            return str(b.rpc_sniff_start(url, scope))

        if method == "sniffdl.sniffResult":
            tid = str(params[0]) if params else ""
            return b.rpc_sniff_result(tid)

        if method == "sniffdl.recentSniff":
            return b.rpc_recent_sniff()
        if method == "sniffdl.clearRecentSniff":
            return b.rpc_clear_recent_sniff()

        if method == "sniffdl.dlDefaults":
            url = str(params[0]) if params else ""
            return b.rpc_dl_defaults(url)
        if method == "sniffdl.addCategory":
            name = str(params[0]) if params else ""
            return b.rpc_add_category(name)
        if method == "sniffdl.pickDir":
            title = str(params[0]) if params else ""
            return b.rpc_pick_dir(title)
        if method == "sniffdl.listDirs":
            path = str(params[0]) if params else ""
            return b.rpc_list_dirs(path)
        if method == "sniffdl.add":
            info = params[0] if params and isinstance(params[0], dict) else {}
            return str(b.rpc_add(info))
        if method == "sniffdl.reveal":
            gid = str(params[0]) if params else ""
            return b.rpc_reveal(gid)
        if method == "sniffdl.openFile":
            gid = str(params[0]) if params else ""
            return b.rpc_open_file(gid)
        if method == "sniffdl.clearQueue":
            done_only = True
            if params and isinstance(params[0], (bool, int)):
                done_only = bool(params[0])
            elif params and isinstance(params[0], str):
                done_only = params[0].lower() not in ("false", "0", "no", "all")
            try:
                return int(b.rpc_clear_queue(done_only=done_only))
            except Exception as e:
                raise RpcError(-32603, "clearQueue failed: %s" % str(e)[:120])

        if method == "sniffdl.getSettings":
            return b.rpc_get_settings()
        if method == "sniffdl.setSetting":
            key = str(params[0]) if params else ""
            value = params[1] if len(params) > 1 else None
            return b.rpc_set_setting(key, value)
        if method == "sniffdl.openOutDir":
            path = str(params[0]) if params and params[0] else None
            return b.rpc_open_outdir(path)
        if method == "sniffdl.stats":
            return b.rpc_stats()
        if method == "sniffdl.cookieInfo":
            return b.rpc_cookie_info()
        if method == "sniffdl.loginCookie":
            return b.rpc_login_cookie(str(params[0]) if params else "")
        if method == "sniffdl.loginStatus":
            return b.rpc_login_status()
        if method == "sniffdl.importCookie":
            return b.rpc_import_cookie(str(params[0]) if params else "")
        if method == "sniffdl.components":
            return b.rpc_components()
        if method == "sniffdl.checkUpdates":
            return b.rpc_check_updates()
        if method == "sniffdl.updateOne":
            return b.rpc_update_one(str(params[0]) if params else "")
        if method == "sniffdl.clipHint":
            return b.rpc_clip_hint()
        if method == "sniffdl.clipTake":
            return b.rpc_clip_take(str(params[0]) if params else "")
        if method == "sniffdl.clipDismiss":
            return b.rpc_clip_dismiss()
        if method == "sniffdl.testGateway":
            return b.rpc_test_gateway(*(params or []))
        if method == "sniffdl.listModels":
            return b.rpc_list_models(*(params or []))

        if method == "system.listMethods":
            return ["aria2.addUri", "aria2.addTorrent", "aria2.remove", "aria2.forceRemove",
                    "aria2.tellStatus", "aria2.getUris", "aria2.getFiles", "aria2.getPeers",
                    "aria2.getServers", "aria2.getOption", "aria2.changePosition",
                    "aria2.unpauseAll", "aria2.forcePauseAll", "aria2.purgeDownloadResult",
                    "aria2.saveSession", "aria2.shutdown", "aria2.forceShutdown",
                    "system.listNotifications",
                    "sniffdl.syncCookies", "sniffdl.addDirect", "sniffdl.netdiskRules",
                    "aria2.pause", "aria2.pauseAll", "aria2.unpause",
                    "aria2.getStatus", "aria2.tellActive", "aria2.tellWaiting",
                    "aria2.tellStopped", "aria2.getDownloadResult",
                    "aria2.getVersion", "aria2.getSessionInfo",
                    "aria2.getGlobalOption", "aria2.getGlobalStat",
                    "aria2.changeGlobalOption", "aria2.changeOption",
                    "aria2.removeDownloadResult",
                    "sniffdl.theme", "sniffdl.status", "sniffdl.intercept",
                    "sniffdl.sniff", "sniffdl.sniffResult",
                    "sniffdl.dlDefaults", "sniffdl.addCategory", "sniffdl.pickDir",
                    "sniffdl.listDirs",
                    "sniffdl.add", "sniffdl.reveal", "sniffdl.openFile", "sniffdl.clearQueue",
                    "sniffdl.recentSniff", "sniffdl.clearRecentSniff",
                    "sniffdl.getSettings", "sniffdl.setSetting",
                    "sniffdl.openOutDir", "sniffdl.stats",
                    "sniffdl.cookieInfo", "sniffdl.loginCookie", "sniffdl.loginStatus",
                    "sniffdl.importCookie", "sniffdl.components", "sniffdl.checkUpdates",
                    "sniffdl.updateOne", "sniffdl.clipHint", "sniffdl.clipTake",
                    "sniffdl.clipDismiss", "sniffdl.testGateway", "sniffdl.listModels",
                    "system.listMethods", "system.multicall"] + (
                        __import__("core.api", fromlist=["methods"]).methods() if hasattr(b, "rpc_ext") else [])

        if method == "system.multicall":
            calls = params[0] if params else []
            out = []
            for c in calls:
                m = c.get("methodName"); p = c.get("params") or []
                try:
                    p = self._check_token(list(p))       # 每个子调用各自带 token（aria2 规范）
                    out.append([self._dispatch(m, p)])
                except RpcError as e:
                    out.append({"code": e.code, "message": e.message})
            return out

        ext = getattr(b, "rpc_ext", None)
        if ext is not None:
            r = ext(method, params, getattr(self._ctx_local, "ctx", None) or {})
            if r is not NotImplemented:
                return r
        raise RpcError(-32601, "Method not found: %s" % method)
