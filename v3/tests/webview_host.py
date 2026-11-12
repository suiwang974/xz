# -*- coding: utf-8 -*-
"""ui_web.py（pywebview 后备宿主）测试。沙箱没有 GTK/Qt，pywebview 开不了真窗口，所以分三层：

  1 CoreManager：真拉起内核（SNIFFDL_CORE_CMD 指向 tests/ui_smoke_core.py 的 tkinter 打桩启动器）→ conn() 拿到 port/token 且 ping 通；
    第二个 CoreManager 复用同一内核（attached，不重复拉起）；stop() 只关自己拉起的内核。
  2 HostApi：假 window（记录 create_file_dialog 调用）测 pick_dir / pick_file（Electron filters → pywebview file_types）/
    save_file（真写盘，对象转 JSON）/ notify / quit；真 pywebview（pip 装到 /tmp/pwv，可选）只校验对话框常量能取到。
  3 页面：Playwright 加载内核托管的 /ui/index.html（ui_web.ui_url；file:// 下 module 被 CORS 拦，实测），init script 造一个 window.pywebview.api（每个方法经 expose_function
    回调到 Python 里真正的 HostApi），再执行 ui_web.HOST_SHIM → window.sniffdlHost 形状与 Electron preload 一致 →
    界面经 sniffdlHost.conn() 连上真内核（__sdlReady、状态栏在线）→ sniffdlHost.saveFile / pickDir / notify 走到 HostApi。

用法：source env/setup.sh && source env/v3env.sh && python3 tests/webview_host.py
"""
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import ui_web  # noqa: E402

DATA = "/tmp/sdlwv"
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("  PASS " if ok else "  FAIL ") + name + ("" if ok else "  ← " + str(detail)[:300]), flush=True)
    return ok


class FakeWV:
    class FileDialog:
        OPEN, FOLDER, SAVE = 10, 20, 30


class FakeWin:
    def __init__(self):
        self.calls, self.destroyed = [], False
        self.answer = {}

    def create_file_dialog(self, kind, **kw):
        self.calls.append((kind, kw))
        return self.answer.get(kind)

    def destroy(self):
        self.destroyed = True

    def restore(self):
        pass

    def show(self):
        pass


