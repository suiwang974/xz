# -*- coding: utf-8 -*-
"""点播 HLS(m3u8) / DASH(mpd) 分段并发下载 + 合并（A17 第一段，自研、不依赖外挂程序）。

思路参考通用流媒体下载器的公开做法（RFC 8216 / ISO 23009-1 标准文档），代码自写：
  · HLS：主列表挑码率（best / 不高于某高度 / 指定序号）、相对地址、#EXT-X-KEY AES-128(CBC，IV 缺省取序号)、
         #EXT-X-MAP 初始化段、#EXT-X-BYTERANGE、独立音轨(#EXT-X-MEDIA TYPE=AUDIO)
  · DASH：静态 MPD，SegmentTemplate($Number$/$Time$/$RepresentationID$/$Bandwidth$、SegmentTimeline)、
          SegmentList、单文件 BaseURL；视频/音频各挑最高码率
  · 带头：Cookie / Referer / UA 等请求头随每个分片、密钥请求一起发
  · 并发：N 线程拉分片，落 <输出>.parts/NNNNN.seg（先 .tmp 后改名，中断后重跑自动续上），按序合并
  · 不做：SAMPLE-AES、DRM(Widevine/PlayReady/FairPlay)、直播（直播走 livehls.py）。遇到这些抛 Unsupported，
          由调用方回落到 yt-dlp。
AES-128 解密优先用 cryptography / pycryptodome，都没有时用内置纯 Python 实现（慢但正确）。
"""
import math
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET


class Unsupported(RuntimeError):
    """本模块不处理（加密方式不支持 / 直播 / DRM）：调用方应回落到别的下载方式。"""


class Cancelled(RuntimeError):
    pass


# ---------------------------------------------------------------- AES-128-CBC（纯 Python 兜底）
_SBOX = None
_INV = None


def _init_tables():
    global _SBOX, _INV
    if _SBOX:
        return
    p = q = 1
    sbox = [0] * 256
    while True:
        p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        x = q ^ ((q << 1) | (q >> 7)) & 0xFF ^ ((q << 2) | (q >> 6)) & 0xFF ^ ((q << 3) | (q >> 5)) & 0xFF \
            ^ ((q << 4) | (q >> 4)) & 0xFF
        sbox[p] = (x ^ 0x63) & 0xFF
        if p == 1:
            break
    sbox[0] = 0x63
    inv = [0] * 256
    for i, v in enumerate(sbox):
        inv[v] = i
    _SBOX, _INV = sbox, inv


def _xt(a):
    a <<= 1
    return (a ^ 0x11B) & 0xFF if a & 0x100 else a


def _mul(a, b):
    r = 0
    while b:
        if b & 1:
            r ^= a
        a = _xt(a)
        b >>= 1
    return r


def _expand_key(key):
    _init_tables()
    w = [list(key[i:i + 4]) for i in range(0, 16, 4)]
    rcon = 1
    for i in range(4, 44):
        t = list(w[i - 1])
        if i % 4 == 0:
            t = t[1:] + t[:1]
            t = [_SBOX[b] for b in t]
            t[0] ^= rcon
            rcon = _xt(rcon)
        w.append([w[i - 4][j] ^ t[j] for j in range(4)])
    return [sum((w[r * 4 + c] for c in range(4)), []) for r in range(11)]


_M9 = _M11 = _M13 = _M14 = None


def _dec_block(blk, rk):
    global _M9, _M11, _M13, _M14
    if _M9 is None:
        _M9 = [_mul(i, 9) for i in range(256)]
        _M11 = [_mul(i, 11) for i in range(256)]
        _M13 = [_mul(i, 13) for i in range(256)]
        _M14 = [_mul(i, 14) for i in range(256)]
    s = [blk[i] ^ rk[10][i] for i in range(16)]
    for rnd in range(9, -1, -1):
        # InvShiftRows（列主序：s[4*c+r]）
        t = [0] * 16
        for c in range(4):
            for r in range(4):
                t[4 * ((c + r) % 4) + r] = s[4 * c + r]
        s = [_INV[b] for b in t]
        s = [s[i] ^ rk[rnd][i] for i in range(16)]
        if rnd:
            o = [0] * 16
            for c in range(4):
                a0, a1, a2, a3 = s[4 * c:4 * c + 4]
                o[4 * c] = _M14[a0] ^ _M11[a1] ^ _M13[a2] ^ _M9[a3]
                o[4 * c + 1] = _M9[a0] ^ _M14[a1] ^ _M11[a2] ^ _M13[a3]
                o[4 * c + 2] = _M13[a0] ^ _M9[a1] ^ _M14[a2] ^ _M11[a3]
                o[4 * c + 3] = _M11[a0] ^ _M13[a1] ^ _M9[a2] ^ _M14[a3]
            s = o
    return bytes(s)


