import sys,datetime as dt,time
sys.path.insert(0,'.')
from core import queues as Q, clipboard as CB
ok=n=0
def chk(name,c):
    global ok,n
    n+=1; ok+=bool(c); print(("PASS " if c else "FAIL ")+name)
D=lambda h,m,wd=0: dt.datetime(2026,11,9+wd,h,m)  # 11-09 是周一
chk("窗口内",Q.in_window("02:00","07:00",D(3,0)))
chk("窗口外",not Q.in_window("02:00","07:00",D(8,0)))
chk("结束边界不含",not Q.in_window("02:00","07:00",D(7,0)))
chk("跨午夜 23:30",Q.in_window("23:00","07:00",D(23,30)))
chk("跨午夜 凌晨 03:00",Q.in_window("23:00","07:00",D(3,0)))
chk("跨午夜 中午外",not Q.in_window("23:00","07:00",D(12,0)))
chk("跨午夜凌晨算前一天: 周二03:00 days=[0]",Q.in_window("23:00","07:00",D(3,0,1),[0]))
chk("days 不含当天",not Q.in_window("02:00","07:00",D(3,0,1),[0]))
saved=[]; cfg={}
m=Q.QueueManager(cfg,lambda:saved.append(1))
chk("默认含主队列",any(q["id"]=="main" for q in m.list()))
r=m.upsert({"name":"  ","max":99,"schedule":{"enabled":True,"start":"2:5","stop":"7:00","days":[9,1,1]}})
chk("名称兜底/上限32/时间规整/days过滤去重",r["name"]=="新队列" and r["max"]==32 and r["schedule"]["start"]=="02:05" and r["schedule"]["days"]==[1])
try: m.upsert({"schedule":{"start":"abc"}}); bad=False
except Exception: bad=True
chk("时间格式错抛异常",bad)
try: m.remove("main"); bad=False
except ValueError: bad=True
chk("主队列不可删",bad)
m.remove(r["id"]); chk("删除队列",m.get(r["id"]) is None and saved)
def T(g,**k):
    d=dict(gid=g,queue="main",status="waiting",running=False,added=int(g[1:]));d.update(k);return d
s,p=m.plan([T("t1"),T("t2"),T("t3")],2)
chk("全局并发2只启2个且按加入顺序",s==["t1","t2"] and p==[])
s,_=m.plan([T("t1",running=True,status="active"),T("t2"),T("t3")],2)
chk("已运行1个再启1个",s==["t2"])
s,_=m.plan([T("t1",hold=True),T("t2")],2)
chk("稍后下载 hold 不自动跑",s==["t2"])
s,_=m.plan([T("t1",retry_at=time.time()+999),T("t2")],2)
chk("retry_at 未到跳过",s==["t2"])
m.paused_queues.add("main"); s,_=m.plan([T("t1")],2); m.paused_queues.clear()
chk("手动停止队列不启动",s==[])
cfg2={"queues":[{"id":"main","name":"主","max":1,"auto":True,"schedule":{"enabled":False}}]}
m2=Q.QueueManager(cfg2,lambda:None)
s,_=m2.plan([T("t1"),T("t2")],5); chk("队列上限1优先于全局",s==["t1"])
# 窗口关闭边沿
cfg3={"queues":[{"id":"main","name":"主","max":0,"auto":True,"schedule":{"enabled":True,"start":"02:00","stop":"07:00","days":list(range(7))}}]}
m3=Q.QueueManager(cfg3,lambda:None)
m3.plan([T("t1",running=True,status="active")],2,D(6,59))
s,p=m3.plan([T("t1",running=True,status="active")],2,D(7,0))
chk("窗口关闭边沿暂停在跑任务",p==["t1"] and s==[])
s,p=m3.plan([T("t1",sched_paused=True)],2,D(8,0)); chk("窗口外不启动",s==[])
s,p=m3.plan([T("t1",sched_paused=True)],2,D(2,0,1)); chk("窗口再开自动续上",s==["t1"])
# 剪贴板
got=[]; w=CB.ClipWatcher(got.append)
chk("多行链接整体送出",w.offer("http://a/1.zip\nhttp://a/2.zip") and got==["http://a/1.zip\nhttp://a/2.zip"])
chk("同文本去重",w.offer("http://a/1.zip\nhttp://a/2.zip") is False)
chk("新文本再触发",w.offer("magnet:?xt=urn:btih:abc") and len(got)==2)
w3=CB.ClipWatcher(lambda t:1/0)
try: r=w3.offer("http://x/y"); fine=True
except Exception: fine=False
chk("回调异常不外抛",fine)
print("%d/%d"%(ok,n)); sys.exit(0 if ok==n else 1)
