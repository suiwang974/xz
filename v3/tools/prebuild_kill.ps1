$ErrorActionPreference = 'SilentlyContinue'
# Preflight for onedir rebuild: kill everything that can lock dist\SniffDL.
# Three kinds matter; miss any one and PyInstaller COLLECT dies on PermissionError:
#   1) SniffDL.exe itself
#   2) pet_electron.exe living under dist\SniffDL (the Live2D pet)
#   3) the shared BT aria2c daemon -- its cwd is inherited from SniffDL.exe
#      (= dist\SniffDL), so it holds the DIRECTORY handle even though its exe is
#      elsewhere. Marker: command line contains .snifftdl .
#   NOTE: other people's aria2c (e.g. Motrix) never matches -> left alone.
# Keep this file pure ASCII: PowerShell 5.1 mis-parses BOM-less UTF-8 Chinese.
$hit = 0
Get-CimInstance Win32_Process | Where-Object {
  ($_.Name -eq 'SniffDL.exe') -or
  ($_.Name -eq 'electron.exe' -and $_.ExecutablePath -like 'D:\wws\sb\dist\SniffDL*') -or
  ($_.Name -eq 'aria2c.exe' -and $_.CommandLine -match '\.snifftdl')
} | ForEach-Object {
  Write-Output ("KILL PID={0} {1}" -f $_.ProcessId, $_.Name)
  Stop-Process -Id $_.ProcessId -Force
  $hit = $hit + 1
}
Start-Sleep -Seconds 2
Write-Output ("preflight killed={0}" -f $hit)
# Rename is the lock detector: if it can be renamed, nobody is holding it.
try {
  Rename-Item 'D:\wws\sb\dist\SniffDL' 'SniffDL_unlock_probe' -ErrorAction Stop
  Rename-Item 'D:\wws\sb\dist\SniffDL_unlock_probe' 'SniffDL' -ErrorAction Stop
  Write-Output 'LOCK=FREE'
} catch {
  Write-Output 'LOCK=HELD  something still holds dist\SniffDL -- do not rebuild yet'
}
