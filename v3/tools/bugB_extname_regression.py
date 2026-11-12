# -*- coding: utf-8 -*-
"""Bug B 回归测试：下载丢扩展名（exe 落成无后缀 download）——全程 netguard 封网。

    python tools/bugB_extname_regression.py

覆盖 HANDOVER_V5 §15.3 Bug B：
  1) 纯函数：从 Content-Disposition(RFC5987/兼容/中文) + Content-Type 映射 + 最终 URL 派生后缀；
     octet-stream / 未知类型一律不硬造后缀（保留来源线索名）。
  2) 路由判定 _rpc_is_direct_file(url, name)：URL 本身无后缀但已知真名带 .exe → 走精确落名直链通道；
     m3u8/mpd 流清单即便出现在 name 里也不当直链。
  3) 端到端 _rpc_direct_file：URL/query 推不出后缀时探测补后缀，落成 *.exe 可直接运行；
     探测无果则保留 download（不伪造），且离线不抛、不阻断主流程。
断言全绿才允许 PyInstaller。任何真触网 / 动到真下载目录都会当场失败。
"""
import os, sys, tempfile, shutil, traceback

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

TMP = tempfile.mkdtemp(prefix="bugB_")
root = tk.Tk()
root.withdraw()
app = VD.App(root)
root.update()
app.cfg["outdir"] = TMP

fails = []


def ck(name, cond, extra=""):
    print("  %-52s %s %s" % (name, "PASS" if cond else "!!FAIL", extra))
    if not cond:
        fails.append(name)


print("[1] Content-Disposition 解析（RFC5987 / 兼容 / 中文）")
ck("filename*=UTF-8'' 中文.exe",
   app._rpc_cd_filename({"Content-Disposition":
                         "attachment; filename*=UTF-8''%E4%B8%AD%E6%96%87.exe"}) == "中文.exe")
ck('filename="setup.exe" 兼容',
   app._rpc_cd_filename({"Content-Disposition": 'attachment; filename="setup.exe"'}) == "setup.exe")
ck("路径只留末段",
   app._rpc_cd_filename({"Content-Disposition":
                         'attachment; filename="a/b\\\\c.pdf"'}) == "c.pdf")
ck("无 Content-Disposition → 空串",
   app._rpc_cd_filename({"Content-Type": "video/mp4"}) == "")

print("\n[2] 后缀派生：CD → Content-Type 映射 → 最终 URL（纯函数）")
ck("CD 优先给 .exe",
   app._rpc_ext_from_headers({"Content-Disposition": 'attachment; filename="w.exe"'}) == ".exe")
ck("x-msdownload → .exe",
   app._rpc_ext_from_headers({"Content-Type": "application/x-msdownload"}) == ".exe")
ck("video/mp4 → .mp4",
   app._rpc_ext_from_headers({"Content-Type": "video/mp4"}) == ".mp4")
ck("text/html → .htm",
   app._rpc_ext_from_headers({"Content-Type": "text/html; charset=utf-8"}) == ".htm")
ck("octet-stream 不硬造后缀",
   app._rpc_ext_from_headers({"Content-Type": "application/octet-stream"}) == "")
ck("无头无 ct，退到最终 URL path 末段 .dmg",
   app._rpc_ext_from_headers({}, "https://cdn.x/a/ installer.dmg?q=1".replace(" ", "")) == ".dmg")
ck("全无线索 → 空串",
   app._rpc_ext_from_headers({}, "https://go.microsoft.com/fwlink/?linkid=2171764") == "")

print("\n[2b] 完整真名择优先 _rpc_name_from_headers（CD → 最终 URL basename）")
ck("CD 名优先",
   app._rpc_name_from_headers({"Content-Disposition": 'attachment; filename="w.exe"'},
                              "https://cdn.final/other.mp4") == "w.exe")
ck("fwlink 场景：无 CD → 用重定向后最终 URL 名",
   app._rpc_name_from_headers({}, "https://download.microsoft.com/download/x/"
      "Windows11InstallationAssistant.exe?e1=1&e2=2") == "Windows11InstallationAssistant.exe")
ck("最终 URL 无后缀 → 空串（不硬造）",
   app._rpc_name_from_headers({}, "https://go.microsoft.com/fwlink/?linkid=2171764") == "")

