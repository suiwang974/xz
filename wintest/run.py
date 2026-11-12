# -*- coding: utf-8 -*-
"""跑一个测试命令：输出原样（按 UTF-8 解码）同时打到控制台和 wintest_out/log_<name>.txt，记录退出码。
    python run.py <name> <cmd...>"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import OUT  # noqa: E402

name, cmd = sys.argv[1], sys.argv[2:]
env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
t0 = time.time()
logp = os.path.join(OUT, "log_%s.txt" % name)
with open(logp, "wb") as lf:
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    for line in iter(p.stdout.readline, b""):
        lf.write(line)
        lf.flush()
        try:
            txt = line.decode("utf-8")
        except UnicodeDecodeError:
            txt = line.decode("mbcs", "replace")
        sys.stdout.buffer.write(txt.encode("utf-8", "replace"))
        sys.stdout.flush()
    rc = p.wait()
st = os.path.join(OUT, "steps.json")
d = json.load(open(st, encoding="utf-8")) if os.path.exists(st) else []
d.append({"name": name, "cmd": " ".join(cmd), "rc": rc, "sec": round(time.time() - t0, 1)})
json.dump(d, open(st, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("== %s rc=%d (%.0fs)" % (name, rc, time.time() - t0))
sys.exit(rc)
