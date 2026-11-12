# -*- coding: utf-8 -*-
"""sniffdl.* v3 接口注册表（扩展点）。

register(name, fn, ui=True)：fn(engine, params) → JSON 可序列化结果。
ui=True 的方法必须带界面令牌（请求头 X-SniffDL-Token 或 Authorization: Bearer），
防止本机任意网页借 CORS 漏洞调「运行命令 / 删除文件 / 关机」之类的危险接口。
不带令牌也能调的（ui=False）只有浏览器扩展需要的只读/安全方法。
aria2.* 与旧 sniffdl.*（扩展/LinkSwift 在用）保持原样，不在这里。
完整契约见 docs/接口契约.md。
"""
from . import platform_util as PU
from . import runtime as RT

_REG = {}


class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


def register(name, ui=True):
    def deco(fn):
        _REG[name] = (fn, ui)
        return fn
    return deco


def methods():
    return sorted(_REG)


def p(params, i, default=None):
    return params[i] if len(params) > i else default


def dispatch(eng, method, params, ctx):
    ent = _REG.get(method)
    if not ent:
        return NotImplemented
    fn, need_ui = ent
    if need_ui and not (ctx or {}).get("ui"):
        raise ApiError(-32001, "需要界面令牌（X-SniffDL-Token）")
    return fn(eng, list(params or []))


# ---------------- 基础
@register("sniffdl.ping", ui=False)
def _ping(e, a):
    return {"ok": True, "service": "SniffDL", "version": RT.VERSION, "port": e.port}


@register("sniffdl.methods")
def _methods(e, a):
    return methods()


@register("sniffdl.snapshot")
def _snapshot(e, a):
    return {"tasks": e.tasks(), "stats": e.stats(), "queues": e.queues.list(),
            "pending": [e._icpt_public(x) for x in e._pending_icpt.values()],
            "grabber": list(e._grabber.values()), "version": RT.VERSION, "port": e.port}


@register("sniffdl.core.shutdown")
def _shutdown(e, a):
    import threading
    threading.Timer(0.3, e.shutdown).start()
    return {"ok": True}


# ---------------- 识别 / 添加
@register("sniffdl.inspect")
def _inspect(e, a):
    return e.inspect(str(p(a, 0, "")), probe=bool(p(a, 1, True)))


@register("sniffdl.torrent.upload")
def _torrent_upload(e, a):
    """界面拖入 .torrent（浏览器拿不到本地路径）：base64 落到 内核数据目录/torrents/，返回本地路径供识别/下载。"""
    import base64, os, re, time
    raw = base64.b64decode(str(p(a, 0, "")) + "=" * (-len(str(p(a, 0, ""))) % 4))
    if raw[:1] != b"d":
        raise ApiError(-32602, "不是有效的 .torrent 文件")
    name = re.sub(r'[\\/:*?"<>|]', "_", str(p(a, 1, "upload.torrent")))[-120:] or "upload.torrent"
    d = os.path.join(_data_dir(), "torrents")
    os.makedirs(d, exist_ok=True)
    fp = os.path.join(d, "%d_%s" % (int(time.time() * 1000), name if name.endswith(".torrent") else name + ".torrent"))
    with open(fp, "wb") as f:
        f.write(raw)
    return {"ok": True, "path": fp}


@register("sniffdl.task.add")
def _add(e, a):
    return e.add_task(p(a, 0, {}) or {})


@register("sniffdl.task.addBatch")
def _add_batch(e, a):
    return e.add_batch(str(p(a, 0, "")), p(a, 1, {}) or {})


@register("sniffdl.batch.expand")
def _expand(e, a):
    from . import batch as B
    urls, trunc = B.expand_text(str(p(a, 0, "")))
    return {"urls": urls[:5000], "count": len(urls), "truncated": trunc}


# ---------------- 任务
@register("sniffdl.tasks")
def _tasks(e, a):
    return e.tasks()


