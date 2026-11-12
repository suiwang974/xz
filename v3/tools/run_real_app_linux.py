# -*- coding: utf-8 -*-
"""在 Linux（Xvfb）上跑【真 SniffDL 主程序】做端到端验收。
只替换真正 Windows 专属的一处：launch_webview（Windows 上拉起 pywebview/WebView2 子进程，
Linux 上 subprocess 的 creationflags 不支持会失败并回退显示 Tk 主窗）。替换成"假装 webview 已开"，
这样 Tk 主窗保持 withdraw —— 和 Windows 上默认 ui_mode=web 的真实状态一致。
    DISPLAY=:77 python tools/run_real_app_linux.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
sys.argv = [os.path.join(ROOT, "video_downloader.py")] + sys.argv[1:]
import video_downloader as V  # noqa: E402

V.App.launch_webview = lambda self: self.log("[测试桩] webview 主界面由桩代替（Windows=WebView2 子进程），Tk 主窗保持隐藏")
V.main()
