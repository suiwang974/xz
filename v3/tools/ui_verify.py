# -*- coding: utf-8 -*-
"""UI 验收 harness（离线，不联网、不下载）：
  1) 同进程构建 App，客观断言换装 chip 不溢出侧栏、第 6 只中心点命中测试能拿到该 cell；
  2) 断言 chip 头像未被画布裁边、渐变位图缓存非空（证明 PIL 路径真的生效）；
  3) PrintWindow 抓主窗（用户常全屏看片，ImageGrab 拍不到），逐视图 × 逐角色出图。
产物：_uicheck/ui_*.png + _uicheck/ui_verify.log；退出码 0 = 全部断言通过。
"""
import os
import sys
import ctypes
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(HERE)
sys.path.insert(0, ROOT_DIR)
SHOT_DIR = os.path.join(ROOT_DIR, "_uicheck")
os.makedirs(SHOT_DIR, exist_ok=True)

FAILS = []
NOTES = []


def check(name, cond, detail=""):
    line = "%-46s %s  %s" % (name, "PASS" if cond else "FAIL", detail)
    print(line)
    (NOTES if cond else FAILS).append(line)
    return cond


# ---------- PrintWindow 抓窗（被遮挡也能拿到窗口内容） ----------
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
_W = ctypes.c_void_p
BOOL, HWND, HDC, HGDIOBJ, UINT = ctypes.c_int, _W, _W, _W, ctypes.c_uint

user32.GetAncestor.argtypes = [HWND, UINT]
user32.GetAncestor.restype = HWND
user32.GetWindowRect.argtypes = [HWND, ctypes.c_void_p]
user32.GetWindowRect.restype = BOOL
user32.PrintWindow.argtypes = [HWND, HDC, UINT]
user32.PrintWindow.restype = BOOL
user32.GetWindowDC.argtypes = [HWND]
user32.GetWindowDC.restype = HDC
user32.ReleaseDC.argtypes = [HWND, HDC]
user32.BringWindowToTop.argtypes = [HWND]
user32.SetForegroundWindow.argtypes = [HWND]
user32.ShowWindow.argtypes = [HWND, ctypes.c_int]
user32.ShowWindow.restype = ctypes.c_int
user32.GetCursorPos.argtypes = [ctypes.c_void_p]
user32.GetCursorPos.restype = BOOL
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype = BOOL
gdi32.CreateCompatibleDC.argtypes = [HDC]
gdi32.CreateCompatibleDC.restype = HDC
gdi32.CreateCompatibleBitmap.argtypes = [HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = HGDIOBJ
gdi32.SelectObject.argtypes = [HDC, HGDIOBJ]
gdi32.SelectObject.restype = HGDIOBJ
gdi32.GetDIBits.argtypes = [HDC, HGDIOBJ, UINT, UINT, ctypes.c_void_p,
                            ctypes.c_void_p, UINT]
gdi32.GetDIBits.restype = ctypes.c_int
gdi32.DeleteObject.argtypes = [HGDIOBJ]
gdi32.DeleteDC.argtypes = [HDC]


class RECT(ctypes.Structure):
    _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long),
                ("r", ctypes.c_long), ("b", ctypes.c_long)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint), ("biWidth", ctypes.c_int),
                ("biHeight", ctypes.c_int), ("biPlanes", ctypes.c_ushort),
                ("biBitCount", ctypes.c_ushort), ("biCompression", ctypes.c_uint),
                ("biSizeImage", ctypes.c_uint), ("biXPelsPerMeter", ctypes.c_int),
                ("biYPelsPerMeter", ctypes.c_int), ("biClrUsed", ctypes.c_uint),
                ("biClrImportant", ctypes.c_uint)]