@register("sniffdl.task.detail")
def _detail(e, a):
    return e.task_detail(str(p(a, 0, "")))


@register("sniffdl.task.pause")
def _pause(e, a):
    return e.task_action("pause", _gids(a))


@register("sniffdl.task.resume")
def _resume(e, a):
    return e.task_action("resume", _gids(a))


@register("sniffdl.task.later")
def _later(e, a):
    return e.task_action("later", _gids(a))


@register("sniffdl.task.remove")
def _remove(e, a):
    c = p(a, 2, None)                 # cancel_cleanup=ask 时界面问到的「同时删掉未完成的临时文件？」
    return e.task_remove(_gids(a), bool(p(a, 1, False)), None if c is None else bool(c))


@register("sniffdl.task.refreshUrl")
def _refresh(e, a):
    return e.task_refresh(str(p(a, 0, "")), str(p(a, 1, "")), p(a, 2))


@register("sniffdl.task.update")
def _update(e, a):
    return e.task_update(str(p(a, 0, "")), p(a, 1, {}) or {})


@register("sniffdl.task.setLimit")
def _set_limit(e, a):
    return e.task_set_limit(str(p(a, 0, "")), p(a, 1, 0))


@register("sniffdl.task.open")
def _open(e, a):
    return e.rpc_open_file(str(p(a, 0, "")))


@register("sniffdl.task.reveal")
def _reveal(e, a):
    return e.rpc_reveal(str(p(a, 0, "")))


@register("sniffdl.task.clear")
def _clear(e, a):
    return {"ok": True, "removed": e.rpc_clear_queue(done_only=(p(a, 0, "done") != "all"))}


@register("sniffdl.tasks.export")
def _export(e, a):
    return e.export_tasks(bool(p(a, 0, True)))


@register("sniffdl.tasks.import")
def _import(e, a):
    return e.import_tasks(p(a, 0, ""), bool(p(a, 1, False)))


def _gids(a):
    g = p(a, 0, [])
    if isinstance(g, str):
        return [g]
    return [str(x) for x in (g or [])]


# ---------------- BT
@register("sniffdl.bt.detail")
def _bt_detail(e, a):
    return e.bt_detail(str(p(a, 0, "")))


@register("sniffdl.bt.select")
def _bt_select(e, a):
    return e.bt_select(str(p(a, 0, "")), p(a, 1, []))


@register("sniffdl.bt.sequential")
def _bt_seq(e, a):
    return e.bt_set_seq(str(p(a, 0, "")), bool(p(a, 1, True)))


@register("sniffdl.bt.exportTorrent")
def _bt_export(e, a):
    return e.bt_export_torrent(str(p(a, 0, "")))


# ---------------- 队列 / 计划
@register("sniffdl.queues")
def _queues(e, a):
    return e.queues.list()


@register("sniffdl.queue.save")
def _queue_save(e, a):
    try:
        q = e.queues.upsert(p(a, 0, {}) or {})
    except Exception as ex:
        raise ApiError(-32602, "队列设置不对：%s" % ex)
    e.bus.emit("queues", {"queues": e.queues.list()})
    e._kick()
    return {"ok": True, "queue": q}


@register("sniffdl.queue.remove")
def _queue_remove(e, a):
    qid = str(p(a, 0, ""))
    try:
        e.queues.remove(qid)
    except ValueError as ex:
        raise ApiError(-32602, str(ex))
    for t in e._rpc_tasks.values():
        if t.get("queue") == qid:
            t["queue"] = "main"
    e.bus.emit("queues", {"queues": e.queues.list()})
    return {"ok": True}


