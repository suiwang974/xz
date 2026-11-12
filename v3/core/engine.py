# -*- coding: utf-8 -*-
"""SniffDL v3 内核 Engine：无界面后台服务。

做法（行为零回退的关键）：Engine 继承 video_downloader.App，但【不调用】它的 __init__（那里建 Tk 窗口），
只初始化下载/嗅探/BT/记忆/RPC 用得到的状态；直链、视频、网盘、BT、记忆恢复、去重闸门等引擎方法
原样复用同一份代码（BT 子任务的修复自动生效）。所有原来弹 Tk 窗/画 Tk 控件的地方在这里改成「推事件」，
由界面（Electron / pywebview / 浏览器）呈现。

新增能力都挂在几个钩子上：
  _rpc_new_task   每个任务的单独选项（队列、限速、连接数、校验和、格式、BT 选文件/首尾优先…）
  _rpc_run_task   开跑前（磁盘空间、重名策略）/ 跑完后（校验、来自网络标记、杀毒、解压、钩子、Webhook、通知、自动重试）
  _rpc_direct_file / _http_aria2c / _h2_rpc   限速、单任务连接数、低速重连、404 快速失败、未完成后缀、服务器时间
  download_one（模块级包装）   视频站：格式预设、字幕、封面/元数据/章节、下载记录、播放列表范围、限速
  _bt_extra_args / _bt_add     DHT/PEX/LSD/端口、做种上限、首尾优先、选文件、tracker 订阅、元数据缓存
"""
import collections
import copy
import datetime
import fnmatch
import json
import os
import queue
import re
import shutil
import threading
import time
import uuid
from urllib.parse import urlparse

import video_downloader as V

from . import settings as S
from . import naming as NM
from . import batch as BATCH
from . import checksum as CK
from . import errors as ERR
from . import events as EV
from . import headless as HL
from . import postactions as PA
from . import platform_util as PU
from . import queues as Q
from . import ratelimit as RL
from . import recognize as RG
from . import bencode as BC
from . import clipboard as CB
from . import livehls as LIVE
from . import hls as HLS
from . import sidecar as SC
from . import runtime as RT
from . import speed as SPD

RPCM = V.RPC
ND = V.ND
_TL = threading.local()          # 当前线程正在跑的任务（限速/连接数/格式等钩子靠它找到任务）

TASK_OPT_KEYS = ("queue", "hold", "limit_kbps", "conns", "checksum", "format", "subs", "playlist_items",
                 "bt_select", "bt_seq", "after", "live", "category", "name_tpl", "resumable", "cert_error",
                 "size_hint", "sha_from", "method", "post_data")

_ORIG_DOWNLOAD_ONE = V.download_one


# ---------------------------------------------------------------- 视频站下载包装
def _ydl_extra_for(t, cfg):
    """把任务选项 / 设置翻译成 yt-dlp 参数（格式预设、字幕、嵌入、下载记录、播放列表范围、限速）。"""
    ex = {}
    pp = []
    fmt = (t.get("format") or S.get(cfg, "format_preset") or "最佳")
    q = None
    if fmt.endswith("p") and fmt[:-1].isdigit():
        q = fmt[:-1]
    elif fmt == "仅音频 MP3":
        ex["format"] = "bestaudio/best"
        pp.append({"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"})
    elif fmt == "仅音频 M4A":
        ex["format"] = "bestaudio[ext=m4a]/bestaudio/best"
        pp.append({"key": "FFmpegExtractAudio", "preferredcodec": "m4a"})
    elif fmt == "MP4（兼容）":
        ex["format_sort"] = ["vcodec:h264", "acodec:aac", "ext:mp4:m4a"]
        ex["merge_output_format"] = "mp4"
    elif fmt == "MKV":
        ex["merge_output_format"] = "mkv"
    subs = t.get("subs")
    if subs is None:
        subs = S.get(cfg, "subs")
    if subs:
        ex["writesubtitles"] = True
        ex["writeautomaticsub"] = False
        ex["subtitleslangs"] = [x.strip() for x in str(S.get(cfg, "sub_langs") or "zh-Hans,en").split(",") if x.strip()]
        ex["subtitlesformat"] = "srt/best"
        pp.append({"key": "FFmpegSubtitlesConvertor", "format": "srt", "when": "before_dl"})
        if S.get(cfg, "embed_subs"):
            pp.append({"key": "FFmpegEmbedSubtitle", "already_have_subtitle": False})
    if S.get(cfg, "embed_meta") or S.get(cfg, "embed_chapters"):
        pp.append({"key": "FFmpegMetadata", "add_metadata": bool(S.get(cfg, "embed_meta")),
                   "add_chapters": bool(S.get(cfg, "embed_chapters")), "add_infojson": False})
    if S.get(cfg, "embed_thumb"):
        ex["writethumbnail"] = True
        pp.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})
    if pp:
        ex["postprocessors"] = pp
    if S.get(cfg, "ytdl_archive"):
        ex["download_archive"] = os.path.join(V.BASE_DIR, "video_archive.txt")
    if t.get("playlist_items"):
        ex["playlist_items"] = str(t["playlist_items"]).strip()
        ex["noplaylist"] = False
    ex["updatetime"] = bool(S.get(cfg, "remote_time"))
    return ex, q


