import sys,errno
sys.path.insert(0,'.')
sys.path.insert(0,"tests"); import ui_smoke_core; ui_smoke_core._stub_tk()
from core import engine as E
ok=0
def chk(n,c):
    global ok
    print(("PASS " if c else "FAIL ")+n); ok+=bool(c)
f=E.Engine._is_diskfull if hasattr(E,'Engine') else None
cls=[v for v in vars(E).values() if isinstance(v,type) and hasattr(v,'_is_diskfull')][0]
chk("ENOSPC",cls._is_diskfull(OSError(errno.ENOSPC,"x")))
chk("msg",cls._is_diskfull(RuntimeError("No space left on device")))
chk("中文",cls._is_diskfull(RuntimeError("磁盘空间不足")))
chk("other",not cls._is_diskfull(RuntimeError("timeout")))
t={}
class X(cls):
    def __init__(s): pass
try: X()._hard_fail(t,"m")
except Exception as e: chk("hardfail abort",type(e).__name__=="_AbortSignal" and t.get("_hard_fail")=="m")
print(ok,"/5"); sys.exit(0 if ok==5 else 1)
