# -*- coding: utf-8 -*-
"""pet_bridge：主程序 ↔ 大肥鱼桌宠宿主（Electron，pet_electron/app/main.js）的桥。

职责：
  1) 拉起/守护桌宠宿主进程（pet_electron/electron(.exe) pet_electron/app --state pet_state.json）
  2) 读 pet_state.json 拿控制端口 + token
  3) 提供 fire-and-forget 控制：say/react/skin/show/hide/quit——全部走本机 HTTP，
     任何失败都静默降级（桌宠挂了不能拖累下载主流程），并自动重启宿主。
"""
import json
import os
import random
import subprocess
import sys
import threading
import time
import urllib.request

def _app_dir():
    # 冻结形态 __file__ 指向 _MEIPASS 解包目录（每次启动变），锚点必须是 exe 所在目录
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))

_APP_DIR = _app_dir()
_IS_WIN = os.name == "nt"
_ELECTRON_NAME = "electron.exe" if _IS_WIN else "electron"
# 状态文件：主程序与 Electron 宿主共用同一个 pet_state.json（启动时用 --state 显式传给 main.js），
# 有 pet_electron/ 目录就放在里面，否则放程序目录。
STATE_FILE = (os.path.join(_APP_DIR, "pet_electron", "pet_state.json")
              if os.path.isdir(os.path.join(_APP_DIR, "pet_electron"))
              else os.path.join(_APP_DIR, "pet_state.json"))


def find_electron(me=None):
    """返回 (electron 可执行文件, app 目录)；找不到返回 (None, None)。
    顺序：pet_electron/electron(.exe) + pet_electron/app（便携包/源码旁放了运行环境）
         → 源码开发形态 pet_electron/node_modules/electron/dist + pet_electron/app。"""
    me = me or _APP_DIR
    pe = os.path.join(me, "pet_electron")
    app_dir = os.path.join(pe, "app")
    if not os.path.isfile(os.path.join(app_dir, "main.js")):
        return None, None
    cands = [os.path.join(pe, _ELECTRON_NAME),
             os.path.join(pe, "node_modules", "electron", "dist", _ELECTRON_NAME)]
    env = os.environ.get("SNIFFDL_ELECTRON", "")
    if env:
        cands.insert(0, env)
    for exe in cands:
        if exe and os.path.isfile(exe):
            return exe, app_dir
    return None, None


class PetBridge:
    def __init__(self, skin=None, port=None, wake=True, attach=None):
        self.lock = threading.Lock()
        self.proc = None
        self.state = None          # {port, token, ...}
        self.dead = False          # 宿主彻底起不来 -> 上层可退回旧桌宠
        self.skin = skin or "horn"
        self.port = int(port or 6801)
        self.wake = bool(wake)     # False=启动即收起（Ctrl+Alt+H 唤醒）
        self.token = "".join(random.choice("0123456789abcdef") for _ in range(24))
        self._restarts = 0
        self._last_up = 0.0
        # attach=(port, token)：桌宠窗由 v3 Electron 宿主（同一运行时）提供，这里只连它的控制端口，不拉进程
        self.attach = attach
        if attach:
            self.port, self.token = int(attach[0]), str(attach[1])
            self.state = {"port": self.port, "token": self.token}

    # ---------- 进程 ----------
    def _spawn(self):
        me = _app_dir()
        exe, app_dir = find_electron(me)
        if not exe:
            return None
        # 旧状态文件（上次运行留下的端口/token）先删掉，免得读到过期 token
        try:
            os.remove(STATE_FILE)
        except OSError:
            pass
        cmd = [exe, app_dir, "--port", str(self.port), "--token", self.token,
               "--state", STATE_FILE]
        if not _IS_WIN:
            cmd.insert(1, "--no-sandbox")         # 仅 Linux 验收用：容器里没有 suid chrome-sandbox
        if not self.wake:
            cmd += ["--hidden", "1"]
        kw = {}
        if _IS_WIN:
            kw["creationflags"] = 0x08000000      # CREATE_NO_WINDOW，仅 Windows 有
        try:
            return subprocess.Popen(cmd, cwd=os.path.dirname(exe),
                                    stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, **kw)
        except Exception:
            return None

    def start(self):
        if self.attach:
            return True
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                return True
            self.proc = self._spawn()
            if self.proc is None:
                self.dead = True
                return False
        threading.Thread(target=self._wait_state, daemon=True).start()
        return True

    def _wait_state(self):
        for _ in range(60):
            time.sleep(0.5)
            st = self._read_state()
            if st:
                with self.lock:
                    self.state = st
                return
        self.dead = True

    def _read_state(self):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                st = json.load(f)
            if time.time() - float(st.get("ts", 0)) > 3600:
                return None
            if st.get("token") != self.token:     # 不是本次拉起的宿主写的
                return None
            return st
        except Exception:
            return None

    def _ensure_up(self):
        if self.attach:
            return True
        with self.lock:
            alive = self.proc is not None and self.proc.poll() is None
        if alive:
            return True
        # 60 秒内重启不超过 3 次，避免宿主反复崩溃时疯抢
        now = time.time()
        if now - self._last_up > 60:
            self._restarts = 0
        if self._restarts >= 3:
            return False
        self._restarts += 1
        self._last_up = now
        self.dead = False
        with self.lock:
            self.state = None
        return self.start()

    # ---------- 控制 ----------
    def _post(self, path, body):
        def run():
            if not self._ensure_up():
                return
            with self.lock:
                st = self.state
            if not st:
                # 宿主刚被（重新）拉起：等它写好 pet_state.json，别把这条指令丢了
                for _ in range(40):
                    st = self._read_state()
                    if st:
                        break
                    time.sleep(0.5)
                if not st:
                    return
                with self.lock:
                    self.state = st
            try:
                req = urllib.request.Request(
                    "http://127.0.0.1:%d%s" % (st["port"], path),
                    data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                    method="POST",
                    headers={"Authorization": "Bearer " + st["token"],
                             "Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=3).read()
            except Exception:
                pass
        threading.Thread(target=run, daemon=True).start()

    def say(self, text, ms=None):
        text = str(text or "")
        if ms is None:
            ms = int(min(9.0, max(3.5, 3.2 + 0.10 * len(text))) * 1000)
        self._post("/api/bubble", {"text": text, "ms": ms})

    def react(self, name):
        self._post("/api/react", {"name": str(name)})

    def set_skin(self, key):
        self.skin = key
        self._post("/api/skin", {"key": key})

    def visible(self, on):
        self._post("/api/visible", {"on": bool(on)})

    def quit(self):
        if self.attach:
            return
        try:
            self._post("/api/quit", {})
        except Exception:
            pass
        with self.lock:
            p = self.proc
        try:
            if p is not None and p.poll() is None:
                p.terminate()
        except Exception:
            pass
