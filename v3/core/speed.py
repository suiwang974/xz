# -*- coding: utf-8 -*-
"""SniffDL v3 直链提速：把 sdl_speed（IDM 式动态分片引擎）接到内核下载流程。

两个入口（由 core/engine.py 接线，接线说明见 docs/跨包请求.md「WP2 提速引擎接线」）：

  run_direct(eng, t, prog, headers) -> path
      整条直链任务：算文件名 → 跳过已存在 → 过期预检 → 按网盘规则补头/定连接上限 → run_http。
  run_http(eng, url, outdir, name, headers, prog=None, cancel=None, conn=None, min_split="1M",
           status_cb=None, task=None) -> path
      与 engine._http_aria2c 同参，可以直接放在 aria2c 前面先试。

连接上限（A15，2026-11-12）：自动 8→64（快主机 128），设置「最大连接数」max_conns=8/16/32/64/128 当上限；
需要 >16 路（needs_sdl）时即使 speed_engine=aria2 也先走本引擎，失败才回 aria2（aria2 那边仍夹到 16）。

结果约定（调用方只需三种处理）：
  · 返回路径                         成功（文件已是最终名）。
  · raise video_downloader._AbortSignal  用户暂停/取消（.sdl + .sdlpart 保留，下次同 URL 自动续传）。
  · raise RuntimeError(中文原因)         404/410/签名过期/磁盘满：直接失败，别再回退。
  · raise Fallback(原因)                 其他一切（不支持、403/412、多次 5xx、网络…）：交回 aria2 原流程。
    抛 Fallback 前已删掉本引擎的 .sdl/.sdlpart，aria2 不会撞上半个预分配文件。

限速：在每条连接自己的线程里读完一块就扣 eng.gbucket（全局）和单任务桶 eng._buckets[gid]（与
engine._throttled_prog 同一个桶对象），不放在进度回调里；运行期间置 core.engine._TL.in_aria2=True，
让外层 _throttled_prog 不再重复扣。限速值每个控制周期重读，界面改限速立即生效。

临时文件：<name>.sdl（与「未完成后缀」一致，engine 的覆盖/取消清理已认这个后缀）+ 侧车 <name>.sdlpart。
代理：get_system_proxy()（http/https/socks5，SOCKS 需 PySocks）；回环地址一律直连且不读环境变量代理。
"""
import os
import sys
import threading
import time
from urllib.parse import urlparse, unquote

try:
    import sdl_speed as SPEED
except Exception:          # 包缺失：run_* 一律 Fallback，行为与没接入时完全一样
    SPEED = None

try:
    import netdisk_rules as ND
except Exception:
    ND = None

TMP_SUFFIX = ".sdl"
SIDE_SUFFIX = ".sdlpart"
LOOPBACK = ("127.0.0.1", "localhost", "::1", "0.0.0.0")


class Fallback(Exception):
    """本引擎不接这单：调用方回退 aria2c（原流程）。str(e) 是中文原因（写日志用）。"""


class _LocalAbort(Exception):
    """没加载 video_downloader（单元测试）时的取消异常。"""


# ------------------------------------------------------------------ 依赖（全部惰性取，测试可不加载 Tk 内核）
def _V():
    return sys.modules.get("video_downloader")


def abort_exc():
    v = _V()
    return getattr(v, "_AbortSignal", None) or _LocalAbort


def _TL():
    m = sys.modules.get("core.engine")
    return getattr(m, "_TL", None)


def _log(eng, msg):
    try:
        eng.log(msg)
    except Exception:
        pass


def available():
    return SPEED is not None


def mode(eng):
    """设置 speed_engine → 'auto'（先动态分片，Fallback 回 aria2）/ 'aria2'（不用本引擎）/ 'sdl'（只用本引擎，
    Fallback 当失败）。sdl_speed 包缺失时一律 'aria2'。兼容旧键 speed_engine_off。"""
    if SPEED is None:
        return "aria2"
    cfg = getattr(eng, "cfg", None) or {}
    if cfg.get("speed_engine_off"):
        return "aria2"
    v = str(cfg.get("speed_engine", "auto")).lower()
    if v in ("aria2", "aria2c", "off", "false", "0"):
        return "aria2"
    return "sdl" if v == "sdl" else "auto"


def enabled(eng):
    return mode(eng) != "aria2"