def _aes_cbc_py(key, iv, data):
    rk = _expand_key(key)
    out = bytearray()
    prev = iv
    for i in range(0, len(data) - len(data) % 16, 16):
        blk = data[i:i + 16]
        d = _dec_block(blk, rk)
        out += bytes(a ^ b for a, b in zip(d, prev))
        prev = blk
    return bytes(out)


def aes128_cbc_decrypt(key, iv, data, unpad=True):
    """AES-128-CBC 解密。unpad=True 去 PKCS7 填充（HLS 分片标准做法）。"""
    if len(key) != 16 or len(iv) != 16:
        raise ValueError("AES-128 需要 16 字节密钥和 IV")
    if len(data) % 16:
        data = data[:len(data) - len(data) % 16]
    plain = None
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        d = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        plain = d.update(data) + d.finalize()
    except ImportError:
        pass
    if plain is None:
        for mod in ("Crypto.Cipher.AES", "Cryptodome.Cipher.AES"):
            try:
                AES = __import__(mod, fromlist=["x"])
                plain = AES.new(key, AES.MODE_CBC, iv).decrypt(data)
                break
            except ImportError:
                continue
    if plain is None and len(data) > 65536 and shutil.which("openssl"):
        try:        # 大分片：有 openssl 命令行就用它（比纯 Python 快两个数量级）
            r = subprocess.run(["openssl", "enc", "-d", "-aes-128-cbc", "-nopad", "-K", key.hex(), "-iv", iv.hex()],
                               input=data, capture_output=True, timeout=60)
            if r.returncode == 0 and len(r.stdout) == len(data):
                plain = r.stdout
        except Exception:
            plain = None
    if plain is None:
        plain = _aes_cbc_py(key, iv, data)
    if unpad and plain:
        n = plain[-1]
        if 1 <= n <= 16 and plain.endswith(bytes([n]) * n):
            plain = plain[:-n]
    return plain


# ---------------------------------------------------------------- HLS 解析
def _attrs(s):
    out = {}
    for m in re.finditer(r'([A-Z0-9-]+)=("([^"]*)"|[^,]*)', s):
        out[m.group(1)] = m.group(3) if m.group(3) is not None else m.group(2)
    return out


def is_master(text):
    return "#EXT-X-STREAM-INF" in text


def parse_master(text, base):
    """主列表 → {"variants":[{bw,res,height,url,audio,codecs}], "audios":{组:[{uri,name,default}]}}"""
    lines = [l.strip() for l in text.splitlines()]
    variants, audios = [], {}
    for i, ln in enumerate(lines):
        if ln.startswith("#EXT-X-STREAM-INF:"):
            a = _attrs(ln.split(":", 1)[1])
            uri = ""
            for nxt in lines[i + 1:]:
                if nxt and not nxt.startswith("#"):
                    uri = nxt
                    break
            if not uri:
                continue
            res = a.get("RESOLUTION", "")
            h = 0
            m = re.match(r"(\d+)x(\d+)", res)
            if m:
                h = int(m.group(2))
            try:
                bw = int(a.get("AVERAGE-BANDWIDTH") or a.get("BANDWIDTH") or 0)
            except ValueError:
                bw = 0
            variants.append({"bw": bw, "res": res, "height": h, "url": urllib.parse.urljoin(base, uri),
                             "audio": a.get("AUDIO", ""), "codecs": a.get("CODECS", "")})
        elif ln.startswith("#EXT-X-MEDIA:"):
            a = _attrs(ln.split(":", 1)[1])
            if a.get("TYPE") == "AUDIO" and a.get("URI"):
                audios.setdefault(a.get("GROUP-ID", ""), []).append(
                    {"uri": urllib.parse.urljoin(base, a["URI"]), "name": a.get("NAME", ""),
                     "default": a.get("DEFAULT", "NO").upper() == "YES"})
    return {"variants": variants, "audios": audios}


