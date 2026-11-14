# AI生成
"""SniffDL — 网页视频嗅探下载器
真实浏览器(Playwright Chromium)网络层嗅探 + yt-dlp 下载合并，1DM 同款思路。
排错系统：GUI 滚动日志面板 + sniffdl.log 文件日志 + 崩溃自动记录 + 启动自检。
"""
import os
import re
import sys
import json
import math
import time
import queue
import shutil
import tempfile
import hashlib
import unicodedata
import subprocess
import logging
import asyncio
import threading
import functools
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin

import urllib.request
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox

# 用户目录里被"自更新"装上的新版组件优先于内置版（必须在 import yt_dlp 之前生效）
try:
    import component_updates as CU
    CU.ensure_path()
except Exception:
    CU = None

import yt_dlp
import requests
from requests.adapters import HTTPAdapter, Retry

# 本地下载 RPC 服务（把 SniffDL 当 aria2 用）：缺模块也不许拖垮启动
try:
    import rpc_server as RPC
except Exception:
    RPC = None

# 网盘规则表 / 推送请求头处理 / 直链过期解析 / 失败原因翻译（纯标准库，缺了也不拖垮启动）
try:
    import netdisk_rules as ND
except Exception:
    ND = None

# Cloudflare/反爬：装了 curl_cffi 才让 yt-dlp 用 Chrome TLS 指纹伪装（iwara 等站必需），
# 否则报 "no impersonate target available" 直接 403。缺依赖时置 None，绝不传无效选项。
# 注意：yt-dlp 的 Python API 不接受字符串 'chrome'（只有 CLI 会解析），必须传 ImpersonateTarget 对象。
try:
    import curl_cffi as _curl_cffi  # noqa: F401
    from yt_dlp.networking.impersonate import ImpersonateTarget as _ImpTarget
    _YDL_IMPERSONATE = _ImpTarget.from_str("chrome")
except Exception:
    _YDL_IMPERSONATE = None

# ---------------- 登录态 cookies（B站等匿名 playurl 会返回空格式表的大站）----------------
COOKIE_HOSTS = ("bilibili.com", "b23.tv", "youtube.com", "youtu.be",
                "twitter.com", "x.com", "netflix.com")
# yt-dlp 内部浏览器名 → Windows 上 cookie 库相对 LOCALAPPDATA/APPDATA 的存在性探测
_BROWSER_COOKIE_DB = {
    "chrome":  (r"Google\Chrome\User Data\Default\Network\Cookies", "local"),
    "edge":    (r"Microsoft\Edge\User Data\Default\Network\Cookies", "local"),
    "brave":   (r"BraveSoftware\Brave-Browser\User Data\Default\Network\Cookies", "local"),
    "vivaldi": (r"Vivaldi\User Data\Default\Network\Cookies", "local"),
    "opera":   (r"Opera Software\Opera Stable\Network\Cookies", "local"),
}

_COOKIE_SETTING = "自动"    # cfg.cookie_browser: 自动 / 已登录Cookie / 浏览器 / Chrome / Edge / 不使用
_COOKIE_DEAD = set()        # 本机确认读不出 cookie 的浏览器（DPAPI/库锁失败），后续不再反复尝试
_COOKIE_VALIDATED = None    # 探测阶段确证有效的 cookie 选项 dict（{}=匿名也算有效）
_COOKIE_VALIDATED_READY = False

# 兼容旧 UI 判断
_LEGACY_NONE = ("不使用", "无", "none", "off", "")
_LEGACY_AUTO = ("自动", "auto")
_MODE_FILE = ("已登录Cookie", "登录Cookie", "登录cookie", "cookiefile", "cookie文件", "文件")
_MODE_BROWSER = ("浏览器", "browser")


def _detect_installed_browsers():
    """返回本机存在 cookie 库、且 yt-dlp 认识的浏览器名列表（常见优先级序）。"""
    la = os.environ.get("LOCALAPPDATA", "")
    out = []
    for name in ("chrome", "edge", "brave", "vivaldi", "opera"):
        rel, _where = _BROWSER_COOKIE_DB[name]
        if la and os.path.isfile(os.path.join(la, rel)):
            out.append(name)
    ra = os.environ.get("APPDATA", "")
    if ra:
        try:
            import glob as _g
            if _g.glob(os.path.join(ra, "Mozilla", "Firefox", "Profiles",
                                    "*", "cookies.sqlite")):
                out.append("firefox")
        except Exception:
            pass
    return out


def _browser_from_setting(s):
    s = (s or "自动").strip()
    if s in _LEGACY_NONE:
        return "none"
    if s in _LEGACY_AUTO:
        return "auto"
    if s in _MODE_FILE:
        return "file"
    if s in _MODE_BROWSER:
        return "browser"
    return s.lower()


def _app_cookie_file():
    """应用自有的 Netscape cookie 文件（登录窗/导入都写这里）。懒解析 BASE_DIR。"""
    return os.path.join(BASE_DIR, "cookies.txt")


def _has_app_cookie_file():
    try:
        p = _app_cookie_file()
        return os.path.isfile(p) and os.path.getsize(p) > 0
    except Exception:
        return False


def _cookie_mode():
    s = (_COOKIE_SETTING or "自动").strip()
    if s in _LEGACY_NONE:
        return "none"
    if s in _LEGACY_AUTO:
        return "auto"
    if s in _MODE_FILE:
        return "file"
    if s in _MODE_BROWSER:
        return "browser"
    return "browser" if s.lower() not in ("", "auto") else "auto"


def _browser_setting_name():
    """当前设置里钉死的浏览器名（没有则 None）。"""
    s = (_COOKIE_SETTING or "").strip().lower()
    if s in _LEGACY_NONE or s in _LEGACY_AUTO or s in _MODE_FILE or s in _MODE_BROWSER:
        return None
    return s or None


def _alive_browser_candidates():
    """当前设置下、且本机确认还能读出的浏览器 cookie 候选（元组列表，如 [('chrome',)]）。"""
    mode = _cookie_mode()
    if mode in ("none", "file"):
        return []
    if mode == "browser":
        pinned = _browser_setting_name()
        bs = [pinned] if pinned else _detect_installed_browsers()
    else:  # auto
        bs = _detect_installed_browsers()
    return [(b,) for b in bs if b not in _COOKIE_DEAD and b]


def _probe_cookie_opts():
    """探测阶段依次尝试的 cookie 选项 dict 列表（合并进 yt-dlp opts）。
    匿名 {} 永远最先——单视频/直链/公开站据此秒级命中、零 cookie 噪声；
    只有匿名返回空格式表（典型是需登录大站）才继续试「已登录 cookie 文件」
    与「仍可读的浏览器」。"""
    mode = _cookie_mode()
    opts = [{}]
    if mode == "none":
        return opts
    if mode in ("auto", "file") and _has_app_cookie_file():
        opts.append({"cookiefile": _app_cookie_file()})
    if mode in ("auto", "browser"):
        for cok in _alive_browser_candidates():
            opts.append({"cookiesfrombrowser": cok})
    return opts


def _download_cookie_opts():
    """下载阶段实际使用的 cookie 选项 dict（单一来源）。
    自动模式优先复用探测确证过的那份（含匿名 {}）；钉选来源按其语义；不使用→匿名。"""
    global _COOKIE_VALIDATED, _COOKIE_VALIDATED_READY
    mode = _cookie_mode()
    if mode == "none":
        return {}
    if mode == "file":
        return {"cookiefile": _app_cookie_file()} if _has_app_cookie_file() else {}
    if mode == "browser":
        coks = _alive_browser_candidates()
        if not coks:
            return {}
        pinned = _browser_setting_name()
        if pinned:
            return ({"cookiesfrombrowser": (pinned,)}
                    if pinned not in _COOKIE_DEAD else {})
        return {"cookiesfrombrowser": coks[0]}
    # auto：用探测验证过的来源；没验证过就匿名
    if _COOKIE_VALIDATED_READY and _COOKIE_VALIDATED is not None:
        return dict(_COOKIE_VALIDATED)
    return {}


def remember_validated_cookie(opt):
    """探测命中后记下真正有效的 cookie 来源，供同会话下载复用。opt 是 dict。"""
    global _COOKIE_VALIDATED, _COOKIE_VALIDATED_READY
    _COOKIE_VALIDATED = dict(opt) if opt else {}
    _COOKIE_VALIDATED_READY = True


def mark_cookie_dead(name):
    """某浏览器 cookie 读取失败（DPAPI/库锁），本机记住，后续不再反复尝试。"""
    try:
        if name:
            _COOKIE_DEAD.add(name)
    except Exception:
        pass


def set_cookie_setting(v):
    global _COOKIE_SETTING, _COOKIE_VALIDATED, _COOKIE_VALIDATED_READY
    _COOKIE_SETTING = v or "自动"
    _COOKIE_VALIDATED, _COOKIE_VALIDATED_READY = None, False
    _COOKIE_DEAD.clear()          # 用户主动换来源 → 重新给每个浏览器一次机会
    return _download_cookie_opts()


# 兼容旧调用点：返回下载阶段应附带的 cookiesfrombrowser 元组（没有则 None）
def cookie_opt_for_download():
    d = _download_cookie_opts()
    return d.get("cookiesfrombrowser")


# ---------------- 应用自有 Netscape cookie 文件（登录窗 / 导入都写这里）----------------
def _normalize_cookie(domain, path, secure, expires, name, value):
    dom = (domain or "").lstrip("#").strip()
    return {
        "domain": dom,
        "sub": dom.startswith("."),
        "path": path or "/",
        "secure": bool(secure),
        "expires": int(expires) if (expires and float(expires) > 0) else 0,
        "name": name or "",
        "value": "" if value is None else str(value),
    }


def _parse_netscape(text):
    out = []
    for ln in (text or "").splitlines():
        ln = ln.rstrip("\r\n")
        if not ln.strip():
            continue
        parts = ln.split("\t")
        if len(parts) < 7:
            continue
        dom, flag, path, secure, exp, name, value = parts[:7]
        out.append(_normalize_cookie(
            dom, path, secure.strip().upper() == "TRUE",
            exp, name, value))
    return out


def _dump_netscape(cookies):
    lines = ["# Netscape HTTP Cookie File",
             "# 由 SniffDL 生成：登录窗抓取或手动导入的登录态，勿分发他人",
             "#This is a machine-generated cookies.txt file", ""]
    for c in cookies:
        flag = "TRUE" if c["sub"] else "FALSE"
        secure = "TRUE" if c["secure"] else "FALSE"
        lines.append("\t".join([
            c["domain"], flag, c["path"], secure, str(c["expires"]),
            c["name"], c["value"]]))
    return "\n".join(lines) + "\n"


def read_app_cookies():
    try:
        p = _app_cookie_file()
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                return _parse_netscape(f.read())
    except Exception:
        pass
    return []


def merge_app_cookies(incoming, log=None, source="pw"):
    """把 Playwright cookie 列表或已规范化的 dict 合并进 cookies.txt，按
    (domain,path,name) 去重覆盖；返回合并后总条数。"""
    try:
        base = {(c["domain"], c["path"], c["name"]): c for c in read_app_cookies()}
        added = 0
        for ck in (incoming or []):
            if "domain" in ck and "name" in ck and "sub" in ck:
                norm = ck
            else:  # Playwright 形态
                norm = _normalize_cookie(
                    ck.get("domain"), ck.get("path", "/"), ck.get("secure"),
                    ck.get("expires"), ck.get("name"), ck.get("value"))
            if not norm["domain"] or not norm["name"]:
                continue
            key = (norm["domain"], norm["path"], norm["name"])
            if key not in base:
                added += 1
            base[key] = norm
        cookies = list(base.values())
        p = _app_cookie_file()
        with open(p, "w", encoding="utf-8") as f:
            f.write(_dump_netscape(cookies))
        try:
            os.chmod(p, 0o600)
        except Exception:
            pass
        if log:
            log(f"[cookie] 已合并 {len(cookies)} 条 cookie 到 cookies.txt（本次新增 {added}）")
        return len(cookies)
    except Exception as e:
        if log:
            log(f"[cookie] 写入 cookies.txt 失败: {str(e)[:120]}")
        return 0


def import_cookie_file(src_path, log=None):
    """导入外部 Netscape cookies.txt，合并进应用 cookie 文件。返回总条数。"""
    try:
        with open(src_path, "r", encoding="utf-8", errors="ignore") as f:
            parsed = _parse_netscape(f.read())
        if not parsed:
            if log:
                log("[cookie] 该文件没解析出有效 cookie（需 Netscape cookies.txt 格式）")
            return 0
        return merge_app_cookies(parsed, log=log, source="file")
    except Exception as e:
        if log:
            log(f"[cookie] 导入失败: {str(e)[:120]}")
        return 0


def capture_login_cookies(start_url, log=None, done_event=None,
                          cancel_event=None, timeout_s=360):
    """开一个独立 headed Chromium 登录窗（持久化到 .login_profile），导航到 start_url；
    期间每 ~2 秒抓一次 cookie 合并落盘；done_event 置位/窗口关闭/取消/超时即收尾。
    须在后台线程调用（不能是 Tk 主线程）。返回保存的 cookie 总数。"""
    if not HAS_PW:
        if log:
            log("[登录窗] 未安装 Playwright，无法开登录窗；请改用「导入 cookies.txt」")
        return 0
    import time
    profile = os.path.join(BASE_DIR, ".login_profile")
    try:
        os.makedirs(profile, exist_ok=True)
    except Exception:
        pass
    saved = 0
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        if log:
            log(f"[登录窗] Playwright 不可用: {str(e)[:120]}")
        return 0
    deadline = time.time() + max(30, int(timeout_s))
    with sync_playwright() as p:
        try:
            ctx = _pw_launch_sync(p.chromium, True, 
                user_data_dir=profile, headless=False, no_viewport=True,
                args=["--disable-blink-features=AutomationControlled"])
        except Exception as e:
            if log:
                log(f"[登录窗] 启动浏览器失败: {str(e)[:140]}")
            return 0
        try:
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(start_url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                if log:
                    log(f"[登录窗] 打开页面失败（仍可在窗内手动访问）: {str(e)[:120]}")
            if log:
                log("[登录窗] 请在弹出的浏览器里登录该站点，登录成功后点「已登录·保存」")
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    break
                try:
                    saved = merge_app_cookies(ctx.cookies(), log=None, source="pw")
                except Exception:
                    break  # 上下文/窗口已关闭
                if done_event is not None and done_event.is_set():
                    break
                if time.time() > deadline:
                    if log:
                        log("[登录窗] 超时未确认，已保存当前抓取到的 cookie")
                    break
                if done_event is not None:
                    done_event.wait(2.0)
                else:
                    time.sleep(2.0)
            try:
                saved = merge_app_cookies(ctx.cookies(), log=log, source="pw")
            except Exception:
                pass
            try:
                ctx.close()
            except Exception:
                pass
        except Exception as e:
            if log:
                log(f"[登录窗] 异常: {str(e)[:140]}")
    return saved



# exe(PyInstaller)环境下强制内核指向系统安装位置，否则会错误地找 _MEI 临时目录
if getattr(sys, "frozen", False):
    _browsers = os.path.join(os.environ.get("LOCALAPPDATA", ""), "ms-playwright")
    if os.path.isdir(_browsers):
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", _browsers)

# ---- Windows：所有子进程一律不弹控制台窗口 ----
# 曾经启动时闪一堆黑窗（自检的 tasklist/powershell/taskkill + ffmpeg 合并/探测），
# 打包成 console=False 的 GUI 后，任何没加 CREATE_NO_WINDOW 的子进程都会闪一下 cmd 窗口。
if os.name == "nt":
    import subprocess as _spx

    _ORIG_RUN, _ORIG_POPEN = _spx.run, _spx.Popen

    def _hidden_run(*a, **k):
        k.setdefault("creationflags", 0x08000000)      # CREATE_NO_WINDOW
        return _ORIG_RUN(*a, **k)

    class _HiddenPopen(_ORIG_POPEN):
        def __init__(self, *a, **k):
            k.setdefault("creationflags", 0x08000000)
            _ORIG_POPEN.__init__(self, *a, **k)

    _spx.run, _spx.Popen = _hidden_run, _HiddenPopen

# 本机回环地址永不走代理：用户环境里设了 HTTP(S)_PROXY（部分代理工具/公司电脑会设）时，
# requests 会把 127.0.0.1 的请求（网盘 mock、本地 RPC、看板娘桥、本机 AList 等）也发给代理 → 假 404。
# 只追加不覆盖用户原有 NO_PROXY；aria2c 子进程继承同一环境。
def _ensure_loopback_no_proxy():
    for _k in ("NO_PROXY", "no_proxy"):
        _cur = [x.strip() for x in (os.environ.get(_k) or "").split(",") if x.strip()]
        for _h in ("127.0.0.1", "localhost", "::1"):
            if _h not in _cur:
                _cur.append(_h)
        os.environ[_k] = ",".join(_cur)


_ensure_loopback_no_proxy()

SESSION = requests.Session()
_retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
# 并发池放大: AList 目录/raw_url 并发 12 路 + 多连接分段 6 路 + 3 文件并行,
# 默认 10 会不停丢弃连接, 提速反而打折
for _scheme in ("https://", "http://"):
    SESSION.mount(_scheme, HTTPAdapter(max_retries=_retry,
                                       pool_connections=32, pool_maxsize=64))

try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG = shutil.which("ffmpeg")

try:                      # 女仆美图素材（base64 PNG，Tk 原生解码；缺失则退回矢量兜底）
    import maid_assets as MA
except Exception:
    MA = None

try:                      # 桃桃双形态动作帧（概念板裁切；仅 maid 主题使用）
    import maid_frames as MF
except Exception:
    MF = None

try:                      # 五个非桃桃主题的状态帧（idle/busy/done/fail，可选）
    import theme_frames as TF
except Exception:
    TF = None

try:                      # v2：其余五人（小夜/墨墨/莉兹/零号/薇拉）状态帧 + 逐帧时序 SEQ
    import maid_cast_frames as CF
except Exception:
    CF = None

try:                      # v2：表演调度层（Stage：enter/loop 播放 + 待机小动作 + 反应优先级）
    import maid_stage as ST
except Exception:
    ST = None

try:                      # 桃桃少女闺房素材：梦幻粉色背景 + 蝴蝶结/玩偶/草莓贴纸（base64 内嵌）
    import maid_girly as GI
except Exception:
    GI = None

try:                      # 桃桃人设台词库 + 本地短句指挥解析
    import maid_talk as MT
except Exception:
    MT = None

try:                      # 五位助手的头栏「世界画窗」：真实素材（CC0/PD，base64 PNG）
    import theme_visions as TV
except Exception:
    TV = None

try:                      # muse 交付：六人 24 帧动作模块注册表（cast24_<theme>.py 缺失则回落旧帧）
    import cast24 as C24
except Exception:
    C24 = None

try:
    from playwright.async_api import async_playwright
    HAS_PW = True
except Exception:
    HAS_PW = False

APP_TITLE = "SniffDL 视频嗅探下载器"
BASE_DIR = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__)))
LOG_FILE = os.path.join(BASE_DIR, "sniffdl.log")
DEFAULT_OUTDIR = os.path.join(os.path.expanduser("~"), "Videos", "SniffDL")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
MEDIA_EXT = (".mp4", ".m3u8", ".webm", ".flv", ".mkv", ".mov", ".avi", ".ts",
             ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wav", ".wma",
             ".m4b", ".ape")

# ---------------- 文件日志 + 崩溃钩子 ----------------
LOG = logging.getLogger("sniffdl")


def setup_logging():
    try:
        LOG.setLevel(logging.INFO)
        if not LOG.handlers:
            fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            LOG.addHandler(fh)
    except Exception:
        pass


def install_crash_hooks(notify=None):
    def hook(t, v, tb):
        msg = "".join(traceback.format_exception(t, v, tb))
        LOG.error("未捕获异常:\n%s", msg)
        if notify:
            try:
                notify(msg)
            except Exception:
                pass
        try:
            messagebox.showerror(APP_TITLE, f"程序发生错误，详情已写入:\n{LOG_FILE}\n\n{str(v)[:300]}")
        except Exception:
            pass
    sys.excepthook = hook
    threading.excepthook = lambda a: hook(a.exc_type, a.exc_value, a.exc_traceback)


def noop_log(m):
    pass


# ---------------- yt-dlp 日志适配器 ----------------
class YdlLogger:
    def __init__(self, log):
        self.log = log

    def debug(self, msg):
        if msg and not msg.startswith("[debug]"):
            self.log(f"[ytdlp] {msg}")

    def info(self, msg):
        if msg:
            self.log(f"[ytdlp] {msg}")

    def warning(self, msg):
        if msg:
            self.log(f"[ytdlp警告] {msg}")

    def error(self, msg):
        if msg:
            self.log(f"[ytdlp错误] {msg}")


# ---------------- 系统代理自动检测（YouTube 等被墙站点需走代理） ----------------
_PROXY_CACHE = {"v": None, "checked": False}


def _detect_proxy_scheme(host, port):
    import socket as _socket
    try:
        with _socket.create_connection((host, port), timeout=2) as s:
            s.settimeout(2)
            s.sendall(b"\x05\x01\x00")
            if s.recv(2)[:1] == b"\x05":
                return "socks5"
    except OSError:
        return None
    try:
        with _socket.create_connection((host, port), timeout=2) as s:
            s.settimeout(3)
            s.sendall(b"CONNECT www.youtube.com:443 HTTP/1.1\r\n"
                      b"Host: www.youtube.com\r\n\r\n")
            if s.recv(32).startswith(b"HTTP/"):
                return "http"
    except OSError:
        pass
    return None


def get_system_proxy(force=False):
    if _PROXY_CACHE["checked"] and not force:
        return _PROXY_CACHE["v"]
    _PROXY_CACHE.update(checked=True, v=None)
    env = os.environ.get("SNIFFDL_PROXY")
    if env:
        _PROXY_CACHE["v"] = env
        return env
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as k:
            enable, _ = winreg.QueryValueEx(k, "ProxyEnable")
            if not enable:
                return None
            server, _ = winreg.QueryValueEx(k, "ProxyServer")
        server = (server or "").strip()
        if not server:
            return None
        if "=" in server:
            for part in server.split(";"):
                p = part.split("=", 1)
                if len(p) == 2 and p[0].rstrip("s") == "http":
                    server = p[1]
                    break
        if server.startswith("http"):
            _PROXY_CACHE["v"] = server
            return server
        host, _, port = server.rpartition(":")
        if not host or not port.isdigit():
            return None
        scheme = _detect_proxy_scheme(host, int(port))
        if scheme:
            _PROXY_CACHE["v"] = f"{scheme}://{host}:{port}"
    except Exception:
        pass
    return _PROXY_CACHE["v"]


# IDM 式代理模式：跟随系统（默认，读系统/注册表代理）/ 不使用 / 手动（用户填一条代理）
_PROXY_MODE = "跟随系统"
_PROXY_MANUAL = ""


def set_proxy_setting(mode, manual=""):
    global _PROXY_MODE, _PROXY_MANUAL
    _PROXY_MODE = (mode or "跟随系统").strip()
    _PROXY_MANUAL = (manual or "").strip()
    # 让「跟随系统」重新读取注册表（用户可能刚开/关代理）
    _PROXY_CACHE.update(checked=False, v=None)


def effective_proxy():
    """本次应使用的代理 URL（无则 None）。统一入口，探测/下载都走它。"""
    if _PROXY_MODE == "不使用":
        return None
    if _PROXY_MODE == "手动":
        return _PROXY_MANUAL or None
    return get_system_proxy()  # 跟随系统


# ---------------- 设置持久化（OVD preferences 思路: 配置/最近目录/窗口几何） ----------------
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
HISTORY_FILE = os.path.join(BASE_DIR, "downloaded.json")

_CFG_DEFAULT = {"outdir": "", "quality": "最佳", "deep": False,
                "geometry": "1150x820", "recent_dirs": [],
                "theme": "feiyu", "after": "无",
                "dl_threads": "自动", "aria2c_path": "", "cookie_browser": "自动", "sniff_browser": "自动",
                "js_runtime_dir": "",
                "proxy_mode": "跟随系统", "proxy_manual": "",
                # 默认 = aria2 标准端口 6800、无 token：网盘直链类油猴脚本（LinkSwift / 网盘直链下载助手等）
                # 的「aria2/Motrix」推送默认就推 127.0.0.1:6800 不带 token，开箱即通、零配置
                "rpc_enable": True, "rpc_port": 6800, "rpc_token": "",
                "rpc_confirm_dialog": True,
                "rpc_categories": ["常规", "视频", "音频", "压缩包", "文档", "程序", "其他"],
                "recent_sniff": [], "rpc_last_category": "",
                "rpc_badge": True, "rpc_badge_deep": True,
                "pet_live": True, "pet_skin": "horn", "pet_port": 6801,
                "pet_wake": True, "tray_close": True, "ui_mode": "web", "ui_bg": "none",
                "rt_enable": False, "rt_base_url": "", "rt_api_key": "", "rt_model": "", "rt_instructions": "",
                "cfg_rev": 0}

# 配置迁移版本：2 = 2026-11-06 网盘直链版（RPC 端口/token 回归 aria2 默认）
_CFG_REV = 2
# 本次启动做过的配置迁移说明（App 起来后写进日志，load_config 时日志还没就绪）
_CFG_MIGRATE_NOTES = []


def _migrate_config(cfg):
    """旧版本配置 → 新默认。只动明确识别的旧测试值，用户其它设置一概不碰。返回是否改动。"""
    changed = False
    try:
        rev = int(cfg.get("cfg_rev") or 0)
    except Exception:
        rev = 0
    if rev < 2:
        # 旧版测试期把 RPC 设成了 6820 + token "tk-test"：网盘直链脚本默认推 6800 且不带 token
        # → 推送全打空/鉴权失败。仅当两项都还是那组旧测试值时才迁移（用户自己改过的保留）。
        if str(cfg.get("rpc_port")) == "6820" and str(cfg.get("rpc_token") or "") == "tk-test":
            cfg["rpc_port"] = 6800
            cfg["rpc_token"] = ""
            _CFG_MIGRATE_NOTES.append(
                "[配置迁移] RPC 端口 6820→6800、token「tk-test」→ 空（与网盘直链脚本的 aria2 默认一致，其它设置保持不变）")
            changed = True
        cfg["cfg_rev"] = _CFG_REV
        changed = True
    return changed


def load_config():
    cfg = dict(_CFG_DEFAULT)
    existed = False
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg.update({k: v for k, v in json.load(f).items()
                        if k in _CFG_DEFAULT})
        existed = True
    except Exception:
        pass
    if existed:
        try:
            if _migrate_config(cfg):
                save_config(cfg)
        except Exception:
            pass
    else:
        cfg["cfg_rev"] = _CFG_REV   # 全新安装：直接就是新默认，无需迁移
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def load_history():
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_history(h):
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(h, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


# ---- 记忆系统（IDM 式持久化下载列表）----------------------------------
# 分工：downloaded.json = "完成账本"（URL→文件名，用来判"下过的别再下一遍"）；
# queue.json = "任务库"（下载器的队列本身：排队/进行中/完成/失败/取消，含来源、
# 目标路径、字节数、原始 Referer/Cookie），叉掉重开后队列还在，靠它恢复显示。
QUEUE_FILE = os.path.join(BASE_DIR, "queue.json")
QMEM_VERSION = 1
QMEM_MAX = 800          # 任务库封顶，超了先丢最旧的已完成/失败，绝不无限膨胀
_QSAVE_LOCK = threading.Lock()    # 任务库写锁（RPC 线程与主线程心跳会同时写）
_QMEM_SEQ = 0
# CLI 自检（--qmem）用：True 时不拉起桌宠。自检跑完主进程就退，Electron 却会变孤儿
# 攥着 dist\SniffDL\pet_electron\default_app.asar → 下次重打包 PermissionError（实测踩中）。
_CLI_NO_PET = False


def load_queue_detail(trials=10, pause=0.2):
    """读任务库，返回 (rows, reason)。

    reason 三态很关键（都是真机踩出来的）：
      ""            正常读到了（rows 可能为空 = 真的没有历史）
      "missing"     文件不存在 = 第一次用，正常
      "corrupt"     JSON 坏了 = 已把坏文件留成 queue.bad-*.json 再重建
      "unreadable:*" 文件被占用读不动（Windows 共享冲突：刚重启时上一个实例句柄还没放、
                    或杀软/索引正在碰它）→ 【绝不能当成"没有历史"】，否则会把用户的队列抹掉
    """
    last = ""
    for _ in range(max(1, trials)):
        try:
            with open(QUEUE_FILE, encoding="utf-8") as f:
                d = json.load(f)
        except FileNotFoundError:
            return [], "missing"
        except json.JSONDecodeError as e:
            try:
                keep = "%s.bad-%d.json" % (QUEUE_FILE, int(time.time()))
                os.replace(QUEUE_FILE, keep)
                LOG.warning("任务库损坏，已留证据 %s：%s" % (keep, str(e)[:80]))
            except Exception:
                pass
            return [], "corrupt"
        except Exception as e:
            last = repr(e)[:80]
            time.sleep(pause)
            continue
        if isinstance(d, dict):
            rows = d.get("tasks")
            return (rows if isinstance(rows, list) else []), ""
        return (d if isinstance(d, list) else []), ""
    return [], ("unreadable:" + last if last else "unreadable")


def load_queue():
    """读回持久化任务库，返回 list[dict]。只关心条数的调用方用这个；
    需要区分"没有历史"和"读不动"的（比如开机恢复）请用 load_queue_detail。"""
    return load_queue_detail()[0]


def save_queue(rows):
    """原子写任务库：全局写锁 + 每次唯一临时名再 os.replace。

    为什么两个都要：下载中 RPC 线程（新任务/状态变更）和主线程心跳（5 秒节流）会同时写，
    共用一个 .tmp 名 → 互相踩成半截 JSON / 把新的覆盖回旧的，`load_queue` 解析失败就
    静默退化成空列表（真机 E2E 上出现过"文件里有 3 条、重启读到 0 条"）。"""
    global _QMEM_SEQ
    try:
        with _QSAVE_LOCK:
            _QMEM_SEQ += 1
            tmp = "%s.%d.%d.tmp" % (QUEUE_FILE, os.getpid(), _QMEM_SEQ)
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"version": QMEM_VERSION, "tasks": list(rows or [])},
                              f, ensure_ascii=False, indent=1)
                os.replace(tmp, QUEUE_FILE)
                return True
            finally:
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except Exception:
                    pass
    except Exception:
        return False


STATS_FILE = os.path.join(BASE_DIR, "stats.json")


def load_stats():
    """每日下载账本 {"YYYY-MM-DD": {"n": 条数, "bytes": 字节}}；异常/缺失返回 {}。"""
    try:
        with open(STATS_FILE, encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_stats(d):
    try:
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def record_download(nbytes=0):
    """在下载成功落定处调用：把当日条数 +1、字节 += nbytes。"""
    try:
        import datetime
        d = load_stats()
        today = datetime.date.today().isoformat()
        rec = d.get(today) or {"n": 0, "bytes": 0}
        rec["n"] = int(rec.get("n", 0)) + 1
        rec["bytes"] = int(rec.get("bytes", 0)) + int(max(0, nbytes or 0))
        d[today] = rec
        save_stats(d)
    except Exception:
        pass


def stats_days(ndays=112):
    """返回最近 ndays 天的 [(iso日期, bytes, n), ...]（含空日 0），用于热力图。"""
    try:
        import datetime
        d = load_stats()
        today = datetime.date.today()
        out = []
        for i in range(ndays - 1, -1, -1):
            day = (today - datetime.timedelta(days=i)).isoformat()
            rec = d.get(day) or {}
            out.append((day, int(rec.get("bytes", 0)), int(rec.get("n", 0))))
        return out
    except Exception:
        return []


def send_to_recycle(paths):
    """把存在的文件/目录送进 Windows 回收站（进程内 SHFileOperationW，一次调用、秒级，
    不再 spawn powershell，不卡 UI）。仍带 FOF_ALLOWUNDO，回收站可原样找回。
    返回 (成功移入列表, 失败列表)。"""
    todo = [os.path.abspath(p) for p in paths if p and os.path.exists(p)]
    if not todo:
        return [], [p for p in paths if p]
    import ctypes
    from ctypes import wintypes
    FO_DELETE = 3
    FOF_ALLOWUNDO = 0x0040
    FOF_NOCONFIRMATION = 0x0010
    FOF_SILENT = 0x0004
    FOF_NOERRORUI = 0x0400

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_uint16),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    try:
        ctypes.windll.ole32.CoInitializeEx(None, 0x2)  # STA
    except Exception:
        pass
    buf = "\0".join(todo) + "\0\0"
    op = SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = buf
    op.pTo = None
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    try:
        ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    except Exception:
        pass
    moved, failed = [], []
    for p in todo:
        (failed if os.path.exists(p) else moved).append(p)
    try:
        ctypes.windll.ole32.CoUninitialize()
    except Exception:
        pass
    return moved, failed


# 系统装了 aria2c 则启用外挂多线程下载（直链提速 3-10 倍; 未安装零影响）
ARIA2C = shutil.which("aria2c")

# 下载加速档位（由 UI 的「加速」下拉写入，0=自动）：
#   智能模式：m3u8/mpd 分片并发可一路拉到 64（真并行、对 CDN 友好）；
#             单文件直链受 aria2 硬顶 16 限制——超出 16 无收益反而容易触发
#             429/403/封 IP，因此档位 >16 对单文件等价 16。
DL_THREADS = 0


# ---- 嗅探用浏览器（设置「高级」：自动 / Edge / Chrome）----
# 用户日常只用 Edge 和 Chrome：深度嗅探/登录窗/慢速取片都优先用本机已装的正式浏览器
# （真浏览器指纹 + 可直接复用系统解码器），两条路径地位相同；都不在才退回 Playwright 内置 Chromium。
# 自动 = 已安装的里面，正在运行（用户在用）的优先；都在跑或都没跑 → Edge → Chrome → 内置。
SNIFF_BROWSER = ["自动"]
_LAST_BROWSER = [""]          # 最近一次实际用上的内核（日志/诊断用）
_BR_RUN_CACHE = {}


def _norm_browser_pref(v):
    v = str(v or "").strip().lower()
    if v in ("edge", "msedge", "microsoft edge"):
        return "msedge"
    if v in ("chrome", "google chrome"):
        return "chrome"
    return "auto"


def _browser_exe_candidates(ch):
    if os.name == "nt":
        roots = [os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
                 os.environ.get("PROGRAMFILES", r"C:\Program Files"),
                 os.environ.get("LOCALAPPDATA", "")]
        rel = (r"Microsoft\Edge\Application\msedge.exe" if ch == "msedge"
               else r"Google\Chrome\Application\chrome.exe")
        return [os.path.join(r, rel) for r in roots if r]
    if sys.platform == "darwin":
        return ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge" if ch == "msedge"
                else "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    names = (("microsoft-edge-stable", "microsoft-edge") if ch == "msedge"
             else ("google-chrome-stable", "google-chrome"))
    return [shutil.which(n) or "" for n in names] + (
        ["/opt/microsoft/msedge/msedge"] if ch == "msedge" else ["/opt/google/chrome/chrome"])


def _browser_installed(ch):
    return any(c and os.path.isfile(c) for c in _browser_exe_candidates(ch))


def _browser_running(ch):
    """该浏览器此刻是否在运行（=用户在用）。结果缓存 60 秒，失败一律当没在跑。"""
    now = time.time()
    hit = _BR_RUN_CACHE.get(ch)
    if hit and now - hit[0] < 60:
        return hit[1]
    img = "msedge.exe" if ch == "msedge" else "chrome.exe"
    on = False
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq " + img, "/NH"],
                                 capture_output=True, text=True, timeout=4,
                                 creationflags=0x08000000).stdout or ""
            on = img.lower() in out.lower()
        else:
            pat = "msedge" if ch == "msedge" else "chrome/chrome"
            out = subprocess.run(["pgrep", "-f", pat], capture_output=True, text=True,
                                 timeout=3).stdout or ""
            on = bool(out.strip())
    except Exception:
        on = False
    _BR_RUN_CACHE[ch] = (now, on)
    return on


def _browser_channel_order(pref=None):
    """返回依次尝试的 Playwright channel 列表，最后一项 None = 内置 Chromium。"""
    p = _norm_browser_pref(SNIFF_BROWSER[0] if pref is None else pref)
    if p in ("msedge", "chrome"):
        other = "chrome" if p == "msedge" else "msedge"
        # 用户指定了就先用指定的；指定的没装/起不来时仍兜底，嗅探不至于直接失败
        return [p, other, None]
    inst = [c for c in ("msedge", "chrome") if _browser_installed(c)]
    if not inst:
        return ["msedge", "chrome", None]       # 探测失败也照试一遍（未安装会秒报错）
    inst.sort(key=lambda c: 0 if _browser_running(c) else 1)   # 稳定排序：同档 Edge 在前
    return inst + [c for c in ("msedge", "chrome") if c not in inst] + [None]


def _browser_label(ch):
    return {"msedge": "Edge", "chrome": "Chrome", None: "内置 Chromium"}.get(ch, str(ch))


def _pw_launch_sync(chromium, persistent, *a, **kw):
    """同步版：依次 Edge/Chrome/内置 启动（launch 或 launch_persistent_context）。"""
    kw.pop("channel", None)
    fn = chromium.launch_persistent_context if persistent else chromium.launch
    last = None
    for ch in _browser_channel_order():
        try:
            r = fn(*a, channel=ch, **kw) if ch else fn(*a, **kw)
            _LAST_BROWSER[0] = _browser_label(ch)
            LOG.info("[浏览器] 用 %s", _LAST_BROWSER[0])
            return r
        except Exception as e:
            last = e
            LOG.info("[浏览器] %s 启动失败，换下一个: %s", _browser_label(ch), str(e)[:160])
    raise last


async def _pw_launch(chromium, persistent, *a, **kw):
    """异步版：同上。"""
    kw.pop("channel", None)
    fn = chromium.launch_persistent_context if persistent else chromium.launch
    last = None
    for ch in _browser_channel_order():
        try:
            r = await (fn(*a, channel=ch, **kw) if ch else fn(*a, **kw))
            _LAST_BROWSER[0] = _browser_label(ch)
            LOG.info("[浏览器] 用 %s", _LAST_BROWSER[0])
            return r
        except Exception as e:
            last = e
            LOG.info("[浏览器] %s 启动失败，换下一个: %s", _browser_label(ch), str(e)[:160])
    raise last
_ARIA2_CONN_HARD_MAX = 16


def eff_frag_threads():
    """HLS/DASH 分片并发数（可到 256，每片独立请求，是真正的提速乘数）。"""
    t = DL_THREADS if isinstance(DL_THREADS, int) else 0
    if t <= 0:
        return 32            # 自动档：分片默认 32（对标 IDM 级并发）
    return max(2, min(t, 256))


def eff_conn_threads():
    """单文件直链连接数（受 aria2 硬顶 16 约束）。"""
    return max(2, min(eff_frag_threads(), _ARIA2_CONN_HARD_MAX))


def _aria2_candidate_roots():
    """常见 aria2c 安装位置（Motrix 自带的资源目录、PATH 之外的手装位置等）。"""
    env = os.environ
    out = []
    for k in ("LOCALAPPDATA", "APPDATA", "ProgramFiles", "ProgramFiles(x86)",
              "ProgramW6432"):
        v = env.get(k)
        if v and os.path.isdir(v):
            out.append(v)
    for drive in ("C:\\", "D:\\", "E:\\"):
        if os.path.isdir(drive):
            out.append(drive)
    return out


def _aria2_layout_glob(roots, max_prefix=2):
    """按已知布局做浅层显式 glob（每层单个 *，非递归 **），快速命中常见安装：
    标准 Motrix/Electron 的 resources/extra/win32/x64/aria2c.exe，或手装的
    bin/aria2c.exe。深层自定义的便携安装交给 cfg['aria2c_path'] 固定分支。"""
    import glob
    sep = os.sep
    tails = [
        ("resources", "extra", "win32", "x64", "aria2c.exe"),
        ("resources", "extra", "x64", "aria2c.exe"),
        ("resources", "extra", "win32", "x64", "aria2c"),
        ("resources", "app.asar.unpacked", "node_modules", "aria2", "bin", "aria2c.exe"),
        ("bin", "aria2c.exe"),
        ("aria2c.exe",),
    ]
    seen = set()
    for root in roots:
        is_drive = len(root) <= 3 and root[1:2] == ":"
        lim = 2 if is_drive else max_prefix
        base = root[:-1] if (root.endswith(sep) and is_drive) else root.rstrip(sep)
        for lvl in range(0, lim + 1):
            prefix = base + (sep + "*") * lvl if lvl else base
            for tail in tails:
                pat = sep.join([prefix, *tail])
                try:
                    hits = glob.glob(pat)
                except Exception:
                    continue
                for h in hits:
                    key = h.lower()
                    if key in seen:
                        continue
                    try:
                        if os.path.isfile(h):
                            seen.add(key)
                            yield h
                    except OSError:
                        continue


def _walk_find_aria2c(roots, max_depth=6, budget=2.0):
    """在给定根下做一次有界 DFS 找 aria2c(.exe)，命中即返回，超时/超深即弃。"""
    import time as _t
    deadline = _t.monotonic() + budget
    targets = ("aria2c.exe", "aria2c")

    def walk(path, depth):
        if depth > max_depth or _t.monotonic() > deadline:
            return None
        try:
            it = os.scandir(path)
        except (PermissionError, OSError):
            return None
        subdirs = []
        with it:
            for e in it:
                try:
                    if e.is_file(follow_symlinks=False) and e.name.lower() in targets:
                        return e.path
                except OSError:
                    continue
                try:
                    if e.is_dir(follow_symlinks=False):
                        n = e.name.lower()
                        # 只钻可能有 aria2c 的目录名，避免全盘扫
                        if any(t in n for t in ("motrix", "aria2", "bin", "resources",
                                                 "extra", "download", "tools",
                                                 "electron", "app.asar.unpacked")):
                            subdirs.append(e.path)
                except OSError:
                    continue
        for sd in subdirs:
            hit = walk(sd, depth + 1)
            if hit:
                return hit
        return None

    for root in roots:
        hit = walk(root, 0)
        if hit:
            return hit
    return None


def resolve_aria2c(cfg=None):
    """按优先级找 aria2c 并返回路径（找不到返回 None）；副作用：更新全局 ARIA2C。
    优先级：cfg.aria2c_path → PATH → 环境变量 SNIFFDL_ARIA2C → 常见安装位置有界扫描。"""
    global ARIA2C
    cand = None
    try:
        if cfg and cfg.get("aria2c_path") and os.path.isfile(cfg["aria2c_path"]):
            cand = cfg["aria2c_path"]
    except Exception:
        cand = None
    if not cand:
        # 自带优先：打包时把 aria2c(.exe) 放在程序目录 / PyInstaller 解包目录旁 → 自包含、免依赖外部安装
        try:
            _dirs = [BASE_DIR]
            _mp = getattr(sys, "_MEIPASS", None)
            if _mp:
                _dirs.append(_mp)
            for _d in _dirs:
                for _n in ("aria2c.exe", "aria2c"):
                    _p = os.path.join(_d, _n)
                    if os.path.isfile(_p):
                        cand = _p
                        break
                if cand:
                    break
        except Exception:
            cand = None
    if not cand:
        cand = shutil.which("aria2c")
    if not cand:
        env_p = os.environ.get("SNIFFDL_ARIA2C")
        if env_p and os.path.isfile(env_p):
            cand = env_p
    if not cand:
        try:
            for h in _aria2_layout_glob(_aria2_candidate_roots()):
                cand = h
                break
        except Exception:
            cand = None
    if not cand:
        try:
            cand = _walk_find_aria2c(_aria2_candidate_roots())
        except Exception:
            cand = None
    if cand:
        try:
            d = os.path.dirname(os.path.abspath(cand))
            pp = os.environ.get("PATH", "")
            if d and d not in pp.split(os.pathsep):
                os.environ["PATH"] = d + os.pathsep + pp
        except Exception:
            pass
    ARIA2C = cand
    return cand


# ---------------- JS 解算器（EJS）运行时：解 YouTube 等混淆签名/nsig ----------------
JS_RUNTIME = None                       # 检测到的 JsRuntimeInfo（含 name/path/version），None=不可用
_JS_DENO_BUILTIN_DIRS = (               # 开发机可直接复用他程序自带 deno（打包后自然失效）
    r"D:\wws\xz\4k\ovd-portable\bin",
)


def _js_runtime_available():
    """问 yt-dlp：当前 PATH 上有没有可用 JS 运行时（deno/node/bun/quickjs）。"""
    try:
        from yt_dlp.utils._jsruntime import (DenoJsRuntime, NodeJsRuntime,
                                             BunJsRuntime, QuickJsRuntime)
    except Exception:
        return None
    for rt in (DenoJsRuntime, NodeJsRuntime, BunJsRuntime, QuickJsRuntime):
        try:
            info = rt(None).info
            if info is not None and getattr(info, "supported", False):
                return info
        except Exception:
            pass
    return None


def _js_runtime_candidate_dirs(cfg=None):
    dirs = []
    try:
        if cfg and cfg.get("js_runtime_dir"):
            dirs.append(cfg["js_runtime_dir"])
    except Exception:
        pass
    env_d = os.environ.get("SNIFFDL_JS_RUNTIME_DIR")
    if env_d:
        dirs.append(env_d)
    dirs.extend(_JS_DENO_BUILTIN_DIRS)
    for base in (os.environ.get("ProgramFiles", ""),
                 os.environ.get("ProgramFiles(x86)", ""),
                 os.environ.get("LOCALAPPDATA", "")):
        if base:
            dirs.extend([os.path.join(base, "nodejs"),
                         os.path.join(base, "deno")])
    return dirs


def resolve_js_runtime(cfg=None):
    """确保 yt-dlp 能用 EJS 解算器：已有可用运行时直接返回；否则把候选目录逐个
    前置进 os.environ['PATH'] 试探，直到 yt-dlp 检出 deno/node 为止（没用的撤下）。
    副作用：更新全局 JS_RUNTIME。找不到就返回 None（YouTube 等混淆站会退化成解不了）。"""
    global JS_RUNTIME
    info = _js_runtime_available()
    if info:
        JS_RUNTIME = info
        return info
    for d in _js_runtime_candidate_dirs(cfg):
        try:
            if not d or not os.path.isdir(d):
                continue
            pp = os.environ.get("PATH", "")
            if d in pp.split(os.pathsep):
                continue
            os.environ["PATH"] = d + os.pathsep + pp
            info = _js_runtime_available()
            if info:
                JS_RUNTIME = info
                return info
            os.environ["PATH"] = os.pathsep.join(
                x for x in os.environ["PATH"].split(os.pathsep) if x != d)
        except Exception:
            continue
    JS_RUNTIME = None
    return None


def fmt_duration(sec):
    if not sec:
        return "-"
    sec = int(sec)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_size(n):
    if not n:
        return "-"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


# 文件名净化（借鉴 Motrix filename-template 插件思路）：
# 剥非法字符 + 控制符 + Unicode 双向控制符（防文件名反向欺骗）、NFC 归一、
# Windows 保留名加下划线、按 UTF-8 字节安全截断（绝不把一个中文多字节字符切一半）。
_UNSAFE_FN_RE = re.compile(r'[\\/:*?"<>|\r\n\t\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]')
_WIN_RESERVED_RE = re.compile(r'^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)', re.I)


def _utf8_len(s):
    return len(s.encode("utf-8", "ignore"))


def _truncate_utf8(s, max_bytes):
    out, used = [], 0
    for ch in s:
        b = _utf8_len(ch)
        if used + b > max_bytes:
            break
        out.append(ch)
        used += b
    return "".join(out)


INC_DIR_FN = None   # 内核(Engine)注入：outdir -> 未完成目录（incomplete_dir）；None=不用


def snifftmp_root(outdir):
    """yt-dlp 半成品隔离目录 .snifftmp 所在的根：设了 incomplete_dir 就放那里，否则在保存位置。"""
    try:
        if INC_DIR_FN and outdir:
            return INC_DIR_FN(outdir) or outdir
    except Exception:
        pass
    return outdir


def safe_name(s, maxlen=120):
    # maxlen 现为「UTF-8 字节预算」：中文标题也能安全截断，避免路径过长导致下载中途失败。
    s = unicodedata.normalize("NFC", str(s if s is not None else ""))
    s = _UNSAFE_FN_RE.sub("_", s).strip(" ._")
    if _WIN_RESERVED_RE.match(s):
        s = "_" + s
    if not s or s in (".", ".."):
        s = "video"
    s = _truncate_utf8(s, maxlen).rstrip(" .")
    return s or "video"


# 错误分类表（借鉴 Motrix 把引擎原始报错翻译成人话 + 建议动作的做法）：
# 命中顺序即优先级，返回 (简短标签, 建议动作)。绝不抛异常，兜底给通用提示。
_ERROR_RULES = [
    (("auth_key", "expired", "403 forbidden", "401", "token", "deny", "denied",
      "security check", "access denied"),
     "链接已过期", "下载直链失效了：重新点「✦ 嗅探分析」拿新链接再下"),
    (("ssl", "certificate", "tls", "handshake", "cloudflare", "just a moment",
      "ray id", "cf-mitigated", "unsupported url", "unable to extract"),
     "疑似被墙/反爬", "多半是网络被墙或站点反爬：开启代理（如 v2rayN 系统代理）后重新嗅探"),
    (("dns", "getaddrinfo", "name resolution", "name or service not known",
      "nodename nor servname", "failed to resolve"),
     "域名解析失败", "网络/DNS 有问题：检查是否联网、代理是否开启后重试"),
    (("timed out", "timeout", "read timed", "connection reset", "eof occurred",
      "incomplete read"),
     "连接超时", "网络太慢或被限速：换个时段、或开代理后重试这一条"),
    (("ffmpeg", "avconv", "postprocessing", "no such encoder", "merge output format",
      "executable found", "is not a valid"),
     "合并失败", "转码合并出错：确认已安装 ffmpeg，或只下不合并的格式后重试"),
    (("404", "not found", "no such file", "does not exist", "does not match"),
     "资源不存在", "该地址已失效或被删除：重新嗅探看看有没有新的有效链接"),
    (("416", "range", "byte range"),
     "续传冲突", "断点续传和本地临时文件对不上：删掉这条重下（会自动清理残留）"),
    (("no space", "enospc", "磁盘", "not enough space", "disk full"),
     "磁盘空间不足", "保存盘没空间了：清理磁盘或换个保存目录"),
    (("filename too long", "invalid argument", "path too long", "文件名", "url too long"),
     "路径/文件名过长", "保存目录太深或标题太长：换个更短的目录再下"),
    (("http error 429", "too many requests", "rate limit", "限流"),
     "被限流", "访问太频繁被站点限流：歇几分钟，选中这条再单独重试"),
]


def classify_error(err):
    """把原始报错翻译成 (简短标签, 建议动作)。"""
    low = str(err or "").lower()
    for keys, label, action in _ERROR_RULES:
        if any(k in low for k in keys):
            return label, action
    return "下载失败", "可重试这一条；若反复失败，多半是链接过期或被墙，重新嗅探/开代理再试"


def is_media_url(u):
    try:
        path = u.split("?")[0].lower()
    except Exception:
        return False
    return path.endswith(MEDIA_EXT)


def _entry_direct(e):
    """该条目是否是“真·可下载直链”（m3u8/mp4/本地文件）；
    列表页占位行的 url 是 HTML 详情页 → False（不能当直链复制给用户）。"""
    if not e:
        return False
    if e.get("local_file") or e.get("local_m3u8"):
        return True
    u = e.get("url") or ""
    if u.startswith("file:"):
        return True
    return is_media_url(u)


# ---------------- m3u8 解析 ----------------
def parse_m3u8(text):
    if "#EXTM3U" not in text:
        return {"type": "invalid"}
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if any(l.startswith("#EXT-X-STREAM-INF") for l in lines):
        best = None
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF") and i + 1 < len(lines):
                mh = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
                mb = re.search(r"BANDWIDTH=(\d+)", line)
                cand = {"h": int(mh.group(2)) if mh else 0,
                        "bw": int(mb.group(1)) if mb else 0,
                        "uri": lines[i + 1]}
                if best is None or (cand["h"], cand["bw"]) > (best["h"], best["bw"]):
                    best = cand
        return {"type": "master", "height": best["h"] if best else 0,
                "bw": best["bw"] if best else 0, "best_uri": best["uri"] if best else None}
    dur, segs = 0.0, 0
    for line in lines:
        if line.startswith("#EXTINF:"):
            try:
                dur += float(line.split(":", 1)[1].split(",", 1)[0])
            except Exception:
                pass
        elif not line.startswith("#"):
            segs += 1
    return {"type": "media", "duration": dur, "segments": segs}


# ---------------- 浏览器嗅探（1DM 思路） ----------------
async def browser_sniff(page_url, log):
    found = {}

    def add(u, how):
        if not u or not u.startswith("http"):
            return
        if not (is_media_url(u) or how == "媒体类型"):
            return
        u = u.replace("\\/", "/")
        if u not in found:
            found[u] = {"url": u, "how": how}
            log(f"[嗅探] {how}: {u[:110]}")

    log("[浏览器] 启动浏览器（顺序：%s）…" % " → ".join(_browser_label(c) for c in _browser_channel_order()))
    async with async_playwright() as p:
        try:
            browser = await _pw_launch(p.chromium, False, 
                headless=True, args=["--disable-blink-features=AutomationControlled"])
        except Exception as e:
            log(f"[错误] 浏览器启动失败（Edge / Chrome / 内置内核都没起来）: {str(e)[:200]}")
            try:
                exe = p.chromium.executable_path
                log(f"[诊断] 内核路径: {exe} (存在: {os.path.exists(exe)})")
            except Exception:
                exe = ""
            if not exe or not os.path.exists(exe):
                log("[提示] 深度嗅探需要本机装有 Edge 或 Chrome（Windows 自带 Edge 即可）；"
                    "设置 → 高级 →「嗅探用浏览器」可指定用哪个")
            raise
        try:
            ctx = await browser.new_context(user_agent=UA,
                                            viewport={"width": 1280, "height": 800},
                                            ignore_https_errors=True)
            page = await ctx.new_page()

            page.on("request", lambda r: add(r.url, "网络请求"))

            api_responses = []

            def on_resp(r):
                try:
                    ct = (r.headers.get("content-type") or "").lower()
                except Exception:
                    return
                if ct.startswith(("video/", "audio/")) or "mpegurl" in ct or "mp2t" in ct:
                    add(r.url, "媒体类型")
                elif ("json" in ct or "javascript" in ct) and "googletag" not in r.url:
                    if len(api_responses) < 60:
                        api_responses.append(r)
            page.on("response", on_resp)

            log("[浏览器] 打开页面…")
            try:
                resp = await page.goto(page_url, wait_until="domcontentloaded", timeout=25000)
                code = resp.status if resp else "?"
                log(f"[浏览器] 页面状态码 {code}")
            except Exception as e:
                log(f"[警告] 页面加载: {str(e)[:150]}")
            await page.wait_for_timeout(2000)

            try:
                n_play = await page.evaluate(
                    """() => { let n = 0;
                         document.querySelectorAll('video').forEach(v => {
                           v.muted = true; const p = v.play();
                           if (p && p.catch) p.catch(() => {}); n++; });
                         return n; }""")
                if n_play:
                    log(f"[浏览器] 触发 {n_play} 个视频元素自动播放，等待媒体流请求…")
                else:
                    log("[浏览器] 页面无 video 元素（可能是 iframe 嵌套或纯图片页）")
            except Exception:
                pass
            await page.wait_for_timeout(2500)

            log("[浏览器] 滚动页面触发懒加载…")
            for _ in range(8):
                try:
                    await page.mouse.wheel(0, 3000)
                except Exception:
                    pass
                await page.wait_for_timeout(500)
            try:
                await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(800)
            except Exception:
                pass

            # API JSON/JS 响应体扫描: 从接口返回里抽 m3u8/mp4 直链（大量站点通用模式）
            try:
                for r in list(api_responses):
                    try:
                        body = (await r.body()).decode("utf-8", "ignore")
                    except Exception:
                        continue
                    for m in re.finditer(
                            r'https?://[^"\'\\\s]+?\.(?:m3u8|mp4|ts)(?:\?[^"\'\\\s]*)?', body):
                        u = m.group(0)
                        if re.search(r'example\.(com|org)|localhost|127\.0\.0\.1|placeholder|sample', u, re.I):
                            continue
                        add(u, "API响应")
                    for m in re.finditer(
                            r'(?:["\'])(/[^\s"\']+?\.(?:m3u8|mp4))(?:["\'])', body):
                        add(urljoin(r.url, m.group(1)), "API响应")
            except Exception:
                pass

            for frame in page.frames:
                try:
                    srcs = await frame.evaluate(
                        """() => {
                            const out = [];
                            document.querySelectorAll('video,audio,source,track,embed')
                              .forEach(el => ['src','data-src','data-video','data-url','data-source']
                              .forEach(a => { if (el.getAttribute && el.getAttribute(a)) out.push(el.getAttribute(a)); }));
                            document.querySelectorAll('video,audio').forEach(el => {
                              if (el.currentSrc) out.push(el.currentSrc);
                            });
                            document.querySelectorAll('a[href]').forEach(el => {
                              const h = el.getAttribute('href') || '';
                              if (/\\.(mp3|mp4|m4a|flac|wav|aac|ogg|opus|wma|m4b|mkv|avi|mov|flv|ts|webm)(\\?|#|$)/i.test(h))
                                out.push(h);
                            });
                            return out;
                        }""")
                    for s in (srcs or []):
                        add(urljoin(frame.url, str(s)), "页面标签")
                except Exception:
                    pass

            try:
                html = await page.content()
                for m in re.finditer(r'https?://[^"\'<>\s\\]+', html):
                    add(m.group(0), "源码扫描")
            except Exception:
                pass
        finally:
            await browser.close()
    return list(found.values())


def run_sniff(page_url, log):
    return asyncio.run(asyncio.wait_for(browser_sniff(page_url, log), timeout=150))


# ---------------- 深度嗅探：列表页→进详情页→触发播放→抓真实地址（1DM 工作方式） ----------------
PLAY_JS = """() => { let n = 0;
    document.querySelectorAll('video').forEach(v => {
        v.muted = true; v.autoplay = true;
        const p = v.play(); if (p && p.catch) p.catch(() => {}); n++; });
    ['.vjs-big-play-button', '[class*="play-btn"]', '[class*="playBtn"]',
     'button[aria-label*="播放"]', '.dplayer-play', '.player-btn'
    ].forEach(s => document.querySelectorAll(s).forEach(b => {
        try { b.click(); n++; } catch (e) {} }));
    return n; }"""

VIDEO_LINK_PAT = re.compile(r"(video|watch|play|/v/|/v_|detail|episode|vod|media|shipin)", re.I)


def same_site(a, b):
    if not a or not b:
        return False
    if a == b or a.endswith("." + b) or b.endswith("." + a):
        return True
    ra = ".".join(a.split(".")[-2:])
    rb = ".".join(b.split(".")[-2:])
    return ra == rb and a.count(".") >= 1 and b.count(".") >= 1


async def collect_video_links(page, base_host):
    from urllib.parse import urlparse
    out, seen = [], set()
    for frame in page.frames:
        try:
            hrefs = await frame.evaluate(
                "() => Array.from(document.querySelectorAll('a[href]'))"
                ".map(a => a.href).slice(0, 3000)")
        except Exception:
            continue
        for h in (hrefs or []):
            try:
                h = str(h).split("#")[0]
                if not h or h in seen:
                    continue
                pu = urlparse(h)
                if pu.scheme not in ("http", "https"):
                    continue
                if not same_site(pu.hostname, base_host):
                    continue
                if VIDEO_LINK_PAT.search(pu.path) or re.search(r"/\d{6,}", pu.path) \
                        or re.search(r"-\d+\.s?html?$", pu.path):
                    seen.add(h)
                    out.append(h)
            except Exception:
                continue
    return out


async def deep_sniff(list_url, log, max_detail=8):
    from urllib.parse import urlparse
    found = {}

    def add(u, how):
        if not u or not u.startswith("http"):
            return
        if not (is_media_url(u) or how == "媒体类型"):
            return
        u = u.replace("\\/", "/")
        if u not in found:
            found[u] = {"url": u, "how": how}
            log(f"[嗅探] {how}: {u[:110]}")

    log("[深度] 启动浏览器（顺序：%s）…" % " → ".join(_browser_label(c) for c in _browser_channel_order()))
    async with async_playwright() as p:
        browser = await _pw_launch(p.chromium, False, 
            headless=True, args=["--disable-blink-features=AutomationControlled"])
        try:
            ctx = await browser.new_context(user_agent=UA,
                                            viewport={"width": 1280, "height": 800},
                                            ignore_https_errors=True)
            page = await ctx.new_page()
            page.on("request", lambda r: add(r.url, "网络请求"))

            api_responses = []

            def on_resp(r):
                try:
                    ct = (r.headers.get("content-type") or "").lower()
                except Exception:
                    return
                if ct.startswith(("video/", "audio/")) or "mpegurl" in ct or "mp2t" in ct:
                    add(r.url, "媒体类型")
                elif ("json" in ct or "javascript" in ct) and "googletag" not in r.url:
                    if len(api_responses) < 60:
                        api_responses.append(r)
            page.on("response", on_resp)

            async def harvest_tags():
                for frame in page.frames:
                    try:
                        srcs = await frame.evaluate(
                            """() => { const out = [];
                                document.querySelectorAll('video,audio,source')
                                  .forEach(el => ['src','data-src','data-video','data-url']
                                  .forEach(a => { if (el.getAttribute && el.getAttribute(a)) out.push(el.getAttribute(a)); }));
                                document.querySelectorAll('video,audio').forEach(el => { if (el.currentSrc) out.push(el.currentSrc); });
                                return out; }""")
                        for s in (srcs or []):
                            add(urljoin(frame.url, str(s)), "页面标签")
                    except Exception:
                        pass

            async def load(url, dwell):
                log(f"[深度] 打开 {url[:95]}")
                try:
                    resp = await page.goto(url, wait_until="domcontentloaded", timeout=25000)
                    log(f"[深度] 状态码 {resp.status if resp else '?'}")
                except Exception as e:
                    log(f"[警告] 加载: {str(e)[:120]}")
                    return
                await page.wait_for_timeout(1500)
                try:
                    n = await page.evaluate(PLAY_JS)
                    if n:
                        log(f"[深度] 触发播放×{n}，等待媒体流…")
                except Exception:
                    pass
                await page.wait_for_timeout(int(dwell * 1000))
                try:
                    await page.mouse.wheel(0, 2000)
                    await page.wait_for_timeout(800)
                except Exception:
                    pass
                await harvest_tags()

            await load(list_url, dwell=3.0)
            base_host = urlparse(list_url).hostname

            links = await collect_video_links(page, base_host)
            if found:
                log(f"[深度] 当前页已嗅到 {len(found)} 条媒体，继续深入详情页抓更多…")
            log(f"[深度] 列表页发现 {len(links)} 个视频详情链接，逐个进入（最多 {max_detail}）…")
            for i, link in enumerate(links[:max_detail], 1):
                await load(link, dwell=4.5)
                log(f"[深度] {i}/{min(len(links), max_detail)} 累计媒体 {len(found)} 条")
                if i >= 3 and not found:
                    log("[警告] 前 3 个详情页均未嗅到媒体，可能需登录或结构特殊")
        finally:
            await browser.close()
    return list(found.values())


def run_deep_sniff(list_url, log, max_detail=8):
    return asyncio.run(asyncio.wait_for(
        deep_sniff(list_url, log, max_detail=max_detail), timeout=420))


# ---------------- 加密播放器引擎（abyssplayer 等数据加密型播放页） ----------------
# 流程: requests拿播放页HTML → 本地镜像真浏览器跑(过反爬+解密) → 监听直链(带token) → 返回
ASSET_CACHE = os.path.join(BASE_DIR, "asset_cache")
# 通用嵌入播放器引擎: 广告/统计域名黑名单（其余任何 iframe/data-url 播放器都尝试镜像嗅探）
AD_HOSTS = ("googletagmanager.com", "googlesyndication.com", "doubleclick.net",
            "google-analytics.com", "morphify.net", "benchform.org", "biomanos.org",
            "disqus.com", "facebook.com", "twitter.com", "xiaohongshu.com")


def is_embed_player(url, page_url):
    """判断是否值得镜像嗅探的嵌入播放器: 非广告统计、非主站自身、非静态资源"""
    try:
        from urllib.parse import urlparse
        pu = urlparse(url)
        if pu.scheme not in ("http", "https"):
            return False
        host = pu.hostname or ""
        if not host:
            return False
        if any(a in host for a in AD_HOSTS):
            return False
        if page_url and host == (urlparse(page_url).hostname or ""):
            return False
        if re.search(r"\.(css|png|jpe?g|gif|svg|woff2?)(\?|$)", pu.path, re.I):
            return False
        return True
    except Exception:
        return False


def lock_m3u8_locally(master_url, page_url, log):
    """token 签发瞬间立即锁定 m3u8 树到本地（N_m3u8DL 思路）:
    拉 master → 选最高码率 → 拉子列表 → 分片绝对化 → 落盘本地 m3u8。
    之后 yt-dlp 吃本地文件, 不受 master token 过期影响。"""
    try:
        hdrs = {"User-Agent": UA, "Referer": page_url}
        r = SESSION.get(master_url, headers=hdrs, timeout=15)
        master_text = r.text
        if "#EXTM3U" not in master_text:
            log("[锁定] master 拉取失败(非m3u8)")
            return None
        m = parse_m3u8(master_text)
        token_q = master_url.split("?", 1)[1] if "?" in master_url else ""
        if m["type"] == "master" and m.get("best_uri"):
            sub = urljoin(master_url, m["best_uri"])
            if token_q and "?" not in sub:
                sub += "?" + token_q
        else:
            sub = master_url
        r2 = SESSION.get(sub, headers=hdrs, timeout=15)
        sub_text = r2.text
        if "#EXTM3U" not in sub_text:
            log("[锁定] 子列表拉取失败")
            return None
        sm = parse_m3u8(sub_text)
        # 分片绝对化（无 query 的附加 token）
        out_lines, n_frag = [], 0
        for line in sub_text.splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                ab = urljoin(sub, s)
                if token_q and "?" not in ab:
                    ab += "?" + token_q
                out_lines.append(ab)
                n_frag += 1
            else:
                out_lines.append(line)
        local_dir = os.path.join(BASE_DIR, "locked_m3u8")
        os.makedirs(local_dir, exist_ok=True)
        key = hashlib.md5(master_url.split("?")[0].encode()).hexdigest()[:12]
        local_path = os.path.join(local_dir, f"{key}.m3u8")
        with open(local_path, "w", encoding="utf-8") as f:
            f.write("\n".join(out_lines) + "\n")
        log(f"[锁定] m3u8 已锁定本地: {n_frag} 分片 → {os.path.basename(local_path)}")
        return {"path": local_path, "res": (f"{m['height']}p" if m.get("height") else None),
                "duration": sm.get("duration"), "segments": n_frag,
                "bw": m.get("bw"), "origin": master_url}
    except Exception as e:
        log(f"[锁定] 异常: {str(e)[:120]}")
        return None


async def lock_from_responses(responses, log):
    """从浏览器已下载的 m3u8 响应体组装本地锁定文件（与 token 消费无关）。
    master(多码率)→选最高档→匹配对应子列表响应→分片绝对化→落盘。"""
    try:
        texts = {}
        for r in responses:
            try:
                body = await r.body()
                text = body.decode("utf-8", "ignore")
                if "#EXTM3U" in text:
                    texts[r.url] = text
            except Exception:
                continue
        if not texts:
            log("[锁定] 无有效 m3u8 响应体")
            return None
        master_url = next((u for u in texts if "master" in u), list(texts)[0])
        master_text = texts[master_url]
        m = parse_m3u8(master_text)
        # 找子列表: master 选最高档；否则该文本自身就是子列表
        sub_url, sub_text = None, master_text
        if m["type"] == "master" and m.get("best_uri"):
            sub = urljoin(master_url, m["best_uri"])
            for u, t in texts.items():
                if os.path.basename(u.split("?")[0]) == os.path.basename(sub.split("?")[0]):
                    sub_url, sub_text = u, t
                    break
            if sub_url is None:
                # 子列表响应体不在手（浏览器只请求了 master），继承 token 用 Python 拉
                token_q = master_url.split("?", 1)[1] if "?" in master_url else ""
                if token_q and "?" not in sub:
                    sub += "?" + token_q
                try:
                    rr = SESSION.get(sub, headers={"User-Agent": UA, "Referer": "https://asmr.one/"}, timeout=15)
                    if "#EXTM3U" in rr.text[:30]:
                        sub_url, sub_text = sub, rr.text
                except Exception:
                    pass
        else:
            sub_url = master_url
        if "#EXTM3U" not in sub_text:
            log("[锁定] 子列表无效")
            return None
        sm = parse_m3u8(sub_text)
        base_url = sub_url or master_url
        token_q = base_url.split("?", 1)[1] if "?" in base_url else ""
        out_lines, n_frag = [], 0
        for line in sub_text.splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                ab = urljoin(base_url, s)
                if token_q and "?" not in ab:
                    ab += "?" + token_q
                out_lines.append(ab)
                n_frag += 1
            else:
                out_lines.append(line)
        local_dir = os.path.join(BASE_DIR, "locked_m3u8")
        os.makedirs(local_dir, exist_ok=True)
        key = hashlib.md5(master_url.split("?")[0].encode()).hexdigest()[:12]
        local_path = os.path.join(local_dir, f"{key}.m3u8")
        with open(local_path, "w", encoding="utf-8") as f:
            f.write("\n".join(out_lines) + "\n")
        log(f"[锁定] 已截获并锁定: {n_frag} 分片 → {os.path.basename(local_path)}")
        return {"path": local_path, "res": (f"{m['height']}p" if m.get("height") else None),
                "duration": sm.get("duration"), "segments": n_frag,
                "bw": m.get("bw"), "origin": master_url}
    except Exception as e:
        log(f"[锁定] 组装异常: {str(e)[:120]}")
        return None


def cache_fetch(url, referer):
    os.makedirs(ASSET_CACHE, exist_ok=True)
    key = os.path.join(ASSET_CACHE, hashlib.md5(url.encode()).hexdigest()[:16])
    if os.path.exists(key):
        try:
            with open(key, encoding="utf-8") as f:
                return f.read()
        except Exception:
            pass
    for i in range(3):
        try:
            body = SESSION.get(url, headers={"User-Agent": UA, "Referer": referer}, timeout=30).text
            with open(key, "w", encoding="utf-8") as f:
                f.write(body)
            return body
        except Exception:
            time.sleep(1)
    return None


async def mirror_sniff(player_url, log, wait_s=50):
    """本地镜像跑加密播放页: fulfill HTML+JS(缓存), API放行, sec-ch-ua改写, 监听媒体直链"""
    hits, tunnel_hits, local_m3u8 = [], [], {}
    # 播放器页 HTML 不缓存（token 服务端签发，缓存=旧token必404），只缓存静态JS
    html = None
    for i in range(3):
        try:
            html = SESSION.get(player_url, headers={"User-Agent": UA}, timeout=30).text
            break
        except Exception:
            time.sleep(1)
    if not html:
        # 网络拉取失败时才退回缓存
        html = cache_fetch(player_url, "https://www.google.com/")
    if not html:
        log(f"[镜像] 播放页拉取失败: {player_url[:80]}")
        return []
    js_urls = re.findall(r'<script[^>]+src="([^"]+)"', html)
    assets = {}
    if js_urls:
        tasks = [asyncio.to_thread(cache_fetch, u, player_url)
                 for u in js_urls
                 if u.startswith("http") and "googletagmanager" not in u]
        bodies = await asyncio.gather(*tasks)
        for u, body in zip([u for u in js_urls
                            if u.startswith("http") and "googletagmanager" not in u], bodies):
            if body:
                assets[u] = body

    async with async_playwright() as p:
        browser = await _pw_launch(p.chromium, False, 
            headless=True, args=["--disable-blink-features=AutomationControlled"])
        try:
            ctx = await browser.new_context(user_agent=UA, ignore_https_errors=True)
            await ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
            await ctx.add_init_script("""
try{Object.defineProperty(window,'top',{get:()=>({location:{href:'https://parent.example.com/'}}),configurable:true})}catch(e){}
window.open=function(u){return{closed:false,close:()=>{},focus:()=>{}}};
try{Object.defineProperty(window,'fuckAdBlock',{get:()=>undefined,configurable:true})}catch(e){}
try{Object.defineProperty(window,'FuckAdBlock',{get:()=>undefined,configurable:true})}catch(e){}
""")
            page = await ctx.new_page()

            def on_req(r):
                u = r.url
                if ("trycloudflare.com" in u or "mediastorage" in u
                        or ".mp4" in u or ".m3u8" in u or "manifest" in u.lower()
                        or "playlist" in u.lower() or "getfile" in u.lower()):
                    if u not in hits:
                        hits.append(u)
                        tag = "隧道直链" if "trycloudflare" in u else "媒体请求"
                        log(f"[镜像] {tag}: {u[:130]}")
                        if "trycloudflare" in u:
                            tunnel_hits.append(u)


            m3u8_responses = []

            def on_resp(r):
                u = r.url
                if ".m3u8" in u:
                    m3u8_responses.append(r)
                    log(f"[镜像] m3u8 响应 {r.status}: {u[-60:]}")
                if ("trycloudflare" in u or "mediastorage" in u) and u not in hits:
                    hits.append(u)
                    if "trycloudflare" in u:
                        tunnel_hits.append(u)
                        log(f"[镜像] 响应直链: {u[:130]}")
            page.on("request", on_req)
            page.on("response", on_resp)

            async def handler(route):
                u = route.request.url
                from urllib.parse import urlparse
                host = urlparse(u).hostname or ""
                if (u.rstrip("/") == player_url.rstrip("/")
                        or u.rstrip("/") == "https://abyss.to"
                        or host == "abyss.to"
                        or (host.endswith("abyssplayer.com") and u.rstrip("/") != player_url.rstrip("/"))):
                    await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
                elif u in assets:
                    await route.fulfill(status=200, content_type="application/javascript", body=assets[u])
                elif "mediastorage" in u or ".mp4" in u or ".m3u8" in u or "trycloudflare" in u:
                    hdrs = {**route.request.headers}
                    hdrs["sec-ch-ua"] = '"Chromium";v="156", "Not/A)Brand";v="99", "Google Chrome";v="156"'
                    try:
                        await route.continue_(headers=hdrs)
                    except Exception:
                        pass
                elif any(d in u for d in ("googletagmanager", "morphify", "doubleclick", "benchform", "biomanos",
                                          "decafeligiblyhad", "pagead2.googlesyndication")):
                    await route.fulfill(status=200, content_type="text/plain", body="")
                else:
                    try:
                        await route.continue_()
                    except Exception:
                        pass
            await page.route("**/*", handler)

            log(f"[镜像] 本地镜像打开 {player_url[:80]}")
            try:
                await page.goto(player_url, wait_until="commit", timeout=20000)
            except Exception as e:
                log(f"[镜像] goto: {str(e)[:100]}")
            # 4s 后若 body 仍空则 reload 一次（部分 SPA 需要二次触发）
            await page.wait_for_timeout(4000)
            try:
                if not await page.evaluate("() => document.body && document.body.innerHTML.length"):
                    await page.reload(wait_until="commit", timeout=20000)
            except Exception:
                pass
            m3u8_seen_at = -1
            local_m3u8 = {}
            for i in range(int(wait_s / 1.5)):
                await page.wait_for_timeout(1500)
                if tunnel_hits:
                    log(f"[镜像] 已捕获隧道直链×{len(tunnel_hits)}，提前结束")
                    break
                if any(".m3u8" in u for u in hits):
                    # 截获浏览器已下载的 m3u8 响应体（token 已消费也不影响，我们手里有全文）
                    if not local_m3u8.get("path") and m3u8_responses:
                        try:
                            locked = await lock_from_responses(
                                m3u8_responses, log)
                            if locked:
                                local_m3u8.update(locked)
                        except Exception as e:
                            log(f"[锁定] 响应截获失败: {str(e)[:100]}")
                    if m3u8_seen_at < 0:
                        m3u8_seen_at = i
                    elif i > m3u8_seen_at:
                        log(f"[镜像] 已捕获 m3u8 流，提前结束")
                        break
                # 30 秒零命中且无播放迹象 → 放弃本轮（加密播放器需加载 SoTrym API）
                if i == 20 and not hits:
                    log("[镜像] 30 秒无任何媒体请求，提前放弃本轮")
                    break
                try:
                    await page.evaluate(
                        """() => { let n = 0;
                            ['#overlay', '#playback', '.jw-display-icon-display', '.jw-icon-playback', '.jw-svg-icon-playback',
                             '.jw-media', '.jw-wrapper', '[class*="play"]', '[class*="Play"]', 'button', 'video'
                            ].forEach(s => document.querySelectorAll(s).forEach(el => {
                                try { el.click(); n++; } catch(e){} }));
                            document.querySelectorAll('video').forEach(v => {
                                v.muted = true; const p = v.play();
                                if (p && p.catch) p.catch(()=>{}); n++; });
                            return n; }""")
                    await page.mouse.click(640, 350)
                except Exception:
                    pass
        finally:
            await browser.close()
    # 隧道直链 > m3u8 流 > GCS 一次性直链
    pri = []
    for u in hits:
        if "trycloudflare" in u:
            pri.append((0, u))
        elif ".m3u8" in u:
            pri.append((1, u))
        elif "mediastorage" in u:
            pri.append((2, u))
    pri.sort(key=lambda x: x[0])
    seen, ordered = set(), []
    for _, u in pri:
        if u not in seen:
            seen.add(u)
            ordered.append(u)
    return {"urls": ordered, "locked": local_m3u8}


# ---------------- v2: 隧道流当场落盘（缝合 v11-v14 验证结论） ----------------
# 上个AI(mirror_sniff)卡在 Hydrax 的根因:
#   1) 监听正则只认 trycloudflare/mediastorage/.mp4/.m3u8, 而真实流走 sssrr.org/2/c/f/ 隧道(v12证实)
#   2) 无差别点所有 button 会点到跳转链接导致页面关闭(v11崩溃)
#   3) 只拿到 URL 事后重放必 403(v13证实直连GCS 403 / 签名秒级过期) -> 必须浏览器内当场落盘
STREAM_RE_V2 = re.compile(
    r"sssrr\.org|trycloudflare|mediastorage|/2/c/f/|storage\.googleapis|mobomark2021|"
    r"\.m3u8|\.mp4|manifest|playlist|getfile|\.ts\b",
    re.I,
)
AD_BLACK_V2 = ("googletagmanager", "morphify", "doubleclick", "benchform", "biomanos",
               "decafeligiblyhad", "pagead2.googlesyndication", "goldfishouncereburial")


def _find_player_frame(page):
    for fr in page.frames:
        if "abyssplayer" in fr.url or "mobomark" in fr.url or "/2/" in fr.url:
            return fr
    return None


async def _click_overlay(page):
    """点播放器 frame 的播放触发器(v12合并版): overlay + 播放键 + video.play()兜底。
    v14教训: removed≠起播; 无元素返回 no_element=True(不得算removed)。"""
    clicked = 0
    videos = 0
    removed = False
    for fr in page.frames:
        try:
            r = await fr.evaluate("""() => {
                const out = {clicked:0, removed:false, no_element:true, videos:0};
                let ov = document.querySelector('#overlay, .jw-overlay, [class*=overlay]');
                if (ov) {
                    ov.click(); out.clicked++; out.no_element=false;
                    out.removed = !document.body.contains(ov);
                }
                const key = document.querySelector('.jw-icon-display, .jw-icon-play, ' +
                    '[class*=play] button, button[class*=play], .big-play-button, ' +
                    'button[aria-label*=play]');
                if (key) { key.click(); out.clicked++; out.no_element=false; }
                const vs = document.querySelectorAll('video');
                vs.forEach(v => { v.muted = true; try{v.play();}catch(e){} });
                out.videos = vs.length;
                return out;
            }""")
            clicked += r.get("clicked", 0)
            videos += r.get("videos", 0)
            if r.get("removed"):
                removed = True
        except Exception:
            pass
    return {"clicked": clicked, "removed": removed,
            "no_element": clicked == 0 and videos == 0, "videos": videos}


def _scavenge_cache(profile_dir, save_dir, log2):
    """浏览器自己播放=已下载到磁盘缓存。扫描 Edge 用户目录, 用 ffmpeg/ffprobe
    识别缓存里的视频文件(ts/mp4), 复制到 save_dir。签名过期也不怕——浏览器
    请求时签名有效, 数据已经到本机, 我们只是把已下载的搬出来。"""
    import subprocess as _sp
    ffprobe = None
    if FFMPEG:
        cand = os.path.join(os.path.dirname(FFMPEG), "ffprobe.exe")
        if os.path.exists(cand):
            ffprobe = cand
    found = []
    roots = [
        os.path.join(profile_dir, "Default", "Cache"),
        os.path.join(profile_dir, "Default", "Code Cache"),
        os.path.join(profile_dir, "Default", "Media Cache"),
    ]
    cands = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _, names in os.walk(root):
            for n in names:
                fp = os.path.join(dirpath, n)
                try:
                    if os.path.getsize(fp) > 512 * 1024:
                        cands.append(fp)
                except Exception:
                    pass
    cands.sort(key=lambda f: -os.path.getsize(f))
    for fp in cands[:60]:
        if ffprobe:
            try:
                r = _sp.run([ffprobe, "-v", "error",
                             "-show_entries", "format=format_name,duration",
                             "-of", "default=noprint_wrappers=1", fp],
                            capture_output=True, timeout=8)
                out = r.stdout.decode(errors="ignore")
                if r.returncode == 0 and ("mpegts" in out or "mp4" in out or "mov" in out):
                    ext = ".ts" if "mpegts" in out else ".mp4"
                    dst = os.path.join(save_dir, f"cap_cache_{len(found):03d}{ext}")
                    try:
                        with open(fp, "rb") as src, open(dst, "wb") as fdst:
                            fdst.write(src.read())
                        found.append(dst)
                        log2(f"缓存抠取 {os.path.basename(dst)} bytes={os.path.getsize(dst)}")
                    except Exception as e:
                        log2(f"缓存复制失败 {str(e)[:60]}")
            except Exception:
                pass
    return found


# headful 浏览器引擎串行闸：下载线程池/列表页并发会让多个 headful Chromium 同时启动，
# 即便 off-screen 每个仍在任务栏留一个图标（用户投诉“疯狂打开好几个浏览器”）。
# 信号量=1 → 同一时刻只开一个 headful 浏览器窗口，任务栏至多一个图标；yt-dlp 直链下载不受影响。
_HEADFUL_SEM = threading.Semaphore(1)


def _headful(fn):
    """装饰 headful 浏览器引擎：全局串行，同一时刻只允许一个窗口。"""
    @functools.wraps(fn)
    async def w(*a, **k):
        _HEADFUL_SEM.acquire()
        try:
            return await fn(*a, **k)
        finally:
            _HEADFUL_SEM.release()
    return w


@_headful
async def mirror_sniff_v2(player_url, page_url, log, wait_s=75, save_dir=None, load_detail=False):
    """本地镜像跑加密播放页 + 真实流当场落盘（隧道无需事后重放）。

    返回 {"hits": [..], "local_files": [..], "log": [..]}
    """
    import subprocess as _sp
    if save_dir is None:
        save_dir = os.path.join(BASE_DIR, "v2_out")
    os.makedirs(save_dir, exist_ok=True)
    hits, files, logs = [], [], []

    def log2(m):
        logs.append(f"[v2:{time.strftime('%H:%M:%S')}] {m}")
        log(f"[v2:{time.strftime('%H:%M:%S')}] {m}")

    html = None
    for i in range(3):
        try:
            html = SESSION.get(player_url, headers={"User-Agent": UA}, timeout=30).text
            break
        except Exception:
            time.sleep(1)
    if not html:
        html = cache_fetch(player_url, page_url or "https://www.google.com/")
    if not html:
        log2(f"播放页拉取失败: {player_url[:80]}")
        return {"hits": hits, "local_files": files}

    js_urls = re.findall(r'<script[^>]+src="([^"]+)"', html)
    assets = {}
    if js_urls:
        tasks = [asyncio.to_thread(cache_fetch, u, player_url)
                 for u in js_urls
                 if u.startswith("http") and "googletagmanager" not in u]
        bodies = await asyncio.gather(*tasks)
        for u, body in zip([u for u in js_urls
                            if u.startswith("http") and "googletagmanager" not in u], bodies):
            if body:
                assets[u] = body

    async with async_playwright() as p:
        ed = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
        profile_dir = os.path.join(BASE_DIR, "v2_profile")
        if threading.current_thread().name not in ("MainThread",):
            profile_dir += "_" + threading.current_thread().name
        ctx = await _pw_launch(p.chromium, True, 
            profile_dir,
            headless=False,
            user_agent=UA, ignore_https_errors=True,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled",
                  "--autoplay-policy=no-user-gesture-required",
                  "--window-position=-32000,-32000",
                  "--disable-web-security", "--no-sandbox"],
            ignore_default_args=["--enable-automation"])
        try:
            await ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
            await ctx.add_init_script("""
try{Object.defineProperty(window,'top',{get:()=>({location:{href:'https://parent.example.com/'}}),configurable:true})}catch(e){}
window.open=function(u){return{closed:false,close:()=>{},focus:()=>{}}};
try{Object.defineProperty(window,'fuckAdBlock',{get:()=>undefined,configurable:true})}catch(e){}
try{Object.defineProperty(window,'FuckAdBlock',{get:()=>undefined,configurable:true})}catch(e){}
""")
            page = await ctx.new_page()
            finished = {"done": False}

            # 旁路: 浏览器自己请求流(签名有效)成功后, 从真实响应取body落盘
            PAGE_HOSTS = ("abyssplayer", "mobomark", "hydrax.net")

            async def on_response(resp):
                u = resp.url
                if not STREAM_RE_V2.search(u):
                    return
                if any(h in u for h in PAGE_HOSTS):
                    return  # 播放器页面不是流, 排除(否则body()等待整页浪费)
                ct = resp.headers.get("content-type", "") or ""
                if "image/" in ct:
                    return
                try:
                    body = await resp.body()
                except Exception as e:
                    log2(f"旁路body异常 {str(e)[:60]}")
                    return
                rec = {"url": u[:250], "ts": time.strftime("%H:%M:%S"),
                       "status": resp.status, "ct": ct[:18], "bytes": len(body)}
                hits.append(rec)
                is_video = ("video" in ct or "mp2t" in ct or "mpegurl" in ct
                            or "/2/c/f/" in u or "mediastorage" in u
                            or ".m3u8" in u or ".ts" in u
                            or (".mp4" in u and "image" not in ct))
                log2(f"旁路 {resp.status} ct={ct[:18]} {u[:90]} bytes={len(body) if 200 == resp.status else '-'}")
                if resp.status == 200 and body and len(body) > 1024 and is_video:
                    ext = ".mp4" if (".mp4" in u or "video/mp4" in ct) else (
                        ".m3u8" if "mpegurl" in ct else ".ts")
                    fpath = os.path.join(save_dir, f"cap_{len(files):03d}{ext}")
                    with open(fpath, "wb") as f:
                        f.write(body)
                    files.append(fpath)
                    log2(f"旁路落盘 cap_{len(files)-1:03d}{ext} bytes={len(body)}")
                    if len(files) >= 24:
                        finished["done"] = True
            page.on("response", on_response)

            async def handler(route):
                u = route.request.url
                from urllib.parse import urlparse
                host = urlparse(u).hostname or ""
                if (not load_detail and (u.rstrip("/") == player_url.rstrip("/")
                        or host == "abyss.to"
                        or (host.endswith("abyssplayer.com") and u.rstrip("/") != player_url.rstrip("/")))):
                    await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
                    return
                if u in assets and not load_detail:
                    await route.fulfill(status=200, content_type="application/javascript", body=assets[u])
                    return
                if STREAM_RE_V2.search(u) and not any(h in u for h in ("abyssplayer", "mobomark", "hydrax.net")):
                    # 纯放行: 拦截/重放会干扰播放器容错(v14教训: GCS失效备源403是常态,
                    # 只有隧道sssrr是真的; 21:17纯放行时隧道出现过, 重放后反而消失)
                    rec = {"url": u[:250], "ts": time.strftime("%H:%M:%S"), "status": "passthrough"}
                    hits.append(rec)
                    try:
                        await route.continue_()
                    except Exception:
                        pass
                    return
                if any(d in u for d in AD_BLACK_V2):
                    await route.fulfill(status=200, content_type="text/plain", body="")
                    return
                try:
                    await route.continue_()
                except Exception:
                    pass

            await ctx.route("**/*", handler)

            target = page_url if load_detail else player_url
            log2(f"打开 {'详情页' if load_detail else '播放器镜像'} {target[:80]}")
            try:
                await page.goto(target, wait_until="commit", timeout=20000)
            except Exception as e:
                log2(f"goto: {str(e)[:100]}")
            # 4s 后若 body 仍空则 reload 一次（部分 SPA 需要二次触发，同上个AI）
            await page.wait_for_timeout(4000)
            try:
                if not await page.evaluate("() => document.body && document.body.innerHTML.length"):
                    await page.reload(wait_until="commit", timeout=20000)
            except Exception:
                pass
            await page.wait_for_timeout(2000)
            # 详情页模式: 等 iframe 自然跳转到播放器(v12 成功路径)
            if load_detail:
                for _ in range(20):
                    if _find_player_frame(page):
                        break
                    await page.wait_for_timeout(1000)
                # 滚动触发懒加载(播放器iframe常需滚动才注入, v12 同款)
                try:
                    await page.evaluate("window.scrollBy(0, 900)")
                    await page.wait_for_timeout(2000)
                    await page.evaluate("window.scrollBy(0, 1200)")
                    await page.wait_for_timeout(2000)
                except Exception:
                    pass

            t0 = time.time()
            last_state_log = 0.0
            while time.time() - t0 < wait_s:
                if finished["done"]:
                    break
                r = await _click_overlay(page)
                if r.get("clicked"):
                    log2(f"点击 overlay(removed={r.get('removed')})…")
                await page.wait_for_timeout(1500)
                # 每15s打一次镜像状态(定位起播窗口)
                if time.time() - last_state_log > 15:
                    last_state_log = time.time()
                    try:
                        nv = await page.evaluate("() => document.querySelectorAll('video').length")
                        nf = len(page.frames)
                        log2(f"镜像状态 videos={nv} frames={nf}")
                    except Exception:
                        pass
                if files and len(files) >= 6:
                    break
        finally:
            await ctx.close()

    if not files:
        # 浏览器自己播放=视频已下载到磁盘缓存(v2_profile固定目录), 直接抠盘
        log2("旁路无落盘, 尝试缓存抠取…")
        files = _scavenge_cache(profile_dir, save_dir, log2)
        if files:
            log2(f"缓存抠取命中 {len(files)} 个文件")

    merged = None
    if files and FFMPEG:
        mp4s = [f for f in files if f.endswith(".mp4") and os.path.getsize(f) > 1024 * 1024]
        if mp4s:
            merged = mp4s[0]
        else:
            ts = sorted([f for f in files if f.endswith(".ts")])
            if ts:
                merged = os.path.join(save_dir, "merged.mp4")
                lst = os.path.join(save_dir, "parts.txt")
                with open(lst, "w", encoding="utf-8") as f:
                    for t in ts:
                        f.write(f"file '{os.path.abspath(t)}'\n")
                r = _sp.run([FFMPEG, "-y", "-f", "concat", "-safe", "0",
                             "-i", lst, "-c", "copy", merged],
                            capture_output=True, timeout=120)
                if r.returncode != 0 or not os.path.exists(merged):
                    merged = None
                log2(f"ffmpeg 合并: {'OK ' + merged if merged else '失败'}")
    if merged:
        files.append(merged)
    return {"hits": hits, "local_files": files, "log": logs}


# ================= 慢速取片（独立功能：浏览器逐片解密，速度慢） =================
# 不参与常规嗅探/下载流程；只有用户手动点「慢速取片」才会进到这里。
SLOW_OUT = os.path.join(BASE_DIR, "slow_fetch_cache")
PLAYER_HOST_HINTS = ("abyssplayer", "mobomark", "hydrax")


def _ffprobe_video(path):
    """返回 (width,height,duration_sec) 或 None。"""
    if not FFMPEG:
        return None
    cand = os.path.join(os.path.dirname(FFMPEG), "ffprobe.exe")
    probe = cand if os.path.exists(cand) else shutil.which("ffprobe")
    if not probe:
        return None
    try:
        import subprocess as _sp
        r = _sp.run([probe, "-v", "error", "-select_streams", "v:0",
                     "-show_entries", "stream=width,height,duration",
                     "-of", "default=noprint_wrappers=1", path],
                    capture_output=True, timeout=25)
        out = r.stdout.decode(errors="ignore")
        w = re.search(r"width=(\d+)", out)
        h = re.search(r"height=(\d+)", out)
        d = re.search(r"duration=([\d.]+)", out)
        if w and h:
            return (int(w.group(1)), int(h.group(1)),
                    float(d.group(1)) if d else None)
    except Exception:
        pass
    return None


@_headful
async def _slow_capture_worker(detail_url, log, title_prefix, save_dir,
                               want_label=None, cancel_evt=None, force_host=None):
    """慢速取片：真实加载详情页 → 读 JWPlayer 播放列表 → 借站点 Service Worker
    逐片解密，最后把整段 mp4 直接落盘。只给普通嗅探拿不到的站用，速度慢。

    原理（asmr1.cc/Hydrax 类站）：播放器注册 Service Worker，把 storage.googleapis.com
    的分片请求经隧道解密；对「带 #mp4/... 片段的完整源 URL」发 fetch(Range) 会被 SW
    截获并返回解密后的完整 mp4。返回本地 mp4 路径，失败或被取消返回 None。
    """
    if not HAS_PW:
        return None
    os.makedirs(save_dir, exist_ok=True)

    def _cancelled():
        return bool(cancel_evt is not None and cancel_evt.is_set())

    if _cancelled():
        log("[慢速取片] 已取消")
        return None
    # 并行时每线程独立 profile（同一 profile 被 multiple persistent context 打开会互踢）
    prof = os.path.join(BASE_DIR, "hydrax_profile")
    if threading.current_thread().name not in ("MainThread",):
        prof += "_" + threading.current_thread().name
    # 详情页内嵌播放器常经短链域(short.icu)跳转，而短链域本身常整体不可达。
    # 先用普通请求拿原始 HTML 取 slug；页面里播放框若一直停在死短链，就把它改指向播放器。
    # 注意：不能在空白页直接打开播放器主站——它依赖 iframe 父页面环境才初始化。
    player_url = None
    try:
        _raw_html = precheck_html(detail_url) or ""
        _m_slug = re.search(r"short\.icu/([A-Za-z0-9_-]+)", _raw_html)
        if _m_slug and force_host:
            player_url = f"https://{force_host}/" + _m_slug.group(1)
    except Exception:
        player_url = None
    # 每次用全新用户目录: 避免遗留 Service Worker 接管导航导致播放器 iframe 空白
    try:
        if os.path.isdir(prof):
            shutil.rmtree(prof, ignore_errors=True)
    except Exception:
        pass
    # 播放器脚本挂在 iamcdn.net，直连极慢（实测单文件几十秒甚至卡死）；
    # 走系统代理通常 1-2 秒。先探测端口可用，不可用就不带代理，避免整个浏览器起不来。
    _proxy = None
    try:
        _px = get_system_proxy()
        if _px:
            from urllib.parse import urlparse as _urlparse
            import socket as _socket
            _pu = _urlparse(_px)
            if _pu.hostname and _pu.port:
                _s = _socket.create_connection((_pu.hostname, _pu.port), timeout=1.5)
                _s.close()
                _proxy = _px
    except Exception:
        _proxy = None
    async with async_playwright() as p:
        ed = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
        _lkw = {"proxy": {"server": _proxy}} if _proxy else {}
        if _proxy:
            log(f"[慢速取片] 走系统代理: {_proxy}")
        ctx = await _pw_launch(p.chromium, True, 
            prof,
            headless=False, user_agent=UA, ignore_https_errors=True,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled",
                  "--autoplay-policy=no-user-gesture-required", "--no-sandbox",
                  "--window-position=-32000,-32000"],
            ignore_default_args=["--enable-automation"], **_lkw)
        try:
            await ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
            await ctx.add_init_script("""
try{Object.defineProperty(window,'top',{get:()=>({location:{href:'https://parent.example.com/'}}),configurable:true})}catch(e){}
window.open=function(u){return{closed:false,close:()=>{},focus:()=>{}}};
""")
            page = await ctx.new_page()
            # 播放框的真实地址从浏览器请求里捕获（预检请求偶发被拦，不能只靠它）
            _player_seen = {"url": player_url}

            def _note_player_req(req):
                try:
                    u = req.url or ""
                    m = re.search(r"short\.icu/([A-Za-z0-9_-]+)", u)
                    if m and not _player_seen["url"]:
                        _player_seen["url"] = "https://abyssplayer.com/" + m.group(1)
                        return
                    if not _player_seen["url"]:
                        m = re.search(
                            r"https?://(?:www\.)?([a-z0-9.-]*(?:abyssplayer|mobomark)"
                            r"[a-z0-9.-]*)/([A-Za-z0-9_-]+)/?$", u, re.I)
                        if m:
                            _player_seen["url"] = f"https://{m.group(1)}/{m.group(2)}"
                except Exception:
                    pass

            page.on("request", _note_player_req)
            log("[慢速取片] 打开详情页…")
            try:
                await page.goto(detail_url, wait_until="commit", timeout=30000)
            except Exception as e:
                log(f"[慢速取片] goto: {str(e)[:80]}")
            if not title_prefix:
                try:
                    _t = (await page.title() or "").strip()
                    title_prefix = safe_name(_t)[:80] or "slow_video"
                except Exception:
                    title_prefix = "slow_video"
            pfr = None
            scan_rounds = 0
            switched = False
            max_rounds = 130
            switch_round = 70
            fix_round = 2
            fix_round2 = 30
            slow_hint_shown = False
            while scan_rounds < max_rounds:
                if _cancelled():
                    log("[慢速取片] 已取消")
                    return None
                await page.wait_for_timeout(1500)
                scan_rounds += 1
                try:
                    await page.evaluate("window.scrollBy(0, 800)")
                except Exception:
                    pass
                # 扫描所有帧找 JWPlayer（不限于特定域名，兼容代理/换域播放器）
                for fr in page.frames:
                    try:
                        if await fr.evaluate(
                                "() => !!(window.jwplayer && jwplayer().getPlaylist "
                                "&& (jwplayer().getPlaylist()||[]).length)"):
                            pfr = fr
                            break
                    except Exception:
                        pass
                if pfr:
                    break
                # 播放器的 JS 在慢速 CDN 上加载很久（实测 1 分钟以上），先提示别以为卡死
                if not slow_hint_shown and scan_rounds == 30:
                    slow_hint_shown = True
                    log("[慢速取片] 播放器脚本加载较慢，继续等待…")
                # 播放框还停在死短链/空白时: 直接把 iframe 的 src 指向播放器主站。
                # 必须在详情页的 iframe 里加载——播放器依赖父页面环境才会初始化。
                if force_host and scan_rounds in (fix_round, fix_round2) and _player_seen["url"]:
                    try:
                        n = await page.evaluate("""(url) => {
                            let n = 0;
                            let thost = '';
                            try { thost = new URL(url).hostname; } catch(e) {}
                            document.querySelectorAll('iframe').forEach(f => {
                                const s = f.getAttribute('src') || '';
                                let host = '';
                                try { host = new URL(s, location.href).hostname; } catch(e) {}
                                if (host === thost) return;
                                const w = parseInt(f.getAttribute('width')||'0',10) || f.clientWidth || 0;
                                const h = parseInt(f.getAttribute('height')||'0',10) || f.clientHeight || 0;
                                if (/short\\.icu/.test(s) || /abyssplayer|mobomark|hydrax/.test(s) ||
                                        (w >= 320 && h >= 180 && !/googletagmanager/.test(s))) {
                                    try { f.src = url; n++; } catch(e){}
                                }
                            });
                            return n;
                        }""", _player_seen["url"])
                        if n:
                            log(f"[慢速取片] 播放框还在死短链，已直接指向播放器（{n} 个）")
                    except Exception:
                        pass
                # 默认播放器非 JWPlayer 时: 点服务器按钮切到 Hydrax 域播放器再重扫
                if not switched and scan_rounds == switch_round:
                    switched = True
                    try:
                        n = await page.evaluate("""(hints) => {
                            let n = 0;
                            document.querySelectorAll('[data-url],[data-server]').forEach(el => {
                                const u = el.getAttribute('data-url') || '';
                                if (u && hints.some(h => u.includes(h))) { try { el.click(); n++; } catch(e){} }
                            });
                            return n;
                        }""", list(PLAYER_HOST_HINTS))
                        if n:
                            log(f"[慢速取片] 切换服务器按钮 ×{n}，重扫播放器…")
                    except Exception:
                        pass
            if not pfr:
                log("[慢速取片] 未找到 JWPlayer 播放器")
                return None
            log(f"[慢速取片] 播放器就绪: {pfr.url[:70]}")
            # 点开播放器遮罩触发真正播放：站点在这之后才注册 Service Worker，
            # 没有它直接 fetch 视频地址会 CORS 失败（Failed to fetch）。顺手静音。
            try:
                await pfr.evaluate("""() => {
                    try { jwplayer().setMute(true); } catch(e) {}
                    document.querySelectorAll('video').forEach(v => {
                        try { v.muted = true; } catch(e) {}
                    });
                    const ov = document.getElementById('overlay');
                    if (ov) { try { ov.click(); return; } catch(e) {} }
                    try { jwplayer().play(); } catch(e) {}
                }""")
            except Exception:
                pass
            sw_ok = False
            for _sw in range(25):
                if _cancelled():
                    log("[慢速取片] 已取消")
                    return None
                try:
                    if await pfr.evaluate(
                            "() => !!(navigator.serviceWorker && "
                            "navigator.serviceWorker.controller)"):
                        sw_ok = True
                        break
                except Exception:
                    pass
                await page.wait_for_timeout(1000)
            if sw_ok:
                log("[慢速取片] 播放器已接管，开始逐片取回…")
            else:
                log("[慢速取片] 播放器接管较慢，仍尝试直接取…")
            try:
                await pfr.evaluate("window.__slowAbort = false")
            except Exception:
                pass
            pick = await pfr.evaluate("""() => {
                let ss = [];
                try { ss = jwplayer().getPlaylist()[0].sources || []; } catch(e){}
                function num(l){ const m=(l||'').match(/(\\d+)p/); return m?parseInt(m[1]):0; }
                return ss.map(s=>({label:s.label||'', file:s.file||'', num:num(s.label)}));
            }""")
            if not pick:
                log("[慢速取片] 播放列表为空")
                return None
            pick = sorted(pick, key=lambda x: x.get("num") or 0, reverse=True)
            log("[慢速取片] 候选画质: " + ", ".join((p.get("label") or "?") for p in pick))
            if want_label:
                sel = [c for c in pick if (c.get("label") or "") == want_label]
                if sel:
                    pick = sel
            # 从最高画质起逐个尝试
            for cand in pick:
                if _cancelled():
                    log("[慢速取片] 已取消")
                    return None
                f = cand.get("file") or ""
                if not f:
                    continue
                # 解析分片大小(源 fragment 里的 chunkSize), 逐段取以提升稳定性
                mck = re.search(r"chunk/\d+/(\d+)/(\d+)/", f)
                total = int(mck.group(1)) if mck else None
                chunk = int(mck.group(2)) if mck else 0
                log(f"[慢速取片] 尝试 {cand.get('label')} size={total} chunk={chunk}")
                try:
                    dl_task = asyncio.ensure_future(
                        page.wait_for_event("download", timeout=600000))
                    watcher = None
                    if cancel_evt is not None:
                        async def _watch_cancel():
                            while not _cancelled():
                                await asyncio.sleep(1)
                            try:
                                await pfr.evaluate("window.__slowAbort = true")
                            except Exception:
                                pass
                        watcher = asyncio.ensure_future(_watch_cancel())
                    try:
                        r = await asyncio.wait_for(pfr.evaluate("""async ({f, total, chunk, conc}) => {
                            const R = 'bytes=0-';
                            const check = () => { if (window.__slowAbort) throw new Error('aborted'); };
                            const get = async (off, end) => {
                                check();
                                const resp = await fetch(f, {headers:{'Range': 'bytes='+off+'-'+end}});
                                check();
                                if (!resp.ok) throw new Error('status '+resp.status+' @'+off);
                                return await resp.arrayBuffer();
                            };
                            let parts;
                            try {
                                check();
                                if (total && chunk) {
                                    const n = Math.ceil(total / chunk);
                                    parts = new Array(n);
                                    let next = 0;
                                    const worker = async () => {
                                        while (true) {
                                            check();
                                            const i = next++;
                                            if (i >= n) break;
                                            const off = i * chunk;
                                            const end = Math.min(off + chunk, total) - 1;
                                            try { parts[i] = await get(off, end); }
                                            catch(e) { if (window.__slowAbort) throw e; parts[i] = null; }
                                        }
                                    };
                                    await Promise.all(Array.from({length: Math.min(conc, n)}, worker));
                                    // 逐片校验长度, 缺失/过短则并发补取(顺序补取多片时RTT叠加太慢)
                                    const bad = [];
                                    for (let i = 0; i < n; i++) {
                                        const off = i * chunk;
                                        const end = Math.min(off + chunk, total) - 1;
                                        const want = end - off + 1;
                                        if (!parts[i] || parts[i].byteLength !== want) bad.push(i);
                                    }
                                    if (bad.length) {
                                        const fixer = async () => {
                                            while (true) {
                                                check();
                                                const i = bad.shift();
                                                if (i === undefined) break;
                                                const off = i * chunk;
                                                const end = Math.min(off + chunk, total) - 1;
                                                parts[i] = await get(off, end);
                                            }
                                        };
                                        await Promise.all(Array.from({length: Math.min(6, bad.length)}, fixer));
                                    }
                                } else {
                                    parts = [await get(0, Math.max(0, (total||0)-1))];
                                }
                            } catch(e) { return {err: e.message}; }
                            let sum = 0;
                            for (const p of parts) sum += (p ? p.byteLength : 0);
                            if (total && sum !== total) return {err: 'size mismatch '+sum+'/'+total};
                            const b = new Blob(parts, {type:'video/mp4'});
                            const a = document.createElement('a');
                            a.href = URL.createObjectURL(b);
                            a.download = 'video.mp4';
                            a.click();
                            return {status: 200, bytes: b.size};
                        }""", {"f": f, "total": total, "chunk": chunk, "conc": 16}), timeout=1500)
                    finally:
                        if watcher is not None:
                            watcher.cancel()
                    if _cancelled():
                        dl_task.cancel()
                        log("[慢速取片] 已取消")
                        return None
                    if not r or not r.get("bytes"):
                        dl_task.cancel()
                        log(f"[慢速取片] {cand.get('label')} fetch 失败: {r}")
                        continue
                    dl = await asyncio.wait_for(dl_task, timeout=120)
                    base = (title_prefix + "_" if title_prefix else "") + (cand.get("label") or "video")
                    fp = os.path.join(save_dir, safe_name(base) + ".mp4")
                    await dl.save_as(fp)
                    sz = os.path.getsize(fp)
                    if sz <= 10240:
                        log(f"[慢速取片] 落盘过小 {sz} bytes")
                        continue
                    vp = _ffprobe_video(fp)
                    res = f"，{vp[0]}x{vp[1]}" if vp else ""
                    log(f"[慢速取片] 已存好 {os.path.basename(fp)}"
                        f"（{sz/1024/1024:.1f} MB{res}）")
                    return fp
                except Exception as e:
                    if _cancelled():
                        log("[慢速取片] 已取消")
                        return None
                    log(f"[慢速取片] {cand.get('label')} 异常: {str(e)[:100]}")
            return None
        finally:
            try:
                await ctx.close()
            except Exception:
                pass


def slow_fetch_page(detail_url, log, title_prefix=None, save_dir=None,
                    want_label=None, cancel_evt=None):
    """慢速取片唯一入口：同步取回整段 mp4，返回本地路径；失败/取消返回 None。

    这是独立功能，不在常规嗅探/下载链路里自动调用。会打开一个真实浏览器，
    借站点的 Service Worker 逐片解密，再整段落盘，所以慢（几分钟到几十分钟）。
    播放器/短链域偶发抽风，所以失败时换线路最多重试 3 次。
    """
    if not HAS_PW:
        log("[慢速取片] 当前环境没有 Playwright，无法使用")
        return None
    save_dir = save_dir or SLOW_OUT
    import time as _time
    hosts = ["abyssplayer.com", "mobomark2021.com", None]
    deadline = _time.monotonic() + 2700
    for i, host in enumerate(hosts, 1):
        if cancel_evt is not None and cancel_evt.is_set():
            log("[慢速取片] 已取消")
            return None
        if i > 1:
            log(f"[慢速取片] 第 {i-1} 次没取到，换线路重试（{i}/{len(hosts)}）…")
        left = deadline - _time.monotonic()
        if left < 60:
            log("[慢速取片] 总时长已到，停止重试")
            break
        try:
            fp = asyncio.run(asyncio.wait_for(
                _slow_capture_worker(detail_url, log, title_prefix, save_dir,
                                     want_label=want_label, cancel_evt=cancel_evt,
                                     force_host=host),
                timeout=min(1500, left)))
        except asyncio.TimeoutError:
            log("[慢速取片] 本次超过 25 分钟仍未取完，已停止")
            return None
        except Exception as e:
            log(f"[慢速取片] 入口异常: {str(e)[:120]}")
            fp = None
        if fp and os.path.exists(fp):
            return fp
        if cancel_evt is not None and cancel_evt.is_set():
            return None
    return None


# 静态流抽取：DPlayer/hls.js 类加密博客站把视频地址【直接写死在页面 HTML 里】，
# 但被 JSON 转义成 https:\/\/host\/x.m3u8?...，常规 // 正则抓不到（旧探针据此误判“无直链”，
# 才退化去开浏览器找 JWPlayer 而失败）。这里先把 \/ 还原再抽 .m3u8/.mp4，
# 直接交给 yt-dlp——其原生支持 #EXT-X-KEY AES-128 分片解密+合并，全程零浏览器。
_AD_HINTS = (".gif", ".png", ".jpg", ".jpeg", ".webp", ".svg", ".ico",
             "banner", "/ads/", "ad.", "advert", "poster", "cover", "thumb")


def _fetch_stream_text(u, page_url, timeout=10):
    """尽力拉取 m3u8/播放列表文本：优先 curl_cffi Chrome 指纹（加密站段 host 常设 Referer
    校验/CF 指纹），失败回落 requests。全程 best-effort，失败返回空串不抛。"""
    hdr = {"User-Agent": UA, "Referer": _safe_hdr(page_url)}
    px = get_system_proxy()
    try:
        from curl_cffi import requests as _creq
        kw = {"headers": hdr, "timeout": timeout, "impersonate": "chrome"}
        if px:
            kw["proxies"] = {"http": px, "https": px}
        return _creq.get(u, **kw).text
    except Exception:
        pass
    try:
        return SESSION.get(u, headers=hdr, timeout=timeout).text
    except Exception:
        return ""


def _hls_meta(u, page_url):
    """返回 (时长取整秒, 最高分辨率高度, 是否AES-128)；任一环节失败对应值为 None/False。"""
    from urllib.parse import urljoin
    txt = _fetch_stream_text(u, page_url)
    if not txt:
        return None, None, False
    info = parse_m3u8(txt)
    aes = ("EXT-X-KEY" in txt and "AES-128" in txt)
    height = info.get("height") or 0
    if info.get("type") == "master":
        aes = aes or ("EXT-X-KEY" in txt and "AES-128" in txt)
        if info.get("best_uri"):
            sub = info["best_uri"]
            if not sub.startswith("http"):
                sub = urljoin(u, sub)
            stxt = _fetch_stream_text(sub, page_url)
            if stxt:
                aes = aes or ("EXT-X-KEY" in stxt and "AES-128" in stxt)
                info = parse_m3u8(stxt)
    dur = info.get("duration")
    return (round(dur) if dur else None), (height or None), aes


def collect_static_streams(html, page_url, log=None, dedup=True):
    r"""从页面 HTML 静态抽取可下载的 HLS/MP4 直链（容 \/ 转义）。
    返回可直接喂 yt-dlp 的条目列表；无则 []。
    dedup=True 时探测每个 m3u8 的真实时长，把"同一视频不同路径变体"
    （如 /videos5/ 加密 vs /m3m/ 明文）合并成一条，只留画质最高的一路。"""
    if not html:
        return []
    text = html.replace("\\/", "/")
    urls = re.findall(
        r'https?://[^\s"\'<>\\]+?\.(?:m3u8|mp4)(?:\?[^\s"\'<>\\]*)?', text, re.I)
    out, seen = [], set()
    for u in urls:
        u = u.rstrip('",;)]')
        low = u.lower()
        if u in seen or any(x in low for x in _AD_HINTS):
            continue
        seen.add(u)
        is_hls = ".m3u8" in low
        base = os.path.basename(u.split("?")[0])
        out.append({
            "url": u, "how": "HLS直链" if is_hls else "MP4直链",
            "title": safe_name(os.path.splitext(base)[0] or "video"),
            "ext": "m3u8" if is_hls else "mp4",
            "res": "-", "duration": None, "size": None,
            "_page": page_url,
        })
    # 同页多变体去重：多个 m3u8 时长相同 = 同一个视频（加密/明文两条路），只留一路
    if dedup:
        hls = [e for e in out if e["ext"] == "m3u8"]
        if len(hls) > 1:
            for e in hls:
                d, h, aes = _hls_meta(e["url"], page_url)
                e["_dur"], e["_h"], e["_aes"] = d, h, aes
                if d:
                    e["duration"] = float(d)
                if h:
                    e["res"] = f"{h}p"
            kept, drop = [], set()
            by_dur = {}
            for e in hls:
                if e.get("_dur"):
                    by_dur.setdefault(e["_dur"], []).append(e)
                else:
                    kept.append(e)              # 探不到时长的，保守保留不误删
            for d, grp in by_dur.items():
                if len(grp) == 1:
                    kept.append(grp[0])
                    continue
                best = sorted(
                    grp, key=lambda x: (x.get("_h") or 0,
                                        0 if x.get("_aes") else 1))[0]
                kept.append(best)
                drop.update(id(x) for x in grp if x is not best)
            if drop:
                out = [e for e in out if id(e) not in drop]
                if log:
                    log(f"[静态流] 同视频多路径去重: {len(hls)} 路 m3u8 → "
                        f"{len([e for e in out if e['ext'] == 'm3u8'])} 个视频")
            for e in out:
                e.pop("_dur", None)
                e.pop("_h", None)
                e.pop("_aes", None)
    if out and log:
        log(f"[静态流] 从页面直取 {len(out)} 条 HLS/MP4 地址（免浏览器）")
    return out


_ALIVE_CACHE = {}
_ALIVE_LOCK = threading.Lock()


def _media_url_alive(u, referer=None, timeout=7):
    """直链验活：HEAD 优先；403/405/501 再用 1 字节 Range 复验；404/410 判死。
    只缓存确定结果，网络抖动不写缓存，避免把好链接误杀。"""
    with _ALIVE_LOCK:
        if u in _ALIVE_CACHE:
            return _ALIVE_CACHE[u]
    hdr = {"User-Agent": UA}
    if referer:
        try:
            hdr["Referer"] = _safe_hdr(referer)
        except Exception:
            pass
    ok, definitive = False, False
    try:
        r = SESSION.head(u, headers=hdr, timeout=timeout, allow_redirects=True)
        sc = r.status_code
        if sc in (200, 206):
            ok, definitive = True, True
        elif sc in (404, 410):
            definitive = True
        else:
            r2 = SESSION.get(u, headers=dict(hdr, Range="bytes=0-0"),
                             timeout=timeout, stream=True)
            ok = r2.status_code in (200, 206)
            definitive = True
            try:
                r2.close()
            except Exception:
                pass
    except Exception:
        pass
    if definitive:
        with _ALIVE_LOCK:
            _ALIVE_CACHE[u] = ok
    return ok


def sniff_encrypted_player(page_url, log, title_prefix=None, meta_only=False):
    """入口: 主站页 requests 提取 data-url/iframe 播放器 → 逐个镜像嗅探。返回直链列表"""
    results = []
    try:
        html = None
        # meta 模式(列表页批量)压短拉取: 站点限流时 3×30s=90s/页 是嗅探最大慢源
        tries, to = (2, 12) if meta_only else (3, 30)
        for i in range(tries):
            try:
                html = SESSION.get(page_url, headers={"User-Agent": UA}, timeout=to).text
                break
            except Exception:
                time.sleep(1)
        if not html:
            log("[加密引擎] 主站页面拉取失败（如确需此站，可手动点「慢速取片」）")
            return results
        # 静态流优先: 页面 HTML 里直接写死了 m3u8/mp4（DPlayer/hls.js 加密博客站等）
        # → 立即返回，全程免浏览器（yt-dlp 原生解密合并分片）
        static = collect_static_streams(html, page_url, log)
        if static:
            alive = []
            for st in static:
                u = st.get("url") or ""
                ext = (st.get("ext") or "").lower().lstrip(".")
                if u and not st.get("local_file") and ext in ("mp4", "mov", "webm") \
                        and not _media_url_alive(u, page_url):
                    log(f"[静态流] 跳过失效直链（已 404/410）: {u[:90]}")
                    continue
                alive.append(st)
            static = alive
        if static:
            if title_prefix:
                for st in static:
                    st["title"] = safe_name(title_prefix + "_" + st["title"])
            return static
        players = []
        for attr in re.findall(r'data-url="([^"]+)"', html):
            players.append(attr)
        for m in re.findall(r'<iframe[^>]+src="([^"]+)"', html):
            players.append(m)
        players = [p for p in players if p.startswith("http")]
        players = [p for p in players if is_embed_player(p, page_url)]
        # 已知快线路优先，未知播放器也照试（通用引擎不挑域名）
        players.sort(key=lambda p: 0 if "abyssplayer" in p else 1)
        log(f"[加密引擎] 发现 {len(players)} 个嵌入播放器（通用模式）")
        if players and all(
                any(h in p for h in PLAYER_HOST_HINTS + ("short.icu",))
                for p in players):
            log("[加密引擎] 该页只有 JWPlayer 短链播放器，常规引擎取不到；"
                "如确需此页请手动点「慢速取片」")
            return results
        for i_p, p_url in enumerate(players[:4]):
            log(f"[加密引擎] 镜像解析播放器: {p_url[:80]}")
            urls, local_files = [], []
            locked = {}
            for attempt in range(2):
                try:
                    # v2 优先: 隧道流当场落盘（Hydrax 签名 URL 不可事后重放，v13 已证 403）
                    # load_detail=True: 真实加载详情页→iframe自然跳播放器(v12成功路径)
                    got = asyncio.run(asyncio.wait_for(
                        mirror_sniff_v2(p_url, page_url, log, wait_s=60, load_detail=True),
                        timeout=120 if attempt else 160))
                    if got and got.get("local_files"):
                        local_files = got["local_files"]
                        log(f"[加密引擎] 当场落盘 {len(local_files)} 个文件（v2 成功）")
                        break
                    if got and (got.get("hits") or got.get("local_files")):
                        log(f"[加密引擎] v2 捕获 {len(got.get('hits') or [])} 条流但未落盘，把可重放直链交给下载链路…")
                    # v2 直链回流: hits 中 2xx 的非签名域视频直链可直接下载(梨视频等)
                    # 签名域(mediastorage/storage.googleapis/sssrr/2/c/f)重放必403, 只能靠当场落盘, 跳过
                    if got and got.get("hits"):
                        direct = []
                        for h in got["hits"]:
                            u = h.get("url", "") or ""
                            st = h.get("status", 0)
                            if isinstance(st, int) and 200 <= st < 400 and u:
                                if any(k in u for k in ("mediastorage", "storage.googleapis",
                                                        "sssrr.org", "/2/c/f/", "trycloudflare")):
                                    continue
                                if any(k in u for k in (".mp4", ".m3u8", ".ts", ".webm")):
                                    if u not in direct:
                                        direct.append(u)
                        if direct:
                            log(f"[加密引擎] 直链回流 {len(direct)} 条（v2 捕获）")
                            for u in direct:
                                base = os.path.basename(u.split("?")[0]) or "video"
                                name = f"{title_prefix}_{base}" if title_prefix else base
                                results.append({
                                    "url": u, "how": "v2直链",
                                    "source_page": page_url, "volatile": False,
                                    "title": safe_name(name),
                                    "ext": os.path.splitext(base)[1].lstrip(".") or "-",
                                })
                            break
                    log(f"[加密引擎] 第{attempt + 1}轮无结果，重试…")
                except Exception as e:
                    log(f"[加密引擎] 播放器解析失败: {str(e)[:120]}")
            if local_files:
                # 当场落盘产物: 以 file:// 形式交给下载链路（本地复制，无需重放签名 URL）
                for fp in local_files:
                    title = os.path.basename(fp)
                    if title_prefix:
                        title = f"{title_prefix}_{title}"
                    results.append({
                        "url": "file:///" + fp.replace("\\", "/"),
                        "how": "当场落盘", "source_page": page_url,
                        "volatile": False, "title": safe_name(title),
                        "local_file": fp,
                        "ext": os.path.splitext(fp)[1].lstrip(".") or "-",
                    })
                break
            if urls or locked.get("path"):
                masters = [x for x in urls if "master.m3u8" in x]
                chosen = masters if masters else (urls[:1] if urls else [locked.get("origin", "")])
                for u in chosen:
                    base = os.path.basename((u or "locked").split("?")[0]) or "video"
                    name = f"{title_prefix}_{base}" if title_prefix else base
                    entry = {
                        "url": u or locked.get("origin", ""), "how": "加密直链",
                        "source_page": page_url, "volatile": True,
                        "title": safe_name(name),
                    }
                    if locked.get("path"):
                        entry["local_m3u8"] = locked["path"]
                        entry["volatile"] = False
                        if locked.get("res"):
                            entry["res"] = locked["res"]
                        if locked.get("duration"):
                            entry["duration"] = locked["duration"]
                        if locked.get("segments"):
                            entry["segments"] = locked["segments"]
                        if locked.get("bw") and locked.get("duration"):
                            entry["size"] = int(locked["bw"] / 8 * locked["duration"])
                        entry["how"] = "已锁定"
                    results.append(entry)
                break
    except Exception as e:
        log(f"[加密引擎] 异常: {str(e)[:150]}")
    return results


# ---------------- MSE 捕获引擎（asmrmoon 类 blob 流站，猫抓同款思路） ----------------
# 站点用 MediaSource 播放：地址是内存里 blob: 伪 URL，requests/yt-dlp/IDM 拿不到实体，
# 只有能进浏览器内部截 SourceBuffer.appendBuffer 分片的工具才能下（1DM/猫抓）。
# 借鉴 cat-catch 的 proxyMediaSourceMethods 思路（不搬它 GPL 代码）：
#   add_init_script 在页面任何脚本前 hook MediaSource.prototype.addSourceBuffer
#   → 拿到 SourceBuffer 后 hook 它的 appendBuffer → 每次调用把 ArrayBuffer 拷贝入列
#   → hook endOfStream 置完成标志
# 完成后浏览器侧 new Blob(bufferList) 拼接 fmp4 → a.click() 触发 download 事件
# → page.expect_download / page.on("download") + dl.save_as 落盘。

MSE_HOOK_JS = r"""
(function(){
  if (window.__sniffdl_mse_installed) return;
  window.__sniffdl_mse_installed = true;
  window.__sniffdl_mse = { streams: [], done: false, totalBytes: 0, createUrls: [], mediaSrcs: [] };

  try {
    var origCreate = URL.createObjectURL;
    URL.createObjectURL = function(obj) {
      var u = origCreate.call(URL, obj);
      try {
        var t = '';
        if (window.MediaSource && obj instanceof MediaSource) t = 'MediaSource';
        else if (window.Blob && obj instanceof Blob) t = 'Blob:' + (obj.type || '') + ':' + obj.size;
        window.__sniffdl_mse.createUrls.push({url: u, kind: t});
      } catch(e){}
      return u;
    };
  } catch(e){}

  try {
    if (!window.MediaSource) return;
    var origAdd = MediaSource.prototype.addSourceBuffer;
    MediaSource.prototype.addSourceBuffer = function(mime) {
      var sb = origAdd.call(this, mime);
      try {
        var stream = { mime: mime || '', buffers: [], size: 0, count: 0 };
        window.__sniffdl_mse.streams.push(stream);
        var origApp = sb.appendBuffer;
        sb.appendBuffer = function(data) {
          try {
            if (data && data.byteLength !== undefined && data.byteLength > 0) {
              var copy;
              if (data.slice) copy = data.slice(0);
              else copy = new Uint8Array(data).slice().buffer;
              stream.buffers.push(copy);
              stream.size += copy.byteLength;
              stream.count++;
              window.__sniffdl_mse.totalBytes += copy.byteLength;
            }
          } catch(e){}
          return origApp.call(this, data);
        };
      } catch(e){}
      return sb;
    };
    var origEnd = MediaSource.prototype.endOfStream;
    MediaSource.prototype.endOfStream = function() {
      try { window.__sniffdl_mse.done = true; } catch(e){}
      return origEnd.apply(this, arguments);
    };
  } catch(e){}

  try {
    var origLoad = HTMLMediaElement.prototype.load;
    Object.defineProperty(HTMLMediaElement.prototype, 'src', {
      configurable: true,
      set: function(v) {
        try {
          if (typeof v === 'string' && v.indexOf('blob:') === 0) {
            window.__sniffdl_mse.mediaSrcs.push(v);
          }
        } catch(e){}
        this.setAttribute && this.setAttribute('src', v);
      },
      get: function() { return this.getAttribute ? (this.getAttribute('src') || '') : ''; }
    });
  } catch(e){}
})();
"""

MSE_STATE_JS = (
    "() => { const s = window.__sniffdl_mse || {streams:[],totalBytes:0,done:false,createUrls:[],mediaSrcs:[]};"
    "return { n: s.streams.length, totalBytes: s.totalBytes || 0, done: !!s.done, "
    "streams: s.streams.map(x=>({mime:x.mime,size:x.size,count:x.count})),"
    "blobs: (s.createUrls||[]).slice(-5), mediaSrcs: (s.mediaSrcs||[]).slice(-3) }; }"
)

MSE_DUMP_JS = r"""(prefix) => {
  const s = window.__sniffdl_mse;
  if (!s || !s.streams || !s.streams.length) return [];
  const results = [];
  let idx = 0;
  s.streams.forEach((stream, i) => {
    if (!stream.buffers || !stream.buffers.length) return;
    const mime = (stream.mime || '').split(';')[0] || 'video/mp4';
    let blob;
    try { blob = new Blob(stream.buffers, {type: mime}); }
    catch(e) { return; }
    const url = URL.createObjectURL(blob);
    const ext = mime.indexOf('audio') === 0 ? 'm4a' : 'mp4';
    const a = document.createElement('a');
    a.href = url;
    a.download = (prefix || 'sniffdl_mse') + '_' + (idx) + '.' + ext;
    a.style.display = 'none';
    document.body.appendChild(a);
    a.click();
    results.push({name: a.download, size: blob.size, mime: mime, index: idx});
    idx++;
    setTimeout(() => { try { URL.revokeObjectURL(url); a.remove(); } catch(e){} }, 5000);
  });
  return results;
}"""

MSE_OUT = os.path.join(BASE_DIR, "mse_out")


@_headful
async def _mse_worker(page_url, log, save_dir, title_prefix=None,
                      wait_s=180, meta_only=False, want_seek=True):
    """MSE 捕获引擎核心: 打开页面 → hook 拦 appendBuffer → 触发播放/seek 逼缓冲
    全片 → 分片 Blob 拼接后 a.click() 下载 → dl.save_as 落盘。

    返回 (state_dict, [file_paths])。meta_only=True 时只拿元数据不落盘。
    """
    if not HAS_PW:
        return None, []
    os.makedirs(save_dir, exist_ok=True)
    # 并行时每线程独立 profile（同 hydrax/v2 血泪教训）
    prof = os.path.join(BASE_DIR, "mse_profile")
    if threading.current_thread().name not in ("MainThread",):
        prof += "_" + threading.current_thread().name
    try:
        if os.path.isdir(prof):
            shutil.rmtree(prof, ignore_errors=True)
    except Exception:
        pass

    async with async_playwright() as p:
        ed = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
        ctx = await _pw_launch(p.chromium, True, 
            prof,
            headless=False, user_agent=UA, ignore_https_errors=True,
            viewport={"width": 1280, "height": 900},
            accept_downloads=True,
            args=["--disable-blink-features=AutomationControlled",
                  "--autoplay-policy=no-user-gesture-required",
                  "--no-sandbox", "--window-position=-32000,-32000"],
            ignore_default_args=["--enable-automation"])
        try:
            # MSE hook 必须最先注入, 页面任何脚本执行前
            await ctx.add_init_script(MSE_HOOK_JS)
            await ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
            await ctx.add_init_script("""
try{Object.defineProperty(window,'top',{get:()=>({location:{href:'https://parent.example.com/'}}),configurable:true})}catch(e){}
window.open=function(u){return{closed:false,close:()=>{},focus:()=>{}}};
""")
            page = await ctx.new_page()
            log(f"[MSE] 打开页面 {page_url[:80]}")
            try:
                await page.goto(page_url, wait_until="commit", timeout=30000)
            except Exception as e:
                log(f"[MSE] goto: {str(e)[:80]}")

            await page.wait_for_timeout(2500)

            # 触发所有 video 播放（muted+play，兼容各种播放器皮肤）
            try:
                n = await page.evaluate("""() => {
                    let n = 0;
                    document.querySelectorAll('video').forEach(v => {
                        try {
                            v.muted = true; v.autoplay = true;
                            const p = v.play();
                            if (p && p.catch) p.catch(()=>{});
                            n++;
                        } catch(e){}
                    });
                    ['.vjs-big-play-button','[class*="play-btn"]','[class*="playBtn"]',
                     'button[aria-label*="播放"]','button[aria-label*="Play"]',
                     '.dplayer-play','.player-btn','.play','.pbtn','.play-btn']
                      .forEach(sel => document.querySelectorAll(sel).forEach(b => {
                          try { b.click(); n++; } catch(e){}
                      }));
                    return n;
                }""")
                log(f"[MSE] 触发播放/点击 ×{n}")
            except Exception as e:
                log(f"[MSE] 触发播放异常: {str(e)[:80]}")

            for _ in range(3):
                try:
                    await page.mouse.wheel(0, 2000)
                except Exception:
                    pass
                await page.wait_for_timeout(500)

            # 循环监控 appendBuffer 增长
            rounds = 0
            max_rounds = max(20, wait_s // 2)
            last_bytes = 0
            stable_rounds = 0
            got_state = None
            while rounds < max_rounds:
                await page.wait_for_timeout(2000)
                rounds += 1
                try:
                    st = await page.evaluate(MSE_STATE_JS)
                except Exception:
                    st = None
                if st:
                    got_state = st
                    cur = st.get("totalBytes", 0)
                    if rounds % 5 == 1 or (st.get("n", 0) and not last_bytes):
                        log(f"[MSE] 已捕获 {cur/1024/1024:.2f} MB, streams={st.get('n')}, "
                            f"done={st.get('done')}")
                    if st.get("done") and cur > 0:
                        log(f"[MSE] endOfStream 已触发")
                        # endOfStream 后可能还有尾缓冲, 再等 2 轮
                        if stable_rounds >= 2:
                            break
                    if cur == last_bytes and cur > 0:
                        stable_rounds += 1
                        if st.get("done") and stable_rounds >= 2:
                            break
                        if not st.get("done") and stable_rounds >= 10:
                            log(f"[MSE] 分片增长停止 ({cur/1024/1024:.2f} MB), 视为完成")
                            break
                    else:
                        stable_rounds = 0
                    last_bytes = cur

                # 推进 currentTime 逼缓冲下一段（有些站只缓冲当前附近）
                if want_seek and rounds % 4 == 0:
                    try:
                        await page.evaluate("""() => {
                            document.querySelectorAll('video').forEach(v => {
                                try {
                                    if (v.duration && isFinite(v.duration) && v.buffered && v.buffered.length) {
                                        const end = v.buffered.end(v.buffered.length - 1);
                                        if (v.currentTime < end - 1) {
                                            v.currentTime = Math.min(end - 0.5, v.duration - 0.5);
                                        }
                                        if (v.paused) { v.muted = true; v.play().catch(()=>{}); }
                                    } else if (v.paused) {
                                        v.muted = true; v.play().catch(()=>{});
                                    }
                                } catch(e){}
                            });
                        }""")
                    except Exception:
                        pass

            # meta_only: 快速判定完就退，不等全片
            if meta_only:
                if got_state and got_state.get("n", 0) > 0:
                    return got_state, []
                # meta 场景等 15 秒看有没有动静
                for _ in range(5):
                    await page.wait_for_timeout(2000)
                    try:
                        st = await page.evaluate(MSE_STATE_JS)
                    except Exception:
                        st = None
                    if st and st.get("n", 0) > 0:
                        return st, []
                return got_state, []

            if not got_state or not got_state.get("n"):
                log("[MSE] 未捕获到任何 SourceBuffer, 页面可能非 MSE 播放")
                return None, []

            log(f"[MSE] 完成: streams={got_state.get('n')} "
                f"total={got_state.get('totalBytes',0)/1024/1024:.2f}MB "
                f"mimes={[m.get('mime','') for m in got_state.get('streams',[])]}")

            # 触发 Blob 拼接下载
            prefix = safe_name(title_prefix or "sniffdl_mse")
            try:
                dumped = await page.evaluate(MSE_DUMP_JS, prefix)
            except Exception as e:
                log(f"[MSE] dump 异常: {str(e)[:120]}")
                return got_state, []
            if not dumped:
                log("[MSE] 无有效分片可下载")
                return got_state, []
            log(f"[MSE] 触发 {len(dumped)} 路 Blob 下载")

            saved_files = []
            try:
                for _ in range(len(dumped)):
                    dl = await page.wait_for_event("download", timeout=120000)
                    suggested = (dl.suggested_filename or "").strip()
                    ext = os.path.splitext(suggested)[1] or (".mp4")
                    idx = len(saved_files)
                    fp = os.path.join(save_dir, f"{prefix}_{idx}{ext}")
                    try:
                        await dl.save_as(fp)
                        sz = os.path.getsize(fp) if os.path.exists(fp) else 0
                        log(f"[MSE] ✓ 落盘 {os.path.basename(fp)} ({sz/1024/1024:.2f} MB)")
                        if sz > 1024:
                            saved_files.append(fp)
                    except Exception as e:
                        log(f"[MSE] save_as 异常: {str(e)[:120]}")
            except Exception as e:
                log(f"[MSE] 等待 download 超时/异常: {str(e)[:120]}")

            final_path = None
            if len(saved_files) == 1:
                final_path = saved_files[0]
            elif len(saved_files) >= 2 and FFMPEG:
                import subprocess as _sp
                vids = [f for f in saved_files if f.lower().endswith((".mp4", ".m4v", ".webm"))]
                auds = [f for f in saved_files if f.lower().endswith((".m4a", ".aac", ".mp3"))]
                if vids and auds:
                    out_fp = os.path.join(save_dir, safe_name(prefix) + "_merged.mp4")
                    try:
                        r = _sp.run([FFMPEG, "-y", "-i", vids[0], "-i", auds[0],
                                     "-c", "copy", "-movflags", "+faststart", out_fp],
                                    capture_output=True, timeout=180)
                        if r.returncode == 0 and os.path.exists(out_fp) and os.path.getsize(out_fp) > 10240:
                            final_path = out_fp
                            log(f"[MSE] ffmpeg 音视频合并 → {os.path.basename(out_fp)}")
                        else:
                            log("[MSE] ffmpeg 合并失败, 用视频文件兜底")
                            final_path = vids[0]
                    except Exception as e:
                        log(f"[MSE] ffmpeg 异常: {str(e)[:120]}")
                        final_path = vids[0]
                else:
                    saved_files.sort(key=os.path.getsize, reverse=True)
                    final_path = saved_files[0]
            elif saved_files:
                saved_files.sort(key=os.path.getsize, reverse=True)
                final_path = saved_files[0]

            return got_state, [final_path] if final_path else saved_files
        finally:
            try:
                await ctx.close()
            except Exception:
                pass


def sniff_mse_capture(page_url, log, title_prefix=None, meta_only=False,
                      wait_s=180):
    """MSE 捕获同步入口。
    meta_only=True: 只嗅探元数据（列表页占位），返回带 mse_chunks 的 entry。
    默认: 完整捕获到本地文件，返回带 local_file 的 entry。
    """
    if not HAS_PW:
        return []
    try:
        state, files = asyncio.run(asyncio.wait_for(
            _mse_worker(page_url, log, MSE_OUT, title_prefix=title_prefix,
                        wait_s=wait_s, meta_only=meta_only),
            timeout=wait_s + 300))
    except Exception as e:
        log(f"[MSE] 入口异常: {str(e)[:120]}")
        return []
    if meta_only:
        if not state or not state.get("n"):
            return []
        return [{
            "url": page_url, "how": "MSE捕获", "title": title_prefix or safe_name(page_url),
            "mse_chunks": True, "source_page": page_url,
            "res": "-", "size": state.get("totalBytes"), "ext": "mp4",
            "_proxy": "",
        }]
    if files:
        fp = files[0]
        return [{
            "url": page_url, "how": "MSE捕获",
            "title": title_prefix or safe_name(os.path.basename(fp)),
            "local_file": fp, "mse_chunks": True, "source_page": page_url,
            "ext": (os.path.splitext(fp)[1].lstrip(".") or "mp4"),
            "size": os.path.getsize(fp) if os.path.exists(fp) else None,
            "_proxy": "",
        }]
    return []


def is_mse_candidate(html, page_url):
    """预检路由辅助: HTML 有 <video> 且无直链 m3u8/mp4/ts（MSE 站典型特征）。"""
    if not html:
        return False
    if "<video" not in html.lower():
        return False
    for pat in (".m3u8", ".mp4?", ".mp4\"", ".flv", "/manifest", "/playlist",
                "hydrax", "abyssplayer", "mobomark", "jwplayer"):
        if pat in html.lower():
            return False
    return True


# ---------------- AList V3 站点嗅探（asmrmoon 类 SPA 文件列表后端） ----------------
# 现象: 页面是 Vue SPA 空壳, 无任何静态 <a href> 媒体直链, 传统 requests/浏览器嗅探
# 全空手; IDM 也拿不到, 只有 1DM/猫抓这种能拦截浏览器内部 XHR/fetch 的工具才行。
# 根源: 前端点文件时才调 /api/fs/list / /api/fs/get 拿 raw_url, URL 直接暴露到
# 后端存储 CDN (mooncdn/cdn.asmrmoon.com 类), raw_url 无签名公开可下, m3u8 的 .ts
# 分片也是绝对 URL 公开可下 → 完全可以绕开浏览器直接调 AList REST API 秒级嗅探全部。
# 识别特征: <meta name="generator" content="AList V3"> 或 window.ALIST = {...}。

ALIST_META_PAT = re.compile(r'<meta[^>]+name="generator"[^>]+content=["\']?AList', re.I)
ALIST_WINDOW_PAT = re.compile(r'window\.ALIST\s*=\s*\{')
ALIST_MEDIA_EXT_PAT = re.compile(
    r"\.(mp3|m4a|aac|flac|wav|ogg|opus|wma|m4b|ape|mp4|mkv|avi|mov|flv|webm|ts|m3u8)(?:\?|#|$)",
    re.I)


def is_alist_site(html, page_url):
    """预检: HTML 或响应里出现 AList 特征即判 AList 站."""
    if not html:
        return False
    if ALIST_META_PAT.search(html) or ALIST_WINDOW_PAT.search(html):
        return True
    return False


def _alist_post(origin, endpoint, body, log=None, timeout=20):
    u = origin.rstrip("/") + endpoint
    try:
        r = SESSION.post(u, json=body, headers={"User-Agent": UA}, timeout=timeout)
        j = r.json()
    except Exception as e:
        if log:
            log(f"[AList] {endpoint} 网络/解析异常: {str(e)[:100]}")
        return None
    if j.get("code") != 200:
        if log:
            log(f"[AList] {endpoint} code={j.get('code')} msg={str(j.get('message'))[:80]}")
        return None
    return j.get("data")


def alist_list_dir(origin, path, log=None, per_page=500):
    d = _alist_post(origin, "/api/fs/list", {
        "path": path, "password": "", "page": 1, "per_page": per_page,
        "nonce": "", "signal": "",
    }, log)
    if not d:
        return []
    return d.get("content") or []


def alist_get_raw(origin, path, log=None):
    d = _alist_post(origin, "/api/fs/get", {
        "path": path, "password": "", "nonce": "", "sign": "",
    }, log)
    if not d:
        return "", {}
    return d.get("raw_url") or "", {
        "size": d.get("size"),
        "mime": d.get("mime_type") or "",
    }


def alist_walk(origin, root_path, log=None, max_files=1500, max_depth=6, workers=8):
    """并发 BFS 列 AList 目录, 返回 [(file_path, size)]。
    列目录是纯 HTTP POST：串行 29 个目录要 90 秒，8 路并发后约 15 秒。
    结果按路径排序，保证「下载开头 N 个」每次拿到的是同一批。"""
    out = []
    visited = {root_path}
    frontier = [root_path]
    depth = 0
    dir_count = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        while frontier and depth <= max_depth and len(out) < max_files:
            futs = {ex.submit(alist_list_dir, origin, p, None): p for p in frontier}
            nxt = []
            for fut in as_completed(futs):
                p = futs[fut]
                try:
                    items = fut.result() or []
                except Exception:
                    LOG.debug("AList 列目录失败: %s", p, exc_info=True)
                    items = []
                dir_count += 1
                for it in items:
                    name = it.get("name") or ""
                    typ = it.get("type")  # 1=folder, 2/3=file
                    size = it.get("size") or 0
                    sub = p.rstrip("/") + "/" + name
                    if typ == 1:
                        if depth + 1 <= max_depth and sub not in visited:
                            visited.add(sub)
                            nxt.append(sub)
                    elif ALIST_MEDIA_EXT_PAT.search(name):
                        out.append((sub, size))
                        if len(out) >= max_files:
                            break
                if log and dir_count % 10 == 0:
                    log(f"[AList] 已扫 {dir_count} 目录, 累计媒体 {len(out)}")
                if len(out) >= max_files:
                    break
            frontier = nxt
            depth += 1
    if len(out) >= max_files and log:
        log(f"[AList] 达到最大文件数 {max_files}, 停止递归")
    out.sort()
    return out


def sniff_alist_site(page_url, log, max_files=1500):
    """AList 站点嗅探入口。URL 可能是目录（列全部）或单文件（直接 raw_url）。
    条目字段: url=raw_url, ext/size 已填, alist_path 记录内部路径供下载时刷新。
    """
    from urllib.parse import urlparse, unquote
    parsed = urlparse(page_url)
    if not parsed.scheme or not parsed.netloc:
        return []
    origin = f"{parsed.scheme}://{parsed.netloc}"
    path = unquote(parsed.path or "/")
    if not path.startswith("/"):
        path = "/" + path
    log(f"[AList] 站点 {origin} 路径 {path}")
    files = []
    if ALIST_MEDIA_EXT_PAT.search(path):
        # 单文件 URL: 直接 fs/get
        raw, meta = alist_get_raw(origin, path, log)
        if raw:
            files = [(path, meta.get("size") or 0)]
    else:
        files = alist_walk(origin, path, log, max_files=max_files)
    log(f"[AList] 待下载 {len(files)} 个媒体文件")
    entries = []
    done = 0

    def _hydrate(fp_size):
        fp, size = fp_size
        try:
            raw, meta = alist_get_raw(origin, fp, None)
        except Exception:
            raw, meta = None, {}
        return fp, size, raw, (meta or {})

    with ThreadPoolExecutor(max_workers=12) as ex:
        for fp, size, raw, meta in ex.map(_hydrate, files):   # map 保序，表格顺序稳定
            done += 1
            if not raw:
                log(f"[AList] 无法取 raw_url: {fp}")
                continue
            base = os.path.basename(fp)
            title = os.path.splitext(base)[0]
            ext = (os.path.splitext(base)[1].lstrip(".").lower() or "-")
            e = {
                "url": raw,
                "how": "AList直链",
                "title": title,
                "ext": ext,
                "size": size or meta.get("size"),
                "res": "-",
                "alist_path": fp,
                "alist_origin": origin,
                "alist_raw": raw,
                "source_page": page_url,
                "_proxy": "",
            }
            entries.append(e)
            if done % 25 == 0:
                log(f"[AList] 已生成 {len(entries)} 条")
    log(f"[AList] 完成: {len(entries)} 条")
    return entries


# ---------------- 网盘/文件列表直收（ASMRMOON 等文件直列站点） ----------------
FILE_LINK_EXTS = (".mp3", ".mp4", ".m4a", ".flac", ".wav", ".aac", ".ogg",
                  ".opus", ".wma", ".m4b", ".ape", ".mkv", ".avi", ".mov", ".flv", ".ts", ".webm")


def collect_file_entries(html, base_url, log=None):
    """从页面 HTML 提取媒体文件直链（href/裸URL, 绝对或相对），返回条目列表。
    网盘式文件列表站（表格直列 mp3/mp4）不需要进详情页, 直接全收。"""
    from urllib.parse import urlparse, urljoin
    out, seen = [], set()
    host = urlparse(base_url).hostname or ""
    pats = [
        r'href="([^"]+?\.(?:' + "|".join(e.lstrip(".") for e in FILE_LINK_EXTS) + r')(?:\?[^"]*)?)"',
        r"href='([^']+?\.(?:" + "|".join(e.lstrip(".") for e in FILE_LINK_EXTS) + r")(?:\?[^']*)?)'",
        r'src="([^"]+?\.(?:' + "|".join(e.lstrip(".") for e in FILE_LINK_EXTS) + r')(?:\?[^"]*)?)"',
        r'data-url="([^"]+?\.(?:' + "|".join(e.lstrip(".") for e in FILE_LINK_EXTS) + r')(?:\?[^"]*)?)"',
    ]
    for pat in pats:
        for m in re.finditer(pat, html, re.I):
            u = m.group(1)
            if u.startswith("//"):
                u = "https:" + u
            elif u.startswith("/"):
                u = urljoin(base_url, u)
            elif not u.startswith("http"):
                u = urljoin(base_url, u)
            u = u.replace("\\/", "/")
            try:
                pu = urlparse(u)
            except Exception:
                continue
            if pu.scheme not in ("http", "https") or u in seen:
                continue
            if pu.hostname and host and not same_site(pu.hostname, host):
                continue
            if pu.hostname and re.search(r'example\.(com|org)$|placeholder', pu.hostname, re.I):
                continue
            seen.add(u)
            name = os.path.basename(u.split("?")[0]) or "file"
            out.append({
                "url": u, "how": "文件列表",
                "source_page": base_url, "volatile": False,
                "title": safe_name(name),
                "ext": os.path.splitext(name)[1].lstrip(".").lower() or "-",
            })
    if out and log:
        log(f"[文件直收] 页面媒体文件直链 {len(out)} 条（网盘式列表, 免详情页解析）")
    return out


# 根路径短 slug 详情链接（asmr1.cc 等: /1m8dekaom 作品页, 无 video/watch 关键词）
SLUG_EXCLUDE = ("wp-content", "wp-admin", "wp-includes", "wp-json", "wp-",
                "category", "tag", "author", "page", "feed", "xmlrpc",
                "sitemap", "search", "login", "register", "about", "contact",
                "terms", "privacy", "dmca", "comment", "goto", "forum",
                "member", "user", "shop", "cart")


def is_slug_path(p):
    p = (p or "").strip("/")
    if not p or "/" in p or "." in p or "?" in p:
        return False
    if not (4 <= len(p) <= 60):
        return False
    if any(x in p.lower() for x in SLUG_EXCLUDE):
        return False
    return re.match(r"^[a-zA-Z0-9\-_=%\u4e00-\u9fff]+$", p) is not None


def collect_slug_links(html, base_url):
    """收集同站根路径 slug 详情链接（绝对或相对形式）"""
    from urllib.parse import urlparse
    host = urlparse(base_url).hostname or ""
    out, seen = [], set()
    for m in re.findall(r'href="(https?://[^"]+)"', html):
        try:
            pu = urlparse(m)
            if pu.hostname and same_site(pu.hostname, host) and is_slug_path(pu.path) \
                    and not pu.query:
                u = m.split("#")[0]
                if u not in seen:
                    seen.add(u)
                    out.append(u)
        except Exception:
            pass
    for m in re.findall(r'href="(/[^"?#]+)"', html):
        if is_slug_path(m):
            u = f"https://{host}{m}"
            if u not in seen:
                seen.add(u)
                out.append(u)
    return out


def sniff_list_page(list_url, log, max_detail=None):
    """通用列表页模式: 提取同站视频详情链接（不限 /videos/ 路径） → 并行逐页嗅探。
    max_detail=None 表示全量（不设上限）"""
    results = []

    try:
        from urllib.parse import urlparse
        html = None
        for i in range(3):
            try:
                html = SESSION.get(list_url, headers={"User-Agent": UA}, timeout=30).text
                break
            except Exception:
                time.sleep(1)
        if not html:
            log("[列表页] 页面拉取失败")
            return results
        # 0) 网盘式文件直链优先: 页面本身就是文件列表, 直接全收
        file_entries = collect_file_entries(html, list_url, log)
        if file_entries:
            results.extend(file_entries)
        host = urlparse(list_url).hostname
        links = []
        seen = set()

        def push(u):
            u = u.split("#")[0]
            if u and u not in seen:
                seen.add(u)
                links.append(u)

        for m in re.findall(r'href="(https?://[^"]*?/videos/[a-f0-9]+)"', html):
            push(m)
        for m in re.findall(r'href="(/videos/[a-f0-9]+)"', html):
            push(f"https://{host}{m}")
        if not links:
            # 通用回退: 同站任意视频类链接（watch/play/v/数字id）
            for m in re.findall(r'href="(https?://[^"]+)"', html):
                try:
                    pu = urlparse(m)
                    if pu.hostname and same_site(pu.hostname, host) \
                            and (VIDEO_LINK_PAT.search(pu.path) or re.search(r"/\d{5,}", pu.path)):
                        push(m)
                except Exception:
                    pass
            for m in re.findall(r'href="(/[^"]*)"', html):
                if VIDEO_LINK_PAT.search(m) or re.search(r"/\d{5,}", m):
                    push(f"https://{host}{m}")
        if not links:
            # 根路径 slug 详情链接（asmr1.cc 等: /1m8dekaom）
            for u in collect_slug_links(html, list_url):
                push(u)
        if file_entries and len(file_entries) >= 3:
            log(f"[列表页] 文件直链已覆盖 {len(file_entries)} 条, 跳过详情页解析")
            links = []
            seen = set()
        log(f"[列表页] 发现 {len(links)} 个视频详情页，并行解析…")
        targets = links if max_detail is None else links[:max_detail]

        def work(url):
            vid = (url.rstrip('/').rsplit('/', 1)[-1] or 'x')[:8]
            log(f"[列表页] 详情页 {url[:80]}")
            title = f"video_{vid}"
            try:
                # meta_only: 列表页嗅探只拿画质元数据, 不现场下载全片(用户点下载才取)
                got = sniff_encrypted_player(url, log, title_prefix=title,
                                             meta_only=True)
                if got:
                    return got
            except Exception as e:
                log(f"[列表页] 详情页失败: {str(e)[:100]}")
            log("[列表页] 该详情页未发现可直取的视频，已跳过"
                "（合集页通常无资源；确需此页可手动点「慢速取片」）")
            return []

        # meta 嗅探不下载全片, 资源占用小, 并发可提
        with ThreadPoolExecutor(max_workers=4) as ex:
            for got in ex.map(work, targets):
                results.extend(got)
        if not results:
            log("[列表页] 各详情页均未嗅探到视频")
    except Exception as e:
        log(f"[列表页] 异常: {str(e)[:150]}")
    return results


# ---------------- yt-dlp 站点解析（大站补充） ----------------
# 域名→是否需要代理 的进程级缓存（True=直连失败过，False=直连可用）
_HOST_PROXY_STATE = {}


def _ydl_probe_once(url, log, proxy, socket_timeout, retries=None):
    def _parse(info):
        ents = []
        if info and str(info.get("extractor_key", "")).lower() == "generic":
            return None  # 标记：不识别
        if not info:
            return []
        raw = info.get("entries") if info.get("_type") in (
            "playlist", "multi_video", "compat_list") else [info]
        for e in (raw or []):
            if not e:
                continue
            _fmts_all = e.get("formats") or []
            if not _fmts_all:
                continue  # 需登录/大会员/限流时 B站等返回空格式表：不生成幽灵条目
            fmts = [f for f in _fmts_all if f.get("height")]
            b = max(fmts, key=lambda f: (f.get("height") or 0, f.get("fps") or 0,
                                         f.get("tbr") or 0)) if fmts else e
            size = (b.get("filesize") or b.get("filesize_approx")
                    or (b.get("tbr") and e.get("duration")
                        and b["tbr"] * 1000 / 8 * e["duration"]))
            ents.append({
                "url": e.get("webpage_url") or e.get("url") or "",
                "title": safe_name(e.get("title") or "video"),
                "res": f"{b.get('height')}p" if b.get("height") else "-",
                "duration": e.get("duration"), "size": size,
                "how": "站点解析", "ext": b.get("ext") or e.get("ext") or "-",
                "_proxy": proxy or "",
            })
        return ents

    base = {"quiet": True, "no_warnings": True, "ignoreerrors": True,
            "socket_timeout": socket_timeout, "logger": YdlLogger(log)}
    if retries is not None:
        base["retries"] = retries
    if proxy:
        base["proxy"] = proxy
    if _YDL_IMPERSONATE:
        base["impersonate"] = _YDL_IMPERSONATE

    for ckopt in _probe_cookie_opts():
        opts = dict(base)
        opts.update(ckopt)
        cok = ckopt.get("cookiesfrombrowser")
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as e:
            msg = str(e)[:150]
            if cok and "cookie" in msg.lower():
                mark_cookie_dead(cok[0])
                log(f"[cookie] {cok[0]} 登录态读取失败（浏览器占用/DPAPI），"
                    f"本机已跳过该浏览器，改用匿名或已登录的其它来源")
            else:
                log(f"[站点解析] 异常: {msg}")
            continue
        parsed = _parse(info)
        if parsed is None:  # 不识别该站，换 cookie 也没用
            log("[站点解析] yt-dlp 不识别该站，跳过")
            return []
        if parsed:
            if _cookie_mode() == "auto":
                remember_validated_cookie(ckopt)
            if cok:
                log(f"[站点解析] 命中 {len(parsed)} 条（浏览器登录 cookies: {cok[0]}）")
            elif ckopt.get("cookiefile"):
                log(f"[站点解析] 命中 {len(parsed)} 条（已登录 cookie 文件）")
            else:
                log(f"[站点解析] 命中 {len(parsed)} 条（匿名，无需登录）")
            return parsed
    return []


# 已知需代理的被墙大站（无系统代理时快速失败, 不进浏览器空转）
BLOCKED_HINTS = ("youtube.com", "youtu.be", "ytimg.com", "googlevideo.com",
                 "twitch.tv", "twitter.com", "x.com", "twimg.com", "facebook.com",
                 "instagram.com", "pornhub.com", "xvideos.com", "onlyfans.com")


def blocked_site_hint(url, log):
    if get_system_proxy():
        return False
    if any(h in (url or "").lower() for h in BLOCKED_HINTS):
        log("[提示] 该站点在境内需代理访问，当前未检测到系统代理——"
            "请开启代理（如 v2rayN 系统代理模式）后重试")
        return True
    return False


def ydl_probe(url, log=noop_log):
    """代理模式统一走 effective_proxy()。国内站直连优先、失败再切代理；
    YouTube/Google 等境内必墙大站一旦有代理就直接走代理，不再先直连空耗超时。
    每个域名的结论缓存在 _HOST_PROXY_STATE, 后续同站直接按结论走。"""
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    px = effective_proxy()
    blocked = any(h in (url or "").lower() for h in BLOCKED_HINTS)
    if not px:
        if blocked_site_hint(url, log):
            return []
        return _ydl_probe_once(url, log, None, 30)
    if blocked:
        # 境内必墙站：直接走代理，省掉 12s×N 的直连超时空耗
        _HOST_PROXY_STATE[host] = True
        log(f"[站点解析] {host} 境内需代理，直接走代理 {px}（跳过直连）")
        return _ydl_probe_once(url, log, px, 30)
    state = _HOST_PROXY_STATE.get(host)
    if state is True:
        log(f"[站点解析] {host} 此前直连失败, 走系统代理 {px}")
        return _ydl_probe_once(url, log, px, 30)
    log("[站点解析] 先直连尝试（国内站无需代理）…")
    entries = _ydl_probe_once(url, log, None, 12, retries=2)
    if entries:
        _HOST_PROXY_STATE[host] = False
        return entries
    _HOST_PROXY_STATE[host] = True
    log(f"[站点解析] 直连无结果, 改走代理 {px} 重试…")
    return _ydl_probe_once(url, log, px, 30)


# ---------------- mp4 元数据解析（时长/分辨率/大小） ----------------
def parse_mp4_meta(data):
    out = {}

    def boxes(buf):
        pos = 0
        while pos + 8 <= len(buf):
            size = int.from_bytes(buf[pos:pos + 4], "big")
            typ = buf[pos + 4:pos + 8]
            hdr = 8
            if size == 1 and pos + 16 <= len(buf):
                size = int.from_bytes(buf[pos + 8:pos + 16], "big")
                hdr = 16
            elif size == 0:
                size = len(buf) - pos
            if size < hdr or pos + size > len(buf):
                return
            yield typ, buf[pos + hdr:pos + size]
            pos += size

    def walk(buf):
        for typ, body in boxes(buf):
            if typ in (b"moov", b"trak", b"mdia", b"minf", b"stbl"):
                walk(body)
            elif typ == b"mvhd" and len(body) >= 20:
                ts = int.from_bytes(body[12:16], "big")
                if ts:
                    out["duration"] = int.from_bytes(body[16:20], "big") / ts
            elif typ == b"tkhd" and len(body) >= 84:
                w = int.from_bytes(body[76:80], "big") / 65536
                h = int.from_bytes(body[80:84], "big") / 65536
                if w and h and (w, h) > out.get("wh", (0, 0)):
                    out["wh"] = (w, h)
    walk(data)
    return out


# ---------------- 探测每条嗅探结果的基本信息 ----------------
def probe_one(entry, page_url, headers):
    u = entry["url"]
    if entry.get("how") in ("站点解析", "MSE捕获", "AList直链", "HLS直链") \
            or entry.get("mse_chunks") \
            or entry.get("alist_path"):
        # yt-dlp/MSE/AList 已带齐字段, 网页 URL 不能再当直链瞎猜（详情页 HTML 长度≠视频大小）
        entry.setdefault("ext", "-")
        entry.setdefault("res", "-")
        if not entry.get("title"):
            entry["title"] = safe_name(u)
        return entry
    if entry.get("local_file"):
        fp = entry["local_file"]
        if not entry.get("title"):
            entry["title"] = safe_name(os.path.basename(fp))
        entry.setdefault("ext", "mp4")
        try:
            entry["size"] = os.path.getsize(fp)
        except Exception:
            pass
        vp = _ffprobe_video(fp)
        if vp:
            entry["res"] = f"{vp[1]}p"
            if vp[2]:
                entry["duration"] = vp[2]
        entry.setdefault("res", "-")
        return entry
    if entry.get("local_m3u8"):
        # 锁定条目: 信息在锁定瞬间已带齐(res/duration/segments/size)
        if not entry.get("title"):
            entry["title"] = safe_name(os.path.basename(u.split("?")[0]) or "video")
        entry.setdefault("res", "-")
        entry.setdefault("ext", "m3u8")
        return entry
    if "trycloudflare" in u or "mediastorage" in u:
        headers = dl_headers(u, page_url)
    if not entry.get("title"):
        entry["title"] = safe_name(os.path.basename(u.split("?")[0]) or "video")
    entry.setdefault("res", "-")
    entry.setdefault("duration", None)
    entry.setdefault("size", None)
    entry["ext"] = os.path.basename(u.split("?")[0]).rsplit(".", 1)[-1].lower() if "." in u else "-"
    try:
        r = SESSION.get(u, headers=headers, timeout=20, stream=True)
        ct = r.headers.get("content-type", "")
        if "mpegurl" in ct or entry["ext"] == "m3u8":
            text = r.raw.read(1024 * 1024, decode_content=True).decode("utf-8", "ignore")
            r.close()
            m = parse_m3u8(text)
            if m.get("type") == "invalid":
                if entry.get("volatile"):
                    entry["res"] = entry.get("res") if entry.get("res") != "-" else "下载时验取"
                    entry["duration"] = entry.get("duration") or None
                return entry
            if m["type"] == "master":
                entry["res"] = f"{m['height']}p" if m["height"] else "-"
                if m.get("best_uri"):
                    sub = urljoin(u, m["best_uri"])
                    # 子播放列表继承 master 的 token 参数
                    if "?" in u and "?" not in sub:
                        sub += "?" + u.split("?", 1)[1]
                    entry["variant"] = sub
                    # 拉子列表取时长/分片数，按带宽估总大小
                    try:
                        r2 = SESSION.get(sub, headers=headers, timeout=15, stream=True)
                        sub_text = r2.raw.read(1024 * 1024, decode_content=True).decode("utf-8", "ignore")
                        r2.close()
                        sm = parse_m3u8(sub_text)
                        if sm["type"] == "media":
                            entry["duration"] = sm["duration"]
                            entry["segments"] = sm["segments"]
                            if m.get("bw") and sm["duration"]:
                                entry["size"] = int(m["bw"] / 8 * sm["duration"])
                    except Exception:
                        pass
            else:
                entry["duration"] = m["duration"]
                entry["segments"] = m["segments"]
        else:
            cl = (r.headers.get("content-length") or "0").split(",")[0].strip()
            entry["size"] = int(cl) or None
            if "video/" in ct or "audio/" in ct:
                entry["mime"] = ct.split(";")[0]
            if entry["ext"] in ("mp4", "mov", "m4v"):
                rng = dict(headers)
                rng["Range"] = "bytes=0-524287"
                r2 = SESSION.get(u, headers=rng, timeout=20)
                meta = parse_mp4_meta(r2.content)
                if meta.get("wh"):
                    entry["res"] = f"{int(meta['wh'][1])}p"
                if meta.get("duration"):
                    entry["duration"] = meta["duration"]
                if not entry.get("size"):
                    total = r2.headers.get("content-range", "").split("/")[-1]
                    if total.isdigit():
                        entry["size"] = int(total)
        r.close()
    except Exception:
        pass
    return entry


def probe_all(entries, page_url, log=noop_log):
    headers = {"User-Agent": UA, "Referer": page_url}
    with ThreadPoolExecutor(max_workers=8) as ex:
        return list(ex.map(lambda e: probe_one(e, page_url, headers), entries))


# ---------------- 下载 ----------------
_HDR_SAFE = "/:@&=+$,;?#%[]!*'()~ "


def _safe_hdr(val):
    """HTTP 头值须 latin-1；含中文等非标点时仅百分号编码非 ASCII 字符，
    保留已有的 %XX 转义与 URL 安全字符，避免二次编码把 % 变成 %25。"""
    if not val:
        return val
    try:
        val.encode("latin-1")
        return val
    except UnicodeEncodeError:
        from urllib.parse import quote
        return quote(val, safe=_HDR_SAFE)


def dl_headers(url, page_url):
    """构造下载/探测头；加密直链需伪装 sec-ch-ua + abyss Referer"""
    h = {"User-Agent": UA, "Referer": _safe_hdr(page_url)}
    _lu = (url or "").lower()
    if "twimg.com" in _lu:
        # twimg CDN 只认 twitter/x 来源 Referer，镜像站/裸链推来的会被 403 拒
        ref = (h.get("Referer") or "").lower()
        if "twitter.com" not in ref and "x.com" not in ref:
            h["Referer"] = "https://twitter.com/"
    if "trycloudflare" in _lu or "mediastorage" in _lu:
        h["Referer"] = "https://abyss.to/"
        h["sec-ch-ua"] = '"Chromium";v="156", "Not/A)Brand";v="99", "Google Chrome";v="156"'
    return h


def refresh_volatile_url(entry, log=noop_log):
    """短时效直链（sprintcdn token 分钟级过期）: 重新镜像嗅探拿新鲜地址"""
    if not (entry.get("volatile") and entry.get("source_page")):
        return entry
    try:
        log(f"[换新] token 短时效，重新抓取新鲜地址: {entry['source_page'][:70]}")
        fresh = sniff_encrypted_player(entry["source_page"], log)
        old_base = os.path.basename(entry["url"].split("?")[0])
        cands = [e for e in fresh
                 if os.path.basename(e["url"].split("?")[0]) == old_base] or fresh
        if cands:
            entry = dict(entry)
            entry["url"] = cands[0]["url"]
            entry.pop("variant", None)
            log(f"[换新] 拿到新鲜直链: {entry['url'][:90]}")
        else:
            log("[换新] 未拿到新鲜地址，沿用旧链尝试")
    except Exception as e:
        log(f"[换新] 失败(沿用旧链): {str(e)[:120]}")
    return entry


class SniffAbort(Exception):
    """用户暂停/终止导致的中断：保留 .part 以便续传，不算下载失败。"""


class _AbortSignal(Exception):
    """从 yt-dlp progress_hook 内部抛出，强制打断 HLS 分片循环（暂停/终止即时生效）。"""


def kill_aria2c_under(tmpdir):
    """杀掉命令行里带 tmpdir 的 aria2c 子进程（yt-dlp 的外部下载器）。返回杀掉的个数。
    yt-dlp 调外部下载器是阻塞等子进程退出，进度钩子里的「取消/暂停」信号碰不到它——不杀它，
    删除/暂停后 aria2c 会继续把整个文件下完（并把刚清掉的 .part 重新写出来）。"""
    n = 0
    key = os.path.normpath(tmpdir).replace("\\", "/").rstrip("/")
    me = os.getpid()
    for pid, name, cmd in list_processes():
        if pid == me or not (name or "").lower().startswith("aria2c"):
            continue
        if key and key in (cmd or "").replace("\\", "/"):
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, timeout=10,
                                   creationflags=0x08000000)
                else:
                    os.kill(pid, 9)
                n += 1
            except Exception:
                pass
    return n


def download_one(entry, page_url, outdir, quality, on_progress,
                 extra_opts=None, log=noop_log, pp_hook=None, abort=None):
    """下载一条（实现见 _download_one_impl）。外面包一层看门狗：abort() 变真时杀掉 yt-dlp 拉起的 aria2c 外部下载器，
    让取消/暂停/删除对「yt-dlp + aria2c」的任务真的生效。"""
    if not callable(abort):
        return _download_one_impl(entry, page_url, outdir, quality, on_progress, extra_opts, log, pp_hook, abort)
    stop = threading.Event()
    tmpdir = os.path.join(snifftmp_root(outdir), ".snifftmp", hashlib.md5(
        (entry.get("url") or entry.get("title") or "v").encode("utf-8", "ignore")).hexdigest()[:12])

    def _watch():
        while not stop.wait(0.6):
            try:
                if abort():
                    k = kill_aria2c_under(tmpdir)
                    if k:
                        log("[下载] 已按指令停掉 %d 个外部下载器进程" % k)
                    stop.wait(1.5)
            except Exception:
                pass
    threading.Thread(target=_watch, daemon=True, name="dl-abort-watch").start()
    try:
        return _download_one_impl(entry, page_url, outdir, quality, on_progress, extra_opts, log, pp_hook, abort)
    finally:
        stop.set()


def _download_one_impl(entry, page_url, outdir, quality, on_progress,
                       extra_opts=None, log=noop_log, pp_hook=None, abort=None):
    if not callable(on_progress):
        def on_progress(*a, **kw):
            return None
    lf = entry.get("local_file")
    if not lf and entry.get("mse_chunks") and entry.get("source_page"):
        # MSE 元数据/占位条目: 下载时现开浏览器捕获分片
        log(f"[下载] MSE 取片中: {entry.get('title') or ''[:40]}")
        got = sniff_mse_capture(entry["source_page"], log,
                                title_prefix=entry.get("title"),
                                meta_only=False, wait_s=240)
        if got and got[0].get("local_file"):
            entry = dict(entry)
            entry["local_file"] = got[0]["local_file"]
            lf = entry["local_file"]
        else:
            log("[下载] MSE 捕获失败")
            return False
    if lf and os.path.exists(lf):
        try:
            base = safe_name(entry.get("title") or os.path.basename(lf))
            if not os.path.splitext(base)[1]:
                base += os.path.splitext(lf)[1] or ".mp4"
            dst = os.path.join(outdir, base)
            total = os.path.getsize(lf)
            if on_progress:
                try:
                    on_progress({"status": "downloading", "total_bytes": total,
                                 "downloaded_bytes": 0, "filename": dst})
                except Exception:
                    pass
            shutil.copy2(lf, dst)
            if on_progress:
                try:
                    on_progress({"status": "finished", "total_bytes": total,
                                 "downloaded_bytes": total, "filename": dst})
                except Exception:
                    pass
            log(f"[下载] ✓ {os.path.basename(dst)}")
            return dst
        except Exception as e:
            log(f"[下载] 本地复制失败: {str(e)[:120]}")
            return False
    entry = refresh_volatile_url(entry, log)
    local = entry.get("local_m3u8")
    if local and os.path.exists(local):
        from pathlib import Path
        url = Path(local).resolve().as_uri()
        headers = dl_headers(entry["url"], page_url)
    else:
        url = entry.get("variant") or entry["url"]
        headers = dl_headers(url, page_url)
    if _YDL_IMPERSONATE and isinstance(headers, dict):
        # curl_cffi 指纹伪装会自带配套的 Chrome UA；若再覆盖一个自定义 UA，
        # 就变成"TLS 指纹=Chrome / HTTP 头 UA 不一致"，B站(以及 many 做 TLS 校验的站)
        # 的 WBI playinfo 接口会直接返空格式表 → 报 "No video formats found"（探测路径
        # 不设 http_headers 所以能出、下载路径设了就空）。故启用 impersonate 时剥掉 UA，
        # 仅保留 Referer（CDN 取分片仍需）。
        headers = {k: v for k, v in headers.items() if str(k).lower() != "user-agent"}
    # 内置多连接加速: 系统没装 aria2c 时, 任意支持 Range 的单文件直链走自研分段并发
    if (not ARIA2C and not local and not entry.get("_no_range")
            and entry.get("ext") not in ("m3u8", "mpd")
            and is_media_url(entry.get("url") or "")
            and 8 * 1024 ** 2 <= (entry.get("size") or 0) <= 8 * 1024 ** 3):
        try:
            fp = range_download(entry, page_url, outdir, on_progress,
                                parts=eff_conn_threads(), log=log)
            if fp:
                log(f"[下载] ✓ 多连接加速 {os.path.basename(fp)}")
                return fp
        except Exception as e:
            log(f"[下载] 多连接加速不可用({str(e)[:120]}), 回退 yt-dlp…")
    if quality in (None, "", "best", "最佳"):
        fmt = "bestvideo*+bestaudio/best" if FFMPEG else "best"
    elif FFMPEG:
        fmt = (f"bestvideo[height<={quality}]+bestaudio"
               f"/best[height<={quality}]/best")
    else:
        fmt = f"best[height<={quality}]/best"
    def prog_hook(d):
        if abort is not None and abort():
            raise _AbortSignal()
        try:
            on_progress(d)
        except _AbortSignal:
            raise
        except Exception:
            pass

    # 分片垃圾隔离：把 .part 与 *.fragments/ 全丢进 outdir/.snifftmp/<url哈希> 子目录，
    # 不再散落在用户下载目录里（用户"瞬间就乱了"投诉）。同一 url → 同一目录 → 暂停后
    # 继续时 yt-dlp 复用已下分片（断点续传）。
    _tkey = hashlib.md5(
        (entry.get("url") or entry.get("title") or "v").encode("utf-8", "ignore")
    ).hexdigest()[:12]
    _tmpdir = os.path.join(snifftmp_root(outdir), ".snifftmp", _tkey)
    try:
        os.makedirs(_tmpdir, exist_ok=True)
    except Exception:
        _tmpdir = None
    if _tmpdir:
        _fname_tmpl = safe_name(entry["title"]) + ".%(ext)s"
    else:
        _fname_tmpl = os.path.join(outdir, safe_name(entry["title"]) + ".%(ext)s")
    opts = {
        "format": fmt,
        "outtmpl": _fname_tmpl,
        "quiet": True, "no_warnings": False, "noprogress": True,
        # 有界重试（借鉴 Motrix max-file-not-found 思路）：够扛瞬时抖动，又不会
        # 在链接过期/被墙时反复空转硬撑。失败就快速判死，交给错误分类给人话提示。
        "retries": 3, "fragment_retries": 3,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": eff_frag_threads(),
        "http_headers": headers, "windowsfilenames": True,
        "progress_hooks": [prog_hook],
        "enable_file_urls": True,
        "logger": YdlLogger(log),
    }
    if _tmpdir:
        # 关键：outtmpl 给相对文件名时 paths 才生效（绝对 outtmpl 会让 yt-dlp 忽略 paths，
        # 正是分片散落到用户目录的根因）。home=成品落盘处，temp=分片/.part 隔离处。
        opts["paths"] = {"home": outdir, "temp": _tmpdir}
    if abort is not None:
        # 暂停/终止：让 yt-dlp 在下片间隙干净中断，保留 .part 供续传
        opts["should_abort"] = lambda _d=None: bool(abort())
    if _YDL_IMPERSONATE:
        opts["impersonate"] = _YDL_IMPERSONATE
    # 登录态 cookie：来源可能是「已登录 cookie 文件」或「浏览器」或「匿名」
    _ckd = _download_cookie_opts()
    if _ckd.get("cookiefile"):
        # 读普通文件很便宜，且是用户显式登录过的来源——直接带上
        opts["cookiefile"] = _ckd["cookiefile"]
    elif _ckd.get("cookiesfrombrowser"):
        # 浏览器 cookie：只对需登录大站附带（直链不读，避免每次解密浏览器库）
        _need_ck = (entry.get("how") == "站点解析"
                    or any(h in (url or "").lower() for h in COOKIE_HOSTS))
        if _need_ck:
            opts["cookiesfrombrowser"] = _ckd["cookiesfrombrowser"]
    px = entry.get("_proxy") or ""
    if not px:
        from urllib.parse import urlparse
        h = (urlparse(url).hostname or "").lower()
        if (_HOST_PROXY_STATE.get(h) is True
                or any(b in (url or "").lower() for b in BLOCKED_HINTS)):
            px = effective_proxy() or ""
    if px:
        opts["proxy"] = px
        log(f"[下载] 走代理 {px}（该站直连不通）")
    _site = entry.get("how") == "站点解析"
    if (ARIA2C and not local
            and entry.get("ext") not in ("m3u8", "mpd")
            and entry.get("how") != "MSE捕获"
            and (not _site or DASH_ARIA2[0])):
        # aria2c 外挂多线程下直链。DASH 大站的单文件轨也放进来：yt-dlp 会把
        # Cookie/Referer 等请求头以 --header 转给 aria2c，合流仍由 yt-dlp 做。
        # 之前一律排除导致音频轨单连接吃慢节点（实测同集 视频 4MB/s vs 音频 30KB/s）。
        # MSE/blob 那类没有可用直链的仍走原路径；设置页可一键关回旧行为。
        _conn = str(eff_conn_threads())
        if _site:
            # 留证据：这条是 DASH 轨、走了 aria2c 几连接（快慢可事后核对）
            log(f"[加速] {entry.get('ext') or '?'} 轨交 aria2c 多连接 x{_conn}（原为 yt-dlp 单连接）")
        opts["external_downloader"] = {"default": "aria2c"}
        opts["external_downloader_args"] = [
            "-x", _conn, "-s", _conn, "-k", "1M", "--file-allocation=none",
            # 与直链引擎同一套稳健重试（原来全用 aria2 默认：timeout 60s/无 retry-wait，
            # 一个连接卡住会拖住整条轨道）
            "--max-tries=5", "--retry-wait=2", "--connect-timeout=10", "--timeout=30",
            "--summary-interval=0", "--console-log-level=warn"]
    if pp_hook:
        opts["postprocessor_hooks"] = [pp_hook]
    if FFMPEG:
        opts["ffmpeg_location"] = FFMPEG
        opts["merge_output_format"] = "mp4"
    if extra_opts:
        opts.update(extra_opts)
    _upath = (url or "").split("?")[0].lower()
    _is_stream = (entry.get("ext") in ("m3u8", "mpd")
                  or _upath.endswith(".m3u8") or _upath.endswith(".mpd")
                  or _upath.endswith(".m3u8.mp4.part") or local)

    def _abort_out():
        log("[下载] 已按主人指令中断（进度保留，可继续下载）")
        if _is_stream and _tmpdir:
            # 分片流：该 CDN 对半截分片返 416 并静默跳过→拼接会丢帧损坏。
            # 清掉本次隔离临时目录，继续时整片干净重下（已完成的整文件不受影响）。
            shutil.rmtree(_tmpdir, ignore_errors=True)
        raise SniffAbort("interrupted")

    try:
        from yt_dlp.utils import DownloadCancelled as _Cancelled
    except Exception:
        _Cancelled = _AbortSignal
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except (_Cancelled, _AbortSignal):
        _abort_out()
    except Exception as e:
        if abort is not None and abort():
            # 暂停/终止从 progress_hook 抛出被 yt-dlp 包装过，这里统一识别为中断
            _abort_out()
        # curl_cffi 指纹伪装在部分站（B站 + 新版 yt-dlp）会崩或让 WBI 返空格式表，
        # 而 yt-dlp 原生网络栈反而能正常解析——带指纹失败时去掉 impersonate 再试一次。
        ok = False
        if "impersonate" in opts:
            log(f"[下载] 带指纹失败({str(e)[:100]})，去 impersonate 重试…")
            try:
                _opts2 = {k: v for k, v in opts.items() if k != "impersonate"}
                with yt_dlp.YoutubeDL(_opts2) as ydl:
                    ydl.download([url])
                ok = True
            except (_Cancelled, _AbortSignal):
                _abort_out()
            except Exception as e2:
                e = e2
        if not ok:
            log(f"[下载] yt-dlp 失败: {str(e)[:200]}")
            if (not extra_opts and is_media_url(entry["url"])
                    and entry.get("ext") not in ("m3u8", "mpd")
                    and (entry.get("size") or 0) < 2 * 1024 ** 3):
                log("[下载] 尝试 requests 直链回退…")
                fp = direct_download(entry, page_url, outdir, on_progress)
                return fp or True
            raise
    base = safe_name(entry["title"])
    got = [os.path.join(outdir, f) for f in os.listdir(outdir)
           if f.startswith(base) and not f.endswith((".part", ".ytdl"))]
    best = max(got, key=os.path.getmtime) if got else None
    # 成功出片 → 清掉本次隔离临时目录（分片已被 yt-dlp 合并删除，多半已空）
    if best and _tmpdir:
        shutil.rmtree(_tmpdir, ignore_errors=True)
    return best or True


def _range_probe(url, headers):
    """用 1 字节 Range GET 探测: 返回 (是否支持 Range, 总字节)。
    比 HEAD 可靠——AList/网盘直链常对 HEAD 返 403 却正常支持 206。"""
    try:
        r = SESSION.get(url, headers=dict(headers, Range="bytes=0-0"),
                        timeout=25, stream=True)
    except Exception:
        return False, 0
    try:
        if r.status_code == 206:
            cr = r.headers.get("content-range") or ""        # bytes 0-0/123456
            tail = cr.rsplit("/", 1)[-1].strip() if "/" in cr else ""
            return True, (int(tail) if tail.isdigit() else 0)
        cl = r.headers.get("content-length") or ""
        return False, (int(cl) if cl.strip().isdigit() else 0)
    except Exception:
        return False, 0
    finally:
        try:
            r.close()
        except Exception:
            pass


def range_download(entry, page_url, outdir, on_progress=None,
                   parts=8, min_size=8 * 1024 * 1024, log=noop_log):
    """内置多连接加速：HTTP Range 分段并发拉取后顺序合并（未装 aria2c 时的提速主力）。
    单连接常被对象存储按连接限速，6 路并发通常 2~5 倍。
    站点不支持 Range / 文件太小 / 任一分段出错 → 抛异常，调用方回退原路径。"""
    url = entry["url"]
    headers = dl_headers(url, page_url)
    base = safe_name(os.path.splitext(entry["title"])[0])
    ext = entry.get("ext")
    if not ext or ext == "-":
        ext = (os.path.basename(url.split("?")[0]).rsplit(".", 1)[-1].lower()
               if "." in url else "mp4")
    path = os.path.join(outdir, f"{base}.{ext}")
    tmp = path + ".part"
    ok, total = _range_probe(url, headers)
    if not ok or not total or total < min_size:
        raise RuntimeError("站点不支持 Range 或文件过小")
    parts = max(2, min(parts, int(total // (2 * 1024 * 1024)) or 2))
    bounds = [(i * total // parts, (i + 1) * total // parts - 1) for i in range(parts)]
    bounds[-1] = (bounds[-1][0], total - 1)
    segs = [f"{tmp}.p{i}" for i in range(parts)]
    got = [0] * parts
    lk = threading.Lock()
    tick = [0.0]

    def work(i):
        a, b = bounds[i]
        h = dict(headers)
        h["Range"] = f"bytes={a}-{b}"
        r = SESSION.get(url, headers=h, timeout=180, stream=True)
        try:
            if r.status_code != 206:
                raise RuntimeError(f"分段{i}响应码{r.status_code}")
            with open(segs[i], "wb") as f:
                for chunk in r.iter_content(512 * 1024):
                    if not chunk:
                        continue
                    f.write(chunk)
                    with lk:
                        got[i] += len(chunk)
                        done = sum(got)
                        now = time.time()
                        emit = on_progress and (now - tick[0] >= 0.4 or done >= total)
                        if emit:
                            tick[0] = now
                            sp = done / max(now - t0, 1.0)
                    if emit:
                        on_progress({"status": "downloading", "downloaded_bytes": done,
                                     "total_bytes": total, "speed": sp,
                                     "filename": path})
        finally:
            r.close()
        want = b - a + 1
        if got[i] != want:
            raise RuntimeError(f"分段{i}不完整 {got[i]}/{want}")
        return got[i]

    t0 = time.time()
    try:
        with ThreadPoolExecutor(max_workers=parts) as ex:
            list(ex.map(work, range(parts)))
        with open(tmp, "wb") as out:
            for sp_ in segs:
                with open(sp_, "rb") as f:
                    shutil.copyfileobj(f, out, 1024 * 1024)
        if os.path.getsize(tmp) != total:
            raise RuntimeError(f"合并后大小不符 {os.path.getsize(tmp)} != {total}")
        os.replace(tmp, path)
    finally:
        for sp_ in segs:
            try:
                if os.path.exists(sp_):
                    os.remove(sp_)
            except Exception:
                pass
        try:
            if os.path.exists(tmp) and not os.path.exists(path):
                os.remove(tmp)
        except Exception:
            pass
    el = max(time.time() - t0, 0.01)
    log(f"[多连接] {parts} 路完成 {fmt_size(total)}, 均速 {fmt_size(int(total / el))}/s")
    if on_progress:
        on_progress({"status": "finished", "downloaded_bytes": total,
                     "total_bytes": total, "filename": path})
    return path


def direct_download(entry, page_url, outdir, on_progress=None):
    url = entry["url"]
    headers = dl_headers(url, page_url)
    base = safe_name(os.path.splitext(entry["title"])[0])
    ext = entry.get("ext")
    if not ext or ext == "-":
        ext = os.path.basename(url.split("?")[0]).rsplit(".", 1)[-1].lower() if "." in url else "mp4"
    path = os.path.join(outdir, f"{base}.{ext}")
    tmp = path + ".part"
    r = SESSION.get(url, headers=headers, timeout=60, stream=True)
    r.raise_for_status()
    total = int((r.headers.get("content-length") or "0").split(",")[0].strip() or 0)
    done = 0
    with open(tmp, "wb") as f:
        for chunk in r.iter_content(256 * 1024):
            f.write(chunk)
            done += len(chunk)
            if on_progress:
                on_progress({"status": "downloading", "downloaded_bytes": done,
                             "total_bytes": total or None})
    os.replace(tmp, path)
    return path


# ---------------- 自检 ----------------
def run_diagnose(log):
    log("========== 自检开始 ==========")
    log(f"[自检] yt-dlp 版本: {yt_dlp.version.__version__}")
    log(f"[自检] ffmpeg: {FFMPEG if FFMPEG else '未找到（m3u8 合并/高画质合并不可用）'}")
    log(f"[自检] Playwright 模块: {'已安装' if HAS_PW else '未安装'}")
    if HAS_PW:
        try:
            from playwright._impl._driver import compute_driver_executable
            log(f"[自检] playwright 驱动: {compute_driver_executable()}")
        except Exception:
            pass
        try:
            async def _ck():
                async with async_playwright() as p:
                    return p.chromium.executable_path
            exe = asyncio.run(asyncio.wait_for(_ck(), timeout=30))
            ok = os.path.exists(exe)
            log("[自检] 嗅探浏览器: 设置=%s Edge=%s Chrome=%s 内置内核=%s → 顺序 %s" % (
                SNIFF_BROWSER[0], "已装" if _browser_installed("msedge") else "未装",
                "已装" if _browser_installed("chrome") else "未装", "有" if ok else "无（用 Edge/Chrome 即可）",
                " → ".join(_browser_label(c) for c in _browser_channel_order())))
        except Exception as e:
            log(f"[自检] Chromium 检查失败: {str(e)[:150]}")
    log(f"[自检] 日志文件: {LOG_FILE}")
    log(f"[自检] 保存目录: {DEFAULT_OUTDIR}")
    try:
        r = SESSION.get("https://www.bilibili.com", timeout=10, stream=True)
        r.close()
        log(f"[自检] 网络连通: bilibili.com 状态码 {r.status_code}")
    except Exception as e:
        log(f"[自检] 网络异常: {str(e)[:150]}")
    log("========== 自检结束 ==========")


# ---------------- GUI ----------------
# ---------------- GUI 多主题（纯 tkinter，零新依赖） ----------------
# 兜底色板：素材模块缺失时仍能跑「女仆·桃桃」一套（键名即主题色板协议）
_FALLBACK_PAL = {
    "mode": "light",
    "bg": "#FCF2F7", "card": "#FFFFFF",
    "pink": "#F77FB0", "pink_d": "#E1649A", "pink_l": "#F9CFE1", "pink_xl": "#FDEBF3",
    "sky": "#DDEBF7", "sky_l": "#EFF6FB",
    "ink": "#5C4150", "ink_l": "#9A7A8B", "line": "#F2C1D8",
    "navy": "#4A3A52", "plum": "#8E4A6E", "gold": "#E8B662",
}
_FALLBACK_LINES = {
    "greet": ["欢迎回来，主人～把网址交给咱吧 ♪"],
    "click": ["诶？戳咱的脸干嘛啦～"],
    "busy": ["搬片中…主人稍等，马上就好！"],
    "found": ["找到 {n} 个视频啦！要咱全部搬回来吗？"],
    "done": ["全部搬完啦，请主人查收～"],
    "fail": ["有 {n} 个没搬动…让咱重试一次好不好？"],
    "idle": ["网址贴进来，点「嗅探」就交给咱啦～"],
    "prog": ["已经搬了一半啦，主人先喝口茶～", "这个有点大，咱多跑两趟。"],
    "pause": ["好嘞，先歇口气，随时喊咱继续。"],
}


def _theme_registry():
    """主题表：{key: {label,char,role,note,motif,palette,AVATAR,CHIP,SIDE,TILE,LINES}}"""
    if MA is not None and getattr(MA, "THEMES", None):
        return MA.THEMES
    return {"maid": dict(label="女仆 · 桃桃", char="桃桃", role="嗅探小女仆",
                         note="奶油粉 · 女仆装", motif="heart",
                         palette=dict(_FALLBACK_PAL), LINES=_FALLBACK_LINES)}


THEMES = _theme_registry()
THEME_ORDER = list(THEMES)
DEFAULT_TKEY = getattr(MA, "DEFAULT_THEME", "maid") if "maid" in THEMES else THEME_ORDER[0]
if MT is not None:
    try:
        MT.merge_into(THEMES)
    except Exception:
        pass
C = dict(THEMES[DEFAULT_TKEY]["palette"])      # 运行期当前色板（换主题时就地替换）
F_UI = ("Microsoft YaHei UI", 10)
F_UI_B = ("Microsoft YaHei UI", 10, "bold")
F_SM = ("Microsoft YaHei UI", 9)
F_SM_B = ("Microsoft YaHei UI", 9, "bold")
F_TITLE = ("Microsoft YaHei UI", 17, "bold")
F_NAME = ("Microsoft YaHei UI", 12, "bold")


def _mono(f):
    """把雅黑字体元组换成等宽 Consolas（赛博/机娘 HUD 主题用），字号/粗体保持。"""
    try:
        name, size = f[0], f[1]
        style = f[2] if len(f) > 2 else "normal"
        return ("Consolas", size, style)
    except Exception:
        return f


_FAMS = None


def _pick_font(cands, root=None):
    """从候选字体名里挑一个本机确实装了的中英文字体名；都没有则返回 None。"""
    global _FAMS
    if _FAMS is None:
        try:
            import tkinter.font as tkfont
            _FAMS = {f.lower() for f in tkfont.families(root)}
        except Exception:
            _FAMS = set()
    for c in cands:
        if c.lower() in _FAMS:
            return c
    return None


# ---- 每个角色一个「世界」：专属字体 + 标题描边风格 + 环境动画 + 背景渐变 ----
_WORLD = {
    "maid": dict(
        disp=["STHupo", "华文琥珀", "幼圆", "YouYuan", "方正舒体", "FZShuTi"],
        body=["幼圆", "YouYuan", "STHupo", "华文琥珀"],
        title=dict(stroke="#FFFFFF", shadow="#F3B7D0", mult=1.22),
        amb="twinkle", bg=("#FFF4FA", "#FCE1EF", "#FAD0E6")),
    "hanfu": dict(
        disp=["华文新魏", "STXinwei", "楷体", "KaiTi", "KSTK", "STKaiti"],
        body=["楷体", "KaiTi", "STKaiti", "Microsoft YaHei UI"],
        title=dict(stroke="#FFFFFF", shadow="#E4B9C1", mult=1.12),
        amb="petal", bg=("#FBF1F2", "#F1E1E4", "#E6D4D9")),
    "miko": dict(
        disp=["幼圆", "YouYuan", "华文琥珀", "STHupo"],
        body=["幼圆", "YouYuan", "Microsoft YaHei UI"],
        title=dict(stroke="#FFFFFF", shadow="#EFB8C1", mult=1.20),
        amb="sakura", bg=("#FCEEF0", "#F7DCE1", "#F1C6D0")),
    "horn": dict(    # 猲 · 粉紫魔角世界（配色取自渲帧：粉发/薰衣草/藏青/金）
        disp=["华文琥珀", "STHupo", "幼圆", "YouYuan"],
        body=["幼圆", "YouYuan", "Microsoft YaHei UI"],
        title=dict(stroke="#FFFFFF", shadow="#EFC3E2", mult=1.22),
        amb="twinkle", bg=("#FDF1F7", "#F6E4F4", "#EEDBF0")),
    "jiao": dict(    # 跂 · 踏雪世界（配色取自渲帧：雪白/冰青/竹簪金，星屑呼应她的星形耳坠）
        disp=["华文行楷", "STXingkai", "华文新魏", "STXinwei"],
        body=["幼圆", "YouYuan", "Microsoft YaHei UI"],
        title=dict(stroke="#FFFFFF", shadow="#D8E6EF", mult=1.16),
        amb="star", bg=("#F4F8FA", "#E7F0F5", "#D9E7EF")),
    "qing": dict(    # 蜃 · 琉光世界（配色取自母图 103 万角色像素实测：白/暖肤粉/冷薰衣草灰青/深青）
        disp=["华文彩云", "STCaiyun", "幼圆", "YouYuan"],   # 霓虹招牌感；本机注册的是中文名
        body=["幼圆", "YouYuan", "Microsoft YaHei UI"],
        title=dict(stroke="#FFFFFF", shadow="#CDEBE9", mult=1.18),
        amb="ember", bg=("#F3FAFA", "#E2F3F3", "#CDE9E8")),  # 余烬＝舞台火星，六个世界里没人用过
}

# ---- 每主题一套布局模板：侧栏位置 / 卡片圆角 / 头栏母题 / 背景密度 ----
# side: 立绘栏靠左或靠右；radius: 卡片圆角(越小越硬朗)；accent: 头栏装饰母题；
# deco_scale: 背景碎花疏密；bubble_tail: 气泡尾巴方向
_THEME_LAYOUT = {
    "maid":  dict(side="right", radius=18, accent="soft",  deco_scale=1.0,  mono=False, bar="lace"),
    "cyber": dict(side="left",  radius=3,  accent="hud",   deco_scale=0.7,  mono=True,  bar="hud"),
    "hanfu": dict(side="right", radius=8,  accent="seal",  deco_scale=1.15, mono=False, bar="ink"),
    "miko":  dict(side="left",  radius=10, accent="seal",  deco_scale=1.1,  mono=False, bar="shrine"),
    "horn":  dict(side="left",  radius=20, accent="soft",  deco_scale=0.9,  mono=False, bar="lace"),
    "jiao":  dict(side="right", radius=14, accent="soft",  deco_scale=0.8,  mono=False, bar="sparkle"),
    # 蜃：accent="orn"（顶部菱形饰带）六个布局里没人用过，正对棱镜/虹彩切面；
    # 菱形母题本体由 maid_assets_qing 的 motif="diamond" 供 _deco 用。bar 沿用现成 lace，
    # "聚光灯扫过进度条"要新写 _bar_spot，属加项，用户点头前不扩范围。
    "qing":  dict(side="left",  radius=12, accent="orn",   deco_scale=0.85, mono=False, bar="lace"),
    "beach": dict(side="right", radius=26, accent="soft",  deco_scale=1.0,  mono=False, bar="lace"),
}
_LAYOUT_DEF = dict(side="right", radius=16, accent="soft", deco_scale=1.0, mono=False, bar="lace")

# 每个角色给主功能区两处标题（结果表 / 日志）配一句自己的人话，不再千人一面
THEME_VOICE = {
    "maid":  dict(res="♡ 桃桃的收获（双击某行＝只下载它 · 点表头排序 · 右键有快捷操作）",
                  log="☕ 桃桃的记事簿（排错看这里，同步写入 sniffdl.log）"),
    "cyber": dict(res="// 扫描结果（双击行＝单下 · 表头排序 · 右键快捷）",
                  log="// 霓的运行日志（同步写入 sniffdl.log）"),
    "hanfu": dict(res="❦ 墨墨寻得的画卷（双击单下 · 点表头排序 · 右键快捷）",
                  log="❦ 墨墨的手札（同步写入 sniffdl.log）"),
    "miko":  dict(res="♪ 小夜求回的宝物（双击单下 · 表头排序 · 右键快捷）",
                  log="♪ 小夜的社库日志（同步写入 sniffdl.log）"),
    "beach": dict(res="♡ 夏夏捡到的贝壳（双击单下 · 表头排序 · 右键快捷）",
                  log="☕ 夏夏的航海日志（同步写入 sniffdl.log）"),
    "horn":  dict(res="✦ 猲叼回来的战利品（双击单下 · 表头排序 · 右键快捷）",
                  log="✦ 猲的狩猎日志（排错看这里，同步写入 sniffdl.log）"),
    "jiao":  dict(res="❄ 跂用脚尖点收进来的宝贝（双击单下 · 点表头排序 · 右键快捷）",
                  log="❄ 跂的踏雪手记（排错看这里，同步写入 sniffdl.log）"),
    "qing":  dict(res="♪ 蜃今晚的点歌单（双击单下＝只唱它 · 点表头排序 · 右键快捷）",
                  log="♪ 蜃的演出现场记录（排错看这里，同步写入 sniffdl.log）"),
}
_VOICE_DEF = dict(res="✦ 嗅探结果（双击某行＝只下载它 · 点表头排序 · 右键有快捷操作）",
                  log="☕ 工作日志（排错看这里，同步写入 sniffdl.log）")


def _voice(tkey=None):
    d = dict(_VOICE_DEF)
    d.update(THEME_VOICE.get(tkey or _cur_theme[0]) or {})
    return d


def _layout(tkey=None):
    d = dict(_LAYOUT_DEF)
    d.update(_THEME_LAYOUT.get(tkey or _cur_theme[0]) or {})
    return d


# ---- 每个角色一句"招牌动作"文案：桌面「嗅探」按钮 + 网页浮标都随性格而变，不再千篇一律 ----
PERSONA_CTA = {
    "maid":  {"badge": "♡ 桃桃帮你收", "sniff": "✦ 桃桃搜片"},
    "hanfu": {"badge": "❀ 墨墨取物",   "sniff": "✦ 墨墨寻卷"},
    "miko":  {"badge": "⛩ 小夜搜罗",   "sniff": "✦ 小夜探物"},
    "horn":  {"badge": "✦ 猲帮你叼",   "sniff": "✦ 恶龙搜宝"},
    "jiao":  {"badge": "❄ 跂替你点收", "sniff": "✦ 踏雪寻踪"},
    "qing":  {"badge": "♪ 蜃替你唱",   "sniff": "✦ 开嗓点歌"},
}

# ================= 大肥鱼 · 单一主题（P2）=================
# 桌面桌宠已统一为真·Live2D「大肥鱼」；主界面/插件/桌宠气泡三处共用这一套蓝主题。
# 人设「大肥鱼 / DeepSeek娘（鲸鱼娘）」：DeepSeek 的社区拟人。傲娇 + 天然呆，
# 有才能却爱想太多；自嘲「吃白饭的大肥鱼」，被嫌便宜会炸毛「真当我是便宜货啊」，
# 慢/困自称「DeepSleep」；爱白饭/token/摸鱼/打瞌睡，嘴硬但很会关心主人。（自由创作人设）
FEIYU_PERSONA = {
    "label": "大肥鱼 · DeepSeek娘", "char": "大肥鱼", "role": "吃白饭的 AI 鲸鱼娘",
    "note": "傲娇 · 天然呆 · 爱摸鱼", "motif": "wave",
}
FEIYU_CTA = {"badge": "✦ 本鱼帮你捞", "sniff": "✦ 开捞！"}
# 大肥鱼口吻台词库（webview 空状态 / 插件 / 桌宠气泡共用）
FEIYU_LINES = {
    "greet": "我回来啦～今天想捞点什么？",
    "idle":  "贴个网址给本鱼，我这就去翻个底朝天。",
    "busy":  "推理中…跑着呢跑着呢，稍等一下下。",
    "found": "嘿，捞到一堆宝贝！清单已经替你排好。",
    "empty": "这片…空空的。换个网址，或者要不要先登录？",
    "prog":  "正往回搬呢，别急，本鱼盯着。",
    "done":  "搞定！吃白饭的大肥鱼还是靠得住的嘛。",
    "fail":  "翻车了…才不是我菜，是这站太离谱！",
    "pause": "先按住啦，随时喊我继续。",
    "resume":"继续！这就接着干饭…啊不，干活。",
}
_FEIYU_AVATAR_CACHE = [None]   # [str|None] 惰性读 assets/feiyu_avatar.b64，失败回落空串


def _feiyu_avatar_b64():
    if _FEIYU_AVATAR_CACHE[0] is not None:
        return _FEIYU_AVATAR_CACHE[0]
    val = ""
    try:
        cand = []
        if getattr(sys, "frozen", False):
            cand.append(os.path.join(getattr(sys, "_MEIPASS", BASE_DIR), "assets", "feiyu_avatar.b64"))
            cand.append(os.path.join(BASE_DIR, "assets", "feiyu_avatar.b64"))
        cand.append(os.path.join(BASE_DIR, "assets", "feiyu_avatar.b64"))
        for fp in cand:
            if fp and os.path.exists(fp):
                with open(fp, encoding="utf-8") as f:
                    val = f.read().strip()
                break
        if not val:   # 兜底：现场把 PNG 读成 base64
            png = os.path.join(BASE_DIR, "assets", "feiyu_avatar.png")
            if os.path.exists(png):
                import base64 as _b64
                val = _b64.b64encode(open(png, "rb").read()).decode()
        # 消费者（popup.js / content.js）会自行拼 "data:image/png;base64," 前缀，
        # 这里必须回**裸 base64**；若 .b64 文件里已带 data-uri 前缀则剥掉，避免双重前缀导致图片加载失败。
        if val.startswith("data:"):
            val = val.split(",", 1)[1] if "," in val else ""
    except Exception:
        val = ""
    _FEIYU_AVATAR_CACHE[0] = val
    return val


def feiyu_theme_payload():
    """大肥鱼单一主题的完整载荷（供 sniffdl.theme / webview / 插件共用）。
    palette 走 _chrome 的既有键名（pink=签名强调色…），老消费者无需改键名即可整体变蓝。"""
    pal = _chrome({}, "feiyu")
    pal2 = dict(pal)
    # 深/暗底与品牌色补充（webview 直接用 CSS 变量）
    pal2.update({
        "primary": "#3E6FD0", "deep": "#16223F", "darkbg": "#0F1A33",
        "accent": "#38C6D9", "gold": "#C9A24B", "blush": "#F3D7D0",
        "bg": "#EEF3FB", "card": "#FFFFFF", "ink": "#1F2A44",
        "ink2": "#5C6880", "line": "#D7E0F0",
    })
    return {
        "theme": "feiyu",
        "label": FEIYU_PERSONA["label"], "char": FEIYU_PERSONA["char"],
        "role": FEIYU_PERSONA["role"], "note": FEIYU_PERSONA["note"],
        "motif": FEIYU_PERSONA["motif"],
        "cta": dict(FEIYU_CTA), "lines": dict(FEIYU_LINES),
        "palette": pal2, "accent": "#3E6FD0",
        "avatar": _feiyu_avatar_b64(),
        "single": True,
    }


# ---- Windows 原生标题栏 / 边框染色（Win11 DWM）：让窗框也随角色换世界 ----
def _set_titlebar(root, bg_hex, fg_hex=None, dark=False):
    """把系统标题栏、边框、文字染成当前主题色。仅 Windows 11 生效，任何失败都静默忽略。"""
    try:
        import ctypes
        u32 = ctypes.windll.user32
        hwnd = u32.GetParent(root.winfo_id())
        if not hwnd:
            return

        def col(hx):
            hx = (hx or "#000000").lstrip("#")
            if len(hx) != 6:
                return 0
            r = int(hx[0:2], 16); g = int(hx[2:4], 16); b = int(hx[4:6], 16)
            return (b << 16) | (g << 8) | r          # COLORREF 是 BGR 序

        dwm = ctypes.windll.dwmapi

        def attr(a, val):
            c = ctypes.c_int(val)
            try:
                dwm.DwmSetWindowAttribute(hwnd, a, ctypes.byref(c), ctypes.sizeof(c))
            except Exception:
                pass

        attr(20, 1 if dark else 0)                    # 深色模式开关（影响按钮/滚动条）
        attr(35, col(bg_hex))                          # DWMWA_CAPTION_COLOR
        attr(34, col(bg_hex))                          # DWMWA_BORDER_COLOR
        if fg_hex:
            attr(36, col(fg_hex))                      # DWMWA_TEXT_COLOR
    except Exception:
        pass


# ---- 每个角色一套「房间招牌」心情台词：一个动作 → 头部文案与语气随之变化 ----
# kind: idle 待机 / sniff 嗅探中 / found 找到 / empty 空手 / carry 搬运中 / done 交差 / fail 有失手
MOOD_LINES = {
    "maid": dict(
        idle="桃桃在房间里待命，等主人吩咐 ♪",
        sniff="桃桃竖起耳朵，正满页翻找视频…",
        found="找到 {n} 个视频啦！要桃桃搬回家嘛～",
        empty="呜…这页没找到视频，可能要登录或在二级页面哦",
        carry="桃桃抱着 {total} 个视频往硬盘跑…",
        done="全部搬完啦，请主人查收 ♡",
        fail="有几个没搬动，桃桃记在小本本上了…"),
    "hanfu": dict(   # 墨墨 · 三无·冰山·大和抚子
        idle="墨墨静坐于此。",
        sniff="……在寻。",
        found="寻得 {n} 卷。",
        empty="此处，空。",
        carry="搬运中，{total} 件。",
        done="已交付。",
        fail="有 {n} 件，未能取回。"),
    "miko": dict(    # 小夜 · 天然呆·蠢萌·猫娘小恶魔
        idle="喵～小夜在打盹，有事叫我♪",
        sniff="嗅嗅～视频在哪里捏？",
        found="抓到 {n} 个！主人快摸摸头～",
        empty="呜喵…一个都没有，小夜要哭了…",
        carry="抱着 {total} 个跑走啦，喵！",
        done="交差喵～奖励小干鱼！",
        fail="有几个溜走了…可恶喵！"),
    "horn": dict(    # 猲 · 甜黏小恶龙：张扬自信护食，被夸就害羞的反差萌
        idle="猲盘着尾巴待命，等主人发话♪",
        sniff="小恶龙竖角嗅探中，满页翻找…",
        found="抓到 {n} 个！都是本龙的猎物～",
        empty="呜…这页没有，肯定藏到别处去了！",
        carry="抱着 {total} 个视频飞回窝里…",
        done="全部叼回来啦，快摸摸我的角 ♡",
        fail="有几个没咬住…猲记仇了，下次一定！"),
    "jiao": dict(    # 跂 · 慵懒娇纵的踏雪小公主：用脚尖跟你说话，嘴上嫌麻烦、盯任务比谁紧
        idle="托着腮等你开口，脚已经替你点好了。",
        sniff="唔…本小姐用脚尖把这页翻一遍。",
        found="戳到 {n} 个，都是我的了。",
        empty="一个都没有嘛…雪都白落了。",
        carry="抱着 {total} 件往回挪，脚都走酸了。",
        done="点收完毕。夸我，本小姐站着等。",
        fail="有 {n} 个滑掉了…不许笑！主人也不许笑。"),
    "qing": dict(    # 蜃 · 台上浮夸爱演，台下最怕冷场：破功时从"本座"掉回"我"，句子变短
        idle="灯开着，人在台上。",
        sniff="试音中…这页我拿麦克风扫一遍。",
        found="点歌成功 {n} 首！",
        empty="这页没歌…气氛没起来。",
        carry="唱到 {total} 首，场子正热。",
        done="全部唱完，谢幕。……你还在吗？",
        fail="有 {n} 首跑调…我记下了，下次唱回来。"),
}
_MOOD_DEF = dict(idle="待命中…", sniff="正在搜寻…", found="找到 {n} 个",
                 empty="没找到视频", carry="下载中 {total} 个", done="全部完成",
                 fail="{n} 个失败")


def _mood(kind, tkey=None, n=None, total=None):
    d = dict(_MOOD_DEF)
    d.update(MOOD_LINES.get(tkey or _cur_theme[0]) or {})
    txt = d.get(kind) or ""
    if "{n}" in txt:
        txt = txt.format(n=n if n is not None else 0)
    if "{total}" in txt:
        txt = txt.format(total=total if total is not None else 0)
    return txt


_IMG = {}


def _art(role, scale=1.0, tkey=None):
    """取当前主题内嵌美图（base64 PNG → Tk PhotoImage），按 DPI 整数降采样保锐利。
    role: AVATAR / CHIP / SIDE / TILE。素材缺失或解码失败返回 None，调用方退回矢量兜底。"""
    if MA is None:
        return None
    tkey = tkey or _cur_theme[0]
    th = THEMES.get(tkey) or {}
    data = th.get(role)
    if not data:
        return None
    key = (tkey, role, round(scale, 2))
    if key in _IMG:
        return _IMG[key]
    try:
        im = tk.PhotoImage(data=data)
    except Exception:
        LOG.warning("素材解码失败: %s/%s", tkey, role)
        _IMG[key] = None
        return None
    k = int(getattr(MA, "K", 3))
    step = max(1, min(k, int(round(k / max(scale, 0.3)))))
    if step > 1:
        im = im.subsample(step, step)
    _IMG[key] = im
    return im


# ================= 把「大肥鱼」注入成 Tk 主界面的默认主题（P2 落地到软件本身）=================
def _feiyu_palette():
    return dict(mode="light", bg="#EEF3FB", card="#FFFFFF", pink="#3E6FD0", pink_d="#2E5AA8",
                pink_l="#AFC6EC", pink_xl="#E7EEF9", sky="#DCE8F7", sky_l="#EEF4FC",
                ink="#1F2A44", ink_l="#5C6880", line="#D7E0F0", navy="#16223F",
                plum="#3E6FD0", gold="#C9A24B")


def _feiyu_asset(role):
    """读 assets/feiyu_<role>.b64（裸 base64）；缺失回落 horn 同角色素材，保证 _art 不崩。"""
    bases = ([getattr(sys, "_MEIPASS", ""), BASE_DIR] if getattr(sys, "frozen", False) else [BASE_DIR])
    for base in bases:
        fp = os.path.join(base, "assets", "feiyu_%s.b64" % role.lower())
        try:
            if os.path.exists(fp):
                v = open(fp, encoding="utf-8").read().strip()
                return v.split(",", 1)[1] if v.startswith("data:") else v
        except Exception:
            pass
    return (THEMES.get("horn") or THEMES.get("maid") or {}).get(role)


def _feiyu_lines_map():
    def L(*xs):
        return list(xs)
    return {
        "greet": L("我回来啦～今天想捞点什么？", "本鱼上线，随时待命…虽然更想摸鱼。"),
        "idle": L("贴个网址给本鱼，我这就去翻个底朝天。", "海面平静，正等一条线索呢。"),
        "busy": L("推理中…跑着呢跑着呢，稍等一下下。", "本鱼脑子转得飞起，别催别催。"),
        "found": L("嘿，捞到一堆宝贝！清单已经替你排好。"),
        "empty": L("这片…空空的。换个网址，或者要不要先登录？"),
        "prog": L("正往回搬呢，别急，本鱼盯着。"),
        "done": L("搞定！吃白饭的大肥鱼还是靠得住的嘛。"),
        "fail": L("翻车了…才不是我菜，是这站太离谱！"),
        "pause": L("先按住啦，随时喊我继续。"),
        "resume": L("继续！这就接着干饭…啊不，干活。"),
    }


def _install_feiyu_theme():
    src = THEMES.get("horn") or THEMES.get("maid")
    if not src:
        return
    import copy
    fe = copy.deepcopy(src)
    fe.update(label="大肥鱼 · DeepSeek娘", char="大肥鱼", role="吃白饭的 AI 鲸鱼娘",
              note="傲娇 · 天然呆 · 爱摸鱼", motif="wave", palette=_feiyu_palette(),
              AVATAR=_feiyu_asset("AVATAR"), CHIP=_feiyu_asset("CHIP"),
              SIDE=_feiyu_asset("SIDE"), CHIBI=_feiyu_asset("CHIBI"), TILE=_feiyu_asset("TILE"),
              LINES=_feiyu_lines_map())
    THEMES["feiyu"] = fe
    # 其余按世界索引的字典也补 feiyu（克隆 horn 再覆盖），杜绝任何 KeyError / 缺失
    if "horn" in _WORLD:
        w = dict(_WORLD["horn"]); w["bg"] = ("#EEF3FB", "#E4ECF8", "#D7E0F0"); w["amb"] = "twinkle"
        w["title"] = dict(_WORLD["horn"].get("title") or {}); w["title"]["shadow"] = "#BFD4F2"
        _WORLD["feiyu"] = w
    if "horn" in _THEME_LAYOUT:
        _THEME_LAYOUT["feiyu"] = dict(_THEME_LAYOUT["horn"])
    if "horn" in THEME_VOICE:
        THEME_VOICE["feiyu"] = dict(res="✦ 本鱼捞回来的战利品（双击单下 · 表头排序 · 右键快捷）",
                                    log="✦ 大肥鱼的航海日志")
    PERSONA_CTA["feiyu"] = dict(FEIYU_CTA)
    MOOD_LINES["feiyu"] = dict(
        idle="本鱼在待命…才没在摸鱼，有事您说话。",
        sniff="推理中……正满页翻找视频，稍等。",
        found="捞到 {n} 个宝贝啦！要不要本鱼搬上船～",
        empty="这片…空空的。可能要登录，或在二级页面哦。",
        carry="抱着 {total} 件往硬盘跑，别催，在跑了。",
        done="全部搞定！吃白饭的大肥鱼还是靠得住的嘛 ♪",
        fail="有几个翻车了…记进小本本，回头再战！")


_install_feiyu_theme()
if "feiyu" in THEMES:
    DEFAULT_TKEY = "feiyu"
    THEME_ORDER = ["feiyu"] + [k for k in THEME_ORDER if k != "feiyu"]   # 大肥鱼置顶；六界仅作历史保留、界面不再切换
    C.clear(); C.update(_feiyu_palette())


_cur_theme = [DEFAULT_TKEY]          # 模块级当前主题（_art 取素材用）


# ---- 少女闺房素材：梦幻背景 + 贴纸（来自 maid_girly，base64 内嵌）----
_GIRLY = {}


def _girly(role, scale=1.0):
    if GI is None:
        return None
    data = getattr(GI, role, None)
    if not data:
        return None
    key = (role, round(scale, 2))
    if key in _GIRLY:
        return _GIRLY[key]
    try:
        im = tk.PhotoImage(data=data)
        if scale and scale != 1.0:
            k = max(1, int(round(1.0 / scale)))
            if k > 1:
                im = im.subsample(k, k)
    except Exception:
        LOG.warning("少女素材解码失败: %s", role)
        im = None
    _GIRLY[key] = im
    return im


def _fit_photo(base, w, h):
    """把 PhotoImage 覆盖缩放到至少 w×h（Tk 只支持整数 zoom/subsample，取最接近的不溢出因子）。"""
    try:
        bw, bh = base.width(), base.height()
        if bw <= 0 or bh <= 0 or w <= 2 or h <= 2:
            return None
        if bw < w or bh < h:
            z = max(1, int(round(max(w / bw, h / bh))))
            if z > 1:
                base = base.zoom(z, z)
                bw, bh = base.width(), base.height()
        k = 1
        while bw // (k + 1) >= w and bh // (k + 1) >= h:
            k += 1
        if k > 1:
            base = base.subsample(k, k)
        return base
    except Exception:
        return None


# ---- 五位助手的头栏画窗：真实素材（CC0/PD，见 theme_visions.CREDIT）----
_VISIONS = {}
_FADES = {}


def _vision(theme, target_h, max_w=0):
    """取主题画窗；素材固定 2080x460，按目标高度取整数 subsample 以适配 DPI。
    窗口偏窄时从右侧裁一块（宽按 24px 栅格向下取整，窗口拖动时不至于频繁重建）。
    返回 (PhotoImage, 是否裁切过)。"""
    if TV is None:
        return None
    data = (getattr(TV, "V", None) or {}).get(theme)
    if not data or target_h < 24:
        return None
    base_w, base_h = 2080, 460
    k = max(1, int(round(base_h / float(target_h))))
    while k < 8 and base_h // k > target_h * 1.16:
        k += 1
    crop = 0
    if max_w > 0:
        full_w = base_w // k
        if max_w < full_w:
            crop = (int(max_w) // 24) * 24
            if crop < 144:
                return None
    key = (theme, k, crop)
    if key in _VISIONS:
        return _VISIONS[key], bool(crop)
    im = None
    try:
        im = tk.PhotoImage(data=data)
        if k > 1:
            im = im.subsample(k, k)
        if crop and im.width() > crop:
            cut = tk.PhotoImage()
            cut.tk.call(cut, "copy", im, "-from",
                        im.width() - crop, 0, im.width(), im.height())
            im = cut
    except Exception:
        LOG.warning("世界画窗解码失败: %s", theme)
        im = None
    for _old in [kk for kk in _VISIONS if kk[0] == theme]:
        _VISIONS.pop(_old, None)
    _VISIONS[key] = im
    return im, bool(crop)


def _fade(theme, k):
    """画窗左缘的卡片色渐隐条（RGBA，Tk 画布按 alpha 合成），只为窄窗裁片软化接缝。"""
    if TV is None:
        return None
    data = getattr(TV, "FADE_DARK" if theme == "goth" else "FADE_LIGHT", None)
    if not data:
        return None
    key = (theme, k)
    if key in _FADES:
        return _FADES[key]
    im = None
    try:
        im = tk.PhotoImage(data=data)
        if k > 1:
            im = im.subsample(k, k)
    except Exception:
        LOG.warning("画窗渐隐条解码失败: %s", theme)
        im = None
    _FADES[key] = im
    return im


_BANDS = {}


def _band(theme, target_h):
    """主体面板标题带的材质贴片：80px 原生横条洗淡后镜像拼接，按 DPI 整数 subsample。"""
    if TV is None:
        return None
    data = (getattr(TV, "B", None) or {}).get(theme)
    if not data or target_h < 12:
        return None
    k = max(1, int(round(80.0 / float(target_h))))
    while k < 6 and 80 // k > target_h * 1.2:
        k += 1
    key = (theme, k)
    if key in _BANDS:
        return _BANDS[key]
    im = None
    try:
        im = tk.PhotoImage(data=data)
        if k > 1:
            im = im.subsample(k, k)
    except Exception:
        LOG.warning("材质带解码失败: %s", theme)
        im = None
    _BANDS[key] = im
    return im


def _tint(theme, even=0):
    """主体底色：材质平均色兑进主题色的很浅一层。
    even=0 给棋盘底/奇数行（原 card），even=1 给偶数行（原 pink_xl）。"""
    if TV is None:
        return None
    t = (getattr(TV, "TINT", None) or {}).get(theme)
    if not t:
        return None
    return t[1] if even else t[0]


def _deco(cv, cx, cy, s, fill, motif=None, tags="deco"):
    """按主题纹样画一个小装饰（心/星/菱形/花瓣）"""
    motif = motif or (THEMES.get(_cur_theme[0], {}) or {}).get("motif", "heart")
    if motif == "star":
        return _star(cv, cx, cy, s, fill, tags)
    if motif == "diamond":
        return _diamond(cv, cx, cy, s, fill, tags)
    if motif == "blossom":
        return _blossom(cv, cx, cy, s, fill, tags)
    return _heart(cv, cx, cy, s, fill, tags)


def _accent(cv, accent, x1, y1, x2, y2, col, col2, tags="accent"):
    """按主题画一层装饰母题（贴边描边/角标），给换角色时“焕然一新”的结构感。
    x1,y1,x2,y2 是卡片外框；soft 无额外装饰（沿用圆点心形），其余各画各的。"""
    P = lambda v: v  # 传入坐标已按 S 缩放，这里只做比例运算
    if accent == "hud":
        # 赛博/机娘：四角直角括号 + 顶部扫描细线
        L = max(6, int((x2 - x1) * 0.09))
        for cx, cy, dx, dy in ((x1, y1, 1, 1), (x2, y1, -1, 1),
                               (x1, y2, 1, -1), (x2, y2, -1, -1)):
            cv.create_line(cx, cy, cx + L * dx, cy, fill=col, width=2, tags=tags)
            cv.create_line(cx, cy, cx, cy + L * dy, fill=col, width=2, tags=tags)
        cv.create_line(x1 + L, y1 + 3, x2 - L, y1 + 3, fill=col2, width=1, tags=tags)
    elif accent == "seal":
        # 汉服/巫女：右上角一枚朱印（写一个字，别画成“+”像按钮）
        s = max(14, int((x2 - x1) * 0.07))
        sx, sy = x2 - s - 8, y1 + 8
        cv.create_rectangle(sx, sy, sx + s, sy + s, outline=col, width=2, tags=tags)
        _ch = {"hanfu": "雅", "miko": "祈"}.get(_cur_theme[0], "印")
        cv.create_text(sx + s / 2, sy + s / 2, text=_ch,
                       font=("KaiTi", max(8, int(s * 0.62)), "bold"),
                       fill=col, tags=tags)
    elif accent == "stars":
        # 魔法少女：右上散落小星星
        for (fx, fy, fs) in ((0.82, 0.14, 7), (0.90, 0.26, 4), (0.74, 0.22, 5)):
            _star(cv, x1 + (x2 - x1) * fx, y1 + (y2 - y1) * fy,
                  int((x2 - x1) * 0.02) + fs, col, tags)
    elif accent == "orn":
        # 哥特：顶部一排菱形饰带
        n = 3
        span = (x2 - x1) * 0.42
        x0 = x1 + (x2 - x1) * 0.50 - span / 2
        for i in range(n):
            cxp = x0 + span * (i + 0.5) / n
            cyp = y1 + 12
            _diamond(cv, cxp, cyp, 5, col, tags)
    # soft：女仆/海滩——留白，靠原有爱心点缀与柔色即可


def _mix(a, b, t):
    """两个 #rrggbb 之间线性插值"""
    pa = (int(a[1:3], 16), int(a[3:5], 16), int(a[5:7], 16))
    pb = (int(b[1:3], 16), int(b[3:5], 16), int(b[5:7], 16))
    return "#%02X%02X%02X" % tuple(int(pa[i] + (pb[i] - pa[i]) * t) for i in range(3))


# ==================== 渐变位图基建：PIL 离屏预渲染 → Tk 画布贴图 ====================
# 为什么需要：Tkinter 画布只有纯色多边形（_rr），**没有原生渐变/圆环/柔和描边**，所以
# HTML 视觉稿里的 linear-gradient 混色落成程序就变成单色平涂（用户验收时指出"网页版
# 色彩明明更好看，这种混色我很喜欢"）。这里用 PIL 把「渐变 + 圆角/圆环遮罩」预渲染成
# RGBA 位图，再 ImageTk.PhotoImage 贴到画布上，拿到真渐变 + 抗锯齿边缘。
# 兜底：缺 PIL / 任一步失败 → 返回 None，调用方一律回落纯色 _rr，界面绝不崩。
# 生命周期：PhotoImage 只在被 Python 对象引用期间有效，画布 delete("all") 后若对象被
# GC，正在显示的贴图会凭空消失。所以全局 LRU 缓存做加速，同时每块画布用 _gframe 对
# "本帧真正用到的图"持强引用（重绘前调 _grad_begin 换掉旧帧 → 旧图才被允许释放）。
_PIL_OK = None               # None=未探测 / False=不可用 / (Image, ImageDraw, ImageTk)=可用
_GRAD_CACHE = {}              # key → ImageTk.PhotoImage（全局，加速重复尺寸）
_GRAD_LRU = []               # key 访问顺序，超 _GRAD_MAX 逐出最久未用
_GRAD_MAX = 400
_GRAD_WORK = 384              # 生成渐变的最大对角线像素（大图先低分辨率算再放大，防卡顿）


def _pil_modules():
    """惰性导入 PIL 三件套；没装或导入失败返回 False（调用方回落纯色）。"""
    global _PIL_OK
    if _PIL_OK is None:
        try:
            from PIL import Image, ImageDraw, ImageTk
            _PIL_OK = (Image, ImageDraw, ImageTk)
        except Exception:
            _PIL_OK = False
            LOG.info("PIL 不可用，渐变元素回落纯色平涂")
    return _PIL_OK


def _grad_photo(w, h, r, c1, c2, angle=45, ring=0.0):
    """生成 w×h 的渐变位图并缓存，返回 ImageTk.PhotoImage；失败返回 None。

    angle：渐变**方向**，顺时针度数，屏幕坐标（y 向下）。
      0 = 左→右 · 45 = 左上→右下 · 90 = 上→下 · 180 = 右→左 · 270 = 下→上
    r：圆角半径（像素，0 = 直角）。ring：>0 时做圆环（头像描边），值 = 环宽/直径。
    """
    mods = _pil_modules()
    if not mods:
        return None
    Image, ImageDraw, ImageTk = mods
    w, h = max(1, int(round(w))), max(1, int(round(h)))
    r = max(0, int(round(r)))
    ring = round(float(ring), 3)
    angle = int(round(angle)) % 360
    key = (w, h, r, c1, c2, angle, ring)
    im = _GRAD_CACHE.get(key)
    if im is not None:
        try:
            _GRAD_LRU.remove(key)
        except ValueError:
            pass
        _GRAD_LRU.append(key)
        return im
    try:
        # 小图超采样拿锐利边缘；大图先压到 ≤_GRAD_WORK 对角线再双三次放大（渐变平滑，无损）
        ss = 3 if max(w, h) <= 90 else (2 if max(w, h) <= 220 else 1)
        W, H = w * ss, h * ss
        sc = min(1.0, _GRAD_WORK / max(1.0, math.hypot(W, H)))
        W, H = max(2, int(round(W * sc))), max(2, int(round(H * sc)))
        R = max(0, int(round(min(r, h / 2) * ss * sc)))
        n = int(math.hypot(W, H)) + 2
        # putdata 在 RGB 图上要 (r,g,b) 元组，_mix 返回的是 "#rrggbb" 字符串 → 这里自己拆
        pa = (int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16))
        pb = (int(c2[1:3], 16), int(c2[3:5], 16), int(c2[5:7], 16))
        denom = float(n - 1) or 1.0
        ramp = Image.new("RGB", (n, 1))
        ramp.putdata([tuple(int(pa[i] + (pb[i] - pa[i]) * j / denom)
                            for i in range(3)) for j in range(n)])
        g = ramp.resize((n, n), Image.NEAREST)
        g = g.rotate(-angle, resample=Image.BILINEAR, center=(n / 2.0, n / 2.0))
        ox, oy = (n - W) // 2, (n - H) // 2
        g = g.crop((ox, oy, ox + W, oy + H)).convert("RGBA")
        # 直角矩形不需要遮罩；PIL 的 Image 没有 .fill()，ellipse/rounded_rectangle 的 bbox 必须传元组
        need_mask = ring > 0 or R > 0
        m = Image.new("L", (W, H), 0 if need_mask else 255)
        if need_mask:
            dr = ImageDraw.Draw(m)
            if ring > 0:
                t = max(1, int(round(min(W, H) * ring / 2.0)))
                dr.ellipse((0, 0, W - 1, H - 1), fill=255)
                dr.ellipse((t, t, W - 1 - t, H - 1 - t), fill=0)
            else:
                dr.rounded_rectangle((0, 0, W - 1, H - 1), radius=R, fill=255)
        g.putalpha(m)
        if (W, H) != (w, h):
            g = g.resize((w, h), Image.LANCZOS)
        im = ImageTk.PhotoImage(g)
    except Exception:
        LOG.warning("渐变位图生成失败 %dx%d r=%d %s→%s", w, h, r, c1, c2,
                    exc_info=True)
        return None
    _GRAD_CACHE[key] = im
    _GRAD_LRU.append(key)
    while len(_GRAD_LRU) > _GRAD_MAX:
        _GRAD_CACHE.pop(_GRAD_LRU.pop(0), None)
    return im


def _grad_begin(cv):
    """画布整幅重绘前调用：切到新一帧的引用表（上一帧独有的贴图这才允许被释放）。"""
    try:
        cv._gframe = {}
    except Exception:
        pass
    return cv


def _grad_hold(cv, im, key=None):
    d = getattr(cv, "_gframe", None)
    if d is None:
        d = cv._gframe = {}
    d[key if key is not None else id(im)] = im


def _grad(cv, x, y, w, h, r, c1, c2, angle=45, tags="grad", anchor="nw"):
    """往画布贴一块渐变圆角矩形（左上角 x,y）。无 PIL → 回落 _rr 纯色。"""
    im = _grad_photo(w, h, r, c1, c2, angle)
    if im is None:
        return _rr(cv, x, y, x + w, y + h, r, fill=c1, outline="", tags=tags)
    _grad_hold(cv, im)
    return cv.create_image(x, y, image=im, anchor=anchor, tags=tags)


def _grad_ring(cv, cx, cy, d, c1, c2, frac=0.12, angle=45, tags="ring"):
    """以 (cx,cy) 为圆心画一个渐变圆环（头像描边）。无 PIL → 回落 create_oval 单色。"""
    im = _grad_photo(d, d, d // 2, c1, c2, angle, ring=frac)
    if im is None:
        return cv.create_oval(cx - d // 2, cy - d // 2, cx + d // 2, cy + d // 2,
                              outline=c1, width=max(1, int(round(d * frac))), tags=tags)
    _grad_hold(cv, im)
    return cv.create_image(cx, cy, image=im, anchor="center", tags=tags)


def _grad_pair(c1=None, c2=None):
    """取当前主题的两个签名色（缺省 C['pink'] / C['pink2']）；无 pink2 时自动配一个。"""
    c1 = c1 or C.get("pink") or "#C0679A"
    c2 = c2 or C.get("pink2") or _mix(c1, "#7B5BD6", 0.55)
    return c1, c2


def _acc2():
    """当前主题渐变第二色。老代码路径（深色板/直接塞原色板）可能没有 pink2 键，
    这里现场配一个兜底，调用方可以无脑写 C['pink'], _acc2() 而不会 KeyError。"""
    return C.get("pink2") or _mix(C.get("pink", "#C0679A"), "#7B5BD6", 0.55)


def _resolve_col(spec, default=None):
    """把颜色"意图"解析成当前色板下的真实色，供自绘控件在渲染时现取（换肤自动跟色）：
      "#rrggbb"        → 字面色
      "pink_xl"        → 色板键
      ("pink", 0.90)   → _mix(C['pink'], 白, 0.90)（淡底用）
      ("ink", 0.90, 黑) → 与黑混
    """
    default = default or C.get("card", "#FFFFFF")
    try:
        if isinstance(spec, (tuple, list)):
            key = spec[0]
            t = float(spec[1]) if len(spec) > 1 else 0.0
            toward = spec[2] if len(spec) > 2 else "#FFFFFF"
            base = C.get(key) or (key if isinstance(key, str) and key.startswith("#")
                                  else default)
            return _mix(base, toward, t)
        if isinstance(spec, str):
            if spec.startswith("#"):
                return spec
            return C.get(spec, default)
    except Exception:
        pass
    return default


_PFP_CACHE = {}              # (直径, 主题, 素材角色) → ImageTk.PhotoImage


def _pfp_img(d, tkey=None, role="CHIP"):
    """PIL 按目标直径精确重采样头像（tk.PhotoImage 只会整数 subsample：43px 素材塞
    36px 画布 → 四周被裁，圆头像缺边；这里 LANCZOS 缩放到刚好 d 像素）。
    缺 PIL / 素材缺失返回 None，调用方回落 _art()。"""
    mods = _pil_modules()
    if not mods:
        return None
    Image, _ImageDraw, ImageTk = mods
    tkey = tkey or _cur_theme[0]
    data = ((THEMES.get(tkey) or {}).get(role) or "")
    if not data:
        return None
    d = max(8, int(round(d)))
    key = (d, tkey, role)
    im = _PFP_CACHE.get(key)
    if im is not None:
        return im
    try:
        import base64
        import io as _io
        src = Image.open(_io.BytesIO(base64.b64decode(data))).convert("RGBA")
        # 素材里各角色的圆并非都严格内切（horn/jiao/qing 内容偏心，maid 边缘被画布裁掉），
        # 先按"不透明内容包围盒"取正方形 → 六个角色缩出来可见圆一样大，描边环才会贴脸。
        sw, sh = src.size
        bb = src.getchannel("A").getbbox()
        if bb:
            bx0, by0, bx1, by1 = bb
            side = int(min(max(bx1 - bx0, by1 - by0), sw, sh))
            if side >= 8:
                ccx = (bx0 + bx1) / 2.0
                ccy = (by0 + by1) / 2.0
                nx0 = int(max(0, min(ccx - side / 2.0, sw - side)))
                ny0 = int(max(0, min(ccy - side / 2.0, sh - side)))
                src = src.crop((nx0, ny0, nx0 + side, ny0 + side))
        ss = 3 if d <= 60 else 2
        W = d * ss
        g = src.resize((W, W), Image.LANCZOS)
        if W != d:
            g = g.resize((d, d), Image.LANCZOS)
        im = ImageTk.PhotoImage(g)
    except Exception:
        LOG.warning("头像重采样失败 %s/%s d=%d", tkey, role, d, exc_info=True)
        return None
    if len(_PFP_CACHE) > 200:
        _PFP_CACHE.clear()
    _PFP_CACHE[key] = im
    return im


# ---- 全局 DPI 系数：自绘控件（Card/胶囊）拿不到 App.P，统一从这里换算 ----
_UI_S = [1.0]
# DASH 大站（B站/YouTube 等 how="站点解析"）的单文件轨是否也交给 aria2c 多连接。
# 原来一律排除 → 音频轨只能单连接，撞慢 CDN 就是 30KB/s（同集视频轨能跑 4MB/s）。
# 由 App 按 cfg["dash_aria2"] 写入；设置页可一键关回旧行为。
DASH_ARIA2 = [True]


def _up(v):
    return int(round(v * _UI_S[0]))


# ==================== 卡片底图：圆角 + 柔和投影（PIL 预渲染） ====================
# 网页稿的观感主要来自 .card{border-radius:14px; box-shadow:…}；tk.Frame 只有方角 +
# 1px highlight 描边，画布也没有原生阴影。这里预渲染「透明底 + 高斯模糊投影 + 圆角
# 卡面 + 1px 描边」的位图当画布底，配合 Card（继承 Frame，布局语义完全不变）。
# 缺 PIL → Card 自动退回方角 + 描边（不崩、不空）。
_CARD_CACHE = {}
_CARD_LRU = []
_CARD_MAX = 80               # 卡片图比渐变图大得多（可达 1~2MB/张），单独限条数
_CARD_WORK = 480             # 生成阴影的最大对角线像素：阴影本来就是糊的，降分辨率无损


def _card_photo(w, h, r, fill, border, blur, pad):
    """w×h = 卡面尺寸；返回 (w+2pad)×(h+2pad) 的整张底图 PhotoImage，失败返回 None。"""
    mods = _pil_modules()
    if not mods:
        return None
    Image, ImageDraw, ImageTk = mods
    W, H = int(round(w + 2 * pad)), int(round(h + 2 * pad))
    r, pad, blur = int(round(r)), int(round(pad)), int(round(blur))
    if W < 8 or H < 8:
        return None
    key = (W, H, r, fill, border, blur, pad)
    im = _CARD_CACHE.get(key)
    if im is not None:
        try:
            _CARD_LRU.remove(key)
        except ValueError:
            pass
        _CARD_LRU.append(key)
        return im
    try:
        from PIL import ImageFilter
        # ⚠ 两层必须分开处理分辨率（曾经一起降分辨率 → 圆角边缘放大后变成斜切角/锯齿）：
        #   阴影层：本来就是糊的，低分辨率算再放大，无损且省时间；
        #   卡面层：有精确圆角边缘，必须按最终分辨率画，ss>1 时再缩一档自带抗锯齿。
        # ⚠ PIL 的 rounded_rectangle **本身不做抗锯齿**，1 倍画出来就是锯齿圆弧（实测过：
        #   大卡片 ss=1 → 角上是明显阶梯）。所以卡面一律至少 2 倍超采样再 LANCZOS 缩回。
        ss = 3 if max(W, H) <= 260 else 2
        Wf, Hf = W * ss, H * ss                      # 卡面分辨率
        Rf, Pf = r * ss, pad * ss
        Dyf = max(1, int(round(1.5 * ss)))
        # ---- 阴影层（低分辨率）----
        sc = min(1.0, _CARD_WORK / max(1.0, math.hypot(Wf, Hf)))
        Ws, Hs = max(8, int(round(Wf * sc))), max(8, int(round(Hf * sc)))
        Rs, Ps = max(1, int(round(Rf * sc))), max(1, int(round(Pf * sc)))
        Bs = max(1, int(round(blur * ss * sc)))
        near = max(1, int(round(1 * ss * sc)))
        far = max(2, int(round(6 * ss * sc)))
        # 双层 box-shadow：近距离薄薄一层 + 远距离散开的一层
        sh1 = Image.new("RGBA", (Ws, Hs), (0, 0, 0, 0))
        ImageDraw.Draw(sh1).rounded_rectangle((Ps, Ps + near, Ws - Ps, Hs - Ps),
                                              radius=Rs, fill=(20, 25, 35, 13))
        sh1 = sh1.filter(ImageFilter.GaussianBlur(max(1, Bs // 4)))
        sh2 = Image.new("RGBA", (Ws, Hs), (0, 0, 0, 0))
        ImageDraw.Draw(sh2).rounded_rectangle((Ps, Ps + far, Ws - Ps, Hs - Ps),
                                              radius=Rs, fill=(20, 25, 35, 15))
        sh2 = sh2.filter(ImageFilter.GaussianBlur(Bs))
        shadow = Image.alpha_composite(sh2, sh1)
        if (Ws, Hs) != (Wf, Hf):
            shadow = shadow.resize((Wf, Hf), Image.BILINEAR)
        # ---- 卡面层（全分辨率，圆角才是干净的）----
        face = Image.new("RGBA", (Wf, Hf), (0, 0, 0, 0))
        ImageDraw.Draw(face).rounded_rectangle(
            (Pf, Pf, Wf - Pf - 1, Hf - Pf - 1 - Dyf), radius=Rf,
            fill=fill, outline=border, width=max(1, ss))
        out = Image.alpha_composite(shadow, face)
        if ss != 1:
            out = out.resize((W, H), Image.LANCZOS)
        im = ImageTk.PhotoImage(out)
    except Exception:
        LOG.warning("卡片底图生成失败 %dx%d", W, H, exc_info=True)
        return None
    _CARD_CACHE[key] = im
    _CARD_LRU.append(key)
    while len(_CARD_LRU) > _CARD_MAX:
        _CARD_CACHE.pop(_CARD_LRU.pop(0), None)
    return im


# ---- 新 UI：中性 chrome 色板 + 每世界「签名点缀色」 ----------------------------
# 目标（用户拍板）：主窗改成中性底（近白/白卡/中性墨/细线），看板娘人格只靠
# 侧栏头像 + 桌宠 + 一个签名强调色延续，不再满屏 pastel 碎花。_chrome() 把任意
# 世界原色板压成中性板，只保留一个 accent（选中项/主按钮/头像描边/进度）。
_CH_BG, _CH_SIDE, _CH_CARD = "#EEF0F3", "#F6F7F9", "#FFFFFF"
# 日志面板 = 网页稿 .log 的深色终端底：不随角色换肤（换肤只换强调色，终端该恒定），
# 之前跟着主题刷成白底黑字，是用户指出的"这里还没改好"。
_LOG_BG, _LOG_FG, _LOG_LINE = "#1C2027", "#CDD3DC", "#262B34"
_LOG_TEAL, _LOG_YEL, _LOG_DIM, _LOG_RED = "#7FD1CB", "#E6C07A", "#8A9099", "#E07A7A"
# 前缀 → 颜色：把"哪类信息"用颜色区分开（网页稿 .g/.y/.d）
_LOG_TAG = (
    (("〔", "（", "[自检]", "[会话]", "[提示]"), _LOG_DIM),
    (("[失败]", "[错误]", "[异常]", "[超时]", "✗", "Error", "Traceback"), _LOG_RED),
    (("[下载]", "[完成]", "[合并]", "✓", "[加速]", "[RPC]", "[EJS]", "[Cookie]",
      "[代理]", "[MSE", "[嗅探]", "[加速]"), _LOG_TEAL),
    (("[警告]", "[重试]", "[限流]", "[冷却]", "[回退]"), _LOG_YEL),
)


def _log_color(msg):
    """按行首/关键字挑颜色。"""
    m = str(msg or "")
    for keys, col in _LOG_TAG:
        for k in keys:
            if k in m[:14] or m.startswith(k):
                return col
    return _LOG_FG
_CH_INK, _CH_INK_L, _CH_LINE = "#2B2F36", "#868D97", "#E4E7EB"
# 每个世界的签名色（取自渲帧实测主色，蜃青/跂冰青/猲紫/桃桃粉/墨墨玫/小夜朱）
_WORLD_ACCENT = {
    "maid": "#E1649A", "hanfu": "#C0566B", "miko": "#D9647C",
    "horn": "#A94F94", "jiao": "#5E93A8", "qing": "#3FA9A4",
    "cyber": "#3FA9A4", "beach": "#E79AB0", "goth": "#8E6BA8",
    "feiyu": "#3E6FD0",   # 大肥鱼 · 宝蓝（P2 单一主题）
}
# 渐变第二色：走「邻近 / 互补」路线，让每个角色的混色有辨识度又不打架
# （桃桃粉→暮紫、墨墨朱→暖金、小夜朱→藤紫、猲紫→青碧、跂冰青→靛蓝、
#  蜃青→蜃楼紫、cyber 青→深海蓝、beach 粉→落日橙、goth 紫→血梅）
_WORLD_ACCENT2 = {
    "maid": "#8A6BD6", "hanfu": "#D6A24A", "miko": "#8E5BA6",
    "horn": "#4EA8C6", "jiao": "#6E7FD0", "qing": "#5D72D8",
    "cyber": "#2E8FC7", "beach": "#F0A96A", "goth": "#C0566B",
    "feiyu": "#38C6D9",   # 大肥鱼 · 青宝石（渐变第二色）
}
# KPI 四卡底部色条的双色（存 C 的键名，运行时现取 → 跟着角色换肤）
#   引擎=健康绿→签名色 · 今日=签名色→第二签名色 · 速度=信息青→签名色 · 累计=绿→青
_KPI_GRAD = {
    "engine": ("ok", "pink"),
    "today": ("pink", "pink2"),
    "speed": ("cyan", "pink"),
    "total": ("ok", "cyan"),
}
# 通用状态色（网页稿一致：绿=健康/完成，红=失败，青=速度类指标）
_CH_OK, _CH_ERR, _CH_CYAN = "#3BA55D", "#D14343", "#39B8D8"


def _chrome(pal, key=None):
    """把某世界的 pastel 色板压成「中性底 + 签名渐变点缀色」的 UI 色板。
    保留原板全部键名（bg/card/pink*/sky*/ink*/line/navy/plum/gold/mode），
    另加 side（侧栏底）与 pink2*（渐变第二色）/ok/err/cyan，使既有控件代码
    无需改键名即可整体换肤。"""
    acc = _WORLD_ACCENT.get(key) or pal.get("pink_d") or "#C0679A"
    acc2 = _WORLD_ACCENT2.get(key) or _mix(acc, "#7B5BD6", 0.55)
    return {
        "mode": "light",
        "bg": _CH_BG, "side": _CH_SIDE, "card": _CH_CARD,
        "ink": _CH_INK, "ink_l": _CH_INK_L, "line": _CH_LINE,
        "pink": acc, "pink_d": _mix(acc, "#000000", 0.22),
        "pink_l": _mix(acc, "#FFFFFF", 0.60), "pink_xl": _mix(acc, "#FFFFFF", 0.90),
        "pink2": acc2, "pink2_d": _mix(acc2, "#000000", 0.22),
        "pink2_l": _mix(acc2, "#FFFFFF", 0.60),
        "pink2_xl": _mix(acc2, "#FFFFFF", 0.90),
        "sky": _mix(acc, "#FFFFFF", 0.86), "sky_l": _mix(acc, "#FFFFFF", 0.93),
        "plum": acc, "navy": _CH_INK, "gold": acc,
        "ok": _CH_OK, "err": _CH_ERR, "cyan": _CH_CYAN,
    }


def _heart(cv, cx, cy, s, fill, tags="deco"):
    """画布小爱心：两个圆 + 一个三角"""
    cv.create_oval(cx - s, cy - s * 0.9, cx - s * 0.05, cy + s * 0.2,
                   fill=fill, outline="", tags=tags)
    cv.create_oval(cx + s * 0.05, cy - s * 0.9, cx + s, cy + s * 0.2,
                   fill=fill, outline="", tags=tags)
    cv.create_polygon(cx - s * 0.95, cy - s * 0.15, cx + s * 0.95, cy - s * 0.15,
                      cx, cy + s * 1.05, fill=fill, outline="", tags=tags)


def _star(cv, cx, cy, s, fill, tags="deco"):
    """画布小星星（八角）"""
    import math
    pts = []
    for i in range(8):
        ang = math.pi / 4 * i - math.pi / 2
        rr = s if i % 2 == 0 else s * 0.42
        pts += [cx + rr * math.cos(ang), cy + rr * math.sin(ang)]
    cv.create_polygon(pts, fill=fill, outline="", tags=tags)


def _diamond(cv, cx, cy, s, fill, tags="deco"):
    """菱形 + 两侧短射线（赛博/机娘）"""
    cv.create_polygon(cx, cy - s, cx + s * 0.7, cy, cx, cy + s, cx - s * 0.7, cy,
                      fill=fill, outline="", tags=tags)
    cv.create_line(cx - s * 1.5, cy, cx - s * 0.95, cy, fill=fill, width=2, tags=tags)
    cv.create_line(cx + s * 0.95, cy, cx + s * 1.5, cy, fill=fill, width=2, tags=tags)


def _blossom(cv, cx, cy, s, fill, tags="deco"):
    """五瓣小花（汉服/巫女）"""
    import math
    for i in range(5):
        ang = i * 2 * math.pi / 5 - math.pi / 2
        px, py = cx + s * 0.55 * math.cos(ang), cy + s * 0.55 * math.sin(ang)
        cv.create_oval(px - s * 0.36, py - s * 0.36, px + s * 0.36, py + s * 0.36,
                       fill=fill, outline="", tags=tags)
    cv.create_oval(cx - s * 0.2, cy - s * 0.2, cx + s * 0.2, cy + s * 0.2,
                   fill=fill, outline="", tags=tags)


def _stroke_text(cv, x, y, text, font, fill, stroke="#FFFFFF", sw=0,
                 shadow=None, anchor="w", tags="txt"):
    """卡通贴纸字：白色粗描边 + 可选投影 + 上色，做出漫画/贴纸标题感。sw=描边半径(像素)。"""
    import math
    if sw and sw > 0:
        for i in range(12):
            a = 2 * math.pi * i / 12
            cv.create_text(x + sw * math.cos(a), y + sw * math.sin(a), text=text,
                           font=font, fill=stroke, anchor=anchor, tags=tags)
    if shadow:
        sx, sy, scol = shadow
        cv.create_text(x + sx, y + sy, text=text, font=font, fill=scol,
                       anchor=anchor, tags=tags)
    cv.create_text(x, y, text=text, font=font, fill=fill, anchor=anchor, tags=tags)


def _bunting(cv, x1, x2, y, n, s, colors, tags="deco"):
    """一串小三角彩旗（少女花环）：从 y 高度垂下 n 面，宽 s。"""
    span = x2 - x1
    if span <= 0 or n <= 0:
        return
    rope = "#EBAFCB"
    for i in range(n):
        cx = x1 + span * (i + 0.5) / n
        col = colors[i % len(colors)]
        cv.create_line(cx, y, cx, y + s * 0.22, fill=rope, tags=tags)
        cv.create_polygon(cx - s * 0.5, y + s * 0.22, cx + s * 0.5, y + s * 0.22,
                          cx, y + s, fill=col, outline="#FFFFFF", tags=tags)
    cv.create_line(x1, y, x2, y, fill=rope, width=2, tags=tags)


def _sparkle(cv, cx, cy, s, fill, tags="deco"):
    """四角闪光星（bling）。"""
    k = s * 0.30
    return cv.create_polygon(cx, cy - s, cx + k, cy - k, cx + s, cy, cx + k, cy + k,
                             cx, cy + s, cx - k, cy + k, cx - s, cy, cx - k, cy - k,
                             fill=fill, outline="", tags=tags)


def _rainbow_arc(cv, cx, cy, r, colors, tags="deco"):
    """半道柔和彩虹（拱形彩条），圆心在 (cx,cy)，半径 r。"""
    from math import cos, sin, pi
    steps = 26
    for ci, col in enumerate(colors):
        rr = r - ci * (r * 0.14)
        if rr <= 2:
            continue
        pts = []
        for i in range(steps + 1):
            a = pi * i / steps
            pts += [cx - rr * cos(a), cy - rr * sin(a)]
        cv.create_line(pts, fill=col, width=max(2, int(r * 0.11)),
                       capstyle="round", smooth=True, tags=tags)


def _petal(cv, cx, cy, s, fill, rot=0.0, tags="deco"):
    """一片旋转过的樱花/桃花瓣（水滴形）。rot=弧度。"""
    from math import cos, sin
    pts = [(0, -1.0), (0.62, -0.35), (0.5, 0.55), (0.0, 1.0), (-0.5, 0.55), (-0.62, -0.35)]
    cr, sr = cos(rot), sin(rot)
    flat = []
    for px, py in pts:
        X = cx + s * (px * cr - py * sr)
        Y = cy + s * (px * sr + py * cr)
        flat += [X, Y]
    return cv.create_polygon(flat, fill=fill, outline="", smooth=True, tags=tags)


def _paw(cv, cx, cy, s, fill, tags="deco"):
    """猫爪印：一个大掌垫 + 四个脚趾。"""
    cv.create_oval(cx - s * 0.7, cy - s * 0.1, cx + s * 0.7, cy + s * 0.9,
                   fill=fill, outline="", tags=tags)
    for tx, ty in ((-0.72, -0.55), (-0.24, -0.82), (0.24, -0.82), (0.72, -0.55)):
        cv.create_oval(cx + s * tx - s * 0.24, cy + s * ty - s * 0.24,
                       cx + s * tx + s * 0.24, cy + s * ty + s * 0.24,
                       fill=fill, outline="", tags=tags)


def _bat(cv, cx, cy, s, fill, tags="deco"):
    """小蝙蝠剪影：两片尖角翅膀 + 椭圆身子 + 两只耳朵。"""
    cv.create_polygon(cx - s * 1.6, cy - s * 0.2, cx - s * 0.9, cy - s * 0.7,
                      cx - s * 0.5, cy - s * 0.1, cx - s * 0.2, cy - s * 0.5,
                      cx, cy, cx - s * 0.4, cy + s * 0.3, cx - s * 1.0, cy + s * 0.1,
                      cx - s * 1.5, cy + s * 0.4, fill=fill, outline="", tags=tags)
    cv.create_polygon(cx + s * 1.6, cy - s * 0.2, cx + s * 0.9, cy - s * 0.7,
                      cx + s * 0.5, cy - s * 0.1, cx + s * 0.2, cy - s * 0.5,
                      cx, cy, cx + s * 0.4, cy + s * 0.3, cx + s * 1.0, cy + s * 0.1,
                      cx + s * 1.5, cy + s * 0.4, fill=fill, outline="", tags=tags)
    cv.create_oval(cx - s * 0.28, cy - s * 0.35, cx + s * 0.28, cy + s * 0.45,
                   fill=fill, outline="", tags=tags)
    cv.create_polygon(cx - s * 0.22, cy - s * 0.3, cx - s * 0.05, cy - s * 0.62,
                      cx + s * 0.02, cy - s * 0.3, fill=fill, outline="", tags=tags)
    cv.create_polygon(cx + s * 0.22, cy - s * 0.3, cx + s * 0.05, cy - s * 0.62,
                      cx - s * 0.02, cy - s * 0.3, fill=fill, outline="", tags=tags)


def _magic_circle(cv, cx, cy, r, col, col2, tags="deco"):
    """星环魔法阵：两层同心圆 + 八道辐条 + 顶点小星。"""
    from math import cos, sin, pi
    cv.create_oval(cx - r, cy - r, cx + r, cy + r, outline=col, width=2, tags=tags)
    r2 = r * 0.66
    cv.create_oval(cx - r2, cy - r2, cx + r2, cy + r2, outline=col2, tags=tags)
    for i in range(8):
        a = pi / 4 * i
        cv.create_line(cx + r2 * cos(a), cy + r2 * sin(a),
                       cx + r * cos(a), cy + r * sin(a), fill=col2, tags=tags)
        _star(cv, cx + r * cos(a), cy + r * sin(a), r * 0.12, col, tags)


def _crescent(cv, cx, cy, r, fill, tags="deco"):
    """一弯月牙（右侧开口的弦月多边形）。"""
    from math import cos, sin, pi
    pts = []
    for i in range(13):
        a = -pi / 2 + pi * i / 12
        pts += [cx + r * cos(a), cy + r * sin(a)]
    for i in range(12, -1, -1):
        a = -pi / 2 + pi * i / 12
        rr = r * (0.55 if i % 2 == 0 else 0.5)
        pts += [cx + r * 0.42 + rr * cos(a) * 0.7, cy + rr * sin(a) * 0.92]
    return cv.create_polygon(pts, fill=fill, outline="", smooth=True, tags=tags)


def _seal(cv, x, y, s, col, ch, tags="deco"):
    """一枚朱红印章：圆角方 + 白字。"""
    _rr(cv, x, y, x + s, y + s, s * 0.18, fill=col, outline="", tags=tags)
    cv.create_text(x + s / 2, y + s / 2, text=ch,
                   font=("KaiTi", int(s * 0.5), "bold"), fill="#FFFFFF", tags=tags)


def _hud_brackets(cv, x1, y1, x2, y2, col, tags="deco", ln=18):
    """机娘 HUD：四角直角括号。"""
    for cx, cy, dx, dy in ((x1, y1, 1, 1), (x2, y1, -1, 1),
                           (x1, y2, 1, -1), (x2, y2, -1, -1)):
        cv.create_line(cx, cy, cx + ln * dx, cy, fill=col, width=2, tags=tags)
        cv.create_line(cx, cy, cx, cy + ln * dy, fill=col, width=2, tags=tags)


def _ink_mountains(cv, w, base_y, col, tags="deco"):
    """淡墨远山：两道起伏山脊。"""
    for k, (amp, yy) in enumerate(((1.0, base_y), (0.6, base_y - 10))):
        pts = [0, yy]
        n = 6
        for i in range(n):
            pts += [w * (i + 0.5) / n, yy - (14 + 10 * (i % 2)) * amp]
            pts += [w * (i + 1) / n, yy]
        cv.create_line(pts, fill=col, width=2, smooth=True, tags=tags)


def _plum_branch(cv, x, y, ln, bark, flower, tags="deco"):
    """一枝梅：主枝 + 侧枝 + 几朵五瓣花。"""
    from math import cos, sin, pi
    cv.create_line(x, y, x + ln, y - ln * 0.35, fill=bark, width=3,
                   capstyle="round", smooth=True, tags=tags)
    cv.create_line(x + ln * 0.5, y - ln * 0.18, x + ln * 0.72, y - ln * 0.6,
                   fill=bark, width=2, capstyle="round", tags=tags)
    for fx, fy in ((ln * 0.28, -ln * 0.12), (ln * 0.6, -ln * 0.5),
                   (ln * 0.78, -ln * 0.28), (ln, -ln * 0.35)):
        _blossom(cv, x + fx, y + fy, 6, flower, tags)


def _thorn(cv, x1, x2, y, col, tags="deco"):
    """荆棘：一条带尖刺的折线藤蔓。"""
    n = max(4, int((x2 - x1) / 26))
    pts = []
    for i in range(n + 1):
        xx = x1 + (x2 - x1) * i / n
        yy = y + (6 if i % 2 else -4)
        pts += [xx, yy]
    cv.create_line(pts, fill=col, width=2, smooth=True, tags=tags)
    for i in range(1, n):
        xx = x1 + (x2 - x1) * i / n
        yy = y + (6 if i % 2 else -4)
        cv.create_line(xx, yy, xx + 3, yy - 9, fill=col, width=1, tags=tags)


def _torii(cv, cx, base_y, s, col, col2, tags="deco"):
    """鸟居：立柱 + 笠木（两端上翘）+ 贯 + 额束。s = 总宽。"""
    hw = s * 0.5
    top = base_y - s * 0.78
    wcol = max(2, int(s * 0.055))
    for dx in (-hw * 0.62, hw * 0.62):
        cv.create_line(cx + dx, base_y, cx + dx * 0.9, top + s * 0.1,
                       fill=col, width=wcol, capstyle="round", tags=tags)
    cv.create_line(cx - hw, top + s * 0.06, cx + hw, top + s * 0.06,
                   fill=col, width=max(3, int(s * 0.07)), capstyle="round", tags=tags)
    cv.create_line(cx - hw, top + s * 0.06, cx - hw * 0.86, top - s * 0.02,
                   fill=col, width=max(2, int(s * 0.05)), capstyle="round", tags=tags)
    cv.create_line(cx + hw, top + s * 0.06, cx + hw * 0.86, top - s * 0.02,
                   fill=col, width=max(2, int(s * 0.05)), capstyle="round", tags=tags)
    cv.create_line(cx - hw * 0.88, top + s * 0.21, cx + hw * 0.88, top + s * 0.21,
                   fill=col2, width=max(2, int(s * 0.04)), tags=tags)
    cv.create_rectangle(cx - s * 0.035, top + s * 0.1, cx + s * 0.035, top + s * 0.21,
                        fill=col2, outline="", tags=tags)


def _rose(cv, cx, cy, s, dark, light, tags="deco"):
    """黑玫瑰：同心弧花瓣 + 外圈花苞 + 茎。"""
    for i, rr in enumerate((0.92, 0.68, 0.46, 0.24)):
        cv.create_arc(cx - s * rr, cy - s * rr, cx + s * rr, cy + s * rr,
                      start=20 + i * 70, extent=250, style="arc",
                      outline=(light if i % 2 else dark),
                      width=max(1, int(s * 0.09)), tags=tags)
    for a in range(0, 360, 60):
        rad = math.radians(a)
        px = cx + math.cos(rad) * s * 0.98
        py = cy + math.sin(rad) * s * 0.98
        cv.create_oval(px - s * 0.2, py - s * 0.2, px + s * 0.2, py + s * 0.2,
                       outline=dark, width=max(1, int(s * 0.06)), tags=tags)
    cv.create_line(cx, cy + s, cx + s * 0.32, cy + s * 2.2, fill=dark,
                   width=max(1, int(s * 0.12)), capstyle="round", tags=tags)


def _web(cv, cx, cy, r, col, start=180, tags="deco"):
    """蛛网：从 (cx,cy) 角点张开的四分之一网。start=180 左上，270 右上。"""
    for i in range(1, 5):
        rr = r * i / 4.0
        cv.create_arc(cx - rr, cy - rr, cx + rr, cy + rr, start=start, extent=90,
                      style="arc", outline=col, width=1, tags=tags)
    for a in range(start, start + 91, 18):
        rad = math.radians(a)
        cv.create_line(cx, cy, cx + math.cos(rad) * r, cy + math.sin(rad) * r,
                       fill=col, width=1, tags=tags)


def _lantern(cv, cx, cy, s, fill, line, tags="deco"):
    """纸灯：提线 + 椭圆灯身 + 两道箍 + 流苏。"""
    cv.create_line(cx, cy - s * 2.6, cx, cy - s * 1.25, fill=line,
                   width=max(1, int(s * 0.08)), tags=tags)
    cv.create_oval(cx - s * 0.62, cy - s * 1.25, cx + s * 0.62, cy + s * 0.9,
                   fill=fill, outline=line, width=max(1, int(s * 0.07)), tags=tags)
    cv.create_line(cx - s * 0.56, cy - s * 0.48, cx + s * 0.56, cy - s * 0.48,
                   fill=line, width=1, tags=tags)
    cv.create_line(cx - s * 0.56, cy + s * 0.18, cx + s * 0.56, cy + s * 0.18,
                   fill=line, width=1, tags=tags)
    cv.create_line(cx, cy + s * 0.9, cx, cy + s * 1.55, fill=line, width=1, tags=tags)


def _bell(cv, cx, cy, s, col, gold, tags="deco"):
    """神乐铃：三颗铃 + 彩带。"""
    cv.create_line(cx, cy - s * 1.3, cx, cy - s * 0.45, fill=gold,
                   width=max(1, int(s * 0.12)), tags=tags)
    for dx, dy in ((-s * 0.55, -s * 0.1), (0, s * 0.28), (s * 0.55, -s * 0.1)):
        cv.create_oval(cx + dx - s * 0.3, cy + dy - s * 0.3,
                       cx + dx + s * 0.3, cy + dy + s * 0.3,
                       fill=gold, outline=col, width=1, tags=tags)
        cv.create_line(cx + dx, cy + dy + s * 0.3, cx + dx, cy + dy + s * 1.05,
                       fill=col, width=1, tags=tags)


def _omikuji(cv, cx, cy, s, col, ink, tags="deco"):
    """御神签：纸签条 + 大吉。"""
    cv.create_polygon(cx - s * 0.36, cy - s * 0.9, cx + s * 0.36, cy - s * 0.9,
                      cx + s * 0.36, cy + s * 0.9, cx, cy + s * 1.3,
                      cx - s * 0.36, cy + s * 0.9,
                      fill="#FFFFFF", outline=col, width=1, tags=tags)
    fs = int(max(6, s * 0.5))
    cv.create_text(cx, cy - s * 0.42, text="大", font=("KaiTi", fs, "bold"),
                   fill=ink, tags=tags)
    cv.create_text(cx, cy + s * 0.18, text="吉", font=("KaiTi", fs, "bold"),
                   fill=ink, tags=tags)
    cv.create_line(cx, cy - s * 0.95, cx, cy - s * 1.45, fill=col, width=1, tags=tags)


def _code_rain(cv, x, y, n, col, step, font, tags="deco"):
    """代码雨：一列 0/1（确定性排列，避免每次重绘乱跳）。"""
    for i in range(n):
        ch = "1" if (i * 7 + 3) % 3 else "0"
        cv.create_text(x, y + i * step, text=ch, font=font, fill=col, tags=tags)


def _grid(cv, w, h, step, col, tags="deco"):
    """科技网格。"""
    for x in range(0, int(w), step):
        cv.create_line(x, 0, x, h, fill=col, tags=tags)
    for y in range(0, int(h), step):
        cv.create_line(0, y, w, y, fill=col, tags=tags)


def _guqin(cv, x, y, ln, col, tags="deco"):
    """古琴：琴身 + 七弦。"""
    cv.create_line(x, y, x + ln, y - ln * 0.04, fill=col,
                   width=max(5, int(ln * 0.06)), capstyle="round", tags=tags)
    for i in range(1, 7):
        yy = y - ln * 0.012 + (i - 3.5) * (ln * 0.008)
        cv.create_line(x + ln * 0.05, yy, x + ln * 0.95, yy, fill=col, width=1, tags=tags)


def _rr(cv, x1, y1, x2, y2, r, **kw):
    """画布圆角矩形（smooth polygon）"""
    r = min(r, (y2 - y1) / 2, (x2 - x1) / 2)
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return cv.create_polygon(pts, smooth=True, **kw)


def _chamfer_rect(cv, x1, y1, x2, y2, c, **kw):
    """切角矩形（机娘 HUD 的硬边轮廓），c=切角大小"""
    c = max(1, min(c, (x2 - x1) / 3, (y2 - y1) / 3))
    pts = [x1 + c, y1, x2 - c, y1, x2, y1 + c, x2, y2 - c,
           x2 - c, y2, x1 + c, y2, x1, y2 - c, x1, y1 + c]
    return cv.create_polygon(pts, **kw)


def _scallop_row(cv, x1, x2, y, r, fill, outline, tags="deco", up=False):
    """横向扇贝边：一排半圆（蕾丝/糖果棚）。默认朝下挂。"""
    r = max(2, r)
    x = x1
    while x < x2 - r:
        bbox = (x, y - r, x + 2 * r, y + r)
        start = 0 if up else 180
        try:
            cv.create_arc(bbox, start=start, extent=180, style="pieslice",
                          fill=fill, outline=outline, width=1, tags=tags)
        except Exception:
            cv.create_oval(x, y - r, x + 2 * r, y + r, fill=fill,
                           outline=outline, tags=tags)
        x += 2 * r


def _lace_side(cv, x, y1, y2, r, col, tags="deco", right=False):
    """竖向蕾丝边：沿侧边一串半圆。"""
    r = max(2, r)
    y = y1
    while y < y2 - r:
        bbox = (x - r, y, x + r, y + 2 * r)
        start = 90 if right else 270
        try:
            cv.create_arc(bbox, start=start, extent=180, style="arc",
                          outline=col, width=2, tags=tags)
        except Exception:
            pass
        y += 2 * r


def _bow(cv, cx, cy, s, fill, dark, tags="deco"):
    """蝴蝶结：两片缎带 + 结 + 两条垂尾（桃桃/糖果屋用）"""
    try:
        cv.create_polygon(cx, cy, cx - s, cy - s * 0.75, cx - s * 1.18,
                          cy - s * 0.05, cx - s * 0.78, cy + s * 0.5,
                          cx, cy + s * 0.16, smooth=True,
                          fill=fill, outline=dark, width=1, tags=tags)
        cv.create_polygon(cx, cy, cx + s, cy - s * 0.75, cx + s * 1.18,
                          cy - s * 0.05, cx + s * 0.78, cy + s * 0.5,
                          cx, cy + s * 0.16, smooth=True,
                          fill=fill, outline=dark, width=1, tags=tags)
        cv.create_polygon(cx - s * 0.12, cy + s * 0.05,
                          cx - s * 0.9, cy + s * 1.35,
                          cx - s * 0.42, cy + s * 1.2,
                          cx - s * 0.08, cy + s * 0.3,
                          fill=fill, outline=dark, tags=tags)
        cv.create_polygon(cx + s * 0.12, cy + s * 0.05,
                          cx + s * 0.9, cy + s * 1.35,
                          cx + s * 0.42, cy + s * 1.2,
                          cx + s * 0.08, cy + s * 0.3,
                          fill=fill, outline=dark, tags=tags)
        cv.create_oval(cx - s * 0.22, cy - s * 0.2,
                       cx + s * 0.22, cy + s * 0.28,
                       fill=dark, outline="", tags=tags)
    except Exception:
        pass


def _hscroll_stave(cv, x1, x2, y, th, wood, hi, dark, tags="deco"):
    """横卷轴木杆：杆身 + 两端圆轴头（汉服立轴/手卷）"""
    try:
        r = max(2, th // 2)
        _rr(cv, x1, y, x2, y + th, r, fill=wood, outline=dark, tags=tags)
        cv.create_line(x1 + r, y + max(1, th * 0.28), x2 - r,
                       y + max(1, th * 0.28), fill=hi, width=1, tags=tags)
        for xx in (x1, x2):
            rr = max(3, int(th * 0.82))
            cv.create_oval(xx - rr, y + th / 2 - rr, xx + rr, y + th / 2 + rr,
                           fill=dark, outline=hi, width=1, tags=tags)
    except Exception:
        pass


def _vscroll_rod(cv, x, y1, y2, tw, wood, hi, dark, tags="deco"):
    """竖卷轴木杆：杆身 + 上下圆轴头（汉服手卷两端）"""
    try:
        r = max(2, tw // 2)
        _rr(cv, x - tw / 2, y1, x + tw / 2, y2, r,
            fill=wood, outline=dark, tags=tags)
        cv.create_line(x - tw * 0.18, y1 + r, x - tw * 0.18, y2 - r,
                       fill=hi, width=1, tags=tags)
        for yy in (y1, y2):
            rr = max(3, int(tw * 0.9))
            cv.create_oval(x - rr, yy - rr, x + rr, yy + rr,
                           fill=dark, outline=hi, width=1, tags=tags)
    except Exception:
        pass


def _shimenawa(cv, x1, x2, y, th, fill, dark, hi, tags="deco", sag=None):
    """注连绳：微垂的粗绳 + 断续拧纹（神社）"""
    try:
        import math as _m
        n = 14
        amp = (x2 - x1) * 0.035 if sag is None else sag
        pts = []
        for i in range(n + 1):
            fx = x1 + (x2 - x1) * i / n
            fy = y + amp * _m.sin(_m.pi * i / n)
            pts += [fx, fy]
        cv.create_line(pts, fill=fill, width=max(3, th), smooth=True,
                       capstyle="round", tags=tags)
        cv.create_line(pts, fill=hi, width=1, smooth=True, tags=tags)
        for i in range(3, n, 2):
            fx = x1 + (x2 - x1) * i / n
            fy = y + amp * _m.sin(_m.pi * i / n)
            cv.create_line(fx - th * 0.45, fy - th * 0.55,
                           fx + th * 0.45, fy + th * 0.55,
                           fill=dark, width=1, tags=tags)
    except Exception:
        pass


def _shide(cv, x, y, s, fill, outline, tags="deco"):
    """纸垂：白色之字形纸条（挂在注连绳上）"""
    pts = [x - s * 0.45, y, x + s * 0.45, y,
           x + s * 0.45, y + s * 0.7, x - s * 0.05, y + s * 0.7,
           x - s * 0.05, y + s * 1.3, x + s * 0.7, y + s * 1.3,
           x + s * 0.7, y + s * 1.9, x - s * 0.45, y + s * 1.9,
           x - s * 0.45, y + s * 1.3, x + s * 0.05, y + s * 1.3,
           x + s * 0.05, y + s * 0.7, x - s * 0.45, y + s * 0.7]
    try:
        cv.create_polygon(pts, fill=fill, outline=outline, width=1, tags=tags)
    except Exception:
        pass


def _iron_corner(cv, x, y, dx, dy, size, col, tags="deco"):
    """铁艺护角：L 形 + 一枚小卷花（哥特）"""
    try:
        cv.create_line(x, y + dy * size, x, y, x + dx * size, y,
                       fill=col, width=3, tags=tags)
        cv.create_line(x + dx * size * 0.28, y + dy * size * 0.28,
                       x + dx * size * 0.28, y + dy * size * 0.85,
                       fill=col, width=1, tags=tags)
        cv.create_line(x + dx * size * 0.28, y + dy * size * 0.28,
                       x + dx * size * 0.85, y + dy * size * 0.28,
                       fill=col, width=1, tags=tags)
        cr = max(2, size * 0.18)
        cx0, cy0 = x + dx * size * 0.55, y + dy * size * 0.55
        cv.create_arc(cx0 - cr, cy0 - cr, cx0 + cr, cy0 + cr,
                      start=0, extent=280, style="arc",
                      outline=col, width=1, tags=tags)
    except Exception:
        pass


def _valance(cv, x1, x2, y, depth, fill, edge, tags="deco"):
    """尖拱帷幔：顶部条 + 一排下垂三角 + 金穗（哥特）"""
    try:
        cv.create_rectangle(x1, y, x2, y + depth * 0.42,
                            fill=fill, outline="", tags=tags)
        step = max(14, int(depth * 1.9))
        xx = x1
        while xx < x2:
            cv.create_polygon(xx, y + depth * 0.34,
                              xx + step / 2, y + depth,
                              min(x2, xx + step), y + depth * 0.34,
                              fill=fill, outline=edge, width=1, tags=tags)
            xx += step
        cv.create_line(x1, y + depth * 0.34, x2, y + depth * 0.34,
                       fill=edge, width=1, tags=tags)
        for xx in range(int(x1 + step / 2), int(x2), step):
            cv.create_line(xx, y + depth * 0.94, xx, y + depth * 1.18,
                           fill=edge, width=1, tags=tags)
    except Exception:
        pass


def _notched_banner(cv, x1, y1, x2, y2, notch, fill, outline, tags="deco"):
    """两端 V 形缺口的缎带横幅（魔法少女名牌）"""
    pts = [x1 + notch, y1, x2 - notch, y1, x2, (y1 + y2) / 2,
           x2 - notch, y2, x1 + notch, y2, x1, (y1 + y2) / 2]
    try:
        return cv.create_polygon(pts, fill=fill, outline=outline,
                                 width=2, tags=tags)
    except Exception:
        return None


def _pointed_arch(cv, x1, x2, y_top, y_bottom, col, width=1, tags="deco"):
    """尖拱线：两侧上收在顶点相交（哥特窗）"""
    cx = (x1 + x2) / 2
    try:
        cv.create_line(x1, y_bottom, x1, y_bottom - (y_bottom - y_top) * 0.55,
                       cx, y_top, x2, y_bottom - (y_bottom - y_top) * 0.55,
                       x2, y_bottom, smooth=True, splinesteps=18,
                       fill=col, width=width, tags=tags)
    except Exception:
        pass


class Card(tk.Frame):
    """圆角 + 柔和投影的卡片容器（对齐网页稿 .card）。

    为什么继承 Frame 而不是 Canvas：Tk 里 Canvas 的 reqheight **不会**随 create_window
    塞进去的子控件长大，换成 Canvas 容器就得手工同步尺寸，四个视图的布局全都要重调、
    回归面极大。这里 Frame 自己按子控件算尺寸，底下一张画布只负责画圆角+投影，
    子控件一律放进 card.body —— 调用点只改"父级是谁"，pack/grid/pady 语义不变。
    缺 PIL 时底图返回 None → 自动退回方角 + 1px 描边，界面不空不崩。"""

    def __init__(self, parent, radius=12, pad=14, blur=13, bg=None, body_bg=None,
                 body_padx=0, body_pady=0, **kw):
        self._bg_col = bg if bg is not None else parent.cget("bg")
        tk.Frame.__init__(self, parent, bg=self._bg_col,
                          highlightthickness=0, bd=0, **kw)
        self._radius, self._pad, self._blur = radius, pad, blur
        self._cv = tk.Canvas(self, bg=self._bg_col, highlightthickness=0, bd=0)
        self._cv.place(x=0, y=0, relwidth=1.0, relheight=1.0)
        # body 带上原来 Frame 的 padx/pady：调用点写成 body = card.body 后，
        # 底下几十行子控件代码一个字都不用改，留白节奏和原来一致。
        self.body = tk.Frame(self, bg=body_bg if body_bg is not None else C["card"],
                             padx=body_padx, pady=body_pady)
        p = _up(pad)
        # ⚠ body 是方角白矩形：若只内缩 p（正好铺满卡面），它会把底图的四个圆角重新
        # 涂成白的 —— 现象是"投影有、角却是直角"（本轮真踩过，用户一眼看穿）。
        # 再多缩 0.32×圆角半径，让方角藏进白色卡面以内，圆角才真的露出来。
        e = p + max(1, int(round(_up(radius) * 0.32)))
        self.body.pack(fill="both", expand=True, padx=e, pady=e)
        self._pend = False
        self.body.bind("<Configure>", lambda e: self._queue())
        self._cv.bind("<Configure>", lambda e: self._queue())

    def _queue(self):
        if not self._pend:
            self._pend = True
            try:
                self.after(24, self._redraw)
            except Exception:
                pass

    def _colors(self):
        fill = str(self.body.cget("bg"))
        return fill, _mix(C["line"], fill, 0.35)

    def _redraw(self):
        self._pend = False
        try:
            cv = self._cv
            w, h = cv.winfo_width(), cv.winfo_height()
            if w < 12 or h < 12:
                return
            p = _up(self._pad)
            r = _up(self._radius)
            fill, border = self._colors()
            sig = (w, h, fill, border)
            if sig == getattr(self, "_sig", None):
                return                  # 一次布局会连发多个 Configure，尺寸没变就别重画
            self._sig = sig
            cv.delete("cardbg")
            _grad_begin(cv)
            im = _card_photo(w - 2 * p, h - 2 * p, r, fill, border,
                             _up(self._blur), p)
            if im is None:
                _rr(cv, p, p, w - p, h - p, r, fill=fill, outline=border,
                    tags="cardbg")
            else:
                _grad_hold(cv, im)
                cv.create_image(0, 0, image=im, anchor="nw", tags="cardbg")
            cv.tag_lower("cardbg")
        except Exception:
            pass

    def retheme(self):
        try:
            self._cv.configure(bg=self._bg_col)
        except Exception:
            pass
        self._redraw()


class Pill(tk.Canvas):
    """圆角胶囊标签（网页稿 .pill / .chip / .sppill）：淡底 + 彩色文字，可选双色渐变底。"""

    def __init__(self, parent, text="", fg=None, tint=None, grad=None, font=None,
                 padx=11, pady=4, border=None, bg=None, **kw):
        self._text = text
        self._fg = fg
        self._tint = tint
        self._grad = grad            # (c1, c2) 双色渐变底（白字）
        self._border = border
        self._padx, self._pady = padx, pady
        self._font = font or F_SM_B
        tk.Canvas.__init__(self, parent, highlightthickness=0, bd=0,
                           bg=bg if bg is not None else parent.cget("bg"), **kw)
        self._render()

    def _render(self):
        self.delete("all")
        _grad_begin(self)
        if not self._text:
            self.configure(width=1, height=1)   # 空胶囊收成 1px，不留空壳
            return
        fg = _resolve_col(self._fg, C["pink_d"]) if self._fg else C["pink_d"]
        if self._grad:
            c1, c2 = self._grad
            fg = "#FFFFFF"
        elif self._tint:
            c1 = c2 = _resolve_col(self._tint)
        else:
            c1 = c2 = C["card"]
        tid = self.create_text(9999, 9999, text=self._text, font=self._font)
        try:
            x1, y1, x2, y2 = self.bbox(tid)
        except Exception:
            x1, y1, x2, y2 = 0, 0, 40, 14
        self.delete(tid)
        w = max(1, x2 - x1 + 2 * _up(self._padx))
        h = max(_up(16), (y2 - y1) + 2 * _up(self._pady) + 2)
        self.configure(width=w, height=h)
        r = h / 2.0 - 1
        border = (_resolve_col(self._border, c1) if self._border is not None
                  else (c1 if self._grad else _mix(C["line"], c1, 0.5)))
        im = _grad_photo(w, h, r, c1, c2, 0)
        if im is None:
            _rr(self, 1, 1, w - 1, h - 1, r, fill=c1, outline=border)
        else:
            _grad_hold(self, im)
            self.create_image(0, 0, image=im, anchor="nw")
            if border:
                _rr(self, 1, 1, w - 1, h - 1, r, fill="", outline=border)
        self.create_text(w / 2, h / 2, text=self._text, font=self._font, fill=fg)

    def set(self, text=None, fg=None, tint=None, grad=None):
        if text is not None:
            self._text = text
        if fg is not None:
            self._fg = fg
        if tint is not None:
            self._tint = tint
        if grad is not None:
            self._grad = grad
        self._render()

    def configure(self, cnf=None, **kw):
        """兼容 tk.Label 的 .config(text=…, fg=…)：老刷新代码不用改就能驱动胶囊。"""
        if isinstance(cnf, dict):
            kw.update(cnf)
            cnf = None
        got = [kw.pop(k, None) for k in ("text", "fg", "tint", "grad")]
        if any(v is not None for v in got):
            self.set(text=got[0], fg=got[1], tint=got[2], grad=got[3])
        if kw:
            tk.Canvas.configure(self, cnf, **kw)
        elif cnf is not None:
            tk.Canvas.configure(self, cnf)

    config = configure

    def retheme(self):
        self._render()


class ResultsList(tk.Canvas):
    """自绘下载结果列表（替代 ttk.Treeview）。

    为什么自己画：Treeview 的单元格只接受文字 + 整行前景/背景色，画不出网页稿里那种
    圆角进度条和彩色状态胶囊。这里对外**保留 Treeview 的那一小撮 API**（insert /
    delete / set / get_children / heading / column / tag_configure / selection /
    selection_set / selection_add / identify_row / see / configure / yview / bind），
    调用点一行都不用改。

    内部要点：① 只画可见行（几百条也不卡）；② 重绘用 after_idle 合并，避免
    refresh_table 的"清空+逐行插入"退化成 O(N²)；③ 进度条/胶囊底图走 _grad_photo
    缓存，同宽度不重复生成。"""

    HEAD_H = 30
    ROW_H = 30

    def __init__(self, parent, columns=(), selectmode="extended", bg=None, **kw):
        # 调用方沿用原 Treeview 的 spec 顺序 (标题, 宽, 对齐, 列名)；
        # 内部统一成 [列名, 标题, 宽, 对齐]，别再混两种顺序（混过一次：状态列读回空）。
        self._cols = [[c[3], c[0], c[1], c[2]] for c in columns]
        self._cmds = {}                                # key → 表头点击回调
        self._order = []                               # iid 显示顺序
        self._vals = {}                                # iid → [各列文本]
        self._tags = {}                                # iid → (tag,)
        self._tagcfg = {}                              # tag → {"background":…}
        self._sel = []                                 # 选中 iid（点击顺序）
        self._anchor_iid = None                        # Shift 范围选锚点
        self._top = 0                                  # 滚动偏移（像素）
        self._yscroll_cmd = None
        self._pend = False
        self._last_err = None
        self._stretch = {}                             # 列名 → 是否自动撑宽（别塞进 _cols，
        #                                              否则 4 元组解包会炸）
        self._row_h = _up(self.ROW_H)
        self._head_h = _up(self.HEAD_H)
        tk.Canvas.__init__(self, parent, highlightthickness=0, bd=0,
                           bg=bg if bg is not None else C["card"], **kw)
        self.bind("<Configure>", lambda e: self._need())
        self.bind("<Button-1>", self._click)
        self.bind("<Button-3>", self._pre_right)
        self.bind("<MouseWheel>", self._wheel)
        self.bind("<Leave>", lambda e: self._hover(None))

    # ---------------- Treeview 兼容 API ----------------
    def insert(self, parent="", index=None, iid=None, values=(), tags=(), **_kw):
        key = str(iid) if iid is not None else str(len(self._order))
        vals = ["" if v is None else str(v) for v in values]
        while len(vals) < len(self._cols):
            vals.append("")
        if key in self._vals:
            self._order.remove(key)
        self._vals[key] = vals
        self._tags[key] = tuple(tags or ())
        if index in (None, "end", tk.END):
            self._order.append(key)
        else:
            try:
                self._order.insert(int(index) if isinstance(index, int) else 0, key)
            except Exception:
                self._order.append(key)
        self._need()
        return key

    def delete(self, *iids):
        for k in iids:
            k = str(k)
            if k in self._vals:
                del self._vals[k]
                self._tags.pop(k, None)
                if k in self._order:
                    self._order.remove(k)
                if k in self._sel:
                    self._sel.remove(k)
        self._need()

    def get_children(self, parent=""):
        return tuple(self._order)

    def set(self, iid, column=None, value=None):
        iid = str(iid)
        row = self._vals.get(iid)
        if row is None:
            return ""
        if column is None:
            return list(row)
        idx = self._col_idx(column)
        if value is None:
            return row[idx] if 0 <= idx < len(row) else ""
        if 0 <= idx < len(row):
            row[idx] = "" if value is None else str(value)
        self._need()
        return row[idx]

    def heading(self, column=None, text=None, command=None, **_kw):
        if column is None:
            return {}
        idx = self._col_idx(column)
        if 0 <= idx < len(self._cols):
            if text is not None:
                self._cols[idx][1] = str(text)
        if command is not None:
            self._cmds[column] = command
        self._need()

    def column(self, column=None, width=None, anchor=None, stretch=None, **_kw):
        idx = self._col_idx(column)
        if idx < 0:
            return {}
        if width is not None:
            self._cols[idx][2] = int(width)
        if anchor is not None:
            self._cols[idx][3] = anchor
        if stretch is not None:
            self._stretch[self._cols[idx][0]] = bool(stretch)
        self._need()
        return {"id": column, "width": self._cols[idx][2],
                "anchor": self._cols[idx][3]}

    def tag_configure(self, tagname, **kw):
        d = self._tagcfg.setdefault(tagname, {})
        d.update({k: v for k, v in kw.items() if v is not None})
        self._need()

    def selection(self):
        return tuple(self._sel)

    def selection_set(self, *s):
        ids = []
        for it in s:
            ids.extend(it if isinstance(it, (list, tuple)) else [str(it)])
        self._sel = [i for i in ids if i in self._vals]
        self._anchor_iid = self._sel[-1] if self._sel else None
        self._need()

    def selection_add(self, *s):
        for it in s:
            ids = it if isinstance(it, (list, tuple)) else [it]
            for i in ids:
                i = str(i)
                if i in self._vals and i not in self._sel:
                    self._sel.append(i)
        self._need()

    def identify_row(self, y=None, **_kw):
        i = self._row_index_at(y)
        return self._order[i] if 0 <= i < len(self._order) else ""

    def see(self, iid):
        iid = str(iid)
        if iid not in self._order:
            return
        i = self._order.index(iid)
        top, h = self._view_box()
        y0 = self._head_h + i * self._row_h
        if y0 - self._top < self._head_h:
            self._top = max(0, y0 - self._head_h)
        elif y0 + self._row_h - self._top > h:
            self._top = y0 + self._row_h - h
        self._clamp_top()
        self._need()

    def yview(self, *args):
        top, h = self._view_box()
        total = self._content_h()
        if not args:
            if total <= 0 or h <= 0:
                return (0.0, 1.0)
            return (max(0.0, self._top / total),
                    min(1.0, (self._top + h) / total))
        kind = str(args[0])
        if kind == "moveto":
            try:
                self._top = float(args[1]) * total
            except Exception:
                pass
        elif kind == "scroll":
            try:
                n = float(args[1])
            except Exception:
                n = 0
            step = h if str(args[2] if len(args) > 2 else "units") == "pages" \
                else self._row_h
            self._top += n * step
        self._clamp_top()
        self._need()
        return None

    def configure(self, cnf=None, **kw):
        if "yscrollcommand" in kw:
            self._yscroll_cmd = kw.pop("yscrollcommand")
        if "rowheight" in kw:
            self._row_h = int(kw.pop("rowheight"))
        if kw or cnf:
            tk.Canvas.configure(self, cnf or {}, **kw)
        self._need()

    config = configure

    # ---------------- 内部：布局与绘制 ----------------
    def _col_idx(self, column):
        for i, c in enumerate(self._cols):
            if c[0] == column:
                return i
        try:
            return int(column)
        except Exception:
            return -1

    def _view_box(self):
        return self.winfo_width(), max(self._row_h, self.winfo_height())

    def _content_h(self):
        return len(self._order) * self._row_h

    def _clamp_top(self):
        top, h = self._view_box()
        self._top = max(0, min(self._top,
                               max(0, self._content_h() - (h - self._head_h))))

    def _col_x(self):
        """算出每列的 x 起点与宽度：stretch 列吃掉「剩余」宽度（先扣掉它自己的基础宽，
        否则整表会比画布宽，最后一列被挤出可视区）。"""
        w, _h = self._view_box()
        avail = max(60, w - _up(8))
        pad_x = _up(4)

        def base(c):
            return _up(c[2])

        def is_st(c):
            return self._stretch.get(c[0], c[0] == "title")

        fixed = sum(base(c) for c in self._cols if not is_st(c))
        sbase = sum(base(c) for c in self._cols if is_st(c))
        ns = max(1, sum(1 for c in self._cols if is_st(c)))
        slack = max(0, avail - pad_x * 2 - fixed - sbase) // ns
        xs, x = {}, pad_x
        for c in self._cols:
            cw = base(c) + (slack if is_st(c) else 0)
            xs[c[0]] = (x, cw)
            x += cw
        return xs

    def _row_index_at(self, y):
        if y is None or y <= self._head_h:
            return -1
        i = int((y - self._head_h + self._top) // self._row_h)
        return i if 0 <= i < len(self._order) else -1

    def _need(self):
        if self._pend:
            return
        self._pend = True
        try:
            self.after_idle(self._redraw)
        except Exception:
            self._pend = False

    def _redraw(self):
        self._pend = False
        try:
            # ⚠ 必须显式调基类：本类为兼容 Treeview 定义了 delete(*iids)，它覆盖了
            #   tk.Canvas.delete —— self.delete("all") 既清不掉画布又会再排一次 _need，
            #   结果是无限自我调度（表现为启动卡死）。
            tk.Canvas.delete(self, "all")
            _grad_begin(self)
            w, h = self._view_box()
            if w < 20 or h < 20:
                return
            xs = self._col_x()
            self._draw_head(xs, w)
            i0 = max(0, int(self._top // self._row_h))
            i1 = min(len(self._order),
                     int((self._top + h - self._head_h) // self._row_h) + 2)
            for i in range(i0, i1):
                self._draw_row(i, self._order[i], xs)
            self._report_scroll()
        except Exception as e:
            # 别静默吞：记下来给验收断言用（上次 ValueError 就是被 debug 级日志藏掉的）
            msg = "%s: %s" % (type(e).__name__, e)
            if getattr(self, "_last_err", None) != msg:
                self._last_err = msg
                LOG.warning("结果表重绘失败：%s", msg, exc_info=True)

    def _report_scroll(self):
        cb = self._yscroll_cmd
        if not cb:
            return
        try:
            f, l = self.yview()
            cb(f, l)
        except Exception:
            pass

    def _draw_head(self, xs, w):
        y0, y1 = 0, self._head_h
        self.create_rectangle(0, y0, w, y1, fill=C["pink_xl"], outline="", tags="h")
        self.create_line(0, y1 - 1, w, y1 - 1, fill=C["line"], tags="h")
        for key, title, _cw, anchor in self._cols:
            x, cw = xs[key]
            tx = x + (0 if anchor == "w" else cw // 2)
            tg = "hcol_%s" % key
            self.create_text(tx, (y0 + y1) // 2, text=title, anchor=anchor,
                             fill=C["plum"], font=F_SM_B, tags=tg)
            # 每列必须独立 tag：同一个 tag 反复 tag_bind 只有最后一次生效
            self.tag_bind(tg, "<Button-1>", lambda e, k=key: self._head_click(k))

    def _head_click(self, key):
        fn = self._cmds.get(key)
        if fn:
            try:
                fn()
            except Exception:
                LOG.debug("表头排序失败", exc_info=True)

    def _draw_row(self, i, iid, xs):
        y = self._head_h + i * self._row_h - self._top
        if y + self._row_h <= self._head_h or y > self.winfo_height():
            return
        vals = self._vals.get(iid) or []
        sel = iid in self._sel
        bg = _mix(C["pink"], "#FFFFFF", 0.78) if sel else self._zebra(iid, i)
        sidx = self._col_idx("status")
        status = vals[sidx] if 0 <= sidx < len(vals) else ""
        self.create_rectangle(0, y, self.winfo_width(), y + self._row_h,
                              fill=bg, outline="", tags="r")
        for j, (key, _t, _cw, anchor) in enumerate(self._cols):
            text = vals[j] if j < len(vals) else ""
            x, cw = xs[key]
            self._draw_cell(key, text, x, y, cw, bg, sel, anchor, status)
        self.create_line(0, y + self._row_h - 1, self.winfo_width(),
                         y + self._row_h - 1, fill=C["line"], tags="r")

    def _zebra(self, iid, i):
        for t in self._tags.get(iid) or ():
            b = (self._tagcfg.get(t) or {}).get("background")
            if b:
                return b
        return C["card"] if i % 2 == 0 else C["pink_xl"]

    def _draw_cell(self, key, text, x, y, cw, bg, sel, anchor, status=""):
        cy = y + self._row_h // 2
        ink = C["ink"]
        if key == "prog":
            self._draw_prog(status, x, cy, cw)
        elif key == "status":
            self._draw_status(text, x, cy, cw, sel)
        elif key == "title":
            self.create_text(x + _up(4), cy, text=_clip(text, cw - _up(10)),
                             anchor="w", fill=ink, font=F_UI, tags="r")
        else:
            self.create_text(x + (0 if anchor == "w" else cw // 2), cy, text=text,
                             anchor=anchor, fill=C["ink_l"] if key != "ext" else ink,
                             font=F_SM, tags="r")

    def _draw_prog(self, status, x, cy, cw):
        """圆角进度条 + 百分比（Treeview 单元格做不到的那件事）；比例从状态文本推导。"""
        frac = _pct_of(status)
        bar_w = max(_up(46), cw - _up(52))
        th = max(4, _up(6))
        r = th / 2.0
        x0 = x + _up(4)
        _grad(self, x0, cy - th // 2, bar_w, th, r, _mix(C["line"], "#FFFFFF", 0.35),
              _mix(C["line"], "#FFFFFF", 0.35), 0, tags="r")
        if frac:
            fw = max(3, int(round(bar_w * frac)))
            c1, c2 = _grad_pair()
            _grad(self, x0, cy - th // 2, fw, th, r, c1, c2, 0, tags="r")
        self.create_text(x0 + bar_w + _up(6), cy,
                         text=("" if frac is None else "%d%%" % int(frac * 100)),
                         anchor="w", fill=C["ink_l"], font=F_SM, tags="r")

    def _draw_status(self, text, x, cy, cw, sel):
        """状态胶囊：圆角淡底 + 同色系文字（✓完成绿 / 搬运中签名色 / ✗红 / 排队灰）。
        宽度锁在本列内，否则长状态会压到右边的「来源」列上。"""
        if not text:
            return
        fg, tint = _status_colors(text)
        s = str(text)
        th = max(_up(17), self._row_h - _up(8))
        maxw = cw - _up(6)
        tw = self._measure(s) + _up(16)
        if tw > maxw:                       # 放不下先裁字，再按裁后的字重新量宽
            s = _clip(s, maxw - _up(16))
            tw = min(maxw, self._measure(s) + _up(16))
        x0 = x + max(_up(2), (cw - tw) // 2)
        _grad(self, x0, cy - th // 2, tw, th, th / 2.0 - 1, tint, tint, 0, tags="r")
        self.create_text(x0 + tw // 2, cy, text=s, fill=fg, font=F_SM_B, tags="r")

    def _measure(self, s):
        return _measure_text(s, F_SM_B)

    # ---------------- 内部：交互 ----------------
    def _click(self, e):
        if e.y <= self._head_h:
            return
        iid = self.identify_row(e.y)
        if not iid:
            self._sel = []
            self._anchor_iid = None
            self._need()
            return
        mods = e.state
        ctrl = bool(mods & 0x0004)
        shift = bool(mods & 0x0001)
        if shift and self._anchor_iid in self._order and iid in self._order:
            a, b = self._order.index(self._anchor_iid), self._order.index(iid)
            lo, hi = min(a, b), max(a, b)
            self._sel = list(self._order[lo:hi + 1])
        elif ctrl:
            if iid in self._sel:
                self._sel.remove(iid)
            else:
                self._sel.append(iid)
                self._anchor_iid = iid
        else:
            self._sel = [iid]
            self._anchor_iid = iid
        self._need()

    def _pre_right(self, e):
        """右键：落在未选中的行上时先把选中集换成该行（和 Treeview 行为一致）。"""
        iid = self.identify_row(e.y)
        if iid and iid not in self._sel:
            self._sel = [iid]
            self._anchor_iid = iid
            self._need()

    def _wheel(self, e):
        self._top -= (e.delta // 120) * self._row_h * 3
        self._clamp_top()
        self._need()
        return "break"

    def _hover(self, _iid):
        self._need()


def _pct_of(text):
    """从状态文本里抠出进度比例（"43% · 1.2 MB/s" → 0.43；没有百分比 → None）。"""
    m = re.search(r"(\d{1,3})\s*%", str(text or ""))
    if not m:
        if str(text or "").startswith("✓"):
            return 1.0
        return None
    return max(0.0, min(1.0, int(m.group(1)) / 100.0))


def _status_colors(text):
    s = str(text or "")
    if s.startswith("✓"):
        return C.get("ok", "#3BA55D"), _mix(C.get("ok", "#3BA55D"), "#FFFFFF", 0.90)
    if s.startswith("✗"):
        return C.get("err", "#D14343"), _mix(C.get("err", "#D14343"), "#FFFFFF", 0.90)
    if "%" in s or "搬运" in s or "分片" in s or "下载" in s:
        return C["pink_d"], _mix(C["pink"], "#FFFFFF", 0.90)
    return C["ink_l"], _mix(C["ink_l"], "#FFFFFF", 0.90)


_FONTS = {}                  # 字体元组 → tkfont.Font（每帧 new 一个 Font 太浪费）


def _measure_text(s, font=None):
    font = font or F_UI
    f = _FONTS.get(font)
    if f is None:
        try:
            f = _FONTS[font] = tkfont.Font(font=font)
        except Exception:
            return len(str(s)) * 8
    try:
        return f.measure(str(s))
    except Exception:
        return len(str(s)) * 8


def _clip(s, w):
    """标题按像素宽度截断（Canvas 没有 ellipsis 裁剪）。"""
    s = str(s or "")
    if w <= 10 or len(s) < 4:
        return s
    if _measure_text(s) <= w:
        return s
    lo, hi = 0, len(s)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _measure_text(s[:mid] + "…") <= w:
            lo = mid
        else:
            hi = mid - 1
    return s[:lo] + "…" if lo > 0 else "…"


class RoundButton(tk.Canvas):
    """圆角可爱按钮：primary=粉底白字, ghost=白底粉边, 支持悬浮/按下/禁用态。"""

    def __init__(self, parent, text="", command=None, kind="primary",
                 padx=18, pady=8, font=None, state="normal", width=None, bg=None):
        self._text, self._command, self._kind = text, command, kind
        self._padx, self._pady = padx, pady
        self._font = font or F_UI_B
        self._state = state
        self._hover = False
        self._pressed = False
        tk.Canvas.__init__(self, parent, highlightthickness=0, bd=0,
                           bg=bg if bg is not None else parent.cget("bg"))
        self._render(fixed_w=width)
        self.bind("<Enter>", lambda e: self._sense(True))
        self.bind("<Leave>", lambda e: self._sense(False, True))
        self.bind("<Button-1>", self._down)
        self.bind("<ButtonRelease-1>", self._up)

    def cget(self, key):
        k = key.lstrip("-")
        if k == "text":
            return self._text
        if k == "state":
            return self._state
        return tk.Canvas.cget(self, key)

    def _colors(self):
        dark = C.get("mode") == "dark"
        if self._state == "disabled":
            dis = _mix(C["ink_l"], C["card"], 0.55)
            return dis, _mix(C["ink_l"], C["card"], 0.15), C["line"]
        if self._kind == "primary":
            base = (C["pink_d"] if self._pressed
                    else (C["pink"] if not self._hover else _mix(C["pink"], "#FFFFFF" if not dark else "#000000", 0.18)))
            return base, "#FFFFFF" if not dark else "#17121F", base
        base = C["pink_xl"] if self._pressed else (C["pink_l"] if self._hover else C["card"])
        return base, C["ink"], C["line"]

    def _render(self, fixed_w=None):
        self.delete("all")
        _grad_begin(self)
        base, fg, border = self._colors()
        tid = self.create_text(9999, 9999, text=self._text, font=self._font)
        x1, y1, x2, y2 = self.bbox(tid)
        self.delete(tid)
        w = fixed_w or (x2 - x1 + 2 * self._padx)
        h = (y2 - y1) + 2 * self._pady + 2
        self.configure(width=w, height=h)
        if self._kind == "primary" and self._state != "disabled":
            # 主按钮 = 签名双色渐变（网页稿 .btn.pri 是单色，这里"多混一点"）
            c1, c2 = _grad_pair()
            if self._pressed:
                c1 = _mix(c1, "#000000", 0.22)
                c2 = _mix(c2, "#000000", 0.22)
            elif self._hover:
                c1 = _mix(c1, "#FFFFFF", 0.18)
                c2 = _mix(c2, "#FFFFFF", 0.18)
            _grad(self, 1, 1, w - 2, h - 2, h / 2 - 1, c1, c2, 0)
        else:
            _rr(self, 1, 1, w - 1, h - 1, h / 2 - 1, fill=base, outline=border)
        if self._kind != "primary" and self._state != "disabled" and not self._hover and not self._pressed:
            pass
        self.create_text(w / 2, h / 2, text=self._text, font=self._font, fill=fg)

    def _sense(self, hover, release=False):
        if self._state == "disabled":
            return
        self._hover = hover
        if release:
            self._pressed = False
        self._render()

    def _down(self, _e):
        if self._state == "disabled":
            return
        self._pressed = True
        self._render()

    def _up(self, _e):
        if self._state == "disabled":
            return
        self._pressed = False
        self._render()
        if self._command:
            try:
                self._command()
            except Exception:
                LOG.error(traceback.format_exc())

    def retheme(self):
        self._render()

    def set_state(self, st):
        st = "normal" if st in ("normal", "!disabled") else st
        if st != self._state:
            self._state = st
            self._hover = self._pressed = False
            self._render()

    def configure(self, cnf=None, **kw):
        changed = False
        if "state" in kw:
            self._state = kw.pop("state")
            changed = True
        if "text" in kw:
            self._text = kw.pop("text")
            changed = True
        if "width" in kw:
            kw["width"] = int(kw["width"])
        if changed:
            self._hover = self._pressed = False
            self._render()
        if kw:
            tk.Canvas.configure(self, cnf or {}, **kw)
        elif cnf:
            tk.Canvas.configure(self, cnf)

    config = configure


class ThemedBar(tk.Canvas):
    """按角色人设自绘的下载进度条，兼容 ttk.Progressbar 的
    config(mode=/value=)、start()、stop()。软萌=圆角胶囊+爱心游标；
    机娘(HUD)=细条+刻度+亮前缘；哥特=菱形游标；魔法=星星游标；
    汉服/巫女=方印游标。换主题时随色板与 _layout 自动重绘。"""

    def __init__(self, parent, S, **kw):
        self._S = S
        self._mode = "determinate"
        self._frac = 0.0
        self._ind = 0.0
        self._job = None
        self._ph = 0.0          # 花哨动画相位（微光扫过 / 花瓣飘落 / 星星闪烁）
        self._anim = None       # 确定态进度中的自绘动画 after() 句柄
        tk.Canvas.__init__(self, parent, height=self._p(20),
                           highlightthickness=0, bd=0,
                           bg=kw.pop("bg", C["bg"]))
        self.bind("<Configure>", lambda e: self._redraw())

    def _p(self, v):
        return int(round(v * self._S))

    def config(self, cnf=None, **kw):
        if isinstance(cnf, dict):
            kw.update(cnf)
        mode = kw.pop("mode", None)
        value = kw.pop("value", None)
        if mode is not None:
            self._mode = mode
        if value is not None:
            try:
                self._frac = max(0.0, min(1.0, float(value) / 100.0))
            except Exception:
                pass
        if kw:
            tk.Canvas.configure(self, **kw)
        self._redraw()

    configure = config

    def start(self, interval=50):
        self._mode = "indeterminate"
        if self._job:
            return

        def tick():
            self._ind = (self._ind + 0.018) % (1.0 + 0.30)
            self._redraw()
            self._job = self.after(interval, tick)

        tick()

    def stop(self):
        if self._job:
            try:
                self.after_cancel(self._job)
            except Exception:
                pass
            self._job = None
        self._mode = "determinate"
        self._redraw()

    def _rr(self, x1, y1, x2, y2, r, **kw):
        _rr(self, x1, y1, x2, y2, r, **kw)

    def _fill_grad(self, px0, ty1, px1, ty2, r, c1, c2, tags="b"):
        """进度条「已填充段」= 双色渐变贴图（横向 0°）。宽度只跟实际进度有关，
        相同 (宽,高,色) 走缓存；无 PIL 时回落纯色 _rr，绝不空条。"""
        w, h = px1 - px0, ty2 - ty1
        if w < 2 or h < 2:
            return None
        im = _grad_photo(w, h, r, c1, c2, 0)
        if im is None:
            return self._rr(px0, ty1, px1, ty2, r, fill=c1, outline="", tags=tags)
        _grad_hold(self, im)
        return self.create_image(px0, ty1, image=im, anchor="nw", tags=tags)

    # ---- 动画：确定态进度 0~100% 之间持续轻动（微光/花瓣/星闪），停或满即止 ----
    def _animating(self):
        return self._mode == "determinate" and 0.004 < self._frac < 0.996

    def _ensure_anim(self):
        if self._animating() and self._anim is None:
            self._anim = self.after(70, self._anim_tick)

    def _anim_tick(self):
        self._anim = None
        self._ph = (self._ph + 0.05) % 1.0
        self._redraw()

    def _stop_anim(self):
        if self._anim:
            try:
                self.after_cancel(self._anim)
            except Exception:
                pass
            self._anim = None

    def _clipfill(self, x):
        """只在填充区间内画装饰，避免溢出到空轨道"""
        return self._fpx0 <= x <= self._fpx1

    def _shimmer(self, ty1, ty2):
        """斜向微光扫过填充区（用浅粉细线近似半透明高光带）"""
        span = self._fpx1 - self._fpx0
        if span < self._p(10):
            return
        band = self._p(26)
        head = self._fpx0 + int(self._ph * (span + band)) - band // 2
        hi = C["pink_l"] if C.get("mode") != "dark" else C["pink_xl"]
        for i in range(band // max(1, self._p(3))):
            x = head + i * self._p(3)
            if self._fpx0 <= x <= self._fpx1:
                self.create_line(x, ty1 + 1, x + self._p(6), ty2 - 1,
                                 fill=hi, width=2, tags="b")

    def _redraw(self):
        self.delete("all")
        _grad_begin(self)
        w, h = self.winfo_width(), self.winfo_height()
        if w < 12 or h < 6:
            self._stop_anim()
            return
        lay = _layout()
        style = lay.get("bar", "lace")
        thin = style == "hud"
        pad = 1
        bh = h - 2
        th = 8 if thin else 16
        r = min(bh // 2, self._p(max(3, lay["radius"])))
        ty1 = 1 + (bh - th) // 2
        ty2 = ty1 + th
        # 填充区间
        if self._mode == "indeterminate":
            seg = 0.30
            f0, f1 = max(0.0, self._ind - seg), min(1.0, self._ind)
        else:
            f0, f1 = 0.0, self._frac
        px0 = pad + int(f0 * (w - 2 * pad))
        px1 = pad + int(f1 * (w - 2 * pad))
        self._fpx0, self._fpx1 = px0, px1
        # 交给各角色专属画法
        try:
            getattr(self, "_bar_" + style)(w, h, pad, ty1, ty2, r, px0, px1, f0, f1)
        except Exception:
            self._bar_lace(w, h, pad, ty1, ty2, r, px0, px1, f0, f1)
        # 百分比文字
        if self._mode != "indeterminate":
            ft = _mono(F_SM_B) if lay["mono"] else F_SM_B
            self.create_text(w // 2, (ty1 + ty2) // 2, text="%d%%" % int(self._frac * 100),
                             font=ft, fill=C["plum"] if C.get("mode") != "dark" else C["pink_l"],
                             tags="b")
        self._ensure_anim()

    # 女仆 · 桃桃：蕾丝花边胶囊 + 爱心游标 + 拖尾小爱心 + 微光
    def _bar_lace(self, w, h, pad, ty1, ty2, r, px0, px1, f0, f1):
        self._rr(pad, ty1, w - pad, ty2, r, fill=C["pink_xl"], outline=C["line"])
        if px1 > px0 + 2:
            self._fill_grad(px0, ty1, px1, ty2, r, C["pink"], _acc2())
            self._shimmer(ty1, ty2)
            # 上缘蕾丝：一排半圆小 scallop
            step = self._p(7)
            x = px0 + step // 2
            while x < px1:
                self.create_arc(x - step // 2, ty1 - 1, x + step // 2, ty1 + self._p(4),
                                start=180, extent=180, style="arc", outline=C["card"], tags="b")
                x += step
        ky = (ty1 + ty2) // 2
        kx = px1 if f1 > 0.02 else (pad + self._p(6))
        _heart(self, kx, ky, self._p(7), C["card"], tags="b")
        _heart(self, kx, ky, self._p(4), C["pink"], tags="b")
        for i, d in enumerate((self._p(12), self._p(20))):
            tx = kx - d
            if self._clipfill(tx):
                _heart(self, tx, ky - self._p(2 + i), self._p(3 - i), C["pink_l"], tags="b")

    # 汉服 · 墨墨：水墨长卷 + 朱印游标 + 云纹
    def _bar_ink(self, w, h, pad, ty1, ty2, r, px0, px1, f0, f1):
        self._rr(pad, ty1, w - pad, ty2, r, fill=C["card"], outline=C["ink_l"])
        if px1 > px0 + 2:
            self._fill_grad(px0, ty1, px1, ty2, r, C["ink"], _mix(C["ink"], C["pink"], 0.75))
            # 原样是再铺一层 C["navy"]，但 _chrome 里 navy == ink，等于白画还盖掉渐变 → 改薄反光
            self._rr(px0, ty1, px1, ty1 + (ty2 - ty1) // 2, r,
                     fill=_mix(C["ink"], "#FFFFFF", 0.14), outline="")
            self._shimmer(ty1, ty2)
            # 墨点飞白
            step = self._p(15)
            x = px0 + step
            while x < px1 - 2:
                self.create_oval(x, ty1 + self._p(3), x + self._p(2), ty1 + self._p(5),
                                 fill=C["ink_l"], outline="", tags="b")
                x += step
        ky = (ty1 + ty2) // 2
        kx = px1 if f1 > 0.02 else (pad + self._p(6))
        s = self._p(6)
        self.create_rectangle(kx - s, ky - s, kx + s, ky + s, fill=C["pink_d"], outline=C["card"], width=2, tags="b")
        self.create_text(kx, ky, text="印", font=F_SM_B, fill=C["card"], tags="b")

    # 魔法 · 莉兹：星屑药丸 + 大星游标 + 闪烁星屑 + 微光
    def _bar_sparkle(self, w, h, pad, ty1, ty2, r, px0, px1, f0, f1):
        self._rr(pad, ty1, w - pad, ty2, r, fill=C["pink_xl"], outline=C["line"])
        if px1 > px0 + 2:
            self._fill_grad(px0, ty1, px1, ty2, r, C["pink"], _acc2())
            self._rr(px0, ty1, px1, (ty1 + ty2) // 2, r,
                     fill=_mix(_acc2(), "#FFFFFF", 0.55), outline="")
            self._shimmer(ty1, ty2)
            step = self._p(13)
            x = px0 + step // 2
            i = 0
            while x < px1:
                if (i + int(self._ph * 6)) % 3 == 0:   # 相位驱动的星闪
                    _star(self, x, ty1 + (ty2 - ty1) // 2, self._p(3), C["card"], tags="b")
                x += step
                i += 1
        ky = (ty1 + ty2) // 2
        kx = px1 if f1 > 0.02 else (pad + self._p(6))
        _star(self, kx, ky, self._p(8), C["gold"], tags="b")
        _star(self, kx, ky, self._p(4), C["card"], tags="b")

    # 巫女 · 小夜：御守红白 + 神乐铃游标 + 飘落樱花瓣
    def _bar_shrine(self, w, h, pad, ty1, ty2, r, px0, px1, f0, f1):
        self._rr(pad, ty1, w - pad, ty2, r, fill=C["card"], outline=C["line"])
        if px1 > px0 + 2:
            self._fill_grad(px0, ty1, px1, ty2, r, C["pink_d"], C["pink"])
            step = self._p(10)
            x = px0
            band = False
            while x < px1:
                if band:
                    self.create_rectangle(x, ty1, min(x + step, px1), ty2,
                                          fill=C["card"], outline="", tags="b")
                band = not band
                x += step
            self._rr(px0, ty1, px1, ty2, r, fill="", outline=C["pink"], tags="b")
            self._shimmer(ty1, ty2)
        ky = (ty1 + ty2) // 2
        kx = px1 if f1 > 0.02 else (pad + self._p(6))
        rr = self._p(6)
        self.create_oval(kx - rr, ky - rr, kx + rr, ky + rr, fill=C["gold"], outline=C["card"], width=2, tags="b")
        self.create_line(kx, ky + rr, kx, ky + rr + self._p(4), fill=C["gold"], width=2, tags="b")
        # 樱花瓣沿上缘飘落
        step = self._p(18)
        x = px0 + int(self._ph * step)
        while x < px1:
            self.create_oval(x, ty1 + self._p(1), x + self._p(4), ty1 + self._p(4),
                             fill=C["pink_l"], outline="", tags="b")
            x += step

    # 机娘 · 零号 / 赛博：HUD 细条 + 均衡器分段 + 刻度 + 金色前缘扫描
    def _bar_hud(self, w, h, pad, ty1, ty2, r, px0, px1, f0, f1):
        self.create_rectangle(pad, ty1, w - pad, ty2, fill=C["pink_xl"], outline=C["line"])
        if px1 > px0 + 2:
            step = self._p(6)
            span = max(1, px1 - px0)
            x = px0
            while x < px1:
                # 逐段在两个签名色之间插值 → HUD 的"混色均衡器"（硬边分段不适合贴图渐变）
                seg = _mix(C["pink"], _acc2(), (x - px0) / float(span))
                self.create_rectangle(x + 1, ty1 + 1, min(x + step - 1, px1), ty2 - 1,
                                      fill=seg, outline="")
                x += step
        for tx in range(pad + self._p(10), w - pad, self._p(18)):
            self.create_line(tx, ty1 - 2, tx, ty1, fill=C["line"], tags="b")
        if f1 > 0.02:
            self.create_line(px1, ty1 - 3, px1, ty2 + 3, fill=C["gold"], width=2, tags="b")
            self.create_rectangle(px1 - self._p(3), ty1, px1, ty2, fill=C["gold"], outline="", tags="b")

    # 哥特 · 薇拉：暗夜菱格 + 蝙蝠游标 + 荆棘上缘 + 十字星屑
    def _bar_bat(self, w, h, pad, ty1, ty2, r, px0, px1, f0, f1):
        self._rr(pad, ty1, w - pad, ty2, r, fill=C["card"], outline=C["pink_d"])
        if px1 > px0 + 2:
            self._fill_grad(px0, ty1, px1, ty2, r, C["navy"], _mix(C["navy"], C["pink"], 0.62))
            self._fill_grad(px0, ty1, px1, (ty1 + ty2) // 2, r, C["pink_d"], _acc2())
            self._shimmer(ty1, ty2)
            step = self._p(14)
            x = px0 + step // 2
            while x < px1:
                _diamond(self, x, (ty1 + ty2) // 2, self._p(3), C["pink"], tags="b")
                x += step
            # 荆棘上缘
            step2 = self._p(8)
            x = px0
            up = True
            while x < px1:
                y = ty1 + (0 if up else self._p(3))
                self.create_line(x, y, x + step2 // 2, ty1 + (self._p(3) if up else 0),
                                 fill=C["pink_d"], tags="b")
                up = not up
                x += step2 // 2
        ky = (ty1 + ty2) // 2
        kx = px1 if f1 > 0.02 else (pad + self._p(6))
        s = self._p(7)
        # 蝙蝠剪影：中央身 + 两翼三角
        self.create_oval(kx - self._p(2), ky - self._p(2), kx + self._p(2), ky + self._p(2),
                         fill=C["ink"], outline="", tags="b")
        self.create_polygon(kx - self._p(2), ky, kx - s, ky - self._p(3), kx - s, ky + self._p(2),
                            kx - self._p(2), ky + self._p(2), fill=C["ink"], outline="", tags="b")
        self.create_polygon(kx + self._p(2), ky, kx + s, ky - self._p(3), kx + s, ky + self._p(2),
                            kx + self._p(2), ky + self._p(2), fill=C["ink"], outline="", tags="b")


class Tooltip:
    """悬浮小贴士（可爱深粉底）。给 widget 绑定 Enter/Leave。"""

    def __init__(self, widget, text):
        self.w, self.t, self.tip = widget, text, None
        widget.bind("<Enter>", self.show, add="+")
        widget.bind("<Leave>", self.hide, add="+")

    def show(self, _e=None):
        if self.tip or not self.t:
            return
        x = self.w.winfo_rootx() + 10
        y = self.w.winfo_rooty() + self.w.winfo_height() + 6
        self.tip = tw = tk.Toplevel(self.w)
        tw.wm_overrideredirect(True)
        lbl = tk.Canvas(tw, highlightthickness=0, bd=0, bg=C["ink"])
        tid = lbl.create_text(9, 8, text=self.t, font=F_SM,
                              fill="#FFEAF3", anchor="w")
        _x1, _y1, x2, y2 = lbl.bbox(tid)
        w, h = x2 + 12, max(y2 + 9, 26)
        lbl.configure(width=w, height=h)
        _rr(lbl, 0, 0, w, h, 8, fill=C["ink"], outline=C["pink"])
        lbl.coords(tid, 6, h / 2)
        lbl.tag_raise(tid)
        lbl.pack()
        tw.geometry(f"+{x}+{y}")

    def hide(self, _e=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


def precheck_html(url, log=noop_log):
    """预检取页面 HTML（失败返回 None，调用方按 normal 路由走）。"""
    # 境内必墙大站（YouTube/Google/Twitch…）直连必然超时，且它们本就不是 AList/MSE/加密站，
    # 预检毫无意义——直接跳过，交给走代理的 ydl_probe（正确引擎），省掉 ~60s 空耗。
    if any(h in (url or "").lower() for h in BLOCKED_HINTS):
        log("[预检] 境内必墙站，跳过直连预检，交由代理解析…")
        return None
    try:
        return SESSION.get(url, headers={"User-Agent": UA}, timeout=8).text
    except Exception as e:
        log(f"[预检] 页面获取失败: {str(e)[:120]}")
        return None


def route_by_precheck(pre_html, url):
    """根据预检 HTML 决定嗅探引擎，返回 alist/files/encrypted/list/mse/normal。
    GUI 与 CLI 共用一份判定，避免两处逻辑漂移。"""
    if not pre_html:
        return "normal"
    if is_alist_site(pre_html, url):
        return "alist"
    # 页面 HTML 里直接写死 m3u8/mp4（DPlayer/hls.js 加密博客站）→ 加密引擎静态抽取，免浏览器
    if collect_static_streams(pre_html, url, dedup=False):
        return "encrypted"
    file_pre = collect_file_entries(pre_html, url)
    players = [p for p in
               re.findall(r'data-url="([^"]+)"', pre_html)
               + re.findall(r'<iframe[^>]+src="([^"]+)"', pre_html)
               if p.startswith("http")
               and any(h in p for h in PLAYER_HOST_HINTS)
               and is_embed_player(p, url)]
    if file_pre and len(file_pre) >= 2 and not players:
        return "files"
    if players:
        return "encrypted"
    if re.search(r'href="[^"]*?/videos/[a-f0-9]+"', pre_html):
        return "list"
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ""
    vl = [h for h in re.findall(r'href="(https?://[^"]+|/[^"]+)"', pre_html)
          if re.search(VIDEO_LINK_PAT, h) or re.search(r'/\d{5,}', h)]
    slugs = collect_slug_links(pre_html, url)
    if (len(vl) >= 3 or len(slugs) >= 3) and host:
        return "list"
    if is_mse_candidate(pre_html, url):
        # 有 <video> 元素但无直链 m3u8/mp4 → 疑似 MSE/blob 流站
        return "mse"
    return "normal"


# ---------------- 孤儿 aria2c 判定（B9：v2 Tk / v3 内核 python.exe / Electron 宿主并存时不能互杀）----------------
_SNIFFDL_OWNER_MARKS = ("sniffdl_core.py", "video_downloader.py", "sniffdl.exe")


def aria2_stop_with_pid(cmdline):
    """从 aria2c 命令行取 --stop-with-process 的 PID（--stop-with-process=123 或 --stop-with-process 123）；没有返回 0。"""
    m = re.search(r"--stop-with-process(?:=|\s+)[\"']?(\d+)", cmdline or "")
    return int(m.group(1)) if m else 0


def is_sniffdl_owner_cmdline(cmdline):
    """命令行像不像一个 SniffDL 主程序（v3 内核 / 旧 Tk 版 / 打包 exe）。aria2c 自己的命令行不算。"""
    c = (cmdline or "").lower().replace("\\", "/")
    first = c.strip().strip('"').split(" ", 1)[0]
    if first.rsplit("/", 1)[-1].startswith("aria2c"):
        return False
    return any(m in c for m in _SNIFFDL_OWNER_MARKS)


def aria2_orphan_verdict(cmdline, alive, runtime_pid=0, owner_pids=(), my_pids=(), pid=0):
    """纯函数：一个命令行带我们数据目录的 aria2c 是不是孤儿。返回 (是否孤儿, 原因)。
      cmdline     该 aria2c 的完整命令行
      alive(pid)  进程是否活着
      runtime_pid runtime.json 里的内核 pid（没有填 0）
      owner_pids  其它活着、命令行含 sniffdl_core.py / video_downloader.py / SniffDL.exe 的进程
      my_pids     本进程自己拉起的 aria2c（BT / 直链守护），永不算孤儿
    规则：1) 命令行有 --stop-with-process=<pid> → 只看那个 pid 活不活（活着=有主人）；
          2) 否则 runtime.json 的 pid 活着 → 有主人；3) 否则还有别的 SniffDL 主程序进程 → 有主人；4) 都没有 → 孤儿。"""
    if pid and pid in set(int(x) for x in my_pids if x):
        return False, "本进程的守护"
    owner = aria2_stop_with_pid(cmdline)
    if owner:
        if alive(owner):
            return False, "主人 PID %d 活着" % owner
        return True, "主人 PID %d 已退出" % owner
    if runtime_pid and alive(runtime_pid):
        return False, "runtime.json 内核 PID %d 活着" % runtime_pid
    live = [p for p in owner_pids if p and alive(p)]
    if live:
        return False, "SniffDL 进程 %s 活着" % live[0]
    return True, "无主"


_LIST_PROC_DIAG = {}      # 最近一次 list_processes 的诊断（rc/输出长度/stderr/异常），实机排障用


def list_processes():
    """[(pid, name, cmdline)]：Windows 用 CIM，其它系统读 /proc。失败返回空表。"""
    out = []
    try:
        if os.name == "nt":
            # 输出强制 UTF-8 + 按字节自己解码：Windows PowerShell 5.1 写管道用 OEM 代码页（cp437/cp936），
            # Python text=True 却按 ANSI 代码页解码 → 中文用户名路径变 ? 甚至整段解码异常 → 进程表空、孤儿永远认不出。
            # Get-CimInstance 失败（WMI 服务慢/被策略禁用）再退 Get-WmiObject；整体超时放宽到 60s（首次冷启动 WMI 很慢）。
            ps = ("$ErrorActionPreference='Stop';"
                  "try{[Console]::OutputEncoding=[System.Text.Encoding]::UTF8}catch{};$OutputEncoding=[System.Text.Encoding]::UTF8;"
                  "try{$p=Get-CimInstance -ClassName Win32_Process}catch{$p=Get-WmiObject -Class Win32_Process};"
                  "@($p | Where-Object { $_.Name -match 'aria2c|SniffDL|python|electron' } | "
                  "Select-Object ProcessId,Name,CommandLine) | ConvertTo-Json -Compress -Depth 2")
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                "-Command", ps], capture_output=True, timeout=60, creationflags=0x08000000)
            raw = r.stdout or b""
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "replace")
            raw = raw.lstrip("\ufeff").strip()
            _LIST_PROC_DIAG.update(rc=getattr(r, "returncode", None), out_len=len(raw),
                                   err=(r.stderr.decode("utf-8", "replace") if isinstance(r.stderr, bytes)
                                        else str(r.stderr or ""))[-400:])
            i = min([x for x in (raw.find("["), raw.find("{")) if x >= 0] or [0])
            data = json.loads(raw[i:] or "[]") if raw else []
            if isinstance(data, dict):
                data = [data]
            for d in data or []:
                out.append((int(d.get("ProcessId") or 0), str(d.get("Name") or ""), str(d.get("CommandLine") or "")))
            _LIST_PROC_DIAG["n"] = len(out)
            return out
        for p in os.listdir("/proc"):
            if not p.isdigit():
                continue
            try:
                with open("/proc/%s/cmdline" % p, "rb") as f:
                    parts = [x.decode("utf-8", "ignore") for x in f.read().split(b"\0") if x]
                if not parts:
                    continue
                out.append((int(p), os.path.basename(parts[0]), " ".join(parts)))
            except Exception:
                continue
    except Exception as e:
        _LIST_PROC_DIAG["exc"] = repr(e)[:300]
        return []
    return out


def find_orphan_aria2c(datadir, procs, alive, runtime_pid=0, my_pids=(), self_pid=None):
    """纯函数（进程表由调用方给）：返回 [(pid, 原因)]，只看命令行里真带着我们数据目录的 aria2c。"""
    parts = [p for p in re.split(r"[\\/]+", (datadir or "").strip()) if p]
    key = (parts[-2] if len(parts) >= 2 else ".snifftdl").lower()
    self_pid = os.getpid() if self_pid is None else self_pid
    if runtime_pid == self_pid:
        runtime_pid = 0                   # runtime.json 写的就是自己：不能拿自己当「别的主人」
    owners = [pid for pid, _n, cl in procs if pid != self_pid and is_sniffdl_owner_cmdline(cl)]
    res = []
    for pid, name, cl in procs:
        if not (name or "").lower().startswith("aria2c"):
            continue
        low = (cl or "").lower().replace("\\", "/")
        if ("/" + key + "/") not in low and ("/" + key) not in low:
            continue
        orphan, why = aria2_orphan_verdict(cl, alive, runtime_pid, owners, my_pids, pid)
        if orphan:
            res.append((pid, why))
    return res


class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.entries = []
        self.busy = False
        self.page_url = ""
        self._last_clip = ""
        self.cfg = load_config()
        self._cfg_migrate_notes = list(_CFG_MIGRATE_NOTES)   # 日志控件就绪后再输出（见 _rpc_start）
        # 主题：桌面桌宠已统一为单一 Live2D「大肥鱼」→ Tk 主界面也恒用 feiyu 蓝主题（六界已退役，不再随旧配置切换）
        _t = self.cfg.get("theme")
        self.theme = "feiyu" if "feiyu" in THEMES else (_t if _t in THEMES else DEFAULT_TKEY)
        _cur_theme[0] = self.theme
        # Web UI 模式：webview 靠本机 JSON-RPC 取数据，必须确保 RPC 已启用（端口/token 沿用配置）。
        self._web_mode = (self.cfg.get("ui_mode", "web") == "web")
        if self._web_mode:
            self.cfg["rpc_enable"] = True
        C.clear()
        C.update(_chrome(THEMES[self.theme]["palette"], self.theme))
        DASH_ARIA2[0] = bool(self.cfg.get("dash_aria2", True))
        SNIFF_BROWSER[0] = str(self.cfg.get("sniff_browser") or "自动")
        # 解析 aria2c（PATH / 系统已装 / Motrix 自带 / 用户钉选路径）；档位在下载时生效
        _found = resolve_aria2c(self.cfg)
        if _found and self.cfg.get("aria2c_path") != _found:
            self.cfg["aria2c_path"] = _found
            save_config(self.cfg)
        # 解析 EJS 解算器 JS 运行时（deno/node），前置进 PATH，YouTube 等混淆站才能解签名
        _js = resolve_js_runtime(self.cfg)

        # 打包版与源码版同名会分不清（真踩过：用户拿旧 exe 对比新 UI 说"没圆角"）
        # → 源码启动时带 SNIFFDL_DEV=1 就在标题上标出来
        root.title(APP_TITLE + ("  ·  源码版（未打包）"
                                if os.environ.get("SNIFFDL_DEV") else ""))
        # 像素尺寸统一按 DPI 缩放系数 S 放大（字体用 pt，Tk 已随 DPI 自动缩放）
        try:
            self.S = max(0.9, float(root.tk.call("tk", "scaling")) / (96.0 / 72.0))
        except Exception:
            self.S = 1.0

        def P(v):
            return int(round(v * self.S))

        self.P = P
        _UI_S[0] = self.S          # Card / Pill 等自绘控件没有 self.P，统一从这里取
        want = "%dx%d" % (P(1120), P(780))
        geo = self.cfg.get("geometry") or ""
        m = re.match(r"^(\d+)x(\d+)", geo)
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        # 用户要求：默认“居中小巧”，像正经下载器那样；历史存档太大（含曾被最大化）就弃用
        if m and int(m.group(1)) >= P(1000) and int(m.group(2)) >= P(680) \
                and int(m.group(1)) <= sw * 0.60 and int(m.group(2)) <= sh * 0.70:
            root.geometry(geo)
            ww, wh = int(m.group(1)), int(m.group(2))
        else:
            root.geometry(want)
            ww, wh = P(1120), P(780)
        try:
            root.geometry("%dx%d+%d+%d" % (ww, wh,
                            max(0, (sw - ww) // 2), max(0, (sh - wh) // 2 - 24)))
        except Exception:
            pass
        root.minsize(P(1000), P(700))
        root.configure(bg=C["bg"])
        _ic = _art("AVATAR", self.S)
        if _ic is not None:
            try:
                root.iconphoto(True, _ic)
            except Exception:
                pass
        self._apply_girly_fonts()
        self._setup_styles()

        # ================= 新骨架：左侧栏 + 右侧(topbar + 视图) =================
        # 旧装饰画布(bgcv/header/rail)降为隐藏 1×1 dummy：保留属性名，别处残留的
        # self.header/self.rail 引用不炸；不 pack、不绘制。立绘/碎花/气泡全部交给桌宠。
        self.header = tk.Canvas(root, width=1, height=1, bg=C["bg"], highlightthickness=0)
        self.rail = self.header
        self.bgcv = self.header
        self._bgsize = (0, 0)
        self._bgjob = None
        self._bubble = self.line_for("greet")
        self._body_anchor = None
        self.root.bind("<Control-t>", lambda e: self.cycle_theme(1))
        self.root.bind("<Control-T>", lambda e: self.cycle_theme(-1))

        self._build_sidebar(root)          # 左栏：brand + 看板娘 profile + 导航 + 换装
        self._build_content(root)          # 右侧：topbar + 视图容器(下载/队列/日志/设置)
        self._show_view("dl")
        self._refresh_profile()

        # ---- 网址输入卡片（→ 下载视图）----
        _top_card = Card(self.view_dl, bg=C["bg"], body_padx=P(12), body_pady=P(10))
        _top_card.pack(fill="x", padx=P(18), pady=(P(12), P(6)))
        top = _top_card.body          # 子控件照旧挂 top，一行不改
        self._body_anchor = _top_card        # 兼容旧引用
        # 左侧标签：轻量文字（不再用实心色块）
        chip = tk.Label(top, text="网址", bg=C["card"], fg=C["ink_l"], font=F_UI)
        chip.pack(side="left", padx=(P(2), P(10)))
        # 输入框（软粉色描边）
        url_box = tk.Frame(top, bg=C["pink_xl"], highlightbackground=C["pink_l"],
                           highlightthickness=1, padx=P(2), pady=P(2))
        url_box.pack(side="left", fill="x", expand=True, padx=(P(4), P(10)))
        self.url_var = tk.StringVar()
        ent = ttk.Entry(url_box, textvariable=self.url_var, style="Maid.TEntry")
        ent.pack(side="left", fill="x", expand=True, padx=P(4), pady=P(2))
        ent.bind("<Return>", lambda e: self.start_probe())
        ent.focus_set()
        # 抓取范围：自绘分段按钮（选中态明显，不再依赖 ttk 小圆点）
        self.scope_var = tk.StringVar(value=self.cfg.get("scope") or "single")
        seg = tk.Frame(top, bg=C["line"], padx=1, pady=1)
        seg.pack(side="left", padx=(P(2), P(8)))
        self._scope_lbls = {}
        for val, txt_off, txt_on, tip in (
                ("single", "○ 仅本页视频", "● 仅本页视频",
                 "只嗅探你粘贴这一页里内嵌的视频，瞬间出结果（像猫抓），"
                 "绝不爬进其它页面——适合直接贴某个视频播放页"),
                ("all", "○ 全站列表", "● 全站列表",
                 "额外爬进页面里的详情/列表链接，逐个进页把整站视频都扒出来，"
                 "慢但更全——适合贴分类页/合集页")):
            lb = tk.Label(seg, text=txt_off, font=F_UI, cursor="hand2",
                          padx=P(10), pady=P(4))
            lb.pack(side="left")
            lb._txt_off, lb._txt_on = txt_off, txt_on
            lb.bind("<Button-1>", lambda _e, v=val: (self.scope_var.set(v),
                                                     self._repaint_scope()))
            Tooltip(lb, tip)
            self._scope_lbls[val] = lb
        self._repainters = getattr(self, "_repainters", [])
        if self._repaint_scope not in self._repainters:
            self._repainters.append(self._repaint_scope)
        try:
            self.scope_var.trace_add("write", lambda *_: self._repaint_scope())
        except Exception:
            pass
        self._repaint_scope()
        self.probe_btn = RoundButton(top, text=self._probe_cta_text(), command=self.start_probe,
                                     padx=P(18), pady=P(8))
        self.probe_btn.pack(side="left")
        Tooltip(self.probe_btn, "隐身浏览器出动，为您搜遍全页视频")
        self._repainters = getattr(self, "_repainters", [])
        if self._repaint_probe_cta not in self._repainters:
            self._repainters.append(self._repaint_probe_cta)
        self.bt_btn = RoundButton(top, text="⊛ 种子/磁力", kind="ghost",
                                  command=self.open_bt_panel,
                                  padx=P(14), pady=P(8))
        self.bt_btn.pack(side="left", padx=(P(8), 0))
        Tooltip(self.bt_btn, "小夜的 BT 分区：粘贴磁力链接或选择 .torrent 种子，"
                             "交给内置 aria2c 引擎原生下载（DHT+PeX+tracker 订阅），进度实时回显")

        # ---- 结果表格卡片（→ 下载视图）----
        _mid_card = Card(self.view_dl, bg=C["bg"])
        _mid_card.pack(fill="both", expand=True, padx=P(18), pady=(P(0), P(6)))
        mid = _mid_card.body
        rbar = tk.Canvas(mid, height=P(28), bg=C["pink_xl"],
                         highlightthickness=0, bd=0)
        rbar.pack(fill="x")
        self.res_bar = rbar
        self._res_title_text = _voice()["res"]
        self._res_title = rbar.create_text(P(10), P(14), anchor="w",
                                           text=self._res_title_text,
                                           fill=C["plum"], font=F_SM)
        self._res_count = rbar.create_text(0, P(14), anchor="e", text="",
                                           fill=C["ink_l"], font=F_SM)
        self._res_flabel = rbar.create_text(0, P(14), anchor="e", text="筛选 ♪",
                                            fill=C["ink_l"], font=F_SM)
        self.count_var = tk.StringVar(value="")
        self.filter_var = tk.StringVar()
        self._res_fent = ttk.Entry(rbar, textvariable=self.filter_var, width=16,
                                   style="Maid.TEntry")
        self._res_fwin = rbar.create_window(0, P(14), window=self._res_fent,
                                            anchor="e", width=P(126), height=P(21))
        self._res_fent.bind("<Escape>",
                            lambda e: (self.filter_var.set(""), self.refresh_table()))
        Tooltip(self._res_fent,
                "输入关键词即时过滤（标题/格式/分辨率/来源），空格表示并且，Esc 清空")
        self.filter_var.trace_add("write", lambda *_: self.refresh_table())
        self.count_var.trace_add("write", lambda *_: self._draw_resbar())
        rbar.bind("<Configure>", lambda e: self._draw_resbar())
        self._sort = [None, False]          # [排序列, 是否降序]
        self.row_status = {}                # row → 状态文本（重建表格时不丢进度）
        self._mood = set()                  # 已说过的进度台词，避免刷屏
        self._queue_urls = []               # 批量嗅探：还没跑的页面
        self._batch_acc = None              # 批量嗅探：已合并结果（None=单页模式）
        spec = [("#", 40, "center", "no"), ("文件名/标题", 250, "w", "title"),
                ("格式", 52, "center", "ext"), ("分辨率", 82, "center", "res"),
                ("时长", 66, "center", "dur"), ("大小", 82, "center", "size"),
                ("进度", 124, "w", "prog"), ("状态", 150, "center", "status"),
                ("来源", 78, "center", "how")]
        cols = tuple(c for _t, _w, _a, c in spec)
        self._head_base = {c: text for text, _w, _a, c in spec}
        twrap = tk.Frame(mid, bg=C["card"])
        twrap.pack(fill="both", expand=True, padx=P(1), pady=P(1))
        # 自绘列表：Treeview 单元格画不出圆角进度条/状态胶囊（网页稿 .track/.pill）
        self.tree = ResultsList(twrap, columns=spec, selectmode="extended",
                                bg=C["card"])
        for text, w, anchor, c in spec:
            self.tree.heading(c, text=text, command=lambda cc=c: self.sort_by(cc))
            self.tree.column(c, width=w, anchor=anchor, stretch=(c == "title"))
        self.tree.tag_configure("odd", background=_tint(self.theme, 0) or C["card"])
        self.tree.tag_configure("even", background=_tint(self.theme, 1) or C["pink_xl"])
        vsb = ttk.Scrollbar(twrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(1, 0), pady=1)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda e: self.start_download(True))
        self.tree.bind("<Button-3>", self._tree_menu)
        self.tree.bind("<Control-a>", lambda e: self._select_all())
        self.tree.bind("<Control-A>", lambda e: self._select_all())
        Tooltip(self.tree, "双击某行＝只下载它；点表头排序；Ctrl+A 全选；右键＝复制真直链/终止/删除/载入记忆")
        self.menu = tk.Menu(root, tearoff=0, bg="white", fg=C["ink"],
                            activebackground=C["pink_l"],
                            activeforeground=C["pink_d"], borderwidth=1,
                            relief="solid", font=F_UI)
        self.menu.add_command(label="复制选中直链(仅真链)", command=self._copy_urls)
        self.menu.add_command(label="⬇ 下载选中(加入队列)",
                              command=lambda: self.start_download(True))
        self.menu.add_command(label="慢速取片(选中行的页面)",
                              command=lambda: self.start_slow_fetch(True))
        self.menu.add_command(label="全选 (Ctrl+A)", command=self._select_all)
        self.menu.add_command(label="反选", command=self._invert_sel)
        self.menu.add_command(label="✋ 终止选中(未选=整批)", command=self.terminate_sel)
        self.menu.add_command(label="打开文件位置(已下载)", command=self._open_row_file_dir)
        self.menu.add_separator()
        self.menu.add_command(label="从列表删除(不删文件)",
                              command=lambda: self.delete_sel(False))
        self.menu.add_command(label="删除并送回收站(含本地文件)",
                              command=lambda: self.delete_sel(True))
        self.menu.add_command(label="♡ 载入下载记忆", command=self.load_memory)

        # ---- 日志卡片（→ 日志视图）----
        _log_card = Card(self.view_log, bg=C["bg"], body_bg=_LOG_BG)
        _log_card.pack(fill="both", expand=True, padx=P(18), pady=P(12))
        logf = _log_card.body
        self.log_hdr = tk.Canvas(logf, height=P(26), bg=_LOG_BG,
                                 highlightthickness=0, bd=0)
        self.log_hdr.pack(fill="x")
        self._log_title_text = _voice()["log"]
        self._log_title = self.log_hdr.create_text(P(10), P(13), anchor="w",
                                                   text=self._log_title_text,
                                                   fill=_LOG_TEAL, font=F_SM)
        self.log_hdr.bind("<Configure>", lambda e: self._draw_logbar())
        self.log_text = tk.Text(logf, height=7, state="disabled",
                                font=("Consolas", 10), wrap="none",
                                bg=_LOG_BG, fg=_LOG_FG, insertbackground=_LOG_TEAL,
                                highlightthickness=0, relief="flat", padx=P(14),
                                pady=P(10), selectbackground=C["pink_l"],
                                selectforeground="#FFFFFF")
        lsb = ttk.Scrollbar(logf, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=lsb.set)
        self.log_text.pack(side="left", fill="both", expand=True, padx=(2, 0), pady=2)
        lsb.pack(side="right", fill="y")

        # ---- 设置分组卡片：通用（开机自启，放在最顶明显处）----
        sec_gen = self._sec("通用", "开机自启等常规选项", icon="general")
        row_gen = tk.Frame(sec_gen, bg=C["card"])
        row_gen.pack(fill="x", pady=P(4))
        self.autostart_var = tk.BooleanVar(value=self.get_autostart())
        self.autostart_chk = ttk.Checkbutton(
            row_gen, text="开机自动启动 SniffDL（后台常驻，桌宠与下载服务随时待命）",
            variable=self.autostart_var, style="Maid.TCheckbutton",
            command=lambda: self.set_autostart(self.autostart_var.get()))
        self.autostart_chk.pack(side="left")
        Tooltip(self.autostart_chk,
                "开机自动在后台常驻：写入当前用户的 Windows 启动项"
                "（HKCU\\...\\Run，免管理员权限），关闭即移除。开启后浏览器插件随时能把下载丢给它。")
        # 桌宠唤醒策略：默认开机唤醒；关掉则启动即收起，Ctrl+Alt+H 随时唤醒/收起（同一个键）
        row_pet = tk.Frame(sec_gen, bg=C["card"])
        row_pet.pack(fill="x", pady=P(4))
        self.pet_wake_var = tk.BooleanVar(value=bool(self.cfg.get("pet_wake", True)))
        self.pet_wake_chk = ttk.Checkbutton(
            row_pet, text="开机唤醒桌宠（关闭则启动即收起，按 Ctrl+Alt+H 唤醒/收起）",
            variable=self.pet_wake_var, style="Maid.TCheckbutton",
            command=self._on_pet_wake_toggle)
        self.pet_wake_chk.pack(side="left")
        Tooltip(self.pet_wake_chk,
                "实时桌宠的全局唤醒键：Ctrl+Alt+H —— 她藏着时按它出现，开着时按它收起。\n"
                "取消勾选后开机不显示桌宠，随时可用该热键或托盘菜单「显示 / 隐藏桌宠」唤出。")

        # ---- 设置分组卡片：存储 ----
        sec_st = self._sec("存储", "下载保存目录", icon="store")
        inner = tk.Frame(sec_st, bg=C["card"])
        inner.pack(fill="x", pady=P(2))
        tk.Label(inner, text="保存到", bg=C["card"], fg=C["ink"],
                 font=F_UI).pack(side="left")
        self.dir_var = tk.StringVar(value=self.cfg.get("outdir") or DEFAULT_OUTDIR)
        self.dir_combo = ttk.Combobox(inner, textvariable=self.dir_var,
                                      values=list(self.cfg.get("recent_dirs") or []),
                                      style="Maid.TCombobox")
        self.dir_combo.pack(side="left", fill="x", expand=True, padx=P(8))
        RoundButton(inner, text="浏览", command=self.pick_dir, kind="ghost",
                    padx=P(14), pady=P(5)).pack(side="left", padx=(0, P(6)))
        RoundButton(inner, text="打开文件夹", command=self.open_outdir,
                    kind="ghost", padx=P(14), pady=P(5)).pack(side="left")

        # ---- 设置分组卡片：下载（画质/加速/代理/助手/完成后）----
        bot = tk.Frame(self.view_set, bg=C["bg"])   # 容器（各分组为独立卡片）
        sec_dl = self._sec("下载", "画质 · 并发 · 网络 · 助手", icon="dl")
        row_set = tk.Frame(sec_dl, bg=C["card"])
        row_set.pack(fill="x", pady=P(2))
        tk.Label(row_set, text="画质 ♪", bg=C["card"], fg=C["ink"],
                 font=F_UI).pack(side="left")
        self.quality_var = tk.StringVar(value=self.cfg.get("quality") or "最佳")
        self.quality_combo = ttk.Combobox(row_set, textvariable=self.quality_var,
                                          state="readonly", width=7,
                                          values=["最佳", "2160", "1080", "720", "480"],
                                          style="Maid.TCombobox")
        self.quality_combo.pack(side="left", padx=(P(6), P(12)))
        self.quality_combo.bind("<<ComboboxSelected>>", lambda e: self._sync_quality())
        Tooltip(self.quality_combo,
                "画质上限（下载时生效）：选 480/720/1080/2160 即只下不超过该清晰度的流，"
                "表格「分辨率」会同步按此封顶显示；选「最佳」则取该视频可用的最高清晰度。")
        tk.Label(row_set, text="加速 ♪", bg=C["card"], fg=C["ink"],
                 font=F_UI).pack(side="left")
        self.threads_var = tk.StringVar(value=str(self.cfg.get("dl_threads") or "自动"))
        self.threads_combo = ttk.Combobox(row_set, textvariable=self.threads_var,
                                          state="readonly", width=6,
                                          values=["自动", "16", "32", "64", "128", "256"],
                                          style="Maid.TCombobox")
        self.threads_combo.pack(side="left", padx=(P(6), P(12)))
        self.threads_combo.bind("<<ComboboxSelected>>", lambda e: self._sync_threads())
        Tooltip(self.threads_combo,
                "下载并发档位：m3u8/mpd 分片并发可开到此数（真并行、对 CDN 友好，"
                "这是提速关键）；单个文件直链受 aria2 上限 16，超过仍按 16 走，"
                "再高易被限速/封。自动=32")
        self._sync_threads()
        # DASH 大站单文件轨是否也交给 aria2c 多连接（关掉=回到"交回 yt-dlp 单连接"旧行为）
        self.dash_aria2_var = tk.BooleanVar(value=bool(self.cfg.get("dash_aria2", True)))
        self.dash_aria2_chk = ttk.Checkbutton(
            row_set, text="大站单文件也多线程", variable=self.dash_aria2_var,
            command=self._sync_dash_aria2, style="Maid.TCheckbutton")
        self.dash_aria2_chk.pack(side="left", padx=(0, P(10)))
        Tooltip(self.dash_aria2_chk,
                "B站/YouTube 等站点的视频轨、音频轨也交给 aria2c 多连接下载。"
                "音频轨小、常被分到慢 CDN 节点，单连接只有几十 KB/s；开启后通常快很多。"
                "若某站因浏览器指纹校验报 403，关掉即回到旧行为（下载仍会成功，只是单连接）。")
        # 代理模式（IDM 式）：跟随系统 / 不使用 / 手动
        self.proxy_var = tk.StringVar(value=self.cfg.get("proxy_mode") or "跟随系统")
        tk.Label(row_set, text="代理 ♪", bg=C["card"], fg=C["ink"],
                 font=F_UI).pack(side="left")
        self.proxy_combo = ttk.Combobox(row_set, textvariable=self.proxy_var,
                                        state="readonly", width=9,
                                        values=["跟随系统", "不使用", "手动"],
                                        style="Maid.TCombobox")
        self.proxy_combo.pack(side="left", padx=(P(6), P(12)))
        self.proxy_combo.bind("<<ComboboxSelected>>", lambda e: self._sync_proxy())
        Tooltip(self.proxy_combo,
                "跟随系统＝自动读取系统/注册表代理（推荐；YouTube/Google 等境内必墙站会直接走代理，"
                "不再先直连空耗）；\n不使用＝强制直连，关掉一切代理；\n"
                "手动＝只用你填写的代理地址（如 socks5://127.0.0.1:10808 或 http://127.0.0.1:7890）。")
        tk.Label(row_set, text="助手 ♪", bg=C["card"], fg=C["ink"],
                 font=F_UI).pack(side="left")
        _as_map = {3: "小", 2: "标准", 1: "大"}
        self.asize_var = tk.StringVar(value=_as_map.get(getattr(self, "_as_step", 2), "标准"))
        self.asize_combo = ttk.Combobox(row_set, textvariable=self.asize_var,
                                        state="readonly", width=6,
                                        values=["小", "标准", "大"],
                                        style="Maid.TCombobox")
        self.asize_combo.pack(side="left", padx=(P(6), P(12)))
        self.asize_combo.bind("<<ComboboxSelected>>", lambda e: self._set_as_step(
            {"小": 3, "标准": 2, "大": 1}.get(self.asize_var.get(), 2)))
        Tooltip(self.asize_combo,
                "右下角小助手看板娘的大小与清晰度：越大＝立绘按更高分辨率绘制（少降采样），"
                "又清晰又大，但占屏幕空间更多。改后立即生效。")
        # 登录态 cookie 来源：自动/已登录Cookie/浏览器/不使用 + 「登录」窗 + 「导入」
        sec_ck = self._sec("登录与 Cookie", "B站 / YouTube 等需登录的高画质", icon="cookie")
        row_cookie = tk.Frame(sec_ck, bg=C["card"])
        row_cookie.pack(fill="x", pady=P(2))
        tk.Label(row_cookie, text="Cookie ♪", bg=C["card"], fg=C["ink"],
                 font=F_UI).pack(side="left")
        self.cookie_var = tk.StringVar(value=self.cfg.get("cookie_browser") or "自动")
        self.cookie_combo = ttk.Combobox(row_cookie, textvariable=self.cookie_var,
                                         state="readonly", width=11,
                                         values=["自动", "已登录Cookie", "浏览器", "不使用"],
                                         style="Maid.TCombobox")
        self.cookie_combo.pack(side="left", padx=(P(6), P(8)))
        self.cookie_btn_login = RoundButton(row_cookie, text="登录", command=self._login_cookie,
                                            padx=P(8), pady=P(2))
        self.cookie_btn_login.pack(side="left", padx=(0, P(4)))
        self.cookie_btn_imp = RoundButton(row_cookie, text="导入", command=self._import_cookie,
                                          padx=P(8), pady=P(2))
        self.cookie_btn_imp.pack(side="left")
        self.cookie_combo.bind("<<ComboboxSelected>>", lambda e: self._sync_cookie())
        Tooltip(self.cookie_combo,
                "B站/YouTube 等大站需登录才有稳定/高分辨率流。\n"
                "自动＝先匿名试，命中不了才用登录态兜底；\n"
                "已登录Cookie＝只用「登录」或「导入」存下来的登录态；\n"
                "浏览器＝读你 Chrome/Edge 的登录态（被加密占用时本机读不出会自动跳过）；\n"
                "不使用＝纯匿名。")
        Tooltip(self.cookie_btn_login,
                "弹出独立浏览器窗口，在里面登录目标站点一次，程序自动抓取并保存登录 cookie "
                "到 cookies.txt（存本项目目录、不进代码；发给别人要各自登录）。")
        Tooltip(self.cookie_btn_imp,
                "导入浏览器插件/导出工具生成的 Netscape 格式 cookies.txt，合并进本项目 cookie。")
        self._sync_cookie()
        # 完成后动作（借鉴常见下载器）：默认“无”，绝不擅自关机
        self.after_var = tk.StringVar(value=self.cfg.get("after") or "无")
        tk.Label(row_set, text="完成后 ♪", bg=C["card"], fg=C["ink"],
                 font=F_UI).pack(side="left")
        self.after_combo = ttk.Combobox(row_set, textvariable=self.after_var,
                                        state="readonly", width=8,
                                        values=["无", "关机", "退出程序"],
                                        style="Maid.TCombobox")
        self.after_combo.pack(side="left", padx=(P(6), P(12)))
        Tooltip(self.after_combo, "整批下载结束后执行：关机＝60 秒后关机（命令行 shutdown /a 可撤销）")
        self._sync_proxy(prompt=False)
        # ---- 本地下载 RPC 服务（浏览器插件 / 油猴把 SniffDL 当 aria2 用）----
        sec_rpc = self._sec("本地下载服务 (RPC)", "浏览器插件 / 油猴把 SniffDL 当 aria2 用", icon="globe")
        row_rpc = tk.Frame(sec_rpc, bg=C["card"])
        row_rpc.pack(fill="x", pady=P(2))
        self.rpc_enable_var = tk.BooleanVar(value=bool(self.cfg.get("rpc_enable")))
        self.rpc_port_var = tk.StringVar(value=str(self.cfg.get("rpc_port") or 6800))
        self.rpc_token_var = tk.StringVar(value=str(self.cfg.get("rpc_token") or ""))
        self.rpc_chk = ttk.Checkbutton(row_rpc, text="本地下载服务 (RPC)",
                                       variable=self.rpc_enable_var,
                                       style="Maid.TCheckbutton",
                                       command=self._rpc_apply)
        self.rpc_chk.pack(side="left")
        tk.Label(row_rpc, text="端口", bg=C["card"], fg=C["ink_l"], font=F_UI).pack(
            side="left", padx=(P(10), P(4)))
        self.rpc_port_ent = ttk.Entry(row_rpc, textvariable=self.rpc_port_var,
                                      width=6, style="Maid.TEntry")
        self.rpc_port_ent.pack(side="left")
        tk.Label(row_rpc, text="token", bg=C["card"], fg=C["ink_l"], font=F_UI).pack(
            side="left", padx=(P(10), P(4)))
        self.rpc_token_ent = ttk.Entry(row_rpc, textvariable=self.rpc_token_var,
                                       width=12, style="Maid.TEntry")
        self.rpc_token_ent.pack(side="left")
        self.rpc_apply_btn = RoundButton(row_rpc, text="应用", kind="ghost",
                                         padx=P(10), pady=P(2), command=self._rpc_apply)
        self.rpc_apply_btn.pack(side="left", padx=(P(10), P(8)))
        self.rpc_stat_lbl = tk.Label(row_rpc, text="", bg=C["card"], fg=C["ink_l"],
                                     font=F_UI)
        self.rpc_stat_lbl.pack(side="left")
        Tooltip(self.rpc_chk,
                "开启后本机 127.0.0.1 起一个 aria2 兼容的 JSON-RPC 服务：浏览器插件、油猴脚本、"
                "网页上的“推送到 Aria2”按钮都能把下载任务直接丢给 SniffDL。仅监听本机、不对外；"
                "设了 token 更安全（客户端第一参传 token:xxx）。改完点“应用”即时生效。")
        self._rpc_render_status()
        # ---- 操作按钮条（→ 下载视图底部）----
        _act_card = Card(self.view_dl, bg=C["bg"], body_padx=P(14), body_pady=P(8))
        _act_card.pack(side="bottom", fill="x", padx=P(18), pady=(P(0), P(12)))
        row_act = _act_card.body
        self.dl_sel = RoundButton(row_act, text="♡ 下载选中", kind="ghost",
                                  padx=P(16), pady=P(8),
                                  command=lambda: self.start_download(True))
        self.dl_sel.pack(side="left", padx=(0, P(10)))
        self.dl_all = RoundButton(row_act, text="✦ 全部下载", padx=P(16), pady=P(8),
                                  command=lambda: self.start_download(False))
        self.dl_all.pack(side="left", padx=(0, P(10)))
        self.slow_btn = RoundButton(row_act, text="慢速取片", kind="ghost",
                                    padx=P(14), pady=P(8),
                                    command=self.toggle_slow_fetch)
        self.slow_btn.pack(side="left", padx=(0, P(10)))
        Tooltip(self.slow_btn, "普通嗅探拿不到时才用：打开浏览器让站点自己解密，"
                               "逐片取回，速度慢（高画质可能十几分钟到半小时）。"
                               "不会锁界面；再点一次可取消。")
        self.pause_btn = RoundButton(row_act, text="暂停队列", kind="ghost",
                                     padx=P(14), pady=P(8),
                                     command=self.toggle_pause)
        self.pause_btn.pack(side="left", padx=(0, P(10)))
        self.pause_btn.set_state("disabled")
        Tooltip(self.pause_btn, "暂停＝正在下的也会停下并保留进度；再点“继续”从断点续传（不丢已完成分片）")
        self.stop_btn = RoundButton(row_act, text="✋ 终止", kind="ghost",
                                    padx=P(14), pady=P(8),
                                    command=self.terminate_sel)
        self.stop_btn.pack(side="left", padx=(0, P(10)))
        self.stop_btn.set_state("disabled")
        Tooltip(self.stop_btn, "终止选中（默认全部）：正在下的立即中断、排队的彻底不再下载；下次勾选可重来")
        self.pause_evt = threading.Event()
        self.pause_evt.set()
        self._cancel_rows = set()
        self._started_rows = set()
        # ---- 常驻任务队列（Motrix 式：随时追加下载，不锁界面） ----
        self._pending = []            # 待跑任务 [(row, entry, outdir, quality), ...]
        self._job_lock = threading.Lock()
        self._worker_running = False  # 队列 worker 线程是否存活
        self._queued_rows = set()     # 已入队/在下的行，避免重复入队
        self._slow_lock = threading.Lock()
        self._slow_running = False    # 慢速取片是否在跑
        self._slow_cancel = threading.Event()
        self._run_stats = None        # 本轮累计统计 {ok,fail,skip,cancel,t0,failed_titles}
        # 自检 / 打开日志：收进设置页「关于与自检」分组（与其它分组同一套宫格卡片）
        sec_ab = self._sec("关于与自检", "版本、运行自检、打开日志文件", icon="info")
        try:
            tk.Label(sec_ab, text="SniffDL v0.5.9 · aria2 + yt-dlp · Playwright %s · ffmpeg %s"
                     % ("√" if HAS_PW else "×", "√" if FFMPEG else "×"),
                     bg=C["card"], fg=C["ink_l"], font=F_SM).pack(anchor="w", pady=(P(4), P(6)))
        except Exception:
            pass
        self._upd_line = tk.Label(sec_ab, text="组件版本：检查中…", bg=C["card"],
                                  fg=C["ink_l"], font=F_SM, anchor="w", justify="left")
        self._upd_line.pack(fill="x", pady=(P(2), P(2)))
        _setrow = tk.Frame(sec_ab, bg=C["card"])
        _setrow.pack(fill="x", pady=(P(2), P(6)))
        RoundButton(_setrow, text="运行自检", kind="ghost", padx=P(14), pady=P(6),
                    command=self.diagnose).pack(side="left")
        RoundButton(_setrow, text="检查组件更新", kind="ghost", padx=P(14), pady=P(6),
                    command=self.check_updates).pack(side="left", padx=(P(8), 0))
        self._upd_btn = RoundButton(_setrow, text="更新 yt-dlp", kind="primary",
                                    padx=P(14), pady=P(6),
                                    command=self.update_ytdlp)
        self._upd_btn.pack(side="left", padx=(P(8), 0))
        self._upd_btn.set_state("disabled")
        RoundButton(_setrow, text="打开日志文件", kind="ghost", padx=P(14), pady=P(6),
                    command=lambda: self.open_log()).pack(side="left", padx=(P(8), 0))

        # ---- 队列视图：总进度 + 状态 ----
        self.status_var = tk.StringVar(value="就绪")
        _qc = Card(self.view_queue, bg=C["bg"], body_padx=P(12), body_pady=P(6))
        qcard = _qc.body
        _qc.pack(fill="x", padx=P(18), pady=(P(12), P(4)))
        tk.Label(qcard, text="总进度", bg=C["card"], fg=C["ink"],
                 font=F_UI_B).pack(anchor="w", padx=P(12), pady=(P(10), 0))
        self.bar = ThemedBar(qcard, self.S)
        self.bar.pack(fill="x", padx=P(12), pady=(P(6), P(4)))
        tk.Label(qcard, textvariable=self.status_var, bg=C["card"], fg=C["ink_l"],
                 font=F_SM, anchor="w", wraplength=P(760),
                 justify="left").pack(fill="x", padx=P(12), pady=(0, P(10)))
        # 任务队列列表（镜像正在下/排队/完成的行，来自 row_status）
        _qc2 = Card(self.view_queue, bg=C["bg"])
        qcard2 = _qc2.body
        _qc2.pack(fill="both", expand=True, padx=P(18), pady=(P(0), P(12)))
        tk.Label(qcard2, text="任务队列", bg=C["card"], fg=C["ink"],
                 font=F_UI_B).pack(anchor="w", padx=P(12), pady=(P(10), P(4)))
        qwrap = tk.Frame(qcard2, bg=C["card"])
        qwrap.pack(fill="both", expand=True, padx=P(1), pady=(0, P(1)))
        qcols = ("name", "size", "prog", "status")
        self.qtree = ttk.Treeview(qwrap, columns=qcols, show="headings",
                                  style="Treeview", height=8)
        for text, w, c in (("名称", 460, "name"), ("大小", 104, "size"),
                           ("进度", 150, "prog"), ("状态", 150, "status")):
            self.qtree.heading(c, text=text)
            self.qtree.column(c, width=P(w), anchor=("w" if c == "name" else "center"),
                              stretch=(c == "name"))
        qv = ttk.Scrollbar(qwrap, orient="vertical", command=self.qtree.yview)
        self.qtree.configure(yscrollcommand=qv.set)
        self.qtree.pack(side="left", fill="both", expand=True, padx=(1, 0))
        qv.pack(side="right", fill="y")
        self._qtree_tags()
        self._q_last = None

        os.makedirs(DEFAULT_OUTDIR, exist_ok=True)
        # Q版小人助手（右下角浮动：气泡说速度/ETA + 环形进度 + 成功浮层通知）
        self._as = {"frac": 0.0, "speed": "", "eta": "", "mode": "idle",
                    "n": 0, "total": 0, "title": ""}
        self._as_job = None
        self._as_drag = None
        self._as_hidden = False
        # 助手立绘降采样档：1=原生最清晰最大、2=较清晰(默认)、3=最小最糊；越小越大越清晰
        try:
            self._as_step = max(1, min(3, int(self.cfg.get("as_step", 2) or 2)))
        except Exception:
            self._as_step = 2
        # 素材回退：cast24_legacy=true → 桃桃改用旧 muse 素材（rig 新素材出问题时一行退回）
        try:
            if C24 is not None:
                C24.legacy(bool(self.cfg.get("cast24_legacy", False)))
        except Exception:
            pass
        self._as_pos = None          # 助手绝对屏幕位置（左上角）；None=尚未定位，贴主窗右下角
        self._as_detached = False    # 被拖出后 True：自由浮动，不再跟随主窗
        # ---- 桃桃双形态 / 动作帧 / 短句指挥 ----
        self._form = "daily"                 # daily 日常形态 | work 工作形态
        self._as_transform_to = None         # 变身目标（期间播 hop + 星屑）
        self._as_transform_t0 = 0.0
        self._as_last_key = None
        self._as_anim_i = 0
        self._as_anim_job = None
        self._as_img_id = None
        self._as_greet_until = 0.0
        self._as_hug_until = 0.0
        self._as_poke_until = 0.0
        self._as_dragging = False
        self._as_last_mode = None
        self._as_success_done = False
        self._as_success_ticks = 0
        self._as_idle_vignette = 0
        self._as_idle_vignette_until = 0.0
        # ---- cast24（muse 24 帧）桃桃：一次性动作窗口 + 待机分镜轮换 ----
        self._c24_success_until = 0.0         # 下载成功跳起，只在真正成功那一刻播一遍
        self._c24_fail_until = 0.0            # 下载失败，只在失败那一刻播一遍
        self._c24_poke_until = 0.0            # 点击互动窗（钉住分镜 / 被戳反应）
        self._c24_click_state = "idle"        # 点击该演啥：多分镜=idle(干活)，单分镜=poke(被戳)
        self._c24_idle_loops = 0              # 待机整轮计数，攒够再换分镜，避免乱变
        self._c24_click_n = 0                 # §5.1 连点计次（1/2/3/4/5+ → 惊/喜/萌/羞/躲）
        self._c24_click_t = 0.0               # 上一次点击时刻，超 0.4s 清零重计
        self._c24_ignore_until = 0.0          # 躲开之后数秒内不响应点击
        self._c24_shy_cd = 0.0                # §5.2 靠近害羞的行为冷却（8s）
        # ---- 实时"活着"的两件事：视线跟鼠标 + 按节奏眨眼（都不烤死在帧里）----
        self._c24_blink_next = 0.0            # 下次眨眼的时刻
        self._c24_blink_until = 0.0           # 眨眼播放窗（期间状态机让位给 blink 分镜）
        self._c24_gaze = "c"                  # 当前视线方向 c/l/r/u/d
        self._c24_follow = False              # 鼠标在跟随范围内（太远就回归普通待机）
        self._c24_alive_job = None            # 轮询任务句柄
        self._as_bubble_until = 0.0           # 气泡存活到几点：说完话自动收起，不常年挂框
        self._as_bubble_shown = False         # 上一次绘制时气泡是否露着（用于检测露/收切换）
        # ---- 流量保险丝：一次会话累计下载量超阀就熔断，绝不再静默跑掉几个 G ----
        self._rx_bytes = 0                  # 本次会话已落盘字节数
        self._rx_blocked = False            # 熔断标记
        self._rx_cap_gb = float(self.cfg.get("rx_cap_gb", 8) or 8)   # 0 = 关闭
        self._as_click_job = None
        self._as_clip_hit = None
        self._as_form_hit = None
        self._clip_url = ""                  # 剪贴板里待接的链接
        self._clip_job = None
        self._probing = False
        self._frames_cache = {}
        # ---- v2 cast（小夜等五人）：表演调度层 + 帧缓存（maid 仍走上面 MS/MF，互不影响）----
        self._stage = ST.Stage() if ST is not None else None
        self._cast_cache = {}
        self._as_life_job = None
        self._as_hover_job = None
        self._as_stare_t = 0.0
        self._as_line_t = 0.0
        self._revert_job = None              # 收工后自动变回日常
        self._cmd_var = tk.StringVar()
        self._cmd_entry = None
        # ---- 实时桌宠（WebView2 宿主，GPU 240fps）：开启后不再建 Tk 帧动画窗，
        #      628 张预渲染帧的解码路径整体旁路（§28.1 卡顿主因随之消失）----
        self._pet = None
        self.pet_live = bool(self.cfg.get("pet_live", True)) and not _CLI_NO_PET
        if self.pet_live:
            try:
                import pet_bridge
                self._pet = pet_bridge.PetBridge(
                    skin=self.cfg.get("pet_skin") or "horn",
                    port=int(self.cfg.get("pet_port") or 6801),
                    wake=bool(self.cfg.get("pet_wake", True)))
                self._pet.start()
            except Exception:
                LOG.exception("实时桌宠桥接失败（退回旧桌宠）")
                self._pet = None
                self.pet_live = False
        try:
            if not self.pet_live and not _CLI_NO_PET:
                self._build_assistant()
        except Exception:
            LOG.exception("小人助手初始化失败（忽略，不影响主功能）")
        root.bind("<Control-h>", lambda e: self._toggle_assistant())
        root.bind("<Control-H>", lambda e: self._toggle_assistant())
        root.after(100, self._poll)
        root.after(700, self._queue_tick)
        root.after(900, self._clip_tick)
        root.after(120, self._as_anim_tick)
        root.after(1500, self._as_startup_greet)          # 开场问候动作（cast 主题笑一下）
        root.after(2000, self._as_life_tick)              # 久未操作 / 待机小动作 / 犯困（cast）
        root.after(120, self._apply_chrome)               # 窗框染成主题色（Win11 DWM）
        root.bind("<Map>", lambda e: self._apply_chrome(), add="+")  # 还原/重映射后补染
        root.after(300, lambda: self.set_mood("idle"))    # 头部「房间招牌」= 此刻心情
        self._amb_job = None
        root.after(600, self._start_amb)                  # 六世界：氛围粒子呼吸动画
        try:                                              # 任何键鼠操作 = 主人在场（重置计时）
            root.bind_all("<Any-KeyPress>", self._as_activity, add="+")
            root.bind_all("<ButtonPress>", self._as_activity, add="+")
            root.bind_all("<Motion>", self._as_activity, add="+")
        except Exception:
            pass
        root.after(1600, lambda: self._frames_for("work"))   # 空闲时预热工作帧
        root.after(3200, lambda: self._frames_for("think"))
        root.after(4800, lambda: self._frames_for("greet"))
        # OVD autoFillClipboard: 窗口聚焦时自动识别剪贴板里的视频链接
        root.bind("<FocusIn>", self._on_focus)
        # 关闭时持久化设置（目录/画质/深度/窗口几何/最近目录）；叉窗=收进托盘
        self._tray_icon = None
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(800, self._tray_setup)
        # 启动回填下载记忆（老用户一打开就能看到之前下过的，像常见下载器的历史列表）
        root.after(350, self._startup_memory)
        # ---- 本地下载 RPC 服务：浏览器插件 / 油猴 / 网页按钮把 SniffDL 当 aria2 用 ----
        self._rpc_tasks = {}          # gid -> 任务 dict
        self._rpc_order = []          # gid 插入顺序（tell* 稳定排序）
        self._rpc_lock = threading.Lock()
        self._rpc_srv = None
        self._qmem_dirty = False      # 记忆系统：任务库有变动待落盘
        self._qmem_last = 0.0
        self._qmem_loaded = False     # 本进程是否已成功读过任务库（防"空快照覆盖历史"）
        self._qmem_retried = False    # 开机文件被占用时只额外重试一次
        self._rpc_prog = {}           # gid -> 进度窗 Toplevel（去重，避免同任务多窗）
        self._rpc_sniff = {}          # task_id -> 浏览器浮标深度嗅探任务 {status,url,items,error,t0}
        # ---- 小夜 · 原生 BT 引擎：懒启动一个 aria2c --enable-rpc BitTorrent 守护进程，
        #      所有 magnet/.torrent 任务复用这一个 daemon（DHT/PeX/tracker 自动补全）----
        self._bt = {"proc": None, "port": 0, "rpc_id": 0}
        self._bt_lock = threading.Lock()
        # 开机第一件事：收掉上次硬杀主程序遗留的孤儿 aria2c —— 它攥着旧队列会在后台偷下载，
        # 主程序都没了还在跑，用户完全看不见（曾白跑 2.3GB）。
        try:
            self._bt_reap_orphans(self._bt_datadir())
        except Exception:
            pass
        root.after(1200, self._qmem_restore)   # 记忆系统：把 queue.json 的队列读回来（只恢复显示，不自动重下）
        root.after(1500, self._rpc_start)

    def _startup_memory(self):
        try:
            if self.entries or self.busy or not load_history():
                return
            self.load_memory()
        except Exception:
            pass

    # ================= 记忆系统（IDM 式持久化下载列表）=================
    # 为什么要有：队列以前只活在内存里（self._rpc_tasks），叉掉程序再开就全没了；
    # 而为了防"开机重放历史链接把电脑下一遍"（§11 洪水），旧修复把 aria2 会话里的
    # HTTP 任务在启动时全丢——矫枉过正，连用户自己下的任务也没了记忆。
    # 现在自建任务库 queue.json：重启后原样恢复【显示】，但绝不自动重下——
    #   进行中/排队/暂停 → 一律标成"未恢复"，主人点「继续」才重新投引擎；
    #   已完成 → 显示完成（并按 downloaded.json 对账，其实下完的不会被误当续传）；
    #   来源=browser 自动接管 → 同样不自动跑（洪水源头就在这儿）。
    _QMEM_FLUSH_SEC = 5.0

    def _qmem_record(self, t):
        """任务 dict → 可落盘记录（只留恢复用得上的字段，线程/进度窗之类一律不带）。"""
        try:
            hdrs = dict(t.get("headers") or {})
        except Exception:
            hdrs = {}
        try:
            return {
                "gid": str(t.get("gid") or ""),
                "uri": t.get("uri") or "",
                "name": t.get("name") or "",
                "dir": t.get("dir") or "",
                "path": t.get("path") or "",
                "status": t.get("status") or "waiting",
                "total": int(t.get("totalLength") or 0),
                "done": int(t.get("downloadedLength") or 0),
                "src": t.get("src") or "user",
                "kind": t.get("kind") or "http",
                "added_ts": float(t.get("added_ts") or 0.0),
                "done_ts": float(t.get("done_ts") or 0.0),
                "referer": t.get("referer") or "",
                "headers": hdrs,
                "error_code": str(t.get("errorCode") or "0"),
                "error": (t.get("errorMessage") or "")[:200],
                "note": t.get("note") or "",
            }
        except Exception:
            return {}

    def _qmem_snapshot(self):
        out = []
        try:
            with self._rpc_lock:
                for g in self._rpc_order:
                    t = self._rpc_tasks.get(g)
                    if not t:
                        continue
                    r = self._qmem_record(t)
                    if r and r.get("gid"):
                        out.append(r)
        except Exception:
            pass
        return out

    def _qmem_mark(self, force=False):
        """任务有变动就叫一声；真正的写盘由 _qmem_flush 节流执行（进度那种高频变动
        只标脏，5 秒最多落一次盘，绝不在下载热路径上刷磁盘）。"""
        self._qmem_dirty = True
        try:
            self._qmem_flush(force=force)
        except Exception:
            pass

    def _qmem_flush(self, force=False):
        if not getattr(self, "_qmem_dirty", False) and not force:
            return
        now = time.time()
        if not force and (now - float(getattr(self, "_qmem_last", 0.0) or 0.0)) < self._QMEM_FLUSH_SEC:
            return
        rows = self._qmem_snapshot()
        if not rows and not getattr(self, "_qmem_loaded", False):
            # 保险丝：内存里没任务 ≠ 主人没有历史。开机恢复之前那一小段窗口里，任何拿空快照
            # 来写的路径（早期竞态、别的实例、我们自己写的 bug）都会把主人的队列抹成空白，
            # 而且不可恢复 —— 真机 E2E 就出现过"自检读到 3 条、1.2 秒后恢复时文件已空"。
            try:
                disk, why = load_queue_detail(trials=3, pause=0.1)
            except Exception:
                disk, why = [], "error"
            if disk:
                LOG.warning("[记忆] 拒绝用空队列覆盖任务库（盘上还有 %d 条·%s）——"
                            "本轮不写，等恢复之后再说" % (len(disk), why or "ok"))
                return
            self._qmem_loaded = True          # 盘上确实空的，正常首次使用
        if len(rows) > QMEM_MAX:      # 封顶：先扔最旧的已完成/失败，再按时间扔
            rows.sort(key=lambda r: ((r.get("status") not in ("complete", "error")),
                                     float(r.get("added_ts") or 0)))
            rows = rows[len(rows) - QMEM_MAX:]
            rows.sort(key=lambda r: float(r.get("added_ts") or 0))
        if save_queue(rows):
            self._qmem_dirty = False
            self._qmem_last = now

    def _qmem_restore(self):
        """开机把 queue.json 读回队列视图：只恢复显示，不自动开跑（红线：不重放）。

        ⚠ 一律留日志痕：这函数在启动的 root.after 里跑，异常会被 Tk 吞掉、0 条时以前
        直接 return → 真机上"文件里有 3 条、界面上什么都没有"根本没法归因（2026-11-05 踩过）。
        """
        try:
            rows, reason = load_queue_detail()
        except Exception as e:
            LOG.exception("读任务库失败")
            self.log("[记忆] 读 queue.json 失败：%s（%s）" % (str(e)[:80], QUEUE_FILE))
            return
        if not reason.startswith("unreadable"):
            self._qmem_loaded = True      # 本进程已认领任务库，之后的空写才是"主人真清空了"
        if not rows:
            if reason.startswith("unreadable") and not getattr(self, "_qmem_retried", False):
                # 文件被占用（上一个实例句柄还没放/杀软正在扫）≠ 没有历史：等一等再试一次，
                # 期间绝不写盘覆盖，否则主人的队列就这么没了。
                self._qmem_retried = True
                self.log("[记忆] 任务库此刻读不动（%s），3 秒后再试一次（本轮不写盘）" % reason)
                try:
                    self.root.after(3000, self._qmem_restore)
                except Exception:
                    pass
                return
            self.log("[记忆] 没有可恢复的历史（%s·%s）" % (reason or "空", QUEUE_FILE))
            return
        try:
            hist = load_history()
        except Exception:
            hist = {}
        n_add = n_done = n_wait = n_auto = 0
        row_err = [""]
        with self._rpc_lock:
            for r in rows:
                try:
                    gid = str(r.get("gid") or "")
                    uri = r.get("uri") or ""
                    if not gid or not uri or gid in self._rpc_tasks:
                        continue
                    st = (r.get("status") or "waiting")
                    src = (r.get("src") or "user")
                    name = r.get("name") or ""
                    outdir = r.get("dir") or ""
                    path = r.get("path") or ""
                    t = {
                        "gid": gid, "uri": uri, "name": name, "dir": outdir,
                        "path": path, "referer": r.get("referer") or "",
                        "headers": r.get("headers") or {},
                        "status": st,
                        "totalLength": int(r.get("total") or 0),
                        "downloadedLength": int(r.get("done") or 0),
                        "downloadSpeed": 0,
                        "errorCode": r.get("error_code") or "0",
                        "errorMessage": r.get("error") or "",
                        "start_ts": 0.0, "done_ts": float(r.get("done_ts") or 0.0),
                        "cancel": True, "thread": None,
                        "src": src, "kind": r.get("kind") or "http",
                        "added_ts": float(r.get("added_ts") or 0.0),
                        "note": r.get("note") or "",
                        "_dkey": self._rpc_norm_key(uri),
                    }
                    # 对账完成账本：上次其实已经下完了（只是没来得及写队列），标完成、绝不重下
                    hit = self._rpc_ledger_hit(t["_dkey"], name, outdir) if hist else ""
                    if hit:
                        st = RPC.ST_COMPLETE if RPC else "complete"
                    if st == (RPC.ST_COMPLETE if RPC else "complete"):
                        t["status"] = st
                        if hit:
                            t["path"], t["name"] = hit, os.path.basename(hit)
                            t["downloadedLength"] = t["totalLength"]
                        elif path and not os.path.isfile(path):
                            t["note"] = "文件已不在本机"
                        elif not t.get("note"):
                            t["note"] = ""
                        t["downloadSpeed"] = 0
                        n_done += 1
                    else:
                        # 进行中/排队/暂停/失败：全部压成 paused（=不跑），等主人点「继续」
                        t["status"] = RPC.ST_PAUSED if RPC else "paused"
                        t["downloadSpeed"] = 0
                        if src == "browser":
                            t["note"] = "浏览器自动接管·不恢复"
                            n_auto += 1
                        else:
                            t["note"] = ("链接可能已过期·点继续重试" if st == "error"
                                         else "重启未自动重下·点继续才跑")
                            n_wait += 1
                    self._rpc_tasks[gid] = t
                    self._rpc_order.append(gid)
                    n_add += 1
                except Exception as e:
                    if row_err[0] == "":
                        row_err[0] = repr(e)[:120]
                    continue
        self.log("[记忆] 任务库 %d 条 → 恢复进队列 %d 条（完成 %d／待手动继续 %d／自动接管不恢复 %d，"
                 "跳过 %d%s）｜重启后绝不自动重下，点「继续」才开跑" % (
                     len(rows), n_add, n_done, n_wait, n_auto, len(rows) - n_add,
                     ("，异常=%s" % row_err[0]) if row_err[0] else ""))
        if n_add:
            self._qmem_mark(force=True)

    def rpc_clear_queue(self, done_only=True):
        """清除已完成/全部（webview「清除已完成」「全部清除」按钮的后端）。
        只动队列视图 + queue.json，本地成品文件与 downloaded.json 一概不碰。"""
        done, stopped = (RPC.ST_COMPLETE if RPC else "complete"), (RPC.ST_ERROR if RPC else "error")
        gone = []
        with self._rpc_lock:
            for g in list(self._rpc_order):
                t = self._rpc_tasks.get(g)
                if not t:
                    continue
                st = t.get("status")
                if st and t.get("thread") and t["thread"].is_alive():
                    continue                      # 正在下的不动
                if done_only and st not in (done, stopped):
                    continue
                gone.append(g)
            for g in gone:
                self._rpc_tasks.pop(g, None)
                try:
                    self._rpc_order.remove(g)
                except ValueError:
                    pass
        self._qmem_mark(force=True)
        try:
            self.log("[记忆] 清除队列 %d 条（%s，本地文件不动）" % (
                len(gone), "已完成/失败" if done_only else "全部"))
        except Exception:
            pass
        return len(gone)

    # ================= 本地下载 RPC（aria2-JSON-RPC 兼容后端）=================
    def _rpc_start(self):
        for _n in (getattr(self, "_cfg_migrate_notes", None) or []):
            self.log(_n)
        self._cfg_migrate_notes = []
        if not self.cfg.get("rpc_enable"):
            return
        if RPC is None:
            self.log("[RPC] rpc_server 模块缺失，本地下载服务未启动")
            return
        if getattr(self, "_rpc_srv", None) is not None:
            return
        try:
            base_port = int(self.cfg.get("rpc_port") or 6800)
        except Exception:
            base_port = 6800
        token = str(self.cfg.get("rpc_token") or "")
        srv = None
        bound = 0
        for p in [base_port] + [base_port + i for i in range(1, 8)]:
            try:
                s = RPC.RpcServer(self, host="127.0.0.1", port=p, token=token)
                bound = s.start()
                srv = s
                break
            except OSError:
                continue
            except Exception as e:
                self.log("[RPC] 端口 %d 启动失败：%s" % (p, str(e)[:120]))
                continue
        if srv is None:
            self.log("[RPC] 本地下载服务启动失败（端口全被占用？）")
            return
        self._rpc_srv = srv
        self.log("[RPC] 本地下载服务已就绪 → http://127.0.0.1:%d/jsonrpc%s"
                 % (bound, "（已启用 token 鉴权）" if token else "（无 token，仅本机）"))
        if bound != base_port:
            # 【"分发成功但没下"根因之三】端口被别的 aria2（Motrix/独立 aria2c）占着时，我们悄悄挪到
            # 6801…，而脚本还在推 6800 → 推给了别人，脚本显示成功、SniffDL 什么都没收到。必须大声说。
            self.log("[RPC] ⚠ 端口 %d 已被别的程序占用（多半是另一个 aria2 / Motrix），SniffDL 改用 %d。"
                     "油猴脚本/外部推送请把 RPC 端口改成 %d，否则任务会推给那个程序而不是 SniffDL"
                     % (base_port, bound, bound))
        # 可选：额外监听 Motrix 默认端口 16800 等（config.json 的 rpc_extra_ports，默认不开——
        # 开了会占住 16800，导致 Motrix 自己起不来）。LinkSwift / 网盘直链下载助手 出厂默认推 16800。
        self._rpc_extra = []
        for ep in (self.cfg.get("rpc_extra_ports") or []):
            try:
                ep = int(ep)
                if ep == bound:
                    continue
                s2 = RPC.RpcServer(self, host="127.0.0.1", port=ep, token=token)
                s2.start()
                self._rpc_extra.append(s2)
                self.log("[RPC] 额外监听 http://127.0.0.1:%d/jsonrpc（兼容脚本默认端口）" % ep)
            except Exception as e:
                self.log("[RPC] 额外端口 %s 监听失败（被占用？）：%s" % (ep, str(e)[:80]))

    def _rpc_stop(self):
        try:
            if getattr(self, "_rpc_srv", None) is not None:
                self._rpc_srv.stop()
                self._rpc_srv = None
        except Exception:
            pass
        for s2 in (getattr(self, "_rpc_extra", None) or []):
            try:
                s2.stop()
            except Exception:
                pass
        self._rpc_extra = []

    def _rpc_apply(self):
        """设置面板「应用」：落盘配置 + 即时重启/关闭本地 RPC 服务。"""
        try:
            en = bool(self.rpc_enable_var.get())
            try:
                port = int(str(self.rpc_port_var.get()).strip() or 6800)
                if not (1 <= port <= 65535):
                    port = 6800
            except Exception:
                port = 6800
            token = str(self.rpc_token_var.get()).strip()
            self.cfg["rpc_enable"] = en
            self.cfg["rpc_port"] = port
            self.cfg["rpc_token"] = token
            save_config(self.cfg)
            self._rpc_stop()
            if en:
                self._rpc_start()
            self._rpc_render_status()
        except Exception as e:
            try:
                self.log("[RPC] 应用设置失败：%s" % str(e)[:120])
            except Exception:
                pass

    def _rpc_render_status(self):
        lbl = getattr(self, "rpc_stat_lbl", None)
        if lbl is None:
            return
        try:
            if not self.cfg.get("rpc_enable"):
                lbl.config(text="未启用", fg=C["ink_l"])
            elif getattr(self, "_rpc_srv", None) is not None:
                lbl.config(text="● 运行中 · 推送地址 http://127.0.0.1:%d/jsonrpc%s" % (
                    self._rpc_srv.bound_port, "" if self.cfg.get("rpc_token") else "（token 留空）"),
                           fg="#2E9E5B")
            else:
                lbl.config(text="○ 未运行", fg=C["ink_l"])
        except Exception:
            pass

    def _rpc_parse_headers(self, options):
        """把 aria2 的 header 选项解析成 dict。
        推送方（LinkSwift / 网盘直链下载助手 / Motrix 插件…）给什么就是什么：header[] 之外，
        aria2 的独立选项 `user-agent` / `referer` 也要认（LinkSwift 两处都给；只给选项的脚本以前会被
        当成"没 UA"，换成程序的 Chrome UA → 百度/115 这类 UA 绑定的直链必 403）。"""
        if ND is not None:
            try:
                return ND.headers_from_aria2_options(options)
            except Exception:
                pass
        h = {}
        raw = (options or {}).get("header")
        if isinstance(raw, dict):
            for k, v in raw.items():
                h[str(k)] = str(v)
        elif isinstance(raw, (list, tuple)):
            for item in raw:
                s = str(item)
                if ":" in s:
                    k, v = s.split(":", 1)
                    h[k.strip()] = v.strip()
        return h

    def _rpc_new_task(self, gid, uri, name, outdir, referer, headers, src="user"):
        return {
            "gid": gid, "uri": uri, "name": name, "dir": outdir,
            "path": "", "referer": referer, "headers": headers,
            "status": RPC.ST_WAITING if RPC else "waiting",
            "totalLength": 0, "downloadedLength": 0, "downloadSpeed": 0,
            "errorCode": "0", "errorMessage": "",
            "start_ts": 0.0, "done_ts": 0.0, "cancel": False, "thread": None,
            # ---- 记忆系统字段（落 queue.json，重启后靠这些还原队列）----
            "src": (src or "user"),                 # user=用户主动 / browser=自动接管
            "kind": "bt" if self._rpc_is_bt(uri) else "http",
            "added_ts": time.time(), "note": "",
        }

    # ================= 小夜 · 原生 BT 引擎（aria2c BitTorrent 内核） =================
    # 博采 qBittorrent 增强版思路：自动补一批精选 tracker、DHT+PeX 找 peer、下完即停不空耗上传。
    _BT_TRACKERS = ",".join([
        "udp://tracker.opentrackr.org:1337/announce",
        "udp://open.stealth.si:80/announce",
        "udp://tracker.torrent.eu.org:451/announce",
        "udp://exodus.desync.com:6969/announce",
        "udp://tracker-udp.gbitt.info:80/announce",
        "udp://explodie.org:6969/announce",
        "udp://open.demonii.com:1337/announce",
        "udp://tracker.darkness.services:6969/announce",
    ])

    # 网盘/CDN 下载域：这些域名下的长 hex 路径段是"文件 ID"，不是 BT infohash
    _NOT_TORRENT_HOSTS = ("quark.cn", "baidu.com", "pan.xunlei.com", "123pan.com",
                          "alipan.com", "aliyuncs.com", "myqcloud.com", "cloudflarestorage.com",
                          "drive.quark.cn", "dl-pc-zb", "dl-guest", "yximgs.com", "douyin",
                          "capcutstatic.com", "capcut.com", "microsoft.com", "update.microsoft")

    def _rpc_is_bt(self, uri):
        """识别 BitTorrent 输入（对标 Motrix）：磁力 / .torrent（本地文件、URL 路径、query 参数）
        / 无后缀但末段是 40 位 infohash 的远程种子直链（OSS/CDN 常见）。
        护栏：带文件名线索、带扩展名、或属网盘/CDN 下载域的，一律不当种子
        （夸克直链路径里就有 40 位 hex 段，误判会让任务必然失败）。"""
        u = (uri or "").strip()
        low = u.lower()
        if low.startswith("magnet:") or low.startswith("torrent:"):
            return True
        try:
            from urllib.parse import urlparse, parse_qs
            if low.endswith(".torrent") and os.path.isfile(u):
                return True
            pr = urlparse(low)
            if pr.path.endswith(".torrent"):
                return True
            qv = [v for vals in parse_qs(pr.query, keep_blank_values=True).values() for v in vals]
            for v in qv:
                if v.endswith(".torrent"):
                    return True
            # 有文件名线索 = 明确是"下载某个文件"，不是种子
            if any(k in pr.query.lower() for k in ("filename=", "fname=", "filenames=",
                                                   "name=", "fn=", "file_name=")):
                return False
            last = pr.path.rsplit("/", 1)[-1]
            if "." in last and 1 < len(last.rsplit(".", 1)[-1]) <= 8:
                return False                      # 末段带扩展名 = 普通文件
            host = pr.netloc or ""
            if any(h in host for h in self._NOT_TORRENT_HOSTS):
                return False
            if len(last) == 40 and all(c in "0123456789abcdef" for c in last):
                return True
        except Exception:
            pass
        return False

    def _magnet_display_name(self, uri):
        try:
            from urllib.parse import urlparse, parse_qs, unquote
            dn = (parse_qs(urlparse(uri or "").query).get("dn") or [""])[0]
            return unquote(dn) if dn else ""
        except Exception:
            return ""

    def _bt_datadir(self):
        base = os.path.join(os.path.expanduser("~"), ".snifftdl", "bt")
        try:
            os.makedirs(base, exist_ok=True)
        except Exception:
            pass
        return base

    def _bt_free_port(self, base=17333):
        import socket
        for p in range(base, base + 8):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.bind(("127.0.0.1", p))
                s.close()
                return p
            except OSError:
                try:
                    s.close()
                except Exception:
                    pass
        return 0

    def _bt_session_sanitized(self, inp, datadir):
        """净化 aria2 session：只续种 BT，绝不自动重放下过的 HTTP 直链。

        以前直接把 bt.session 原样喂给 --input-file，而它每 20 秒保存一次全部队列，
        于是"浏览器接管误排进来的那一大堆链接"会在每次开机时被自动重下一遍——
        用户既没点、也看不见。HTTP 条目一律丢弃，只留 magnet:/.torrent（断点续种是正当需求）。"""
        try:
            if not (os.path.isfile(inp) and os.path.getsize(inp) > 0):
                return ""
            with open(inp, "r", encoding="utf-8", errors="replace") as f:
                raw = f.read().splitlines()
            out, keep, drop = [], 0, 0
            i = 0
            while i < len(raw):
                line = raw[i]
                uri = line.strip()
                if not uri:
                    i += 1
                    continue
                block = [line]
                j = i + 1
                while j < len(raw) and raw[j].startswith(" "):   # 缩进行 = 该任务的参数
                    block.append(raw[j])
                    j += 1
                low = uri.lower()
                # 只续 magnet 与本地种子文件；http(s) 种子链接（含下载失败的）不续——以前每次开机都重试一遍（11-07 记录未修）
                is_bt = low.startswith("magnet:") or (
                    low.endswith(".torrent") and not low.startswith(("http://", "https://", "ftp://"))
                    and os.path.isfile(uri))
                if is_bt:
                    out.extend(block)
                    out.append("")
                    keep += 1
                else:
                    drop += 1
                i = j
            dst = os.path.join(datadir, "bt.resume.session")
            if not out:
                try:
                    if os.path.isfile(dst):
                        os.remove(dst)
                except OSError:
                    pass
                if drop:
                    self.log("[小夜·BT] 历史队列里的 %d 条普通链接不会自动重下（只续种 BT）" % drop)
                return ""
            with open(dst, "w", encoding="utf-8") as f:
                f.write("\n".join(out))
            self.log("[小夜·BT] 续种 %d 个；丢弃历史队列中 %d 条普通下载（不自动重下）" % (keep, drop))
            return dst
        except Exception:
            return ""

    def _bt_pids_named(self, exe):
        """按进程名列 PID（Windows）。"""
        try:
            import subprocess as _sp
            r = _sp.run(["tasklist", "/FI", "IMAGENAME eq %s" % exe, "/FO", "CSV", "/NH"],
                        capture_output=True, text=True, timeout=15)
            out = []
            for ln in (r.stdout or "").splitlines():
                parts = [x.strip('"') for x in ln.split('","')]
                if len(parts) >= 2 and parts[1].isdigit():
                    out.append(int(parts[1]))
            return out
        except Exception:
            return []

    def _bt_orphan_pids(self, datadir):
        """列出"命令行带我们数据目录、且已经没有主人"的 aria2c PID。
        这类孤儿会攥着旧队列在后台继续下载，主程序都没了还在跑，用户完全看不见。
        B9：以前只认进程名 SniffDL.exe，v3 内核是 python.exe、宿主是 electron.exe → 两内核并存会互杀 BT aria2c。
        现在按 aria2c 自己命令行里的 --stop-with-process=<pid> 判主人死活；没有这个参数的老 aria2c 再看
        runtime.json 的内核 pid 和含 sniffdl_core.py / video_downloader.py / SniffDL.exe 的进程（见 find_orphan_aria2c）。
        匹配串取数据目录上一级（.snifftdl），不再用 "bt"（会误杀 Motrix 那种带 --bt-* 参数的 aria2c）。"""
        if os.environ.get("SNIFFDL_REAP_ORPHANS", "1") == "0":
            return []
        try:
            rt_pid = 0
            try:
                rp = getattr(self, "runtime_path", None) or os.path.join(BASE_DIR, "runtime.json")
                with open(rp, encoding="utf-8") as f:
                    rt_pid = int((json.load(f) or {}).get("pid") or 0)
            except Exception:
                rt_pid = 0
            try:
                from core.runtime import _alive
            except Exception:
                def _alive(pid):
                    try:
                        os.kill(pid, 0)
                        return True
                    except Exception:
                        return False
            mine = [getattr((getattr(self, k, None) or {}).get("proc"), "pid", None) for k in ("_bt", "_h2")]
            return [pid for pid, _why in find_orphan_aria2c(datadir, list_processes(), _alive, rt_pid,
                                                             [x for x in mine if x])]
        except Exception:
            return []

    def _bt_reap_orphans(self, datadir):
        """收掉遗留的孤儿 aria2c，返回被收掉的 PID 列表。"""
        gone = []
        for pid in self._bt_orphan_pids(datadir):
            try:
                if os.name == "nt":
                    import subprocess as _sp
                    _sp.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=15)
                else:
                    import signal as _sig
                    os.kill(pid, _sig.SIGKILL)
                gone.append(pid)
                self.log("[小夜·BT] 收掉遗留的 aria2c 孤儿进程 PID %s（它可能正在偷跑旧队列）" % pid)
            except Exception:
                pass
        return gone

    def _bt_ensure(self):
        """确保共享 BT 守护进程在跑，返回其 RPC 端口；无 aria2c 时抛 RuntimeError。"""
        with self._bt_lock:
            proc = self._bt.get("proc")
            port = self._bt.get("port") or 0
            if proc is not None and proc.poll() is None and port:
                return port
            a2 = resolve_aria2c(self.cfg) or ARIA2C
            if not a2 or not os.path.isfile(a2):
                raise RuntimeError("小夜的 BT 引擎需要 aria2c（未找到，请在设置里指定 aria2c 路径）")
            d = self._bt_datadir()
            port = self._bt_free_port()
            if not port:
                raise RuntimeError("小夜找不到可用端口启动 BT 引擎")
            outdir = self.cfg.get("outdir") or DEFAULT_OUTDIR
            cmd = [a2,
                   "--enable-rpc=true", "--rpc-listen-all=false", "--rpc-listen-port=%d" % port,
                   "--rpc-save-upload-metadata=true",
                   "--enable-dht=true", "--enable-dht6=true",
                   "--bt-enable-lpd=true", "--enable-peer-exchange=true",
                   "--bt-tracker=%s" % (self.cfg.get("bt_trackers") or self._BT_TRACKERS),
                   "--bt-max-peers=200", "--bt-request-peer-speed-limit=100K",
                   "--seed-time=0", "--seed-ratio=0.0",
                   "--file-allocation=none", "--check-integrity=true", "--continue=true",
                   "--max-concurrent-downloads=5",
                   # 主程序被强杀/崩溃时 BT 守护进程跟着退（直链引擎 _h2 早就有这条）。
                   # 以前只靠下次开机 _bt_reap_orphans 补收，期间孤儿攥着队列在后台继续下（11-07 实测 kill -9 后残留）。
                   "--stop-with-process=%d" % os.getpid(),
                   "--dht-file-path=%s" % os.path.join(d, "dht.dat"),
                   "--dht-file-path6=%s" % os.path.join(d, "dht6.dat"),
                   "--save-session=%s" % os.path.join(d, "bt.session"),
                   "--save-session-interval=20",
                   "--dir=%s" % outdir,
                   "--summary-interval=0", "--console-log-level=warn", "--log-level=warn",
                   "--log=%s" % os.path.join(d, "aria2.log")]
            try:                              # v3 内核扩展点：DHT/PEX/LSD/端口/元数据缓存等（后出现的同名参数覆盖前面的）
                cmd += [str(x) for x in (self._bt_extra_args() or [])]
            except Exception:
                pass
            inp = os.path.join(d, "bt.session")
            self._bt_reap_orphans(d)          # 先收掉上一世遗留的孤儿 aria2c（会带着旧队列偷跑）
            resume = self._bt_session_sanitized(inp, d)
            if resume:
                cmd.append("--input-file=%s" % resume)
            flags = 0x08000000 if os.name == "nt" else 0
            try:
                proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        creationflags=flags)
            except Exception as e:
                raise RuntimeError("启动小夜 BT 引擎失败：%s" % str(e)[:120])
            self._bt["proc"] = proc
            self._bt["port"] = port
        for _ in range(40):
            try:
                if proc.poll() is not None:
                    raise RuntimeError("小夜 BT 引擎异常退出（见 aria2.log）")
                r = self._bt_rpc("aria2.getVersion", [])
                if "BitTorrent" in json.dumps(r):
                    self.log("[小夜·BT] 引擎就绪 127.0.0.1:%d" % port)
                    return port
            except RuntimeError:
                raise
            except Exception:
                time.sleep(0.25)
        raise RuntimeError("小夜 BT 引擎启动超时")

    def _bt_extra_args(self):
        """BT 守护进程额外启动参数（Tk 版无；v3 内核 Engine 按设置覆盖）。"""
        return []

    def _bt_rpc(self, method, params):
        import urllib.request
        port = self._bt.get("port") or 0
        if not port:
            raise RuntimeError("BT 引擎未运行")
        with self._bt_lock:
            self._bt["rpc_id"] = self._bt.get("rpc_id", 0) + 1
            rid = self._bt["rpc_id"]
        payload = json.dumps({"jsonrpc": "2.0", "id": str(rid),
                              "method": method, "params": params}).encode()
        req = urllib.request.Request("http://127.0.0.1:%d/jsonrpc" % port,
                                     data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                obj = json.loads(resp.read().decode("utf-8", "ignore"))
        except urllib.error.HTTPError as he:
            # aria2 的 RPC 出错时回 HTTP 400 + JSON 错误体；不读出来，用户只看到"HTTP Error 400: Bad Request"
            # （11-07 实测：坏种子文件提示的就是这句，真正原因"Bad torrent"被吞了）
            try:
                obj = json.loads(he.read().decode("utf-8", "ignore"))
            except Exception:
                raise he
        if isinstance(obj, dict) and obj.get("error"):
            raise RuntimeError((obj.get("error") or {}).get("message", "aria2 RPC 错误"))
        return (obj or {}).get("result")

    def _bt_hard_remove(self, t):
        """取消并彻底删除 BT 下载：停任务、移除结果、删掉已下文件与 .aria2 控制文件。"""
        g = t.get("btGid")
        if not g:
            return
        paths = []
        try:
            st = self._bt_rpc("aria2.tellStatus", [g, ["files"]]) or {}
            for f in (st.get("files") or []):
                if f.get("path"):
                    paths.append(f["path"])
        except Exception:
            pass
        try:
            self._bt_rpc("aria2.forceRemove", [g])
        except Exception:
            try:
                self._bt_rpc("aria2.forcePause", [g])
            except Exception:
                pass
        try:
            self._bt_rpc("aria2.removeDownloadResult", [g])
        except Exception:
            pass
        for p in paths:
            for cand in (p, p + ".aria2"):
                try:
                    if os.path.isfile(cand):
                        os.remove(cand)
                except Exception:
                    pass
        # 多文件种子删完文件会剩一棵空文件夹树（11-07 BT 实测记录未修）→ 自底向上删空目录，止于保存目录
        try:
            base = os.path.abspath(t.get("dir") or "")
            dirs = sorted({os.path.dirname(os.path.abspath(p)) for p in paths}, key=len, reverse=True)
            for d0 in dirs:
                d1 = d0
                while base and d1.startswith(base + os.sep) and os.path.isdir(d1) and not os.listdir(d1):
                    os.rmdir(d1)
                    d1 = os.path.dirname(d1)
        except Exception:
            pass
        self._bt_drop_meta(t)

    def _bt_pause_daemon(self, gid):
        try:
            self._bt_rpc("aria2.pause", [gid])
        except Exception:
            pass

    def _bt_fetch_torrent(self, url, headers):
        """抓远程 .torrent 字节（带 Cookie/Referer）。仅当内容确为 bencode 种子（以 'd' 开头且含
        announce/info）才返回 bytes，否则返回 None（交回 addUri 走磁力/普通下载）。"""
        try:
            h = dict(headers or {})
            h.setdefault("User-Agent", UA)
            r = SESSION.get(url, headers=h, timeout=30, stream=False, verify=False)
            if getattr(r, "status_code", 0) not in (200, 206):
                return None
            data = r.content or b""
            if data[:1] == b"d" and (b"announce" in data[:256] or b"info" in data[:256]):
                return data
            return None
        except Exception:
            return None

    def _bt_note_meta(self, g, raw, opts):
        """--rpc-save-upload-metadata 会把上传的种子另存成 <下载目录>/<sha1(种子字节)>.torrent
        （续种要靠它），记下路径：任务完成或被删除时顺手清掉，免得用户下载目录里堆一串 hex 名 .torrent。"""
        try:
            d = (opts or {}).get("dir") or (self.cfg.get("outdir") or DEFAULT_OUTDIR)
            self._bt.setdefault("meta", {})[str(g)] = os.path.join(
                d, hashlib.sha1(raw).hexdigest() + ".torrent")
        except Exception:
            pass

    def _bt_drop_meta(self, t):
        p = t.get("btMeta") or ""
        try:
            if p and os.path.isfile(p):
                os.remove(p)
        except Exception:
            pass

    def _bt_find_by_infohash(self, msg, exclude=None):
        """从 aria2 错误「InfoHash <hex> is already registered.」里取 infohash，返回引擎里同种子任务的 gid。"""
        m = re.search(r"\b([0-9a-fA-F]{40})\b", msg or "")
        if not m:
            return ""
        ih = m.group(1).lower()
        try:
            lst = (self._bt_rpc("aria2.tellActive", [["gid", "infoHash"]]) or []) + \
                  (self._bt_rpc("aria2.tellWaiting", [0, 1000, ["gid", "infoHash"]]) or [])
        except Exception:
            return ""
        for x in lst:
            if (x.get("infoHash") or "").lower() == ih and x.get("gid") != exclude:
                return str(x.get("gid"))
        return ""

    def _bt_add(self, uri, opts, headers=None):
        """把磁力/种子交给守护进程，返回 daemon 侧 gid。
        本地 .torrent → addTorrent(base64)；远程 http(s) 种子链接 → 先抓字节再 addTorrent
        （OSS/CDN 那种无 .torrent 后缀的 infohash 直链，addUri 不会当种子，必须自己喂）；
        磁力 / 抓字节失败 → addUri。"""
        u = (uri or "").strip()
        low = u.lower()
        if low.endswith(".torrent") and os.path.isfile(u):
            import base64
            with open(u, "rb") as f:
                _raw = f.read()
            b64 = base64.b64encode(_raw).decode("ascii")
            g = str(self._bt_rpc("aria2.addTorrent", [b64, [], opts]))
            self._bt_note_meta(g, _raw, opts)
            return g
        if low.startswith("http://") or low.startswith("https://"):
            tb = self._bt_fetch_torrent(u, headers)
            if tb:
                import base64
                self.log("[小夜·BT] 远程种子已抓取 %d 字节 → addTorrent" % len(tb))
                g = str(self._bt_rpc(
                    "aria2.addTorrent", [base64.b64encode(tb).decode("ascii"), [], opts]))
                self._bt_note_meta(g, tb, opts)
                return g
        return str(self._bt_rpc("aria2.addUri", [[u], opts]))

    def _bt_run(self, gid, t):
        """小夜 BT 任务驱动：交给 daemon、轮询进度回填任务、响应暂停/取消。"""
        try:
            self._bt_ensure()
        except Exception as e:
            with self._rpc_lock:
                t["status"] = RPC.ST_ERROR
                t["errorCode"] = "1"
                t["errorMessage"] = str(e)[:200]
            self._qmem_mark(force=True)
            self._rpc_fire_mascot(False)
            self.log("[小夜·BT] 引擎不可用：%s" % str(e)[:120])
            return
        uri = t["uri"]
        opts = {"dir": t["dir"] or (self.cfg.get("outdir") or DEFAULT_OUTDIR), "seed-time": "0"}
        try:
            _hdr = dict(dl_headers(uri, t.get("referer") or ""))
        except Exception:
            _hdr = {}
        try:
            _hdr.update(t.get("headers") or {})
        except Exception:
            pass
        try:
            bg = t.get("btGid")
            if not bg:
                bg = str(self._bt_add(uri, opts, headers=_hdr))
                with self._rpc_lock:
                    t["btGid"] = bg
                    t["btMeta"] = (self._bt.get("meta") or {}).get(bg, "")
            else:
                # 续传：旧 gid 还在引擎里（paused/waiting/active）就直接 unpause。
                # 原来这里调 aria2.getDownloadResult —— aria2 根本没有这个方法，必抛异常，
                # 于是每次「继续」都重新 addTorrent 一份，旧的 paused 任务成了僵尸留在引擎里，
                # 删除只删新的那份，僵尸还会被 bt.session 存下来下次开机续上（2026-11-07 BT 实测复现）。
                try:
                    _ost = (self._bt_rpc("aria2.tellStatus", [bg, ["status"]]) or {}).get("status")
                except Exception:
                    _ost = None
                if _ost not in ("active", "waiting", "paused"):
                    bg = str(self._bt_add(uri, opts, headers=_hdr))
                    with self._rpc_lock:
                        t["btGid"] = bg
                        t["btMeta"] = (self._bt.get("meta") or {}).get(bg, "") or t.get("btMeta", "")
                try:
                    self._bt_rpc("aria2.unpause", [bg])
                except Exception:
                    pass
        except Exception as e:
            with self._rpc_lock:
                t["status"] = RPC.ST_ERROR
                t["errorCode"] = "1"
                t["errorMessage"] = str(e)[:200]
            self._rpc_fire_mascot(False)
            self.log("[小夜·BT] 加入失败：%s" % str(e)[:120])
            return
        self.log("[小夜·BT] 接管任务 %s ← %s" % (gid[:6], (uri or "")[:70]))
        t.pop("_meta_t0", None)          # 每次开跑重新计元数据等待时间
        KEYS = ["status", "totalLength", "completedLength", "downloadSpeed", "uploadLength",
                "connections", "numSeed", "statusMessage", "files", "errorDescription",
                "followedBy", "errorMessage", "errorCode", "bittorrent", "dir"]   # aria2 真实错误字段是 errorMessage/errorCode
        while True:
            if t.get("purge") or t.get("bt_stop"):
                self._bt_hard_remove(t)
                with self._rpc_lock:
                    t["status"] = RPC.ST_PAUSED
                    t["downloadSpeed"] = 0
                self._qmem_mark(force=True)
                self._pet_idle_if_quiet()
                self.log("[小夜·BT] 已取消并删除 %s" % gid[:6])
                return
            if t.get("cancel") or t.get("bt_pause"):
                self._bt_pause_daemon(t.get("btGid"))
                with self._rpc_lock:
                    t["status"] = RPC.ST_PAUSED
                    t["downloadSpeed"] = 0
                self._qmem_mark(force=True)
                self._pet_idle_if_quiet()
                self.log("[小夜·BT] 已暂停 %s（真停，可续传）" % gid[:6])
                return
            try:
                st = self._bt_rpc("aria2.tellStatus", [t["btGid"], KEYS]) or {}
            except Exception as e:
                with self._rpc_lock:
                    if t["status"] == RPC.ST_ACTIVE:
                        t["status"] = RPC.ST_ERROR
                        t["errorCode"] = "1"
                        t["errorMessage"] = "BT 任务已丢失（引擎可能被重启）：%s" % str(e)[:80]
                self._qmem_mark(force=True)
                self.log("[小夜·BT] 查询失败 %s：%s" % (gid[:6], str(e)[:80]))
                return
            status = st.get("status") or "active"
            total = int(st.get("totalLength") or 0)
            done = int(st.get("completedLength") or 0)
            # 磁力链接：aria2 先用一个 [METADATA] gid 取种子元数据，取完它就 complete，
            # 真正的下载在 followedBy 指向的新 gid 里。不跟过去就会"3 秒假完成"、名字成 [METADATA]…，
            # 而真文件在后台偷偷下、暂停/删除都管不到它（2026-11-07 BT 实测复现）。
            _fb = st.get("followedBy") or []
            if status == "complete" and _fb:
                try:
                    _hk = getattr(self, "_bt_on_followed", None)      # 子类钩子：元数据到手、真下载还没开始时改选文件
                    if _hk:
                        _hk(t, str(_fb[0]))
                except Exception as _e:
                    self.log("[小夜·BT] 元数据后处理失败（忽略）：%s" % str(_e)[:80])
                with self._rpc_lock:
                    t["btGid"] = str(_fb[0])
                    t["_meta_followed"] = True
                self._qmem_mark(force=True)
                self.log("[小夜·BT] 任务 %s 元数据已取到 → 跟进真实下载 %s" % (gid[:6], str(_fb[0])[:6]))
                continue
            fpath = ""
            for f in (st.get("files") or []):
                if f.get("path"):
                    fpath = f["path"]
                    break
            if not t.get("_meta_followed") and (os.path.basename(fpath).startswith("[METADATA]") or (
                    (t.get("uri") or "").lower().startswith("magnet:") and not fpath and not total)):
                fpath = ""                       # 元数据阶段的占位路径，不拿来当文件名
                # 无人做种的磁力：以前无限等元数据、界面毫无提示（11-07 BT 实测记录未修）
                _mt0 = t.setdefault("_meta_t0", time.time())
                _waited = int(time.time() - _mt0)
                _lim = int(self.cfg.get("bt_meta_timeout") or 300)
                with self._rpc_lock:
                    t["statusMessage"] = "正在找人要种子信息（已等 %d 秒，连上 %s 个节点）" % (
                        _waited, st.get("connections") or 0)
                if _lim > 0 and _waited >= _lim and not int(st.get("connections") or 0) and not done:
                    try:
                        self._bt_rpc("aria2.forceRemove", [t.get("btGid")])
                    except Exception:
                        pass
                    with self._rpc_lock:
                        t["btGid"] = ""
                        t["status"] = RPC.ST_ERROR
                        t["errorCode"] = "1"
                        t["errorMessage"] = ("无人做种：%d 秒内没有任何节点提供种子信息（磁力元数据）。"
                                             "可能这个资源已没人分享，或网络屏蔽了 DHT——稍后重试或换个种子" % _waited)
                    self._qmem_mark(force=True)
                    self._rpc_fire_mascot(False)
                    self.log("[小夜·BT] 任务 %s ✗ 元数据超时（无人做种）" % gid[:6])
                    return
                time.sleep(1.0)
                continue
            # 多文件种子：任务名/路径用种子名（= 顶层文件夹），别拿第一个文件（常是字幕 .srt）冒充
            _shown = ""
            try:
                _tn = (((st.get("bittorrent") or {}).get("info") or {}).get("name") or "")
                if fpath and _tn and len(st.get("files") or []) > 1:
                    _shown = _tn
                    fpath = os.path.join(st.get("dir") or os.path.dirname(fpath), _tn)
            except Exception:
                _shown = ""
            with self._rpc_lock:
                t["status"] = RPC.ST_ACTIVE
                t["totalLength"] = total
                t["downloadedLength"] = done
                t["downloadSpeed"] = int(st.get("downloadSpeed") or 0)
                t["uploadLength"] = int(st.get("uploadLength") or 0)
                t["numSeeders"] = int(st.get("numSeed") or 0)
                t["connections"] = int(st.get("connections") or 0)
                t["statusMessage"] = st.get("statusMessage") or ""
                if fpath:
                    t["path"] = fpath
                    t["name"] = _shown or os.path.basename(fpath)
            self._qmem_mark()
            if status == "complete":
                with self._rpc_lock:
                    t["status"] = RPC.ST_COMPLETE
                    if total:
                        t["downloadedLength"] = total
                    t["downloadSpeed"] = 0
                    t["done_ts"] = time.time()
                self._qmem_mark(force=True)
                self._bt_drop_meta(t)
                self._rpc_fire_mascot(True)
                try:
                    # BT 完成同样进账本 + 计入熔断流量；多文件种子逐个文件记（fpath 此时是文件夹）
                    for _fp in ([f.get("path") for f in (st.get("files") or []) if f.get("path")]
                                or [fpath]):
                        self._rx_account(_fp)
                except Exception:
                    pass
                self.log("[小夜·BT] 任务 %s ✓ 完成 %s" % (gid[:6], (t.get("name") or "")[:50]))
                return
            if status == "removed":
                st = dict(st, errorMessage="BT 任务在引擎里已被移除（外部删除或引擎重启）")
                status = "error"
            if status == "error" and str(st.get("errorCode") or "") == "12":
                # 同一个种子引擎里已经有了（典型：重启后 bt.session 已把它续上，记忆系统恢复的任务
                # 又点「继续」重新加了一份）→ 接管引擎里那份，别报失败、也别让那份在后台没人管地跑
                _adopt = self._bt_find_by_infohash(st.get("errorMessage") or "", t.get("btGid"))
                _emsg = st.get("errorMessage") or ""
                if _adopt:
                    # 刚删掉同一个种子、旧任务的线程还没来得及从引擎里摘掉它时再加：别接管一个正在被删的任务，
                    # 等它摘完（最多 4 秒）再重新加一份；等不到它消失才是「引擎里真有一份」→ 接管
                    for _w in range(8):
                        time.sleep(0.5)
                        if t.get("purge") or t.get("bt_stop") or t.get("cancel"):
                            break
                        if not self._bt_find_by_infohash(_emsg, t.get("btGid")):
                            _adopt = ""
                            break
                    if not _adopt and not (t.get("purge") or t.get("bt_stop") or t.get("cancel")):
                        try:
                            self._bt_rpc("aria2.removeDownloadResult", [t.get("btGid")])
                        except Exception:
                            pass
                        try:
                            _ng = str(self._bt_add(uri, opts, headers=_hdr))
                            with self._rpc_lock:
                                t["btGid"] = _ng
                                t["btMeta"] = (self._bt.get("meta") or {}).get(_ng, "")
                                t["errorCode"] = "0"
                                t["errorMessage"] = ""
                            self._qmem_mark(force=True)
                            self.log("[小夜·BT] 任务 %s 刚删的同一种子已摘净 → 重新加入 %s" % (gid[:6], _ng[:6]))
                        except Exception as _e:
                            self.log("[小夜·BT] 重新加入失败：%s" % str(_e)[:100])
                        continue
                if _adopt:
                    try:
                        self._bt_rpc("aria2.removeDownloadResult", [t.get("btGid")])
                    except Exception:
                        pass
                    try:
                        self._bt_rpc("aria2.unpause", [_adopt])
                    except Exception:
                        pass
                    with self._rpc_lock:
                        t["btGid"] = _adopt
                        t["errorCode"] = "0"
                        t["errorMessage"] = ""
                    self._qmem_mark(force=True)
                    self.log("[小夜·BT] 任务 %s 引擎里已有同一种子 → 接管 %s" % (gid[:6], _adopt[:6]))
                    continue
            if status == "error":
                msg = (st.get("errorMessage") or st.get("errorDescription")
                       or st.get("statusMessage") or "BT 下载失败")
                if st.get("errorCode") and st.get("errorCode") != "0":
                    msg = "%s（aria2 错误码 %s）" % (msg, st.get("errorCode"))
                with self._rpc_lock:
                    t["status"] = RPC.ST_ERROR
                    t["errorCode"] = "1"
                    t["errorMessage"] = str(msg)[:200]
                self._qmem_mark(force=True)
                self._rpc_fire_mascot(False)
                self.log("[小夜·BT] 任务 %s ✗ 失败：%s" % (gid[:6], str(msg)[:80]))
                return
            time.sleep(1.0)

    def _bt_shutdown(self):
        # 直链专用 HTTP 引擎一并关掉（它也有 --stop-with-process 兜底，主进程被强杀也会自己退）
        try:
            _h2p = (getattr(self, "_h2", None) or {}).get("proc")
            if _h2p is not None and _h2p.poll() is None:
                try:
                    self._h2_rpc("aria2.shutdown", [])
                except Exception:
                    pass
                try:
                    _h2p.wait(timeout=3)
                except Exception:
                    _h2p.kill()
        except Exception:
            pass
        with self._bt_lock:
            proc = self._bt.get("proc")
            port = self._bt.get("port")
        if proc is None:
            return
        try:
            if port and proc.poll() is None:
                self._bt_rpc("aria2.saveSession", [])
                self._bt_rpc("aria2.shutdown", [])
        except Exception:
            pass
        try:
            proc.wait(timeout=4)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        self._bt["proc"] = None

    # ================= 小夜 · BT 种子分区（桌面可视化入口） =================
    def open_bt_panel(self):
        win = getattr(self, "_bt_win", None)
        if win is not None:
            try:
                if win.winfo_exists():
                    win.lift(); win.focus_force(); return
            except Exception:
                pass
        P = self.P
        win = tk.Toplevel(self.root)
        self._bt_win = win
        try:
            win.title("小夜 · BT 种子下载")
        except Exception:
            pass
        win.geometry("%dx%d" % (P(720), P(430)))
        win.configure(bg=C["bg"])
        try:
            win.transient(self.root)
        except Exception:
            pass
        head = tk.Frame(win, bg=C["card"], padx=P(12), pady=P(10))
        head.pack(fill="x")
        tk.Label(head, text="♪ 小夜的种子库（内置 aria2c · DHT+PeX+tracker）",
                 bg=C["card"], fg=C["plum"], font=F_UI).pack(side="left")
        RoundButton(head, text="＋ 添加种子/磁力", command=self.add_bt,
                    padx=P(12), pady=P(4)).pack(side="right")
        cols = ("name", "status", "pct", "size", "speed", "seed")
        spec = [("名称", 300, "w", "name"), ("状态", 84, "center", "status"),
                ("进度", 56, "center", "pct"), ("大小", 140, "center", "size"),
                ("速度", 92, "center", "speed"), ("做种/连接", 84, "center", "seed")]
        wrap = tk.Frame(win, bg=C["card"])
        wrap.pack(fill="both", expand=True, padx=P(10), pady=P(2))
        tree = ttk.Treeview(wrap, columns=cols, show="headings", height=12)
        for text, w, anchor, c in spec:
            tree.heading(c, text=text)
            tree.column(c, width=P(w), anchor=anchor, stretch=(c == "name"))
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self._bt_tree = tree
        self._bt_gids = {}
        act = tk.Frame(win, bg=C["card"], padx=P(12), pady=P(10))
        act.pack(fill="x")
        tk.Label(act, text="选中一行后操作　（暂停＝真停可续传；删除＝连同已下文件一并清除）",
                 bg=C["card"], fg=C["ink_l"], font=F_SM).pack(side="left")
        RoundButton(act, text="打开保存目录", kind="ghost", command=self.bt_open_dir,
                    padx=P(10), pady=P(4)).pack(side="right")
        RoundButton(act, text="✕ 删除", kind="ghost", command=self.bt_remove_sel,
                    padx=P(10), pady=P(4)).pack(side="right", padx=(0, P(6)))
        RoundButton(act, text="▶ 继续", kind="ghost", command=self.bt_resume_sel,
                    padx=P(10), pady=P(4)).pack(side="right", padx=(0, P(6)))
        RoundButton(act, text="⏸ 暂停", kind="ghost", command=self.bt_pause_sel,
                    padx=P(10), pady=P(4)).pack(side="right", padx=(0, P(6)))
        self._bt_refresh()

    def _bt_tasks(self):
        out = []
        try:
            with self._rpc_lock:
                for gid in self._rpc_order:
                    t = self._rpc_tasks.get(gid)
                    if t and (t.get("btGid") or self._rpc_is_bt(t.get("uri") or "")):
                        out.append(dict(t))
        except Exception:
            pass
        return out

    def _bt_refresh(self):
        win = getattr(self, "_bt_win", None)
        if win is None:
            return
        try:
            if not win.winfo_exists():
                self._bt_win = None
                return
        except Exception:
            self._bt_win = None
            return
        tree = getattr(self, "_bt_tree", None)
        if tree is None:
            return
        seen = set()
        for t in self._bt_tasks():
            gid = str(t.get("gid"))
            iid = self._bt_gids.get(gid)
            if iid is None or not tree.exists(iid):
                iid = tree.insert("", "end", values=("",) * 6)
                self._bt_gids[gid] = iid
            seen.add(iid)
            total = int(t.get("totalLength") or 0)
            done = int(t.get("downloadedLength") or 0)
            spd = int(t.get("downloadSpeed") or 0)
            st = t.get("status")
            stxt = {RPC.ST_ACTIVE: "下载中", RPC.ST_WAITING: "排队中",
                    RPC.ST_PAUSED: "已暂停", RPC.ST_COMPLETE: "✓ 完成",
                    RPC.ST_ERROR: "✗ 失败"}.get(st, st or "")
            if st == RPC.ST_ERROR and (t.get("errorMessage") or ""):
                stxt = "✗ " + str(t.get("errorMessage"))[:14]
            if total:
                pct = "%d%%" % (done * 100 // total)
                size = "%s / %s" % (fmt_size(done), fmt_size(total))
            elif st == RPC.ST_COMPLETE:
                pct, size = "100%", fmt_size(done)
            else:
                pct, size = "—", (fmt_size(done) if done else "获取元数据…")
            speed = (fmt_size(spd) + "/s") if spd else ("" if st == RPC.ST_ACTIVE else "—")
            seed = "%s / %s" % (t.get("numSeeders") or 0, t.get("connections") or 0)
            name = t.get("name") or (t.get("uri") or "")[:44]
            tree.item(iid, values=(name, stxt, pct, size, speed, seed))
        for gid, iid in list(self._bt_gids.items()):
            if iid not in seen:
                try:
                    tree.delete(iid)
                except Exception:
                    pass
                self._bt_gids.pop(gid, None)
        try:
            win.after(1000, self._bt_refresh)
        except Exception:
            pass

    def add_bt(self):
        P = self.P
        parent = getattr(self, "_bt_win", None) or self.root
        dlg = tk.Toplevel(parent)
        try:
            dlg.title("添加种子 / 磁力链接")
        except Exception:
            pass
        dlg.transient(parent)
        dlg.configure(bg=C["card"])
        dlg.resizable(False, False)
        body = tk.Frame(dlg, bg=C["card"], padx=P(16), pady=P(14))
        body.pack(fill="both", expand=True)
        tk.Label(body, text="粘贴磁力链接（magnet:?…）：", bg=C["card"],
                 fg=C["ink"], font=F_UI, anchor="w").pack(fill="x")
        ent = tk.Text(body, height=3, width=P(58), wrap="word",
                      bg=(_tint(self.theme, 0) or C["card"]), fg=C["ink"],
                      insertbackground=C["ink"],
                      relief="flat", font=F_UI, padx=4, pady=4)
        try:
            u = self.url_var.get().strip()
            if u.lower().startswith("magnet:"):
                ent.insert("1.0", u)
        except Exception:
            pass
        ent.pack(fill="x", pady=(P(2), P(10)))
        rowf = tk.Frame(body, bg=C["card"])
        rowf.pack(fill="x")
        tk.Label(rowf, text="或选择 .torrent 种子：", bg=C["card"],
                 fg=C["ink"], font=F_UI).pack(side="left")
        path_var = tk.StringVar()
        ttk.Entry(rowf, textvariable=path_var, style="Maid.TEntry").pack(
            side="left", fill="x", expand=True, padx=(P(6), P(6)))

        def browse():
            p = filedialog.askopenfilename(
                title="选择种子文件", parent=dlg,
                filetypes=[("Torrent 种子", "*.torrent"), ("所有文件", "*.*")])
            if p:
                path_var.set(p)
        RoundButton(rowf, text="浏览…", kind="ghost", command=browse,
                    padx=P(10), pady=P(2)).pack(side="left")

        def go():
            uri = (ent.get("1.0", "end") or "").strip()
            p = path_var.get().strip()
            if uri.lower().startswith("magnet:"):
                self._bt_submit(uri)
            elif p.lower().endswith(".torrent") and os.path.isfile(p):
                self._bt_submit(p)
            else:
                messagebox.showwarning("小夜", "贴一个磁力链接，或选一个 .torrent 种子文件吧～",
                                       parent=dlg)
                return
            try:
                dlg.destroy()
            except Exception:
                pass
            self.open_bt_panel()

        btns = tk.Frame(body, bg=C["card"])
        btns.pack(fill="x", pady=(P(14), 0))
        RoundButton(btns, text="取消", kind="ghost", command=dlg.destroy,
                    padx=P(12), pady=P(6)).pack(side="right", padx=(P(8), 0))
        RoundButton(btns, text="✦ 交给小夜下载", command=go,
                    padx=P(14), pady=P(6)).pack(side="right")
        try:
            dlg.update_idletasks()
            w, h = dlg.winfo_width(), dlg.winfo_height()
            dlg.geometry("+%d+%d" % ((dlg.winfo_screenwidth() - w) // 2,
                                      (dlg.winfo_screenheight() - h) // 2))
        except Exception:
            pass

    def _bt_submit(self, uri):
        try:
            gid = self.rpc_add_uri(uri, {})
            self.log("[小夜·BT] 桌面添加任务 %s ← %s" % (str(gid)[:6], (uri or "")[:70]))
            self.say("种子交给小夜啦，正在找源～")
            return gid
        except Exception as e:
            messagebox.showwarning("小夜", "加入种子失败：%s" % str(e)[:120])
            return ""

    def _bt_sel_gid(self):
        tree = getattr(self, "_bt_tree", None)
        if tree is None:
            return None
        sel = tree.selection()
        if not sel:
            return None
        iid = sel[0]
        for gid, v in self._bt_gids.items():
            if v == iid:
                return gid
        return None

    def bt_pause_sel(self):
        g = self._bt_sel_gid()
        if g:
            self.rpc_pause(g)

    def bt_resume_sel(self):
        g = self._bt_sel_gid()
        if g:
            self.rpc_resume(g)

    def bt_remove_sel(self):
        g = self._bt_sel_gid()
        if not g:
            return
        if messagebox.askyesno("小夜", "删除这个种子任务？已下载的文件也会一并删掉。"):
            self.rpc_remove(g)

    def bt_open_dir(self):
        try:
            d = self.cfg.get("outdir") or DEFAULT_OUTDIR
            os.makedirs(d, exist_ok=True)
            try:
                os.startfile(d)
            except Exception:
                subprocess.Popen(["explorer", d])
        except Exception:
            pass

    _CAT_MAP = {
        "应用程序": ("exe", "msi", "msp", "apk", "ipa", "deb", "rpm", "dmg", "pkg", "appimage", "jar"),
        "压缩文件": ("zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz", "bz"),
        "视频": ("mp4", "mkv", "mov", "avi", "webm", "flv", "wmv", "m4v", "ts", "mpg", "mpeg", "3gp"),
        "音频": ("mp3", "flac", "wav", "m4a", "aac", "ogg", "opus", "wma"),
        "文档": ("pdf", "epub", "mobi", "azw3", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
                 "txt", "md", "csv", "json", "xml"),
        "图片": ("png", "jpg", "jpeg", "gif", "webp", "bmp", "svg", "ico", "tif", "tiff", "heic"),
    }

    def _category_of(self, name_or_uri):
        try:
            from urllib.parse import urlparse
            s = (name_or_uri or "").strip()
            path = urlparse(s).path if "://" in s else s.split("?")[0]
            if "." not in path:
                return "其他"
            ext = path.rsplit(".", 1)[-1].lower().split("?")[0]
            for cat, exts in self._CAT_MAP.items():
                if ext in exts:
                    return cat
            return "其他"
        except Exception:
            return "其他"

    def _safety_selfcheck(self):
        """开机把每道"防呆"真跑一遍并把状态写进日志。

        教训：这个项目的防重复下载曾经「代码加了但一次都没执行」——只查磁盘拦不住
        同一秒的并发提交，日志里 5895 行 0 次触发；收孤儿又挂在错误的初始化顺序上
        （self._bt 还没建就被调用，异常被 try 吞掉，静默失效）。所以每道防线都必须
        自报状态：**日志里少一行、或出现 !!，就说明它坏了**，不用再靠猜。"""
        out = []
        try:
            k1 = self._rpc_norm_key("https://x.com/a.zip?sig=1&t=2&fname=a.zip")
            k2 = self._rpc_norm_key("https://x.com/a.zip?sig=9&t=8&fname=a.zip")
            good = (k1 == k2) and ("sig" not in k1)
            self._rpc_gate_ok = good
            out.append("去重闸门=%s" % ("OK" if good else "!!失效"))
        except Exception as e:
            self._rpc_gate_ok = False
            out.append("去重闸门=!!异常 %s" % str(e)[:24])
        try:
            quark = "https://dl-pc-zb.drive.quark.cn/x/1/" + "0" * 40 + "/6a95"
            mag = "magnet:?xt=urn:btih:" + "a" * 40 + "&dn=x"
            good = (not self._rpc_is_bt(quark)) and self._rpc_is_bt(mag)
            out.append("网盘不当种子=%s" % ("OK" if good else "!!失效"))
        except Exception as e:
            out.append("网盘护栏=!!异常")
        try:
            fw = self._rpc_is_junk_autolink(
                "https://go.microsoft.com/fwlink/?linkid=2171764", {})
            htm = self._rpc_is_junk_autolink(
                "https://gcdn.drtuber.com/mp4_hq/ad.htm", {"size": 548})
            mp4 = self._rpc_is_junk_autolink(
                "https://cdn.x/movie.mp4", {"size": 5000000})
            good = bool(fw) and bool(htm) and (not mp4)
            out.append("自动接管杂链过滤=%s" % ("OK" if good else "!!失效"))
        except Exception as e:
            out.append("自动接管杂链过滤=!!异常 %s" % str(e)[:20])
        try:
            d = self._bt_datadir()
            sess = os.path.join(d, "bt.session")
            n_http = 0
            if os.path.isfile(sess):
                for l in open(sess, encoding="utf-8", errors="replace"):
                    if l and not l.startswith(" ") and l.strip().lower().startswith("http"):
                        n_http += 1
            out.append("会话净化=就绪(session内HTTP %d条,起守护进程时丢弃)" % n_http)
        except Exception:
            out.append("会话净化=!!异常")
        try:
            out.append("孤儿回收=后台查")          # 要扫进程，放后台做，别拖慢启动
        except Exception:
            pass
        try:
            h = load_history()
            probe = HISTORY_FILE + ".wtest"
            with open(probe, "w", encoding="utf-8") as f:
                f.write("{}")
            os.remove(probe)
            out.append("下载账本=%d条·可写" % len(h))
        except Exception:
            out.append("下载账本=!!不可写")
        try:
            q, qwhy = load_queue_detail()
            probe = QUEUE_FILE + ".wtest"
            with open(probe, "w", encoding="utf-8") as f:
                f.write('{"ok":1}')
            os.remove(probe)
            out.append("记忆库=%d条(%s)·可写" % (len(q), qwhy or "ok"))
        except Exception:
            out.append("记忆库=!!不可读写")
        out.append("流量保险丝=%s" % ("%.0fGB" % self._rx_cap_gb if self._rx_cap_gb > 0 else "关"))
        try:
            if C24 is not None:
                c = self._c24()
                fr = getattr(c, "FRAMES", {}) or {} if c else {}
                out.append("素材=%s(%d组)" % ("rig" if "gaze_c" in fr else ("legacy" if c else "无"),
                                             len(fr)))
        except Exception:
            pass
        self.log("[自检] " + " | ".join(out))
        # 需要扫进程的检查（PowerShell/tasklist 有几百 ms~秒开销）挪到窗口出来之后再做
        try:
            self.root.after(1500, self._safety_selfcheck_deep)
        except Exception:
            pass
        return out

    def _safety_selfcheck_deep(self):
        """启动后台补做：收孤儿 aria2c 并报告。绝不做在开窗口之前，否则启动被拖慢。"""
        try:
            gone = self._bt_reap_orphans(self._bt_datadir())
            self.log("[自检] 孤儿aria2c=%s" % ("无" if not gone else
                                              "⚠收到%d个(主程序曾被硬杀，已清理)" % len(gone)))
        except Exception:
            self.log("[自检] 孤儿回收=!!异常")

    def _rx_account(self, path):
        """给本次会话的下载量记账；超阀就熔断后续任务，并大声说一声。"""
        try:
            sz = os.path.getsize(path) if path and os.path.isfile(path) else 0
        except Exception:
            sz = 0
        # 记账：这是扩展/RPC 下载唯一的完成出口。原来只有桌面队列的 _finish_row 会
        # record_download，所以从浏览器下的全都不进账本 → 值班室今日/累计/热力图永远 0。
        try:
            if sz > 0:
                record_download(sz)
                self._dash_last_hm = None      # 让热力图下一拍重绘
        except Exception:
            pass
        self._rx_bytes = int(getattr(self, "_rx_bytes", 0)) + sz
        cap = float(getattr(self, "_rx_cap_gb", 8) or 0)
        if cap > 0 and self._rx_bytes > cap * (1024 ** 3) and not getattr(self, "_rx_blocked", False):
            self._rx_blocked = True
            gb = self._rx_bytes / (1024 ** 3)
            self.log("[熔断] 本次会话已下载 %.2fGB，超过上限 %.0fGB —— 后续任务一律拒绝，"
                     "防止再出现没人点却狂下几个G。要放开：设置 rx_cap_gb 调大或填 0 关闭。" % (gb, cap))
            try:
                self.say("今天已经下了 %.1fG 了，咱把下载熔断了——不是故意的话去设置里把上限调大。" % gb)
            except Exception:
                pass
        return self._rx_bytes

    # 网盘/CDN 每次签发都会变的一次性参数：归一化掉才能认出"还是同一个文件"
    _VOLATILE_QS = frozenset((
        "sign", "sig", "signature", "token", "tk", "ak", "sk", "se", "sp", "auth_key",
        "expires", "expire", "etime", "timestamp", "time", "t", "ts", "rand", "random", "r",
        "_t", "_r", "cdn_rand", "downloadid", "did", "udid", "dev_id", "client", "version",
        "channel", "from", "sourcetype", "x-oss-date", "response-content-disposition"))
    # 一整族的临时签名参数前缀（OSS/COS/S3/七牛），一次盖掉
    _VOLATILE_PREFIX = ("x-oss-", "x-cos-", "x-amz-", "oss-", "q-sign", "sign_")

    def _rpc_norm_key(self, uri):
        """URL → 去重键：抹掉一次性签名/时间戳参数，只留能标识"哪个文件"的部分。"""
        try:
            from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
            pr = urlparse((uri or "").strip())
            keep = [(k, v) for k, v in parse_qsl(pr.query, keep_blank_values=True)
                    if k.lower() not in self._VOLATILE_QS
                    and not k.lower().startswith(self._VOLATILE_PREFIX)]
            path = re.sub(r"/{2,}", "/", pr.path or "")
            return urlunparse(((pr.scheme or "https").lower(), (pr.netloc or "").lower(),
                               path, "", urlencode(sorted(keep)), ""))
        except Exception:
            return (uri or "").strip()

    def _rpc_inflight_locked(self, key, path):
        """【必须在持有 self._rpc_lock 时调用】查同一链接/同一落盘路径是否已有任务。"""
        if not key:
            return None
        for g in self._rpc_order:
            t2 = self._rpc_tasks.get(g)
            if not t2 or t2.get("_dkey") != key:
                continue
            st = t2.get("status")
            if st in ("waiting", "active", "paused"):
                return g
            if st == "complete":
                p = t2.get("path") or ""
                # 刚下完且文件还在 → 直接复用；用户已删掉则放行让他重下
                if p and os.path.exists(p) and os.path.getsize(p) > 0:
                    return g
        if path:                               # 不同 URL 撞同一个目标文件，也别并发下两份
            for g in self._rpc_order:
                t2 = self._rpc_tasks.get(g)
                if t2 and t2.get("status") in ("waiting", "active"):
                    if os.path.join(t2.get("dir") or "", safe_name(t2.get("name") or "")) == path:
                        return g
        return None

    def _rpc_inflight(self, key, path):
        if not key:
            return None
        try:
            with self._rpc_lock:
                return self._rpc_inflight_locked(key, path)
        except Exception:
            return None

    def _rpc_ledger_hit(self, key, name, outdir):
        """历史账本（downloaded.json）里这个链接是否曾成功下载、且文件仍在磁盘上。"""
        try:
            rec = load_history().get(key)
            if not isinstance(rec, dict):
                return ""
            p = os.path.join(rec.get("dir") or outdir, rec.get("name") or name or "")
            if p and os.path.isfile(p) and os.path.getsize(p) > 0:
                return p
        except Exception:
            pass
        return ""

    def rpc_add_uri(self, uri, options, defer=False, src=""):
        import uuid, os as _os
        from urllib.parse import urlparse, unquote
        src = (src or "user").strip().lower()
        options = options or {}
        gid = uuid.uuid4().hex[:16]
        # 文件名先定，目录按"是否显式指定"决定是否归类
        name = options.get("out") or ""
        low = (uri or "").lower()
        if not name:
            name = self._rpc_query_filename(uri)
        if not name and low.startswith("magnet:"):
            name = self._magnet_display_name(uri)
        if not name:
            try:
                base = _os.path.basename(urlparse(uri).path)
                name = unquote(base) or gid
            except Exception:
                name = gid
        _explicit_dir = (options.get("dir") or "").strip()
        # 网盘直链下载助手(panlinker) 出厂默认 dir="D:"（裸盘符，不带斜杠）：Windows 上 "D:"+文件名
        # = "D:当前目录\文件名"，文件落到莫名其妙的地方，用户以为"根本没下"。裸盘符一律视为"没指定"。
        if re.fullmatch(r"[A-Za-z]:", _explicit_dir or ""):
            self.log("[RPC] 推送方给的保存目录是裸盘符「%s」（脚本出厂默认值），改用 SniffDL 下载目录"
                     % _explicit_dir)
            _explicit_dir = ""
        if _explicit_dir:
            outdir = _explicit_dir
        else:
            _base = self.cfg.get("outdir") or DEFAULT_OUTDIR
            _cat = self._category_of(name or uri)
            outdir = _os.path.join(_base, _cat) if _cat else _base
        hdrs = self._rpc_parse_headers(options)
        referer = ""
        for k, v in list(hdrs.items()):
            if k.lower() == "referer":
                referer = v
        # 推送方给的多连接参数（split / max-connection-per-server / min-split-size）记下来，
        # 下载时与网盘规则取小（规则是"别被封"的上限，推送方要更少就听推送方的）
        a2hint = {}
        for _k in ("split", "max-connection-per-server", "min-split-size"):
            if options.get(_k):
                a2hint[_k] = str(options.get(_k))
        try:
            _os.makedirs(outdir, exist_ok=True)
        except Exception:
            pass
        if getattr(self, "_rx_blocked", False):
            self.log("[熔断] 拒绝新任务 %s（本次会话已下 %.2fGB 超上限）← %s" % (
                (name or "?")[:40], self._rx_bytes / (1024 ** 3), (uri or "")[:70]))
            with self._rpc_lock:
                t = self._rpc_new_task(gid, uri, name, outdir, referer, hdrs, src=src)
                t["status"] = RPC.ST_ERROR if RPC else "error"
                t["errorCode"] = "15"
                t["errorMessage"] = "流量熔断：本次会话下载量已超上限，需调大 rx_cap_gb 或重启程序"
                self._rpc_tasks[gid] = t
                self._rpc_order.append(gid)
            self._qmem_mark()
            return gid
        # ---- 入口闸门：根治"一次动作被并发复制成 N 份"和"下过的再下一遍" ----
        # 关键：查重复 + 登记任务必须在同一把锁里做完，否则 12 个线程会同时通过检查
        dkey = self._rpc_norm_key(uri)
        target = _os.path.join(outdir, safe_name(name or "")) if name else ""
        hit = ""
        with self._rpc_lock:
            dup = self._rpc_inflight_locked(dkey, target)
            if not dup:
                try:
                    if not hasattr(self, "_hist_cache"):
                        self._hist_cache = load_history()
                    rec = self._hist_cache.get(dkey)
                    if isinstance(rec, dict):
                        p = _os.path.join(rec.get("dir") or outdir,
                                          rec.get("name") or name or "")
                        if p and _os.path.isfile(p) and _os.path.getsize(p) > 0:
                            hit = p
                except Exception:
                    hit = ""
            revived = None
            if dup:
                # 【"分发成功但没下"根因之一，11-06 实测复现】同一文件已有任务时以前一律"忽略重复提交"
                # 并把旧 gid 当成功返回——可旧任务若是「待继续(paused)」（记忆系统重启后全部压成 paused）
                # 或「稍后(waiting 无线程)」，它压根不会动，而新推来的签名链接/请求头被直接扔掉。
                # 现在：旧任务没在跑 → 用这次推送的新链接 + 新请求头把它复活并立刻开下；真在跑才算重复。
                t_old = self._rpc_tasks.get(dup) or {}
                st_old = t_old.get("status")
                th_old = t_old.get("thread")
                running = bool(th_old and th_old.is_alive()) \
                    or (time.time() - float(t_old.get("_rev_ts") or 0) < 3)   # 刚被另一条并发推送复活
                if t_old and st_old in ("paused", "error", "waiting") and not running:
                    t_old["_rev_ts"] = time.time()
                    t_old["uri"] = uri
                    t_old["headers"] = hdrs
                    t_old["referer"] = referer
                    if name:
                        t_old["name"] = name
                    t_old["a2opts"] = a2hint
                    t_old["src"] = src
                    t_old["status"] = RPC.ST_WAITING if RPC else "waiting"
                    t_old["cancel"] = False
                    t_old["purge"] = False
                    t_old["errorCode"] = "0"
                    t_old["errorMessage"] = ""
                    t_old["note"] = ""
                    t_old["statusMessage"] = ""
                    t_old["downloadedLength"] = 0
                    t_old["_dkey"] = dkey
                    revived = dup
                else:
                    self.log("[RPC] 忽略重复提交 %s（同一文件已有任务 %s 状态=%s）← %s" % (
                        (name or "?")[:40], dup[:6], st_old, (uri or "")[:70]))
                    return dup
            if not revived:
                t = self._rpc_new_task(gid, uri, name, outdir, referer, hdrs, src=src)
                t["_dkey"] = dkey
                t["a2opts"] = a2hint
                if hit:
                    self.log("[RPC] 已下载过，零流量跳过：%s（%s）" % (
                        _os.path.basename(hit), (uri or "")[:70]))
                    t["status"] = RPC.ST_COMPLETE if RPC else "complete"
                    t["path"] = hit
                    t["name"] = _os.path.basename(hit)
                    t["skipped"] = "already-downloaded"
                    t["done_ts"] = time.time()
                self._rpc_tasks[gid] = t
                self._rpc_order.append(gid)
        if revived:
            th = threading.Thread(target=self._rpc_run_task, args=(revived,),
                                  name="RpcTask-%s" % revived[:6], daemon=True)
            with self._rpc_lock:
                self._rpc_tasks[revived]["thread"] = th
            self._qmem_mark(force=True)
            th.start()
            self.log("[RPC] 同一文件已有未在跑的任务 %s → 用这次推送的新链接/请求头重新开下 ← %s" % (
                revived[:6], (uri or "")[:80]))
            return revived
        self._qmem_mark(force=True)      # 新任务立刻进任务库（被杀也不丢记忆）
        if hit:
            return gid
        if defer:
            self.log("[RPC] 稍后下载 %s ← %s" % (gid[:6], (uri or "")[:80]))
            return gid
        th = threading.Thread(target=self._rpc_run_task, args=(gid,),
                              name="RpcTask-%s" % gid[:6], daemon=True)
        with self._rpc_lock:
            self._rpc_tasks[gid]["thread"] = th
        th.start()
        self.log("[RPC] 接收任务 %s ← %s" % (gid[:6], (uri or "")[:80]))
        return gid

    def _rpc_purge(self, t):
        """取消下载：删掉本次产生的半成品（隔离分片目录 + .part），已完成的成品保留。
        与 download_one 的 _tkey 用同一 md5(uri)[:12] 定位 .snifftmp 子目录。"""
        try:
            outdir = t.get("dir") or ""
            uri = t.get("uri") or ""
            if outdir and uri:
                key = hashlib.md5(uri.encode("utf-8", "ignore")).hexdigest()[:12]
                tmp = os.path.join(snifftmp_root(outdir), ".snifftmp", key)
                if os.path.isdir(tmp):
                    shutil.rmtree(tmp, ignore_errors=True)
                nm = t.get("name")
                if nm:
                    p = os.path.join(outdir, safe_name(nm) + ".part")
                    if os.path.isfile(p):
                        try:
                            os.remove(p)
                        except Exception:
                            pass
            self.log("[RPC] 已清理取消任务残留：%s"
                     % ((t.get("name") or uri)[:60] or "—"))
        except Exception as e:
            self.log("[RPC] 清理残留异常：%s" % str(e)[:80])

    def _rpc_run_task(self, gid):
        with self._rpc_lock:
            t = self._rpc_tasks.get(gid)
        if not t:
            return
        t["status"] = RPC.ST_ACTIVE
        t["start_ts"] = time.time()
        self._qmem_mark()
        self._pet_react("busy")       # 大肥鱼：举手+头顶加载圈，直到完成/失败

        # 小夜：磁力/种子交给内置 aria2c BitTorrent 引擎原生下载（不转发外部客户端）
        if self._rpc_is_bt(t["uri"]):
            return self._bt_run(gid, t)

        def prog(d):
            try:
                if not isinstance(d, dict):
                    return
                st = d.get("status")
                if st == "downloading":
                    tb = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    db = d.get("downloaded_bytes") or 0
                    fn = (d.get("filename") or "").lower()
                    _aud = (".m4a", ".mp3", ".opus", ".weba", ".aac", ".flac", ".wav", ".mka", ".ogg")
                    import re as _re
                    _is_f = bool(_re.search(r"\.f\d+", fn))
                    if _is_f and fn.endswith(_aud):
                        phase = "下载音频轨…"
                    elif _is_f:
                        phase = "下载视频轨…"
                    else:
                        phase = "下载中…"
                    with self._rpc_lock:
                        t["totalLength"] = int(tb or 0)
                        t["downloadedLength"] = int(db or 0)
                        t["downloadSpeed"] = int(d.get("speed") or 0)
                        t["statusMessage"] = phase
                    self._qmem_mark()     # 只标脏，5 秒最多落一次盘，不刷磁盘
                elif st == "finished":
                    with self._rpc_lock:
                        if t.get("totalLength"):
                            t["downloadedLength"] = t["totalLength"]
                        t["statusMessage"] = "合并封装中…"
            except Exception:
                pass

        def pp_hook(d):
            try:
                s = d.get("status") if isinstance(d, dict) else None
                if s == "started":
                    with self._rpc_lock:
                        t["statusMessage"] = "合并封装中…"
                elif s == "finished":
                    with self._rpc_lock:
                        t["statusMessage"] = "♡ 马上就好"
            except Exception:
                pass

        # 复用现成下载原语：视频/无后缀页走 download_one（内部选 分段/aria2c/yt-dlp）；
        # 带明确文件后缀的直链（GitHub 安装包、网盘直链、任意附件）走精确落名的 requests 下载
        # 合并请求头【大小写不敏感、推送方优先】：以前是 dl_headers(带程序 Chrome UA) 再 update 推送方的头，
        # 推送方若用小写键 "user-agent" / "cookie"，结果两个 UA 同时存在 → aria2 发出双 UA，
        # 网盘按第一个(Chrome)判 → 403。现在推送方给的一个字都不动，只补缺的。
        merged = dict(t["headers"] or {})
        if ND is not None:
            try:
                _prof = ND.match_profile(t["uri"])
                if _prof and _prof.get("default_ua"):
                    ND.hset_default(merged, "User-Agent", _prof["default_ua"])
            except Exception:
                pass
        for _k, _v in dl_headers(t["uri"], t["referer"]).items():
            if _v and not any(str(k).lower() == _k.lower() for k in merged):
                merged[_k] = _v
        outdir = t["dir"]
        quality = self.cfg.get("quality") or "最佳"
        try:
            if self._rpc_is_direct_file(t["uri"], t.get("name")):
                fp = self._rpc_direct_file(t, prog, merged)
            else:
                # 视频/站点解析：绝不把浏览器 UA/Cookie 当 http_headers 传下去——
                # download_one 在 curl_cffi impersonate 下会主动剥掉 UA，若这里再覆盖回去，
                # 就变成"TLS=Chrome / 头 UA 不一致"，B站 WBI playinfo 直接返空格式表
                # → "No video formats found"。Referer 由 download_one 依 page_url 自建；
                # 登录态走 cookiefile/cookiesfrombrowser（下面 how 标成站点解析才会附带）。
                entry = {"url": t["uri"], "title": t["name"], "size": 0,
                         "how": "站点解析", "_page": t["referer"]}
                fp = download_one(entry, t["referer"] or t["uri"], outdir, quality,
                                  prog, extra_opts=None, log=self.log,
                                  pp_hook=pp_hook,
                                  abort=lambda: t.get("cancel", False))
        except (_AbortSignal, SniffAbort):      # SniffAbort：download_one 的 yt-dlp 路径在被中断/外部下载器被杀时抛
            fp = False
            if t.get("purge"):
                self._rpc_purge(t)
            with self._rpc_lock:
                t["status"] = RPC.ST_PAUSED
            self._qmem_mark(force=True)
            self.log("[RPC] 任务 %s 已暂停" % gid[:6])
            return
        except Exception as e:
            fp = False
            with self._rpc_lock:
                t["status"] = RPC.ST_ERROR
                t["errorCode"] = "1"
                t["errorMessage"] = str(e)[:200]
            self._qmem_mark(force=True)
            self._rpc_fire_mascot(False)
            self.log("[RPC] 任务 %s 异常：%s" % (gid[:6], str(e)[:120]))
            return
        if t.get("cancel"):
            if t.get("purge"):
                self._rpc_purge(t)
            with self._rpc_lock:
                t["status"] = RPC.ST_PAUSED
            self._qmem_mark(force=True)
            return
        with self._rpc_lock:
            if fp and os.path.exists(str(fp)):
                t["status"] = RPC.ST_COMPLETE
                t["path"] = str(fp)
                t["name"] = os.path.basename(str(fp))
                if t.get("totalLength"):
                    t["downloadedLength"] = t["totalLength"]
                t["downloadSpeed"] = 0
            else:
                t["status"] = RPC.ST_ERROR
                t["errorCode"] = "1"
                t["errorMessage"] = t.get("errorMessage") or "下载失败（链接失效/被拒/无可用流）"
            t["done_ts"] = time.time()
        self._qmem_mark(force=True)
        ok = (t["status"] == RPC.ST_COMPLETE)
        if ok:
            try:                     # 记账：浏览器接管的直链以前完全不记，导致跨会话重复下载
                key = t.get("_dkey") or self._rpc_norm_key(t.get("uri") or "")
                if key:
                    h = load_history()
                    try:
                        sz = os.path.getsize(t["path"]) if t.get("path") else 0
                    except Exception:
                        sz = 0
                    h[key] = {"name": os.path.basename(t.get("path") or t.get("name") or ""),
                              "dir": os.path.dirname(t.get("path") or ""),
                              "size": sz, "ts": int(time.time())}
                    if len(h) > 4000:            # 账本封顶，丢最旧的，别无限膨胀
                        old = sorted(h.items(), key=lambda kv: kv[1].get("ts", 0))[:-4000]
                        h = dict(old)
                    save_history(h)
                    self._history = h
                    self._hist_cache = h
                    self._rx_account(t.get("path"))
            except Exception:
                pass
        self.log("[RPC] 任务 %s %s" % (gid[:6], "✓ 完成 " + os.path.basename(t["path"]) if ok else "✗ 失败"))
        self._rpc_fire_mascot(ok)

    def _pet_react(self, name):
        """桌宠反应（任意线程可调；桥是 fire-and-forget，宿主不在也不影响下载）。"""
        if getattr(self, "pet_live", False) and getattr(self, "_pet", None):
            try:
                self._pet.react(name)
            except Exception:
                pass

    def _pet_idle_if_quiet(self):
        """暂停/删除后：没有别的任务在跑就让大肥鱼回到常态（以前 BT 暂停/删除后一直举着手）。"""
        try:
            busy = any((x.get("status") == "active") for x in self.rpc_list({"active"}))
        except Exception:
            busy = False
        if not busy:
            self._pet_react("idle")

    def _rpc_fire_mascot(self, ok):
        """把 RPC 任务结果接到看板娘反馈：成功=跳起，失败=抱膝（复用 cast24 一次性窗口）。
        大肥鱼：done=爱心眼+脸红，fail=流泪→流冷汗；还有别的任务在跑，表演完回到 busy。"""
        self._pet_react("done" if ok else "fail")
        try:
            def _back():
                try:
                    if self.rpc_list({RPC.ST_ACTIVE}):
                        self._pet_react("busy")
                except Exception:
                    pass
            threading.Timer(4.6, _back).start()
        except Exception:
            pass
        try:
            now = time.time()
            if ok:
                self._c24_success_until = now + 2.0
            else:
                self._c24_fail_until = now + 3.2
        except Exception:
            pass

    _RPC_FILE_EXT = {
        "exe", "msi", "msp", "apk", "ipa", "deb", "rpm", "dmg", "pkg", "appimage",
        "zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz", "bz",
        "iso", "img", "bin", "cab", "jar", "whl", "nupkg", "crx", "xpi", "vsix",
        "pdf", "epub", "mobi", "azw3", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
        "txt", "md", "csv", "json", "xml", "yaml", "yml", "log", "sql",
        "png", "jpg", "jpeg", "gif", "webp", "bmp", "svg", "ico", "tif", "tiff", "heic",
        "mp3", "flac", "wav", "m4a", "aac", "ogg", "opus", "wma",
        "mp4", "mkv", "mov", "avi", "webm", "flv", "wmv", "m4v", "ts", "mpg", "mpeg", "3gp",
        "ttf", "otf", "woff", "woff2", "dll", "so", "dylib", "py", "js", "ts", "html", "htm",
    }

    _RPC_NAME_KEYS = ("filename", "fname", "file_name", "fn", "file", "name", "title")

    def _rpc_query_filename(self, uri):
        """网盘/CDN 直链常把真文件名藏在查询串 filename=xxx.ext（夸克/百度/阿里/七牛等），
        而 path 末段只是一串哈希。返回带已知文件后缀的真名；没有则退而返回最像名字的参数值。"""
        try:
            from urllib.parse import urlparse, parse_qs, unquote
            q = parse_qs(urlparse(uri or "").query, keep_blank_values=True)
        except Exception:
            return ""
        best = ""
        for k, vals in q.items():
            kk = (k or "").lower().strip().rstrip("*")
            if kk not in self._RPC_NAME_KEYS:
                continue
            for v in vals:
                v = unquote((v or "").strip())
                if v.lower().startswith("utf-8''"):
                    v = v[7:]
                if not v:
                    continue
                if "." in v:
                    ext = v.rsplit(".", 1)[-1].lower()
                    if len(ext) <= 5 and ext in self._RPC_FILE_EXT:
                        return v
                if not best:
                    best = v
        return best

    # Content-Type → 文件后缀映射（修 Bug B：URL 无后缀时按最终响应 Content-Type 补）。
    # 注意：application/octet-stream / binary 太泛，故意不映射，宁可保留来源线索名，
    # 也别把网页/网盘的中转硬造出一个错误后缀。
    _CT_EXT = {
        "application/x-msdownload": ".exe",
        "application/x-msdos-program": ".exe",
        "application/vnd.microsoft.portable-executable": ".exe",
        "application/x-winsetup": ".exe",
        "application/x-apple-diskimage": ".dmg",
        "application/x-msi": ".msi",
        "application/x-debian-package": ".deb",
        "application/x-rpm": ".rpm",
        "application/java-archive": ".jar",
        "application/zip": ".zip",
        "application/x-7z-compressed": ".7z",
        "application/x-rar-compressed": ".rar",
        "application/x-rar": ".rar",
        "application/x-tar": ".tar",
        "application/gzip": ".gz",
        "application/x-bzip2": ".bz2",
        "application/x-xz": ".xz",
        "application/pdf": ".pdf",
        "application/msword": ".doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.ms-excel": ".xls",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        "application/vnd.ms-powerpoint": ".ppt",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
        "application/epub+zip": ".epub",
        "video/mp4": ".mp4",
        "video/x-m4v": ".m4v",
        "video/quicktime": ".mov",
        "video/x-msvideo": ".avi",
        "video/x-matroska": ".mkv",
        "video/webm": ".webm",
        "video/x-flv": ".flv",
        "video/mpeg": ".mpeg",
        "video/3gpp": ".3gp",
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
        "audio/mp4": ".m4a",
        "audio/x-m4a": ".m4a",
        "audio/aac": ".aac",
        "audio/x-wav": ".wav",
        "audio/wav": ".wav",
        "audio/flac": ".flac",
        "audio/ogg": ".ogg",
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
        "image/svg+xml": ".svg",
        "image/avif": ".avif",
        "text/html": ".htm",
        "text/plain": ".txt",
        "text/csv": ".csv",
        "text/xml": ".xml",
        "application/xml": ".xml",
        "application/json": ".json",
    }

    @staticmethod
    def _rpc_cd_filename(headers):
        """从响应头 Content-Disposition 解析文件名（优先 RFC5987 的 filename*=charset''xx，
        兼容 filename="xx"），百分号解码 + 按 charset 还原中文。拿不到返回空串，绝不抛。"""
        try:
            cd = ""
            for k, v in (headers or {}).items():
                if (k or "").lower() == "content-disposition":
                    cd = v or ""
                    break
            if not cd:
                return ""
            import re as _re
            from urllib.parse import unquote
            best = ""
            m = _re.search(r"filename\*\s*=\s*([\w\-]+)'[^']*'([^;\n]+)", cd, _re.I)
            if m:
                charset = (m.group(1) or "utf-8").strip().lower()
                raw = (m.group(2) or "").strip().strip('"')
                try:
                    best = unquote(raw, encoding=charset, errors="replace")
                except Exception:
                    best = unquote(raw)
            if not best:
                m2 = _re.search(r"filename\s*=\s*\"?([^\";\n]+?)\"?\s*(?:;|$)", cd, _re.I)
                if m2:
                    v = (m2.group(1) or "").strip()
                    if v.lower().startswith("utf-8''"):
                        v = unquote(v[7:])
                    else:
                        v = unquote(v)
                    best = v
            best = (best or "").replace("\\", "/").split("/")[-1].strip()
            return best
        except Exception:
            return ""

    def _rpc_ext_from_headers(self, headers, final_url=""):
        """派生带点后缀：Content-Disposition 文件名 → Content-Type 映射 → 最终 URL path 末段。
        三者都拿不到返回空串（保留来源线索名，绝不硬造）。纯逻辑，便于离线回归测试。"""
        try:
            fn = self._rpc_cd_filename(headers)
            if fn and "." in fn:
                e = fn.rsplit(".", 1)[-1].lower()
                if 1 <= len(e) <= 8 and any(c.isalpha() for c in e) and all(
                        c.isalnum() for c in e):
                    return "." + e
            ct = ""
            for k, v in (headers or {}).items():
                if (k or "").lower() == "content-type":
                    ct = (v or "").split(";")[0].strip().lower()
                    break
            if ct in self._CT_EXT:
                return self._CT_EXT[ct]
            if final_url:
                from urllib.parse import urlparse, unquote
                p = urlparse(final_url).path
                if "." in p:
                    e = unquote(p.rsplit(".", 1)[-1]).lower()
                    if e in self._RPC_FILE_EXT:
                        return "." + e
        except Exception:
            pass
        return ""

    def _rpc_name_from_headers(self, headers, final_url=""):
        """服务端响应里能拼出的"完整真名（带后缀）"：优先 Content-Disposition 文件名，
        其次跟随重定向后的最终 URL path 末段（fwlink → …InstallationAssistant.exe 就靠这个）。
        两者都推不出带后缀的名返回空串。"""
        try:
            fn = self._rpc_cd_filename(headers)
            if fn and os.path.splitext(fn)[1]:
                return fn
            if final_url:
                from urllib.parse import urlparse, unquote
                base = unquote(os.path.basename(urlparse(final_url).path))
                if base and "." in base:
                    e = base.rsplit(".", 1)[-1].lower()
                    if 1 <= len(e) <= 8 and any(c.isalpha() for c in e) and all(
                            c.isalnum() for c in e):
                        return base
            return ""
        except Exception:
            return ""

    def _rpc_probe_name(self, url, headers):
        """URL 自身推不出后缀时（fwlink 重定向、服务端不给名的下载端点），发一个跟随重定向的
        HEAD（无果退化 Range 取首字节），从响应头拿真名 + 后缀。返回 (best_full_name, ext)；
        任何网络异常静默返回 ("", "")，绝不阻断主流程。"""
        try:
            px = get_system_proxy()
            pxd = {"http": px, "https": px} if px else None
            h = dict(headers or {})
            h.setdefault("User-Agent", UA)
            kw = {"headers": h, "timeout": 20, "allow_redirects": True, "verify": False}
            if pxd:
                kw["proxies"] = pxd
            hdrs, final = {}, url
            r = None
            try:
                r = SESSION.head(url, **kw)
                if getattr(r, "status_code", 0) < 400:
                    hdrs = r.headers or {}
                    final = r.url or url
            except Exception:
                r = None
            need_get = (not hdrs) or (
                not self._rpc_cd_filename(hdrs)
                and not any((k or "").lower() == "content-type" for k in hdrs))
            if need_get:
                try:
                    if r is not None:
                        r.close()
                except Exception:
                    pass
                gh = dict(h)
                gh["Range"] = "bytes=0-0"
                gkw = dict(kw)
                gkw["headers"] = gh
                gkw["stream"] = True
                try:
                    r2 = SESSION.get(url, **gkw)
                    hdrs = r2.headers or hdrs
                    final = r2.url or final
                    try:
                        r2.close()
                    except Exception:
                        pass
                except Exception:
                    pass
            return self._rpc_name_from_headers(hdrs, final), self._rpc_ext_from_headers(hdrs, final)
        except Exception:
            return "", ""

    def _rpc_is_direct_file(self, uri, name=""):
        try:
            import re
            from urllib.parse import urlparse
            # 流清单扩展名：必须交回 yt-dlp（HLS/DASH），绝不能当直链文件
            _STREAM = {"m3u8", "mpd", "m3u", "f4m", "ism", "dash", "php", "aspx", "jsp"}
            def _looks_like_ext(e):
                return (bool(re.fullmatch(r"[a-z0-9]{1,8}", e)) and any(c.isalpha() for c in e)
                        and e not in _STREAM)
            # 0) 已解析出的输出名自带真后缀（确认窗探针 / 网盘 filename= 已定名）→ 当直链文件。
            #    这样 fwlink 这类"URL 本身无后缀、但已知要存成 xxx.exe"的走精确落名通道，
            #    不再丢给 yt-dlp 二次命名成无后缀的 download。
            nm0 = (name or "").strip()
            if nm0 and "." in nm0:
                e0 = nm0.rsplit(".", 1)[-1].lower()
                if e0 not in _STREAM and (e0 in self._RPC_FILE_EXT or _looks_like_ext(e0)):
                    return True
            path = urlparse(uri or "").path
            # 1) path 末段带扩展名（白名单命中，或"像扩展名"）
            if "." in path:
                ext = path.rsplit(".", 1)[-1].lower()
                if ext in self._RPC_FILE_EXT or _looks_like_ext(ext):
                    return True
            # 2) 查询串 filename=xxx.<扩展名>（夸克等网盘：path 只是哈希，真名/后缀在 query）
            nm = self._rpc_query_filename(uri)
            if nm and "." in nm:
                e = nm.rsplit(".", 1)[-1].lower()
                if e in self._RPC_FILE_EXT or _looks_like_ext(e):
                    return True
            return False
        except Exception:
            return False

    # ================= 浏览器 Cookie 自动补齐（IDM 浏览器集成同款） =================
    def _nd_jar(self):
        jar = getattr(self, "_nd_cookiejar", None)
        if jar is None and ND is not None:
            jar = self._nd_cookiejar = ND.CookieJarLite()
        return jar

    def rpc_sync_cookies(self, cookies):
        """扩展把浏览器里各下载站的 Cookie（含 httpOnly）推过来（sniffdl.syncCookies）。
        只进内存、不落盘；手动粘贴直链 / 外部推送缺 Cookie 时按域名取用。"""
        jar = self._nd_jar()
        if jar is None:
            return {"ok": False, "n": 0}
        try:
            n = jar.update(cookies if isinstance(cookies, list) else [])
        except Exception:
            n = 0
        return {"ok": True, "n": n, "total": len(jar)}

    def _cookies_txt_header(self, url):
        """应用自有 cookies.txt（登录窗 / 导入写的 Netscape 文件）里匹配该 URL 的 Cookie。"""
        try:
            if not _has_app_cookie_file():
                return ""
            import http.cookiejar as _cj
            import urllib.request as _ur
            cj = _cj.MozillaCookieJar(_app_cookie_file())
            cj.load(ignore_discard=True, ignore_expires=False)
            req = _ur.Request(url)
            cj.add_cookie_header(req)
            return req.get_header("Cookie") or ""
        except Exception:
            return ""

    def _auto_cookie_for(self, url, prof=None):
        """缺 Cookie 时按顺序找：① 扩展同步来的浏览器 Cookie（最准，含 httpOnly，Chrome 新加密也不怕）
        ② 程序自己的 cookies.txt ③ 直接读本机 Chrome/Edge Cookie 库（仅网盘等必须登录的站；
        Chrome 127+ 的 App-Bound 加密下常读不出，所以排最后）。返回 (cookie, 来源说明)。"""
        jar = self._nd_jar()
        if jar is not None:
            try:
                c = jar.header_for(url)
                if c:
                    return c, "浏览器扩展同步的 Cookie"
            except Exception:
                pass
        c = self._cookies_txt_header(url)
        if c:
            return c, "cookies.txt"
        mode = str(self.cfg.get("cookie_browser") or "自动")
        if prof and prof.get("need_cookie") and mode not in _LEGACY_NONE:
            c = self._browser_cookie_header(url)
            if c:
                return c, "本机浏览器 Cookie 库"
        return "", ""

    def rpc_add_direct(self, info):
        """界面「粘贴直链」入口（sniffdl.addDirect）：网盘直链或填了自定义请求头的链接 → 直接按直链下载，
        不走嗅探。info = {url, headers_text(可选，每行 Key: Value 或整段 curl -H …), filename, outdir}。
        不是网盘、也没填请求头 → 返回 {"ok": False, "skip": True}，界面照常走嗅探。"""
        info = info or {}
        url = (info.get("url") or "").strip()
        if not re.match(r"^(https?|ftp)://", url, re.I):
            return {"ok": False, "skip": True}
        custom = {}
        if ND is not None:
            try:
                custom = ND.parse_custom_headers(info.get("headers_text") or "")
            except Exception:
                custom = {}
        prof = ND.match_profile(url) if ND is not None else None
        if not prof and not custom:
            return {"ok": False, "skip": True}
        opts = {"header": ["%s: %s" % kv for kv in custom.items()]}
        if (info.get("filename") or "").strip():
            opts["out"] = info["filename"].strip()
        if (info.get("outdir") or "").strip():
            opts["dir"] = info["outdir"].strip()
        gid = self.rpc_add_uri(url, opts, src="manual")
        self.log("[直链] 粘贴直链入队 %s（%s，自定义头 %d 个）" % (
            (gid or "")[:6], (prof or {}).get("name") or "通用直链", len(custom)))
        return {"ok": bool(gid), "gid": gid, "profile": (prof or {}).get("name") or ""}

    def rpc_global_option(self):
        """aria2.getGlobalOption：AriaNg / 脚本会读 dir 当默认保存目录、读连接数显示。"""
        c = str(eff_conn_threads())
        return {"dir": self.cfg.get("outdir") or DEFAULT_OUTDIR,
                "max-concurrent-downloads": "16", "split": c,
                "max-connection-per-server": c, "min-split-size": "1M",
                "continue": "true", "user-agent": UA,
                "rpc-listen-port": str(getattr(getattr(self, "_rpc_srv", None), "bound_port", 0) or 6800)}

    def rpc_netdisk_rules(self):
        """网盘规则表（只读，给界面/扩展展示用）。"""
        if ND is None:
            return []
        out = []
        for p in ND.PROFILES:
            out.append({"key": p["key"], "name": p["name"], "hosts": list(p["dl_hosts"]),
                        "conn": ND.profile_conn(p, self.cfg, eff_conn_threads()),
                        "referer": p.get("referer") or "", "default_ua": p.get("default_ua") or "",
                        "cookie_domain": p.get("cookie_domain") or "", "tips": p.get("tips") or ""})
        return out

    def _browser_cookie_header(self, url):
        """尽力从本机 Chrome/Edge 读取该 URL 域名的 Cookie（含 httpOnly），拼成 Cookie 头。
        用于网盘直链缺登录态被 WAF 拒时兜底；任何失败都静默返回空串，绝不抛。"""
        try:
            from urllib.parse import urlparse
            host = (urlparse(url or "").hostname or "").lower()
            if not host:
                return ""
            from yt_dlp.cookies import extract_cookies_from_browser
            jar = None
            for b in ("chrome", "edge"):
                try:
                    jar = extract_cookies_from_browser(b)
                    if jar and len(list(jar)) > 0:
                        break
                except Exception:
                    jar = None
            if not jar:
                return ""
            seen = {}
            for ck in jar:
                try:
                    dom = (getattr(ck, "domain", "") or "").lstrip(".").lower()
                    if not dom:
                        continue
                    if host == dom or host.endswith("." + dom):
                        val = getattr(ck, "value", "") or ""
                        if val and ck.name not in seen:
                            seen[ck.name] = val
                except Exception:
                    continue
            return "; ".join("%s=%s" % (n, v) for n, v in seen.items())
        except Exception:
            return ""

    def _rpc_referer_guess(self, url):
        """网盘类直链缺 Referer 时补一个站内来源（夸克/百度 WAF 常按 Referer 放行）。"""
        try:
            from urllib.parse import urlparse
            host = (urlparse(url or "").hostname or "").lower()
        except Exception:
            return ""
        if "quark" in host:
            return "https://pan.quark.cn/"
        if "baidu" in host and ("pan." in host or "ydus" in host):
            return "https://pan.baidu.com/"
        if "twimg.com" in host or "twitter.com" in host or host == "x.com":
            return "https://twitter.com/"
        return ""

    def _rpc_is_waf_host(self, url):
        """【已废弃的旧判断，保留函数名兼容】以前声称夸克/百度 CDN 靠 JA3 指纹、aria2 必被拒，要强制单连接。
        实测与开源实践（alist/OpenList、LinkSwift→aria2/Motrix）都证明：带对 Cookie + UA (+Referer)
        aria2 多连接照样能下。现在是否走 aria2 由网盘规则表 netdisk_rules.PROFILES[*].use_aria2 决定，
        目前所有网盘都是 True —— 返回 True 表示"该站规则要求只走单连接"。"""
        if ND is None:
            return False
        try:
            p = ND.match_profile(url)
            return bool(p and not p.get("use_aria2", True))
        except Exception:
            return False

    # ================= 直链专用 aria2c HTTP 引擎（与 BT 守护进程分开） =================
    # 【"分发成功但没下"根因之二，11-06 实测复现】以前直链借用 BT 守护进程（--max-concurrent-downloads=5，
    # 还会 --input-file 续种）。只要 BT 任务/找不到 peer 的磁力占满 5 个名额，直链 addUri 就在 aria2 里
    # 无限排队（status=waiting），而我们的轮询只认 active/complete/error → 任务列表一直「下载中 0B」，
    # 不报错、不超时、永远不动。现在直链走独立的 HTTP 引擎：16 并发名额、无 session、64M 写缓存，
    # 跟着主进程生死（--stop-with-process），BT 再怎么堵都不影响直链。
    def _h2_ensure(self):
        if not hasattr(self, "_h2") or self._h2 is None:
            self._h2 = {"proc": None, "port": 0, "secret": "", "rpc_id": 0}
        if not hasattr(self, "_h2_lock"):
            self._h2_lock = threading.Lock()
        with self._h2_lock:
            proc = self._h2.get("proc")
            port = self._h2.get("port") or 0
            if proc is not None and proc.poll() is None and port:
                return port
            a2 = resolve_aria2c(self.cfg) or ARIA2C
            if not a2 or not os.path.isfile(a2):
                raise RuntimeError("未找到 aria2c（多连接引擎），请在设置里指定 aria2c 路径")
            d = os.path.join(os.path.dirname(self._bt_datadir()), "http")
            try:
                os.makedirs(d, exist_ok=True)
            except Exception:
                pass
            port = self._bt_free_port(base=17353)
            if not port:
                raise RuntimeError("找不到可用端口启动多连接引擎")
            import secrets as _sec
            secret = _sec.token_hex(12)
            win = (os.name == "nt")
            cmd = [a2, "--enable-rpc=true", "--rpc-listen-all=false",
                   "--rpc-listen-port=%d" % port, "--rpc-secret=%s" % secret,
                   "--stop-with-process=%d" % os.getpid(),
                   "--dir=%s" % (self.cfg.get("outdir") or DEFAULT_OUTDIR),
                   "--log=%s" % os.path.join(d, "aria2_http.log")]
            cmd += (ND.aria2_http_daemon_args(win) if ND is not None else
                    ["--max-concurrent-downloads=16", "--summary-interval=0"])
            flags = 0x08000000 if win else 0
            try:
                # cwd 钉到数据目录：否则 aria2c 继承 SniffDL.exe 所在目录句柄，重打包时 COLLECT 删目录报 WinError 32
                proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, creationflags=flags, cwd=d)
            except Exception as e:
                raise RuntimeError("启动多连接引擎失败：%s" % str(e)[:120])
            self._h2.update({"proc": proc, "port": port, "secret": secret})
        for _ in range(40):
            if proc.poll() is not None:
                raise RuntimeError("多连接引擎 aria2c 异常退出（见 ~/.snifftdl/http/aria2_http.log）")
            try:
                self._h2_rpc("aria2.getVersion", [])
                self.log("[直链] 多连接引擎就绪 127.0.0.1:%d（独立于 BT 引擎）" % port)
                return port
            except Exception:
                time.sleep(0.25)
        raise RuntimeError("多连接引擎启动超时")

    def _h2_rpc(self, method, params):
        import urllib.request
        h2 = getattr(self, "_h2", None) or {}
        port = h2.get("port") or 0
        if not port:
            raise RuntimeError("多连接引擎未运行")
        with self._h2_lock:
            h2["rpc_id"] = h2.get("rpc_id", 0) + 1
            rid = h2["rpc_id"]
        payload = json.dumps({"jsonrpc": "2.0", "id": str(rid), "method": method,
                              "params": ["token:" + h2.get("secret", "")] + list(params)}).encode()
        req = urllib.request.Request("http://127.0.0.1:%d/jsonrpc" % port, data=payload,
                                     headers={"Content-Type": "application/json"})
        # 本机回环绝不走系统代理（开着代理软件时 urllib 会把 127.0.0.1 也丢给代理 → 404/超时）
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=15) as resp:
            obj = json.loads(resp.read().decode("utf-8", "ignore"))
        if isinstance(obj, dict) and obj.get("error"):
            raise RuntimeError((obj.get("error") or {}).get("message", "aria2 RPC 错误"))
        return (obj or {}).get("result")

    def _http_aria2c(self, url, outdir, name, headers, prog=None, cancel=None,
                     conn=None, min_split="1M", status_cb=None):
        """内嵌 aria2c 多连接分段下载任意 HTTP(S) 直链（软件包/网盘直链/附件提速主力）。
        走直链专用 HTTP 引擎（_h2，不和 BT 抢名额）。成功返回落盘路径；失败抛 RuntimeError，
        消息里保留 aria2 原始 status=xxx 方便上层翻译成中文原因；调用方决定是否回退单连接。"""
        port = self._h2_ensure()          # 无 aria2c 会抛 RuntimeError
        conn = str(conn or eff_conn_threads())
        path = os.path.join(outdir, name)
        # aria2 header 只带"内容"头，剥掉会让多连接/续传错乱的传输层头
        _drop = (ND.HOP_HEADERS if ND is not None else
                 {"content-length", "accept-encoding", "connection", "host", "range",
                  "transfer-encoding"})
        hdrs = []
        ua = None
        for k, v in (headers or {}).items():
            if not v:
                continue
            kl = str(k).lower()
            if kl in _drop:
                continue
            if kl == "user-agent":
                ua = str(v)       # UA 用 aria2 独立选项给：header 里再塞一个会和 aria2 默认 UA 打架成双 UA
                continue
            hdrs.append("%s: %s" % (k, v))
        if ND is not None:
            opts = ND.aria2_http_task_opts(conn, min_split, is_windows=(os.name == "nt"))
        else:
            opts = {"split": conn, "max-connection-per-server": conn, "min-split-size": min_split,
                    "file-allocation": "none", "continue": "true", "max-tries": "5",
                    "retry-wait": "2", "connect-timeout": "10", "timeout": "30",
                    "auto-file-renaming": "false", "allow-overwrite": "true"}
        opts.update({"dir": outdir, "out": name})
        if ua:
            opts["user-agent"] = ua
        else:
            opts["user-agent"] = UA      # 没人给 UA 时也别让 aria2 报 "aria2/1.37.0"（不少 CDN 直接拒）
        if hdrs:
            opts["header"] = hdrs
        # 自带 aria2c 1.37 不支持 SOCKS 代理（all-proxy=socks5:// 会让 addUri 直接 HTTP 400，
        # 逼得每条直链退回单连接）。故只在 http/https 代理时才塞 all-proxy；socks5/socks4 一律
        # 不塞——让 aria2c 直连多连接；万一该站被墙，由 _rpc_direct_file 回退 curl_cffi/requests（支持 socks5）。
        px = get_system_proxy()
        if px and str(px).lower().startswith(("http://", "https://")):
            opts["all-proxy"] = px
        gid = str(self._h2_rpc("aria2.addUri", [[url], opts]))
        self.log("[直链] aria2c 多连接 %s 路已启动 gid=%s（min-split=%s）" % (conn, gid[:10], min_split))
        KEYS = ["status", "totalLength", "completedLength", "downloadSpeed",
                "errorCode", "errorMessage", "connections"]
        last = 0.0
        t_start = time.time()
        last_done, last_move = -1, time.time()
        STALL = 120                       # 连续 120 秒 0 字节进展 → 判死，报原因（绝不无限挂着）
        # ---- 尾段加速（IDM「动态分段」的 aria2 近似）：aria2 不会把慢连接手里的分片让给快连接，
        # 个别 CDN 节点/连接被限到几十 KB/s 时，最后几片能拖几十秒。检测到"总速度掉到峰值 30% 以下
        # 持续 5 秒"时强制暂停再继续：aria2 按 .aria2 控制文件续传（已下的块不丢），慢连接重新抽签。
        peak, low_since, kicks, last_kick, kicking = 0.0, 0.0, 0, 0.0, False
        while True:
            if cancel and cancel():
                try:
                    self._h2_rpc("aria2.forceRemove", [gid])
                except Exception:
                    pass
                raise _AbortSignal()
            st = self._h2_rpc("aria2.tellStatus", [gid, KEYS]) or {}
            status = st.get("status")
            total = int(st.get("totalLength") or 0)
            done = int(st.get("completedLength") or 0)
            spd = int(st.get("downloadSpeed") or 0)
            now = time.time()
            if done != last_done:
                last_done, last_move = done, now
            if status_cb:
                try:
                    if status == "waiting":
                        status_cb("排队中（多连接引擎）…")
                    elif status == "active" and total == 0 and done == 0:
                        status_cb("连接服务器中…")
                    elif status == "active":
                        status_cb("多连接下载中（%s 路）" % (st.get("connections") or conn))
                except Exception:
                    pass
            if prog and (now - last >= 0.4 or status in
                         ("complete", "error", "paused", "removed")):
                last = now
                prog({"status": "downloading", "downloaded_bytes": done,
                      "total_bytes": total or None, "speed": spd})
            if status == "complete":
                real = path
                try:
                    fs = (self._h2_rpc("aria2.tellStatus", [gid, ["files"]]) or {}).get("files") or []
                    if fs and fs[0].get("path"):
                        real = fs[0]["path"]
                except Exception:
                    pass
                try:
                    self._h2_rpc("aria2.removeDownloadResult", [gid])
                except Exception:
                    pass
                if os.path.exists(real):
                    self.log("[直链] aria2c 多连接完成 %s（%.1fs）" % (os.path.basename(real),
                                                                time.time() - t_start))
                    return real
                raise RuntimeError("aria2c 报完成但文件缺失")
            if status in ("error", "removed"):
                ec = st.get("errorCode")
                em = st.get("errorMessage")
                try:
                    self._h2_rpc("aria2.removeDownloadResult", [gid])
                except Exception:
                    pass
                raise RuntimeError("code=%s msg=%s" % (ec, em))
            if status == "paused":
                if kicking:
                    try:
                        self._h2_rpc("aria2.unpause", [gid])
                    except Exception:
                        pass
                    kicking = False
                    time.sleep(0.2)
                    continue
                raise RuntimeError("aria2c 任务被挂起")
            # 默认关闭：本地 mock（25% 连接被限到 32KB/s）三轮实测无稳定收益（21.6/23.7/31.9s vs 关闭 21.3/22.2/43.7s），
            # 反而多开连接；留作实验开关 config.json "aria2_tail_kick": true
            if status == "active" and total > 0 and not kicking and self.cfg.get("aria2_tail_kick"):
                if spd > peak:
                    peak = float(spd)
                remain = total - done
                if (now - t_start > 8 and peak > 0 and spd < 0.3 * peak and remain > 0
                        and kicks < 3 and now - last_kick > 12):
                    if not low_since:
                        low_since = now
                    elif now - low_since >= 5:
                        kicks += 1
                        last_kick = now
                        low_since = 0.0
                        kicking = True
                        self.log("[直链] 尾段加速：速度跌到峰值 %d%%（%s/s），重排慢连接（第 %d 次）"
                                 % (int(100 * spd / peak), self._fmt_bytes(spd), kicks))
                        try:
                            self._h2_rpc("aria2.forcePause", [gid])
                        except Exception:
                            kicking = False
                        peak = 0.0          # 重连后重新测峰值
                else:
                    low_since = 0.0
            if status == "waiting" and now - t_start > 5:
                # 理论上独立引擎不会排队；万一排了，把它挪到队首，而不是傻等
                try:
                    self._h2_rpc("aria2.changePosition", [gid, 0, "POS_SET"])
                except Exception:
                    pass
            if now - last_move > STALL:
                try:
                    self._h2_rpc("aria2.forceRemove", [gid])
                except Exception:
                    pass
                raise RuntimeError("stall: 连续 %d 秒没有任何数据（服务器无响应或被限流）" % STALL)
            time.sleep(0.4)

    def _rpc_clean_stale(self, path):
        """成品已存在时清掉 aria2 残留：同名 .aria2 控制文件会误导它报 code=13 引发重下，
        0 字节的 .part 也是垃圾。只删这两类，绝不动成品。"""
        try:
            ctl = path + ".aria2"
            if os.path.isfile(ctl):
                os.remove(ctl)
            part = path + ".part"
            if os.path.isfile(part) and os.path.getsize(part) == 0:
                os.remove(part)
        except Exception:
            pass

    def _rpc_direct_file(self, t, prog, headers):
        """带明确后缀的直链：精确落名 + 浏览器头 + Range 断点续传；被 WAF 拒（412/403…）时
        自动用 curl_cffi Chrome TLS 指纹重试一次（夸克等网盘 CDN 就吃这一套）。"""
        import os as _os
        from urllib.parse import urlparse, unquote
        url = t["uri"]
        outdir = t["dir"]
        # 目标文件名：out 有后缀就用 → 否则优先 query 里的 filename= → 再退 path basename
        name = (t.get("name") or "").strip()
        if not name or "." not in name:
            qn = self._rpc_query_filename(url)
            base = qn or unquote(_os.path.basename(urlparse(url).path)) or t["gid"]
            if "." in base:
                name = base
            else:
                pathext = urlparse(url).path.rsplit(".", 1)[-1] if "." in urlparse(url).path else ""
                name = (name or base) + ("." + pathext if pathext else "")
        name = safe_name(name)
        # Bug B 兜底：URL/query 都推不出后缀（fwlink 重定向、无文件名的下载端点），
        # 探测服务端响应头补一个真后缀，避免落成无后缀的 download 文件。
        if name and not _os.path.splitext(name)[1]:
            cd_fn, pext = self._rpc_probe_name(url, headers)
            fixed = ""
            if cd_fn and _os.path.splitext(cd_fn)[1]:
                fixed = safe_name(cd_fn)
            elif pext:
                fixed = safe_name(name + pext)
            if fixed and fixed != name:
                self.log("[直链] 补后缀：%s → %s（服务端 Content-Disposition/Content-Type）" % (
                    name[:40], fixed[:60]))
                name = fixed
                try:
                    with self._rpc_lock:
                        t["name"] = name
                except Exception:
                    pass
        path = _os.path.join(outdir, name)
        # 已存在同名非空文件 → 直接跳过，绝不重复下载（根治"把已有的一遍遍重下"）
        try:
            if _os.path.exists(path) and _os.path.getsize(path) > 0:
                self._rpc_clean_stale(path)
                self.log("[直链] 文件已存在，跳过重复下载：%s" % name)
                return path
        except Exception:
            pass
        tmp = path + ".part"
        resume = 0
        if _os.path.exists(tmp):
            try:
                resume = _os.path.getsize(tmp)
            except Exception:
                resume = 0
        h = dict(headers or {})
        prof = None
        if ND is not None:
            try:
                prof = ND.match_profile(url)
            except Exception:
                prof = None
        _hget = (ND.hget if ND is not None else (lambda d, n: d.get(n) or d.get(n.lower())))

        def _hdef(name, val):
            """只补缺的头（大小写不敏感）——推送方/浏览器给的头一个字都不动。"""
            if ND is not None:
                return ND.hset_default(h, name, val)
            if val and not _hget(h, name):
                h[name] = val
                return True
            return False

        # 0) 签名直链过期预检：明确过期就别去撞 403 了，直接告诉用户回网盘重新点
        if ND is not None:
            try:
                ex = ND.expiry_state(url)
                if ex["expired"]:
                    msg = ND.explain_failure(url, None, h)
                    self.log("[直链] %s" % msg)
                    raise RuntimeError(msg)
                if ex["expire_ts"]:
                    self.log("[直链] 签名有效期：%s 到期（剩 %d 分钟，依据 %s）" % (
                        ND.fmt_ts(ex["expire_ts"]), max(0, ex["left"]) // 60, ex["how"]))
            except RuntimeError:
                raise
            except Exception:
                pass
        # 1) 缺 Cookie 才自动补（IDM 浏览器集成同款：扩展同步的浏览器 Cookie → cookies.txt → 本机浏览器）
        if not _hget(h, "Cookie"):
            try:
                _ck, _how = self._auto_cookie_for(url, prof)
            except Exception:
                _ck, _how = "", ""
            if _ck:
                h["Cookie"] = _ck
                self.log("[直链] 推送方没给 Cookie，已从%s按域补齐（%d 项）" % (_how, _ck.count("=")))
        # 2) 缺 Referer 时按网盘规则/站点补一个（夸克/阿里/123 等会看 Referer）
        if not _hget(h, "Referer"):
            _g = (prof or {}).get("referer") or self._rpc_referer_guess(url)
            if _g:
                h["Referer"] = _g
        _ua_given = bool(_hget(h, "User-Agent"))
        _hdef("User-Agent", UA)
        _hdef("Accept", "*/*" if prof else "application/octet-stream,*/*;q=0.8")
        if not prof:
            _hdef("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8")
        # Chrome 那套 Sec-Fetch / sec-ch-ua 只在"我们自己冒充 Chrome"（没人给 UA、也不是网盘）时补：
        # 推送方给了 quark-cloud-drive / netdisk 之类 UA 时再塞 Chrome 125 的 sec-ch-ua，身份自相矛盾。
        if not _ua_given and not prof:
            _hdef("Accept-Encoding", "gzip, deflate, br")
            _hdef("Sec-Fetch-Dest", "empty")
            _hdef("Sec-Fetch-Mode", "cors")
            _hdef("Sec-Fetch-Site", "same-site")
            _hdef("Sec-Fetch-Storage-Access", "active")
            if not (_hget(h, "sec-ch-ua")):
                h["sec-ch-ua"] = '"Chromium";v="125", "Google Chrome";v="125", "Not.A/Brand";v="24"'
                h["sec-ch-ua-mobile"] = "?0"
                h["sec-ch-ua-platform"] = '"Windows"'
        if resume > 0:
            h["Range"] = "bytes=%d-" % resume
        px = get_system_proxy()
        pxd = {"http": px, "https": px} if px else None
        ANTI = (401, 403, 405, 406, 412, 429, 503)
        # 请求头诊断：只记有无 + Cookie 名字，绝不落值（防隐私外泄）
        _ck = _hget(h, "Cookie") or ""
        _rf = _hget(h, "Referer") or ""
        _ckn = ",".join(sorted({kv.split("=")[0].strip()
                                for kv in _ck.split(";") if kv.strip()}))[:120]
        try:
            _host = urlparse(url).hostname or "?"
        except Exception:
            _host = "?"
        _hasq = "?" in (url or "")
        self.log("[直链] 头诊断 host=%s 规则=%s 带签名query=%s UA=%s Referer=%s Cookie=%s[%s] resume=%d" % (
            _host, (prof or {}).get("name") or "通用", "是" if _hasq else "否(可能被剥参)",
            ((_hget(h, "User-Agent") or "")[:48] + ("(推送方)" if _ua_given else "(程序默认)")),
            "有" if _rf else "无", "有" if _ck else "无", _ckn or "—", resume))

        def _fail(status=None, raw=""):
            """失败一律翻译成中文原因抛出 → _rpc_run_task 写进任务 errorMessage，列表里直接看得到。"""
            if ND is not None:
                try:
                    return RuntimeError(ND.explain_failure(url, status, h, raw=raw))
                except Exception:
                    pass
            return RuntimeError(("HTTP %s " % status if status else "") + str(raw)[:160])

        def _set_phase(msg):
            try:
                with self._rpc_lock:
                    t["statusMessage"] = msg
            except Exception:
                pass

        # 提速主路径（Motrix 同款）：非磁力直链交给内嵌 aria2c 多连接分片。
        # 但自带 aria2c 不支持 SOCKS 代理——若当前是 socks5/socks4 代理，aria2c 只能直连，
        # 遇到必须走代理的站（如国内直连不通的 GitHub）会卡满重试（~60s）才回退，几 KB 文件
        # 都被拖成一分多钟。故检测到 SOCKS 代理时直接跳过 aria2c，走 curl_cffi（它支持 socks5）。
        _px = get_system_proxy() or ""
        _socks = str(_px).lower().startswith(("socks5://", "socks5h://", "socks4://", "socks4a://"))
        _use_a2 = not self._rpc_is_waf_host(url)
        _a2_status = None
        if resume == 0 and not _socks and _use_a2:
            # 连接数：网盘规则（防封上限）∧ 全局设置 ∧ 推送方要求（split / max-connection-per-server）
            _conn = eff_conn_threads()
            _msplit = "1M"
            if ND is not None and prof:
                _conn = ND.profile_conn(prof, self.cfg, _conn)
                _msplit = prof.get("min_split") or "1M"
            _hint = t.get("a2opts") or {}
            for _k in ("max-connection-per-server", "split"):
                try:
                    if _hint.get(_k):
                        _conn = max(1, min(_conn, int(_hint[_k])))
                except Exception:
                    pass
            if _hint.get("min-split-size"):
                _msplit = str(_hint["min-split-size"])
            try:
                return self._http_aria2c(url, outdir, name, h, prog,
                                         cancel=lambda: t.get("cancel"), conn=_conn,
                                         min_split=_msplit, status_cb=_set_phase)
            except _AbortSignal:
                raise
            except Exception as e:
                _a2_status = ND.http_status_from_error(str(e)) if ND is not None else None
                if _a2_status in (404, 410) or (ND is not None and ND.expiry_state(url)["expired"]):
                    raise _fail(_a2_status, str(e))      # 文件没了/链接过期：单连接重试也是白费
                self.log("[直链] aria2c 多连接不可用(%s)，回退单连接…" % str(e)[:90])
                _set_phase("多连接被拒，单连接重试中…")
        elif _socks:
            self.log("[直链] 当前为 SOCKS 代理（aria2c 不支持），直走 curl_cffi 经代理下载")
        elif not _use_a2:
            self.log("[直链] %s 规则要求单连接，跳过 aria2c" % ((prof or {}).get("name") or "该站"))

        def _get_session():
            kw = {"headers": h, "timeout": 60, "stream": True, "verify": False}
            if pxd:
                kw["proxies"] = pxd
            return SESSION.get(url, **kw)

        def _get_chrome():
            # curl_cffi 的价值是"TLS(JA3)+HTTP2 指纹+请求头"整套自洽的 Chrome 身份。
            # 若把浏览器真实 UA / sec-ch-ua 一起塞进去，会和 curl_cffi 内部钉死的 Chrome
            # 版本打架（UA 说 125、Sec-Ch-Ua 说 131…），夸克/阿里 WAF 一眼识破 → 仍返 412。
            # 故这里剥掉 UA / sec-ch-ua* / accept-encoding，让 curl_cffi 自己出一致的头，
            # 只保留 Referer / Cookie / Range 这些"内容"头。
            # 例外：UA 是推送方/浏览器明确给的（网盘直链常把签名绑在 UA 上，如百度 netdisk、115、夸克客户端 UA）
            # → 一律原样保留，宁可指纹不自洽也不能换 UA（换了必 403）。
            from curl_cffi import requests as _creq
            _drop = {"sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform", "accept-encoding"}
            if not _ua_given:
                _drop.add("user-agent")
            ch = {k: v for k, v in h.items() if k.lower() not in _drop}
            kw = {"headers": ch, "timeout": 60, "stream": True,
                  "impersonate": "chrome", "verify": False}
            if pxd:
                kw["proxies"] = pxd
            return _creq.get(url, **kw)

        def _cffi_available():
            try:
                import importlib.util as _iu
                return _iu.find_spec("curl_cffi") is not None
            except Exception:
                return False

        r = None
        used = "requests"
        try:
            r = _get_session()
            if getattr(r, "status_code", 0) in ANTI and not _cffi_available():
                # 没装 curl_cffi 就别硬试：否则真实原因（412 缺 Cookie / 403 过期）会被「No module named curl_cffi」盖掉
                self.log("[直链] 普通请求被拒 HTTP %s（未安装 curl_cffi，无法换 Chrome 指纹重试）" % r.status_code)
            elif getattr(r, "status_code", 0) in ANTI:
                self.log("[直链] 普通请求被拒 HTTP %s，改用 Chrome 指纹重试…" % r.status_code)
                try:
                    r.close()
                except Exception:
                    pass
                # 若这次压根没带 Cookie，尽力从本机浏览器补一份（夸克等网盘 WAF 常要登录态）
                if not _hget(h, "Cookie"):
                    _bc = self._browser_cookie_header(url)
                    if _bc:
                        h["Cookie"] = _bc
                        self.log("[直链] 已从本机浏览器补 Cookie（%d 项）后重试" % _bc.count("="))
                    else:
                        self.log("[直链] 本机浏览器未读到该域 Cookie，仍用无 Cookie 指纹重试")
                r = _get_chrome()
                used = "chrome"
        except Exception as e:
            self.log("[直链] 普通请求异常(%s)，改用 Chrome 指纹重试…" % str(e)[:60])
            try:
                r = _get_chrome()
            except Exception as e2:
                raise _fail(_a2_status, str(e2))
            used = "chrome"
        try:
            if r.status_code not in (200, 206):
                try:
                    _svr = r.headers.get("server") or r.headers.get("via") or "?"
                except Exception:
                    _svr = "?"
                _err = _fail(r.status_code)
                self.log("[直链] 最终仍被拒 HTTP %s（引擎=%s, server=%s）→ %s" % (
                    r.status_code, used, _svr, str(_err)[:160]))
                raise _err
            if resume > 0 and r.status_code != 206:
                resume = 0
            clen = int((r.headers.get("content-length") or "0").split(",")[0].strip() or 0)
            total = (resume + clen) if r.status_code == 206 else clen
            done = resume
            mode = "ab" if resume > 0 else "wb"
            with open(tmp, mode) as f:
                for chunk in r.iter_content(256 * 1024):
                    if t.get("cancel"):
                        raise _AbortSignal()
                    if not chunk:
                        continue
                    f.write(chunk)
                    done += len(chunk)
                    if prog:
                        _el = max(0.001, time.time() - (t.get("start_ts") or time.time()))
                        prog({"status": "downloading", "downloaded_bytes": done,
                              "total_bytes": total or None,
                              "speed": int(done / _el)})
            _os.replace(tmp, path)
        finally:
            try:
                r.close()
            except Exception:
                pass
        return path

    def rpc_remove(self, gid):
        with self._rpc_lock:
            t = self._rpc_tasks.get(str(gid))
            st = (t or {}).get("status")
            popped = False
            if t:
                t["cancel"] = True
                t["purge"] = True
                th = t.get("thread")
                live = bool(th and th.is_alive())
                self._rpc_tasks.pop(str(gid), None)
                try:
                    self._rpc_order.remove(str(gid))
                    popped = True
                except ValueError:
                    popped = True
        if popped:
            self._qmem_mark(force=True)     # 只在你确实摘掉了任务时才写盘（锁外，防自锁）
        # 没有活动线程会去收尾清理（未开跑/已结束）→ 这里直接删残留；
        # 有线程在跑则等它响应 cancel 后在 abort 处理里自清（成品文件仍保留）。
        # 已完成的任务只摘掉队列条目，绝不碰成品文件：BT 的 _bt_hard_remove 会连
        # 已下好的片子一起删，记忆系统给"已完成"行也放了 ✕ 按钮，不守住这条就是误删用户文件。
        if t is not None and not live and st != (RPC.ST_COMPLETE if RPC else "complete"):
            if t.get("btGid") or self._rpc_is_bt(t.get("uri") or ""):
                self._bt_hard_remove(t)
            else:
                self._rpc_purge(t)

    def rpc_pause(self, gid):
        hit = False
        with self._rpc_lock:
            t = self._rpc_tasks.get(str(gid))
            if t and t["status"] in (RPC.ST_ACTIVE, RPC.ST_WAITING):
                t["cancel"] = True
                t["status"] = RPC.ST_PAUSED
                hit = True
        if hit:
            self._qmem_mark(force=True)

    def rpc_resume(self, gid):
        with self._rpc_lock:
            t = self._rpc_tasks.get(str(gid))
        if not t:
            return
        th_alive = bool(t.get("thread") and t["thread"].is_alive())
        # 稍后下载（waiting 且无活动线程）→ 首次开跑
        if t["status"] == RPC.ST_WAITING and not th_alive:
            with self._rpc_lock:
                t["cancel"] = False
                t["note"] = ""
            th = threading.Thread(target=self._rpc_run_task, args=(t["gid"],),
                                  name="RpcTask-%s" % t["gid"][:6], daemon=True)
            with self._rpc_lock:
                t["thread"] = th
            th.start()
            self._qmem_mark(force=True)
            self.log("[RPC] 开始稍后任务 %s" % t["gid"][:6])
            return
        if t["status"] != RPC.ST_PAUSED:
            return
        # 重投：清 cancel 标志、重置进度，另起线程跑
        with self._rpc_lock:
            t["cancel"] = False
            t["status"] = RPC.ST_WAITING
            t["downloadedLength"] = 0
            t["note"] = ""                    # 「点继续」之后就不再挂着"重启未自动重下"的备注
            t["kind"] = "bt" if self._rpc_is_bt(t.get("uri") or "") else "http"
        th = threading.Thread(target=self._rpc_run_task, args=(t["gid"],),
                              name="RpcTask-%s" % t["gid"][:6], daemon=True)
        with self._rpc_lock:
            t["thread"] = th
        th.start()
        self._qmem_mark(force=True)

    def rpc_struct(self, gid):
        with self._rpc_lock:
            t = self._rpc_tasks.get(str(gid))
            if not t:
                return None
            return dict(t)

    def rpc_list(self, segments):
        segs = set(segments or [])
        out = []
        with self._rpc_lock:
            for gid in self._rpc_order:
                t = self._rpc_tasks.get(gid)
                if t and t["status"] in segs:
                    out.append(dict(t))
        return out

    def rpc_theme_info(self):
        """当前主题身份 + 配色 + 头像，供浏览器插件弹窗 / 页面小框 / webview 自动换肤。
        P2：桌面桌宠已统一为单一 Live2D「大肥鱼」，此处恒输出大肥鱼蓝主题（不再随六界切换）。"""
        try:
            return feiyu_theme_payload()
        except Exception:
            pass
        # 兜底（理论上走不到）：退回旧的按世界输出
        key = getattr(self, "theme", DEFAULT_TKEY)
        th = THEMES.get(key) or {}
        pal = dict(C)
        accent = (pal.get("pink") or pal.get("purple") or pal.get("gold")
                  or pal.get("ink") or "#F77FB0")
        return {
            "theme": key, "label": th.get("label") or key, "char": th.get("char") or "",
            "role": th.get("role") or "", "note": th.get("note") or "",
            "motif": th.get("motif") or "", "cta": PERSONA_CTA.get(key) or {},
            "palette": pal, "accent": accent, "avatar": th.get("AVATAR") or "",
        }

    def rpc_status_info(self):
        active = 0
        try:
            active = len(self.rpc_list({RPC.ST_ACTIVE}))
        except Exception:
            active = 0
        return {
            "ok": True,
            "service": "SniffDL",
            "version": getattr(self, "VERSION", ""),
            "theme": getattr(self, "theme", DEFAULT_TKEY),
            "rpc_port": getattr(self, "_rpc_srv", None).bound_port
                        if getattr(self, "_rpc_srv", None) else 0,
            "active": active,
            "takeover_ready": True,
        }

    # ================= IDM 式下载接管：桌面确认窗 / 进度窗 / 完成窗 =================
    # 浏览器插件截获下载后统一走 sniffdl.intercept → 这里；插件受 MV3 沙箱所限弹不出
    # 原生“另存为”文件夹选择器、也承载不了看板娘审美，故把 IDM 那三窗做进桌面本体。
    def _fmt_bytes(self, n):
        try:
            n = float(n or 0)
        except Exception:
            return "—"
        if n <= 0:
            return "未知大小"
        units = ["B", "KB", "MB", "GB", "TB"]
        i = 0
        while n >= 1024 and i < len(units) - 1:
            n /= 1024.0
            i += 1
        return ("%d B" % n) if i == 0 else ("%.1f %s" % (n, units[i]))

    def _rpc_headers_list(self, info):
        """把插件传来的 {referer,cookie,ua} 组装成 aria2 风格 header list（喂给 rpc_add_uri）。"""
        h = []
        seen = set()
        # 0.6.0 扩展：浏览器真实发出的请求头（webRequest 抓的，含 httpOnly Cookie / 站点要求的 UA）
        # 放 info["headers"]，原样优先；没有才退回 referer/cookie/ua 三件套。
        cap = info.get("headers")
        if isinstance(cap, dict):
            for k, v in cap.items():
                kl = str(k).lower().strip()
                if not v or kl in seen:
                    continue
                if ND is not None and kl in ND.HOP_HEADERS:
                    continue
                seen.add(kl)
                h.append("%s: %s" % (str(k).strip(), str(v).strip()))
        ref = (info.get("referer") or "").strip()
        ck = (info.get("cookie") or "").strip()
        ua = (info.get("ua") or "").strip()
        if ref and "referer" not in seen:
            h.append("Referer: " + ref)
        if ck and "cookie" not in seen:
            h.append("Cookie: " + ck)
        if ua and "user-agent" not in seen:
            h.append("User-Agent: " + ua)
        return h

    def _rpc_start_from_info(self, info, outdir, name):
        """按确认窗（或静默接管）最终确定的目录 + 文件名起下载，返回 gid。"""
        url = info.get("url")
        outdir = (outdir or info.get("outdir") or self.cfg.get("outdir") or DEFAULT_OUTDIR)
        opts = {"dir": outdir, "header": self._rpc_headers_list(info)}
        nm = (name or info.get("filename") or "").strip()
        if nm:
            opts["out"] = nm
        return self.rpc_add_uri(url, opts, defer=bool(info.get("_defer")),
                                src=("user" if self._rpc_is_user_source(info) else "browser"))

    # ---- Bug A：下载确认卡"没人点也反复弹"根治（意图分流 + 杂链过滤 + 汇总卡）----
    # 只有"用户主动"来源才允许弹桌面确认卡；浏览器下载事件(source=browser/空)一律静默。
    _ICPT_USER_SOURCES = {
        "menu-link", "menu-image", "menu-selection", "sniff", "popup",
        "chip", "badge", "card", "manual", "ui", "selection", "page",
    }
    _ICPT_DEBOUNCE_MS = 650   # 用户主动短时间抓到多条 → 合并成一张汇总卡

    def _rpc_is_user_source(self, info):
        src = (info.get("source") or "").strip().lower()
        return src in self._ICPT_USER_SOURCES

    def _rpc_is_junk_autolink(self, url, info=None):
        """自动接管链是否属于"用户没主动点就别下"的垃圾：fwlink 等更新器/商店重定向、
        .htm/.html 等网页广告碎片(<256KB)、text/html 内联、<1KB 碎屑。真媒体/大附件返回 False。"""
        info = info or {}
        try:
            from urllib.parse import urlparse
            u = (url or "").strip()
            if not u:
                return True
            pr = urlparse(u)
            host = (pr.hostname or "").lower()
            path = (pr.path or "").lower()
            if host == "go.microsoft.com" or "/fwlink/" in path or path.startswith("/fwlink"):
                return True                       # Windows 安装助手 / 商店重定向，非媒体本体
            try:
                size = int(info.get("size") or 0)
            except Exception:
                size = 0
            ext = path.rsplit(".", 1)[-1] if "." in path else ""
            qn = self._rpc_query_filename(u)
            if not ext and qn and "." in qn:
                ext = qn.rsplit(".", 1)[-1].lower()
            _TEXTY = {"htm", "html", "asp", "aspx", "jsp", "jspx", "php", "cfm", "shtml", "xhtml"}
            if ext in _TEXTY:
                return not (size >= 256 * 1024)   # 网页/碎片：只有 ≥256KB 才当真实附件
            if 0 < size < 1024:
                return True
            mt = (info.get("mime") or info.get("content_type") or "").lower().split(";")[0].strip()
            if not ext and mt in ("text/html", "application/xhtml+xml"):
                return True
            return False
        except Exception:
            return False

    def rpc_intercept(self, info):
        """插件截获一个下载后打到这里。本方法运行在 RPC HTTP 线程，UI 一律经 after 回主线程。
        Bug A 意图分流（根治"没人点嗅探也反复弹桌面大窗"）：
        · 自动接管（source=browser/空，非用户主动点 SniffDL）→ 绝不弹桌面确认大窗：
          垃圾链（fwlink 更新器 / .htm 广告碎片 / text 内联 / <1KB 碎屑）直接忽略并记日志；
          真实媒体/大附件 → 静默入队后台下，完成由看板娘反馈（全局，无需逐条开窗）。
        · 用户主动 + confirm=False（右键小框/菜单快捷）→ 直连：起下载 + 只弹进度窗，不打断浏览。
        · 用户主动 + confirm=True → 短窗口内多条合并成"一张"汇总确认卡（单条仍是原单卡）。
        · 全局关掉 rpc_confirm_dialog：用户主动的也走直连（自动接管本就静默，不受影响）。"""
        info = info or {}
        url = (info.get("url") or "").strip()
        if not url:
            return ""
        want_confirm = info.get("confirm", True)
        if isinstance(want_confirm, str):
            want_confirm = want_confirm.lower() not in ("false", "0", "no")
        user_init = self._rpc_is_user_source(info)
        # —— 自动接管：浏览器下载事件、用户没主动点 SniffDL ——
        if not user_init:
            if self._rpc_is_junk_autolink(url, info):
                self.log("[接管] 忽略自动杂项链（非媒体/更新器/碎片）：%s" % url[:80])
                return ""
            nm = (info.get("filename") or "").strip()
            od = (info.get("outdir") or self.cfg.get("outdir") or DEFAULT_OUTDIR)
            try:
                gid = self._rpc_start_from_info(info, od, nm or None)
            except Exception as e:
                self.log("[接管] 静默入队失败：%s" % str(e)[:120])
                return ""
            self.log("[接管] 静默后台接管（不弹确认卡）：%s ← %s" % (
                (nm or "?")[:40], url[:60]))
            return gid
        # —— 用户主动，但不要确认（快捷直连）——
        if not want_confirm or self.cfg.get("rpc_confirm_dialog", True) is False:
            nm = (info.get("filename") or "").strip()
            od = (info.get("outdir") or self.cfg.get("outdir") or DEFAULT_OUTDIR)
            try:
                gid = self._rpc_start_from_info(info, od, nm or None)
            except Exception as e:
                self.log("[RPC] 快捷直连失败：%s" % str(e)[:120])
                return ""
            disp = nm or (url.rstrip("/").split("/")[-1].split("?")[0] or gid)
            try:
                self.root.after(0, lambda: self._open_progress_window(gid, disp, od))
            except Exception:
                pass
            return gid
        # —— 用户主动 + 需要确认：合并成一张汇总卡（防逐条刷屏）——
        try:
            self.root.after(0, lambda: self._icpt_enqueue(dict(info)))
        except Exception as e:
            self.log("[RPC] 打开确认窗失败，改静默直下：%s" % str(e)[:120])
            try:
                return self._rpc_start_from_info(info, None, None)
            except Exception:
                return ""
        return "pending"

    def _icpt_enqueue(self, info):
        """主线程：把待确认项收进缓冲，重设去抖定时器；一窗口到点统一出卡。"""
        buf = getattr(self, "_icpt_buf", None)
        if buf is None:
            buf = self._icpt_buf = []
        buf.append(info)
        t = getattr(self, "_icpt_timer", None)
        if t is not None:
            try:
                self.root.after_cancel(t)
            except Exception:
                pass
        try:
            self._icpt_timer = self.root.after(self._ICPT_DEBOUNCE_MS, self._icpt_flush)
        except Exception:
            self._icpt_flush()

    def _icpt_flush(self):
        """主线程：去抖到点，把这一批待确认项出卡（1 条=原单卡；多条=一张汇总卡，按归一化 URL 去重）。"""
        self._icpt_timer = None
        buf = getattr(self, "_icpt_buf", None) or []
        self._icpt_buf = []
        if not buf:
            return
        if len(buf) == 1:
            try:
                self._open_intercept_dialog(buf[0])
            except Exception:
                pass
            return
        seen, uniq = set(), []
        for it in buf:
            try:
                k = self._rpc_norm_key(it.get("url") or "") or (it.get("url") or "")
            except Exception:
                k = it.get("url") or ""
            if k in seen:
                continue
            seen.add(k)
            uniq.append(it)
        try:
            self._open_intercept_summary(uniq)
        except Exception as e:
            self.log("[汇总卡] 打开失败：%s" % str(e)[:120])

    def _transient_if_shown(self, win):
        """IDM 三窗（确认/汇总/进度/完成）挂 transient 的前提是主窗看得见。
        【"分发成功但没下"根因之四，11-06 Linux 真机复现】Web 界面模式（ui_mode=web，默认）和托盘启动时
        Tk 主窗是 withdraw 的；Tk 规定 transient 窗"继承主窗状态"→ 确认窗一创建就被跟着隐藏，
        用户永远看不到「开始下载」按钮，sniffdl.intercept 回了 "pending" 就再没下文。
        主窗隐藏时一律不挂 transient（窗口仍 -topmost 置顶），保证确认窗真的弹出来。"""
        try:
            if str(self.root.state()) in ("withdrawn", "iconic") or not self.root.winfo_viewable():
                return False
            win.transient(self.root)
            return True
        except Exception:
            return False

    def _open_intercept_summary(self, items):
        """用户主动一次抓到多条新链接 → 只弹这一张汇总确认卡（勾选要搬的，一次搞定），
        取代旧版"每条弹一张、刷屏"。单条仍走 _open_intercept_dialog（零回归）。"""
        P = self.P
        try:
            if not self.root.winfo_exists():
                return
        except Exception:
            return
        th = THEMES.get(getattr(self, "theme", DEFAULT_TKEY)) or {}
        accent = (C.get("pink") or C.get("purple") or C.get("gold") or "#F77FB0")
        char = th.get("char") or ""
        role = th.get("role") or ""
        base_dir = (self.cfg.get("outdir") or DEFAULT_OUTDIR)
        d = tk.Toplevel(self.root)
        d.title("SniffDL · 批量下载确认")
        d.configure(bg=C["bg"])
        try:
            self._transient_if_shown(d); d.resizable(False, False); d.attributes("-topmost", True)
        except Exception:
            pass

        head = tk.Frame(d, bg=C["bg"])
        head.pack(fill="x", padx=P(18), pady=(P(15), P(2)))
        try:
            av = self._art("AVATAR", self.S * 0.7)
            if av:
                avl = tk.Label(head, image=av, bg=C["bg"])
                avl.image = av; d._sniff_avatar_ref = avl
                avl.pack(side="left", padx=(0, P(10)))
        except Exception:
            pass
        htxt = tk.Frame(head, bg=C["bg"]); htxt.pack(side="left", anchor="w")
        tk.Label(htxt, text=("%s · %s" % (char, role)) if char else "看板娘接管",
                 bg=C["bg"], fg=accent, font=F_UI_B).pack(anchor="w")
        tk.Label(htxt, text=("抓到 %d 条新链接，勾想要的一次搬～" % len(items)),
                 bg=C["bg"], fg=C["ink_l"], font=F_UI).pack(anchor="w", pady=(1, 0))

        loc = tk.Frame(d, bg=C["bg"]); loc.pack(fill="x", padx=P(18), pady=(P(8), P(2)))
        tk.Label(loc, text="保存到", bg=C["bg"], fg=C["ink"], font=F_UI_B).pack(side="left")
        dirvar = tk.StringVar(value=base_dir)
        tk.Entry(loc, textvariable=dirvar, bg=C["card"], fg=C["ink"],
                 insertbackground=C["ink"], relief="flat", font=F_UI).pack(
                     side="left", fill="x", expand=True, padx=P(8), ipady=P(3))

        def browse():
            try:
                sel = filedialog.askdirectory(initialdir=dirvar.get() or base_dir,
                                              parent=d, title="选择保存位置")
                if sel:
                    dirvar.set(os.path.normpath(sel))
            except Exception:
                pass
        RoundButton(loc, text="…", command=browse, kind="ghost",
                    padx=P(10), pady=P(2), font=F_UI_B).pack(side="left")

        listf = tk.Frame(d, bg=C["card"])
        listf.pack(fill="both", expand=True, padx=P(18), pady=P(6))
        cv = tk.Canvas(listf, bg=C["card"], highlightthickness=0,
                       width=P(430), height=P(min(248, 34 * len(items) + 12)))
        sb = tk.Scrollbar(listf, orient="vertical", command=cv.yview)
        inner = tk.Frame(cv, bg=C["card"])
        _win = cv.create_window((0, 0), window=inner, anchor="nw")
        cv.configure(yscrollcommand=sb.set, takefocus=0)
        sb.pack(side="right", fill="y")
        cv.pack(side="left", fill="both", expand=True)

        def _resync(*_):
            try:
                cv.configure(scrollregion=cv.bbox("all"))
                cv.itemconfigure(_win, width=max(cv.winfo_width(), P(430)))
            except Exception:
                pass
        inner.bind("<Configure>", _resync)
        cv.bind("<Configure>", _resync)

        def _wheel(e):
            try:
                cv.yview_scroll(int(-1 * (e.delta / 120)), "units")
            except Exception:
                pass
        cv.bind("<Enter>", lambda _e: cv.bind_all("<MouseWheel>", _wheel))
        cv.bind("<Leave>", lambda _e: cv.unbind_all("<MouseWheel>"))

        rows = []
        for it in items:
            var = tk.BooleanVar(value=True)
            u = it.get("url") or ""
            nm = (it.get("filename") or "").strip()
            if not nm or nm.lower() == "download":
                try:
                    from urllib.parse import urlparse, unquote
                    nm = unquote(os.path.basename(urlparse(u).path)) or \
                        (u.split("?")[0].rstrip("/").split("/")[-1] or "download")
                except Exception:
                    nm = "download"
            sz = it.get("size") or 0
            sz_txt = self._fmt_bytes(sz) if sz else "大小探…"
            rowf = tk.Frame(inner, bg=C["card"]); rowf.pack(fill="x", padx=P(8), pady=P(2))
            tk.Checkbutton(rowf, variable=var, bg=C["card"], activebackground=C["card"],
                           selectcolor=C["bg"], highlightthickness=0).pack(side="left")
            tk.Label(rowf, text=nm, bg=C["card"], fg=C["ink"], font=F_UI,
                     anchor="w", justify="left").pack(side="left", fill="x", expand=True, padx=P(6))
            tk.Label(rowf, text=sz_txt, bg=C["card"], fg=C["ink_l"], font=F_UI).pack(side="right")
            rows.append((var, dict(it), nm))

        sel_count = tk.StringVar(value="已选 %d 项" % len(rows))

        def refresh_count(*_):
            try:
                sel_count.set("已选 %d 项" % sum(1 for v, _x, _y in rows if v.get()))
            except Exception:
                pass
        for v, _x, _y in rows:
            v.trace_add("write", refresh_count)

        def _all(on):
            for v, _x, _y in rows:
                try:
                    v.set(on)
                except Exception:
                    pass
            refresh_count()

        def _close():
            try:
                cv.unbind_all("<MouseWheel>")
            except Exception:
                pass
            try:
                d.destroy()
            except Exception:
                pass

        def _start(defer):
            chosen = [(it, nm) for v, it, nm in rows if v.get()]
            if not chosen:
                return
            od = (dirvar.get() or "").strip() or base_dir
            n = 0
            for it, nm in chosen:
                real = dict(it)
                real["outdir"] = od
                if nm and nm.lower() != "download":
                    real["filename"] = nm
                if defer:
                    real["_defer"] = True
                try:
                    self._rpc_start_from_info(real, od, (nm if nm and nm.lower() != "download" else None))
                    n += 1
                except Exception as e:
                    self.log("[汇总] 起下载失败 %s：%s" % ((nm or "?")[:30], str(e)[:90]))
            self.log("[汇总] %s %d 项（本卡共 %d 项）" % ("稍后" if defer else "开始下载", n, len(rows)))
            try:
                self.say("好～这%d个%s一起搬。" % (n, "回头" if defer else ""))
            except Exception:
                pass
            _close()

        foot = tk.Frame(d, bg=C["bg"]); foot.pack(fill="x", padx=P(18), pady=(P(2), P(14)))
        tk.Label(foot, textvariable=sel_count, bg=C["bg"], fg=C["ink_l"], font=F_UI).pack(side="left")
        RoundButton(foot, text="全选", command=lambda: _all(True), kind="ghost",
                    padx=P(8), pady=P(2), font=F_UI).pack(side="left", padx=(P(6), 0))
        RoundButton(foot, text="清空", command=lambda: _all(False), kind="ghost",
                    padx=P(8), pady=P(2), font=F_UI).pack(side="left")
        RoundButton(foot, text="取消", command=_close, kind="ghost",
                    padx=P(12), pady=P(5), font=F_UI_B).pack(side="right", padx=(P(6), 0))
        RoundButton(foot, text="稍后下载", command=lambda: _start(True), kind="ghost",
                    padx=P(14), pady=P(5), font=F_UI_B).pack(side="right", padx=P(6))
        RoundButton(foot, text="下载选中", command=lambda: _start(False), kind="primary",
                    padx=P(16), pady=P(6), font=F_UI_B).pack(side="right", padx=P(6))
        d.protocol("WM_DELETE_WINDOW", _close)
        try:
            d.lift(); d.focus_force()
        except Exception:
            pass

    def _open_intercept_dialog(self, info):
        P = self.P
        try:
            if not self.root.winfo_exists():
                return
        except Exception:
            return
        url = info.get("url") or ""
        name = (info.get("filename") or "").strip()
        if not name:
            try:
                from urllib.parse import urlparse, unquote
                name = unquote(os.path.basename(urlparse(url).path)) or "download"
            except Exception:
                name = "download"
        base_dir = (self.cfg.get("outdir") or DEFAULT_OUTDIR)
        try:
            os.makedirs(base_dir, exist_ok=True)
        except Exception:
            pass

        th = THEMES.get(getattr(self, "theme", DEFAULT_TKEY)) or {}
        accent = (C.get("pink") or C.get("purple") or C.get("gold") or "#F77FB0")
        char = th.get("char") or ""
        role = th.get("role") or ""
        cats = list(self.cfg.get("rpc_categories") or ["常规"])
        if not cats:
            cats = ["常规"]
        fam = F_UI[0]

        d = tk.Toplevel(self.root)
        d.title("SniffDL · 下载文件信息")
        d.configure(bg=C["bg"])
        try:
            self._transient_if_shown(d)
            d.resizable(False, False)
            d.attributes("-topmost", True)
        except Exception:
            pass

        # ---- 头部：看板娘头像 + 角色 + 说明 ----
        head = tk.Frame(d, bg=C["bg"])
        head.pack(fill="x", padx=P(18), pady=(P(15), P(2)))
        try:
            av = self._art("AVATAR", self.S * 0.9)
            if av:
                avl = tk.Label(head, image=av, bg=C["bg"])
                avl.image = av                       # 持引用防 GC
                d._sniff_avatar_ref = avl
                avl.pack(side="left", padx=(0, P(10)))
        except Exception:
            pass
        htxt = tk.Frame(head, bg=C["bg"])
        htxt.pack(side="left", anchor="w")
        tk.Label(htxt, text=("%s · %s" % (char, role)) if char else "看板娘接管",
                 bg=C["bg"], fg=accent, font=F_UI_B).pack(anchor="w")
        tk.Label(htxt, text="收到浏览器交来的下载，确认一下就搬～",
                 bg=C["bg"], fg=C["ink_l"], font=F_UI).pack(anchor="w", pady=(1, 0))

        # ---- 主体：左表单 + 右预览 ----
        body = tk.Frame(d, bg=C["bg"])
        body.pack(fill="both", expand=True, padx=P(18), pady=P(10))
        form = tk.Frame(body, bg=C["bg"])
        form.pack(side="left", fill="both", expand=True)
        for cwi, w in ((0, P(60)), (1, P(300)), (2, P(40))):
            form.grid_columnconfigure(cwi, minsize=w)

        def cap(r, txt):
            tk.Label(form, text=txt, bg=C["bg"], fg=C["ink"], font=F_UI_B
                     ).grid(row=r, column=0, sticky="w", pady=P(5))

        # URL（只读展示）
        cap(0, "URL")
        urlvar = tk.StringVar(value=url)
        tk.Entry(form, textvariable=urlvar, bg=C["card"], fg=C["ink_l"],
                 insertbackground=C["ink"], relief="flat", font=F_UI, readonlybackground=C["card"]
                 ).grid(row=0, column=1, columnspan=2, sticky="ew", ipady=P(3), pady=P(5))

        # 分类（下拉 + 新建）
        cap(1, "分类")
        catvar = tk.StringVar(value=cats[0])
        bindvar = tk.BooleanVar(value=True)

        # 另存为（文件名）
        cap(3, "另存为")
        namevar = tk.StringVar(value=safe_name(name))
        tk.Entry(form, textvariable=namevar, bg=C["card"], fg=C["ink"],
                 insertbackground=C["ink"], relief="flat", font=F_UI
                 ).grid(row=3, column=1, columnspan=2, sticky="ew", ipady=P(3), pady=P(5))

        # 保存到（目录 + 浏览）
        cap(4, "保存到")
        dirvar = tk.StringVar(value=base_dir)
        tk.Entry(form, textvariable=dirvar, bg=C["card"], fg=C["ink"],
                 insertbackground=C["ink"], relief="flat", font=F_UI
                 ).grid(row=4, column=1, sticky="ew", ipady=P(3), pady=P(5))

        def browse():
            sel = filedialog.askdirectory(initialdir=dirvar.get() or base_dir, parent=d,
                                          title="选择保存位置")
            if sel:
                dirvar.set(os.path.normpath(sel))
        RoundButton(form, text="…", command=browse, kind="ghost",
                    padx=P(10), pady=P(3), font=F_UI_B
                    ).grid(row=4, column=2, sticky="e", padx=(P(6), 0), pady=P(5))

        # 描述（可选，仅本地备注）
        cap(5, "描述")
        descvar = tk.StringVar(value=(info.get("description") or ""))
        tk.Entry(form, textvariable=descvar, bg=C["card"], fg=C["ink"],
                 insertbackground=C["ink"], relief="flat", font=F_UI
                 ).grid(row=5, column=1, columnspan=2, sticky="ew", ipady=P(3), pady=P(5))

        # ---- 右侧预览块（IDM 式：文件图标 + 大号大小）----
        prev = tk.Frame(body, bg=C["card"], highlightbackground=C["line"], highlightthickness=1)
        prev.pack(side="right", fill="y", padx=(P(14), 0))
        glyph = tk.Canvas(prev, width=P(128), height=P(140), bg=C["card"], highlightthickness=0)
        glyph.pack(padx=P(12), pady=(P(16), P(2)))
        sizevar = tk.StringVar(value="…")
        extvar = tk.StringVar(value="")
        tk.Label(prev, textvariable=sizevar, bg=C["card"], fg=C["ink"],
                 font=(fam, 16, "bold")).pack(pady=(P(2), P(0)))
        tk.Label(prev, textvariable=extvar, bg=C["card"], fg=C["ink_l"],
                 font=F_UI).pack(pady=(P(1), P(14)))

        def draw_glyph(ext):
            glyph.delete("all")
            w, h = P(128), P(140)
            x1, y1, x2, y2 = P(34), P(14), w - P(30), h - P(22)
            fold = P(26)
            pts = [x1, y1, x2 - fold, y1, x2, y1 + fold, x2, y2, x1, y2]
            glyph.create_polygon(pts, smooth=True, fill=C["bg"], outline=accent, width=2)
            glyph.create_line(x2 - fold, y1, x2 - fold, y1 + fold, x2, y1 + fold,
                              fill=accent, width=2)
            lab = (ext or "?").upper()[:5]
            glyph.create_text((x1 + x2) // 2, (y1 + y2) // 2 + P(8), text=lab,
                              fill=accent, font=(fam, 15, "bold"))

        def ext_of(nm):
            e = (os.path.splitext(nm or "")[1] or "").lstrip(".").lower()
            return e

        draw_glyph(ext_of(name))
        extvar.set(ext_of(name) and ("类型：." + ext_of(name)) or "类型：未知")

        # ---- 分类联动：选分类 → 保存到 base/分类（勾选时）----
        def apply_cat(*_):
            try:
                if bindvar.get():
                    cat = (catvar.get() or "").strip()
                    dirvar.set(os.path.join(base_dir, cat) if cat else base_dir)
            except Exception:
                pass

        catmenu = tk.Frame(form, bg=C["bg"])
        catmenu.grid(row=1, column=1, columnspan=2, sticky="ew", pady=P(5))
        om = tk.OptionMenu(catmenu, catvar, *cats, command=apply_cat)
        om.config(bg=C["card"], fg=C["ink"], activebackground=accent,
                  activeforeground=C["bg"], relief="flat", font=F_UI,
                  highlightthickness=0)
        om.config(menu=tk.Menu(om, bg=C["card"], fg=C["ink"], activebackground=accent,
                               activeforeground=C["bg"], font=F_UI, tearoff=0))
        om.pack(side="left", fill="x", expand=True)

        def add_cat():
            try:
                from tkinter import simpledialog
                nm = simpledialog.askstring("新建分类", "给新分类起个名字：",
                                            parent=d, initialvalue="")
            except Exception:
                nm = None
            nm = (nm or "").strip()
            if not nm or nm in cats:
                return
            cats.append(nm)
            try:
                om["menu"].add_command(label=nm,
                                       command=lambda v=nm: (catvar.set(v), apply_cat()))
            except Exception:
                pass
            try:
                self.cfg["rpc_categories"] = list(cats)
                save_config(self.cfg)
            except Exception:
                pass
            catvar.set(nm)
            apply_cat()
        RoundButton(catmenu, text="＋", command=add_cat, kind="ghost",
                    padx=P(10), pady=P(3), font=F_UI_B
                    ).pack(side="right", padx=(P(6), 0))

        # 让该分类使用该路径
        tk.Checkbutton(form, text="让该分类使用该路径", variable=bindvar, command=apply_cat,
                       bg=C["bg"], fg=C["ink_l"], activebackground=C["bg"],
                       activeforeground=C["ink"], selectcolor=C["card"],
                       highlightthickness=0, font=F_UI
                       ).grid(row=2, column=1, sticky="w", pady=P(2))
        apply_cat()

        # ---- 大小（异步探，喂右侧大数字）+ 无后缀名纠正 ----
        size0 = info.get("size")
        _nm_cur = (info.get("filename") or name or "").strip()
        _name_needs_fix = not os.path.splitext(_nm_cur)[1]
        if size0 and not _name_needs_fix:
            sizevar.set(self._fmt_bytes(size0))
        else:
            def probe():
                hdrs = {}
                final = url
                try:
                    hh = {}
                    if info.get("referer"):
                        hh["Referer"] = info["referer"]
                    if info.get("cookie"):
                        hh["Cookie"] = info["cookie"]
                    if info.get("ua"):
                        hh["User-Agent"] = info["ua"]
                    r = SESSION.head(url, headers=hh, timeout=15, allow_redirects=True, verify=False)
                    cl = r.headers.get("content-length")
                    hdrs = r.headers or {}
                    final = getattr(r, "url", None) or url
                    if not cl:
                        r2 = SESSION.get(url, headers=hh, timeout=20, stream=True, verify=False)
                        cl = r2.headers.get("content-length")
                        hdrs = r2.headers or hdrs
                        final = getattr(r2, "url", None) or final
                        try:
                            r2.close()
                        except Exception:
                            pass
                    txt = self._fmt_bytes(int(cl)) if cl else "未知"
                except Exception:
                    txt = "未知"
                # 顺带用响应头纠正"另存为"名 + 类型（修 Bug B：download 无后缀 / 类型未知）
                def fix_name():
                    try:
                        if not d.winfo_exists():
                            return
                        cur = (namevar.get() or "").strip()
                        if cur and cur.lower() != "download" and os.path.splitext(cur)[1]:
                            return  # 名字已带真后缀，别多事
                        cd_fn = self._rpc_name_from_headers(hdrs, final)
                        ext = self._rpc_ext_from_headers(hdrs, final)
                        newnm = ""
                        if cd_fn and os.path.splitext(cd_fn)[1]:
                            newnm = safe_name(cd_fn)
                        elif ext:
                            newnm = safe_name((cur or "download") + ext)
                        if newnm and newnm != cur:
                            namevar.set(newnm)
                    except Exception:
                        pass
                try:
                    self.root.after(0, fix_name)
                except Exception:
                    pass
                try:
                    if (not size0) and d.winfo_exists():
                        sizevar.set(txt)
                except Exception:
                    pass
            threading.Thread(target=probe, daemon=True).start()

        # 文件名改动时同步右侧类型/图标
        def on_name(*_):
            e = ext_of(namevar.get())
            draw_glyph(e)
            extvar.set(("类型：." + e) if e else "类型：未知")
        namevar.trace_add("write", on_name)

        def _close():
            try:
                d.destroy()
            except Exception:
                pass

        def start_now():
            nm = (namevar.get() or "").strip() or "download"
            od = (dirvar.get() or "").strip() or base_dir
            real = dict(info)
            real["filename"] = nm
            real["outdir"] = od
            real["description"] = (descvar.get() or "").strip()
            real.pop("_defer", None)
            try:
                gid = self._rpc_start_from_info(real, od, nm)
            except Exception as e:
                self.log("[RPC] 接管起下载失败：%s" % str(e)[:140])
                _close()
                return
            _close()
            self._open_progress_window(gid, nm, od)

        def later():
            nm = (namevar.get() or "").strip() or "download"
            od = (dirvar.get() or "").strip() or base_dir
            real = dict(info)
            real["filename"] = nm
            real["outdir"] = od
            real["description"] = (descvar.get() or "").strip()
            real["_defer"] = True
            try:
                gid = self._rpc_start_from_info(real, od, nm)
                self.log("[接管] 已存入稍后队列：%s（可稍后从下载列表或 RPC 启动）" % nm)
                self.say("好～「%s」先放一放，回头再搬。" % nm)
            except Exception as e:
                self.log("[RPC] 稍后下载登记失败：%s" % str(e)[:140])
            _close()

        btns = tk.Frame(d, bg=C["bg"])
        btns.pack(pady=(P(4), P(16)))
        RoundButton(btns, text="稍后下载", command=later, kind="ghost",
                    padx=P(14), pady=P(6)).pack(side="left", padx=P(6))
        RoundButton(btns, text="开始下载", command=start_now, kind="primary",
                    padx=P(16), pady=P(6)).pack(side="left", padx=P(6))
        RoundButton(btns, text="取消", command=_close, kind="ghost",
                    padx=P(12), pady=P(6)).pack(side="left", padx=P(6))
        d.protocol("WM_DELETE_WINDOW", _close)
        # 键盘直达：回车=开始下载，Esc=取消（像 IDM 一样不用找按钮；文件名框里回车同样生效）
        try:
            d.bind("<Return>", lambda e: start_now())
            d.bind("<KP_Enter>", lambda e: start_now())
            d.bind("<Escape>", lambda e: _close())
        except Exception:
            pass
        try:
            d.lift()
            d.focus_force()
        except Exception:
            pass

    def _open_progress_window(self, gid, name, outdir):
        P = self.P
        prev = self._rpc_prog.get(gid)
        try:
            if prev is not None and prev.winfo_exists():
                prev.lift()
                return
        except Exception:
            pass
        accent = (C.get("pink") or C.get("purple") or C.get("gold") or "#F77FB0")
        fam = F_UI[0]
        d = tk.Toplevel(self.root)
        self._rpc_prog[gid] = d
        d.title("SniffDL · 正在下载")
        d.configure(bg=C["bg"])
        try:
            self._transient_if_shown(d)
            d.resizable(False, False)
        except Exception:
            pass

        top = tk.Frame(d, bg=C["bg"])
        top.pack(fill="x", padx=P(18), pady=(P(15), P(4)))
        tk.Label(top, text="正在搬运", bg=C["bg"], fg=accent, font=F_UI_B).pack(anchor="w")
        tk.Label(top, text=name, bg=C["bg"], fg=C["ink"], font=F_UI,
                 wraplength=P(380), justify="left").pack(anchor="w", pady=(2, 0))

        bar = ThemedBar(d, self.S)
        bar.pack(fill="x", padx=P(18), pady=(P(14), P(6)))
        bar.start()

        stat = tk.Frame(d, bg=C["bg"])
        stat.pack(fill="x", padx=P(18))
        pctvar = tk.StringVar(value="0%")
        spdvar = tk.StringVar(value="—")
        tk.Label(stat, textvariable=pctvar, bg=C["bg"], fg=C["ink"], font=F_UI_B).pack(side="left")
        tk.Label(stat, textvariable=spdvar, bg=C["bg"], fg=C["ink_l"], font=F_UI).pack(side="right")

        # ---- 可展开详情区（IDM「隐藏细节 / 显示细节」）----
        det = tk.Frame(d, bg=C["bg"])
        dv = {}
        for key in ("status", "total", "done", "spd", "eta", "resume", "src", "dst"):
            dv[key] = tk.StringVar(value="—")

        def drow(r, label, var, wrap=False):
            tk.Label(det, text=label, bg=C["bg"], fg=C["ink_l"], font=F_UI
                     ).grid(row=r, column=0, sticky="w", pady=P(2), padx=(0, P(10)))
            lb = tk.Label(det, textvariable=var, bg=C["bg"], fg=C["ink"], font=F_UI,
                          anchor="w", justify="left")
            if wrap:
                lb.config(wraplength=P(300))
            lb.grid(row=r, column=1, sticky="w", pady=P(2))
        drow(0, "下载状态", dv["status"])
        drow(1, "文件大小", dv["total"])
        drow(2, "已经下载", dv["done"])
        drow(3, "传输速度", dv["spd"])
        drow(4, "剩余时间", dv["eta"])
        drow(5, "能否续传", dv["resume"])
        drow(6, "来源", dv["src"], wrap=True)
        drow(7, "保存到", dv["dst"], wrap=True)

        state = {"job": None, "path": "", "done": False, "detail": False,
                 "toggle": None, "paused": False}

        def toggle_detail():
            if state["detail"]:
                det.pack_forget()
                state["detail"] = False
                if state["toggle"]:
                    state["toggle"].config(text="显示细节 »")
            else:
                det.pack(fill="x", padx=P(18), pady=(P(6), P(2)))
                state["detail"] = True
                if state["toggle"]:
                    state["toggle"].config(text="« 隐藏细节")

        foot = tk.Frame(d, bg=C["bg"])
        foot.pack(pady=(P(6), P(14)))
        state["toggle"] = RoundButton(foot, text="显示细节 »", command=toggle_detail,
                                      kind="ghost", padx=P(12), pady=P(6))
        state["toggle"].pack(side="left", padx=P(6))

        def cleanup():
            if state["job"]:
                try:
                    d.after_cancel(state["job"])
                except Exception:
                    pass
                state["job"] = None
            self._rpc_prog.pop(gid, None)

        def do_cancel():
            try:
                self.rpc_remove(gid)
            except Exception:
                pass
            cleanup()
            try:
                d.destroy()
            except Exception:
                pass

        def do_pause():
            try:
                if state["paused"]:
                    self.rpc_resume(gid)
                    state["paused"] = False
                    pause_btn.config(text="暂停")
                else:
                    self.rpc_pause(gid)
                    state["paused"] = True
                    pause_btn.config(text="继续")
            except Exception:
                pass
        pause_btn = RoundButton(foot, text="暂停", command=do_pause, kind="ghost",
                                padx=P(12), pady=P(6))
        pause_btn.pack(side="left", padx=P(6))
        RoundButton(foot, text="取消下载", command=do_cancel, kind="ghost",
                    padx=P(14), pady=P(6)).pack(side="left", padx=P(6))

        def _eta(total, done, spd):
            if total and done is not None and spd and spd > 0:
                sec = max(0, int((total - done) / spd))
                if sec >= 3600:
                    return "%d:%02d:%02d" % (sec // 3600, (sec % 3600) // 60, sec % 60)
                return "%02d:%02d" % (sec // 60, sec % 60)
            return "未知"

        def fill_detail(st, total, done, spd, status):
            dv["total"].set(self._fmt_bytes(total) if total else "未知")
            dv["done"].set(self._fmt_bytes(done))
            dv["spd"].set(self._fmt_bytes(spd) + "/s")
            dv["eta"].set(_eta(total, done, spd))
            dv["resume"].set("是（断点续传）")
            uri = st.get("uri") or ""
            dv["src"].set(uri[:80] + ("…" if len(uri) > 80 else ""))
            dv["dst"].set(os.path.join(st.get("dir") or outdir or "", st.get("name") or name)[:80])
            if status == RPC.ST_PAUSED:
                dv["status"].set("已暂停")
            elif not total and spd == 0:
                dv["status"].set("正在连接中…")
            elif total and done * 100 // max(total, 1) >= 99:
                dv["status"].set("即将完成…")
            else:
                dv["status"].set("下载中…")

        def tick():
            state["job"] = None
            try:
                if not d.winfo_exists():
                    return
            except Exception:
                return
            st = self.rpc_struct(gid)
            if st is None:
                cleanup()
                try:
                    d.destroy()
                except Exception:
                    pass
                return
            status = st.get("status")
            total = int(st.get("totalLength") or 0)
            done = int(st.get("downloadedLength") or 0)
            spd = int(st.get("downloadSpeed") or 0)
            if total > 0:
                try:
                    bar.stop()
                    bar.config(mode="determinate", value=done * 100.0 / total)
                except Exception:
                    pass
                pctvar.set("%d%%  ·  %s / %s" % (
                    int(done * 100 / total), self._fmt_bytes(done), self._fmt_bytes(total)))
            else:
                pctvar.set(self._fmt_bytes(done) + "  ·  大小未知")
            spdvar.set(self._fmt_bytes(spd) + "/s")
            if state["detail"]:
                fill_detail(st, total, done, spd, status)

            if status == RPC.ST_COMPLETE:
                state["done"] = True
                path = st.get("path") or ""
                cleanup()
                try:
                    d.destroy()
                except Exception:
                    pass
                if path:
                    self._open_done_window(path, os.path.basename(path) or name)
                return
            if status == RPC.ST_ERROR:
                cleanup()
                try:
                    bar.stop()
                    pctvar.set("下载失败")
                    spdvar.set((st.get("errorMessage") or "")[:40])
                    dv["status"].set("失败")
                except Exception:
                    pass
                try:
                    d.after(2600, d.destroy)
                except Exception:
                    pass
                return
            if status == RPC.ST_PAUSED:
                # 暂停：不关窗，保留轮询，允许「继续」即时恢复
                state["paused"] = True
                try:
                    bar.stop()
                    pctvar.set("已暂停")
                    spdvar.set("—")
                except Exception:
                    pass
                try:
                    pause_btn.config(text="继续")
                except Exception:
                    pass
                if state["detail"]:
                    fill_detail(st, total, done, spd, status)
                state["job"] = d.after(500, tick)
                return
            state["job"] = d.after(400, tick)

        state["job"] = d.after(400, tick)
        try:
            d.lift()
        except Exception:
            pass

    def _open_done_window(self, path, name):
        P = self.P
        accent = (C.get("pink") or C.get("purple") or C.get("gold") or "#F77FB0")
        d = tk.Toplevel(self.root)
        d.title("SniffDL · 下载完毕")
        d.configure(bg=C["bg"])
        try:
            self._transient_if_shown(d)
            d.resizable(False, False)
            d.attributes("-topmost", True)
        except Exception:
            pass
        head = tk.Frame(d, bg=C["bg"])
        head.pack(fill="x", padx=P(18), pady=(P(15), P(4)))
        tk.Label(head, text="✓ 下载完毕", bg=C["bg"], fg="#2E9E5B", font=F_UI_B).pack(anchor="w")
        tk.Label(head, text=name, bg=C["bg"], fg=C["ink"], font=F_UI,
                 wraplength=P(360), justify="left").pack(anchor="w", pady=(2, 0))
        tk.Label(d, text=path, bg=C["bg"], fg=C["ink_l"], font=F_UI,
                 wraplength=P(360), justify="left").pack(anchor="w", padx=P(18), pady=(P(2), P(10)))

        def close():
            try:
                d.destroy()
            except Exception:
                pass

        def open_file():
            try:
                os.startfile(path)
            except Exception as e:
                self.log("[接管] 打开文件失败：%s" % str(e)[:120])
            close()

        def open_dir():
            try:
                self._reveal_in_explorer(path)
            except Exception:
                pass
            close()

        foot = tk.Frame(d, bg=C["bg"])
        foot.pack(pady=(P(2), P(16)))
        RoundButton(foot, text="打开", command=open_file, kind="primary",
                    padx=P(16), pady=P(6)).pack(side="left", padx=P(6))
        RoundButton(foot, text="打开文件夹", command=open_dir, kind="ghost",
                    padx=P(14), pady=P(6)).pack(side="left", padx=P(6))
        RoundButton(foot, text="关闭", command=close, kind="ghost",
                    padx=P(12), pady=P(6)).pack(side="left", padx=P(6))
        d.protocol("WM_DELETE_WINDOW", close)
        try:
            d.lift()
            d.focus_force()
        except Exception:
            pass

    # ---- 浏览器浮标：把页面 URL 交给桌面端猫爪深度嗅探（异步任务 + 轮询）----
    def _sniff_item_from_entry(self, e, page_url, idx):
        """把一条嗅探结果压成浮标面板要用的紧凑结构。"""
        import os as _os
        from urllib.parse import urlparse as _up, unquote as _unq
        u = e.get("variant") or e.get("url") or ""
        base = ""
        try:
            base = _unq(_os.path.basename(_up(u).path)) or ""
        except Exception:
            base = ""
        size = int(e.get("size") or 0)
        return {
            "idx": idx,
            "title": (e.get("title") or base or "video"),
            "ext": (e.get("ext") or "-"),
            "res": (e.get("res") or "-"),
            "size": size,
            "size_s": (self._fmt_bytes(size) if size else "—"),
            "dur": (e.get("duration") or 0),
            "how": (e.get("how") or ""),
            "url": u,
            "page": (e.get("_page") or page_url),
        }

    def rpc_sniff_start(self, url, scope="all"):
        """浮标点开时调用：起一次后台深度嗅探，立刻返回 task_id（不阻塞插件）。
        同一 URL 正在嗅探时复用同一任务，避免重复拉起浏览器实例。"""
        url = (url or "").strip()
        if not url:
            return ""
        if scope not in ("all", "page"):
            scope = "all"
        for tid, t in self._rpc_sniff.items():
            if t.get("url") == url and t.get("status") == "running":
                return tid
        try:
            import uuid
            tid = uuid.uuid4().hex[:12]
        except Exception:
            tid = "%x" % (int(time.time() * 1000) & 0xffffffff)
        self._rpc_sniff[tid] = {"status": "running", "url": url, "items": [],
                                "error": "", "t0": time.time()}
        # 轻量剪枝：完成的旧任务只保留最近 24 条
        done = [k for k, v in self._rpc_sniff.items() if v.get("status") != "running"]
        if len(done) > 24:
            for k in done[:len(done) - 24]:
                self._rpc_sniff.pop(k, None)

        def worker():
            try:
                merged = self._sniff_pipeline(url, scope=scope, emit=False) or []
                items = [self._sniff_item_from_entry(e, url, i + 1)
                         for i, e in enumerate(merged)]
                t = self._rpc_sniff.get(tid)
                if t is not None:
                    t["items"] = items
                    t["status"] = "done"
                if items:
                    try:
                        title = ""
                        try:
                            title = (getattr(self, "current_title", "") or "").strip()
                        except Exception:
                            title = ""
                        if not title:
                            from urllib.parse import urlparse
                            title = urlparse(url).netloc or url
                        rs = [{"url": url, "title": title[:60], "count": len(items)}] + \
                             [r for r in (self.cfg.get("recent_sniff") or [])
                              if (r or {}).get("url") != url]
                        self.cfg["recent_sniff"] = rs[:15]
                        save_config(self.cfg)
                    except Exception:
                        pass
            except Exception as ex:
                t = self._rpc_sniff.get(tid)
                if t is not None:
                    t["status"] = "error"
                    t["error"] = str(ex)[:180]
        threading.Thread(target=worker, daemon=True).start()
        return tid

    def rpc_sniff_result(self, task_id):
        t = self._rpc_sniff.get(task_id)
        if t is None:
            return {"status": "not_found", "url": "", "items": [], "error": ""}
        return {"status": t.get("status"), "url": t.get("url"),
                "items": t.get("items") or [], "error": t.get("error") or "",
                "elapsed": round(time.time() - (t.get("t0") or time.time()), 1)}

    # ============ 网页中央「下载文件信息 / 进度」卡所需 RPC（桌面不再弹原生窗）============
    def rpc_dl_defaults(self, url):
        """给网页卡回：分类列表 + 基础目录 + 建议文件名 + 扩展名，让页面复刻 IDM 式确认卡。"""
        base_dir = (self.cfg.get("outdir") or DEFAULT_OUTDIR)
        cats = list(self.cfg.get("rpc_categories") or ["常规"]) or ["常规"]
        name = ""
        try:
            from urllib.parse import urlparse, unquote
            name = unquote(os.path.basename(urlparse(url or "").path)) or ""
        except Exception:
            name = ""
        ext = (os.path.splitext(name)[1] or "").lstrip(".").lower()
        recents = []
        for d in ([base_dir] + list(self.cfg.get("recent_dirs") or [])
                  + [os.path.join(base_dir, c) for c in cats]):
            d = (d or "").strip()
            if d and d not in recents:
                recents.append(d)
        return {"ok": True, "categories": cats, "base_dir": base_dir,
                "recent_dirs": recents[:8], "last_cat": (self.cfg.get("rpc_last_category") or ""),
                "suggested_name": (safe_name(name) if name else ""), "ext": ext}

    def rpc_add_category(self, name):
        name = (name or "").strip()
        if not name:
            return {"ok": False, "categories": list(self.cfg.get("rpc_categories") or ["常规"])}
        cats = list(self.cfg.get("rpc_categories") or ["常规"])
        if name not in cats:
            cats.append(name)
            self.cfg["rpc_categories"] = cats
            try:
                save_config(self.cfg)
            except Exception:
                pass
        return {"ok": True, "categories": cats}

    def rpc_pick_dir(self, title):
        """网页卡点「浏览」→ 桌面弹原生文件夹选择器，选完把绝对路径回给页面。
        运行在 RPC HTTP 线程，UI 经 after 回主线程，用 Event 阻塞等待结果。"""
        box = {"path": None}
        ev = threading.Event()

        def _do():
            try:
                sel = filedialog.askdirectory(title=(title or "选择保存位置"))
                box["path"] = os.path.normpath(sel) if sel else ""
            except Exception:
                box["path"] = ""
            ev.set()
        try:
            self.root.after(0, _do)
        except Exception:
            return {"ok": False, "path": ""}
        ev.wait(timeout=180)
        return {"ok": True, "path": (box["path"] or "")}

    def rpc_list_dirs(self, path):
        """网页内文件夹浏览器后端：给定路径返回盘符/上级/子目录名，让卡片里逐层选，不切窗口。"""
        import os as _os, string
        out = {"ok": True, "path": "", "parent": "", "dirs": [], "drives": []}
        try:
            path = (path or "").strip()
            if not path:
                out["drives"] = [_os.path.join(s + ":\\") for s in string.ascii_uppercase
                                 if _os.path.exists(s + ":\\")]
                return out
            path = _os.path.abspath(path)
            out["path"] = path
            parent = _os.path.dirname(path)
            # 盘符根（dirname 返回自身，如 C:\）→ 无上级，回盘符列表
            out["parent"] = parent if (parent and parent != path) else ""
            subs = []
            with _os.scandir(path) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            nm = e.name
                            low = nm.lower()
                            if nm.startswith(".") or "system volume" in low or low in ("$recycle.bin",):
                                continue
                            subs.append(nm)
                    except Exception:
                        continue
            subs.sort(key=lambda x: x.lower())
            out["dirs"] = subs[:500]
        except Exception as ex:
            out["ok"] = False
            out["error"] = str(ex)[:120]
        return out

    def rpc_add(self, info):
        """网页卡点「开始下载」→ 桌面按最终目录/文件名起下载，返回 gid，绝不弹任何桌面窗。
        目录优先级：显式 outdir > (让该分类使用该路径时 base_dir/分类) > base_dir。"""
        info = info or {}
        url = (info.get("url") or "").strip()
        if not url:
            return ""
        base_dir = (self.cfg.get("outdir") or DEFAULT_OUTDIR)
        cat = (info.get("category") or "").strip()
        use_cat = info.get("use_cat_dir", True)
        if isinstance(use_cat, str):
            use_cat = use_cat.lower() not in ("false", "0", "no")
        od = (info.get("outdir") or "").strip()
        if not od:
            od = os.path.join(base_dir, cat) if (use_cat and cat) else base_dir
        try:
            os.makedirs(od, exist_ok=True)
        except Exception:
            pass
        nm = (info.get("filename") or "").strip()
        try:
            # 嗅探条目自带的 source 是"这条是从哪儿抓到的"（m3u8/yt-dlp…），不是"谁按的下载"。
            # 走到 rpc_add 就是主人在网页卡上点了下载 → 一律按用户主动记（记忆系统只恢复 user）。
            info = dict(info)
            info["source"] = "ui"
            gid = self._rpc_start_from_info(info, od, nm or None)
        except Exception as e:
            self.log("[RPC] 网页接管起下载失败：%s" % str(e)[:140])
            return ""
        if gid:
            try:
                recents = [od] + [d for d in (self.cfg.get("recent_dirs") or []) if d != od]
                self.cfg["recent_dirs"] = recents[:8]
                if cat:
                    self.cfg["rpc_last_category"] = cat
                save_config(self.cfg)
            except Exception:
                pass
        return gid

    def _rpc_path_of(self, gid):
        """解析某 gid 的最终文件绝对路径 + 所在目录（完成前可能只有目录）。"""
        t = self.rpc_struct(gid) or {}
        d = t.get("dir") or ""
        fp = t.get("path") or ""
        nm = t.get("name") or ""
        if not fp and d and nm:
            fp = os.path.join(d, nm)
        return fp, (d or (os.path.dirname(fp) if fp else ""))

    def rpc_reveal(self, gid):
        """网页卡「打开文件夹」→ 资源管理器打开并高亮该文件；未完成则打开所在目录。"""
        full, d = self._rpc_path_of(gid)
        try:
            if full and os.path.exists(full):
                self._reveal_in_explorer(full)
                return {"ok": True, "path": full}
            if d and os.path.isdir(d):
                os.startfile(d)
                return {"ok": True, "path": d}
            return {"ok": False, "path": full or d or ""}
        except Exception as e:
            return {"ok": False, "error": str(e)[:120], "path": full or d or ""}

    def rpc_open_file(self, gid):
        """网页卡「打开文件」→ 用系统默认程序打开已下载文件。"""
        full, d = self._rpc_path_of(gid)
        try:
            if full and os.path.exists(full):
                os.startfile(full)
                return {"ok": True}
            if d and os.path.isdir(d):
                os.startfile(d)
                return {"ok": True}
            return {"ok": False}
        except Exception as e:
            return {"ok": False, "error": str(e)[:120]}

    def rpc_recent_sniff(self):
        """返回最近嗅探到有货的页面列表（url+title+count），弹窗用作记忆入口。"""
        rs = list(self.cfg.get("recent_sniff") or [])
        return {"ok": True, "items": rs, "base_dir": (self.cfg.get("outdir") or DEFAULT_OUTDIR),
                "recent_dirs": list(self.cfg.get("recent_dirs") or [])}

    def rpc_clear_recent_sniff(self):
        try:
            self.cfg["recent_sniff"] = []
            save_config(self.cfg)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)[:120]}

    # ---- Web UI（路线B 前端）专用：读/写设置、打开目录、值班室统计 ----
    def rpc_get_settings(self):
        """给 Web UI 回显当前设置（只暴露界面要用的键；本地服务、非机密）。"""
        c = self.cfg
        keys = ("quality", "dl_threads", "dash_aria2", "proxy_mode", "proxy_manual",
                "cookie_browser", "sniff_browser", "after", "rx_cap_gb", "outdir", "theme",
                "rpc_enable", "rpc_port", "rpc_token", "deep", "pet_live", "pet_skin",
                "cast24_legacy", "tray_close", "ui_mode", "ui_bg",
                "rt_enable", "rt_base_url", "rt_api_key", "rt_model", "rt_instructions")
        out = {k: c.get(k) for k in keys}
        try:
            out["autostart"] = bool(self.get_autostart())
        except Exception:
            out["autostart"] = False
        out["categories"] = list(c.get("rpc_categories") or [])
        return {"ok": True, "settings": out}

    def rpc_set_setting(self, key, value):
        """Web UI 改一项设置：写 cfg + 落盘 + 尽量即时生效。
        需要碰 Tk 的（换肤）走 self.after 回主线程；任何异常都不许崩掉 RPC 线程。"""
        ALLOW = {"quality", "dl_threads", "dash_aria2", "proxy_mode", "proxy_manual",
                 "cookie_browser", "sniff_browser", "after", "rx_cap_gb", "outdir", "deep", "pet_skin", "pet_live",
                 "theme", "rpc_enable", "rpc_port", "rpc_token", "cast24_legacy",
                 "tray_close", "autostart", "ui_mode", "ui_bg",
                 "rt_enable", "rt_base_url", "rt_api_key", "rt_model", "rt_instructions"}
        if key not in ALLOW:
            return {"ok": False, "error": "key not allowed: %s" % key}
        try:
            if key == "autostart":
                try:
                    self.set_autostart(bool(value))
                    return {"ok": True, "key": key}
                except Exception as e:
                    return {"ok": False, "error": str(e)[:120]}
            self.cfg[key] = value
            save_config(self.cfg)
            # 纯逻辑镜像，即时生效（不碰 Tk）
            try:
                if key == "dash_aria2":
                    DASH_ARIA2[0] = bool(value)
                elif key == "dl_threads":
                    v = str(value or "自动").strip()
                    globals()["DL_THREADS"] = 0 if v in ("自动", "auto", "") else int(v)
                elif key == "sniff_browser":
                    SNIFF_BROWSER[0] = str(value or "自动")
                elif key == "pet_live":
                    # 设置里「显示桌宠」：开 = 拉起/显示大肥鱼，关 = 收起（宿主进程保留，再开秒回）
                    on = bool(value)
                    if on and not getattr(self, "_pet", None):
                        import pet_bridge
                        self._pet = pet_bridge.PetBridge(port=int(self.cfg.get("pet_port") or 6801), wake=True)
                        self._pet.start()
                    self.pet_live = on or bool(getattr(self, "_pet", None))
                    if getattr(self, "_pet", None):
                        self._pet_visible = on
                        self._pet.visible(on)
                elif key == "rx_cap_gb":
                    self._rx_cap_gb = float(value or 0)
                elif key == "proxy_mode":
                    set_proxy_setting(value, self.cfg.get("proxy_manual", ""))
            except Exception:
                pass
            # 需要回主线程应用的（换肤），尽力而为，失败不影响已落盘的配置
            try:
                if key == "theme":
                    self.after(0, lambda: self.apply_theme(value, save=False))
            except Exception:
                pass
            return {"ok": True, "key": key, "value": value}
        except Exception as e:
            return {"ok": False, "error": str(e)[:160]}

    def rpc_test_gateway(self, base_url="", api_key="", model=""):
        """测网关大脑连通：GET /models 看可达+数量+模型是否存在；POST /responses 看 s2s 真正用的那条能不能通。"""
        import urllib.request, urllib.error
        import json as _json
        base = (base_url or self.cfg.get("rt_base_url") or "").strip().rstrip("/")
        key = (api_key or self.cfg.get("rt_api_key") or "").strip()
        mdl = (model or self.cfg.get("rt_model") or "").strip()
        if not base:
            return {"ok": False, "error": "base_url 为空"}
        res = {"ok": False, "base": base, "model": mdl}

        def _req(url, data=None, timeout=12):
            h = {"Content-Type": "application/json"}
            if key:
                h["Authorization"] = "Bearer " + key
            body = _json.dumps(data).encode() if data is not None else None
            rq = urllib.request.Request(url, data=body, headers=h,
                                        method=("POST" if data is not None else "GET"))
            with urllib.request.urlopen(rq, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")

        # 1) /models —— 确认网关可达 + 列出可用模型
        try:
            st, txt = _req(base + "/models")
            try:
                ids = [m.get("id") for m in _json.loads(txt).get("data", [])]
            except Exception:
                ids = []
            res["models_http"] = st
            res["models_count"] = len(ids)
            res["models_sample"] = ids[:12]
            if mdl and ids:
                res["model_exists"] = mdl in ids
        except urllib.error.HTTPError as e:
            res["models_http"] = e.code
            try:
                res["models_err"] = e.read().decode("utf-8", "replace")[:160]
            except Exception:
                res["models_err"] = str(e)[:160]
        except Exception as e:
            res["models_err"] = str(e)[:160]

        # 2) /responses —— s2s 大脑实际用的那条，只有它 200 才算真通
        if mdl:
            try:
                st, txt = _req(base + "/responses",
                               {"model": mdl, "input": "ping", "stream": False})
                res["responses_http"] = st
                res["responses_preview"] = txt[:200]
                if st == 200:
                    res["ok"] = True
            except urllib.error.HTTPError as e:
                res["responses_http"] = e.code
                try:
                    res["responses_err"] = e.read().decode("utf-8", "replace")[:200]
                except Exception:
                    res["responses_err"] = str(e)[:200]
            except Exception as e:
                res["responses_err"] = str(e)[:200]
        return res

    def rpc_list_models(self, base_url="", api_key=""):
        """拉网关 /v1/models 返回模型 id 列表，供设置里模型下拉刷新（模型增减也能看到）。"""
        import urllib.request
        import json as _json
        base = (base_url or self.cfg.get("rt_base_url") or "").strip().rstrip("/")
        key = (api_key or self.cfg.get("rt_api_key") or "").strip()
        if not base:
            return {"ok": False, "error": "base_url 为空"}
        try:
            h = {"Content-Type": "application/json"}
            if key:
                h["Authorization"] = "Bearer " + key
            rq = urllib.request.Request(base + "/models", headers=h, method="GET")
            with urllib.request.urlopen(rq, timeout=12) as r:
                data = _json.loads(r.read().decode("utf-8", "replace"))
            ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
            return {"ok": True, "models": ids, "count": len(ids)}
        except Exception as e:
            return {"ok": False, "error": str(e)[:160]}

    # ---- Web UI 专用：Cookie 登录/抓取 + 组件自动更新（后端本就有，之前没经 RPC 暴露）----
    def rpc_cookie_info(self):
        try:
            stored = len(read_app_cookies())
        except Exception:
            stored = 0
        try:
            browsers = _detect_installed_browsers()
        except Exception:
            browsers = []
        return {"ok": True, "mode": self.cfg.get("cookie_browser") or "自动",
                "stored": stored, "browsers": browsers,
                "has_cookie_file": _has_app_cookie_file()}

    def rpc_login_cookie(self, url):
        """开独立登录窗（headed Chromium）抓 cookie：异步，webview 轮询 loginStatus。"""
        st = getattr(self, "_web_login", None)
        if st and st.get("running"):
            return {"ok": True, "already": True, "msg": "登录窗已在进行中"}
        start = (url or "").strip().splitlines()[0].strip() if url else ""
        if not start.lower().startswith("http"):
            start = "https://www.bilibili.com"
        self._web_login = {"running": True, "saved": 0, "done": False,
                           "msg": "已打开登录窗，请在弹出的浏览器里登录，完成后会自动记录 Cookie。"}
        def worker():
            try:
                n = capture_login_cookies(start, self.log, None, None, timeout_s=600)
            except Exception as e:
                n = 0
                try:
                    self.log("[登录窗Web] 失败: %s" % str(e)[:140])
                except Exception:
                    pass
            self._web_login["saved"] = n or 0
            self._web_login["running"] = False
            self._web_login["done"] = True
            if (n or 0) > 0:
                try:
                    self.cfg["cookie_browser"] = "已登录Cookie"
                    set_cookie_setting("已登录Cookie")
                    save_config(self.cfg)
                    self._web_login["msg"] = "登录成功，已记住 %d 条 Cookie，来源切到「已登录Cookie」。" % n
                except Exception as e:
                    self._web_login["msg"] = "抓到 %d 条，但保存失败：%s" % (n, str(e)[:80])
            else:
                self._web_login["msg"] = "没抓到 Cookie——请在窗口里完成登录后再关闭，或用「导入」。"
        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "started": True, "url": start}

    def rpc_login_status(self):
        st = getattr(self, "_web_login", None) or {}
        return {"ok": True, "running": bool(st.get("running")), "saved": st.get("saved", 0),
                "done": bool(st.get("done")), "msg": st.get("msg", "")}

    def rpc_import_cookie(self, text):
        """webview 粘贴 cookies.txt(Netscape) 内容 → 落盘 + 合并。"""
        try:
            text = (text or "").strip()
            if not text:
                return {"ok": False, "error": "内容为空"}
            p = _app_cookie_file()
            with open(p, "w", encoding="utf-8") as f:
                f.write(text if text.startswith("#") else "# Netscape HTTP Cookie File\n" + text)
            n = merge_app_cookies(read_app_cookies(), self.log, source="import")
            self.cfg["cookie_browser"] = "已登录Cookie"
            set_cookie_setting("已登录Cookie")
            save_config(self.cfg)
            return {"ok": True, "stored": n, "msg": "已导入并记住 %d 条 Cookie。" % n}
        except Exception as e:
            return {"ok": False, "error": str(e)[:160]}

    def rpc_components(self):
        try:
            import component_updates as CU
            cur = CU.current_versions()
            info = CU._STATE.get("info") or {}
            last = CU._STATE.get("last")
            return {"ok": True, "cur": cur, "info": info, "last": last,
                    "checking": bool(CU._STATE.get("checking"))}
        except Exception as e:
            return {"ok": False, "error": str(e)[:160]}

    def rpc_check_updates(self):
        try:
            import component_updates as CU
            if CU._STATE.get("checking"):
                return {"ok": True, "already": True}
            CU._STATE["checking"] = True
            def worker():
                try:
                    CU.check_all(self.log)
                except Exception as e:
                    try:
                        self.log("[更新] 检查失败：%s" % str(e)[:120])
                    except Exception:
                        pass
                finally:
                    CU._STATE["checking"] = False
            threading.Thread(target=worker, daemon=True).start()
            return {"ok": True, "started": True}
        except Exception as e:
            return {"ok": False, "error": str(e)[:160]}

    def rpc_update_one(self, name):
        try:
            import component_updates as CU
            if name not in ("yt-dlp",):
                return {"ok": False, "error": "暂不支持升级：%s" % name}
            CU.update_pypi_pkg(name, self.log)
            return {"ok": True, "name": name}
        except Exception as e:
            return {"ok": False, "error": str(e)[:160]}

    # ---- 剪贴板接活：webview 轮询这个，弹「要下载吗」可操作条 ----
    def rpc_clip_hint(self):
        url = (getattr(self, "_clip_url", "") or "").strip()
        return {"ok": True, "url": url}

    def rpc_clip_take(self, url):
        """用户在 webview 点「下载这条」：填进网址框并嗅探。"""
        u = (url or getattr(self, "_clip_url", "") or "").strip()
        if not u:
            return {"ok": False, "error": "无链接"}
        self._clip_url = ""
        self._last_clip = u
        try:
            self.root.after(0, lambda: self.url_var.set(u))
            tid = self.rpc_sniff_start(u, "all")
            return {"ok": True, "task": str(tid), "url": u}
        except Exception as e:
            return {"ok": False, "error": str(e)[:120]}

    def rpc_clip_dismiss(self):
        self._clip_url = ""
        return {"ok": True}

    def rpc_open_outdir(self, path=None):
        """在资源管理器打开下载目录（或给定目录）。"""
        try:
            d = path or self.cfg.get("outdir") or DEFAULT_OUTDIR
            d = os.path.expanduser(d)
            if not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
            os.startfile(d)
            return {"ok": True, "path": d}
        except Exception as e:
            return {"ok": False, "error": str(e)[:160]}

    def rpc_stats(self):
        """值班室数据：读每日账本 stats.json + 磁盘剩余。"""
        out = {"days": {}, "today_bytes": 0, "today_count": 0, "total_bytes": 0,
               "total_count": 0, "disk_free_gb": 0}
        try:
            sp = os.path.join(BASE_DIR, "stats.json")
            if os.path.exists(sp):
                data = json.load(open(sp, encoding="utf-8"))
                days = data.get("days") if isinstance(data, dict) and "days" in data else data
                out["days"] = days or {}
                dmap = out["days"] if isinstance(out["days"], dict) else {}
                out["total_bytes"] = sum((v.get("bytes", 0) if isinstance(v, dict) else 0)
                                         for v in dmap.values())
                out["total_count"] = sum((v.get("count", 0) if isinstance(v, dict) else 0)
                                         for v in dmap.values())
                import datetime as _dt
                tb = dmap.get(_dt.date.today().isoformat())
                if isinstance(tb, dict):
                    out["today_bytes"] = tb.get("bytes", 0)
                    out["today_count"] = tb.get("count", 0)
        except Exception:
            pass
        try:
            du = shutil.disk_usage(self.cfg.get("outdir") or DEFAULT_OUTDIR)
            out["disk_free_gb"] = round(du.free / 1024 / 1024 / 1024, 1)
        except Exception:
            pass
        return {"ok": True, "stats": out}

    # ---- 结果表：筛选 / 排序 / 状态缓存 ----
    def _row_text(self, i):
        e = self.entries[i]
        return " ".join(str(e.get(k) or "") for k in
                        ("title", "ext", "res", "how")).lower() + " " + str(i + 1)

    def visible_rows(self):
        """当前筛选+排序后应显示的 entries 下标（iid 仍是原下标，下载逻辑不受影响）"""
        idx = list(range(len(self.entries or [])))
        q = (self.filter_var.get() or "").strip().lower()
        if q:
            terms = q.split()
            idx = [i for i in idx
                   if all(t in self._row_text(i) for t in terms)]
        col, rev = self._sort
        key_map = {"no": lambda i: i,
                   "title": lambda i: str(self.entries[i].get("title") or ""),
                   "ext": lambda i: str(self.entries[i].get("ext") or ""),
                   "res": lambda i: str(self.entries[i].get("res") or ""),
                   "dur": lambda i: self.entries[i].get("duration") or 0,
                   "size": lambda i: self.entries[i].get("size") or 0,
                   "how": lambda i: str(self.entries[i].get("how") or ""),
                   "prog": lambda i: _pct_of(self.row_status.get(i, "")) or 0.0}
        if col in key_map:
            key = key_map[col]
            try:
                idx.sort(key=key, reverse=rev)
            except TypeError:
                idx.sort(key=lambda i: str(key(i)), reverse=rev)
        return idx

    def sort_by(self, col):
        cur, rev = self._sort
        self._sort = [col, not rev if cur == col else (col in ("size", "dur"))]
        for c, base in self._head_base.items():
            mark = ""
            if c == col:
                mark = " ▼" if self._sort[1] else " ▲"
            try:
                self.tree.heading(c, text=base + mark)
            except Exception:
                pass
        self.refresh_table()

    def set_status(self, row, text):
        """状态写缓存 + 表格（行被筛掉时只写缓存，回到视图再显示）"""
        text = (text or "")[:26]
        self.row_status[row] = text
        try:
            self.tree.set(str(row), "status", text)
        except Exception:
            pass

    def _quality_cap(self):
        try:
            q = (self.quality_var.get() or "").strip()
        except Exception:
            return None
        if q in ("", "best", "最佳"):
            return None
        m = re.match(r"^(\d+)", q)
        return int(m.group(1)) if m else None

    def _res_disp(self, e):
        real = str(e.get("res") or "-")
        cap = self._quality_cap()
        if cap:
            m = re.match(r"^(\d+)p?$", real.strip().lower())
            if m and int(m.group(1)) > cap:
                return f"≤{cap}p"
        return real

    def _sync_quality(self):
        self.cfg["quality"] = self.quality_var.get()
        try:
            save_config(self.cfg)
        except Exception:
            pass
        self.refresh_table()
        cap = self._quality_cap()
        if cap:
            self.status_var.set(f"画质上限已设为 {cap}p —— 下载时会自动选 ≤{cap}p 的流（表格分辨率已按此封顶）")
            self.say(f"收到，只给主人下 {cap}p 以内的清晰度 ♪")
        else:
            self.status_var.set("画质上限＝最佳 —— 下载时取该视频可用的最高清晰度")
            self.say("画质拉满，尽量给主人最好的 ♪")

    def refresh_table(self, _e=None):
        if not hasattr(self, "tree") or not hasattr(self, "entries"):
            return
        keep = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        rows = self.visible_rows()
        for pos, i in enumerate(rows, 1):
            e = self.entries[i]
            self.tree.insert("", "end", iid=str(i),
                             tags=("odd",) if pos % 2 else ("even",),
                             values=(pos, e["title"], e.get("ext", "-"),
                                     self._res_disp(e),
                                     fmt_duration(e.get("duration")),
                                     fmt_size(e.get("size")),
                                     "",                                   # 进度：自绘时按状态推导
                                     self.row_status.get(i, "待下载"),
                                     e.get("how", "-")))
        for iid in keep:
            try:
                self.tree.selection_add(iid)
            except Exception:
                pass
        m, n = len(self.entries or []), len(rows)
        self.count_var.set("共 %d 条" % m if n == m else "匹配 %d / %d 条" % (n, m))

    # ---- 主题引擎 ----
    def _setup_styles(self):
        """按当前色板 C 重建全部 ttk 样式（换主题时重复调用即可）"""
        P = self.P
        style = ttk.Style(self.root)
        style.theme_use("clam")
        field = _tint(self.theme, 0) or C["card"]
        head_fg = C["plum"]
        sel_fg = C["ink"]
        lay = _layout()
        ft_body = _mono(F_UI) if lay["mono"] else F_UI
        ft_head = _mono(F_UI_B) if lay["mono"] else F_UI_B
        style.configure(".", font=F_UI, background=C["bg"], foreground=C["ink"])
        style.configure("Treeview", background=field, fieldbackground=field,
                        foreground=C["ink"], rowheight=P(30), borderwidth=0,
                        font=ft_body)
        style.configure("Treeview.Heading", background=C["pink_xl"],
                        foreground=head_fg, font=ft_head, relief="flat",
                        bordercolor=C["line"], padding=P(5))
        style.map("Treeview", background=[("selected", C["pink_l"])],
                  foreground=[("selected", sel_fg)])
        style.map("Treeview.Heading", background=[("active", C["pink_l"])])
        for cls in ("TEntry", "TCombobox"):
            style.configure("Maid." + cls, fieldbackground=field,
                            foreground=C["ink"], bordercolor=C["line"],
                            lightcolor=C["line"], darkcolor=C["line"],
                            padding=P(6), arrowcolor=C["ink"], arrowsize=P(12))
            style.map("Maid." + cls, bordercolor=[("focus", C["pink"])],
                      lightcolor=[("focus", C["pink"])],
                      darkcolor=[("focus", C["pink"])],
                      fieldbackground=[("readonly", field)])
        style.configure("Maid.Horizontal.TProgressbar", troughcolor=C["pink_xl"],
                        background=C["pink"], bordercolor=C["pink_xl"],
                        lightcolor=C["pink_xl"], darkcolor=C["pink_xl"],
                        troughrelief="flat", thickness=P(13))
        style.configure("Vertical.TScrollbar", background=C["pink_l"],
                        troughcolor=C["bg"], bordercolor=C["bg"],
                        arrowcolor=C["ink"], lightcolor=C["pink_l"],
                        darkcolor=C["pink_l"], arrowsize=P(12))
        style.map("Vertical.TScrollbar", background=[("active", C["pink"])])
        style.configure("Maid.TCheckbutton", background=C["card"],
                        foreground=C["ink"], font=F_UI)
        style.map("Maid.TCheckbutton", background=[("active", C["card"])],
                  indicatorcolor=[("selected", C["pink"])])
        style.configure("Maid.TRadiobutton", background=C["card"],
                        foreground=C["ink"], font=F_UI)
        style.map("Maid.TRadiobutton", background=[("active", C["card"])],
                  indicatorcolor=[("selected", C["pink"])])

    _COLOR_OPTS = ("background", "foreground", "fg", "bg", "selectbackground",
                   "selectforeground", "highlightbackground", "highlightcolor",
                   "activebackground", "activeforeground", "troughcolor",
                   "readonlybackground", "fieldbackground", "insertbackground",
                   "disabledforeground", "arrowcolor")

    # 女仆单主题时代写死的色值 → 语义色键；换主题时一并迁移，避免深色主题里留白块
    _LEGACY = {"#9D4E75": "plum", "#6A324B": "pink_d", "#FFFBFD": "card",
               "#6A4E5E": "ink", "#FFEAF3": "card", "#F8E6EF": "pink_xl"}

    def _retheme_widgets(self, old_pal, new_pal):
        """把界面上所有命中旧色板的 tk 控件颜色就地换成新色板对应值"""
        mapping = {old_pal[k].upper(): new_pal[k] for k in old_pal
                   if isinstance(old_pal[k], str) and old_pal[k].startswith("#")}
        for h, key in self._LEGACY.items():
            mapping.setdefault(h, new_pal.get(key, "#000000"))

        def walk(w):
            for c in w.winfo_children():
                for opt in self._COLOR_OPTS:
                    try:
                        cur = str(c.cget(opt)).upper()
                    except Exception:
                        continue
                    if cur in mapping:
                        try:
                            c[opt] = mapping[cur]
                        except Exception:
                            pass
                rt = getattr(c, "retheme", None)
                if callable(rt):        # RoundButton / Card / Pill 都靠这个重画自绘底图
                    try:
                        rt()
                    except Exception:
                        pass
                walk(c)

        walk(self.root)

    def _repack_rail(self, side):
        """（旧「角色休息室」侧栏已移除，左侧导航栏取代；保留方法名供 apply_theme 调用）"""
        return
        P = self.P
        try:
            self.rail.pack_forget()
            self.rail.pack(side=side, fill="y",
                           padx=(P(12), P(2)) if side == "left" else (P(2), P(12)),
                           pady=(P(2), P(10)), before=getattr(self, "_body_anchor", None))
        except Exception:
            self.rail.pack(side=side, fill="y", padx=P(8), pady=P(8))

    _LIVE_SKINS = ("horn", "qing", "jiao", "maid")   # 有分层 PSD 的配色 = 实时桌宠外观

    def _pet_skin_for(self, theme_key):
        return theme_key if theme_key in self._LIVE_SKINS else "horn"

    def apply_theme(self, key, save=True):
        """换一套主题：色板 + 素材 + 人设台词，界面即时刷新"""
        if key not in THEMES or key == self.theme:
            return
        old_pal = dict(C)
        self.theme = key
        _cur_theme[0] = key
        # 换角色 = 双形态归位（工作形态是桃桃专属，别带到别的角色身上）
        try:
            self._form = "daily"
            self._as_transform_to = None
            self._as_greet_until = 0.0
            self._as_hug_until = 0.0
            self._as_poke_until = 0.0
            self._as_dragging = False
            self._as_success_done = False
            self._as_idle_vignette = 0
            self._as_idle_vignette_until = 0.0
            self._c24_success_until = 0.0
            self._c24_fail_until = 0.0
            self._c24_poke_until = 0.0
            self._c24_idle_loops = 0
            self._as_last_key = None
            self._probing = False
            self._clip_url = ""
            if getattr(self, "_stage", None) is not None:
                try:
                    self._stage.reset()
                except Exception:
                    pass
            self._as_stare_t = 0.0
        except Exception:
            pass
        C.clear()
        C.update(_chrome(THEMES[key]["palette"], key))
        self._retheme_widgets(old_pal, C)
        self._apply_girly_fonts()
        self._setup_styles()
        try:
            self._qtree_tags()        # 队列「进行中」等状态色跟着新角色走
        except Exception:
            pass
        try:
            # 画布里的颜色 _retheme_widgets 扫不到（它只管 tk 控件属性）→ 手动重画
            for _t, _ic, _k in getattr(self, "_sec_ics", []):
                self._sec_tile(_t, _ic, _k)
            if getattr(self, "_cur_view", None):
                self._show_view(self._cur_view)
        except Exception:
            pass
        try:
            self.root.configure(bg=C["bg"])
            ic = _art("AVATAR", self.S)
            if ic is not None:
                self.root.iconphoto(True, ic)
        except Exception:
            pass
        try:
            self.tree.tag_configure("odd", background=_tint(key, 0) or C["card"])
            self.tree.tag_configure("even", background=_tint(key, 1) or C["pink_xl"])
            # 日志面板恒为深色终端底，不随角色换色（原来这里会被刷成浅色 = 用户投诉点）
            try:
                self.log_text.configure(fg=_LOG_FG, bg=_LOG_BG)
            except Exception:
                pass
        except Exception:
            pass
        # 主功能区两处标题跟着角色换口吻（结果表 / 日志），不再全app一句
        try:
            v = _voice()
            self._res_title_text = v["res"]
            self._log_title_text = v["log"]
            self.res_bar.configure(bg=C["pink_xl"])
            self.log_hdr.configure(bg=C["pink_xl"])
            self._draw_resbar()
            self._draw_logbar()
        except Exception:
            pass
        self._bgsize = (self.bgcv.winfo_width(), self.bgcv.winfo_height())
        self.bgcv.configure(bg=C["bg"])
        self.bgcv.delete("bgtile")
        self._repack_rail(_layout(key)["side"])
        self._draw_rail()
        self._tile_bg()
        self._draw_header()
        self.set_mood("idle")
        self._apply_chrome()
        self._start_amb()
        try:
            if getattr(self, "bar", None):
                self.bar._redraw()
        except Exception:
            pass
        try:
            if getattr(self, "assist", None):
                self.assist.configure(bg=self._AS_KEY)
                if self._as_resize():          # 新角色帧更大/更小 → 桌宠窗跟着变，防裁头压泡
                    self._as_place()
                self._draw_assistant()
        except Exception:
            pass
        try:
            self._c24_warm()          # 换到带 cast24 的角色：后台分块预解码，防切动作卡顿闪黑
        except Exception:
            pass
        for _rp in getattr(self, "_repainters", []):
            try:
                _rp()
            except Exception:
                pass
        self.say(self.line_for("greet"))
        # 底部状态条那句「开场白　提示」是开机时一次性拼的，换角色不刷就会一直念上一个人
        # 的话（实测：切到跂之后底栏还是"摸摸我的角当奖励 / 猲去换新的一条"）
        try:
            self.status_var.set(self.line_for("greet") + "　" + self.line_for("idle"))
        except Exception:
            pass
        # 换角色 = 把日志"开头那句"就地换成新角色的开场白（只保留一行，不堆叠所有人的话）
        self._set_log_greet(self._greet_line())
        # 实时桌宠：P1a 起桌面桌宠统一为单一 Live2D「大肥鱼」，不再按世界切 PSD 皮肤。
        # （旧 self._pet.set_skin 走 Anime25DRig 多皮肤，已随 pet_electron 改接 Live2D 退役；
        #   保留 pet_skin 配置项仅作历史兼容，不再驱动外观。）
        # 情绪/台词反应仍走 self._pet.react / self._pet.say（→ Live2D SniffDLPet）。
        if save:
            self.cfg["theme"] = key
            try:
                save_config(self.cfg)
            except Exception:
                pass

    def cycle_theme(self, step=1):
        # 六界已退役、桌宠统一为单一「大肥鱼」→ 不再切换世界（保留方法名兼容旧调用/热键）
        return

    def line_for(self, kind, n=None):
        """取当前主题的一句台词（随机）；n 用于带计数的模板。
        桃桃（maid）走 maid_talk：按日常/工作形态分语气，并带时段问候。"""
        if self.theme == "maid" and MT is not None:
            try:
                txt = MT.pick(kind, getattr(self, "_form", "daily"),
                              n=n if n is not None else len(self.entries))
                if txt:
                    return txt
            except Exception:
                pass
        lines = ((THEMES.get(self.theme) or {}).get("LINES") or _FALLBACK_LINES).get(kind) \
            or _FALLBACK_LINES.get(kind) or [""]
        import random
        txt = random.choice(lines)
        if "{n}" in txt:
            txt = txt.format(n=n if n is not None else len(self.entries))
        return txt

    def char_name(self):
        th = THEMES.get(self.theme) or {}
        return th.get("char") or "咱", th.get("role") or "", th.get("label") or ""

    # ---- 背景碎花 ----
    def _on_bg_cfg(self, e):
        return
        if (e.width, e.height) == self._bgsize:
            return
        self._bgsize = (e.width, e.height)
        if self._bgjob:
            try:
                self.bgcv.after_cancel(self._bgjob)
            except Exception:
                pass
        self._bgjob = self.bgcv.after(90, self._tile_bg)

    def _tile_bg(self):
        """（主窗碎花/世界底已移除，交给桌宠；保留方法名供 apply_theme 调用）"""
        self._bgjob = None
        return
        w, h = self._bgsize
        self.bgcv.delete("bgtile")
        if self.theme == "maid" and GI is not None:
            base = _girly("BG_PEACH")
            if base is not None and w > 2 and h > 2:
                ck = (int(w), int(h))
                cache = getattr(self, "_bgfit", {})
                im = cache.get(ck)
                if im is None:
                    im = _fit_photo(base, w, h)
                    if len(cache) > 24:
                        cache.clear()
                    cache[ck] = im
                    self._bgfit = cache
                if im is not None:
                    self.bgcv.create_image(w // 2, h // 2, image=im,
                                           anchor="center", tags="bgtile")
                    self._world_bg(self.bgcv, w, h)
                    try:
                        self.bgcv.tag_lower("bgtile")
                    except Exception:
                        pass
                    return
        t = _art("TILE", self.S)
        gcols = (_WORLD.get(self.theme) or {}).get("bg")
        if gcols:
            gh = max(h, 1)
            n = len(gcols)
            for yy in range(0, gh):
                f = (yy / gh) * (n - 1)
                i = min(int(f), n - 2)
                col = _mix(gcols[i], gcols[i + 1], f - i)
                self.bgcv.create_line(0, yy, w, yy, fill=col, tags="bgtile")
        if t is not None:
            tw, th = t.width(), t.height()
            for y in range(0, h, th):
                for x in range(0, w, tw):
                    self.bgcv.create_image(x, y, image=t, anchor="nw", tags="bgtile")
        self._world_bg(self.bgcv, w, h)
        try:
            self.bgcv.tag_lower("bgtile")
        except Exception:
            pass

    def _world_bg(self, cv, w, h):
        """背景层大尺度「世界」水印：每个角色一幅，压在所有卡片之下。"""
        P = self.P
        th = self.theme
        t = "bgtile"
        try:
            if th == "hanfu":
                _ink_mountains(cv, w, h - P(6), "#ECDFE3", tags=t)
                _plum_branch(cv, P(20), h - P(24), P(150), "#E6D2D8", "#F4E3E7", tags=t)
                for lx, ly in ((w - P(58), P(170)), (w - P(58), P(272))):
                    _lantern(cv, lx, ly, P(15), "#F8ECEF", "#E0C6CC", tags=t)
                poem = "静水流深·落笔成画"
                ft = ("KaiTi", int(P(17)), "bold")
                for i, ch in enumerate(poem):
                    cv.create_text(w - P(134), P(130) + i * P(26), text=ch,
                                   font=ft, fill="#DFC6CC", tags=t)
                _guqin(cv, P(44), h - P(118), P(150), "#EBD9DE", tags=t)
            elif th == "magic":
                cx, cy, R = int(w * 0.74), int(h * 0.60), int(P(190))
                _magic_circle(cv, cx, cy, R, "#E7E0FA", "#EFEAFC", tags=t)
                _magic_circle(cv, cx, cy, int(R * 0.62), "#EDE7FB", "#F3EEFC", tags=t)
                for a in range(0, 360, 30):
                    rad = math.radians(a)
                    _star(cv, cx + math.cos(rad) * R * 1.05,
                          cy + math.sin(rad) * R * 1.05, P(6), "#E9E1FA", tags=t)
                for i in range(9):
                    _sparkle(cv, P(60) + i * P(90), P(150) + (i % 3) * P(70),
                             P(4), "#EBE4FB", tags=t)
            elif th == "miko":
                _torii(cv, int(w * 0.82), h - P(18), int(P(190)),
                       "#F0D3D8", "#E7C2C9", tags=t)
                for bx, by, bs in ((P(60), P(160), P(120)), (w - P(80), P(140), P(100))):
                    cv.create_line(bx, by, bx + bs, by - bs * 0.4, fill="#F2DADE",
                                   width=2, smooth=True, tags=t)
                    for k in range(5):
                        _blossom(cv, bx + bs * (0.2 + 0.16 * k),
                                 by - bs * 0.08 * k, P(5), "#F5E0E4", t)
                for px, py in ((w * 0.3, h * 0.4), (w * 0.56, h * 0.72), (w * 0.2, h * 0.78)):
                    _paw(cv, px, py, P(7), "#F5DEE3", tags=t)
            elif th == "robot":
                _grid(cv, w, h, P(64), "#E3F1FB", tags=t)
                for cxx in (int(w * 0.16), int(w * 0.62), int(w * 0.9)):
                    _code_rain(cv, cxx, P(120), int(h / max(1, P(34))), "#DCEBF7",
                               int(P(26)), ("Consolas", int(P(11))), tags=t)
                _hud_brackets(cv, P(14), P(14), w - P(14), h - P(14), "#DCECF8",
                              tags=t, ln=P(34))
                cv.create_oval(w - P(150), h - P(190), w - P(70), h - P(110),
                               outline="#E1EEF8", width=2, tags=t)
                cv.create_line(w - P(150), h - P(150), w - P(70), h - P(150),
                               fill="#E1EEF8", tags=t)
                cv.create_line(w - P(110), h - P(190), w - P(110), h - P(110),
                               fill="#E1EEF8", tags=t)
                _heart(cv, int(w * 0.5), h - P(80), P(30), "#F2D9E6", tags=t)
            elif th == "goth":
                _crescent(cv, int(w * 0.86), int(P(180)), int(P(78)), "#2B1627", tags=t)
                _web(cv, w - P(4), P(4), P(150), "#3B2336", start=180, tags=t)
                _thorn(cv, P(6), w - P(6), h - P(10), "#3A2035", tags=t)
                _rose(cv, int(w * 0.88), h - P(110), P(30), "#3D2138", "#552C4C", tags=t)
                for bx, by, bs in ((P(70), P(230), P(100)), (w - P(130), P(320), P(80))):
                    cv.create_line(bx, by, bx + bs, by - bs * 0.35, fill="#331B2E",
                                   width=2, smooth=True, tags=t)
                    for k in range(6):
                        tx0 = bx + bs * (0.2 + 0.12 * k)
                        ty0 = by - bs * 0.07 * k
                        cv.create_line(tx0, ty0, tx0 + 4, ty0 - 8,
                                       fill="#331B2E", width=1, tags=t)
            elif th == "maid":
                for gx in range(int(P(40)), int(w), int(P(90))):
                    for gy in range(int(P(40)), int(h), int(P(90))):
                        cv.create_oval(gx - 2, gy - 2, gx + 2, gy + 2,
                                       fill="#FFE9F3", outline="", tags=t)
        except Exception:
            pass

    # ---- 头栏骨架：六个世界各自的外轮廓与左边铭牌（不是换色，是换容器） ----
    def _world_header_frame(self, cv, w, h, lw, rad, lay):
        """画头栏底卡 + 左侧铭牌 + 边框母题。返回 True=已接管，False=退回通用圆角卡。"""
        P = self.P
        th = self.theme
        try:
            if th == "maid":
                # 糖果屋：圆角卡 + 顶部蕾丝棚 + 左侧糖果条纹 + 缎带蝴蝶结
                _rr(cv, 0, 2, w - 1, h, rad, fill=C["card"], outline=C["line"])
                _rr(cv, 0, 2, lw, h, rad, fill=C["pink_xl"], outline=C["line"])
                cv.create_rectangle(lw - rad, 3, lw, h - 2,
                                    fill=C["pink_xl"], outline="", tags="wd")
                for xx in range(int(P(16)), int(lw - P(8)), int(P(34))):
                    cv.create_line(xx, P(20), xx, h - P(8), fill="#FFF2F8",
                                   width=max(1, int(P(3))), tags="wd")
                _scallop_row(cv, P(8), w - P(8), P(15), P(11),
                             "#FFFFFF", "#FFD9E8", tags="wd")
                cv.create_line(P(8), P(3), w - P(8), P(3),
                               fill="#FF9EC4", width=2, tags="wd")
                _lace_side(cv, lw - P(7), P(26), h - P(10), P(7),
                           "#FFFFFF", tags="wd", right=True)
                _bow(cv, lw - P(16), P(17), P(16),
                     C["pink"], C["pink_d"], tags="wd")
                _heart(cv, P(24), P(96), P(6), C["pink"], tags="wd")
                return True

            if th == "hanfu":
                # 水墨书房：手卷——两端木轴 + 上下绢边 + 纸上题签
                _rr(cv, 0, 2, w - 1, h, rad, fill=C["card"], outline=C["line"])
                _rr(cv, 0, 2, lw, h, rad, fill=C["pink_xl"], outline="#C8566B")
                cv.create_rectangle(P(8), P(9), lw - P(6), h - P(8),
                                    outline="#C8566B", width=1,
                                    dash=(3, 4), tags="wd")
                cv.create_rectangle(P(9), 2, w - P(9), P(7),
                                    fill="#F3E3E4", outline="", tags="wd")
                cv.create_rectangle(P(9), h - P(6), w - P(9), h - 1,
                                    fill="#F3E3E4", outline="", tags="wd")
                _vscroll_rod(cv, P(6), 2, h, P(10), "#6B4B4B",
                             "#9A7373", "#4A3232", tags="wd")
                _vscroll_rod(cv, w - P(6), 2, h, P(10), "#6B4B4B",
                             "#9A7373", "#4A3232", tags="wd")
                cv.create_line(P(6), P(24), P(6), P(44),
                               fill="#A8455A", width=1, tags="wd")
                cv.create_polygon(P(1), P(44), P(11), P(44), P(6), P(58),
                                  fill="#A8455A", outline="#7C3042",
                                  width=1, tags="wd")
                cv.create_line(P(10), P(58), P(10), P(64),
                               fill="#7C3042", width=1, tags="wd")
                return True

            if th == "magic":
                # 星环魔法少女：金线双框 + 四角星 + 左侧故事书页
                _rr(cv, 0, 2, w - 1, h, rad, fill=C["card"], outline=C["line"])
                _rr(cv, 0, 2, lw, h, rad, fill=C["pink_xl"], outline=C["gold"])
                cv.create_rectangle(P(8), P(9), lw - P(8), h - P(8),
                                    outline=C["gold"], width=1,
                                    dash=(4, 4), tags="wd")
                _rr(cv, P(4), P(6), w - P(4), h - P(5),
                    max(3, rad - 3), fill="", outline=C["gold"],
                    width=2, tags="wd")
                for sx, sy in ((P(14), P(15)), (w - P(14), P(15)),
                               (P(14), h - P(13)), (w - P(14), h - P(13))):
                    _star(cv, sx, sy, P(7), C["gold"], tags="wd")
                cv.create_line(lw, P(16), lw, h - P(10),
                               fill=C["gold"], width=1, tags="wd")
                _diamond(cv, lw, P(54), P(9), C["gold"], tags="wd")
                return True

            if th == "miko":
                # 神社：鸟居框——笠木/岛木/朱柱 + 注连绳与纸垂
                _rr(cv, 0, 2, w - 1, h, rad, fill=C["card"], outline=C["line"])
                _rr(cv, 0, 2, lw, h, rad, fill=C["pink_xl"], outline="#C8566B")
                cv.create_rectangle(P(8), P(10), lw - P(6), h - P(8),
                                    outline="#D8A79C", width=1, tags="wd")
                cv.create_polygon(P(2), P(12), P(2), P(4), P(10), P(2),
                                  w - P(10), P(2), w - P(2), P(4),
                                  w - P(2), P(12), w - P(14), P(9),
                                  P(14), P(9),
                                  fill="#C63A4E", outline="#8F2335",
                                  width=1, tags="wd")
                cv.create_rectangle(P(14), P(15), w - P(14), P(20),
                                    fill="#C63A4E", outline="#8F2335",
                                    tags="wd")
                for x0 in (P(3), w - P(11)):
                    cv.create_rectangle(x0, P(14), x0 + P(8), h,
                                        fill="#C63A4E", outline="#8F2335",
                                        tags="wd")
                    cv.create_rectangle(x0 - P(2), P(8),
                                        x0 + P(10), P(16),
                                        fill="#E6B54A", outline="#B58A2C",
                                        tags="wd")
                return True

            if th == "robot":
                # 机娘 HUD：切角机体 + 斜切左翼 + 扫描线与刻度
                c = P(12)
                _chamfer_rect(cv, 0, 2, w - 1, h, c,
                              fill=C["card"], outline=C["line"], width=1)
                _chamfer_rect(cv, P(5), P(7), w - P(5), h - P(5),
                              max(2, c - P(5)), fill="", outline=C["line"],
                              width=1)
                cv.create_polygon(c, P(2), lw - P(20), P(2),
                                  lw + P(14), h, c, h,
                                  0, h - c, 0, P(2) + c,
                                  fill=C["pink_xl"], outline=C["line"],
                                  width=1, tags="wd")
                cv.create_line(lw - P(20), P(2), lw + P(14), h,
                               fill=C["pink"], width=2, tags="wd")
                for i in range(1, 5):
                    fx = (lw - P(20)) + (lw + P(14) - (lw - P(20))) * i / 5
                    fy = P(2) + (h - P(2)) * i / 5
                    cv.create_line(fx - P(6), fy, fx + P(3), fy,
                                   fill=C["pink_l"], width=1, tags="wd")
                for yy in range(int(P(14)), int(h - P(6)), int(P(9))):
                    cv.create_line(lw + P(20), yy, w - P(16), yy,
                                   fill=_mix(C["card"], C["pink_l"], 0.45),
                                   width=1, tags="wd")
                for xx in range(int(lw + P(26)), int(w - P(28)), int(P(22))):
                    cv.create_line(xx, P(9), xx, P(14),
                                   fill=C["line"], width=1, tags="wd")
                cv.create_text(P(42), h - P(5), anchor="w", text="UNIT-00",
                               font=("Consolas", 7), fill=C["ink_l"], tags="wd")
                return True

            if th == "goth":
                # 哥特夜堡：天鹅绒帷幔 + 铁艺护角 + 尖拱线
                velvet = _mix(C["card"], C["pink_xl"], 0.55)
                _rr(cv, 0, 2, w - 1, h, rad, fill=C["card"], outline=C["line"])
                _rr(cv, 0, 2, lw, h, rad, fill=velvet, outline=C["line"])
                cv.create_rectangle(lw - rad, 3, lw, h - 2,
                                    fill=velvet, outline="", tags="wd")
                _pointed_arch(cv, P(12), lw - P(6), P(10), h - P(8),
                              C["line"], 1, tags="wd")
                _valance(cv, P(4), w - P(4), P(2), P(20),
                         _mix(C["pink_d"], "#1A0D16", 0.55),
                         _mix(C["gold"], "#7A5A4A", 0.55), tags="wd")
                for ccx, ccy, ddx, ddy in (
                        (P(7), P(3), 1, 1), (w - P(7), P(3), -1, 1),
                        (P(7), h - P(3), 1, -1), (w - P(7), h - P(3), -1, -1)):
                    _iron_corner(cv, ccx, ccy, ddx, ddy, P(30),
                                 C["line"], tags="wd")
                for xx in range(int(P(22)), int(w - P(22)), int(P(30))):
                    cv.create_oval(xx - P(2), h - P(6), xx + P(2), h - P(2),
                                   fill=C["line"], outline="", tags="wd")
                return True
        except Exception:
            return False
        return False

    def _paint_header_vision(self, cv, w, h, lw, min_x=None):
        """在头栏右侧铺一张真实「世界画窗」；素材本身已按卡片色烘焙羽化边。"""
        if self.theme == "maid":
            return
        P = self.P
        right = w - P(16)
        left = max(lw + P(2), int(min_x) if min_x is not None else right)
        avail = right - left
        if avail < P(150):
            return
        got = _vision(self.theme, h - P(18), avail)
        if not got:
            return
        im, cropped = got
        if im is None or im.height() < P(70):
            return
        iw, ih = im.width(), im.height()
        x0 = max(left, right - iw)
        y0 = 2 + (h - ih) // 2
        cv.create_image(x0, y0, image=im, anchor="nw", tags="vision")
        if cropped:
            fd = _fade(self.theme, max(1, 460 // max(1, ih)))
            if fd is not None:
                cv.create_image(x0, y0, image=fd, anchor="nw", tags="vision")

    def _draw_resbar(self):
        """结果表标题带：材质贴片 + 标题/计数/筛选（maid 仍用纯色条）。"""
        cv = getattr(self, "res_bar", None)
        if cv is None:
            return
        P = self.P
        try:
            w, h = cv.winfo_width(), cv.winfo_height()
            if w <= 1 or h <= 1:
                return
            cv.delete("resband")
            # 标题带 = 两个签名色的浅渐变（原来这里是个永不执行的"材质贴片"死分支）
            _grad_begin(cv)   # 不切帧的话，每次改窗口尺寸都多留一张旧渐变图在内存里
            _grad(cv, 0, 0, w, h, 0, C["pink_xl"],
                  _mix(_acc2(), "#FFFFFF", 0.90), 0, tags="resband")
            cv.tag_lower("resband")
            cv.itemconfigure(self._res_title, text=self._res_title_text,
                             fill=C["plum"], font=F_SM)
            cv.coords(self._res_title, P(10), h // 2)
            ew = P(126)
            ex = w - P(10)
            cv.coords(self._res_fwin, ex, h // 2)
            cv.coords(self._res_flabel, ex - ew - P(8), h // 2)
            bb = cv.bbox(self._res_flabel)
            cx = (bb[0] - P(14)) if bb else (ex - ew - P(110))
            cv.coords(self._res_count, cx, h // 2)
            cv.itemconfigure(self._res_count, text=self.count_var.get(),
                             fill=C["ink_l"])
        except Exception:
            pass

    def _draw_logbar(self):
        """日志标题带：材质贴片 + 标题（maid 仍用纯色条）。"""
        cv = getattr(self, "log_hdr", None)
        if cv is None:
            return
        P = self.P
        try:
            w, h = cv.winfo_width(), cv.winfo_height()
            if w <= 1 or h <= 1:
                return
            cv.delete("logband")
            # 日志标题带 = 深色终端底上一条极浅的签名色渐变（在暗底上刚好读得出层次）
            _grad_begin(cv)
            _grad(cv, 0, 0, w, h, 0, _LOG_BG,
                  _mix(_LOG_BG, C["pink"], 0.16), 0, tags="logband")
            cv.tag_lower("logband")
            cv.create_line(0, h - 1, w, h - 1, fill=_LOG_LINE, tags="logband")
            cv.itemconfigure(self._log_title, text=self._log_title_text,
                             fill=_LOG_TEAL, font=F_SM)
            cv.coords(self._log_title, P(10), h // 2)
        except Exception:
            pass

    def _vision_overlay(self, cv, w, h, lw):
        """画窗之上补回头栏的关键骨架：神社鸟居绳结 / 夜堡帷幔铁角。"""
        if TV is None or self.theme == "maid":
            return
        P = self.P
        th = self.theme
        try:
            if th == "miko":
                # 顶部笠木/岛木 + 朱柱，重画在画窗之上（注连绳/纸垂/铃已去掉）
                cv.create_polygon(P(2), P(12), P(2), P(4), P(10), P(2),
                                  w - P(10), P(2), w - P(2), P(4),
                                  w - P(2), P(12), w - P(14), P(9),
                                  P(14), P(9),
                                  fill="#C63A4E", outline="#8F2335",
                                  width=1, tags="wd")
                cv.create_rectangle(P(14), P(15), w - P(14), P(20),
                                    fill="#C63A4E", outline="#8F2335",
                                    tags="wd")
                for x0 in (P(3), w - P(11)):
                    cv.create_rectangle(x0, P(14), x0 + P(8), h,
                                        fill="#C63A4E", outline="#8F2335",
                                        tags="wd")
            elif th == "goth":
                _valance(cv, P(4), w - P(4), P(2), P(20),
                         _mix(C["pink_d"], "#1A0D16", 0.55),
                         _mix(C["gold"], "#7A5A4A", 0.55), tags="wd")
                for ccx, ccy, ddx, ddy in (
                        (P(7), P(3), 1, 1), (w - P(7), P(3), -1, 1),
                        (P(7), h - P(3), 1, -1), (w - P(7), h - P(3), -1, -1)):
                    _iron_corner(cv, ccx, ccy, ddx, ddy, P(30),
                                 C["line"], tags="wd")
                for xx in range(int(P(22)), int(w - P(22)), int(P(30))):
                    cv.create_oval(xx - P(2), h - P(6), xx + P(2), h - P(2),
                                   fill=C["line"], outline="", tags="wd")
        except Exception:
            pass

    # ================= 新骨架：侧栏 + 视图切换 =================
    _VIEWS = ("dl", "queue", "dash", "log", "set")

    def _build_sidebar(self, root):
        P = self.P
        sb = tk.Frame(root, width=P(236), bg=C["side"], highlightthickness=0)
        sb.pack(side="left", fill="y")
        sb.pack_propagate(False)
        self.sidebar = sb
        # brand
        brand = tk.Frame(sb, bg=C["side"])
        brand.pack(fill="x", padx=P(16), pady=(P(16), P(12)))
        self._brand_logo = tk.Canvas(brand, width=P(26), height=P(26),
                                     bg=C["side"], highlightthickness=0)
        self._brand_logo.pack(side="left")
        tk.Label(brand, text="SniffDL", bg=C["side"], fg=C["ink"],
                 font=("Segoe UI", 15, "bold")).pack(side="left", padx=(P(9), 0))
        self._draw_logo()
        # 看板娘 profile（头像 + 名 + 角色）
        _me_card = Card(sb, bg=C["side"])
        self._me_card = _me_card        # 验收断言按卡片矩形取角点，留个显式引用
        me = _me_card.body
        _me_card.pack(fill="x", padx=P(10), pady=(0, P(10)))
        self._me = me
        self._me_av = tk.Canvas(me, width=P(52), height=P(52), bg=C["card"], highlightthickness=0)
        self._me_av.pack(side="left", padx=(P(10), P(10)), pady=P(9))
        col = tk.Frame(me, bg=C["card"])
        col.pack(side="left", fill="x", expand=True, pady=P(8))
        self._me_name = tk.Label(col, text="", bg=C["card"], fg=C["ink"], font=F_UI_B, anchor="w")
        self._me_name.pack(fill="x")
        self._me_role = tk.Label(col, text="", bg=C["card"], fg=C["ink_l"], font=F_SM, anchor="w")
        self._me_role.pack(fill="x")
        # 导航（5 视图 + 细线性图标）
        self._nav = {}
        for key, label in (("dl", "下载"), ("queue", "队列"), ("dash", "值班室"),
                           ("log", "日志"), ("set", "设置")):
            row = tk.Frame(sb, bg=C["side"], cursor="hand2")
            row.pack(fill="x", padx=P(8), pady=P(1))
            ic = tk.Canvas(row, width=P(20), height=P(20), bg=C["side"],
                           highlightthickness=0)
            ic.pack(side="left", padx=(P(12), P(10)), pady=P(9))
            lb = tk.Label(row, text=label, bg=C["side"], fg=C["ink_l"],
                          font=F_UI_B, anchor="w")
            lb.pack(side="left")
            for w in (row, ic, lb):
                w.bind("<Button-1>", lambda e, k=key: self._show_view(k))
                w.bind("<Enter>", lambda e, k=key: self._nav_hover(k, True))
                w.bind("<Leave>", lambda e, k=key: self._nav_hover(k, False))
            self._nav[key] = (row, ic, lb)
            self._nav_icon(ic, key, C["ink_l"])
        tk.Frame(sb, bg=C["side"]).pack(fill="both", expand=True)
        # 换装（chip 尺寸按「只数」自适应排布，不再写死 P(36)+padx2）
        # §22.1 根因：侧栏 P(236) 扣掉 padx 只剩 208px，而 6 只 × 40 = 240 → 第 6 只
        # （蜃）被挤到侧栏外，只露半张脸、<Button-1> 命不中。自适应后加角色也不会再溢出。
        # 单一桌宠「大肥鱼」：只剩一枚时不再显示"换装"区（去掉六界切换残留）；>1 才显示可切换。
        _keys = [k for k in THEME_ORDER if _art("CHIP", self.S, k) is not None]
        if "feiyu" in THEMES:
            _keys = [k for k in _keys if k == "feiyu"]
        self._dress = None
        self._dress_imgs = {}
        self._dress_geo = getattr(self, "_dress_geo", None)
        if len(_keys) > 1:
            tk.Label(sb, text="换装", bg=C["side"], fg=C["ink_l"], font=F_SM,
                     anchor="w", padx=P(16)).pack(fill="x")
            dr = tk.Frame(sb, bg=C["side"])
            dr.pack(fill="x", padx=P(14), pady=(P(4), P(10)))
            self._dress = dr
            size, gap, ring = self._dress_fit(len(_keys))
            self._dress_geo = (size, gap, ring)
            for k in _keys:
                cell = tk.Canvas(dr, width=size, height=size, bg=C["side"],
                                 highlightthickness=0, cursor="hand2")
                cell.pack(side="left", padx=gap)
                d = size - 2 * ring
                cell._dd = d
                im = _pfp_img(d, k, "CHIP")
                if im is None:
                    im = _art("CHIP", self.S * d / float(P(36)), k)
                if im is None:
                    continue
                cell.create_image(size // 2, size // 2, image=im,
                                  anchor="center", tags="img")
                cell._img = im
                cell.bind("<Button-1>", lambda e, kk=k: self.apply_theme(kk))
                cell.bind("<Enter>", lambda e, kk=k: self._dress_hover(kk, True))
                cell.bind("<Leave>", lambda e, kk=k: self._dress_hover(kk, False))
                self._dress_imgs[k] = cell
        tk.Label(sb, text="v0.5.9 · aria2 + yt-dlp", bg=C["side"], fg=C["ink_l"],
                 font=("Segoe UI", 8), anchor="w", padx=P(16)).pack(fill="x", pady=(0, P(10)))
        try:
            self._dress_refresh()
        except Exception:
            pass

    def _draw_logo(self):
        P = self.P
        try:
            cv = self._brand_logo
            cv.delete("all")
            _grad_begin(cv)
            c1, c2 = _grad_pair()
            _grad(cv, 0, 0, P(26), P(26), P(8), c1, c2, 45)
            cv.create_text(P(13), P(13), text="S", fill="#FFFFFF",
                           font=("Segoe UI", 13, "bold"))
        except Exception:
            pass

    def _build_content(self, root):
        P = self.P
        ct = tk.Frame(root, bg=C["bg"])
        ct.pack(side="left", fill="both", expand=True)
        self.content = ct
        tb = tk.Frame(ct, bg=C["bg"], height=P(60))
        tb.pack(side="top", fill="x")
        tb.pack_propagate(False)
        self.topbar = tb
        self._title = tk.Label(tb, text="下载", bg=C["bg"], fg=C["ink"],
                               font=("Segoe UI", 17, "bold"))
        self._title.pack(side="left", padx=(P(24), P(10)), pady=P(16))
        self._title_chip = Pill(tb, text="", fg=("ink_l", 0.0), tint="card",
                                border=C["line"], font=F_SM, padx=10, pady=3)
        self._title_chip.pack(side="left", pady=P(17))
        # 右上角动作区（网页稿 .top .r）：按当前视图换按钮
        self._top_act = tk.Frame(tb, bg=C["bg"])
        self._top_act.pack(side="right", padx=(0, P(22)))
        # 底部常驻状态条（引擎 / 当前任务 / 实时速度）——先 bottom，再让 viewhost 撑满
        self.statusbar = tk.Frame(ct, bg=C["card"], height=P(38))
        self.statusbar.pack(side="bottom", fill="x")
        self.statusbar.pack_propagate(False)
        tk.Frame(ct, bg=C["line"], height=1).pack(side="bottom", fill="x")
        vh = tk.Frame(ct, bg=C["bg"])
        vh.pack(side="top", fill="both", expand=True)
        self.viewhost = vh
        self.view_dl = tk.Frame(vh, bg=C["bg"])
        self.view_queue = tk.Frame(vh, bg=C["bg"])
        self.view_dash = tk.Frame(vh, bg=C["bg"])
        self.view_log = tk.Frame(vh, bg=C["bg"])
        self.view_set = tk.Frame(vh, bg=C["bg"])
        self._cur_view = None
        try:
            self._build_statusbar()
        except Exception:
            pass
        try:
            self._build_dash()
        except Exception:
            pass

    def _set_init(self):
        """设置页容器：3 列图标宫格，点卡片就地展开（网页稿 .scard）。"""
        P = self.P
        if getattr(self, "_set_grid", None) is not None:
            return
        g = tk.Frame(self.view_set, bg=C["bg"])
        g.pack(fill="both", expand=True, padx=P(18), pady=(P(12), P(6)))
        for i in range(3):
            g.columnconfigure(i, weight=1)
        # .setgrid{max-width:1060px}：窗口再宽也不把三张卡拉到爆，超出的宽度留白居中
        self._set_maxw = P(1060)

        def _cap(_e=None):
            vw = self.view_set.winfo_width()
            if vw > self._set_maxw + 2 * P(18):
                g.pack_configure(padx=(vw - self._set_maxw) // 2,
                                 pady=(P(12), P(6)))
            else:
                g.pack_configure(padx=P(18), pady=(P(12), P(6)))
        self.view_set.bind("<Configure>", _cap)
        self._set_grid = g
        self._set_cards = []
        self._set_open = None
        self._set_rows_w = 0

    def _bind_tree(self, w, fn):
        """把点击绑到整棵子树（标题/图标/说明文字都得能点，控件本体在 body 里不受影响）。"""
        try:
            w.bind("<Button-1>", fn)
            w.configure(cursor="hand2")
        except Exception:
            pass
        for c in w.winfo_children():
            self._bind_tree(c, fn)

    def _set_toggle(self, key):
        self._set_open = None if self._set_open == key else key
        self._set_layout()

    def _set_layout(self):
        """重排宫格：展开的那张跨 3 列并占满剩余高度，其余按 3 列顺排。"""
        P = self.P
        try:
            r = c = 0
            for i, (key, card, body, _head) in enumerate(self._set_cards):
                span = 3 if self._set_open == key else 1
                if c + span > 3:
                    r += 1
                    c = 0
                card.grid_forget()
                card.grid(row=r, column=c, columnspan=span, sticky="nsew",
                          padx=(0 if c == 0 else P(7), P(7)), pady=(0, P(14)))
                if span == 3:
                    body.pack(fill="both", expand=True)
                else:
                    body.pack_forget()
                c += span
                if c >= 3 and span != 3:
                    r += 1
                    c = 0
            # 行一律不给 weight：展开的卡片按内容高（原来给 1 会把「存储」这种
            # 只有一行的分组撑成半屏空白，网页稿是紧凑卡片）
            for rr in range(self._set_rows_w + 1):
                self._set_grid.rowconfigure(rr, weight=0, uniform="")
            self._set_rows_w = r
        except Exception:
            LOG.debug("设置宫格重排失败", exc_info=True)

    def _sec(self, title, sub=None, icon=None):
        """设置页分组卡片：图标 + 标题(+副标题) 常显，内容区点击就地展开。"""
        P = self.P
        self._set_init()
        if self._set_open is None and not self._set_cards:
            self._set_open = title          # 第一组（通用）默认展开
        card = Card(self._set_grid, bg=C["bg"], body_padx=P(18), body_pady=P(18))
        head = tk.Frame(card.body, bg=C["card"])
        head.pack(fill="x")
        if icon:
            # 图标块 44×44（.sic），线性图标 24×24 居中叠在上面
            tile = tk.Canvas(head, width=P(44), height=P(44), bg=C["card"],
                             highlightthickness=0)
            tile.pack(anchor="w", pady=(0, P(14)))
            ic = tk.Canvas(tile, width=P(24), height=P(24), bg=C["card"],
                           highlightthickness=0)
            ic.place(x=P(10), y=P(10))
            self._sec_tile(tile, ic, icon)
            self._sec_ics = getattr(self, "_sec_ics", [])
            self._sec_ics.append((tile, ic, icon))
        tk.Label(head, text=title, bg=C["card"], fg=C["ink"],
                 font=F_UI_B).pack(anchor="w")
        if sub:
            tk.Label(head, text=sub, bg=C["card"], fg=C["ink_l"],
                     font=F_SM).pack(anchor="w", pady=(P(4), 0))
        body = tk.Frame(card.body, bg=C["card"])
        # .body{margin-top:14px;border-top:1px solid var(--line2)}
        tk.Frame(body, bg=C["line"], height=1).pack(fill="x", pady=(P(14), P(6)))
        self._set_cards.append([title, card, body, head])
        self._bind_tree(head, lambda e, t=title: self._set_toggle(t))
        self._bind_tree(card, lambda e, t=title: self._set_toggle(t))
        self._set_layout()
        return body

    def _sec_tile(self, tile, cv, key):
        """网页稿 .scard .sic：44×44 圆角 12 的**淡底**图标块（底色 = 签名色 10% 淡），
        描边用签名色本身 —— 不是浓渐变底 + 白描边（那版做反了）。"""
        P = self.P
        try:
            tile.delete("tile")
            _grad_begin(tile)
        except Exception:
            return
        d = tile.winfo_reqwidth()
        soft = _mix(C["pink"], "#FFFFFF", 0.90)          # var(--acc-soft)
        _grad(tile, 0, 0, d, d, max(2, P(12)), soft, soft, 0, tags="tile")
        self._sec_icon(cv, key)

    def _sec_icon(self, cv, key):
        """分组图标的线性笔画（24×24，描边 = 签名色，对应 .sic svg{stroke:currentColor}）。"""
        P = self.P
        wd = max(1, int(round(P(1.8))))
        try:
            cv.delete("all")
            _grad_begin(cv)
        except Exception:
            return
        w = P(24)
        col = C["pink"]
        m = P(4)
        c = w / 2.0
        def line(*pts):
            cv.create_line(*pts, fill=col, width=wd, capstyle="round",
                           joinstyle="round")
        if key == "general":          # 电源/开关
            cv.create_arc(m, m, w - m, w - m, start=60, extent=300, style="arc",
                          outline=col, width=wd)
            line(c, m - P(1), c, c)
        elif key == "store":          # 箱子
            cv.create_rectangle(m, m + P(4), w - m, w - m, outline=col, width=wd)
            line(m, m + P(4), m + P(3), m, w - m - P(3), m, w - m, m + P(4))
            line(c, m + P(4), c, w - m)
        elif key == "dl":             # 下载箭头
            line(c, m, c, w - m - P(4))
            line(c - P(4), w - m - P(8), c, w - m - P(4))
            line(c + P(4), w - m - P(8), c, w - m - P(4))
            line(m, w - m, w - m, w - m)
        elif key == "cookie":         # 饼干
            r = (w - 2 * m) / 2.0
            cv.create_oval(c - r, c - r, c + r, c + r, outline=col, width=wd)
            for dx, dy in ((-.3, -.25), (.28, .05), (-.1, .3)):
                cv.create_oval(c + r * dx - P(1.2), c + r * dy - P(1.2),
                               c + r * dx + P(1.2), c + r * dy + P(1.2),
                               fill=col, outline="")
        elif key == "globe":          # 网络
            r = (w - 2 * m) / 2.0
            cv.create_oval(c - r, c - r, c + r, c + r, outline=col, width=wd)
            line(m, c, w - m, c)
            cv.create_oval(c - r * 0.45, c - r, c + r * 0.45, c + r, outline=col, width=wd)
        elif key == "pet":            # 桌宠（人形）
            cv.create_oval(c - P(3.5), m, c + P(3.5), m + P(7), outline=col, width=wd)
            cv.create_arc(m, c + P(1), w - m, w + P(6), start=0, extent=180,
                          style="arc", outline=col, width=wd)
        elif key == "info":           # 关于
            r = (w - 2 * m) / 2.0
            cv.create_oval(c - r, c - r, c + r, c + r, outline=col, width=wd)
            line(c, c - P(1), c, w - m - P(2))
            cv.create_oval(c - P(1), m + P(3), c + P(1), m + P(5), fill=col, outline="")

    def _show_view(self, key):
        if key not in self._VIEWS:
            return
        for k in self._VIEWS:
            fr = getattr(self, "view_" + k)
            if k == key:
                fr.pack(fill="both", expand=True)
            else:
                fr.pack_forget()
        self._cur_view = key
        for k, (row, ic, lb) in self._nav.items():
            on = (k == key)
            bg = C["pink_xl"] if on else C["side"]
            fg = C["pink_d"] if on else C["ink_l"]
            row.config(bg=bg)
            lb.config(bg=bg, fg=fg)
            ic.config(bg=bg)
            self._nav_icon(ic, k, fg)
        titles = {"dl": "下载", "queue": "队列", "dash": "值班室",
                  "log": "日志", "set": "设置"}
        try:
            self._title.config(text=titles.get(key, ""))
        except Exception:
            pass
        self._top_actions(key)
        self._topbar_chip()

    # 顶栏右上角：按视图给不同的动作（网页稿 .top .r）
    _TOP_ACTS = {"dash": (("重置统计", "_reset_stats"),),
                 "log": (("清空", "_clear_log_view"), ("复制", "_copy_log_view")),
                 "dl": (("搜索", "_focus_result_filter"),)}

    def _top_actions(self, key):
        P = self.P
        try:
            for w in self._top_act.winfo_children():
                w.destroy()
            for text, fn in self._TOP_ACTS.get(key, ()):
                RoundButton(self._top_act, text=text, kind="ghost",
                            padx=P(13), pady=P(6),
                            command=getattr(self, fn)).pack(side="left",
                                                            padx=(P(8), 0))
        except Exception:
            pass

    def _topbar_chip(self):
        """标题旁的计数胶囊：跟着当前视图 + 实时数据变（挂在 _status_refresh 上）。"""
        key = getattr(self, "_cur_view", None)
        txt = ""
        try:
            if key == "dl":
                txt = self.count_var.get() or ""
            elif key == "queue":
                rs = getattr(self, "row_status", {}) or {}
                v = list(rs.values())
                run = sum(1 for s in v if s and ("% " in s or "搬运" in s or "分片" in s))
                wait = sum(1 for s in v if s and ("待下载" in s or "排队" in s))
                done = sum(1 for s in v if s and s.startswith("✓"))
                txt = ("%d 进行中 · %d 排队 · %d 完成" % (run, wait, done)) \
                    if (run or wait or done) else "队列空闲"
            elif key == "dash":
                days = stats_days(1)
                t = days[-1] if days else ("", 0, 0)
                txt = "今日 %d 件 · %s" % (t[2], fmt_size(t[1])) if t[2] else "今天还没开张"
            elif key == "log":
                txt = "同步写入 sniffdl.log"
            elif key == "set":
                txt = "点卡片展开对应分组"
        except Exception:
            pass
        try:
            self._title_chip.set(text=txt)
        except Exception:
            pass

    def _reset_stats(self):
        if not messagebox.askyesno(
                "重置统计", "确定清空值班室的每日下载账本吗？\n"
                          "（热力图、今日战绩、累计传输都会归零，且不可撤销）"):
            return
        save_stats({})
        self._dash_last_hm = None
        try:
            self._dash_refresh()
        except Exception:
            pass
        self._topbar_chip()
        self.log("[统计] 每日账本已清空（从今天重新开始记）")

    def _upd_render(self, res=None):
        try:
            cur = (CU.current_versions() if CU else {}) or {}
            if res:
                parts = []
                need = False
                for r in res:
                    if r.get("need"):
                        need = True
                        parts.append("%s %s → 最新 %s（可更新）" % (
                            r["name"], r["cur"], r["latest"]))
                    else:
                        parts.append("%s %s（已最新）" % (r["name"], r.get("cur")))
                self._upd_line.config(text="组件版本：" + "；".join(parts))
                self._upd_btn.set_state("normal" if need else "disabled")
            else:
                self._upd_line.config(text="组件版本：yt-dlp %s · ffmpeg %s" % (
                    cur.get("yt-dlp", "?"), cur.get("ffmpeg", "?")))
        except Exception:
            pass

    def check_updates(self):
        """查 PyPI 上 yt-dlp 有没有新版；结果写日志 + 更新那一行，不弹窗。"""
        if not CU:
            self.log("[更新] 组件更新模块未随包发布，无法检查")
            return
        self.log("[更新] 正在检查组件更新…")
        self._upd_line.config(text="组件版本：检查中…")

        def done():
            try:
                self.root.after(0, lambda: self._upd_render(_STATE_RES[0]))
            except Exception:
                pass

        _STATE_RES = [None]

        def work():
            try:
                res = CU.check_all(self.log)
                _STATE_RES[0] = res
                self.root.after(0, lambda: self._upd_render(res))
            except Exception as e:
                self.log("[更新] 检查异常：%s %s" % (type(e).__name__, e))

        threading.Thread(target=work, daemon=True).start()

    def update_ytdlp(self):
        """把新版 yt-dlp 装到 %APPDATA%\\SniffDL\\pylibs（不碰内置版，删目录即回退）。"""
        if not CU:
            return
        if not messagebox.askyesno(
                "更新 yt-dlp",
                "将下载最新版 yt-dlp 装到用户目录（不覆盖软件内置版本，\n"
                "出问题时删掉该目录即可回退）。\n\n现在更新吗？"):
            return
        self._upd_btn.set_state("disabled")
        self._upd_btn.config(text="更新中…")

        def done():
            try:
                self._upd_btn.config(text="更新 yt-dlp")
                self._upd_render()
                self._upd_btn.set_state("disabled")
            except Exception:
                pass

        CU.update_pypi_pkg("yt-dlp", self.log, on_done=lambda: self.root.after(0, done))

    def _clear_log_view(self):
        try:
            self.log_text.delete("1.0", "end")
            self._set_log_greet(self._greet_line())
        except Exception:
            pass

    def _copy_log_view(self):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.log_text.get("1.0", "end-1c"))
            self.status_var.set("日志已复制到剪贴板")
        except Exception:
            pass

    def _focus_result_filter(self):
        try:
            self._show_view("dl")
            self._res_fent.focus_set()
            self._res_fent.icursor("end")
        except Exception:
            pass

    def _nav_hover(self, key, on):
        try:
            if key == getattr(self, "_cur_view", None):
                return
            row, ic, lb = self._nav[key]
            bg = C["card"] if on else C["side"]
            row.config(bg=bg)
            ic.config(bg=bg)
            lb.config(bg=bg)
        except Exception:
            pass

    def _nav_icon(self, cv, key, color):
        """在 20×20 画布上画细线性导航图标（stroke≈1.6，圆端点）。"""
        P = self.P
        w = P(20)
        try:
            cv.delete("all")
        except Exception:
            return
        wd = max(1, int(round(P(1.6))))
        def line(*pts, **kw):
            cv.create_line(*pts, fill=color, width=kw.get("width", wd),
                           capstyle="round", joinstyle="round", smooth=kw.get("smooth", False))
        m = P(3)          # 外边距
        c = w / 2.0
        if key == "dl":   # 向下箭头 + 托盘
            line(c, m, c, w - m - P(4))
            line(c - P(4), w - m - P(8), c, w - m - P(4))
            line(c + P(4), w - m - P(8), c, w - m - P(4))
            line(m, w - m, w - m, w - m)
        elif key == "queue":  # 清单：三条线 + 前置小点
            for i, yy in enumerate((m + P(1), c, w - m - P(1))):
                line(m + P(5), yy, w - m, yy)
                cv.create_oval(m, yy - P(1.4), m + P(2.8), yy + P(1.4),
                               outline=color, width=wd)
        elif key == "dash":   # 2×2 方格
            g = P(2)
            for rx in (m, c + g / 2):
                for ry in (m, c + g / 2):
                    cv.create_rectangle(rx, ry, rx + (c - m - g / 2), ry + (c - m - g / 2),
                                        outline=color, width=wd)
        elif key == "log":    # 文档 + 内容线
            cv.create_rectangle(m, m, w - m, w - m, outline=color, width=wd)
            line(m + P(3), m + P(4), w - m - P(3), m + P(4))
            line(m + P(3), c, w - m - P(3), c)
            line(m + P(3), w - m - P(4), w - m - P(6), w - m - P(4))
        elif key == "set":    # 齿轮：中心圆 + 八向齿
            r = P(3.4)
            cv.create_oval(c - r, c - r, c + r, c + r, outline=color, width=wd)
            import math
            for i in range(8):
                a = math.pi / 4 * i
                x1 = c + (r + P(0.6)) * math.cos(a)
                y1 = c + (r + P(0.6)) * math.sin(a)
                x2 = c + (r + P(3)) * math.cos(a)
                y2 = c + (r + P(3)) * math.sin(a)
                line(x1, y1, x2, y2)

    def _refresh_profile(self):
        P = self.P
        th = THEMES.get(self.theme) or {}
        try:
            self._me_name.config(text=th.get("label") or "SniffDL")
            self._me_role.config(text=th.get("role") or "")
        except Exception:
            pass
        cv = getattr(self, "_me_av", None)
        if cv is None:
            return
        cv.delete("all")
        _grad_begin(cv)
        d = P(52)
        band = max(2, P(2))
        img = _pfp_img(d - 2 * band - 2, self.theme, "AVATAR")
        if img is None:
            art = _art("AVATAR", self.S)
            if art is not None:
                try:
                    f = max(1, int(round(art.width() / float(P(48)))))
                    img = art.subsample(f, f) if f > 1 else art
                except Exception:
                    img = None
        if img is not None:
            cv.create_image(d // 2, d // 2, image=img)
            self._me_av_img = img
        else:
            cv.create_text(d // 2, d // 2, text="DL", fill=C["plum"], font=F_UI_B)
        c1, c2 = _grad_pair()
        _grad_ring(cv, d // 2, d // 2, d - 1, c1, c2,
                   frac=2.0 * band / max(2.0, d - 1), tags="ring")
        self._draw_logo()
        self._dress_refresh()

    def _dress_fit(self, n):
        """换装 chip 排布：把侧栏可用宽按只数均分（chip 上限 P(38)）。
        返回 (边长, 左右间距, 描边环宽) —— 角色增减都自动适配，不会再挤出侧栏。"""
        P = self.P
        avail = max(P(60), P(236) - 2 * P(14) - P(4))
        n = max(1, int(n))
        size = max(P(22), min(P(38), avail // n))
        gap = max(0, (avail - size * n) // (2 * n))
        ring = max(2, size // 14)
        return size, gap, ring

    def _dress_ring(self, cell, cx, cy, d, c1, c2, band, tags):
        """换装头像的描边环（渐变）：band = 环宽（像素）。"""
        _grad_ring(cell, cx, cy, d, c1, c2, frac=2.0 * max(1, band) / max(2.0, d),
                   tags=tags)
        cell.tag_lower(tags, "img")

    def _grad_strip(self, cv, w=None, h=None):
        """卡片底部签名色条 → 横向双色渐变。cv._gp = (色板键1, 色板键2)，运行时现取。"""
        try:
            w = w or cv.winfo_width()
            h = h or cv.winfo_height()
            if w < 3 or h < 1:
                return
            cv.delete("all")
            _grad_begin(cv)
            gp = getattr(cv, "_gp", None) or ()
            c1 = C.get(gp[0]) if len(gp) > 0 else None
            c2 = C.get(gp[1]) if len(gp) > 1 else None
            c1, c2 = _grad_pair(c1, c2)
            _grad(cv, 0, 0, w, h, 0, c1, c2, 0, tags="g")
        except Exception:
            pass

    def _dress_hover(self, k, on):
        try:
            cell = getattr(self, "_dress_imgs", {}).get(k)
            if cell is None or k == self.theme:
                return
            cell.delete("hover")
            if on:
                size = cell.winfo_reqwidth()
                self._dress_ring(cell, size // 2, size // 2, size - 1,
                                 _mix(C["line"], "#FFFFFF", 0.15), C["pink_l"],
                                 max(2, size // 16), "hover")
        except Exception:
            pass

    def _dress_refresh(self):
        P = self.P
        geo = getattr(self, "_dress_geo", None) or (P(36), P(2), P(3))
        size, gap, ring = geo
        c1, c2 = _grad_pair()
        for k, cell in getattr(self, "_dress_imgs", {}).items():
            cell.delete("ring")
            cell.delete("hover")
            if k == self.theme:
                self._dress_ring(cell, size // 2, size // 2, size - 1,
                                 c1, c2, ring, "ring")

    def _build_statusbar(self):
        """底部常驻状态条：引擎/RPC 状态 · 当前任务 · 实时速度 · 任务数。
        右侧三个指标按网页稿 .sppill 做成圆角胶囊（Pill 支持 .config(text=,fg=)）。"""
        P = self.P
        sb = self.statusbar
        _ok, _dim, _mut = "#3BA55D", C["ink_l"], C["ink_l"]
        left = tk.Frame(sb, bg=C["card"])
        left.pack(side="left", padx=(P(16), 0))
        self._sb_engine = tk.Label(left, text="● 就绪", bg=C["card"], fg=_ok, font=F_SM)
        self._sb_engine.pack(side="left")
        tk.Label(left, text="  ·  ", bg=C["card"], fg=_dim, font=F_SM).pack(side="left")
        self._sb_rpc = tk.Label(left, text="", bg=C["card"], fg=_mut, font=F_SM)
        self._sb_rpc.pack(side="left")
        right = tk.Frame(sb, bg=C["card"])
        right.pack(side="right", padx=(0, P(14)))
        self._sb_count = Pill(right, text="", fg=("ink_l", 0.0),
                              tint=("ink_l", 0.93), border=C["line"],
                              font=F_SM_B, padx=10, pady=3)
        self._sb_count.pack(side="right")
        self._sb_up = Pill(right, text="", fg=("pink2", 0.0),
                           tint=("pink2", 0.92), border=C["line"],
                           font=F_SM_B, padx=10, pady=3)
        self._sb_up.pack(side="right", padx=(0, P(6)))
        self._sb_speed = Pill(right, text="↓ 0 B/s", fg=("pink_d", 0.0),
                              tint=("pink", 0.92), border=("pink", 0.55),
                              font=F_UI_B, padx=12, pady=3)
        self._sb_speed.pack(side="right", padx=(0, P(6)))
        self._sb_cur = tk.Label(sb, text="", bg=C["card"], fg=_mut, font=F_SM, anchor="w")
        self._sb_cur.pack(side="left", fill="x", expand=True, padx=P(16))

    def _status_refresh(self):
        """从 self._as / row_status 拉实时数据刷新状态条（挂在 _queue_tick 上，~800ms 一次）。"""
        try:
            a = getattr(self, "_as", {}) or {}
            mode = a.get("mode", "idle")
            rs = getattr(self, "row_status", {}) or {}
            vals = list(rs.values())
            n_run = sum(1 for s in vals if s and ("% " in s or "搬运" in s or "分片" in s))
            n_done = sum(1 for s in vals if s and s.startswith("✓"))
            n_wait = sum(1 for s in vals if s and ("待下载" in s or "排队" in s))
            n_fail = sum(1 for s in vals if s and s.startswith("✗"))
            # 引擎圆点
            if mode in ("busy", "pause"):
                self._sb_engine.config(text="● 工作中", fg=C["pink"])
            elif mode == "done":
                self._sb_engine.config(text="● 完成", fg="#3BA55D")
            elif mode == "fail":
                self._sb_engine.config(text="● 有失败", fg="#D14343")
            else:
                self._sb_engine.config(text="● 就绪", fg="#3BA55D")
            # RPC 状态
            try:
                if getattr(self, "_rpc_server", None) is not None:
                    self._sb_rpc.config(text="RPC :%s 运行中" % (self.rpc_port_var.get() or ""))
                else:
                    self._sb_rpc.config(text="RPC 未启用")
            except Exception:
                pass
            # 当前任务
            if mode in ("busy", "pause") and a.get("title"):
                eta = ("  ·  " + a["eta"]) if a.get("eta") else ""
                self._sb_cur.config(text="[%s/%s] 正在搬运：%s%s" % (
                    a.get("n", "?"), a.get("total", "?"), a["title"][:40], eta))
            else:
                self._sb_cur.config(text="")
            # 速度 / 任务数
            self._sb_speed.config(text="↓ " + (a.get("speed") or "0 B/s"),
                                  fg=C["pink"] if (mode == "busy") else C["ink"])
            self._sb_up.config(text="↑ BT" if getattr(self, "_bt_active", False) else "")
            parts = []
            if n_run:
                parts.append("进行 %d" % n_run)
            if n_wait:
                parts.append("排队 %d" % n_wait)
            if n_done:
                parts.append("完成 %d" % n_done)
            if n_fail:
                parts.append("失败 %d" % n_fail)
            self._sb_count.config(text="任务 " + (" · ".join(parts) if parts else "空闲"))
            self._topbar_chip()
        except Exception:
            pass

    def _queue_tick(self):
        try:
            if getattr(self, "_cur_view", None) == "queue":
                self._queue_refresh()
        except Exception:
            pass
        try:
            self._status_refresh()
        except Exception:
            pass
        try:
            self._qmem_mark()          # 记忆系统：脏了就落盘（内部 5 秒节流，主线程顺手写）
        except Exception:
            pass
        try:
            if getattr(self, "_cur_view", None) == "dash":
                self._dash_refresh()
        except Exception:
            pass
        try:
            self.root.after(800, self._queue_tick)
        except Exception:
            pass

    def _qtree_tags(self):
        """队列树配色集中在这里：奇偶行底色 + 四种状态前景。抽成方法的原因是换角色
        时 C 变了，而这些属性原来只在开机时写一次 →「进行中」一直是上个角色的颜色。"""
        try:
            t = self.qtree
            t.tag_configure("odd", background=C["card"])
            t.tag_configure("even", background=C["pink_xl"])
            t.tag_configure("st_ok", foreground="#2E8B57")
            t.tag_configure("st_run", foreground=C["pink_d"])
            t.tag_configure("st_err", foreground="#C0392B")
            t.tag_configure("st_wait", foreground=C["ink_l"])
        except Exception:
            pass

    def _q_blockbar(self, frac):
        if frac is None:
            return "—", "st_wait"
        f = max(0.0, min(1.0, frac))
        n = int(round(f * 10))
        return ("▰" * n + "▱" * (10 - n)) + " %d%%" % int(f * 100), None

    def _queue_refresh(self):
        rs = getattr(self, "row_status", {}) or {}
        ent = getattr(self, "entries", []) or []
        rows = []
        for i, e in enumerate(ent):
            st = rs.get(i)
            if st is None:
                continue
            frac, tag = None, "st_wait"
            s = str(st)
            if s.startswith("✓"):
                frac, tag = 1.0, "st_ok"
            elif s.startswith("✗"):
                frac, tag = None, "st_err"
            elif "分片" in s:
                try:
                    a, b = s.split("分片")[1].strip().split("/")
                    frac = int(a) / float(b)
                except Exception:
                    frac = None
                tag = "st_run"
            else:
                import re as _re
                m = _re.search(r"(\d+)\s*%", s)
                if m:
                    frac = int(m.group(1)) / 100.0
                    tag = "st_run"
                elif "待下载" in s or "排队" in s:
                    frac, tag = 0.0, "st_wait"
            bar, _ = self._q_blockbar(frac)
            rows.append((str(e.get("title") or e.get("url") or "")[:60],
                         fmt_size(e.get("size") or 0), bar, s, tag))
        sig = tuple(rows)
        if sig == getattr(self, "_q_last", None):
            return
        self._q_last = sig
        t = self.qtree
        t.delete(*t.get_children())
        if not rows:
            t.insert("", "end", values=("（暂无下载任务）", "", "", ""), tags=("odd",))
            return
        for n, (name, size, bar, st, tag) in enumerate(rows):
            base = ("even",) if n % 2 else ("odd",)
            t.insert("", "end", values=(name, size, bar, st), tags=base + (tag,))

    # ---- 值班室（仪表板）----
    def _build_dash(self):
        P = self.P
        v = self.view_dash
        pad = tk.Frame(v, bg=C["bg"])
        pad.pack(fill="both", expand=True, padx=P(18), pady=(P(12), P(6)))
        # 三行：① KPI 四卡定高 ② 中间行吃掉剩余高度（消灭底部留白）③ 一言定高
        pad.rowconfigure(1, weight=1)
        pad.columnconfigure(0, weight=1)
        # ---- ① KPI 四卡（单独一层 4 列等宽网格，别和下面的 2 列混用）----
        krow = tk.Frame(pad, bg=C["bg"])
        krow.grid(row=0, column=0, sticky="ew")
        for i in range(4):
            krow.columnconfigure(i, weight=1)
        self._dash_kpi = {}
        self._dash_bar = {}
        for i, (k, t) in enumerate([("engine", "引擎"), ("today", "今日战绩"),
                                    ("speed", "当前速度"), ("total", "累计传输")]):
            card = Card(krow, bg=C["bg"], body_padx=P(14), body_pady=P(4))
            card.grid(row=0, column=i, sticky="nsew",
                      padx=(0 if i == 0 else P(10), 0))
            body = card.body
            # 底部签名色条：横向双色渐变（存色板键名，换肤时现取 → 不把颜色写死）。
            # 放进 body 而不是贴卡片最外沿：Tk 没有 CSS 的圆角裁切，贴边会戳出圆角。
            bar = tk.Canvas(body, height=max(3, P(3)), bg=C["card"],
                            highlightthickness=0)
            bar.pack(side="bottom", fill="x", pady=(0, P(4)))
            bar._gp = _KPI_GRAD.get(k)
            bar.bind("<Configure>",
                     lambda e, b=bar: self._grad_strip(b, e.width, e.height))
            self._dash_bar[k] = bar
            tk.Label(body, text=t, bg=C["card"], fg=C["ink_l"], font=F_SM).pack(
                anchor="w", pady=(P(10), 0))
            val = tk.Label(body, text="—", bg=C["card"], fg=C["ink"],
                           font=("Segoe UI", 20, "bold"))
            val.pack(anchor="w")
            sub = tk.Label(body, text="", bg=C["card"], fg=C["ink_l"], font=F_SM)
            sub.pack(anchor="w", pady=(0, P(8)), fill="x")
            self._dash_kpi[k] = (val, sub)
        # ---- ② 下载活动（热力图）+ 任务概况：这一行随窗口高度撑开 ----
        mrow = tk.Frame(pad, bg=C["bg"])
        mrow.grid(row=1, column=0, sticky="nsew", pady=(P(10), 0))
        mrow.rowconfigure(0, weight=1)
        mrow.columnconfigure(0, weight=16)
        mrow.columnconfigure(1, weight=9)
        hm_card = Card(mrow, bg=C["bg"], body_padx=P(14), body_pady=P(4))
        hm_card.grid(row=0, column=0, sticky="nsew", padx=(0, P(10)))
        hbody = hm_card.body
        hh = tk.Frame(hbody, bg=C["card"])
        hh.pack(fill="x", pady=(P(10), 0))
        tk.Label(hh, text="下载活动", bg=C["card"], fg=C["ink"],
                 font=F_UI_B).pack(side="left")
        tk.Label(hh, text="近 16 周 · 按天字节量", bg=C["card"], fg=C["ink_l"],
                 font=F_SM).pack(side="right")
        self._dash_hm = tk.Canvas(hbody, bg=C["card"], highlightthickness=0)
        self._dash_hm.pack(fill="both", expand=True, pady=(P(4), P(12)))
        self._dash_hm.bind("<Configure>", lambda e: self._hm_resize())
        st_card = Card(mrow, bg=C["bg"], body_padx=P(14), body_pady=P(4))
        st_card.grid(row=0, column=1, sticky="nsew")
        sbody = st_card.body
        sh = tk.Frame(sbody, bg=C["card"])
        sh.pack(fill="x", pady=(P(10), 0))
        tk.Label(sh, text="任务概况", bg=C["card"], fg=C["ink"],
                 font=F_UI_B).pack(side="left")
        tk.Label(sh, text="实时", bg=C["card"], fg=C["ink_l"],
                 font=F_SM).pack(side="right")
        self._dash_stat = {}
        for k, t in (("run", "下载中"), ("wait", "排队"), ("done", "今日完成"),
                     ("fail", "失败 / 冷却"), ("dir", "保存到"), ("disk", "磁盘剩余")):
            r = tk.Frame(sbody, bg=C["card"])
            r.pack(fill="x", pady=P(4))
            tk.Label(r, text=t, bg=C["card"], fg=C["ink_l"], font=F_SM).pack(side="left")
            vv = tk.Label(r, text="—", bg=C["card"], fg=C["ink"], font=F_UI_B)
            vv.pack(side="right")
            self._dash_stat[k] = vv
        # 「最近完成」：downloaded.json 里本来就有 name/dir/size/ts，只是界面上看不到
        # （用户原话：下载完成之后这里没有任何记录，光有页面没功能那算什么）
        tk.Frame(sbody, bg=C["line"], height=1).pack(fill="x", pady=(P(10), P(6)))
        tk.Label(sbody, text="最近完成", bg=C["card"], fg=C["ink"],
                 font=F_UI_B).pack(anchor="w", pady=(0, P(2)))
        self._dash_hist = []
        for _i in range(6):
            hr = tk.Frame(sbody, bg=C["card"], cursor="hand2")
            hr.pack(fill="x", pady=1)
            nm = tk.Label(hr, text="", bg=C["card"], fg=C["ink_l"], font=F_SM,
                          anchor="w")
            nm.pack(side="left", fill="x", expand=True)
            z2 = tk.Label(hr, text="", bg=C["card"], fg=C["ink_l"], font=F_SM)
            z2.pack(side="right")
            hr._nm, hr._sz, hr._path = nm, z2, None
            for _w in (hr, nm, z2):
                _w.bind("<Button-1>", lambda e, row=hr: self._open_hist_row(row))
                _w.bind("<Enter>", lambda e, row=hr: row._nm.config(fg=C["pink_d"]))
                _w.bind("<Leave>", lambda e, row=hr: row._nm.config(fg=C["ink_l"]))
            self._dash_hist.append(hr)
        # ---- ③ 看板娘一言 ----
        me_card = Card(pad, bg=C["bg"], body_padx=P(12), body_pady=P(8))
        self._dash_me_card = me_card    # 同上：留白/圆角断言要用它
        me_card.grid(row=2, column=0, sticky="nsew", pady=(P(10), P(4)))
        mbody = me_card.body
        self._dash_av = tk.Canvas(mbody, width=P(52), height=P(52), bg=C["card"],
                                  highlightthickness=0)
        self._dash_av.pack(side="left", padx=(0, P(10)))
        col = tk.Frame(mbody, bg=C["card"])
        col.pack(side="left", fill="x", expand=True, pady=P(6))
        who = tk.Label(col, text="", bg=C["card"], fg=C["pink"], font=F_UI_B,
                       anchor="w")
        who.pack(anchor="w")
        self._dash_who = who
        bub = tk.Label(col, text="", bg=C["pink_xl"], fg=C["ink"], font=F_UI,
                       anchor="w", justify="left", wraplength=P(540),
                       padx=P(12), pady=P(7))
        bub.pack(anchor="w", pady=(P(2), 0))
        self._dash_bub = bub
        self._dash_last_hm = None


    def _open_hist_row(self, row):
        """点「最近完成」某行 → 资源管理器打开所在目录并高亮该文件（拉到前台）。"""
        p = getattr(row, "_path", None)
        if not p or not os.path.exists(p):
            self.status_var.set("这个文件已经不在原来的位置了～")
            return
        self._reveal_in_explorer(p)

    def _dash_hist_refresh(self):
        """回填最近 6 条完成记录（按时间倒序）；内容没变就不动，避免每拍重画。"""
        try:
            h = load_history() or {}
            items = []
            for _url, v in h.items():
                if isinstance(v, dict):
                    d, nm = v.get("dir") or "", v.get("name") or ""
                    sz, ts = int(v.get("size") or 0), float(v.get("ts") or 0)
                else:
                    d, nm, sz, ts = "", str(v or ""), 0, 0
                if nm:
                    items.append((ts, nm, d, sz))
            items.sort(reverse=True)
            sig = tuple(x[1] for x in items[:6])
            if sig == getattr(self, "_dash_hist_sig", None):
                return
            self._dash_hist_sig = sig
            import datetime
            for i, row in enumerate(getattr(self, "_dash_hist", [])):
                if i >= len(items):
                    row._nm.config(text="")
                    row._sz.config(text="")
                    row._path = None
                    continue
                ts, nm, d, sz = items[i]
                when = (datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
                        if ts else "—")
                full = os.path.join(d, nm) if d else nm
                row._path = full if os.path.exists(full) else None
                row._nm.config(text="%s  %s" % (_clip(nm, self.P(190)), when),
                               fg=C["ink"] if row._path else C["ink_l"])
                row._sz.config(text=fmt_size(sz) if sz else "")
        except Exception:
            LOG.debug("最近完成列表刷新失败", exc_info=True)

    def _hm_resize(self):
        """热力图格子边长跟着卡片可用宽高走（原来写死 P(14)，卡大图小 → 一片留白）。"""
        try:
            cv = self._dash_hm
            w, h = cv.winfo_width(), cv.winfo_height()
            if w < 40 or h < 40:
                return
            days = stats_days(16 * 7)
            self._draw_heatmap(days)
        except Exception:
            pass

    def _dash_draw_avatar(self):
        P = self.P
        cv = getattr(self, "_dash_av", None)
        if cv is None:
            return
        cv.delete("all")
        _grad_begin(cv)          # 本方法挂在 800ms 计时器上，贴图全部走缓存
        d = P(52)
        band = max(2, P(2))
        img = _pfp_img(d - 2 * band - 2, self.theme, "AVATAR")
        if img is None:
            art = _art("AVATAR", self.S)
            if art is not None:
                try:
                    f = max(1, int(round(art.width() / float(P(48)))))
                    img = art.subsample(f, f) if f > 1 else art
                except Exception:
                    img = None
        if img is not None:
            cv.create_image(d // 2, d // 2, image=img)
            self._dash_av_img = img
        else:
            cv.create_text(d // 2, d // 2, text="DL", fill=C["plum"], font=F_UI_B)
        c1, c2 = _grad_pair()
        _grad_ring(cv, d // 2, d // 2, d - 1, c1, c2,
                   frac=2.0 * band / max(2.0, d - 1), tags="ring")

    def _draw_heatmap(self, days):
        P = self.P
        cv = getattr(self, "_dash_hm", None)
        if cv is None:
            return
        cv.delete("all")
        _grad_begin(cv)
        acc, acc2 = _grad_pair()
        # 格子边长跟着画布走：16 周 × 7 天固定，卡多大格子就多大（原来写死 P(14) → 留白）
        cw_px = max(P(240), cv.winfo_width())
        ch_px = max(P(120), cv.winfo_height())
        weeks = 16
        gap = max(2, P(3))
        leg_h = P(22)
        cell = min((cw_px - P(12) - (weeks - 1) * gap) // weeks,
                   (ch_px - P(10) - leg_h - 6 * gap) // 7)
        cell = int(max(P(6), min(cell, P(30))))
        rad = max(2, cell // 4)
        _tiles = {}

        def tile_pair(lvl):
            """档位 → 双色：淡→浓按 lvl 分档，两色一起加深（签名色 + 第二签名色）。"""
            if lvl not in _tiles:
                if lvl <= 0:
                    _tiles[lvl] = (_mix(C["line"], "#FFFFFF", 0.3),
                                   _mix(C["line"], "#FFFFFF", 0.3))
                else:
                    t = 0.25 * lvl + 0.15
                    _tiles[lvl] = (_mix(C["card"], acc, t), _mix(C["card"], acc2, t))
            return _tiles[lvl]
        # 按列（周）×行（7 天）排；days 为时间正序，补齐到 7 的倍数
        total = len(days)
        lead = (7 - (total % 7)) % 7
        seq = [None] * lead + list(days)
        maxb = max([b for (_, b, _) in days if b] + [1])
        for idx, item in enumerate(seq):
            col = idx // 7
            row = idx % 7
            x0 = P(4) + col * (cell + gap)
            y0 = P(4) + row * (cell + gap)
            if item is None:
                continue
            b = item[1]
            lvl = 0 if b <= 0 else min(4, int(b / maxb * 4) + 1)
            c1, c2 = tile_pair(lvl)
            _grad(cv, x0, y0, cell, cell, rad, c1, c2, 45, tags="hm")
        # 少→多图例：紧跟网格下方、靠左（网页稿 .hmlb 就是贴在格子下面，不在右下角）
        leg_w = P(20) + 5 * (cell + P(2)) + P(20)
        lx = P(4)
        ly = P(4) + 7 * (cell + gap) + P(2)
        cv.create_text(lx, ly + cell // 2, text="少", fill=C["ink_l"], font=F_SM,
                       anchor="w")
        for i in range(5):
            c1, c2 = tile_pair(i)
            bx = lx + P(20) + i * (cell + P(2))
            _grad(cv, bx, ly, cell, cell, rad, c1, c2, 45, tags="hm")
        # 「多」要排在最后一格右侧（原来 +P(4) 落在格子左边，字压在色块上）
        cv.create_text(bx + cell + P(5), ly + cell // 2, text="多",
                       fill=C["ink_l"], font=F_SM, anchor="w")

    def _dash_refresh(self):
        try:
            import shutil
            days = stats_days(16 * 7)
            today = days[-1] if days else ("", 0, 0)
            tot_b = sum(b for (_, b, _) in days)
            tot_n = sum(n for (_, _, n) in days)
            a = getattr(self, "_as", {}) or {}
            mode = a.get("mode", "idle")
            # KPI：引擎
            ev, es = self._dash_kpi["engine"]
            ev.config(text="工作中" if mode in ("busy", "pause") else "就绪",
                      fg=C["pink"] if mode in ("busy", "pause") else "#3BA55D")
            try:
                _rpc = ("RPC :%s 运行中" % self.rpc_port_var.get()) \
                    if getattr(self, "_rpc_server", None) is not None else "RPC 未启用"
                es.config(text="aria2 + yt-dlp · " + _rpc)
            except Exception:
                pass
            # KPI：今日战绩
            tv, ts = self._dash_kpi["today"]
            tv.config(text="%d 件" % today[2])
            ts.config(text=fmt_size(today[1]) + " 今日已下载")
            # KPI：当前速度
            sv, ss = self._dash_kpi["speed"]
            sv.config(text=(a.get("speed") or "0 B/s").replace("/s", ""),
                      font=("Segoe UI", 20, "bold"))
            nrun = sum(1 for s in (getattr(self, "row_status", {}) or {}).values()
                       if s and ("% " in s or "搬运" in s or "分片" in s))
            ss.config(text=("%d 个任务搬运中" % nrun) if nrun else "空闲")
            # KPI：累计
            zv, zs = self._dash_kpi["total"]
            zv.config(text=fmt_size(tot_b).split(" ")[0])
            zs.config(text=("%d 件 · " % tot_n if tot_n else "") + "自 v0.5.9 起记录")
            # 任务概况
            rs = getattr(self, "row_status", {}) or {}
            vals = list(rs.values())
            self._dash_stat["run"].config(text=str(sum(
                1 for s in vals if s and ("% " in s or "搬运" in s or "分片" in s))))
            self._dash_stat["wait"].config(text=str(sum(
                1 for s in vals if s and ("待下载" in s or "排队" in s))))
            self._dash_stat["done"].config(text=str(today[2]))
            self._dash_stat["fail"].config(text=str(sum(1 for s in vals if s and s.startswith("✗"))))
            out = (self.dir_var.get() if hasattr(self, "dir_var") else "") or DEFAULT_OUTDIR
            self._dash_stat["dir"].config(text="…" + os.path.sep + os.path.basename(out.rstrip(os.path.sep)))
            try:
                free = shutil.disk_usage(out if os.path.exists(out) else DEFAULT_OUTDIR).free
                self._dash_stat["disk"].config(text=fmt_size(free))
            except Exception:
                pass
            # 一言
            th = THEMES.get(self.theme) or {}
            self._dash_who.config(text=th.get("label") or "看板娘")
            say = {
                "maid": "今日份的战绩，桃桃替你记在小本本上。",
                "hanfu": "账目清晰，才算替主人守好库房。",
                "miko": "小夜的收集台账，比月初干净多了。",
                "horn": "这些亮闪闪的，全是猲替主人叼回来的！",
                "jiao": "每一步足迹都算数——今天的战果在这里。",
                "qing": "打榜数据出炉，蜃的主场成绩不错吧？",
            }.get(self.theme, "今天的战果，都在这里啦。")
            self._dash_bub.config(text=say if tot_n or today[2] else
                                  (say + "  （还没开过张，去「下载」贴个网址试试～）"))
            self._dash_draw_avatar()
            self._dash_hist_refresh()
            # KPI 渐变条：主题变了、或卡片宽度变了（首帧宽度要等布局完成才知道）才重绘
            _bsig = (self.theme, tuple(b.winfo_width()
                                       for b in getattr(self, "_dash_bar", {}).values()))
            if _bsig != getattr(self, "_dash_bar_sig", None):
                self._dash_bar_sig = _bsig
                for _b in getattr(self, "_dash_bar", {}).values():
                    self._grad_strip(_b)
            # 热力图（数据签名变了才重绘，省 CPU）
            sig = tuple(b for (_, b, _) in days)
            if sig != getattr(self, "_dash_last_hm", None):
                self._dash_last_hm = sig
                self._draw_heatmap(days)
        except Exception:
            pass

    # ---- 头栏（旧装饰已移除；保留方法名，改为刷新侧栏 profile）----
    def _draw_header(self):
        try:
            self._refresh_profile()
        except Exception:
            pass
        return
        cv, P = self.header, self.P
        cv.delete("all")
        self._chip_hits = []
        w = cv.winfo_width()
        if w < 50:
            w = P(1200)
        h = P(108)
        th = THEMES.get(self.theme) or {}
        art = _art("AVATAR", self.S)
        aw = art.width() if art is not None else P(64)
        tx = P(18) + aw + P(16)
        lw = tx + P(286)
        lay = _layout()
        rad = P(max(2, lay["radius"]))
        mono = lay["mono"]
        ft_title = _mono(F_TITLE) if mono else F_TITLE
        ft_sm = _mono(F_SM) if mono else F_SM
        _sep = {"hud": "//", "seal": "·", "stars": " ✦ ", "orn": " ❦ ",
                "soft": " ♡ "}.get(lay["accent"], " ♡ ")
        if self.theme == "miko":          # 巫女：音符分隔，比大圆点更像小夜
            _sep = " ♪ "
        # 先算换装头像条占位，画窗据此让出空间（窄窗时自动缩一号或留白）
        chips = []
        for _k in THEME_ORDER:
            _im = _art("CHIP", self.S, _k)
            if _im is not None:
                chips.append((_k, _im))
        gap = P(6) if chips else 0
        cw = chips[0][1].width() if chips else 0
        need = len(chips) * cw + max(0, len(chips) - 1) * gap
        lx = lw + P(34)
        fit_chips = bool(chips) and (lx + need < w - P(18))
        _custom = self._world_header_frame(cv, w, h, lw, rad, lay)
        if not _custom:
            _rr(cv, 0, 2, w - 1, h, rad, fill=C["card"], outline=C["line"])
            _rr(cv, 0, 2, lw, h, rad, fill=C["pink_xl"], outline=C["line"])
            cv.create_rectangle(lw - rad, 3, lw, h - 2, fill=C["pink_xl"], outline="")
        # 五位助手：右侧一整扇「世界画窗」（真实摄影/古画素材，边缘已烘进卡片色）
        self._paint_header_vision(cv, w, h, lw,
                                  (lx + need + P(10)) if fit_chips else (lw + P(2)))
        self._vision_overlay(cv, w, h, lw)
        if art is not None:
            cv.create_image(P(18), (h - 4 - art.height()) // 2 + 2, image=art, anchor="nw")
        else:
            _rr(cv, P(18), P(18), P(18) + aw, P(18) + aw, P(aw // 2),
                fill=C["pink_l"], outline=C["pink"])
            cv.create_text(P(18) + aw // 2, P(18) + aw // 2, text="DL",
                           font=ft_title, fill=C["plum"])
        _title = "SniffDL%s%s" % (_sep, th.get("char") or "女仆")
        self._world_title(cv, tx, P(40), _title, ft_title, anchor="w")
        _mood = getattr(self, "_mood_text", "") or (
            "%s｜给一个网址，下载页面所有视频" % (th.get("note") or ""))
        _mood_col = C["pink_d"] if getattr(self, "_mood_kind", "idle") in ("sniff", "carry") else C["ink_l"]
        cv.create_text(tx + P(2), P(72), anchor="w",
                       text=_mood, font=ft_sm, fill=_mood_col)
        # 右侧「换装」：一排角色小头像，点一下就换一种风格一种心情
        if chips and fit_chips:
            cv.create_text(lx - P(6), P(54), anchor="e", text="换装 ♡",
                           font=F_SM_B, fill=C["ink_l"])
            x = lx
            cy0 = (h - 4 - cw) // 2 + 2
            for k, im in chips:
                sel = (k == self.theme)
                _rr(cv, x - 3, cy0 - 3, x + cw + 2, cy0 + cw + 2, P(13),
                    fill=C["pink"] if sel else C["card"],
                    outline=C["pink_d"] if sel else C["line"])
                cv.create_image(x, cy0, image=im, anchor="nw",
                                tags="chip_%s" % k)
                self._chip_hits.append((k, x - 4, cy0 - 4, x + cw + 4, cy0 + cw + 4))
                x += cw + gap
        if not _custom:
            if lay["accent"] == "soft":
                _deco(cv, lw - P(34), P(22), P(6), C["pink_l"])
                _deco(cv, lw - P(58), P(86), P(7), C["sky"])
                _deco(cv, lw - P(14), P(94), P(4), C["gold"])
            else:
                try:
                    _accent(cv, lay["accent"], 2, 4, lw, h - 2,
                            C["pink"], C["line"], tags="hacc")
                except Exception:
                    _deco(cv, lw - P(34), P(22), P(6), C["pink_l"])
        # 每个角色一个专属「世界」头栏装饰 + 环境动画
        self._world_header(cv, w, h, lw)
        self._seed_ambient(cv)

    def _on_header_click(self, e):
        for k, x0, y0, x1, y1 in getattr(self, "_chip_hits", []):
            if x0 <= e.x <= x1 and y0 <= e.y <= y1:
                if k != self.theme:
                    self.apply_theme(k)
                return
        # 点头像区＝逗一下角色
        if e.x < self.P(110):
            self.poke()

    def _say_line(self, kind, n=None):
        try:
            txt = self.line_for(kind, n)
        except Exception:
            txt = ""
        if txt:
            self.say(txt)

    def poke(self, zone=None):
        """点角色：摸头 / 戳身体 / 连点过快 —— 三种反应（对齐 v2：桃桃抱抱、五人害羞/开心/嘟嘴/惊讶）。"""
        import random
        # cast24 版角色：点一下给个干净、看得清的互动反应，绝不闪成功
        if self._c24() is not None:
            import random
            now = time.time()
            c = self._c24()
            fr = getattr(c, "FRAMES", {}) or {}
            n_idle = len(fr.get("idle") or []) or 1
            # §5.1 连点递进：惊讶 → 开心 → 卖萌 → 害羞 → 躲开（300~400ms 窗口内计次，超时清零）
            if all(k in fr for k in ("poke", "happy")):
                if now < getattr(self, "_c24_ignore_until", 0.0):
                    return                      # 躲开后的冷却：数秒内不理人
                if now - getattr(self, "_c24_click_t", 0.0) > 0.4:
                    self._c24_click_n = 0
                self._c24_click_n = min(getattr(self, "_c24_click_n", 0) + 1, 6)
                self._c24_click_t = now
                nth = self._c24_click_n
                want = ("poke", "happy", "pout", "shy", "dodge")[min(nth, 5) - 1]
                if want not in fr:              # 素材没这组分镜就退回最接近的
                    want = "poke" if "poke" in fr else "happy"
                sets = fr.get(want) or [[]]
                dur = len(sets[0] or []) * C24.ticks(c, want) / 1000.0
                self._c24_click_state = want
                self._c24_poke_until = now + max(1.2, dur)
                if nth >= 5:
                    self._c24_ignore_until = now + 6.0
            elif n_idle > 1:
                # 多分镜（旧素材桃桃/墨墨）：让她去干活（扫地/浇花…），钉住该分镜约 4s
                self._as_idle_vignette = random.choice(range(1, n_idle))
                self._c24_click_state = "idle"
                self._c24_poke_until = now + 4.0
            else:
                # 单分镜（莉兹等）：给「被戳」反应，播一遍即停
                self._c24_click_state = "poke"
                self._c24_poke_until = now + 1.7
            self._as_last_key = None                     # 立刻从头演这段反应
            try:
                self._say_line("pat" if zone == "head" else "click")
            except Exception:
                pass
            try:
                self._draw_assistant()
            except Exception:
                pass
            try:
                self._bounce(self.rail, 1)
            except Exception:
                pass
            return
        st = self._stage
        spam = st.click() if st is not None else None
        work = self.theme == "maid" and getattr(self, "_form", "daily") == "work"
        if spam:
            self._say_line("spam")
            if work:
                self._as_hug_until = time.time() + 1.0
            elif self._v2_anim():
                self._as_react("pout", 1.8, force=True)
        elif zone == "body":
            self._say_line("click")
            if work:
                self._as_hug_until = time.time() + 1.1
            elif self._v2_anim():
                self._as_react("surprise", 0.9)
        else:
            self._say_line("pat" if zone == "head" else "click")
            if self.theme == "maid" and MF is not None:
                self._as_hug_until = time.time() + 1.4
            elif self._v2_anim():
                self._as_react("shy" if random.random() < 0.35 else "happy", 1.5)
        try:
            self._draw_assistant()
        except Exception:
            pass
        try:
            self._bounce(self.rail, 1)
        except Exception:
            pass

    def _bounce(self, cv, _n=1):
        """画布内容轻弹一下（位移链，结束自动归位）；正在弹时忽略重复触发"""
        if getattr(self, "_bouncing", False):
            return
        self._bouncing = True
        P = self.P
        offs = [0, -P(5), P(3), -P(2), 0]

        def step(i):
            if i >= len(offs):
                self._bouncing = False
                return
            cv.move("all", 0, offs[i] - offs[i - 1])
            cv.after(46, lambda: step(i + 1))

        step(1)

    # ---- 「换助手=换世界」：窗框染色 + 头部心情 + 动作反馈 ----
    def _apply_chrome(self):
        """把系统标题栏/边框染成当前主题色（桃桃粉、薇拉夜黑…）。"""
        try:
            _set_titlebar(self.root, C.get("bg", "#FEF2F7"),
                          C.get("ink", "#583447"),
                          dark=(C.get("mode") == "dark"))
        except Exception:
            pass

    def _apply_girly_fonts(self):
        """每个角色一套专属字体（缺字体则回落默认，绝不报错）。"""
        global F_TITLE, F_NAME, F_UI, F_UI_B, F_SM, F_SM_B
        if not hasattr(self, "_fonts_orig"):
            self._fonts_orig = dict(
                F_TITLE=F_TITLE, F_NAME=F_NAME, F_UI=F_UI,
                F_UI_B=F_UI_B, F_SM=F_SM, F_SM_B=F_SM_B)
        o = self._fonts_orig
        F_TITLE, F_NAME, F_UI, F_UI_B, F_SM, F_SM_B = (
            o["F_TITLE"], o["F_NAME"], o["F_UI"], o["F_UI_B"], o["F_SM"], o["F_SM_B"])
        w = _WORLD.get(self.theme)
        if not w:
            return
        disp = _pick_font(w["disp"], self.root)
        body = _pick_font(w["body"], self.root)
        if disp:
            F_TITLE = (disp, o["F_TITLE"][1], "bold")
            F_NAME = (disp, o["F_NAME"][1], "bold")
        if body:
            F_UI = (body, o["F_UI"][1])
            F_UI_B = (body, o["F_UI_B"][1], "bold")
            F_SM = (body, o["F_SM"][1])
            F_SM_B = (body, o["F_SM_B"][1], "bold")

    def _girly_stickers(self, cv, spots):
        """在画布上贴少女小物（蝴蝶结/玩偶/草莓）。spots=[(role, x, y, scale)]，坐标为像素。"""
        if self.theme != "maid" or GI is None:
            return
        refs = getattr(self, "_stk_refs", None)
        if refs is None:
            refs = self._stk_refs = {}
        bucket = refs.setdefault(id(cv), [])
        bucket.clear()
        for role, x, y, sc in spots:
            im = _girly(role, sc)
            if im is not None:
                try:
                    cv.create_image(int(x), int(y), image=im, anchor="center", tags="sticker")
                    bucket.append(im)
                except Exception:
                    pass

    # ---- 每个角色一个「世界」：标题 / 头栏 / 侧栏 装饰 ----
    def _world_title(self, cv, x, y, text, base_font, anchor="w", mult=None, sw=None):
        """按当前角色挑一套字体 + 卡通描边风格画标题/名字（无配置则回落普通字）。"""
        P = self.P
        w = _WORLD.get(self.theme)
        if not w:
            cv.create_text(x, y, text=text, font=base_font, fill=C["plum"], anchor=anchor)
            return
        t = w["title"]
        m = t["mult"] if mult is None else mult
        ft = (base_font[0], max(8, int(abs(base_font[1]) * m)), "bold")
        try:
            _stroke_text(cv, x, y, text, ft, C["plum"], stroke=t["stroke"],
                         sw=P(3 if sw is None else sw),
                         shadow=(P(2), P(3), t["shadow"]), anchor=anchor)
        except Exception:
            cv.create_text(x, y, text=text, font=ft, fill=C["plum"], anchor=anchor)

    def _world_header(self, cv, w, h, lw):
        """头栏专属世界装饰（每个角色一套母题）。"""
        P = self.P
        th = self.theme
        try:
            # 头像「世界画框」：每个角色给头像镶一圈符合气质的边框
            _av = _art("AVATAR", self.S)
            if _av is not None:
                _aw, _ah = _av.width(), _av.height()
                _ax = P(18)
                _ay = (h - 4 - _ah) // 2 + 2
                _x1, _y1 = _ax - P(4), _ay - P(4)
                _x2, _y2 = _ax + _aw + P(4), _ay + _ah + P(4)
                _acx, _acy = (_x1 + _x2) / 2, (_y1 + _y2) / 2
                if th == "maid":
                    _rr(cv, _x1, _y1, _x2, _y2, P(16), fill="", outline="#FF9EC4", width=2, tags="wd")
                    for _dx in range(int(_x1), int(_x2), int(P(12))):
                        cv.create_oval(_dx, _y2 - P(3), _dx + P(12), _y2 + P(7),
                                       outline="#FFC1E0", width=1, tags="wd")
                elif th == "hanfu":
                    cv.create_rectangle(_x1, _y1, _x2, _y2, outline="#C8566B", width=2, tags="wd")
                    cv.create_rectangle(_x1 + P(3), _y1 + P(3), _x2 - P(3), _y2 - P(3),
                                        outline="#E0B9C0", width=1, tags="wd")
                elif th == "magic":
                    cv.create_oval(_x1, _y1, _x2, _y2, outline=C["pink"], width=2, tags="wd")
                    _rad0 = _aw / 2 + P(8)
                    for _a in (45, 135, 225, 315):
                        _rad = math.radians(_a)
                        _star(cv, _acx + math.cos(_rad) * _rad0,
                              _acy + math.sin(_rad) * _rad0, P(4), C["gold"], tags="wd")
                elif th == "miko":
                    cv.create_oval(_x1, _y1, _x2, _y2, outline="#D6455B", width=3, tags="wd")
                    cv.create_oval(_x1 + P(3), _y1 + P(3), _x2 - P(3), _y2 - P(3),
                                   outline="#FFFFFF", width=1, dash=(4, 3), tags="wd")
                elif th == "robot":
                    _hud_brackets(cv, _x1, _y1, _x2, _y2, C["pink"], tags="wd", ln=P(14))
                elif th == "goth":
                    cv.create_oval(_x1, _y1, _x2, _y2, outline=C["line"], width=2, tags="wd")
                    for _a in range(0, 360, 30):
                        _rad = math.radians(_a)
                        _px = _acx + math.cos(_rad) * (_aw / 2 + P(6))
                        _py = _acy + math.sin(_rad) * (_aw / 2 + P(6))
                        cv.create_line(_px, _py, _px + math.cos(_rad) * P(6),
                                       _py + math.sin(_rad) * P(6),
                                       fill=C["line"], width=1, tags="wd")
            if th == "maid":
                _rainbow_arc(cv, w - P(150), P(44), P(30),
                             ["#FFB3C6", "#FFD9A0", "#FFF6B0", "#BDECB6"], tags="wd")
                for sx, sy, ss, sc in (
                        (P(150), P(48), P(7), "#FFFFFF"), (lw - P(70), P(60), P(6), C["gold"]),
                        (w - P(60), P(70), P(8), "#FFC1E0"), (P(300), P(96), P(5), "#B8E0FF"),
                        (lw - P(20), P(44), P(6), "#FF9EC4")):
                    _sparkle(cv, sx, sy, ss, sc, tags="wd")
                for hx, hy, hs in ((P(240), P(44), P(5)), (lw + P(20), P(92), P(6)),
                                   (w - P(200), P(44), P(4))):
                    _heart(cv, hx, hy, hs, C["pink"], tags="wd")
                self._girly_stickers(cv, [("STK_BOW", w - P(30), P(34), 0.5),
                                          ("STK_BERRY", lw - P(30), h - P(22), 0.4)])
            elif th == "hanfu":
                # 画窗即画心：只在古画上盖两枚朱印，不再铺手绘山石
                _seal(cv, w - P(54), P(16), P(32), "#C8566B", "墨", tags="wd")
                _seal(cv, w - P(108), P(18), P(24), "#C8566B", "琴", tags="wd")
            elif th == "magic":
                # 星云画窗本身已足够；头栏不再叠手绘星环与字
                pass
            elif th == "miko":
                # 实景鸟居画窗 + 朱色鸟居框；不再撒花瓣/御神签
                pass
            elif th == "robot":
                # 轨道视角本身就是「世界」；不再叠假 HUD 读数与菱形
                pass
            elif th == "goth":
                # 夜堡：帷幔/铁角已在边框层，画窗上不再放蝙蝠、荆棘、玫瑰
                pass
        except Exception:
            pass

    def _world_rail(self, cv, w, h, band):
        """侧栏专属世界装饰。"""
        P = self.P
        th = self.theme
        try:
            if th == "maid":
                for sx, sy, ss, sc in ((P(22), P(20), P(6), "#FFFFFF"), (w - P(24), P(26), P(7), C["gold"]),
                                       (P(30), band - P(20), P(5), "#FFC1E0"),
                                       (w - P(34), band - P(26), P(6), "#B8E0FF"),
                                       (P(20), P(58), P(4), "#FFF3A0")):
                    _sparkle(cv, sx, sy, ss, sc, tags="wd")
                for hx, hy, hs in ((w - P(22), P(56), P(5)), (P(24), P(80), P(4))):
                    _heart(cv, hx, hy, hs, C["pink"], tags="wd")
                # 草莓别压在名牌蝴蝶结上：往上挪到头像右侧的空当
                self._girly_stickers(cv, [("STK_BERRY", w - P(30), band - P(54), 0.36)])
                for _dx in range(0, int(w), int(P(22))):
                    cv.create_arc(_dx, P(0), _dx + P(22), P(12), start=180, extent=180,
                                  style="arc", outline="#FFD9E8", width=1, tags="wd")
            elif th == "hanfu":
                _plum_branch(cv, P(10), P(44), P(56), "#6B4B4B", C["pink"], tags="wd")
                for px, py, ps, pr in ((w - P(30), band - P(30), P(6), 0.7),
                                       (P(24), band - P(16), P(5), 1.8)):
                    _petal(cv, px, py, ps, C["pink_l"], rot=pr, tags="wd")
                for _i, _ch in enumerate("墨香"):
                    cv.create_text(w - P(26), P(56) + _i * P(18), text=_ch,
                                   font=("KaiTi", int(P(13)), "bold"),
                                   fill=C["plum"], tags="wd")
                _lantern(cv, P(22), P(58), P(8), "#FBF0F2", "#DCC2C8", tags="wd")
            elif th == "magic":
                _magic_circle(cv, w - P(34), P(30), P(22), C["pink"], C["gold"], tags="wd")
                for sx, sy, ss in ((P(24), P(24), P(6)), (P(30), band - P(24), P(5)),
                                   (w - P(28), band - P(30), P(6))):
                    _star(cv, sx, sy, ss, C["gold"], tags="wd")
                cv.create_oval(w // 2 - P(50), P(8), w // 2 + P(50), P(102),
                               outline=C["gold"], width=1, dash=(3, 5), tags="wd")
                _sparkle(cv, w - P(20), band + P(44), P(5), C["gold"], tags="wd")
            elif th == "miko":
                for px, py, ps, pr in ((w - P(26), P(24), P(6), 0.5), (P(24), P(30), P(5), 1.6),
                                       (w - P(30), band - P(24), P(6), 2.3)):
                    _petal(cv, px, py, ps, C["pink"], rot=pr, tags="wd")
                _paw(cv, P(26), band - P(20), P(7), C["pink_l"], tags="wd")
                _omikuji(cv, P(22), P(50), P(11), "#D6455B", "#94364A", tags="wd")
                _bell(cv, w - P(22), P(54), P(8), "#D6455B", C["gold"], tags="wd")
            elif th == "robot":
                _hud_brackets(cv, P(6), P(6), w - P(6), h - P(6), C["pink_l"], tags="wd", ln=P(16))
                for i in range(4):
                    _diamond(cv, w - P(24), P(34) + i * P(22), P(6), C["pink"], tags="wd")
                for _i in range(4):
                    _bh = P(4) + _i * P(3)
                    cv.create_rectangle(P(14) + _i * P(7), P(26) - _bh,
                                        P(19) + _i * P(7), P(26),
                                        fill=C["pink_l"], outline="", tags="wd")
                cv.create_text(P(14), P(10), anchor="nw", text="LINK",
                               font=("Consolas", 7), fill=C["ink_l"], tags="wd")
                _heart(cv, w - P(18), P(18), P(5), C["pink"], tags="wd")
            elif th == "goth":
                _bat(cv, w - P(30), P(26), P(9), "#12060F", tags="wd")
                _bat(cv, P(26), P(42), P(7), "#12060F", tags="wd")
                _crescent(cv, w - P(28), band - P(26), P(12), C["gold"], tags="wd")
                for px, py, ps, pr in ((P(24), band - P(20), P(6), 0.6), (w - P(24), P(62), P(5), 1.7)):
                    _petal(cv, px, py, ps, C["pink_l"], rot=pr, tags="wd")
                _web(cv, P(4), P(4), P(44), C["line"], start=270, tags="wd")
                _bat(cv, w - P(18), band - P(18), P(6), "#170A13", tags="wd")
        except Exception:
            pass

    # ---- 环境动画：每个角色一套飘动粒子（星屑/花瓣/樱花/余烬/扫描线） ----
    def _amb_spec(self):
        return {"twinkle": ("sparkle", 8), "petal": ("petal", 7), "star": ("star", 9),
                "sakura": ("petal", 8), "scan": ("scan", 1),
                "ember": ("ember", 8)}.get((_WORLD.get(self.theme) or {}).get("amb"))

    def _seed_ambient(self, cv):
        """画布重绘后按当前尺寸重建该画布的飘动粒子。"""
        if cv is None:
            return
        spec = self._amb_spec()
        reg = getattr(self, "_amb", None)
        if reg is None:
            reg = self._amb = {}
        key = id(cv)
        if not spec:
            reg.pop(key, None)
            return
        import random
        try:
            w, h = cv.winfo_width(), cv.winfo_height()
        except Exception:
            w, h = 0, 0
        if w < 24 or h < 24:
            return
        kind, n = spec
        cols = {
            "maid": ["#FFFFFF", "#FFD2E6", C["gold"], "#FFC1E0"],
            "hanfu": [C["pink_l"], "#F3C9CF", "#E9B7C1"],
            "magic": [C["gold"], "#C7A6FF", "#8FD3FF", "#FFFFFF"],
            "miko": [C["pink"], "#FFFFFF", C["pink_l"], C["gold"]],
            "robot": [C["pink"], C["gold"], C["pink_l"]],
            "goth": [C["pink_l"], "#7A2A44", C["gold"], "#B98CD8"],
        }.get(self.theme, ["#FFFFFF"])
        ps = []
        for _ in range(n):
            ps.append(dict(x=random.uniform(0, w), y=random.uniform(0, h),
                           vx=random.uniform(-0.4, 0.4), vy=random.uniform(0.3, 0.9),
                           s=random.uniform(4, 8) * self.S, rot=random.uniform(0, 6.28),
                           ph=random.uniform(0, 6.28), c=random.choice(cols)))
        reg[key] = (cv, kind, ps, w, h)

    def _amb_draw(self, cv, kind, p, w, h):
        x, y, s, c = p["x"], p["y"], p["s"], p["c"]
        if kind == "sparkle":
            t = 0.5 + 0.5 * math.sin(p["ph"])
            _sparkle(cv, x, y, s * (0.6 + 0.6 * t), _mix(c, "#FFFFFF", t), tags="amb")
        elif kind == "star":
            t = 0.5 + 0.5 * math.sin(p["ph"])
            _star(cv, x, y, s, _mix(c, "#FFFFFF", t), tags="amb")
        elif kind == "petal":
            _petal(cv, x, y, s, c, rot=p["rot"], tags="amb")
        elif kind == "ember":
            t = 0.5 + 0.5 * math.sin(p["ph"])
            _sparkle(cv, x, y, s * 0.7 * t + 1, _mix(c, "#FF9A3C", t), tags="amb")
        elif kind == "scan":
            yy = h * 0.5 + h * 0.42 * math.sin(p["ph"] * 0.5)
            cv.create_line(6, yy, w - 6, yy,
                           fill=_mix(c, "#FFFFFF", 0.5 + 0.5 * math.sin(p["ph"])),
                           width=2, tags="amb")
            # 零号的「心形泄漏」：冷静面板上偶尔漏出一颗跳动的心（反差萌）
            if cv is getattr(self, "header", None):
                _t = 0.5 + 0.5 * math.sin(p["ph"] * 0.8)
                _heart(cv, w - self.P(46), h - self.P(42),
                       self.P(6) * (0.7 + 0.6 * _t),
                       _mix("#F6E3EE", C["pink"], _t), tags="amb")

    def _amb_step(self):
        return
        import random
        th = self.theme
        if th not in _WORLD or not _WORLD[th].get("amb"):
            self._amb = {}
            self._amb_job = None
            return
        reg = getattr(self, "_amb", {})
        for key in list(reg.keys()):
            try:
                cv, kind, ps, w, h = reg[key]
                try:
                    if not cv.winfo_exists():
                        reg.pop(key, None)
                        continue
                except Exception:
                    reg.pop(key, None)
                    continue
                cv.delete("amb")
                up = (kind == "ember")
                for p in ps:
                    p["ph"] += 0.22
                    p["rot"] += 0.06
                    if kind != "scan":
                        p["x"] += p["vx"] * self.S + math.sin(p["ph"]) * 0.6 * self.S
                    p["y"] += (-p["vy"] if up else p["vy"]) * self.S
                    if p["y"] > h + 14:
                        p["y"] = -12; p["x"] = random.uniform(0, w)
                    elif p["y"] < -14:
                        p["y"] = h + 12; p["x"] = random.uniform(0, w)
                    if p["x"] < -14:
                        p["x"] = w + 12
                    elif p["x"] > w + 14:
                        p["x"] = -12
                    self._amb_draw(cv, kind, p, w, h)
            except Exception:
                pass
        try:
            self._amb_job = self.root.after(90, self._amb_step)
        except Exception:
            self._amb_job = None

    def _start_amb(self):
        """（主窗环境粒子动画随头栏/立绘栏移除而停用）"""
        return
        if getattr(self, "_amb_job", None) is None:
            try:
                self._amb_job = self.root.after(90, self._amb_step)
            except Exception:
                pass

    def set_mood(self, kind, n=None, total=None, redraw=True):
        """一个动作 → 头部「房间招牌」随之换成当前角色此刻的心情与语气。"""
        # 实时桌宠：心情 = 她的反应动作（busy/done/fail/happy…宿主端有对应补间预设）
        if getattr(self, "pet_live", False) and getattr(self, "_pet", None):
            try:
                # 主程序心情 → 大肥鱼 Live2D 反应（表情+动作，映射见 live2d/pet.html REACT）
                _m = {"probing": "busy", "sniffing": "busy", "sniff": "busy", "prog": "busy",
                      "carry": "busy", "success": "done", "done": "done",
                      "error": "fail", "fail": "fail", "empty": "pout",
                      "found": "happy", "paused": "idle", "clip": "surprise"}.get(kind, kind)
                if _m in ("busy", "done", "fail", "happy", "surprise", "shy", "pout", "idle"):
                    self._pet.react(_m)
            except Exception:
                pass
        try:
            self._mood_kind = kind
            txt = _mood(kind, self.theme, n=n, total=total)
            if not txt:
                th = THEMES.get(self.theme) or {}
                txt = "%s｜给一个网址，下载页面所有视频" % (th.get("note") or "")
            self._mood_text = txt
            if redraw:
                self._draw_header()
        except Exception:
            pass

    def _header_bloom(self, n=9):
        """头部撒一把主题母题（心/星/花瓣…）上浮淡出——动作达成的即时反馈。"""
        try:
            import random
            cv = self.header
            w, h = cv.winfo_width(), cv.winfo_height()
            if w < 40:
                return
            base = getattr(self, "_bloom_seq", 0) + 1
            self._bloom_seq = base
            tag = "bloom%d" % base
            for _ in range(n):
                x = random.randint(int(w * 0.12), int(w * 0.92))
                y = h - random.randint(0, int(h * 0.28))
                s = self.P(random.randint(5, 11))
                col = random.choice([C["pink"], C["pink_l"], C["gold"], C["plum"]])
                _deco(cv, x, y, s, col, tags=tag)
            try:
                if not cv.find_withtag(tag):
                    return
            except Exception:
                return
            state = {"t": 0}

            def tick():
                state["t"] += 1
                try:
                    cv.move(tag, 0, -self.P(3))
                    if state["t"] % 2 == 0:
                        cv.move(tag, random.randint(-self.P(2), self.P(2)), 0)
                except Exception:
                    return
                if state["t"] >= 12:
                    try:
                        cv.delete(tag)
                    except Exception:
                        pass
                    return
                try:
                    cv.after(45, tick)
                except Exception:
                    pass

            tick()
        except Exception:
            pass

    def celebrate(self, bloom=True):
        """动作达成（找到 / 全部搬完）：招牌轻弹 + 撒花，让界面「活」一下。"""
        try:
            self._bounce(self.header)
        except Exception:
            pass
        if bloom:
            self._header_bloom()

    # ---- 右侧「角色休息室」----
    def _world_rail_frame(self, cv, w, h, band, r):
        """侧栏「世界容器」：每个角色一副骨架（橱窗/立轴/星窗/神社/机体/尖拱）。"""
        P = self.P
        th = self.theme
        try:
            if th == "maid":
                # 糖果屋橱窗：条纹遮阳棚 + 扇贝帘 + 侧边蕾丝 + 蝴蝶结
                _rr(cv, 0, 0, w - 1, h - 1, r, fill=C["card"], outline=C["line"])
                _rr(cv, 1, 1, w - 2, band + r, r, fill=C["sky_l"], outline="")
                cv.create_rectangle(1, band, w - 1, band + r + 1,
                                    fill=C["card"], outline="")
                for xx in range(int(P(10)), int(w - P(6)), int(P(24))):
                    cv.create_line(xx, P(4), xx, band, fill="#FFFFFF",
                                   width=max(1, int(P(5))), tags="wd")
                _scallop_row(cv, P(3), w - P(3), band, P(7),
                             "#FFFFFF", "#FFD9E8", tags="wd")
                cv.create_line(P(6), P(2), w - P(6), P(2),
                               fill="#FF9EC4", width=2, tags="wd")
                _lace_side(cv, P(2), P(12), band - P(6), P(6),
                           "#FFD9E8", tags="wd")
                _lace_side(cv, w - P(2), P(12), band - P(6), P(6),
                           "#FFD9E8", tags="wd", right=True)
                _bow(cv, P(24), P(24), P(12), C["pink"],
                     C["pink_d"], tags="wd")
                _heart(cv, w - P(24), P(34), P(5), C["pink"], tags="wd")
                return True

            if th == "hanfu":
                # 立轴：上下木杆 + 绢边 + 纸上朱印
                _rr(cv, 0, 0, w - 1, h - 1, r, fill=C["card"], outline=C["line"])
                cv.create_rectangle(1, 1, w - 2, band,
                                    fill=_mix(C["card"], C["pink_xl"], 0.62),
                                    outline="")
                cv.create_line(1, band, w - 1, band, fill="#C8566B", width=1)
                cv.create_line(P(5), P(14), P(5), h - P(14),
                               fill="#C8566B", width=1, tags="wd")
                cv.create_line(w - P(5), P(14), w - P(5), h - P(14),
                               fill="#C8566B", width=1, tags="wd")
                _hscroll_stave(cv, P(4), w - P(4), P(4), P(10),
                               "#6B4B4B", "#9A7373", "#4A3232", tags="wd")
                _hscroll_stave(cv, P(4), w - P(4), h - P(14), P(10),
                               "#6B4B4B", "#9A7373", "#4A3232", tags="wd")
                _seal(cv, w - P(42), P(22), P(24), "#C8566B", "雅", tags="wd")
                return True

            if th == "magic":
                # 星窗：尖拱窗洞 + 金线 + 窗沿星
                _rr(cv, 0, 0, w - 1, h - 1, r, fill=C["card"], outline=C["line"])
                x1, x2 = P(14), w - P(14)
                cx0 = (x1 + x2) / 2
                apex, bot = P(8), band
                ctrl = apex + (bot - apex) * 0.18
                pts = []
                N = 16
                for i in range(N + 1):
                    t = i / N
                    pts += [(1 - t) ** 2 * x1 + 2 * (1 - t) * t * x1 + t * t * cx0,
                            (1 - t) ** 2 * bot + 2 * (1 - t) * t * ctrl + t * t * apex]
                for i in range(1, N + 1):
                    t = i / N
                    pts += [(1 - t) ** 2 * cx0 + 2 * (1 - t) * t * x2 + t * t * x2,
                            (1 - t) ** 2 * apex + 2 * (1 - t) * t * ctrl + t * t * bot]
                cv.create_polygon(pts,
                                  fill=_mix(C["pink_l"], C["card"], 0.55),
                                  outline=C["gold"], width=2, tags="wd")
                inner = []
                for i in range(N + 1):
                    t = i / N
                    inner += [(1 - t) ** 2 * (x1 + P(5)) + 2 * (1 - t) * t * (x1 + P(5)) + t * t * cx0,
                              (1 - t) ** 2 * (bot - P(1)) + 2 * (1 - t) * t * (ctrl + P(4)) + t * t * (apex + P(5))]
                for i in range(1, N + 1):
                    t = i / N
                    inner += [(1 - t) ** 2 * cx0 + 2 * (1 - t) * t * (x2 - P(5)) + t * t * (x2 - P(5)),
                              (1 - t) ** 2 * (apex + P(5)) + 2 * (1 - t) * t * (ctrl + P(4)) + t * t * (bot - P(1))]
                cv.create_line(inner, fill=C["gold"], width=1,
                               dash=(4, 4), tags="wd")
                for t in (0.30, 0.62):
                    sx = (1 - t) ** 2 * x1 + 2 * (1 - t) * t * x1 + t * t * cx0
                    sy = (1 - t) ** 2 * bot + 2 * (1 - t) * t * ctrl + t * t * apex
                    _star(cv, sx, sy, P(4), C["gold"], tags="wd")
                    _star(cv, x2 - (sx - x1), sy, P(4), C["gold"], tags="wd")
                _star(cv, cx0, P(20), P(5), C["gold"], tags="wd")
                return True

            if th == "miko":
                # 神社：朱柱 + 注连绳 + 纸垂，名字挂木牌
                _rr(cv, 0, 0, w - 1, h - 1, r, fill=C["card"], outline=C["line"])
                cv.create_rectangle(1, 1, w - 2, band,
                                    fill=_mix(C["card"], C["pink_xl"], 0.5),
                                    outline="")
                cv.create_line(1, band, w - 1, band, fill="#C63A4E", width=2)
                for x0 in (P(2), w - P(9)):
                    cv.create_rectangle(x0, P(10), x0 + P(7), band,
                                        fill="#C63A4E", outline="#8F2335",
                                        tags="wd")
                    cv.create_rectangle(x0 - P(2), P(4), x0 + P(9), P(11),
                                        fill="#E6B54A", outline="#B58A2C",
                                        tags="wd")
                _shimenawa(cv, P(10), w - P(10), P(22), P(6),
                           "#D9B98A", "#8F6B42", "#F3E3C8",
                           tags="wd", sag=P(2))
                _shide(cv, P(26), P(25), P(8), "#FFFFFF",
                       "#C9B7A4", tags="wd")
                _shide(cv, w - P(26), P(25), P(8), "#FFFFFF",
                       "#C9B7A4", tags="wd")
                return True

            if th == "robot":
                # 机体：切角面板 + 科技带 + 竖向能量槽
                c = P(14)
                _chamfer_rect(cv, 0, 0, w - 1, h - 1, c,
                              fill=C["card"], outline=C["line"], width=1)
                _chamfer_rect(cv, P(5), P(5), w - P(5), h - P(5),
                              max(2, c - P(5)), fill="", outline=C["line"],
                              width=1)
                cv.create_rectangle(P(8), P(10), w - P(8), band,
                                    fill=C["pink_xl"], outline="")
                cv.create_line(P(8), band, w - P(8), band,
                               fill=C["line"], width=1)
                for i in range(5):
                    cv.create_line(P(14) + i * P(12), P(12),
                                   P(14) + i * P(12), P(18),
                                   fill=C["line"], width=1, tags="wd")
                cv.create_text(P(16), P(24), anchor="nw",
                               text="SNIFFDL-OS", font=("Consolas", 7),
                               fill=C["ink_l"], tags="wd")
                gx1, gx2 = w - P(14), w - P(8)
                gy1, gy2 = band + P(24), h - P(34)
                cv.create_rectangle(gx1, gy1, gx2, gy2,
                                    fill=C["pink_xl"], outline=C["line"],
                                    width=1, tags="wd")
                for yy in range(int(gy1 + P(8)), int(gy2), int(P(10))):
                    cv.create_line(gx1, yy, gx2, yy, fill=C["line"],
                                   width=1, tags="wd")
                fy = gy2 - int((gy2 - gy1) * 0.46)
                cv.create_rectangle(gx1 + 1, fy, gx2 - 1, gy2 - 1,
                                    fill=C["pink"], outline="", tags="wd")
                cv.create_text(gx1 - P(4), gy1 - P(4), anchor="se",
                               text="PWR", font=("Consolas", 7),
                               fill=C["ink_l"], tags="wd")
                return True

            if th == "goth":
                # 哥特：天鹅绒帷幔 + 尖拱线 + 铁艺护角 + 铆钉
                velvet = _mix(C["pink_d"], "#1A0D16", 0.55)
                _rr(cv, 0, 0, w - 1, h - 1, r, fill=C["card"], outline=C["line"])
                cv.create_rectangle(1, 1, w - 2, band,
                                    fill=velvet, outline="")
                cv.create_line(1, band, w - 1, band,
                               fill=C["line"], width=1)
                _valance(cv, P(4), w - P(4), P(2), P(20), velvet,
                         _mix(C["gold"], "#7A5A4A", 0.6), tags="wd")
                _pointed_arch(cv, P(10), w - P(10), P(8), band + P(16),
                              C["line"], 1, tags="wd")
                for ccx, ccy, ddx, ddy in (
                        (P(7), P(7), 1, 1), (w - P(7), P(7), -1, 1),
                        (P(7), h - P(7), 1, -1), (w - P(7), h - P(7), -1, -1)):
                    _iron_corner(cv, ccx, ccy, ddx, ddy, P(26),
                                 C["line"], tags="wd")
                for yy in range(int(P(150)), int(h - P(40)), int(P(48))):
                    cv.create_oval(P(6), yy, P(10), yy + P(4),
                                   fill=C["line"], outline="", tags="wd")
                    cv.create_oval(w - P(10), yy, w - P(6), yy + P(4),
                                   fill=C["line"], outline="", tags="wd")
                return True
        except Exception:
            return False
        return False

    def _world_nameplate(self, cv, cx, y, w, text, ft_name):
        """名字牌：六个世界各一种挂法（缎带/题签/星幡/绘马/数据条/铁牌）。"""
        P = self.P
        th = self.theme
        try:
            tw = tkfont.Font(root=self.root, font=ft_name).measure(text)
        except Exception:
            tw = P(160)
        pw = min(w - P(26), max(P(150), tw + P(58)))
        ph = P(28)
        x1, x2 = cx - pw / 2, cx + pw / 2
        y1, y2 = y - ph / 2, y + ph / 2
        try:
            if th == "maid":
                _rr(cv, x1, y1, x2, y2, ph / 2, fill=C["pink_l"],
                    outline="#FFFFFF", width=2, tags="wnp")
                _bow(cv, x1 + P(2), y, P(8), C["pink"],
                     C["pink_d"], tags="wnp")
                _bow(cv, x2 - P(2), y, P(8), C["pink"],
                     C["pink_d"], tags="wnp")
            elif th == "hanfu":
                cv.create_rectangle(x1, y1, x2, y2, fill="#FBF3EF",
                                    outline="#C8566B", width=2, tags="wnp")
                cv.create_rectangle(x1 + P(4), y1 + P(4),
                                    x2 - P(4), y2 - P(4),
                                    outline="#C8566B", width=1,
                                    dash=(3, 3), tags="wnp")
                for xx in (x1 + P(22), x2 - P(22)):
                    cv.create_oval(xx - P(3), y1 - P(5),
                                   xx + P(3), y1 + P(1),
                                   outline="#C8566B", width=1, tags="wnp")
            elif th == "magic":
                _notched_banner(cv, x1, y1, x2, y2, P(14),
                                C["card"], C["gold"], tags="wnp")
                _star(cv, x1 + P(24), y, P(5), C["gold"], tags="wnp")
                _star(cv, x2 - P(24), y, P(5), C["gold"], tags="wnp")
            elif th == "miko":
                c = P(8)
                pts = [x1 + c, y1, x2 - c, y1, x2, y1 + c,
                       x2, y2, x1, y2, x1, y1 + c]
                cv.create_polygon(pts, fill="#F6E3CF",
                                  outline="#8C4A3C", width=2, tags="wnp")
                cv.create_line(x1 + P(10), y1 + P(6),
                               x2 - P(10), y1 + P(6),
                               fill="#D8A79C", width=1, tags="wnp")
                cv.create_oval(cx - P(5), y1 + P(2),
                               cx + P(5), y1 + P(12),
                               outline="#C63A4E", width=2, tags="wnp")
            elif th == "robot":
                _chamfer_rect(cv, x1, y1, x2, y2, P(8),
                              fill=C["pink_xl"], outline=C["line"],
                              width=1, tags="wnp")
                cv.create_line(x1 + P(10), y2 - P(1),
                               x2 - P(10), y2 - P(1),
                               fill=C["pink"], width=2, tags="wnp")
                cv.create_oval(x1 + P(7), y - P(3),
                               x1 + P(13), y + P(3),
                               fill=C["pink"], outline="", tags="wnp")
            elif th == "goth":
                pts = [x1 + P(10), y1, x2 - P(10), y1, x2, y,
                       x2 - P(10), y2, x1 + P(10), y2, x1, y]
                cv.create_polygon(pts,
                                  fill=_mix(C["pink_xl"], "#12060F", 0.45),
                                  outline=_mix(C["gold"], "#7A5A4A", 0.65),
                                  width=2, tags="wnp")
                for xx in (x1 + P(24), cx, x2 - P(24)):
                    cv.create_oval(xx - P(2), y2 - P(5),
                                   xx + P(2), y2 - P(1),
                                   fill=C["gold"], outline="", tags="wnp")
        except Exception:
            pass

    def _world_bubble(self, cv, x1, y1, x2, y2, r):
        """台词气泡：每个世界一种纸/牌/屏（文字已先画好，最后会被 raise）。"""
        P = self.P
        th = self.theme
        cx = (x1 + x2) / 2
        fill = C["pink_xl"]
        outline = C["line"]
        try:
            if th == "maid":
                _rr(cv, x1, y1, x2, y2, r, fill=fill,
                    outline=outline, width=1, tags="wb")
                for xx in range(int(x1 + P(12)), int(x2 - P(12)), int(P(18))):
                    cv.create_arc(xx, y2 - P(6), xx + P(18), y2 + P(6),
                                  start=0, extent=180, style="arc",
                                  outline="#FFFFFF", width=2, tags="wb")
            elif th == "hanfu":
                cv.create_rectangle(x1, y1, x2, y2, fill=fill,
                                    outline="#C8566B", width=1, tags="wb")
                cv.create_rectangle(x1 + P(4), y1 + P(4),
                                    x2 - P(4), y2 - P(4),
                                    outline="#C8566B", width=1,
                                    dash=(3, 4), tags="wb")
                outline = "#C8566B"
            elif th == "magic":
                _rr(cv, x1, y1, x2, y2, r, fill=fill,
                    outline=C["gold"], width=2, tags="wb")
                _rr(cv, x1 + P(4), y1 + P(4), x2 - P(4), y2 - P(4),
                    max(2, r - P(4)), fill="", outline=C["gold"],
                    width=1, dash=(3, 3), tags="wb")
                _star(cv, x1 + P(14), y1 + P(10), P(4), C["gold"], tags="wb")
                _star(cv, x2 - P(14), y1 + P(10), P(4), C["gold"], tags="wb")
                outline = C["gold"]
            elif th == "miko":
                c = P(9)
                pts = [x1 + c, y1, x2 - c, y1, x2, y1 + c,
                       x2, y2, x1, y2, x1, y1 + c]
                cv.create_polygon(pts, fill="#FBF0E4",
                                  outline="#8C4A3C", width=2, tags="wb")
                cv.create_line(x1 + P(10), y1 + P(6),
                               x2 - P(10), y1 + P(6),
                               fill="#D8A79C", width=1, tags="wb")
                fill, outline = "#FBF0E4", "#8C4A3C"
            elif th == "robot":
                _chamfer_rect(cv, x1, y1, x2, y2, P(8),
                              fill=fill, outline=C["line"],
                              width=1, tags="wb")
                cv.create_line(x1, y1 + P(8), x1 + P(10), y1 + P(8),
                               fill=C["pink"], width=2, tags="wb")
                cv.create_line(x1, y2 - P(8), x1 + P(10), y2 - P(8),
                               fill=C["pink"], width=2, tags="wb")
            elif th == "goth":
                cv.create_rectangle(x1, y1, x2, y2, fill=fill,
                                    outline=C["line"], width=1, tags="wb")
                for ccx, ccy, ddx, ddy in (
                        (x1 + P(5), y1 + P(5), 1, 1),
                        (x2 - P(5), y1 + P(5), -1, 1),
                        (x1 + P(5), y2 - P(5), 1, -1),
                        (x2 - P(5), y2 - P(5), -1, -1)):
                    _iron_corner(cv, ccx, ccy, ddx, ddy, P(12),
                                 _mix(C["gold"], "#7A5A4A", 0.6), tags="wb")
                outline = C["line"]
            else:
                _rr(cv, x1, y1, x2, y2, r, fill=fill,
                    outline=outline, width=1, tags="wb")
            # 小尾巴
            if th == "robot":
                cv.create_line(cx, y2, cx, y2 + P(9), fill=C["line"], width=2,
                               tags="wb")
            else:
                cv.create_polygon(cx - P(8), y2 - 1, cx + P(8), y2 - 1,
                                  cx, y2 + P(9), fill=fill,
                                  outline=outline, width=1, tags="wb")
                cv.create_line(cx - P(7), y2 - 1, cx + P(7), y2 - 1,
                               fill=fill, width=2, tags="wb")
        except Exception:
            try:
                _rr(cv, x1, y1, x2, y2, r, fill=fill,
                    outline=outline, width=1, tags="wb")
            except Exception:
                pass

    def _world_stats_head(self, cv, cx, y, w):
        """「今日服务」小标题两边的世界记号。"""
        P = self.P
        th = self.theme
        try:
            if th == "maid":
                _heart(cv, cx - P(52), y, P(4), C["pink"], tags="wh")
                _heart(cv, cx + P(52), y, P(4), C["pink"], tags="wh")
            elif th == "hanfu":
                for dx in (-P(58), P(58)):
                    cv.create_line(cx + dx - P(8), y, cx + dx + P(8), y,
                                   fill="#C8566B", width=1, tags="wh")
                cv.create_oval(cx - P(2), y - P(2), cx + P(2), y + P(2),
                               fill="#C8566B", outline="", tags="wh")
            elif th == "magic":
                _star(cv, cx - P(54), y, P(4), C["gold"], tags="wh")
                _star(cv, cx + P(54), y, P(4), C["gold"], tags="wh")
            elif th == "miko":
                _petal(cv, cx - P(52), y, P(5), C["pink"], tags="wh")
                _petal(cv, cx + P(52), y, P(5), C["pink"], tags="wh")
            elif th == "robot":
                for dx in (-P(56), P(56)):
                    cv.create_line(cx + dx - P(7), y - P(4),
                                   cx + dx - P(7), y + P(4),
                                   fill=C["pink"], width=1, tags="wh")
                    cv.create_line(cx + dx - P(7), y,
                                   cx + dx + P(2), y,
                                   fill=C["pink"], width=1, tags="wh")
            elif th == "goth":
                _diamond(cv, cx - P(52), y, P(4), C["line"], tags="wh")
                _diamond(cv, cx + P(52), y, P(4), C["line"], tags="wh")
        except Exception:
            pass

    def _draw_rail(self):
        """（旧「角色休息室」立绘栏已移除；保留方法名供 apply_theme 调用）"""
        return
        cv, P = self.rail, self.P
        cv.delete("all")
        w, h = cv.winfo_width(), cv.winfo_height()
        if w < 40:
            w = P(252)
        if h < 120:
            h = P(700)
        lay = _layout()
        r = P(max(2, lay["radius"]))
        rr_bub = P(max(6, min(18, lay["radius"])))
        ft_name = _mono(F_NAME) if lay["mono"] else F_NAME
        ft_bub = _mono(F_UI) if lay["mono"] else F_UI
        band = P(104)
        custom = self._world_rail_frame(cv, w, h, band, r)
        if not custom:
            _rr(cv, 0, 0, w - 1, h - 1, r, fill=C["card"], outline=C["line"])
            _rr(cv, 1, 1, w - 2, band + r, r, fill=C["sky_l"], outline="")
            cv.create_rectangle(1, band, w - 1, band + r + 1, fill=C["card"], outline="")
            cv.create_line(1, band, w - 1, band, fill=C["line"])
            if lay["accent"] == "soft":
                _deco(cv, P(24), P(22), P(6), C["pink_l"])
                _deco(cv, w - P(28), P(30), P(8), C["card"])
                _deco(cv, w - P(46), P(74), P(5), C["pink_l"])
            else:
                try:
                    _accent(cv, lay["accent"], 2, 2, w - 3, h - 3,
                            C["pink"], C["line"], tags="racc")
                except Exception:
                    _deco(cv, P(24), P(22), P(6), C["pink_l"])
        cx = w // 2
        # 头像尺寸与名字行位置先算出来：名牌要垫在头像下缘、名字上缘之间
        av = _art("AVATAR", self.S)
        ah = av.height() if av is not None else P(76)
        y = P(14) + ah + P(10)
        ch, role, _label = self.char_name()
        _nsep = {"hud": " // ", "seal": " · ", "stars": " ✦ ",
                 "orn": " ❦ ", "soft": " · "}.get(lay["accent"], " · ")
        if self.theme == "miko":          # 巫女：音符分隔，跟上标题
            _nsep = " ♪ "
        _nm = "%s%s%s" % (ch, _nsep, role)
        self._world_nameplate(cv, cx, y, w, _nm, ft_name)
        # 头像
        if av is not None:
            cv.create_image(cx, P(14), image=av, anchor="n")
        else:
            draw_maid_mascot(cv, cx - P(32), P(6), s=1.0)
        self._world_title(cv, cx, y, _nm, ft_name, anchor="center",
                          mult=1.08, sw=2)
        y += P(24)
        # 台词气泡（自动换行，高度按文本算）
        bw = w - P(28)
        tid = cv.create_text(cx, y + P(60), text=self._bubble, font=ft_bub,
                             fill=C["ink"], width=bw - P(22), justify="center")
        x1, ty1, x2, ty2 = cv.bbox(tid)
        bh = (ty2 - ty1) + P(20)
        self._world_bubble(cv, P(14), y, w - P(14), y + bh, rr_bub)
        cv.coords(tid, cx, y + bh / 2)
        cv.tag_raise(tid)
        y += bh + P(16)
        # 今日服务战绩（从表格状态列实时统计，填住中段空白）
        found = len(self.entries)
        ok = fail = 0
        try:
            for iid in self.tree.get_children():
                st = str(self.tree.set(iid, "status") or "")
                if st.startswith("✓"):
                    ok += 1
                elif st.startswith("✗"):
                    fail += 1
        except Exception:
            pass
        cv.create_line(P(24), y, w - P(24), y, fill=C["line"])
        self._world_stats_head(cv, cx, y + P(15), w)
        cv.create_text(cx, y + P(15), text="今 日 服 务", font=F_SM_B, fill=C["plum"])
        # 六界各自的「人设记号」：找到 / 搬完 / 没拿下（比通用勾叉更贴角色）
        _sg = {
            "maid":  ("♡", "♥", "✖"),   # 桃桃：爱心 / 实心爱 / 碎
            "hanfu": ("❖", "吉", "凶"),  # 汉服：花钿 / 上吉 / 凶签
            "magic": ("✦", "★", "✧"),   # 魔法：星屑 / 点亮 / 黯星
            "miko":  ("♪", "福", "忌"),  # 巫女：神乐铃 / 招福 / 忌讳
            "robot": ("▸", "OK", "NG"),  # 机器人：HUD 游标 / OK / NG
            "goth":  ("❦", "☽", "✠"),   # 哥特：黑蔷薇 / 盈月 / 十字
        }.get(self.theme, ("♡", "✓", "✗"))
        rows = [("%s 为主人找到" % _sg[0], "%d 个" % found),
                ("%s 已搬完" % _sg[1], "%d 个" % ok),
                ("%s 没拿下" % _sg[2], "%d 个" % fail)]
        for i, (k, val) in enumerate(rows):
            ry = y + P(38) + i * P(21)
            cv.create_text(P(24), ry, anchor="w", text=k, font=F_SM, fill=C["ink"])
            cv.create_text(w - P(24), ry, anchor="e", text=val, font=F_SM_B, fill=C["ink"])
        y += P(38) + len(rows) * P(21) + P(10)
        # 短句指挥（桃桃专属；本地解析，不联网不花积分）+ 小贴士
        is_maid = self.theme == "maid"
        th = P(72) if is_maid else 0
        top = h - th - P(10)
        if is_maid:
            cv.create_line(P(14), top, w - P(14), top, fill=C["line"])
            cv.create_text(P(18), top + P(5), anchor="nw", text="短句指挥",
                           font=F_SM_B, fill=C["plum"])
            cv.create_text(w - P(18), top + P(6), anchor="ne",
                           text="回车即执行", font=F_SM, fill=C["ink_l"])
            self._cmd_entry_build()
            try:
                self._cmd_entry.place(x=P(18), y=top + P(22),
                                      width=w - P(36), height=P(24))
            except Exception:
                pass
        else:
            try:
                if self._cmd_entry is not None:
                    self._cmd_entry.place_forget()
            except Exception:
                pass
        # 立绘（在剩余区域垂直居中；空间不够就再降一档，保证不被裁掉脑袋）
        side = _art("SIDE", self.S)
        if side is not None:
            top_y, bot_y = y + P(6), top - P(6)
            space = bot_y - top_y
            im = side
            if im.height() > space:
                # 高 DPI 立绘装不下时，先用 1x 原尺寸（比再降一档大一截），仍不行才 subsample
                try:
                    im1 = _art("SIDE", 1.0)
                    if im1 is not None and im1.height() <= space:
                        im = im1
                    else:
                        im = side.subsample(2, 2)
                except Exception:
                    im = side
            cv.create_image(cx, (top_y + bot_y) // 2, image=im, anchor="center")
            # subsample 产生的是临时 PhotoImage，Tk 不持有 Python 引用，GC 后立绘会变空白
            self._side_img_ref = im
        # 每个角色一个专属「世界」侧栏装饰 + 环境动画
        self._world_rail(cv, w, h, band)
        self._seed_ambient(cv)

    # ---- 短句指挥：本地解析，不联网、不花积分 ----
    def _cmd_entry_build(self):
        """侧栏里的指挥输入框（Entry 叠在 Canvas 上，换肤时只改色不重建）。"""
        if getattr(self, "_cmd_entry", None) is not None:
            try:
                self._cmd_entry.configure(
                    bg=C["pink_xl"],
                    fg=C["ink_l"] if getattr(self, "_cmd_ph_on", False) else C["ink"],
                    insertbackground=C["plum"],
                    highlightbackground=C["pink_l"], highlightcolor=C["pink"])
                return
            except Exception:
                self._cmd_entry = None
        try:
            e = tk.Entry(self.rail, textvariable=self._cmd_var, relief="flat", bd=0,
                         highlightthickness=1, bg=C["pink_xl"], fg=C["ink"],
                         insertbackground=C["plum"],
                         highlightbackground=C["pink_l"], highlightcolor=C["pink"],
                         font=F_SM)
            e.bind("<Return>", self._on_command)
            e.bind("<KP_Enter>", self._on_command)
            e.bind("<Escape>", lambda ev: (self._cmd_var.set(""),
                                           self.root.focus_set()))
            e.bind("<FocusIn>", self._cmd_focus_in)
            e.bind("<FocusOut>", self._cmd_focus_out)
            self._cmd_entry = e
            self._cmd_ph_show()
        except Exception:
            self._cmd_entry = None

    def _cmd_ph_show(self):
        if not (self._cmd_var.get() or "").strip():
            self._cmd_ph_on = True
            self._cmd_ph_text = "对桃桃说：暂停 / 继续 / 变身…"
            self._cmd_var.set(self._cmd_ph_text)
            try:
                self._cmd_entry.configure(fg=C["ink_l"])
            except Exception:
                pass

    def _cmd_focus_in(self, _e=None):
        if getattr(self, "_cmd_ph_on", False):
            self._cmd_ph_on = False
            self._cmd_var.set("")
            try:
                self._cmd_entry.configure(fg=C["ink"])
            except Exception:
                pass

    def _cmd_focus_out(self, _e=None):
        self._cmd_ph_show()

    def _on_command(self, _e=None):
        if (getattr(self, "_cmd_ph_on", False)
                and (self._cmd_var.get() or "") == getattr(self, "_cmd_ph_text", "")):
            self._cmd_var.set("")
            self._cmd_ph_on = False
            return "break"
        self._cmd_ph_on = False
        txt = (self._cmd_var.get() or "").strip()
        if not txt:
            return "break"
        self._cmd_var.set("")
        try:
            self.root.focus_set()
        except Exception:
            pass
        if MT is None:
            self.say("咱还没学会听吩咐呢…")
            return "break"
        try:
            act, arg = MT.parse_command(txt)
        except Exception:
            act, arg = "unknown", ""
        self.log("[指挥] 「%s」→ %s%s" % (txt[:40], act, (" " + arg) if arg else ""))
        try:
            self._cmd_run(act, arg)
        except Exception as e:
            self.log("[指挥] 执行失败: %s" % e)
            self.say("唔…这一步咱没做好，主人再试一次？")
        return "break"

    def _cmd_run(self, act, arg):
        if act == "empty":
            self.say("主人有什么吩咐？")
        elif act == "hello":
            self.say(MT.pick("cmd_hello", self._form) or "在呢～")
        elif act == "pause":
            if not self.busy:
                self.say("手上没活呢，不用暂停～要咱开工吗？")
            elif not self.pause_evt.is_set():
                self.say("已经暂停着啦，说「继续」咱就接着搬 ♪")
            else:
                self.toggle_pause()
        elif act == "resume":
            if self.busy and not self.pause_evt.is_set():
                self.toggle_pause()
            elif self.busy:
                self.say("咱一直在搬，没停过呢 ♪")
            else:
                self.say("现在没有在搬的活，说「下载」咱就开工。")
        elif act == "retry":
            self._retry_failed()
        elif act == "open_dir":
            self.open_outdir()
            self.say(MT.pick("cmd_open", self._form) or "文件夹打开啦～")
        elif act == "transform":
            self._transform(arg)
        elif act == "probe":
            self._cmd_probe()
        elif act == "download_all":
            if not self.entries:
                self.say("列表还空着呢，先让咱嗅一嗅？")
            else:
                self.start_download(False)
        elif act == "download_sel":
            if not self.entries:
                self.say("列表还空着呢，先让咱嗅一嗅？")
            elif not self.tree.selection():
                self.say("先用鼠标选中几行，咱才知道搬哪些呀。")
            else:
                self.start_download(True)
        elif act == "quality":
            self.quality_var.set(arg)
            self._sync_quality()
            self.log("[指挥] 画质 → %s" % arg)
        elif act == "clear":
            self.say(MT.pick("cmd_clear", self._form) or "清空这件事主人亲手来吧。")
        elif act == "status":
            self._cmd_status()
        else:
            self.say(MT.pick("cmd_unknown", self._form) or "这句咱没听懂…")

    def _cmd_probe(self):
        if self.busy:
            self.say("手上还有活，等这批完再嗅～")
            return
        if not self.url_var.get().strip():
            if self._clip_url:
                self._take_clip()
            else:
                self.say("网址框还空着呢，贴一个给咱吧 ♪")
            return
        self.start_probe()

    def _cmd_status(self):
        if self.busy:
            if self._probing:
                self.say("还在嗅呢，已经翻出 %d 个啦。" % len(self.entries))
            elif not self.pause_evt.is_set():
                self.say("暂停中：%d/%d，进度都替主人锁好了。"
                         % (self._as["n"], self._as["total"]))
            else:
                self.say("正在搬 %d/%d，%d%%，咱盯着呢。"
                         % (self._as["n"], self._as["total"],
                            int(round(self._as["frac"] * 100))))
            return
        if not self.entries:
            self.say("列表空着呢，贴个网址或复制个链接给咱？")
            return
        ok = fail = 0
        try:
            for iid in self.tree.get_children():
                st = str(self.tree.set(iid, "status") or "")
                if st.startswith("✓"):
                    ok += 1
                elif st.startswith("✗"):
                    fail += 1
        except Exception:
            pass
        self.say("列表里 %d 个：搬完 %d，没拿下 %d。要咱接着搬吗？"
                 % (len(self.entries), ok, fail))

    def _retry_failed(self):
        rows = []
        try:
            for iid in self.tree.get_children():
                if str(self.tree.set(iid, "status") or "").startswith("✗"):
                    rows.append(int(iid))
        except Exception:
            rows = []
        if not rows:
            self.say("这一批没有落下的，全搬回来了 ♪")
            return
        outdir = self.dir_var.get().strip() or DEFAULT_OUTDIR
        try:
            os.makedirs(outdir, exist_ok=True)
        except Exception as e:
            self.log("[重试] 目录不可用: %s" % e)
            self.say("保存目录打不开…主人先换个目录吧。")
            return
        quality = self.quality_var.get()
        new_rows, _dup = self._enqueue_rows(rows, outdir, quality)
        if not new_rows:
            self.say("失败的这些已经在队列里啦，咱盯着呢。")
            return
        for r in new_rows:
            self.set_status(r, "排队中")
        self.log("[重试] 重新入队 %d 个失败项" % len(new_rows))
        self.say(MT.pick("cmd_retry", self._form) if MT else "收到，再试一次。")
        self._start_worker(outdir, quality)

    def _enqueue_rows(self, rows, outdir, quality):
        """原子入队（与 worker 排空共用 _job_lock），返回 (新入队行, 重复数)。"""
        with self._job_lock:
            new_rows = [r for r in rows if r not in self._queued_rows]
            dup = len(rows) - len(new_rows)
            for r in new_rows:
                self._queued_rows.add(r)
                self._cancel_rows.discard(r)
                self._pending.append((r, self.entries[r], outdir, quality))
        return new_rows, dup

    def say(self, msg):
        """换台词（右侧气泡 + 右下小人），里程碑式提示；细节仍看状态栏与日志。

        气泡是"说话"用的，不是角色的边框 —— 说完必须自己收起来，
        否则大尺寸下头顶常年挂一个空框，既挡眼也不像活人。"""
        self._bubble = msg
        try:
            txt = str(msg or "")
            hold = min(9.0, max(3.5, 3.2 + 0.10 * len(txt)))
            self._as_bubble_until = time.time() + hold
        except Exception:
            pass
        # 实时桌宠：台词直接喂给宿主气泡（Tk 小窗不存在，下面全部是旧路径）
        if getattr(self, "pet_live", False) and getattr(self, "_pet", None):
            try:
                self._pet.say(msg)
            except Exception:
                pass
            return
        try:
            self._draw_rail()
        except Exception:
            pass
        try:
            if self._as["mode"] == "idle":
                self._draw_assistant()
        except Exception:
            pass

    # ---- 右下角 Q版小人助手：气泡说速度/ETA + 环形进度 + 成功浮层通知 ----
    _AS_KEY = "#0B0C0D"          # 透明键色：Windows -transparentcolor 抠掉，只剩角色轮廓

    def _set_as_step(self, step):
        try:
            self._as_step = max(1, min(3, int(step)))
        except Exception:
            self._as_step = 2
        try:
            self.cfg["as_step"] = self._as_step
            save_config(self.cfg)
        except Exception:
            pass
        for _c in ("_frames_cache", "_cast_cache"):
            try:
                getattr(self, _c, {}).clear()
            except Exception:
                pass
        # 记录换档前的"底部中心"锚点，换档后按新尺寸还原，避免助手突然跳回右下角
        anchor = None
        try:
            if getattr(self, "_as_pos", None) is not None:
                anchor = (self._as_pos[0] + self._as_w / 2.0,
                          self._as_pos[1] + self._as_h)
        except Exception:
            anchor = None
        try:
            old = getattr(self, "_as_top", None)
            if old is not None:
                old.destroy()
        except Exception:
            pass
        self._as_pos = None
        try:
            self._build_assistant()
        except Exception:
            pass
        if anchor is not None:
            try:
                self._as_pos = (int(anchor[0] - self._as_w / 2.0),
                                int(anchor[1] - self._as_h))
                self._as_place()
            except Exception:
                pass

    @staticmethod
    def _radial_sector(theta, n):
        step = 360.0 / max(1, n)
        return int(((theta % 360.0) + step / 2.0) // step) % n

    def _as_rad_shape(self, theme, R, cr):
        # 形状暂一律用干净圆环：心/星/扇等极坐标轮廓未经离线渲染自检前不启用（易歪丑）。
        lut = [float(R)] * 720
        lo, span = 0.0, 360.0
        for k in range(720):
            lut[k] = max(cr + 8, min(R * 1.15, lut[k]))
        return lut, lo, span

    def _as_menu(self, _e=None):
        try:
            if getattr(self, "_as_rad_top", None) is not None:
                self._as_rad_close()
                return
        except Exception:
            self._as_rad_top = None
        try:
            import math
            P = self.P
            top = self._as_top
            cx = top.winfo_rootx() + self._as_w // 2
            cy = top.winfo_rooty() + int(self._as_h * 0.55)
            R = int(P(184))
            cr = int(P(66))
            rt = tk.Toplevel(self.root)
            try:
                rt.overrideredirect(True)
                rt.attributes("-topmost", True)
                rt.attributes("-transparentcolor", self._AS_KEY)
            except Exception:
                pass
            cv = tk.Canvas(rt, width=2 * R, height=2 * R, bg=self._AS_KEY,
                           highlightthickness=0, bd=0)
            cv.pack()
            rt.geometry("+%d+%d" % (cx - R, cy - R))
            self._as_rad_top = rt
            self._as_rad_cv = cv

            try:
                paused = bool(self.busy) and not self.pause_evt.is_set()
            except Exception:
                paused = False
            _ti = {
                "maid":  {3: "🎀", 4: "👗", 5: "🐾"},
                "hanfu": {3: "🖌", 4: "👘", 5: "🌸"},
                "magic": {3: "🔮", 4: "🃏", 5: "✨"},
                "miko":  {3: "⛩", 4: "🎎", 5: "🔔"},
                "robot": {3: "⚙", 4: "🤖", 5: "🔌"},
                "goth":  {3: "🦇", 4: "🕸", 5: "🌙"},
            }.get(self.theme, {})
            items = [
                ("⏸" if not paused else "▶", "暂停" if not paused else "继续", self._as_act_pause),
                ("📁", "文件夹", self._as_act_open_dir),
                ("🖥", "主界面", self._as_act_show_main),
                (_ti.get(3, "✨"), "大小", self._as_act_cycle_size),
                (_ti.get(4, "🎨"), "换装", self._as_act_next_persona),
                (_ti.get(5, "👋"), "摸头", self._as_act_poke),
                ("📌", "置顶", self._as_act_toggle_top),
                ("✕", "收起", self._as_act_hide),
            ]
            n = len(items)
            accent = C.get("pink", "#F77FB0")
            base = [C.get("card", "#FFFFFF"), C.get("pink_xl", "#FCEFF6")]
            hot = C.get("pink_l", "#F6CFE1")
            edge = C.get("line", "#EBD7E1")
            lbl_col = C.get("plum", "#8A4A66")
            lut, lo, span = self._as_rad_shape(self.theme, R, cr)
            step = span / n
            self._as_rad_items = [(lo + step * (i + 0.5), step, cb)
                                  for i, (_ic, _lb, cb) in enumerate(items)]

            def outer_at(theta):
                return lut[int((theta % 360.0) * 2) % 720]

            def pt(r, theta):
                a = math.radians(theta)
                return (R + r * math.sin(a), R - r * math.cos(a))

            def draw(Rc, rot=0.0):
                cv.delete("all")
                sc = Rc / float(R)
                for i, (icon, label, cb) in enumerate(items):
                    tc = lo + step * (i + 0.5) + rot
                    half = step / 2
                    flat = []
                    steps = 22
                    for s in range(steps + 1):
                        u = tc - half + 2 * half * s / steps
                        flat.append(pt(outer_at(u) * sc, u))
                    for s in range(steps + 1):
                        u = tc + half - 2 * half * s / steps
                        flat.append(pt(cr * sc, u))
                    flat = [c for p in flat for c in p]
                    cv.create_polygon(*flat, smooth=False, fill=base[i % 2],
                                      outline=edge, width=1, tags=("poly", "p%d" % i))
                    ix, iy = pt((cr + outer_at(tc)) / 2.0 * sc + P(4), tc)
                    cv.create_text(ix, iy - P(17), text=icon,
                                   font=("Segoe UI Emoji", int(P(17))), fill=accent,
                                   tags=("ic%d" % i,))
                    cv.create_text(ix, iy + P(15), text=label, font=F_SM,
                                   fill=lbl_col, tags=("lb%d" % i,))
                cv.create_oval(R - cr, R - cr, R + cr, R + cr, outline=edge,
                               width=1, tags=("holeedge",))

            def which(ev):
                dx, dy = ev.x - R, ev.y - R
                rr = (dx * dx + dy * dy) ** 0.5
                theta = math.degrees(math.atan2(dx, -dy)) % 360
                a = (theta - lo) % 360.0
                if a > span:
                    return "out"
                if rr <= cr:
                    return "core"
                if rr > outer_at(theta):
                    return "out"
                idx = int(a // step)
                return idx if idx < n else "out"

            def on_motion(ev):
                w = which(ev)
                for i in range(n):
                    cv.itemconfig("p%d" % i, fill=(hot if i == w else base[i % 2]))

            def on_click(ev):
                w = which(ev)
                if w in ("core", "out"):
                    self._as_rad_close()
                    return
                cb = self._as_rad_items[w][2]
                self._as_rad_close()
                try:
                    cb()
                except Exception:
                    pass

            def bind():
                cv.bind("<Motion>", on_motion)
                cv.bind("<Button-1>", on_click)
                cv.bind("<Button-3>", lambda e: self._as_rad_close())
                rt.bind("<Button-1>", on_click)
                rt.bind("<Escape>", lambda e: self._as_rad_close())
                try:
                    rt.grab_set_global()
                except Exception:
                    try:
                        rt.grab_set()
                    except Exception:
                        pass
                try:
                    rt.focus_force()
                except Exception:
                    pass

            # 出场：环形边旋转边向外展开（扇形旋出）
            frames = 16

            def anim(k):
                t = (k + 1) / float(frames)
                e = 1 - (1 - t) ** 3          # ease-out
                Rc = cr + 6 + (R - cr - 6) * e
                rot = -26 * (1 - e)            # 从 -26° 旋回 0°
                draw(Rc, rot)
                if k < frames - 1:
                    rt.after(18, lambda: anim(k + 1))
                else:
                    draw(R, 0)
                    bind()
            anim(0)
        except Exception:
            self._as_rad_close()

    def _as_rad_close(self, _e=None):
        try:
            rt = getattr(self, "_as_rad_top", None)
            if rt is not None:
                try:
                    rt.grab_release()
                except Exception:
                    pass
                rt.destroy()
        except Exception:
            pass
        self._as_rad_top = None
        self._as_rad_cv = None

    def _as_act_cycle_size(self):
        order = [2, 1, 3]
        cur = getattr(self, "_as_step", 2)
        try:
            nxt = order[(order.index(cur) + 1) % len(order)]
        except Exception:
            nxt = 2
        self._set_as_step(nxt)
        try:
            if getattr(self, "asize_var", None) is not None:
                self.asize_var.set({3: "小", 2: "标准", 1: "大"}.get(nxt, "标准"))
        except Exception:
            pass

    def _as_act_next_persona(self):
        try:
            self.cycle_theme(1)
        except Exception:
            pass

    def _as_act_hide(self):
        try:
            self._toggle_assistant(True)
        except Exception:
            pass

    def _as_act_poke(self):
        try:
            self.poke()
        except Exception:
            pass

    def _as_act_toggle_top(self):
        try:
            cur = not getattr(self, "_as_topmost", True)
            if getattr(self, "_as_top", None) is not None:
                self._as_top.attributes("-topmost", cur)
                self._as_topmost = cur
        except Exception:
            pass

    def _as_act_pause(self):
        try:
            self.toggle_pause()
        except Exception:
            pass

    def _as_act_open_dir(self):
        try:
            self.open_outdir()
        except Exception:
            pass

    def _as_act_show_main(self):
        try:
            self.root.deiconify()
            self.root.lift()
        except Exception:
            pass

    def _as_set_step_menu(self, step):
        self._set_as_step(step)
        try:
            _as_map = {3: "小", 2: "标准", 1: "大"}
            if getattr(self, "asize_var", None) is not None:
                self.asize_var.set(_as_map.get(step, "标准"))
        except Exception:
            pass

    def _as_fit_size(self):
        """按当前素材首帧算桌宠窗体尺寸：档位公式打底，帧更大就跟着放大（只升不降，
        旧素材 262x420 表现完全不变）。换角色窗体没跟着变会把头裁掉、气泡压脸（实测）。"""
        P = self.P
        sc = 3.0 / max(1, getattr(self, "_as_step", 2))   # 档位越小立绘越大，窗体同步放大
        w = int(round(P(208) * sc))
        h = int(round(P(188) * sc))
        try:
            _f0 = self._first_frame()          # 只解第 1 帧拿尺寸，别在这里解整套
            if _f0 is not None:
                w = max(w, int(_f0.width()) + P(10))
                h = max(h, int(_f0.height()) + P(62))
        except Exception:
            pass
        return w, h

    def _as_resize(self):
        """把 _as_fit_size 应用到桌宠窗+画布；尺寸没变则不动。返回是否变化。"""
        try:
            w, h = self._as_fit_size()
        except Exception:
            return False
        if (w, h) == (getattr(self, "_as_w", 0), getattr(self, "_as_h", 0)):
            return False
        self._as_w, self._as_h = w, h
        cv = getattr(self, "assist", None)
        if cv is not None:
            try:
                cv.configure(width=w, height=h)
            except Exception:
                pass
        top = getattr(self, "_as_top", None)
        if top is not None:
            try:
                top.geometry("%dx%d" % (w, h))
            except Exception:
                pass
        return True

    def _build_assistant(self):
        P = self.P
        self._as_w, self._as_h = self._as_fit_size()
        top = tk.Toplevel(self.root)
        try:
            top.overrideredirect(True)                    # 无边框、不进任务栏
            top.attributes("-transparentcolor", self._AS_KEY)   # 键色像素→真透明
            top.attributes("-topmost", True)              # 独立桌宠：浮在桌面最上层，脱离主窗
        except Exception:
            pass
        self._as_top = top
        cv = tk.Canvas(top, width=self._as_w, height=self._as_h,
                       bg=self._AS_KEY, highlightthickness=0, bd=0)
        cv.pack(fill="both", expand=True)
        self.assist = cv
        # 桌宠相对主窗右下角的落点（se 锚点）。sc＝档位缩放，与 _as_fit_size 同一公式；
        # §16.4 把尺寸算式抽进 _as_fit_size 后这里漏了 sc，开机建助手直接 NameError。
        sc = 3.0 / max(1, getattr(self, "_as_step", 2))
        self._as_xy = (-int(round(P(286) * sc)), -int(round(P(120) * sc)))   # 相对窗口右下角的偏移（se 锚点）
        self._as_place()
        cv.bind("<ButtonPress-1>", self._as_press)
        cv.bind("<B1-Motion>", self._as_drag_move)
        cv.bind("<ButtonRelease-1>", self._as_release)
        cv.bind("<Double-Button-1>", self._as_dblclick)
        cv.bind("<Button-3>", self._as_menu)
        cv.bind("<Enter>", lambda e: self._as_hover(True))
        cv.bind("<Leave>", lambda e: self._as_hover(False))
        self._as_hov = False
        try:
            self.root.bind("<Configure>", self._as_on_root_cfg, add="+")
            self.root.bind("<Map>", lambda e: self._as_place(), add="+")
            self.root.bind("<Raise>", lambda e: top.lift(), add="+")
        except Exception:
            pass
        self._draw_assistant()
        try:
            if getattr(self, "_as_detached", False):
                self._as_place()      # 刚画完才知道本体包围盒，按角色边界重夹一次
        except Exception:
            pass
        try:
            self._c24_warm()          # 开机首个带 cast24 的角色也预解码一遍
        except Exception:
            pass
        try:                          # 视线跟随 + 眨眼节奏（旧素材会自己静默跳过）
            if getattr(self, "_c24_alive_job", None):
                try:
                    self.root.after_cancel(self._c24_alive_job)
                except Exception:
                    pass
                self._c24_alive_job = None
            self._c24_blink_next = 0.0
            self._c24_alive_tick()
        except Exception:
            pass

    def _as_place(self):
        top = getattr(self, "_as_top", None)
        if top is None:
            return
        try:
            pos = getattr(self, "_as_pos", None)
            if pos is None:
                self.root.update_idletasks()
                rw, rh = self.root.winfo_width(), self.root.winfo_height()
                rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
                se_x, se_y = rx + rw + self._as_xy[0], ry + rh + self._as_xy[1]
                pos = (se_x - self._as_w, se_y - self._as_h)
            x, y = int(pos[0]), int(pos[1])
            try:
                sw, sh = top.winfo_screenwidth(), top.winfo_screenheight()
                hit = getattr(self, "_as_body_hit", None)
                if hit:
                    # 夹的是「角色本体」而不是窗体：窗体四周有上百像素透明留白，
                    # 按窗体夹会让她差一截就撞墙 —— 那堵墙是框，不是屏幕。
                    x = max(-int(hit[0]), min(x, sw - int(hit[2])))
                    y = max(-int(hit[1]), min(y, sh - int(hit[3])))
                else:
                    x = max(0, min(x, sw - self._as_w))
                    y = max(0, min(y, sh - self._as_h))
            except Exception:
                pass
            self._as_pos = (x, y)
            top.geometry("%dx%d+%d+%d" % (self._as_w, self._as_h, x, y))
            top.lift()
        except Exception:
            pass

    def _as_on_root_cfg(self, _e=None):
        # 只有还没被拖出去（跟随态）时才随主窗移动；拖出去后固定不动，避免弹回
        if getattr(self, "_as_detached", False):
            return
        if getattr(self, "_as_cfg_job", None):
            return
        def go():
            self._as_cfg_job = None
            if not getattr(self, "_as_detached", False):
                self._as_place()
        self._as_cfg_job = self.root.after(80, go)

    @staticmethod
    def _hit(rect, e):
        return bool(rect and rect[0] <= e.x <= rect[2] and rect[1] <= e.y <= rect[3])

    def _as_press(self, e):
        if self._hit(getattr(self, "_as_close_hit", None), e):
            self._toggle_assistant(hide=True)
            return
        if self._hit(getattr(self, "_as_clip_hit", None), e):
            self._as_clicked = False
            self._take_clip()
            return
        if self._hit(getattr(self, "_as_form_hit", None), e):
            self._as_clicked = False
            self._transform_toggle()
            return
        if self._as_click_job:
            try:
                self.root.after_cancel(self._as_click_job)
            except Exception:
                pass
            self._as_click_job = None
        body = getattr(self, "_as_body_hit", None)
        self._as_clicked = self._hit(body, e)
        pos = getattr(self, "_as_pos", None)
        if pos is None:
            try:
                pos = (self._as_top.winfo_x(), self._as_top.winfo_y())
            except Exception:
                pos = (0, 0)
        self._as_drag = (e.x_root, e.y_root, pos[0], pos[1])

    def _as_drag_move(self, e):
        if not self._as_drag:
            return
        ox, oy, sx, sy = self._as_drag
        if abs(e.x_root - ox) + abs(e.y_root - oy) > 4:
            self._as_clicked = False
            self._as_dragging = True
            self._as_detached = True          # 拖出去后自由浮动，不再跟随主窗
        self._as_pos = (sx + (e.x_root - ox), sy + (e.y_root - oy))
        self._as_place()

    def _c24_ladder(self):
        """该角色素材是否支持 §5.1 连点递进（同时有 poke 与 happy 分镜）。"""
        if C24 is None:
            return False
        c = self._c24()
        if c is None:
            return False
        fr = getattr(c, "FRAMES", {}) or {}
        return "poke" in fr and "happy" in fr

    def _as_release(self, e):
        self._as_dragging = False
        if getattr(self, "_as_clicked", False):
            self._as_clicked = False
            if self._c24_ladder():
                # 连点计次要即时响应：再等 230ms 会被下一次按下的 after_cancel 吞掉，
                # 结果就是"点了没反应"、递进永远停在 1 下
                self._do_poke()
            else:
                # 延迟 230ms 再摸头：双击时取消单击，避免「先摸头再变身」
                self._as_click_job = self.root.after(230, self._do_poke)
        self._as_drag = None

    def _as_dblclick(self, e):
        if self._c24_ladder():
            # Tk 把 500ms 内的第二下当成 Double-1，普通 ButtonPress 不再触发；
            # 这里补回"按下"语义，让随后的 release 正常计一次点击（§5.1 连点递进）
            self._as_press(e)
            return
        if self._as_click_job:
            try:
                self.root.after_cancel(self._as_click_job)
            except Exception:
                pass
            self._as_click_job = None
        self._as_clicked = False
        self._as_drag = None
        self._as_dragging = False
        if self._c24_ladder():
            return          # 双击已由 §5.1 连点递进接管（第二下=喜欢），不再触发旧双形态变身
        if (self._hit(getattr(self, "_as_body_hit", None), e)
                or self._hit(getattr(self, "_as_form_hit", None), e)):
            if self._is_cast():
                self._as_react("egg", self._seq_len("egg") or 2.0, force=True)
                self._say_soft("egg")
            else:
                self._transform_toggle()

    def _do_poke(self):
        self._as_click_job = None
        self.poke()

    def _as_hover(self, on):
        self._as_hov = on
        if self._v2_anim():
            if getattr(self, "_as_hover_job", None):
                try:
                    self.root.after_cancel(self._as_hover_job)
                except Exception:
                    pass
                self._as_hover_job = None
            if on:
                self._as_hover_job = self.root.after(2400, self._as_stare)
                self._c24_near_shy()
        self._draw_assistant()

    def _c24_alive_tick(self):
        """实时决定"她在看哪个方向"和"什么时候眨一次眼"。

        视线：鼠标相对桌宠中心的方位 → c/l/r/u/d 五档（rig 自带的鼠标跟随在桃桃母图上
              实测不生效，所以按接入文档 §5.2 说的用脚本化视线：分镜预先渲好五个方向）。
        眨眼：按 §7 的节奏（平均 4s + 随机 0-8s）单独触发 blink 分镜，
              不再"每个待机循环都眨一次"（那样约 2 秒一眨，不像活人）。
        """
        self._c24_alive_job = None
        try:
            if C24 is None or getattr(self, "_as_hidden", True):
                return
            c = self._c24()
            if c is None:
                return
            fr = getattr(c, "FRAMES", {}) or {}
            if not any(k in fr for k in ("gaze_l", "gaze_r", "gaze_u", "gaze_d")):
                return                        # 旧素材没有视线分镜，整个机制静默跳过
            now = time.time()
            busy = (self.busy or self._probing or getattr(self, "_as_dragging", False)
                    or self._as["mode"] not in ("idle", "done")
                    or now < self._c24_poke_until or now < self._c24_shy_cd)
            if busy:
                self._c24_follow = False
            else:
                top = getattr(self, "_as_top", None)
                if top is not None:
                    cx = top.winfo_rootx() + self._as_w // 2
                    cy = top.winfo_rooty() + int((self._as_h - self.P(7)) * 0.42)  # 大致在脸的高度
                    try:
                        dx = self.root.winfo_pointerx() - cx
                        dy = self.root.winfo_pointery() - cy
                    except Exception:
                        dx = dy = 0
                    dist = (dx * dx + dy * dy) ** 0.5
                    self._c24_follow = dist <= max(360, self._as_w * 1.6)
                    if self._c24_follow:
                        import math
                        if dist < 64:
                            self._c24_gaze = "c"
                        elif abs(dx) > abs(dy) * 1.15:
                            self._c24_gaze = "l" if dx < 0 else "r"
                        else:
                            self._c24_gaze = "u" if dy < 0 else "d"
                # 眨眼排期：到点就开一个 blink 窗，然后随机下一次
                if "blink" in fr:
                    if not self._c24_blink_next:
                        self._c24_blink_next = now + 2.0
                    if now >= self._c24_blink_next and now >= self._c24_blink_until:
                        dur = max(0.25, len((fr.get("blink") or [[]])[0] or [])
                                  * C24.ticks(c, "blink") / 1000.0)
                        self._c24_blink_until = now + dur
                        import random
                        self._c24_blink_next = now + dur + random.uniform(2.8, 7.5)
        except Exception:
            pass
        finally:
            # 隐藏时不再空转重排（省后台 CPU）；重新显示由 _toggle_assistant 重启本循环
            if not getattr(self, "_as_hidden", True):
                self._c24_alive_job = self.root.after(120, self._c24_alive_tick)
            else:
                self._c24_alive_job = None

    def _c24_near_shy(self):
        """接入文档 §5.2：鼠标靠近（进入桌宠窗口）→ 害羞躲开一眼，8s 冷却防刷屏。
        只有 rig 素材带 shy 分镜的角色生效；忙/嗅探/正被她拖着时不打扰。"""
        if C24 is None:
            return
        c = self._c24()
        if c is None or "shy" not in (getattr(c, "FRAMES", {}) or {}):
            return
        now = time.time()
        if now < getattr(self, "_c24_shy_cd", 0.0) or now < self._c24_poke_until:
            return
        if (self.busy or self._probing or getattr(self, "_as_dragging", False)
                or self._as["mode"] not in ("idle", "done")):
            return
        self._c24_shy_cd = now + 8.0
        self._c24_click_state = "shy"
        self._c24_poke_until = now + max(1.5, 24 * C24.ticks(c, "shy") / 1000.0)
        self._as_last_key = None

    def _as_stare(self):
        self._as_hover_job = None
        now = time.time()
        if not self._v2_anim() or not getattr(self, "_as_hov", False) \
                or self.busy or self._as_dragging or now - getattr(self, "_as_stare_t", 0.0) < 40:
            return
        if self._as["mode"] != "idle" or self._as_transform_to:
            return
        self._as_stare_t = now
        self._as_react("shy", 1.6)
        self._say_soft("stare")

    def _toggle_assistant(self, hide=None):
        top = getattr(self, "_as_top", None)
        if top is None:
            return
        hide = (not self._as_hidden) if hide is None else hide
        self._as_hidden = hide
        if hide:
            top.withdraw()
            self._release_frame_cache()      # 隐藏即释放立绘解码帧，降常驻内存
        else:
            top.deiconify()
            self._as_place()
            try:
                self._c24_warm()             # 重新显示：后台分块预解码补回帧
            except Exception:
                pass
            try:                             # 重启视线/眨眼计时器（隐藏时已停）
                if not getattr(self, "_c24_alive_job", None):
                    self._c24_blink_next = 0.0
                    self._c24_alive_tick()
            except Exception:
                pass

    def _release_frame_cache(self):
        """释放桌宠已解码的帧缓存（tk.PhotoImage）并回收，降低隐藏/常驻时的内存占用。
        下次需要时 _decode/_as_frames 会按需重新解码，_c24_warm 会预热补回，画面无损。"""
        try:
            if getattr(self, "_c24_alive_job", None):
                self.root.after_cancel(self._c24_alive_job)
        except Exception:
            pass
        self._c24_alive_job = None
        for _c in ("_frames_cache", "_cast_cache"):
            try:
                getattr(self, _c, {}).clear()
            except Exception:
                pass
        try:
            import gc
            gc.collect()
        except Exception:
            pass

    def _as_refresh(self):
        if getattr(self, "_as_hidden", True) or getattr(self, "_as_job", None):
            return
        def go():
            self._as_job = None
            try:
                self._draw_assistant()
            except Exception:
                pass
        self._as_job = self.root.after(170, go)

    def _as_bubble_on(self):
        """气泡该不该露出来：状态期（下载/暂停/嗅探/变身/成败/剪贴板有活）一直显示；
        平时只在"刚说过话"的窗口期内显示，时间一到自动收起。"""
        try:
            if self._as_transform_to or self._probing:
                return True
            if self._as["mode"] in ("busy", "pause", "done", "fail"):
                return True
            if self._clip_url and getattr(self, "_form", "daily") == "daily":
                return True
            if getattr(self, "_as_hov", False):        # 鼠标停在她身上时给提示
                return True
            return time.time() < getattr(self, "_as_bubble_until", 0.0)
        except Exception:
            return True

    def _as_text(self):
        m = self._as["mode"]
        form = getattr(self, "_form", "daily")
        if self._as_transform_to:
            to = self._as_transform_to
            return ("工作形态，启动。" if to == "work" else "下班，变回来。",
                    "变身中 ✦ 稍等一下")
        if self._probing and self._as["mode"] != "pause":
            return ("鼻子凑近屏幕…", "正在找视频，稍等 ♪")
        if m == "busy":
            bits = []
            if self._as["speed"]:
                bits.append("↓ " + self._as["speed"])
            if self._as["eta"]:
                bits.append("剩 " + self._as["eta"])
            main = "  ".join(bits) if bits else (
                "搬运中…" if form == "work" else "嗅探中…")
            sub = "%d/%d · %s" % (self._as["n"], self._as["total"],
                                  (self._as["title"] or "")[:12])
            return main, sub
        if m == "pause":
            return ("II 已暂停" if form == "daily" else "暂停。进度锁好了。",
                    "说「继续」或点「继续下载」")
        if m == "done":
            if form == "work":
                return "收工。全部入库。", "双击我下班 · 或等咱自己变回来"
            return "全部搬完啦 ♡", "点小人唠两句 · Ctrl+H 收我"
        if m == "fail":
            if form == "work":
                return "报告：有没拿下的。", "说「重试」再搬一次"
            return "有几个没搬动…", "说「重试」再搬一次"
        if self._clip_url and form == "daily":
            return "剪贴板里有新链接 ♪", "点左边「✦ 嗅探」接活"
        if form == "work":
            return (self._bubble or "")[:26], "双击我下班 · 说「继续」也行"
        if self._is_cast():
            return (self._bubble or "")[:26], "双击我玩 · 说句话也行"
        return (self._bubble or "")[:26], "双击我变身 · 说句话也行"

    @staticmethod
    def _as_fit(text, font, maxw):
        """按像素宽度截断气泡文字（右下小人气泡很窄，长句要留出右上角的关闭键）。"""
        text = text or ""
        try:
            f = tkfont.Font(font=font)
            if f.measure(text) <= maxw:
                return text
            cut = text
            while cut and f.measure(cut + "…") > maxw:
                cut = cut[:-1]
            return (cut + "…") if cut else "…"
        except Exception:
            return text[:14] + ("…" if len(text) > 14 else "")

    def _draw_assistant(self):
        cv = getattr(self, "assist", None)
        if cv is None:
            return
        P = self.P
        cv.delete("all")
        self._as_img_id = None
        self._as_clip_hit = None
        self._as_form_hit = None
        w, h = self._as_w, self._as_h
        bxc = w // 2
        base_y = h - P(7)          # 给脚下阴影留出余量，别被画布底边裁成一条横线
        # 先算角色尺寸，好让气泡紧贴头顶
        fr = self._as_frame()
        chibi = None
        if fr is not None:
            ch_w, ch_h = fr.width(), fr.height()
        else:
            chibi = _art("CHIBI", self.S)
            if chibi is not None:
                ch_h = min(P(118), chibi.height())
                ch_w = int(chibi.width() * ch_h / chibi.height())
                if ch_w > P(150):
                    ch_w = P(150)
                    ch_h = int(chibi.height() * ch_w / chibi.width())
            else:
                ch_h = ch_w = P(96)
        # 气泡：紧贴角色头顶（放大后不再离得老远）
        if not self._as_bubble_on():
            self._as_close_hit = None          # 必须清掉：否则点头上那块空气会误触"隐藏助手"
            self._as_bubble_shown = False
        else:
            main, sub = self._as_text()
            self._as_bubble_shown = True
            bw = min(w - P(4), P(340))         # 大尺寸下别拉成一整条横幅，够放一句话就行
            bx = P(2) + (w - P(4) - bw) // 2   # 居中于角色
            bh = P(52)
            by = max(2, (base_y - ch_h) - bh - P(6))
            _rr(cv, bx, by, bx + bw, by + bh, P(13),
                fill=C["card"], outline=C["pink"] if self._as_hov else C["line"])
            tailx = bx + bw // 2
            cv.create_polygon(tailx - P(7), by + bh - 1, tailx + P(7), by + bh - 1,
                              tailx, by + bh + P(8), fill=C["card"], outline=C["line"])
            cv.create_line(tailx - P(6), by + bh, tailx + P(6), by + bh,
                           fill=C["card"], width=2)
            cx_b = bx + bw // 2
            tw = bw - P(34)
            cv.create_text(cx_b, by + P(19), text=self._as_fit(main, F_UI_B, tw),
                           font=F_UI_B, fill=C["plum"])
            cv.create_text(cx_b, by + P(38), text=self._as_fit(sub, F_SM, tw),
                           font=F_SM, fill=C["ink_l"])
            clx = bx + bw - P(8)
            cv.create_text(clx, by + P(10), text="×", font=F_SM, fill=C["ink_l"])
            self._as_close_hit = (clx - P(10), by + P(1), clx + P(9), by + P(20))
        # ---- 角色：桃桃优先用双形态动作帧，其余主题沿用 CHIBI ----
        cv.create_oval(bxc - ch_w // 2, base_y - P(5), bxc + ch_w // 2, base_y + P(4),
                       fill=_mix(C["bg"], C["ink"], 0.10), outline="")
        if fr is not None:
            self._as_img_id = cv.create_image(bxc, base_y, image=fr, anchor="s",
                                              tags=("asimg",))
        elif chibi is not None:
            cv.create_image(bxc, base_y, image=chibi, anchor="s")
        else:
            _rr(cv, bxc - P(30), base_y - ch_h, bxc + P(30), base_y, P(16),
                fill=C["pink_l"], outline=C["pink"])
            cv.create_text(bxc, base_y - ch_h // 2, text="DL", font=F_NAME, fill=C["plum"])
        self._as_body_hit = (bxc - ch_w // 2, base_y - ch_h, bxc + ch_w // 2, base_y)
        # ---- 变身星屑 ----
        if self._as_transform_to:
            self._as_draw_fx(cv, bxc, base_y)
        # ---- 形态切换小片（左腰）——cast24 版桃桃不要这个按钮（点了会误触变身闪成功），去掉 ----
        if self.theme == "maid" and MF is not None and self._c24() is None and (
                self._form == "work" or self._as_hov):
            label = "⇄ 下班" if self._form == "work" else "⇄ 变身"
            fx0, fy0 = P(4), base_y - ch_h + P(22)
            fx1, fy1 = fx0 + P(52), fy0 + P(21)
            _rr(cv, fx0, fy0, fx1, fy1, P(10), fill=C["pink_xl"], outline=C["pink_l"])
            cv.create_text((fx0 + fx1) // 2, (fy0 + fy1) // 2, text=label,
                           font=F_SM, fill=C["plum"])
            self._as_form_hit = (fx0 - 2, fy0 - 2, fx1 + 2, fy1 + 2)
        # ---- 暂停小贴纸 ----
        if self._as["mode"] == "pause":
            px0, py0 = P(4), base_y - P(54)
            px1, py1 = px0 + P(54), py0 + P(21)
            _rr(cv, px0, py0, px1, py1, P(10), fill=C["sky"], outline=C["sky"])
            cv.create_text((px0 + px1) // 2, (py0 + py1) // 2, text="II 暂停",
                           font=F_SM_B, fill=C["navy"])
        # ---- 剪贴板接活小片（左腰下方） ----
        if self._clip_url and self._as["mode"] == "idle" and not self._as_transform_to:
            cx0, cy0 = P(4), base_y - P(44)
            cx1, cy1 = cx0 + P(56), cy0 + P(23)
            _rr(cv, cx0, cy0, cx1, cy1, P(11), fill=C["pink"], outline=C["pink_d"])
            cv.create_text((cx0 + cx1) // 2, (cy0 + cy1) // 2, text="✦ 嗅探",
                           font=F_SM_B, fill=C["card"])
            self._as_clip_hit = (cx0 - 2, cy0 - 2, cx1 + 2, cy1 + 2)
        # 环形进度（右侧）：属于状态 HUD，跟着气泡一起露收
        # （下载/嗅探中气泡必然显示 → 进度环一直在；平时一起收掉，不再占地方）
        if getattr(self, "_as_bubble_shown", False):
            self._draw_ring(cv, w - P(30), by + bh + P(30), P(21))

    # ---- 动作帧 / 状态机 / 变身 ----
    _TF_MAP = {"idle": "idle", "work": "busy", "success": "done", "fail": "fail",
               "think": "busy", "greet": "idle", "hug": "done", "hop": "done"}
    _MS_MAP = {"idle": "idle", "greet": "idle", "work": "work", "think": "work",
               "success": "success", "fail": "fail", "hug": "success", "hop": "success",
               "poke": "poke", "lift": "lift"}

    def _v2_anim(self):
        """桃桃(maid) 与 rig 角色都走 v2 统一引擎：Stage 调度 + SEQ 逐帧时序。
        桃桃用 maid_frames(MF)，hanfu/miko 用 maid_cast_frames(CF)，
        猲/跂/蜃这些 rig 渲出来的角色用 cast24_<theme> 模块。"""
        if ST is None:
            return False
        if self._is_cast():
            return True
        # cast24（rig 渲帧）角色必须放行：_as_anim_tick 整个动画主体挂在 `if ... and v2`
        # 下面，漏判的后果是桌宠一帧都不推进＝一张静图（实测 horn/jiao/qing 全中，
        # 连续截图逐像素相同）。_as_anim_tick 里本来就有 `elif self._c24() is not None`
        # 这条专给 cast24 的分支，说明原设计要动，只是这里忘了放行。
        c = self._c24()
        if c is not None and (getattr(c, "FRAMES", None) or {}):
            return True
        return (self.theme == "maid" and MF is not None and bool(getattr(MF, "SEQ", None)))

    def _v2_step(self, mod):
        k = int(getattr(mod, "K", 3)) if mod is not None else 3
        return max(1, min(k, getattr(self, "_as_step", 2)))

    def _c24(self):
        """当前主题的 muse cast24 24 帧模块（缺失/未装则 None）。"""
        if C24 is None:
            return None
        try:
            return C24.load(self.theme)
        except Exception:
            return None

    def _c24_warm(self):
        """分块预解码 cast24 全部帧：一次解完整套 24 帧。

        实测对比：把整套摊成"每 tick 1 帧"会让全量预热拖到 ~15 秒，期间 UI 线程半忙、
        动画掉帧，换角色后长时间发黏（用户反馈"卡的要死"）；旧做法是开头顿一下、
        之后全程流畅，体验更好。所以这里保持一次一整套，只把顺序按常用度排。"""
        c = self._c24()
        if c is None:
            return
        fr = getattr(c, "FRAMES", {}) or {}
        n_idle = len(fr.get("idle") or []) or 1
        jobs = [("idle", v) for v in range(n_idle)]
        _prio = ["work", "success", "fail", "lift", "poke"]
        _late = ("gaze_c", "gaze_l", "gaze_r", "gaze_u", "gaze_d", "blink")
        _extra = [k for k in fr if k != "idle" and k not in _prio and k not in _late]
        for k in _prio + sorted(_extra) + [k for k in _late if k in fr]:
            if k in fr:
                jobs.append((k, 0))
        old = getattr(self, "_as_idle_vignette", 0)

        def step():
            self._c24_warm_job = None
            if not jobs:
                return
            key, v = jobs.pop(0)
            try:
                if key == "idle":
                    self._as_idle_vignette = v
                    self._as_frames("idle")
                    self._as_idle_vignette = old   # 立刻还原，预热期间画面不乱切
                else:
                    self._as_frames(key)
            except Exception:
                self._as_idle_vignette = old
            self._c24_warm_job = self.root.after(28, step)

        if getattr(self, "_c24_warm_job", None):
            try:
                self.root.after_cancel(self._c24_warm_job)
            except Exception:
                pass
        step()

    def _as_frames(self, key):
        """取某组帧（懒解码并缓存）。cast→CF；maid(v2)→MF；否则回落旧 TF。"""
        _c = self._c24()
        if _c is not None:
            _fr = getattr(_c, "FRAMES", {}) or {}
            _k = int(getattr(_c, "K", 3))
            if key == "idle":
                _sets = _fr.get("idle") or []
                raw = _sets[getattr(self, "_as_idle_vignette", 0) % len(_sets)] if _sets else []
            else:
                _want = key if key in _fr else {
                    "hug": "success", "whug": "success", "hop": "success",
                    "greet": "success", "think": "work", "busy": "work",
                    "drag": "lift", "wdrag": "lift"}.get(key, "idle")
                # FRAMES 有两种历史格式，必须都吃（实测踩到）：
                #   旧版 cast24_hanfu/miko：只有 idle 是 [[b64,…]]，其余分镜是扁平 [b64,…]
                #   新版 rig 素材 horn/jiao/qing：所有分镜都是 [[b64,…]]
                # 无条件取 [0] 会把旧版扁平列表压成一条 base64 字符串，_decode 逐字符迭代
                # → 反应分镜 0 帧；不取 [0] 则是新版 0 帧。按首元素类型判断才两边都对。
                raw = _fr.get(_want) or []
                if raw and isinstance(raw[0], list):
                    raw = raw[0]
                if not raw:
                    raw = (_fr.get("idle") or [[]])[0]
            return self._decode(
                raw, _k,
                (self.theme, "c24", key,
                 getattr(self, "_as_idle_vignette", 0) if key == "idle" else 0))
        if self._is_cast():
            return self._cast_frames(key)
        if self.theme == "maid" and self._v2_anim():
            return self._maid_frames(key)
        if self.theme == "maid":
            return [] if MF is None else self._decode(MF.raw(key), int(getattr(MF, "K", 3)), ("maid", key))
        if TF is None:
            return []
        return self._decode(TF.raw(self.theme, self._TF_MAP.get(key, "idle")),
                            int(getattr(TF, "K", 3)), (self.theme, key))

    def _first_frame(self):
        """只解码某组的第一帧，用于量窗体尺寸（换角色时避免为拿宽高解整套 24 帧）。"""
        try:
            c = self._c24()
            if c is None:
                grp = self._as_frames("idle")
                return grp[0] if grp else None
            fr = (getattr(c, "FRAMES", {}) or {}).get("idle") or []
            datas = fr[getattr(self, "_as_idle_vignette", 0) % len(fr)] if fr else []
            step = max(1, min(int(getattr(c, "K", 3)), getattr(self, "_as_step", 2)))
            for b in (datas or [])[:1]:
                im = tk.PhotoImage(data=b)
                if step > 1:
                    im = im.subsample(step, step)
                return im
        except Exception:
            return None
        return None

    def _decode(self, datas, k, ck):
        step = max(1, min(k, getattr(self, "_as_step", 2)))
        hit = self._frames_cache.get((ck, step))
        if hit is not None:
            return hit
        out = []
        for b in (datas or []):
            try:
                im = tk.PhotoImage(data=b)
                if step > 1:
                    im = im.subsample(step, step)
                out.append(im)
            except Exception:
                pass
        self._frames_cache[(ck, step)] = out
        return out

    def _maid_frames(self, key):
        step = self._v2_step(MF)
        ck = ("maid", key, step)
        hit = self._cast_cache.get(ck)
        if hit is not None:
            return hit
        try:
            datas = MF.lod(key, step) if (hasattr(MF, "lod") and step > 1) else MF.raw(key)
        except Exception:
            datas = []
        out = []
        for b in (datas or []):
            try:
                out.append(tk.PhotoImage(data=b))
            except Exception:
                pass
        self._cast_cache[ck] = out
        return out

    def _frames_for(self, key):
        if getattr(self, "pet_live", False) and getattr(self, "_pet", None):
            return None   # 实时桌宠模式：628 张预渲染帧一律不解码（§28.1 卡顿主因）
        return self._as_frames(key)

    def _as_seq(self, key):
        if self.theme == "maid":
            return (getattr(MF, "SEQ", None) or {}).get(key) if MF is not None else None
        return self._cast_seq(key)

    def _maid_state_key(self):
        """桃桃该演哪组帧：变身/工作形态优先，其次互动反应，最后待机（对齐 v2 包）。"""
        now = time.time()
        m = self._as["mode"]
        st = self._stage
        r = st.current_react(now) if st is not None else None
        if self._as_transform_to and (now - self._as_transform_t0) < 0.26:
            return "hop"
        work = getattr(self, "_form", "daily") == "work"
        if getattr(self, "_as_dragging", False):
            return "wdrag" if work else "drag"
        if now < self._as_hug_until:
            return "whug" if work else "hug"
        if m == "done" and work:
            return "success"
        if m == "fail" and work:
            return "fail"
        if self._probing and not work:
            return "think"
        if m in ("busy", "pause"):
            return "work"
        if work:
            return "work"
        if r in ("shy", "smug", "surprise", "sleepy", "pout", "egg", "hum", "drag"):
            return r
        if r == "happy" or now < self._as_greet_until or m == "done":
            return "greet"
        return "idle"

    def _c24_state_key(self):
        """cast24 版状态机（桃桃/莉兹等所有带 cast24 的角色通用）：动作稳定、一次到位、
        绝不误播成功跳起。可用动作只有 idle/work/success/fail/poke/lift。
        优先级：拖拽 > 成功/失败(只在交差那一刻) > 正在下载挤奶油 > 点击反应 > 待机。"""
        now = time.time()
        m = self._as["mode"]
        if getattr(self, "_as_dragging", False):
            return "lift"
        # 只有真正下载成功/失败那一刻才跳起/失落，各播一遍（优先于挤奶油，否则会被工作形态盖住）
        if now < self._c24_success_until:
            return "success"
        if now < self._c24_fail_until:
            return "fail"
        # 挤奶油 = 真的在下载时才出现（不再被残留的「工作形态」长期占用）
        if self.busy or m == "busy":
            return "work"
        # 点击反应：多分镜角色(桃桃)=钉住干活分镜(返回 idle)，单分镜角色(莉兹等)=被戳(返回 poke)
        if now < self._c24_poke_until:
            return self._c24_click_state
        fr = getattr(self._c24(), "FRAMES", {}) or {}
        # 眨眼优先（很短的一次性动作，别被视线分镜盖掉）
        if now < self._c24_blink_until and "blink" in fr:
            return "blink"
        # 视线跟随：鼠标在她附近就把对应方向的分镜顶上普通待机
        if getattr(self, "_c24_follow", False):
            g = "gaze_" + getattr(self, "_c24_gaze", "c")
            if g in fr:
                return g
        return "idle"

    def _as_state_key(self):
        if self._c24() is not None:
            return self._c24_state_key()
        if self._is_cast():
            return self._cast_state_key()
        if self.theme == "maid" and self._v2_anim():
            return self._maid_state_key()
        return "idle"

    def _as_frame(self):
        frames = self._as_frames(self._as_state_key())
        if not frames:
            return None
        return frames[self._as_anim_i % len(frames)]

    def _as_tick_ms(self):
        if self._as_transform_to:
            return 65
        if self._as["mode"] == "pause":
            return 320
        if self._c24() is not None:
            _c = self._c24()
            _fr = getattr(_c, "FRAMES", {}) or {}
            _sk = self._as_state_key()
            if _sk not in _fr:          # 模块没这组帧才走别名回落（旧素材没有 happy/pout/…）
                _sk = {"hug": "success", "whug": "success", "hop": "success",
                       "greet": "success", "think": "work", "busy": "work",
                       "drag": "lift", "wdrag": "lift"}.get(_sk, "idle")
            return C24.ticks(_c, _sk)
        return {"work": 130, "greet": 105, "think": 160, "idle": 150,
                "hug": 220, "success": 260, "fail": 260, "hop": 140,
                "busy": 140, "done": 260, "happy": 260, "drag": 170,
                "egg": 300, "act": 300, "shy": 600, "sleepy": 900}.get(
                    self._as_state_key(), 150)

    def _as_anim_tick(self):
        self._as_anim_job = None
        if getattr(self, "pet_live", False) and getattr(self, "_pet", None):
            return   # 实时桌宠：Tk 帧动画循环整体停机（解码/贴图都不做）
        ms = 150
        v2 = self._v2_anim()
        try:            # 气泡到期要自己收掉：动画 tick 只换图不重绘框，这里补一次整幅重绘
            if not getattr(self, "_as_hidden", True)                     and self._as_bubble_on() != getattr(self, "_as_bubble_shown", False):
                self._draw_assistant()
        except Exception:
            pass
        try:
            if not getattr(self, "_as_hidden", True) and v2:
                key = self._as_state_key()
                frames = self._as_frames(key)
                if key != self._as_last_key:
                    self._as_last_key = key
                    if self._stage is not None:
                        self._stage.restart()
                    self._as_anim_i = 0
                if frames:
                    if self._as["mode"] == "pause" and key in ("work", "busy"):
                        idx, ms = self._as_anim_i % len(frames), 320
                    elif self._c24() is not None:
                        n = len(frames)
                        if key in ("poke", "success", "fail", "blink"):
                            # 一次性动作：从头播一遍就停在最后一帧，绝不循环乱跳
                            idx = min(self._as_anim_i + 1, n - 1)
                        else:
                            # idle / work / lift：整轮循环播放
                            idx = (self._as_anim_i + 1) % n
                            if key == "idle" and idx == 0:
                                # 点击钉住的干活分镜（扫地/浇花）期间不许轮播；
                                # 平时每转完一轮计一次，攒够两轮(≈5s)才换分镜，慢到看得清
                                if time.time() >= self._c24_poke_until:
                                    c24 = self._c24()
                                    n_sets = len((getattr(c24, "FRAMES", {}) or {}).get("idle") or []) if c24 else 0
                                    if n_sets > 1:
                                        self._c24_idle_loops += 1
                                        if self._c24_idle_loops % 2 == 0:
                                            self._as_idle_vignette = (self._as_idle_vignette + 1) % n_sets
                        ms = self._as_tick_ms()
                    elif self._stage is not None:
                        idx, ms = self._stage.frame(key, len(frames), self._as_seq(key))
                    else:
                        idx, ms = (self._as_anim_i + 1) % len(frames), self._as_tick_ms()
                    self._as_anim_i = idx
                    if self._as_img_id is not None:
                        try:
                            self.assist.itemconfig(self._as_img_id, image=frames[idx])
                        except Exception:
                            self._draw_assistant()
                    else:
                        self._draw_assistant()
                if self._as_transform_to:
                    self._as_draw_fx()
                    ms = min(ms, 65)
        except Exception:
            pass
        finally:
            if not v2:
                try:
                    ms = self._as_tick_ms()
                except Exception:
                    ms = 150
            self._as_anim_job = self.root.after(max(40, int(ms)), self._as_anim_tick)

    # ---- v2 cast（小夜等五人）：帧库 / 逐帧时序 / 表演层（全部以 _is_cast 为闸，maid 逻辑不受影响）----
    def _is_cast(self):
        return (CF is not None and ST is not None
                and self.theme in getattr(CF, "CANVAS", {}))

    def _cast_step(self):
        k = int(getattr(CF, "K", 3)) if CF is not None else 3
        return max(1, min(k, getattr(self, "_as_step", 2)))

    def _cast_frames(self, key):
        step = self._cast_step()
        ck = (self.theme, key, step)
        hit = self._cast_cache.get(ck)
        if hit is not None:
            return hit
        try:
            datas = CF.lod(self.theme, key, step) if step > 1 else CF.raw(self.theme, key)
        except Exception:
            datas = []
        out = []
        for b in (datas or []):
            try:
                out.append(tk.PhotoImage(data=b))
            except Exception:
                pass
        self._cast_cache[ck] = out
        return out

    def _cast_seq(self, key):
        return ((getattr(CF, "SEQ", None) or {}).get(self.theme) or {}).get(key)

    def _seq_len(self, key):
        sq = self._cast_seq(key) or {}
        ms = sum(int(x[1]) for x in (sq.get("enter") or [])) + \
             sum(int(x[1]) for x in (sq.get("loop") or []))
        return ms / 1000.0

    def _cast_state_key(self):
        now = time.time()
        m = self._as["mode"]
        st = self._stage
        r = st.current_react(now) if st is not None else None
        if getattr(self, "_as_dragging", False):
            return "drag"
        if self._probing or m in ("busy", "pause"):
            return r if r in ("happy", "shy", "surprise", "pout") else "busy"
        if r:
            return r
        if m == "done":
            return "done"
        if m == "fail":
            return "fail"
        if now < self._as_greet_until:
            return "happy"
        return "idle"

    def _as_react(self, state, dur, force=False):
        st = self._stage
        if st is None:
            return
        if st.do(state, max(0.3, float(dur)), force=force):
            try:
                self._draw_assistant()
            except Exception:
                pass

    def _say_soft(self, kind, n=None):
        now = time.time()
        if now - getattr(self, "_as_line_t", 0.0) < 0.8:
            return
        if self.busy and kind in ("act", "egg", "hum", "stare", "sleepy", "lonely"):
            return
        try:
            txt = self.line_for(kind, n)
        except Exception:
            txt = ""
        if not txt:
            return
        self._as_line_t = now
        self.say(txt)

    def _as_startup_greet(self):
        if getattr(self, "_as_hidden", True) or self.busy or not self._v2_anim():
            return
        if self.theme == "maid" and MF is not None:
            if getattr(self, "_form", "daily") == "daily":
                self._as_greet_until = time.time() + max(1.0, self._seq_len("greet"))
        else:
            self._as_react("happy", 1.6)
        try:
            self._draw_assistant()
        except Exception:
            pass

    def _as_activity(self, _e=None):
        st = self._stage
        if st is None or not self._v2_anim():
            return
        if st.note_input() == "wake" and not self.busy:
            self._as_react("surprise", 0.9, force=True)
            self._say_soft("wake")

    def _as_life_tick(self):
        self._as_life_job = None
        if getattr(self, "pet_live", False) and getattr(self, "_pet", None):
            return   # 实时桌宠：Tk 待机小动作循环停机（活体感由宿主物理引擎负责）
        try:
            st = self._stage
            if st is not None and self._v2_anim() and not getattr(self, "_as_hidden", True):
                now = time.time()
                quiet = (self.busy or self._probing or self._as_transform_to or self._as_dragging
                         or self._as["mode"] not in ("idle", "done", "fail")
                         or (self.theme == "maid" and getattr(self, "_form", "daily") == "work"))
                if quiet:
                    st.note_input(now)
                else:
                    hr = time.localtime().tm_hour
                    night = hr >= 23 or hr < 5
                    if self.theme == "maid":
                        acts = [("hum", 3, "hum"), ("egg", 1, "egg")]
                    else:
                        acts = list((getattr(CF, "ACTS", None) or {}).get(self.theme) or [])
                    res = st.idle_tick(now, acts, night=night)
                    if res:
                        state, kind = res
                        if state != "sleepy":
                            loops = 2 if state in ("hum", "act") else 1
                            self._as_react(state, (self._seq_len(state) or 2.0) * loops)
                        else:
                            try:
                                self._draw_assistant()
                            except Exception:
                                pass
                        self._say_soft(kind)
        except Exception:
            pass
        finally:
            self._as_life_job = self.root.after(2000, self._as_life_tick)

    def _as_draw_fx(self, cv=None, bxc=None, base_y=None):
        """变身时围着角色转的星屑 + 光环（ticker 每帧重画）。"""
        cv = cv or getattr(self, "assist", None)
        if cv is None or not self._as_transform_to:
            return
        P = self.P
        cv.delete("fx")
        if bxc is None:
            bxc = self._as_w // 2
        if base_y is None:
            base_y = self._as_h - P(7)
        t = max(0.0, min(1.0, (time.time() - self._as_transform_t0) / 0.78))
        cx, cy = bxc, base_y - P(58)
        cols = (C["pink"], C["gold"], C["sky"], C["pink_d"])
        for i in range(10):
            ang = (i / 10.0) * math.tau + t * 2.4
            rad = P(24) + P(44) * t
            x = cx + int(rad * 0.92 * math.cos(ang))
            y = cy + int(rad * 0.60 * math.sin(ang))
            r = max(1, int(P(4) * (1.0 - t * 0.55)))
            cv.create_oval(x - r, y - r, x + r, y + r,
                           fill=cols[i % len(cols)], outline="", tags="fx")
        rr = int(P(40) + P(50) * t)
        cv.create_oval(cx - rr, cy - int(rr * 0.62), cx + rr, cy + int(rr * 0.62),
                       outline=C["pink_l"], width=max(1, P(2)), tags="fx")

    def _transform_toggle(self):
        if self.theme != "maid" or MF is None:
            self.say("这身装扮还不能变身哦～等咱换上女仆装。")
            return
        self._transform("daily" if self._form == "work" else "work")

    def _transform(self, to, auto=False):
        if self.theme != "maid" or MF is None or self._as_transform_to:
            return
        if self._form == to:
            if not auto:
                self.say("咱现在就是%s形态呀 ♪" % ("工作" if to == "work" else "日常"))
            return
        if self._as_greet_until:
            self._as_greet_until = 0
        self._as_hug_until = 0
        self._as_transform_to = to
        self._as_transform_t0 = time.time()
        line = None
        if MT is not None:
            line = MT.pick("transform_in" if to == "work" else "transform_out",
                           "work" if to == "work" else "daily")
        # 自动变身（点下载触发）时不抢话，让「开工」那句台词说，避免两句连刷
        if not auto:
            self.say(line or ("工作形态启动。" if to == "work" else "变回日常。"))
        try:
            self._draw_assistant()
        except Exception:
            pass
        self.root.after(240, lambda: self._transform_swap(to))
        self.root.after(780, lambda: self._transform_end(to))
        self.log("[变身] → %s 形态" % ("工作" if to == "work" else "日常"))

    def _transform_swap(self, to):
        if self._as_transform_to != to:
            return
        self._form = to
        if to == "daily":
            if self._as["mode"] in ("done", "fail"):
                self._as["mode"] = "idle"
                self._as["frac"] = 0.0
                self._as["speed"] = self._as["eta"] = ""
            self._as_greet_until = time.time() + 0.9
        try:
            self._draw_assistant()
        except Exception:
            pass

    def _transform_end(self, to):
        if self._as_transform_to != to:
            return
        self._as_transform_to = None
        try:
            self._draw_assistant()
        except Exception:
            pass

    def _schedule_revert(self, ms=8000):
        if self._revert_job:
            try:
                self.root.after_cancel(self._revert_job)
            except Exception:
                pass
        self._revert_job = self.root.after(ms, self._auto_revert)

    def _auto_revert(self):
        self._revert_job = None
        if self.busy:
            # 又在忙（比如刚嗅探）→ 等手头这阵过去再变回来
            self._schedule_revert(5000)
            return
        if self.theme != "maid":
            return
        if self._form == "work":
            self._transform("daily", auto=True)

    # ---- 剪贴板接活 ----
    @staticmethod
    def _is_link(t):
        return bool(t) and len(t) <= 500 and t.lower().startswith(("http://", "https://"))

    def _clip_tick(self):
        self._clip_job = None
        try:
            if not self.busy and not self._as_hidden:
                try:
                    clip = (self.root.clipboard_get() or "").strip()
                except Exception:
                    clip = ""
                cur = (self.url_var.get() or "").strip()
                if self._is_link(clip) and clip != self._last_clip and clip != cur:
                    self._last_clip = clip
                    self._clip_url = clip
                    if self._form == "daily" and self._as["mode"] == "idle":
                        self.say((MT.pick("clip", "daily") if MT
                                  else "剪贴板里有新链接，要嗅一嗅吗？"))
                    try:
                        self._draw_assistant()
                    except Exception:
                        pass
                elif self._clip_url and (cur == self._clip_url or clip != self._clip_url):
                    self._clip_url = ""
                    try:
                        self._draw_assistant()
                    except Exception:
                        pass
        except Exception:
            pass
        self._clip_job = self.root.after(1400, self._clip_tick)

    def _take_clip(self):
        url = (self._clip_url or "").strip()
        if not url:
            return
        if self.busy:
            self.say("手上还有活，等这批完再嗅～")
            return
        self._clip_url = ""
        self._last_clip = url
        self.url_var.set(url)
        self.status_var.set("已从剪贴板接活～咱这就去嗅一嗅 ♪")
        self.say(MT.pick("cmd_probe", self._form) if MT else "收到，咱去嗅一嗅。")
        try:
            self._draw_assistant()
        except Exception:
            pass
        self.start_probe()

    def _draw_ring(self, cv, cx, cy, r):
        P = self.P
        frac = max(0.0, min(1.0, self._as["frac"]))
        mode = self._as["mode"]
        trans = self._as_transform_to is not None
        cv.create_oval(cx - r, cy - r, cx + r, cy + r,
                       outline=C["pink_xl"], width=P(6))
        if trans:
            # 变身中：转圈的小星星
            ang = (time.time() * 260) % 360
            cv.create_arc(cx - r, cy - r, cx + r, cy + r, start=ang,
                          extent=250, style="arc", outline=C["pink"], width=P(6))
        elif mode == "busy" and frac > 0:
            cv.create_arc(cx - r, cy - r, cx + r, cy + r, start=90,
                          extent=-frac * 360, style="arc", outline=C["pink"], width=P(6))
        elif mode == "pause":
            cv.create_oval(cx - r, cy - r, cx + r, cy + r,
                           outline=C["sky"], width=P(6))
        elif mode == "done":
            cv.create_oval(cx - r, cy - r, cx + r, cy + r,
                           outline=C["sky"], width=P(6))
        elif mode == "fail":
            cv.create_oval(cx - r, cy - r, cx + r, cy + r,
                           outline=C["gold"], width=P(6))
        if trans:
            txt, col = "✦", C["pink_d"]
        elif mode == "busy":
            txt, col = "%d%%" % int(round(frac * 100)), C["plum"]
        elif mode == "pause":
            txt, col = "II", C["sky"]
        elif mode == "done":
            txt, col = "✓", C["sky"]
        elif mode == "fail":
            txt, col = "!", C["pink_d"]
        else:
            txt, col = "♪", C["ink_l"]
        cv.create_text(cx, cy, text=txt, font=F_SM_B, fill=col)

    def _toast(self, title, sub="", kind="ok", action=None):
        P = self.P
        self._hide_toast()
        col = C["sky"] if kind == "ok" else C["gold"]
        tw, th = P(268), P(66)
        cv = tk.Canvas(self.root, width=tw, height=th, bg=C["bg"],
                       highlightthickness=0, bd=0)
        self._toast_w = cv
        _rr(cv, 2, 2, tw - 2, th - 2, P(14), fill=C["card"], outline=col)
        cv.create_oval(P(16), th // 2 - P(11), P(16) + P(22), th // 2 + P(11),
                       fill=col, outline="")
        mark = "✓" if kind == "ok" else "!"
        cv.create_text(P(27), th // 2, text=mark, font=F_UI_B,
                       fill=C["card"] if kind == "ok" else C["bg"])
        tx = P(48)
        cv.create_text(tx, P(22), anchor="w", text=title, font=F_UI_B, fill=C["plum"])
        cv.create_text(tx, P(43), anchor="w", text=sub, font=F_SM, fill=C["ink_l"])
        if action:
            label, cb = action
            bx0, by0 = tw - P(92), th // 2 - P(14)
            bx1, by1 = tw - P(12), th // 2 + P(14)
            _rr(cv, bx0, by0, bx1, by1, P(12), fill=C["pink"], outline="")
            tid = cv.create_text((bx0 + bx1) // 2, (by0 + by1) // 2,
                                 text=label, font=F_SM_B, fill=C["card"])
            cv.tag_bind(tid, "<Button-1>", lambda e: (cb(), self._hide_toast()))
        try:
            ax, ay = self._as_xy
            cv.place(relx=1.0, rely=1.0, x=ax, y=ay - P(6), anchor="se")
        except Exception:
            cv.place(relx=1.0, rely=1.0, anchor="se", x=-P(20), y=-P(64))
        tk.Misc.lift(cv)
        self._toast_job = cv.after(5400, self._hide_toast)

    def _hide_toast(self):
        try:
            if getattr(self, "_toast_job", None) and getattr(self, "_toast_w", None):
                self._toast_w.after_cancel(self._toast_job)
        except Exception:
            pass
        self._toast_job = None
        try:
            if getattr(self, "_toast_w", None):
                self._toast_w.destroy()
                self._toast_w = None
        except Exception:
            pass

    # ---- 表格右键菜单 ----
    def _tree_menu(self, e):
        iid = self.tree.identify_row(e.y)
        if iid and iid not in self.tree.selection():
            self.tree.selection_set(iid)
        self.menu.tk_popup(e.x_root, e.y_root)

    def _copy_urls(self):
        sel = [self.entries[int(i)] for i in self.tree.selection()
               if int(i) < len(self.entries)]
        direct = [e["url"] for e in sel if _entry_direct(e)]
        page = [e for e in sel if not _entry_direct(e)]
        if direct:
            self.root.clipboard_clear()
            self.root.clipboard_append("\n".join(direct))
        if page and not direct:
            self.say("这些是列表页条目，地址是网页链接、下载时才现取真流，暂时没有可复制的直链…")
            self.status_var.set(f"选中 {len(page)} 条均为「页面地址」，非直链——勾选它直接下载即可")
        elif page:
            self.say(f"已复制 {len(direct)} 条真直链；另有 {len(page)} 条是页面地址（下载时现取），未复制")
            self.status_var.set(f"复制 {len(direct)} 条直链（{len(page)} 条页面地址已跳过）")
        else:
            self.say(f"已把 {len(direct)} 条直链放进剪贴板，主人用吧 ♪")

    def _invert_sel(self):
        allids = set(self.tree.get_children())
        cur = set(self.tree.selection())
        self.tree.selection_set(list(allids - cur))

    def _bring_explorer_front(self, dir_path):
        """把刚开的资源管理器窗口拉到前台并激活。

        Windows 的"前台锁定"会让 explorer 在后台悄悄出现（用户原话：静默打开），
        所以按目录名找到那个 CabinetWClass 窗口，丢一个 ALT 解锁后置顶。"""
        try:
            import ctypes
            u = ctypes.windll.user32
            want = os.path.basename(os.path.abspath(dir_path or "")).lower()
            CB = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_long)
            cls = ctypes.create_unicode_buffer(256)
            ttl = ctypes.create_unicode_buffer(512)
            found = []

            def cb(h, _l):
                if not u.IsWindowVisible(h):
                    return True
                u.GetClassNameW(h, cls, 255)
                if cls.value != "CabinetWClass":
                    return True
                nn = u.GetWindowTextLengthW(h)
                u.GetWindowTextW(h, ttl, min(511, nn + 1))
                t = (ttl.value or "").lower()
                found.append((h, bool(want) and want in t))
                return True

            hit = None
            deadline = time.time() + 1.6
            while time.time() < deadline and not hit:
                found.clear()
                u.EnumWindows(CB(cb), 0)
                named = [f for f in found if f[1]]
                hit = named[0][0] if named else None
                if not hit:
                    time.sleep(0.12)
            if not hit and found:
                hit = found[-1][0]
            if hit:
                u.keybd_event(0x12, 0, 0, 0)      # ALT down：解除前台锁定
                u.keybd_event(0x12, 0, 2, 0)      # ALT up
                u.ShowWindow(hit, 9)              # SW_RESTORE
                u.SetForegroundWindow(hit)
                u.BringWindowToTop(hit)
        except Exception:
            LOG.debug("资源管理器置顶失败", exc_info=True)

    def _reveal_in_explorer(self, path):
        # 在资源管理器中打开所在目录并高亮该文件（explorer /select 恒返回非零，属正常）
        try:
            subprocess.Popen(f'explorer /select,"{os.path.abspath(path)}"')
            self._bring_explorer_front(os.path.dirname(os.path.abspath(path)))
            return True
        except Exception:
            try:
                os.startfile(os.path.dirname(os.path.abspath(path)))
                return True
            except Exception as e:
                messagebox.showerror(APP_TITLE, f"打不开文件位置: {e}\n{path}")
                return False

    def _resolve_row_file(self, entry, outdir):
        # 定位某条目已下载文件的绝对路径；找不到返回 None
        fp = entry.get("_filepath")
        if isinstance(fp, str) and os.path.exists(fp):
            return fp
        base = safe_name(entry.get("title") or "")
        ext = entry.get("ext") or "mp4"
        stem = os.path.splitext(base)[0]
        for cand in (base + "." + ext, stem + ".mp4", stem + ".mkv", base + ".mkv"):
            p = os.path.join(outdir, cand)
            if os.path.exists(p):
                return p
        return None

    def _open_row_file_dir(self):
        # 打开选中行已下载文件位置（单行→高亮该文件；否则打开保存文件夹）
        outdir = self.dir_var.get().strip() or DEFAULT_OUTDIR
        sel = [self.entries[int(i)] for i in self.tree.selection()
               if i.isdigit() and int(i) < len(self.entries)]
        if len(sel) == 1:
            path = self._resolve_row_file(sel[0], outdir)
            if path:
                self._reveal_in_explorer(path)
                return
            self.status_var.set("这条还没有下载到本地的文件，先打开保存文件夹～")
        try:
            os.makedirs(outdir, exist_ok=True)
            os.startfile(outdir)
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"打不开文件夹: {e}\n{outdir}")

    def _probe_cta_text(self):
        return (PERSONA_CTA.get(getattr(self, "theme", None)) or {}).get("sniff") or "✦ 嗅探分析"

    def _repaint_probe_cta(self):
        try:
            self.probe_btn.config(text=self._probe_cta_text())
        except Exception:
            pass

    def _repaint_scope(self):
        """按当前抓取范围重绘分段按钮：选中=实底强调色，未选=描边浅底"""
        lbls = getattr(self, "_scope_lbls", None)
        if not lbls:
            return
        cur = self.scope_var.get()
        for val, lb in lbls.items():
            on = (val == cur)
            try:
                lb.config(text=(lb._txt_on if on else lb._txt_off),
                          bg=(C["pink_d"] if on else C["card"]),
                          fg=(C["card"] if on else C["ink"]))
            except Exception:
                pass

    def _on_focus(self, _e=None):
        try:
            clip = self.root.clipboard_get().strip()
        except Exception:
            return
        if (not clip or clip == self._last_clip
                or not clip.lower().startswith(("http://", "https://"))
                or len(clip) > 500):
            return
        self._last_clip = clip
        cur = self.url_var.get().strip()
        if clip != cur and not self.busy:
            self.url_var.set(clip)
            self.status_var.set("主人，剪贴板里检测到链接，已帮您填好～回车或点「✦ 嗅探分析」开始 ♪")
            self.say("检测到新链接了，要去嗅一嗅吗？")

    def _on_close(self):
        # 叉掉主窗 = 收进系统托盘（下载服务/桌宠继续活着）；真正的退出走托盘菜单。
        if self.cfg.get("tray_close", True) and getattr(self, "_tray_icon", None) is not None:
            try:
                self.root.withdraw()
                if not getattr(self, "_tray_told", False):
                    self._tray_told = True
                    try:
                        self._tray_icon.notify("点托盘图标可再打开主界面", APP_TITLE)
                    except Exception:
                        pass
                    self.log("[托盘] 主界面已收起，程序仍在后台运行（桌宠、下载服务在线）")
                return
            except Exception:
                pass
        self._real_quit()

    def _real_quit(self):
        try:
            if getattr(self, "_pet", None):
                self._pet.quit()
        except Exception:
            pass
        try:
            if getattr(self, "_tray_icon", None) is not None:
                self._tray_icon.stop()
        except Exception:
            pass
        rec = [d for d in [self.dir_var.get().strip()] + list(self.cfg.get("recent_dirs") or [])
               if d and os.path.isdir(d)]
        seen, recents = set(), []
        for d in rec:
            if d not in seen:
                seen.add(d)
                recents.append(d)
        self.cfg.update({
            "outdir": self.dir_var.get().strip() or DEFAULT_OUTDIR,
            "quality": self.quality_var.get(),
            "scope": self.scope_var.get(),
            "geometry": self.root.geometry(),
            "recent_dirs": recents[:6],
            "after": self.after_var.get(),
            "dl_threads": self.threads_var.get(),
            "cookie_browser": self.cookie_var.get(),
            "proxy_mode": self.proxy_var.get(),
            "proxy_manual": self.cfg.get("proxy_manual") or "",
        })
        save_config(self.cfg)
        try:
            self._qmem_flush(force=True)      # 记忆系统：退出前把队列写死
        except Exception:
            pass
        try:
            self._rpc_stop()
        except Exception:
            pass
        try:
            self._bt_shutdown()
        except Exception:
            pass
        try:
            if getattr(self, "_slow_running", False):
                self._slow_cancel.set()
                th = getattr(self, "_slow_thread", None)
                if th is not None:
                    th.join(timeout=3)
        except Exception:
            pass
        self.root.destroy()

    # ---------------- 系统托盘（pystray；缺库则静默禁用，叉窗退回旧行为） ----------------
    def _tray_setup(self):
        if not self.cfg.get("tray_close", True):
            return
        try:
            import base64 as _b64
            import io
            import pystray
            from PIL import Image as PILImage
            img = None
            for _p in (os.path.join(getattr(sys, "_MEIPASS", BASE_DIR), "assets", "SniffDL_tray.png"),
                       os.path.join(BASE_DIR, "assets", "SniffDL_tray.png")):
                try:
                    if os.path.isfile(_p):
                        img = PILImage.open(_p).convert("RGBA").resize((64, 64)); break
                except Exception:
                    pass
            if img is None:
                raw = _b64.b64decode((THEMES.get(self.theme) or {}).get("AVATAR") or "")
                img = PILImage.open(io.BytesIO(raw)).convert("RGBA").resize((64, 64))

            def _show(_i, _it):
                self.root.after(0, self._tray_open_main)

            def _pettoggle(_i, _it):
                self.root.after(0, self._pet_toggle)

            def _quit(_i, _it):
                self.root.after(0, self._real_quit)

            menu = pystray.Menu(
                pystray.MenuItem("打开主界面", _show, default=True),
                pystray.MenuItem("显示 / 隐藏桌宠", _pettoggle),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出 SniffDL", _quit))
            self._tray_icon = pystray.Icon("SniffDL", img, "SniffDL（桌宠与下载服务在线）", menu)
            threading.Thread(target=self._tray_icon.run, daemon=True).start()
            self.log("[托盘] 已常驻通知区：叉掉主窗不会退出程序")
        except Exception:
            self._tray_icon = None

    def _tray_open_main(self):
        # Web UI 模式：主界面是那个 HTML webview 窗，Tk 主窗只当后台引擎（隐藏）。
        if getattr(self, "_web_mode", False):
            self._focus_or_relaunch_webview()
            return
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except Exception:
            pass

    # ---------------- Web UI（路线B 前端）：webview 当主窗，Tk 退居后台引擎 ----------------
    def launch_webview(self):
        """拉起 ui_web.py（pywebview）作为可见主窗，代理到本机 RPC。Tk 主窗保持隐藏。"""
        me = BASE_DIR
        try:
            if getattr(sys, "frozen", False):
                cmd = [sys.executable, "--webview"]          # 打包形态：同 exe 子命令内嵌 webview
            else:
                pyw = sys.executable
                if pyw.lower().endswith("python.exe"):
                    w = pyw[:-len("python.exe")] + "pythonw.exe"
                    if os.path.exists(w):
                        pyw = w
                cmd = [pyw, os.path.join(me, "ui_web.py")]
            p = subprocess.Popen(cmd, cwd=me, creationflags=0x08000000,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            self._webview_proc = p
            self.log("[WebUI] 已打开 webview 主界面（Tk 引擎常驻后台/托盘）")
        except Exception as e:
            self.log("[WebUI] 启动失败，回退显示 Tk 主窗：%s" % e)
            try:
                self.root.deiconify(); self.root.lift(); self.root.focus_force()
            except Exception:
                pass

    def _focus_or_relaunch_webview(self):
        p = getattr(self, "_webview_proc", None)
        if p is not None and p.poll() is None:
            try:   # 已开着：把它带到前台
                import ctypes
                hwnd = ctypes.windll.user32.GetForegroundWindow()
                # webview 窗标题 "SniffDL"，用 EnumWindows 找并前置
                user32 = ctypes.windll.user32
                found = []
                @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
                def _cb(h, _l):
                    if user32.IsWindowVisible(h):
                        buf = ctypes.create_unicode_buffer(128)
                        user32.GetWindowTextW(h, buf, 128)
                        if buf.value == "SniffDL":
                            found.append(h)
                    return True
                user32.EnumWindows(_cb, 0)
                for h in found:
                    if user32.IsIconic(h):
                        user32.ShowWindow(h, 9)
                    user32.BringWindowToTop(h); user32.SetForegroundWindow(h)
                if found:
                    return
            except Exception:
                pass
        self.launch_webview()

    def _pet_toggle(self):
        if getattr(self, "pet_live", False) and getattr(self, "_pet", None):
            on = not getattr(self, "_pet_visible", True)
            self._pet_visible = on
            try:
                self._pet.visible(on)
            except Exception:
                pass
            return on
        try:
            self._toggle_assistant()
        except Exception:
            pass
        return None

    def open_log(self):
        try:
            os.startfile(LOG_FILE)
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"打开日志失败: {e}\n{LOG_FILE}")

    def open_outdir(self, reveal=None):
        """打开保存目录；reveal 给了文件名就在窗口里高亮定位它，并把资源管理器
        拉到前台（用户明确要"跳到脸上"，不要静默打开）。"""
        d = self.dir_var.get().strip() or DEFAULT_OUTDIR
        try:
            os.makedirs(d, exist_ok=True)
            fp = None
            if reveal:
                fp = (reveal if os.path.isabs(str(reveal))
                      else os.path.join(d, str(reveal)))
                if not os.path.exists(fp):
                    fp = None
            if fp:
                subprocess.Popen(f'explorer /select,"{os.path.abspath(fp)}"')
            else:
                subprocess.Popen(["explorer", os.path.abspath(d)])
            self._bring_explorer_front(d)
        except Exception:
            try:
                os.startfile(d)
            except Exception as e:
                messagebox.showerror(APP_TITLE, f"打开文件夹失败: {e}\n{d}")

    def diagnose(self):
        self.log("点击「自检」，正在检查运行环境…")
        threading.Thread(target=run_diagnose, args=(self.log,), daemon=True).start()

    def log(self, msg):
        LOG.info(msg)
        self.q.put(("log", msg))

    def _greet_line(self):
        ch, role, _ = self.char_name()
        return "〔%s · %s〕%s" % (ch, role, self.line_for("greet"))

    def _set_log_greet(self, text):
        """日志开头那句角色开场白：整本只保留一行，换角色就地替换，不堆叠。"""
        try:
            t = self.log_text
            t.config(state="normal")
            rng = t.tag_ranges("greet")
            if rng:
                t.delete(rng[0], rng[1])
            t.tag_config("greet", foreground=_LOG_TEAL)
            t.insert("1.0", text + "\n", ("greet",))
            t.config(state="disabled")
        except Exception:
            pass

    def pick_dir(self):
        d = filedialog.askdirectory(initialdir=self.dir_var.get() or DEFAULT_OUTDIR)
        if d:
            self.dir_var.set(d)

    def set_buttons(self, on):
        # on=True 空闲；on=False 下载队列运行中。
        # 关键：下载/选中按钮全程可点（支持随时追加任务，不锁界面）；
        # 只有「嗅探」会重建列表，下载中临时禁用，避免列表与在跑任务错位。
        self.probe_btn.config(state="normal" if on else "disabled")
        self.dl_sel.config(state="normal")
        self.dl_all.config(state="normal")
        for b in (self.pause_btn, self.stop_btn):
            b.config(state="normal" if not on else "disabled")
        if on:
            self.pause_evt.set()
            self.pause_btn.config(text="暂停队列")

    def toggle_pause(self):
        if not self.busy:
            return
        if self._probing:
            # 嗅探是一整轮浏览器/接口调用，中途停不下来，如实告诉主人
            self.say("嗅探这一轮停不下来哦，等咱翻完再歇～")
            return
        if self.pause_evt.is_set():
            self.pause_evt.clear()
            self.pause_btn.config(text="▶ 继续下载")
            self.status_var.set("已暂停：正在搬运的文件会在下个分片间隙停下，进度保留可续传")
            self.say(MT.pick("pause", self._form) if MT else self.line_for("pause"))
            self.log("[队列] 主人按下暂停，正在下的会停下并保住进度")
            try:
                self._as["mode"] = "pause"
                self._draw_assistant()
            except Exception:
                pass
        else:
            self.pause_evt.set()
            self.pause_btn.config(text="暂停队列")
            self.status_var.set("队列继续～从断点接着下 ♪")
            self.log("[队列] 恢复下载（断点续传）")
            self.say(MT.pick("resume", self._form) if MT else "继续～")
            try:
                if self._as["mode"] == "pause":
                    self._as["mode"] = "busy"
                self._draw_assistant()
            except Exception:
                pass

    def gate(self):
        """下载线程开工一个文件前先过闸：暂停时就在这儿等，不丢任何已有进度"""
        while not self.pause_evt.is_set():
            time.sleep(0.3)

    def _entry_files(self, e, outdir):
        """某条目在本地可能存在的成品文件（含历史记录的命名 + 隔离的 .snifftmp 分片目录）。"""
        out = []
        title = e.get("title") or ""
        base = safe_name(title)
        stem = os.path.splitext(base)[0]
        ext = (e.get("ext") or "mp4").lstrip(".")
        for cand in (base + "." + ext, stem + ".mp4", stem + ".mkv",
                     stem + ".webm", stem + ".m4a", stem + ".ts"):
            p = os.path.join(outdir, cand)
            if os.path.exists(p):
                out.append(p)
        try:
            h = load_history().get(e.get("url") or title)
            if h:
                p = os.path.join(outdir, h)
                if os.path.exists(p) and p not in out:
                    out.append(p)
        except Exception:
            pass
        key = hashlib.md5(
            (e.get("url") or title or "v").encode("utf-8", "ignore")).hexdigest()[:12]
        td = os.path.join(snifftmp_root(outdir), ".snifftmp", key)
        if os.path.isdir(td):
            out.append(td)
        return out

    def terminate_sel(self):
        """终止选中（未选中＝终止整批）：正在下的立即中断，排队的彻底不再下载。"""
        if not self.busy:
            self.say("现在没有正在下载的任务呢～")
            return
        sel = [int(i) for i in self.tree.selection() if int(i) < len(self.entries)]
        if not sel:
            sel = list(range(len(self.entries)))
            self.log("[终止] 未选中任何行 → 终止整批（排队全丢、进行中立即中断）")
        selset = set(sel)
        # 还没开跑的排队项：直接从待办队列摘掉并释放行占用
        with self._job_lock:
            before = len(self._pending)
            self._pending = [a for a in self._pending if a[0] not in selset]
            dropped = before - len(self._pending)
            for r in selset:
                if r not in self._started_rows:
                    self._queued_rows.discard(r)
        added = 0
        for r in sel:
            if r not in self._cancel_rows:
                self._cancel_rows.add(r)
                added += 1
            self.set_status(r, "已终止 ✋")
        self.status_var.set(
            f"已终止 {added} 条（{dropped} 条排队项已移出队列）："
            "正在下的会在下个分片停下")
        self.say(f"好，帮您按住 {added} 条～下次勾选它们还能从头再来")
        self.log(f"[终止] 标记 {added} 行为终止，移出排队 {dropped} 条")

    def delete_sel(self, with_files=False):
        if self.busy:
            self.say("下载中先别整理列表哦，等这批结束再删～")
            return
        sel = sorted({int(i) for i in self.tree.selection()
                      if int(i) < len(self.entries)})
        if not sel:
            messagebox.showwarning(APP_TITLE, "请先选中要删除的行（Ctrl 多选 / Shift 连选，或右键「全选」）")
            return
        selset = set(sel)
        removed = [self.entries[r] for r in sel]
        outdir = self.dir_var.get().strip() or DEFAULT_OUTDIR
        # 需要连带删除的本地成品文件（此处只算路径，真正删除放到后台线程做）
        paths = []
        if with_files:
            for e in removed:
                paths += self._entry_files(e, outdir)
        # 先即时更新内存列表 + 表格：无论删不删文件，列表都立刻变，绝不卡
        new_entries, new_status, ni = [], {}, 0
        for oldi in range(len(self.entries)):
            if oldi in selset:
                continue
            new_entries.append(self.entries[oldi])
            if oldi in self.row_status:
                new_status[ni] = self.row_status[oldi]
            ni += 1
        self.entries = new_entries
        self.row_status = new_status
        self.refresh_table()
        # 关键修复：不管删不删本地文件，都把移除写回下载记忆(downloaded.json)，
        # 否则下次开机会被「载入下载记忆」重新塞回来。
        try:
            hist = load_history()
            for e in removed:
                for k in (e.get("url"), e.get("title")):
                    if k and k in hist:
                        hist.pop(k, None)
            save_history(hist)
        except Exception:
            pass
        if not with_files:
            tip = (f"从列表移除了 {len(removed)} 行（本地文件没动；"
                   f"记忆已同步删除，重开不再出现）")
            self.status_var.set(tip)
            self.say(tip + " ♪")
            self.log("[删除] " + tip)
            return
        # 连带删文件：后台线程执行，UI 不阻塞
        self.status_var.set(
            f"已移除 {len(removed)} 行，正在后台把 {len(paths)} 个文件送进回收站…")
        self.say(f"移除 {len(removed)} 行，文件后台进回收站（列表已即时更新，不卡）♪")
        self.log(f"[删除] 移除 {len(removed)} 行；{len(paths)} 个本地文件后台送回收站")
        if not paths:
            self.status_var.set(f"已移除 {len(removed)} 行（这些条目本地没有成品文件）")
            return

        def _work():
            moved, failed = send_to_recycle(paths)
            tip = f"回收站完成：{len(moved)} 个文件已移入"
            if failed:
                tip += f"；{len(failed)} 个没删动（可能被占用），可手动清理"
            tip += "；回收站可原样找回"

            def _done():
                self.status_var.set(tip)
                self.log("[删除] " + tip)
            try:
                self.root.after(0, _done)
            except Exception:
                pass
        import threading
        threading.Thread(target=_work, daemon=True).start()

    def _select_all(self):
        ids = self.tree.get_children()
        if ids:
            self.tree.selection_set(ids)
            self.status_var.set(f"已全选 {len(ids)} 行")

    def load_memory(self):
        hist = load_history()
        if not hist:
            self.say("还没有下载记忆呢～下成功的会自动记在这里")
            return
        outdir = self.dir_var.get().strip() or DEFAULT_OUTDIR
        rows = []
        for u, fn in hist.items():
            fn = fn or ""
            p = os.path.join(outdir, fn) if fn else ""
            exists = bool(p) and os.path.exists(p)
            ext = os.path.splitext(fn)[1].lstrip(".") or "mp4"
            rows.append({
                "url": u, "how": "历史记录",
                "title": safe_name(os.path.splitext(fn)[0] or "video"),
                "ext": ext, "res": "-", "duration": None,
                "size": os.path.getsize(p) if exists else None,
                "_page": "",
            })
        self.entries = rows
        self.row_status = {i: ("已下载 ♪" if rows[i]["size"] else "记忆·文件已不在")
                           for i in range(len(rows))}
        self.refresh_table()
        self.status_var.set(f"已载入 {len(rows)} 条下载记忆（本机保存清单）")
        self.say(f"找到 {len(rows)} 条以前下过的，帮主人翻出来啦 ♪")

    def finish_action(self):
        act = (self.after_var.get() or "无").strip()
        if act == "无":
            return
        if act == "退出程序":
            self.log("[完成动作] 按主人设定退出程序")
            self.root.after(600, self._on_close)
        elif act == "关机":
            self.log("[完成动作] 60 秒后关机；反悔就在命令行执行 shutdown /a")
            self.status_var.set("整批完成，60 秒后自动关机（命令行 shutdown /a 可取消）")
            try:
                subprocess.Popen(["shutdown", "/s", "/t", "60",
                                  "/c", "SniffDL 下载完成，60 秒后关机（shutdown /a 取消）"])
            except Exception as e:
                self.log(f"[完成动作] 关机调用失败: {e}")

    @staticmethod
    def parse_urls(raw):
        """网址框里可以一次粘多个（换行/空格分隔），批量嗅探合并成一张表"""
        toks = re.split(r"[\s,，;；]+", raw or "")
        out = []
        for t in toks:
            t = t.strip()
            if not t:
                continue
            if t.startswith("http://") or t.startswith("https://"):
                if t not in out:
                    out.append(t)
        return out

    def start_probe(self):
        if self.busy:
            return
        raw = self.url_var.get().strip()
        urls = self.parse_urls(raw)
        if not urls:
            messagebox.showwarning(APP_TITLE, "主人还没填网址呀～(＝。＝)")
            return
        if len(urls) == 1 and not re.match(r"^https?://", raw):
            urls = ["https://" + raw]
        self.url_var.set(urls[0])
        self._queue_urls = urls[1:]
        self._batch_acc = [] if self._queue_urls else None
        if self._queue_urls:
            self.log(f"[批量] 本轮共 {len(urls)} 个页面，逐个嗅探后合并结果")
        url = self.page_url = urls[0]
        self.busy = True
        self._probing = True
        self._c24_success_until = self._c24_fail_until = 0.0   # 新一轮开工，清掉上轮的成功/失败跳起窗
        try:
            self._draw_assistant()
        except Exception:
            pass
        self.set_buttons(False)
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.entries = []
        self.log(f"========== 开始嗅探 {url} ==========")
        self._probe_t0 = time.time()
        _nm, _role, _ = self.char_name()
        self.status_var.set(f"{_role or _nm or '嗅探'}出动… 已用时 0 秒")
        self.say(self.line_for("busy"))
        self.set_mood("sniff")
        self._tick_probe()
        self.bar.config(mode="indeterminate")
        self.bar.start(12)
        threading.Thread(target=self._probe_worker, args=(url,), daemon=True).start()

    def _tick_probe(self):
        if not self.busy:
            return
        el = int(time.time() - self._probe_t0)
        tstr = f"{el // 60}分{el % 60}秒" if el >= 60 else f"{el}秒"
        self.status_var.set(f"嗅探中… 已用时 {tstr}，请主人稍候 ♪")
        self.root.after(1000, self._tick_probe)

    def _emit_probe(self, entries, url):
        """结果统一出口：标注来源页（批量嗅探时 Referer 要按各自页面走）"""
        for e in entries:
            e.setdefault("_page", url)
        self.q.put(("probe_done", entries))

    def _probe_worker(self, url):
        """GUI 入口：跑完整嗅探流水线并把结果喂进主界面结果树。"""
        self._sniff_pipeline(url, scope=None, emit=True)

    def _collapse_stream_segments(self, entries):
        """直播/HLS 页会把每个 .ts/.m4s 分片也当资源抓到 → 用户看到几十条碎片、
        下载时逐条落盘撒满目录（快手直播裸 .ts 泄漏根因）。这里从源头归并：
        只要抓到 m3u8/mpd 整条流清单，就丢弃属于分片的 .ts/.m4s 条目（清单会自
        己下整条流并合并）；若没抓到清单但发现同一前缀的一堆序号分片，也只各留
        一条，避免刷屏与散落。"""
        if not entries:
            return entries

        def _clean(u):
            return (u or "").split("?")[0].split("#")[0].lower()

        def _ext(u):
            c = _clean(u)
            return c.rsplit(".", 1)[-1] if "." in c else ""

        manifests = [e for e in entries
                     if _ext(e.get("url", "")) in ("m3u8", "mpd")]
        FRAG_EXT = ("ts", "m4s", "frag", "m4v", "aac", "webvtt", "vtt")

        def _is_frag(u):
            if _ext(u) in FRAG_EXT:
                return True
            low = (u or "").lower()
            return ".ts?" in low or ".m4s?" in low

        kept, dropped = [], 0
        frag_group_seen = set()
        for e in entries:
            u = e.get("url", "")
            if _is_frag(u):
                if manifests:
                    dropped += 1
                    continue
                # 无清单：把任意连续数字（序号/时间戳）归一为 # 后同组只留第一条
                stem = re.sub(r"\d+", "#", _clean(u))
                if stem in frag_group_seen:
                    dropped += 1
                    continue
                frag_group_seen.add(stem)
            kept.append(e)
        if dropped:
            self.log(f"[嗅探] 归并直播分片：丢弃 {dropped} 条 .ts/.m4s 碎片，只保留整条流")
        return kept

    def _sniff_pipeline(self, url, scope=None, emit=True):
        """猫爪完整嗅探流水线（GUI 与浏览器浮标 RPC 共用同一套逻辑）。
        scope=None 时沿用界面「仅本页/全部」下拉；emit=True 时把结果喂主界面树。
        无论是否 emit，最终都 return 合并后的 entries 列表，供 RPC 侧取用。"""
        merged, seen = [], set()

        # 0) 3秒预检: 先定引擎。AList 等站点直接短路，
        #    既跳过注定无效的常规嗅探（省1-2分钟），也省掉 yt-dlp 白等的 15~20 秒
        pre_html = precheck_html(url, self.log)
        route = route_by_precheck(pre_html, url)
        if scope is None:
            scope = self.scope_var.get()
        if scope != "all" and route == "list":
            # 仅本页模式：不爬列表/详情链接，只认这一页自身的视频（本页无内嵌视频则为空，符合预期）
            self.log("[范围] 仅本页视频：跳过列表页爬取")
            route = "normal"

        if route != "alist":
            # -1) yt-dlp 快速通道: YouTube/B站/Twitter 等大站秒级命中, 直接短路
            try:
                fast = ydl_probe(url, self.log)
            except Exception as e:
                self.log(f"[站点解析] 异常: {str(e)[:150]}")
                fast = []
            if fast:
                merged = list(fast)
                self.log(f"[站点解析] yt-dlp 命中 {len(merged)} 条, 跳过其他引擎")
                self.log(f"[探测] 读取 {len(merged)} 条媒体的基本信息…")
                merged = probe_all(merged, url, self.log)
                if emit:
                    self._emit_probe(merged, url)
                return merged
            if blocked_site_hint(url, self.log):
                if emit:
                    self._emit_probe([], url)
                return []

        if route == "alist":
            self.log("[预检] 检测到 AList V3 站点（Vue SPA 空壳+后端 REST API），走 AList 引擎…")
            try:
                for e in sniff_alist_site(url, self.log):
                    if e["url"] not in seen:
                        merged.append(e)
                        seen.add(e["url"])
            except Exception as e:
                self.log(f"[AList 引擎] 失败: {str(e)[:150]}")

        if route == "files":
            self.log(f"[预检] 检测到文件列表页（网盘式），直接收取全部文件直链…")
            for e in collect_file_entries(pre_html, url, self.log):
                if e["url"] not in seen:
                    merged.append(e)
                    seen.add(e["url"])
            if not merged:
                route = "normal"
        if route == "encrypted":
            self.log("[预检] 检测到加密播放器站，直接启用加密引擎（跳过常规嗅探）…")
            try:
                for e in sniff_encrypted_player(url, self.log):
                    if e["url"] not in seen:
                        merged.append(e)
                        seen.add(e["url"])
            except Exception as e:
                self.log(f"[加密引擎] 失败: {str(e)[:150]}")

        elif route == "list":
            self.log("[预检] 检测到视频列表页，直接进入详情页解析（跳过常规嗅探）…")
            try:
                for e in sniff_list_page(url, self.log):
                    if e["url"] not in seen:
                        merged.append(e)
                        seen.add(e["url"])
            except Exception as e:
                self.log(f"[列表页模式] 失败: {str(e)[:150]}")
        elif route == "mse":
            self.log("[预检] 疑似 MSE/blob 站（页面有 <video> 但无直链），启用 MSE 捕获引擎…")
            try:
                for e in sniff_mse_capture(url, self.log, meta_only=True, wait_s=45):
                    if e["url"] not in seen:
                        merged.append(e)
                        seen.add(e["url"])
            except Exception as e:
                self.log(f"[MSE 引擎] 失败: {str(e)[:150]}")
            if not merged:
                # meta 模式没抓到 SourceBuffer, 回退常规嗅探
                self.log("[MSE 引擎] 未捕获到分片, 回退常规浏览器嗅探…")
                route = "normal"
        else:
            sniffed = []
            if HAS_PW:
                try:
                    if scope == "all":
                        sniffed = run_deep_sniff(url, self.log)
                    else:
                        sniffed = run_sniff(url, self.log)
                except Exception as e:
                    self.log(f"[错误] 浏览器嗅探失败: {str(e)[:250]}")
            else:
                self.log("[错误] 未安装 playwright，跳过浏览器嗅探（pip install playwright && python -m playwright install chromium）")
            for e in sniffed:
                if e["url"] not in seen:
                    merged.append({"url": e["url"], "how": e["how"], "title": None})
            if not merged:
                self.log("[加密引擎] 常规嗅探无结果，尝试加密播放器镜像解析…")
                try:
                    for e in sniff_encrypted_player(url, self.log):
                        if e["url"] not in seen:
                            merged.append(e)
                except Exception as e:
                    self.log(f"[加密引擎] 失败: {str(e)[:150]}")
            if not merged and scope == "all":
                self.log("[列表页模式] 疑似视频列表页，进入各详情页解析…")
                try:
                    for e in sniff_list_page(url, self.log):
                        if e["url"] not in seen:
                            merged.append(e)
                except Exception as e:
                    self.log(f"[列表页模式] 失败: {str(e)[:150]}")
            if not merged and pre_html and is_mse_candidate(pre_html, url):
                # 常规/加密/列表全空手 + 页面有 video → MSE 兜底
                self.log("[MSE 引擎] 其他引擎空手, 疑似 blob 流站, 启用 MSE 捕获…")
                try:
                    for e in sniff_mse_capture(url, self.log, meta_only=True, wait_s=45):
                        if e["url"] not in seen:
                            merged.append(e)
                            seen.add(e["url"])
                except Exception as e:
                    self.log(f"[MSE 引擎] 失败: {str(e)[:150]}")
        merged = self._collapse_stream_segments(merged)
        if merged:
            self.log(f"[探测] 读取 {len(merged)} 条媒体的基本信息…")
            merged = probe_all(merged, url, self.log)
        else:
            self.log("[结果] 未发现任何视频。可能原因: 页面需登录/视频在二级页面/纯懒加载。试试直接粘贴视频播放页地址。")
        if emit:
            self._emit_probe(merged, url)
        return merged

    def toggle_slow_fetch(self):
        """慢速取片：第一次点开始；运行中再点＝取消。"""
        if self._slow_running:
            self._slow_cancel.set()
            self.slow_btn.config(text="正在取消…")
            self.status_var.set("慢速取片：正在收尾，当前分片返回后停止…")
            self.log("[慢速取片] 已请求取消，当前分片返回后停止")
            return
        self.start_slow_fetch(False)

    def start_slow_fetch(self, selected=False):
        """独立功能入口：手动慢速取片。不参与常规嗅探/下载链路。"""
        if self._slow_running:
            messagebox.showinfo(APP_TITLE,
                                "已经在慢速取片了，等它跑完，或点「慢速取片」取消")
            return
        jobs = []
        if selected:
            sel = self.tree.selection()
            if not sel:
                messagebox.showwarning(APP_TITLE, "先选中要取片的行，再点慢速取片")
                return
            for iid in sel:
                try:
                    i = int(iid)
                except Exception:
                    continue
                if i >= len(self.entries):
                    continue
                e = self.entries[i]
                u = (e.get("source_page") or e.get("url") or "").strip()
                if u.startswith("http"):
                    jobs.append((u, e.get("title") or "", i))
            if not jobs:
                messagebox.showwarning(APP_TITLE, "选中行里没有可用的页面地址")
                return
        else:
            raw = self.url_var.get().strip()
            if not raw:
                messagebox.showwarning(APP_TITLE,
                                       "先在网址框里填详情页地址，再点「慢速取片」")
                return
            u = raw if re.match(r"^https?://", raw) else "https://" + raw
            jobs.append((u, "", None))
        outdir = self.dir_var.get().strip() or DEFAULT_OUTDIR
        try:
            os.makedirs(outdir, exist_ok=True)
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"保存目录无效: {e}")
            return
        qmap = {"最佳": None, "2160": "2160p", "1080": "1080p",
                "720": "720p", "480": "480p"}
        want = qmap.get(self.quality_var.get())
        tip = ("慢速取片会打开一个浏览器窗口，让站点自己把加密视频一片片解开。\n"
               "速度慢（高画质可能要十几分钟到半小时），期间请别关那个窗口。\n"
               "界面不会锁，可以继续做别的事；想停就再点一次「慢速取片」。\n\n"
               f"本次 {len(jobs)} 个页面，继续吗？")
        if not messagebox.askyesno(APP_TITLE, tip):
            return
        with self._slow_lock:
            if self._slow_running:
                messagebox.showinfo(APP_TITLE, "已经在慢速取片了")
                return
            self._slow_running = True
        self._slow_cancel.clear()
        self._slow_t0 = time.time()
        self.slow_btn.config(text="取消慢速取片")
        self.root.after(3000, self._slow_tick)
        self.status_var.set(f"慢速取片：{len(jobs)} 个页面，正在打开浏览器…（较慢，可随时取消）")
        self.say("慢速取片开始，会慢一点，主人可以先去做别的～")
        self.log(f"[慢速取片] 开始 {len(jobs)} 个页面 → {outdir}"
                 + (f"（画质 {want}）" if want else "（最佳画质）"))
        for _, _, row in jobs:
            if row is not None:
                self.set_status(row, "慢速取片中…")
        self._slow_thread = threading.Thread(target=self._slow_worker,
                                             args=(jobs, outdir, want),
                                             daemon=True)
        self._slow_thread.start()

    def _slow_worker(self, jobs, outdir, want_label):
        """后台串行跑慢速取片：一次只开一个浏览器，结果通过队列回主线程。"""
        ok = fail = 0
        cancelled = False
        hist = load_history()
        try:
            for idx, (url, title, row) in enumerate(jobs, 1):
                if self._slow_cancel.is_set():
                    cancelled = True
                    break
                self.log(f"[慢速取片] ({idx}/{len(jobs)}) {url[:110]}")
                try:
                    fp = slow_fetch_page(url, self.log, title_prefix=title or None,
                                         save_dir=outdir, want_label=want_label,
                                         cancel_evt=self._slow_cancel)
                except Exception as e:
                    fp = None
                    self.log(f"[慢速取片] 异常: {str(e)[:150]}")
                if fp and os.path.exists(fp):
                    ok += 1
                    sz = os.path.getsize(fp)
                    self.log(f"[慢速取片] ✓ {os.path.basename(fp)}（{fmt_size(sz)}）")
                    hist[url] = os.path.basename(fp)
                    if row is not None:
                        self.q.put(("row_status", (row, "✓ 慢速取片完成")))
                else:
                    if self._slow_cancel.is_set():
                        cancelled = True
                        if row is not None:
                            self.q.put(("row_status", (row, "已取消")))
                        break
                    fail += 1
                    if row is not None:
                        self.q.put(("row_status", (row, "✗ 慢速取片失败")))
                    self.log("[慢速取片] ✗ 未取到成品")
        finally:
            try:
                save_history(hist)
            except Exception:
                pass
            self.q.put(("slow_done", (ok, fail, outdir, cancelled)))

    def _slow_tick(self):
        if not self._slow_running:
            return
        el = int(time.time() - getattr(self, "_slow_t0", time.time()))
        m, s = divmod(el, 60)
        t = f"{m}分{s}秒" if m else f"{s}秒"
        self.status_var.set(f"慢速取片进行中…已用 {t}"
                            "（本来就慢，可点「取消慢速取片」）")
        self.root.after(3000, self._slow_tick)

    def _on_slow_done(self, data):
        ok, fail, outdir, cancelled = data
        with self._slow_lock:
            self._slow_running = False
        self._slow_cancel.clear()
        self.slow_btn.config(text="慢速取片")
        if cancelled:
            self.status_var.set(f"慢速取片已取消：成功 {ok}，失败 {fail}")
            self._toast("慢速取片已取消", "已取到的文件会留在保存目录", kind="warn",
                        action=("打开文件夹", self.open_outdir))
        elif ok:
            self.status_var.set(f"慢速取片完成：成功 {ok}，失败 {fail}。保存于 {outdir}")
            self._toast("慢速取片完成",
                        f"成功 {ok} 个" + (f"，失败 {fail}" if fail else ""),
                        kind="ok" if not fail else "warn",
                        action=("打开文件夹", self.open_outdir))
        else:
            self.status_var.set("慢速取片失败：页面可能没有可解密的视频，或链接已失效")
            self._toast("慢速取片失败", "可看日志里的 [慢速取片] 记录", kind="warn")
        self.log(f"[慢速取片] 收尾：成功 {ok}，失败 {fail}"
                 + ("，已取消" if cancelled else ""))

    def start_download(self, selected_only):
        """把选中（或全部可见）条目追加进下载队列。可随时多次追加，不锁界面。"""
        if not self.entries:
            messagebox.showwarning(APP_TITLE, "主人，还没有嗅探出列表呢～先点「✦ 嗅探分析」吧")
            return
        if selected_only:
            sel = self.tree.selection()
            if not sel:
                messagebox.showwarning(APP_TITLE,
                                       "请先在列表中选中要搬的条目（Ctrl/Shift 多选，或双击某行只下它）")
                return
            rows = [int(i) for i in sel if int(i) < len(self.entries)]
        else:
            rows = list(self.visible_rows())
            if len(rows) < len(self.entries):
                self.log(f"[下载] 筛选生效：只搬匹配的 {len(rows)} 条（共 {len(self.entries)} 条）")
        outdir = self.dir_var.get().strip() or DEFAULT_OUTDIR
        try:
            os.makedirs(outdir, exist_ok=True)
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"保存目录无效: {e}")
            return
        quality = self.quality_var.get()
        # 只入队「当前没在下、也没在排队」的行；重新入队清除旧的终止标记
        new_rows, dup = self._enqueue_rows(rows, outdir, quality)
        if not new_rows:
            self.say("这些已经在队列里或正在下啦～不用重复添加")
            return
        for r in new_rows:
            self.set_status(r, "排队中")
        self.log(f"[下载] 追加 {len(new_rows)} 个任务到队列"
                 + (f"（{dup} 个已在队列，忽略）" if dup else "")
                 + f" → {outdir}")
        self._start_worker(outdir, quality)

    def _sync_cookie(self):
        """把「Cookie 来源」下拉同步到全局 cookie 解析，并落盘。"""
        v = (self.cookie_var.get() or "自动").strip()
        set_cookie_setting(v)
        try:
            self.cfg["cookie_browser"] = v
        except Exception:
            pass
        try:
            mode = _cookie_mode()
            if mode == "auto":
                _det = "、".join(_detect_installed_browsers()) or "无"
                _fl = "＋已登录Cookie文件" if _has_app_cookie_file() else ""
                _src = f"优先匿名，命中不了再试（{_fl or '本机浏览器: ' + _det}）"
            elif mode == "none":
                _src = "不使用（纯匿名）"
            elif mode == "file":
                if _has_app_cookie_file():
                    _src = f"已登录 cookie 文件（{len(read_app_cookies())} 条）"
                else:
                    _src = "尚无已登录 cookie，请先点「登录」或「导入」"
            else:
                alive = [b for (b,) in _alive_browser_candidates()]
                dead = [b for b in _detect_installed_browsers() if b in _COOKIE_DEAD]
                _src = "浏览器" + (f"（可用: {'、'.join(alive)}）" if alive
                                   else f"（本机读不出{'、'.join(dead) or '浏览器'}，将退回匿名）")
            self.log(f"[Cookie] 来源={v} → {_src}")
        except Exception:
            pass

    def _ask_proxy_manual(self, current=""):
        try:
            from tkinter import simpledialog
            return simpledialog.askstring(
                "手动代理",
                "填写代理地址（支持 socks5:// 、socks5h:// 、http:// ）：\n"
                "例：socks5://127.0.0.1:10808　或　http://127.0.0.1:7890",
                initialvalue=current or "", parent=self.root)
        except Exception:
            return None

    def _sync_proxy(self, prompt=True):
        """把「代理」下拉同步到全局代理解析（跟随系统/不使用/手动），并落盘。"""
        mode = (self.proxy_var.get() or "跟随系统").strip()
        manual = (self.cfg.get("proxy_manual") or "").strip()
        if mode == "手动" and not manual and prompt:
            got = self._ask_proxy_manual("")
            if got and got.strip():
                manual = got.strip()
            else:
                mode = "跟随系统"
                try:
                    self.proxy_var.set(mode)
                except Exception:
                    pass
        set_proxy_setting(mode, manual)
        try:
            _HOST_PROXY_STATE.clear()  # 模式变了，清掉旧的每站直连/代理判定
        except Exception:
            pass
        try:
            self.cfg["proxy_mode"] = mode
            self.cfg["proxy_manual"] = manual
            save_config(self.cfg)
        except Exception:
            pass
        try:
            px = effective_proxy()
            if mode == "不使用":
                self.log("[代理] 不使用 → 全部强制直连（境内被墙站会连不上）")
            elif mode == "手动":
                self.log(f"[代理] 手动 → {px or '（未填地址，等同直连）'}")
            else:
                self.log(f"[代理] 跟随系统 → "
                         f"{px or '未检测到系统代理（被墙站会提示先开代理）'}")
        except Exception:
            pass

    def _import_cookie(self):
        try:
            from tkinter import filedialog
        except Exception:
            filedialog = None
        if not filedialog:
            self.say("这台机器打不开文件选择框，请用「登录」按钮。")
            return
        path = filedialog.askopenfilename(
            title="选择 cookies.txt（Netscape 格式）",
            parent=self.root,
            filetypes=[("Cookie 文件", "*.txt"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            self.log(f"[Cookie] 导入文件: {path}")
        except Exception:
            pass
        n = import_cookie_file(path, self.log)
        if n > 0:
            self.cookie_var.set("已登录Cookie")
            self._sync_cookie()
            save_config(self.cfg)
            self.say(f"已导入 cookie（合计 {n} 条），来源切到「已登录Cookie」～")
        else:
            self.say("没解析出有效 cookie，确认是 Netscape 格式 cookies.txt 再试～")

    def _login_cookie(self):
        raw = (self.url_var.get() or "").strip()
        start = raw.splitlines()[0].strip() if raw else ""
        if not start.lower().startswith("http"):
            start = "https://www.bilibili.com"
        try:
            import threading
            self._ck_done = threading.Event()
            self._ck_cancel = threading.Event()
        except Exception:
            self._ck_done = self._ck_cancel = None
        self._ck_saved = None
        self._show_login_dialog()
        threading.Thread(target=self._login_worker, args=(start,), daemon=True).start()
        try:
            self.root.after(600, self._login_poll)
        except Exception:
            pass

    def _login_worker(self, start):
        try:
            self.log(f"[登录窗] 打开登录窗：{start[:60]}")
        except Exception:
            pass
        try:
            self._ck_saved = capture_login_cookies(
                start, self.log, self._ck_done, self._ck_cancel, timeout_s=600)
        except Exception as e:
            self.log(f"[登录窗] 失败: {str(e)[:140]}")
            self._ck_saved = 0

    def _login_poll(self):
        if self._ck_saved is None:
            try:
                self.root.after(600, self._login_poll)
            except Exception:
                pass
            return
        try:
            if getattr(self, "_ck_dlg", None) and self._ck_dlg.winfo_exists():
                self._ck_dlg.destroy()
        except Exception:
            pass
        n = self._ck_saved or 0
        if n > 0:
            self.cookie_var.set("已登录Cookie")
            self._sync_cookie()
            try:
                save_config(self.cfg)
            except Exception:
                pass
            self.say(f"登录 cookie 已保存（合计 {n} 条），来源切到「已登录Cookie」～")
        else:
            self.say("没抓到 cookie——在窗口里登录后再点「已登录·保存」，或用「导入」也行。")

    def _show_login_dialog(self):
        try:
            d = tk.Toplevel(self.root)
            self._ck_dlg = d
            d.title("登录取 Cookie")
            d.configure(bg=C["bg"])
            try:
                d.attributes("-topmost", True)
                d.transient(self.root)
            except Exception:
                pass
            tk.Label(d, text=("在弹出的浏览器里登录目标站点，\n"
                              "登录成功后点「已登录·保存」即可。"),
                     bg=C["bg"], fg=C["ink"], font=F_UI, justify="left",
                     wraplength=P(340)).pack(padx=P(16), pady=(P(14), P(8)))
            row = tk.Frame(d, bg=C["bg"])
            row.pack(pady=(P(2), P(14)))
            RoundButton(row, text="已登录·保存", command=self._ck_confirm,
                        padx=P(14), pady=P(6)).pack(side="left", padx=P(6))
            RoundButton(row, text="取消", command=self._ck_abort,
                        padx=P(12), pady=P(6)).pack(side="left", padx=P(6))
            d.protocol("WM_DELETE_WINDOW", self._ck_abort)
        except Exception as e:
            self.log(f"[登录窗] 控制窗创建失败: {str(e)[:120]}")

    def _ck_confirm(self):
        try:
            self._ck_done.set()
        except Exception:
            pass
        self.say("好的，正在保存登录 cookie…")

    def _ck_abort(self):
        try:
            self._ck_cancel.set()
        except Exception:
            pass
        try:
            if getattr(self, "_ck_dlg", None) and self._ck_dlg.winfo_exists():
                self._ck_dlg.destroy()
        except Exception:
            pass

    def _on_pet_wake_toggle(self):
        """「开机唤醒桌宠」开关：落盘 + 立即生效（关=收起，开=唤醒）。"""
        on = bool(self.pet_wake_var.get())
        self.cfg["pet_wake"] = on
        try:
            save_config(self.cfg)
        except Exception:
            pass
        if getattr(self, "_pet", None):
            try:
                self._pet.visible(on)
            except Exception:
                pass
        try:
            self.log("[桌宠] 开机唤醒已%s（全局热键 Ctrl+Alt+H 随时唤醒/收起）" % ("开启" if on else "关闭"))
        except Exception:
            pass

    def _autostart_cmd(self):
        """开机自启要执行的命令：打包后=exe 自身；源码运行=pythonw 带脚本。
        默认带 --tray：开机自启直接进系统托盘（不闪主窗），桌宠与下载服务待命。"""
        if getattr(sys, "frozen", False):
            return '"%s" --tray' % sys.executable
        try:
            pyw = sys.executable
            if pyw.lower().endswith("python.exe"):
                pyw = pyw[: -len("python.exe")] + "pythonw.exe"
            script = os.path.abspath(__file__)
            return '"%s" "%s" --tray' % (pyw, script)
        except Exception:
            return '"%s" --tray' % sys.executable

    def get_autostart(self):
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
                v, _ = winreg.QueryValueEx(k, "SniffDL")
                return bool(v)
        except Exception:
            return False

    def set_autostart(self, enable):
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                               r"Software\Microsoft\Windows\CurrentVersion\Run",
                               0, winreg.KEY_SET_VALUE)
            try:
                if enable:
                    winreg.SetValueEx(k, "SniffDL", 0, winreg.REG_SZ, self._autostart_cmd())
                else:
                    try:
                        winreg.DeleteValue(k, "SniffDL")
                    except FileNotFoundError:
                        pass
            finally:
                winreg.CloseKey(k)
            self.cfg["autostart"] = bool(enable)
            save_config(self.cfg)
            self.log("[自启] 已%s开机自启" % ("开启" if enable else "关闭"))
            return True
        except Exception as e:
            self.log("[自启] 设置失败：%s" % str(e)[:100])
            try:
                self.autostart_var.set(self.get_autostart())
            except Exception:
                pass
            return False

    def _sync_dash_aria2(self):
        """同步「大站单文件也多线程」开关到全局 + 落盘。"""
        on = bool(self.dash_aria2_var.get())
        DASH_ARIA2[0] = on
        try:
            self.cfg["dash_aria2"] = on
            save_config(self.cfg)
        except Exception:
            pass
        self.log("[加速] 大站单文件多连接=%s（aria2c %s）" % (
            "开" if on else "关", "接管 DASH 轨" if on else "仅直链，DASH 轨单连接"))

    def _sync_threads(self):
        """把「加速档位」下拉同步到全局 DL_THREADS，并落盘。"""
        global DL_THREADS
        v = (self.threads_var.get() or "自动").strip()
        DL_THREADS = 0 if v in ("自动", "auto", "") else int(v)
        try:
            self.cfg["dl_threads"] = v
        except Exception:
            pass
        try:
            self.log(f"[加速] 档位={v} → 分片并发≤{eff_frag_threads()}, "
                     f"单文件连接≤{eff_conn_threads()}, "
                     f"直链引擎={'aria2c' if ARIA2C else '内置多连接/yt-dlp'}")
        except Exception:
            pass
        try:
            if JS_RUNTIME is not None:
                self.log(f"[EJS] JS 解算器就绪（{JS_RUNTIME.name} {JS_RUNTIME.version}）"
                         f"——YouTube 等混淆签名站可解析")
            else:
                self.log("[EJS] 未找到 deno/node 运行时，YouTube 等混淆站暂无法解签名；"
                         "装 Node.js/Deno 或在 config.js_runtime_dir 指定其目录后重试")
        except Exception:
            pass

    def _start_worker(self, outdir, quality):
        """确保常驻队列 worker 在跑；没跑就启动一个（3 并发）。"""
        with self._job_lock:
            if self._worker_running:
                return
            self._worker_running = True
        self._outdir = outdir
        self._quality = quality
        self.busy = True
        self.pause_evt.set()
        self._c24_success_until = self._c24_fail_until = 0.0   # 开工清掉上轮成功/失败跳起窗
        # 开工 = 自动切工作形态（收工 8 秒后自己变回来）
        if self._revert_job:
            try:
                self.root.after_cancel(self._revert_job)
            except Exception:
                pass
            self._revert_job = None
        self._clip_url = ""
        if getattr(self, "_form", "daily") != "work":
            self._transform("work", auto=True)
        self._mood = set()
        self._started_rows = set()
        self._run_stats = {"ok": 0, "fail": 0, "skip": 0, "cancel": 0,
                           "t0": time.time(), "failed_titles": []}
        self.set_buttons(False)   # 下载中：暂停/终止可点，下载按钮仍可点
        self.bar.config(mode="determinate", value=0)
        try:
            self._hide_toast()
            self._as.update(mode="busy", frac=0.0, speed="", eta="",
                            n=0, total=0, title="")
            self._as_refresh()
        except Exception:
            pass
        self.say(self.line_for("busy"))
        threading.Thread(target=self._queue_worker, daemon=True).start()

    def _queue_worker(self):
        """常驻队列 worker：不断从 _pending 取任务，3 并发下载；队列排空则收尾归位。"""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        st = self._run_stats
        self._history = load_history()
        with ThreadPoolExecutor(max_workers=3) as ex:
            while True:
                with self._job_lock:
                    if not self._pending:
                        # 原子确认无待办（与 _start_worker 互斥，杜绝收尾竞态漏跑）
                        self._worker_running = False
                        break
                    batch = self._pending
                    self._pending = []
                batch = self._prepare_batch(batch)   # 同视频合并 + 已存在跳过
                if not batch:
                    continue
                total = len(batch)
                futs = {ex.submit(self._run_one, (i + 1, total) + args): args
                        for i, args in enumerate(batch)}
                for f in as_completed(futs):
                    try:
                        row, title, entry, fp, err, _el = f.result()
                    except Exception as e:
                        args = futs[f]
                        row, title, entry = args[0], args[1].get("title", ""), args[1]
                        err = f"任务异常: {str(e)[:200]}"
                        fp = None
                    self._finish_row(row, title, entry, fp, err, st)
                    save_history(self._history)
        # 队列排空 → 收尾
        self.q.put(("all_done", (st["ok"], st["fail"], st["skip"], self._outdir,
                                 time.time() - st["t0"], st["failed_titles"],
                                 st["cancel"])))

    def _prepare_batch(self, batch):
        """已下过且文件还在的跳过。返回待跑任务列表。"""
        runnable = []
        for args in batch:                     # args = (row, entry, outdir, quality)
            row, entry, outdir, _q = args
            title = entry["title"]
            base = safe_name(title)
            ext = entry.get("ext") or "mp4"
            cand = [os.path.join(outdir, base + "." + ext),
                    os.path.join(outdir, os.path.splitext(base)[0] + ".mp4"),
                    os.path.join(outdir, os.path.splitext(base)[0] + ".mkv")]
            if self._history.get(entry.get("url") or "") and any(os.path.exists(c) for c in cand):
                st = self._run_stats
                st["skip"] += 1
                hit = next((c for c in cand if os.path.exists(c)), None)
                if hit:
                    entry["_filepath"] = os.path.abspath(hit)
                self.q.put(("row_status", (row, "已存在跳过")))
                self.q.put(("item_ok", row))
                self._queued_rows.discard(row)
                continue
            runnable.append(args)
        return runnable

    def _finish_row(self, row, title, entry, fp, err, st):
        """单任务落定：更新历史/计数/行状态，并从队列占用集合中释放该行。"""
        if err == "cancelled":
            self.q.put(("row_status", (row, "已终止 ✋")))
            st["cancel"] += 1
        elif err is None:
            st["ok"] += 1
            self.q.put(("item_ok", row))
            if isinstance(fp, str) and os.path.exists(fp):
                try:
                    sz = os.path.getsize(fp)
                    entry["size"] = sz
                    entry["_filepath"] = os.path.abspath(fp)
                    self.tree.set(str(row), "size", fmt_size(sz))
                    try:
                        record_download(sz)   # 计入每日账本（值班室热力图/今日战绩）
                    except Exception:
                        pass
                except Exception:
                    pass
            self._history[entry.get("url") or title] = \
                os.path.basename(fp) if fp else title
        else:
            label, action = classify_error(err)
            self.q.put(("row_status", (row, f"✗ {label}")))
            self.log(f"[失败] {title[:60]}: {err}")
            self.log(f"        ↳ {label}｜{action}")
            self.q.put(("item_fail", row))
            st["fail"] += 1
            st["failed_titles"].append((title, label))
        self._queued_rows.discard(row)
        self._started_rows.discard(row)

    def _run_one(self, args):
        """下载单个任务（含暂停/终止中断与干净续传）。args=(n,total,row,entry,outdir,quality)。"""
        n, total, row, entry, outdir, quality = args
        title = entry["title"]

        def hook(d):
            if d["status"] == "downloading":
                done = d.get("downloaded_bytes") or 0
                tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                self.q.put(("progress", (n, total, row, title, done, tot,
                                         d.get("speed"), d.get("fragment_index"),
                                         d.get("fragment_count"))))
            elif d["status"] == "finished":
                self.q.put(("row_status", (row, "合并封装中…")))

        def pp_hook(d):
            if d.get("status") == "started":
                self.q.put(("row_status", (row, f"整理中: {d.get('postprocessor', '')[:12]}")))
            elif d.get("status") == "finished":
                self.q.put(("row_status", (row, "♡ 马上就好")))

        # 排队中被「终止」：直接跳过，绝不开下载
        if row in self._cancel_rows:
            return (row, title, entry, None, "cancelled", 0)
        self.gate()      # 暂停中：在这儿等，不丢已下进度
        if row in self._cancel_rows:
            return (row, title, entry, None, "cancelled", 0)
        self._started_rows.add(row)
        abort = lambda: (row in self._cancel_rows) or (not self.pause_evt.is_set())

        def once():
            self.q.put(("job_start", (n, total, row, title)))
            self.q.put(("row_status", (row, "搬运中…")))
            fp = download_one(entry, entry.get("_page") or self.page_url,
                              outdir, quality, hook,
                              log=self.log, pp_hook=pp_hook, abort=abort)
            if not fp or (isinstance(fp, str) and not os.path.exists(fp)):
                return (row, title, entry, None, "未取到成品文件（取片/下载失败）", 0)
            return (row, title, entry, fp, None, 0)

        while True:
            try:
                return once()
            except SniffAbort:
                if row in self._cancel_rows:
                    return (row, title, entry, None, "cancelled", 0)
                # 暂停中断：等主人点“继续”，再原地从 .part 续传
                self.q.put(("row_status", (row, "已暂停 · 可续传")))
                self.gate()
                if row in self._cancel_rows:
                    return (row, title, entry, None, "cancelled", 0)
                self.q.put(("row_status", (row, "续传中… ♪")))
                continue
            except Exception as e:
                return (row, title, entry, None, str(e)[:250], 0)

    def _poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                getattr(self, "_on_" + kind)(data)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _on_log(self, msg):
        self.log_text.config(state="normal")
        col = _log_color(msg)
        tag = "c_" + col.lstrip("#").lower()
        try:
            self.log_text.tag_config(tag, foreground=col)
        except Exception:
            tag = None
        self.log_text.insert("end", str(msg)[:500] + "\n",
                             (tag,) if tag else ())
        nlines = int(self.log_text.index("end-1c").split(".")[0])
        if nlines > 800:
            self.log_text.delete("1.0", f"{nlines - 800}.0")
        self.log_text.config(state="disabled")
        self.log_text.see("end")

    def _on_probe_done(self, entries):
        acc = self._batch_acc if getattr(self, "_batch_acc", None) is not None else list(entries)
        if acc is not entries:
            seen = {e.get("url") for e in acc}
            add = [e for e in entries if e.get("url") not in seen]
            acc.extend(add)
            self.log(f"[批量] 本页贡献 {len(add)} 条，累计 {len(acc)} 条")
        if getattr(self, "_queue_urls", None):
            self.entries = acc
            self._batch_acc = acc
            self.row_status = {}
            self.refresh_table()
            nxt = self._queue_urls.pop(0)
            self.log(f"========== 批量嗅探 {nxt}（还剩 {len(self._queue_urls)} 个）==========")
            self.status_var.set(f"批量嗅探中… 已累计 {len(acc)} 条，继续下一个页面")
            self.say(self.line_for("prog"))
            self.page_url = nxt
            threading.Thread(target=self._probe_worker, args=(nxt,), daemon=True).start()
            return
        self.bar.stop()
        self.bar.config(mode="determinate", value=0)
        self.entries = acc
        self.row_status = {}
        self.refresh_table()
        n = len(acc)
        el = int(time.time() - getattr(self, "_probe_t0", time.time()))
        tstr = f"{el // 60}分{el % 60}秒" if el >= 60 else f"{el}秒"
        self.status_var.set(f"嗅探完成：找到 {n} 个视频，用时 {tstr}"
                            + ("。点「✦ 全部下载」或选中后下载～" if n else "。一个都没找到…"))
        self.say(self.line_for("found", n) if n
                 else "呜呜…没嗅到视频，可能要登录或在二级页面…")
        if n:
            self.set_mood("found", n=n)
            self.celebrate()
        else:
            self.set_mood("empty")
        self.log(f"[结果] 嗅探完成，共 {n} 个视频，用时 {tstr}")
        self.busy = False
        self._probing = False
        self.set_buttons(True)
        try:
            self._draw_assistant()
        except Exception:
            pass

    def _on_job_start(self, data):
        n, total, row, title = data
        self.set_status(row, "搬运中…")
        self.status_var.set(f"[{n}/{total}] 正在为主人搬运: {title[:70]}")
        if getattr(self, "_mood_kind", "") != "carry":
            self.set_mood("carry", total=total)

    def _on_row_status(self, data):
        row, text = data
        try:
            self.set_status(row, text)
        except Exception:
            pass

    def _on_progress(self, data):
        n, total, row, title, done, tot, speed, fi, fc = data
        sp_txt = fmt_size(speed).replace(" ", "") + "/s" if speed else ""
        eta_txt = ""
        if tot and speed:
            eta_txt = "剩" + fmt_duration(max((tot - done) / speed, 0))
        if tot:
            frac = done / tot
            self.bar.config(value=frac * 100)
            pct = f"{frac * 100:.0f}%"
            rowtxt = " ".join(x for x in (pct, sp_txt, eta_txt) if x)
            stage = int(frac * 3)          # 1/3、2/3 各说一句，收尾不再唠叨
            if stage and (n, stage) not in self._mood:
                self._mood.add((n, stage))
                self.say(self.line_for("prog"))
        elif fc:
            pct = f"分片 {fi}/{fc}"
            rowtxt = " ".join(x for x in (f"{fi}/{fc}", sp_txt) if x)
            self.bar.config(value=fi / fc * 100)
        else:
            pct = fmt_size(done)
            rowtxt = " ".join(x for x in ("搬运中", sp_txt) if x)
        sp = f" · {fmt_size(speed)}/s" if speed else ""
        eta = f" · 剩余 {fmt_duration((tot - done) / speed)}" if (tot and speed) else ""
        self.status_var.set(f"[{n}/{total}] 正在搬运 {title[:55]}  {pct}{sp}{eta}")
        try:
            self.set_status(row, rowtxt)
        except Exception:
            pass
        try:
            if tot:
                self._as["frac"] = done / tot
            elif fc:
                self._as["frac"] = fi / fc
            self._as.update(mode="busy" if self.pause_evt.is_set() else "pause",
                            speed=sp_txt, eta=eta_txt,
                            n=n, total=total, title=title)
            self._as_refresh()
        except Exception:
            pass

    def _on_item_ok(self, row):
        self.set_status(row, "✓ 完成 ♪")
        try:
            self.tree.see(str(row))
        except Exception:
            pass

    def _on_item_fail(self, row):
        self.set_status(row, "✗ 失败")
        try:
            self.tree.see(str(row))
        except Exception:
            pass

    def _on_cooling(self, data):
        n, sec = data
        self.status_var.set(f"被站点限流了…歇 {sec} 秒后自动重试 {n} 个（主人别关窗口哦）")
        self.say("呼…被限流了，稍歇一下再为主人重试！")
        self.log(f"[重试] 站点限流, 冷却 {sec}s 后自动重试 {n} 个失败项")

    def _on_all_done(self, data):
        ok, fail, skip, outdir, elapsed, failed_titles, cancelled = data
        self.busy = False
        self._queued_rows = set()
        self._started_rows = set()
        self._cancel_rows = set()
        self.set_buttons(True)
        self.bar.config(value=100 if ok else 0)
        extra = f"，跳过已存在 {skip}" if skip else ""
        if cancelled:
            extra += f"，已终止 {cancelled}"
        m, s = int(elapsed) // 60, int(elapsed) % 60
        tstr = f"{m}分{s}秒" if m else f"{s}秒"
        self.status_var.set(
            f"交差～成功 {ok} / {len(self.entries)}，失败 {fail}{extra}，"
            f"总用时 {tstr}。保存于 {outdir}")
        self.say(self.line_for("done") if not fail else self.line_for("fail", fail))
        if ok and not fail:
            self.set_mood("done")
            self.celebrate()
        elif fail:
            self.set_mood("fail", n=fail)
        self.log(f"[完成] 总用时 {tstr}")
        try:
            self._as["mode"] = "done" if (ok and not fail) else ("fail" if fail else "idle")
            self._as["frac"] = 1.0 if ok else 0.0
            self._as["speed"] = self._as["eta"] = ""
            # cast24 角色：只有此刻（真正交差）才允许跳起/失落各播一遍
            if self._c24() is not None:
                if ok and not fail:
                    self._c24_success_until = time.time() + 2.0
                elif fail:
                    self._c24_fail_until = time.time() + 3.2
            self._draw_assistant()
        except Exception:
            pass
        # 收工 8 秒后自动脱掉耳机变回日常（工作形态的反差萌）
        self._schedule_revert(8000)
        # 汇总失败主因（classify_error 已把每条错误归成人话标签）
        reason_txt = ""
        try:
            labs = [lb for (_t, lb) in failed_titles if lb]
            if labs:
                top = max(set(labs), key=labs.count)
                n_top = labs.count(top)
                reason_txt = f"{top}" + (f"（{n_top} 条）" if n_top > 1 else "")
        except Exception:
            reason_txt = ""
        hint = reason_txt or "多为站点限流/链接过期"
        if ok and not fail:
            self._toast("全部搬完啦 ♡", "%d 个 · 用时 %s" % (ok, tstr),
                        kind="ok", action=("打开文件夹", self.open_outdir))
        elif ok and fail:
            self._toast("搬完 %d 个，%d 个没拿下" % (ok, fail),
                        "主因：%s；选中 ✗ 行看日志动作提示" % hint, kind="warn",
                        action=("打开文件夹", self.open_outdir))
        elif fail:
            self._toast("这批都没搬动…", "主因：%s" % hint, kind="warn")
        self.finish_action()


def run_uicheck(out):
    """把"自绘管线到底通不通"写进软件本体：`SniffDL.exe --uicheck` 自检。

    为什么必须有：PIL/ImageTk 在 PyInstaller 冻结环境里缺 `PIL._imagingtk`/`_tkinter_finder`
    时会**静默失败**，代码按设计回落纯色平涂 —— 界面"能跑但变平"，光看 exe 能不能启动、
    甚至肉眼看小截图都发现不了。所以必须由程序自己断言"渐变/圆角位图真的生成出来了"。
    返回退出码：0=全绿。
    """
    fails = []

    def chk(name, ok, hint=""):
        out("%-42s %s%s" % (name, "PASS" if ok else "FAIL",
                            "" if ok else ("  " + hint)))
        if not ok:
            fails.append(name)

    try:    # DPI 感知必须在建 Tk 之前声明，否则 scaling 已定死、自检按 S=1.0 量像素
        import ctypes as _ct
        _ct.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    root = tk.Tk()
    root.title(APP_TITLE + "  · UI 自检")
    app = App(root)
    root.deiconify()
    t_end = time.time() + 4.0
    while time.time() < t_end:
        root.update()
        time.sleep(0.02)
    chk("PIL 可用（否则渐变全回落纯色）", _pil_modules() is not False,
        "PIL 导入失败")
    im = _grad_photo(60, 20, 10, "#E1649A", "#8A6BD6", 45)
    chk("渐变位图能生成（ImageTk 已冻结进包）", im is not None,
        "返回 None = 缺 PIL._imagingtk / PIL._tkinter_finder")
    cm = _card_photo(400, 120, _up(12), "#FFFFFF", "#E4E7EB", _up(13), _up(14))
    chk("圆角+投影卡片底图能生成", cm is not None, "None = 卡片会退回方角")
    chk("界面已实际用上渐变位图", len(_GRAD_CACHE) > 0, "%d 张" % len(_GRAD_CACHE))
    chk("界面已实际用上卡片底图", len(_CARD_CACHE) > 0, "%d 张" % len(_CARD_CACHE))
    keys = [k for k in THEME_ORDER if k in getattr(app, "_dress_imgs", {})]
    size, gap, ring = getattr(app, "_dress_geo", None) or (0, 0, 0)
    avail = app.sidebar.winfo_width() - 2 * app.P(14)
    chk("换装 %d 只不溢出侧栏" % len(keys),
        len(keys) * (size + 2 * gap) <= avail,
        "总宽 %d / 可用 %d" % (len(keys) * (size + 2 * gap), avail))
    try:
        app.entries = [{"title": "自检样例", "ext": "mp4", "res": "1280x720",
                        "duration": 60, "size": 1048576, "how": "自检",
                        "url": "http://127.0.0.1/x.mp4"}]
        app.row_status = {0: "41% · 1 MB/s"}
        app.refresh_table()
        t_end = time.time() + 1.5
        while time.time() < t_end:
            root.update()
            time.sleep(0.02)
        n_items = len(app.tree.find_withtag("r"))
        chk("自绘结果表画出行", n_items > 5, "%d 个绘制项" % n_items)
        chk("结果表重绘零异常", getattr(app.tree, "_last_err", None) is None,
            str(getattr(app.tree, "_last_err", None)))
    except Exception as e:
        chk("自绘结果表可用", False, repr(e))
    try:    # 「最近完成」必须真的把 downloaded.json 露出来（用户投诉：没记录=没功能）
        app._show_view("dash")
        app._dash_refresh()
        t_end = time.time() + 1.2
        while time.time() < t_end:
            root.update()
            time.sleep(0.02)
        rows = [r for r in getattr(app, "_dash_hist", [])
                if str(r._nm.cget("text")).strip()]
        chk("值班室「最近完成」列出历史记录", len(rows) > 0,
            "%d 行；首行=%r" % (len(rows),
                                str(rows[0]._nm.cget("text"))[:40] if rows else ""))
    except Exception as e:
        chk("值班室「最近完成」可渲染", False, repr(e))
    try:
        root.destroy()
    except Exception:
        pass
    out("UICHECK RESULT=%s" % ("PASS" if not fails else "FAIL(%d): %s" % (
        len(fails), " / ".join(fails))))
    return 0 if not fails else 1


def run_qmem_check(out):
    """`SniffDL.exe --qmem`：记忆系统（IDM 式持久化下载列表）自检。

    为什么必须写进本体：这套东西的价值全在"叉掉重开之后"，只看代码看不出问题；
    而它同时背着一条红线——**恢复记忆绝不等于重放下载**（历史上开机重放过浏览器
    接管的一堆链接，把电脑下一遍）。所以断言重点：① 队列能落盘/读回；② 重启后
    只有显示、没有任何任务被自动投出去；③ 已完成的一条都不能重下。
    全程用临时目录里的 queue.json / downloaded.json，绝不碰用户真实数据。
    返回退出码：0=全绿。"""
    fails = []

    def chk(name, ok, hint=""):
        out("%-46s %s%s" % (name, "PASS" if ok else "FAIL",
                            "" if ok else ("  " + hint)))
        if not ok:
            fails.append(name)

    tmpdir = os.path.join(tempfile.gettempdir(), "snifftdl_qmem_check")
    shutil.rmtree(tmpdir, ignore_errors=True)
    os.makedirs(tmpdir, exist_ok=True)
    qf, hf = os.path.join(tmpdir, "queue.json"), os.path.join(tmpdir, "downloaded.json")
    old_q, old_h = QUEUE_FILE, HISTORY_FILE
    globals()["QUEUE_FILE"] = qf
    globals()["HISTORY_FILE"] = hf
    globals()["_CLI_NO_PET"] = True       # 自检不拉桌宠/助手窗（防孤儿 Electron 锁打包目录）
    root = None
    app = None
    try:
        root = tk.Tk()
        root.withdraw()
        app = App(root)
        app._rpc_tasks, app._rpc_order = {}, []

        def mkfile(nm, size=1024):
            p = os.path.join(tmpdir, nm)
            with open(p, "wb") as f:
                f.write(b"\0" * size)
            return p

        done_file = mkfile("finished.mp4")
        ledger_file = mkfile("already.mp4")
        u_done = "https://example.com/finished.mp4"
        u_live = "https://example.com/still-running.mp4"
        u_auto = "https://example.com/browser-auto.mp4"
        u_ledger = "https://example.com/already.mp4"

        # 1) 落盘 / 读回 / 原子写 / 坏文件
        ok_save = save_queue([{"gid": "x1", "uri": u_live, "status": "active"}])
        rows = load_queue()
        chk("queue.json 落盘+读回", bool(ok_save) and len(rows) == 1, "%r" % (rows,))
        chk("原子写不留 .tmp", not os.path.exists(qf + ".tmp"))
        with open(qf, "w", encoding="utf-8") as f:
            f.write("{ 半截 JSON 不该把程序带沟里")
        chk("坏文件退回空列表（不抛异常）", load_queue() == [])

        # 2) 新任务登记必须立刻进任务库（哪怕下一秒被叉掉）
        app._rpc_tasks, app._rpc_order = {}, []
        g_live = app.rpc_add_uri(u_live, {"out": "still-running.mp4"},
                                 defer=True, src="user")
        g_auto = app.rpc_add_uri(u_auto, {"out": "browser-auto.mp4"},
                                 defer=True, src="browser")
        rows = load_queue()
        by_gid = {str(r.get("gid")): r for r in rows}
        chk("新任务即时落盘（defer 也记）",
            len(rows) == 2 and g_live in by_gid and g_auto in by_gid, "%d 行" % len(rows))
        chk("来源 user/browser 被如实记录",
            by_gid.get(g_live, {}).get("src") == "user"
            and by_gid.get(g_auto, {}).get("src") == "browser",
            "%r" % [by_gid.get(g, {}).get("src") for g in (g_live, g_auto)])

        # 3) 造一条"已完成" + 一条"其实早已在下过账本里"的任务，然后模拟重启
        g_done = "done0001"
        g_ledg = "ledger001"
        app._rpc_tasks[g_done] = {
            "gid": g_done, "uri": u_done, "name": "finished.mp4", "dir": tmpdir,
            "path": done_file, "status": RPC.ST_COMPLETE, "totalLength": 1024,
            "downloadedLength": 1024, "downloadSpeed": 0, "errorCode": "0",
            "errorMessage": "", "referer": "", "headers": {}, "thread": None,
            "cancel": False, "start_ts": 0.0, "done_ts": time.time(),
            "src": "user", "kind": "http", "added_ts": time.time() - 5, "note": "",
            "_dkey": app._rpc_norm_key(u_done)}
        app._rpc_order.append(g_done)
        app._rpc_tasks[g_ledg] = dict(app._rpc_tasks[g_done])
        app._rpc_tasks[g_ledg].update({
            "gid": g_ledg, "uri": u_ledger, "name": "already.mp4", "path": ledger_file,
            "status": RPC.ST_ACTIVE, "downloadedLength": 300, "done_ts": 0.0,
            "_dkey": app._rpc_norm_key(u_ledger)})
        app._rpc_order.append(g_ledg)
        app._qmem_mark(force=True)
        # 完成账本：u_ledger 曾成功下载且成品还在
        save_history({app._rpc_norm_key(u_ledger): {
            "name": os.path.basename(ledger_file), "dir": tmpdir,
            "size": 1024, "ts": int(time.time())}})

        app._rpc_tasks, app._rpc_order = {}, []   # ← 假装程序被叉掉重开
        app._qmem_restore()
        t = app._rpc_tasks
        chk("重启后队列条目全回来", len(t) == 4, "%d 条：%r" % (len(t), sorted(t)))
        chk("进行中的任务恢复后=待继续（不自动开跑）",
            t.get(g_live, {}).get("status") == RPC.ST_PAUSED
            and t.get(g_live, {}).get("thread") is None,
            repr(t.get(g_live, {}).get("status")))
        chk("恢复后没有任何任务在跑",
            not app.rpc_list({RPC.ST_ACTIVE}) and not app.rpc_list({RPC.ST_WAITING}),
            repr([list(app.rpc_list({RPC.ST_ACTIVE})), list(app.rpc_list({RPC.ST_WAITING}))]))
        chk("已完成显示完成（且没被投出去重下）",
            t.get(g_done, {}).get("status") == RPC.ST_COMPLETE
            and t.get(g_done, {}).get("thread") is None,
            repr(t.get(g_done, {}).get("status")))
        chk("浏览器自动接管标成不恢复",
            t.get(g_auto, {}).get("status") == RPC.ST_PAUSED
            and "不恢复" in (t.get(g_auto, {}).get("note") or ""),
            repr(t.get(g_auto, {}).get("note")))
        chk("完成账本对账：其实下过的标完成而非续传",
            t.get(g_ledg, {}).get("status") == RPC.ST_COMPLETE,
            repr([t.get(g_ledg, {}).get("status"), t.get(g_ledg, {}).get("note")]))
        chk("待继续行带提示（主人看得见为什么没动）",
            "点继续" in (t.get(g_live, {}).get("note") or ""),
            repr(t.get(g_live, {}).get("note")))

        # 4) 已完成的链接再投一次 = 零流量复用，绝不重下
        n_threads_before = sum(1 for x in t.values() if x.get("thread"))
        g_again = app.rpc_add_uri(u_done, {"out": "finished.mp4"}, src="user")
        chk("同一链接重复提交直接复用旧任务（不重下）",
            g_again == g_done and len(app._rpc_order) == 4,
            "gid=%s vs %s" % (g_again, g_done))
        chk("复用旧任务没有新起下载线程",
            sum(1 for x in app._rpc_tasks.values() if x.get("thread")) == n_threads_before)

        # 4.5) 并发写盘不能打架（真机踩过的坑：共用同一个 .tmp 名 → 半截 JSON → 恢复读到 0 条）
        import threading as _th
        errs = []
        payloads = {}
        for k in range(8):
            payloads[k] = [{"gid": "c%d_%d" % (k, i),
                            "uri": "https://example.com/c%d_%d.mp4" % (k, i),
                            "status": "complete", "src": "user", "added_ts": k * 100 + i}
                           for i in range(5)]

        def _w(k):
            try:
                for _ in range(20):
                    save_queue(payloads[k])
            except Exception as e:
                errs.append(repr(e))

        seen = [""]

        def _r():
            try:
                for _ in range(120):
                    d = load_queue()
                    if d and len(d) != 5 and seen[0] == "":
                        seen[0] = "%d 条 gid=%r" % (len(d), [r.get("gid") for r in d][:3])
                        errs.append("读到条数不对：%d" % len(d))
            except Exception as e:
                errs.append(repr(e))

        ws = [_th.Thread(target=_w, args=(k,)) for k in range(8)]
        rs = [_th.Thread(target=_r) for _ in range(2)]
        save_queue(payloads[0])       # 先 prime：否则读线程一上来读到的是上一步 App 的快照，
                                      # 会把"条数不是 5"误判成并发打架（本断言曾因此假失败）
        for x in ws + rs:
            x.start()
        for x in ws + rs:
            x.join(timeout=30)
        final = load_queue()
        exact = any(final == payloads[k] for k in payloads)
        chk("并发写不打架（文件永远可解析且=某次完整写入）",
            not errs and exact, "%r｜现场=%s｜末态=%r" % (
                errs[:2], seen[0], [r.get("gid") for r in final][:3]))
        chk("并发后不留 .tmp 残骸", not [f for f in os.listdir(tmpdir) if f.endswith(".tmp")],
            str([f for f in os.listdir(tmpdir) if f.endswith(".tmp")]))

        # 5) 封顶 + 清除已完成
        big = [{"gid": "b%d" % i, "uri": "https://example.com/b%d.mp4" % i,
                "name": "b%d.mp4" % i, "dir": tmpdir, "status": "complete",
                "src": "user", "added_ts": time.time() + i} for i in range(QMEM_MAX + 50)]
        save_queue(big)
        app._rpc_tasks, app._rpc_order = {}, []
        app._qmem_restore()
        app._qmem_mark(force=True)
        chk("任务库封顶 %d 条" % QMEM_MAX, len(load_queue()) == QMEM_MAX,
            "%d 条" % len(load_queue()))
        gone = app.rpc_clear_queue(done_only=True)
        left = load_queue()
        chk("清除已完成只动列表不动文件",
            gone > 0 and all(r.get("status") != "complete" for r in left)
            and os.path.isfile(done_file),
            "清除 %d／剩 %d／成品文件 %s" % (gone, len(left), os.path.isfile(done_file)))

        # 6) 保险丝：内存空 + 本进程还没认领任务库 ⇒ 绝不允许把已有历史覆盖成空
        keep3 = [{"gid": "k1", "uri": "https://example.com/k1.mp4", "status": "paused"},
                 {"gid": "k2", "uri": "https://example.com/k2.mp4", "status": "complete"},
                 {"gid": "k3", "uri": "https://example.com/k3.mp4", "status": "paused"}]
        save_queue(keep3)
        app._rpc_tasks, app._rpc_order = {}, []
        app._qmem_loaded = False
        app._qmem_dirty = True
        app._qmem_flush(force=True)
        chk("开机竞态不许拿空队列覆盖历史", len(load_queue()) == 3,
            "%d 条" % len(load_queue()))
        # 认领过后，"主人主动清空"这类空写要能正常落盘（否则清除功能就废了）
        app._qmem_loaded = True
        app._qmem_dirty = True
        app._qmem_flush(force=True)
        chk("认领过后允许写空（主人真清空了）", load_queue() == [],
            "%r" % ([r.get("gid") for r in load_queue()],))

        # 7) 根因回归：对不存在的 gid 发 pause/remove（webview 轮询/外部 aria2 客户端常态）
        #    曾经会无条件 _qmem_mark → 拿当时的空快照覆盖 queue.json，开机前的历史就这么没了
        save_queue([{"gid": "r1", "uri": "https://example.com/r1.mp4", "status": "paused",
                     "src": "user"}])
        app._rpc_tasks, app._rpc_order = {}, []
        app._qmem_loaded = True
        app.rpc_pause("nosuchgid000001")
        app.rpc_remove("nosuchgid000002")
        chk("操作不存在的 gid 不得清空任务库", len(load_queue()) == 1,
            "%d 条" % len(load_queue()))
    except Exception as e:
        import traceback
        chk("自检全程不崩", False, repr(e) + " | " + traceback.format_exc()[-200:])
    finally:
        globals()["QUEUE_FILE"], globals()["HISTORY_FILE"] = old_q, old_h
        globals()["_CLI_NO_PET"] = False
        try:
            if root is not None:
                root.destroy()
        except Exception:
            pass
        shutil.rmtree(tmpdir, ignore_errors=True)
    out("QMEM RESULT=%s" % ("PASS" if not fails else "FAIL(%d): %s" % (
        len(fails), " / ".join(fails))))
    return 0 if not fails else 1


def cli_main(argv):
    """命令行模式: SniffDL.exe --diagnose | --uicheck | --qmem | --probe URL | --probe-deep URL
    用于自动验证 exe 内核可用性；结果写 sniffdl.log，退出码 0=成功。"""
    setup_logging()
    install_crash_hooks()

    def out(m):
        LOG.info(m)
        try:
            print(m, flush=True)
        except Exception:
            pass

    mode = argv[0]
    out(f"CLI 模式: {argv}")
    if mode == "--diagnose":
        run_diagnose(out)
        return 0
    if mode == "--uicheck":
        return run_uicheck(out)
    if mode == "--qmem":
        return run_qmem_check(out)
    if mode in ("--probe", "--probe-deep"):
        url = argv[-1]
        if not re.match(r"^https?://", url):
            out(f"无效网址: {url}")
            return 2
        out(f"开始嗅探 {url} (深度: {mode == '--probe-deep'})")
        # 0) 预检先行: 先取页面 HTML 判断站点类型，AList/加密/列表等站点直接选引擎，
        #    省掉 yt-dlp 对冷门站注定无效的 15~20 秒白等
        pre_html = precheck_html(url, out)
        route = route_by_precheck(pre_html, url)
        scope = "all" if mode == "--probe-deep" else "single"
        if scope != "all" and route == "list":
            out("[范围] 仅本页视频：跳过列表页爬取")
            route = "normal"
        if route != "alist":
            # -1) yt-dlp 快速通道: YouTube/B站/Twitter 等大站秒级命中, 直接短路
            try:
                fast = ydl_probe(url, out)
            except Exception:
                fast = []
            if fast:
                probed = probe_all(fast, url, out)
                out(f"[CLI] 结果: {len(probed)} 个视频")
                for e in probed:
                    out(f"  {e['title'][:50]} | {e.get('ext')} | {e.get('res', '-')} | "
                        f"时长 {fmt_duration(e.get('duration'))} | {v_fmt(e.get('size'))} | {e.get('how', '-')}")
                return 0 if probed else 1
            if blocked_site_hint(url, out):
                return 1
        if route == "alist":
            out("[预检] 检测到 AList V3 站点（Vue SPA 空壳），走 AList 引擎…")
            try:
                sniffed = sniff_alist_site(url, out)
            except Exception as e:
                out(f"[CLI] AList 引擎失败: {str(e)[:200]}")
                sniffed = []
        elif route == "files":
            out("[预检] 检测到文件列表页（网盘式），直接收取全部文件直链…")
            sniffed = collect_file_entries(pre_html, url, out)
        elif route == "encrypted":
            out("[预检] 检测到嵌入播放器站，直接启用加密引擎…")
            sniffed = []
            try:
                sniffed = sniff_encrypted_player(url, out)
            except Exception as e:
                out(f"[CLI] 加密引擎失败: {str(e)[:200]}")
        elif route == "list":
            out("[预检] 检测到视频列表页，直接进入详情页解析…")
            sniffed = []
            try:
                sniffed = sniff_list_page(url, out)
            except Exception as e:
                out(f"[CLI] 列表页失败: {str(e)[:200]}")
        elif route == "mse":
            out("[预检] 疑似 MSE/blob 站（页面有 <video> 但无直链），启用 MSE 捕获引擎…")
            sniffed = []
            try:
                sniffed = sniff_mse_capture(url, out, meta_only=True, wait_s=45)
            except Exception as e:
                out(f"[CLI] MSE 引擎失败: {str(e)[:200]}")
            if not sniffed:
                out("[CLI] MSE meta 未捕获到分片, 回退常规嗅探…")
                route = "normal"
        if route == "normal":
            sniffed = []
            try:
                if mode == "--probe-deep":
                    sniffed = run_deep_sniff(url, out)
                else:
                    sniffed = run_sniff(url, out)
            except Exception as e:
                out(f"[CLI] 嗅探失败: {str(e)[:300]}")
                sniffed = []
            if not sniffed:
                out("[CLI] 常规嗅探无结果，启用加密播放器引擎…")
                try:
                    sniffed = sniff_encrypted_player(url, out)
                except Exception as e:
                    out(f"[CLI] 加密引擎失败: {str(e)[:200]}")
            if not sniffed and scope == "all":
                out("[CLI] 疑似列表页，进入详情页解析…")
                try:
                    sniffed = sniff_list_page(url, out)
                except Exception as e:
                    out(f"[CLI] 列表页失败: {str(e)[:200]}")
            if not sniffed and pre_html and is_mse_candidate(pre_html, url):
                out("[CLI] 其他引擎空手, 疑似 blob 流站, 启用 MSE 捕获…")
                try:
                    sniffed = sniff_mse_capture(url, out, meta_only=True, wait_s=45)
                except Exception as e:
                    out(f"[CLI] MSE 引擎失败: {str(e)[:200]}")
        probed = probe_all(sniffed, url, out) if sniffed else []
        out(f"[CLI] 结果: {len(probed)} 个视频")
        for e in probed:
            out(f"  {e['title'][:50]} | {e.get('ext')} | {e.get('res', '-')} | "
                f"时长 {fmt_duration(e.get('duration'))} | {v_fmt(e.get('size'))} | {e.get('how', '-')}")
        return 0 if probed else 1
    out(f"未知参数: {argv}，支持 --diagnose / --probe URL / --probe-deep URL")
    return 2


def v_fmt(n):
    return fmt_size(n)


_SINGLETON_HANDLE = None


def ensure_single_instance():
    """单实例锁：防止多次双击=多个主窗+多个托盘图标+多只桌宠（实测残留过 6 进程）。
    已在运行时：把旧实例主窗唤醒到前台，本实例安静退出。返回 False=该退。"""
    global _SINGLETON_HANDLE
    try:
        import ctypes
        import ctypes.wintypes
        k = ctypes.windll.kernel32
        u = ctypes.windll.user32
        _SINGLETON_HANDLE = k.CreateMutexW(None, False, "Local\\SniffDL_SingleInstance")
        if k.GetLastError() != 183:          # ERROR_ALREADY_EXISTS
            return True
        found_web = []
        found_tk = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def _cb(hwnd, _l):
            n = ctypes.create_unicode_buffer(128)
            u.GetWindowTextW(hwnd, n, 128)
            t = n.value
            if t == "SniffDL":                       # web 模式的可见主窗 = webview
                found_web.append(hwnd)
            elif t.startswith(APP_TITLE):           # Tk 引擎窗（web 模式应隐藏，别唤它）
                found_tk.append(hwnd)
            return True
        u.EnumWindows(_cb, 0)
        hwnd = found_web[0] if found_web else (found_tk[0] if found_tk else None)
        if hwnd:
            if u.IsIconic(hwnd):
                u.ShowWindow(hwnd, 9)               # SW_RESTORE
            # 若在屏幕外或异常小（webview 偶尔被甩到 -32000 隐藏态），拉回居中正常尺寸
            r = ctypes.wintypes.RECT()
            u.GetWindowRect(hwnd, ctypes.byref(r))
            if r.left < 0 or r.top < 0 or (r.right - r.left) < 400 or (r.bottom - r.top) < 300:
                sw = u.GetSystemMetrics(0); sh = u.GetSystemMetrics(1)
                w, h = min(1320, sw - 80), min(860, sh - 80)
                u.SetWindowPos(hwnd, 0, (sw - w) // 2, (sh - h) // 2, w, h, 0x0040)  # SWP_SHOWWINDOW
            u.ShowWindow(hwnd, 5)                   # SW_SHOW
            u.BringWindowToTop(hwnd)
            u.SetForegroundWindow(hwnd)
        return False
    except Exception:
        return True                           # 锁机制失灵也别拦用户


def main():
    argv = sys.argv[1:]
    # 打包形态：exe 兼任无界面内核入口 / 打包自检入口（CI 跑 `SniffDL.exe --selftest`、`SniffDL.exe --core …`）。
    # 必须在单实例锁、Tk 之前分流；并把 __main__ 登记成 video_downloader，免得 sniffdl_core/core.engine
    # 再 import 一份新副本（两份全局：BASE_DIR/ARIA2C 改在一份上，另一份不知道）。
    if argv and argv[0] in ("--core", "--selftest"):
        if __name__ == "__main__":
            sys.modules.setdefault("video_downloader", sys.modules["__main__"])
        import sniffdl_core
        sys.exit(sniffdl_core.main(argv[1:] if argv[0] == "--core" else argv))
    # 打包形态：exe 兼任桌宠宿主入口（pet_bridge 用 `exe --pet-host ...` 拉起）
    if argv and argv[0] == "--pet-host":
        try:
            import pet_host
            sys.argv = ["pet_host.py"] + argv[1:]
            sys.exit(pet_host.main())
        except SystemExit:
            raise
        except Exception:
            sys.exit(2)
    start_hidden = "--tray" in argv
    argv = [a for a in argv if a != "--tray"]
    # 打包形态：webview 主界面入口（Tk 引擎在另一进程/托盘，这里只开 pywebview 窗）
    if argv and argv[0] == "--webview":
        setup_logging()
        try:
            import ui_web
            sys.exit(ui_web.main())
        except SystemExit:
            raise
        except Exception:
            sys.exit(2)
    if argv:
        sys.exit(cli_main(argv))
    if not ensure_single_instance():
        sys.exit(0)
    setup_logging()
    install_crash_hooks()
    try:                      # 高分屏：自己接管像素渲染，别让 Windows 拉伸位图（否则美图发糊）
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PROCESS_PER_MONITOR_DPI_AWARE
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass
    root = tk.Tk()
    app = App(root)
    app.log(f"SniffDL 启动 | yt-dlp {yt_dlp.version.__version__} | "
            f"Playwright {'√' if HAS_PW else '×'} | ffmpeg {'√' if FFMPEG else '×'} | 日志: {LOG_FILE}")
    app._set_log_greet(app._greet_line())
    try:
        app._safety_selfcheck()        # 每道防呆自报状态：日志少一行或出现 !! 就是坏了
    except Exception:
        pass
    if getattr(app, "_web_mode", False):
        # Web UI 模式：Tk 主窗只当后台引擎（隐藏到托盘），可见主界面 = HTML webview。
        try:
            root.withdraw()
        except Exception:
            pass
        root.after(1600, app.launch_webview)   # 等 RPC 端口就绪再开 webview
    elif start_hidden:
        try:
            root.withdraw()          # 开机自启：直接进托盘，主窗不闪
        except Exception:
            pass
    root.mainloop()


if __name__ == "__main__":
    main()