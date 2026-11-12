# -*- coding: utf-8 -*-
"""队列视图（记忆系统）界面渲染验收：Playwright 起真 Chromium，给 pywebview 桥打桩，
喂三档任务（完成 / 待继续 / 浏览器接管不恢复 / 失败），断言界面真的按 IDM 那样把
"记忆"表达出来了，并出截图给用户看。

为什么这么测：webview 界面的 JS 只在打包后的程序里跑，Python 侧日志看不到"按钮有没有
画出来"。改 UI 不做这一步就等于没验（历史教训：设计稿当成已实现、打包了旧成品）。
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, "ui_web", "index.html")
OUT_DIR = os.path.join(ROOT, "outputs")
SHOT = os.path.join(OUT_DIR, "SniffDL_队列_记忆系统.png")

STUB = [
    {"gid": "a1", "status": "active", "totalLength": str(16 * 1024 * 1024),
     "completedLength": str(4 * 1024 * 1024), "downloadSpeed": "512000",
     "name": "正在下的这部.mp4", "dir": "C:\\Videos\\SniffDL", "uri": "http://x/a1.mp4",
     "src": "user", "note": "", "errorCode": "0", "errorMessage": "",
     "files": [{"path": "正在下的这部.mp4", "uris": [{"uri": "http://x/a1.mp4"}]}]},
    {"gid": "b2", "status": "paused", "totalLength": str(16 * 1024 * 1024),
     "completedLength": str(9 * 1024 * 1024), "downloadSpeed": "0",
     "name": "重启前没下完的.mp4", "dir": "C:\\Videos\\SniffDL", "uri": "http://x/b2.mp4",
     "src": "user", "note": "重启未自动重下·点继续才跑", "errorCode": "0", "errorMessage": "",
     "files": [{"path": "重启前没下完的.mp4", "uris": [{"uri": "http://x/b2.mp4"}]}]},
    {"gid": "c3", "status": "paused", "totalLength": str(16 * 1024 * 1024),
     "completedLength": str(1024 * 1024), "downloadSpeed": "0",
     "name": "浏览器自动接管的.mp4", "dir": "C:\\Videos\\SniffDL", "uri": "http://x/c3.mp4",
     "src": "browser", "note": "浏览器自动接管·不恢复", "errorCode": "0",
     "errorMessage": "", "files": [{"path": "浏览器自动接管的.mp4"}]},
    {"gid": "d4", "status": "complete", "totalLength": str(2 * 1024 * 1024),
     "completedLength": str(2 * 1024 * 1024), "downloadSpeed": "0",
     "name": "早就下完的.mp4", "dir": "C:\\Videos\\SniffDL", "uri": "http://x/d4.mp4",
     "src": "user", "note": "", "errorCode": "0", "errorMessage": "",
     "files": [{"path": "早就下完的.mp4"}]},
    {"gid": "e5", "status": "error", "totalLength": "0", "completedLength": "0",
     "downloadSpeed": "0", "name": "直链过期了.mp4", "dir": "C:\\Videos\\SniffDL",
     "uri": "http://x/e5.mp4", "src": "user", "note": "链接可能已过期·点继续重试",
     "errorCode": "1", "errorMessage": "HTTP 403", "files": [{"path": "直链过期了.mp4"}]},
]

CALLS = []


def build_init_script():
    """给 window.pywebview.api.call 打桩：按 method 返回 aria2 风格的三段队列。"""
    src = """
    window.__calls = [];
    (function(){
      var STUB = %s;
      function seg(){ return {ok:true, result:STUB, error:null}; }
      window.pywebview = { api: { call: function(method, paramsJson){
        window.__calls.push(method);
        var res;
        if (method === 'aria2.tellActive')      res = {ok:true, result:STUB.filter(function(t){return t.status==='active';})};
        else if (method === 'aria2.tellWaiting')res = {ok:true, result:[]};
        else if (method === 'aria2.tellStopped')res = {ok:true, result:STUB.filter(function(t){return t.status!=='active';})};
        else if (method === 'aria2.getGlobalStat') res = {ok:true, result:{numActive:'1',numWaiting:'0',numStopped:'4',downloadSpeed:'512000',uploadSpeed:'0'}};
        else if (method === 'sniffdl.status')   res = {ok:true, result:{ok:true, service:'SniffDL', version:'0.5.11', rpc_port:6800}};
        else if (method === 'sniffdl.stats')    res = {ok:true, result:{stats:{today_count:1,today_bytes:2097152,total_bytes:2097152,total_count:1,days:{},disk_free_gb:120}}};
        else if (method === 'sniffdl.theme')    res = {ok:true, result:{theme:'feiyu',label:'',char:'',role:'',palette:{},accent:'#4C9BFF',avatar:''}};
        else if (method === 'sniffdl.getSettings') res = {ok:true, result:{}};
        else if (method === 'sniffdl.clearQueue')  res = {ok:true, result:3};
        else res = {ok:true, result:null};
        return Promise.resolve(JSON.stringify(res));
      } } };
    })();
    """ % json.dumps(STUB, ensure_ascii=False)
    return src


def main():
    from playwright.sync_api import sync_playwright
    os.makedirs(OUT_DIR, exist_ok=True)
    fails = []

    def chk(name, ok, hint=""):
        print("%-48s %s%s" % (name, "PASS" if ok else "FAIL", "" if ok else ("  " + str(hint))))
        if not ok:
            fails.append(name)

    with sync_playwright() as p:
        b = p.chromium.launch(args=["--disable-gpu"])
        pg = b.new_page(viewport={"width": 1320, "height": 860})
        pg.add_init_script(build_init_script())
        pg.goto("file:///" + HTML.replace("\\", "/"))
        pg.wait_for_timeout(2500)
        pg.click("button.nav[data-v=queue]")
        pg.wait_for_timeout(2200)
        rows = pg.locator("#qrows .qrow").count()
        chk("队列视图画出行", rows >= 4, "%d 行" % rows)
        body = pg.inner_text("#v-queue")
        for needle in ("⏸ 待继续", "▶ 继续", "重启未自动重下", "浏览器自动接管",
                       "✓ 完成", "正在下的这部.mp4", "早就下完的.mp4"):
            chk("界面含「%s」" % needle, needle in body, body[:80])
        chk("计数芯片把待继续单列出来", "待继续" in pg.inner_text("#qcount"),
            pg.inner_text("#qcount"))
        chk("「清除已完成」「全部清除」按钮在",
            pg.locator("#qclear_done").count() == 1 and pg.locator("#qclear_all").count() == 1)
        # 点「继续」→ 必须真的把 aria2.unpause 打出去（带 gid）
        before = pg.evaluate("window.__calls.length")
        pg.locator("#qrows .qrow button[data-op=resume]").first.click()
        pg.wait_for_timeout(1200)
        calls = pg.evaluate("window.__calls")
        chk("点「继续」发出 aria2.unpause", "aria2.unpause" in calls[before:], str(calls[before:]))
        pg.locator("#qrows .qrow button[data-op=remove]").last.click()
        pg.wait_for_timeout(1200)
        calls2 = pg.evaluate("window.__calls")
        chk("点「✕」发出 aria2.forceRemove", "aria2.forceRemove" in calls2, str(calls2[-3:]))
        pg.locator("#qclear_done").click()
        pg.wait_for_timeout(1000)
        chk("「清除已完成」发出 sniffdl.clearQueue",
            "sniffdl.clearQueue" in pg.evaluate("window.__calls"), "")
        pg.screenshot(path=SHOT)
        b.close()
    print("截图：%s" % SHOT)
    print("QUEUEUI RESULT=%s" % ("PASS" if not fails else "FAIL(%d): %s" % (len(fails), fails)))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
