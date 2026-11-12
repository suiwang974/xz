# -*- coding: utf-8 -*-
"""网盘规则（netdisk profile 表）+ 外部推送请求头处理 + 直链过期解析 + 失败原因翻译。

定位（主人 2026-11-06 拍板）：SniffDL **不做网盘解析、不登录网盘**。
解析交给 LinkSwift / 网盘直链下载助手 这类油猴脚本（它们按 aria2 JSON-RPC 推给我们，
或者用户在浏览器里点下载被扩展接管 / 手动粘贴直链）。本模块只负责"把直链像 IDM 一样稳稳下完"：
  1. 推送方给的请求头一字不改（UA / Cookie / Referer 绝不覆盖），只在**缺**的时候按规则补默认值；
  2. 每个网盘一行规则：用几条连接、是否走 aria2 多连接、缺 UA/Referer 时补什么、Cookie 去哪个域取；
  3. 解析签名直链里的到期时间（夸克/UC/阿里 CDN 的 auth_key、OSS/S3 的 Expires…），
     过期直接告诉用户「链接已过期，请回网盘重新点下载」，不再拿过期链接去撞 403；
  4. 把 403/412/404/429… 翻译成用户看得懂的中文原因，写进任务列表（不再静默失败）。

纯标准库、无界面依赖，可单测：python tools/test_netdisk_rules.py
规则出处（UA / Referer / 连接数）见 docs/netdisk_rules.md。
"""
import re
import time
from urllib.parse import urlparse, parse_qs

