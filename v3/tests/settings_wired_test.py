# -*- coding: utf-8 -*-
"""设置页每个开关都有人读（A11 收尾的静态守卫）：schema 里的每个 key 必须在内核/界面/外挂代码里被引用，
动态拼接的（hook_<事件>）按前缀+事件名核对；不允许出现「设置页有开关、代码里没人读」。
用法：source env/setup.sh && source env/v3env.sh && python3 tests/settings_wired_test.py     退出码 0 = 全过
"""
import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from core import settings as S  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  ← %s" % str(detail)[:400]))


def sources():
    files = []
    for pat in ("core/*.py", "sdl_speed/*.py", "ui/js/**/*.js", "pet_electron/app/*.js", "extension/*.js"):
        files += glob.glob(os.path.join(HERE, pat), recursive=True)
    for f in ("video_downloader.py", "rpc_server.py", "sniffdl_core.py", "native_host.py", "pet_bridge.py", "netdisk_rules.py"):
        files.append(os.path.join(HERE, f))
    out = []
    for f in files:
        if f.endswith(os.path.join("core", "settings.py")) or not os.path.isfile(f):
            continue
        out.append((f, open(f, encoding="utf-8", errors="ignore").read()))
    return out


def main():
    src = sources()
    keys = [d["key"] for d in S.SCHEMA]
    check("schema 有 100+ 个设置项", len(keys) >= 100, len(keys))
    dynamic = {k: k.split("_", 1)[0] + "_" for k in keys if k.startswith("hook_")}
    unread = []
    for k in keys:
        if k in dynamic:
            ev = k[len("hook_"):]
            ok = any('"hook_" + event' in t or "'hook_' + event" in t for _, t in src) and \
                any(('"%s"' % ev) in t for _, t in src if t.find('_hook("') >= 0 or t.find("_hook(") >= 0)
            if not ok:
                unread.append(k)
            continue
        pat = re.compile(r"""["']%s["']|\.%s\b|\b%s\s*[:=]""" % ((re.escape(k),) * 3))
        if not any(pat.search(t) for _, t in src):
            unread.append(k)
    check("每个设置项都有代码在读（未接线：%s）" % (",".join(unread) or "无"), not unread, unread)
    # 反向：外挂三项必须由 core/sidecar.py + engine 读取
    eng = open(os.path.join(HERE, "core", "engine.py"), encoding="utf-8").read()
    for k in ("sidecar_m3u8", "sidecar_bili", "sidecar_paths", "hls_native", "hls_max_height"):
        check("引擎读取 %s" % k, 'S.get(self.cfg, "%s")' % k in eng)
    ui = open(os.path.join(HERE, "ui", "js", "main.js"), encoding="utf-8").read()
    check("界面读取 sound（完成提示音）", "S.settings?.sound" in ui)
    n = len(RESULTS)
    ok = sum(RESULTS)
    print("\nsettings_wired_test: %d/%d 通过" % (ok, n))
    return 0 if ok == n else 1


if __name__ == "__main__":
    sys.exit(main())
