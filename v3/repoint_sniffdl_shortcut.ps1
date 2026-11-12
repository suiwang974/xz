# ASCII only. Repoint the desktop SniffDL shortcut + HKCU autostart to the new onedir exe.
$ErrorActionPreference = 'SilentlyContinue'
$target   = 'D:\wws\sb\dist\SniffDL\SniffDL.exe'
$workdir  = 'D:\wws\sb\dist\SniffDL'
if (-not (Test-Path $target)) { Write-Output ('MISSING target: ' + $target); exit 1 }

$desktop = [Environment]::GetFolderPath('Desktop')
$ws = New-Object -ComObject WScript.Shell
$fixed = @()
# repoint any desktop shortcut whose name is SniffDL (handles "SniffDL.lnk" and "SniffDL - 快捷方式.lnk")
Get-ChildItem -LiteralPath $desktop -Filter 'SniffDL*.lnk' | ForEach-Object {
  $s = $ws.CreateShortcut($_.FullName)
  $s.TargetPath = $target
  $s.WorkingDirectory = $workdir
  $s.IconLocation = $target + ',0'
  $s.Save()
  $fixed += $_.Name
}
Write-Output ('repointed shortcuts: ' + ($fixed -join ', '))

# fix autostart Run value if it exists and points to the old onefile
$run = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$cur = (Get-ItemProperty -Path $run -Name SniffDL).SniffDL
if ($cur) {
  Write-Output ('autostart was: ' + $cur)
  $new = '"' + $target + '" --tray'
  Set-ItemProperty -Path $run -Name SniffDL -Value $new
  Write-Output ('autostart now: ' + $new)
} else {
  Write-Output 'autostart: none set (nothing to fix)'
}
