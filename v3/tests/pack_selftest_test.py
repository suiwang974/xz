# -*- coding: utf-8 -*-
"""计划4 打包收尾：pack_selftest 清单/检查函数 + sniffdl_core --selftest + exe 入口分流 + tools/exe_ci_check 判定。
沙箱跑（不打 exe）；冻结形态用 sys.frozen/_MEIPASS 模拟。用法：python3 tests/pack_selftest_test.py → 打印 x/y，全过退出 0。"""
import ast
import glob
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import pack_selftest as PS  # noqa: E402
import exe_ci_check as XC  # noqa: E402

OK = BAD = 0


def chk(name, cond, detail=""):
    global OK, BAD
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  ← %s" % str(detail)[:300]), flush=True)
    OK += bool(cond)
    BAD += (not cond)


SPEC = open(os.path.join(ROOT, "SniffDL.spec"), encoding="utf-8").read()


def spec_hiddenimports():
    """把 spec 里所有 hiddenimports += [...] 的字面量收集起来。"""
    out = set()
    for m in re.finditer(r"hiddenimports\s*\+=\s*(\[[^\]]*\])", SPEC):
        try:
            out.update(ast.literal_eval(m.group(1)))
        except Exception:
            pass
    return out


# 1 清单 ⊆ spec hiddenimports（第三方包由 collect_all 带，不要求字面量）
hid = spec_hiddenimports()
own = [m for m in PS.REQUIRED_MODULES if m not in PS.THIRD_PARTY]
miss = [m for m in own if m not in hid]
chk("pack_selftest 清单里自有模块全部在 spec hiddenimports", not miss, miss)
chk("Windows 托盘后端 pystray._win32 在 spec", all(m in hid for m in PS.WIN_ONLY_MODULES), PS.WIN_ONLY_MODULES)

# 2 清单覆盖磁盘上 core/*.py、sdl_speed/*.py（新模块忘进清单就红）
disk = {"core." + os.path.basename(f)[:-3] for f in glob.glob(os.path.join(ROOT, "core", "*.py"))
        if not f.endswith("__init__.py")}
disk |= {"sdl_speed." + os.path.basename(f)[:-3] for f in glob.glob(os.path.join(ROOT, "sdl_speed", "*.py"))
         if not f.endswith("__init__.py")}
chk("清单覆盖 core/ 与 sdl_speed/ 全部模块", disk <= set(PS.REQUIRED_MODULES), sorted(disk - set(PS.REQUIRED_MODULES)))
must = ["core.hls", "core.sidecar", "sdl_speed", "netdisk_rules", "rpc_server", "sniffdl_core", "video_downloader"]
chk("关键模块（hls/sidecar/sdl_speed/netdisk_rules/rpc_server/入口）在清单", all(m in PS.REQUIRED_MODULES for m in must))

# 3 spec 数据：扩展、内核界面、托盘资源、aria2c、ffmpeg
chk("spec 打包 ui/ 与 extension/ 与 assets/", all("'%s'" % d in SPEC for d in ("ui", "extension", "assets")))
chk("spec 带 aria2c.exe 且缺失时告警", "aria2c.exe" in SPEC and "vendor/aria2c.exe 不存在" in SPEC)
chk("spec collect_all('imageio_ffmpeg')", "collect_all('imageio_ffmpeg')" in SPEC)
chk("REQUIRED_DATA 在源码树里都存在", all(PS.find_resource(r) for r, _ in PS.REQUIRED_DATA),
    [r for r, _ in PS.REQUIRED_DATA if not PS.find_resource(r)])

# 4 check_modules：真模块过，假模块报名字
it = PS.check_modules(["json", "core.hls"], win=False)
chk("check_modules 真模块 → OK", it[0]["ok"] and len(it) == 1, it)
it = PS.check_modules(["json", "sdl_no_such_mod_xyz"], win=False)
chk("check_modules 缺模块 → FAIL 且点名", (not it[0]["ok"]) and "sdl_no_such_mod_xyz" in it[0]["detail"], it)

