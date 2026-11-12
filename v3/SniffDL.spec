# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = []
tmp_ret = collect_all('yt_dlp')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('playwright')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('curl_cffi')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('Cryptodome')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

# 渐变混色（§22.2）依赖 PIL：Image/ImageDraw 离屏预渲染 → ImageTk 贴到画布。
# ⚠ PIL._tkinter_finder / _imagingtk 必须显式带上：冻结环境里缺它们 ImageTk 会静默抛错，
# 代码按设计回落纯色平涂 —— 界面"能跑但变平"，光看 exe 能否启动根本发现不了退化。
try:
    tmp_ret = collect_all('PIL')
    datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
except Exception as _e:
    print('PIL collect_all 失败（exe 里渐变将回落纯色）: %s' % _e)
hiddenimports += ['PIL', 'PIL._tkinter_finder', 'PIL.Image', 'PIL.ImageDraw',
                  'PIL.ImageTk', 'PIL._imagingtk', 'PIL._imaging']

# 小夜 · 原生 BT 引擎需要 aria2c：把自带副本打进包根（resolve_aria2c 会优先查 _MEIPASS/程序目录旁）
import os as _os
_aria2_bin = _os.path.join(_os.path.dirname(_os.path.abspath(SPEC)), 'vendor', 'aria2c.exe')
if _os.path.isfile(_aria2_bin):
    datas += [(_aria2_bin, '.')]

# 旧六界帧桌宠（cast24_*/maid_assets_*）已退役删除，不再扫描、不再打进包。
_spec_dir = _os.path.dirname(_os.path.abspath(SPEC))
# rpc_server 在 try/except 里 import，静态分析可能漏，显式补
hiddenimports += ['rpc_server']
# v3 内核全部模块显式带上（防运行时动态 import 漏包）
hiddenimports += ['core', 'core.api', 'core.batch', 'core.bencode', 'core.checksum', 'core.clipboard', 'core.engine', 'core.errors', 'core.events', 'core.headless', 'core.hls', 'core.sidecar', 'core.linkdecode', 'core.livehls', 'core.naming', 'core.platform_util', 'core.postactions', 'core.queues', 'core.ratelimit', 'core.recognize', 'core.runtime', 'core.settings', 'core.speed', 'native_host']

# v3 提速引擎（core.speed → sdl_speed）在运行时按 speed_engine 设置 import；SOCKS 代理走 PySocks（可选）
hiddenimports += ['core.speed', 'sdl_speed', 'sdl_speed.engine', 'sdl_speed.backends',
                  'sdl_speed.errors', 'sdl_speed.profiles']
try:
    import socks as _socks  # noqa: F401
    hiddenimports += ['socks', 'sockshandler']
except Exception:
    print('PySocks 未安装：exe 里 SOCKS 代理不可用（HTTP 代理不受影响）')
# 网盘规则表（2026-11-06 网盘直链里程碑；video_downloader 在 try 里 import，显式补）
hiddenimports += ['netdisk_rules']
# 组件自更新模块（video_downloader 顶部 import，静态分析可能漏）
hiddenimports += ['component_updates']
# 实时桌宠桥（运行时动态 import，静态分析抓不到）。宿主已 Electron-only，pet_host 退役不打包。
hiddenimports += ['pet_bridge']
# 系统托盘 pystray：Windows 后端是运行时按平台 import 的，必须显式带上，
# 否则 exe 里托盘静默失效（设置页勾了但任务栏没图标）
hiddenimports += ['pystray', 'pystray._win32', 'pystray._util.win32_guids']

# 实时桌宠 + webview 界面 + 大肥鱼素材 + 实时后端脚本：纳入打包
for _d in ['live2d', 'ui_web', 'assets']:
    _p = _os.path.join(_spec_dir, _d)
    if _os.path.isdir(_p):
        datas += [(_p, _d)]
for _f in ['start-feiyu-realtime.sh']:
    _p = _os.path.join(_spec_dir, _f)
    if _os.path.isfile(_p):
        datas += [(_p, '.')]

a = Analysis(
    ['video_downloader.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='SniffDL',
    icon='assets/SniffDL.ico',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=["libcurl*", "curl*.dll", "*curlffi*", "neqo*.dll"],
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=["libcurl*", "curl*.dll", "*curlffi*", "neqo*.dll"],
    name='SniffDL',
)
