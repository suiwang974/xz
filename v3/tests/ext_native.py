import json, os, socket, struct, subprocess, sys, tempfile, time
ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0,ROOT)
import native_host as nh
ok=0;fail=0
def chk(n,c):
    global ok,fail
    print(("PASS " if c else "FAIL ")+n); ok+=c; fail+=(not c)
def rpc(proc,m):
    b=json.dumps(m).encode(); proc.stdin.write(struct.pack("<I",len(b))+b); proc.stdin.flush()
    n=struct.unpack("<I",proc.stdout.read(4))[0]; return json.loads(proc.stdout.read(n))
s=socket.socket(); s.bind(("127.0.0.1",0)); port=s.getsockname()[1]; s.close()
fake=os.path.join(tempfile.mkdtemp(),"fake.py"); cnt=fake+".cnt"
open(fake,"w").write("import socket,time,sys\nopen(%r,'a').write('x')\ns=socket.socket();s.setsockopt(1,2,1);s.bind(('127.0.0.1',%d));s.listen(5)\ntime.sleep(30)\n"%(cnt,port))
env=dict(os.environ,SNIFFDL_LAUNCH_CMD=sys.executable+" "+fake)
p=subprocess.Popen([sys.executable,os.path.join(ROOT,"native_host.py")],stdin=subprocess.PIPE,stdout=subprocess.PIPE,env=env)
r=rpc(p,{"action":"ping","port":port}); chk("ping 回包",r.get("ok") and r.get("host")=="com.sniffdl.host")
chk("ping 内核未跑=false",r.get("running") is False)
r=rpc(p,{"action":"launch","port":port}); chk("launch 拉起成功",r.get("ok") and r.get("started"))
chk("launch 后端口可连",nh.port_open(port))
r=rpc(p,{"action":"launch","port":port}); chk("重复 launch 不重起",r.get("ok") and not r.get("started"))
chk("只起了一个进程",open(cnt).read()=="x")
chk("未知指令报错",rpc(p,{"action":"zzz"}).get("ok") is False)
d=tempfile.mkdtemp(); f=nh.install(["abcdefghijklmnopabcdefghijklmnop"],[d])[0]
m=json.load(open(f)); chk("清单注册文件",m["name"]=="com.sniffdl.host" and m["allowed_origins"][0].startswith("chrome-extension://") and os.path.exists(m["path"]))
mf=json.load(open(os.path.join(ROOT,"extension/manifest.json"))); chk("扩展声明 nativeMessaging","nativeMessaging" in mf["permissions"])
p.stdin.close(); p.wait(5)
subprocess.run(["pkill","-f",fake])
print("RESULT",ok,"/",ok+fail); sys.exit(1 if fail else 0)