def grab_window(hwnd, out_png):
    from PIL import Image
    r = RECT()
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.r - r.l, r.b - r.t
    if w < 10 or h < 10:
        return None
    # 不 BringWindowToTop：PrintWindow 抓的是窗口自己的 DC，被遮挡也能拿到，
    # 置顶只会无谓地抢走用户屏幕。
    hdc = user32.GetWindowDC(hwnd)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mem, bmp)
    # PW_RENDERFULLCONTENT = 2：DirectComposition / GPU 渲染的窗口也抓得到
    ok = user32.PrintWindow(hwnd, mem, 2)
    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.biWidth, bi.biHeight = w, -h          # 负高 = top-down
    bi.biPlanes, bi.biBitCount, bi.biCompression = 1, 32, 0
    buf = ctypes.create_string_buffer(w * h * 4)
    got = gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)
    img = None
    if got:
        img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1)
        img.convert("RGB").save(out_png)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(hwnd, hdc)
    return (bool(ok), w, h, bool(got))


def raise_main(root, hwnd):
    """什么都不做 —— 保留函数名兼容旧调用。

    曾经这里会 -topmost + SetForegroundWindow + BringWindowToTop，把窗口硬弹到用户
    屏幕最前面（用户在刷视频，被反复打断）。PrintWindow(PW_RENDERFULLCONTENT) 本来
    就能抓被遮挡的窗口，根本不需要置顶，所以全部去掉。"""
    try:
        root.deiconify()
    except Exception:
        pass


def prep_shot(root, P, w=None, h=None):
    """把主窗设成确定尺寸并空转几轮事件，逼 Tk 整幅重画。
    Tk 只重绘"脏区"：窗口尺寸被外部改过后，PrintWindow 会拍到一条从没画过的黑区。
    ⚠ 窗口一旦被最小化，GetWindowRect 只回 158x26×DPI（≈237x39）这种废矩形，抓出来是
    废图；而 Tk 的 state() 缓存可能还报 "normal"，所以这里**无条件**走一遍还原。
    返回 (重新解析的 hwnd, tk宽, tk高)。"""
    w = w or P(1240)
    h = h or P(880)
    # SW_SHOWNOACTIVATE(4)：恢复/显示窗口但**不激活、不动 Z 序** —— 既能拿到合法的
    # GetWindowRect（最小化时只会回 158x26 废矩形），又不会盖到用户屏幕上（用户正在刷视频）。
    hw = user32.GetAncestor(root.winfo_id(), 2)
    user32.ShowWindow(hw, 4)
    root.geometry("+40+30")
    root.geometry("%dx%d+40+30" % (w, h))
    for _ in range(14):
        root.update()
        time.sleep(0.04)
    return (user32.GetAncestor(root.winfo_id(), 2),
            root.winfo_width(), root.winfo_height())


def grab_ok(root, P, path, want_w, tries=3):
    """抓主窗并和 Tk 自报尺寸对账；对不上（最小化残影/坏句柄）就还原重试。"""
    last = None
    for i in range(tries):
        hwnd, tw, th = prep_shot(root, P)
        r = grab_window(hwnd, path)
        last = (r, tw, th, hwnd)
        if r and abs(r[1] - want_w) <= P(30):
            return r, tw, th, hwnd, True
        time.sleep(0.4)
    return last[0], last[1], last[2], last[3], False


def pump(root, seconds):
    t0 = time.time()
    while time.time() - t0 < seconds:
        root.update()
        time.sleep(0.02)



def near(c, target, tol=6):
    return all(abs(c[i] - target[i]) <= tol for i in range(3))


