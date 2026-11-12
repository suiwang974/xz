# -*- coding: utf-8 -*-
"""一个框识别一切：用户粘贴/拖入的任何东西 → 结构化识别结果。

kind:
  magnet    磁力（含只粘贴 40 位 hex / 32 位 base32 infohash，自动补成磁力）
  torrent   .torrent（本地路径或网址）
  m3u8      HLS 播放列表（直播/点播）
  video     视频站页面（yt-dlp 有专门解析器的站：B站/YouTube/抖音…）
  netdisk   网盘下载直链（带防封规则、过期预判）
  direct    普通文件直链（有扩展名或服务器说是附件）
  page      普通网页（交给嗅探找视频）
  batch     多条链接 / 区间模式（file[001-100].jpg）
"""
import os
import re
import threading
from urllib.parse import urlparse, unquote

from . import batch as B

_HEX40 = re.compile(r"^[0-9a-fA-F]{40}$")
_B32 = re.compile(r"^[A-Z2-7]{32}$")
_MEDIA_PAGE_HINT = re.compile(r"(video|watch|play|/v/|bangumi|episode|vod|live)", re.I)
_FILE_EXT = re.compile(r"\.([a-z0-9]{1,8})$", re.I)
_DIRECT_EXTS = set((
    "zip rar 7z tar gz tgz bz2 xz iso img bin exe msi msp apk ipa deb rpm dmg pkg appimage jar whl "
    "pdf epub mobi azw3 doc docx xls xlsx ppt pptx txt csv json "
    "mp4 mkv mov avi webm flv wmv m4v ts mpg mpeg 3gp mp3 flac wav m4a aac ogg opus wma "
    "png jpg jpeg gif webp bmp svg heic psd ai gguf safetensors ckpt pt onnx torrent").split())

_ydl_lock = threading.Lock()
_ydl_ies = None


def _extractors():
    global _ydl_ies
    with _ydl_lock:
        if _ydl_ies is None:
            try:
                from yt_dlp.extractor import gen_extractor_classes
                _ydl_ies = [ie for ie in gen_extractor_classes()
                            if ie.ie_key() not in ("Generic",) and getattr(ie, "_VALID_URL", None)]
            except Exception:
                _ydl_ies = []
    return _ydl_ies


def video_site(url):
    """yt-dlp 是否有专门解析器（返回解析器名）。"""
    try:
        for ie in _extractors():
            try:
                if ie.suitable(url):
                    return ie.IE_NAME
            except Exception:
                continue
    except Exception:
        pass
    return ""


def hash_to_magnet(s):
    s = s.strip()
    if _HEX40.match(s) or _B32.match(s.upper()):
        return "magnet:?xt=urn:btih:" + (s.lower() if _HEX40.match(s) else s.upper())
    return ""


def url_ext(url):
    try:
        p = unquote(urlparse(url).path)
    except Exception:
        return ""
    m = _FILE_EXT.search(os.path.basename(p))
    return m.group(1).lower() if m else ""


def classify_one(s, nd=None):
    """单条（不联网）。nd = netdisk_rules 模块（可选）。"""
    s = (s or "").strip().strip('"').strip("'")
    if not s:
        return {"kind": "empty", "input": s}
    mg = hash_to_magnet(s)
    if mg:
        return {"kind": "magnet", "url": mg, "input": s, "from_hash": True}
    low = s.lower()
    if low.startswith("magnet:?"):
        return {"kind": "magnet", "url": s, "input": s}
    if (low.endswith(".torrent") and os.path.isfile(s)) or (os.path.isfile(s) and low.endswith(".torrent")):
        return {"kind": "torrent", "url": s, "input": s, "local": True}
    if os.path.isabs(s) and os.path.isfile(s):
        return {"kind": "unsupported", "input": s, "reason": "只支持 .torrent 本地文件"}
    if not re.match(r"^(https?|ftp)://", s, re.I):
        if re.match(r"^[\w.-]+\.[a-z]{2,}(/|$)", s, re.I):
            s = "https://" + s
            low = s.lower()
        else:
            return {"kind": "unsupported", "input": s, "reason": "不是链接"}
    ext = url_ext(s)
    if ext == "torrent":
        return {"kind": "torrent", "url": s, "input": s}
    if ext in ("m3u8",) or ".m3u8?" in low:
        return {"kind": "m3u8", "url": s, "input": s}
    prof = None
    if nd is not None:
        try:
            prof = nd.match_profile(s)
        except Exception:
            prof = None
    if prof:
        out = {"kind": "netdisk", "url": s, "input": s, "netdisk": prof.get("name") or ""}
        try:
            ex = nd.expiry_state(s)
            out["expired"] = bool(ex.get("expired"))
            out["expire_ts"] = ex.get("expire_ts") or 0
        except Exception:
            pass
        return out
    if ext in _DIRECT_EXTS and ext not in ("mp4", "webm", "flv") or (ext in ("mp4", "webm", "flv", "mp3", "m4a")):
        return {"kind": "direct", "url": s, "input": s, "ext": ext}
    ie = video_site(s)
    if ie:
        return {"kind": "video", "url": s, "input": s, "site": ie}
    if ext and ext not in ("html", "htm", "php", "asp", "aspx", "jsp", "shtml"):
        return {"kind": "direct", "url": s, "input": s, "ext": ext}
    return {"kind": "page", "url": s, "input": s}


