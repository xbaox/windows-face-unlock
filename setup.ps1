# Developer bootstrap for Face Unlock (a source checkout -- not the installer).
# Creates .venv, installs the hash-pinned dependencies and, unless -SkipAutostart, registers the
# three developer logon tasks (FaceUnlock-Service, -Presence, -Watchdog) via tools\register_tasks.ps1.
#
# Stage 9 (F-224): every native step is checked (Windows PowerShell 5.1 does not stop on a native
# exit code), Python 3.12 is required, and the install is hash-checked (requirements.lock, R18).
# R19: the task registration refuses a tree that non-administrators can change -- see CONTRIBUTING.

param(
    [string]$PythonExe = "python",
    [switch]$Gpu,
    [switch]$SkipAutostart
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot

function Invoke-Checked {
    param([string]$What, [scriptblock]$Block)
    & $Block
    if ($LASTEXITCODE) { throw "$What failed (exit code $LASTEXITCODE)" }
}

Write-Host "== Face Unlock developer setup ==" -ForegroundColor Cyan

$ver = & $PythonExe -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ($LASTEXITCODE -or $ver -ne '3.12') {
    throw "Python 3.12 is required (found: $ver). Pass -PythonExe <path to python 3.12>."
}

# 1. venv
if (-not (Test-Path "$root\.venv")) {
    Invoke-Checked 'creating the venv' { & $PythonExe -m venv "$root\.venv" }
}
$py = "$root\.venv\Scripts\python.exe"
$lock = if ($Gpu) { "$root\requirements-gpu.lock" } else { "$root\requirements.lock" }
Invoke-Checked 'installing the dependencies' { & $py -m pip install --require-hashes -r $lock }

# 2. The data directory only; no config file is seeded -- the service runs on its built-in defaults.
$home_cfg = Join-Path $env:USERPROFILE ".face-unlock"
if (-not (Test-Path $home_cfg)) { New-Item -ItemType Directory -Path $home_cfg | Out-Null }

if ($SkipAutostart) { Write-Host "Skipping the task registration."; return }

# 3. The developer logon tasks (single registrar: tools\register_tasks.ps1 + tools\tasks.psd1).
& "$root\tools\register_tasks.ps1" -Mode Dev -Action Register
if ($LASTEXITCODE) {
    Write-Host "Task registration did not complete (see above). Run the modules by hand instead:" -ForegroundColor Yellow
    Write-Host "  $py -m face_service      and      $py -m presence_monitor"
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "Next steps:" -ForegroundColor Yellow
Write-Host "  1. $py -m presence_monitor.password_gui      (save the Windows password)"
Write-Host "  2. $py -m presence_monitor.enroll_gui        (set up the face)"
Write-Host "  3. (Optional) build and register the Credential Provider -- credential_provider\README.md"
Write-Host ""
Write-Host "Defaults are built in -- no config file is needed. To customise, copy config.example.toml"
Write-Host "to $home_cfg\config.toml; every key is optional."
