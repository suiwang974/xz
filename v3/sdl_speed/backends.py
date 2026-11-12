"""底层：HTTP 请求（原样请求头 + 手动重定向）、预分配文件 + pwrite、侧车文件原子保存。"""
import json
import os
import sys
import threading
from http.cookiejar import DefaultCookiePolicy
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import SKIP_HEADER

from .errors import SpeedError, http_error, network_error, io_error

IS_WIN = os.name == "nt"

# 引擎自己管理的头：调用方给了也丢掉（Range / Accept-Encoding 由引擎写，其余是逐跳头）
ENGINE_HEADERS = {
    "range", "if-range", "accept-encoding", "host", "content-length",
    "connection", "keep-alive", "proxy-connection", "proxy-authorization", "te", "trailer",
    "transfer-encoding", "upgrade",
}
REDIRECTS = (301, 302, 303, 307, 308)
MAX_REDIRECTS = 10


def clean_headers(headers):
    """保留调用方的头（顺序、大小写、值都不改），只剔除引擎管理的头和空值。"""
    out = {}
    items = headers.items() if hasattr(headers, "items") else (headers or [])
    for k, v in items:
        if k is None or v is None or v == "":
            continue
        if str(k).strip().lower() in ENGINE_HEADERS:
            continue
        out[str(k).strip()] = str(v)
    return out


def _hget(h, name):
    name = name.lower()
    for k, v in h.items():
        if k.lower() == name:
            return v
    return None


def _hset(h, name, value):
    for k in list(h.keys()):
        if k.lower() == name.lower():
            h[k] = value
            return
    h[name] = value


def merge_set_cookie(headers, set_cookies):
    """把重定向响应里的 Set-Cookie 合进 Cookie 头（同名覆盖，其余原样保留）。"""
    if not set_cookies:
        return headers
    cur = _hget(headers, "Cookie") or ""
    pairs = []
    for part in cur.split(";"):
        part = part.strip()
        if part:
            n, _, v = part.partition("=")
            pairs.append([n.strip(), v])
    for sc in set_cookies:
        first = sc.split(";", 1)[0].strip()
        if "=" not in first:
            continue
        n, _, v = first.partition("=")
        n = n.strip()
        for p in pairs:
            if p[0] == n:
                p[1] = v
                break
        else:
            pairs.append([n, v])
    h = dict(headers)
    _hset(h, "Cookie", "; ".join("%s=%s" % (n, v) for n, v in pairs))
    return h


class Http:
    """一个连接（一个 worker 一个实例）。Session 默认头清空，Cookie 罐禁用，重定向手动跟。"""

    def __init__(self, proxies=None, verify=True, timeout=(15, 30), trust_env=True):
        s = requests.Session()
        s.headers.clear()
        s.trust_env = bool(trust_env)   # False：不读环境变量代理（回环地址直连、调用方明确不要代理）
        s.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))  # 不自动存/发 Cookie
        ad = HTTPAdapter(pool_connections=2, pool_maxsize=2, max_retries=0)
        s.mount("http://", ad)
        s.mount("https://", ad)
        self.s = s
        self.proxies = proxies
        self.verify = verify
        self.timeout = timeout

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass

    def get(self, url, headers, start=None, end=None, method="GET", data=None):
        """GET（或 POST：每个分片重发同一请求体 + Range，Gopeed 式）；start/end 为闭区间字节号
        （end=None 表示到文件尾）。返回 (response, final_url, headers_after_redirects)。
        跟完重定向后的方法与请求体记在 self.final_method / self.final_data（301/302/303 按浏览器规矩
        改成 GET 丢请求体；307/308 保留）。网络错误抛 SpeedError(network)。"""
        h = dict(headers)
        cur = url
        method = (method or "GET").upper()
        if method == "GET":
            data = None
        for _ in range(MAX_REDIRECTS + 1):
            send = dict(h)
            if start is not None:
                send["Range"] = "bytes=%d-%s" % (start, "" if end is None else end)
            send["Accept-Encoding"] = "identity"
            if _hget(send, "User-Agent") is None:
                send["User-Agent"] = SKIP_HEADER   # 不让 urllib3 偷偷加 python-urllib3/x.y
            try:
                r = self.s.request(method, cur, headers=send, data=data, stream=True, allow_redirects=False,
                                   proxies=self.proxies, verify=self.verify, timeout=self.timeout)
            except requests.RequestException as e:
                raise network_error(e)
            if r.status_code in REDIRECTS:
                loc = r.headers.get("Location")
                try:
                    sc = r.raw.headers.getlist("Set-Cookie")
                except Exception:
                    sc = []
                r.close()
                if not loc:
                    raise SpeedError("http", "重定向缺少目标地址", status=r.status_code)
                h = merge_set_cookie(h, sc)
                cur = urljoin(cur, loc)
                if r.status_code in (301, 302, 303) and method != "GET":
                    method, data = "GET", None
                    h = {k: v for k, v in h.items() if k.lower() != "content-type"}
                continue
            self.final_method, self.final_data = method, data
            return r, cur, h
        raise SpeedError("http", "重定向次数过多")


