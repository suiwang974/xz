# -*- coding: utf-8 -*-
"""迅雷/快车/QQ旋风 专用链接解码（clean-room，公开的 base64 约定）。"""
import base64
import re
from urllib.parse import unquote


def _b64(s):
    s = unquote(s.strip()).rstrip("/")
    s += "=" * (-len(s) % 4)
    try:
        return base64.b64decode(s.replace("-", "+").replace("_", "/"))
    except Exception:
        return b""


def _text(raw):
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except Exception:
            pass
    return ""


def decode(url):
    """thunder:// flashget:// qqdl:// → 真实链接；不是专用链接/解不出则原样返回。"""
    m = re.match(r"^(thunder|flashget|qqdl)://(.+)$", (url or "").strip(), re.I)
    if not m:
        return url
    kind, body = m.group(1).lower(), m.group(2)
    t = _text(_b64(body.split("&")[0] if kind == "flashget" else body))
    if not t:
        return url
    if kind == "thunder" and t.startswith("AA") and t.endswith("ZZ"):
        t = t[2:-2]
    elif kind == "flashget":
        t = re.sub(r"^\[FLASHGET\]|\[FLASHGET\]$", "", t)
    return t if re.match(r"^(https?|ftp)://", t, re.I) else url
