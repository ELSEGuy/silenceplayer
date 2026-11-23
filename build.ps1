# Builds dist\silenceplayer.exe from a clean virtual environment.
#
#   powershell -ExecutionPolicy Bypass -File build.ps1
#   powershell -ExecutionPolicy Bypass -File build.ps1 -SkipTests
#
# Needs: Windows x64 and Python 3.12 installed (the "py" launcher must find it).
param(
    [string]$PythonVersion = "3.12",
    [switch]$SkipTests,
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

$venv = ".venv-build"
if ($Fresh -and (Test-Path $venv)) { Remove-Item -Recurse -Force $venv }
if (-not (Test-Path "$venv\Scripts\python.exe")) {
    Invoke-Checked "Create venv (Python $PythonVersion)" { py "-$PythonVersion" -m venv $venv }
}
$py = Join-Path $PSScriptRoot "$venv\Scripts\python.exe"

Invoke-Checked "Install pinned dependencies" {
    & $py -m pip install --disable-pip-version-check -r requirements.txt -r requirements-dev.txt
}

if (-not (Test-Path "plugins\plugins.dat")) {
    throw "Bundled VLC plugins folder is missing or incomplete (plugins\plugins.dat not found)."
}

if (-not $SkipTests) {
    Invoke-Checked "Run tests" { & $py -m pytest -q }
}

Invoke-Checked "PyInstaller" { & $py -m PyInstaller --clean --noconfirm silenceplayer.spec }

$exe = "dist\silenceplayer.exe"
$info = Get-Item $exe
$hash = (Get-FileHash $exe -Algorithm SHA256).Hash
& $py -m pip freeze | Out-File -Encoding utf8 "dist\build-freeze.txt"

Write-Host ""
Write-Host "Built $exe" -ForegroundColor Green
Write-Host ("  Size:   {0:N1} MB" -f ($info.Length / 1MB))
Write-Host "  SHA256: $hash"
Write-Host "  Exact package versions: dist\build-freeze.txt"
