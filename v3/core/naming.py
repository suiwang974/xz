# -*- coding: utf-8 -*-
"""自动分类（扩展名 / 站点）、文件名模板、重名策略。"""
import datetime
import os
import re
from urllib.parse import urlparse

_UNSAFE = re.compile(r'[\\/:*?"<>|\r\n\t\x00-\x1f]')


def parse_rules(text):
    """'文件夹=a,b,c' 每行一条 → [(文件夹, {a,b,c})]（保持顺序）。"""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        items = {x.strip().lower().lstrip(".") for x in re.split(r"[,，\s]+", v) if x.strip()}
        if k and items:
            out.append((k, items))
    return out


def parse_site_rules(text):
    """'bilibili.com=B站' 每行一条 → [(域名, 文件夹)]。"""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip().lower().lstrip("*.").strip(), v.strip()
        if k and v:
            out.append((k, v))
    return out


def host_of(url):
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def host_match(host, dom):
    host, dom = (host or "").lower(), (dom or "").lower()
    return bool(host) and (host == dom or host.endswith("." + dom))


def ext_of(name_or_url):
    s = (name_or_url or "").strip()
    path = urlparse(s).path if "://" in s else s.split("?")[0]
    base = os.path.basename(path)
    if "." not in base:
        return ""
    return base.rsplit(".", 1)[-1].lower()[:12]


def category_for(name, url, cat_rules_text, site_rules_text):
    """返回子文件夹名；都不命中返回 '其他'。站点规则优先。"""
    h = host_of(url)
    for dom, folder in parse_site_rules(site_rules_text):
        if host_match(h, dom):
            return folder
    e = ext_of(name) or ext_of(url)
    if e:
        for folder, exts in parse_rules(cat_rules_text):
            if e in exts:
                return folder
    return "其他"


def apply_template(tpl, name, url="", category="", now=None):
    """文件名模板：{name}=不含扩展名的原名 {ext}=扩展名 {date} {time} {host} {category}。
    模板里没写 {ext} 时自动补回原扩展名，绝不把文件改成没后缀。"""
    tpl = (tpl or "{name}").strip() or "{name}"
    if tpl == "{name}":
        return name
    now = now or datetime.datetime.now()
    stem, ext = os.path.splitext(name or "")
    ext = ext.lstrip(".")
    vals = {"name": stem, "ext": ext, "date": now.strftime("%Y%m%d"), "time": now.strftime("%H%M%S"),
            "host": host_of(url) or "local", "category": category or ""}
    try:
        out = tpl.format(**vals)
    except Exception:
        out = stem
    out = _UNSAFE.sub("_", out).strip(" .") or stem or "download"
    if ext and "{ext}" not in tpl:
        out += "." + ext
    return out[:200]


def unique_path(path):
    """重命名策略：a.zip → a (1).zip → a (2).zip …（同时避开 .part/.aria2 残留占位）。"""
    if not os.path.exists(path):
        return path
    d, base = os.path.split(path)
    stem, ext = os.path.splitext(base)
    for i in range(1, 10000):
        cand = os.path.join(d, "%s (%d)%s" % (stem, i, ext))
        if not os.path.exists(cand) and not os.path.exists(cand + ".part") and not os.path.exists(cand + ".aria2"):
            return cand
    return path
