# -*- coding: utf-8 -*-
"""网盘直链 v3 子集：包一层 tools/netdisk_e2e.py --v3（自起 v3 内核 + mock 网盘），要求全过且断言数不少于 23。
覆盖：LinkSwift/panlinker 组包、缺 Cookie 报 412 原因（没装 curl_cffi 时不被 ImportError 盖住）、过期、
同名旧失败任务再推、粘贴直链补同步 Cookie、curl 命令头框、多连接生效。"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def main():
    p = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "netdisk_e2e.py"), "--v3",
                        "--only", "A,B,C,D,G,H"], capture_output=True, text=True, timeout=300, cwd=ROOT)
    out = p.stdout
    m = re.search(r"\((\d+)/(\d+) PASS\)", out)
    npass, ntot = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    for ln in out.splitlines():
        if ln.startswith("FAIL"):
            print(ln[:200])
    ok = p.returncode == 0 and ntot >= 23 and npass == ntot
    print("%s netdisk_v3_test %d/%d" % ("PASS" if ok else "FAIL", npass, ntot))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
