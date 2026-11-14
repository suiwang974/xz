# -*- coding: utf-8 -*-
"""打包自检（`SniffDL.exe --selftest` / `python sniffdl_core.py --selftest` 共用）。

只验「包打得全不全、产物能不能干活」，全离线、几秒内完成：
  1) 打包清单里的每个模块都能 import（hls / sidecar / sdl_speed / netdisk_rules / rpc_server …）
  2) aria2c 能找到（冻结产物要求随包那份在 _MEIPASS / 程序目录）
  3) 数据目录可写，含「中文 + 空格」子目录与中文文件名的写读删
  4) RPC：起来能 ping 通；shutdown 后端口真关、runtime.json 真删
  5) 托盘资源（SniffDL_tray.png / SniffDL.ico）在包里
  6) 浏览器扩展目录（manifest.json）、内核界面 ui/ 在包里

清单 REQUIRED_MODULES 是唯一来源：tests/pack_selftest_test.py 断言它 ⊆ SniffDL.spec 的 hiddenimports，
且覆盖 core/*.py、sdl_speed/*.py 全部模块 —— 新加模块忘了进 spec，沙箱单测就红。
"""
import importlib
import json
import os
import socket
import sys
import time

# 冻结产物里 core/ 在 PYZ 里，没法 listdir —— 所以这里是静态清单，靠单测和磁盘上的 core/*.py 对齐
CORE_MODULES = [
    "core", "core.api", "core.batch", "core.bencode", "core.checksum", "core.clipboard", "core.engine",
    "core.errors", "core.events", "core.headless", "core.hls", "core.sidecar", "core.linkdecode",
    "core.livehls", "core.naming", "core.platform_util", "core.postactions", "core.queues",
    "core.ratelimit", "core.recognize", "core.runtime", "core.settings", "core.speed",
]
SPEED_MODULES = ["sdl_speed", "sdl_speed.engine", "sdl_speed.backends", "sdl_speed.errors", "sdl_speed.profiles"]
APP_MODULES = ["rpc_server", "native_host", "netdisk_rules", "component_updates", "pet_bridge",
               "sniffdl_core", "video_downloader", "pack_selftest"]
THIRD_PARTY = ["yt_dlp", "requests", "PIL.Image", "PIL.ImageTk"]
REQUIRED_MODULES = CORE_MODULES + SPEED_MODULES + APP_MODULES + THIRD_PARTY
# 托盘后端按平台运行时 import：只在 Windows 上强制
WIN_ONLY_MODULES = ["pystray", "pystray._win32"]

# (相对路径, 说明)：在资源目录（_MEIPASS / 程序目录 / 源码目录）任一处找到即可
REQUIRED_DATA = [
    ("assets/SniffDL_tray.png", "托盘图标"),
    ("assets/SniffDL.ico", "程序图标"),
    ("extension/manifest.json", "浏览器扩展"),
    ("ui/index.html", "内核界面 ui/"),
]

CN_DIRNAME = "自检 中文目录"
CN_FILENAME = "写入测试_✓.txt"


def is_frozen():
    return bool(getattr(sys, "frozen", False))


def resource_dirs():
    """资源查找顺序：PyInstaller 解包目录（6.x onedir 为 _internal）→ exe 所在目录 → 源码目录。"""
    out = []
    mp = getattr(sys, "_MEIPASS", None)
    if mp:
        out.append(mp)
    if is_frozen():
        out.append(os.path.dirname(os.path.abspath(sys.executable)))
    out.append(os.path.dirname(os.path.abspath(__file__)))
    seen, res = set(), []
    for d in out:
        k = os.path.normcase(os.path.abspath(d))
        if k not in seen:
            seen.add(k)
            res.append(d)
    return res


def find_resource(rel):
    for d in resource_dirs():
        fp = os.path.join(d, *rel.split("/"))
        if os.path.exists(fp):
            return fp
    return None


def _item(name, ok, detail="", fatal=True):
    return {"name": name, "ok": bool(ok), "detail": str(detail)[:300], "fatal": bool(fatal)}


def check_modules(mods=None, win=None):
    """每个模块 import 一次；失败的列名字和异常。返回单条汇总 + 每个失败一条。"""
    mods = list(REQUIRED_MODULES if mods is None else mods)
    win = (sys.platform == "win32") if win is None else win
    if win:
        mods += [m for m in WIN_ONLY_MODULES if m not in mods]
    bad = []
    for m in mods:
        try:
            importlib.import_module(m)
        except Exception as ex:  # noqa: BLE001 — 任何导入异常都算打包缺件
            bad.append((m, "%s: %s" % (type(ex).__name__, str(ex)[:120])))
    items = [_item("模块可导入 %d/%d" % (len(mods) - len(bad), len(mods)), not bad,
                   "全部可导入" if not bad else "缺：" + ", ".join(m for m, _ in bad))]
    for m, why in bad:
        items.append(_item("模块 " + m, False, why))
    return items


