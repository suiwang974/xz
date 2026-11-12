# SniffDL project cleanup - batch send-to-Recycle-Bin (ASCII only)
Add-Type -AssemblyName Microsoft.VisualBasic
$root = 'D:\wws\sb'
$targets = @()

# single files (explicit)
$files = @(
  "$root\deepseek-chan.zip",
  "$root\_t_addchk.py",
  "$root\_water_strip.png",
  "$root\video_downloader.py.bak_20261031_194812",
  "$root\rebuild312.log",
  "$root\tools\pet_host_test.py",
  "$root\tools\pet_gaze_drag_test.py",
  "$root\tools\_pet_selftest.py",
  "$root\tools\_pet_alpha_test.py",
  "$root\tools\_pet_electron_test.py",
  "$root\tools\_pet_ring_test.py",
  "C:\Users\Public\kill_pet_host.ps1",
  "C:\Users\Public\kill_pet_all.ps1"
)
foreach ($f in $files) { if (Test-Path $f) { $targets += Get-Item $f } }

# wildcard groups
$patterns = @(
  @{d=$root;            p='_*.ps1'},
  @{d=$root;            p='cast24*.py'},
  @{d="$root\_uicheck"; p='pet_*.png'},
  @{d="$root\_uicheck"; p='pet_*.log'},
  @{d="$root\_uicheck"; p='_gaze_*.png'},
  @{d="$root\_uicheck"; p='gaze_drag.out'},
  @{d="$root";          p='_backup_*'}
)
foreach ($g in $patterns) {
  if (Test-Path $g.d) {
    Get-ChildItem -Path $g.d -Filter $g.p -Force | ForEach-Object { $targets += $_ }
  }
}

# regenerable / heavy directories
$dirs = @(
  "$root\_rigframes",
  "$root\build",
  "$root\__pycache__",
  "$root\tools\__pycache__",
  "$root\_pyi_tmp"
)
foreach ($d in $dirs) { if (Test-Path $d) { $targets += Get-Item $d } }

$totalMB = 0
foreach ($t in $targets) {
  try {
    $sz = 0
    if ($t.PSIsContainer) {
      $sz = ((Get-ChildItem $t.FullName -Recurse -File -Force -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum) / 1MB
      [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory($t.FullName, 'OnlyErrorDialogs', 'SendToRecycleBin')
    } else {
      $sz = $t.Length / 1MB
      [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile($t.FullName, 'OnlyErrorDialogs', 'SendToRecycleBin')
    }
    $totalMB += $sz
    Write-Output ("RECYCLED {0:N1}MB  {1}" -f $sz, $t.FullName)
  } catch {
    Write-Output ("SKIP(err) {0} : {1}" -f $t.FullName, $_.Exception.Message)
  }
}
Write-Output ("TOTAL {0:N0} MB -> Recycle Bin, items={1}" -f $totalMB, $targets.Count)
# verify
foreach ($t in $targets) { if (Test-Path $t.FullName) { Write-Output ("STILL-EXISTS " + $t.FullName) } }
Write-Output "CLEANUP_DONE"
