# -*- coding: utf-8 -*-
"""直播录制（N_m3u8DL-RE 三件套的轻量实现）：
  · 边录边合并：新分片直接追加进同一个 .ts，中途停止文件也能播
  · 录制时长上限：到点自动停（limit_sec，0 = 不限）
  · 空闲停止：连续 idle_sec 秒没有新分片（直播结束/断流）自动停
只处理明文 HLS（含标准 AES-128 时需要 key，本实现不解密，遇到 #EXT-X-KEY 直接报不支持）；DRM 一律不做。
"""
import time
import urllib.parse

import requests


def _parse(text, base):
    segs, target, ended, master = [], 6.0, False, []
    seq = 0
    lines = [ln.strip() for ln in text.splitlines()]
    for i, ln in enumerate(lines):
        if ln.startswith("#EXT-X-TARGETDURATION:"):
            try:
                target = float(ln.split(":", 1)[1])
            except Exception:
                pass
        elif ln.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                seq = int(ln.split(":", 1)[1])
            except Exception:
                pass
        elif ln.startswith("#EXT-X-ENDLIST"):
            ended = True
        elif ln.startswith("#EXT-X-KEY") and "METHOD=NONE" not in ln.upper():
            raise RuntimeError("这个直播流是加密的（#EXT-X-KEY），直播录制只支持明文 HLS")
        elif ln.startswith("#EXT-X-STREAM-INF"):
            for nxt in lines[i + 1:]:
                if nxt and not nxt.startswith("#"):
                    master.append(urllib.parse.urljoin(base, nxt))
                    break
        elif ln and not ln.startswith("#"):
            segs.append((seq, urllib.parse.urljoin(base, ln)))
            seq += 1
    return segs, target, ended, master


def is_live(text):
    return "#EXTINF" in text and "#EXT-X-ENDLIST" not in text


def record(url, out_path, headers=None, limit_sec=0, idle_sec=30, cancel=None, prog=None, log=print,
           session=None, poll_min=1.0):
    """录到 out_path（.ts）。返回 (path, reason)。reason: limit / idle / ended / cancel。"""
    s = session or requests.Session()
    h = dict(headers or {})
    r = s.get(url, headers=h, timeout=15)
    r.raise_for_status()
    segs, target, ended, master = _parse(r.text, r.url)
    if master and not segs:
        url = master[-1]                       # 选码率最高（通常最后一条）
        log("[直播] 主列表 → 选最后一路 %s" % url[:80])
    seen = set()
    t0 = time.time()
    last_new = time.time()
    total = 0
    reason = "ended"
    with open(out_path, "ab") as f:
        while True:
            if cancel and cancel():
                reason = "cancel"
                break
            if limit_sec and time.time() - t0 >= limit_sec:
                reason = "limit"
                break
            try:
                r = s.get(url, headers=h, timeout=15)
                segs, target, ended, _m = _parse(r.text, r.url)
            except RuntimeError:
                raise
            except Exception as e:
                log("[直播] 刷新列表失败：%s" % str(e)[:80])
                segs, ended = [], False
            new = [(sq, u) for sq, u in segs if sq not in seen]
            for sq, u in new:
                if cancel and cancel():
                    break
                try:
                    data = s.get(u, headers=h, timeout=20).content
                except Exception as e:
                    log("[直播] 分片下载失败（跳过）：%s" % str(e)[:60])
                    seen.add(sq)
                    continue
                f.write(data)
                f.flush()
                seen.add(sq)
                total += len(data)
                last_new = time.time()
                if prog:
                    prog({"status": "downloading", "downloaded_bytes": total, "total_bytes": None,
                          "speed": int(total / max(0.001, time.time() - t0)), "elapsed": time.time() - t0})
            if ended and not [x for x in segs if x[0] not in seen]:
                reason = "ended"
                break
            if idle_sec and time.time() - last_new >= idle_sec:
                reason = "idle"
                break
            time.sleep(max(poll_min, min(target / 2.0, 5.0)))
    log("[直播] 录制结束（%s），共 %d 字节 → %s" % (reason, total, out_path))
    return out_path, reason