def pick_variant(master, prefer="best", max_height=0):
    """prefer: best（最高码率）/ worst / 序号字符串。max_height>0 时在不高于该高度的里选最高。"""
    vs = list(master["variants"])
    if not vs:
        return None
    if str(prefer).isdigit():
        i = int(prefer)
        if 0 <= i < len(vs):
            return vs[i]
    if max_height:
        ok = [v for v in vs if v["height"] and v["height"] <= max_height]
        if ok:
            vs = ok
    key = lambda v: (v["bw"], v["height"])      # noqa: E731
    return min(vs, key=key) if prefer == "worst" else max(vs, key=key)


def parse_media(text, base):
    """媒体列表 → {"segments":[{url,dur,seq,key,range}], "init":{url,range}|None, "ended":bool, "target":float}
    key = None 或 {"method","url","iv"(bytes|None)}；遇到不支持的加密抛 Unsupported。"""
    segs, init, ended, target = [], None, False, 6.0
    seq = 0
    cur_key = None
    dur = 0.0
    rng = None
    last_end = {}
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln:
            continue
        if ln.startswith("#EXT-X-TARGETDURATION:"):
            try:
                target = float(ln.split(":", 1)[1])
            except ValueError:
                pass
        elif ln.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                seq = int(ln.split(":", 1)[1])
            except ValueError:
                pass
        elif ln.startswith("#EXT-X-ENDLIST"):
            ended = True
        elif ln.startswith("#EXT-X-KEY:"):
            a = _attrs(ln.split(":", 1)[1])
            meth = a.get("METHOD", "NONE").upper()
            if meth == "NONE":
                cur_key = None
            elif meth == "AES-128":
                fmt = a.get("KEYFORMAT", "identity")
                if fmt != "identity":
                    raise Unsupported("密钥格式 %s 属于 DRM，不处理" % fmt)
                iv = None
                if a.get("IV"):
                    h = a["IV"][2:] if a["IV"].lower().startswith("0x") else a["IV"]
                    iv = bytes.fromhex(h.zfill(32))
                cur_key = {"method": "AES-128", "url": urllib.parse.urljoin(base, a.get("URI", "")), "iv": iv}
            else:
                raise Unsupported("加密方式 %s 不支持（只支持 AES-128）" % meth)
        elif ln.startswith("#EXT-X-MAP:"):
            a = _attrs(ln.split(":", 1)[1])
            r = _parse_range(a.get("BYTERANGE"), None) if a.get("BYTERANGE") else None
            init = {"url": urllib.parse.urljoin(base, a.get("URI", "")), "range": r[0] if r else None}
        elif ln.startswith("#EXTINF:"):
            try:
                dur = float(ln.split(":", 1)[1].split(",")[0])
            except ValueError:
                dur = 0.0
        elif ln.startswith("#EXT-X-BYTERANGE:"):
            rng = ln.split(":", 1)[1].strip()
        elif ln.startswith("#"):
            continue
        else:
            url = urllib.parse.urljoin(base, ln)
            r = None
            if rng:
                r = _parse_range(rng, last_end.get(url))
                last_end[url] = r[1]
                r = r[0]
            segs.append({"url": url, "dur": dur, "seq": seq, "key": cur_key, "range": r})
            seq += 1
            rng = None
            dur = 0.0
    return {"segments": segs, "init": init, "ended": ended, "target": target}


def _parse_range(spec, prev_end):
    n, _, o = spec.partition("@")
    n = int(n)
    start = int(o) if o else (prev_end or 0)
    return ("bytes=%d-%d" % (start, start + n - 1), start + n)


# ---------------------------------------------------------------- DASH 解析
def _tag(e):
    return e.tag.rsplit("}", 1)[-1]