def read1(resp, n):
    """urllib3 read1：有多少读多少（不等凑满 n），流结束返回 b""。"""
    return resp.raw.read1(n, decode_content=False)


def parse_content_range(v):
    """'bytes 0-99/1000' -> (0, 99, 1000)；总长 '*' -> total None；格式不对返回 None。"""
    if not v:
        return None
    try:
        unit, _, rest = v.strip().partition(" ")
        if unit.lower() != "bytes":
            return None
        rng, _, tot = rest.partition("/")
        a, _, b = rng.partition("-")
        total = None if tot.strip() == "*" else int(tot)
        return int(a), int(b), total
    except Exception:
        return None


class Storage:
    """预分配的目标临时文件。POSIX 用 os.pwrite（无锁并发写）；Windows 用 锁+seek+write。"""

    def __init__(self, path, size=None, keep=False):
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
        try:
            if not keep and os.path.exists(path):
                os.remove(path)
            self.fd = os.open(path, flags, 0o644)
        except OSError as e:
            raise io_error(e)
        self.path = path
        self._lock = threading.Lock()
        if size:
            self.preallocate(size)

    def preallocate(self, size):
        try:
            cur = os.fstat(self.fd).st_size
            if cur == size:
                return
            if cur > size:
                os.ftruncate(self.fd, size)
                return
            if IS_WIN:
                _win_set_size(self.fd, size)
                return
            fa = getattr(os, "posix_fallocate", None)
            if fa is not None:
                try:
                    fa(self.fd, 0, size)
                    return
                except OSError:
                    pass                      # 文件系统不支持就退回稀疏文件
            os.ftruncate(self.fd, size)
        except OSError as e:
            raise io_error(e)

    def write(self, off, data):
        try:
            if IS_WIN or not hasattr(os, "pwrite"):
                with self._lock:
                    os.lseek(self.fd, off, os.SEEK_SET)
                    mv = memoryview(data)
                    while mv:
                        n = os.write(self.fd, mv)
                        mv = mv[n:]
            else:
                mv = memoryview(data)
                while mv:
                    n = os.pwrite(self.fd, mv, off)
                    mv = mv[n:]
                    off += n
        except OSError as e:
            raise io_error(e)

    def size(self):
        return os.fstat(self.fd).st_size

    def truncate(self, n):
        os.ftruncate(self.fd, n)

    def sync(self):
        try:
            os.fsync(self.fd)
        except OSError:
            pass

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            finally:
                self.fd = None


def _win_set_size(fd, size):
    """Windows：SetEndOfFile 直接设长度（不像 _chsize 那样逐字节写零）。"""
    try:
        import ctypes
        import msvcrt
        k32 = ctypes.windll.kernel32
        h = msvcrt.get_osfhandle(fd)
        dist = ctypes.c_longlong(size)
        if not k32.SetFilePointerEx(ctypes.c_void_p(h), dist, None, 0):
            raise OSError("SetFilePointerEx failed")
        if not k32.SetEndOfFile(ctypes.c_void_p(h)):
            raise OSError("SetEndOfFile failed")
    except Exception:
        os.ftruncate(fd, size)


class Sidecar:
    """out.sdlpart：剩余区间、总大小、ETag、Last-Modified。写临时文件再 os.replace，断电也不会写坏。"""

    VERSION = 1

    def __init__(self, path):
        self.path = path

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if d.get("v") != self.VERSION:
                return None
            d["ranges"] = [(int(a), int(b)) for a, b in d.get("ranges") or [] if int(b) > int(a)]
            return d
        except (OSError, ValueError, TypeError):
            return None

    def save(self, url, total, etag, last_modified, ranges):
        d = {"v": self.VERSION, "url": url, "total": total, "etag": etag,
             "last_modified": last_modified, "ranges": [[a, b] for a, b in ranges]}
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def remove(self):
        for p in (self.path, self.path + ".tmp"):
            try:
                os.remove(p)
            except OSError:
                pass


def is_windows():
    return sys.platform.startswith("win")
