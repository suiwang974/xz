# -*- coding: utf-8 -*-
"""v3 界面第二块冒烟：设置表单 / 队列编辑 + 计划短窗 / 待确认批量 / BT 文件+节点（aria2c 本地做种 + 自建 tracker）/ 404 一键处理。

真内核（隔离数据目录 /tmp/sdlv3p）→ /ui/ 托管 → Playwright。
用法：source env/setup.sh && source env/v3env.sh && python3 tests/ui_pages.py [--only settings,queues,pending,bt,error,bg] [--shots]
背景图（bg）：缩略图点选/滑块/自定义路径/失败回退/重启保持；--shots 时另出 docs/ui_shots/bg_*.png 并按截图取样算文字对比度。
退出码 0 = 全部通过；每项结果打印 PASS/FAIL。
"""
import argparse
import datetime
import hashlib
import http.server
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = "/tmp/sdlv3p"
SHOTS = os.path.join(HERE, "docs", "ui_shots")
TOKEN = "pagestoken456"
ARIA_DIR = "/workspace/work/sdl/speed/tools"
os.environ["PATH"] = ARIA_DIR + os.pathsep + os.environ.get("PATH", "")
FILE_SIZE = 6 * 1024 * 1024
BLOB = os.urandom(FILE_SIZE)
STATE = {"fixed404": False}
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("  PASS " if ok else "  FAIL ") + name + ("" if ok else "  ← " + str(detail)[:300]), flush=True)
    return ok


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Files(http.server.BaseHTTPRequestHandler):
    """/files/* 慢速文件（支持 Range）；/404/* 在 STATE['fixed404'] 之前回 404。"""
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/404/") and not STATE["fixed404"]:
            self.send_response(404)
            self.send_header("Content-Length", "9")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(b"not found")
            return
        rg = self.headers.get("Range")
        a, b = 0, FILE_SIZE
        if rg and rg.startswith("bytes="):
            s, _, e = rg[6:].partition("-")
            a = int(s or 0)
            b = int(e) + 1 if e else FILE_SIZE
            self.send_response(206)
            self.send_header("Content-Range", "bytes %d-%d/%d" % (a, b - 1, FILE_SIZE))
        else:
            self.send_response(200)
        nm = os.path.basename(urllib.parse.urlparse(self.path).path) or "x.bin"
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(b - a))
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % nm)
        self.end_headers()
        if self.command == "HEAD":
            return
        pos = a
        try:
            while pos < b:
                n = min(64 * 1024, b - pos)
                self.wfile.write(BLOB[pos:pos + n])
                pos += n
                time.sleep(0.04)
        except Exception:
            pass

    do_HEAD = do_GET


# ---------------- 最小 HTTP tracker（compact peers）
PEERS = {}


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
        return b"d" + b"".join(benc(k) + benc(x[k]) for k in sorted(x, key=lambda k: k.encode() if isinstance(k, str) else k)) + b"e"
    raise TypeError(x)


class Tracker(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query, keep_blank_values=True)
        port = int((q.get("port") or ["0"])[0])
        pid = (q.get("peer_id") or [""])[0]
        if (q.get("event") or [""])[0] == "stopped":
            PEERS.pop(pid, None)
        elif port:
            PEERS[pid] = port
        others = b"".join(socket.inet_aton("127.0.0.1") + struct.pack(">H", p) for k, p in PEERS.items() if k != pid)
        body = benc({"interval": 5, "min interval": 2, "complete": 1, "incomplete": 1, "peers": others})
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def make_torrent(root, announce, out):
    files = sorted(os.listdir(os.path.join(root, "pack")))
    plen = 64 * 1024
    data = b"".join(open(os.path.join(root, "pack", f), "rb").read() for f in files)
    pieces = b"".join(hashlib.sha1(data[i:i + plen]).digest() for i in range(0, len(data), plen))
    info = {"name": "pack", "piece length": plen, "pieces": pieces,
            "files": [{"length": os.path.getsize(os.path.join(root, "pack", f)), "path": [f]} for f in files]}
    with open(out, "wb") as fh:
        fh.write(benc({"announce": announce, "info": info, "comment": "sniffdl ui test"}))


def rpc(port, method, *params):
    req = urllib.request.Request("http://127.0.0.1:%d/jsonrpc" % port,
                                 data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": list(params)}).encode(),
                                 headers={"Content-Type": "application/json", "X-SniffDL-Token": TOKEN})
    with urllib.request.urlopen(req, timeout=15) as r:
        j = json.loads(r.read())
    if "error" in j:
        raise RuntimeError(j["error"])
    return j["result"]


