# -*- coding: utf-8 -*-
"""下载后校验：用户填 md5/sha1/sha256/sha512（可带 'sha256:' 前缀，也可只贴哈希按长度识别）。"""
import hashlib
import re

_LEN = {32: "md5", 40: "sha1", 64: "sha256", 128: "sha512"}


def parse(spec):
    """'sha256:abcd…' / 'abcd…' → (algo, hexdigest)；无效返回 (None, None)。"""
    s = (spec or "").strip().lower().replace(" ", "")
    if not s:
        return None, None
    algo = None
    m = re.match(r"^(md5|sha1|sha-1|sha256|sha-256|sha512|sha-512)[:=]?(.*)$", s)
    if m:
        algo, s = m.group(1).replace("-", ""), m.group(2)
    if not re.fullmatch(r"[0-9a-f]+", s or ""):
        return None, None
    algo = algo or _LEN.get(len(s))
    if not algo or _LEN.get(len(s)) != algo:
        return None, None
    return algo, s


def file_hash(path, algo, cancel=None):
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        while True:
            b = f.read(1024 * 1024)
            if not b:
                break
            h.update(b)
            if cancel and cancel():
                return None
    return h.hexdigest()


def verify(path, spec):
    """返回 (ok, algo, actual)；spec 无效返回 (None, None, None)。"""
    algo, want = parse(spec)
    if not algo:
        return None, None, None
    got = file_hash(path, algo)
    return got == want, algo, got
