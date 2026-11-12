# -*- coding: utf-8 -*-
"""拟人操作 + 录屏分段时间戳（给 edit_video.py 剪辑用）。"""
import json
import random
import time

try:
    import pyautogui
    pyautogui.FAILSAFE = False
    pyautogui.PAUSE = 0
except Exception:          # 没有 pyautogui 时退化为无鼠标动作
    pyautogui = None


class Timeline:
    """记录录屏里每一段的起止时间与播放倍速：speed=1 正常操作，>1 是等待（剪辑时加速）。"""

    def __init__(self, path):
        self.path = path
        self.t0 = None
        self.segs = []
        self.cur = None

    def start(self, rec=None):
        self.t0 = time.time()
        self.offset = (self.t0 - rec.t0) if rec is not None and hasattr(rec, "t0") else 0.0

    def seg(self, label, speed=1.0):
        now = time.time() - (self.t0 or time.time())
        if self.cur:
            self.cur["end"] = now
            self.segs.append(self.cur)
        self.cur = {"label": label, "start": now, "speed": speed}

    def end(self):
        self.seg("_end")
        self.cur = None
        json.dump({"offset": getattr(self, "offset", 0.0), "segs": self.segs}, open(self.path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def pause(a=0.4, b=1.0):
    time.sleep(random.uniform(a, b))


def move(x, y, dur=None):
    if not pyautogui:
        return
    dur = dur if dur is not None else random.uniform(0.2, 0.5)
    pyautogui.moveTo(int(x), int(y), duration=dur, tween=pyautogui.easeInOutQuad)


def click(x, y, button="left"):
    if not pyautogui:
        return False
    move(x, y)
    time.sleep(random.uniform(0.08, 0.18))
    pyautogui.click(button=button)
    pause()
    return True


def type_text(s, a=0.03, b=0.09):
    if not pyautogui:
        return False
    for ch in s:
        pyautogui.write(ch)
        time.sleep(random.uniform(a, b))
    return True


def hotkey(*keys):
    if pyautogui:
        pyautogui.hotkey(*keys)
        pause(0.3, 0.6)


def press(k, n=1):
    if pyautogui:
        for _ in range(n):
            pyautogui.press(k)
            time.sleep(random.uniform(0.15, 0.3))


def page_xy(page, selector):
    """页面元素中心点 → 屏幕坐标（Windows 100% 缩放；窗口有/无边框都按 outer-inner 差算）。"""
    box = page.locator(selector).first.bounding_box()
    if not box:
        return None
    g = page.evaluate("({sx:window.screenX, sy:window.screenY, ow:window.outerWidth, oh:window.outerHeight,"
                      " iw:window.innerWidth, ih:window.innerHeight, dpr:window.devicePixelRatio})")
    bx = (g["ow"] - g["iw"]) / 2
    by = g["oh"] - g["ih"] - bx
    d = g["dpr"] or 1
    return (g["sx"] + bx + box["x"] + box["width"] / 2) * d, (g["sy"] + by + box["y"] + box["height"] / 2) * d
