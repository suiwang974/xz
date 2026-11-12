# -*- coding: utf-8 -*-
"""为大肥鱼 Tk 主界面生成整套内嵌素材：AVATAR/CHIP/SIDE/CHIBI/TILE（透明 PNG + .b64）。
从修复后的 live2d/pet.html 无头渲染干净全身，裁切派生。供 video_downloader.py 注入 feiyu 主题用。
用法: python tools/gen_feiyu_tk_art.py"""
import functools, http.server, socketserver, threading, socket, os, base64, math
from playwright.sync_api import sync_playwright
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE2D = os.path.join(ROOT, "live2d"); ASSETS = os.path.join(ROOT, "assets")
os.makedirs(ASSETS, exist_ok=True)

def serve(d, p):
    h = functools.partial(http.server.SimpleHTTPRequestHandler, directory=d)
    class Q(socketserver.ThreadingTCPServer): allow_reuse_address = True; daemon_threads = True
    s = Q(("127.0.0.1", p), h); threading.Thread(target=s.serve_forever, daemon=True).start(); return s

def b64(path):
    return base64.b64encode(open(path, "rb").read()).decode()

def save_pair(im, name):
    png = os.path.join(ASSETS, name + ".png"); im.save(png)
    open(os.path.join(ASSETS, name + ".b64"), "w").write(b64(png))
    print("  ", name, im.size)

def main():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    httpd = serve(LIVE2D, port); url = "http://127.0.0.1:%d/pet.html" % port
    W, H = 720, 1000
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        ctx = b.new_context(viewport={"width": W, "height": H})
        pg = ctx.new_page(); pg.goto(url, wait_until="load")
        pg.wait_for_function("()=>!!window.__petModelReady", timeout=60000)
        pg.evaluate("try{SniffDLPet.react('idle');SniffDLPet.bubble('',0);}catch(e){}")
        pg.wait_for_timeout(1600)
        pg.screenshot(path=os.path.join(ASSETS, "_feiyu_full.png"), omit_background=True)
        b.close()
    httpd.shutdown()

    im = Image.open(os.path.join(ASSETS, "_feiyu_full.png")).convert("RGBA")
    bb = im.getbbox(); im = im.crop(bb) if bb else im
    w, h = im.size; print("full body:", w, "x", h)
    # SIDE：全身（保持比例，限高 ~720）
    side = im.copy()
    if side.height > 720:
        r = 720 / side.height; side = side.resize((int(side.width * r), 720), Image.LANCZOS)
    save_pair(side, "feiyu_side")
    # CHIBI：全身缩到 ~190 高
    chibi = im.copy(); r = 190 / chibi.height
    chibi = chibi.resize((max(1, int(chibi.width * r)), 190), Image.LANCZOS)
    save_pair(chibi, "feiyu_chibi")
    # AVATAR：头肩方图 256
    face = im.crop((0, 0, w, int(h * 0.42))); fw, fh = face.size; sq = min(fw, fh)
    face = face.crop(((fw - sq) // 2, (fh - sq) // 2, (fw - sq) // 2 + sq, (fh - sq) // 2 + sq)).resize((256, 256), Image.LANCZOS)
    save_pair(face, "feiyu_avatar")
    save_pair(face.resize((64, 64), Image.LANCZOS), "feiyu_chip")
    # TILE：程序生成深海波纹小图（宝蓝→青宝石，透明底）
    T = 96; tile = Image.new("RGBA", (T, T), (238, 243, 251, 0)); d = ImageDraw.Draw(tile)
    for k, yy in enumerate((24, 52, 80)):
        for xx in range(0, T, 4):
            y = yy + 6 * math.sin(xx / 12.0 + k)
            col = (62, 111, 208, 26) if k % 2 == 0 else (56, 198, 217, 26)
            d.point((xx, y), fill=col)
    save_pair(tile, "feiyu_tile")
    print("OK")

if __name__ == "__main__":
    main()
