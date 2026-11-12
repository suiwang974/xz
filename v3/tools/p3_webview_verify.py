# -*- coding: utf-8 -*-
"""P3 验收：无头加载真实 ui_web/index.html，stub 掉 pywebview 桥（返回与后端同构的
真实载荷），断言五视图被真数据填满 + 大肥鱼蓝主题生效 + 控件能触发 RPC。
用法: python tools/p3_webview_verify.py  退出码 0=全绿。"""
import functools
print = functools.partial(print, flush=True)
import importlib.util, json, os, sys
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
OUT = os.path.join(ROOT, "outputs")
os.makedirs(OUT, exist_ok=True)
INDEX = "file:///" + os.path.join(ROOT, "ui_web", "index.html").replace("\\", "/")

results = []
def check(name, cond, detail=""):
    ok = bool(cond); results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (("  | " + str(detail)) if detail else ""))

def real_theme():
    spec = importlib.util.spec_from_file_location("vd", os.path.join(ROOT, "video_downloader.py"))
    vd = importlib.util.module_from_spec(spec); spec.loader.exec_module(vd)
    return vd.feiyu_theme_payload()

# 与后端同构的 canned 载荷
SETTINGS = {"ok": True, "settings": {
    "quality": "1080", "dl_threads": "64", "dash_aria2": True, "proxy_mode": "跟随系统",
    "proxy_manual": "", "cookie_browser": "自动", "after": "无", "rx_cap_gb": 8,
    "outdir": "C:\\Users\\Test\\Videos\\SniffDL", "theme": "feiyu", "rpc_enable": True,
    "rpc_port": 6820, "rpc_token": "tk-test", "deep": False, "pet_live": True,
    "pet_skin": "horn", "cast24_legacy": False, "tray_close": True, "autostart": False,
    "categories": ["常规", "视频"]}}
STATUS = {"ok": True, "service": "SniffDL", "version": "0.5.9", "theme": "feiyu",
          "rpc_port": 6820, "active": 2, "takeover_ready": True}
GLOBAL = {"numActive": "2", "numWaiting": "1", "numStopped": "3",
          "downloadSpeed": "4500000", "uploadSpeed": "12000"}
STATS = {"ok": True, "stats": {
    "days": {"2026-11-03": {"bytes": 645922816, "count": 3}, "2026-11-01": {"bytes": 200000000, "count": 1}},
    "today_bytes": 645922816, "today_count": 3, "total_bytes": 13743895347, "total_count": 41,
    "disk_free_gb": 218.0}}
ACTIVE = [{"gid": "1", "name": "深海宝藏.mp4", "status": "active", "totalLength": "584500000",
           "completedLength": "455000000", "downloadSpeed": "3200000"},
          {"gid": "2", "name": "鲸落现场.mkv", "status": "active", "totalLength": "260000000",
           "completedLength": "106000000", "downloadSpeed": "1300000"}]
WAITING = [{"gid": "3", "name": "潮汐电台.flac", "status": "waiting", "totalLength": "174900000", "completedLength": "0"}]
STOPPED = [{"gid": "4", "name": "珊瑚图鉴.pdf", "status": "complete", "totalLength": "31800000", "completedLength": "31800000"}]
SNIFF_ITEMS = [{"name": "桑多涅的私宠", "url": "https://x/a.m3u8", "ext": "m3u8", "resolution": "1920x1080",
                "duration": "38:44", "size": 612904960, "status": "待下载", "source": "AList直链"},
               {"name": "玛奇玛护士服", "url": "https://x/b.mp4", "ext": "mp4", "duration": "17:20",
                "size": 33344512, "status": "待下载", "source": "站点解析"}]