# ---------------------------------------------------------------------------
# 网盘规则表
#   key           规则名
#   name          中文名（日志/界面用）
#   dl_hosts      下载 CDN 域名后缀（命中=按本规则下载）
#   cookie_domain 缺 Cookie 时去哪个域取浏览器 Cookie（扩展同步 / cookies.txt / 本机浏览器）
#   referer       缺 Referer 时补的默认值（None=不补）
#   default_ua    缺 UA 时补的默认值（None=用程序默认 Chrome UA）；推送方给了 UA 一律不动
#   conn          默认连接数（aria2 max-connection-per-server/split，aria2 硬顶 16）
#   min_split     aria2 min-split-size（分段最小粒度）
#   use_aria2     是否走 aria2 多连接（False=只用单连接兜底，目前没有网盘需要 False）
#   need_cookie   没 Cookie 基本必失败（用于失败原因判断 + 是否去本机浏览器捞 Cookie）
#   expiry        过期参数解析方式（见 parse_expiry）
#   tips          失败时给用户的补充提示
# ---------------------------------------------------------------------------
PROFILES = [
    {"key": "quark", "name": "夸克网盘",
     "dl_hosts": ("drive.quark.cn", "quark.cn"),
     "cookie_domain": "quark.cn", "referer": "https://pan.quark.cn/", "default_ua": None,
     "conn": 16, "min_split": "1M", "use_aria2": True, "need_cookie": True, "expiry": "auth_key",
     "tips": "夸克直链与生成它时的登录 Cookie 绑定：请保持网盘网页已登录，链接取到后尽快下"},
    {"key": "uc", "name": "UC网盘",
     "dl_hosts": ("drive.uc.cn", "uc.cn"),
     "cookie_domain": "uc.cn", "referer": "https://drive.uc.cn/", "default_ua": None,
     "conn": 16, "min_split": "1M", "use_aria2": True, "need_cookie": True, "expiry": "auth_key",
     "tips": "UC 直链与登录 Cookie 绑定，请保持网页已登录"},
    {"key": "baidu", "name": "百度网盘",
     "dl_hosts": ("d.pcs.baidu.com", "pcs.baidu.com", "baidupcs.com", "bcebos.com"),
     "cookie_domain": "baidu.com", "referer": None, "default_ua": "pan.baidu.com",
     "conn": 16, "min_split": "10M", "use_aria2": True, "need_cookie": False, "expiry": "baidu",
     "tips": "百度直链只认解析工具指定的 UA（常见 pan.baidu.com / netdisk / LogStatistic），"
             "403 多半是 UA 不对或链接过期；非 SVIP 账号服务端本身限速"},
    {"key": "aliyun", "name": "阿里云盘",
     "dl_hosts": ("aliyundrive.net", "alicloudccp.com", "alipan.com", "aliyundrive.com"),
     "cookie_domain": None, "referer": "https://www.alipan.com/", "default_ua": None,
     "conn": 10, "min_split": "1M", "use_aria2": True, "need_cookie": False, "expiry": "oss",
     "tips": "阿里云盘直链必须带 Referer: https://www.alipan.com/ ，有效期约 4 小时"},
    {"key": "123pan", "name": "123云盘",
     "dl_hosts": ("123pan.com", "123pan.cn", "123957.com", "123295.com", "123684.com",
                  "123865.com", "123912.com", "123952.com"),
     "cookie_domain": None, "referer": "https://yun.123pan.com/", "default_ua": None,
     "conn": 8, "min_split": "1M", "use_aria2": True, "need_cookie": False, "expiry": "generic",
     "tips": "123 云盘直链需 Referer: https://yun.123pan.com/"},
    {"key": "xunlei", "name": "迅雷云盘",
     "dl_hosts": ("xunlei.com", "xlcdn.com", "mypikpak.com"),
     "cookie_domain": None, "referer": None,
     "default_ua": "Dalvik/2.1.0 (Linux; U; Android 12; M2004J7AC Build/SP1A.210812.016)",
     "conn": 8, "min_split": "1M", "use_aria2": True, "need_cookie": False, "expiry": "generic",
     "tips": "迅雷云盘直链与取链时的客户端 UA 绑定，请使用解析脚本给的 UA"},
    {"key": "lanzou", "name": "蓝奏云",
     "dl_hosts": ("lanzoug.com", "lanzouc.com", "lanzoux.com", "lanzoui.com", "lanzouo.com",
                  "lanzouw.com", "lanzoue.com", "lanzouj.com", "lanzoum.com", "lanzouv.com",
                  "lanzout.com", "woozooo.com"),
     "cookie_domain": None, "referer": "https://pc.woozooo.com/", "default_ua": None,
     "conn": 4, "min_split": "1M", "use_aria2": True, "need_cookie": False, "expiry": "generic",
     "tips": "蓝奏云直链短时有效，失败请回分享页重新获取"},
    {"key": "115", "name": "115网盘",
     "dl_hosts": ("115cdn.net", "115.com", "115cdn.com"),
     "cookie_domain": "115.com", "referer": "https://115.com/", "default_ua": None,
     "conn": 4, "min_split": "1M", "use_aria2": True, "need_cookie": True, "expiry": "t115",
     "tips": "115 直链与取链时的 UA + Cookie 绑定，必须原样使用；连接数过多容易 403"},
    {"key": "189", "name": "天翼云盘",
     "dl_hosts": ("ctyunxs.cn", "cloud.189.cn", "189.cn"),
     "cookie_domain": None, "referer": "https://cloud.189.cn/", "default_ua": None,
     "conn": 8, "min_split": "1M", "use_aria2": True, "need_cookie": False, "expiry": "generic",
     "tips": "天翼云盘直链短时有效"},
]

ARIA2_CONN_MAX = 16          # aria2 1.37 max-connection-per-server 硬顶

# 浏览器 / aria2 传输层头：这些交给下载引擎自己管，绝不转发（会让多连接 / 续传错乱）
HOP_HEADERS = frozenset(("host", "content-length", "connection", "range", "transfer-encoding",
                         "accept-encoding", "keep-alive", "proxy-connection", "te", "upgrade",
                         "if-range", "if-modified-since", "if-none-match"))


def host_of(url):
    try:
        return (urlparse(url or "").hostname or "").lower()
    except Exception:
        return ""


def _host_match(host, suffix):
    return host == suffix or host.endswith("." + suffix)


def match_profile(url):
    """按下载域名找网盘规则；不是网盘返回 None。"""
    h = host_of(url)
    if not h:
        return None
    for p in PROFILES:
        if any(_host_match(h, s) for s in p["dl_hosts"]):
            return p
    return None