def main():
    shutil.rmtree(DATA, ignore_errors=True)
    os.makedirs(DATA)
    os.environ["SNIFFDL_CORE_CMD"] = json.dumps([sys.executable, os.path.join(HERE, "tests", "ui_smoke_core.py")])
    rt = os.path.join(DATA, "runtime.json")
    cm = ui_web.CoreManager(rt, os.path.join(DATA, "core"), 0)
    t0 = time.time()
    c = cm.conn()
    check("1 CoreManager 拉起内核并拿到 port/token", c and c["port"] and len(c["token"]) == 32 and not c["attached"], c)
    print("    内核就绪 %.1fs" % (time.time() - t0))
    check("1 ui_token 能调 ui 方法（sniffdl.snapshot）", isinstance(ui_web._rpc(cm.info, "sniffdl.snapshot"), dict))
    cm2 = ui_web.CoreManager(rt, os.path.join(DATA, "core"), 0)
    c2 = cm2.conn()
    check("1 第二个宿主复用同一内核（attached）", c2 and c2["port"] == c["port"] and c2["token"] == c["token"] and c2["attached"]
          and cm2.proc is None, c2)

    win = FakeWin()
    api = ui_web.HostApi(cm, lambda: win, FakeWV)
    check("2 conn() 同 Electron 形状 {port, token, pid, version}", set(["port", "token", "pid", "version"]) <= set(api.conn()))
    win.answer[20] = ("/tmp/pick_here",)
    check("2 pick_dir → 文件夹对话框并返回路径", api.pick_dir("选保存位置") == "/tmp/pick_here" and win.calls[-1][0] == 20)
    win.answer[10] = ("/tmp/a.torrent",)
    r = api.pick_file([{"name": "种子", "extensions": ["torrent"]}])
    check("2 pick_file：Electron filters → file_types", r == "/tmp/a.torrent" and
          win.calls[-1][1]["file_types"] == ("种子 (*.torrent)", "所有文件 (*.*)"), win.calls[-1])
    out = os.path.join(DATA, "导出.json")
    win.answer[30] = out
    p = api.save_file("tasks.json", {"a": 1, "名": "值"})
    check("2 save_file：另存为 + 对象转 JSON 写盘（UTF-8）", p == out and json.load(open(out, encoding="utf-8")) == {"a": 1, "名": "值"}
          and win.calls[-1][1]["save_filename"] == "tasks.json")
    win.answer[30] = None
    check("2 save_file 取消 → None", api.save_file("x.txt", "t") is None)
    check("2 notify 返回布尔", isinstance(api.notify("标题", "正文"), bool))
    check("2 electron_filters 空/通配", ui_web.electron_filters(None) == [] and
          ui_web.electron_filters([{"name": "全部", "extensions": ["*"]}])[0] == "全部 (*)")
    try:
        sys.path.insert(0, "/tmp/pwv")
        import webview as real_wv
        check("2 真 pywebview %s：对话框常量可取" % getattr(real_wv, "__version__", "?"),
              all(ui_web._dialog(real_wv, k) is not None for k in ("OPEN", "FOLDER", "SAVE")))
    except Exception as e:
        print("    （未装 pywebview，跳过常量校验：%s）" % e)

    # 3 页面
    from playwright.sync_api import sync_playwright
    calls = []

    def bridge(name, args):
        calls.append(name)
        fn = getattr(api, name)
        return fn(*(args or []))
    index = ui_web.ui_url(c)     # 与 ui_web.main 相同：内核托管的 /ui/（不带 #token，令牌只经宿主 conn() 给）
    with sync_playwright() as pw:
        br = pw.chromium.launch(args=["--no-sandbox"])
        ctx = br.new_context(viewport={"width": 1320, "height": 860}, bypass_csp=True,
                             user_agent="Mozilla/5.0 Chrome/131 SniffDL-pywebview/test")
        errs = []
        pg = ctx.new_page()
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        pg.expose_function("__pyBridge", bridge)
        # 模拟 pywebview：桥在页面脚本之后才就绪（真实情况），并派发 pywebviewready
        pg.add_init_script("""
          setTimeout(() => {
            const names = ['conn','pick_dir','pick_file','save_file','notify','show','quit','set_progress'];
            window.pywebview = { api: {} };
            for (const n of names) window.pywebview.api[n] = (...a) => window.__pyBridge(n, a);
            window.dispatchEvent(new Event('pywebviewready'));
          }, 300);
        """)
        pg.goto(index)
        pg.wait_for_function("!!window.pywebview", timeout=5000)
        pg.evaluate(ui_web.HOST_SHIM)     # 等价于 ui_web.main 里 events.loaded → evaluate_js(HOST_SHIM)
        shape = pg.evaluate("Object.keys(window.sniffdlHost).sort().join(',')")
        check("3 window.sniffdlHost 形状（conn/pickDir/pickFile/saveFile/notify/show/quit/setProgress）",
              all(k in shape.split(",") for k in ("conn", "pickDir", "pickFile", "saveFile", "notify", "show", "quit", "setProgress")), shape)
        try:
            pg.wait_for_function("window.__sdlReady === true", timeout=20000)
            ok = True
        except Exception as e:
            ok = False
        check("3 界面（无 #token）经宿主 conn() 连上真内核（__sdlReady）", ok and "conn" in calls, (calls, errs[-3:]))
        on = pg.wait_for_function("document.body.innerText.includes('已连接') || document.body.innerText.includes('在线') ? true : null",
                                  timeout=8000) if ok else None
        check("3 状态栏显示已连接", bool(on), pg.inner_text("body")[-300:] if ok else "")
        win.answer[30] = os.path.join(DATA, "from_page.json")
        r = pg.evaluate("window.sniffdlHost.saveFile('list.json', {n: 3})")
        check("3 sniffdlHost.saveFile → HostApi.save_file 写盘", r == win.answer[30] and
              json.load(open(r, encoding="utf-8")) == {"n": 3}, r)
        win.answer[20] = ("/tmp/from_page_dir",)
        check("3 sniffdlHost.pickDir → HostApi.pick_dir", pg.evaluate("window.sniffdlHost.pickDir('x')") == "/tmp/from_page_dir")
        check("3 sniffdlHost.notify → HostApi.notify", isinstance(pg.evaluate("window.sniffdlHost.notify('a','b')"), bool))
        check("3 无 JS 报错", not errs, errs[:5])
        br.close()

    check("2 quit → window.destroy", api.quit() and win.destroyed)
    pid = cm.info.get("pid")
    cm2.stop()
    check("1 复用方 stop() 不关内核", ui_web._pid_alive(pid) and cm._alive(cm.info))
    cm.stop()
    time.sleep(0.5)
    check("1 拉起方 stop() 关掉内核", not cm._alive({"port": c["port"], "ui_token": c["token"]}))
    n = sum(1 for r in RESULTS if r[1])
    print("\nwebview_host: %d/%d PASS" % (n, len(RESULTS)))
    sys.exit(0 if n == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
