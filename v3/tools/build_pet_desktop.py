# -*- coding: utf-8 -*-
"""把 /workspace/work/dfy 的大肥鱼预览改成「桌面版」页面，写进 pet_electron/app/。

    python tools/build_pet_desktop.py [DFY_DIR]

改动（全部是对预览源码的字符串替换，替换不到就报错，不静默）：
  - fast=false（真实时间节奏），floorY 不减 44px（任务栏由 Windows 自己画）
  - 禁用窗台：ledge() 返回屏外、jumpToLedge 直接 toIdle、菜单去掉「去窗台/下来」、日常随机里窗台权重 0
  - 隐藏 #taskbar #win #hud #fabs #help-btn #loading；舞台/页面背景透明
  - window.__pet 追加 pose/toIdle/particle/faceName/T 以及 setScale（尺寸档）
  - 新增 FACES.busyhand（举手+开心）、doneLove（爱心眼+脸红）
  - 模型从 sniffpet://app/model/大肥鱼.desktop.model3.json 加载；纹理 desktop_tex/t0-t4.webp（2048）
"""
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DFY = sys.argv[1] if len(sys.argv) > 1 else "/workspace/work/dfy"
APP = os.path.join(HERE, "pet_electron", "app")
PAGE = os.path.join(APP, "page")
MODEL = os.path.join(APP, "model")


def rep(s, a, b, n=1):
    c = s.count(a)
    if c < 1 or (n and c != n):
        raise SystemExit("替换失败（出现 %d 次）：%r" % (c, a[:80]))
    return s.replace(a, b)


# ---------- page/desk.html ----------
html = open(os.path.join(DFY, "index.html"), encoding="utf-8").read()
html = rep(html, "<title>大肥鱼 桌宠预览</title>", "<title>大肥鱼 · SniffDL 桌宠</title>")
html = rep(html, '<div data-a="ledge">去窗台</div><div data-a="down">下来</div>', "")
# 库/脚本改为同源相对路径（sniffpet://app/page/…）
html = rep(html, '<script src="lib/cubismcore.js"></script>', '<script src="lib/cubismcore.js"></script>')
html = rep(html, '<script src="js/main.js"></script>',
           '<script>window.__DFY_MODEL__="sniffpet://app/model/desktop.model3.json";</script>\n'
           '<script src="js/main.js"></script>\n<script src="desk.js"></script>')
desk_css = """
<style id="desk">
/* 桌面版：透明全屏层，只画她本人 */
html,body{background:transparent!important}
#stage{background:transparent!important}
#taskbar,#win,#hud,#fabs,#help-btn,#loading{display:none!important}
#menu div[data-a=hide],#menu div[data-a=quit]{color:#5a6785}
#menu div[data-a=quit]{border-top:1px solid #eef0f6;margin-top:2px}
</style>
"""
html = rep(html, "</head>", desk_css + "</head>")

# ---------- page/js/main.js ----------
js = open(os.path.join(DFY, "js", "main.js"), encoding="utf-8").read()
js = rep(js, "fast = true,", "fast = false,")
js = rep(js, "function floorY() { return stage.clientHeight - 44; }",
         "function floorY() { return stage.clientHeight; }")
js = rep(js, "function ledge() { const r = win.getBoundingClientRect(); return { y: r.top, l: r.left + 26, r: r.right - 26 }; }",
         "function ledge() { return { y: -1e9, l: 1e9, r: -1e9 }; }   // 桌面版禁用窗台")
js = rep(js, "function jumpToLedge() {\n", "function jumpToLedge() { toIdle(); return;   // 桌面版禁用窗台\n")
js = rep(js, "[P.onLedge || L2D.chibiLevel() < .5 ? 0 : 8, () => jumpToLedge()],",
         "[0, () => jumpToLedge()],")
# 尺寸档：boxFor 乘一个全局倍率（控制 API /api/size）
js = rep(js, "  const h = lerp(nh, ch, k), w = h * lerp(.62, .9, k);",
         "  const S = window.__petScale || 1;\n  const h = lerp(nh, ch, k) * S, w = h * lerp(.62, .9, k);")
