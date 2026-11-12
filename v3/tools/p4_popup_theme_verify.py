# -*- coding: utf-8 -*-
"""P4 验收：用真实 popup.html + popup.js（stub 掉 chrome.*），喂进后端真实产出的
大肥鱼主题载荷，无头渲染 → 断言 CSS 变量/文案/头像都变成大肥鱼蓝，并截图。
用法: python tools/p4_popup_theme_verify.py  退出码 0=全绿。"""
import functools
print = functools.partial(print, flush=True)
import importlib.util, json, os, sys
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
EXT = os.path.join(ROOT, "extension")
OUT = os.path.join(ROOT, "outputs")
os.makedirs(OUT, exist_ok=True)

results = []
def check(name, cond, detail=""):
    ok = bool(cond); results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (("  | " + str(detail)) if detail else ""))

def get_theme():
    spec = importlib.util.spec_from_file_location("vd", os.path.join(ROOT, "video_downloader.py"))
    vd = importlib.util.module_from_spec(spec); spec.loader.exec_module(vd)
    return vd.feiyu_theme_payload()

def main():
    theme = get_theme()
    check("后端主题 theme=feiyu", theme.get("theme") == "feiyu", theme.get("theme"))
    theme_json = json.dumps(theme, ensure_ascii=False)

    init = """
    window.__THEME = %s;
    (function(){
      function send(msg, cb){
        if (typeof cb !== 'function') return;
        var t = (msg && msg.type) || '';
        if (t === 'ping') cb({ ok:true, theme: window.__THEME, status:{ rpc_port:6820 } });
        else if (t === 'load') cb({ ok:true, data:{ enabled:true, port:6820, token:'x', minSizeMB:0, onlyExtensions:'' } });
        else if (t === 'tasks') cb({ ok:true, active:[], waiting:[], stopped:[] });
        else if (t === 'rpc') cb({ ok:true, result:{ items:[] } });
        else cb({ ok:true });
      }
      window.chrome = {
        runtime: { sendMessage: send, id: 'sniffdl-test' },
        tabs: { create: function(){} },
        storage: { local: { get: function(k,cb){ cb && cb({}); }, set: function(k,cb){ cb&&cb(); } } }
      };
    })();
    """ % theme_json

    popup_url = "file:///" + os.path.join(EXT, "popup.html").replace("\\", "/")
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--allow-file-access-from-files"])
        ctx = b.new_context(viewport={"width": 360, "height": 720})
        pg = ctx.new_page()
        pg.add_init_script(init)
        pg.goto(popup_url, wait_until="load")
        pg.wait_for_timeout(1200)
        vals = pg.evaluate("""() => {
          const rs = getComputedStyle(document.documentElement);
          return {
            accent: rs.getPropertyValue('--accent').trim(),
            accent2: rs.getPropertyValue('--accent2').trim(),
            bg: rs.getPropertyValue('--bg').trim(),
            ink: rs.getPropertyValue('--ink').trim(),
            line: rs.getPropertyValue('--line').trim(),
            charName: (document.getElementById('charName')||{}).textContent || '',
            charRole: (document.getElementById('charRole')||{}).textContent || '',
            avatarSrc: (document.getElementById('avatarImg')||{}).getAttribute
              ? ((document.getElementById('avatarImg').getAttribute('src')||'').slice(0,24)) : '',
            avatarNaturalW: (document.getElementById('avatarImg')||{}).naturalWidth || 0,
            avatarDisplay: (document.getElementById('avatarImg')||{}).style ? (document.getElementById('avatarImg').style.display) : '',
            heroBg: (()=>{const h=document.querySelector('.hero');return h?getComputedStyle(h).backgroundImage:'';})()
          };
        }""")
        print("computed:", json.dumps(vals, ensure_ascii=False))
        check("--accent = 大肥鱼宝蓝 #3E6FD0", vals["accent"].lower() == "#3e6fd0", vals["accent"])
        check("--accent2 = 青宝石 #38C6D9", vals["accent2"].lower() == "#38c6d9", vals["accent2"])
        check("--bg = #EEF3FB", vals["bg"].lower() == "#eef3fb", vals["bg"])
        check("charName 含 大肥鱼", "大肥鱼" in vals["charName"], vals["charName"])
        check("charRole 含 深海", "深海" in vals["charRole"], vals["charRole"])
        check("头像 src 是 data:image", vals["avatarSrc"].startswith("data:image/png"), vals["avatarSrc"])
        check("头像真正加载成功(naturalWidth>0)", vals["avatarNaturalW"] > 0, "naturalW=%s display=%s" % (vals["avatarNaturalW"], vals["avatarDisplay"]))
        check("hero 背景是蓝色渐变", ("62, 111, 208" in vals["heroBg"]) or ("#3e6fd0" in vals["heroBg"].lower())
              or ("3e6fd0" in vals["heroBg"].lower()), vals["heroBg"][:80])
        pg.screenshot(path=os.path.join(OUT, "p4_popup_blue.png"), full_page=True)
        print("screenshot ->", os.path.join(OUT, "p4_popup_blue.png"))
        b.close()

    passed = sum(1 for _, ok, _ in results if ok); total = len(results)
    print("\n==== P4 插件主题验收: %d/%d PASS ====" % (passed, total))
    for n, ok, d in results:
        if not ok: print("  FAILED:", n, "|", d)
    return 0 if passed == total else 1

if __name__ == "__main__":
    sys.exit(main())
