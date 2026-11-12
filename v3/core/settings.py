# -*- coding: utf-8 -*-
"""设置表：唯一的设置来源（默认值 / 类型 / 分组 / 校验 / 说明）。

界面不硬编码任何设置项：它调 sniffdl.settings.schema 拿到这张表，「常用」组直接显示，
其余按 section 进「高级」折叠。新增设置 = 在 SCHEMA 里加一行（扩展点）。
旧键名（quality / dl_threads / proxy_mode / cookie_browser / sniff_browser / after / rx_cap_gb /
outdir / rpc_* / pet_live / tray_close / dash_aria2 / bt_trackers / rpc_confirm_dialog / deep）原样沿用，
老 config.json 不用迁移。
"""
import re

DEFAULT_TRACKERS_SUB = "https://cdn.jsdelivr.net/gh/ngosang/trackerslist@master/trackers_best.txt"

DEFAULT_CATEGORY_RULES = "\n".join([
    "视频=mp4,mkv,mov,avi,webm,flv,wmv,m4v,ts,mpg,mpeg,3gp",
    "音频=mp3,flac,wav,m4a,aac,ogg,opus,wma",
    "压缩文件=zip,rar,7z,tar,gz,tgz,bz2,xz",
    "应用程序=exe,msi,msp,apk,ipa,deb,rpm,dmg,pkg,appimage,jar",
    "文档=pdf,epub,mobi,azw3,doc,docx,xls,xlsx,ppt,pptx,txt,md,csv",
    "图片=png,jpg,jpeg,gif,webp,bmp,svg,ico,tif,tiff,heic",
])

FORMAT_PRESETS = ["最佳", "2160p", "1440p", "1080p", "720p", "480p", "仅音频 MP3", "仅音频 M4A", "MP4（兼容）", "MKV"]

DEFAULT_BT_EXCLUDE = "\n".join(["*.url", "*.lnk", "*.html", "*.htm", "*.mht", "_____padding_file*", ".pad/*",
                                "*广告*", "*推广*", "*更多资源*", "*最新地址*", "*关注*.txt", "*.apk.txt"])

DEFAULT_TAKEOVER_EXTS = ("mp4,mkv,webm,m4v,mov,flv,ts,m3u8,mpd,mp3,flac,wav,aac,ogg,m4a,opus,"
                         "zip,rar,7z,iso,exe,msi,apk,dmg,pdf")

