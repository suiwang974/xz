import sys,errno,tempfile
sys.path.insert(0,'.'); sys.path.insert(0,"tests"); import ui_smoke_core; ui_smoke_core._stub_tk()
from core import engine as E
import video_downloader as V
cls=[v for v in vars(E).values() if isinstance(v,type) and hasattr(v,'_is_diskfull')][0]
ok=0;n=0
def chk(name,c):
    global ok,n
    n+=1; ok+=bool(c); print(("PASS " if c else "FAIL ")+name)
e=cls.__new__(cls)
t={}
# _hard_fail 抛 AbortSignal 并记录原因；无任务时抛 RuntimeError
try: e._hard_fail(t,"磁盘空间不足，已停止：x")
except V._AbortSignal: chk("hard_fail abort",t.get("_hard_fail","").startswith("磁盘"))
try: e._hard_fail(None,"m"); chk("none",False)
except RuntimeError as x: chk("hard_fail none->RuntimeError",str(x)=="m")
# _rpc_direct_file：AbortSignal 带 _hard_fail → RuntimeError(msg)，且只调一次底层
calls=[]
orig=V.__dict__
base=[c for c in cls.__mro__ if c.__name__!=cls.__name__ and hasattr(c,'_rpc_direct_file')][0]
def fake(self,t,prog,headers):
    calls.append(1); t["_hard_fail"]="磁盘空间不足，已停止：ENOSPC"; raise V._AbortSignal()
orig_fn=base._rpc_direct_file
base._rpc_direct_file=fake
e._is_post=lambda t:False
e._throttled_prog=lambda t,p:p
try:
    e._rpc_direct_file({"url":"u"},None,{}); chk("raises",False)
except RuntimeError as x:
    chk("RuntimeError 含磁盘",("磁盘" in str(x)))
chk("底层只调一次(不回退单连接)",len(calls)==1)
t2={}
def fake2(self,t,prog,headers): raise V._AbortSignal()
base._rpc_direct_file=fake2
try: e._rpc_direct_file(t2,None,{}); chk("passthru",False)
except V._AbortSignal: chk("无hard_fail时原样上抛(暂停)",True)
base._rpc_direct_file=orig_fn
chk("ENOSPC 识别",cls._is_diskfull(OSError(errno.ENOSPC,"x")))
print("%d/%d"%(ok,n)); sys.exit(0 if ok==n else 1)
