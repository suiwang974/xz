param([string]$Mode = '--qmem')
$ErrorActionPreference = 'Stop'
$exe = 'D:\wws\sb\dist\SniffDL\SniffDL.exe'
$p = Start-Process -FilePath $exe -ArgumentList $Mode -PassThru -Wait
Write-Output ("MODE={0} EXITCODE={1}" -f $Mode, $p.ExitCode)
