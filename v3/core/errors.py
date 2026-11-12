# -*- coding: utf-8 -*-
"""友好错误提示（AB DM 同款）：原始报错 → {title 人话标题, hint 怎么办, action 一键处理}。

action 取值（界面按它出按钮）：
  refresh  刷新下载地址（链接过期/403/签名失效）     retry   重试
  space    换保存位置 / 清理磁盘                     proxy   去设置代理
  login    去登录（缺 Cookie）                        none    没有可一键处理的
"""
import re

ARIA2_CODES = {
    "3": ("资源不存在", "服务器上找不到这个文件（404）", "refresh"),
    "9": ("磁盘空间不足", "保存盘满了，任务已暂停，腾出空间后点继续即可（进度保留）", "space"),
    "13": ("文件已存在", "同名文件已在，按「重名时」设置处理", "none"),
    "16": ("无法写入文件", "保存目录没有写权限或被占用，换个保存位置", "space"),
    "19": ("域名解析失败", "检查网络 / 代理后重试", "proxy"),
    "22": ("服务器拒绝", "HTTP 响应异常，多半是链接过期或需要登录", "refresh"),
    "24": ("需要登录授权", "服务器要求身份认证", "login"),
    "6": ("网络中断", "网络问题，稍后会自动重试", "retry"),
    "2": ("连接超时", "网络太慢或被限速", "retry"),
}

_RULES = [
    # 带明确 HTTP 404/410 的先判：引擎报错常写成「文件不存在或链接已失效（HTTP 404）」，不能被下面的「失效」吞成「链接已过期」
    (r"\b404\b|\b410\b|not found|不存在", "文件不存在", "地址已失效，点「刷新地址」换新链接", "refresh"),
    (r"过期|expired|签名失效|auth_key|已失效", "链接已过期", "回网页重新获取链接，或点「刷新地址」粘贴新链接，进度不丢", "refresh"),
    (r"缺.{0,4}cookie|登录|412|unauthori[sz]ed|401", "需要登录 Cookie", "在浏览器里登录后再推送，或在设置「嗅探与登录」里登录", "login"),
    (r"\b403\b|forbidden|denied|被拒", "服务器拒绝下载", "链接可能过期或被防盗链，点「刷新地址」换新链接", "refresh"),
    (r"no space|enospc|磁盘空间|disk full|not enough space", "磁盘空间不足", "腾出空间后点继续（进度保留）或换保存位置", "space"),
    (r"certificate|ssl|证书", "HTTPS 证书异常", "网站证书有问题：确认来源可信再继续，或检查系统时间/代理", "proxy"),
    (r"getaddrinfo|name resolution|dns|解析失败", "域名解析失败", "检查网络与代理设置后重试", "proxy"),
    (r"timed? ?out|超时|connection reset|stall|没有任何数据", "连接超时", "网络慢或被限速，已自动重试；仍失败可换时段或开代理", "retry"),
    (r"\b429\b|too many|限流", "被限流", "访问太频繁，歇几分钟再试", "retry"),
    (r"校验失败|checksum", "校验不一致", "下载内容与你填的校验值不同，文件可能损坏或被篡改，建议重下", "retry"),
    (r"ffmpeg|merge|合并", "合并失败", "视频音频合并出错，确认 ffmpeg 可用后重试", "retry"),
    (r"流量熔断", "流量熔断", "本次运行下载量超过上限，去设置调大「单次运行流量上限」", "none"),
]


def friendly(msg, code=None):
    s = str(msg or "")
    m = re.search(r"code=(\d+)", s)
    c = str(code or (m.group(1) if m else "") or "")
    for pat, title, hint, act in _RULES:
        if re.search(pat, s, re.I):
            return {"title": title, "hint": hint, "action": act, "raw": s[:300]}
    if c in ARIA2_CODES:
        t, h, a = ARIA2_CODES[c]
        return {"title": t, "hint": h, "action": a, "raw": s[:300]}
    return {"title": "下载失败", "hint": (s[:120] or "未知原因") + "。可点重试；反复失败多半是链接过期", "action": "retry",
            "raw": s[:300]}


def retryable(msg):
    """哪些失败值得自动重试（404/过期/磁盘满/校验失败/熔断 = 重试也白费）。"""
    f = friendly(msg)
    return f["action"] == "retry" and f["title"] not in ("校验不一致",)