def _kids(e, name):
    return [c for c in e if _tag(c) == name]


def _kid(e, name):
    k = _kids(e, name)
    return k[0] if k else None


def _iso_dur(s):
    m = re.match(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?)?$", s or "")
    if not m:
        return 0.0
    d, h, mi, sec = m.groups()
    return int(d or 0) * 86400 + int(h or 0) * 3600 + int(mi or 0) * 60 + float(sec or 0)


def _fill(tpl, rid, bw, num=None, tm=None):
    def rep(m):
        name, fmt = m.group(1), m.group(2)
        v = {"RepresentationID": rid, "Bandwidth": bw, "Number": num, "Time": tm}.get(name)
        if name == "" or v is None:
            return "$" if name == "" else m.group(0)
        if fmt:
            return (fmt % int(v)) if "d" in fmt else (fmt % v)
        return str(v)
    return re.sub(r"\$(\w*)(%0\d+d)?\$", rep, tpl)


def parse_mpd(text, base):
    """静态 MPD → {"video":[track...], "audio":[track...]}。
    track = {id,bw,height,width,codecs,mime,init:{url,range}|None,segments:[{url,dur,range}]}"""
    root = ET.fromstring(text)
    if root.get("type") == "dynamic":
        raise Unsupported("直播 MPD（type=dynamic）不在这里处理")
    total = _iso_dur(root.get("mediaPresentationDuration"))
    mbase = urllib.parse.urljoin(base, (_kid(root, "BaseURL").text or "").strip()) if _kid(root, "BaseURL") is not None else base
    out = {"video": [], "audio": []}
    periods = _kids(root, "Period")
    if not periods:
        raise Unsupported("MPD 没有 Period")
    period = periods[0]
    pdur = _iso_dur(period.get("duration")) or total
    pb = mbase
    if _kid(period, "BaseURL") is not None:
        pb = urllib.parse.urljoin(mbase, (_kid(period, "BaseURL").text or "").strip())
    for aset in _kids(period, "AdaptationSet"):
        if _kid(aset, "ContentProtection") is not None:
            raise Unsupported("MPD 带 ContentProtection（DRM），不处理")
        mime = aset.get("mimeType") or ""
        ctype = aset.get("contentType") or ""
        ab = pb
        if _kid(aset, "BaseURL") is not None:
            ab = urllib.parse.urljoin(pb, (_kid(aset, "BaseURL").text or "").strip())
        for rep in _kids(aset, "Representation"):
            if _kid(rep, "ContentProtection") is not None:
                raise Unsupported("MPD 带 ContentProtection（DRM），不处理")
            rmime = rep.get("mimeType") or mime
            kind = ctype or rmime.split("/")[0]
            if kind not in ("video", "audio"):
                continue
            rid = rep.get("id") or ""
            bw = int(rep.get("bandwidth") or 0)
            rb = ab
            if _kid(rep, "BaseURL") is not None:
                rb = urllib.parse.urljoin(ab, (_kid(rep, "BaseURL").text or "").strip())
            tr = {"id": rid, "bw": bw, "height": int(rep.get("height") or aset.get("height") or 0),
                  "width": int(rep.get("width") or aset.get("width") or 0),
                  "codecs": rep.get("codecs") or aset.get("codecs") or "", "mime": rmime,
                  "init": None, "segments": []}
            st = _kid(rep, "SegmentTemplate")
            if st is None:       # 注意：无子节点的 Element 布尔值为 False，必须用 is None 判断
                st = _kid(aset, "SegmentTemplate")
            sl = _kid(rep, "SegmentList")
            if sl is None:
                sl = _kid(aset, "SegmentList")
            sb = _kid(rep, "SegmentBase")
            if st is not None:
                ts = int(st.get("timescale") or 1)
                if st.get("initialization"):
                    tr["init"] = {"url": urllib.parse.urljoin(rb, _fill(st.get("initialization"), rid, bw)), "range": None}
                media = st.get("media") or ""
                start = int(st.get("startNumber") or 1)
                tl = _kid(st, "SegmentTimeline")
                if tl is not None:
                    t, num = 0, start
                    for s in _kids(tl, "S"):
                        if s.get("t") is not None:
                            t = int(s.get("t"))
                        d = int(s.get("d"))
                        for _ in range(int(s.get("r") or 0) + 1):
                            tr["segments"].append({"url": urllib.parse.urljoin(rb, _fill(media, rid, bw, num, t)),
                                                   "dur": d / ts, "range": None})
                            t += d
                            num += 1
                else:
                    d = int(st.get("duration") or 0)
                    if not d or not pdur:
                        raise Unsupported("SegmentTemplate 缺 duration / 总时长")
                    n = int(math.ceil(pdur * ts / d))
                    for i in range(n):
                        tr["segments"].append({"url": urllib.parse.urljoin(rb, _fill(media, rid, bw, start + i, i * d)),
                                               "dur": d / ts, "range": None})
            elif sl is not None:
                ini = _kid(sl, "Initialization")
                if ini is not None and ini.get("sourceURL"):
                    tr["init"] = {"url": urllib.parse.urljoin(rb, ini.get("sourceURL")), "range": None}
                ts = int(sl.get("timescale") or 1)
                d = int(sl.get("duration") or 0)
                for u in _kids(sl, "SegmentURL"):
                    tr["segments"].append({"url": urllib.parse.urljoin(rb, u.get("media") or ""),
                                           "dur": (d / ts) if d else 0, "range": None})
            elif sb is not None or _kid(rep, "BaseURL") is not None:
                tr["segments"].append({"url": rb, "dur": pdur, "range": None})
            if tr["segments"]:
                out[kind].append(tr)
    if not out["video"] and not out["audio"]:
        raise Unsupported("MPD 里没有可下载的视频/音频轨")
    return out


