# -*- coding: utf-8 -*-
"""按录制脚本的时间轴剪辑：每段 trim → 等待段加速（setpts）→ 0.3s xfade 交叉淡化串起来 → ≤1080p、h264 crf28。
    python edit_video.py raw.mp4 timeline.json out.mp4 [--target 120] [--max-mb 49]
没有时间轴或剪辑失败时退化为 mpdecimate 去静止帧 + 整体加速。"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import ffmpeg_exe  # noqa: E402

XF = 0.3


def duration(ff, path):
    r = subprocess.run([ff, "-i", path], capture_output=True, text=True, errors="replace")
    for ln in r.stderr.splitlines():
        if "Duration:" in ln:
            h, m, s = ln.split("Duration:")[1].split(",")[0].strip().split(":")
            return int(h) * 3600 + int(m) * 60 + float(s)
    return 0.0


def plan(tl, total, target):
    off = tl.get("offset", 0.0)
    segs = []
    for s in tl["segs"]:
        a = max(0.0, s["start"] + off)
        b = min(total, s["end"] + off)
        if b - a >= 0.5:
            segs.append({"label": s["label"], "a": a, "b": b, "speed": float(s.get("speed") or 1)})
    if not segs:
        return []

    def out_len(sg):
        return sum((x["b"] - x["a"]) / x["speed"] for x in sg) - XF * (len(sg) - 1)
    # 太长：先把等待段提速到 8x，再整体提速（操作段最多 2x），再截短等待段
    for x in segs:
        if out_len(segs) <= target:
            break
        if x["speed"] > 1:
            x["speed"] = 8.0
    k = 1.0
    while out_len(segs) > target * 1.15 and k < 2.0:
        k += 0.1
        for x in segs:
            if x["speed"] <= 2.0:
                x["speed"] = round(x.get("base", x.setdefault("base", x["speed"])) * k, 2)
    for x in segs:                         # 每段至少 0.8s，xfade 才不出错
        if (x["b"] - x["a"]) / x["speed"] < 0.8:
            x["speed"] = max(1.0, (x["b"] - x["a"]) / 0.8)
    return segs


def edit(raw, tlp, out, target=120, max_mb=49):
    ff = ffmpeg_exe()
    total = duration(ff, raw)
    tl = json.load(open(tlp, encoding="utf-8")) if tlp and os.path.exists(tlp) else None
    segs = plan(tl, total, target) if tl else []
    print("raw %.1fs, segs:" % total, json.dumps(segs, ensure_ascii=False))
    crf = 28
    for attempt in range(3):
        if segs:
            parts, lens = [], []
            for i, s in enumerate(segs):
                parts.append("[0:v]trim=start=%.3f:end=%.3f,setpts=(PTS-STARTPTS)/%.3f,fps=30,"
                             "scale='min(1920,iw)':'min(1080,ih)':force_original_aspect_ratio=decrease,"
                             "scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1,format=yuv420p[v%d]" % (s["a"], s["b"], s["speed"], i))
                lens.append((s["b"] - s["a"]) / s["speed"])
            cur, acc = "v0", lens[0]
            for i in range(1, len(segs)):
                nxt = "x%d" % i
                parts.append("[%s][v%d]xfade=transition=fade:duration=%.2f:offset=%.3f[%s]" % (cur, i, XF, acc - XF, nxt))
                acc = acc + lens[i] - XF
                cur = nxt
            fc = ";".join(parts)
            cmd = [ff, "-y", "-i", raw, "-filter_complex", fc, "-map", "[%s]" % cur, "-an",
                   "-c:v", "libx264", "-preset", "medium", "-crf", str(crf), "-movflags", "+faststart", out]
        else:
            cmd = [ff, "-y", "-i", raw, "-vf", "mpdecimate,setpts=N/30/TB,fps=30,scale='min(1920,iw)':-2", "-an",
                   "-c:v", "libx264", "-preset", "medium", "-crf", str(crf), "-movflags", "+faststart", out]
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        if r.returncode != 0:
            print(r.stderr[-3000:])
            if segs:
                segs = []          # 退化
                continue
            return 1
        mb = os.path.getsize(out) / 1e6
        print("out %s %.1fMB %.1fs crf=%d" % (out, mb, duration(ff, out), crf))
        if mb <= max_mb:
            return 0
        crf += 4
    return 0


if __name__ == "__main__":
    a = sys.argv[1:]
    tgt = 120
    if "--target" in a:
        i = a.index("--target"); tgt = float(a[i + 1]); del a[i:i + 2]
    sys.exit(edit(a[0], a[1] if len(a) > 2 else None, a[-1], tgt))
