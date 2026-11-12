# -*- coding: utf-8 -*-
"""P1a 验收(页内)：无头 Chromium 加载 live2d/pet.html，断言真 Live2D 模型加载 +
SniffDLPet API（react/expr/bubble/focus）不报错、模型有尺寸、视线/动作可驱动。
用法: python tools/pet_page_headless_verify.py
退出码 0=全绿。"""
import functools
print = functools.partial(print, flush=True)

import http.server
import socketserver
import threading
import os
import sys
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE2D = os.path.join(ROOT, "live2d")
OUT = os.path.join(ROOT, "outputs")
os.makedirs(OUT, exist_ok=True)

results = []
def check(name, cond, detail=""):
    ok = bool(cond)
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (("  | " + str(detail)) if detail else ""))

def serve(directory, port):
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=directory)
    class Q(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True
    httpd = Q(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd

def main():
    # 找一个空闲端口
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    httpd = serve(LIVE2D, port)
    url = "http://127.0.0.1:%d/pet.html" % port
    print("serving", LIVE2D, "at", url)

    with sync_playwright() as p:
        browser = p.chromium.launch(args=[
            "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader",
            "--allow-file-access-from-files"])
        ctx = browser.new_context(viewport={"width": 520, "height": 780})
        page = ctx.new_page()
        console_err = []
        page.on("console", lambda m: console_err.append(m.text) if m.type in ("error",) else None)
        page.goto(url, wait_until="load")
        # 等待模型就绪（大 moc3 首次解码可能慢，给足 60s）
        try:
            page.wait_for_function("() => !!window.__petModelReady", timeout=60000)
        except Exception as e:
            check("模型加载完成 __petModelReady", False, repr(e))
        err = page.evaluate("window.__err || ''")
        check("无加载致命错误 __err 为空", (err == ""), "err=%r" % err)
        check("window.__ok 已置位", page.evaluate("!!window.__ok"))

        # SniffDLPet 接口存在性
        api = page.evaluate("""() => {
            var s = window.SniffDLPet || {};
            return {has:!!window.SniffDLPet, bubble:typeof s.bubble, react:typeof s.react,
                    expr:typeof s.expr, focus:typeof s.focus, isReady:typeof s.isReady,
                    ringToggle:typeof s.ringToggle};
        }""")
        check("SniffDLPet 已挂载", api.get("has"), api)
        for k in ("bubble", "react", "expr", "focus", "isReady", "ringToggle"):
            check("接口 %s 为函数" % k, api.get(k) == "function", api.get(k))

        check("isReady()==True", page.evaluate("window.SniffDLPet.isReady()"))

        # 模型有可见尺寸（PIXI 渲染出来了）
        dims = page.evaluate("""() => {
            var cv=document.getElementById('cv');
            return {w:cv?cv.width:0, h:cv?cv.height:0};
        }""")
        check("canvas 有尺寸", dims.get("w", 0) > 0 and dims.get("h", 0) > 0, dims)

        # 逐个情绪反应不抛错
        for name in ("busy", "done", "happy", "fail", "surprise", "shy", "idle", "drag"):
            threw = page.evaluate("""(n) => { try { window.SniffDLPet.react(n); return ''; }
                catch(e){ return String(e); } }""", name)
            check("react('%s') 不抛错" % name, threw == "", threw)

        # 气泡 + 口型
        page.evaluate("window.SniffDLPet.bubble('捞上来啦！咕嘟～', 3000)")
        bub = page.evaluate("""() => { var b=document.getElementById('bub');
            return {text:b?b.textContent:'', op:b?getComputedStyle(b).opacity:'0'}; }""")
        check("bubble 文本已写入", bub.get("text", "").find("捞上来") >= 0, bub)

        # 表情切换（走真实 expressionManager）
        expr_ok = page.evaluate("""() => { try { window.SniffDLPet.expr('爱心眼.exp3'); return ''; }
            catch(e){ return String(e); } }""")
        check("expr('爱心眼.exp3') 不抛错", expr_ok == "", expr_ok)

        # 视线跟随（focus 内部走 model.focus 夹取）
        focus_ok = page.evaluate("""() => { try { window.SniffDLPet.focus(120, 260); return ''; }
            catch(e){ return String(e); } }""")
        check("focus() 不抛错", focus_ok == "", focus_ok)

        # 圆盘菜单开/关不抛错
        ring_ok = page.evaluate("""() => { try { window.SniffDLPet.ringToggle();
            var r=document.getElementById('petRing'); var shown=r&&r.style.display!=='none';
            window.SniffDLPet.ringClose(); return JSON.stringify({shown:shown}); }
            catch(e){ return 'ERR:'+e; } }""")
        check("圆盘可打开/关闭", isinstance(ring_ok, str) and ring_ok.find("ERR") < 0
              and '"shown":true' in ring_ok, ring_ok)

        page.wait_for_timeout(800)
        page.screenshot(path=os.path.join(OUT, "pet_headless_verify.png"),
                        omit_background=False)
        print("screenshot ->", os.path.join(OUT, "pet_headless_verify.png"))

        # 关键控制台错误（过滤 swiftshader 噪声）
        real_err = [e for e in console_err
                    if "swiftshader" not in e.lower() and "GPU" not in e
                    and "Automatic fallback" not in e and "WebGL" not in e]
        check("无致命控制台 error", len(real_err) == 0, real_err[:3])

        browser.close()
    httpd.shutdown()

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print("\n==== P1a 页内验收: %d/%d PASS ====" % (passed, total))
    for name, ok, detail in results:
        if not ok:
            print("  FAILED:", name, "|", detail)
    return 0 if passed == total else 1

if __name__ == "__main__":
    sys.exit(main())
