import sys,os,tempfile,json
sys.path.insert(0,'.'); sys.path.insert(0,"tests"); import ui_smoke_core; ui_smoke_core._stub_tk()
from core import engine as E, clipboard as CB
ok=n=0
def chk(name,c):
    global ok,n
    n+=1; ok+=bool(c); print(("PASS " if c else "FAIL ")+name)
got=[]; en=[True]
w=CB.ClipWatcher(got.append, enabled=lambda: en[0])
chk("offer 新文本触发",w.offer("http://a.com/x.zip")is True and got==["http://a.com/x.zip"])
chk("重复文本不重复触发",w.offer("http://a.com/x.zip")is False and len(got)==1)
chk("空白忽略",w.offer("   ")is False)
en[0]=False
chk("关闭监视不触发",w.offer("http://b.com/y.zip")is False and len(got)==1)
en[0]=True
chk("host 来源刷新 last_host_offer",w.last_host_offer>0)
w2=CB.ClipWatcher(got.append); w2.offer("t1",source="native")
chk("native 不刷新 host 时间戳",w2.last_host_offer==0.0)
cls=[v for v in vars(E).values() if isinstance(v,type) and hasattr(v,'get_autostart') and hasattr(v,'_on_clip_text')][0]
e=cls.__new__(cls); e.cfg={}; e._save_cfg=lambda:None
chk("非Windows autostart 默认关",e.get_autostart() is False)
e.set_autostart(True); chk("set_autostart(True) 读回",e.get_autostart() is True and e.cfg["autostart"] is True)
e.set_autostart(False); chk("set_autostart(False) 读回",e.get_autostart() is False)
d=tempfile.mkdtemp(); open(os.path.join(d,"启动 SniffDL.vbs"),"w").close()
E.V.BASE_DIR=d
c=e._autostart_cmd(); chk("自启命令走宿主 vbs --tray",c.startswith("wscript.exe") and c.endswith("--tray") and d in c)
print("%d/%d"%(ok,n)); sys.exit(0 if ok==n else 1)
