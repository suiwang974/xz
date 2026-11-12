# ASCII only. Sends retired old-pet frame/asset modules + the Anime2.5DRig site to the
# Recycle Bin (recoverable), and removes the regenerable build/ dir. Keeps maid_talk/maid_stage.
Add-Type -AssemblyName Microsoft.VisualBasic
$ErrorActionPreference = 'SilentlyContinue'
$root = 'D:\wws\sb'

$files = @(
  'maid_assets.py','maid_assets_horn.py','maid_assets_jiao.py','maid_assets_qing.py',
  'maid_cast_frames.py','maid_frames.py','maid_girly.py','theme_visions.py','pet_host.py'
)
$dirs = @('vendor\anime25drig','build')

$freed = 0
foreach ($f in $files) {
  $p = Join-Path $root $f
  if (Test-Path -LiteralPath $p) {
    $sz = (Get-Item -LiteralPath $p).Length
    [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile($p,'OnlyErrorDialogs','SendToRecycleBin')
    if (-not (Test-Path -LiteralPath $p)) { $freed += $sz; Write-Output ('recycled ' + $f) }
    else { Write-Output ('FAILED  ' + $f) }
  } else { Write-Output ('skip (absent) ' + $f) }
}
foreach ($d in $dirs) {
  $p = Join-Path $root $d
  if (Test-Path -LiteralPath $p) {
    $sz = (Get-ChildItem -LiteralPath $p -Recurse -File | Measure-Object Length -Sum).Sum
    [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory($p,'OnlyErrorDialogs','SendToRecycleBin')
    if (-not (Test-Path -LiteralPath $p)) { $freed += $sz; Write-Output ('recycled dir ' + $d) }
    else { Write-Output ('FAILED dir ' + $d) }
  } else { Write-Output ('skip (absent dir) ' + $d) }
}
Write-Output ('total freed(bytes) = ' + $freed)
