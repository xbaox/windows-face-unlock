$ErrorActionPreference = "Stop"
if (-not $env:VIRTUAL_ENV) { throw "venv не активирован: .\.venv\Scripts\Activate.ps1" }

$env:PIP_CONFIG_FILE = Join-Path $PSScriptRoot "pip.ini"
$env:UV_HTTP_TIMEOUT = "120"

$uv = Join-Path $env:VIRTUAL_ENV "Scripts\uv.exe"
if (-not (Test-Path $uv)) { Write-Host "ставлю uv в venv..." -ForegroundColor Yellow; python -m pip install -U uv }

$root = Split-Path $PSScriptRoot -Parent
if ($args.Count -gt 0) {
    Write-Host "uv pip install $($args -join ' ')" -ForegroundColor Cyan
    & $uv pip install @args
} else {
    $lock = Join-Path $root "requirements.lock"
    if (-not (Test-Path $lock)) { throw "нет requirements.lock — передай пакеты аргументами" }
    Write-Host "uv pip install -r requirements.lock" -ForegroundColor Cyan
    & $uv pip install -r $lock
}
Write-Host "готово." -ForegroundColor Green
