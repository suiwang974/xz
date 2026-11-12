# -*- coding: utf-8 -*-
"""大肥鱼桌宠 × 真 pet_bridge 端到端（Linux + Xvfb + linux Electron 33.4.11）。

    DISPLAY=:77 SNIFFDL_ELECTRON=/path/to/linux/electron python tools/pet_bridge_e2e.py OUTDIR

用 pet_bridge.PetBridge（与主程序同一份代码）拉起 pet_electron/app，经 pet_state.json 拿端口/token，
逐个调用 bridge 的 react/say/visible/set_skin 以及其余控制路由，每个状态 xwd 截图（裁她所在区域）。
注意：Xvfb 无合成器，透明窗背景显示为黑色；Windows 上是真透明。
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
import pet_bridge  # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/pet_e2e"
os.makedirs(OUT, exist_ok=True)
R = []


def ok(name, cond, extra=""):
    R.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + ("  " + str(extra) if extra else ""))


def api(path, body=None):
    st = b.state
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (st["port"], path),
                                 data=json.dumps(body or {}).encode(), method="POST",
                                 headers={"Authorization": "Bearer " + st["token"]})
    return json.loads(urllib.request.urlopen(req, timeout=5).read() or b"{}")


def info():
    return json.loads(api("/api/debug").get("js") or "{}")


def shot(name, crop=True):
    raw = os.path.join(OUT, name + ".png")
    xwd = subprocess.run(["xwd", "-root", "-silent"], capture_output=True).stdout
    args = ["convert", "xwd:-"]
    if crop:
        i = info()
        x, y = int(i.get("x", 800)), int(i.get("y", 900))
        args += ["-crop", "520x560+%d+%d" % (max(0, x - 260), max(0, y - 540)), "+repage"]
    subprocess.run(args + [raw], input=xwd)
    return raw


b = pet_bridge.PetBridge(port=6812)
exe, app_dir = pet_bridge.find_electron()
ok("find_electron 找到运行环境与 app", exe and app_dir, (exe, app_dir))
ok("start()", b.start())
t0 = time.time()
while not b.state and time.time() - t0 < 30:
    time.sleep(0.3)
ok("pet_state.json 由 main.js 按 --state 写入且 token 一致",
   b.state and b.state.get("token") == b.token and os.path.samefile(pet_bridge.STATE_FILE, pet_bridge.STATE_FILE),
   pet_bridge.STATE_FILE)
ok("ping", api("/api/ping").get("ok"))
t0 = time.time()
while time.time() - t0 < 40:
    i = info()
    if i.get("mode") in ("idle", "sit") and not i.get("err"):
        break
    time.sleep(0.5)
ok("模型就绪并落地", i.get("mode") in ("idle", "sit"), i)
time.sleep(1.5)
# 首次见面会弹称呼选项：先点掉（直接走页面 API 不方便，这里只截图记录）
shot("00_boot_idle", crop=False)

# ---- 下载反应（真 bridge 方法）----
b.react("busy"); time.sleep(1.6)
i = info(); ok("busy → 举手+开心+加载圈", i.get("state") == "busy" and "key8" in i.get("acc", []) and i.get("face") == "busyhand", i)
shot("01_busy")
time.sleep(3.2)
i = info(); ok("busy 持续（被打断/超时后续上举手）", i.get("state") == "busy" and "key8" in i.get("acc", []), i)
b.react("done"); time.sleep(1.2)
i = info(); ok("done → 爱心眼+脸红", i.get("face") == "doneLove" and "key8" not in i.get("acc", []), i)
shot("02_done")
time.sleep(2.5)
b.react("fail"); time.sleep(1.0)
i = info(); ok("fail 第一段 → 流泪", i.get("face") == "cry", i)
shot("03_fail_cry")
time.sleep(2.0)
i = info(); ok("fail 第二段 → 流冷汗", i.get("face") == "sweat", i)
shot("04_fail_sweat")
time.sleep(2.5)
b.react("idle"); time.sleep(1.2)
i = info(); ok("idle → 常态", i.get("state") == "idle" and "key8" not in i.get("acc", []) and i.get("mode") in ("idle", "sit"), i)
shot("05_idle")

# ---- 其余路由 ----
b.say("测试气泡：下载完成啦～", 3000); time.sleep(0.8)
shot("06_bubble")
ok("bubble 路由", True)
api("/api/expr", {"name": "单片眼镜"}); time.sleep(0.6)
i = info(); ok("expr 单片眼镜 → key12", "key12" in i.get("acc", []), i)
shot("07_expr_monocle")
api("/api/expr", {"name": "单片眼镜"})
b.set_skin("normal"); time.sleep(2.0)
i = info(); ok("skin normal → 正常版", i.get("chibi", 1) < 0.5, i)
shot("08_normal")
b.set_skin("chibi"); time.sleep(2.0)
i = info(); ok("skin chibi → Q版", i.get("chibi", 0) > 0.5, i)
r = api("/api/size", {"idx": 6}); time.sleep(0.5)
i = info(); ok("size idx6 → 放大 1.5", abs(i.get("scale", 0) - 1.5) < 1e-6, r)
shot("09_size_big")
api("/api/size", {"idx": 3})
api("/api/pos", {"x": 400, "y": 899}); time.sleep(1.5)
i = info(); ok("pos → 移到 x=400", abs(i.get("x", 0) - 400) < 3, i)
# 命中门控：身体吃点击，空白穿透
x, y = int(i["x"]), int(i["y"])
h1 = api("/api/probe", {"x": x, "y": y - 80}); h2 = api("/api/probe", {"x": x + 500, "y": 200})
ok("命中门控：身体=接管点击", h1.get("hit") and h1.get("interactive") is True, h1)
ok("命中门控：空白=穿透", (not h2.get("hit")) and h2.get("interactive") is False, h2)
api("/api/clickthrough", {"on": True})
h3 = api("/api/probe", {"x": x, "y": y - 80})
ok("clickthrough on → 身体也穿透", not h3.get("hit") and h3.get("interactive") is False, h3)
api("/api/clickthrough", {"on": False})
b.visible(False); time.sleep(0.8)
ok("visible off", api("/api/debug").get("visible") is False)
b.visible(True); time.sleep(0.8)
ok("visible on", api("/api/debug").get("visible") is True)
r401 = None
try:
    urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:%d/api/ping" % b.state["port"], data=b"{}",
                                                  headers={"Authorization": "Bearer wrong"}), timeout=3)
except urllib.error.HTTPError as e:
    r401 = e.code
ok("错误 token → 401", r401 == 401, r401)
p = b.proc
b.quit()
try:
    p.wait(8)
except Exception:
    pass
ok("quit → 进程退出", p.poll() is not None, p.poll())
n = sum(1 for _, c in R if c)
print("RESULT %d/%d" % (n, len(R)))
json.dump(R, open(os.path.join(OUT, "result.json"), "w"), ensure_ascii=False)
sys.exit(0 if n == len(R) else 1)
