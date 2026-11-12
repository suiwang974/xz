# -*- coding: utf-8 -*-
"""外挂引擎（设置页「外挂引擎」分组）：找程序、拼命令行、跑起来、取消时杀进程、认出成品文件。

  · N_m3u8DL-RE：设置 sidecar_m3u8 开且找得到程序 → m3u8/mpd 先交给它；失败/没成品 → 调用方回落内置分段下载
  · BBDown：设置 sidecar_bili 开且找得到程序 → B 站视频链接交给它；失败 → 调用方回落 yt-dlp
找程序顺序：设置 sidecar_paths（每行「名称=路径」）→ 项目 vendor/ → PATH。找不到返回 None（开关再开也不会报错）。
这里只启动用户自己装的外部程序，不内置、不下载二进制。
"""
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

NAMES = {
    "n_m3u8dl": ("N_m3u8DL-RE", "N_m3u8DL-RE.exe"),
    "bbdown": ("BBDown", "BBDown.exe"),
}


def parse_paths(text):
    """多行「名称=路径」→ {小写名称: 路径}；空行、# 开头的注释行忽略。"""
    out = {}
    for ln in str(text or "").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        out[k.strip().lower()] = v.strip().strip('"')
    return out


def find(key, paths_text=""):
    """key: n_m3u8dl / bbdown。返回可执行文件绝对路径或 None。"""
    names = NAMES.get(key, ())
    custom = parse_paths(paths_text)
    for n in (key,) + tuple(x.lower() for x in names):
        p = custom.get(n.lower())
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    for n in names:
        p = os.path.join(HERE, "vendor", n)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


def build_m3u8_cmd(exe, url, save_dir, save_name, headers=None, threads=16, tmp_dir=None):
    cmd = [exe, url, "--save-dir", save_dir, "--save-name", save_name, "--thread-count", str(int(threads)),
           "--auto-select", "--no-log", "--check-segments-count", "false", "-M", "format=mp4"]
    if tmp_dir:
        cmd += ["--tmp-dir", tmp_dir]
    for k, v in (headers or {}).items():
        if str(k).lower() in ("host", "content-length"):
            continue
        cmd += ["-H", "%s: %s" % (k, v)]
    return cmd


def build_bili_cmd(exe, url, work_dir):
    return [exe, url, "--work-dir", work_dir]


def is_bili(url):
    return bool(re.match(r"https?://([\w-]+\.)*(bilibili\.com|b23\.tv)/", str(url or ""), re.I))


def _new_files(d, since, stem=None):
    out = []
    try:
        for n in os.listdir(d):
            p = os.path.join(d, n)
            if os.path.isfile(p) and os.path.getmtime(p) >= since - 1 and not n.endswith((".tmp", ".part", ".aria2")):
                if stem is None or n.startswith(stem):
                    out.append(p)
    except OSError:
        pass
    return sorted(out, key=lambda p: os.path.getmtime(p), reverse=True)


def run(cmd, cancel=None, log=print, cwd=None, timeout=6 * 3600, on_line=None):
    """跑外挂命令，逐行读输出；取消 → terminate → kill，不留孤儿。返回 (退出码, 末尾输出)。取消抛 InterruptedError。"""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=cwd,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0)
    tail = []
    import threading
    def pump():
        for raw in iter(p.stdout.readline, b""):
            ln = raw.decode("utf-8", "replace").rstrip()
            if ln:
                tail.append(ln)
                del tail[:-30]
                if on_line:
                    try:
                        on_line(ln)
                    except Exception:
                        pass
    th = threading.Thread(target=pump, daemon=True)
    th.start()
    t0 = time.time()
    try:
        while p.poll() is None:
            if cancel and cancel():
                p.terminate()
                try:
                    p.wait(3)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
                raise InterruptedError("cancel")
            if time.time() - t0 > timeout:
                p.kill()
                p.wait()
                return -9, "\n".join(tail)
            time.sleep(0.1)
    finally:
        if p.poll() is None:
            p.kill()
    th.join(2)
    return p.returncode, "\n".join(tail)


def run_m3u8(exe, url, save_dir, stem, headers=None, threads=16, cancel=None, log=print, on_line=None):
    """成功返回成品路径；失败抛 RuntimeError（调用方回落）。"""
    t0 = time.time()
    rc, tail = run(build_m3u8_cmd(exe, url, save_dir, stem, headers, threads), cancel=cancel, log=log, on_line=on_line)
    if rc != 0:
        raise RuntimeError("N_m3u8DL-RE 退出码 %s：%s" % (rc, tail[-120:]))
    media = [p for p in _new_files(save_dir, t0, stem) if p.lower().endswith((".mp4", ".mkv", ".ts", ".m4a", ".aac", ".mp3"))]
    if not media:
        raise RuntimeError("N_m3u8DL-RE 没产出成品文件")
    return media[0]


def run_bili(exe, url, save_dir, cancel=None, log=print, on_line=None):
    t0 = time.time()
    rc, tail = run(build_bili_cmd(exe, url, save_dir), cancel=cancel, log=log, on_line=on_line, cwd=save_dir)
    if rc != 0:
        raise RuntimeError("BBDown 退出码 %s：%s" % (rc, tail[-120:]))
    media = [p for p in _new_files(save_dir, t0) if p.lower().endswith((".mp4", ".mkv", ".flv", ".m4a", ".mp3"))]
    if not media:
        raise RuntimeError("BBDown 没产出成品文件")
    return media[0]
