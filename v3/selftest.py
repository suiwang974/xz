"""SniffDL 自动检测：真实网页 嗅探→下载→验证 全链路。
用例1 普通网页 mp4 / 用例2 m3u8 流合并 / 用例3 视频平台完整链路。
检测不过关就修到过关。用法: python selftest.py
"""
import os
import time

import yt_dlp
import video_downloader as v

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "selftest_out")


def log(m):
    print(m, flush=True)


def sniff_page(url):
    t0 = time.time()
    sniffed = v.run_sniff(url, log)
    log(f"  嗅探到 {len(sniffed)} 条媒体 ({time.time() - t0:.1f}s)")
    for e in sniffed:
        log(f"    - [{e['how']}] {os.path.basename(e['url'].split('?')[0])[:60]}")
    return sniffed


def download_entry(entry, page_url, quality="best", first_seconds=None, title=None):
    os.makedirs(OUT, exist_ok=True)
    e = dict(entry)
    if title:
        e["title"] = title
    e["title"] = os.path.splitext(e["title"])[0]

    def hook(d):
        pass

    extra = {}
    if first_seconds:
        extra["download_ranges"] = yt_dlp.utils.download_range_func(None, [(0, first_seconds)])
    v.download_one(e, page_url, OUT, quality, hook, extra_opts=extra or None)
    name = v.safe_name(e["title"])
    hits = [f for f in os.listdir(OUT) if f.startswith(name)]
    assert hits, "下载后未找到输出文件"
    path = os.path.join(OUT, hits[0])
    return path, os.path.getsize(path)


def case_w3schools():
    log("\n" + "=" * 62)
    log("用例1: 普通网页 mp4 嗅探并下载 (w3schools)")
    url = "https://www.w3schools.com/html/html5_video.asp"
    sniffed = sniff_page(url)
    assert sniffed, "嗅探结果为空"
    probed = v.probe_all(sniffed, url, log)
    mp4 = next((e for e in probed if e.get("ext") == "mp4"), probed[0])
    log(f"  [2] 下载: {mp4['title']} ({mp4.get('ext')}, {v.fmt_size(mp4.get('size'))}, {mp4.get('res')})")
    path, size = download_entry(mp4, url)
    assert size > 100_000, f"文件过小: {size} B"
    log(f"  ✓ 下载成功 {path} ({v.fmt_size(size)})")


def case_m3u8():
    log("\n" + "=" * 62)
    log("用例2: m3u8 HLS 流下载并合并为 mp4 (mux 测试流, 截取前60秒)")
    url = "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8"
    entry = {"url": url, "title": "hls_60s", "how": "直链"}
    path, size = download_entry(entry, url, quality="480", first_seconds=60)
    assert size > 300_000, f"文件过小: {size} B"
    log(f"  ✓ HLS 下载合并成功 {path} ({v.fmt_size(size)})")


def case_bilibili():
    log("\n" + "=" * 62)
    log("用例3: 国内视频平台完整链路 嗅探→解析→下载 (bilibili, 截前60秒)")
    url = "https://www.bilibili.com/video/BV1GJ411x7h7"
    t0 = time.time()
    try:
        sniffed = v.run_sniff(url, log)
    except Exception as e:
        log(f"  嗅探异常: {str(e)[:150]}")
        sniffed = []
    try:
        ydl = v.ydl_probe(url)
    except Exception:
        ydl = []
    log(f"  嗅探 {len(sniffed)} 条, 站点解析 {len(ydl)} 条 ({time.time() - t0:.1f}s)")
    assert sniffed, "B站嗅探到 0 条（媒体类型识别失败）"
    assert ydl, "B站站点解析失败"
    entry = ydl[0]
    log(f"  [2] 下载: {entry['title'][:50]} {entry.get('res', '')} {v.fmt_size(entry.get('size'))}")
    path, size = download_entry(entry, url, quality="480", first_seconds=60, title="bilibili_60s")
    assert size > 100_000, f"文件过小: {size} B"
    log(f"  ✓ B站下载成功 {path} ({v.fmt_size(size)})")


def main():
    results = []
    for fn in (case_w3schools, case_m3u8, case_bilibili):
        try:
            fn()
            results.append((fn.__name__, True, ""))
        except Exception as e:
            results.append((fn.__name__, False, str(e)[:200]))
            log(f"  ✗ 失败: {str(e)[:300]}")
    log("\n" + "=" * 62)
    for n, s, err in results:
        log(f"  {'✓ PASS' if s else '✗ FAIL'}  {n}  {err}")
    fails = [n for n, s, _ in results if not s]
    if fails:
        raise SystemExit(f"检测未通过: {fails}")
    log("全部检测通过 ✔")


if __name__ == "__main__":
    main()