import subprocess, sys, os
R=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
p=subprocess.run([sys.executable,os.path.join(R,"sniffdl_core.py"),"--selftest"],capture_output=True,text=True,timeout=90,cwd=R)
o=p.stdout
chk=[("退出码0",p.returncode==0),("PASS行","SELFTEST PASS" in o),("aria2c项","aria2c" in o),("数据目录项","数据目录可写" in o),("端口项","内核端口" in o),("无FAIL","FAIL " not in o)]
for n,k in chk: print(("ok " if k else "NOT ok ")+n)
sys.exit(0 if all(k for _,k in chk) else 1)
