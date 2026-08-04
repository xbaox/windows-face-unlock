<#
.SYNOPSIS
    Remove Face Unlock from this machine, and prove what is left.

.DESCRIPTION
    MASTER-TZ section 7 asks for an uninstaller that removes the CP registration,
    the tasks, .venv and the config "without a trace". Before Stage 7d-I there was
    no uninstall entry point for the dev layout at all: setup.ps1 had no
    counterpart, and INSTALL.md section 9 was a block of commands to type by hand.
    The Inno uninstaller covered the installed layout only, and even there it left
    the model cache, the update downloads and (by default) the data directory.

    TWO PHASES, and the split is the point.

      PHASE A -- INVENTORY. Pure reads: Get-ScheduledTask, Test-Path,
                 Get-ItemProperty, Get-ChildItem. Nothing is stopped, deleted or
                 unregistered. It prints every trace it can find, names the ones
                 that hold secrets, and names the files nothing in the repo
                 claims to have written.

      PHASE B -- REMOVAL. Entered ONLY with -Force. Order: graceful service
                 shutdown, then tasks, then the Credential Provider registration,
                 then files. Ends by re-running the inventory and reporting
                 whatever survived, because "I ran the uninstaller" and "the
                 machine is clean" are different claims and only the second one
                 matters.

    Task removal and CP unregistration DELEGATE to the scripts that own them
    (tools\register_tasks.ps1, credential_provider\register.ps1) rather than
    reimplementing their criteria -- the same rule clean_restart.ps1 follows. What
    this script adds on top is the belt-and-braces the delegates cannot provide:
    a direct registry delete of both CP keys, which matters because
    register.ps1 refuses to run when the DLL is missing and the Inno uninstaller
    gates its regsvr32 /u on the same condition, so a deleted or moved DLL used to
    strand the lock-screen registration permanently.

    Data is KEPT by default. Enrollment images, embeddings and the DPAPI
    credential blob are the user's, and an uninstaller that silently destroys
    biometric material is worse than one that leaves a directory behind. Pass
    -RemoveData to delete it, -IncludeModels for the shared ~600 MiB InsightFace
    cache.

.PARAMETER Mode
    Which layout to clean. Dev = this repo checkout. Installed = a Program Files
    install; -InstallDir is resolved from HKLM if not given.

.PARAMETER Force
    Actually remove things. Without it this script only reports (phase A).

.PARAMETER DryRun
    Explicitly ask for phase A. Same behaviour as passing nothing; it exists so
    "show me what would happen" is sayable out loud, as in register_tasks.ps1.

.PARAMETER RemoveData
    Also delete the data directory (config, embeddings, credentials.bin, audit
    log, enrollment images). Off by default.

.PARAMETER IncludeModels
    Also delete %USERPROFILE%\.insightface. Off by default: that cache is shared
    with any other InsightFace application on this machine.

.EXAMPLE
    .\tools\uninstall.ps1
.EXAMPLE
    .\tools\uninstall.ps1 -Force
.EXAMPLE
    .\tools\uninstall.ps1 -Mode Installed -Force -RemoveData -IncludeModels
#>
#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [ValidateSet('Dev', 'Installed')]
    [string]$Mode = 'Dev',

    [string]$InstallDir,

    [switch]$Force,

    [switch]$DryRun,

    [switch]$RemoveData,

    [switch]$IncludeModels
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# NAMING RULE (inherited from register_tasks.ps1): no local variable may share a
# name with a parameter of this script or of a script it calls, in any casing --
# a validation attribute binds to the NAME for the whole scope and re-validates
# every later assignment. Internal names are fu*-prefixed.
# ---------------------------------------------------------------------------

$fuRepoRoot   = Split-Path -Parent $PSScriptRoot
$fuTaskPrefix = 'FaceUnlock-'
$fuRegistrar  = Join-Path $PSScriptRoot 'register_tasks.ps1'
$fuCpScript   = Join-Path $fuRepoRoot 'credential_provider\register.ps1'

