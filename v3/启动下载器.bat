@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 未找到 python，请先安装 Python 3.10+
  pause
  exit /b 1
)
echo 正在启动 SniffDL 视频嗅探下载器...
start "" pythonw video_downloader.py
if errorlevel 1 (
  echo [错误] 启动失败，尝试用 python 运行查看报错：
  python video_downloader.py
  pause
)