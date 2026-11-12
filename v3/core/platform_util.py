# -*- coding: utf-8 -*-
"""跨平台：打开文件/文件夹并高亮、关机/睡眠/休眠命令、开机自启。

测试与演示：环境变量 SNIFFDL_DRYRUN_OS=1 时只记录要执行的动作（写进 ACTIONS 并返回），
不真的打开资源管理器/关机——Linux 上的端到端测试靠它断言「点了打开文件夹 → 内核确实去打开了哪个路径」。
"""
import os
import subprocess
import sys
import time

IS_WIN = os.name == "nt"
ACTIONS = []          # 最近执行的系统动作（dry-run 与真实都记，供 sniffdl.debug.osActions 查询）


def _dry():
    return os.environ.get("SNIFFDL_DRYRUN_OS") == "1"


def _record(kind, target):
    ACTIONS.append({"kind": kind, "target": target, "ts": time.time()})
    del ACTIONS[:-200]


def open_path(path):
    """系统默认程序打开文件（或文件夹）。"""
    _record("open", path)
    if _dry():
        return True
    if IS_WIN:
        os.startfile(path)                                  # noqa
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


def reveal_path(path):
    """打开所在文件夹并选中该文件。"""
    _record("reveal", path)
    if _dry():
        return True
    if IS_WIN:
        if os.path.isfile(path):
            subprocess.Popen('explorer /select,"%s"' % os.path.abspath(path))
        else:
            os.startfile(path if os.path.isdir(path) else os.path.dirname(path))   # noqa
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    else:
        d = path if os.path.isdir(path) else os.path.dirname(path)
        subprocess.Popen(["xdg-open", d], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


POWER_CMDS_WIN = {
    "关机": ["shutdown", "/s", "/t", "60", "/c", "SniffDL：下载已全部完成，60 秒后关机（shutdown /a 可取消）"],
    "睡眠": ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
    "休眠": ["shutdown", "/h"],
    "重启": ["shutdown", "/r", "/t", "0"],
}
POWER_CMDS_LINUX = {"关机": ["systemctl", "poweroff"], "睡眠": ["systemctl", "suspend"],
                    "休眠": ["systemctl", "hibernate"], "重启": ["systemctl", "reboot"]}


def power(action):
    cmds = POWER_CMDS_WIN if IS_WIN else POWER_CMDS_LINUX
    cmd = cmds.get(action)
    if not cmd:
        return False
    _record("power", action + " :: " + " ".join(cmd))
    if _dry():
        return True
    try:
        if IS_WIN and action == "睡眠":
            # 睡眠前关掉「休眠代替睡眠」不是我们的事；直接调 SetSuspendState
            subprocess.Popen(cmd, creationflags=0x08000000)
        else:
            subprocess.Popen(cmd, **({"creationflags": 0x08000000} if IS_WIN else {}))
        return True
    except Exception:
        return False


def cancel_shutdown():
    _record("power", "cancel")
    if _dry() or not IS_WIN:
        return True
    try:
        subprocess.Popen(["shutdown", "/a"], creationflags=0x08000000)
        return True
    except Exception:
        return False


def run_command(cmdline, env_extra=None, cwd=None):
    """完成后运行命令（用户在设置里写的），不等待。{file}/{dir}/{name} 已由调用方替换。"""
    _record("run", cmdline)
    env = dict(os.environ)
    env.update(env_extra or {})
    kw = {"shell": True, "env": env, "cwd": cwd or None,
          "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if IS_WIN:
        kw["creationflags"] = 0x08000000
    return subprocess.Popen(cmdline, **kw)


def drives():
    if IS_WIN:
        import string
        return [s + ":\\" for s in string.ascii_uppercase if os.path.exists(s + ":\\")]
    return ["/"] + ([os.path.expanduser("~")] if os.path.isdir(os.path.expanduser("~")) else [])
