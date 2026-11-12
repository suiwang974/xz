# -*- coding: utf-8 -*-
"""测试期网络硬闸：任何想连外网的动作直接抛错，而不是悄悄把用户的流量跑掉。

为什么需要它：曾经有个"只读诊断"脚本只把 _http_aria2c / curl_fetch 打了桩，
但 _rpc_direct_file 里还有别的网络出口没堵，结果真的下载了 121MB。
所以别指望"我记得堵住了所有出口"——直接在 socket 层封掉，漏网的会当场炸出来。

用法：
    import netguard; netguard.install()          # 只允许 127.0.0.1 / localhost
    before = netguard.snapshot_dir(r"C:\\...\\SniffDL")
    ...跑你要测的代码...
    netguard.assert_no_new_files(before)         # 断言目录没被动过
"""
import os, socket

ALLOWED = {"127.0.0.1", "localhost", "::1", "0.0.0.0", ""}
BLOCKED = []          # 记录被拦下的目标，便于事后确认"确实想跑没跑成"

_orig_connect = socket.socket.connect
_orig_connect_ex = socket.socket.connect_ex
_orig_getaddrinfo = socket.getaddrinfo


def _check(host, what):
    if isinstance(host, str) and host not in ALLOWED:
        BLOCKED.append("%s %s" % (what, host))
        raise RuntimeError("[netguard] 测试禁止触网：拦下 %s → %s" % (what, host))


def install(extra_allow=()):
    ALLOWED.update(set(extra_allow))

    def connect(self, addr, *a, **k):
        if isinstance(addr, tuple) and addr:
            _check(addr[0], "connect")
        return _orig_connect(self, addr, *a, **k)

    def connect_ex(self, addr, *a, **k):
        if isinstance(addr, tuple) and addr:
            _check(addr[0], "connect_ex")
        return _orig_connect_ex(self, addr, *a, **k)

    def getaddrinfo(host, *a, **k):
        _check(host, "getaddrinfo")
        return _orig_getaddrinfo(host, *a, **k)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.getaddrinfo = getaddrinfo
    return BLOCKED


def uninstall():
    socket.socket.connect = _orig_connect
    socket.socket.connect_ex = _orig_connect_ex
    socket.getaddrinfo = _orig_getaddrinfo


def snapshot_dir(root):
    """目录快照：{路径: (大小, mtime)}，用于事后断言"什么都没被写进来"。"""
    d = {}
    if not os.path.isdir(root):
        return d
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(dirpath, f)
            try:
                st = os.stat(p)
                d[p] = (st.st_size, round(st.st_mtime, 3))
            except OSError:
                pass
    return d


def assert_no_new_files(before, root=None):
    """断言目录没有新增/增长的文件。返回问题列表（空 = 干净）。"""
    after = snapshot_dir(root) if root else snapshot_dir(next(iter(before), "")) if before else {}
    after = snapshot_dir(root) if root else after
    bad = []
    for p, (sz, mt) in after.items():
        old = before.get(p)
        if old is None:
            bad.append("新增 %s (%d 字节)" % (p, sz))
        elif sz != old[0]:
            bad.append("增长 %s %d→%d" % (p, old[0], sz))
    return bad
