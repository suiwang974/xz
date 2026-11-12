# -*- coding: utf-8 -*-
"""Bug A 回归测试：下载确认卡"没人点嗅探也反复弹" + 一次多条合并为一张汇总卡。
全程 netguard 封网；所有下载/UI 出口都打桩，绝不真下、绝不真弹窗。

    python tools/bugA_intercept_regression.py

覆盖 HANDOVER_V5 §15.3 Bug A：
  1) 意图分流：自动接管(source=browser/空)绝不弹桌面确认大窗。
  2) 杂链过滤：fwlink 更新器 / 广告 .htm 碎片 / <1KB 碎屑 / text-html 内联 → 忽略不下；
     真媒体/大附件 → 静默入队。
  3) 汇总卡：用户主动 confirm=True 的多条 → 去抖窗口后合并成"一张"汇总卡（1 条仍是原单卡，零回归）。
  4) 判据复现：不点嗅探时 0 张确认卡；卡里没有 fwlink / 548B .htm。
"""
import os, sys, tempfile, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
# 控制台默认 GBK，最后一行结论里的 ✅/❌ 会 UnicodeEncodeError 把判定吞掉
# （打包闸门看不到"全绿"= 等于没跑）→ 强制 stdout/stderr 走 utf-8
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import netguard
netguard.install()

import tkinter as tk                                # noqa: E402
import video_downloader as VD                       # noqa: E402

REAL_DL = r"C:\Users\王伟\Videos\SniffDL"
before = netguard.snapshot_dir(REAL_DL)

TMP = tempfile.mkdtemp(prefix="bugA_")
root = tk.Tk()
root.withdraw()
app = VD.App(root)
root.update()
app.cfg["outdir"] = TMP
app.cfg["rpc_confirm_dialog"] = True

fails = []


def ck(name, cond, extra=""):
    print("  %-52s %s %s" % (name, "PASS" if cond else "!!FAIL", extra))
    if not cond:
        fails.append(name)


# —— 打桩：计数各出口，不真下、不真弹窗 ——
CALLS = {"start": [], "prog": [], "dialog": [], "summary": []}
app._rpc_start_from_info = lambda info, od, nm: (CALLS["start"].append((info.get("url"), nm)), "GID")[1]
app._open_progress_window = lambda gid, nm, od: CALLS["prog"].append(nm)
app._open_intercept_dialog = lambda info: CALLS["dialog"].append(info.get("url"))
app._open_intercept_summary = lambda items: CALLS["summary"].append(list(items))

# —— 让 root.after(0, fn) 同步执行；延时>0(去抖)只登记不触发（由测试手动 flush）——
_scheduled = []


def fake_after(delay, func, *a):
    if delay == 0:
        func(*a)
    else:
        _scheduled.append((delay, func))
    return ("tk", len(_scheduled))


app.root.after = fake_after
app.root.after_cancel = lambda t: None


def reset():
    for k in CALLS:
        CALLS[k].clear()
    app._icpt_buf = []
    app._icpt_timer = None
    del _scheduled[:]


print("[1] 意图判定 _rpc_is_user_source")
ck("browser 非用户主动", app._rpc_is_user_source({"source": "browser"}) is False)
ck("空/缺省 非用户主动", app._rpc_is_user_source({}) is False)
ck("menu-link 用户主动", app._rpc_is_user_source({"source": "menu-link"}) is True)
ck("sniff/popup/chip 用户主动",
   all(app._rpc_is_user_source({"source": s}) for s in ("sniff", "popup", "chip", "badge")))

print("\n[2] 杂链过滤 _rpc_is_junk_autolink")
ck("fwlink 更新器 → 垃圾",
   app._rpc_is_junk_autolink("https://go.microsoft.com/fwlink/?linkid=2171764", {}) is True)
ck("drtuber 548B .htm → 垃圾",
   app._rpc_is_junk_autolink("https://gcdn.drtuber.com/mp4_hq/x.htm", {"size": 548}) is True)