def profile_conn(profile, cfg=None, global_conn=16):
    """该规则的有效连接数：cfg['netdisk_conn'][key] 可覆盖；不超过 aria2 硬顶与全局设置。"""
    n = profile["conn"] if profile else global_conn
    try:
        ov = ((cfg or {}).get("netdisk_conn") or {}).get(profile["key"]) if profile else None
        if ov:
            n = int(ov)
    except Exception:
        pass
    try:
        n = min(int(n), int(global_conn) if global_conn else ARIA2_CONN_MAX)
    except Exception:
        pass
    return max(1, min(n, ARIA2_CONN_MAX))


# ---------------------------------------------------------------------------
# 请求头：解析 / 合并（推送方优先，绝不覆盖）
# ---------------------------------------------------------------------------
def hget(headers, name):
    """大小写不敏感取头。"""
    low = name.lower()
    for k, v in (headers or {}).items():
        if str(k).lower() == low:
            return v
    return None


def hset_default(headers, name, value):
    """只在缺这个头（大小写不敏感）时补上；已有就不动。返回是否补了。"""
    if value is None or value == "":
        return False
    if hget(headers, name) not in (None, ""):
        return False
    headers[name] = value
    return True


def parse_header_lines(raw):
    """aria2 header 选项 → dict。兼容：list['K: V'] / 单个字符串（可多行）/ dict。
    同名头保留第一个（大小写不敏感），空值头丢弃。"""
    out = {}

    def _put(k, v):
        k = str(k).strip()
        v = str(v).strip()
        if not k or not v:
            return
        if hget(out, k) is not None:
            return
        out[k] = v

    if isinstance(raw, dict):
        for k, v in raw.items():
            _put(k, v)
    elif isinstance(raw, (list, tuple)):
        for item in raw:
            s = str(item)
            if ":" in s:
                k, v = s.split(":", 1)
                _put(k, v)
    elif isinstance(raw, str):
        for line in re.split(r"[\r\n]+", raw):
            if ":" in line:
                k, v = line.split(":", 1)
                _put(k, v)
    return out


def headers_from_aria2_options(options):
    """aria2.addUri 的 options → 请求头 dict（推送方给什么就是什么）。
    header[] 优先；`user-agent` / `referer` 是 aria2 的独立选项（LinkSwift 两处都给），header 里没有时才用。"""
    options = options or {}
    h = parse_header_lines(options.get("header"))
    ua = options.get("user-agent") or options.get("userAgent")
    ref = options.get("referer") or options.get("referrer")
    if ua:
        hset_default(h, "User-Agent", str(ua).strip())
    if ref and str(ref).strip() not in ("*",):
        hset_default(h, "Referer", str(ref).strip())
    return h


def parse_custom_headers(text):
    """「自定义请求头」输入框（IDM/Motrix 同款）：每行 `Key: Value`；也能直接粘 curl 命令里的 -H '...'
    / --header "..." / -A UA / -e Referer / -b Cookie。返回 dict。"""
    text = (text or "").strip()
    if not text:
        return {}
    out = {}
    if re.search(r"(^|\s)(-H|--header|-A|--user-agent|-e|--referer|-b|--cookie)\s", text):
        for m in re.finditer(r"(?:-H|--header)\s+(['\"])(.*?)\1", text, re.S):
            line = m.group(2)
            if ":" in line:
                k, v = line.split(":", 1)
                hset_default(out, k.strip(), v.strip())
        for flag, name in ((r"-A|--user-agent", "User-Agent"), (r"-e|--referer", "Referer"),
                           (r"-b|--cookie", "Cookie")):
            m = re.search(r"(?:^|\s)(?:%s)\s+(['\"])(.*?)\1" % flag, text, re.S)
            if m:
                hset_default(out, name, m.group(2).strip())
        return out
    return parse_header_lines(text)