def assert_rounded(img, hwnd, card, S):
    """像素级验证卡片圆角真的露出来了（不再靠肉眼看缩略图）。

    取三点对比：卡片几何左上角"弧外"那点必须是卡片外围底色（圆角被切掉的透明区），
    圆角弧内一点和卡面中心必须是卡面白。若弧外那点也是白色 → 圆角被上层方角控件盖住了。
    """
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    ox, oy = r.l, r.t
    x0 = card.winfo_rootx() - ox
    y0 = card.winfo_rooty() - oy
    w = card.winfo_width()
    h = card.winfo_height()
    if w < 40 or h < 40:
        return False, "卡片还没布局好 %dx%d" % (w, h)
    p = int(round(card._pad * S))
    rad = int(round(card._radius * S))
    # 只判两件事：弧外那点必须是卡片外围底色（说明角被切掉了）、弧内那点必须是卡面白。
    # 不取几何中心——那里常常压着内容（一言卡中心就是粉色气泡），会误判。
    outside = img.getpixel((x0 + p + 1, y0 + p + 1))
    inside = img.getpixel((x0 + p + rad, y0 + p + rad))
    # 右下同理，防"只修了左上"
    outside2 = img.getpixel((x0 + w - p - 2, y0 + h - p - 2))
    frame_col = tuple(int(str(card._bg_col)[i:i + 2], 16) for i in (1, 3, 5))
    ok = (near(outside, frame_col) and near(outside2, frame_col)
          and near(inside, (255, 255, 255)))
    return ok, "左上弧外%s / 右下弧外%s (应≈%s)；弧内%s" % (
        outside[:3], outside2[:3], frame_col, inside[:3])