@register("sniffdl.queue.startStop")
def _queue_ss(e, a):
    qid, run = str(p(a, 0, "main")), bool(p(a, 1, True))
    if run:
        e.queues.paused_queues.discard(qid)
        for t in e._rpc_tasks.values():
            if (t.get("queue") or "main") == qid:
                t.pop("_q_paused", None)
            if (t.get("queue") or "main") == qid and t.get("status") in ("paused",) and not t.get("errorMessage"):
                t["status"] = "waiting"
            if (t.get("queue") or "main") == qid and t.get("hold") and t.get("status") == "waiting":
                t["hold"] = False
    else:
        e.queues.paused_queues.add(qid)
        for t in list(e._rpc_tasks.values()):
            if (t.get("queue") or "main") == qid and t.get("status") == "active":
                e.rpc_pause(t["gid"])
                t["status"] = "waiting"
                t["_q_paused"] = True
    e._kick()
    e.bus.emit("queues", {"queues": e.queues.list(), "paused": sorted(e.queues.paused_queues)})
    return {"ok": True, "paused": sorted(e.queues.paused_queues)}


@register("sniffdl.all.pause")
def _pause_all(e, a):
    return e.task_action("pause", [t["gid"] for t in e.tasks() if t["status"] in ("active", "waiting")])


@register("sniffdl.all.resume")
def _resume_all(e, a):
    return e.task_action("resume", [t["gid"] for t in e.tasks() if t["status"] in ("paused",)])


# ---------------- 设置
@register("sniffdl.settings.get")
def _settings_get(e, a):
    return e.settings_get()


@register("sniffdl.settings.set")
def _settings_set(e, a):
    return e.settings_set(p(a, 0, {}) or {})


@register("sniffdl.turtle")
def _turtle(e, a):
    on = p(a, 0)
    on = (not bool(e.cfg.get("turtle"))) if on is None else bool(on)
    return e.settings_set({"turtle": on})


@register("sniffdl.takeoverRules", ui=False)
def _takeover(e, a):
    return e.takeover_rules()


# ---------------- 交互：确认 / 剪贴板 / 待确认区 / 完成后动作
@register("sniffdl.intercept.answer")
def _icpt_answer(e, a):
    return e.intercept_answer(str(p(a, 0, "")), bool(p(a, 1, True)), p(a, 2, {}) or {})


@register("sniffdl.clip.offer")
def _clip_offer(e, a):
    return {"ok": True, "taken": e.clip.offer(str(p(a, 0, "")), source="host")}


@register("sniffdl.grabber.push", ui=False)
def _grab_push(e, a):
    return e.grabber_push(p(a, 0, []) or [], str(p(a, 1, "ext")))


@register("sniffdl.grabber.list")
def _grab_list(e, a):
    return list(e._grabber.values())


@register("sniffdl.grabber.remove")
def _grab_rm(e, a):
    urls = p(a, 0, [])
    if urls == "all":
        e._grabber.clear()
    else:
        for u in urls or []:
            e._grabber.pop(u, None)
    e.bus.emit("grabber", {"count": len(e._grabber), "reset": True, "items": list(e._grabber.values())})
    return {"ok": True, "count": len(e._grabber)}


@register("sniffdl.power.cancel")
def _power_cancel(e, a):
    PU.cancel_shutdown()
    return e.power_cancel()


# ---------------- 统计 / 日志 / 系统
@register("sniffdl.stats.now")
def _stats_now(e, a):
    return e.stats()


@register("sniffdl.stats.speedHistory")
def _speed_hist(e, a):
    return e.speed_history()


@register("sniffdl.logs")
def _logs(e, a):
    n = int(p(a, 0, 300) or 300)
    return list(e._log_ring)[-n:]


@register("sniffdl.os.openPath")
def _open_path(e, a):
    path = str(p(a, 0, ""))
    import os
    if not path or not os.path.exists(path):
        return {"ok": False, "error": "路径不存在"}
    PU.open_path(path)
    return {"ok": True}


@register("sniffdl.os.listDirs")
def _list_dirs(e, a):
    return e.rpc_list_dirs(str(p(a, 0, "")))


@register("sniffdl.debug.osActions")
def _os_actions(e, a):
    return PU.ACTIONS[-int(p(a, 0, 50) or 50):]


