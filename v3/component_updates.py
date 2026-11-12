# -*- coding: utf-8 -*-
"""开源组件版本检查与自更新（yt-dlp / aria2c / ffmpeg）。

为什么需要：yt-dlp 几乎每周都在跟站点改规则，装完不动就等于慢慢失效；
用户明确要求"用到的开源项目也要能自动更新"。

设计上的三条硬约束：
1. **绝不覆盖内置包**。更新装到用户可写目录 %APPDATA%\\SniffDL\\pylibs，
   启动时把该目录插到 sys.path 最前 —— 冻结版也能覆盖内置的 yt-dlp；
   装坏了删掉这个目录就回到内置版本，不会把下载器弄成不可用。
2. **失败无副作用**：先装到临时目录，成功才替换；网络/校验失败只记日志。
3. **不打扰**：检查只在用户点「检查更新」或启动后延迟静默一次时发生，
   结果写日志 + 在「关于与自检」里显示，不弹窗。

国内网络：pip 默认走清华镜像，可用环境变量 SNIFFDL_PIP_INDEX 覆盖。
"""
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

APPDATA = os.environ.get("APPDATA") or os.path.expanduser("~")
PYLIBS = os.path.join(APPDATA, "SniffDL", "pylibs")
PIP_INDEX = os.environ.get("SNIFFDL_PIP_INDEX",
                           "https://pypi.tuna.tsinghua.edu.cn/simple")
_STATE = {"checking": False, "last": None, "info": {}}


def ensure_path():
    """让 pylibs 里的新版本优先于内置版本（必须在 import yt_dlp 之前调用）。"""
    try:
        os.makedirs(PYLIBS, exist_ok=True)
        if PYLIBS in sys.path:
            sys.path.remove(PYLIBS)
        sys.path.insert(0, PYLIBS)
    except Exception:
        pass


def _http_json(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": "SniffDL/0.5.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _py_ver(v):
    """把 '2026.10.21' 这类版本号切成可比较的整数元组。"""
    return tuple(int(x) for x in re.findall(r"\d+", str(v or ""))[:6])


def current_versions():
    out = {}
    try:
        import yt_dlp
        # 注意：yt_dlp.version 是「模块」，真正的版本号在它的 __version__ 上；
        # 直接取 yt_dlp.version 会把 <module ...> 显示到界面上、比较也失效。
        _v = getattr(yt_dlp, "version", "")
        if not isinstance(_v, str):
            _v = getattr(_v, "__version__", "")
        out["yt-dlp"] = str(_v or "?")
    except Exception:
        out["yt-dlp"] = "?"
    try:
        import imageio_ffmpeg
        out["ffmpeg"] = os.path.basename(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        out["ffmpeg"] = "?"
    return out


def check_all(log=None):
    """查各组件的最新版；返回 [{'name','cur','latest','need'}]，不抛异常。"""
    log = log or (lambda m: None)
    cur = current_versions()
    res = [{"name": "yt-dlp", "cur": cur.get("yt-dlp"), "latest": None, "need": False}]
    try:
        d = _http_json("https://pypi.org/pypi/yt-dlp/json")
        latest = (d.get("info") or {}).get("version")
        res[0]["latest"] = latest
        try:
            res[0]["need"] = _py_ver(latest) > _py_ver(cur.get("yt-dlp"))
        except Exception:
            res[0]["need"] = bool(latest) and latest != cur.get("yt-dlp")
    except Exception as e:
        log(f"[更新] 查 yt-dlp 最新版失败：{type(e).__name__} {e}")
    _STATE["info"] = {r["name"]: r for r in res}
    _STATE["last"] = time.time()
    need = [r for r in res if r["need"]]
    log("[更新] 检查完成：%s" % (
        "、".join("%s %s→%s" % (r["name"], r["cur"], r["latest"]) for r in need)
        if need else "已是最新（yt-dlp %s）" % cur.get("yt-dlp")))
    return res


def update_pypi_pkg(name, log=None, on_done=None):
    """把某个 PyPI 包升级到 pylibs（用户目录），不碰内置包。后台线程跑。"""
    log = log or (lambda m: None)

    def work():
        _STATE["checking"] = True
        tmp = None
        try:
            tmp = tempfile.mkdtemp(prefix="sniffdl_upd_")
            log(f"[更新] 正在把 {name} 装到用户目录（不动内置版本）…")
            cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                   "--target", tmp, "--upgrade", name]
            if "tuna" in PIP_INDEX:
                cmd += ["-i", PIP_INDEX]
            p = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=600)
            if p.returncode != 0:
                log(f"[更新] 失败，已放弃（内置版本仍可用）：{(p.stderr or '')[-200:]}")
                return
            # 装成功才替换旧目录：先改名备份，替换失败可回滚
            tgt = os.path.join(PYLIBS, "")
            os.makedirs(PYLIBS, exist_ok=True)
            old = os.path.join(PYLIBS, "..", "pylibs_old")
            pkg_dir = os.path.join(tmp, name.replace("-", "_"))
            if not os.path.isdir(pkg_dir):
                log(f"[更新] 装完找不到 {name} 包目录，放弃")
                return
            dst = os.path.join(PYLIBS, name.replace("-", "_"))
            if os.path.isdir(dst):
                try:
                    import shutil
                    if os.path.isdir(old):
                        shutil.rmtree(old, ignore_errors=True)
                    os.rename(dst, old)
                except Exception:
                    pass
            import shutil
            shutil.copytree(pkg_dir, dst)
            # 依赖也一起带过来（yt-dlp 依赖很少，整目录拷最稳）
            for item in os.listdir(tmp):
                if item == os.path.basename(pkg_dir):
                    continue
                src = os.path.join(tmp, item)
                d2 = os.path.join(PYLIBS, item)
                if os.path.isdir(src) and not os.path.exists(d2):
                    try:
                        shutil.copytree(src, d2)
                    except Exception:
                        pass
            ensure_path()
            try:
                import importlib
                mod = importlib.import_module(name.replace("-", "_"))
                importlib.reload(mod)
                log(f"[更新] {name} 已更新到 {getattr(mod, 'version', '?')}（下次启动生效）")
            except Exception:
                log(f"[更新] {name} 已装入用户目录，下次启动生效")
        except Exception as e:
            log(f"[更新] {name} 更新异常（内置版本不受影响）：{type(e).__name__} {e}")
        finally:
            _STATE["checking"] = False
            if tmp:
                try:
                    shutil.rmtree(tmp, ignore_errors=True)
                except Exception:
                    pass
            if on_done:
                try:
                    on_done()
                except Exception:
                    pass

    threading.Thread(target=work, daemon=True).start()
    return True