def start_core(cport):
    log = open(os.path.join(DATA, "core.out"), "a")
    core = subprocess.Popen([sys.executable, os.path.join(HERE, "tests", "ui_smoke_core.py"), "--data-dir", DATA, "--port", str(cport),
                             "--runtime", os.path.join(DATA, "runtime.json"), "--ui-token", TOKEN],
                            cwd=HERE, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(120):
        try:
            if rpc(cport, "sniffdl.ping").get("ok"):
                return core
        except Exception:
            time.sleep(0.25)
    raise SystemExit("内核没起来，见 %s/core.out" % DATA)


def stop_core(cport, core):
    try:
        rpc(cport, "sniffdl.core.shutdown")
    except Exception:
        pass
    for _ in range(30):
        if core.poll() is not None:
            break
        time.sleep(0.2)
    if core.poll() is None:
        core.terminate()
        core.wait(5)


def wait(fn, timeout=10, step=0.25):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            last = fn()
            if last:
                return last
        except Exception as e:
            last = e
        time.sleep(step)
    return None


def open_ui(br, cport, scheme="light", vw=1320, vh=860, errors=None):
    ctx = br.new_context(viewport={"width": vw, "height": vh}, color_scheme=scheme, device_scale_factor=1, bypass_csp=True)
    pg = ctx.new_page()
    if errors is not None:
        pg.on("pageerror", lambda e: errors.append("pageerror: %s" % e))
        pg.on("console", lambda m: errors.append("console.%s: %s" % (m.type, m.text)) if m.type == "error" else None)
    pg.goto("http://127.0.0.1:%d/ui/index.html#port=%d&token=%s" % (cport, cport, TOKEN))
    pg.wait_for_function("window.__sdlReady === true", timeout=20000)
    return ctx, pg


def tasks(cport):
    return rpc(cport, "sniffdl.tasks")


def hm(dt):
    return dt.strftime("%H:%M")


# ======================================================================
def t_settings(br, cport, core, errors):
    print("[设置]", flush=True)
    ctx, pg = open_ui(br, cport, errors=errors)
    pg.keyboard.press("Alt+7")
    pg.wait_for_selector("#vSettings .set[data-key]")
    sch = rpc(cport, "sniffdl.settings.get")["schema"]
    n_vis = len([s for s in sch if s["group"] != "hidden"])
    n_rows = pg.locator("#vSettings .set[data-key]").count()
    check("表单按 schema 生成全部 %d 项" % n_vis, n_rows == n_vis, "页面 %d 行" % n_rows)
    first_sec = pg.evaluate("[...document.querySelectorAll('#vSettings [data-sec]')].map(e=>e.dataset.sec)")
    check("「常用」在最前，高级分卡", first_sec[0] == "常用" and len(first_sec) >= 6, first_sec)
    check("高级默认折叠", pg.evaluate("[...document.querySelectorAll('#vSettings details.card')].every(d=>!d.open)"))
    # 平台限制标注
    for k, tag in (("motw", "仅 Windows"), ("keep_awake", "仅 Windows"), ("av_scan", "仅 Windows"), ("auto_extract", "7-Zip"), ("bt_port", "UPnP")):
        txt = pg.text_content('#vSettings .set[data-key="%s"] .l' % k)
        check("平台限制标注 %s" % k, tag in txt, txt)
    # 搜索
    pg.fill("#setSearch", "解压")
    time.sleep(0.2)
    vis = pg.evaluate("[...document.querySelectorAll('#vSettings .set[data-key]:not(.hidden)')].map(e=>e.dataset.key)")
    check("搜索「解压」只剩相关项并展开所在分组", set(vis) >= {"auto_extract", "extract_delete", "extract_passwords"} and len(vis) <= 5
          and pg.evaluate("document.querySelector('#vSettings details[data-sec=\"下载\"]').open"), vis)
    pg.fill("#setSearch", "zzzz不存在")
    time.sleep(0.2)
    check("搜不到时提示", pg.is_visible("#setNone"))
    pg.fill("#setSearch", "")
    # 开关即时保存
    pg.click('#vSettings .set[data-key="sound"] .switch span')
    check("开关即时保存（sound=关）", wait(lambda: rpc(cport, "sniffdl.settings.get")["values"]["sound"] is False, 5))
    # 数字：回车保存
    pg.fill("#set_max_concurrent", "3")
    pg.press("#set_max_concurrent", "Enter")
    check("数字即时保存（同时下载数=3）", wait(lambda: rpc(cport, "sniffdl.settings.get")["values"]["max_concurrent"] == 3, 5))
    pg.fill("#set_max_concurrent", "99")
    pg.press("#set_max_concurrent", "Enter")
    err = wait(lambda: pg.inner_text('#vSettings .set[data-key="max_concurrent"] .err').strip(), 5)
    check("非法值显示中文原因且不保存", err and "16" in err and rpc(cport, "sniffdl.settings.get")["values"]["max_concurrent"] == 3, err)
    pg.fill("#set_max_concurrent", "3")
    pg.press("#set_max_concurrent", "Enter")
    # 下拉 + 文本 + 高级里的项
    pg.click('#vSettings details[data-sec="下载"] > summary')
    pg.select_option("#set_conflict", "重命名")
    pg.fill("#set_name_template", "{date}_{name}")
    pg.press("#set_name_template", "Enter")
    check("下拉/文本即时保存", wait(lambda: (lambda v: v["conflict"] == "重命名" and v["name_template"] == "{date}_{name}")(rpc(cport, "sniffdl.settings.get")["values"]), 5))
    # SSE 回显：外部改 → 控件跟着变
    rpc(cport, "sniffdl.settings.set", {"notify": False, "retry_count": 7})
    ok = wait(lambda: pg.evaluate("!document.getElementById('set_notify').checked && document.getElementById('set_retry_count').value==='7'"), 5)
    check("SSE 回显外部改动", ok)
    # 主题段
    pg.click('#set_ui_theme button[data-v="深色"]')
    check("外观即时切换", wait(lambda: pg.evaluate("document.documentElement.dataset.theme") == "dark", 3))
    pg.click('#set_ui_theme button[data-v="跟随系统"]')
    ctx.close()
    # 重启内核后仍在
    stop_core(cport, core)
    core = start_core(cport)
    v = rpc(cport, "sniffdl.settings.get")["values"]
    check("重启内核后设置仍在", v["sound"] is False and v["max_concurrent"] == 3 and v["conflict"] == "重命名"
          and v["name_template"] == "{date}_{name}" and v["retry_count"] == 7, {k: v[k] for k in ("sound", "max_concurrent", "conflict", "name_template", "retry_count")})
    ctx, pg = open_ui(br, cport, errors=errors)
    pg.keyboard.press("Alt+7")
    pg.wait_for_selector("#set_sound", state="attached")
    check("重启后界面读到保存值", pg.evaluate("!document.getElementById('set_sound').checked && document.getElementById('set_max_concurrent').value==='3'"))
    ctx.close()
    rpc(cport, "sniffdl.settings.set", {"conflict": "跳过", "name_template": "{name}", "retry_count": 3, "max_concurrent": 5})
    return core


# ======================================================================
def t_queues(br, cport, fport, errors):
    print("[队列]", flush=True)
    rpc(cport, "sniffdl.settings.set", {"speed_limit_kbps": 150})
    ctx, pg = open_ui(br, cport, errors=errors)
    pg.keyboard.press("Alt+6")
    pg.wait_for_selector("#qAdd")
    pg.click("#qAdd")
    pg.wait_for_selector(".modal input")
    pg.fill(".modal input", "夜间")
    pg.keyboard.press("Enter")
    q = wait(lambda: next((x for x in rpc(cport, "sniffdl.queues") if x["name"] == "夜间"), None), 5)
    check("新建队列", q)
    if not q:
        ctx.close()
        return
    qid = q["id"]
    card = '.qbox[data-q="%s"]' % qid
    pg.wait_for_selector(card + " #qMax")
    pg.fill(card + " #qMax", "1")
    pg.press(card + " #qMax", "Tab")
    now = datetime.datetime.now()
    pg.click(card + " #qSch + span")
    pg.fill(card + " #qStart", hm(now + datetime.timedelta(hours=2)))
    pg.fill(card + " #qStop", hm(now + datetime.timedelta(hours=3)))
    pg.press(card + " #qStop", "Tab")
    time.sleep(0.6)
    q = next(x for x in rpc(cport, "sniffdl.queues") if x["id"] == qid)
    check("同时数 + 计划写入内核", q["max"] == 1 and q["schedule"]["enabled"] and q["schedule"]["start"] == hm(now + datetime.timedelta(hours=2)), q)
    g1 = rpc(cport, "sniffdl.task.add", {"url": "http://127.0.0.1:%d/files/q1.bin" % fport, "queue": qid})["gid"]
    g2 = rpc(cport, "sniffdl.task.add", {"url": "http://127.0.0.1:%d/files/q2.bin" % fport, "queue": qid})["gid"]
    time.sleep(3)
    st = {t["gid"]: t["status"] for t in tasks(cport)}
    check("计划时段外：任务只排队不开始", st.get(g1) == "waiting" and st.get(g2) == "waiting", st)
    pg.wait_for_selector(card + ' .qt[data-gid="%s"]' % g1, timeout=5000)
    check("队列卡里列出任务", pg.locator(card + " .qt").count() == 2)
    # 打开时段（含当前分钟）
    now = datetime.datetime.now()
    pg.fill(card + " #qStart", hm(now - datetime.timedelta(minutes=1)))
    pg.fill(card + " #qStop", hm(now + datetime.timedelta(minutes=5)))
    pg.press(card + " #qStop", "Tab")
    act = wait(lambda: (lambda s: s.count("active") == 1 and s.count("waiting") == 1)([t["status"] for t in tasks(cport) if t["gid"] in (g1, g2)]), 8)
    check("进入时段：按队列同时数=1 只开 1 个", act, [t["status"] for t in tasks(cport) if t["gid"] in (g1, g2)])
    # 关闭时段（结束 = 当前分钟）→ 在跑的被暂停，标记计划暂停
    now = datetime.datetime.now()
    pg.fill(card + " #qStart", hm(now - datetime.timedelta(minutes=2)))
    pg.fill(card + " #qStop", hm(now))
    pg.press(card + " #qStop", "Tab")
    paused = wait(lambda: all(t["status"] == "waiting" for t in tasks(cport) if t["gid"] in (g1, g2)), 8)
    note = [t.get("note") for t in tasks(cport) if t["gid"] in (g1, g2)]
    check("时段结束：自动暂停在跑任务（标注下个时段继续）", paused and any("计划时段" in (n or "") for n in note), note)
    time.sleep(2.5)
    st = [t["status"] for t in tasks(cport) if t["gid"] in (g1, g2)]
    check("下载线程退出后仍是 waiting（不会变成手动暂停）", st == ["waiting", "waiting"], st)
    # 关计划 → 启停按钮
    pg.click(card + " #qSch + span")
    check("关掉计划后自动续上", wait(lambda: any(t["status"] == "active" for t in tasks(cport) if t["gid"] in (g1, g2)), 8))
    pg.click(card + ' [data-act="stop"]')
    check("停止队列：在跑任务暂停", wait(lambda: not any(t["status"] == "active" for t in tasks(cport) if t["gid"] in (g1, g2)), 6))
    pg.wait_for_selector(card + ' [data-act="start"]', timeout=5000)
    check("停止后显示「已停止」", "已停止" in pg.inner_text(card + " .qcard"))
    pg.click(card + ' [data-act="start"]')
    check("开始队列：任务续上", wait(lambda: any(t["status"] == "active" for t in tasks(cport) if t["gid"] in (g1, g2)), 8))
    # 拖到主队列
    time.sleep(1.2)
    src = pg.locator(card + ' .qt[data-gid="%s"]' % g2)
    src.drag_to(pg.locator('.qbox[data-q="main"] .qcard'))
    check("拖动任务到主队列", wait(lambda: next(t for t in tasks(cport) if t["gid"] == g2).get("queue") == "main", 5),
          next(t for t in tasks(cport) if t["gid"] == g2).get("queue"))
    # 行内下拉移回
    time.sleep(1.2)
    pg.select_option('.qbox[data-q="main"] .qt[data-gid="%s"] select' % g2, qid)
    check("行内下拉移到队列", wait(lambda: next(t for t in tasks(cport) if t["gid"] == g2).get("queue") == qid, 5))
    if SHOT:
        time.sleep(1.2)
        pg.screenshot(path=os.path.join(SHOTS, "p2_light_queues_1320.png"))
    # 改名
    pg.click(card + ' [data-act="rename"]')
    pg.wait_for_selector(".modal input")
    pg.fill(".modal input", "夜间下载")
    pg.keyboard.press("Enter")
    check("改名", wait(lambda: next(x for x in rpc(cport, "sniffdl.queues") if x["id"] == qid)["name"] == "夜间下载", 5))
    # 删除 → 任务回主队列
    time.sleep(1.2)
    pg.click(card + ' [data-act="delete"]')
    pg.wait_for_selector(".modal .btn.danger")
    pg.click(".modal .btn.danger")
    ok = wait(lambda: not any(x["id"] == qid for x in rpc(cport, "sniffdl.queues")), 5)
    check("删除队列，任务回主队列", ok and all(t.get("queue") == "main" for t in tasks(cport) if t["gid"] in (g1, g2)))
    rpc(cport, "sniffdl.task.remove", [g1, g2], True)
    rpc(cport, "sniffdl.settings.set", {"speed_limit_kbps": 0})
    ctx.close()


# ======================================================================
def t_pending(br, cport, fport, errors):
    print("[待确认]", flush=True)
    base = "http://127.0.0.1:%d/files/" % fport
    for nm in ("movie_a.mp4", "movie_b.mp4", "pack.zip"):
        rpc(cport, "sniffdl.intercept", {"url": base + nm, "filename": nm, "size": 5 << 20 if nm != "pack.zip" else 300 << 20,
                                         "source": "menu-link", "confirm": True, "referer": "https://example.com/page"})
    rpc(cport, "sniffdl.grabber.push", [{"url": base + "doc_%d.pdf" % i, "name": "doc_%d.pdf" % i} for i in range(3)]
        + [{"url": base + "song.mp3", "name": "song.mp3"}], "ext")
    ok = wait(lambda: len(rpc(cport, "sniffdl.snapshot")["pending"]) == 3, 5)
    check("造出 3 个待确认 + 4 个嗅探资源", ok and len(rpc(cport, "sniffdl.grabber.list")) == 4)
    ctx, pg = open_ui(br, cport, errors=errors)
    pg.keyboard.press("Alt+5")
    pg.wait_for_selector("#vPending .prow[data-key]")
    rows = lambda: pg.locator("#vPending .prow[data-key]").count()
    check("列出 7 项", wait(lambda: rows() == 7, 5), rows())
    check("侧栏徽标 = 7", pg.inner_text('[data-nav="pending"] .badge').strip() == "7")
    pg.select_option("#pendType", label=pg.evaluate("[...document.querySelectorAll('#pendType option')].find(o=>o.value==='视频').textContent"))
    check("按类型筛选（视频 2）", wait(lambda: rows() == 2, 3), rows())
    pg.select_option("#pendType", "全部类型")
    pg.select_option("#pendSize", "4")
    check("按大小筛选（100MB–1GB 只有 zip）", wait(lambda: rows() == 1, 3) and "pack.zip" in pg.inner_text("#vPending .plist"), rows())
    pg.select_option("#pendSize", "0")
    pg.fill("#pendQ", "doc_")
    check("按文件名筛选（doc_ 3 项）", wait(lambda: rows() == 3, 3), rows())
    pg.fill("#pendQ", "")
    wait(lambda: rows() == 7, 3)
    if SHOT:
        pg.click('#vPending .prow[data-key^="p:"] .pn')
        pg.click('#vPending .prow[data-key^="g:"] .pn')
        time.sleep(0.4)
        pg.screenshot(path=os.path.join(SHOTS, "p2_light_pending_1320.png"))
        pg.click('#vPending .prow.sel[data-key^="p:"] .pn')
        pg.click('#vPending .prow.sel[data-key^="g:"] .pn')
    # 勾选 2 个视频（等确认）+ 1 个 mp3（嗅探）→ 确认所选
    n0 = len(tasks(cport))
    pg.select_option("#pendType", "视频")
    pg.click("#pendAll")
    check("全选当前筛选", "确认所选 2" in pg.inner_text("#pendYes"), pg.inner_text("#pendYes"))
    pg.click("#pendYes")
    check("确认所选 → 开始 2 个下载", wait(lambda: len(tasks(cport)) == n0 + 2, 6) and len(rpc(cport, "sniffdl.snapshot")["pending"]) == 1,
          (len(tasks(cport)), n0))
    pg.select_option("#pendType", "全部类型")
    pg.fill("#pendQ", "song")
    pg.click('#vPending .prow[data-key] .pn')
    pg.click("#pendYes")
    check("嗅探资源确认 → 加入下载并移出列表", wait(lambda: len(tasks(cport)) == n0 + 3 and len(rpc(cport, "sniffdl.grabber.list")) == 3, 6))
    pg.fill("#pendQ", "")
    check("剩 4 项（1 待确认 + 3 嗅探）", wait(lambda: rows() == 4, 3), (rows(), pg.inner_text("#vPending .grab-filters")))
    pg.click("#pendNo")
    pg.wait_for_selector(".modal .btn.danger")
    pg.click(".modal .btn.danger")
    check("全部取消 → 清空", wait(lambda: not rpc(cport, "sniffdl.snapshot")["pending"] and not rpc(cport, "sniffdl.grabber.list"), 6)
          and wait(lambda: rows() == 0, 3) and len(tasks(cport)) == n0 + 3)
    check("徽标清零", wait(lambda: not pg.is_visible('[data-nav="pending"] .badge'), 3))
    ctx.close()
    rpc(cport, "sniffdl.task.remove", [t["gid"] for t in tasks(cport)], True)


# ======================================================================
def t_bt(br, cport, errors):
    print("[BT 文件 / 节点]", flush=True)
    root = os.path.join(DATA, "bt")
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(os.path.join(root, "pack"))
    for nm, n in (("a_video.mkv", 3 << 20), ("b_extra.bin", 2 << 20), ("c_readme.txt", 300 << 10)):
        with open(os.path.join(root, "pack", nm), "wb") as f:
            f.write(os.urandom(n))
    tport = free_port()
    tsrv = http.server.ThreadingHTTPServer(("127.0.0.1", tport), Tracker)
    threading.Thread(target=tsrv.serve_forever, daemon=True).start()
    tor = os.path.join(root, "pack.torrent")
    make_torrent(root, "http://127.0.0.1:%d/announce" % tport, tor)
    sport = free_port()
    seeder = subprocess.Popen(["aria2c", "--dir=" + root, "--check-integrity=true", "--seed-ratio=0.0", "--max-upload-limit=180K",
                               "--listen-port=%d" % sport, "--enable-dht=false", "--enable-dht6=false", "--bt-enable-lpd=false",
                               "--enable-peer-exchange=false", "--bt-tracker-interval=3", "--console-log-level=warn",
                               "--summary-interval=0", "--bt-detach-seed-only=true", tor],
                              stdout=open(os.path.join(DATA, "seeder.out"), "w"), stderr=subprocess.STDOUT)
    try:
        rpc(cport, "sniffdl.settings.set", {"bt_tracker_auto": False, "bt_dht": False, "bt_lpd": False, "bt_pex": False, "bt_seed": True,
                                            "bt_seed_minutes": 5})
        check("本地种子已向 tracker 报到", wait(lambda: len(PEERS) >= 1, 15), PEERS)
        r = rpc(cport, "sniffdl.task.add", {"url": tor})
        gid = r.get("gid")
        check("添加 .torrent 任务", gid, r)
        ctx, pg = open_ui(br, cport, errors=errors)
        pg.wait_for_selector('.task[data-gid="%s"]' % gid, timeout=10000)
        pg.evaluate("g => window.__sdl.openDetail(g)", gid)
        pg.wait_for_selector('#detail [data-tab="files"]', timeout=8000)
        pg.click('#detail [data-tab="files"]')
        ok = wait(lambda: pg.locator("#detail #dFiles tbody tr").count() == 3, 25)
        check("文件页列出 3 个文件", ok, pg.inner_text("#detail .db")[:200] if not ok else "")
        # 取消勾选 b_extra.bin → 应用
        idx_b = pg.evaluate("[...document.querySelectorAll('#dFiles tbody tr')].find(tr=>tr.textContent.includes('b_extra')).dataset.fi")
        pg.click('#dFiles tr[data-fi="%s"] input' % idx_b)
        check("改勾选后「应用选择」可点", pg.is_enabled("#dApplySel"))
        pg.click("#dApplySel")
        sel = wait(lambda: (lambda b: [f["index"] for f in b["files"] if f["selected"]] if b.get("files") else None)(rpc(cport, "sniffdl.bt.detail", gid)), 8)
        check("应用选择 → aria2 只下另外 2 个", sel and str(idx_b) not in [str(x) for x in sel] and len(sel) == 2, sel)
        if SHOT:
            time.sleep(1)
            pg.screenshot(path=os.path.join(SHOTS, "p2_light_bt_files_1320.png"))
        pg.click('#detail [data-tab="peers"]')
        ok = wait(lambda: "127.0.0.1:%d" % sport in pg.inner_text("#detail .db"), 30)
        check("节点页显示本地做种节点（127.0.0.1:%d，种子）" % sport, ok, pg.inner_text("#detail .db")[:300])
        if ok:
            check("节点标「种子」", "种子" in pg.inner_text("#detail .db"))
            pr = [x["progress"] for x in rpc(cport, "sniffdl.bt.detail", gid)["peers"] if x["port"] == sport or str(x["port"]) == str(sport)]
            check("做种节点对方进度 = 100%（bitfield 按真实分块数算）", pr and pr[0] == 100, pr)
        if SHOT:
            pg.screenshot(path=os.path.join(SHOTS, "p2_light_bt_peers_1320.png"))
        done = wait(lambda: (lambda b: b.get("files") and all(f["done"] == f["length"] for f in b["files"] if f["selected"]))(rpc(cport, "sniffdl.bt.detail", gid)), 60, 1)
        check("选中的 2 个文件下完", done)
        b = rpc(cport, "sniffdl.bt.detail", gid)
        unsel = [f for f in b["files"] if not f["selected"]]
        check("未选文件没下完整", unsel and unsel[0]["done"] < unsel[0]["length"], unsel)
        ctx.close()
        rpc(cport, "sniffdl.task.remove", [gid], False)
    finally:
        seeder.terminate()
        tsrv.shutdown()
        rpc(cport, "sniffdl.settings.set", {"bt_seed": False})


# ======================================================================
def t_error(br, cport, fport, errors):
    print("[失败一键处理]", flush=True)
    STATE["fixed404"] = False
    rpc(cport, "sniffdl.settings.set", {"retry_count": 0, "max_file_not_found": 1})
    gid = rpc(cport, "sniffdl.task.add", {"url": "http://127.0.0.1:%d/404/gone.bin" % fport})["gid"]
    ok = wait(lambda: next(t for t in tasks(cport) if t["gid"] == gid)["status"] == "error", 30)
    check("404 → 任务失败", ok, next(t for t in tasks(cport) if t["gid"] == gid))
    ctx, pg = open_ui(br, cport, errors=errors)
    pg.wait_for_function("g => window.__sdl.S.tasks.get(g)?.status === 'error'", arg=gid, timeout=8000)
    pg.evaluate("g => window.__sdl.openDetail(g)", gid)
    pg.wait_for_selector("#detail .warnbox.err", timeout=8000)
    txt = pg.inner_text("#detail .warnbox.err")
    check("友好提示（中文标题 + 建议）", ("不存在" in txt or "404" in txt) and "刷新" in txt, txt)
    check("有一键处理按钮 + 重试", pg.is_visible("#dFix") and "重试" in txt, txt)
    if SHOT:
        pg.screenshot(path=os.path.join(SHOTS, "p2_light_error_1320.png"))
    STATE["fixed404"] = True
    pg.click("#detail .warnbox.err button:has-text('重试')")
    ok = wait(lambda: next(t for t in tasks(cport) if t["gid"] == gid)["status"] == "complete", 40)
    check("服务器恢复后点「重试」→ 下完", ok, next(t for t in tasks(cport) if t["gid"] == gid))
    check("下完后错误框消失", wait(lambda: not pg.is_visible("#detail .warnbox.err"), 5))
    # 刷新地址按钮：再造一个 404，用 #dFix 换成可用地址
    STATE["fixed404"] = False
    g2 = rpc(cport, "sniffdl.task.add", {"url": "http://127.0.0.1:%d/404/gone2.bin" % fport})["gid"]
    ok = wait(lambda: next(t for t in tasks(cport) if t["gid"] == g2)["status"] == "error", 30)
    check("第二个 404 任务失败", ok, next(t for t in tasks(cport) if t["gid"] == g2))
    pg.keyboard.press("Escape")
    pg.wait_for_function("g => window.__sdl.S.tasks.get(g)?.status === 'error'", arg=g2, timeout=8000)
    pg.evaluate("g => window.__sdl.openDetail(g)", g2)
    pg.wait_for_selector("#dFix", timeout=8000)
    pg.click("#dFix")
    pg.wait_for_selector(".modal input, .modal textarea", timeout=5000)
    pg.fill(".modal input, .modal textarea", "http://127.0.0.1:%d/files/gone2.bin" % fport)
    pg.click(".modal .btn.pri")
    ok = wait(lambda: next(t for t in tasks(cport) if t["gid"] == g2)["status"] == "complete", 40)
    check("「刷新下载地址」换新链接后下完", ok, next(t for t in tasks(cport) if t["gid"] == g2))
    ctx.close()
    rpc(cport, "sniffdl.settings.set", {"retry_count": 3, "max_file_not_found": 3})
    rpc(cport, "sniffdl.task.remove", [gid, g2], True)


# ======================================================================
def t_shots(br, cport, fport):
    """1320×860 与 980×640，浅 / 深色：设置（含搜索）、队列（编辑展开）、待确认。"""
    print("[截图]", flush=True)
    base = "http://127.0.0.1:%d/files/" % fport
    q = rpc(cport, "sniffdl.queue.save", {"name": "夜间下载", "max": 2, "auto": False,
                                          "schedule": {"enabled": True, "start": "23:00", "stop": "07:00", "days": [0, 1, 2, 3, 4]}})["queue"]
    rpc(cport, "sniffdl.settings.set", {"speed_limit_kbps": 100})
    gs = [rpc(cport, "sniffdl.task.add", {"url": base + "night_%d.mkv" % i, "queue": q["id"]})["gid"] for i in range(3)]
    gs.append(rpc(cport, "sniffdl.task.add", {"url": base + "ubuntu-24.04-desktop-amd64.iso"})["gid"])
    for nm in ("lecture_01.mp4", "dataset_v2.zip"):
        rpc(cport, "sniffdl.intercept", {"url": base + nm, "filename": nm, "size": 48 << 20, "source": "menu-link", "confirm": True,
                                         "referer": "https://example.com/course"})
    rpc(cport, "sniffdl.grabber.push", [{"url": base + n, "name": n} for n in ("trailer_1080p.mp4", "ost_track01.flac", "manual.pdf", "cover.jpg")], "ext")
    time.sleep(2)
    for scheme in ("light", "dark"):
        for vw, vh in ((1320, 860), (980, 640)):
            ctx, pg = open_ui(br, cport, scheme, vw, vh)
            sfx = "%s_%d" % (scheme, vw)
            pg.keyboard.press("Alt+7")
            pg.wait_for_selector("#set_sound", state="attached")
            time.sleep(0.3)
            pg.screenshot(path=os.path.join(SHOTS, "p2_%s_settings.png" % sfx))
            pg.click('#vSettings details[data-sec="下载"] > summary')
            pg.evaluate("document.querySelector('#vSettings details[data-sec=\"下载\"]').scrollIntoView()")
            time.sleep(0.3)
            pg.screenshot(path=os.path.join(SHOTS, "p2_%s_settings_adv.png" % sfx))
            pg.fill("#setSearch", "windows")
            time.sleep(0.3)
            pg.screenshot(path=os.path.join(SHOTS, "p2_%s_settings_search.png" % sfx))
            pg.keyboard.press("Alt+6")
            pg.wait_for_selector("#qAdd")
            pg.click('.qbox[data-q="%s"] [data-act="edit"]' % q["id"])
            time.sleep(0.4)
            pg.mouse.move(2, 2)
            pg.screenshot(path=os.path.join(SHOTS, "p2_%s_queues.png" % sfx))
            pg.keyboard.press("Alt+5")
            pg.wait_for_selector("#vPending .prow[data-key]")
            pg.click('#vPending .prow[data-key] .pn')
            time.sleep(0.3)
            pg.mouse.move(2, 2)
            pg.screenshot(path=os.path.join(SHOTS, "p2_%s_pending.png" % sfx))
            ctx.close()
    rpc(cport, "sniffdl.task.remove", gs, True)
    rpc(cport, "sniffdl.settings.set", {"speed_limit_kbps": 0})


# ======================================================================
def _lum(c):
    def ch(v):
        v = v / 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(c[0]) + 0.7152 * ch(c[1]) + 0.0722 * ch(c[2])


def contrast_report(pg, path, sels):
    """截图后按元素取样：文字色（computed color）vs 元素框内像素中位数（≈实际背景），算 WCAG 对比度。"""
    from PIL import Image
    im = Image.open(path).convert("RGB")
    out = {}
    for name, sel in sels:
        info = pg.evaluate("""(sel) => { const e = document.querySelector(sel); if (!e) return null; const r = e.getBoundingClientRect();
            const c = getComputedStyle(e).color.match(/[\\d.]+/g).map(Number); return {x:r.x,y:r.y,w:r.width,h:r.height,c}; }""", sel)
        if not info or info["w"] < 2 or info["h"] < 2:
            continue
        box = (int(info["x"]), int(info["y"]), int(info["x"] + info["w"]), int(info["y"] + info["h"]))
        px = sorted(im.crop(box).getdata(), key=lambda p: p[0] * 0.3 + p[1] * 0.59 + p[2] * 0.11)
        # 背景 = 远离文字色的那一半像素的中位数（文字笔画只占少数）
        tl = _lum(info["c"][:3])
        far = [p for p in px if abs(_lum(p) - tl) > 0.08] or px
        bgc = far[len(far) // 2]
        l1, l2 = sorted((tl, _lum(bgc)), reverse=True)
        out[name] = round((l1 + 0.05) / (l2 + 0.05), 2)
    return out


BG_SELS = [("侧栏导航", ".nav button:not(.on) span:nth-child(2)"), ("当前导航", ".nav button.on span:nth-child(2)"),
           ("任务名", "#tasksHost .task .nm .t"), ("任务元信息", "#tasksHost .task .nm .meta"), ("表头", "#tasksHost .thead span:nth-child(2)"),
           ("状态栏", "#status b"), ("添加框提示", ".hintline span"), ("速度卡", "#spdDown")]


def t_bg(br, cport, fport, core, errors):
    print("[背景图]", flush=True)
    ctx, pg = open_ui(br, cport, errors=errors)
    pg.keyboard.press("Alt+7")
    pg.wait_for_selector("#bgPick .bgtile")
    check("外观卡：无背景 + 4 张内置 + 自定义 共 6 个缩略图", pg.locator("#bgPick .bgtile").count() == 6)
    check("默认无背景：保持原界面", pg.evaluate("!document.documentElement.dataset.bg") and pg.is_hidden('#vSettings .set[data-key="ui_bg_dim"]'))
    imgs_ok = pg.evaluate("[...document.querySelectorAll('#bgPick img')].every(i => i.complete && i.naturalWidth > 0)")
    check("缩略图都加载出来", imgs_ok)
    pg.click('#bgPick .bgtile[data-bg="bg2"]')
    ok = wait(lambda: pg.evaluate("document.documentElement.dataset.bg === 'on' && getComputedStyle(document.querySelector('#bgLayer .bg-img')).backgroundImage.includes('bg2.webp')"), 3)
    check("点缩略图即时生效（bg2）", ok)
    check("选中写入内核 ui_bg=bg2", wait(lambda: rpc(cport, "sniffdl.settings.get")["values"]["ui_bg"] == "bg2", 3))
    check("有背景时显示 填充/对齐/透明度/模糊/遮罩", all(pg.is_visible('#vSettings .set[data-key="%s"]' % k) for k in ("ui_bg_fit", "ui_bg_pos", "ui_bg_opacity", "ui_bg_blur", "ui_bg_dim")))
    check("面板变玻璃（backdrop-filter）", "blur" in pg.evaluate("getComputedStyle(document.querySelector('.side')).backdropFilter"))
    # 滑块：拖动预览、松手保存
    pg.evaluate("""() => { const e = document.getElementById('set_ui_bg_dim'); e.value = 80; e.dispatchEvent(new Event('input', {bubbles:true})); }""")
    check("遮罩滑块实时预览", wait(lambda: pg.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--dim').trim()") == "0.8", 2))
    pg.evaluate("""() => { const e = document.getElementById('set_ui_bg_dim'); e.dispatchEvent(new Event('change', {bubbles:true})); }""")
    check("遮罩强度松手保存", wait(lambda: rpc(cport, "sniffdl.settings.get")["values"]["ui_bg_dim"] == 80, 3))
    pg.evaluate("""() => { const e = document.getElementById('set_ui_bg_blur'); e.value = 6; e.dispatchEvent(new Event('input', {bubbles:true})); e.dispatchEvent(new Event('change', {bubbles:true})); }""")
    check("模糊生效", wait(lambda: "blur(6px)" in pg.evaluate("document.querySelector('#bgLayer .bg-img').style.filter"), 3))
    pg.select_option("#set_ui_bg_pos", "左")
    check("对齐位置生效", wait(lambda: pg.evaluate("document.querySelector('#bgLayer .bg-img').style.backgroundPosition").startswith("0%"), 3))
    rpc(cport, "sniffdl.settings.set", {"ui_bg_blur": 0, "ui_bg_pos": "自动", "ui_bg_dim": 60})
    check("外部改动回显（SSE）", wait(lambda: pg.evaluate("document.getElementById('set_ui_bg_dim').value") == "60", 3))
    # 自定义：浏览器模式降级为输入路径
    pg.click("#bgCustomTile")
    check("浏览器模式点「自定义」展开路径输入", wait(lambda: pg.is_visible("#set_ui_bg_custom"), 2))
    good = "http://127.0.0.1:%d/ui/assets/bg/orig_bg3.jpg" % cport
    pg.fill("#set_ui_bg_custom", good)
    pg.press("#set_ui_bg_custom", "Enter")
    ok = wait(lambda: pg.evaluate("document.documentElement.dataset.bg === 'on' && document.documentElement.dataset.bgKind === 'photo'"), 4)
    v = rpc(cport, "sniffdl.settings.get")["values"]
    check("自定义图片（URL）生效并保存", ok and v["ui_bg"] == "custom" and v["ui_bg_custom"] == good, v.get("ui_bg_custom"))
    check("自定义缩略图显示该图", wait(lambda: pg.evaluate("!!document.querySelector('#bgCustomTile img.photo')"), 3))
    pg.fill("#set_ui_bg_custom", "/nope/不存在.jpg")
    pg.press("#set_ui_bg_custom", "Enter")
    ok = wait(lambda: pg.evaluate("!document.documentElement.dataset.bg") and pg.inner_text("#bgMsg").strip(), 5)
    check("读不到的图片：退回无背景 + 中文提示", ok, pg.inner_text("#bgMsg"))
    errors[:] = [e for e in errors if "不存在" not in e and "nope" not in e and "ERR_" not in e and "Not allowed to load local resource" not in e]
    pg.click('#bgPick .bgtile[data-bg="none"]')
    ok = wait(lambda: pg.evaluate("!document.documentElement.dataset.bg && getComputedStyle(document.querySelector('.side')).backdropFilter === 'none'"), 3)
    check("切回无背景：玻璃效果撤掉", ok and pg.is_hidden('#vSettings .set[data-key="ui_bg_dim"]'))
    pg.click('#bgPick .bgtile[data-bg="bg4"]')
    check("点 bg4 写入内核", wait(lambda: rpc(cport, "sniffdl.settings.get")["values"]["ui_bg"] == "bg4", 3), rpc(cport, "sniffdl.settings.get")["values"]["ui_bg"])
    ctx.close()
    stop_core(cport, core)
    core = start_core(cport)
    vv = rpc(cport, "sniffdl.settings.get")["values"]
    check("重启内核后背景设置仍在", vv["ui_bg"] == "bg4", {k: vv[k] for k in vv if k.startswith("ui_bg")})
    ctx, pg = open_ui(br, cport, errors=errors)
    ok = wait(lambda: pg.evaluate("document.documentElement.dataset.bg === 'on' && getComputedStyle(document.querySelector('#bgLayer .bg-img')).backgroundImage.includes('bg4.webp')"), 4)
    check("重启后打开界面直接是 bg4", ok)
    ctx.close()
    rpc(cport, "sniffdl.settings.set", {"ui_bg": "none", "ui_bg_custom": ""})
    if SHOT and "bgshots" not in ONLY:
        t_bg_shots(br, cport, fport)
    return core


def t_bg_shots(br, cport, fport):
    print("[背景截图]", flush=True)
    base = "http://127.0.0.1:%d/files/" % fport
    rpc(cport, "sniffdl.settings.set", {"speed_limit_kbps": 160})
    names = ["【4K】海底两万里.Twenty.Thousand.Leagues.2160p.mkv", "ubuntu-24.04-desktop-amd64.iso", "lecture_03_信号与系统.mp4",
             "dataset_v2_final.zip", "ost_disc1.flac", "manual_zh-CN.pdf"]
    gs = [rpc(cport, "sniffdl.task.add", {"url": base + n})["gid"] for n in names]
    STATE["fixed404"] = False
    ge = rpc(cport, "sniffdl.task.add", {"url": "http://127.0.0.1:%d/404/missing_part2.rar" % fport})["gid"]
    gs.append(ge)
    time.sleep(3)
    try:
        rpc(cport, "sniffdl.task.pause", [gs[4]])
    except Exception:
        pass
    time.sleep(1.5)
    worst = []
    plan = [(b, sc, 1320, 860) for b in ("bg1", "bg2", "bg3", "bg4") for sc in ("light", "dark")]
    plan += [("bg1", "dark", 980, 640), ("bg2", "light", 980, 640), ("bg3", "dark", 980, 640), ("bg4", "light", 980, 640)]
    for bg, sc, vw, vh in plan:
        rpc(cport, "sniffdl.settings.set", {"ui_bg": bg})
        ctx, pg = open_ui(br, cport, sc, vw, vh)
        time.sleep(0.8)
        pg.mouse.move(vw - 5, 5)
        path = os.path.join(SHOTS, "bg_%s_%s_%d.png" % (bg, sc, vw))
        pg.screenshot(path=path)
        cr = contrast_report(pg, path, BG_SELS)
        mn = min(cr.values()) if cr else 0
        worst.append((os.path.basename(path), mn, cr))
        print("   %s 对比度最低 %.2f %s" % (os.path.basename(path), mn, cr), flush=True)
        if vw == 1320 and bg in ("bg2", "bg4"):
            # 详情抽屉 / 弹窗 / 设置页
            pg.click("#tasksHost .task .nm .t")
            pg.keyboard.press("Enter")
            time.sleep(0.6)
            if pg.locator(".drawer").count():
                pg.screenshot(path=os.path.join(SHOTS, "bg_%s_%s_drawer.png" % (bg, sc)))
                pg.keyboard.press("Escape")
                time.sleep(0.3)
            pg.click("#tasksHost .task .nm .t")
            pg.keyboard.press("Delete")
            time.sleep(0.5)
            if pg.locator(".modal").count():
                pg.screenshot(path=os.path.join(SHOTS, "bg_%s_%s_modal.png" % (bg, sc)))
                pg.keyboard.press("Escape")
                time.sleep(0.3)
            pg.keyboard.press("Alt+7")
            pg.wait_for_selector("#bgPick")
            pg.evaluate("document.getElementById('lookCard').scrollIntoView({block: 'start'})")
            time.sleep(0.4)
            pg.mouse.move(vw - 5, 5)
            p2 = os.path.join(SHOTS, "bg_%s_%s_settings.png" % (bg, sc))
            pg.screenshot(path=p2)
            cr2 = contrast_report(pg, p2, [("设置标题", "#vSettings .vhead h1"), ("外观卡标题", "#lookCard .ch h3"),
                                           ("设置项名", "#lookCard .set .l b"), ("设置说明", "#lookCard .set[data-key=ui_bg] .l small"),
                                           ("滑块数值", "#lookCard .set[data-key=ui_bg_dim] .rv")])
            worst.append((os.path.basename(p2), min(cr2.values()) if cr2 else 0, cr2))
            print("   %s 对比度最低 %.2f %s" % (os.path.basename(p2), min(cr2.values()) if cr2 else 0, cr2), flush=True)
            pg.keyboard.press("Alt+5")
            time.sleep(0.4)
            pg.screenshot(path=os.path.join(SHOTS, "bg_%s_%s_pending.png" % (bg, sc)))
        ctx.close()
    # 自定义照片
    rpc(cport, "sniffdl.settings.set", {"ui_bg": "custom", "ui_bg_custom": "http://127.0.0.1:%d/ui/assets/bg/orig_bg2.jpg" % cport})
    for sc in ("light", "dark"):
        ctx, pg = open_ui(br, cport, sc, 1320, 860)
        time.sleep(0.8)
        pg.mouse.move(1315, 5)
        path = os.path.join(SHOTS, "bg_custom_%s_1320.png" % sc)
        pg.screenshot(path=path)
        cr = contrast_report(pg, path, BG_SELS)
        worst.append((os.path.basename(path), min(cr.values()) if cr else 0, cr))
        print("   %s 对比度最低 %.2f %s" % (os.path.basename(path), min(cr.values()) if cr else 0, cr), flush=True)
        ctx.close()
    main_txt = [w for w in worst]
    low = [(n, m) for n, m, _ in main_txt if m < 4.5]
    check("背景截图主要文字对比度 ≥ 4.5（%d 张）" % len(main_txt), not low, low)
    rpc(cport, "sniffdl.task.remove", gs, True)
    rpc(cport, "sniffdl.settings.set", {"speed_limit_kbps": 0, "ui_bg": "none", "ui_bg_custom": ""})


SHOT = False
ONLY = set()


def t_logs(br, cport, fport, errors):
    print("[日志页 + 统计页]", flush=True)
    rpc(cport, "sniffdl.task.add", {"url": "http://127.0.0.1:%d/404/logprobe.bin" % fport})
    time.sleep(2)
    # 造统计数据：写进真账本再由接口读出
    today = datetime.date.today()
    st = {}
    for i, b in ((0, 5 << 20), (1, 1 << 20), (3, 3 << 20), (40, 9 << 20)):
        st[(today - datetime.timedelta(days=i)).isoformat()] = {"bytes": b, "n": 2}
    with open(os.path.join(DATA, "stats.json"), "w", encoding="utf-8") as f:
        json.dump(st, f)
    for scheme in ("light", "dark"):
        ctx, pg = open_ui(br, cport, scheme=scheme, errors=errors)
        pg.click("[data-nav=logs]")
        pg.wait_for_selector("#logBox", timeout=8000)
        ok = wait(lambda: len(pg.locator("#logBox").inner_text()) > 20, 8)
        check("日志页(%s)：显示真实日志" % scheme, ok, pg.locator("#logBox").inner_text()[:200])
        chip = pg.inner_text("#logCount")
        check("日志页(%s)：条数徽标" % scheme, "共" in chip and "显示" in chip, chip)
        if scheme == "light":
            pg.fill("#logQ", "logprobe")
            check("关键字筛选只留匹配行", wait(lambda: pg.locator("#logBox .ll").count() >= 1 and all("logprobe" in t for t in pg.locator("#logBox .ll").all_inner_texts()), 5), pg.inner_text("#logBox")[:200])
            pg.fill("#logQ", "zzz_no_such_zzz")
            check("无匹配显示提示", "没有匹配" in pg.inner_text("#logBox"))
            pg.fill("#logQ", "")
            pg.select_option("#logLv", "err")
            ts = pg.locator("#logBox .ll").all_inner_texts()
            check("仅错误筛选只剩错误行", all(("失败" in t or "错误" in t or "异常" in t or "rror" in t or "ail" in t) for t in ts), ts[:3])
            pg.select_option("#logLv", "")
            n0 = pg.locator("#logBox .ll").count()
            rpc(cport, "sniffdl.task.add", {"url": "http://127.0.0.1:%d/404/logprobe2.bin" % fport})
            check("新日志实时追加", wait(lambda: pg.locator("#logBox .ll").count() > n0 or "logprobe2" in pg.inner_text("#logBox"), 8))
            check("复制/打开日志文件按钮可见", pg.is_visible("#logOpen") and pg.is_visible("#logCopy"))
            r = rpc(cport, "sniffdl.os.openLog")
            check("openLog 接口返回路径", bool(r.get("path")), r)
        if SHOT:
            pg.screenshot(path=os.path.join(SHOTS, "log_%s_1320.png" % scheme))
        pg.click("[data-nav=stats]")
        pg.wait_for_selector("#heatmap i[data-date]", timeout=8000)
        check("统计页(%s)：热力图 112 格" % scheme, pg.locator("#heatmap i[data-date]").count() == 112, pg.locator("#heatmap i[data-date]").count())
        kp = pg.locator(".kpi").all_inner_texts()
        check("统计页(%s)：4 张 KPI 卡" % scheme, len(kp) == 4, kp)
        check("统计页(%s)：今日 5.0 MB / 累计 18.0 MB·8 个" % scheme, "5.0 MB" in kp[0] and "18.0 MB" in kp[1] and "8 个" in kp[1], kp)
        lv = pg.evaluate("() => [...document.querySelectorAll('#heatmap i.l4')].map(i => i.dataset.date)")
        check("统计页(%s)：最大那天（40 天前）为最深色" % scheme, lv == [(today - datetime.timedelta(days=40)).isoformat()], lv)
        if SHOT:
            pg.screenshot(path=os.path.join(SHOTS, "stats_%s_1320.png" % scheme))
        ctx.close()


def main():
    global SHOT
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="settings,queues,pending,bt,error,bg")
    ap.add_argument("--shots", action="store_true")
    a = ap.parse_args()
    SHOT = a.shots
    only = set(a.only.split(","))
    ONLY.update(only)
    shutil.rmtree(DATA, ignore_errors=True)
    os.makedirs(os.path.join(DATA, "out"), exist_ok=True)
    os.makedirs(SHOTS, exist_ok=True)
    fport = free_port()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", fport), Files)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cport = free_port()
    core = start_core(cport)
    rpc(cport, "sniffdl.settings.set", {"outdir": os.path.join(DATA, "out"), "clip_watch": False, "notify": False, "ui_theme": "跟随系统",
                                        "bt_tracker_auto": False})
    errors = []
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as p:
            br = p.chromium.launch(args=["--no-sandbox"])
            for name, fn in (("settings", lambda: t_settings(br, cport, core, errors)), ("queues", lambda: t_queues(br, cport, fport, errors)),
                             ("pending", lambda: t_pending(br, cport, fport, errors)), ("bt", lambda: t_bt(br, cport, errors)),
                             ("error", lambda: t_error(br, cport, fport, errors)), ("bg", lambda: t_bg(br, cport, fport, core, errors)),
                             ("logs", lambda: t_logs(br, cport, fport, errors)), ("bgshots", lambda: t_bg_shots(br, cport, fport)), ("shots", lambda: t_shots(br, cport, fport))):
                if name not in only and not (name == "shots" and SHOT and "noshotsect" not in only):
                    continue
                try:
                    r = fn()
                    if name in ("settings", "bg") and r is not None:
                        core = r
                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    check("%s 段异常" % name, False, repr(e)[:300])
            br.close()
        errs = [e for e in errors if "favicon" not in e and "Not allowed to load local resource" not in e and "nope" not in e]
        check("页面无 JS 报错", not errs, "\n  ".join(errs[:10]))
    finally:
        stop_core(cport, core)
        srv.shutdown()
    n_ok = sum(1 for r in RESULTS if r[1])
    print("\n%d/%d 通过" % (n_ok, len(RESULTS)))
    for r in RESULTS:
        if not r[1]:
            print(" FAIL", r[0], "←", str(r[2])[:300])
    return 0 if n_ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
