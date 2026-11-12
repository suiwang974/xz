$ErrorActionPreference='Stop'
$dir='D:\wws\sb'
$desktop=[Environment]::GetFolderPath('Desktop')
$ws=New-Object -ComObject WScript.Shell
Get-ChildItem -LiteralPath $dir -Filter *.bat | ForEach-Object {
  $c = Get-Content -LiteralPath $_.FullName -Raw -Encoding UTF8
  if ($c -match 'DaFeiyu') {
    $lnk = Join-Path $desktop ($_.BaseName + '.lnk')
    $s = $ws.CreateShortcut($lnk)
    $s.TargetPath = $_.FullName
    $s.WorkingDirectory = $dir
    $s.IconLocation = 'shell32.dll,173'
    $s.Save()
    Write-Output ('shortcut -> ' + $lnk)
  }
}