@register("sniffdl.sniff.start")
def _sniff_start(e, a):
    return {"task": e.rpc_sniff_start(str(p(a, 0, "")), str(p(a, 1, "page")))}


@register("sniffdl.sniff.result")
def _sniff_result(e, a):
    return e.rpc_sniff_result(str(p(a, 0, "")))


@register("sniffdl.pet")
def _pet(e, a):
    act = str(p(a, 0, "say"))
    if not e._pet:
        return {"ok": False, "error": "桌宠未连接"}
    if act == "say":
        e._pet.say(str(p(a, 1, "")))
    elif act == "react":
        e._pet.react(str(p(a, 1, "idle")))
    elif act == "visible":
        e._pet.visible(bool(p(a, 1, True)))
    return {"ok": True}



# ================= WP1a（2026-11-09）：把旧内核已有能力包成 sniffdl.* =================
def _V():
    import video_downloader as V
    return V


def _data_dir():
    """内核数据目录（--data-dir / SNIFFDL_DATA_DIR；默认程序目录）。sniffdl_core 启动时已把 V.BASE_DIR 指过去。"""
    return _V().BASE_DIR


# ---------------- 登录与 Cookie
@register("sniffdl.cookie.info")
def _cookie_info(e, a):
    return e.rpc_cookie_info()


@register("sniffdl.cookie.login")
def _cookie_login(e, a):
    return e.rpc_login_cookie(str(p(a, 0, "") or ""))


@register("sniffdl.cookie.loginStatus")
def _cookie_login_status(e, a):
    return e.rpc_login_status()


@register("sniffdl.cookie.import")
def _cookie_import(e, a):
    r = e.rpc_import_cookie(str(p(a, 0, "") or ""))
    if r.get("ok"):
        e.bus.emit("settings", {"changed": {"cookie_browser": e.cfg.get("cookie_browser")}})
    return r


# ---------------- 组件
@register("sniffdl.components.list")
def _comp_list(e, a):
    return e.rpc_components()


@register("sniffdl.components.check")
def _comp_check(e, a):
    return e.rpc_check_updates()


@register("sniffdl.components.update")
def _comp_update(e, a):
    return e.rpc_update_one(str(p(a, 0, "") or ""))


# ---------------- 实时语音网关
def _rt_key(e, k):
    from . import settings as S
    k = str(k or "")
    return "" if k == S.SECRET_MASK else k        # 界面回传掩码 = 用已存的密钥


@register("sniffdl.rt.test")
def _rt_test(e, a):
    r = e.rpc_test_gateway(str(p(a, 0, "") or ""), _rt_key(e, p(a, 1, "")), str(p(a, 2, "") or ""))
    r.pop("key", None)
    return r


@register("sniffdl.rt.models")
def _rt_models(e, a):
    return e.rpc_list_models(str(p(a, 0, "") or ""), _rt_key(e, p(a, 1, "")))


# ---------------- 统计
@register("sniffdl.stats.history")
def _stats_history(e, a):
    """最近 n 天（默认 112 = 16 周）每日 {date, bytes, count} + 今日/累计 KPI。账本兼容 n / count 两种键。"""
    import datetime
    V = _V()
    n = max(1, min(3660, int(p(a, 0, 112) or 112)))
    d = V.load_stats()
    today = datetime.date.today()
    days = []
    for i in range(n - 1, -1, -1):
        k = (today - datetime.timedelta(days=i)).isoformat()
        rec = d.get(k) if isinstance(d.get(k), dict) else {}
        days.append({"date": k, "bytes": int(rec.get("bytes", 0) or 0),
                     "count": int(rec.get("n", rec.get("count", 0)) or 0)})
    allv = [v for v in d.values() if isinstance(v, dict)]
    tb = days[-1]
    return {"ok": True, "days": days, "today_bytes": tb["bytes"], "today_count": tb["count"],
            "total_bytes": sum(int(v.get("bytes", 0) or 0) for v in allv),
            "total_count": sum(int(v.get("n", v.get("count", 0)) or 0) for v in allv),
            "active_days": sum(1 for v in allv if int(v.get("n", v.get("count", 0)) or 0) > 0)}