ck("无扩展 text/html → 垃圾",
   app._rpc_is_junk_autolink("https://x.com/page", {"mime": "text/html"}) is True)
ck("<1KB 碎屑 → 垃圾",
   app._rpc_is_junk_autolink("https://x.com/a.bin", {"size": 512}) is True)
ck("5MB .mp4 → 保留（非垃圾）",
   app._rpc_is_junk_autolink("https://cdn.x/movie.mp4", {"size": 5000000}) is False)
ck("10MB .exe → 保留",
   app._rpc_is_junk_autolink("https://cdn.x/tool.exe", {"size": 10000000}) is False)
ck("大 .html(≥256KB) → 保留",
   app._rpc_is_junk_autolink("https://x.com/big.html", {"size": 300000}) is False)

print("\n[3] 自动接管绝不弹桌面确认大窗")
reset()
app.rpc_intercept({"url": "https://go.microsoft.com/fwlink/?linkid=2171764",
                   "source": "browser", "confirm": True})
ck("fwlink 自动项：不起下载", CALLS["start"] == [])
ck("fwlink 自动项：不弹单卡", CALLS["dialog"] == [])
ck("fwlink 自动项：不弹汇总卡", CALLS["summary"] == [])
ck("fwlink 自动项：不弹进度窗", CALLS["prog"] == [])

reset()
g = app.rpc_intercept({"url": "https://cdn.x/movie.mp4", "source": "browser",
                       "confirm": True, "size": 5000000})
ck("真媒体自动项：静默入队(起下载 1 次)", len(CALLS["start"]) == 1)
ck("真媒体自动项：返回 gid", g == "GID")
ck("真媒体自动项：不弹任何确认卡", CALLS["dialog"] == [] and CALLS["summary"] == [])

print("\n[4] 用户主动 confirm=False → 直连 + 进度窗")
reset()
app.rpc_intercept({"url": "https://cdn.x/v.mp4", "source": "menu-link",
                   "confirm": False, "filename": "v.mp4"})
ck("直连：起下载 1 次", len(CALLS["start"]) == 1)
ck("直连：弹进度窗 1 次", len(CALLS["prog"]) == 1)
ck("直连：不弹确认卡", CALLS["dialog"] == [] and CALLS["summary"] == [])

print("\n[5] 用户主动 confirm=True → 合并汇总卡")
reset()
app.rpc_intercept({"url": "https://cdn.x/a.mp4", "source": "popup", "confirm": True,
                   "filename": "a.mp4"})
reset()
# 单条：入缓冲后手动 flush → 原单卡
app.rpc_intercept({"url": "https://cdn.x/a.mp4", "source": "popup", "confirm": True, "filename": "a.mp4"})
app._icpt_flush()
ck("用户单条：走原单卡(dialog)", len(CALLS["dialog"]) == 1)
ck("用户单条：不走汇总卡", CALLS["summary"] == [])

reset()
for u in ("https://cdn.x/a.mp4", "https://cdn.x/b.mkv", "https://cdn.x/a.mp4?sig=9", "https://cdn.x/c.mp4"):
    app.rpc_intercept({"url": u, "source": "popup", "confirm": True})
app._icpt_flush()
ck("用户多条：只弹一张汇总卡", len(CALLS["summary"]) == 1)
ck("汇总卡内按归一化 URL 去重(4→3)",
   len(CALLS["summary"][0]) == 3 if CALLS["summary"] else False,
   "(got=%d)" % (len(CALLS["summary"][0]) if CALLS["summary"] else -1))
ck("合并期间不弹单卡/进度窗", CALLS["dialog"] == [] and CALLS["prog"] == [])

print("\n[6] 真下载目录必须零变动")
after = netguard.snapshot_dir(REAL_DL)
ck("真目录字节未变", before == after, "(%d→%d)" % (len(before), len(after)))

shutil.rmtree(TMP, ignore_errors=True)
print("\n===> %s (%d fail)" % ("ALL GREEN" if not fails else "FAILED: " + ", ".join(fails), len(fails)))
sys.exit(1 if fails else 0)
