# Builds Silence Player from a clean virtual environment:
#   dist\SilencePlayer\                      the app folder (silenceplayer.exe + _internal + license files)
#   dist\SilencePlayer-<version>-portable.zip that folder zipped, to run from anywhere
#   dist\SilencePlayer-Setup-<version>.exe    the installer (with -Installer)
#   dist\SHA256SUMS.txt                       checksums of the files above
#
#   powershell -ExecutionPolicy Bypass -File build.ps1
#   powershell -ExecutionPolicy Bypass -File build.ps1 -SkipTests -Installer
#
# Needs: Windows x64 and Python 3.12 (the "py" launcher must find it).
# -Installer also needs Inno Setup (https://jrsoftware.org/isinfo.php).
param(
    [string]$PythonVersion = "3.12",
    [switch]$SkipTests,
    [switch]$Installer,
    [switch]$Fresh
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Invoke-Checked {
    param([string]$What, [scriptblock]$Command)
    Write-Host "==> $What" -ForegroundColor Cyan
    & $Command
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE)" }
}

function Find-Iscc {
    if ($env:ISCC -and (Test-Path $env:ISCC)) { return $env:ISCC }
    $cmd = Get-Command iscc.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($root in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, "$env:LOCALAPPDATA\Programs")) {
        if (-not $root) { continue }
        $found = Get-ChildItem -Path $root -Filter "Inno Setup*" -Directory -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending |
            ForEach-Object { Join-Path $_.FullName "ISCC.exe" } |
            Where-Object { Test-Path $_ } | Select-Object -First 1
        if ($found) { return $found }
    }
    return $null
}

$venv = ".venv-build"
if ($Fresh -and (Test-Path $venv)) { Remove-Item -Recurse -Force $venv }
if (-not (Test-Path "$venv\Scripts\python.exe")) {
    Invoke-Checked "Create venv (Python $PythonVersion)" { py "-$PythonVersion" -m venv $venv }
}
$py = Join-Path $PSScriptRoot "$venv\Scripts\python.exe"

Invoke-Checked "Install pinned dependencies" {
    & $py -m pip install --disable-pip-version-check -r requirements.txt -r requirements-dev.txt
}

if (-not (Test-Path "libvlc.dll") -or -not (Test-Path "plugins\codec")) {
    throw "The bundled VLC files are missing (libvlc.dll / plugins). See tools\update_vlc.py."
}

if (-not $SkipTests) {
    Invoke-Checked "Run tests" { & $py -m pytest -q }
}

$version = (& $py -c "import silenceplayer; print(silenceplayer.APP_VERSION)").Trim()
if ($LASTEXITCODE -ne 0 -or -not $version) { throw "Could not read APP_VERSION" }
Write-Host "Version: $version"

Invoke-Checked "PyInstaller" { & $py -m PyInstaller --clean --noconfirm silenceplayer.spec }

$appDir = "dist\SilencePlayer"
if (-not (Test-Path "$appDir\silenceplayer.exe")) { throw "PyInstaller did not produce $appDir\silenceplayer.exe" }

# LICENSE.txt, THIRD-PARTY-NOTICES.txt and licenses\ next to silenceplayer.exe
Invoke-Checked "License files" { & $py tools\collect_licenses.py $appDir }

$zip = "dist\SilencePlayer-$version-portable.zip"
if (Test-Path $zip) { Remove-Item $zip }
Write-Host "==> Portable zip" -ForegroundColor Cyan
Compress-Archive -Path $appDir -DestinationPath $zip
$outputs = @($zip)

if ($Installer) {
    $iscc = Find-Iscc
    if (-not $iscc) { throw "Inno Setup (ISCC.exe) not found. Install it or set `$env:ISCC." }
    Invoke-Checked "Installer (Inno Setup)" { & $iscc "/DAppVersion=$version" "installer\SilencePlayer.iss" }
    $setup = "dist\SilencePlayer-Setup-$version.exe"
    if (-not (Test-Path $setup)) { throw "Inno Setup did not produce $setup" }
    $outputs = @($setup) + $outputs
}

& $py -m pip freeze | Out-File -Encoding utf8 "dist\build-freeze.txt"
$sums = foreach ($f in $outputs) {
    "{0}  {1}" -f (Get-FileHash $f -Algorithm SHA256).Hash.ToLower(), (Split-Path $f -Leaf)
}
$sums | Out-File -Encoding ascii "dist\SHA256SUMS.txt"

Write-Host ""
Write-Host "Built Silence Player $version" -ForegroundColor Green
foreach ($f in $outputs) {
    Write-Host ("  {0}  ({1:N1} MB)" -f $f, ((Get-Item $f).Length / 1MB))
}
Write-Host "  Checksums: dist\SHA256SUMS.txt   Package versions: dist\build-freeze.txt"
