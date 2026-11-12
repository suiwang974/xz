# -*- coding: utf-8 -*-
"""最小 bencode 解码 + .torrent 文件清单（选文件下载用）。"""
import hashlib
import os


def _decode(b, i):
    c = b[i:i + 1]
    if c == b"i":
        j = b.index(b"e", i)
        return int(b[i + 1:j]), j + 1
    if c == b"l":
        i += 1
        out = []
        while b[i:i + 1] != b"e":
            v, i = _decode(b, i)
            out.append(v)
        return out, i + 1
    if c == b"d":
        i += 1
        out = {}
        while b[i:i + 1] != b"e":
            k, i = _decode(b, i)
            v, i = _decode(b, i)
            out[k] = v
        return out, i + 1
    if c.isdigit():
        j = b.index(b":", i)
        n = int(b[i:j])
        return b[j + 1:j + 1 + n], j + 1 + n
    raise ValueError("bad bencode at %d" % i)


def decode(b):
    v, _ = _decode(b, 0)
    return v


def _info_span(b):
    """找 info 字典原始字节区间（算 infohash 必须用原字节）。"""
    k = b.find(b"4:info")
    if k < 0:
        return None
    start = k + 6
    _, end = _decode(b, start)
    return start, end


def _s(x):
    if isinstance(x, bytes):
        try:
            return x.decode("utf-8")
        except Exception:
            return x.decode("gbk", "replace")
    return str(x)


def torrent_info(data):
    """返回 {name, infohash, total, files:[{index(1 起), path, length}], trackers, comment}。"""
    d = decode(data)
    info = d.get(b"info") or {}
    name = _s(info.get(b"name.utf-8") or info.get(b"name") or b"")
    files = []
    if b"files" in info:
        for i, f in enumerate(info[b"files"], 1):
            parts = f.get(b"path.utf-8") or f.get(b"path") or []
            files.append({"index": i, "path": "/".join(_s(p) for p in parts),
                          "length": int(f.get(b"length") or 0)})
    else:
        files.append({"index": 1, "path": name, "length": int(info.get(b"length") or 0)})
    span = _info_span(data)
    ih = hashlib.sha1(data[span[0]:span[1]]).hexdigest() if span else ""
    trackers = []
    if d.get(b"announce"):
        trackers.append(_s(d[b"announce"]))
    for tier in d.get(b"announce-list") or []:
        for t in tier:
            t = _s(t)
            if t not in trackers:
                trackers.append(t)
    return {"name": name, "infohash": ih, "total": sum(f["length"] for f in files),
            "files": files, "trackers": trackers, "comment": _s(d.get(b"comment") or b""),
            "piece_length": int(info.get(b"piece length") or 0)}


def torrent_info_file(path):
    with open(path, "rb") as f:
        return torrent_info(f.read())


def magnet_info(uri):
    from urllib.parse import urlparse, parse_qs, unquote
    q = parse_qs(urlparse(uri).query)
    xt = (q.get("xt") or [""])[0]
    ih = xt.split(":")[-1] if xt.lower().startswith("urn:btih:") else ""
    return {"name": unquote((q.get("dn") or [""])[0]), "infohash": ih.lower(),
            "trackers": q.get("tr") or []}
