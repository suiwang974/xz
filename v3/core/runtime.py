# -*- coding: utf-8 -*-
"""运行时握手：内核把「真实端口 + 界面令牌」交给宿主，彻底解决端口被占后界面连错的问题。

两条通道（宿主任选其一，Electron 两条都用）：
  1) stdout 一行：SNIFFDL_READY {"port":6800,"ui_token":"…","pid":123,"version":"3.0.0"}
  2) 运行时状态文件 runtime.json（默认 程序目录/runtime.json，可 --runtime 指定），
     内容同上 + ts；内核退出时删除。pywebview 后备宿主与命令行工具读它。
"""
import json
import os
import secrets
import sys
import time

VERSION = "3.0.0"


def new_token():
    return secrets.token_hex(16)


def write(path, port, ui_token, extra=None):
    info = {"port": int(port), "ui_token": ui_token, "pid": os.getpid(), "version": VERSION,
            "ts": int(time.time())}
    info.update(extra or {})
    if path:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(info, f, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception:
            pass
    try:
        sys.stdout.write("SNIFFDL_READY " + json.dumps(info, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except Exception:
        pass
    return info


def read(path, max_age=None):
    try:
        with open(path, encoding="utf-8") as f:
            info = json.load(f)
        if max_age and time.time() - float(info.get("ts") or 0) > max_age:
            return None
        pid = int(info.get("pid") or 0)
        if pid and not _alive(pid):
            return None
        return info
    except Exception:
        return None


def _alive(pid):
    try:
        if os.name == "nt":
            # 只 OpenProcess 不够：进程已退出但还有人攥着它的句柄（比如拉起它的宿主/测试进程的 Popen）时
            # OpenProcess 照样成功 → 看门狗永远以为父进程活着。必须再查退出码是不是 STILL_ACTIVE(259)。
            import ctypes
            from ctypes import wintypes
            k = ctypes.windll.kernel32
            k.OpenProcess.restype = wintypes.HANDLE
            k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            h = k.OpenProcess(0x1000, False, int(pid))      # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            try:
                code = wintypes.DWORD(0)
                if not k.GetExitCodeProcess(h, ctypes.byref(code)):
                    return True                              # 查不到退出码：保守当活着
                return code.value == 259                     # STILL_ACTIVE
            finally:
                k.CloseHandle(h)
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def remove(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass
