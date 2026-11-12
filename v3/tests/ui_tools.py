# -*- coding: utf-8 -*-
"""设置页「工具与诊断」卡（打开保存目录 / 打开日志 / 试听提示音 / 运行自检 / 网站登录 Cookie）。

真内核（SNIFFDL_DRYRUN_OS=1，只记录要打开的路径）+ Playwright：默认折叠；点按钮 → 内核记到的动作正确；
自检弹窗列出真实检查项且有 ✓；试听调用 AudioContext；Cookie 行显示真实条数；登录按钮把输入的站点原样发给内核
（浏览器端拦截，不真弹登录窗）；浅色/深色各截图到 docs/ui_shots/settings_tools_*.png；页面无 JS 报错。
用法：source env/setup.sh && source env/v3env.sh && python3 tests/ui_tools.py     退出码 0 = 全过
"""
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["SNIFFDL_DRYRUN_OS"] = "1"
import ui_pages as P  # noqa: E402
from ui_pages import check, wait, rpc  # noqa: E402

STUB = """window.__osc = 0; class FakeAC { constructor(){this.state='running';this.currentTime=0;this.destination={};}
 resume(){} createGain(){return {gain:{setValueAtTime(){},exponentialRampToValueAtTime(){}},connect(){}};}
 createOscillator(){window.__osc++;return {frequency:{},connect(){},start(){},stop(){}};} } window.AudioContext = FakeAC;"""


def main():
    P.DATA = "/tmp/sdlv3_tools"
    shutil.rmtree(P.DATA, ignore_errors=True)
    os.makedirs(os.path.join(P.DATA, "out"), exist_ok=True)
    cport = P.free_port()
    core = P.start_core(cport)
    rpc(cport, "sniffdl.settings.set", {"outdir": os.path.join(P.DATA, "out"), "clip_watch": False, "notify": False})
    errors = []
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as p:
            br = p.chromium.launch(args=["--no-sandbox"])
            for scheme in ("light", "dark"):
                ctx = br.new_context(viewport={"width": 1320, "height": 900}, color_scheme=scheme, bypass_csp=True)
                ctx.add_init_script(STUB)
                pg = ctx.new_page()
                pg.on("pageerror", lambda e: errors.append(str(e)))
                sent = []

                def mk(sent):
                  def route(r, _req=None):
                    try:
                        b = json.loads(r.request.post_data or "{}")
                    except Exception:
                        b = {}
                    if b.get("method") == "sniffdl.cookie.login":
                        sent.append(b.get("params"))
                        r.fulfill(status=200, content_type="application/json",
                                  body=json.dumps({"jsonrpc": "2.0", "id": b.get("id"), "result": {"ok": True, "msg": "测试：登录窗已拦截"}}))
                    else:
                        r.continue_()
                  return route
                pg.route("**/jsonrpc", mk(sent))
                pg.goto("http://127.0.0.1:%d/ui/index.html#port=%d&token=%s" % (cport, cport, P.TOKEN))
                pg.wait_for_function("window.__sdlReady === true", timeout=20000)
                pg.keyboard.press("Alt+9")
                pg.wait_for_selector("#toolsCard", state="attached")
                check("%s T1 工具卡默认折叠" % scheme, pg.evaluate("!document.querySelector('#toolsCard').open"))
                pg.click("#toolsCard > summary")
                pg.wait_for_selector("#btnOpenDir", state="visible")
                check("%s T2 展开后有 5 类工具（目录/日志/试听/自检/登录）" % scheme,
                      all(pg.is_visible(s) for s in ("#btnOpenDir", "#btnOpenLog", "#btnChime", "#btnSelftest", "#btnCkLogin")))
                n0 = len(rpc(cport, "sniffdl.debug.osActions", 50))
                pg.click("#btnOpenDir")
                ok = wait(lambda: len(rpc(cport, "sniffdl.debug.osActions", 50)) > n0, 5)
                acts = rpc(cport, "sniffdl.debug.osActions", 50)
                check("%s T3 打开保存目录 → 内核收到 open 动作，路径是保存目录" % scheme,
                      ok and acts[-1]["kind"] == "open" and acts[-1]["target"] == os.path.join(P.DATA, "out"), acts[-1:])
                n0 = len(acts)
                pg.click("#btnOpenLog")
                ok = wait(lambda: len(rpc(cport, "sniffdl.debug.osActions", 50)) > n0, 5)
                acts = rpc(cport, "sniffdl.debug.osActions", 50)
                check("%s T4 打开日志 → 内核收到 open 动作，指向 sniffdl.log" % scheme,
                      ok and str(acts[-1]["target"]).endswith("sniffdl.log"), acts[-1:])
                check("%s T5a 点试听之前没有任何多余的响声（0）" % scheme, pg.evaluate("window.__osc") == 0, pg.evaluate("window.__osc"))
                pg.evaluate("window.__osc = 0")
                pg.click("#btnChime")
                check("%s T5 试听 → 合成两个音（AudioContext 振荡器 2 个）" % scheme, wait(lambda: pg.evaluate("window.__osc") == 2, 3), pg.evaluate("window.__osc"))
                pg.click("#btnSelftest")
                pg.wait_for_selector("#selftestList .stitem", timeout=20000)
                names = pg.eval_on_selector_all("#selftestList .stitem b", "els=>els.map(e=>e.textContent)")
                oks = pg.eval_on_selector_all("#selftestList .stitem", "els=>els.map(e=>e.dataset.ok)")
                check("%s T6 自检弹窗列出真实检查项（含 aria2c、保存目录可写、内核端口）" % scheme,
                      {"aria2c", "保存目录可写", "内核端口"} <= set(names) and "1" in oks, names)
                if scheme == "dark":
                    pg.screenshot(path=os.path.join(P.SHOTS, "settings_selftest_dark.png"))
                pg.keyboard.press("Escape")
                pg.wait_for_selector(".modal", state="detached", timeout=5000)
                check("%s T7 自检弹窗 Esc 关闭" % scheme, True)
                ck = pg.inner_text("#ckInfo")
                check("%s T8 Cookie 行显示真实条数与来源" % scheme, "已保存" in ck and "条 Cookie" in ck and "来源" in ck, ck)
                pg.fill("#ckSite", "https://example.com/login")
                pg.click("#btnCkLogin")
                check("%s T9 登录按钮把站点原样发给内核" % scheme, wait(lambda: sent and sent[-1] == ["https://example.com/login"], 5), sent)
                pg.fill("#setSearch", "提示音")
                pg.wait_for_timeout(300)
                check("%s T10 搜索时工具卡随无匹配项隐藏" % scheme, pg.evaluate("document.querySelector('#toolsCard').classList.contains('hidden')"))
                pg.fill("#setSearch", "")
                pg.wait_for_timeout(300)
                pg.evaluate("document.querySelector('#toolsCard').scrollIntoView()")
                pg.screenshot(path=os.path.join(P.SHOTS, "settings_tools_%s.png" % scheme))
                ctx.close()
            check("T11 页面无 JS 报错", not errors, errors[:3])
            br.close()
    finally:
        P.stop_core(cport, core)
    bad = [r for r in P.RESULTS if not r[1]]
    print("\n%d/%d 通过" % (len(P.RESULTS) - len(bad), len(P.RESULTS)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
