# Bootstrap script for Face Unlock.
# Creates a venv, installs Python deps, sets up autostart for FaceService
# and PresenceMonitor via Task Scheduler (logon trigger, per-user).

param(
    [string]$PythonExe = "python",
    [switch]$SkipAutostart
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot

Write-Host "== Face Unlock setup ==" -ForegroundColor Cyan

# 1. venv
if (-not (Test-Path "$root\.venv")) {
    & $PythonExe -m venv "$root\.venv"
}
$py = "$root\.venv\Scripts\python.exe"

& $py -m pip install --upgrade pip
& $py -m pip install -r "$root\requirements.txt"

# 2. config
# Only the data directory is created. The config file is deliberately NOT
# seeded from config.example.toml: with no config.toml the service runs on the
# defaults compiled into face_service/config.py, whereas a copied example
# freezes whatever values that file happened to carry at install time -- which
# is exactly how a stale example became a live behaviour change before.
$home_cfg = Join-Path $env:USERPROFILE ".face-unlock"
if (-not (Test-Path $home_cfg)) { New-Item -ItemType Directory -Path $home_cfg | Out-Null }

if ($SkipAutostart) { Write-Host "Skipping autostart registration."; return }

# 3. Task Scheduler entries
# Delegated to the single registrar: the task list lives in tools\tasks.psd1 and
# the trigger/principal/settings are defined once in tools\register_tasks.ps1,
# so setup and the installer cannot drift apart.
& "$root\tools\register_tasks.ps1" -Mode Dev -Action Register

Write-Host ""
Write-Host "Next steps:" -ForegroundColor Yellow
Write-Host "  1. $py -m tools.enroll capture --count 15"
Write-Host "  2. $py -m tools.set_password"
Write-Host "  3. (Optional) Build + register the C++ Credential Provider - see credential_provider\README.md"
Write-Host ""
Write-Host "Defaults are built in - no config file is needed to run."
Write-Host "To customise, copy config.example.toml to $home_cfg\config.toml and edit it;"
Write-Host "every key is optional and anything you leave out keeps its default."