def pick_track(tracks, max_height=0):
    if not tracks:
        return None
    ts = list(tracks)
    if max_height:
        ok = [t for t in ts if t["height"] and t["height"] <= max_height]
        if ok:
            ts = ok
    return max(ts, key=lambda t: (t["bw"], t["height"]))


# ---------------------------------------------------------------- 下载核心
def _get(session, url, headers, rng, timeout, cancel):
    h = dict(headers or {})
    if rng:
        h["Range"] = rng
    r = session.get(url, headers=h, timeout=timeout, verify=False, stream=False)
    if r.status_code >= 400:
        raise RuntimeError("HTTP %d：%s" % (r.status_code, url[-80:]))
    return r.content


def _fetch_all(jobs, parts_dir, session, headers, conns, cancel, tick, throttle, retries, timeout, log):
    """jobs: [{url, range, key(dict|None), seq, idx}]。完成的分片写入 parts_dir/<idx>.seg。返回总字节数。"""
    os.makedirs(parts_dir, exist_ok=True)
    keys = {}
    klock = threading.Lock()

    def get_key(k):
        with klock:
            if k["url"] not in keys:
                keys[k["url"]] = _get(session, k["url"], headers, None, timeout, cancel)
                if len(keys[k["url"]]) != 16:
                    raise RuntimeError("密钥长度不是 16 字节（%d）：可能需要登录/带头" % len(keys[k["url"]]))
            return keys[k["url"]]

    todo = []
    done_bytes = 0
    for j in jobs:
        p = os.path.join(parts_dir, "%06d.seg" % j["idx"])
        if os.path.isfile(p) and os.path.getsize(p) > 0:
            done_bytes += os.path.getsize(p)
            continue
        todo.append(j)
    it = iter(todo)
    lock = threading.Lock()
    err = []
    state = {"bytes": done_bytes}
    if done_bytes:
        tick(done_bytes)

    def worker():
        while not err:
            if cancel and cancel():
                return
            with lock:
                j = next(it, None)
            if j is None:
                return
            last = None
            for attempt in range(retries + 1):
                if cancel and cancel():
                    return
                try:
                    data = _get(session, j["url"], headers, j.get("range"), timeout, cancel)
                    k = j.get("key")
                    if k:
                        iv = k["iv"] or j["seq"].to_bytes(16, "big")
                        data = aes128_cbc_decrypt(get_key(k), iv, data)
                    p = os.path.join(parts_dir, "%06d.seg" % j["idx"])
                    with open(p + ".tmp", "wb") as f:
                        f.write(data)
                    os.replace(p + ".tmp", p)
                    if throttle:
                        throttle(len(data))
                    with lock:
                        state["bytes"] += len(data)
                        b = state["bytes"]
                    tick(b)
                    last = None
                    break
                except Exception as e:      # noqa: BLE001
                    last = e
                    time.sleep(min(0.3 * (attempt + 1), 2))
            if last is not None:
                err.append("分片 %d 下载失败：%s" % (j["idx"], str(last)[:120]))
                return

    ths = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, min(int(conns or 8), 64)))]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    if cancel and cancel():
        raise Cancelled()
    if err:
        raise RuntimeError(err[0])
    return state["bytes"]


