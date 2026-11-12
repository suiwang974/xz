# -*- coding: utf-8 -*-
"""把 index.html 里的内联 <script> 抽出来做语法体检（node --check）。

为什么必须查：webview 界面是单文件内联 JS，一处括号没配对 → 整个页面白屏且**没有任何
Python 侧报错**，日志里干干净净，只有用户看得到。改完 index.html 必须先过这道静态关。
"""
import os
import re
import subprocess
import sys
import tempfile

HTML = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "ui_web", "index.html")


def main():
    src = open(HTML, encoding="utf-8").read()
    blocks = re.findall(r"<script[^>]*>(.*?)</script>", src, re.S | re.I)
    print("内联 script 块 = %d 个，总 %d 字符" % (len(blocks), sum(len(b) for b in blocks)))
    bad = 0
    for i, b in enumerate(blocks):
        if not b.strip() or "src=" in b[:0]:
            continue
        p = os.path.join(tempfile.gettempdir(), "snifftdl_js_check_%d.js" % i)
        with open(p, "w", encoding="utf-8") as f:
            f.write(b)
        r = subprocess.run(["node", "--check", p], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        ok = r.returncode == 0
        print("  block#%d (%d 字符) %s%s" % (i, len(b), "SYNTAX OK" if ok else "SYNTAX FAIL",
                                             "" if ok else "\n" + (r.stderr or "")[:600]))
        if not ok:
            bad += 1
        try:
            os.remove(p)
        except Exception:
            pass
    # 顺手确认关键 DOM id / 函数都还在（改 HTML 时最容易把 id 改名把 JS 打断）
    need_ids = ["qrows", "qcount", "qempty", "qtot", "qbar", "qclear_done", "qclear_all"]
    miss = [x for x in need_ids if ('id="%s"' % x) not in src]
    print("队列视图必备 id 缺失：%s" % (miss or "无"))
    for fn in ("function qrow", "function renderQueue", "function refreshQueue",
               "sniffdl.clearQueue", "aria2.unpause", "aria2.forceRemove", 'class="h6"',
               ".qrow .ops"):
        print("  %-24s %s" % (fn, "在" if fn in src else "!! 缺"))
    print("JSCHECK RESULT=%s" % ("PASS" if bad == 0 and not miss else "FAIL"))
    return 0 if bad == 0 and not miss else 1


if __name__ == "__main__":
    sys.exit(main())
