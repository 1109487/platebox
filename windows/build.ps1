# Builds PlateBox-Setup-<version>.exe  (the Windows installer, with everything inside).
#
#   powershell -ExecutionPolicy Bypass -File windows\build.ps1 -FfmpegDir C:\path\to\ffmpeg\bin
#
# Needs: 64-bit Python 3.11 (any), internet (first run: libraries and the plate models), and Inno Setup 6 (ISCC.exe).
# Build files go outside the repository (and outside OneDrive) because they are large: about 1.5 GB at the peak.
param(
    [string]$Work       = 'C:\ba-build\pbwin-out',
    [string]$Venv       = 'C:\ba-build\pbwin',
    [string]$BasePython = 'C:\Espressif\python_env\idf5.5_py3.11_env\Scripts\python.exe',
    [string]$FfmpegDir  = '',          # folder with ffmpeg.exe (and LICENSE); leave empty to build WITHOUT the live view
    [string]$Iscc       = 'C:\ba-build\inno\ISCC.exe',
    [int]$SmokePort     = 8077
)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path          # ...\plate-box\windows
$root = Split-Path -Parent $here                                 # ...\plate-box
$py   = Join-Path $Venv 'Scripts\python.exe'
function Step($m) { Write-Host "`n== $m" -ForegroundColor Cyan }
function Check($what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit $LASTEXITCODE)" } }
# Programs such as pip and PyInstaller write their ordinary progress to stderr, which Windows PowerShell 5.1 would
# treat as a failure. Run them with errors non-fatal and judge them by their exit code instead.
function Native([scriptblock]$Run, [string]$What) {
    $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { & $Run 2>&1 | ForEach-Object { "$_" } | Select-Object -Last 3 } finally { $ErrorActionPreference = $prev }
    Check $What
}

$version = (Select-String -Path (Join-Path $root 'platebox\__init__.py') -Pattern '__version__\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
Write-Host "PlateBox $version"

Step 'Build environment'
if (-not (Test-Path $py)) { & $BasePython -m venv $Venv; Check 'venv' }
Native { & $py -m pip install --disable-pip-version-check --no-cache-dir -r (Join-Path $root 'requirements.txt') pyinstaller } 'pip install'

Step 'Plate models (downloaded once, then packed into the program)'
$models = Join-Path $here 'models_home'
if (-not (Test-Path (Join-Path $models '.cache\open-image-models'))) {
    New-Item -ItemType Directory -Force $models | Out-Null
    $env:USERPROFILE = $models; $env:HOME = $models
    Native { & $py -c "from fast_alpr import ALPR; ALPR(detector_model='yolo-v9-t-384-license-plate-end2end', ocr_model='cct-s-v2-global-model'); print('models ready')" } 'model download'
    $env:USERPROFILE = [Environment]::GetFolderPath('UserProfile'); $env:HOME = $env:USERPROFILE
}

Step 'Packaging the program (PyInstaller)'
$dist = Join-Path $Work 'dist'; $work2 = Join-Path $Work 'work'; $out = Join-Path $Work 'installer'
Push-Location $here
try {
    Native { & (Join-Path $Venv 'Scripts\pyinstaller.exe') --noconfirm --clean --distpath $dist --workpath $work2 platebox.spec } 'PyInstaller'
} finally { Pop-Location }
$app = Join-Path $dist 'PlateBox'

Step 'Live view: ffmpeg'
if ($FfmpegDir -and (Test-Path (Join-Path $FfmpegDir 'ffmpeg.exe'))) {
    $ff = Join-Path $app 'ffmpeg'
    New-Item -ItemType Directory -Force $ff | Out-Null
    Copy-Item (Join-Path $FfmpegDir 'ffmpeg.exe') $ff
    foreach ($l in 'LICENSE', '..\LICENSE', 'LICENSE.txt') { $p = Join-Path $FfmpegDir $l; if (Test-Path $p) { Copy-Item $p (Join-Path $ff 'LICENSE.txt'); break } }
    Write-Host "ffmpeg included"
} else {
    Write-Warning 'No ffmpeg given: the Live page will say it needs ffmpeg. Everything else works.'
}

Step 'Smoke test of the packaged program'
$tmp = Join-Path $Work 'smoke'
if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
New-Item -ItemType Directory -Force $tmp | Out-Null
$env:PLATEBOX_DATA = Join-Path $tmp 'data'; $env:PLATEBOX_SFTP_ROOT = Join-Path $tmp 'sftp'
$env:PLATEBOX_PORT = "$SmokePort"; $env:PLATEBOX_SFTP_PORT = '2298'; $env:PLATEBOX_LOCK_PORT = '50398'
$env:PLATEBOX_DNS_PORT = '5398'; $env:PLATEBOX_NTP_PORT = '12398'      # never the real DNS/time ports in a test
$p = Start-Process -FilePath (Join-Path $app 'platebox.exe') -PassThru -WindowStyle Hidden
try {
    $ok = $false
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep 1
        try { $h = Invoke-RestMethod "http://localhost:$SmokePort/healthz" -TimeoutSec 3; if ($h.ok) { $ok = $true; break } } catch { }
    }
    if (-not $ok) { throw 'The packaged program did not start.' }
    $page = Invoke-WebRequest "http://localhost:$SmokePort/setup" -UseBasicParsing
    if ($page.Content -notmatch 'Setup code') { throw 'The setup page did not render.' }
    Write-Host "packaged program starts and serves its pages: ok"
} finally {
    Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
    'PLATEBOX_DATA', 'PLATEBOX_SFTP_ROOT', 'PLATEBOX_PORT', 'PLATEBOX_SFTP_PORT', 'PLATEBOX_LOCK_PORT', 'PLATEBOX_DNS_PORT', 'PLATEBOX_NTP_PORT' | ForEach-Object { Remove-Item "Env:$_" -ErrorAction SilentlyContinue }
}

Step 'Installer (Inno Setup)'
if (-not (Test-Path $Iscc)) { throw "Inno Setup not found at $Iscc" }
Native { & $Iscc /Qp "/DDistDir=$app" "/DOutDir=$out" "/DAppVersion=$version" (Join-Path $here 'platebox.iss') } 'Inno Setup'
$setup = Join-Path $out "PlateBox-Setup-$version.exe"
"{0}  ({1:N0} MB)" -f $setup, ((Get-Item $setup).Length / 1MB)
