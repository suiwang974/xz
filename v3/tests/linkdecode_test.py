import base64, sys
sys.path.insert(0, ".")
from core import linkdecode as LD, batch as B
e = lambda s: base64.b64encode(s.encode()).decode()
u = "http://a.com/文件.zip"
R = []
def ck(n, ok): R.append(ok); print("PASS" if ok else "FAIL", n)
ck("thunder", LD.decode("thunder://" + e("AA" + u + "ZZ")) == u)
ck("thunder 大小写", LD.decode("THUNDER://" + e("AA" + u + "ZZ")) == u)
ck("flashget", LD.decode("flashget://" + e("[FLASHGET]" + u + "[FLASHGET]") + "&abc") == u)
ck("qqdl", LD.decode("qqdl://" + e(u)) == u)
ck("普通链接不动", LD.decode("http://x/y") == "http://x/y")
ck("坏 base64 原样", LD.decode("thunder://!!!") == "thunder://!!!")
ck("批量文本识别", B.expand_text("thunder://" + e("AAhttp://b.com/1.binZZ"))[0] == ["http://b.com/1.bin"])
print("%d/%d" % (sum(R), len(R))); sys.exit(0 if all(R) else 1)