# ------------------------------------------------------------------ 限速器（在连接线程里调用）
class Limiter:
    """全局桶 ∧ 单任务桶。consume(n) 由 sdl_speed 每条连接读完一块后调用；rate() 每周期重读。"""

    def __init__(self, eng, task=None, cancel=None):
        self.eng = eng
        self.task = task
        self.cancel = cancel

    def _task_rate(self):
        t = self.task
        try:
            return int((t or {}).get("limit_kbps") or 0) * 1024
        except Exception:
            return 0

    def _task_bucket(self, rate):
        eng, t = self.eng, self.task
        if not t or rate <= 0:
            return None
        bs = getattr(eng, "_buckets", None)
        if bs is None:
            return None
        gid = t.get("gid") or id(t)
        b = bs.get(gid)
        if b is None or b.rate != rate:
            from . import ratelimit as RL
            b = bs[gid] = RL.TokenBucket(rate)
        return b

    def rate(self):
        g = getattr(getattr(self.eng, "gbucket", None), "rate", 0) or 0
        vals = [x for x in (int(g), self._task_rate()) if x > 0]
        return min(vals) if vals else 0

    def consume(self, n, cancel=None):
        def _c():
            try:
                if cancel and cancel():
                    return True
                return bool(self.cancel and self.cancel())
            except Exception:
                return False
        b = self._task_bucket(self._task_rate())
        if b is not None:
            b.consume(n, cancel=_c)
        gb = getattr(self.eng, "gbucket", None)
        if gb is not None and gb.rate:
            gb.consume(n, cancel=_c)


# ------------------------------------------------------------------ 小工具
def _host(url):
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _proxy_for(url):
    """返回 (proxies dict 或 None, trust_env)。回环直连；SOCKS 无 PySocks 抛 Fallback。"""
    if _host(url) in LOOPBACK:
        return None, False
    v = _V()
    px = None
    if v is not None and hasattr(v, "get_system_proxy"):
        try:
            px = v.get_system_proxy()
        except Exception:
            px = None
    if not px:
        return None, True          # 读环境变量（含 NO_PROXY）
    if str(px).lower().startswith("socks"):
        try:
            import socks  # noqa: F401  PySocks
        except Exception:
            raise Fallback("SOCKS 代理需要 PySocks，未安装")
    return {"http": px, "https": px}, False


ARIA2_HARD = 16          # aria2 1.37 max-connection-per-server 硬顶
SDL_HARD = 128           # 自研引擎绝对上限（= sdl_speed.profiles.HARD_MAX）
AUTO_MAX = 64            # 「最大连接数=自动」时普通站点的自动上限（快主机 128，见 sdl_speed.profiles）


def max_conns_setting(eng):
    """设置「最大连接数」（max_conns）：返回 int 上限，自动 / 没设 / 非法 → None。"""
    cfg = getattr(eng, "cfg", None) or {}
    v = str(cfg.get("max_conns") or "自动").strip()
    if v.isdigit() and int(v) > 0:
        return max(1, min(SDL_HARD, int(v)))
    return None


def _profile_for(eng, url, conn, min_split, task):
    """返回 (sdl_speed profile 或 None=自动 8→64（快主机 128）, 规则名)。
    网盘规则 / 推送方 split / 任务手动连接数 / 设置「最大连接数」：照它当上限（都取最小）；普通站点自动加连接。
    网盘规则的防封默认值不变（≤16）；用户在 netdisk_conn 里把某网盘覆盖到 >16 时，自研引擎照覆盖值走（最多 128）。"""
    prof = None
    if ND is not None:
        try:
            prof = ND.match_profile(url)
        except Exception:
            prof = None
    cfg = getattr(eng, "cfg", None) or {}
    cap = None
    gmax = max_conns_setting(eng)
    hint = (task or {}).get("a2opts") or {}
    tc = str((task or {}).get("conns") or "")
    if prof is not None:
        try:
            cap = ND.profile_conn(prof, cfg, gmax, hard_max=SDL_HARD)
        except TypeError:          # 旧版 netdisk_rules 没有 hard_max
            cap = ND.profile_conn(prof, cfg, int(conn) if conn else 16)
        except Exception:
            cap = int(prof.get("conn") or 8)
        min_split = prof.get("min_split") or min_split
    elif gmax is not None:
        cap = gmax
    for k in ("max-connection-per-server", "split"):
        try:
            if hint.get(k):
                cap = min(cap or 9999, int(hint[k]))
        except Exception:
            pass
    if hint.get("min-split-size"):
        min_split = str(hint["min-split-size"])
    if tc.isdigit():
        cap = min(cap or 9999, max(1, int(tc)))
    if cap is None:
        return None, "通用"
    cap = max(1, min(SDL_HARD, int(cap)))
    try:
        ms = SPEED.profiles.parse_size(min_split or "1M")
    except Exception:
        ms = 1024 * 1024
    name = (prof or {}).get("name") or ("最大连接数" if gmax is not None and not tc.isdigit() and
                                        not any(hint.get(k) for k in ("max-connection-per-server", "split"))
                                        else "手动")
    return {"name": name, "max_conn": max(1, int(cap)), "start_conn": min(8, max(1, int(cap))),
            "min_split": ms}, name


