import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import rpc_server as R
bad = 0
def chk(n, c):
    global bad
    print(("PASS " if c else "FAIL ") + n); bad += (not c)
src = open(R.__file__, encoding="utf-8").read()
chk("getServers 不再恒空", 'if method == "aria2.getServers"' in src)
chk("getPeers 读后端", "rpc_peers" in src)
class B:
    def __init__(s, st): s.st = st
    def rpc_struct(s, g): return {"gid": g, "status": s.st, "uri": "http://a/x.bin", "name": "x.bin", "dir": "/tmp", "downloadSpeed": 123} if g == "g1" else None
    def rpc_peers(s, g): return [{"ip": "1.2.3.4", "port": "6881"}]
srv = R.RpcServer(B("active"))
def call(b, m, g):
    srv.backend = b
    return srv._dispatch(m, [g])
r = call(B("active"), "aria2.getServers", "g1")
chk("active 返回服务器含 uri", r and r[0]["servers"][0]["uri"] == "http://a/x.bin")
chk("速度字段", r[0]["servers"][0]["downloadSpeed"] == "123")
chk("非 active 返回空", call(B("paused"), "aria2.getServers", "g1") == [])
chk("getPeers 返回后端数据", call(B("active"), "aria2.getPeers", "g1")[0]["ip"] == "1.2.3.4")
try:
    call(B("active"), "aria2.getPeers", "nope"); chk("未知 gid 报错", False)
except R.RpcError:
    chk("未知 gid 报错", True)
sys.exit(1 if bad else 0)