@register("sniffdl.stats.reset")
def _stats_reset(e, a):
    V = _V()
    old = V.load_stats()
    V.save_stats({})
    e.log("[统计] 已重置（清掉 %d 天记录）" % len(old))
    e.bus.emit("stats", {"reset": True})
    return {"ok": True, "cleared_days": len(old)}


# ---------------- 系统
@register("sniffdl.os.openOutdir")
def _open_outdir(e, a):
    return e.rpc_open_outdir(p(a, 0) or None)


@register("sniffdl.os.openLog")
def _open_log(e, a):
    import os
    lf = _V().LOG_FILE
    if not os.path.exists(lf):
        try:
            open(lf, "a", encoding="utf-8").close()
        except Exception as ex:
            return {"ok": False, "error": str(ex)[:120], "path": lf}
    PU.open_path(lf)
    return {"ok": True, "path": lf}


# ---------------- 慢速取片（可取消后台任务）
def _slow_jobs(e):
    if not hasattr(e, "_slow_jobs"):
        e._slow_jobs = {}
    return e._slow_jobs


def _slow_pub(j):
    return {k: j[k] for k in ("id", "url", "state", "path", "error", "started", "finished")} | {"log": j["log"][-50:]}


@register("sniffdl.sniff.slow")
def _sniff_slow(e, a):
    """开一个慢速取片后台任务，立刻返回 {id}；用 sniff.slowStatus 轮询，sniff.slowCancel 取消。"""
    import threading, time, uuid, os
    url = str(p(a, 0, "") or "").strip()
    if not url.lower().startswith("http"):
        raise ApiError(-32602, "需要 http(s) 页面地址")
    save_dir = str(p(a, 1, "") or "") or (e.cfg.get("outdir") or _V().DEFAULT_OUTDIR)
    jid = uuid.uuid4().hex[:12]
    evt = threading.Event()
    j = {"id": jid, "url": url, "state": "running", "path": "", "error": "", "log": [],
         "started": time.time(), "finished": 0, "_evt": evt}
    _slow_jobs(e)[jid] = j

    def log(m):
        j["log"].append(str(m))
        try:
            e.log(str(m))
        except Exception:
            pass

    def worker():
        try:
            os.makedirs(os.path.expanduser(save_dir), exist_ok=True)
            fp = _V().slow_fetch_page(url, log, save_dir=os.path.expanduser(save_dir), cancel_evt=evt)
            if evt.is_set():
                j["state"] = "cancelled"
            elif fp:
                j["state"], j["path"] = "done", fp
            else:
                j["state"], j["error"] = "error", (j["log"][-1] if j["log"] else "没取到")
        except Exception as ex:
            j["state"], j["error"] = ("cancelled" if evt.is_set() else "error"), str(ex)[:200]
        j["finished"] = time.time()
        e.bus.emit("slow", _slow_pub(j))

    threading.Thread(target=worker, daemon=True, name="slow-" + jid).start()
    return {"ok": True, "id": jid}


@register("sniffdl.sniff.slowStatus")
def _sniff_slow_status(e, a):
    jid = str(p(a, 0, "") or "")
    jobs = _slow_jobs(e)
    if not jid:
        return [_slow_pub(j) for j in jobs.values()]
    j = jobs.get(jid)
    if not j:
        raise ApiError(-32602, "没有这个慢速取片任务")
    return _slow_pub(j)


@register("sniffdl.sniff.slowCancel")
def _sniff_slow_cancel(e, a):
    j = _slow_jobs(e).get(str(p(a, 0, "") or ""))
    if not j:
        raise ApiError(-32602, "没有这个慢速取片任务")
    j["_evt"].set()
    if j["state"] == "running":
        j["state"] = "cancelling"
    return {"ok": True, "state": j["state"]}


