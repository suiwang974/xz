# -*- coding: utf-8 -*-
"""桃桃的人设与台词库（日常 / 工作双形态）+ 本地短句指挥解析。

人设基调（写死在这里，后续助手沿用同一套方法）：
- 桃桃，奶油粉女仆，自称「咱」，称用户「主人」。软糯、黏人、爱被夸，但不笨。
- 自尊来源：鼻子灵、搬片稳、从不让主人等太久。被摸头会害羞，会嘴硬。
- 小缺点：怕被冷落、怕黑；主人夸别的下载器时会小吃醋（用「哼」掩饰）。
- 工作形态：戴上耳机，句子变短、语速变快，像在报数；收工后一秒软回来，反差萌。
- 语言：日常用「～ ♪ ♡ ☕ ✦」，工作形态少用语气词；不卖惨、不油腻、不卑微。
"""

import random
import time

# ---------------------------------------------------------------- 台词
DAILY = {
    "greet_morning": [
        "早安，主人 ♪ 咱把窗帘拉开了一点点～",
        "主人早上好！今天先搬哪一部呢？",
        "早呀～茶还烫，先想好要下的片子 ♡",
        "早安。咱已经把围裙系好了，随时开工。",
        "睡醒啦？咱等主人好久了呢～",
        "早上好，主人。今天的阳光很适合看片 ♪",
    ],
    "greet_noon": [
        "中午啦，主人吃饭了吗？咱可以边搬边等 ♪",
        "午安～要不要先歇一会儿，让咱来盯着？",
        "这个点最适合泡杯茶，看咱搬片 ♡",
        "主人先去吃饭吧，链接留下就好。",
        "中午好！咱刚把下载目录擦得亮亮的 ✦",
    ],
    "greet_afternoon": [
        "下午好，主人～犯困的话，咱讲个笑话？",
        "主人回来啦，咱一直守着窗口呢 ♪",
        "下午茶时间到——主人要红茶，还是要咱？♡",
        "这个点搬片刚刚好，网速也听话。",
        "下午好！咱把列表整理得整整齐齐的。",
    ],
    "greet_evening": [
        "晚上好，主人。今天辛苦了，剩下的交给咱 ♡",
        "夜色刚好，适合窝着看片～",
        "主人回来了！咱把灯调暖了一点 ♪",
        "晚饭吃了吗？没吃的话咱要生气了哦。",
        "晚上好～今晚想搬什么，咱都陪着。",
    ],
    "greet_night": [
        "这么晚还没睡呀…咱陪主人，但只陪一部哦。",
        "夜深了，主人记得把音量调小一点 ♪",
        "熬夜会长黑眼圈的…咱帮主人盯着，您先眯一会儿？",
        "夜里网速快，主人真会挑时间 ✦",
        "咱不困。主人什么时候睡，咱什么时候收工。",
        "小声一点哦，别把邻居吵醒了…",
    ],
    "greet": [
        "欢迎回来，主人～把网址交给咱吧 ♪",
        "主人辛苦啦，嗅探下载交给咱～",
    ],
    "idle": [
        "网址贴进来，咱就出发 ♪",
        "主人想看点什么？咱的鼻子很灵的。",
        "今天也把搬片的事交给咱吧 ♡",
        "咱把茶放在桌角了，记得喝。",
        "没事做的话…陪咱说说话也好呀。",
        "点一下右下角的咱，会有回应哦 ♪",
        "主人，咱今天也很能干的对吧？",
        "有链接就丢过来，咱接得住。",
        "咱在待命。围裙都系好了。",
        "要不要试试对咱说句话？比如「暂停」或「变身」。",
    ],
    "click": [
        "诶？戳咱的脸干嘛啦～",
        "主人手指凉凉的…",
        "被戳到会更有干劲哦 ♪",
        "再戳一下，咱就赖着不走了。",
        "哼，咱可是在工作呢…不过可以再戳一下。",
        "痒…主人是故意的吧？",
        "戳这里会掉出「喜欢」的哦 ♡",
        "主人今天心情不错嘛。",
        "咱的脸不是按钮啦…虽然也不是不行。",
        "好啦好啦，咱看着你呢。",
    ],
    "hug": [
        "哇…突然抱过来！",
        "好啦好啦，咱在呢，一直在。",
        "主人的心跳，咱听到了哦…",
        "抱这么紧，咱可要当真了 ♡",
        "今天也辛苦了，抱一会儿吧。",
        "唔…围裙会被弄皱的…不过算了。",
        "再抱一下，咱就把今天的疲惫都收走。",
        "主人身上有茶香。",
    ],
    "clip": [
        "剪贴板里有链接！要咱去嗅一嗅吗？",
        "主人刚复制了一个网址吧？咱看到了 ♪",
        "有活上门！点咱左边的小按钮就出发。",
        "这个链接…咱闻着像有视频 ♡",
        "已备好，主人一句话咱就跑。",
        "剪贴板接活，咱最拿手了。",
    ],
    "transform_in": [
        "围裙系紧——工作模式，启动。",
        "耳机戴上。主人，交给咱。",
        "变身！…咳，是「切换工作形态」。",
        "日常模式收起来，干活了。",
        "咱要认真了，主人退后一点 ♪",
        "工作形态，上线。",
    ],
    "transform_out": [
        "呼…下班啦。主人，咱变回来了 ♡",
        "耳机摘掉，咱又是主人的软软女仆了。",
        "收工收工～要摸摸头奖励吗？",
        "切回日常。肩膀好酸…主人帮咱捏捏？",
        "工作模式，退场。接下来是撒娇时间 ♪",
        "搬完了，咱回来陪主人啦。",
    ],
    "busy": [
        "咱去看看这页藏了什么 ♪",
        "鼻子凑近一点…嗯，有味道。",
        "稍等，咱把页面翻个底朝天。",
        "别急别急，咱正在找呢～",
    ],
    "prog": [
        "已经搬了一半啦，主人先喝口茶～",
        "这个有点大，咱多跑两趟。",
        "进度在走，放心交给咱 ♪",
    ],
    "found": [
        "找到 {n} 个！主人要全搬吗？",
        "一共 {n} 条，咱的鼻子没白长 ♡",
        "{n} 个视频，藏得再深也被咱翻出来了。",
        "报数：{n} 个目标。",
    ],
    "done": [
        "全部搬完啦，请主人查收～",
        "交差！要不要再来一轮？",
        "都搬回来了，咱厉害吧 ♡",
        "主人，搬完啦。能夸咱一句吗？",
    ],
    "fail": [
        "有 {n} 个没搬动…让咱再试一次好不好？",
        "呜…{n} 个失败了。主人别皱眉，咱去查。",
        "这几个站点有点狡猾，咱记下来了。",
    ],
    "pause": [
        "好嘞，先歇口气，随时喊咱继续。",
        "暂停中…咱在旁边待命～",
    ],
    "cmd_hello": [
        "在呢在呢，主人叫咱？♡",
        "咱一直在，随时听吩咐 ♪",
        "嗯？主人想咱了？",
    ],
    "cmd_unknown": [
        "咱只听懂这些：暂停 / 继续 / 重试 / 打开文件夹 / 变身 / 嗅探。",
        "唔…这句咱没听懂。主人换种说法？比如「重试」。",
        "咱的小脑袋转不过来了…说「暂停」或「变身」试试？",
    ],
    "cmd_retry": [
        "好，咱把失败的再拎出来试一遍。",
        "收到，失败的那些咱再搬一次。",
    ],
    "cmd_open": [
        "文件夹给您打开啦，慢慢挑 ♪",
        "喏，成品都在这个文件夹里 ♡",
    ],
    "cmd_probe": [
        "收到，咱去嗅一嗅。",
        "这就去！鼻子已经贴到屏幕上了 ♪",
    ],
    "cmd_download": [
        "好，开搬！",
        "收到，咱这就动手。",
    ],
    "cmd_quality": [
        "画质换成 {q}p 了，下次搬片生效。",
        "好，切到 {q}p。主人眼睛真挑 ♪",
    ],
    "cmd_clear": [
        "清空这种大事，主人还是亲手点表格右键吧，咱不敢乱动。",
    ],
}