# 5 中文路径
tmp = tempfile.mkdtemp(prefix="p4t_")
try:
    base = os.path.join(tmp, "数据 目录")
    os.makedirs(base)
    i = PS.check_cn_dir(base)
    chk("中文路径可写 → OK 且不留垃圾", i["ok"] and os.listdir(base) == [], (i, os.listdir(base)))
    blocker = os.path.join(tmp, "是文件")
    open(blocker, "w").close()
    i = PS.check_cn_dir(blocker)                 # 父路径是文件 → 建目录必失败
    chk("中文路径不可写 → FAIL", not i["ok"], i)

    # 6 资源与 aria2c：冻结形态模拟（_MEIPASS 优先）
    i = PS.check_resources([("no/such/资源.png", "假资源")])
    chk("缺资源 → FAIL", not i[0]["ok"], i)
    chk("aria2c 找不到 → FAIL", not PS.check_aria2c(None)["ok"])
    mp = os.path.join(tmp, "_internal")
    os.makedirs(mp)
    old = (getattr(sys, "frozen", None), getattr(sys, "_MEIPASS", None))
    sys.frozen, sys._MEIPASS = True, mp
    try:
        chk("冻结形态 resource_dirs 首位是 _MEIPASS", PS.resource_dirs()[0] == mp, PS.resource_dirs())
        fake = os.path.join(tmp, "外部aria2c")
        open(fake, "w").close()
        chk("冻结形态 aria2c 不在包里 → FAIL（不许拿外部的蒙混）", not PS.check_aria2c(fake)["ok"])
        an = "aria2c.exe" if sys.platform == "win32" else "aria2c"
        open(os.path.join(mp, an), "w").close()
        chk("冻结形态 aria2c 在 _MEIPASS → OK", PS.check_aria2c(os.path.join(mp, an))["ok"])
    finally:
        if old[0] is None:
            del sys.frozen
        else:
            sys.frozen = old[0]
        if old[1] is None:
            del sys._MEIPASS
        else:
            sys._MEIPASS = old[1]
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# 7 RPC 停止判定：端口还在听 → FAIL
s = socket.socket()
s.bind(("127.0.0.1", 0))
s.listen(64)
lp = s.getsockname()[1]


def _acc():
    while True:
        try:
            c, _ = s.accept()
            c.close()
        except OSError:
            return


import threading  # noqa: E402
threading.Thread(target=_acc, daemon=True).start()
chk("RPC 停止：端口仍监听 → FAIL", not PS.check_rpc_down(lp, "/nonexistent/rt.json", wait=0.3)["ok"])
s.close()
chk("RPC 停止：端口关 + runtime 无 → OK", PS.check_rpc_down(lp, "/nonexistent/rt.json", wait=0.3)["ok"])

# 8 建议项/致命项 + 汇总格式 + CI 解析往返
items = PS.merge_api_items([{"name": "ffmpeg", "ok": False}, {"name": "aria2 兼容端口 6800", "ok": False},
                            {"name": "aria2c", "ok": True}, {"name": "yt-dlp", "ok": True}])
sm = PS.summarize(items)
chk("ffmpeg/6800 只警告不判失败", sm["ok"] and sm["warn"] == 2 and sm["total"] == 2, sm)
chk("--strict 下 ffmpeg 变致命", not PS.summarize(PS.merge_api_items([{"name": "ffmpeg", "ok": False}], strict=True))["ok"])
lines = PS.format_lines(items, sm, {"ready_s": 1.2})
txt = "\n".join(lines)
p = XC.parse_selftest(txt)
chk("SELFTEST x/y 行可被 CI 解析", p["verdict"] == "PASS" and (p["x"], p["y"]) == (2, 2) and p["json"]["ready_s"] == 1.2, p)
chk("CI 判定：条目太少 → 不过", not XC.judge_selftest(0, p)[0], XC.judge_selftest(0, p))