def planned_cap(eng, url, conn=None, task=None):
    """这单交给自研引擎时的连接上限（自动模式 = 自动上限 64/128）。"""
    try:
        prof, _ = _profile_for(eng, url, conn, "1M", task)
    except Exception:
        return None
    if prof:
        return int(prof["max_conn"])
    try:
        return int(SPEED.profiles.resolve(url).max_conn) if SPEED is not None else None
    except Exception:
        return AUTO_MAX


def needs_sdl(eng, url, conn=None, task=None):
    """用户/推送方/网盘覆盖【明确要求】>16 路（aria2 做不到）→ 返回该路数，否则 0。
    「最大连接数=自动」不算明确要求：speed_engine=aria2 时仍尊重用户选 aria2。"""
    if SPEED is None:
        return 0
    try:
        prof, pname = _profile_for(eng, url, conn, "1M", task)
    except Exception:
        return 0
    if not prof:
        return 0
    n = int(prof["max_conn"])
    return n if n > ARIA2_HARD else 0


def _discard(out_path):
    for p in (out_path + TMP_SUFFIX, out_path + SIDE_SUFFIX, out_path + SIDE_SUFFIX + ".tmp"):
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


def _fatal(url, headers, status, raw):
    if ND is not None:
        try:
            return RuntimeError(ND.explain_failure(url, status, headers, raw=raw))
        except Exception:
            pass
    return RuntimeError(("HTTP %s " % status if status else "") + str(raw)[:160])


# ------------------------------------------------------------------ run_http：与 _http_aria2c 同参
def _apply_remote_time(eng, path, last_modified):
    """设置「保留服务器文件时间」(remote_time) 开时，用响应的 Last-Modified 设文件 mtime（aria2 路径由 remote-time 选项管）。"""
    try:
        if not last_modified or str((getattr(eng, "cfg", None) or {}).get("remote_time", False)).lower() in ("false", "0", "", "none"):
            return
        from email.utils import parsedate_to_datetime
        ts = parsedate_to_datetime(last_modified).timestamp()
        if os.path.isfile(path):
            os.utime(path, (ts, ts))
    except Exception:
        pass


