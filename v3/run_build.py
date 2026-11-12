# AI生成
"""构建编排：安装依赖并重打包 SniffDL（console=False，无黑框）

用 D:\python.exe 运行本脚本（无需 PyInstaller），内部以 subprocess 调用
干净环境的 Python312 完成 pip 安装 + PyInstaller 打包，全程写日志。
"""
import subprocess
import sys
import time

PY312 = r"C:\Users\王伟\AppData\Local\Programs\Python\Python312\python.exe"
WORK = r"D:\wws\sb"
LOG = r"D:\wws\sb\rebuild312.log"


def run(cmd, log):
    log.write(f"\n>>> {' '.join(cmd)}\n")
    log.flush()
    p = subprocess.run(cmd, cwd=WORK, capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (p.stdout or "") + (p.stderr or "")
    log.write(out[-4000:])
    log.flush()
    return p.returncode


def main():
    with open(LOG, "w", encoding="utf-8") as log:
        log.write("=== rebuild start %s ===\n" % time.strftime("%H:%M:%S"))
        rc = run([PY312, "-m", "pip", "install", "--disable-pip-version-check",
                  "-i", "https://pypi.tuna.tsinghua.edu.cn/simple",
                  "yt-dlp", "playwright", "curl_cffi", "pycryptodomex", "imageio-ffmpeg", "pillow", "pyinstaller"], log)
        if rc != 0:
            log.write("PIP_FAILED rc=%s\n" % rc)
            return
        rc = run([PY312, "-m", "PyInstaller", "SniffDL.spec", "--noconfirm"], log)
        log.write("=== BUILD EXIT=%s ===\n" % rc)
        log.write("DONE_MARKER\n")
    print("REBUILD_FINISHED")


if __name__ == "__main__":
    main()