def main():
    # 验收会点 chip、切角色 → apply_theme 默认写 config.json，先把用户偏好原样备份，退出前还原
    cfg_path = os.path.join(ROOT_DIR, "config.json")
    cfg_bak = None
    if os.path.exists(cfg_path):
        with open(cfg_path, "rb") as f:
            cfg_bak = f.read()

    def restore_cfg():
        if cfg_bak is not None:
            try:
                with open(cfg_path, "wb") as f:
                    f.write(cfg_bak)
            except Exception:
                pass

    # 和 main() 一致：先声明 DPI 感知，别让 Windows 拉伸位图
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    import tkinter as tk
    import video_downloader as V
    V.setup_logging()
    root = tk.Tk()
    root.title("SniffDL UI 验收")
    app = V.App(root)
    root.deiconify()
    pump(root, 4.0)
    root.update_idletasks()
    root.update()

    P, S = app.P, app.S
    print("S=%.3f  PIL=%s  theme=%s" % (S, bool(V._pil_modules()), app.theme))
    check("PIL 可用（渐变走贴图路径）", V._pil_modules() is not False,
          "否则全部回落纯色平涂")

    hwnd = user32.GetAncestor(root.winfo_id(), 2)   # GA_ROOT
    print("hwnd=%s  window=%dx%d" % (hwnd, root.winfo_width(), root.winfo_height()))
    raise_main(root, hwnd)
    pump(root, 0.6)

    if getattr(app, "_dress", None) is not None:
        # ---------- 1. 换装 chip 不溢出 + 第 6 只点得到 ----------
        sb = app.sidebar
        dr = app._dress
        cells = app._dress_imgs
        n = len(cells)
        size, gap, ring = app._dress_geo
        order = [k for k in V.THEME_ORDER if k in cells]
        print("侧栏宽=%d  换装容器宽=%d  chip=%d gap=%d ring=%d 只数=%d" % (
            sb.winfo_width(), dr.winfo_width(), size, gap, ring, n))
        total = n * (size + 2 * gap)
        avail = sb.winfo_width() - 2 * P(14)
        check("换装总宽 %d <= 侧栏可用 %d" % (total, avail), total <= avail)
        for k in order:
            c = cells[k]
            # 每个 cell 的右缘必须落在侧栏内（相对侧栏坐标）
            rx = c.winfo_rootx() + c.winfo_width() - sb.winfo_rootx()
            if rx > sb.winfo_width():
                check("chip %s 未越侧栏右界" % k, False, "右缘 %d > 侧栏 %d" % (rx, sb.winfo_width()))
                break
        else:
            check("全部 %d 只 chip 都在侧栏内" % n, True,
                  "右缘最大 %d / 侧栏 %d" % (
                      max(cells[k].winfo_rootx() + cells[k].winfo_width() - sb.winfo_rootx()
                          for k in order), sb.winfo_width()))

        # 命中判定（三重客观证据）。实测：Tk 的 root.event_generate 只派发给顶层，不会向
        # 子控件做命中路由（上一版拿它当"真实点击"是无效方法，不是产品问题）。
        class PT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        user32.WindowFromPoint.argtypes = [PT]
        user32.WindowFromPoint.restype = HWND
        decoy = order[0] if order[-1] != order[0] else order[1]
        app.apply_theme(decoy, save=False)
        pump(root, 0.8)
        last = cells[order[-1]]
        cx = last.winfo_rootx() + last.winfo_width() // 2
        cy = last.winfo_rooty() + last.winfo_height() // 2
        in_cell = (last.winfo_rootx() <= cx < last.winfo_rootx() + last.winfo_width()
                   and last.winfo_rooty() <= cy < last.winfo_rooty() + last.winfo_height())
        in_win = (root.winfo_rootx() <= cx < root.winfo_rootx() + root.winfo_width()
                  and root.winfo_rooty() <= cy < root.winfo_rooty() + root.winfo_height())
        check("第 6 只中心在自身矩形内且未越出窗口", in_cell and in_win,
              "chip 中心(%d,%d) 窗口 x∈[%d,%d] y∈[%d,%d]" % (
                  cx, cy, root.winfo_rootx(),
                  root.winfo_rootx() + root.winfo_width(), root.winfo_rooty(),
                  root.winfo_rooty() + root.winfo_height()))
        wf = user32.WindowFromPoint(PT(cx, cy))
        wf_root = user32.GetAncestor(wf, 2) if wf else None
        check("系统判定该像素归属主窗（没被裁到窗外）", wf_root == hwnd,
              "WindowFromPoint→%s 主窗→%s" % (wf_root, hwnd))
        hit = root.winfo_containing(cx, cy)
        check("Tk 命中该像素的控件正是第 6 只 chip", hit is last,
              "命中=%s" % (type(hit).__name__ if hit else None))
        before = app.theme
        last.event_generate("<Button-1>")
        pump(root, 1.0)
        check("第 6 只的点击绑定能真的换肤", app.theme == order[-1],
              "%s → %s" % (before, app.theme))

        # ---------- 2. chip 头像不被裁 + 渐变位图在用 ----------
        for k in order:
            c = cells[k]
            im = c._img
            want = size - 2 * ring
            if im.width() != want:
                check("chip %s 头像尺寸=预留位" % k, False,
                      "图 %dpx vs 预留 %dpx（超出即被画布裁边）" % (im.width(), want))
                break
        else:
            check("全部 chip 头像精确等于预留位（未裁边）", True,
                  "直径 %d in cell %d" % (want, size))
        check("渐变位图缓存已使用", len(V._GRAD_CACHE) > 0,
              "缓存 %d 张（logo/环/色条/热力图）" % len(V._GRAD_CACHE))
        check("头像重采样缓存已使用", len(V._PFP_CACHE) > 0, "%d 张" % len(V._PFP_CACHE))
    else:
        # 单一桌宠「大肥鱼」：换装区已退役 → 校验无六界切换残留
        check("单一桌宠：换装区已隐藏（无六界切换残留）",
              getattr(app, "_dress", None) is None and len(getattr(app, "_dress_imgs", {}) or {}) <= 1,
              "dress=%s imgs=%d" % (getattr(app,'_dress',None), len(getattr(app,'_dress_imgs',{}) or {})))

    # ---------- 2.5 自绘结果表 / 卡片圆角 / 设置宫格 ----------
    app.entries = [
        {"title": "验收样例标题 %d —— 很长的名字用来测试截断行为是否正常" % i,
         "ext": "mp4", "res": "1920x1080", "duration": 1234,
         "size": (58 - i * 7) * 1024 * 1024, "how": "站点解析",
         "url": "http://example.test/%d" % i} for i in range(6)]
    app.row_status = {0: "✓ 完成", 1: "78% · 3.2 MB/s · 剩2m",
                      2: "分片 8/20 · 41%", 3: "待下载",
                      4: "✗ 403 拒绝 · 冷却 30s 后重试", 5: "41% · 1.1 MB/s"}
    app._show_view("dl")
    app.refresh_table()
    pump(root, 1.0)
    tl = app.tree
    n_items = len(tl.find_withtag("r"))
    check("结果表自绘出行内容（%d 个绘制项）" % n_items, n_items > 30,
          "0 项 = 表是空的（上次就是 delete 覆盖了 Canvas.delete）")
    check("结果表重绘零异常", getattr(tl, "_last_err", None) is None,
          str(getattr(tl, "_last_err", None)))
    check("状态/列宽不溢出画布",
          all(x + w <= tl.winfo_width() + P(2)
              for x, w in tl._col_x().values()),
          "列总宽 %d vs 画布 %d" % (
              max(x + w for x, w in tl._col_x().values()), tl.winfo_width()))
    check("行数据可读回（Treeview 兼容 API 生效）",
          tl.set("1", "status").startswith("78%"), repr(tl.set("1", "status")))
    tl.selection_set("2")
    check("selection_set/get 一致", tl.selection() == ("2",), str(tl.selection()))
    tl.see("5")
    pump(root, 0.3)
    check("see() 不越界", 0 <= tl._top <= max(0, tl._content_h()), "top=%d" % tl._top)
    check("卡片圆角底图已生成（PIL 投影生效）", len(V._CARD_CACHE) > 0,
          "%d 张卡片底图" % len(V._CARD_CACHE))
    # 值班室纵向撑满：一言卡底缘应贴近内容区底部（上次底部一大片空白）
    app._show_view("dash")
    app._dash_refresh()
    pump(root, 0.8)
    try:
        vb = app.view_dash
        me_card = app._dash_me_card
        gap_px = (vb.winfo_rooty() + vb.winfo_height()) - (
            me_card.winfo_rooty() + me_card.winfo_height())
        ratio = gap_px / float(max(1, vb.winfo_height()))
        check("值班室底部留白 < 18%%（实测 %.1f%%）" % (ratio * 100), ratio < 0.18,
              "一言卡下方空 %d px / 视图 %d px" % (gap_px, vb.winfo_height()))
    except Exception as e:
        check("值班室留白可测", False, repr(e))
    # 设置宫格：多卡片 + 点一下就地展开（同时只一个展开）
    app._show_view("set")
    pump(root, 0.6)
    cards = getattr(app, "_set_cards", [])
    check("设置页已改成图标宫格（%d 组）" % len(cards), len(cards) >= 5)
    try:
        # 只看"内容体是否被 pack 上"（mapped）；winfo_height 依赖布局时机，不稳
        def _open_keys():
            return [k for k, c, b, h in app._set_cards if b.winfo_ismapped()]
        app._set_toggle("存储")
        pump(root, 1.0)
        check("点卡片能展开且同时只展开一个", _open_keys() == ["存储"], str(_open_keys()))
        app._set_toggle("下载")
        pump(root, 1.0)
        check("切到另一组会收起上一组", _open_keys() == ["下载"], str(_open_keys()))
        app._set_toggle("通用")
        pump(root, 0.5)
    except Exception as e:
        check("设置宫格交互", False, repr(e))
    app._show_view("dl")
    pump(root, 0.4)

    # ---------- 3. 逐视图 × 逐角色截图 ----------
    want_w = P(1240)
    shots, bad = 0, 0
    for key in V.App._VIEWS:
        app._show_view(key)
        if key == "dash":
            app._dash_refresh()
        p = os.path.join(SHOT_DIR, "ui_view_%s.png" % key)
        r, tw, th, hwnd, good = grab_ok(root, P, p, want_w)
        shots += 1
        if not good:
            bad += 1
            print("!! 截图尺寸异常 view=%s grab=%s tk自报=%dx%d hwnd=%s（期望宽≈%d）"
                  % (key, r, tw, th, hwnd, want_w))
        else:
            print("shot view %-6s → %s  %s" % (key, p, r))
    for tk_key in V.THEME_ORDER:
        try:
            app.apply_theme(tk_key, save=False)
        except Exception:
            print("apply_theme(%s) 异常:\n%s" % (tk_key, traceback.format_exc()))
            FAILS.append("apply_theme(%s) 抛异常" % tk_key)
            continue
        for key in ("dl", "dash", "set"):
            app._show_view(key)
            if key == "dash":
                app._dash_refresh()
            p = os.path.join(SHOT_DIR, "ui_theme_%s_%s.png" % (tk_key, key))
            r, tw, th, hwnd, good = grab_ok(root, P, p, want_w)
            shots += 1
            if not good:
                bad += 1
                print("!! 截图尺寸异常 theme=%s view=%s grab=%s tk自报=%dx%d"
                      % (tk_key, key, r, tw, th))
        print("shot theme %s ok  accent=%s accent2=%s cache=%d" % (
            tk_key, V.C["pink"], V.C.get("pink2"), len(V._GRAD_CACHE)))
    check("截图全部尺寸正常（无黑区/坏句柄）", bad == 0,
          "%d 张里 %d 张异常" % (shots, bad))
    # 「最近完成」：downloaded.json 有记录时值班室必须列出来（光有页面没功能=用户投诉点）
    try:
        app._show_view("dash")
        app._dash_refresh()
        pump(root, 0.8)
        rows = [r for r in getattr(app, "_dash_hist", [])
                if str(r._nm.cget("text")).strip()]
        check("值班室「最近完成」列出 %d 行" % len(rows), len(rows) > 0,
              "首行=%r" % (str(rows[0]._nm.cget("text"))[:44] if rows else ""))
    except Exception as e:
        check("值班室「最近完成」可渲染", False, repr(e))

    # ---------- 3.5 卡片圆角像素级验证（曾经"投影有、角是直角"被上层白 body 盖住） ----------
    try:
        from PIL import Image as _Im
        # 必须"先切到值班室、当场拍一张"再量：截图循环结束时停在别的视图，
        # 拿那张图量值班室的卡会采到空白处（三个点全是页面底色 → 假 FAIL）。
        app._show_view("dash")
        app._dash_refresh()
        hwnd, tw, th = prep_shot(root, P)
        shot = os.path.join(SHOT_DIR, "ui_corner_check.png")
        for _t in range(3):
            grab_window(hwnd, shot)
            if os.path.exists(shot):
                break
            time.sleep(0.4)
        im = _Im.open(shot).convert("RGB")
        for nm, card in (("侧栏 profile", getattr(app, "_me_card", None)),
                         ("值班室 一言", getattr(app, "_dash_me_card", None)),
                         ("值班室 KPI", getattr(app, "_dash_bar", {})
                                      .get("engine").master.master
                          if getattr(app, "_dash_bar", {}).get("engine") else None)):
            ok, why = assert_rounded(im, hwnd, card, S)
            check("卡片圆角 %s 真的露出来了" % nm, ok, why)
    except Exception as e:
        check("卡片圆角像素验证", False, repr(e))

    print("渐变缓存 %d 张 / 上限 %d，头像缓存 %d 张" % (
        len(V._GRAD_CACHE), V._GRAD_MAX, len(V._PFP_CACHE)))

    print("\n==== 断言汇总 ====")
    for l in NOTES:
        print(l)
    for l in FAILS:
        print(l)
    print("SHOTS=%d BAD=%d" % (shots, bad))
    print("RESULT=%s" % ("PASS" if not FAILS else "FAIL(%d)" % len(FAILS)))
    restore_cfg()
    sys.stdout.flush()
    os._exit(0 if not FAILS else 1)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.stdout.flush()
        os._exit(2)
