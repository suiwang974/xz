@echo off
chcp 65001 >nul
set PY=C:\Users\王伟\AppData\Local\Programs\Python\Python312\python.exe
echo === install deps ===
"%PY%" -m pip install --disable-pip-version-check -i https://pypi.tuna.tsinghua.edu.cn/simple yt-dlp playwright imageio-ffmpeg pyinstaller > rebuild312.log 2>&1
echo === build ===
cd /d D:\wws\sb
"%PY%" -m PyInstaller SniffDL.spec --noconfirm >> rebuild312.log 2>&1
echo EXIT=%ERRORLEVEL% >> rebuild312.log
echo DONE_MARKER >> rebuild312.log