def build_init(theme):
    stub = {
        "THEME": theme, "SETTINGS": SETTINGS, "STATUS": STATUS, "GLOBAL": GLOBAL,
        "STATS": STATS, "ACTIVE": ACTIVE, "WAITING": WAITING, "STOPPED": STOPPED,
        "SNIFF_ITEMS": SNIFF_ITEMS,
        "COOKIE_INFO": {"ok": True, "mode": "自动", "stored": 128, "browsers": ["chrome", "edge"], "has_cookie_file": True},
        "COMPONENTS": {"ok": True, "cur": {"yt-dlp": "2024.10.1", "ffmpeg": "ffmpeg-win-x86_64-v7.exe"},
                       "info": {"yt-dlp": {"name": "yt-dlp", "cur": "2024.10.1", "latest": "2024.11.0", "need": True}},
                       "last": 0, "checking": False},
    }
    return """
    var DB = %s;
    window.__setCalls = []; window.__rpcCalls = [];
    window.pywebview = { api: { call: function(method, paramsJson){
      var params; try{ params = JSON.parse(paramsJson||'[]'); }catch(e){ params=[]; }
      window.__rpcCalls.push(method);
      var R = function(x){ return Promise.resolve(JSON.stringify({ok:true, result:x})); };
      if(method==='sniffdl.theme') return R(DB.THEME);
      if(method==='sniffdl.getSettings') return R(DB.SETTINGS);
      if(method==='sniffdl.setSetting'){ window.__setCalls.push([params[0],params[1]]); return R({ok:true,key:params[0],value:params[1]}); }
      if(method==='sniffdl.stats') return R(DB.STATS);
      if(method==='sniffdl.status') return R(DB.STATUS);
      if(method==='aria2.getGlobalStat') return R(DB.GLOBAL);
      if(method==='aria2.tellActive') return R(DB.ACTIVE);
      if(method==='aria2.tellWaiting') return R(DB.WAITING);
      if(method==='aria2.tellStopped') return R(DB.STOPPED);
      if(method==='sniffdl.sniff') return R('tid-1');
      if(method==='sniffdl.sniffResult') return R({status:'done', items:DB.SNIFF_ITEMS});
      if(method==='sniffdl.openOutDir') return R({ok:true});
      if(method==='sniffdl.pickDir') return R({ok:true, path:'C:\\\\Test\\\\Picked'});
      if(method==='sniffdl.add') return R('gid-new');
      if(method==='sniffdl.cookieInfo') return R(DB.COOKIE_INFO);
      if(method==='sniffdl.loginCookie') return R({ok:true, started:true, url:params[0]});
      if(method==='sniffdl.loginStatus') return R({ok:true, running:false, saved:42, done:true, msg:'登录成功，已记住 42 条 Cookie'});
      if(method==='sniffdl.importCookie') return R({ok:true, stored:7, msg:'已导入并记住 7 条'});
      if(method==='sniffdl.components') return R(DB.COMPONENTS);
      if(method==='sniffdl.checkUpdates') return R({ok:true, started:true});
      if(method==='sniffdl.updateOne') return R({ok:true, name:params[0]});
      if(method==='sniffdl.clipHint') return R({ok:true, url: window.__clipUrl||''});
      if(method==='sniffdl.clipDismiss'){ window.__clipUrl=''; return R({ok:true}); }
      if(method==='sniffdl.clipTake'){ window.__clipUrl=''; return R({ok:true, task:'tid-clip'}); }
      return R(null);
    } } };
    """ % json.dumps(stub, ensure_ascii=False)

