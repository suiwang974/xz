import re,sys,os
t=open('docs/wintest用例清单.md',encoding='utf8').read()
r=open('docs/wintest对账.md',encoding='utf8').read()
ok=n=0
def chk(name,c):
    global ok,n
    n+=1; ok+=bool(c); print(("PASS " if c else "FAIL ")+name)
cases=[int(x) for x in re.findall(r'^(\d+)\. ',t,re.M)]
chk("清单 ≥23 条且编号连续",len(cases)>=23 and cases==list(range(1,len(cases)+1)))
refs=set(re.findall(r'(tests/[\w]+\.(?:py|js)|tools/[\w]+\.py)',t+r))
miss=[f for f in refs if not os.path.isfile(f)]
chk("清单/对账里引用的 %d 个测试文件都存在"%len(refs),not miss and len(refs)>=15)
if miss: print(miss)
# 每个用例号都在对账表里出现
covered=set()
for row in re.findall(r'^\| ([\d/]+) ',r,re.M):
    covered|={int(x) for x in row.split('/')}
chk("每个用例编号在对账表里都有对应行",set(cases)<=covered)
if set(cases)-covered: print("缺",sorted(set(cases)-covered))
chk("对账表声明了 Windows 未实测","Windows 未实测" in r)
print(ok,"/",n); sys.exit(0 if ok==n else 1)