def finalize_headers(url, headers, app_ua=None):
    """下载前最终请求头：推送方/浏览器给的原样保留，只按网盘规则补"缺"的 UA / Referer。
    返回 (headers, profile, added)；added 是补了哪些头（写日志用，证明没改推送方的头）。"""
    h = {}
    for k, v in (headers or {}).items():
        if v in (None, ""):
            continue
        if str(k).lower() in HOP_HEADERS:
            continue
        h[k] = v
    prof = match_profile(url)
    added = []
    if prof:
        if hset_default(h, "Referer", prof.get("referer")):
            added.append("Referer")
        if hset_default(h, "User-Agent", prof.get("default_ua")):
            added.append("User-Agent")
    if hset_default(h, "User-Agent", app_ua):
        added.append("User-Agent")
    return h, prof, added


def cookie_names(cookie):
    return sorted({kv.split("=")[0].strip() for kv in (cookie or "").split(";") if kv.strip()})


# ---------------------------------------------------------------------------
# 浏览器 Cookie 仓（扩展把网盘域的 Cookie 同步过来；手动粘贴直链时按域取，IDM 浏览器集成同款）
# ---------------------------------------------------------------------------
class CookieJarLite:
    """内存 Cookie 仓：只存扩展推来的 Cookie（含 httpOnly），按 RFC 6265 域/路径/过期规则匹配。"""

    def __init__(self):
        self._c = {}          # (domain, path, name) -> dict

    def update(self, cookies, now=None):
        now = now or time.time()
        n = 0
        for ck in cookies or []:
            try:
                name = str(ck.get("name") or "")
                if not name:
                    continue
                dom = str(ck.get("domain") or "").lower()
                host_only = bool(ck.get("hostOnly")) or not dom.startswith(".")
                dom = dom.lstrip(".")
                path = str(ck.get("path") or "/")
                exp = ck.get("expirationDate")
                key = (dom, path, name)
                if exp and float(exp) < now:
                    self._c.pop(key, None)
                    continue
                self._c[key] = {"name": name, "value": str(ck.get("value") or ""),
                                "domain": dom, "hostOnly": host_only, "path": path,
                                "secure": bool(ck.get("secure")),
                                "exp": float(exp) if exp else None}
                n += 1
            except Exception:
                continue
        return n

    def header_for(self, url, now=None):
        now = now or time.time()
        try:
            pr = urlparse(url or "")
        except Exception:
            return ""
        host = (pr.hostname or "").lower()
        path = pr.path or "/"
        https = (pr.scheme or "").lower() == "https"
        picked = {}
        for c in sorted(self._c.values(), key=lambda c: -len(c["path"])):
            if c["exp"] and c["exp"] < now:
                continue
            if c["hostOnly"]:
                if host != c["domain"]:
                    continue
            elif not _host_match(host, c["domain"]):
                continue
            if not path.startswith(c["path"].rstrip("/") or "/"):
                continue
            if c["secure"] and not https:
                continue
            picked.setdefault(c["name"], c["value"])
        return "; ".join("%s=%s" % kv for kv in picked.items())

    def __len__(self):
        return len(self._c)


# ---------------------------------------------------------------------------
# 签名直链过期解析
# ---------------------------------------------------------------------------
_DUR = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _ts(v):
    try:
        v = str(v).strip()
        if re.fullmatch(r"\d{10}", v):
            return int(v)
        if re.fullmatch(r"\d{13}", v):
            return int(v) // 1000
    except Exception:
        pass
    return None


