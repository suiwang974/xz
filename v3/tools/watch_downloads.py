# -*- coding: utf-8 -*-
"""盯住下载目录的每一次变动，并把变动和"是谁提交的"对上。

    python tools/watch_downloads.py            # 持续盯，Ctrl+C 停
    python tools/watch_downloads.py --once     # 只快照一次现状

每发现一个文件新建/增长/消失，就打一行：时间、文件、大小变化、
以及从 sniffdl.log 里刚出现的日志行（接收任务 / 忽略重复 / BT 接管 / 完成），
这样"我没点它怎么多了个文件"能当场看到源头链接。
另外每次扫描都检查有没有孤儿 aria2c（SniffDL 不在、aria2c 还活着 = 后台偷跑）。
"""
import argparse, glob, os, subprocess, sys, time

DIR = r"C:\Users\王伟\Videos\SniffDL"
LOG = r"D:\wws\sb\dist\sniffdl.log"
OUT = r"D:\wws\sb\downloads_watch.log"


def snap():
    d = {}
    for p in glob.glob(os.path.join(DIR, "**", "*"), recursive=True):
        try:
            if os.path.isfile(p):
                st = os.stat(p)
                d[p] = (st.st_size, round(st.st_mtime, 3))
        except OSError:
            pass
    return d


def proc_running(name):
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq %s" % name],
                             capture_output=True, text=True, timeout=10).stdout
        return ("%s" % name.lower()) in out.lower()
    except Exception:
        return False


def tail_log(mark):
    """返回日志新增的行（从 mark 行之后），并回传新的行号。"""
    try:
        with open(LOG, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return [], mark
    new = lines[mark:]
    return new, len(lines)


KEYWORDS = ("接收任务", "忽略重复提交", "已下载过", "接管任务", "✓ 完成", "异常", "SniffDL 启动")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=float, default=1.0)
    a = ap.parse_args()
    prev = snap()
    mark = 0
    _, mark = tail_log(0)                     # 从当前末尾开始，只看新日志
    fh = open(OUT, "a", encoding="utf-8", buffering=1)

    def emit(s):
        line = "%s  %s" % (time.strftime("%H:%M:%S"), s)
        print(line, flush=True)
        fh.write(time.strftime("%Y-%m-%d ") + line + "\n")

    emit("开始盯 %s（当前 %d 个文件）；日志基线行号 %d" % (DIR, len(prev), mark))
    if a.once:
        for p, (sz, _m) in sorted(prev.items()):
            emit("  现有 %12d  %s" % (sz, os.path.relpath(p, DIR)))
        return
    while True:
        time.sleep(a.interval)
        cur = snap()
        for p, (sz, mt) in cur.items():
            old = prev.get(p)
            if old is None:
                emit("★ 新建 %12d  %s" % (sz, os.path.relpath(p, DIR)))
            elif sz != old[0]:
                emit("↑ 增长 %12d → %12d (+%s)  %s" % (
                    old[0], sz, "{:,}".format(sz - old[0]), os.path.relpath(p, DIR)))
        for p in prev:
            if p not in cur:
                emit("✗ 消失 %s" % os.path.relpath(p, DIR))
        if len(cur) != len(prev):
            emit("  目录现有 %d 个文件" % len(cur))
        prev = cur
        new, mark = tail_log(mark)
        for l in new:
            if any(k in l for k in KEYWORDS):
                emit("  [日志] %s" % l.split("INFO ")[-1][:160])
        if not proc_running("SniffDL.exe") and proc_running("aria2c.exe"):
            emit("!! 孤儿 aria2c：SniffDL 已退出但 aria2c 还在跑 = 后台偷下载，须立刻杀")


if __name__ == "__main__":
    main()
