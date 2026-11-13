# -*- coding: utf-8 -*-
"""剪贴板监视：宿主（Electron clipboard 模块）每秒把剪贴板文本上报给内核 sniffdl.clip.offer；
没有宿主上报时（pywebview 后备 / 纯内核），Windows 上内核自己用 Win32 API 轮询兜底。
内核只做识别 + 去重 + 推 clip 事件，弹提示和一键下载由界面完成。"""
import os
import threading
import time

IS_WIN = os.name == "nt"


def read_win32():
    if not IS_WIN:
        return None
    import ctypes
    from ctypes import wintypes
    CF_UNICODETEXT = 13
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    u32.GetClipboardData.restype = wintypes.HANDLE
    k32.GlobalLock.restype = wintypes.LPVOID
    if not u32.OpenClipboard(None):
        return None
    try:
        h = u32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ""
        p = k32.GlobalLock(h)
        try:
            return ctypes.wstring_at(p)
        finally:
            k32.GlobalUnlock(h)
    finally:
        u32.CloseClipboard()


class ClipWatcher:
    def __init__(self, on_text, enabled=lambda: True):
        self.on_text = on_text
        self.enabled = enabled
        self.last_host_offer = 0.0
        self._last = None
        if IS_WIN:
            threading.Thread(target=self._loop, daemon=True, name="ClipWatch").start()

    def offer(self, text, source="host"):
        if source == "host":
            self.last_host_offer = time.time()
        text = (text or "").strip()
        if not text or text == self._last:
            return False
        self._last = text
        if self.enabled():
            try:
                self.on_text(text)
            except Exception:
                return False            # 回调出错不能带崩监视线程
            return True
        return False

    def _loop(self):
        first = True
        while True:
            time.sleep(1.0)
            if time.time() - self.last_host_offer < 5:       # 宿主在报，就不重复读
                continue
            try:
                t = read_win32()
            except Exception:
                t = None
            if t is None:
                continue
            if first:                       # 启动时剪贴板里的旧内容不算「新复制」
                self._last = (t or "").strip()
                first = False
                continue
            self.offer(t, source="native")
