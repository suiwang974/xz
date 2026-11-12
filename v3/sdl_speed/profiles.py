"""每主机的连接策略。

默认：起始 8 连接、上限 32；已知的大型 CDN（“快主机”）上限 64。
SniffDL 可以通过 set_resolver() 接入 netdisk_rules.match_profile，把网盘的防封上限
（例如某网盘最多 4 连接、单连接站点）变成这里的 HostProfile。
"""
import fnmatch
import threading
from dataclasses import dataclass, replace
from urllib.parse import urlsplit

DEFAULT_START = 8
DEFAULT_MAX = 32
FAST_MAX = 64
MIN_SPLIT = 512 * 1024


@dataclass(frozen=True)
class HostProfile:
    name: str = "通用"
    start_conn: int = DEFAULT_START
    max_conn: int = DEFAULT_MAX
    min_split: int = MIN_SPLIT

    def clamp(self):
        mx = max(1, int(self.max_conn))
        st = max(1, min(int(self.start_conn), mx))
        return replace(self, max_conn=mx, start_conn=st, min_split=max(64 * 1024, int(self.min_split)))


# 公开的大型 CDN：单连接通常已经很快，加连接收益也能继续上去
FAST_HOSTS = (
    "*.cloudfront.net", "*.akamaized.net", "*.akamaihd.net", "*.fastly.net",
    "*.githubusercontent.com", "objects.githubusercontent.com", "*.r2.cloudflarestorage.com",
    "dl-cdn.alpinelinux.org", "*.cdn.mozilla.net", "download.visualstudio.microsoft.com",
    "*.b-cdn.net", "*.jsdelivr.net",
)

_lock = threading.Lock()
_resolver = None


def set_resolver(fn):
    """fn(url) -> HostProfile 或 None（None 表示走内置规则）。传 None 取消。"""
    global _resolver
    with _lock:
        _resolver = fn


def host_of(url):
    try:
        return (urlsplit(url).hostname or "").lower()
    except Exception:
        return ""


def _match(host, pat):
    return host == pat or fnmatch.fnmatch(host, pat)


def resolve(url, override=None):
    """返回该 URL 的 HostProfile；override 是调用方显式给的 dict（start_conn/max_conn/min_split）。"""
    prof = None
    fn = _resolver
    if fn is not None:
        try:
            prof = fn(url)
        except Exception:
            prof = None
    if prof is None:
        h = host_of(url)
        if any(_match(h, p) for p in FAST_HOSTS):
            prof = HostProfile(name="快主机", max_conn=FAST_MAX)
        else:
            prof = HostProfile()
    if override:
        kw = {k: v for k, v in override.items() if k in ("start_conn", "max_conn", "min_split", "name") and v}
        prof = replace(prof, **kw)
        if "max_conn" in kw and "start_conn" not in kw:
            prof = replace(prof, start_conn=min(prof.start_conn, prof.max_conn))
    return prof.clamp()


def from_netdisk_rule(rule, cfg=None, global_conn=None):
    """把 SniffDL netdisk_rules 的规则 dict 转成 HostProfile（可选辅助，不依赖 SniffDL）。

    rule 形如 {"name": "某网盘", "conn": 4, "min_split": "1M", ...}；rule 为 None 返回 None。
    """
    if not rule:
        return None
    n = rule.get("conn") or DEFAULT_MAX
    try:
        ov = ((cfg or {}).get("netdisk_conn") or {}).get(rule.get("key"))
        if ov:
            n = int(ov)
    except Exception:
        pass
    if global_conn:
        n = min(int(n), int(global_conn))
    return HostProfile(name=rule.get("name") or "网盘", start_conn=min(DEFAULT_START, int(n)),
                       max_conn=int(n), min_split=parse_size(rule.get("min_split") or MIN_SPLIT))


def parse_size(v):
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip().upper()
    mul = 1
    for suf, m in (("K", 1024), ("M", 1024 ** 2), ("G", 1024 ** 3)):
        if s.endswith(suf):
            s, mul = s[:-1], m
            break
    return int(float(s) * mul)
