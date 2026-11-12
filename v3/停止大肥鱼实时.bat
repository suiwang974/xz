@echo off
chcp 65001 >nul
title DaFeiyu Stopper
echo [1/4] stop brain (llama-server) ...
taskkill /IM llama-server.exe /F >nul 2>&1

echo [2/4] stop ears+mouth backend (WSL speech-to-speech) ...
wsl -e bash -lc "if [ -f ~/logs/feiyu-realtime.pid ]; then kill $(cat ~/logs/feiyu-realtime.pid) 2>/dev/null; fi; pkill -f 'speech[-]to[-]speech'; sleep 1"

echo [3/4] stop Live2D pet + gateway bridge ...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_feiyu_proc.ps1"

echo [4/4] waiting 3s, then VERIFY ...
ping -n 4 127.0.0.1 >nul

echo.
echo ========== VERIFY (empty port = stopped) ==========
for %%P in (8090 8765 6801 3066) do (
  netstat -ano | findstr LISTENING | findstr ":%%P " >nul 2>&1 && (echo   [FAIL] port %%P still listening) || (echo   [ OK ] port %%P free)
)
tasklist | findstr /I "llama-server.exe electron.exe" >nul 2>&1 && (echo   [FAIL] leftover process: llama-server/electron) || (echo   [ OK ] no llama-server / electron)
echo.
echo   VRAM:
nvidia-smi --query-gpu=memory.used,memory.free --format=csv,noheader
echo.
echo   If every line says [ OK ] and VRAM is low, the realtime stack is fully stopped.
echo   Next launch: double-click the "Start" shortcut.
pause
