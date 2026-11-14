"""生成 Windows 实测对比报告模板（IDM / aria2 / SniffDL），Markdown。"""
import sys
CASES=["小文件(1MB×50)","大文件(1GB)","高延迟(200ms)","限速服务器","HLS 流","BT 种子"]
TOOLS=["SniffDL","IDM","aria2 -x16"]
def make(cases=CASES,tools=TOOLS):
    L=["# Windows 实测速度对比报告（待填）","","环境：Windows 版本=__ 网络=__ 日期=__","",
       "| 场景 | "+" | ".join(t+" (MB/s)" for t in tools)+" | 备注 |",
       "|"+"---|"*(len(tools)+2)]
    for c in cases: L.append("| "+c+" | "+" | ".join("" for _ in tools)+" | |")
    L+=["","结论：未实测前不得宣称 Windows 已测。"]
    return "\n".join(L)+"\n"
if __name__=="__main__":
    sys.stdout.write(make())
