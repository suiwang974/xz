# -*- coding: utf-8 -*-
"""SniffDL v3 界面 · pywebview 后备宿主（Electron 起不来 / 不想装 Electron 时用）。

和 Electron 宿主（pet_electron/app/main.js + preload.js）做同一件事：
  · 加载 v3/ui/index.html——经内核托管的 http://127.0.0.1:<port>/ui/（file:// 下 ES module 会被 CORS 拦，见 main()）；
  · 自行拉起内核 sniffdl_core.py（打包版 SniffDL.exe --core），或复用已在跑的内核（runtime.json 的 pid 活着且 ping 通）；
  · 给页面同一套 window.sniffdlHost：conn() / pickDir(title) / pickFile(filters) / saveFile(name, text) / notify(title, body)
    （另有 show / quit / setProgress，与 Electron 一致；kind = 'webview'）。

实现：Python 侧 HostApi 走 pywebview js_api（页面里是 window.pywebview.api.*，全异步 Promise），
页面加载后注入一小段 JS 把它包成 window.sniffdlHost。ui/js/api.js 两条路都认（sniffdlHost 优先，
pywebview.api.conn/pick_dir/pick_file 兜底），注入早晚都不影响连接。

用法：python ui_web.py [--runtime runtime.json] [--data-dir DIR] [--core-port N] [--debug]
退出：关窗即退；本宿主拉起的内核随之关闭（复用的内核不关，不归本宿主管）。
"""
import argparse
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = getattr(sys, "_MEIPASS", HERE)
VERSION_HOST = "webview"

# 页面加载后注入：把 pywebview.api 包成与 Electron preload 同形的 window.sniffdlHost
HOST_SHIM = r"""
(function () {
  if (window.sniffdlHost && window.sniffdlHost.kind === 'webview') return;
  var A = function () { return (window.pywebview && window.pywebview.api) || {}; };
  var J = function (v) { try { return typeof v === 'string' ? JSON.parse(v) : v; } catch (e) { return v; } };
  window.sniffdlHost = {
    kind: 'webview',
    conn: function () { return Promise.resolve(A().conn ? A().conn() : null).then(J); },
    pickDir: function (title) { return A().pick_dir ? A().pick_dir(title || '') : Promise.resolve(null); },
    pickFile: function (filters) { return A().pick_file ? A().pick_file(filters || null) : Promise.resolve(null); },
    saveFile: function (name, text) {
      if (text !== null && typeof text === 'object') text = JSON.stringify(text, null, 2);
      return A().save_file ? A().save_file(name || 'sniffdl.json', String(text == null ? '' : text)) : Promise.resolve(null);
    },
    notify: function (title, body) { return A().notify ? A().notify(String(title || ''), String(body || '')) : Promise.resolve(false); },
    show: function () { return A().show && A().show(); },
    quit: function () { return A().quit && A().quit(); },
    setProgress: function (p) { return A().set_progress && A().set_progress(Number(p)); },
  };
  try { window.dispatchEvent(new Event('sniffdlhostready')); } catch (e) {}
})();
"""


def _rpc(info, method, params=None, timeout=4):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}).encode("utf-8")
    req = urllib.request.Request("http://127.0.0.1:%d/jsonrpc" % int(info["port"]), data=body,
                                 headers={"Content-Type": "application/json", "X-SniffDL-Token": info.get("ui_token") or ""})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        j = json.loads(r.read().decode("utf-8", "replace"))
    if j.get("error"):
        raise RuntimeError(j["error"].get("message") or "rpc error")
    return j.get("result")