def main():
    theme = real_theme()
    init_js = build_init(theme)
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--allow-file-access-from-files"])
        ctx = b.new_context(viewport={"width": 1280, "height": 820})
        pg = ctx.new_page()
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.add_init_script(init_js)
        pg.goto(INDEX, wait_until="load")
        pg.wait_for_timeout(1500)

        # 主题
        acc = pg.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--acc').trim()")
        check("--acc 大肥鱼蓝 #3E6FD0", acc.lower() == "#3e6fd0", acc)
        menm = pg.evaluate("document.getElementById('menm').textContent")
        check("侧栏身份=大肥鱼", "大肥鱼" in menm, menm)
        avsrc = pg.evaluate("document.getElementById('meimg').getAttribute('src')||''")
        check("身份头像来自 theme", avsrc.startswith("data:image/png;base64,"), avsrc[:24])
        avnat = pg.evaluate("document.getElementById('meimg').naturalWidth||0")
        check("头像真实加载(naturalWidth>0)", avnat > 0, "naturalW=%s" % avnat)

        # 设置回显
        outdir = pg.evaluate("document.getElementById('s_outdir').value")
        check("设置回显 outdir", outdir == SETTINGS["settings"]["outdir"], outdir)
        thr = pg.evaluate("document.getElementById('s_threads').value")
        check("设置回显 加速档位=64", thr == "64", thr)
        dash_on = pg.evaluate("!document.getElementById('s_dash').classList.contains('off')")
        check("设置回显 大站多线程=开", dash_on, dash_on)

        # 值班室真数据
        today = pg.evaluate("document.getElementById('kToday').textContent")
        check("KPI 今日=3 件", "3" in today, today)
        total = pg.evaluate("document.getElementById('kTotal').textContent")
        check("KPI 累计≈12.8GB", "12.8" in total, total)
        heat = pg.evaluate("document.querySelectorAll('#heat i').length")
        check("热力图 112 格", heat == 112, heat)
        heat_l4 = pg.evaluate("document.querySelectorAll('#heat i.l4').length")
        check("热力图有高等级日", heat_l4 >= 1, heat_l4)

        # 队列真数据
        qrows = pg.evaluate("document.querySelectorAll('#qrows .qrow').length")
        check("队列渲染 4 行(2活+1队+1停)", qrows == 4, qrows)
        qname = pg.evaluate("(document.querySelector('#qrows .nm')||{}).textContent||''")
        check("队列含真实任务名", "深海宝藏" in qname, qname)

        # 状态条
        rpcp = pg.evaluate("document.getElementById('sbRpc').textContent")
        check("状态条显示 RPC 端口", "6820" in rpcp, rpcp)
        down = pg.evaluate("document.getElementById('sbDown').textContent")
        check("状态条显示速度", "MB/s" in down or "B/s" in down, down)

        # 下载：点嗅探 → 真数据填表
        pg.fill("#url", "https://example.com/video")
        pg.click("#sniffbtn")
        pg.wait_for_timeout(2200)  # 等一轮 sniffResult 轮询
        dlrows = pg.evaluate("document.querySelectorAll('#dlrows tr').length")
        check("嗅探→下载表填 2 行", dlrows == 2, dlrows)
        dlcount = pg.evaluate("document.getElementById('dlcount').textContent")
        check("下载计数=2 条", "2" in dlcount, dlcount)

        # 控件触发 RPC：切到设置视图 + 展开「下载」卡 → 改画质 → setSetting
        pg.click(".nav[data-v=set]"); pg.wait_for_timeout(200)
        pg.evaluate("""(()=>{var el=document.getElementById('s_quality');
            var card=el.closest('.scard'); if(card)card.classList.add('open');})()""")
        pg.wait_for_timeout(200)
        pg.select_option("#s_quality", "720")
        pg.wait_for_timeout(300)
        calls = pg.evaluate("window.__setCalls")
        check("改画质触发 setSetting(quality)", any(c[0] == "quality" and c[1] == "720" for c in calls), calls)

        # 开关触发 setSetting：点「大站多线程」sw
        pg.evaluate("""(()=>{var el=document.getElementById('s_dash');el.closest('.scard').classList.add('open');})()""")
        pg.click("#s_dash"); pg.wait_for_timeout(200)
        calls2 = pg.evaluate("window.__setCalls")
        check("点开关触发 setSetting(dash_aria2)", any(c[0] == "dash_aria2" for c in calls2), calls2[-3:])

        # Cookie 卡：状态回显 + 登录窗按钮触发 loginCookie
        pg.evaluate("""(()=>{var el=document.getElementById('ck_login');el.closest('.scard').classList.add('open');})()""")
        pg.wait_for_timeout(200)
        ckb = pg.evaluate("document.getElementById('ck_browsers').textContent")
        check("Cookie 卡显示本机浏览器", "chrome" in ckb and "edge" in ckb, ckb)
        cks = pg.evaluate("document.getElementById('ck_stored').textContent")
        check("Cookie 卡显示已存条数", "128" in cks, cks)
        pg.fill("#ck_url", "instagram.com")
        pg.click("#ck_login"); pg.wait_for_timeout(400)
        rpcs = pg.evaluate("window.__rpcCalls")
        check("点登录窗触发 sniffdl.loginCookie", "sniffdl.loginCookie" in rpcs, "loginCookie" in rpcs)
        pg.wait_for_timeout(2200)  # 等一轮 loginStatus 轮询
        ckmsg = pg.evaluate("document.getElementById('ck_msg').textContent")
        check("登录完成回显记住条数", "42" in ckmsg or "记住" in ckmsg, ckmsg)

        # 组件卡：版本回显 + 检查更新触发 + 有新版显示升级按钮
        pg.evaluate("""(()=>{var el=document.getElementById('cp_check');el.closest('.scard').classList.add('open');})()""")
        pg.wait_for_timeout(200)
        cpy = pg.evaluate("document.getElementById('cp_ytdlp').textContent")
        check("组件卡显示 yt-dlp 版本+最新", "2024.10.1" in cpy and "2024.11.0" in cpy, cpy)
        upd_visible = pg.evaluate("document.getElementById('cp_upd').style.display!=='none'")
        check("发现新版→显示升级按钮", upd_visible, upd_visible)
        pg.click("#cp_check"); pg.wait_for_timeout(300)
        rpcs2 = pg.evaluate("window.__rpcCalls")
        check("点检查更新触发 sniffdl.checkUpdates", "sniffdl.checkUpdates" in rpcs2, "checkUpdates" in rpcs2)

        # 快捷键提示（桌宠卡 body 折叠时用 textContent 取全量文本）
        pet_txt = pg.evaluate("""(()=>{var el=document.getElementById('s_pet');return el?el.closest('.scard').textContent:'';})()""")
        check("桌宠卡提示 Ctrl+Alt+H 快捷键", "Ctrl + Alt + H" in pet_txt, pet_txt[:60])

        # 全部下载 → sniffdl.add + toast（切回下载视图）
        pg.click(".nav[data-v=dl]"); pg.wait_for_timeout(200)
        pg.click("#dlall")
        pg.wait_for_timeout(400)
        toast = pg.evaluate("document.getElementById('toast').textContent")
        check("全部下载有反馈 toast", "搬" in toast or "交给" in toast, toast)

        # 打开文件夹 → openOutDir（无异常即可，见 errors）
        pg.click("#openout")
        pg.wait_for_timeout(200)

        # 剪贴板接活：注入一条链接 → 轮询应弹出可操作下载条 → 点下载触发嗅探
        pg.evaluate("window.__clipUrl='https://www.instagram.com/reel/demo/'")
        pg.wait_for_timeout(3000)   # 等一轮 pollClip(2.5s)
        clip_on = pg.evaluate("document.getElementById('clipbar').classList.contains('on')")
        clip_lk = pg.evaluate("document.getElementById('clklk').textContent")
        check("剪贴板检测到链接→弹出下载条", clip_on and "instagram" in clip_lk, "on=%s lk=%s" % (clip_on, clip_lk))
        before = pg.evaluate("window.__rpcCalls.length")
        pg.click("#clkgo"); pg.wait_for_timeout(400)
        rpcs3 = pg.evaluate("window.__rpcCalls")
        check("点『下载这条』触发嗅探", "sniffdl.sniff" in rpcs3[before:], rpcs3[before:])
        clip_off = pg.evaluate("!document.getElementById('clipbar').classList.contains('on')")
        check("下载后下载条收起", clip_off, clip_off)

        # 截图（下载视图 + 值班室）
        pg.click(".nav[data-v=dl]"); pg.wait_for_timeout(300)
        pg.screenshot(path=os.path.join(OUT, "p3_webview_dl.png"))
        pg.click(".nav[data-v=dash]"); pg.wait_for_timeout(300)
        pg.screenshot(path=os.path.join(OUT, "p3_webview_dash.png"))
        pg.click(".nav[data-v=queue]"); pg.wait_for_timeout(300)
        pg.screenshot(path=os.path.join(OUT, "p3_webview_queue.png"))
        pg.click(".nav[data-v=set]"); pg.wait_for_timeout(300)
        pg.screenshot(path=os.path.join(OUT, "p3_webview_set.png"))
        b.close()

    real_err = [e for e in errors if "no-bridge" not in e]
    check("无 JS 运行错误", len(real_err) == 0, real_err[:3])

    passed = sum(1 for _, ok, _ in results if ok); total = len(results)
    print("\n==== P3 webview 验收: %d/%d PASS ====" % (passed, total))
    for n, ok, d in results:
        if not ok: print("  FAILED:", n, "|", d)
    return 0 if passed == total else 1

if __name__ == "__main__":
    sys.exit(main())
