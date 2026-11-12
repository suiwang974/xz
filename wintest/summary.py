# -*- coding: utf-8 -*-
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import OUT  # noqa: E402

lines = ["# SniffDL v3 Windows 实测汇总", ""]
st = os.path.join(OUT, "steps.json")
if os.path.exists(st):
    lines += ["| 步骤 | 退出码 | 用时s |", "|---|---|---|"]
    for s in json.load(open(st, encoding="utf-8")):
        lines.append("| %s | %s | %s |" % (s["name"], "✅ 0" if s["rc"] == 0 else "❌ %d" % s["rc"], s["sec"]))
for f in sorted(glob.glob(os.path.join(OUT, "result_*.json"))):
    r = json.load(open(f, encoding="utf-8"))
    lines += ["", "## " + r["suite"], ""]
    for i in r["items"]:
        mark = "⏭️ SKIP" if i.get("skip") else ("✅" if i["ok"] else "❌")
        lines.append("- %s %s %s" % (mark, i["name"], "" if i["ok"] else "— " + i["detail"][:300].replace("\n", " ")))
txt = "\n".join(lines) + "\n"
open(os.path.join(OUT, "summary.md"), "w", encoding="utf-8").write(txt)
if os.environ.get("GITHUB_STEP_SUMMARY"):
    open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8").write(txt)
print(txt)