def parse_expiry(url):
    """从签名直链里读到期时间。返回 (到期 unix 秒 或 None, 依据说明)。只认有把握的格式，宁缺毋滥。
      · 阿里 CDN A 型鉴权 auth_key=<到期ts>-<rand>-<uid/ttl>-<md5>（夸克/UC：实测第 1 段=生成时刻+21600）
      · 百度 PCS：expires=8h + time=<签发ts>；或 expires=<10 位 ts>
      · OSS V1 / S3 V2：Expires=<10 位 ts>；OSS V4：x-oss-date + x-oss-expires(秒)；S3 V4：X-Amz-Date + X-Amz-Expires
      · 阿里云盘：x-oss-expires=<10 位 ts>
      · 115：t=<到期 ts>（仅 115 规则下认）"""
    try:
        pr = urlparse(url or "")
        q = {k.lower(): v[0] for k, v in parse_qs(pr.query, keep_blank_values=True).items() if v}
    except Exception:
        return None, ""
    prof = match_profile(url)
    ak = q.get("auth_key")
    if ak:
        m = re.match(r"^(\d{10})-", ak)
        if m:
            if prof and prof["key"] in ("quark", "uc"):
                return int(m.group(1)), "auth_key"
            # 其它用阿里 CDN A 型鉴权的站：第 1 段按阿里文档是"时间戳"，实际失效 = 时间戳 + 控制台配置的
            # 有效时长（默认 1800s）。不知道人家配了多久 → 保守按 +1800s 推断，宁可晚报过期也不误杀。
            return int(m.group(1)) + 1800, "auth_key(推断+1800s)"
    exp = q.get("expires")
    if exp:
        t = _ts(exp)
        if t:
            return t, "expires"
        m = re.fullmatch(r"(\d+)([smhd])", exp.strip().lower())
        base = _ts(q.get("time") or q.get("ts") or "")
        if m and base:
            return base + int(m.group(1)) * _DUR[m.group(2)], "expires+time"
    xo = q.get("x-oss-expires")
    if xo:
        t = _ts(xo)
        if t:
            return t, "x-oss-expires"
        d = q.get("x-oss-date")
        if d and xo.isdigit():
            try:
                base = int(time.mktime(time.strptime(d, "%Y%m%dT%H%M%SZ"))) - time.timezone
                return base + int(xo), "x-oss-date+expires"
            except Exception:
                pass
    xa = q.get("x-amz-expires")
    xd = q.get("x-amz-date")
    if xa and xd and xa.isdigit():
        try:
            import calendar
            base = calendar.timegm(time.strptime(xd, "%Y%m%dT%H%M%SZ"))
            return base + int(xa), "x-amz"
        except Exception:
            pass
    if prof and prof["expiry"] == "t115":
        t = _ts(q.get("t") or "")
        if t:
            return t, "t(115)"
    return None, ""


def expiry_state(url, now=None, grace=60):
    """返回 dict(expired=bool|None, expire_ts, left=秒, how)。grace 容忍本机时钟误差。"""
    now = now or time.time()
    ts, how = parse_expiry(url)
    if not ts:
        return {"expired": None, "expire_ts": None, "left": None, "how": ""}
    left = ts - now
    return {"expired": left < -grace, "expire_ts": ts, "left": int(left), "how": how}


def fmt_ts(ts):
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(ts))
    except Exception:
        return str(ts)


# ---------------------------------------------------------------------------
# 失败原因翻译
# ---------------------------------------------------------------------------
def http_status_from_error(msg):
    """从 aria2 errorMessage（'...status=412'）/ requests（'HTTP Error 412' / '412 Client Error'）抽 HTTP 状态码。"""
    s = str(msg or "")
    for pat in (r"status=(\d{3})", r"HTTP Error (\d{3})", r"\b(\d{3}) (?:Client|Server) Error",
                r"HTTP (\d{3})\b", r"response code (\d{3})"):
        m = re.search(pat, s)
        if m:
            return int(m.group(1))
    return None