# Single source of truth for the CLSID: parsed out of the CP registrar rather
# than copied. INSTALL.md documents the GUID as living in two places; it is
# actually in four, so a fifth copy here would be the wrong direction.
$fuClsid = $null
if (Test-Path -LiteralPath $fuCpScript) {
    $fuCpText = Get-Content -LiteralPath $fuCpScript -Raw
    if ($fuCpText -match "\`$Clsid\s*=\s*'(\{[0-9A-Fa-f\-]+\})'") {
        $fuClsid = $Matches[1]
    }
}
if (-not $fuClsid) {
    Write-Warning "could not read the CLSID from $fuCpScript; CP registry checks will be skipped"
}

$fuCpKeys = @()
if ($fuClsid) {
    $fuCpKeys = @(
        "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Authentication\Credential Providers\$fuClsid",
        "HKLM:\SOFTWARE\Classes\CLSID\$fuClsid"
    )
}

# The data directory honours FACE_UNLOCK_HOME exactly as face_service/config.py
# does. Every previous removal path hardcoded %USERPROFILE%\.face-unlock and
# would therefore have missed a relocated directory entirely.
$fuDataDir = if ($env:FACE_UNLOCK_HOME) { $env:FACE_UNLOCK_HOME }
             else { Join-Path $env:USERPROFILE '.face-unlock' }
$fuModelDir = Join-Path $env:USERPROFILE '.insightface'
$fuTempDir  = Join-Path ([IO.Path]::GetTempPath()) 'windows-face-unlock-update'
$fuVenvDir  = Join-Path $fuRepoRoot '.venv'
$fuBuildCp  = Join-Path $fuRepoRoot 'build-cp'

# Files under the data directory that some part of the project writes. Anything
# else there is reported by name: three copies of the DPAPI credential blob and
# an orphaned probe log were found in the field with no writer anywhere in the
# repo, and an uninstaller that silently deletes unknown files is as wrong as one
# that silently leaves secrets behind.
$fuKnownData = @(
    'config.toml', 'embeddings.npz', 'credentials.bin', 'pipe_entropy.bin',
    'service.log', 'presence.log', 'enroll.log', 'watchdog.log',
    'lockout.json', 'audit.jsonl', 'adaptive.npz', 'watchdog.pause',
    'threshold_samples.json', 'session_lock_probe.log'
)
$fuKnownPatterns = @(
    '^audit\.jsonl\.\d+$', '^lowlight_probe_.*\.csv$', '^service\.log\.\d+$',
    '^presence\.log\.\d+$', '^enroll\.log\.\d+$', '^watchdog\.log\.\d+$'
)