def classify(text, nd=None):
    """整段输入 → {"kind": 单条 kind 或 batch, "items": [...]}。"""
    text = (text or "").strip()
    if not text:
        return {"kind": "empty", "items": []}
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) == 1 and not B.has_pattern(lines[0]):
        one = classify_one(lines[0], nd)
        if one["kind"] == "unsupported":
            urls = B.extract_urls(lines[0])
            if len(urls) == 1:
                one = classify_one(urls[0], nd)
            elif len(urls) > 1:
                return {"kind": "batch", "items": [classify_one(u, nd) for u in urls[:5000]]}
        return {"kind": one["kind"], "items": [one]}
    raw = []
    for ln in lines:
        if hash_to_magnet(ln) or ln.lower().startswith("magnet:?") or os.path.isfile(ln):
            raw.append(ln)
    urls, trunc = B.expand_text(text)
    for u in urls:
        if u not in raw:
            raw.append(u)
    items = [classify_one(u, nd) for u in raw]
    items = [i for i in items if i["kind"] not in ("empty", "unsupported")]
    if len(items) == 1:
        return {"kind": items[0]["kind"], "items": items}
    return {"kind": "batch", "items": items, "truncated": trunc}


def probe_http(url, headers=None, timeout=8, session=None, tls_check=True):
    """联网探测直链：在线？大小？文件名？能否续传？证书是否正常？
    先 HEAD，不行再 GET Range: bytes=0-0。返回 dict（失败也返回，带 online=False 与原因）。"""
    import requests
    s = session or requests
    h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/125.0 Safari/537.36", "Accept": "*/*"}
    h.update(headers or {})
    out = {"online": False, "size": 0, "name": "", "resumable": None, "mime": "", "status": 0,
           "cert_error": "", "final_url": url}

    def _req(verify):
        r = s.head(url, headers=h, timeout=timeout, allow_redirects=True, verify=verify)
        if r.status_code in (403, 405, 400, 501) or not r.headers.get("content-length"):
            r.close()
            h2 = dict(h, Range="bytes=0-0")
            r = s.get(url, headers=h2, timeout=timeout, allow_redirects=True, stream=True, verify=verify)
        return r
    try:
        try:
            r = _req(True if tls_check else False)
        except requests.exceptions.SSLError as e:
            out["cert_error"] = "HTTPS 证书异常：%s" % (str(e).split(":")[-1].strip()[:120] or "校验失败")
            r = _req(False)
        out["status"] = r.status_code
        out["final_url"] = r.url
        out["online"] = 200 <= r.status_code < 400
        cr = r.headers.get("content-range") or ""
        if r.status_code == 206 and "/" in cr:
            tot = cr.rsplit("/", 1)[-1]
            out["size"] = int(tot) if tot.isdigit() else 0
            out["resumable"] = True
        else:
            try:
                out["size"] = int((r.headers.get("content-length") or "0").split(",")[0])
            except Exception:
                out["size"] = 0
            ar = (r.headers.get("accept-ranges") or "").lower()
            out["resumable"] = True if ar == "bytes" else (False if r.status_code == 200 and ar in ("", "none") else None)
        out["mime"] = (r.headers.get("content-type") or "").split(";")[0].strip()
        cd = r.headers.get("content-disposition") or ""
        m = re.search(r"filename\*=(?:UTF-8|utf-8)''([^;]+)", cd) or re.search(r'filename="?([^";]+)"?', cd)
        if m:
            out["name"] = unquote(m.group(1)).strip()
        if not out["name"]:
            out["name"] = unquote(os.path.basename(urlparse(r.url).path)) or ""
        r.close()
    except Exception as e:
        out["error"] = str(e)[:160]
    return out
