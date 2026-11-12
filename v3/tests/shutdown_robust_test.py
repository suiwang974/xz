# -*- coding: utf-8 -*-
"""B8（Windows 实测）：内核带着在下的任务被宿主拖着退出时 runtime.json 必须被清掉。
shutdown() 里任何一步抛错（_rpc_stop / _bt_shutdown / keepawake / root.stop）都不能让 runtime.json 残留，
且后面的步骤仍要继续。退出码 0 = 全过。"""
import os, sys, shutil, json
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, 'tests'))
import ui_smoke_core; ui_smoke_core._stub_tk()  # 无 tk 环境打桩
R = []
def chk(n, c, d=""):
    R.append(bool(c)); print(("PASS " if c else "FAIL ") + n + ("" if c else "  ← %s" % str(d)[:300]), flush=True)

def mk(tag):
    d = "/tmp/sdlv3_shut_" + tag
    shutil.rmtree(d, ignore_errors=True); os.makedirs(d)
    os.environ["SNIFFDL_DATA_DIR"] = d
    from core.engine import Engine
    rt = os.path.join(d, "runtime.json")
    eng = Engine(runtime_path=rt, ui_token="t")
    eng.start(port=0)
    return eng, rt

def main():
    os.environ["SNIFFDL_DRYRUN_OS"] = "1"
    eng, rt = mk("a")
    chk("启动后 runtime.json 存在", os.path.exists(rt))
    called = []
    def boom(): called.append("rpc_stop"); raise RuntimeError("连接还没断")
    eng._rpc_stop = boom
    orig_set = eng.keepawake.set
    def ka(v): called.append("keepawake"); orig_set(v)
    eng.keepawake.set = ka
    eng.shutdown()
    chk("_rpc_stop 抛错 → runtime.json 仍被删除", not os.path.exists(rt), os.listdir(os.path.dirname(rt)))
    chk("_rpc_stop 抛错 → 后续 keepawake 关闭仍执行", called == ["rpc_stop", "keepawake"], called)
    eng2, rt2 = mk("b")
    def boom2(): raise OSError("x")
    eng2._bt_shutdown = boom2
    eng2.root.stop = boom2
    eng2.shutdown()
    chk("_bt_shutdown 与 root.stop 都抛错 → runtime.json 仍被删除", not os.path.exists(rt2))
    eng3, rt3 = mk("c")
    eng3.shutdown()
    chk("正常路径 runtime.json 被删除", not os.path.exists(rt3))
    eng3.shutdown()
    chk("shutdown 可重复调用不抛错", True)
    ok = all(R); print("%d/%d PASS" % (sum(R), len(R)), flush=True)
    os._exit(0 if ok else 1)

if __name__ == "__main__":
    main()
