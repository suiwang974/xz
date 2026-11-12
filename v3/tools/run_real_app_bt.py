# -*- coding: utf-8 -*-
"""BT 实测用：在 Linux（Xvfb）上跑【真 SniffDL 主程序】，但把 HOME / config / 队列 / 账本全部隔离到 --sandbox 目录，
绝不碰源码目录里用户的 config.json / queue.json / downloaded.json。
额外只做两件"观测"桩（不改行为）：
  - launch_webview 同 run_real_app_linux.py（Windows=WebView2 子进程）；
  - _pet_react 包一层：把每次调用写进 <sandbox>/pet_react.log 再调原函数（检查 BT 开始/完成/失败是否触发桌宠）。
    DISPLAY=:77 python tools/run_real_app_bt.py --sandbox /tmp/bt/sb [--port 6800]
"""
import argparse
import json
import os
import sys
import time

ap = argparse.ArgumentParser()
ap.add_argument("--sandbox", required=True)
ap.add_argument("--port", type=int, default=6800)
ap.add_argument("--aria2c", default="/usr/bin/aria2c")
a, rest = ap.parse_known_args()
SB = os.path.abspath(a.sandbox)
os.makedirs(SB, exist_ok=True)
os.environ["HOME"] = SB                       # ~/.snifftdl/bt 与 DEFAULT_OUTDIR 都落进沙箱

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
sys.argv = [os.path.join(ROOT, "video_downloader.py")] + rest
import video_downloader as V  # noqa: E402

cfgp = os.path.join(SB, "config.json")
if not os.path.isfile(cfgp):
    with open(cfgp, "w", encoding="utf-8") as f:
        json.dump({"outdir": os.path.join(SB, "下载 输出"), "aria2c_path": a.aria2c,
                   "rpc_enable": True, "rpc_port": a.port, "rpc_token": "", "pet_live": False,
                   "rt_enable": False}, f, ensure_ascii=False, indent=1)
V.CONFIG_FILE = cfgp
V.HISTORY_FILE = os.path.join(SB, "downloaded.json")
V.QUEUE_FILE = os.path.join(SB, "queue.json")
V.STATS_FILE = os.path.join(SB, "stats.json")

V.App.launch_webview = lambda self: self.log("[测试桩] webview 主界面由桩代替，Tk 主窗保持隐藏")
_orig_react = V.App._pet_react


def _logged_react(self, name):
    with open(os.path.join(SB, "pet_react.log"), "a", encoding="utf-8") as f:
        f.write("%.2f %s\n" % (time.time(), name))
    return _orig_react(self, name)


V.App._pet_react = _logged_react

_orig_log = V.App.log


def _tee_log(self, m, *aa, **kk):
    try:
        with open(os.path.join(SB, "app.log"), "a", encoding="utf-8") as f:
            f.write("%.2f %s\n" % (time.time(), m))
    except Exception:
        pass
    return _orig_log(self, m, *aa, **kk)


V.App.log = _tee_log
_orig_init = V.App.__init__


def _init(self, *aa, **kk):
    _orig_init(self, *aa, **kk)
    import signal
    # 测试钩子：SIGUSR1 = 走真退出路径 _real_quit（会调 _bt_shutdown）；SIGUSR2 = 显示 Tk 主窗并打开 BT 面板（面板 transient 于主窗，主窗隐藏时面板也不显示）
    signal.signal(signal.SIGUSR1, lambda *_: self.root.after(0, self._real_quit))
    signal.signal(signal.SIGUSR2, lambda *_: self.root.after(0, lambda: (self.root.deiconify(), self.open_bt_panel())))

    def _tick():                       # 让 Tk 主循环定期回到 Python，信号才会被处理
        self.root.after(200, _tick)
    _tick()


V.App.__init__ = _init
with open(os.path.join(SB, "app.pid"), "w") as f:
    f.write(str(os.getpid()))
V.main()
