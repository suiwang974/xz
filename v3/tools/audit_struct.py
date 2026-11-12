# -*- coding: utf-8 -*-
"""结构体检：对比两个版本的大文件（备份 vs 改后），用 ast 逐函数/逐方法统计语法节点数。
用途：在 1.5 万行的文件里插方法时，最阴的事故是「把 def 写在某个方法体中间」——
后面整段代码被吞进新方法，py_compile 不报错、程序也能跑，但流程静默断掉。
这个脚本只比节点数，能一眼看出"哪些方法被我改动了、有没有方法凭空少了身体"。
用法：python tools/audit_struct.py <旧文件> <新文件>
退出码 0 = 差异只在预期内（只打印清单，由人判断）；2 = 文件解析失败。
"""
import ast
import io
import sys


def walk(path):
    src = io.open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    out = {}

    def count(node):
        return sum(1 for _ in ast.walk(node))

    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[n.name] = (count(n), n.lineno)
        elif isinstance(n, ast.ClassDef):
            for m in n.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out["%s.%s" % (n.name, m.name)] = (count(m), m.lineno)
                elif isinstance(m, ast.Assign):
                    for tg in m.targets:
                        if isinstance(tg, ast.Name):
                            out["%s.%s(attr)" % (n.name, tg.id)] = (count(m), m.lineno)
    return out


def main():
    old_p, new_p = sys.argv[1], sys.argv[2]
    try:
        a, b = walk(old_p), walk(new_p)
    except SyntaxError as e:
        print("解析失败:", e)
        return 2
    gone = sorted(set(a) - set(b))
    new = sorted(set(b) - set(a))
    grown, shrunk = [], []
    for k in sorted(set(a) & set(b)):
        if a[k][0] != b[k][0]:
            d = b[k][0] - a[k][0]
            (grown if d > 0 else shrunk).append((k, a[k][0], b[k][0], d))
    print("== 删除的函数/方法 (%d) ==" % len(gone))
    for k in gone:
        print("  -", k)
    print("== 新增的函数/方法 (%d) ==" % len(new))
    for k in new:
        print("  +", k)
    print("== 节点数变多 (%d) ==" % len(grown))
    for k, x, y, d in grown:
        print("  ~ %-46s %d → %d (+%d)" % (k, x, y, d))
    print("== 节点数变少 (%d) ==" % len(shrunk))
    for k, x, y, d in shrunk:
        print("  ! %-46s %d → %d (%d)  ← 检查是不是被别的方法吞了" % (k, x, y, d))
    if not gone and not shrunk:
        print("\n✓ 没有任何函数/方法被删掉或瘦身（新增/扩充符合预期）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
