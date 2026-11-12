# -*- coding: utf-8 -*-
"""批量链接展开（IDM「批量下载」/ FDM 同款）。

  http://x/img[001-120].jpg    → 001…120（保留位数）
  http://x/p[1-9:2].png        → 步长 2
  http://x/[a-f].zip           → a…f
  多行文本 / 空格分隔          → 逐条
展开上限 5000 条，防误写 [1-99999999] 卡死。
"""
import re

MAX_ITEMS = 5000
_RANGE = re.compile(r"\[(\d+)-(\d+)(?::(\d+))?\]|\[([a-zA-Z])-([a-zA-Z])\]")
_URL_RE = re.compile(r"(?:https?|ftp)://[^\s\"'<>]+|magnet:\?[^\s\"'<>]+", re.I)


def has_pattern(url):
    return bool(_RANGE.search(url or ""))


def expand_one(url, limit=MAX_ITEMS):
    m = _RANGE.search(url)
    if not m:
        return [url]
    pre, post = url[:m.start()], url[m.end():]
    vals = []
    if m.group(1) is not None:
        a, b = m.group(1), m.group(2)
        step = max(1, int(m.group(3) or 1))
        width = len(a) if (a.startswith("0") and len(a) > 1) or len(a) == len(b) else 0
        lo, hi = int(a), int(b)
        rng = range(lo, hi + 1, step) if lo <= hi else range(lo, hi - 1, -step)
        for i in rng:
            vals.append(str(i).zfill(width) if width else str(i))
            if len(vals) > limit:
                break
    else:
        a, b = ord(m.group(4)), ord(m.group(5))
        rng = range(a, b + 1) if a <= b else range(a, b - 1, -1)
        vals = [chr(c) for c in rng]
    out = []
    for v in vals:
        for u in expand_one(pre + v + post, limit):
            out.append(u)
            if len(out) >= limit:
                return out
    return out


def extract_urls(text):
    from . import linkdecode as LD
    out = [u.rstrip(").,;，。、") for u in _URL_RE.findall(text or "")]
    for m in re.findall(r"(?:thunder|flashget|qqdl)://[A-Za-z0-9+/=_%-]+", text or "", re.I):
        d = LD.decode(m)
        if d != m:
            out.append(d)
    return out


def expand_text(text, limit=MAX_ITEMS):
    """整段文本 → 去重后的链接列表（含区间展开）。返回 (urls, truncated)。"""
    seen, out = set(), []
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    cands = []
    for ln in lines:
        found = extract_urls(ln)
        cands.extend(found if found else ([ln] if has_pattern(ln) else []))
    truncated = False
    for c in cands:
        for u in expand_one(c, limit):
            if u not in seen:
                seen.add(u)
                out.append(u)
            if len(out) >= limit:
                truncated = True
                return out, truncated
    return out, truncated
