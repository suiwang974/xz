# -*- coding: utf-8 -*-
"""完成后动作 / 事件钩子 / Webhook / 自动解压 / 杀毒扫描 / 来自网络标记 / 下载时防睡眠。"""
import json
import os
import shlex
import shutil
import tarfile
import threading
import time
import urllib.request
import zipfile

from . import platform_util as PU

IS_WIN = os.name == "nt"
HOOK_EVENTS = ("added", "start", "error", "complete", "seed_done")


def fill(cmd, t):
    """命令里的占位符：{file} {dir} {name} {gid} {url} {status} {size}（自动加引号）。"""
    path = t.get("path") or os.path.join(t.get("dir") or "", t.get("name") or "")
    vals = {"file": path, "dir": t.get("dir") or os.path.dirname(path), "name": t.get("name") or "",
            "gid": t.get("gid") or "", "url": t.get("uri") or "", "status": t.get("status") or "",
            "size": str(t.get("totalLength") or 0)}
    out = cmd
    for k, v in vals.items():
        q = ('"%s"' % v.replace('"', '')) if IS_WIN else shlex.quote(v)
        out = out.replace("{%s}" % k, q)
    return out


def run_hook(cmd, t, log):
    if not (cmd or "").strip():
        return None
    line = fill(cmd, t)
    try:
        p = PU.run_command(line, env_extra={"SNIFFDL_GID": t.get("gid") or "",
                                            "SNIFFDL_FILE": t.get("path") or "",
                                            "SNIFFDL_STATUS": t.get("status") or ""},
                           cwd=t.get("dir") or None)
        log("[钩子] 已运行：%s" % line[:160])
        return p
    except Exception as e:
        log("[钩子] 运行失败：%s" % str(e)[:120])
        return None


def webhook(url, event, t, log):
    """Gopeed 式 webhook：POST JSON {event, task}。后台线程发，3 次重试，失败只记日志。"""
    if not (url or "").strip():
        return
    body = json.dumps({"event": event, "time": int(time.time()), "task": {
        "gid": t.get("gid"), "name": t.get("name"), "path": t.get("path"), "url": t.get("uri"),
        "status": t.get("status"), "size": t.get("totalLength"), "error": t.get("errorMessage") or ""}},
        ensure_ascii=False).encode("utf-8")

    def go():
        for i in range(3):
            try:
                req = urllib.request.Request(url.strip(), data=body, method="POST",
                                             headers={"Content-Type": "application/json",
                                                      "User-Agent": "SniffDL-Webhook/3"})
                urllib.request.urlopen(req, timeout=8).read()
                log("[Webhook] %s → %s 已送达" % (event, url[:60]))
                return
            except Exception as e:
                err = str(e)[:80]
                time.sleep(1 + i * 2)
        log("[Webhook] %s 发送失败：%s" % (event, err))
    threading.Thread(target=go, daemon=True, name="Webhook").start()


# ---------------- 自动解压 ----------------
ARCHIVE_EXT = (".zip", ".7z", ".rar", ".tar", ".tar.gz", ".tgz", ".tar.xz", ".tar.bz2")


def is_archive(path):
    low = (path or "").lower()
    if re_part(low):
        return True
    return low.endswith(ARCHIVE_EXT)


def re_part(low):
    import re
    return re.search(r"\.(part\d+\.rar|7z\.\d{3}|zip\.\d{3}|z\d{2})$", low) is not None


def find_7z():
    cands = [shutil.which("7z"), shutil.which("7za"), shutil.which("7zz")]
    if IS_WIN:
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
            cands.append(os.path.join(base, "7-Zip", "7z.exe"))
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def _safe_members_zip(zf, dest):
    for m in zf.infolist():
        p = os.path.realpath(os.path.join(dest, m.filename))
        if not p.startswith(os.path.realpath(dest) + os.sep) and p != os.path.realpath(dest):
            raise RuntimeError("压缩包含越界路径，已拒绝：%s" % m.filename[:60])


