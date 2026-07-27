$ErrorActionPreference = "Stop"
if (-not $env:VIRTUAL_ENV) { throw "venv не активирован: .\.venv\Scripts\Activate.ps1" }
if (-not (Get-Command aria2c -EA SilentlyContinue)) { throw "нет aria2c. Поставь: winget install aria2.aria2" }

$env:PIP_CONFIG_FILE = Join-Path $PSScriptRoot "pip.ini"
$root   = Split-Path $PSScriptRoot -Parent
$wheel  = Join-Path $root "wheelhouse"
$report = Join-Path $root "report.json"
$urls   = Join-Path $root "urls.txt"
New-Item -ItemType Directory -Force -Path $wheel | Out-Null

if ($args.Count -gt 0) { $spec = @($args) }
else {
    $lock = Join-Path $root "requirements.lock"
    if (-not (Test-Path $lock)) { throw "нет requirements.lock — передай пакеты аргументами" }
    $spec = @("-r", $lock)
}

Write-Host "резолвлю список колёс..." -ForegroundColor Cyan
python -m pip install --dry-run --ignore-installed --report $report @spec | Out-Null
$u = (Get-Content $report -Raw | ConvertFrom-Json).install |
     ForEach-Object { $_.download_info.url } | Where-Object { $_ -like "*.whl" }
if (-not $u) { throw "в report.json нет .whl URL" }
$u | Set-Content -Encoding ascii $urls
Write-Host "к загрузке колёс: $(@($u).Count)" -ForegroundColor Green

aria2c -x16 -s16 -j5 -c -d $wheel -i $urls
if ($LASTEXITCODE -ne 0) { throw "aria2c exit $LASTEXITCODE" }

Write-Host "ставлю оффлайн из wheelhouse..." -ForegroundColor Cyan
python -m pip install --no-index --find-links $wheel @spec
Write-Host "готово." -ForegroundColor Green