# ---------------- 自检（离线、几秒内完成）
@register("sniffdl.selftest")
def _selftest(e, a):
    import os, shutil, socket, subprocess, tempfile
    V = _V()
    items = []

    def add(name, ok, detail="", fix=""):
        items.append({"name": name, "ok": bool(ok), "detail": str(detail)[:300], "fix": fix})

    def ver(cmd):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
            return ((r.stdout or r.stderr).strip().splitlines() or [""])[0]
        except Exception as ex:
            return "运行失败：%s" % str(ex)[:80]

    try:
        a2 = V.resolve_aria2c(e.cfg) or V.ARIA2C
    except Exception:
        a2 = V.ARIA2C or shutil.which("aria2c")
    add("aria2c", a2 and os.path.exists(a2), "%s  %s" % (a2, ver([a2, "--version"])) if a2 else "没找到",
        "" if a2 else "把 aria2c 放进程序目录或 PATH")
    ff = getattr(V, "FFMPEG", None) or shutil.which("ffmpeg")
    add("ffmpeg", ff and os.path.exists(ff), "%s  %s" % (ff, ver([ff, "-version"])) if ff else "没找到",
        "" if ff else "合并音视频需要 ffmpeg，可在「组件」里安装")
    try:
        import yt_dlp
        add("yt-dlp", True, "版本 %s" % yt_dlp.version.__version__)
    except Exception as ex:
        add("yt-dlp", False, str(ex)[:120], "在「组件」里升级 yt-dlp")
    dd = _data_dir()
    try:
        fd, tp = tempfile.mkstemp(prefix=".sdl_w_", dir=dd)
        os.close(fd)
        os.remove(tp)
        add("数据目录可写", True, dd)
    except Exception as ex:
        add("数据目录可写", False, "%s：%s" % (dd, str(ex)[:100]), "换个有写权限的位置")
    od = os.path.expanduser(e.cfg.get("outdir") or V.DEFAULT_OUTDIR)
    try:
        os.makedirs(od, exist_ok=True)
        fd, tp = tempfile.mkstemp(prefix=".sdl_w_", dir=od)
        os.close(fd)
        os.remove(tp)
        free = shutil.disk_usage(od).free
        add("保存目录可写", True, "%s（剩余 %.1f GB）" % (od, free / 1073741824))
    except Exception as ex:
        add("保存目录可写", False, "%s：%s" % (od, str(ex)[:100]), "在设置里换保存位置")
    want = int(e.cfg.get("rpc_port") or 6800)
    try:
        c = socket.create_connection(("127.0.0.1", int(e.port)), timeout=2)
        c.close()
        listening = True
    except Exception:
        listening = False
    add("内核端口", listening, "正在监听 127.0.0.1:%s" % e.port if listening else "端口 %s 连不上" % e.port)
    add("aria2 兼容端口 %d" % want, int(e.port) == want,
        "LinkSwift 可直接推送" if int(e.port) == want else "%d 被占用，已顺延到 %s；LinkSwift 里要改端口" % (want, e.port),
        "" if int(e.port) == want else "关掉占用 %d 的程序（常见：另一个 aria2 / Motrix）" % want)
    return {"ok": all(i["ok"] for i in items), "items": items}


# ---------------- BT 批量
def _bt_gids(e, statuses):
    return [t["gid"] for t in e.tasks() if t.get("kind") == "bt" and t.get("status") in statuses]


@register("sniffdl.bt.pauseAll")
def _bt_pause_all(e, a):
    return e.task_action("pause", _bt_gids(e, ("active", "waiting")))


@register("sniffdl.bt.resumeAll")
def _bt_resume_all(e, a):
    return e.task_action("resume", _bt_gids(e, ("paused",)))
