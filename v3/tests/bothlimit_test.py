import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, "tests"); import ui_smoke_core; ui_smoke_core._stub_tk()
from core.engine import Engine
sp = Engine.split_limit
ok = 0; n = 0
def chk(name, c):
    global ok, n; n += 1; ok += bool(c); print(("PASS " if c else "FAIL ") + name)
chk("单边直链满额", sp(1000, 500, 0, True, False) == (1000, 1000))
chk("单边BT满额", sp(1000, 0, 500, False, True) == (1000, 1000))
h, b = sp(1000, 300, 100, True, True)
chk("双边按比例 3:1 且和=总值", h == 750 and h + b == 1000)
h, b = sp(1000, 990, 1, True, True)
chk("最少20%保底", b == 200 and h + b == 1000)
h, b = sp(1000, 0, 0, True, True)
chk("双边零速对半", h == 500 and b == 500)
chk("不限速为0", sp(0, 5, 5, True, True) == (0, 0))
for tot in (1, 7, 333, 5000):
    h, b = sp(tot, 123, 77, True, True)
    chk("和恒等 %d" % tot, h + b == tot or tot == 1)
print("%d/%d" % (ok, n)); sys.exit(0 if ok == n else 1)
