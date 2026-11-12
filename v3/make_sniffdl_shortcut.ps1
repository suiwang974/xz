$ErrorActionPreference='Stop'
$target = 'D:\wws\sb\dist\SniffDL\SniffDL.exe'
$workdir = 'D:\wws\sb\dist\SniffDL'
if (-not (Test-Path -LiteralPath $target)) { Write-Output ('MISSING target: ' + $target); exit 1 }
$d = [Environment]::GetFolderPath('Desktop')
$lnk = Join-Path $d 'SniffDL.lnk'
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
$s.TargetPath = $target
$s.WorkingDirectory = $workdir
$s.IconLocation = $target + ',0'
$s.Save()
Write-Output ('created ' + $lnk + ' -> ' + $target + ' | target exists=' + (Test-Path -LiteralPath $target))