def _merge(parts_dir, n, out_path, prefix=None):
    with open(out_path, "wb") as out:
        if prefix:
            out.write(prefix)
        for i in range(n):
            with open(os.path.join(parts_dir, "%06d.seg" % i), "rb") as f:
                shutil.copyfileobj(f, out, 1 << 20)


def _run_ffmpeg(args, cancel, log):
    """跑 ffmpeg；取消时 terminate→kill 子进程，不留孤儿。返回退出码。"""
    exe = shutil.which("ffmpeg")
    if not exe:
        return None
    p = subprocess.Popen([exe, "-y", "-loglevel", "error"] + args, stdout=subprocess.DEVNULL,
                         stderr=subprocess.PIPE)
    while p.poll() is None:
        if cancel and cancel():
            p.terminate()
            try:
                p.wait(3)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
            raise Cancelled()
        time.sleep(0.1)
    if p.returncode:
        try:
            log("[HLS] ffmpeg 失败：%s" % p.stderr.read().decode("utf-8", "replace")[-200:])
        except Exception:
            pass
    return p.returncode


def _track_download(jobs_src, out_path, parts_dir, session, headers, conns, cancel, tick, throttle, retries,
                    timeout, log, init=None, key_for_init=None):
    jobs = []
    for i, s in enumerate(jobs_src):
        jobs.append({"url": s["url"], "range": s.get("range"), "key": s.get("key"), "seq": s.get("seq", i), "idx": i})
    prefix = None
    if init:
        prefix = _get(session, init["url"], headers, init.get("range"), timeout, cancel)
    total = _fetch_all(jobs, parts_dir, session, headers, conns, cancel, tick, throttle, retries, timeout, log)
    _merge(parts_dir, len(jobs), out_path, prefix)
    shutil.rmtree(parts_dir, ignore_errors=True)
    return total + (len(prefix) if prefix else 0)