function Format-Size {
    param([long]$Bytes)
    if ($Bytes -ge 1GB) { return ('{0:N2} GiB' -f ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return ('{0:N1} MiB' -f ($Bytes / 1MB)) }
    if ($Bytes -ge 1KB) { return ('{0:N0} KiB' -f ($Bytes / 1KB)) }
    return "$Bytes B"
}

function Get-DirSize {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    $fuItems = @(Get-ChildItem -LiteralPath $Path -Recurse -File -Force -ErrorAction SilentlyContinue)
    return [pscustomobject]@{
        Files = $fuItems.Count
        Bytes = ($fuItems | Measure-Object -Property Length -Sum).Sum
    }
}

function Write-Trace {
    param([string]$Label, [bool]$Present, [string]$Detail = '')
    $fuMark = if ($Present) { '[PRESENT]' } else { '[absent] ' }
    $fuColor = if ($Present) { 'Yellow' } else { 'DarkGray' }
    Write-Host ("  {0} {1}" -f $fuMark, $Label) -ForegroundColor $fuColor -NoNewline
    if ($Detail) { Write-Host "  $Detail" -ForegroundColor DarkGray } else { Write-Host '' }
}

# ===========================================================================
# PHASE A -- INVENTORY. Everything below this banner only READS.
# ===========================================================================
function Invoke-Inventory {
    $fuFound = [ordered]@{}

    Write-Host ''
    Write-Host '=== Scheduled tasks ===' -ForegroundColor Cyan
    $fuTasks = @(Get-ScheduledTask -ErrorAction SilentlyContinue |
                 Where-Object { $_.TaskName -like "$fuTaskPrefix*" })
    $fuFound['tasks'] = $fuTasks
    if ($fuTasks) {
        foreach ($fuT in $fuTasks) {
            Write-Trace ("task {0}" -f $fuT.TaskName) $true ("state=" + $fuT.State)
        }
    }
    else { Write-Trace "no $fuTaskPrefix* tasks" $false }

    Write-Host ''
    Write-Host '=== Credential Provider registration ===' -ForegroundColor Cyan
    $fuCpPresent = @()
    foreach ($fuKey in $fuCpKeys) {
        $fuIs = Test-Path -LiteralPath $fuKey
        if ($fuIs) { $fuCpPresent += $fuKey }
        $fuDetail = ''
        if ($fuIs -and $fuKey -like '*Classes\CLSID*') {
            $fuInproc = Join-Path $fuKey 'InprocServer32'
            if (Test-Path -LiteralPath $fuInproc) {
                $fuDll = (Get-ItemProperty -LiteralPath $fuInproc -ErrorAction SilentlyContinue).'(default)'
                if ($fuDll) { $fuDetail = "-> $fuDll" }
            }
        }
        Write-Trace $fuKey $fuIs $fuDetail
    }
    $fuFound['cpKeys'] = $fuCpPresent

    Write-Host ''
    Write-Host '=== Installed layout ===' -ForegroundColor Cyan
    $fuAppDir = $InstallDir
    if (-not $fuAppDir) {
        $fuAppDir = (Get-ItemProperty -Path 'HKLM:\SOFTWARE\WindowsFaceUnlock' `
                                      -Name 'InstallLocation' -ErrorAction SilentlyContinue
                    ).InstallLocation
    }
    $fuFound['appDir'] = $fuAppDir
    if ($fuAppDir) {
        $fuSz = Get-DirSize $fuAppDir
        Write-Trace $fuAppDir ([bool]$fuSz) $(if ($fuSz) { "{0} files, {1}" -f $fuSz.Files, (Format-Size $fuSz.Bytes) } else { '' })
    }
    else { Write-Trace 'no install directory recorded in HKLM' $false }
    Write-Trace 'HKLM:\SOFTWARE\WindowsFaceUnlock' (Test-Path -LiteralPath 'HKLM:\SOFTWARE\WindowsFaceUnlock') `
                'written by installer.iss; removed by its own uninstaller'

    Write-Host ''
    Write-Host '=== Repository (dev layout) ===' -ForegroundColor Cyan
    foreach ($fuPair in @(@{P = $fuVenvDir; N = '.venv' }, @{P = $fuBuildCp; N = 'build-cp' })) {
        $fuSz = Get-DirSize $fuPair.P
        Write-Trace $fuPair.P ([bool]$fuSz) $(if ($fuSz) { "{0} files, {1}" -f $fuSz.Files, (Format-Size $fuSz.Bytes) } else { '' })
    }
    $fuFound['venv'] = (Test-Path -LiteralPath $fuVenvDir)
    if (Test-Path -LiteralPath $fuBuildCp) {
        Write-Host '     note: build-cp holds the DLL the registered CLSID points at.' -ForegroundColor DarkGray
        Write-Host '           Unregister the Credential Provider BEFORE deleting the repo,' -ForegroundColor DarkGray
        Write-Host '           or the lock screen keeps a dangling InprocServer32 path.' -ForegroundColor DarkGray
    }

    Write-Host ''
    Write-Host '=== User data ===' -ForegroundColor Cyan
    $fuSz = Get-DirSize $fuDataDir
    Write-Trace $fuDataDir ([bool]$fuSz) $(if ($fuSz) { "{0} files, {1}" -f $fuSz.Files, (Format-Size $fuSz.Bytes) } else { '' })
    $fuFound['data'] = [bool]$fuSz
    if ($env:FACE_UNLOCK_HOME) {
        Write-Host "     FACE_UNLOCK_HOME is set, so this is NOT the default location." -ForegroundColor DarkGray
    }
    if ($fuSz) {
        $fuSecret = @('credentials.bin', 'pipe_entropy.bin') |
                    Where-Object { Test-Path -LiteralPath (Join-Path $fuDataDir $_) }
        $fuEnroll = @(Get-ChildItem -LiteralPath (Join-Path $fuDataDir 'enroll') -File -Force `
                                    -ErrorAction SilentlyContinue)
        if ($fuSecret) {
            Write-Host ("     SECRETS: {0} (DPAPI-encrypted, this account only)" -f ($fuSecret -join ', ')) `
                       -ForegroundColor Yellow
        }
        if ($fuEnroll.Count) {
            Write-Host ("     BIOMETRIC: {0} enrollment image(s)" -f $fuEnroll.Count) -ForegroundColor Yellow
        }
        # Files nothing in the repo claims to write. Named, never auto-deleted.
        $fuUnknown = @(Get-ChildItem -LiteralPath $fuDataDir -File -Force -ErrorAction SilentlyContinue |
                       Where-Object {
                           $fuN = $_.Name
                           (-not ($fuKnownData -contains $fuN)) -and
                           (-not ($fuKnownPatterns | Where-Object { $fuN -match $_ }))
                       })
        if ($fuUnknown.Count) {
            Write-Host '     UNMANAGED (no writer anywhere in this repo):' -ForegroundColor Magenta
            foreach ($fuU in $fuUnknown) {
                Write-Host ("       - {0}  ({1})" -f $fuU.Name, (Format-Size $fuU.Length)) -ForegroundColor Magenta
            }
        }
    }

    Write-Host ''
    Write-Host '=== Caches and downloads ===' -ForegroundColor Cyan
    $fuSz = Get-DirSize $fuModelDir
    Write-Trace $fuModelDir ([bool]$fuSz) $(if ($fuSz) { "{0} files, {1} -- SHARED with any InsightFace app" -f $fuSz.Files, (Format-Size $fuSz.Bytes) } else { '' })
    $fuFound['models'] = [bool]$fuSz
    $fuSz = Get-DirSize $fuTempDir
    Write-Trace $fuTempDir ([bool]$fuSz) $(if ($fuSz) { "{0} installer(s), {1}" -f $fuSz.Files, (Format-Size $fuSz.Bytes) } else { '' })
    $fuFound['temp'] = [bool]$fuSz

    return $fuFound
}

Write-Host ''
Write-Host "Face Unlock uninstaller -- Mode=$Mode" -ForegroundColor White
Write-Host ('=' * 60)
$fuBefore = Invoke-Inventory

Write-Host ''
if (-not $Force) {
    Write-Host 'PHASE A ONLY. Nothing was changed.' -ForegroundColor Green
    if ($DryRun) { Write-Host '(-DryRun requested; this is the same inventory -Force would act on.)' -ForegroundColor DarkGray }
    Write-Host ''
    Write-Host 'To remove, re-run with -Force. Add:' -ForegroundColor DarkGray
    Write-Host '    -RemoveData     also delete the data directory (config, embeddings, credentials)' -ForegroundColor DarkGray
    Write-Host '    -IncludeModels  also delete the shared InsightFace model cache' -ForegroundColor DarkGray
    exit 0
}

# ===========================================================================
# PHASE B -- REMOVAL. Every mutating call in this file is below this banner.
# ===========================================================================
Write-Host 'PHASE B -- removing' -ForegroundColor Yellow
Write-Host ('=' * 60)

# 1. Tasks, through the registrar that owns them. It stops the service
#    gracefully first (7d-E), so the persistent camera is handed back rather
#    than torn away -- the Frame Server wedge is a reboot to clear.
Write-Host ''
Write-Host '[1/4] scheduled tasks (delegated to register_tasks.ps1)' -ForegroundColor Cyan
if (Test-Path -LiteralPath $fuRegistrar) {
    $fuRegArgs = @{ Action = 'Unregister'; Mode = $Mode }
    if ($InstallDir) { $fuRegArgs['InstallDir'] = $InstallDir }
    try { & $fuRegistrar @fuRegArgs }
    catch { Write-Warning ("registrar failed: {0}" -f $_.Exception.Message) }
}
else { Write-Warning "registrar not found at $fuRegistrar; skipping task removal" }

# 2. Credential Provider. Try the owner script first, then delete the keys
#    directly whatever it said. register.ps1 REFUSES to run when the DLL is
#    missing, and installer.iss gates its regsvr32 /u on the same file existing,
#    so a moved or deleted DLL used to leave the lock-screen registration behind
#    permanently, pointing at nothing.
Write-Host ''
Write-Host '[2/4] Credential Provider registration' -ForegroundColor Cyan
if (Test-Path -LiteralPath $fuCpScript) {
    try { & $fuCpScript -Action unregister }
    catch { Write-Warning ("register.ps1 unregister failed: {0}" -f $_.Exception.Message) }
}
else { Write-Warning "register.ps1 not found; going straight to the direct key removal" }

foreach ($fuKey in $fuCpKeys) {
    if (Test-Path -LiteralPath $fuKey) {
        Write-Host "  removing leftover key: $fuKey"
        try { Remove-Item -LiteralPath $fuKey -Recurse -Force }
        catch { Write-Warning ("could not remove {0}: {1}" -f $fuKey, $_.Exception.Message) }
    }
}

# 3. Files.
Write-Host ''
Write-Host '[3/4] files' -ForegroundColor Cyan
function Remove-Trace {
    param([string]$Path, [string]$Label)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    Write-Host "  removing $Label : $Path"
    try { Remove-Item -LiteralPath $Path -Recurse -Force }
    catch { Write-Warning ("could not remove {0}: {1}" -f $Path, $_.Exception.Message) }
}

Remove-Trace $fuTempDir 'downloaded installers'
if ($Mode -eq 'Dev') { Remove-Trace $fuVenvDir '.venv' }
if ($RemoveData)     { Remove-Trace $fuDataDir 'user data' }
else {
    Write-Host "  KEPT: $fuDataDir  (pass -RemoveData to delete enrollment + credentials)" -ForegroundColor DarkGray
}
if ($IncludeModels) { Remove-Trace $fuModelDir 'InsightFace model cache' }
else {
    Write-Host "  KEPT: $fuModelDir  (shared cache; pass -IncludeModels to delete)" -ForegroundColor DarkGray
}
Write-Host "  KEPT: $fuBuildCp  (repo build output; delete with the repo itself)" -ForegroundColor DarkGray

# 4. Verify. "I ran the uninstaller" is not the same claim as "the machine is
#    clean", and only the second one is worth anything.
Write-Host ''
Write-Host '[4/4] verification -- re-reading the machine' -ForegroundColor Cyan
Write-Host ('=' * 60)
$fuAfter = Invoke-Inventory

$fuLeft = @()
if ($fuAfter['tasks'].Count)  { $fuLeft += "{0} scheduled task(s)" -f $fuAfter['tasks'].Count }
if ($fuAfter['cpKeys'].Count) { $fuLeft += "{0} Credential Provider registry key(s)" -f $fuAfter['cpKeys'].Count }
if ($fuAfter['temp'])         { $fuLeft += 'downloaded installers' }
if ($Mode -eq 'Dev' -and $fuAfter['venv']) { $fuLeft += '.venv' }
if ($RemoveData -and $fuAfter['data'])      { $fuLeft += 'the data directory' }
if ($IncludeModels -and $fuAfter['models']) { $fuLeft += 'the model cache' }

Write-Host ''
if ($fuLeft.Count) {
    Write-Host ('INCOMPLETE -- still present: ' + ($fuLeft -join '; ')) -ForegroundColor Red
    Write-Host 'A locked file usually means a process is still running; reboot and re-run.' -ForegroundColor Red
    exit 1
}
Write-Host 'CLEAN -- everything this script was asked to remove is gone.' -ForegroundColor Green
if (-not $RemoveData)    { Write-Host "Kept by request: $fuDataDir" -ForegroundColor DarkGray }
if (-not $IncludeModels) { Write-Host "Kept by request: $fuModelDir" -ForegroundColor DarkGray }
exit 0
