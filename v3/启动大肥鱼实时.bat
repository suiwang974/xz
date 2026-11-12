@echo off
chcp 65001 >nul
title DaFeiyu Realtime Launcher
set LLAMA_DIR=D:\wws\Agent\llama\llama-b10405-bin-win-cuda-13.3-x64
set GGUF=%LLAMA_DIR%\models\Dolphin3.0-Qwen2.5-3b-Q4_K_M.gguf
set PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe
REM NGL = brain model layers on GPU (model has 36 layers). 0 = all CPU = stable on 8GB VRAM.
REM Measured with mouth(Qwen3-TTS)=GPU + pet=GPU + ears=CPU: ngl=0 leaves ~1000MB free.
set NGL=0

echo [1/4] start brain llama-server on :8090 (ngl=%NGL%) ...
start "llama-server" "%LLAMA_DIR%\llama-server.exe" -m "%GGUF%" --alias dolphin-qwen2.5-3b --host 0.0.0.0 --port 8090 -ngl %NGL% -c 8192

echo [2/4] start gateway bridge on :3066 ...
start "bridge" "%PY%" "D:\wws\sb\live2d\bridge_gateway.py"

echo waiting for brain to load ...
ping -n 11 127.0.0.1 >nul

echo [3/4] start realtime backend (ears=CPU, mouth=GPU) ...
wsl -e bash -lc "bash /mnt/d/wws/sb/start-feiyu-realtime.sh"

echo [4/4] waiting ~65s for backend to load, then launch the pet ...
ping -n 66 127.0.0.1 >nul
start "DaFeiyu-Pet" /D "%~dp0pet_electron" "%~dp0pet_electron\node_modules\.bin\electron.cmd" .

echo.
echo ALL STARTED: brain + bridge + ears + mouth + pet are up. Just talk to her.
echo To stop everything and free VRAM: double-click the "Stop" shortcut on the Desktop.
pause
