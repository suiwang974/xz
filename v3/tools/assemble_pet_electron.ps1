# Assemble the Live2D pet for the packaged app. Auto-detects onedir (dist\SniffDL\SniffDL.exe)
# vs legacy onefile (dist\SniffDL.exe). Copies electron runtime + app(fixed main.js) + live2d +
# a config.json seed. Preserves rpc/outdir by seeding from the already-patched dist\config.json.
$ErrorActionPreference = 'Stop'
$root = 'D:\wws\sb'
$dist = Join-Path $root 'dist'

if (Test-Path (Join-Path $dist 'SniffDL\SniffDL.exe')) {
  $exeDir = Join-Path $dist 'SniffDL'          # onedir
} else {
  $exeDir = $dist                              # onefile
}
$dst = Join-Path $exeDir 'pet_electron'
$app = Join-Path $dst 'app'
if (-not (Test-Path $app)) { New-Item -ItemType Directory -Path $app -Force | Out-Null }

# 1) electron runtime
$edist = Join-Path $root 'pet_electron\node_modules\electron\dist'
if (-not (Test-Path (Join-Path $edist 'electron.exe'))) { throw 'electron.exe missing' }
Copy-Item -Path (Join-Path $edist '*') -Destination $dst -Recurse -Force

# 2) app files (fixed main.js: 127.0.0.1 ws + Electron-only + prompt read)
Copy-Item -Path (Join-Path $root 'pet_electron\main.js')      -Destination $app -Force
Copy-Item -Path (Join-Path $root 'pet_electron\preload.js')   -Destination $app -Force
Copy-Item -Path (Join-Path $root 'pet_electron\package.json') -Destination $app -Force

# 3) live2d site (pet.html + libs + models/da-feiyu)
$srcLive = Join-Path $root 'live2d'
$dstLive = Join-Path $dst 'live2d'
if (Test-Path $dstLive) { Remove-Item -LiteralPath $dstLive -Recurse -Force }
Copy-Item -Path $srcLive -Destination $dstLive -Recurse -Force

# 4) config: seed app config + pet config from the patched dist\config.json (keeps rpc 6800/no-token, ui_bg, rt)
$cfgSrc = Join-Path $dist 'config.json'
if (Test-Path $cfgSrc) {
  Copy-Item -Path $cfgSrc -Destination (Join-Path $exeDir 'config.json') -Force
  Copy-Item -Path $cfgSrc -Destination (Join-Path $dst 'config.json') -Force
}

# 5) drop stale anime25drig + old onefile artifacts
$old = Join-Path $dst 'vendor\anime25drig'
if (Test-Path $old) { Remove-Item -LiteralPath $old -Recurse -Force }

$sz = (Get-ChildItem $dst -Recurse -File | Measure-Object -Property Length -Sum).Sum / 1MB
Write-Output ('exeDir = ' + $exeDir)
Write-Output ('pet_electron assembled: {0:N0} MB' -f $sz)
Write-Output ('electron.exe: '    + (Test-Path (Join-Path $dst 'electron.exe')))
Write-Output ('app main.js: '     + (Test-Path (Join-Path $app 'main.js')))
Write-Output ('live2d pet.html: ' + (Test-Path (Join-Path $dstLive 'pet.html')))
Write-Output ('da-feiyu model: '  + (Test-Path (Join-Path $dstLive 'models\da-feiyu')))
Write-Output ('app config.json: ' + (Test-Path (Join-Path $exeDir 'config.json')))