def explain_failure(url, status=None, headers=None, raw="", now=None):
    """把下载失败翻译成一句中文原因（写进任务 errorMessage，界面直接显示）。"""
    prof = match_profile(url)
    name = prof["name"] if prof else "该站"
    ck = hget(headers or {}, "Cookie") or ""
    ex = expiry_state(url, now=now)
    tip = (" ｜" + prof["tips"]) if prof and prof.get("tips") else ""
    if status is None:
        status = http_status_from_error(raw)
    if ex["expired"]:
        return ("链接已过期（签名到期 %s），请回网盘页面重新点下载 / 让脚本重新解析后再推送（HTTP %s）"
                % (fmt_ts(ex["expire_ts"]), status or "-"))
    if status == 412:
        if not ck:
            return ("缺登录 Cookie（HTTP 412）：%s要求登录态，但推送方/浏览器没带 Cookie。"
                    "请确认网盘网页已登录，用浏览器扩展接管或让脚本携带 Cookie%s" % (name, tip))
        return ("登录 Cookie 无效或与取链时不一致（HTTP 412）：请刷新网盘网页后重新获取链接%s" % tip)
    if status == 403:
        if prof and prof["key"] == "baidu":
            return ("被拒（HTTP 403）：UA 不对或链接已过期——百度直链必须用解析工具指定的 UA"
                    "（当前 UA=%s）%s" % ((hget(headers or {}, "User-Agent") or "无")[:40], tip))
        if prof and prof.get("need_cookie") and not ck:
            return "被拒（HTTP 403）：缺登录 Cookie%s" % tip
        return ("被拒（HTTP 403）：链接可能已过期，或 UA/Cookie/IP 与取链时不一致，请重新获取直链%s" % tip)
    if status in (401,):
        return "需要鉴权（HTTP 401）：链接要求账号授权，请重新获取直链"
    if status in (404, 410):
        return "文件不存在或链接已失效（HTTP %d），请重新获取直链" % status
    if status in (429, 503, 509):
        return ("服务器限流（HTTP %d）：连接数太多或请求太频繁，稍后点继续重试；"
                "可在 config.json 的 netdisk_conn 调小该网盘连接数" % status)
    if status == 416:
        return "服务器不接受分段请求（HTTP 416），请点继续重试（将从头下载）"
    if status:
        return "下载被拒（HTTP %d）%s" % (status, tip)
    s = str(raw or "")
    low = s.lower()
    if "timed out" in low or "timeout" in low:
        return "连接超时：网络不通或服务器无响应（%s）" % s[:80]
    if "name resolution" in low or "resolve" in low or "getaddrinfo" in low:
        return "域名解析失败：请检查网络/代理（%s）" % s[:80]
    if "refused" in low:
        return "连接被拒：服务器拒绝连接（%s）" % s[:80]
    return s[:200] or "下载失败（原因未知，详见 sniffdl.log）"


# ---------------------------------------------------------------------------
# aria2 HTTP 引擎参数（IDM/Motrix/Gopeed 实践）
# ---------------------------------------------------------------------------
def aria2_http_task_opts(conn, min_split="1M", is_windows=False):
    """单任务 addUri 选项（多连接 + 稳健重试）。全局项（disk-cache/async-dns…）见 aria2_http_daemon_args。"""
    conn = str(max(1, min(int(conn), ARIA2_CONN_MAX)))
    return {
        "split": conn, "max-connection-per-server": conn, "min-split-size": min_split,
        # NTFS 上 falloc=SetFileValidData 级的瞬时预分配，减少碎片；其它平台 none（不阻塞开局）
        "file-allocation": "falloc" if is_windows else "none",
        "continue": "true",
        # 原来 max-tries=2/timeout=20：一次瞬断整条任务就失败、掉回单连接。改成 IDM 式"多试几次"
        "max-tries": "5", "retry-wait": "2",
        "connect-timeout": "10", "timeout": "30",
        "max-file-not-found": "2",
        "content-disposition-default-utf8": "true",   # 中文文件名
        "auto-file-renaming": "false", "allow-overwrite": "true",
        "remote-time": "false",
    }


def aria2_http_daemon_args(is_windows=False):
    """HTTP 专用 aria2c 守护进程的全局参数（与 BT 守护进程分开，互不排队）。"""
    a = ["--max-concurrent-downloads=16",
         "--disk-cache=64M",                  # 64MB 写缓存：多连接小块写盘合并，HDD 上收益明显
         "--enable-http-keep-alive=true",
         "--enable-http-pipelining=false",    # 管线化很多 CDN 不兼容，保持关闭
         "--optimize-concurrent-downloads=false",
         "--max-overall-download-limit=0",
         "--summary-interval=0", "--console-log-level=warn", "--log-level=warn",
         "--auto-save-interval=10",
         "--uri-selector=feedback",
         "--stream-piece-selector=default"]
    if is_windows:
        a.append("--async-dns=false")         # Windows 上 c-ares 异步 DNS 偶发解析失败，用系统解析器
    return a
