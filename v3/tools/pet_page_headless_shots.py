# -*- coding: utf-8 -*-
"""无头 Chromium 截桌面版大肥鱼各状态（真透明背景 PNG）。
    python tools/pet_page_headless_shots.py OUTDIR [chromium可执行文件]
把 pet_electron/app 用本地 HTTP 端起来，sniffpet://app/ 前缀替换成 http://127.0.0.1:PORT/。"""
import asyncio, functools, http.server, os, sys, threading
from playwright.async_api import async_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "pet_electron", "app")
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/pet_headless"
EXE = sys.argv[2] if len(sys.argv) > 2 else None
os.makedirs(OUT, exist_ok=True)


class H(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def translate_path(self, path):
        if path.split("?")[0] == "/model/desktop.model3.json":
            path = "/model/%E5%A4%A7%E8%82%A5%E9%B1%BC.desktop.model3.json"
        return super().translate_path(path)


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(H, directory=APP))
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:%d/" % srv.server_address[1]


async def main():
    async with async_playwright() as p:
        br = await p.chromium.launch(executable_path=EXE, args=["--enable-unsafe-swiftshader", "--use-gl=swiftshader"])
        pg = await br.new_page(viewport={"width": 900, "height": 700})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))

        async def fix(route):
            r = await route.fetch()
            body = (await r.text()).replace("sniffpet://app/", BASE)
            await route.fulfill(response=r, body=body)
        await pg.route(BASE + "page/desk.html", fix)
        await pg.add_init_script("localStorage.setItem('dfy_pet_v1', JSON.stringify({nick:'主人',firstSeen:Date.now()-864e5,lastSeen:Date.now(),aff:60,chibi:1,stats:{},ach:{},days:{},remind:{},outfit:{long:0,chest:0,tail:1}}))")
        await pg.goto(BASE + "page/desk.html")
        await pg.wait_for_function("window.__petModelReady===true", timeout=60000)
        await pg.wait_for_function("['idle','sit'].includes(__pet.P.mode)", timeout=20000)
        await pg.wait_for_timeout(2500)
        await pg.evaluate("document.getElementById('bubble').classList.remove('show');document.getElementById('chips').style.display='none'")

        async def shot(name):
            r = await pg.evaluate("(()=>{const r=__pet.rect();return [r.left,r.top,r.w,r.h]})()")
            clip = {"x": max(0, r[0] - 70), "y": max(0, r[1] - 90), "width": r[2] + 140, "height": r[3] + 100}
            await pg.screenshot(path=os.path.join(OUT, name + ".png"), omit_background=True, clip=clip)
        for name, react, wait in (("h1_busy", "busy", 1500), ("h2_done", "done", 1300),
                                  ("h3_fail_cry", "fail", 1100), ("h4_fail_sweat", None, 1400),
                                  ("h5_idle", "idle", 4000)):
            if react:
                await pg.evaluate("SniffDLPet.react(%r)" % react)
            await pg.wait_for_timeout(wait)
            await shot(name)
            print(name, await pg.evaluate("JSON.stringify(SniffDLPet.info())"))
        await pg.evaluate("SniffDLPet.setChibi(false)")
        await pg.wait_for_timeout(2500)
        await shot("h6_normal_idle")
        for name, react, wait in (("h7_normal_busy", "busy", 2000), ("h8_normal_fail_cry", "fail", 1100), ("h9_normal_fail_sweat", None, 1400)):
            if react:
                await pg.evaluate("SniffDLPet.react(%r)" % react)
            await pg.wait_for_timeout(wait)
            await shot(name)
        print("pageerrors", errs)
        await br.close()

asyncio.run(main())