def download(url, out_path, session, headers=None, conns=8, prefer="best", max_height=0, cancel=None, prog=None,
             log=print, throttle=None, retries=3, timeout=30, mux=True):
    """下载一个 m3u8 / mpd 点播流到 out_path（扩展名按内容修正，见返回值）。
    返回 {"path","kind":"hls|dash","segments":N,"bytes":N,"variant":str,"audio_path":str|None,"muxed":bool}
    直播 / SAMPLE-AES / DRM 抛 Unsupported；取消抛 Cancelled。"""
    headers = dict(headers or {})
    txt = session.get(url, headers=headers, timeout=timeout, verify=False)
    if txt.status_code >= 400:
        raise RuntimeError("播放列表请求失败：HTTP %d" % txt.status_code)
    base = txt.url
    body = txt.text
    t0 = time.time()
    base_out, _ = os.path.splitext(out_path)
    ctx = {"done": 0, "est": 0, "extra": 0}

    def tick_factory(offset_ref):
        def tick(b):
            d = offset_ref[0] + b
            if prog:
                prog({"status": "downloading", "downloaded_bytes": d, "total_bytes": None,
                      "speed": int(d / max(0.001, time.time() - t0)), "elapsed": time.time() - t0})
        return tick

    off = [0]
    tick = tick_factory(off)
    if body.lstrip().startswith("<?xml") or "<MPD" in body[:2000]:
        mp = parse_mpd(body, base)
        v = pick_track(mp["video"], max_height)
        a = pick_track(mp["audio"])
        name = "%s (%s)" % (v["id"], "%dx%d" % (v["width"], v["height"]) if v else "") if v else ""
        paths = []
        nseg = 0
        total = 0
        for tr, tag in ((v, "v"), (a, "a")):
            if not tr:
                continue
            ext = ".m4v" if tag == "v" else ".m4a"
            pth = "%s.%s%s" % (base_out, tag, ext)
            n = _track_download(tr["segments"], pth, pth + ".parts", session, headers, conns, cancel, tick, throttle,
                                retries, timeout, log, init=tr["init"])
            off[0] += n
            total += n
            nseg += len(tr["segments"])
            paths.append(pth)
        final = base_out + ".mp4"
        muxed = False
        if len(paths) == 2 and mux:
            rc = _run_ffmpeg(["-i", paths[0], "-i", paths[1], "-c", "copy", final], cancel, log)
            muxed = rc == 0
        elif len(paths) == 1:
            if paths[0].endswith(".m4a") and not v:
                final = base_out + ".m4a"
            if os.path.exists(final):
                os.remove(final)
            os.replace(paths[0], final)
            paths = []
            muxed = True
        if muxed:
            for p in paths:
                if os.path.exists(p):
                    os.remove(p)
            return {"path": final, "kind": "dash", "segments": nseg, "bytes": total, "variant": name,
                    "audio_path": None, "muxed": True}
        # 没有 ffmpeg / 合并失败：两条轨道分别保留，路径如实返回
        return {"path": paths[0], "kind": "dash", "segments": nseg, "bytes": total, "variant": name,
                "audio_path": paths[1] if len(paths) > 1 else None, "muxed": False}

    if "#EXTM3U" not in body[:200]:
        raise Unsupported("不是 m3u8 / mpd 内容")
    variant = ""
    audio_url = None
    if is_master(body):
        m = parse_master(body, base)
        v = pick_variant(m, prefer, max_height)
        if not v:
            raise Unsupported("主列表里没有可用码率")
        variant = "%s %dk" % (v["res"] or "?", v["bw"] // 1000)
        log("[HLS] 选码率 %s → %s" % (variant, v["url"][-60:]))
        aud = m["audios"].get(v["audio"]) if v["audio"] else None
        if aud:
            audio_url = (next((x for x in aud if x["default"]), aud[0]))["uri"]
        r = session.get(v["url"], headers=headers, timeout=timeout, verify=False)
        if r.status_code >= 400:
            raise RuntimeError("子列表请求失败：HTTP %d" % r.status_code)
        base, body = r.url, r.text
    mp = parse_media(body, base)
    if not mp["ended"]:
        raise Unsupported("这是直播流（没有 #EXT-X-ENDLIST），请走直播录制")
    if not mp["segments"]:
        raise RuntimeError("播放列表里没有分片")
    ext = ".mp4" if mp["init"] else ".ts"
    vpath = base_out + ext
    n = _track_download(mp["segments"], vpath, vpath + ".parts", session, headers, conns, cancel, tick, throttle,
                        retries, timeout, log, init=mp["init"])
    total, nseg = n, len(mp["segments"])
    audio_path, muxed = None, False
    if audio_url:
        off[0] += n
        ar = session.get(audio_url, headers=headers, timeout=timeout, verify=False)
        am = parse_media(ar.text, ar.url)
        aext = ".m4a" if am["init"] else ".aac"
        apath = "%s.a%s" % (base_out, aext)
        n2 = _track_download(am["segments"], apath, apath + ".parts", session, headers, conns, cancel, tick, throttle,
                             retries, timeout, log, init=am["init"])
        total += n2
        nseg += len(am["segments"])
        final = base_out + ".mp4"
        rc = _run_ffmpeg(["-i", vpath, "-i", apath, "-c", "copy", final], cancel, log) if mux else None
        if rc == 0:
            os.remove(vpath)
            os.remove(apath)
            vpath, muxed = final, True
        else:
            audio_path = apath
    return {"path": vpath, "kind": "hls", "segments": nseg, "bytes": total, "variant": variant,
            "audio_path": audio_path, "muxed": muxed}