# type: bool / int / float / str / text / select / path / time
# group: common = 设置页最上面「常用」；advanced = 折叠在「高级」里（按 section 分卡）
SCHEMA = [
    # ---------------- 常用 ----------------
    dict(key="outdir", label="保存位置", type="path", default="", group="common", section="常用",
         help="留空 = 系统「视频/SniffDL」目录"),
    dict(key="max_concurrent", label="同时下载数", type="int", default=5, min=1, max=16,
         group="common", section="常用", help="超出的任务自动排队，前面的下完立刻接上"),
    dict(key="speed_limit_kbps", label="全局限速", type="int", default=0, min=0, max=10_000_000,
         unit="KB/s", group="common", section="常用", help="0 = 不限速"),
    dict(key="format_preset", label="视频格式", type="select", default="最佳",
         options=FORMAT_PRESETS, group="common", section="常用",
         help="视频站下载时的默认格式；添加时也可单独选"),
    dict(key="quality", label="画质上限（旧）", type="select", default="最佳",
         options=["最佳", "2160", "1440", "1080", "720", "480", "360"], group="hidden", section="常用"),
    dict(key="clip_watch", label="剪贴板监视", type="bool", default=True, group="common", section="常用",
         help="复制了可下载的链接时右下角提示，一键下载"),
    dict(key="after", label="全部完成后", type="select", default="无",
         options=["无", "关机", "重启", "睡眠", "休眠", "退出程序"], group="common", section="常用",
         help="关机/睡眠前有 60 秒倒计时，可在界面上取消"),
    dict(key="notify", label="完成通知", type="bool", default=True, group="common", section="常用"),
    dict(key="sound", label="完成提示音", type="bool", default=True, group="common", section="常用"),
    dict(key="ui_theme", label="主题", type="select", default="跟随系统",
         options=["跟随系统", "浅色", "深色"], group="common", section="外观"),
    # ---- 外观 · 背景图（设置页「外观」卡里用缩略图点选；旧版 ui_bg 键名沿用）----
    dict(key="ui_bg", label="界面背景", type="select", default="none",
         options=["none", "bg1", "bg2", "bg3", "bg4", "custom"], group="common", section="外观",
         help="点缩略图即时切换；自定义 = 选一张本机图片"),
    dict(key="ui_bg_custom", label="自定义背景图片", type="str", default="", group="common", section="外观",
         help="本机图片路径（jpg / png / webp）或 http(s) 图片地址"),
    dict(key="ui_bg_fit", label="填充方式", type="select", default="自动",
         options=["自动", "铺满", "完整"], group="common", section="外观",
         help="自动：内置插画完整显示人物，自定义图片铺满窗口"),
    dict(key="ui_bg_pos", label="对齐位置", type="select", default="自动",
         options=["自动", "右", "居中", "左", "顶部", "底部"], group="common", section="外观",
         help="图片比窗口大时保留哪一侧（人物别被裁掉）"),
    dict(key="ui_bg_opacity", label="背景透明度", type="int", default=100, min=10, max=100, unit="%",
         group="common", section="外观", slider=True),
    dict(key="ui_bg_blur", label="背景模糊", type="int", default=0, min=0, max=24, unit="px",
         group="common", section="外观", slider=True),
    dict(key="ui_bg_dim", label="遮罩强度", type="int", default=60, min=0, max=100, unit="%",
         group="common", section="外观", slider=True, help="越高文字越清晰、插画越淡（左侧重、右侧轻）"),

    dict(key="pet_live", label="显示桌宠大肥鱼", type="bool", default=True, group="common", section="常用"),
    dict(key="tray_close", label="关闭窗口时缩到托盘", type="bool", default=True, group="common", section="常用"),
    dict(key="autostart", label="开机自启", type="bool", default=False, group="common", section="常用"),

    dict(key="turtle", label="乌龟模式（备用限速）", type="bool", default=False, group="common", section="常用",
         help="一键切到下面的备用限速；托盘菜单里也能切"),
    dict(key="keep_awake", label="下载时不让电脑睡眠", type="bool", default=True, group="common", section="常用"),
    dict(key="subs", label="视频带字幕", type="bool", default=False, group="common", section="常用",
         help="有字幕就一起下（语言见高级 · 视频）"),

    # ---------------- 高级 · 下载 ----------------
    dict(key="task_conns", label="单任务连接数", type="select", default="自动",
         options=["自动", "1", "2", "4", "8", "16"], group="advanced", section="下载",
         help="直链多连接分段数（网盘按各家防封上限自动收紧）"),
    dict(key="max_conns", label="最大连接数", type="select", default="自动",
         options=["自动", "8", "16", "32", "64", "128"], group="advanced", section="下载",
         help="直链/网盘单文件最多开几路。自动 = 从 8 路起按实测速度加，每加一批提速不到 5% 就停，最多 64 路；"
              "遇到 429/503/连接被重置自动减。超过 16 路时改走 SniffDL 自研引擎（aria2 最多 16 路，仍作回退）"),
    dict(key="dl_threads", label="视频分片线程", type="select", default="自动",
         options=["自动", "16", "32", "64", "128", "256"], group="advanced", section="下载"),
    dict(key="dash_aria2", label="大站单文件也多线程", type="bool", default=True, group="advanced", section="下载"),
    dict(key="auto_category", label="按类型自动分到子文件夹", type="bool", default=True,
         group="advanced", section="下载"),
    dict(key="category_rules", label="分类规则（文件夹=扩展名）", type="text", default=DEFAULT_CATEGORY_RULES,
         group="advanced", section="下载"),
    dict(key="site_rules", label="按站点分类（域名=文件夹）", type="text", default="",
         group="advanced", section="下载", help="例：bilibili.com=B站。优先于扩展名规则"),
    dict(key="name_template", label="文件名模板", type="str", default="{name}",
         group="advanced", section="下载",
         help="可用 {name} {ext} {date} {time} {host} {category}，例：{date}_{name}"),
    dict(key="conflict", label="重名时", type="select", default="跳过",
         options=["跳过", "重命名", "覆盖"], group="advanced", section="下载",
         help="跳过 = 已有同名文件就不重下（默认，防重复下载）"),
    dict(key="retry_count", label="失败自动重试次数", type="int", default=3, min=0, max=20,
         group="advanced", section="下载"),
    dict(key="retry_wait", label="重试间隔", type="int", default=10, min=1, max=3600, unit="秒",
         group="advanced", section="下载"),
    dict(key="fullspeed_window", label="仅在时段内不限速", type="bool", default=False,
         group="advanced", section="下载", help="开启后，全局限速只在下面时段之外生效（夜间全速）"),
    dict(key="fullspeed_start", label="全速开始", type="time", default="23:00", group="advanced", section="下载"),
    dict(key="fullspeed_end", label="全速结束", type="time", default="07:00", group="advanced", section="下载"),
    dict(key="rx_cap_gb", label="单次运行流量上限", type="float", default=8, min=0, max=100000, unit="GB",
         group="advanced", section="下载", help="防止误操作狂下；0 = 关闭"),

    dict(key="turtle_kbps", label="乌龟模式限速", type="int", default=512, min=1, max=10_000_000, unit="KB/s",
         group="advanced", section="下载"),
    dict(key="incomplete_suffix", label="未完成文件加 .sdl 后缀", type="bool", default=True,
         group="advanced", section="下载", help="下完才改回正式文件名，不会误开半截文件"),
    dict(key="disk_check", label="开始前检查磁盘空间", type="bool", default=True, group="advanced", section="下载",
         help="不够就不开始；下到一半磁盘满只暂停、进度保留"),
    dict(key="lowest_speed_kbps", label="低于此速度自动重连", type="int", default=0, min=0, max=100000, unit="KB/s",
         group="advanced", section="下载", help="0 = 关闭；某个连接卡在龟速时断开换新连接"),
    dict(key="max_file_not_found", label="404 几次就判失败", type="int", default=3, min=0, max=50,
         group="advanced", section="下载"),
    dict(key="remote_time", label="保留服务器文件时间", type="bool", default=False, group="advanced", section="下载"),
    dict(key="motw", label="给下载的文件打「来自网络」标记", type="bool", default=True, group="advanced", section="下载",
         help="Windows SmartScreen / Office 受保护视图照常检查"),
    dict(key="auto_extract", label="下完自动解压", type="bool", default=False, group="advanced", section="下载",
         help="zip/tar 内置；7z/rar/分卷需要本机装 7-Zip"),
    dict(key="extract_delete", label="解压成功后删除压缩包", type="bool", default=False, group="advanced", section="下载"),
    dict(key="extract_passwords", label="解压密码表（每行一个）", type="text", default="", group="advanced", section="下载"),
    dict(key="av_scan", label="下完杀毒扫描", type="bool", default=False, group="advanced", section="下载",
         help="默认调用 Windows Defender；也可在下面填自定义命令"),
    dict(key="av_cmd", label="自定义扫描命令", type="str", default="", group="advanced", section="下载",
         help="用 {file} 代表文件路径"),
    dict(key="watch_dir", label="监视文件夹", type="path", default="", group="advanced", section="下载",
         help="往里放 .torrent 或写满链接的 .txt，自动加入下载；处理过的文件移进 已添加 子目录"),

    # ---------------- 高级 · 视频 ----------------
    dict(key="sub_langs", label="字幕语言", type="str", default="zh-Hans,zh-CN,zh,en", group="advanced", section="视频"),
    dict(key="embed_subs", label="字幕嵌入视频", type="bool", default=True, group="advanced", section="视频"),
    dict(key="embed_thumb", label="嵌入封面", type="bool", default=False, group="advanced", section="视频"),
    dict(key="embed_meta", label="嵌入标题等元数据", type="bool", default=False, group="advanced", section="视频"),
    dict(key="embed_chapters", label="嵌入章节", type="bool", default=False, group="advanced", section="视频"),
    dict(key="ytdl_archive", label="记住下过的视频，重下列表时跳过", type="bool", default=False,
         group="advanced", section="视频"),
    dict(key="live_limit_min", label="直播录制时长上限", type="int", default=0, min=0, max=100000, unit="分钟",
         group="advanced", section="视频", help="0 = 不限"),
    dict(key="live_idle_sec", label="直播无新画面多久后停止", type="int", default=60, min=5, max=3600, unit="秒",
         group="advanced", section="视频"),

    # ---------------- 高级 · 自动化 ----------------
    dict(key="hook_added", label="添加任务时运行", type="str", default="", group="advanced", section="自动化",
         help="可用 {file} {dir} {name} {gid} {url} {status} {size}"),
    dict(key="hook_start", label="开始下载时运行", type="str", default="", group="advanced", section="自动化"),
    dict(key="hook_complete", label="下载完成时运行", type="str", default="", group="advanced", section="自动化"),
    dict(key="hook_error", label="下载出错时运行", type="str", default="", group="advanced", section="自动化"),
    dict(key="hook_seed_done", label="做种结束时运行", type="str", default="", group="advanced", section="自动化"),
    dict(key="webhook_url", label="Webhook 地址", type="str", default="", group="advanced", section="自动化",
         help="完成 / 出错时 POST 一段 JSON（可接手机推送、飞书机器人、Home Assistant）"),

    # ---------------- 高级 · 网络 ----------------
    dict(key="proxy_mode", label="代理", type="select", default="跟随系统",
         options=["跟随系统", "不使用", "手动"], group="advanced", section="网络"),
    dict(key="proxy_manual", label="手动代理地址", type="str", default="", group="advanced", section="网络",
         help="如 http://127.0.0.1:7890 或 socks5://127.0.0.1:1080"),
    dict(key="tls_warn", label="HTTPS 证书异常时提示", type="bool", default=True,
         group="advanced", section="网络"),

    # ---------------- 高级 · BT ----------------
    dict(key="bt_seed", label="下完继续做种", type="bool", default=False, group="advanced", section="BT"),
    dict(key="bt_seed_ratio", label="做种分享率上限", type="float", default=1.0, min=0, max=100,
         group="advanced", section="BT"),
    dict(key="bt_seed_minutes", label="做种时间上限", type="int", default=60, min=0, max=100000, unit="分钟",
         group="advanced", section="BT"),
    dict(key="bt_seed_idle_min", label="多久没人连就停止做种", type="int", default=0, min=0, max=100000,
         unit="分钟", group="advanced", section="BT", help="0 = 不按无活动停止"),
    dict(key="bt_stop_timeout", label="BT 零速多久自动停", type="int", default=0, min=0, max=100000, unit="秒",
         group="advanced", section="BT", help="下载卡死时让出名额；0 = 关闭"),
    dict(key="bt_meta_timeout", label="磁力找不到种子信息多久算失败", type="int", default=300, min=0, max=86400,
         unit="秒", group="advanced", section="BT", help="超时提示「无人做种」；0 = 一直等"),
    dict(key="bt_save_meta", label="磁力拿到元数据后存一份 .torrent", type="bool", default=True,
         group="advanced", section="BT", help="下次加同一磁力秒开，也能导出分享"),
    dict(key="bt_exclude", label="默认不勾选的文件（通配符，每行一个）", type="text", default=DEFAULT_BT_EXCLUDE,
         group="advanced", section="BT", help="广告/推广/填充文件"),
    dict(key="bt_dht", label="DHT", type="bool", default=True, group="advanced", section="BT"),
    dict(key="bt_pex", label="PEX（用户交换）", type="bool", default=True, group="advanced", section="BT"),
    dict(key="bt_lpd", label="LSD（局域网发现）", type="bool", default=True, group="advanced", section="BT"),
    dict(key="bt_port", label="监听端口", type="int", default=0, min=0, max=65535,
         group="advanced", section="BT", help="0 = 自动；UPnP 端口映射 aria2 不支持，需要时请在路由器手动映射"),
    dict(key="bt_tracker_auto", label="自动追加 tracker 列表", type="bool", default=True,
         group="advanced", section="BT"),
    dict(key="bt_tracker_sub", label="tracker 订阅地址", type="str", default=DEFAULT_TRACKERS_SUB,
         group="advanced", section="BT"),
    dict(key="bt_trackers", label="额外 tracker（每行一个）", type="text", default="",
         group="advanced", section="BT"),

    # ---------------- 高级 · 浏览器接管 ----------------
    dict(key="takeover_enable", label="接管浏览器下载", type="bool", default=True,
         group="advanced", section="浏览器接管"),
    dict(key="takeover_exts", label="接管这些扩展名", type="text", default=DEFAULT_TAKEOVER_EXTS,
         group="advanced", section="浏览器接管"),
    dict(key="takeover_min_kb", label="小于此大小不接管", type="int", default=0, min=0, max=10_000_000,
         unit="KB", group="advanced", section="浏览器接管"),
    dict(key="takeover_whitelist", label="总是接管的站点（每行一个域名）", type="text", default="",
         group="advanced", section="浏览器接管"),
    dict(key="takeover_blacklist", label="从不接管的站点（每行一个域名）", type="text", default="",
         group="advanced", section="浏览器接管"),
    dict(key="takeover_alt_bypass", label="按住 Alt 点击时不接管", type="bool", default=True,
         group="advanced", section="浏览器接管"),
    dict(key="rpc_confirm_dialog", label="主动点下载时先确认", type="bool", default=True,
         group="advanced", section="浏览器接管", help="关掉后扩展里点「用 SniffDL 下载」直接开下"),

    # ---------------- 高级 · 嗅探与登录 ----------------
    dict(key="sniff_browser", label="嗅探用浏览器", type="select", default="自动",
         options=["自动", "Edge", "Chrome"], group="advanced", section="嗅探与登录"),
    dict(key="cookie_browser", label="Cookie 来源", type="select", default="自动",
         options=["自动", "已登录Cookie", "浏览器", "不使用"], group="advanced", section="嗅探与登录"),
    dict(key="deep", label="深度嗅探（进详情页）", type="bool", default=False, group="advanced", section="嗅探与登录"),

    # ---------------- 高级 · 外部推送（aria2 RPC） ----------------
    dict(key="rpc_enable", label="启用 aria2 兼容推送", type="bool", default=True,
         group="advanced", section="外部推送", help="LinkSwift / 网盘直链下载助手 选 aria2 推送即可"),
    dict(key="rpc_port", label="端口", type="int", default=6800, min=1, max=65535,
         group="advanced", section="外部推送", help="被占用时自动顺延，界面始终显示实际端口"),
    dict(key="rpc_token", label="密钥（token）", type="str", default="", group="advanced", section="外部推送",
         help="留空 = 无密钥（LinkSwift 默认）"),
    # ---------------- WP1a（2026-11-09）：提速引擎 / 外挂引擎 / BT 上传 / 实时语音 / 断点与清理 ----------------
    dict(key="speed_engine", label="直链下载引擎", type="select", default="auto",
         options=["auto", "aria2", "sdl"], group="advanced", section="提速引擎",
         help="auto = 先用 SniffDL 动态分片，失败自动回落 aria2；aria2 = 只用 aria2；sdl = 只用动态分片"),
    dict(key="sidecar_m3u8", label="m3u8 / mpd 交给 N_m3u8DL-RE", type="bool", default=True,
         group="advanced", section="外挂引擎", help="失败自动回落 yt-dlp"),
    dict(key="sidecar_bili", label="B 站交给 BBDown", type="bool", default=False,
         group="advanced", section="外挂引擎", help="需要组件里装好 BBDown；失败回落 yt-dlp"),
    dict(key="sidecar_paths", label="外挂程序路径（每行 名称=路径）", type="text", default="",
         group="advanced", section="外挂引擎", help="留空 = 自动在 vendor/ 与 PATH 里找"),
    dict(key="bt_upload_limit_kbps", label="BT 上传限速", type="int", default=0, min=0, max=10_000_000,
         unit="KB/s", group="advanced", section="BT", help="0 = 不限；对应 aria2 max-overall-upload-limit"),
    dict(key="rt_enable", label="启用桌宠实时语音", type="bool", default=False, group="advanced", section="实时语音"),
    dict(key="rt_base_url", label="网关地址", type="str", default="", group="advanced", section="实时语音",
         help="OpenAI 兼容接口，如 https://example.com/v1"),
    dict(key="rt_api_key", label="网关密钥", type="password", default="", secret=True,
         group="advanced", section="实时语音", help="只存在本机 config.json，界面不回显明文"),
    dict(key="rt_model", label="模型", type="str", default="", group="advanced", section="实时语音"),
    dict(key="rt_instructions", label="提示词", type="text", default="", group="advanced", section="实时语音"),
    dict(key="incomplete_dir", label="未完成文件临时目录", type="path", default="", group="advanced", section="下载",
         help="留空 = 直接下到保存位置；下完再移过去"),
    dict(key="cancel_cleanup", label="删除任务时清理残留", type="select", default="ask",
         options=["ask", "always", "never"], group="advanced", section="下载",
         help="ask = 删除时询问；always = 总是删掉未完成的分片/临时文件；never = 保留"),
    dict(key="resume_policy", label="断点续传策略", type="select", default="auto",
         options=["auto", "always", "restart"], group="advanced", section="下载",
         help="auto = 服务器支持就续传，不支持就从头；always = 不支持也保留已下部分；restart = 总是从头下"),
]

