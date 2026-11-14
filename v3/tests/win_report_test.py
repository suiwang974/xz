import sys,os
sys.path.insert(0,os.path.join(os.path.dirname(__file__),"..","tools"))
import make_win_report as m
r=m.make();n=0
def ck(c):
    global n;assert c;n+=1
ck("SniffDL" in r);ck("IDM" in r);ck("aria2" in r)
ck(r.count("\n| ")>=len(m.CASES)+1)
ck("未实测" in r);ck(m.make(["x"]).count("| x |")==1)
print("win_report_test %d/%d"%(n,n))