# 9 emit：stdout=None（窗口版 exe）也不炸，文件照写
tf = os.path.join(tempfile.gettempdir(), "p4_emit_测试.txt")
so = sys.stdout
sys.stdout = None
try:
    PS.emit(["SELFTEST PASS 1/1"], tf)
finally:
    sys.stdout = so
chk("stdout=None 时 emit 写文件", open(tf, encoding="utf-8").read().strip() == "SELFTEST PASS 1/1")
os.remove(tf)

# 10 exe 入口分流在单实例锁/Tk 之前
vd = open(os.path.join(ROOT, "video_downloader.py"), encoding="utf-8").read()
mi = vd.index("\ndef main():")
body = vd[mi:mi + 3000]
chk("video_downloader.main 先分流 --core/--selftest 再上单实例锁",
    '("--core", "--selftest")' in body and body.index('"--selftest")') < body.index("ensure_single_instance()"))

# 11 端到端：经 exe 入口（video_downloader.py --selftest）真跑，PASS x/y 全过，且关键条目齐
env = dict(os.environ)
of = os.path.join(tempfile.gettempdir(), "p4_selftest_vd.txt")
r = subprocess.run([sys.executable, os.path.join(ROOT, "video_downloader.py"), "--selftest", "--selftest-out", of],
                   capture_output=True, timeout=120, cwd=ROOT, env=env)
out = open(of, encoding="utf-8").read() if os.path.isfile(of) else ""
ok, why = XC.judge_selftest(r.returncode, XC.parse_selftest(out))
chk("video_downloader.py --selftest 退出 0 且 CI 判定通过（%s）" % why, ok, out[-800:] + r.stderr.decode("utf-8", "replace")[-400:])

# 12 失败要真红：PATH 里拿掉 aria2c → 退出码 1 + SELFTEST FAIL
dirs = [d for d in env.get("PATH", "").split(os.pathsep) if d and not os.path.exists(os.path.join(d, "aria2c"))]
env2 = dict(env, PATH=os.pathsep.join(dirs))
env2.pop("SNIFFDL_ARIA2C", None)
r = subprocess.run([sys.executable, os.path.join(ROOT, "sniffdl_core.py"), "--selftest", "--selftest-out", of],
                   capture_output=True, timeout=120, cwd=ROOT, env=env2)
out = open(of, encoding="utf-8").read() if os.path.isfile(of) else ""
if "aria2c 可找到  /" in out:
    chk("缺 aria2c 判红（本机常见位置仍扫到 aria2c，跳过）", True)
else:
    chk("缺 aria2c → 退出码 1 且 SELFTEST FAIL", r.returncode == 1 and "SELFTEST FAIL" in out, (r.returncode, out[-500:]))
try:
    os.remove(of)
except OSError:
    pass

# 13 CI 验收脚本源码代跑：selftest + --core 冷启动 READY + 真下载 sha256 + 正常退出 + summary 有体积/耗时
co = tempfile.mkdtemp(prefix="p4_ci_")
sumf = os.path.join(co, "step_summary.md")
r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "exe_ci_check.py"), "--exe",
                    os.path.join(ROOT, "video_downloader.py"), "--dist", os.path.join(ROOT, "core"),
                    "--out", os.path.join(co, "out"), "--summary", sumf],
                   capture_output=True, timeout=180, cwd=ROOT, env=env)
md = open(sumf, encoding="utf-8").read() if os.path.isfile(sumf) else ""
chk("exe_ci_check 源码代跑全过（退出 0）", r.returncode == 0, r.stdout.decode("utf-8", "replace")[-800:])
chk("step summary 含目录大小 + 冷启动 READY 耗时 + sha256 结果",
    "目录大小" in md and re.search(r"冷启动到 READY.*\| [\d.]+s \|", md) and "真实下载 + sha256 | 通过" in md, md[:600])
shutil.rmtree(co, ignore_errors=True)

print("%d/%d" % (OK, OK + BAD))
sys.exit(1 if BAD else 0)