# 下载反应表情
js = rep(js, "  focus: { eyeOpen: 1.05,",
         "  busyhand: { Movement2: 1, EyeSmile: 1, MouthSmile_L: 1, MouthSmile_R: 1, ParamMouthForm: 1, ParamBrowLY: .4, ParamBrowRY: .4 },\n"
         "  doneLove: { key4: 1, key7: 1, EyeSmile: .4, MouthSmile_L: 1, MouthSmile_R: 1, ParamMouthForm: 1 },\n"
         "  focus: { eyeOpen: 1.05,")
js = rep(js, "window.__pet = { rect: charRect, region,",
         "window.__pet = { pose, toIdle, particle, faceName, T, relayout, closeMenu, chips, ask, rect: charRect, region,")
js = rep(js, "  $('loading').style.display = 'none';\n",
         "  $('loading').style.display = 'none';\n  window.__petModelReady = true;\n")
js = rep(js, "start().catch(e => { $('loading').textContent = '加载失败：' + e; console.error(e); });",
         "start().catch(e => { window.__err = String(e); $('loading').textContent = '加载失败：' + e; console.error(e); });")

os.makedirs(os.path.join(PAGE, "js"), exist_ok=True)
os.makedirs(os.path.join(PAGE, "lib"), exist_ok=True)
open(os.path.join(PAGE, "desk.html"), "w", encoding="utf-8").write(html)
open(os.path.join(PAGE, "js", "main.js"), "w", encoding="utf-8").write(js)
for f in ("l2d.js", "lines.js"):
    shutil.copy2(os.path.join(DFY, "js", f), os.path.join(PAGE, "js", f))
for f in ("cubismcore.js", "pixi.min.js", "cubism4p.min.js"):
    shutil.copy2(os.path.join(DFY, "lib", f), os.path.join(PAGE, "lib", f))

# ---------- model ----------
if os.path.isdir(MODEL):
    shutil.rmtree(MODEL)
os.makedirs(os.path.join(MODEL, "desktop_tex"))
src = os.path.join(DFY, "model")
for f in ("dfy.moc3", "dfy.physics3.json"):
    shutil.copy2(os.path.join(src, f), os.path.join(MODEL, f))
shutil.copytree(os.path.join(src, "anim"), os.path.join(MODEL, "anim"))
shutil.copytree(os.path.join(src, "exp"), os.path.join(MODEL, "exp"))
try:
    from PIL import Image
except Exception:
    Image = None
mj = json.load(open(os.path.join(src, "dfy.model3.json"), encoding="utf-8"))
tex = []
for i, t in enumerate(mj["FileReferences"]["Textures"]):
    s = os.path.join(src, t)
    d = os.path.join(MODEL, "desktop_tex", "t%d.webp" % i)
    if Image is not None:
        im = Image.open(s)
        if im.size != (2048, 2048):
            im = im.resize((2048, 2048), Image.LANCZOS)
            im.save(d, "WEBP", quality=92, method=6)
        else:
            shutil.copy2(s, d)        # 已是 2048 webp：原样拷，避免二次有损
    else:
        shutil.copy2(s, d)
    tex.append("desktop_tex/t%d.webp" % i)
mj["FileReferences"]["Textures"] = tex
# 表情也登记进 model3（桌面页自己按参数驱动；登记后别的 Live2D 查看器也能用）
mj["FileReferences"]["Expressions"] = [
    {"Name": os.path.splitext(os.path.splitext(f)[0])[0], "File": "exp/" + f}
    for f in sorted(os.listdir(os.path.join(MODEL, "exp")))]
json.dump(mj, open(os.path.join(MODEL, "大肥鱼.desktop.model3.json"), "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
print("page ->", PAGE)
print("model ->", MODEL, sorted(os.listdir(os.path.join(MODEL, "desktop_tex"))))