print("\n[3] 路由判定 _rpc_is_direct_file(uri, name)")
FW = "https://go.microsoft.com/fwlink/?linkid=2171764"
ck("URL 无后缀 + 真名带 .exe → 直链", app._rpc_is_direct_file(FW, "Windows11InstallationAssistant.exe") is True)
ck("URL 无后缀 + 名 download 无后缀 → 非直链", app._rpc_is_direct_file(FW, "download") is False)
ck("name 是 m3u8 流清单 → 非直链（交回 yt-dlp）", app._rpc_is_direct_file(FW, "master.m3u8") is False)
ck("URL 自身带 .mp4 → 直链", app._rpc_is_direct_file("https://cdn.x/v.mp4", "") is True)
ck("旧签名兼容：不传 name 仍按 URL 判", app._rpc_is_direct_file("https://cdn.x/v.mkv") is True)

print("\n[4] 端到端 _rpc_direct_file：无后缀名探测补后缀")


def run_direct(probe_ret, initial_name):
    """把网络出口全打桩：探测用 probe_ret，落盘用假 aria2c（按传入 name 建文件）。
    返回最终落盘文件名。全程不触网（netguard + 桩）。"""
    VD.get_system_proxy = lambda: None            # 避免 socks 分支绕开 aria2c
    app._rpc_probe_name = lambda url, headers: probe_ret
    seen = {}

    def fake_aria2c(url, outdir, name, headers, prog=None, cancel=None):
        seen["name"] = name
        p = os.path.join(outdir, VD.safe_name(name))
        with open(p, "wb") as f:
            f.write(b"MZ" + b"\x00" * 64)
        return p
    orig_aria = app._http_aria2c
    orig_clean = app._rpc_clean_stale
    app._http_aria2c = fake_aria2c
    app._rpc_clean_stale = lambda path: None
    gid = "gid" + str(abs(hash(initial_name)) % 9999)
    sub = tempfile.mkdtemp(prefix="dl_", dir=TMP)
    t = {"uri": FW, "dir": sub, "gid": gid, "name": initial_name,
         "referer": "", "headers": {}, "cancel": False, "start_ts": 0}
    try:
        path = app._rpc_direct_file(t, None, {})
        return os.path.basename(path), t.get("name"), seen.get("name")
    finally:
        app._http_aria2c = orig_aria
        app._rpc_clean_stale = orig_clean
        shutil.rmtree(sub, ignore_errors=True)


base, tname, aname = run_direct(("Windows11InstallationAssistant.exe", ".exe"), "download")
ck("补 CD 真名 → *.exe", base.endswith(".exe") and base == "Windows11InstallationAssistant.exe",
   "(got=%s)" % base)
ck("t['name'] 同步更新为 .exe", (tname or "").endswith(".exe"))
ck("传给下载引擎的名字也带 .exe", (aname or "").endswith(".exe"))

base2, _, _ = run_direct(("", ".htm"), "download")
ck("仅有 Content-Type → download.htm", base2.endswith(".htm"), "(got=%s)" % base2)

base3, _, _ = run_direct(("", ""), "download")
ck("探测无果 → 保留 download（不伪造后缀）", base3 == "download", "(got=%s)" % base3)

base4, _, _ = run_direct(("报告.pdf", ".pdf"), "download")
ck("中文 CD 名直用", base4 == "报告.pdf", "(got=%s)" % base4)

print("\n[5] 离线安全：_rpc_probe_name 在封网下静默返回、绝不抛")
VD.get_system_proxy = lambda: None
try:
    cd, ext = app._rpc_probe_name("https://go.microsoft.com/fwlink/?linkid=2171764", {})
    ck("封网探测不抛异常", True)
except Exception:
    traceback.print_exc()
    ck("封网探测不抛异常", False)

print("\n[6] 真下载目录必须零变动")
after = netguard.snapshot_dir(REAL_DL)
ck("真目录字节未变", before == after, "(before=%s after=%s)" % (len(before), len(after)))

shutil.rmtree(TMP, ignore_errors=True)
print("\n===> %s (%d fail)" % ("ALL GREEN" if not fails else "FAILED: " + ", ".join(fails), len(fails)))
sys.exit(1 if fails else 0)
