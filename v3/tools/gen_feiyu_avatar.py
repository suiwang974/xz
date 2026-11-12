# -*- coding: utf-8 -*-
"""从真·Live2D 大肥鱼渲染一张干净头像（默认表情、无气泡），裁头部方形 →
assets/feiyu_avatar.png（256x256）+ assets/feiyu_chip.png（64x64）。供后端主题/插件/webview 用。
用法: python tools/gen_feiyu_avatar.py"""
import functools
print = functools.partial(print, flush=True)
import http.server, socketserver, threading, socket, os, sys, base64, io
from playwright.sync_api import sync_playwright
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE2D = os.path.join(ROOT, "live2d")
ASSETS = os.path.join(ROOT, "assets")
os.makedirs(ASSETS, exist_ok=True)

def serve(directory, port):
    h = functools.partial(http.server.SimpleHTTPRequestHandler, directory=directory)
    class Q(socketserver.ThreadingTCPServer):
        allow_reuse_address = True; daemon_threads = True
    httpd = Q(("127.0.0.1", port), h)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd

def main():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    httpd = serve(LIVE2D, port)
    url = "http://127.0.0.1:%d/pet.html" % port
    W, H = 620, 900
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--use-gl=angle", "--use-angle=swiftshader",
                                    "--enable-unsafe-swiftshader"])
        # 深色底便于裁切透明；omit_background 出 PNG 带 alpha
        ctx = b.new_context(viewport={"width": W, "height": H})
        pg = ctx.new_page()
        pg.goto(url, wait_until="load")
        pg.wait_for_function("() => !!window.__petModelReady", timeout=60000)
        # 回到默认表情、清掉气泡，静置一拍让物理稳定
        pg.evaluate("try{SniffDLPet.react('idle');SniffDLPet.bubble('',0);}catch(e){}")
        pg.wait_for_timeout(1500)
        shot = os.path.join(ASSETS, "_feiyu_full.png")
        pg.screenshot(path=shot, omit_background=True)
        b.close()
    httpd.shutdown()

    im = Image.open(shot).convert("RGBA")
    # 自动裁到非透明包围盒
    bbox = im.getbbox()
    if bbox:
        im = im.crop(bbox)
    w, h = im.size
    print("cropped model box:", w, "x", h)
    # 头像取上半部分（头肩）：宽居中，高取顶部 ~46%
    side = w
    top_h = int(h * 0.46)
    face = im.crop((0, 0, w, top_h))
    fw, fh = face.size
    sq = min(fw, fh)
    # 方形居中裁
    face = face.crop(((fw - sq) // 2, (fh - sq) // 2, (fw - sq) // 2 + sq, (fh - sq) // 2 + sq))
    face = face.resize((256, 256), Image.LANCZOS)
    avatar = os.path.join(ASSETS, "feiyu_avatar.png")
    face.save(avatar)
    face.resize((64, 64), Image.LANCZOS).save(os.path.join(ASSETS, "feiyu_chip.png"))
    b64 = base64.b64encode(open(avatar, "rb").read()).decode()
    open(os.path.join(ASSETS, "feiyu_avatar.b64"), "w").write("data:image/png;base64," + b64)
    print("avatar ->", avatar, "(256x256) b64len", len(b64))
    return 0

if __name__ == "__main__":
    sys.exit(main())