_BY_KEY = {s["key"]: s for s in SCHEMA}
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def spec(key):
    return _BY_KEY.get(key)


def defaults():
    return {s["key"]: s["default"] for s in SCHEMA}


def get(cfg, key):
    s = _BY_KEY.get(key)
    v = (cfg or {}).get(key)
    if v is None:
        return s["default"] if s else None
    return v


def validate(key, value):
    """把界面传来的值规整成正确类型；非法抛 ValueError（中文原因）。"""
    s = _BY_KEY.get(key)
    if not s:
        raise ValueError("未知设置项：%s" % key)
    t = s["type"]
    if t == "bool":
        if isinstance(value, str):
            return value.strip().lower() not in ("false", "0", "no", "off", "")
        return bool(value)
    if t in ("int", "float"):
        try:
            v = int(float(value)) if t == "int" else float(value)
        except Exception:
            raise ValueError("「%s」需要数字" % s["label"])
        if "min" in s and v < s["min"]:
            raise ValueError("「%s」不能小于 %s" % (s["label"], s["min"]))
        if "max" in s and v > s["max"]:
            raise ValueError("「%s」不能大于 %s" % (s["label"], s["max"]))
        return v
    if t == "select":
        v = str(value)
        if v not in s["options"]:
            raise ValueError("「%s」只能是：%s" % (s["label"], " / ".join(s["options"])))
        return v
    if t == "time":
        v = str(value).strip()
        if not _TIME_RE.match(v):
            raise ValueError("「%s」格式应为 HH:MM" % s["label"])
        h, m = v.split(":")
        return "%02d:%02d" % (int(h), int(m))
    v = "" if value is None else str(value)
    if t in ("str", "password") and len(v) > 2000:
        raise ValueError("「%s」太长" % s["label"])
    if t == "text" and len(v) > 20000:
        raise ValueError("「%s」太长" % s["label"])
    if key == "name_template" and not v.strip():
        v = "{name}"
    return v


def public_schema():
    out = []
    for s in SCHEMA:
        d = dict(s)
        if d.get("secret"):
            d["default"] = ""
        out.append(d)
    return out


SECRET_MASK = "********"


def secret_keys():
    return [s["key"] for s in SCHEMA if s.get("secret")]


def values(cfg):
    """给界面的值：密码类只回 "********"（已设）或 ""（未设），不回显明文。"""
    out = {}
    for s in SCHEMA:
        v = get(cfg, s["key"])
        if s.get("secret"):
            v = SECRET_MASK if v else ""
        out[s["key"]] = v
    return out
