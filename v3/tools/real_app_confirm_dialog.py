# -*- coding: utf-8 -*-
"""真 SniffDL（Linux: Xvfb + xdotool）：用户主动来源的 sniffdl.intercept 会弹桌面「下载文件信息」确认窗。
验：Web 界面模式（Tk 主窗 withdraw）下确认窗【真的可见】，点「开始下载」后文件真的落盘。
    DISPLAY=:77 python tools/real_app_confirm_dialog.py --outdir /tmp/realrun_out
"""
import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mock_netdisk_site as M  # noqa: E402
from netdisk_e2e import LINKSWIFT_QUARK_UA, mock_url  # noqa: E402
from real_app_push import rpc, wait, find_file  # noqa: E402


def visible(title):
    """按标题找 X 窗口并用 xwininfo 看 Map State（无窗口管理器时 xdotool --onlyvisible 不可靠）。"""
    # xdotool search --name 对中文标题匹配不到（WM_NAME 是 COMPOUND_TEXT 编码），
    # 先按 ASCII 前缀 "SniffDL" 搜，再用 getwindowname（读 _NET_WM_NAME/UTF-8）过滤中文标题
    r = subprocess.run(["xdotool", "search", "--name", "SniffDL"], capture_output=True, text=True)
    out = []
    for w in r.stdout.split():
        nm = subprocess.run(["xdotool", "getwindowname", w], capture_output=True, text=True).stdout
        if title not in nm:
            continue
        info = subprocess.run(["xwininfo", "-id", w], capture_output=True, text=True).stdout
        # 过滤 Tk 内部同名小窗（如 326x294 的离屏辅助窗），只认真正的顶层窗口尺寸
        wd = [l for l in info.splitlines() if l.strip().startswith("Width:")]
        if "IsViewable" in info and wd and int(wd[0].split(":")[1]) >= 400:
            out.append(w)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", default="http://127.0.0.1:6800/jsonrpc")
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--mport", type=int, default=18796)
    a = ap.parse_args()
    M.serve(a.mport, 8 * 1024 * 1024, "quark-cloud-drive", "__pus=OK")
    main_vis = visible("SniffDL 视频嗅探下载器")
    print("Tk 主窗可见=%s（Web 模式下应为隐藏）" % bool(main_vis))
    fn = "confirm_%d.zip" % int(time.time())
    t0 = time.time()
    info = {"url": mock_url(a.mport, fn=fn, tag="cf"), "filename": fn, "referer": "https://pan.quark.cn/",
            "cookie": "__pus=OK", "ua": LINKSWIFT_QUARK_UA, "size": 8 * 1024 * 1024,
            "source": "chip", "confirm": True}
    r = rpc(a.rpc, "sniffdl.intercept", [info])
    print("intercept 返回:", r.get("result"))
    win = []
    for _ in range(60):
        time.sleep(0.5)
        win = visible("下载文件信息")
        if win:
            break
    print("%s 确认窗可见=%s（intercept 后 %.1fs 出现）" % ("PASS" if win else "FAIL", bool(win), time.time() - t0))
    if not win:
        print("CONFIRM DIALOG RESULT=FAIL")
        return 1
    # 截图留证 + 点「开始下载」：窗口内按钮位置不固定 → 用 Tab 焦点不可靠，直接找按钮坐标太脆；
    # 这里走窗口内的快捷键：确认窗把 <Return> 绑到 start_now（若没绑则退回点击右下区域）
    subprocess.run(["import", "-window", "root", "/tmp/confirm_dialog.png"], capture_output=True)
    # 无窗口管理器：windowactivate 不可用；windowfocus + 真实键盘事件（--window 发的是 XSendEvent 合成事件，Tk 会忽略）
    subprocess.run(["xdotool", "windowfocus", win[0]], capture_output=True)
    time.sleep(0.3)
    subprocess.run(["xdotool", "key", "Return"], capture_output=True)
    time.sleep(1.5)
    gid = ""
    for _ in range(10):
        lst = rpc(a.rpc, "aria2.tellStopped", [0, 50]).get("result") or []
        lst += rpc(a.rpc, "aria2.tellActive", []).get("result") or []
        for t in lst:
            if (t.get("uri") or "").find("/cf/") > 0:
                gid = t["gid"]
        if gid:
            break
        time.sleep(0.5)
    if not gid:
        print("FAIL 按回车后没有任务入队（确认窗没绑回车？）")
        print("CONFIRM DIALOG RESULT=FAIL")
        return 1
    st, dt = wait(a.rpc, gid, "", 60)
    p = find_file(a.outdir, fn, t0)
    ok = st.get("status") == "complete" and p and os.path.getsize(p) == 8 * 1024 * 1024
    print("%s 确认后下载完成 status=%s %s" % ("PASS" if ok else "FAIL", st.get("status"), p))
    print("CONFIRM DIALOG RESULT=%s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