def run_http(eng, url, outdir, name, headers, prog=None, cancel=None, conn=None, min_split="1M",
             status_cb=None, task=None, stall_timeout=60.0):
    if SPEED is None:
        raise Fallback("sdl_speed 包缺失")
    tl = _TL()
    if task is None and tl is not None:
        task = getattr(tl, "task", None)
    path = os.path.join(outdir, name)
    # aria2 自己的半成品在：让它续，别抢
    for ctl in (path + ".aria2", path + ".sdl.aria2"):
        if os.path.exists(ctl):
            raise Fallback("存在 aria2 续传进度（%s），交回 aria2" % os.path.basename(ctl))
    proxies, trust_env = _proxy_for(url)
    profile, pname = _profile_for(eng, url, conn, min_split, task)
    cancel = cancel or (lambda: bool((task or {}).get("cancel")))
    lim = Limiter(eng, task, cancel)
    try:
        os.makedirs(outdir, exist_ok=True)
    except OSError as e:
        raise RuntimeError("无法创建保存目录：%s" % e)

    last = [0.0]
    phase = [None]

    def _cb(p):
        st = p["state"]
        if status_cb is not None:
            msg = {"probing": "连接服务器中…", "verifying": "校验文件中…"}.get(st)
            if st == "downloading":
                msg = "动态分片下载中（%d 路）" % max(1, p.get("connections") or 1)
            if msg and msg != phase[0]:
                phase[0] = msg
                try:
                    status_cb(msg)
                except Exception:
                    pass
        if prog is None or st not in ("downloading", "verifying", "done"):
            return
        now = time.monotonic()
        if st == "downloading" and now - last[0] < 0.4:
            return
        last[0] = now
        try:
            prog({"status": "downloading", "downloaded_bytes": p["bytes"],
                  "total_bytes": p["total"], "speed": int(p["speed"] or 0), "eta": p["eta"],
                  "connections": p["connections"]})
        except Exception:
            pass

    resumed = os.path.exists(path + SIDE_SUFFIX) and os.path.exists(path + TMP_SUFFIX)
    _log(eng, "[直链] 动态分片引擎启动：%s 连接策略=%s%s%s" % (
        name[:60], pname, "（上限 %d）" % profile["max_conn"] if profile else "（自动 8→%d）" % (
            SPEED.profiles.resolve(url).max_conn if SPEED is not None else AUTO_MAX),
        "，从 .sdl 续传" if resumed else ""))
    prev_in_a2 = getattr(tl, "in_aria2", False) if tl is not None else False
    if tl is not None:
        tl.in_aria2 = True        # 外层 _throttled_prog 别再扣一遍
    t0 = time.monotonic()
    dt = None
    try:
        try:
            meth = str((task or {}).get("method") or "GET").upper()
            body = (task or {}).get("post_data") if meth != "GET" else None
            dt = SPEED.start(url, headers, path, _cb, proxies=proxies, verify=False, profile=profile,
                             stall_timeout=stall_timeout, limiter=lim, tmp_suffix=TMP_SUFFIX,
                             side_suffix=SIDE_SUFFIX, trust_env=trust_env, method=meth, data=body,
                             **({"max_probe_retries": 2} if meth != "GET" else {}))   # POST 非幂等：少重试
            while not dt._done_evt.wait(0.2):
                c = False
                try:
                    c = cancel()
                except Exception:
                    c = False
                if c:
                    dt.cancel()           # 保留 .sdl/.sdlpart
                    _log(eng, "[直链] 动态分片已暂停/取消，进度保留（%s）" % name[:60])
                    raise abort_exc()()
            res = dt.result()
            if task is not None and getattr(dt, "cdisp", ""):
                task["_cdisp"] = dt.cdisp
            _apply_remote_time(eng, path, getattr(dt, "last_modified", None))
        except SPEED.CancelledError:
            raise abort_exc()()
        except SPEED.SpeedError as e:
            st = e.status
            expired = False
            if ND is not None:
                try:
                    expired = bool(ND.expiry_state(url)["expired"])
                except Exception:
                    expired = False
            if e.fatal_link or expired:
                _discard(path)
                raise _fatal(url, headers, st, str(e))
            if e.kind == "io" and "空间" in e.message:
                raise RuntimeError("磁盘空间不足：%s" % e.detail[:120])   # _post_run 会改成暂停
            _discard(path)
            raise Fallback("%s [%s]" % (e, e.kind))
        except Exception as e:
            if isinstance(e, (Fallback, RuntimeError)) or e.__class__ is abort_exc():
                raise
            _discard(path)
            raise Fallback("引擎异常：%s: %s" % (type(e).__name__, str(e)[:120]))
    finally:
        if tl is not None:
            tl.in_aria2 = prev_in_a2
    el = max(0.001, time.monotonic() - t0)
    stt = dt.stats if dt is not None else {}
    try:
        size = os.path.getsize(res)
    except OSError:
        size = 0
    if task is not None:
        task["conns_eff"] = str(stt.get("max_conn") or "")
        task["speed_engine"] = "sdl"
    _log(eng, "[直链] 动态分片完成 %s：%.1f MB / %.1fs = %.2f MB/s，最多 %s 路，窃取 %s 次，踢慢连接 %s 次" % (
        name[:60], size / 1048576.0, el, size / 1048576.0 / el, stt.get("max_conn"),
        stt.get("steals"), stt.get("demotions")))
    return res


# ------------------------------------------------------------------ run_direct：整条直链任务
def parse_cdisp(h):
    """Content-Disposition → 文件名（filename*=UTF-8''xx 优先，其次 filename=）；没有返回空串。"""
    import re
    h = h or ""
    m = re.search(r"filename\*\s*=\s*([^\';]*)\'[^\']*\'([^;]+)", h, re.I)
    if m:
        try:
            return unquote(m.group(2).strip().strip('"'), encoding=(m.group(1).strip() or "utf-8"))
        except Exception:
            pass
    m = re.search(r'filename\s*=\s*"((?:[^"\\]|\\.)*)"', h, re.I) or re.search(r"filename\s*=\s*([^;]+)", h, re.I)
    if m:
        n = m.group(1).strip()
        try:
            if any(ord(c) > 127 for c in n):
                n = n.encode("latin-1").decode("utf-8")
        except Exception:
            pass
        return os.path.basename(n.replace("\\", "/"))
    return ""