def _pid_alive(pid):
    if not pid:
        return False
    try:
        if os.name == "nt":
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if not h:
                return False
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def core_cmd():
    """与 Electron coreCmd() 同序：SNIFFDL_CORE_CMD（JSON 数组）> Windows 下 SniffDL.exe --core > python sniffdl_core.py"""
    try:
        c = json.loads(os.environ.get("SNIFFDL_CORE_CMD") or "null")
        if isinstance(c, list) and c:
            return c
    except Exception:
        pass
    if os.name == "nt":
        for exe in (os.path.join(HERE, "SniffDL.exe"), os.path.join(os.path.dirname(sys.executable), "SniffDL.exe")):
            if os.path.isfile(exe):
                return [exe, "--core"]
    py = os.environ.get("SNIFFDL_PYTHON") or (sys.executable if not getattr(sys, "frozen", False) else "python")
    return [py, os.path.join(HERE, "sniffdl_core.py")]


class CoreManager:
    """拉起或复用内核；conn() 给界面用。内核意外退出时下一次 conn() 自动再拉起一次（token 会变，api.js 会重新 discover）。"""

    def __init__(self, runtime=None, data_dir=None, port=0):
        self.runtime = os.path.abspath(runtime or os.path.join(HERE, "runtime.json"))
        self.data_dir, self.port = data_dir, int(port or 0)
        self.proc, self.info, self.attached = None, None, False
        self._lock = threading.Lock()

    def _read_runtime(self):
        try:
            with open(self.runtime, encoding="utf-8") as f:
                j = json.load(f)
            return j if j.get("port") and _pid_alive(j.get("pid")) else None
        except Exception:
            return None

    def _alive(self, info):
        try:
            return bool((_rpc(info, "sniffdl.ping", timeout=2) or {}).get("ok"))
        except Exception:
            return False

    def ensure(self, timeout=20):
        with self._lock:
            if self.info and self._alive(self.info):
                return self.info
            old = self._read_runtime()
            if old and self._alive(old):
                self.info, self.attached, self.proc = old, True, None
                return old
            return self._spawn(timeout)

    def _spawn(self, timeout):
        token = secrets.token_hex(16)
        cmd = core_cmd() + ["--runtime", self.runtime, "--ui-token", token, "--host", "webview",
                            "--ui-dir", os.path.join(BASE, "ui"), "--parent-pid", str(os.getpid()), "--watch-stdin"]
        if self.port:
            cmd += ["--port", str(self.port)]
        if self.data_dir:
            cmd += ["--data-dir", os.path.abspath(self.data_dir)]
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", SNIFFDL_HOST="webview")
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        self.proc = subprocess.Popen(cmd, cwd=HERE, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, **kw)
        got = {}

        def _read():
            for raw in iter(self.proc.stdout.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("SNIFFDL_READY ") and not got:
                    try:
                        got.update(json.loads(line[len("SNIFFDL_READY "):]))
                    except Exception:
                        pass
        threading.Thread(target=_read, daemon=True).start()
        end = time.time() + timeout
        while time.time() < end and not got and self.proc.poll() is None:
            time.sleep(0.1)
        if not got:
            return None
        self.info, self.attached = got, bool(got.get("attached"))
        if self.attached:          # 并发拉起时别人先起来了：我们这个进程已自己退出
            self.proc = None
        return self.info

    def conn(self):
        info = self.ensure()
        if not info:
            return None
        return {"port": int(info["port"]), "token": info.get("ui_token") or "", "pid": info.get("pid"),
                "version": info.get("version"), "attached": self.attached}

    def stop(self):
        """只关本宿主拉起的内核。关 stdin（--watch-stdin）+ shutdown RPC 双保险。"""
        if not self.proc:
            return
        try:
            _rpc(self.info, "sniffdl.core.shutdown", timeout=3)
        except Exception:
            pass
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(5)
        except Exception:
            self.proc.terminate()
        self.proc = None


class HostApi:
    """pywebview js_api 叶子对象。注意：pywebview 会递归扫描 js_api 的属性生成桥，持有 window 等对象引用
    会爬进对象图挂死（§29.2 实测坑）——所以引用都藏在下划线私有属性 + 模块级 _STATE 里。"""

    def __init__(self, core, window_getter=None, webview_mod=None):
        _STATE["core"], _STATE["win"], _STATE["wv"] = core, window_getter or (lambda: None), webview_mod

    # --- 与 Electron preload 同名同义（pywebview 侧是 snake_case，HOST_SHIM 包成 camelCase）
    def conn(self):
        return _STATE["core"].conn()

    def pick_dir(self, title=""):
        w, wv = _STATE["win"](), _STATE["wv"]
        if not w or not wv:
            return None
        r = w.create_file_dialog(_dialog(wv, "FOLDER"), directory=_STATE.get("last_dir") or "")
        p = (r[0] if isinstance(r, (list, tuple)) and r else r) or None
        if p:
            _STATE["last_dir"] = p
        return p

    def pick_file(self, filters=None):
        w, wv = _STATE["win"](), _STATE["wv"]
        if not w or not wv:
            return None
        r = w.create_file_dialog(_dialog(wv, "OPEN"), file_types=tuple(electron_filters(filters)))
        return (r[0] if isinstance(r, (list, tuple)) and r else r) or None

    def save_file(self, name, text):
        w, wv = _STATE["win"](), _STATE["wv"]
        if not w or not wv:
            return None
        r = w.create_file_dialog(_dialog(wv, "SAVE"), save_filename=os.path.basename(name or "sniffdl.json"))
        p = (r[0] if isinstance(r, (list, tuple)) and r else r) or None
        if not p:
            return None
        if not isinstance(text, str):
            text = json.dumps(text, ensure_ascii=False, indent=2)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def notify(self, title, body=""):
        return system_notify(title, body)

    def show(self):
        w = _STATE["win"]()
        try:
            w and (w.restore(), w.show())
        except Exception:
            pass
        return True

    def quit(self):
        w = _STATE["win"]()
        try:
            w and w.destroy()
        except Exception:
            pass
        return True

    def set_progress(self, p):
        return False   # pywebview 无跨平台任务栏进度；Electron 宿主才有


_STATE = {}


def _dialog(wv, kind):
    # pywebview ≥5 用 webview.FileDialog.FOLDER；4.x 用 webview.FOLDER_DIALOG
    fd = getattr(wv, "FileDialog", None)
    if fd is not None and hasattr(fd, kind):
        return getattr(fd, kind)
    return getattr(wv, kind + "_DIALOG")


def electron_filters(filters):
    """Electron 格式 [{name:'种子', extensions:['torrent']}] → pywebview 格式 ('种子 (*.torrent)', …)"""
    out = []
    for f in filters or []:
        try:
            exts = [e.strip().lstrip(".") for e in (f.get("extensions") or []) if e and e.strip()]
            if not exts:
                continue
            pat = ";".join("*" if e == "*" else "*." + e for e in exts)
            out.append("%s (%s)" % (f.get("name") or "文件", pat))
        except Exception:
            continue
    if out:
        out.append("所有文件 (*.*)")
    return out


def system_notify(title, body=""):
    """系统通知：Windows 用 PowerShell 气泡（无依赖），macOS osascript，Linux notify-send；都没有返回 False。"""
    title, body = str(title or "SniffDL")[:120], str(body or "")[:400]
    try:
        if os.name == "nt":
            ps = ("Add-Type -AssemblyName System.Windows.Forms;$n=New-Object System.Windows.Forms.NotifyIcon;"
                  "$n.Icon=[System.Drawing.SystemIcons]::Information;$n.Visible=$true;"
                  "$n.ShowBalloonTip(5000,$env:SDL_T,$env:SDL_B,'Info');Start-Sleep 6;$n.Dispose()")
            subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                             env=dict(os.environ, SDL_T=title, SDL_B=body), creationflags=0x08000000)
            return True
        if sys.platform == "darwin":
            subprocess.Popen(["osascript", "-e", 'display notification %s with title %s' % (json.dumps(body), json.dumps(title))])
            return True
        subprocess.Popen(["notify-send", "-a", "SniffDL", title, body], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sniffdl-webview")
    ap.add_argument("--runtime", default=os.path.join(HERE, "runtime.json"))
    ap.add_argument("--data-dir", default="")
    ap.add_argument("--core-port", type=int, default=0)
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--legacy-v2", action="store_true", help="旧路线 B：加载 ui_web/index.html（v2 视觉稿）并代理到旧 Tk 程序的 RPC")
    a, _rest = ap.parse_known_args(sys.argv[1:] if argv is None else argv)   # 容忍打包入口带的 --webview 等参数
    import webview
    if a.legacy_v2:
        return legacy_v2_main(webview)
    core = CoreManager(a.runtime, a.data_dir or None, a.core_port)
    holder = {}
    api = HostApi(core, lambda: holder.get("w"), webview)
    # 界面不用 file:// 加载：WebView2 / Chromium 对 file:// 的 ES module 一律按 CORS 拦掉（实测 main.js 被拦），
    # 所以先显示「正在启动」，内核就绪后加载内核自己托管的 http://127.0.0.1:<port>/ui/index.html（同源、CSP 'self' 生效）。
    w = webview.create_window("SniffDL", html=LOADING_HTML, js_api=api, width=1320, height=860,
                              min_size=(980, 640), resizable=True)
    holder["w"] = w
    try:
        w.events.loaded += lambda: w.evaluate_js(HOST_SHIM)
    except Exception:
        pass

    def boot():
        c = core.conn()
        if c:
            w.load_url(ui_url(c))
        else:
            w.load_html(FAIL_HTML.replace("{RT}", core.runtime))
    try:
        # UA 带 pywebview：api.js 会先等 pywebviewready 再 discover，避免桥还没注入就判「拿不到内核地址」
        webview.start(boot, debug=a.debug, user_agent=_ua(), private_mode=False)
    finally:
        core.stop()


def ui_url(c):
    return "http://127.0.0.1:%d/ui/index.html" % int(c["port"])


LOADING_HTML = ("<html><body style='font:15px sans-serif;display:flex;align-items:center;justify-content:center;"
                "height:95vh;color:#5C6880'>正在启动 SniffDL 内核…</body></html>")
FAIL_HTML = ("<html><body style='font:15px sans-serif;padding:40px;color:#1F2A44'><h3>SniffDL 内核没能启动</h3>"
             "<p>20 秒内没收到内核就绪信号。请查看程序目录下的 sniffdl.log，或从命令行运行 "
             "<code>python sniffdl_core.py</code> 看报错。</p><p style='color:#888'>runtime：{RT}</p></body></html>")


# ---------------- 旧路线 B（v2 视觉稿 + 旧 Tk RPC），保留以免旧入口失效 ----------------
class LegacyApi:
    def call(self, method, params_json="[]"):
        port, token = 6800, ""
        for cf in (os.path.join(HERE, "config.json"), os.path.join(BASE, "config.json")):
            try:
                c = json.load(open(cf, encoding="utf-8"))
                port, token = int(c.get("rpc_port") or port), c.get("rpc_token") or ""
                break
            except Exception:
                continue
        try:
            params = list(json.loads(params_json) if params_json else [])
        except Exception:
            params = []
        if token:
            params = ["token:" + token] + params
        try:
            r = _rpc({"port": port}, method, params, timeout=8)
            return json.dumps({"ok": True, "result": r, "error": None})
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)})


def legacy_v2_main(webview):
    idx = os.path.join(BASE, "ui_web", "index.html")
    webview.create_window("SniffDL", "file:///" + idx.replace("\\", "/").lstrip("/"), js_api=LegacyApi(),
                          width=1320, height=860, min_size=(980, 640), resizable=True)
    webview.start()


def _ua():
    return ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36 SniffDL-pywebview/" + VERSION_HOST)


if __name__ == "__main__":
    main()