WORK = {
    "greet": [
        "工作形态待机中。主人还有活吗？",
        "耳机还戴着…要咱下班吗？",
    ],
    "busy": [
        "收到，开工。",
        "三路并行，主人等我报数。",
        "耳机戴好，闲人免进～",
        "正在搬。别急，咱盯着呢。",
        "链接已锁定，开始拆包。",
        "这个站点有点硬，咱啃一啃。",
        "速度还行，主人放心。",
        "报数：正在搬，暂无异常。",
    ],
    "prog": [
        "过半了，稳住。",
        "还剩一段，咱不松手。",
        "进度在走，主人不用盯。",
        "这个文件有点大，咱多跑两趟。",
        "分片都接上了，继续。",
    ],
    "done": [
        "收工。全部入库。",
        "清点完毕，零失误。",
        "搬完了。主人验收吧。",
        "任务结束，咱把耳机摘了。",
    ],
    "fail": [
        "报告：{n} 个没拿下，咱盯着呢。",
        "有 {n} 个没搬动…咱再试一次。",
    ],
    "pause": [
        "暂停。进度都锁好了。",
        "停。随时可以续。",
        "好，歇口气。咱守着进度。",
    ],
    "click": [
        "别闹，咱在搬片…不过主人可以摸头。",
        "主人，工作形态也是会害羞的。",
        "报数中断…都怪主人。",
    ],
    "hug": [
        "抱一下就好…咱还得回去盯着。",
        "等这批搬完，咱再好好抱。",
        "主人的抱抱，算加班补贴 ♡",
    ],
}

