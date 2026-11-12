# ASCII only. Reliably stops the DaFeiyu Live2D pet + gateway bridge, without touching
# unrelated Electron apps (QoderWork etc.) or our Qwen backend python.
$ErrorActionPreference = 'SilentlyContinue'
$root = $PSScriptRoot

# 1) Live2D pet electron: primary = kill the pid tree recorded in pet_state.json
try {
  $st = Get-Content -LiteralPath (Join-Path $root 'pet_state.json') -Raw | ConvertFrom-Json
  if ($st -and $st.pid) {
    & taskkill /T /F /PID $st.pid 2>&1 | Out-Null
    Write-Output ('killed pet tree pid=' + $st.pid)
  }
} catch {}

# 2) fallback: any electron whose cmdline references our pet app / page / live2d
Get-CimInstance Win32_Process -Filter "Name='electron.exe'" |
  Where-Object { $_.CommandLine -match 'pet_electron|pet\.html|live2d' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Output ('killed electron pid=' + $_.ProcessId) }

# 3) gateway bridge python (only bridge_gateway.py)
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*bridge_gateway.py*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Output ('killed bridge pid=' + $_.ProcessId) }

Write-Output 'pet+bridge stop pass done'
