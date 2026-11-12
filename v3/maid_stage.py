# -*- coding: utf-8 -*-
"""小助手「舞台」：纯 Python 的表演调度（不依赖 Tk，可单测）。

借鉴（只学思路）：
- VPet 的 A_Start / B_Loop / C_End：每个状态 = enter 段（播一次）+ loop 段（循环），状态切换不硬切。
- DyberPet 的 random_act + act_prob：待机随机小动作按权重抽签；摸摸 / 连点过快 / 拖拽 各有专属反应。
- live2d-widget 的触发器台词：悬停、单击、时段、久未操作分别取池，久未操作有计时器（用户一动就重置）。
- Shimeji-ee 的 Dragged / Thrown：被拖动时有挣扎小动作，松手有落地反应。

Stage 只决定「现在演哪个状态、播第几帧、下一帧多久后」，绘制仍由宿主（video_downloader）负责。
"""
import random
import time

# 反应优先级（越大越优先）；工作 / 下载状态由宿主在 choose() 里按业务优先处理
PRI = {"act": 1, "egg": 2, "hum": 1, "sleepy": 1, "happy": 3, "greet": 3, "surprise": 4, "shy": 4,
       "smug": 4, "pout": 5, "hug": 5, "whug": 5, "drag": 9, "wdrag": 9}

IDLE_ACT_AFTER = 75.0        # 无操作多少秒后开始做待机小动作
IDLE_ACT_GAP = (22.0, 48.0)  # 小动作之间的随机间隔
SLEEPY_AFTER = 300.0         # 无操作 5 分钟：犯困
LONELY_AFTER = 600.0         # 10 分钟：被冷落（只说一次）
SPAM_WINDOW = 3.0            # 连点判定窗口
SPAM_COUNT = 4


class Stage:
    def __init__(self, now=None):
        now = time.time() if now is None else now
        self.reset(now)

    def reset(self, now=None):
        now = time.time() if now is None else now
        self.react = None            # (state, until, pri)
        self.state = None
        self.phase = "enter"
        self.pos = 0
        self.last_input = now
        self.next_act = now + IDLE_ACT_AFTER
        self.clicks = []
        self.dragging = False
        self.sleepy_said = False
        self.lonely_said = False
        self.hover_since = None
        self.stare_at = 0.0

    # ------------------------------------------------------------ input
    def note_input(self, now=None):
        """用户有任何操作：重置久未操作计时；如果正在犯困，返回 'wake'"""
        now = time.time() if now is None else now
        was_idle = now - self.last_input
        self.last_input = now
        self.next_act = now + IDLE_ACT_AFTER
        woke = self.sleepy_said and was_idle >= SLEEPY_AFTER
        self.sleepy_said = False
        self.lonely_said = False
        if self.react and self.react[0] == "sleepy":
            self.react = None
        return "wake" if woke else None

    def click(self, now=None):
        """记一次单击；返回 'spam'（连点过快）或 None"""
        now = time.time() if now is None else now
        self.clicks = [t for t in self.clicks if now - t <= SPAM_WINDOW] + [now]
        if len(self.clicks) >= SPAM_COUNT:
            self.clicks = []
            return "spam"
        return None

    def do(self, state, dur, now=None, force=False):
        """播一个反应（有优先级：低优先级不打断高优先级，除非 force）"""
        now = time.time() if now is None else now
        pri = PRI.get(state, 3)
        if (not force and self.react and self.react[1] > now and self.react[2] > pri):
            return False
        self.react = (state, now + dur, pri)
        return True

    def clear(self, *states):
        if self.react and (not states or self.react[0] in states):
            self.react = None

    def current_react(self, now=None):
        now = time.time() if now is None else now
        if self.react and self.react[1] > now:
            return self.react[0]
        self.react = None
        return None

    # ------------------------------------------------------------ idle life
    def idle_tick(self, now, acts, quiet=False, night=False):
        """空闲时调用。返回 (state, line_kind) 表示该开演一个小动作 / 犯困 / 被冷落；否则 None。
        acts: [(state, weight, line_kind)]（DyberPet random_act 的思路）"""
        idle_for = now - self.last_input
        if quiet:
            return None
        if idle_for >= LONELY_AFTER and not self.lonely_said:
            self.lonely_said = True
            self.sleepy_said = True          # 被冷落时已经是犯困姿势，下一拍不再补一句「困了」
            self.do("sleepy", 3600, now, force=True)
            return ("sleepy", "lonely")
        if idle_for >= (SLEEPY_AFTER * (0.5 if night else 1.0)) and not self.sleepy_said:
            self.sleepy_said = True
            self.do("sleepy", 3600, now, force=True)
            return ("sleepy", "sleepy")
        if self.sleepy_said or now < self.next_act or self.current_react(now):
            return None
        self.next_act = now + random.uniform(*IDLE_ACT_GAP)
        if not acts:
            return None
        tot = sum(w for _, w, _ in acts)
        r = random.uniform(0, tot)
        for st, w, kind in acts:
            r -= w
            if r <= 0:
                return (st, kind)
        return (acts[-1][0], acts[-1][2])

    # ------------------------------------------------------------ playback
    def frame(self, state, nframes, seq):
        """推进播放：返回 (帧号, 毫秒)。seq = {'enter': [(i, ms)], 'loop': [(i, ms)]}"""
        if nframes <= 0:
            return 0, 150
        if state != self.state:
            self.state = state
            self.phase = "enter" if (seq and seq.get("enter")) else "loop"
            self.pos = 0
        if not seq:
            i = self.pos % nframes
            self.pos += 1
            return i, 150
        lst = seq.get(self.phase) or seq.get("loop") or []
        if not lst:
            return 0, 150
        if self.pos >= len(lst):
            self.phase, self.pos = "loop", 0
            lst = seq.get("loop") or lst
        i, ms = lst[self.pos % len(lst)]
        self.pos += 1
        return min(int(i), nframes - 1), int(ms)

    def restart(self):
        self.state = None