def _download_one_v3(entry, page_url, outdir, quality, on_progress, extra_opts=None, log=V.noop_log,
                     pp_hook=None, abort=None):
    eng = getattr(_TL, "engine", None)
    t = getattr(_TL, "task", None)
    if eng is None or t is None:
        return _ORIG_DOWNLOAD_ONE(entry, page_url, outdir, quality, on_progress, extra_opts=extra_opts,
                                  log=log, pp_hook=pp_hook, abort=abort)
    ex, q = _ydl_extra_for(t, eng.cfg)
    if extra_opts:
        ex.update(extra_opts)
    lim = eng._task_rate(t)
    if lim:
        ex["ratelimit"] = lim
        ex["external_downloader"] = {}             # 限速时不外挂 aria2c（yt-dlp 自己的 ratelimit 才生效）
    prog = eng._throttled_prog(t, on_progress)
    eng.log("[视频] 格式=%s%s%s" % (t.get("format") or S.get(eng.cfg, "format_preset"),
                                  "，字幕" if ex.get("writesubtitles") else "",
                                  "，限速 %dKB/s" % (lim // 1024) if lim else ""))
    t["_ydl_opts"] = {k: v for k, v in ex.items() if k not in ("logger",)}
    return _ORIG_DOWNLOAD_ONE(entry, page_url, outdir, q or quality, prog, extra_opts=ex,
                              log=log, pp_hook=pp_hook, abort=abort)


V.download_one = _download_one_v3


class Engine(V.App):
    """无界面内核。构造后调 start()。"""

    def __init__(self, runtime_path=None, ui_token=None, host="none", ui_dir=None):
        self.root = HL.HeadlessRoot()
        self.after = self.root.after
        self.q = queue.Queue(maxsize=1)          # 旧 Tk 探针结果队列（内核不消费），满了直接丢
        self.entries = []
        self.busy = False
        self.page_url = ""
        self._last_clip = ""
        self.cfg = V.load_config()
        self._load_v3_keys()
        V.INC_DIR_FN = self._work_dir      # yt-dlp 半成品也进 incomplete_dir（成品由 yt-dlp 移回保存位置）
        self._cfg_migrate_notes = list(V._CFG_MIGRATE_NOTES)
        self.theme = "feiyu"
        self._web_mode = True
        self.cfg["rpc_enable"] = True
        self.host = host
        self.ui_dir = ui_dir
        self.runtime_path = runtime_path
        self.ui_token = ui_token or RT.new_token()
        self.started_ts = time.time()
        # 旧代码引用的 Tk 变量 → 线程安全替身
        self.url_var = HL.Var("")
        self.scope_var = HL.Var(self.cfg.get("scope") or "single")
        self.quality_var = HL.Var(self.cfg.get("quality") or "最佳")
        self.rpc_enable_var = HL.Var(True)
        self.rpc_port_var = HL.Var(str(self.cfg.get("rpc_port") or 6800))
        self.rpc_token_var = HL.Var(str(self.cfg.get("rpc_token") or ""))
        # 引擎状态（与 App.__init__ 同名同义）
        self.pause_evt = threading.Event()
        self.pause_evt.set()
        self._rpc_tasks, self._rpc_order = {}, []
        self._rpc_lock = threading.Lock()
        self._rpc_srv = None
        self._rpc_extra = []
        self._qmem_dirty = False
        self._qmem_last = 0.0
        self._qmem_loaded = False
        self._qmem_retried = False
        self._rpc_prog = {}
        self._rpc_sniff = {}
        self._bt = {"proc": None, "port": 0, "rpc_id": 0}
        self._bt_lock = threading.Lock()
        self._rx_bytes = 0
        self._rx_blocked = False
        self._rx_cap_gb = float(self.cfg.get("rx_cap_gb", 8) or 8)
        self._clip_url = ""
        self._c24_success_until = 0.0
        self._c24_fail_until = 0.0
        self._pet = None
        self.pet_live = False
        self._pet_visible = True
        # v3 新增
        self.bus = EV.EventBus()
        self._log_ring = collections.deque(maxlen=1500)
        self.queues = Q.QueueManager(self.cfg, self._save_cfg)
        self.gbucket = RL.TokenBucket(0)
        self._buckets = {}
        self._eff_limit = None
        self._a2_limit_applied = {}
        self.keepawake = PA.KeepAwake()
        self._views = {}
        self._speed_hist = collections.deque(maxlen=600)
        self._pending_icpt = collections.OrderedDict()
        self._grabber = collections.OrderedDict()
        self._power = None
        self._had_work = False
        self._clip_seen = collections.deque(maxlen=50)
        self._stop = threading.Event()
        self._trackers_cache = {"ts": 0, "list": ""}
        self._watch_seen = set()
        self.clip = CB.ClipWatcher(self._on_clip_text, enabled=lambda: bool(S.get(self.cfg, "clip_watch")))
        self._apply_globals()

    # ================================================================ 生命周期
    def _apply_globals(self):
        c = self.cfg
        V.DASH_ARIA2[0] = bool(c.get("dash_aria2", True))
        V.SNIFF_BROWSER[0] = str(c.get("sniff_browser") or "自动")
        try:
            v = str(c.get("dl_threads") or "自动").strip()
            V.DL_THREADS = 0 if v in ("自动", "auto", "") else int(v)
        except Exception:
            V.DL_THREADS = 0
        try:
            V.set_proxy_setting(c.get("proxy_mode") or "跟随系统", c.get("proxy_manual") or "")
        except Exception:
            pass
        try:
            V.set_cookie_setting(c.get("cookie_browser") or "自动")
        except Exception:
            pass
        try:
            found = V.resolve_aria2c(c)
            if found and c.get("aria2c_path") != found:
                c["aria2c_path"] = found
                self._save_cfg()
        except Exception:
            pass
        try:
            V.resolve_js_runtime(c)
        except Exception:
            pass

    def start(self, port=None):
        """启动 RPC（aria2 兼容 + sniffdl.* + /events），恢复任务库，写运行时握手。返回实际端口。"""
        if port:
            self.cfg["rpc_port"] = int(port)
        self._rpc_start()
        if self._rpc_srv is None:
            raise RuntimeError("RPC 端口全部被占用，内核无法启动")
        self._qmem_restore()
        self._setup_pet()
        self.log("SniffDL 内核 v%s 启动 | yt-dlp %s | ffmpeg %s | aria2c %s | 宿主=%s" % (
            RT.VERSION, getattr(V.yt_dlp.version, "__version__", "?"), "√" if V.FFMPEG else "×",
            "√" if V.resolve_aria2c(self.cfg) else "×", self.host))
        try:
            self._safety_selfcheck()
        except Exception:
            pass
        threading.Thread(target=self._tick_loop, name="Core-Tick", daemon=True).start()
        threading.Thread(target=self._slow_loop, name="Core-Slow", daemon=True).start()
        RT.write(self.runtime_path, self.port, self.ui_token,
                 {"rpc_token": bool(self.cfg.get("rpc_token")), "host": self.host})
        self.bus.emit("hello", {"port": self.port, "version": RT.VERSION})
        return self.port

    @property
    def port(self):
        return self._rpc_srv.bound_port if self._rpc_srv else 0

    def shutdown(self):
        self._stop.set()
        try:
            self._qmem_flush(force=True)
        except Exception:
            pass
        for t in list(self._rpc_tasks.values()):
            if t.get("status") in ("active",):
                t["cancel"] = True
        try:
            self._bt_shutdown()
        except Exception:
            pass
        try:
            if self._pet and self.host != "electron":
                self._pet.quit()
        except Exception:
            pass
        # 每一步各自兜底：Windows 实测 B8——宿主被 taskkill /F、内核带着在下的任务自退时，
        # _rpc_stop 等一步抛错会让 runtime.json 留在磁盘上（后续启动被误认成「内核还活着」）。
        # runtime.json 必须无论如何删掉，放在最先一个独立步骤的 finally 里。
        try:
            self._rpc_stop()
        except Exception:
            pass
        finally:
            RT.remove(self.runtime_path)
        for _step in (lambda: self.keepawake.set(False), lambda: self.root.stop()):
            try:
                _step()
            except Exception:
                pass

    def _load_v3_keys(self):
        """V.load_config 只保留旧版 _CFG_DEFAULT 里的键 → v3 设置表新增的键和 queues 重启后会丢。
        这里把 config.json 里 schema 认识的键（逐个过 validate，坏值回落默认）和 queues 补回来。"""
        try:
            with open(V.CONFIG_FILE, encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            return
        for k, v in raw.items():
            if k in self.cfg or not S.spec(k):
                continue
            try:
                self.cfg[k] = S.validate(k, v)
            except Exception:
                pass
        if isinstance(raw.get("queues"), list) and raw["queues"]:
            self.cfg["queues"] = raw["queues"]

    def _save_cfg(self):
        try:
            V.save_config(self.cfg)
        except Exception:
            pass

    def _setup_pet(self):
        """桌宠：Electron 宿主自己带桌宠窗（同一运行时），内核只连它的控制端口；
        其它宿主（pywebview 后备）沿用 pet_bridge 单独拉起桌宠进程。"""
        want = bool(S.get(self.cfg, "pet_live")) and not V._CLI_NO_PET
        port = os.environ.get("SNIFFDL_PET_PORT")
        tok = os.environ.get("SNIFFDL_PET_TOKEN")
        try:
            import pet_bridge
            if port and tok:
                self._pet = pet_bridge.PetBridge(port=int(port), wake=want, attach=(int(port), tok))
                self.pet_live = True
                if not want:
                    self._pet.visible(False)
            elif want and self.host != "electron":
                self._pet = pet_bridge.PetBridge(port=int(self.cfg.get("pet_port") or 6801), wake=True)
                self._pet.start()
                self.pet_live = True
        except Exception as e:
            self.log("[桌宠] 连接失败：%s" % str(e)[:100])
            self._pet = None
            self.pet_live = False

    # ================================================================ 被覆盖的 UI 钩子
    def log(self, msg):
        msg = str(msg)
        if msg.startswith("[RPC] 稍后下载 ") and not getattr(_TL, "user_defer", False):
            msg = msg.replace("[RPC] 稍后下载 ", "[RPC] 接收任务（进入队列）", 1)
        try:
            V.LOG.info(msg)
        except Exception:
            pass
        rec = {"ts": time.time(), "msg": msg}
        self._log_ring.append(rec)
        try:
            self.bus.emit("log", rec)
        except Exception:
            pass

    def say(self, msg):
        if self.pet_live and self._pet:
            try:
                self._pet.say(msg)
            except Exception:
                pass
        self.bus.emit("say", {"text": str(msg)})

    def apply_theme(self, *a, **k):
        return None

    def _toggle_assistant(self, *a, **k):
        return None

    def load_memory(self, *a, **k):
        return None

    def _c24(self):
        return None

    def _open_progress_window(self, gid, name, outdir):
        self.bus.emit("notify", {"kind": "started", "gid": gid, "title": "开始下载", "body": name})

    def _open_done_window(self, path, name):
        return None

    def _icpt_enqueue(self, info):
        """用户主动点「用 SniffDL 下载」且要确认 → 不再弹 Tk 窗：进「待确认」，推 intercept 事件给界面。"""
        pid = uuid.uuid4().hex[:10]
        info = dict(info)
        info["_pid"] = pid
        info["_ts"] = time.time()
        self._pending_icpt[pid] = info
        self.bus.emit("intercept", {"id": pid, "url": info.get("url"), "filename": info.get("filename") or "",
                                    "size": info.get("size") or 0, "referer": info.get("referer") or "",
                                    "source": info.get("source") or ""})
        self.log("[接管] 等待在界面确认：%s" % (info.get("filename") or info.get("url") or "")[:80])

    def rpc_pick_dir(self, title):
        return {"ok": False, "path": "", "need_host": True, "error": "由界面宿主弹文件夹选择框"}

    def _reveal_in_explorer(self, path):
        return PU.reveal_path(path)

    def rpc_reveal(self, gid):
        full, d = self._rpc_path_of(gid)
        if full and os.path.exists(full):
            PU.reveal_path(full)
            return {"ok": True, "path": full}
        if d and os.path.isdir(d):
            PU.open_path(d)
            return {"ok": True, "path": d}
        return {"ok": False, "path": full or d or "", "error": "文件不在了"}

    def rpc_open_file(self, gid):
        full, d = self._rpc_path_of(gid)
        if full and os.path.exists(full):
            PU.open_path(full)
            return {"ok": True, "path": full}
        return {"ok": False, "error": "文件不在了"}

    def rpc_open_outdir(self, path=None):
        d = os.path.expanduser(path or self.cfg.get("outdir") or V.DEFAULT_OUTDIR)
        os.makedirs(d, exist_ok=True)
        PU.open_path(d)
        return {"ok": True, "path": d}

    def rpc_stats(self):
        """覆盖旧版：账本 record_download 写的是 n，旧 rpc_stats 读 count → 永远 0。这里两个键都认。"""
        out = super().rpc_stats()
        try:
            dmap = V.load_stats()
            days = {k: {"bytes": int(v.get("bytes", 0)), "count": int(v.get("n", v.get("count", 0)) or 0)}
                    for k, v in dmap.items() if isinstance(v, dict)}
            today = datetime.date.today().isoformat()
            out["days"] = days
            out["total_bytes"] = sum(v["bytes"] for v in days.values())
            out["total_count"] = sum(v["count"] for v in days.values())
            out["today_bytes"] = (days.get(today) or {}).get("bytes", 0)
            out["today_count"] = (days.get(today) or {}).get("count", 0)
        except Exception:
            pass
        return out

    def rpc_list_dirs(self, path):
        if not (path or "").strip():
            return {"ok": True, "path": "", "parent": "", "dirs": [], "drives": PU.drives()}
        return super().rpc_list_dirs(path)

    def get_autostart(self):
        if os.name != "nt":
            return bool(self.cfg.get("autostart"))
        return super().get_autostart()

    def set_autostart(self, enable):
        if os.name != "nt":
            self.cfg["autostart"] = bool(enable)
            self._save_cfg()
            return True
        return super().set_autostart(enable)

    def _autostart_cmd(self):
        """开机自启：启动「宿主」（便携包里的 启动 SniffDL.vbs 或 SniffDL.exe --tray），不是只开内核。"""
        base = os.path.dirname(os.path.abspath(V.sys.executable)) if getattr(V.sys, "frozen", False) else V.BASE_DIR
        vbs = os.path.join(base, "启动 SniffDL.vbs")
        if os.path.isfile(vbs):
            return 'wscript.exe "%s" --tray' % vbs
        return super()._autostart_cmd()

    def rpc_clip_take(self, url):
        u = (url or self._clip_url or "").strip()
        if not u:
            return {"ok": False, "error": "无链接"}
        self._clip_url = ""
        self._last_clip = u
        return {"ok": True, "url": u, "inspect": self.inspect(u)}

    # ================================================================ 任务选项注入
    def _rpc_start_from_info(self, info, outdir, name):
        """扩展接管 POST 下载：info 带 method=POST/post_data 时注入任务选项（走 sdl 分片，不探测）。
        info 带 mime=application/x-bittorrent（浏览器 DownloadItem.mime）→ 无后缀链接也按种子归 BT。"""
        if self._is_torrent_hint(info.get("filename") or name or "", info.get("mime") or ""):
            self.__dict__.setdefault("_torrent_uris", set()).add((info.get("url") or "").strip())
        if str(info.get("method") or "").upper() == "POST":
            _TL.add_opts = {"method": "POST", "post_data": info.get("post_data") or ""}
            try:
                return super()._rpc_start_from_info(info, outdir, name)
            finally:
                _TL.add_opts = None
        return super()._rpc_start_from_info(info, outdir, name)

    @staticmethod
    def _is_torrent_hint(name, mime=""):
        """推送方给的文件名是 *.torrent，或 Content-Type 是 application/x-bittorrent → 是种子。"""
        return (name or "").strip().lower().endswith(".torrent") or \
            (mime or "").split(";")[0].strip().lower() == "application/x-bittorrent"

    def _rpc_is_bt(self, uri):
        """在 v2 规则（磁力 / 路径或参数含 .torrent / infohash 直链）之外：无后缀的种子直链
        （网盘/论坛 attachment.php?aid=…），推送时文件名是 .torrent 的，也归 BT。"""
        if super()._rpc_is_bt(uri):
            return True
        u = (uri or "").strip()
        if not u.lower().startswith(("http://", "https://", "ftp://")):
            return False
        reg = self.__dict__.get("_torrent_uris")
        if reg and u in reg:
            return True
        for t in list((self.__dict__.get("_rpc_tasks") or {}).values()):   # 重启恢复的任务：按记下的名字认
            if isinstance(t, dict) and t.get("uri") == u and self._is_torrent_hint(t.get("name"), t.get("mime")):
                self.__dict__.setdefault("_torrent_uris", set()).add(u)
                return True
        return False

    def _rpc_new_task(self, gid, uri, name, outdir, referer, headers, src="user"):
        opts0 = getattr(_TL, "add_opts", None) or {}
        if self._is_torrent_hint(name, opts0.get("mime") or ""):
            self.__dict__.setdefault("_torrent_uris", set()).add((uri or "").strip())
        t = super()._rpc_new_task(gid, uri, name, outdir, referer, headers, src=src)
        opts = getattr(_TL, "add_opts", None) or {}
        for k in TASK_OPT_KEYS:
            if k in opts and opts[k] not in (None, ""):
                t[k] = opts[k]
        t.setdefault("queue", Q.MAIN)
        if self.queues.get(t["queue"]) is None:
            t["queue"] = Q.MAIN
        q = self.queues.get(t["queue"]) or {}
        if not q.get("auto", True):
            t["hold"] = True
        # 文件名模板（只改有后缀的名字；视频站名字由 yt-dlp 决定）
        tpl = opts.get("name_tpl") or S.get(self.cfg, "name_template")
        if name and "." in name and tpl and tpl != "{name}" and not opts.get("keep_name"):
            t["name"] = NM.apply_template(tpl, name, uri, os.path.basename(outdir or ""))
        return t

    def _category_of(self, name_or_uri):
        if not S.get(self.cfg, "auto_category"):
            return ""
        url = getattr(_TL, "add_url", "") or name_or_uri
        return NM.category_for(name_or_uri, url, S.get(self.cfg, "category_rules"), S.get(self.cfg, "site_rules"))

    def rpc_add_uri(self, uri, options, defer=False, src=""):
        """所有入口（aria2 推送 / 扩展接管 / 界面添加）统一进队列：先登记为 waiting，由队列按「同时下载数」开跑。"""
        _TL.add_url = uri
        _TL.user_defer = bool(defer)
        try:
            gid = super().rpc_add_uri(uri, options, defer=True, src=src)
        finally:
            _TL.add_url = ""
        t = self._rpc_tasks.get(gid)
        if t is not None and not t.get("_hooked"):
            t["_hooked"] = True
            if defer:
                t["hold"] = True
            self._hook("added", t)
            self._kick()
        return gid

    # ================================================================ 运行包装
    def _rpc_run_task(self, gid):
        t = self._rpc_tasks.get(gid)
        if not t:
            return
        _TL.task, _TL.engine = t, self
        t["_run_n"] = int(t.get("_run_n") or 0) + 1
        t.pop("retry_at", None)
        try:
            if not self._pre_run(t):
                return
            self._hook("start", t)
            if t.get("live"):
                self._run_live(t)
            elif self._sidecar_wanted(t) and self._run_sidecar(t):
                pass
            elif self._hls_wanted(t) and self._run_hls(t):
                pass
            else:
                super()._rpc_run_task(gid)
        finally:
            _TL.task = None
            self._buckets.pop(gid, None)
        try:
            self._post_run(t)
        except Exception as e:
            self.log("[完成后] 处理异常：%s" % str(e)[:120])

    def _pre_run(self, t):
        """开跑前：磁盘空间预检、重名策略。返回 False = 本次不跑（状态已写好）。"""
        d = t.get("dir") or self.cfg.get("outdir") or V.DEFAULT_OUTDIR
        try:
            os.makedirs(d, exist_ok=True)
        except Exception as e:
            self._fail(t, "无法创建保存目录：%s" % str(e)[:80])
            return False
        need = int(t.get("size_hint") or t.get("totalLength") or 0) - int(t.get("downloadedLength") or 0)
        if S.get(self.cfg, "disk_check") and need > 0:
            try:
                free = shutil.disk_usage(d).free
                if free < need + 64 * 1024 * 1024:
                    with self._rpc_lock:
                        t["status"] = "paused"
                        t["note"] = "磁盘空间不足：还需 %s，只剩 %s" % (V.fmt_size(need), V.fmt_size(free))
                        t["errorMessage"] = t["note"]
                    self.log("[磁盘] %s → 不开始（%s）" % ((t.get("name") or "")[:40], t["note"]))
                    self.bus.emit("notify", {"kind": "error", "gid": t["gid"], "title": "磁盘空间不足",
                                             "body": t["note"]})
                    return False
            except Exception:
                pass
        name = t.get("name") or ""
        pol = S.get(self.cfg, "conflict")
        if name and "." in name and not self._rpc_is_bt(t.get("uri") or "") and not t.get("live"):
            target = os.path.join(d, V.safe_name(name))
            if os.path.isfile(target) and os.path.getsize(target) > 0 and not t.get("_conflict_done"):
                t["_conflict_done"] = True
                if pol == "重命名":
                    nn = os.path.basename(NM.unique_path(target))
                    with self._rpc_lock:
                        t["name"] = nn
                    self.log("[重名] 已有 %s → 改名为 %s" % (name, nn))
                elif pol == "覆盖":
                    try:
                        os.remove(target)
                        for ext in (".aria2", ".part", ".sdl", ".sdl.aria2", ".sdlpart"):
                            if os.path.exists(target + ext):
                                os.remove(target + ext)
                        self.log("[重名] 覆盖旧文件 %s" % name)
                    except Exception as e:
                        self.log("[重名] 覆盖失败：%s" % str(e)[:80])
        return True

    def _fail(self, t, msg):
        with self._rpc_lock:
            t["status"] = "error"
            t["errorCode"] = "1"
            t["errorMessage"] = msg
        self._qmem_mark(force=True)

    def _post_run(self, t):
        st = t.get("status")
        gid = t["gid"]
        if st == "complete" and t.get("path") and os.path.isfile(t["path"]):
            path = t["path"]
            spec = t.get("checksum")
            if spec:
                with self._rpc_lock:
                    t["statusMessage"] = "校验中…"
                    t["_verifying"] = True
                ok, algo, got = CK.verify(path, spec)
                t.pop("_verifying", None)
                if ok is False:
                    with self._rpc_lock:
                        t["status"] = "error"
                        t["errorCode"] = "32"
                        t["errorMessage"] = "校验失败（%s 不一致）：期望 %s，实际 %s" % (
                            algo, CK.parse(spec)[1][:16], (got or "")[:16])
                        t["checksum_ok"] = False
                    self._qmem_mark(force=True)
                    self.log("[校验] ✗ %s %s" % (os.path.basename(path), t["errorMessage"]))
                elif ok:
                    t["checksum_ok"] = True
                    t["statusMessage"] = "%s 校验通过" % algo
                    self.log("[校验] ✓ %s %s=%s" % (os.path.basename(path), algo, got[:16]))
        st = t.get("status")
        if st == "complete":
            path = t.get("path") or ""
            if path and os.path.isfile(path):
                if S.get(self.cfg, "motw"):
                    PA.mark_of_web(path, t.get("uri") or "", t.get("referer") or "")
                if S.get(self.cfg, "av_scan"):
                    PA.av_scan(path, S.get(self.cfg, "av_cmd"), self.log)
                if S.get(self.cfg, "auto_extract") and PA.is_archive(path):
                    pws = [x.strip() for x in (S.get(self.cfg, "extract_passwords") or "").splitlines() if x.strip()]
                    ok, dest, msg = PA.extract(path, pws, bool(S.get(self.cfg, "extract_delete")), self.log)
                    t["extracted"] = dest if ok else ""
                    t["extract_msg"] = msg
            after = t.get("after")
            if after == "open" and path:
                PU.open_path(path)
            elif after == "folder" and path:
                PU.reveal_path(path)
            self._hook("complete", t)
            self.bus.emit("notify", {"kind": "done", "gid": gid, "title": "下载完成",
                                     "body": t.get("name") or os.path.basename(path)})
        elif st == "error":
            msg = t.get("errorMessage") or ""
            low = msg.lower()
            if "no space" in low or "enospc" in low or "磁盘" in msg or str(t.get("errorCode")) == "9":
                with self._rpc_lock:
                    t["status"] = "paused"
                    t["note"] = "磁盘满了，已暂停（进度保留），腾出空间后点继续"
                self._qmem_mark(force=True)
                self.bus.emit("notify", {"kind": "error", "gid": gid, "title": "磁盘空间不足", "body": t["note"]})
                return
            n = int(t.get("_retries") or 0)
            if n < int(S.get(self.cfg, "retry_count") or 0) and ERR.retryable(msg) and not t.get("cancel"):
                wait = int(S.get(self.cfg, "retry_wait") or 10)
                with self._rpc_lock:
                    t["_retries"] = n + 1
                    t["status"] = "waiting"
                    t["retry_at"] = time.time() + wait
                    t["note"] = "%d 秒后自动重试（第 %d 次）：%s" % (wait, n + 1, ERR.friendly(msg)["title"])
                    t["thread"] = None
                self._qmem_mark(force=True)
                self.log("[重试] %s %d 秒后第 %d 次重试（%s）" % ((t.get("name") or "")[:40], wait, n + 1, msg[:60]))
                return
            self._hook("error", t)
            f = ERR.friendly(msg)
            self.bus.emit("notify", {"kind": "error", "gid": gid, "title": f["title"],
                                     "body": (t.get("name") or "") + " · " + f["hint"]})

    def _hook(self, event, t):
        cmd = S.get(self.cfg, "hook_" + event)
        if cmd:
            PA.run_hook(cmd, t, self.log)
        if event in ("complete", "error", "seed_done"):
            PA.webhook(S.get(self.cfg, "webhook_url"), event, t, self.log)

    # ---------------- 限速
    def _eff_global_kbps(self):
        c = self.cfg
        if S.get(c, "turtle"):
            return int(S.get(c, "turtle_kbps") or 0)
        lim = int(S.get(c, "speed_limit_kbps") or 0)
        if lim and S.get(c, "fullspeed_window") and Q.in_window(S.get(c, "fullspeed_start"), S.get(c, "fullspeed_end")):
            return 0
        return lim

    def _task_rate(self, t):
        """单任务实际限速（字节/秒）：单任务限速 ∧ 全局限速，取小；0 = 不限。"""
        a = int(t.get("limit_kbps") or 0) * 1024
        g = self._eff_global_kbps() * 1024
        vals = [x for x in (a, g) if x > 0]
        return min(vals) if vals else 0

    def _throttled_prog(self, t, prog):
        gid = t["gid"]
        last = {"n": None}

        def wrapped(d):
            try:
                if isinstance(d, dict) and d.get("status") == "downloading" and not getattr(_TL, "in_aria2", False) \
                        and not t.get("_spd_active"):
                    n = int(d.get("downloaded_bytes") or 0)
                    if last["n"] is not None and n > last["n"]:
                        delta = n - last["n"]
                        a = int(t.get("limit_kbps") or 0) * 1024
                        if a:
                            b = self._buckets.get(gid)
                            if b is None or b.rate != a:
                                b = self._buckets[gid] = RL.TokenBucket(a)
                            b.consume(delta, cancel=lambda: t.get("cancel"))
                        if self.gbucket.rate:
                            self.gbucket.consume(delta, cancel=lambda: t.get("cancel"))
                    last["n"] = n
            except Exception:
                pass
            if prog:
                return prog(d)
        return wrapped

    def _is_post(self, t):
        return str((t or {}).get("method") or "GET").upper() not in ("GET", "")

    def _rpc_is_direct_file(self, uri, name=""):
        t = getattr(_TL, "task", None)
        if t is not None and self._is_post(t) and (t.get("uri") or "") == uri:
            return True               # POST 下载只有直链引擎会发请求体，别交给 yt-dlp
        return super()._rpc_is_direct_file(uri, name)

    def _post_direct_file(self, t, prog, headers):
        """POST 下载（表单导出、一次性下载接口）：只走动态分片引擎，每条连接重发同一请求体 + Range。
        不做任何事先探测（一次性链接第一次请求就会被消耗），也不回退 aria2 / 单连接（它们只会 GET）。"""
        url = t["uri"]
        outdir = t["dir"]
        name = SPD._guess_name(self, t, url) or ("%s.bin" % t.get("gid"))
        auto_name = False
        if "." not in name:
            name += ".bin"
            auto_name = True
        try:
            with self._rpc_lock:
                t["name"] = name
        except Exception:
            pass
        work = self._work_dir(outdir)
        final = os.path.join(outdir, name)
        self.log("[直链] POST 下载：%s（请求体 %d 字节）" % (name[:60], len(t.get("post_data") or "")))
        t["_spd_active"] = True
        try:
            res = SPD.run_http(self, url, work, name, headers, self._throttled_prog(t, prog),
                               lambda: bool(t.get("cancel")), None, "1M", None, task=t)
        except SPD.Fallback as e:
            raise RuntimeError("POST 下载失败：%s（POST 链接不能换引擎重试，请回网页重新点下载）" % str(e)[:150])
        finally:
            t.pop("_spd_active", None)
        cd = SPD.parse_cdisp(t.pop("_cdisp", ""))
        if cd and (auto_name or not (t.get("name") or "").strip() or "." not in (t.get("name") or "")):
            try:
                cd = SPD._guess_name(self, {"name": cd}, url)
                nf = os.path.join(outdir, cd)
                if cd and os.path.abspath(nf) != os.path.abspath(res):
                    res = self._move_done(res, nf) if os.path.dirname(os.path.abspath(res)) != os.path.dirname(os.path.abspath(nf)) else (os.replace(res, nf) or nf)
                    with self._rpc_lock:
                        t["name"] = cd
                    return res
            except Exception as e:
                self.log("[直链] 按 Content-Disposition 改名失败：%s" % str(e)[:80])
        return self._move_done(res, final)

    def _rpc_direct_file(self, t, prog, headers):
        if self._is_post(t):
            return self._post_direct_file(t, prog, headers)
        try:
            return super()._rpc_direct_file(t, self._throttled_prog(t, prog), headers)
        except V._AbortSignal:
            # speed_engine=sdl 时动态分片失败：_http_aria2c 用 _AbortSignal + _hard_fail 跳出原函数的
            # 「回退单连接」分支，这里换回普通异常 → 任务记为失败（不是暂停）
            msg = t.pop("_hard_fail", None)
            if msg:
                raise RuntimeError(msg)
            raise

    # ---------------- 提速引擎 / 未完成目录 / 续传策略（WP1 接线 core/speed.py，2026-11-09）
    TEMP_SUFFIXES = (".part", ".aria2", ".sdl", ".sdl.aria2", ".sdlpart", ".sdlpart.tmp")

    def _work_dir(self, outdir):
        """incomplete_dir：未完成文件放这里，下完再移到保存位置。留空 / 与保存位置相同 / 建不了 → 直接用保存位置。"""
        inc = (S.get(self.cfg, "incomplete_dir") or "").strip()
        if not inc:
            return outdir
        inc = os.path.abspath(os.path.expanduser(inc))
        try:
            if os.path.abspath(outdir) == inc:
                return outdir
            os.makedirs(inc, exist_ok=True)
            return inc
        except Exception as e:
            self.log("[下载] 未完成目录不可用（%s），直接下到保存位置" % str(e)[:80])
            return outdir

    def _temp_paths(self, t):
        """任务可能留下的临时文件（保存位置 + 未完成目录 两处）。"""
        nm = t.get("name") or ""
        if not nm:
            return []
        nm = V.safe_name(nm)
        dirs = [t.get("dir") or self.cfg.get("outdir") or V.DEFAULT_OUTDIR]
        wd = self._work_dir(dirs[0])
        if wd != dirs[0]:
            dirs.append(wd)
        out = []
        for d in dirs:
            base = os.path.join(d, nm)
            out += [base + s for s in self.TEMP_SUFFIXES]
            if d != dirs[0]:
                out.append(base)          # 未完成目录里的同名文件只可能是半成品（无后缀模式）
        return out

    def _resume_probe(self, url, headers, start):
        """resume_policy=always 且已有 .sdl 进度：先问服务器认不认 Range。True=认 / False=不认 / None=没问到。"""
        try:
            h = dict(headers or {})
            h["Range"] = "bytes=%d-" % max(1, int(start or 1))
            prox = None
            try:
                px = V.get_system_proxy()
                if px and urlparse(url).hostname not in SPD.LOOPBACK:
                    prox = {"http": px, "https": px}
            except Exception:
                prox = None
            r = V.SESSION.get(url, headers=h, stream=True, timeout=15, verify=False, proxies=prox)
            code = r.status_code
            r.close()
            if code == 206:
                return True
            if code == 200:
                return False
        except Exception:
            pass
        return None

    def _sdl_resume_start(self, path):
        try:
            with open(path + SPD.SIDE_SUFFIX, encoding="utf-8") as f:
                rs = json.load(f).get("ranges") or []
            return int(rs[0][0]) if rs else 1
        except Exception:
            return 1

    def _http_aria2c(self, url, outdir, name, headers, prog=None, cancel=None, conn=None, min_split="1M",
                     status_cb=None):
        """外层兜底：动态分片或 aria2 任何一处报磁盘满，都直接让任务停，不交给 v2 回退单连接重下。"""
        try:
            return self._http_aria2c_body(url, outdir, name, headers, prog, cancel, conn, min_split, status_cb)
        except V._AbortSignal:
            raise
        except (OSError, RuntimeError) as e:
            if self._is_diskfull(e):
                self.log("[直链] 磁盘写满，直接停止（不换单连接重下）：%s" % str(e)[:90])
                self._hard_fail(getattr(_TL, "task", None), "磁盘空间不足，已停止：" + str(e)[:80])
            raise

    def _http_aria2c_body(self, url, outdir, name, headers, prog=None, cancel=None, conn=None, min_split="1M",
                          status_cb=None):
        t = getattr(_TL, "task", None)
        work = self._work_dir(outdir)
        final = os.path.join(outdir, name)
        wpath = os.path.join(work, name)
        pol = S.get(self.cfg, "resume_policy") or "auto"
        if pol == "restart":
            gone = []
            if os.path.exists(wpath + ".aria2") and os.path.isfile(wpath):
                try:
                    os.remove(wpath)          # 无后缀模式：有 .aria2 控制文件说明同名文件是半成品
                    gone.append("(半成品)")
                except OSError:
                    pass
            for suf in (".sdl", ".sdlpart", ".sdlpart.tmp", ".aria2", ".sdl.aria2"):
                if os.path.exists(wpath + suf):
                    try:
                        os.remove(wpath + suf)
                        gone.append(suf)
                    except OSError:
                        pass
            if work != outdir and os.path.isfile(wpath):
                try:
                    os.remove(wpath)          # 未完成目录里的无后缀半成品
                    gone.append("(半成品)")
                except OSError:
                    pass
            if gone:
                self.log("[续传] 策略=总是从头：丢弃旧进度 %s（%s）" % (" ".join(gone), name[:60]))
        m = SPD.mode(self)
        if m == "aria2" and SPD.available():
            need = SPD.needs_sdl(self, url, conn, t)
            if need:
                # A15：要 >16 路时 aria2 做不到（1.37 硬顶 16），先走自研引擎，失败仍回退 aria2
                self.log("[直链] 需要 %d 路 > aria2 上限 16，改走动态分片引擎（失败回退 aria2）" % need)
                m = "auto"
        if m != "aria2":
            try:
                if pol == "always" and os.path.exists(wpath + SPD.SIDE_SUFFIX) and os.path.exists(wpath + SPD.TMP_SUFFIX):
                    ok = self._resume_probe(url, headers, self._sdl_resume_start(wpath))
                    if ok is False:
                        self._hard_fail(t, "服务器不支持断点续传；续传策略为「始终续传」，已保留已下部分（改成 auto 可从头下）")
                if t is not None:
                    t["_spd_active"] = True       # 进度回调在 sdl_speed 自己的线程里跑，_TL.in_aria2 管不到，靠它防重复扣限速
                try:
                    res = SPD.run_http(self, url, work, name, headers, prog, cancel, conn, min_split, status_cb, task=t)
                finally:
                    if t is not None:
                        t.pop("_spd_active", None)
                return self._move_done(res, final)
            except SPD.Fallback as e:
                if m == "sdl":
                    msg = "动态分片失败：%s" % str(e)[:150]
                    self.log("[直链] " + msg + "（提速引擎=只用动态分片，不回退 aria2）")
                    self._hard_fail(t, msg)
                self.log("[直链] 动态分片不接（%s），回退 aria2c…" % str(e)[:90])
                if status_cb:
                    try:
                        status_cb("动态分片失败，改用 aria2c…")
                    except Exception:
                        pass
            # _AbortSignal（暂停/取消）与 RuntimeError（404/410/过期/磁盘满）原样上抛
        # aria2 不认 sdl 的预分配半成品：有 .sdlpart 说明 .sdl 是动态分片留下的，交给 aria2 前删掉
        if os.path.exists(wpath + SPD.SIDE_SUFFIX):
            for suf in (SPD.TMP_SUFFIX, SPD.SIDE_SUFFIX, SPD.SIDE_SUFFIX + ".tmp"):
                try:
                    if os.path.exists(wpath + suf):
                        os.remove(wpath + suf)
                except OSError:
                    pass
            self.log("[直链] 改用 aria2c：丢弃动态分片的进度文件（两种引擎的进度格式不通用）")
        _TL.in_aria2 = True
        suffix = bool(S.get(self.cfg, "incomplete_suffix"))
        if t is not None:
            t["speed_engine"] = "aria2"
        try:
            if not suffix:
                real = super()._http_aria2c(url, work, name, headers, prog, cancel, conn, min_split, status_cb)
                return self._move_done(real, final)
            real = super()._http_aria2c(url, work, name + ".sdl", headers, prog, cancel, conn, min_split, status_cb)
            if real and real.endswith(".sdl"):
                os.replace(real, wpath)
                real = wpath
            return self._move_done(real, final)
        except (OSError, RuntimeError) as e:
            if self._is_diskfull(e):
                self._hard_fail(t, "磁盘空间不足，已停止：" + str(e)[:80])
            raise
        finally:
            _TL.in_aria2 = False

    @staticmethod
    def _is_diskfull(e):
        import errno
        if getattr(e, "errno", None) in (errno.ENOSPC, getattr(errno, "EDQUOT", -1)):
            return True
        m = str(e).lower()
        if any(k in m for k in ("no space left", "disk full", "磁盘空间不足", "磁盘已满", "not enough disk space",
                                "disk cache flush failure")):
            return True
        import re
        return bool(re.search(r"\bcode=9\b", m))       # aria2 errorCode 9 = 磁盘空间不足

    def _hard_fail(self, t, msg):
        """让任务直接失败：原 _rpc_direct_file 对 _http_aria2c 抛出的普通异常会「回退单连接」，
        只有 _AbortSignal 原样上抛，所以借它跳出，再由本类 _rpc_direct_file 换成 RuntimeError(msg)。"""
        if t is not None:
            t["_hard_fail"] = msg
            raise V._AbortSignal()
        raise RuntimeError(msg)

    def _move_done(self, real, final):
        """未完成目录里下好的文件移到保存位置（跨盘用 shutil.move）。"""
        if not real or os.path.abspath(real) == os.path.abspath(final):
            return real
        if os.path.dirname(os.path.abspath(real)) == os.path.dirname(os.path.abspath(final)):
            return real                   # 同目录（aria2 自己改了名之类）：不动
        try:
            os.makedirs(os.path.dirname(final), exist_ok=True)
            if os.path.exists(final):
                os.remove(final)
            shutil.move(real, final)
            self.log("[下载] 已从未完成目录移到保存位置：%s" % os.path.basename(final))
            return final
        except Exception as e:
            raise RuntimeError("下载完成但移到保存位置失败：%s" % str(e)[:120])

    def _h2_rpc(self, method, params):
        t = getattr(_TL, "task", None)
        if method == "aria2.addUri" and t is not None and len(params) >= 2 and isinstance(params[1], dict):
            o = params[1]
            lim = int(t.get("limit_kbps") or 0)
            if lim:
                o["max-download-limit"] = "%dK" % lim
            c = str(t.get("conns") or S.get(self.cfg, "task_conns") or "自动")
            prof = None
            try:
                prof = ND.match_profile(t.get("uri") or "") if ND else None
            except Exception:
                prof = None
            if c.isdigit() and not prof:
                o["split"] = o["max-connection-per-server"] = str(max(1, min(16, int(c))))
            lo = int(S.get(self.cfg, "lowest_speed_kbps") or 0)
            if lo:
                o["lowest-speed-limit"] = "%dK" % lo
            mf = int(S.get(self.cfg, "max_file_not_found") or 0)
            if mf:
                o["max-file-not-found"] = str(mf)
            o["remote-time"] = "true" if S.get(self.cfg, "remote_time") else "false"
            # resume_policy：always = 服务器不认续传就报错保留已下部分；auto/restart = 不认就从头下
            if str((params[0] or [""])[0]).lower().startswith(("http://", "https://")):
                o["always-resume"] = "true" if (S.get(self.cfg, "resume_policy") or "auto") == "always" else "false"
            r = super()._h2_rpc(method, params)
            t["_h2gid"] = str(r)
            t["conns_eff"] = o.get("split")
            return r
        return super()._h2_rpc(method, params)

    @staticmethod
    def split_limit(total_kb, h2_speed, bt_speed, h2_active, bt_active):
        """直链/BT 两个 aria2 守护共享全局限速：只有一边活跃 → 满额；两边都活跃 → 按当前速度比例分，
        每边至少 20%，两份之和恒等于 total_kb。返回 (h2_kb, bt_kb)。"""
        total_kb = int(total_kb)
        if total_kb <= 0:
            return 0, 0
        if h2_active and not bt_active:
            return total_kb, total_kb
        if bt_active and not h2_active:
            return total_kb, total_kb
        if not h2_active and not bt_active:
            return total_kb, total_kb
        tot = float(h2_speed + bt_speed)
        frac = 0.5 if tot <= 0 else h2_speed / tot
        frac = min(0.8, max(0.2, frac))
        h = max(1, int(round(total_kb * frac)))
        return h, max(1, total_kb - h)

    def _limit_shares(self, kb):
        h = b = 0
        ha = ba = False
        try:
            for v in self.tasks():
                if v["status"] != "active":
                    continue
                if v["kind"] == "bt":
                    ba = True
                    b += v["speed"]
                elif v["kind"] in ("file", "video"):
                    ha = True
                    h += v["speed"]
        except Exception:
            pass
        return self.split_limit(kb, h, b, ha, ba)

    def _apply_limits(self, force=False):
        """全局限速变化 → Python 令牌桶 + 两个 aria2 守护进程（直链 / BT）同步；两边同时跑时按需求比例分摊。"""
        kb = self._eff_global_kbps()
        hk, bk = self._limit_shares(kb)
        key = (kb, hk, bk)
        if key == self._eff_limit and not force:
            return
        changed_total = (self._eff_limit is None or self._eff_limit[0] != kb)
        self._eff_limit = key
        self.gbucket.set_rate(hk * 1024 if kb else 0)      # 自研分片/yt-dlp 共用此桶：BT 同时在跑时只拿直链那一份，否则两边各吃满额
        for name, fn, live, share in (("h2", self._h2_rpc_raw, (getattr(self, "_h2", None) or {}).get("port"), hk),
                                      ("bt", self._bt_rpc, (self._bt or {}).get("port"), bk)):
            if not live:
                continue
            val = "%dK" % share if kb else "0"
            try:
                fn("aria2.changeGlobalOption", [{"max-overall-download-limit": val}])
                self._a2_limit_applied[name] = val
            except Exception:
                pass
        if changed_total:
            self.log("[限速] 全局 %s" % ("%d KB/s" % kb if kb else "不限速"))
            self.bus.emit("limit", {"kbps": kb, "turtle": bool(S.get(self.cfg, "turtle"))})

    def _h2_rpc_raw(self, method, params):
        return V.App._h2_rpc(self, method, params)

    def _h2_ensure(self):
        port = super()._h2_ensure()
        if self._a2_limit_applied.get("h2_port") != port:
            self._a2_limit_applied["h2_port"] = port
            kb = self._eff_global_kbps()
            try:
                self._h2_rpc_raw("aria2.changeGlobalOption", [{"max-overall-download-limit": "%dK" % kb if kb else "0"}])
            except Exception:
                pass
        return port

    def task_set_limit(self, gid, kbps):
        t = self._rpc_tasks.get(gid)
        if not t:
            return {"ok": False, "error": "任务不存在"}
        kbps = max(0, int(kbps or 0))
        t["limit_kbps"] = kbps
        val = "%dK" % kbps if kbps else "0"
        for g, fn in ((t.get("_h2gid"), self._h2_rpc_raw), (t.get("btGid"), self._bt_rpc)):
            if g:
                try:
                    fn("aria2.changeOption", [g, {"max-download-limit": val}])
                except Exception:
                    pass
        self._buckets.pop(gid, None)
        self._qmem_mark(force=True)
        return {"ok": True, "gid": gid, "limit_kbps": kbps}

    # ---------------- BT
    def _bt_extra_args(self):
        c = self.cfg
        d = self._bt_datadir()
        a = ["--enable-dht=%s" % ("true" if S.get(c, "bt_dht") else "false"),
             "--enable-dht6=%s" % ("true" if S.get(c, "bt_dht") else "false"),
             "--enable-peer-exchange=%s" % ("true" if S.get(c, "bt_pex") else "false"),
             "--bt-enable-lpd=%s" % ("true" if S.get(c, "bt_lpd") else "false"),
             "--bt-detach-seed-only=true"]
        p = int(S.get(c, "bt_port") or 0)
        if p:
            a += ["--listen-port=%d" % p, "--dht-listen-port=%d" % p]
        if S.get(c, "bt_save_meta"):
            a += ["--bt-save-metadata=true", "--bt-load-saved-metadata=true"]
        st = int(S.get(c, "bt_stop_timeout") or 0)
        if st:
            a.append("--bt-stop-timeout=%d" % st)
        tr = self._tracker_list()
        if tr:
            a.append("--bt-tracker=%s" % tr)
        kb = self._eff_global_kbps()
        if kb:
            a.append("--max-overall-download-limit=%dK" % kb)
        up = int(S.get(c, "bt_upload_limit_kbps") or 0)
        if up:
            a.append("--max-overall-upload-limit=%dK" % up)
        return a

    def _apply_upload_limit(self):
        """BT 上传限速（bt_upload_limit_kbps）热更新到 BT aria2 守护。"""
        up = int(S.get(self.cfg, "bt_upload_limit_kbps") or 0)
        if (self._bt or {}).get("port"):
            try:
                self._bt_rpc("aria2.changeGlobalOption", [{"max-overall-upload-limit": "%dK" % up if up else "0"}])
            except Exception:
                pass
        self.log("[限速] BT 上传 %s" % ("%d KB/s" % up if up else "不限速"))

    def _tracker_list(self):
        c = self.cfg
        extra = [x.strip() for x in (S.get(c, "bt_trackers") or "").replace(",", "\n").splitlines() if x.strip()]
        base = [x for x in V.App._BT_TRACKERS.split(",") if x]
        sub = self._trackers_cache.get("list") or ""
        allv = []
        for x in extra + base + [y for y in sub.split(",") if y]:
            if x not in allv:
                allv.append(x)
        return ",".join(allv[:120])

    def _refresh_trackers(self):
        if not S.get(self.cfg, "bt_tracker_auto"):
            return
        url = (S.get(self.cfg, "bt_tracker_sub") or "").strip()
        if not url or time.time() - self._trackers_cache["ts"] < 12 * 3600:
            return
        self._trackers_cache["ts"] = time.time()
        try:
            r = V.SESSION.get(url, timeout=15)
            if r.status_code == 200:
                lst = [x.strip() for x in r.text.splitlines() if x.strip().startswith(("udp://", "http", "wss://"))]
                self._trackers_cache["list"] = ",".join(lst[:80])
                self.log("[BT] tracker 订阅已更新：%d 个" % len(lst))
        except Exception as e:
            self.log("[BT] tracker 订阅拉取失败（用内置列表）：%s" % str(e)[:60])

    def _bt_add(self, uri, opts, headers=None):
        t = getattr(_TL, "task", None) or {}
        c = self.cfg
        opts = dict(opts or {})
        if S.get(c, "bt_seed"):
            opts["seed-ratio"] = str(float(S.get(c, "bt_seed_ratio") or 0))
            opts["seed-time"] = str(int(S.get(c, "bt_seed_minutes") or 0))
        else:
            opts["seed-time"] = "0"
        if t.get("bt_seq"):
            opts["bt-prioritize-piece"] = "head=8M,tail=8M"
        sel = t.get("bt_select")
        if not sel and uri.lower().endswith(".torrent") and os.path.isfile(uri):
            try:
                info = BC.torrent_info_file(uri)
                sel = self._bt_auto_select(info["files"])
                if sel and len(sel) < len(info["files"]):
                    t["bt_select"] = sel
                    self.log("[BT] 自动不勾选 %d 个广告/填充文件" % (len(info["files"]) - len(sel)))
                else:
                    sel = None
            except Exception:
                sel = None
        if sel:
            opts["select-file"] = ",".join(str(int(i)) for i in sel)
        elif (uri or "").lower().startswith("magnet:") and any(
                x.strip() for x in (S.get(c, "bt_exclude") or "").splitlines()):
            # 磁力：元数据到手前不知道文件清单；让 aria2 取完元数据后把真下载先暂停，
            # _bt_on_followed 按 bt_exclude 改好 select-file 再放行（不然广告文件会被先下一截）
            opts["pause-metadata"] = "true"
        tr = self._tracker_list()
        if tr:
            opts["bt-tracker"] = tr
        lim = int(t.get("limit_kbps") or 0)
        if lim:
            opts["max-download-limit"] = "%dK" % lim
        return super()._bt_add(uri, opts, headers)

    def _bt_on_followed(self, t, new_gid):
        """磁力元数据已取到：按 bt_exclude 给真下载（new_gid）定 select-file，然后放行（它是 pause-metadata 暂停着的）。"""
        paused = False
        try:
            st = self._bt_rpc("aria2.tellStatus", [new_gid, ["status", "files", "dir"]]) or {}
            paused = st.get("status") == "paused"
            d = (st.get("dir") or "").replace("\\", "/").rstrip("/") + "/"
            files = []
            for f in (st.get("files") or []):
                pth = (f.get("path") or "").replace("\\", "/")
                files.append({"index": int(f.get("index") or 0), "path": pth[len(d):] if pth.startswith(d) else pth})
            if t.get("bt_select") or not files:
                return
            sel = self._bt_auto_select(files)
            if sel and len(sel) < len(files):
                self._bt_rpc("aria2.changeOption", [new_gid, {"select-file": ",".join(str(i) for i in sel)}])
                t["bt_select"] = sel
                self.log("[BT] 磁力元数据到手：自动不勾选 %d 个广告/填充文件" % (len(files) - len(sel)))
        finally:
            if paused:
                try:
                    self._bt_rpc("aria2.unpause", [new_gid])
                except Exception:
                    pass

    def _bt_auto_select(self, files):
        pats = [p.strip().lower() for p in (S.get(self.cfg, "bt_exclude") or "").splitlines() if p.strip()]
        keep = []
        for f in files:
            pth = (f.get("path") or "").lower()
            base = pth.rsplit("/", 1)[-1]
            if any(fnmatch.fnmatch(base, p) or fnmatch.fnmatch(pth, p) for p in pats):
                continue
            keep.append(int(f["index"]))
        return keep or [int(f["index"]) for f in files]

    def bt_detail(self, gid):
        t = self._rpc_tasks.get(gid)
        if not t:
            return {"ok": False, "error": "任务不存在"}
        bg = t.get("btGid")
        out = {"ok": True, "gid": gid, "files": [], "peers": [], "infohash": "", "ratio": 0.0,
               "uploaded": int(t.get("uploadLength") or 0), "seeding": False}
        if not bg or not (self._bt or {}).get("port"):
            if (t.get("uri") or "").lower().endswith(".torrent") and os.path.isfile(t.get("uri")):
                try:
                    info = BC.torrent_info_file(t["uri"])
                    sel = set(t.get("bt_select") or [f["index"] for f in info["files"]])
                    out["files"] = [dict(f, selected=f["index"] in sel, done=0) for f in info["files"]]
                    out["infohash"] = info["infohash"]
                except Exception:
                    pass
            return out
        try:
            st = self._bt_rpc("aria2.tellStatus", [bg, ["files", "infoHash", "uploadLength", "completedLength",
                                                        "totalLength", "uploadSpeed", "downloadSpeed", "numSeeders",
                                                        "connections", "seeder", "bittorrent", "status", "numPieces"]]) or {}
            for f in st.get("files") or []:
                out["files"].append({"index": int(f.get("index") or 0), "path": f.get("path") or "",
                                     "length": int(f.get("length") or 0),
                                     "done": int(f.get("completedLength") or 0),
                                     "selected": f.get("selected") == "true"})
            out["infohash"] = st.get("infoHash") or ""
            up, dn = int(st.get("uploadLength") or 0), int(st.get("completedLength") or 0)
            out.update(uploaded=up, ratio=round(up / dn, 3) if dn else 0.0, seeding=st.get("seeder") == "true",
                       upSpeed=int(st.get("uploadSpeed") or 0), downSpeed=int(st.get("downloadSpeed") or 0),
                       seeders=int(st.get("numSeeders") or 0), connections=int(st.get("connections") or 0),
                       comment=((st.get("bittorrent") or {}).get("comment") or ""))
        except Exception as e:
            out["error"] = str(e)[:100]
        _np = 0
        try:
            _np = int((st or {}).get("numPieces") or 0)
        except Exception:
            pass
        try:
            for p in self._bt_rpc("aria2.getPeers", [bg]) or []:
                out["peers"].append({"ip": p.get("ip"), "port": p.get("port"), "down": int(p.get("downloadSpeed") or 0),
                                     "up": int(p.get("uploadSpeed") or 0), "seeder": p.get("seeder") == "true",
                                     "progress": _bitfield_pct(p.get("bitfield") or "", _np)})
        except Exception:
            pass
        return out

    def bt_select(self, gid, indices):
        t = self._rpc_tasks.get(gid)
        if not t:
            return {"ok": False, "error": "任务不存在"}
        idx = sorted({int(i) for i in indices or [] if int(i) > 0})
        if not idx:
            return {"ok": False, "error": "至少选一个文件"}
        t["bt_select"] = idx
        bg = t.get("btGid")
        if bg and (self._bt or {}).get("port"):
            try:
                self._bt_rpc("aria2.changeOption", [bg, {"select-file": ",".join(map(str, idx))}])
            except Exception as e:
                return {"ok": False, "error": "引擎改选失败：%s" % str(e)[:80]}
        self._qmem_mark(force=True)
        return {"ok": True, "selected": idx}

    def bt_set_seq(self, gid, on):
        t = self._rpc_tasks.get(gid)
        if not t:
            return {"ok": False}
        t["bt_seq"] = bool(on)
        bg = t.get("btGid")
        if bg and (self._bt or {}).get("port"):
            try:
                self._bt_rpc("aria2.changeOption", [bg, {"bt-prioritize-piece": "head=8M,tail=8M" if on else ""}])
            except Exception:
                pass
        return {"ok": True, "bt_seq": bool(on)}

    def bt_export_torrent(self, gid):
        """磁力转种子：aria2 --bt-save-metadata 存在保存目录的 <infohash>.torrent → 在文件夹里选中它。"""
        d = self.bt_detail(gid)
        ih = (d.get("infohash") or "").lower()
        t = self._rpc_tasks.get(gid) or {}
        for cand in (os.path.join(t.get("dir") or "", ih + ".torrent"),
                     os.path.join(self.cfg.get("outdir") or V.DEFAULT_OUTDIR, ih + ".torrent")):
            if ih and os.path.isfile(cand):
                PU.reveal_path(cand)
                return {"ok": True, "path": cand}
        return {"ok": False, "error": "还没拿到种子元数据（或已关闭「存一份 .torrent」）"}

    # ---------------- 外挂引擎：N_m3u8DL-RE / BBDown（用户自己装的程序，找不到就当没开）
    def _sidecar_wanted(self, t):
        if t.get("_sc_off"):
            return False
        uri = t.get("uri") or ""
        pt = S.get(self.cfg, "sidecar_paths")
        try:
            path = urlparse(uri).path.lower()
        except Exception:
            return False
        if (path.endswith(".m3u8") or path.endswith(".mpd")) and S.get(self.cfg, "sidecar_m3u8"):
            exe = SC.find("n_m3u8dl", pt)
            if exe:
                t["_sc"] = ("m3u8", exe)
                return True
        if S.get(self.cfg, "sidecar_bili") and SC.is_bili(uri):
            exe = SC.find("bbdown", pt)
            if exe:
                t["_sc"] = ("bili", exe)
                return True
        return False

    def _run_sidecar(self, t):
        """True = 外挂已处理（成功或用户暂停）；False = 失败，调用方回落内置/yt-dlp。"""
        kind, exe = t["_sc"]
        d = t.get("dir") or self.cfg.get("outdir") or V.DEFAULT_OUTDIR
        os.makedirs(d, exist_ok=True)
        name = t.get("name") or ""
        stem = V.safe_name(os.path.splitext(name)[0] if name else "video_%s" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
        label = "N_m3u8DL-RE" if kind == "m3u8" else "BBDown"
        with self._rpc_lock:
            t["status"] = "active"
            t["start_ts"] = time.time()
            t["statusMessage"] = "交给 %s 下载中…" % label
        self._pet_react("busy")
        self.log("[外挂] %s → %s" % (label, exe))

        def on_line(ln):
            m = re.search(r"(\d{1,3}(?:\.\d+)?)%", ln)
            if m:
                with self._rpc_lock:
                    t["statusMessage"] = "%s · %s%%" % (label, m.group(1))
        try:
            if kind == "m3u8":
                c = str(t.get("conns") or S.get(self.cfg, "task_conns") or "自动")
                fp = SC.run_m3u8(exe, t["uri"], d, stem, headers=dict(t.get("headers") or {}),
                                 threads=int(c) if c.isdigit() else 16, cancel=lambda: t.get("cancel"),
                                 log=self.log, on_line=on_line)
            else:
                fp = SC.run_bili(exe, t["uri"], d, cancel=lambda: t.get("cancel"), log=self.log, on_line=on_line)
        except InterruptedError:
            with self._rpc_lock:
                t["status"] = "paused"
                t["downloadSpeed"] = 0
            self._qmem_mark(force=True)
            return True
        except Exception as e:
            self.log("[外挂] %s 失败（%s）→ 回落%s" % (label, str(e)[:100], "内置分段下载" if kind == "m3u8" else " yt-dlp"))
            t["_sc_off"] = True
            return False
        with self._rpc_lock:
            t["path"] = fp
            t["name"] = os.path.basename(fp)
            t["totalLength"] = t["downloadedLength"] = os.path.getsize(fp)
            t["downloadSpeed"] = 0
            t["status"] = "complete"
            t["statusMessage"] = "%s 已完成" % label
            t["done_ts"] = time.time()
        self._qmem_mark(force=True)
        self._rpc_fire_mascot(True)
        return True

    # ---------------- 点播 m3u8 / mpd：内置分段并发下载（A17）
    def _hls_wanted(self, t):
        """点播 m3u8/mpd 且开关打开 → 走内置分段下载；t["_hls_off"] = 本任务已回落过。"""
        if t.get("_hls_off") or not S.get(self.cfg, "hls_native"):
            return False
        try:
            path = urlparse(t.get("uri") or "").path.lower()
        except Exception:
            return False
        return path.endswith(".m3u8") or path.endswith(".mpd")

    def _hls_height(self, t):
        """画质上限：任务的 format 写 720 / 720p / h720 → 720；否则取设置 hls_max_height（0 = 不限）。"""
        import re as _re
        m = _re.fullmatch(r"\s*h?(\d{3,4})p?\s*", str(t.get("format") or ""), _re.I)
        if m:
            return int(m.group(1))
        return int(S.get(self.cfg, "hls_max_height") or 0)

    def _run_hls(self, t):
        """返回 True = 已处理（成功/失败/暂停都算）；False = 不支持（直播/SAMPLE-AES/DRM），调用方回落原路径。"""
        d = t.get("dir") or self.cfg.get("outdir") or V.DEFAULT_OUTDIR
        uri = t["uri"]
        name = t.get("name") or ""
        stem = os.path.splitext(name)[0] if name else os.path.splitext(os.path.basename(urlparse(uri).path))[0]
        stem = V.safe_name(stem or "video_%s" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
        out_path = os.path.join(d, stem + ".ts")
        c = str(t.get("conns") or S.get(self.cfg, "task_conns") or "自动")
        conns = max(1, min(32, int(c))) if c.isdigit() else 8
        with self._rpc_lock:
            t["status"] = "active"
            t["start_ts"] = time.time()
            t["statusMessage"] = "分段下载 m3u8/mpd…"
        self._pet_react("busy")
        gid = t["gid"]

        def throttle(n):
            a = int(t.get("limit_kbps") or 0) * 1024
            if a:
                b = self._buckets.get(gid)
                if b is None or b.rate != a:
                    b = self._buckets[gid] = RL.TokenBucket(a)
                b.consume(n, cancel=lambda: t.get("cancel"))
            if self.gbucket.rate:
                self.gbucket.consume(n, cancel=lambda: t.get("cancel"))

        def prog(p):
            with self._rpc_lock:
                t["downloadedLength"] = int(p.get("downloaded_bytes") or 0)
                t["downloadSpeed"] = int(p.get("speed") or 0)
            self._qmem_mark()

        try:
            hdr = dict(t.get("headers") or {})
            r = HLS.download(uri, out_path, V.SESSION, headers=hdr, conns=conns,
                             max_height=self._hls_height(t),
                             cancel=lambda: t.get("cancel"), prog=prog, log=self.log, throttle=throttle)
        except HLS.Unsupported as e:
            self.log("[HLS] 内置下载不处理（%s）→ 回落原路径" % str(e)[:80])
            t["_hls_off"] = True
            return False
        except HLS.Cancelled:
            if t.get("purge"):
                try:
                    import glob
                    for pd in glob.glob(glob.escape(os.path.join(d, stem)) + "*.parts"):
                        shutil.rmtree(pd, ignore_errors=True)
                except Exception:
                    pass
            with self._rpc_lock:
                t["status"] = "paused"
                t["downloadSpeed"] = 0
            self._qmem_mark(force=True)
            return True
        except Exception as e:
            self._fail(t, "m3u8/mpd 下载失败：%s" % str(e)[:150])
            self._rpc_fire_mascot(False)
            return True
        fp = r["path"]
        with self._rpc_lock:
            t["path"] = fp
            t["name"] = os.path.basename(fp)
            t["totalLength"] = t["downloadedLength"] = os.path.getsize(fp)
            t["downloadSpeed"] = 0
            t["status"] = "complete"
            t["statusMessage"] = ("已合并 %d 个分片" % r["segments"]) if r.get("muxed") or not r.get("audio_path") else \
                "已下载 %d 个分片；音轨单独保存（没有 ffmpeg 无法合并）：%s" % (r["segments"], os.path.basename(r["audio_path"]))
            if r.get("audio_path"):
                t["extra_files"] = [r["audio_path"]]
            t["done_ts"] = time.time()
        self._qmem_mark(force=True)
        self._rpc_fire_mascot(True)
        return True

    # ---------------- 直播录制
    def _run_live(self, t):
        live = t.get("live") or {}
        lim = int(live.get("limit_min") if live.get("limit_min") is not None else S.get(self.cfg, "live_limit_min")) * 60
        idle = int(live.get("idle_sec") or S.get(self.cfg, "live_idle_sec") or 60)
        name = t.get("name") or ("live_%s.ts" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
        if not name.lower().endswith(".ts"):
            name = os.path.splitext(name)[0] + ".ts"
        path = os.path.join(t.get("dir") or self.cfg.get("outdir") or V.DEFAULT_OUTDIR, V.safe_name(name))
        with self._rpc_lock:
            t["status"] = "active"
            t["start_ts"] = time.time()
            t["name"] = os.path.basename(path)
            t["statusMessage"] = "直播录制中…"
        self._pet_react("busy")

        def prog(d):
            with self._rpc_lock:
                t["downloadedLength"] = int(d.get("downloaded_bytes") or 0)
                t["downloadSpeed"] = int(d.get("speed") or 0)
                t["statusMessage"] = "直播录制中 · 已录 %s" % V.fmt_duration(d.get("elapsed") or 0)
        try:
            hdr = dict(t.get("headers") or {})
            fp, reason = LIVE.record(t["uri"], path, headers=hdr, limit_sec=lim, idle_sec=idle,
                                     cancel=lambda: t.get("cancel"), prog=self._throttled_prog(t, prog), log=self.log,
                                     session=V.SESSION)
            with self._rpc_lock:
                t["path"] = fp
                t["totalLength"] = t["downloadedLength"]
                t["downloadSpeed"] = 0
                t["status"] = "complete" if os.path.isfile(fp) and os.path.getsize(fp) > 0 else "error"
                t["statusMessage"] = {"limit": "已到录制时长上限", "idle": "直播无新画面，已自动停止",
                                      "ended": "直播已结束", "cancel": "已手动停止（文件可直接播放）"}.get(reason, "")
                if t["status"] == "error":
                    t["errorMessage"] = "没有录到任何画面"
                t["done_ts"] = time.time()
            if reason == "cancel" and t.get("purge"):
                t["status"] = "paused"
        except Exception as e:
            self._fail(t, "直播录制失败：%s" % str(e)[:150])
        self._qmem_mark(force=True)
        self._rpc_fire_mascot(t.get("status") == "complete")

    # ================================================================ 对外：添加 / 识别
    def add_task(self, info):
        """界面「开始下载」的统一入口。info: {url, name?, dir?, category?, headers?, queue?, hold?, limit_kbps?,
        conns?, checksum?, format?, subs?, playlist_items?, bt_select?, bt_seq?, after?, live?, size_hint?}"""
        info = dict(info or {})
        url = (info.get("url") or "").strip()
        if not url:
            return {"ok": False, "error": "没有链接"}
        from . import linkdecode as LD
        url = LD.decode(url)
        info["url"] = url
        mg = RG.hash_to_magnet(url)
        if mg:
            url = mg
        if info.get("checksum"):
            a, h = CK.parse(info["checksum"])
            if not a:
                return {"ok": False, "error": "校验值格式不对（支持 md5/sha1/sha256/sha512）"}
        opts = {}
        d = (info.get("dir") or "").strip()
        if d:
            opts["dir"] = d
        elif info.get("category"):
            opts["dir"] = os.path.join(self.cfg.get("outdir") or V.DEFAULT_OUTDIR, info["category"])
        if info.get("name"):
            opts["out"] = str(info["name"]).strip()
        hdr = info.get("headers")
        if isinstance(hdr, dict):
            opts["header"] = ["%s: %s" % (k, v) for k, v in hdr.items() if v]
        elif isinstance(hdr, str) and hdr.strip():
            opts["header"] = [ln.strip() for ln in hdr.splitlines() if ":" in ln]
        elif isinstance(hdr, list):
            opts["header"] = hdr
        add = {k: info.get(k) for k in TASK_OPT_KEYS if info.get(k) not in (None, "")}
        if info.get("name"):
            add["keep_name"] = True
        _TL.add_opts = add
        try:
            gid = self.rpc_add_uri(url, opts, defer=bool(info.get("hold")), src="user")
        finally:
            _TL.add_opts = None
        t = self._rpc_tasks.get(gid) or {}
        if info.get("start_now") and t.get("status") == "waiting" and not t.get("thread"):
            self._start(gid)
        return {"ok": True, "gid": gid, "status": t.get("status"), "name": t.get("name")}

    def add_batch(self, text, common=None):
        urls, trunc = BATCH.expand_text(text)
        out = []
        for u in urls:
            info = dict(common or {})
            info["url"] = u
            out.append(self.add_task(info))
        return {"ok": True, "count": len(out), "gids": [r.get("gid") for r in out if r.get("ok")],
                "truncated": trunc}

    def inspect(self, text, probe=True):
        """一个框识别一切 + 联网探测（大小/文件名/能否续传/在线/证书）。"""
        res = RG.classify(text, ND)
        items = res.get("items") or []
        if probe:
            def one(it):
                k = it.get("kind")
                try:
                    if k in ("direct", "netdisk"):
                        p = RG.probe_http(it["url"], tls_check=bool(S.get(self.cfg, "tls_warn")),
                                          session=V.SESSION)
                        it.update({"size": p["size"], "name": p["name"], "resumable": p["resumable"],
                                   "online": p["online"], "status": p["status"], "mime": p["mime"],
                                   "cert_error": p.get("cert_error") or "", "probe_error": p.get("error") or ""})
                        it["category"] = self._category_of(p["name"] or it["url"]) if S.get(self.cfg, "auto_category") else ""
                    elif k == "torrent":
                        data = None
                        if it.get("local"):
                            with open(it["url"], "rb") as f:
                                data = f.read()
                        else:
                            data = self._bt_fetch_torrent(it["url"], {})
                        if data:
                            info = BC.torrent_info(data)
                            sel = set(self._bt_auto_select(info["files"]))
                            it.update({"name": info["name"], "size": info["total"], "infohash": info["infohash"],
                                       "files": [dict(f, selected=f["index"] in sel) for f in info["files"]],
                                       "online": True})
                    elif k == "magnet":
                        mi = BC.magnet_info(it["url"])
                        it.update({"name": mi["name"], "infohash": mi["infohash"], "online": True})
                    elif k == "m3u8":
                        r = V.SESSION.get(it["url"], timeout=10, verify=False)
                        txt = r.text if r.status_code == 200 else ""
                        it["online"] = r.status_code == 200
                        it["live"] = LIVE.is_live(txt)
                        vs = []
                        try:
                            for v in (V.parse_m3u8(txt) or [])[:12]:
                                if isinstance(v, dict):
                                    vs.append({"res": v.get("res") or v.get("resolution") or "",
                                               "bw": v.get("bw") or v.get("bandwidth") or 0,
                                               "url": v.get("url") or v.get("variant") or ""})
                        except Exception:
                            pass
                        it["variants"] = vs
                        it["encrypted"] = "#EXT-X-KEY" in txt and "METHOD=NONE" not in txt.upper()
                except Exception as e:
                    it["probe_error"] = str(e)[:120]
                return it
            ths = []
            for it in items[:60]:
                th = threading.Thread(target=one, args=(it,), daemon=True)
                th.start()
                ths.append(th)
            for th in ths:
                th.join(timeout=12)
        res["items"] = items
        return res

    # ================================================================ 任务操作
    def _start(self, gid):
        t = self._rpc_tasks.get(gid)
        if not t:
            return False
        t["hold"] = False
        th = t.get("thread")
        if th is not None and th.is_alive():
            if t.get("status") in ("paused", "error") and not t.get("_resume_deferred"):
                # 刚点暂停、工作线程还在收尾就点继续：等它退出再续上，不能静默丢掉这次「继续」
                t["_resume_deferred"] = True

                def _later(th=th):
                    th.join(15)
                    t.pop("_resume_deferred", None)
                    if not th.is_alive() and t.get("status") in ("paused", "error") and gid in self._rpc_tasks:
                        self._start(gid)
                threading.Thread(target=_later, daemon=True, name="resume-" + gid).start()
                return True
            return False
        if t.get("status") == "waiting":
            super().rpc_resume(gid)
        elif t.get("status") in ("paused", "error"):
            with self._rpc_lock:
                t["status"] = "paused"
                t["errorMessage"] = ""
                t["errorCode"] = "0"
            super().rpc_resume(gid)
        return True

    def task_action(self, action, gids):
        out = {"ok": True, "done": []}
        for gid in gids or []:
            t = self._rpc_tasks.get(gid)
            if not t:
                continue
            if action == "pause":
                self.rpc_pause(gid)
                with self._rpc_lock:
                    t["sched_paused"] = False          # 用户手动暂停：不再被计划 / 队列自动续上
                    t.pop("_q_paused", None)
                    if t.get("status") == "waiting":
                        t["status"] = "paused"
                self._pet_idle_if_quiet()
            elif action in ("resume", "start"):
                t["_retries"] = 0
                self._start(gid)
            elif action == "later":
                with self._rpc_lock:
                    if t.get("status") in ("paused", "error"):
                        t["status"] = "waiting"
                    t["hold"] = True
            out["done"].append(gid)
        self._qmem_mark(force=True)
        return out

    def _cleanup_choice(self, delete_files, cleanup=None):
        """cancel_cleanup：always = 总删未完成临时文件；never = 总保留；ask = 听界面这次的回答
        （task.remove 第 3 个参数；没传就跟「删文件」勾选走）。"""
        pol = S.get(self.cfg, "cancel_cleanup") or "ask"
        if pol == "always":
            return True
        if pol == "never":
            return False
        if cleanup is not None:
            return bool(cleanup)
        return True if delete_files else "legacy"      # 界面没问：勾了删文件就全清，否则沿用旧行为（只清 .part）

    def _rpc_purge(self, t):
        """取消/删除任务时的残留清理（原版只删 .part 与 .snifftmp）：按 cancel_cleanup 决定，
        并补上 .sdl/.sdlpart/.aria2/.sdl.aria2，保存位置与未完成目录两处都清。"""
        want = t.get("_cleanup")
        if want is None:                  # 不是从 task.remove 来的（如 aria2.remove 推送方删任务）
            want = self._cleanup_choice(False, None)
        if want is False:
            self.log("[删除] 按设置保留未完成的临时文件：%s" % ((t.get("name") or "")[:60] or "—"))
            return
        super()._rpc_purge(t)
        if want == "legacy":
            return

        def sweep():
            n = 0
            for p in self._temp_paths(t):
                try:
                    if os.path.isfile(p):
                        os.remove(p)
                        n += 1
                except OSError:
                    pass
            return n
        n = sweep()
        if n:
            self.log("[删除] 已清理 %d 个未完成临时文件：%s" % (n, (t.get("name") or "")[:60]))

        def later():                       # 引擎线程收尾时可能再写一次进度文件（aria2 控制文件 / .sdlpart）
            for d in (1.0, 2.5):
                time.sleep(d)
                sweep()
        threading.Thread(target=later, daemon=True, name="purge-late").start()

    def _bt_hard_remove(self, t):
        """BT 删除也听 cancel_cleanup：选「保留」时只停任务、摘结果，不删已下片段与 .aria2（以后可重新加种续下）。
        原版 _bt_hard_remove 无条件删文件。"""
        want = t.get("_cleanup")
        if want is None:
            want = self._cleanup_choice(False, None)
        if want is not False:
            return super()._bt_hard_remove(t)
        g = t.get("btGid")
        if g:
            for m in ("aria2.forceRemove", "aria2.removeDownloadResult"):
                try:
                    self._bt_rpc(m, [g])
                except Exception:
                    pass
        self.log("[删除] BT 任务按设置保留未完成的文件：%s" % ((t.get("name") or "")[:60] or "—"))

    def task_remove(self, gids, delete_files=False, cleanup=None):
        moved, failed = [], []
        for gid in gids or []:
            t = self._rpc_tasks.get(gid)
            if not t:
                continue
            t["_cleanup"] = self._cleanup_choice(delete_files, cleanup)
            files = []
            if delete_files:
                p = t.get("path") or (os.path.join(t.get("dir") or "", t.get("name") or "") if t.get("name") else "")
                if p and os.path.isfile(p):
                    files.append(p)         # 用户勾了删文件：成品进回收站；临时文件由 _rpc_purge 按 cancel_cleanup 处理
            self.rpc_remove(gid)
            if files:
                m, f = _to_trash(files)
                moved += m
                failed += f
        self._pet_idle_if_quiet()
        if moved:
            self.log("[删除] %d 个文件已移到回收站" % len(moved))
        return {"ok": True, "trashed": moved, "failed": failed}

    def task_refresh(self, gid, new_url, headers=None):
        """IDM「刷新下载地址」：链接过期后换新链接续传，保留文件名/目录/已下部分。"""
        t = self._rpc_tasks.get(gid)
        if not t:
            return {"ok": False, "error": "任务不存在"}
        new_url = (new_url or "").strip()
        if not new_url.lower().startswith(("http://", "https://", "ftp://")):
            return {"ok": False, "error": "新地址不是 http(s) 链接"}
        th = t.get("thread")
        if th is not None and th.is_alive():
            self.rpc_pause(gid)
            th.join(timeout=8)
        with self._rpc_lock:
            t["uri"] = new_url
            t["_dkey"] = self._rpc_norm_key(new_url)
            if headers:
                t["headers"] = self._rpc_parse_headers({"header": headers if isinstance(headers, list)
                                                        else [x for x in str(headers).splitlines() if ":" in x]})
            t["status"] = "paused"
            t["errorMessage"] = ""
            t["errorCode"] = "0"
            t["note"] = "已换新地址"
            t["_retries"] = 0
            t.pop("_h2gid", None)
        self.log("[刷新地址] %s → %s" % ((t.get("name") or gid)[:40], new_url[:80]))
        self._start(gid)
        return {"ok": True, "gid": gid}

    def task_update(self, gid, patch):
        t = self._rpc_tasks.get(gid)
        if not t:
            return {"ok": False, "error": "任务不存在"}
        patch = patch or {}
        if "limit_kbps" in patch:
            self.task_set_limit(gid, patch["limit_kbps"])
        for k in ("queue", "checksum", "after", "conns", "format", "subs"):
            if k in patch:
                if k == "checksum" and patch[k] and not CK.parse(patch[k])[0]:
                    return {"ok": False, "error": "校验值格式不对"}
                if k == "queue" and not self.queues.get(patch[k]):
                    return {"ok": False, "error": "没有这个队列"}
                t[k] = patch[k]
        self._qmem_mark(force=True)
        return {"ok": True}

    def task_detail(self, gid):
        t = self._rpc_tasks.get(gid)
        if not t:
            return {"ok": False, "error": "任务不存在"}
        v = self._view(t)
        key = gid[:6]
        logs = [r for r in self._log_ring if key in r["msg"] or (t.get("name") and t["name"][:20] in r["msg"])]
        hdr = {}
        for k, val in (t.get("headers") or {}).items():
            hdr[k] = ("（%d 字符，已隐藏）" % len(str(val))) if k.lower() in ("cookie", "authorization") else val
        v.update({"log": logs[-80:], "headers": hdr, "referer": t.get("referer") or "",
                  "ydl_opts": t.get("_ydl_opts") or {}, "aria2_gid": t.get("_h2gid") or t.get("btGid") or ""})
        if t.get("_h2gid") and t.get("status") == "active":
            try:
                st = self._h2_rpc_raw("aria2.tellStatus", [t["_h2gid"], ["connections", "downloadSpeed"]]) or {}
                v["connections"] = int(st.get("connections") or 0)
                srv = self._h2_rpc_raw("aria2.getServers", [t["_h2gid"]]) or []
                v["servers"] = [{"uri": s2.get("currentUri"), "speed": int(s2.get("downloadSpeed") or 0)}
                                for f in srv for s2 in (f.get("servers") or [])][:32]
            except Exception:
                pass
        return {"ok": True, "task": v}

    def export_tasks(self, include_done=True):
        rows = []
        for gid in list(self._rpc_order):
            t = self._rpc_tasks.get(gid)
            if not t or (not include_done and t.get("status") == "complete"):
                continue
            hdr = {k: v for k, v in (t.get("headers") or {}).items() if k.lower() not in ("cookie", "authorization")}
            rows.append({"url": t.get("uri"), "name": t.get("name"), "dir": t.get("dir"), "status": t.get("status"),
                         "headers": hdr, "checksum": t.get("checksum") or "", "queue": t.get("queue") or Q.MAIN,
                         "size": int(t.get("totalLength") or 0)})
        return {"ok": True, "format": "sniffdl-tasks", "version": 1, "exported": int(time.time()), "tasks": rows}

    def import_tasks(self, data, start=False):
        if isinstance(data, str):
            data = data.strip()
            try:
                data = json.loads(data)
            except Exception:
                return self.add_batch(data, {"hold": not start})          # 纯文本 = 链接列表
        rows = data.get("tasks") if isinstance(data, dict) else data
        n = 0
        for r in rows or []:
            if not isinstance(r, dict) or not r.get("url"):
                continue
            if r.get("status") == "complete":
                continue
            self.add_task({"url": r["url"], "name": r.get("name") or None, "dir": r.get("dir") or None,
                           "headers": r.get("headers") or None, "checksum": r.get("checksum") or None,
                           "queue": r.get("queue") or None, "hold": not start})
            n += 1
        return {"ok": True, "count": n}

    def intercept_answer(self, pid, accept, info=None):
        it = self._pending_icpt.pop(pid, None)
        if not it:
            return {"ok": False, "error": "已处理或已过期"}
        if not accept:
            self.log("[接管] 已取消：%s" % (it.get("filename") or it.get("url") or "")[:60])
            return {"ok": True, "cancelled": True}
        info = dict(info or {})
        merged = dict(it)
        merged.update({k: v for k, v in info.items() if v not in (None, "")})
        hl = self._rpc_headers_list(merged)
        add = {k: merged.get(k) for k in TASK_OPT_KEYS if merged.get(k) not in (None, "")}
        if merged.get("filename"):
            add["keep_name"] = True
        _TL.add_opts = add
        try:
            opts = {"dir": merged.get("dir") or merged.get("outdir") or self.cfg.get("outdir") or V.DEFAULT_OUTDIR,
                    "header": hl}
            if merged.get("filename"):
                opts["out"] = merged["filename"]
            gid = self.rpc_add_uri(merged["url"], opts, defer=bool(merged.get("hold")), src="user")
        finally:
            _TL.add_opts = None
        return {"ok": True, "gid": gid}

    # ---------------- 待确认区（LinkGrabber）
    def grabber_push(self, items, source="ext"):
        added = []
        for it in items or []:
            if isinstance(it, str):
                it = {"url": it}
            u = (it.get("url") or "").strip()
            if not u or u in self._grabber:
                continue
            cls = RG.classify_one(u, ND)
            if cls["kind"] in ("unsupported", "empty"):
                continue
            rec = {"url": u, "name": it.get("name") or it.get("text") or "", "kind": cls["kind"],
                   "ext": RG.url_ext(u), "referer": it.get("referer") or "", "source": source, "ts": time.time(),
                   "size": int(it.get("size") or 0), "online": None}
            self._grabber[u] = rec
            added.append(rec)
        if added:
            self.bus.emit("grabber", {"added": added, "count": len(self._grabber)})
            threading.Thread(target=self._grabber_check, args=([a["url"] for a in added],), daemon=True).start()
        return {"ok": True, "added": len(added), "count": len(self._grabber)}

    def _grabber_check(self, urls):
        for u in urls[:200]:
            rec = self._grabber.get(u)
            if not rec or rec["kind"] not in ("direct", "netdisk", "page"):
                continue
            p = RG.probe_http(u, session=V.SESSION, tls_check=False, timeout=6)
            rec.update(online=p["online"], size=p["size"] or rec["size"], name=rec["name"] or p["name"],
                       resumable=p["resumable"], mime=p["mime"])
            self.bus.emit("grabber", {"updated": [rec], "count": len(self._grabber)})

    # ================================================================ 剪贴板
    def _on_clip_text(self, text):
        res = RG.classify(text, ND)
        items = [i for i in res.get("items") or [] if i.get("kind") not in ("page", "unsupported", "empty")]
        if not items and res.get("kind") == "page" and len(text) < 2000:
            items = res["items"]            # 网页也提示（多半是视频页）
            if not RG.video_site(items[0]["url"]):
                return
        if not items:
            return
        key = items[0]["url"]
        if key in self._clip_seen:
            return
        self._clip_seen.append(key)
        self._clip_url = key
        self.bus.emit("clip", {"kind": res.get("kind"), "count": len(items), "items": items[:20], "text": text[:2000]})
        self.log("[剪贴板] 识别到 %s：%s" % (res.get("kind"), key[:80]))

    # ================================================================ 视图 / 事件
    def _view(self, t):
        now = time.time()
        st = t.get("status") or "waiting"
        total = int(t.get("totalLength") or 0)
        done = int(t.get("downloadedLength") or 0)
        spd = int(t.get("downloadSpeed") or 0) if st == "active" else 0
        th = t.get("thread")
        running = bool(th is not None and th.is_alive())
        kind = t.get("_kind")
        if not kind:
            uri = t.get("uri") or ""
            if t.get("live"):
                kind = "live"
            elif self._rpc_is_bt(uri):
                kind = "bt"
            else:
                try:
                    kind = "file" if self._rpc_is_direct_file(uri, t.get("name")) else "video"
                except Exception:
                    kind = "file"
            t["_kind"] = kind
        phase = st
        if st == "active" and kind == "bt" and total and done >= total:
            phase = "seeding"
        elif st == "waiting":
            if t.get("retry_at") and t["retry_at"] > now:
                phase = "retry"
            elif t.get("hold"):
                phase = "hold"
            elif not running:
                phase = "queued"
        if t.get("_verifying"):
            phase = "verifying"
        up = int(t.get("uploadLength") or 0)
        v = {"gid": t["gid"], "name": t.get("name") or "", "status": st, "phase": phase, "kind": kind,
             "total": total, "done": done, "speed": spd, "eta": int((total - done) / spd) if spd and total > done else -1,
             "uploaded": up, "ratio": round(up / done, 3) if done and kind == "bt" else 0,
             "src": t.get("src") or "user", "dir": t.get("dir") or "", "path": t.get("path") or "",
             "uri": t.get("uri") or "", "added": t.get("added_ts") or 0, "started": t.get("start_ts") or 0,
             "finished": t.get("done_ts") or 0, "queue": t.get("queue") or Q.MAIN,
             "limit_kbps": int(t.get("limit_kbps") or 0), "conns": t.get("conns") or "",
             "checksum": t.get("checksum") or "", "checksum_ok": t.get("checksum_ok"),
             "retries": int(t.get("_retries") or 0), "retry_at": t.get("retry_at") or 0,
             "resumable": t.get("resumable"), "cert_error": t.get("cert_error") or "",
             "note": t.get("note") or "", "msg": t.get("statusMessage") or "",
             "seeders": int(t.get("numSeeders") or 0), "connections": int(t.get("connections") or 0),
             "bt_seq": bool(t.get("bt_seq")), "hold": bool(t.get("hold")), "format": t.get("format") or "",
             "extracted": t.get("extracted") or "", "engine": t.get("speed_engine") or ""}
        if st == "error":
            v["err"] = ERR.friendly(t.get("errorMessage"), t.get("errorCode"))
        return v

    def tasks(self):
        with self._rpc_lock:
            ts = [self._rpc_tasks[g] for g in self._rpc_order if g in self._rpc_tasks]
        return [self._view(t) for t in ts]

    def stats(self):
        vs = self.tasks()
        down = sum(v["speed"] for v in vs)
        up = sum(int(self._rpc_tasks.get(v["gid"], {}).get("uploadSpeed") or 0) for v in vs)
        try:
            free = shutil.disk_usage(self.cfg.get("outdir") or V.DEFAULT_OUTDIR).free
        except Exception:
            free = 0
        cnt = collections.Counter(v["phase"] for v in vs)
        return {"down": down, "up": up, "active": cnt.get("active", 0), "seeding": cnt.get("seeding", 0),
                "waiting": cnt.get("queued", 0) + cnt.get("hold", 0) + cnt.get("retry", 0) + cnt.get("waiting", 0),
                "paused": cnt.get("paused", 0), "complete": cnt.get("complete", 0), "error": cnt.get("error", 0),
                "total": len(vs), "disk_free": free, "limit_kbps": self._eff_global_kbps(),
                "turtle": bool(S.get(self.cfg, "turtle")), "port": self.port, "power": self._power}

    def _emit_diffs(self):
        cur = {}
        ups = []
        for v in self.tasks():
            s = json.dumps(v, sort_keys=True, ensure_ascii=False, default=str)
            cur[v["gid"]] = s
            if self._views.get(v["gid"]) != s:
                ups.append(v)
        gone = [g for g in self._views if g not in cur]
        self._views = cur
        if ups or gone:
            self.bus.emit("tasks", {"upsert": ups, "removed": gone})
        try:
            self._ws_notify(ups, gone)
        except Exception:
            pass

    def _ws_notify(self, ups, gone):
        """任务状态迁移 → aria2 WebSocket 通知（AriaNg/Motrix 类客户端靠它实时刷新）。
        首次（启动恢复的任务）只记状态不通知；active 之外新任务出现也只在迁移时发。"""
        srv = self._rpc_srv
        prev = self.__dict__.setdefault("_ws_prev", {})
        first = not self.__dict__.get("_ws_seeded")
        self._ws_seeded = True
        for v in ups:
            g, st, ph = v["gid"], v.get("status"), v.get("phase")
            old = prev.get(g)
            prev[g] = (st, ph)
            if first or srv is None or (old and old == (st, ph)):
                continue
            ost = old[0] if old else None
            oph = old[1] if old else None
            if st in ("complete", "error") and ost in (None, "waiting"):
                srv.notify("aria2.onDownloadStart", g)       # 小文件一个 tick 内就下完：补发 Start，与 aria2 顺序一致
            if st == "active" and ost != "active":
                srv.notify("aria2.onDownloadStart", g)
            elif st == "paused" and ost != "paused":
                srv.notify("aria2.onDownloadPause", g)
            elif st == "complete" and ost != "complete":
                srv.notify("aria2.onDownloadComplete", g)
            elif st == "error" and ost != "error":
                srv.notify("aria2.onDownloadError", g)
            if v.get("kind") == "bt" and ph in ("seeding", "complete") and oph not in ("seeding", "complete"):
                srv.notify("aria2.onBtDownloadComplete", g)
        for g in gone:
            if prev.pop(g, None) is not None and srv is not None and not first:
                srv.notify("aria2.onDownloadStop", g)

    def rpc_peers(self, gid):
        """aria2.getPeers：BT 任务经 BT 守护取真实 peer，转成 aria2 格式；其它任务空。"""
        t = self._rpc_tasks.get(str(gid))
        bg = (t or {}).get("btGid")
        if not t or not bg or not (self._bt or {}).get("port"):
            return []
        out = []
        for p in self._bt_rpc("aria2.getPeers", [bg]) or []:
            out.append({"peerId": p.get("peerId") or "", "ip": p.get("ip") or "", "port": str(p.get("port") or ""),
                        "bitfield": p.get("bitfield") or "", "amChoking": p.get("amChoking") or "false",
                        "peerChoking": p.get("peerChoking") or "false",
                        "downloadSpeed": str(p.get("downloadSpeed") or 0),
                        "uploadSpeed": str(p.get("uploadSpeed") or 0), "seeder": p.get("seeder") or "false"})
        return out

    def _tick_loop(self):
        n = 0
        while not self._stop.is_set():
            try:
                self._queue_tick()
                self._emit_diffs()
                n += 1
                if n % 2 == 0:
                    st = self.stats()
                    self._speed_hist.append((int(time.time()), st["down"], st["up"]))
                    self.bus.emit("stats", st)
                    self._apply_limits()
                    self._after_all_tick(st)
                    self.keepawake.set(bool(S.get(self.cfg, "keep_awake")) and (st["active"] > 0))
                    self._bt_watch()
                    self._icpt_timeout()
                    if self._qmem_dirty:
                        self._qmem_flush()
            except Exception as e:
                V.LOG.exception("tick")
                self.log("[内核] tick 异常：%s" % str(e)[:120])
            time.sleep(0.5)

    def _slow_loop(self):
        while not self._stop.is_set():
            try:
                self._refresh_trackers()
                self._watch_folder()
            except Exception:
                pass
            self._stop.wait(5)

    def _kick(self):
        try:
            self._queue_tick()
        except Exception:
            pass

    def _queue_tick(self):
        rows = []
        with self._rpc_lock:
            items = [self._rpc_tasks[g] for g in self._rpc_order if g in self._rpc_tasks]
        for t in items:
            th = t.get("thread")
            running = bool(th is not None and th.is_alive())
            # 计划时段结束 / 停止队列 暂停的任务：下载线程退出时会把状态写回 paused（与这里设的 waiting 竞争），
            # 线程结束后统一拉回 waiting，下个时段 / 队列开始时才会被自动续上
            if not running and t.get("status") == "paused" and (t.get("sched_paused") or t.get("_q_paused")) \
                    and not t.get("errorMessage"):
                t["status"] = "waiting"
            total = int(t.get("totalLength") or 0)
            seeding = (t.get("status") == "active" and t.get("_kind") == "bt" and total
                       and int(t.get("downloadedLength") or 0) >= total)
            rows.append({"gid": t["gid"], "queue": t.get("queue") or Q.MAIN, "status": t.get("status"),
                         "running": running, "seeding": bool(seeding), "hold": bool(t.get("hold")),
                         "retry_at": t.get("retry_at") or 0, "added": t.get("added_ts") or 0,
                         "sched_paused": bool(t.get("sched_paused"))})
        start, pause = self.queues.plan(rows, S.get(self.cfg, "max_concurrent"))
        for g in pause:
            t = self._rpc_tasks.get(g)
            if t:
                self.rpc_pause(g)
                with self._rpc_lock:
                    t["status"] = "waiting"
                    t["sched_paused"] = True
                    t["note"] = "计划时段结束，已暂停，下个时段自动继续"
                self.log("[计划] 时段结束，暂停 %s" % (t.get("name") or g)[:40])
        for g in start:
            t = self._rpc_tasks.get(g)
            if t:
                t["sched_paused"] = False
                t.pop("_q_paused", None)
                if t.get("note", "").startswith(("计划时段", "重启未自动")):
                    t["note"] = ""
                super().rpc_resume(g)

    def _after_all_tick(self, st):
        busy = st["active"] + st["waiting"] - sum(1 for t in self._rpc_tasks.values() if t.get("hold") and t.get("status") == "waiting")
        if busy > 0:
            self._had_work = True
            if self._power and self._power.get("state") == "countdown":
                self._power = None
            return
        if not self._had_work:
            return
        self._had_work = False
        act = S.get(self.cfg, "after")
        if act and act != "无":
            self._power = {"action": act, "state": "countdown", "at": time.time() + 60}
            self.bus.emit("power", self._power)
            self.log("[完成后] 全部下载结束，60 秒后%s（可在界面取消）" % act)
            threading.Thread(target=self._power_wait, daemon=True).start()

    def _power_wait(self):
        p = self._power
        while p is self._power and p and time.time() < p["at"]:
            time.sleep(0.5)
        if p is not self._power or not p:
            return
        act = p["action"]
        p["state"] = "done"
        self.bus.emit("power", p)
        if act == "退出程序":
            self.bus.emit("quit", {"reason": "after"})
            threading.Timer(1.5, lambda: os._exit(0) if os.environ.get("SNIFFDL_DRYRUN_OS") != "1" else None).start()
        else:
            PU.power(act)

    def power_cancel(self):
        if self._power:
            self._power = None
            self.bus.emit("power", {"state": "cancelled"})
            return {"ok": True}
        return {"ok": False, "error": "没有待执行的动作"}

    def _icpt_timeout(self):
        """没有界面在看（没人订阅事件）时，待确认项 20 秒后直接开下，绝不让推送「分发成功但没下」。"""
        for pid, it in list(self._pending_icpt.items()):
            if time.time() - it.get("_ts", 0) > 20 and self.bus.subscribers == 0:
                self.log("[接管] 无界面确认，自动开始：%s" % (it.get("filename") or it.get("url") or "")[:60])
                self.intercept_answer(pid, True)

    def _bt_watch(self):
        """做种：到「无活动上限」就停；做种结束触发 seed_done 钩子。"""
        idle_min = int(S.get(self.cfg, "bt_seed_idle_min") or 0)
        for t in list(self._rpc_tasks.values()):
            if t.get("_kind") != "bt":
                continue
            total = int(t.get("totalLength") or 0)
            seeding = t.get("status") == "active" and total and int(t.get("downloadedLength") or 0) >= total
            if seeding:
                if not t.get("_seed_t0"):
                    t["_seed_t0"] = time.time()
                    t["_seed_last_up"] = (time.time(), int(t.get("uploadLength") or 0))
                    self.bus.emit("notify", {"kind": "done", "gid": t["gid"], "title": "下载完成，做种中",
                                             "body": t.get("name") or ""})
                lt, lu = t["_seed_last_up"]
                up = int(t.get("uploadLength") or 0)
                if up > lu:
                    t["_seed_last_up"] = (time.time(), up)
                elif idle_min and time.time() - lt > idle_min * 60:
                    self.log("[BT] %s 做种 %d 分钟无人连接，停止做种" % ((t.get("name") or "")[:40], idle_min))
                    try:
                        self._bt_rpc("aria2.forceRemove", [t.get("btGid")])
                    except Exception:
                        pass
                    with self._rpc_lock:
                        t["status"] = "complete"
                        t["done_ts"] = time.time()
                        t["cancel"] = True
                    self._hook("seed_done", t)
            elif t.get("_seed_t0") and t.get("status") == "complete" and not t.get("_seed_done"):
                t["_seed_done"] = True
                self._hook("seed_done", t)

    # ---------------- 监视文件夹
    def _watch_folder(self):
        d = (S.get(self.cfg, "watch_dir") or "").strip()
        if not d or not os.path.isdir(d):
            return
        done_dir = os.path.join(d, "已添加")
        for fn in sorted(os.listdir(d)):
            p = os.path.join(d, fn)
            if not os.path.isfile(p) or p in self._watch_seen:
                continue
            low = fn.lower()
            if not (low.endswith(".torrent") or low.endswith(".txt")):
                continue
            if time.time() - os.path.getmtime(p) < 1.5:          # 还在写
                continue
            self._watch_seen.add(p)
            try:
                os.makedirs(done_dir, exist_ok=True)
                dst = os.path.join(done_dir, fn)
                if os.path.exists(dst):
                    dst = NM.unique_path(dst)
                os.replace(p, dst)
                if low.endswith(".torrent"):
                    self.add_task({"url": dst})
                else:
                    with open(dst, encoding="utf-8", errors="replace") as f:
                        self.add_batch(f.read())
                self.log("[监视文件夹] 已加入：%s" % fn)
            except Exception as e:
                self.log("[监视文件夹] 处理 %s 失败：%s" % (fn, str(e)[:80]))

    # ---------------- 设置
    def settings_get(self):
        vals = S.values(self.cfg)
        vals["autostart"] = bool(self.get_autostart())
        return {"ok": True, "values": vals, "schema": S.public_schema(),
                "rpc": {"port": self.port, "configured_port": int(self.cfg.get("rpc_port") or 6800),
                        "token_set": bool(self.cfg.get("rpc_token")),
                        "url": "http://127.0.0.1:%d/jsonrpc" % self.port},
                "defaults": {"outdir": V.DEFAULT_OUTDIR}}

    def settings_set(self, patch):
        patch = patch or {}
        changed, errors = {}, {}
        secrets_ = set(S.secret_keys())
        for k, v in patch.items():
            if k in secrets_ and v == S.SECRET_MASK:
                continue                      # 界面回传的掩码 = 没改，保留原值
            try:
                vv = S.validate(k, v)
            except ValueError as e:
                errors[k] = str(e)
                continue
            if k == "autostart":
                self.set_autostart(vv)
            self.cfg[k] = vv
            changed[k] = vv
        if changed:
            if "format_preset" in changed and changed["format_preset"].endswith("p"):
                self.cfg["quality"] = changed["format_preset"][:-1]
            elif "format_preset" in changed and changed["format_preset"] == "最佳":
                self.cfg["quality"] = "最佳"
            self._save_cfg()
            self._apply_changed(changed)
            pub = {k: ((S.SECRET_MASK if v else "") if k in secrets_ else v) for k, v in changed.items()}
            self.bus.emit("settings", {"changed": pub})
            changed = pub
        return {"ok": not errors, "changed": changed, "errors": errors}

    def _apply_changed(self, ch):
        if any(k in ch for k in ("dash_aria2", "sniff_browser", "dl_threads", "proxy_mode", "proxy_manual",
                                 "cookie_browser")):
            self._apply_globals()
        if any(k in ch for k in ("speed_limit_kbps", "turtle", "turtle_kbps", "fullspeed_window",
                                 "fullspeed_start", "fullspeed_end")):
            self._apply_limits(force=True)
        if "bt_upload_limit_kbps" in ch:
            self._apply_upload_limit()
        if "rx_cap_gb" in ch:
            self._rx_cap_gb = float(ch["rx_cap_gb"] or 0)
        if "pet_live" in ch:
            on = bool(ch["pet_live"])
            if on and not self._pet:
                self._setup_pet()
            if self._pet:
                self._pet.visible(on)
                self.pet_live = True
        if any(k in ch for k in ("rpc_port", "rpc_token", "rpc_enable")):
            self.cfg["rpc_enable"] = True              # 界面依赖它，不允许关掉主端口
            old = self.port
            self._rpc_stop()
            self._rpc_start()
            if self.port != old:
                RT.write(self.runtime_path, self.port, self.ui_token, {"host": self.host})
                self.bus.emit("port", {"port": self.port})
        if any(k.startswith("bt_") for k in ch):
            self.log("[BT] 设置已保存；DHT/端口等启动参数在 BT 引擎下次启动时生效")

    def speed_history(self):
        return [{"t": a, "down": b, "up": c} for a, b, c in list(self._speed_hist)]

    # ---------------- 事件流
    def serve_events(self, handler, last_id=0):
        def on_open():
            return [(self.bus.last_id, "snapshot", {"tasks": self.tasks(), "stats": self.stats(),
                                                    "pending": [self._icpt_public(p) for p in self._pending_icpt.values()],
                                                    "grabber": list(self._grabber.values())}, time.time())]
        EV.serve_sse(handler, self.bus, last_id=last_id, on_open=on_open, should_stop=self._stop.is_set)

    def _icpt_public(self, it):
        return {"id": it.get("_pid"), "url": it.get("url"), "filename": it.get("filename") or "",
                "size": it.get("size") or 0, "referer": it.get("referer") or ""}

    def takeover_rules(self):
        c = self.cfg

        def lines(k):
            return [x.strip().lower() for x in (S.get(c, k) or "").replace(",", "\n").splitlines() if x.strip()]
        return {"ok": True, "enabled": bool(S.get(c, "takeover_enable")), "exts": lines("takeover_exts"),
                "min_kb": int(S.get(c, "takeover_min_kb") or 0), "whitelist": lines("takeover_whitelist"),
                "blacklist": lines("takeover_blacklist"), "alt_bypass": bool(S.get(c, "takeover_alt_bypass")),
                "confirm": bool(S.get(c, "rpc_confirm_dialog"))}

    def rpc_global_option(self):
        c = self.cfg
        lim = int(S.get(c, "speed_limit_kbps") or 0)
        return {"dir": c.get("outdir") or V.DEFAULT_OUTDIR,
                "max-concurrent-downloads": str(int(S.get(c, "max_concurrent") or 5)),
                "max-overall-download-limit": "%dK" % lim if lim else "0",
                "max-overall-upload-limit": "%dK" % int(S.get(c, "bt_upload_limit_kbps") or 0)
                if int(S.get(c, "bt_upload_limit_kbps") or 0) else "0",
                "continue": "true", "min-split-size": "1M"}

    def rpc_change_global_option(self, opts):
        """aria2.changeGlobalOption：只认识的键真生效（限速/并发/上传限速/保存目录），其余忽略但回 OK。"""
        from rpc_server import size_to_kbps
        patch = {}
        for k, v in (opts or {}).items():
            if k == "max-overall-download-limit":
                patch["speed_limit_kbps"] = size_to_kbps(v)
            elif k == "max-overall-upload-limit":
                patch["bt_upload_limit_kbps"] = size_to_kbps(v)
            elif k == "max-concurrent-downloads":
                patch["max_concurrent"] = max(1, min(16, int(v)))
            elif k == "dir" and v:
                patch["outdir"] = str(v)
        if patch:
            r = self.settings_set(patch)
            if r.get("errors"):
                raise ValueError(str(r["errors"]))
        return True

    def rpc_change_option(self, gid, opts):
        from rpc_server import size_to_kbps
        if gid not in self._rpc_tasks:
            return False
        for k, v in (opts or {}).items():
            if k == "max-download-limit":
                self.task_set_limit(gid, size_to_kbps(v))
        return True

    def rpc_ext(self, method, params, ctx):
        from . import api
        return api.dispatch(self, method, params, ctx)


def _bitfield_pct(bf, num_pieces=0):
    """aria2 bitfield（十六进制）→ 百分比。最后一字节有补位 0，必须按真实分块数算，否则满种显示 97%。"""
    if not bf:
        return 0
    ones = sum(bin(int(c, 16)).count("1") for c in bf if c in "0123456789abcdefABCDEF")
    total = int(num_pieces or 0) or len(bf) * 4
    return round(min(100.0, 100.0 * ones / total), 1)


def _to_trash(paths):
    """删除到回收站：Windows 用 SHFileOperation；其它系统移进 freedesktop 回收站（~/.local/share/Trash）。"""
    if os.name == "nt":
        return V.send_to_recycle(paths)
    moved, failed = [], []
    base = os.path.join(os.path.expanduser("~"), ".local", "share", "Trash")
    os.makedirs(os.path.join(base, "files"), exist_ok=True)
    os.makedirs(os.path.join(base, "info"), exist_ok=True)
    for p in paths:
        try:
            name = os.path.basename(p)
            dst = NM.unique_path(os.path.join(base, "files", name))
            shutil.move(p, dst)
            with open(os.path.join(base, "info", os.path.basename(dst) + ".trashinfo"), "w") as f:
                f.write("[Trash Info]\nPath=%s\nDeletionDate=%s\n" % (
                    os.path.abspath(p), datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")))
            moved.append(dst)
            PU._record("trash", p)
        except Exception:
            failed.append(p)
    return moved, failed
