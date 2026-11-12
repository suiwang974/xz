@echo off
chcp 65001 >nul
cd /d D:\wws\sb
set PY="C:\Users\王伟\AppData\Local\Programs\Python\Python312\python.exe"

echo ==================================================
echo  SniffDL 换角色：新角色 assistant1（1280 原图，无损）-^> maid
echo ==================================================
echo.

echo [0/4] 清掉上次中断的混帧...
if exist _rigframes\maid rmdir /s /q _rigframes\maid

echo [1/4] 渲帧：15 组动作，原图不缩，慢就慢，务必等它自己跑完...
%PY% tools\rig_frames.py --theme maid --model models/assistant1.psd
if errorlevel 1 goto fail

echo.
echo [2/4] 生成自检图...
%PY% tools\make_overview.py D:\wws\sb\_rigframes\maid
if errorlevel 1 goto fail

echo.
echo [3/4] 入库 cast24_maid.py...
%PY% tools\build_cast24.py --theme maid
if errorlevel 1 goto fail

echo.
echo [4/4] 打包 SniffDL.exe（几分钟）...
taskkill /F /IM SniffDL.exe /T >nul 2>&1
%PY% -m PyInstaller SniffDL.spec --noconfirm --clean
if errorlevel 1 goto fail

echo.
echo ============== 全部完成 ==============
echo 自检图：_rigframes\maid\OVERVIEW_auto.jpg
echo 成品：dist\SniffDL.exe
pause
exit /b 0

:fail
echo.
echo !! 出错了，把上面的报错内容发我
pause
exit /b 1