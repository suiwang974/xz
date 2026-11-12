# -*- coding: utf-8 -*-
"""组装免安装试用版目录 SniffDL_v3_trial_win64/：
    python/    便携 Python 3.11（复制 setup-python 的完整解释器，含 tkinter；再 pip 装内核依赖）
    electron/  Electron 33 win64
    aria2/     aria2c.exe 1.37.0（官方 release）
    v3/        源码（去掉文档/参考图/PSD/旧 live2d 等与运行无关的大件）
    启动SniffDL.vbs（双击，无黑框） / 启动SniffDL.bat / 使用说明.txt
用法：python make_trial.py <dest> <python_home> <electron_dist> <aria2c.exe>
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
V3 = os.path.join(ROOT, "v3")
dest, pyhome, eldist, aria = [os.path.abspath(x) for x in sys.argv[1:5]]

SKIP_DIRS = {"docs", "_ref", "coding_memory", "context", "memory", "todo", "messages", "agents", "outputs",
             "live2d", "vendor", "__pycache__", "node_modules", "tests", "背景"}
SKIP_EXT = (".psd", ".log", ".pyc", ".zip", ".bak.js")
SKIP_FILES = {"config.json", "runtime.json", "pet_state.json", "queue.json", "downloaded.json", "stats.json"}

shutil.rmtree(dest, ignore_errors=True)
os.makedirs(dest)


def ig(d, names):
    out = []
    for n in names:
        p = os.path.join(d, n)
        if os.path.isdir(p) and n in SKIP_DIRS and os.path.abspath(d) == V3:
            out.append(n)
        elif os.path.isdir(p) and n in ("__pycache__", "node_modules"):
            out.append(n)
        elif os.path.isfile(p) and (n.endswith(SKIP_EXT) or (n in SKIP_FILES and os.path.abspath(d) == V3)):
            out.append(n)
    return out


shutil.copytree(V3, os.path.join(dest, "v3"), ignore=ig)
shutil.copytree(pyhome, os.path.join(dest, "python"), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
shutil.copytree(eldist, os.path.join(dest, "electron"))
os.makedirs(os.path.join(dest, "aria2"))
shutil.copy(aria, os.path.join(dest, "aria2", "aria2c.exe"))
for n in ("AUTHORS", "COPYING", "LICENSE.OpenSSL", "README.html"):
    p = os.path.join(os.path.dirname(aria), n)
    if os.path.exists(p):
        shutil.copy(p, os.path.join(dest, "aria2", n))

py = os.path.join(dest, "python", "python.exe")
env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PYTHON") and k != "pythonLocation"}
env["PYTHONNOUSERSITE"] = "1"
subprocess.check_call([py, "-m", "pip", "install", "--no-warn-script-location", "--disable-pip-version-check",
                       "-r", os.path.join(ROOT, "wintest", "requirements-trial.txt")], env=env)
subprocess.call([py, "-m", "pip", "uninstall", "-y", "pip", "setuptools", "wheel"], env=env)   # 瘦身
# 删掉解释器里用不到的大件
for sub in ("Lib\\test", "Lib\\idlelib\\idle_test", "Doc", "include", "libs", "Tools"):
    shutil.rmtree(os.path.join(dest, "python", sub), ignore_errors=True)

BAT = r"""@echo off
rem SniffDL v3 试用版启动器（双击 启动SniffDL.vbs 可无黑框启动）
setlocal
set "R=%~dp0"
set "SNIFFDL_PYTHON=%R%python\python.exe"
set "SNIFFDL_ARIA2C=%R%aria2\aria2c.exe"
set "PYTHONNOUSERSITE=1"
set "PYTHONHOME="
set "PYTHONPATH="
set "PATH=%R%aria2;%R%python;%R%python\Scripts;%PATH%"
if not exist "%R%data\pet" mkdir "%R%data\pet"
start "" "%R%electron\electron.exe" "%R%v3\pet_electron\app" --user-data "%R%data\electron" --data-dir "%R%data" --runtime "%R%data\runtime.json" --state "%R%data\pet\pet_state.json" %SNIFFDL_EXTRA% %*
"""
with open(os.path.join(dest, "启动SniffDL.bat"), "w", encoding="ascii", errors="ignore", newline="\r\n") as f:
    f.write("\n".join(ln for ln in BAT.splitlines() if not ln.startswith("rem")) + "\n")

VBS = ('Set fso = CreateObject("Scripting.FileSystemObject")\r\n'
       'd = fso.GetParentFolderName(WScript.ScriptFullName)\r\n'
       'Set sh = CreateObject("WScript.Shell")\r\n'
       'sh.CurrentDirectory = d\r\n'
       'sh.Run """" & d & "\\启动SniffDL.bat""", 0, False\r\n')
with open(os.path.join(dest, "启动SniffDL.vbs"), "w", encoding="utf-16") as f:     # WSH 认 UTF-16LE+BOM 的中文
    f.write(VBS)

README = """SniffDL v3 免安装试用版（Windows 10/11 64 位）

1. 把整个压缩包解压到任意文件夹（路径可以有中文和空格），不要在压缩包里直接运行。
2. 双击「启动SniffDL.vbs」。约 5~15 秒后出现主窗口和桌宠大肥鱼；托盘里有 SniffDL 图标。
   想看启动日志就双击「启动SniffDL.bat」。
3. 退出：托盘图标右键 →「退出 SniffDL」（关主窗只是缩到托盘）。
4. 配置、任务、日志都在本目录 data\\ 下，不碰你电脑上原来的 SniffDL / 桌宠数据；删掉整个文件夹即卸载。
5. 第一次运行 Windows 可能弹 SmartScreen「已保护你的电脑」→ 点「更多信息」→「仍要运行」；
   防火墙若询问 python.exe / aria2c.exe 联网，允许「专用网络」即可（只监听 127.0.0.1）。
6. 已在 GitHub Actions 的 Windows Server 2022 上自动冒烟：双击 vbs → 内核握手 → 主窗 → 下载 → 桌宠 → 退出。

组成：Python 3.11（含 tkinter）+ yt-dlp/requests/curl_cffi/Pillow/imageio-ffmpeg、Electron 33、aria2 1.37.0。
未内置：Playwright 浏览器（深度嗅探会用你电脑上的 Edge/Chrome）。
"""
with open(os.path.join(dest, "使用说明.txt"), "w", encoding="utf-8-sig", newline="\r\n") as f:
    f.write(README)

tot = sum(os.path.getsize(os.path.join(d, x)) for d, _, fs in os.walk(dest) for x in fs)
print("trial dir", dest, "%.1f MB" % (tot / 1e6))