RESUME = [
    "恢复。接着搬。",
    "继续～断点都还在 ♪",
    "好，咱回来了。进度没丢。",
]

KINDS = {}
for _k, _v in DAILY.items():
    KINDS.setdefault(_k, {})["daily"] = list(_v)
for _k, _v in WORK.items():
    KINDS.setdefault(_k, {})["work"] = list(_v)
KINDS["resume"] = {"any": list(RESUME)}
for _k in ("greet_morning", "greet_noon", "greet_afternoon", "greet_evening", "greet_night"):
    KINDS[_k]["any"] = list(DAILY[_k])

_RECENT = {}


def _bucket():
    h = time.localtime().tm_hour
    if 5 <= h < 11:
        return "greet_morning"
    if 11 <= h < 14:
        return "greet_noon"
    if 14 <= h < 18:
        return "greet_afternoon"
    if 18 <= h < 23:
        return "greet_evening"
    return "greet_night"


def _fmt(txt, n=None, q=None):
    try:
        return txt.format(n=n, q=q) if ("{" in txt) else txt
    except Exception:
        return txt


def pick(kind, form="daily", n=None, q=None):
    """按形态取一句台词；同一种类最近 3 句不重复。返回 None 表示没有该种类。"""
    if kind == "greet":
        kind = _bucket()
    slot = KINDS.get(kind)
    if not slot:
        return None
    pool = slot.get(form) or slot.get("daily") or slot.get("any") or []
    if not pool:
        return None
    key = (kind, form)
    recent = _RECENT.setdefault(key, [])
    cand = [t for t in pool if t not in recent]
    if not cand:
        recent.clear()
        cand = list(pool)
    txt = random.choice(cand)
    recent.append(txt)
    if len(recent) > 3:
        del recent[0]
    return _fmt(txt, n=n, q=q)


def merge_into(themes):
    """把桃桃的新台词并进 maid 主题（旧台词保留，只做去重追加）。"""
    th = (themes or {}).get("maid")
    if not th:
        return
    lines = th.setdefault("LINES", {})
    for kind, slot in KINDS.items():
        add = list(slot.get("daily") or []) + list(slot.get("work") or []) + list(slot.get("any") or [])
        base = list(lines.get(kind) or [])
        seen = set(base)
        for t in add:
            if t not in seen:
                seen.add(t)
                base.append(t)
        lines[kind] = base


# ---------------------------------------------------------------- 短句指挥
def parse_command(text):
    """本地解析（不联网、不花积分）。返回 (action, arg)。"""
    s = (text or "").strip().lower()
    s = s.replace("，", " ").replace("。", " ").replace("！", " ").replace("？", " ")
    s = s.replace(",", " ").replace(".", " ").replace("!", " ").replace("?", " ")
    if not s:
        return ("empty", "")
    # 否定优先：不要停 / 别停 → 继续
    if ("不要停" in s) or ("别停" in s) or ("甭停" in s):
        return ("resume", "")
    if any(k in s for k in ("暂停", "停一下", "先停", "歇一下", "pause", "stop")):
        return ("pause", "")
    if any(k in s for k in ("继续", "接着", "恢复", "resume", "开跑")):
        return ("resume", "")
    if any(k in s for k in ("重试", "再试", "retry")):
        return ("retry", "")
    if any(k in s for k in ("打开文件夹", "开文件夹", "打开目录", "开目录", "文件夹", "open")):
        return ("open_dir", "")
    if any(k in s for k in ("下班", "日常形态", "日常模式", "变回来", "变回日常", "回家")):
        return ("transform", "daily")
    if any(k in s for k in ("变身", "工作模式", "工作形态", "上班", "干活", "work")):
        return ("transform", "work")
    if any(k in s for k in ("嗅探", "嗅一下", "嗅一嗅", "分析", "解析", "probe")):
        return ("probe", "")
    if any(k in s for k in ("全部下载", "全下", "都下", "download all")):
        return ("download_all", "")
    if any(k in s for k in ("下载选中", "下选中", "download")):
        return ("download_sel", "")
    if any(k in s for k in ("下载", "开始", "开工", "搬吧", "下吧")):
        return ("download_all", "")
    if "720" in s or "七二零" in s:
        return ("quality", "720p")
    if "1080" in s or "一零八零" in s:
        return ("quality", "1080p")
    if "最佳" in s or "原画" in s or "最高" in s:
        return ("quality", "最佳")
    if any(k in s for k in ("清空", "清除", "清理")):
        return ("clear", "")
    if any(k in s for k in ("状态", "进度", "怎么样", "好了没", "在干嘛")):
        return ("status", "")
    if any(k in s for k in ("在吗", "桃桃", "你好", "嗨", "hi", "hello")):
        return ("hello", "")
    return ("unknown", "")