def extract(path, passwords=(), delete_after=False, log=print):
    """解到「同名文件夹」。zip/tar 用 Python 自带；7z/rar/分卷用 7-Zip（找不到就提示）。
    返回 (ok, dest, msg)。分卷：只在第一卷（.part1.rar / .7z.001 / .zip.001）上触发。"""
    low = path.lower()
    import re
    if re.search(r"\.part(\d+)\.rar$", low) and not re.search(r"\.part0*1\.rar$", low):
        return False, "", "非第一卷，跳过"
    if re.search(r"\.(7z|zip)\.(\d{3})$", low) and not low.endswith(".001"):
        return False, "", "非第一卷，跳过"
    stem = os.path.basename(path)
    for suf in (".tar.gz", ".tar.xz", ".tar.bz2", ".tgz", ".tar", ".zip", ".7z", ".rar", ".001"):
        if stem.lower().endswith(suf):
            stem = stem[:-len(suf)]
            break
    stem = re.sub(r"\.(part0*1|7z|zip)$", "", stem, flags=re.I)
    dest = os.path.join(os.path.dirname(path), stem or "extracted")
    os.makedirs(dest, exist_ok=True)
    try:
        if low.endswith(".zip"):
            with zipfile.ZipFile(path) as zf:
                _safe_members_zip(zf, dest)
                last = None
                for pw in [None] + [p for p in passwords if p]:
                    try:
                        zf.extractall(dest, pwd=(pw.encode() if pw else None))
                        last = None
                        break
                    except RuntimeError as e:      # 密码错误/需要密码
                        last = e
                if last is not None:
                    raise RuntimeError("需要密码（密码表里的都不对）")
        elif low.endswith((".tar", ".tar.gz", ".tgz", ".tar.xz", ".tar.bz2")):
            with tarfile.open(path) as tf:
                try:
                    tf.extractall(dest, filter="data")
                except TypeError:
                    tf.extractall(dest)
        else:
            sz = find_7z()
            if not sz:
                return False, dest, "没找到 7-Zip（7z/rar/分卷需要它），装 7-Zip 后会自动解压"
            import subprocess
            ok = False
            for pw in [""] + [p for p in passwords if p]:
                cmd = [sz, "x", "-y", "-o" + dest, "-p" + pw, path]
                r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   **({"creationflags": 0x08000000} if IS_WIN else {}))
                if r.returncode == 0:
                    ok = True
                    break
            if not ok:
                raise RuntimeError("7-Zip 解压失败（密码不对或分卷不全）")
        if delete_after:
            try:
                os.remove(path)
            except Exception:
                pass
        log("[解压] ✓ %s → %s" % (os.path.basename(path), dest))
        return True, dest, "已解压"
    except Exception as e:
        log("[解压] ✗ %s：%s" % (os.path.basename(path), str(e)[:120]))
        return False, dest, str(e)[:160]


# ---------------- 杀毒扫描 ----------------
def defender_path():
    if not IS_WIN:
        return None
    base = os.environ.get("ProgramFiles", r"C:\Program Files")
    p = os.path.join(base, "Windows Defender", "MpCmdRun.exe")
    return p if os.path.isfile(p) else None


def av_scan(path, custom_cmd, log):
    """完成后杀毒：优先用户自定义命令（{file} 占位），否则 Windows Defender 单文件扫描。"""
    if (custom_cmd or "").strip():
        return run_hook(custom_cmd, {"path": path, "dir": os.path.dirname(path),
                                     "name": os.path.basename(path)}, log)
    mp = defender_path()
    if not mp:
        PU._record("av", "no-scanner :: " + path)
        log("[杀毒] 本机没有可用的扫描器（非 Windows 或 Defender 缺失），跳过")
        return None
    cmd = '"%s" -Scan -ScanType 3 -File "%s" -DisableRemediation' % (mp, path)
    log("[杀毒] Defender 扫描：%s" % os.path.basename(path))
    return PU.run_command(cmd)


# ---------------- Mark-of-the-Web ----------------
def mark_of_web(path, url="", referer=""):
    """给下好的文件写 Zone.Identifier（ZoneId=3 来自互联网），SmartScreen / Office 受保护视图照常生效。
    只在 Windows NTFS 上有意义；其它平台记录后跳过。"""
    PU._record("motw", path)
    if not IS_WIN:
        return False
    try:
        txt = "[ZoneTransfer]\r\nZoneId=3\r\n"
        if referer:
            txt += "ReferrerUrl=%s\r\n" % referer
        if url and not url.startswith("magnet:"):
            txt += "HostUrl=%s\r\n" % url
        with open(path + ":Zone.Identifier", "w", encoding="utf-8") as f:
            f.write(txt)
        return True
    except Exception:
        return False


# ---------------- 下载时防睡眠 ----------------
class KeepAwake:
    """有任务在下时阻止系统自动睡眠（Windows SetThreadExecutionState），全部停下立即放开。
    专门一个线程持有状态（ES_CONTINUOUS 绑定在调用线程上）。"""
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001

    def __init__(self):
        self.active = False
        self._want = False
        self._ev = threading.Event()
        self.history = []
        threading.Thread(target=self._loop, daemon=True, name="KeepAwake").start()

    def set(self, want):
        want = bool(want)
        if want != self._want:
            self._want = want
            self._ev.set()

    def _apply(self, on):
        self.active = on
        self.history.append((time.time(), on))
        del self.history[:-50]
        PU._record("keepawake", "on" if on else "off")
        if IS_WIN:
            try:
                import ctypes
                flags = self.ES_CONTINUOUS | (self.ES_SYSTEM_REQUIRED if on else 0)
                ctypes.windll.kernel32.SetThreadExecutionState(flags)
            except Exception:
                pass

    def _loop(self):
        while True:
            self._ev.wait()
            self._ev.clear()
            if self._want != self.active:
                self._apply(self._want)
