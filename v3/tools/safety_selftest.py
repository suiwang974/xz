# -*- coding: utf-8 -*-
"""打包前必跑的下载安全自测（全程 netguard 封网，任何真触网都会当场抛错）。

    python tools/safety_selftest.py

八项断言，全绿才允许 PyInstaller。写这个文件的理由：这个项目已经有三次
「我以为修好了」——查磁盘的去重其实是死代码、收孤儿挂在错误的初始化顺序上被
try 静默吞掉、以及诊断脚本误下 121MB。光靠读代码判断"应该没问题"是不行的。
"""
import os, sys, threading, time, tempfile, shutil

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
before = netguard.snapshot_dir(REAL_DL)             # 真目录快照：测完必须一个字节都没变

TMP = tempfile.mkdtemp(prefix="safety_")
root = tk.Tk()
app = VD.App(root)
root.update()
app.cfg["outdir"] = TMP
app._rx_cap_gb = 0.0                                # 先关熔断，单独测它
LOGS = []
_orig_log = app.log
app.log = lambda m: (LOGS.append(str(m)), _orig_log(str(m)))[0]

hits = []
lk = threading.Lock()


def fake_direct(t, prog, headers):
    p = os.path.join(t["dir"], VD.safe_name(t["name"]))
    with lk:
        hits.append(t["gid"])
    time.sleep(0.08)
    with open(p, "wb") as f:
        f.write(b"y" * 2048)
    return p


app._rpc_direct_file = fake_direct
os.makedirs(os.path.join(TMP, "应用程序"), exist_ok=True)
URL = "https://example.invalid/tool-1.0.exe"

fails = []


def ck(name, cond, extra=""):
    print("  %-46s %s %s" % (name, "PASS" if cond else "!!FAIL", extra))
    if not cond:
        fails.append(name)


print("[1] 每道防呆必须自报状态（日志里出现 !! 即视为坏了）")
rep = app._safety_selfcheck()
line = " | ".join(rep)
print("   ", line[:180])
ck("自检全部为 OK/就绪，无 !!", "!!" not in line)
ck("自检覆盖了 5 道防线", len(rep) >= 5, "(%d 项)" % len(rep))

print("\n[2] 同一链接并发提交 12 次只落 1 次")
gids = []
ths = [threading.Thread(target=lambda: gids.append(app.rpc_add_uri(URL, {})))
       for _ in range(12)]
for t in ths:
    t.start()
for t in ths:
    t.join()
dl = time.time() + 12
while time.time() < dl and any(app._rpc_tasks.get(g, {}).get("status") not in
                               ("complete", "error", "paused") for g in set(gids)):
    root.update()
    time.sleep(0.05)
ck("只落盘 1 次", len(hits) == 1, "(hits=%d)" % len(hits))
ck("12 次返回同一任务号", len(set(gids)) == 1)
ck("重复被明确记入日志", sum(1 for l in LOGS if "忽略重复提交" in l) >= 11)

print("\n[3] 跨会话：任务表清空后再提交，靠账本零流量跳过")
hits.clear()
app._rpc_tasks.clear()
app._rpc_order.clear()
app.rpc_add_uri(URL, {})
root.update()
time.sleep(0.5)
root.update()
ck("没有再落盘", len(hits) == 0, "(hits=%d)" % len(hits))
ck("账本命中并记日志", sum(1 for l in LOGS if "已下载过" in l) >= 1)

print("\n[4] aria2 session 净化：HTTP 条目绝不自动重放")
d = tempfile.mkdtemp()
with open(os.path.join(d, "bt.session"), "w", encoding="utf-8") as f:
    f.write("https://cdn.x/a.zip?sig=1\n dir=%s\n out=a.zip\n\n" % d)
    f.write("https://cdn.x/b.zip?sig=2\n dir=%s\n out=b.zip\n\n" % d)
    f.write("magnet:?xt=urn:btih:%s\n dir=%s\n\n" % ("a" * 40, d))
out = app._bt_session_sanitized(os.path.join(d, "bt.session"), d)
txt = open(out, encoding="utf-8").read() if out else ""
ck("2 条 HTTP 被丢弃", ".zip" not in txt, repr(txt[:60]))
ck("1 条 magnet 保留续种", txt.count("magnet:") == 1)
shutil.rmtree(d, ignore_errors=True)

print("\n[5] 网盘直链不被误判成 BT 种子")
ck("夸克长 hex ≠ BT", app._rpc_is_bt(
    "https://dl-pc-zb.drive.quark.cn/x/1/" + "0" * 40 + "/6a95346b") is False)
ck("带 fname 的 OSS ≠ BT", app._rpc_is_bt(
    "https://b.oss-cn-beijing.aliyuncs.com/p/tool.exe?fname=tool.exe") is False)
ck("真磁力 = BT", app._rpc_is_bt("magnet:?xt=urn:btih:" + "b" * 40) is True)

print("\n[6] 流量保险丝：超上限后拒绝新任务")
app._rx_cap_gb = 0.000001                         # ≈1KB，比测试文件的 2KB 小
app._rx_bytes = 0
app._rx_blocked = False
app._rx_account(os.path.join(TMP, "应用程序", VD.safe_name("tool-1.0.exe")))
ck("已触发熔断", app._rx_blocked is True)
app._rpc_tasks.clear()
app._rpc_order.clear()
hits.clear()
g = app.rpc_add_uri("https://example.invalid/another.exe", {})
root.update()
time.sleep(0.4)
root.update()
ck("熔断后不再落盘", len(hits) == 0)
ck("熔断有明确日志", sum(1 for l in LOGS if "熔断" in l) >= 1)
app._rx_blocked = False
app._rx_cap_gb = 0.0

print("\n[7] 归一化不误杀：不同文件必须是不同键")
k1 = app._rpc_norm_key("https://x.com/a.zip?sig=1")
k2 = app._rpc_norm_key("https://x.com/b.zip?sig=1")
ck("不同文件 → 不同键", k1 != k2)

print("\n[8] 本次自测有没有偷碰真下载目录 / 有没有触网")
bad = netguard.assert_no_new_files(before, REAL_DL)
ck("真下载目录零变动", not bad, "; ".join(bad[:3]))
ck("没有外网连接被放行", len(netguard.BLOCKED) >= 0 and all(
    "example.invalid" not in b for b in netguard.BLOCKED),
   "拦截样本 %d 条" % len(netguard.BLOCKED))

shutil.rmtree(TMP, ignore_errors=True)
try:
    h = VD.load_history()
    h.pop(app._rpc_norm_key(URL), None)
    VD.save_history(h)
except Exception:
    pass
root.destroy()
print("\n结果：%s（%d 项失败）" % ("全部通过 ✅" if not fails else "失败 → " + ", ".join(fails), len(fails)))
sys.exit(0 if not fails else 1)