def _guess_name(eng, t, url):
    name = (t.get("name") or "").strip()
    if not name or "." not in name:
        qn = ""
        try:
            qn = eng._rpc_query_filename(url) or ""
        except Exception:
            qn = ""
        base = qn or unquote(os.path.basename(urlparse(url).path)) or ""
        if "." in base:
            name = base
        else:
            p = urlparse(url).path
            ext = p.rsplit(".", 1)[-1] if "." in os.path.basename(p) else ""
            name = (name or base) + ("." + ext if ext else "")
    v = _V()
    if v is not None and hasattr(v, "safe_name"):
        try:
            name = v.safe_name(name)
        except Exception:
            pass
    else:
        name = "".join("_" if c in '\\/:*?"<>|' else c for c in name).strip()
    return name


def run_direct(eng, t, prog, headers):
    """整条直链任务。prog 必须是【未经 _throttled_prog 包装】的原始进度回调
    （包装了也不会重复扣：运行期间 _TL.in_aria2=True）。"""
    if SPEED is None:
        raise Fallback("sdl_speed 包缺失")
    url = t["uri"]
    outdir = t["dir"]
    name = _guess_name(eng, t, url)
    if not name or not os.path.splitext(name)[1]:
        raise Fallback("推不出文件后缀，交回原流程探测")
    path = os.path.join(outdir, name)
    try:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            _log(eng, "[直链] 文件已存在，跳过重复下载：%s" % name)
            return path
    except OSError:
        pass
    if os.path.exists(path + ".part"):
        raise Fallback("存在旧版 .part 续传进度，交回原流程")
    try:
        if eng._rpc_is_waf_host(url):
            raise Fallback("网盘规则要求单连接")
    except Fallback:
        raise
    except Exception:
        pass
    h = dict(headers or {})
    prof = None
    if ND is not None:
        try:
            prof = ND.match_profile(url)
        except Exception:
            prof = None
        try:
            ex = ND.expiry_state(url)
            if ex["expired"]:
                raise RuntimeError(ND.explain_failure(url, None, h))
        except RuntimeError:
            raise
        except Exception:
            pass
    hget = ND.hget if ND is not None else (lambda d, n: next((v for k, v in d.items() if k.lower() == n.lower()), None))

    def hdef(k, val):
        if val and not hget(h, k):
            h[k] = val

    if not hget(h, "Cookie"):
        try:
            ck, how = eng._auto_cookie_for(url, prof)
            if ck:
                h["Cookie"] = ck
                _log(eng, "[直链] 推送方没给 Cookie，已从%s按域补齐（%d 项）" % (how, ck.count("=")))
        except Exception:
            pass
    if not hget(h, "Referer"):
        ref = (prof or {}).get("referer")
        if not ref:
            try:
                ref = eng._rpc_referer_guess(url)
            except Exception:
                ref = None
        hdef("Referer", ref)
    v = _V()
    hdef("User-Agent", (prof or {}).get("default_ua") or getattr(v, "UA", None) or
         "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
         "Chrome/125.0 Safari/537.36")
    hdef("Accept", "*/*" if prof else "application/octet-stream,*/*;q=0.8")
    if not prof:
        hdef("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8")
    conn = 16
    if v is not None and hasattr(v, "eff_conn_threads"):
        try:
            conn = v.eff_conn_threads()
        except Exception:
            pass
    rl = getattr(eng, "_rpc_lock", None)

    def set_phase(msg):
        try:
            if rl is not None:
                with rl:
                    t["statusMessage"] = msg
            else:
                t["statusMessage"] = msg
        except Exception:
            pass

    if t.get("name") != name:
        try:
            if rl is not None:
                with rl:
                    t["name"] = name
            else:
                t["name"] = name
        except Exception:
            pass
    return run_http(eng, url, outdir, name, h, prog, cancel=lambda: bool(t.get("cancel")), conn=conn,
                    min_split=(prof or {}).get("min_split") or "1M", status_cb=set_phase, task=t)