def check_cn_dir(base):
    """数据目录下建「中文 + 空格」子目录，写中文文件名、读回比对、删掉。"""
    sub = os.path.join(base, CN_DIRNAME)
    fp = os.path.join(sub, CN_FILENAME)
    payload = "SniffDL 自检 ✓ 中文内容\n".encode("utf-8")
    try:
        os.makedirs(sub, exist_ok=True)
        with open(fp, "wb") as f:
            f.write(payload)
        with open(fp, "rb") as f:
            back = f.read()
        os.remove(fp)
        os.rmdir(sub)
        ok = back == payload and not os.path.exists(sub)
        return _item("中文路径可写", ok, sub if ok else "读回不一致：%r" % back[:40])
    except Exception as ex:
        return _item("中文路径可写", False, "%s：%s" % (sub, str(ex)[:120]))


def check_aria2c(found_path):
    """found_path 来自 V.resolve_aria2c。冻结产物额外要求随包那份存在（用户机器上不能指望 PATH）。"""
    if not found_path or not os.path.exists(found_path):
        return _item("aria2c 可找到", False, "没找到（源码运行放 PATH，exe 由 spec 打进包根）")
    if is_frozen():
        bundled = find_resource("aria2c.exe" if sys.platform == "win32" else "aria2c")
        return _item("aria2c 随包", bool(bundled), bundled or "包里没有 aria2c，%s 是外部的" % found_path)
    return _item("aria2c 可找到", True, found_path)


def check_resources(required=None):
    items = []
    for rel, why in (REQUIRED_DATA if required is None else required):
        fp = find_resource(rel)
        items.append(_item("%s 在包内" % why, bool(fp), fp or "缺 %s（查 spec datas）" % rel))
    return items


def port_open(port, timeout=1.0):
    try:
        c = socket.create_connection(("127.0.0.1", int(port)), timeout=timeout)
        c.close()
        return True
    except Exception:
        return False


def rpc_ping(port, timeout=3):
    import urllib.request
    req = urllib.request.Request("http://127.0.0.1:%d/jsonrpc" % int(port),
                                 data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "sniffdl.ping",
                                                  "params": []}).encode(),
                                 headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        r = json.loads(opener.open(req, timeout=timeout).read().decode("utf-8", "ignore"))
        return (r.get("result") or {}).get("service") == "SniffDL"
    except Exception:
        return False


def check_rpc_up(port):
    return _item("RPC 启动并 ping 通", rpc_ping(port), "127.0.0.1:%s" % port)


def check_rpc_down(port, runtime_path, wait=5.0):
    """shutdown 之后：端口必须关、runtime.json 必须删。"""
    end = time.time() + wait
    while time.time() < end and port_open(port, 0.3):
        time.sleep(0.2)
    closed = not port_open(port, 0.5)
    rt_gone = not os.path.exists(runtime_path)
    return _item("RPC 停止（端口关闭 + runtime.json 已删）", closed and rt_gone,
                 "端口%s，runtime.json %s" % ("已关" if closed else "仍在监听", "已删" if rt_gone else "仍在"))


# 来自 core.api 的 sniffdl.selftest 条目：这些是「用户环境建议项」，打包自检里只警告不判失败
ADVISORY_PREFIXES = ("ffmpeg", "aria2 兼容端口", "保存目录可写")


def merge_api_items(api_items, strict=False):
    out = []
    for i in api_items or []:
        fatal = strict or not str(i.get("name", "")).startswith(ADVISORY_PREFIXES)
        out.append(_item(i.get("name", "?"), i.get("ok"), i.get("detail", ""), fatal))
    return out


def summarize(items):
    fatal = [i for i in items if i["fatal"]]
    x = sum(1 for i in fatal if i["ok"])
    return {"ok": x == len(fatal), "passed": x, "total": len(fatal),
            "warn": sum(1 for i in items if not i["fatal"] and not i["ok"])}


def format_lines(items, summary, extra=None):
    lines = []
    for i in items:
        tag = "OK   " if i["ok"] else ("FAIL " if i["fatal"] else "WARN ")
        lines.append(tag + i["name"] + "  " + i.get("detail", ""))
    for k, v in (extra or {}).items():
        lines.append("INFO %s  %s" % (k, v))
    lines.append("SELFTEST %s %d/%d" % ("PASS" if summary["ok"] else "FAIL", summary["passed"], summary["total"]))
    lines.append("SELFTEST_JSON " + json.dumps(dict(summary, items=items, **(extra or {})), ensure_ascii=False))
    return lines


def emit(lines, out_path=None):
    """stdout 可能是 None（console=False 的窗口版 exe 不带控制台）——同时写文件，CI 读文件。"""
    text = "\n".join(lines) + "\n"
    if out_path:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception:
            pass
    try:
        if sys.stdout is not None:
            try:
                sys.stdout.write(text)
            except UnicodeEncodeError:
                sys.stdout.buffer.write(text.encode("utf-8"))
            sys.stdout.flush()
    except Exception:
        pass
    return text
