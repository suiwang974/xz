# -*- coding: utf-8 -*-
"""P1a 验收(真 Electron)：起 pet_electron 桌宠窗 → 轮询 /api/ping bound →
/api/debug 确认 Live2D ready → 打 react/bubble → 截屏证明桌面透明显示 → 收尾杀进程。
用法: python tools/pet_electron_verify.py
退出码 0=全绿。绝不硬杀，terminate 自己起的 electron。"""
import functools
print = functools.partial(print, flush=True)

import json
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PET_DIR = os.path.join(ROOT, "pet_electron")
ELECTRON = os.path.join(PET_DIR, "node_modules", "electron", "dist", "electron.exe")
OUT = os.path.join(ROOT, "outputs")
os.makedirs(OUT, exist_ok=True)
PORT = 6801
TOKEN = "tkv-verify"

results = []
def check(name, cond, detail=""):
    ok = bool(cond)
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (("  | " + str(detail)) if detail else ""))

def api(path, body=None):
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (PORT, path),
        data=json.dumps(body or {}, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=3).read().decode("utf-8"))

def main():
    if not os.path.exists(ELECTRON):
        check("electron.exe 存在", False, ELECTRON)
        return 1
    check("electron.exe 存在", True)

    logf = open(os.path.join(OUT, "pet_electron_run.log"), "w", encoding="utf-8")
    proc = subprocess.Popen(
        [ELECTRON, PET_DIR, "--port", str(PORT), "--token", TOKEN],
        cwd=ROOT, stdout=logf, stderr=subprocess.STDOUT)
    print("electron pid", proc.pid)

    try:
        # 1) 等 ping 通 + bound
        bound = False
        ping = None
        t0 = time.time()
        while time.time() - t0 < 75:
            if proc.poll() is not None:
                check("electron 进程存活", False, "提前退出 rc=%s" % proc.poll())
                break
            try:
                ping = api("/api/ping")
                if ping.get("bound"):
                    bound = True
                    break
            except Exception:
                pass
            time.sleep(0.5)
        check("/api/ping 返回 ok", bool(ping and ping.get("ok")), ping)
        check("桌宠 bound(Live2D 就绪)", bound, ping)

        if bound:
            # 2) debug 确认模型 ready
            dbg = api("/api/debug")
            jsd = dbg.get("js")
            try:
                jsd = json.loads(jsd) if isinstance(jsd, str) else jsd
            except Exception:
                pass
            check("/api/debug ready", bool(isinstance(jsd, dict) and jsd.get("ready") and jsd.get("model")), jsd)

            # 3) 驱动反应 + 气泡
            for r in ("busy", "done", "happy"):
                try:
                    check("react(%s) ok" % r, api("/api/react", {"name": r}).get("ok"))
                except Exception as e:
                    check("react(%s) ok" % r, False, repr(e))
            try:
                check("bubble ok", api("/api/bubble", {"text": "深海捞宝成功！咕嘟～", "ms": 4000}).get("ok"))
            except Exception as e:
                check("bubble ok", False, repr(e))

            # 4) 截屏证明桌面渲染（全屏抓取，桌宠在右下角）
            time.sleep(1.2)
            try:
                from PIL import ImageGrab
                img = ImageGrab.grab(all_screens=True)
                shot = os.path.join(OUT, "pet_electron_desktop.png")
                img.save(shot)
                check("桌面截图已生成", os.path.exists(shot), shot)
            except Exception as e:
                check("桌面截图已生成", False, repr(e))
    finally:
        try:
            api("/api/quit", {})
        except Exception:
            pass
        # Electron 会派生多个子进程（GPU/renderer），terminate 只杀父进程会留孤儿
        # → 占着单实例锁 + 端口，下次起不来。用 taskkill /T 整棵进程树清干净。
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, timeout=10)
        except Exception:
            pass
        try:
            proc.wait(timeout=8)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        logf.close()

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print("\n==== P1a 真Electron验收: %d/%d PASS ====" % (passed, total))
    for name, ok, detail in results:
        if not ok:
            print("  FAILED:", name, "|", detail)
    return 0 if passed == total else 1

if __name__ == "__main__":
    sys.exit(main())
