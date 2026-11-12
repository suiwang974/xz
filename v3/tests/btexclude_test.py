import sys,os;sys.path.insert(0,".");sys.path.insert(0,"tests");import ui_smoke_core;ui_smoke_core._stub_tk()
from core import engine as E
class F(E.Engine if hasattr(E,'Engine') else object):pass
import types
fn=[v for k,v in vars(E).items() if isinstance(v,type) and hasattr(v,'_bt_auto_select')][0]._bt_auto_select
def run(ex,files):
    s=types.SimpleNamespace(cfg={"bt_exclude":ex})
    return fn(s,files)
fs=[{"index":1,"path":"a/movie.mkv"},{"index":2,"path":"a/广告.url"},{"index":3,"path":"a/Readme.TXT"},{"index":4,"path":"a/sub/x.mp4"}]
ok=0;n=0
def chk(c,m):
    global ok,n;n+=1;ok+=bool(c);print("PASS" if c else "FAIL",m)
chk(run("*.url\n*.txt",fs)==[1,4],"后缀排除")
chk(run("",fs)==[1,2,3,4],"空规则全选")
chk(run("a/sub/*",fs)==[1,2,3],"路径通配")
chk(run("*",fs)==[1,2,3,4],"全排除回退全选")
chk(run("  \n*.URL",fs)==[1,3,4],"大小写+空行")
chk(run("*.mkv\n*.url",fs)==[3,4],"多规则")
print(ok,"/",n)
sys.exit(0 if ok==n else 1)
