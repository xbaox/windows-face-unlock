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

# --- What counts as sensitive under the data directory ----------------------
#
# Two kinds, and keeping them apart is the point. Key material is a password: it can
# be reissued, so losing a copy is bad but recoverable. A face cannot be reissued, so
# a template or a photograph left on disk is a different and permanently open kind of
# leftover. They are reported as separate categories for that reason, not merged into
# one "sensitive" bucket that would let the weaker claim stand in for the stronger.
#
# STEMS, not exact names. The field turned up credentials.bin.bak and
# credentials.bin.pre-stage4 next to the live blob (pre-Stage-4 v1 copies, decryptable
# with the constant printed in face_service/credentials.py:33) and embeddings.npz.bak
# next to the live template. Nothing in this repo writes any of them, which is exactly
# why an exact-name list cannot work: it can only chase spellings someone has already
# seen, and the next one is missed the same way. Anything called <stem>* IS the thing
# or a copy of it. The match is deliberately loose (prefix, case-insensitive) --
# over-classifying costs a line of report, under-classifying is the bug being fixed.
$fuSecretStems = @('credentials.bin', 'pipe_entropy.bin')

# Face-embedding files. adaptive.npz is here and not in $fuKnownData because it holds
# the same thing embeddings.npz does -- ArcFace vectors, (M, 512) float32, see
# face_service/adaptive.py:94 -- and being the ADAPTIVE gallery rather than the
# enrolled one makes no difference to what it discloses.
$fuTemplateStems = @('embeddings.npz', 'adaptive.npz')

# Directories under the data directory whose entire contents are enrollment imagery,
# classified BY LOCATION rather than by name: everything below them, at any depth.
# The wizard writes full frames as enroll_<ms>.jpg (presence_monitor/enroll_gui.py:698)
# and the QC probe writes aligned face crops into enroll\_qc_crops (tools/
# enroll_qc_probe.py:66,102). A raw frame is MORE disclosing than a template -- it is
# the original, not a derived vector -- so a rule that only knew file names would be
# protecting the lesser artifact while ignoring the greater.
$fuBiometricDirs = @('enroll')

# Files at the TOP level of the data directory that some part of the project writes.
# Anything else there is reported by name: an orphaned probe log was found in the
# field with no writer anywhere in the repo, and an uninstaller that silently deletes
# unknown files is as wrong as one that silently leaves secrets behind. Secret and
# template stems are deliberately NOT repeated here -- Get-FuDataClass owns them, so
# every file under the data directory has exactly one classifier.
$fuKnownData = @(
    'config.toml',
    'service.log', 'presence.log', 'enroll.log', 'watchdog.log',
    'lockout.json', 'audit.jsonl', 'watchdog.pause',
    'threshold_samples.json', 'session_lock_probe.log'
)
$fuKnownPatterns = @(
    '^audit\.jsonl\.\d+$', '^lowlight_probe_.*\.csv$', '^service\.log\.\d+$',
    '^presence\.log\.\d+$', '^enroll\.log\.\d+$', '^watchdog\.log\.\d+$'
)

function Get-FuDataClass {
    <#
    Classify ONE entry under the data directory from its path RELATIVE to that
    directory ('credentials.bin.bak', 'enroll\_qc_crops\x_crop.png'). The single
    source of truth for sensitivity: the SECRETS block, the BIOMETRIC block and the
    unmanaged filter all derive from this one answer, so a file cannot be sensitive
    to one of them and invisible to another.

    Returns exactly one of:
        'keymaterial'         the DPAPI blob or the entropy that unlocks it, or a copy
        'biometric:template'  a face-embedding file, or a copy
        'biometric:image'     anything inside an enrollment image directory
        $null                 not sensitive; the caller decides known vs unmanaged

    Order is load-bearing. Key material wins first so a credential copy that happens
    to sit inside enroll\ is still reported as a credential. Location beats stem after
    that, because everything under enroll\ is imagery whatever it is named.

    PURE by construction: a string goes in, a label comes out. No file system access,
    no output -- nothing that could make phase A anything other than a read.
    #>
    param([Parameter(Mandatory)][string]$RelPath)
    $fuParts = $RelPath -split '[\\/]'
    $fuLeaf  = $fuParts[$fuParts.Count - 1]

    foreach ($fuStem in $fuSecretStems) {
        if ($fuLeaf.StartsWith($fuStem, [StringComparison]::OrdinalIgnoreCase)) { return 'keymaterial' }
    }
    if ($fuParts.Count -gt 1) {
        foreach ($fuDir in $fuBiometricDirs) {
            if ($fuParts[0] -eq $fuDir) { return 'biometric:image' }
        }
    }
    foreach ($fuStem in $fuTemplateStems) {
        if ($fuLeaf.StartsWith($fuStem, [StringComparison]::OrdinalIgnoreCase)) { return 'biometric:template' }
    }
    return $null
}

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

function Get-RegValueOrNull {
    <#
    Read one OPTIONAL registry value. Returns $null when the key is missing, the value is
    missing, or the read fails. Never throws.

    Written after a live -DryRun died here. The obvious form,

        (Get-ItemProperty -Path $k -Name $n -ErrorAction SilentlyContinue).$n

    looks safe because of -ErrorAction, but -ErrorAction only suppresses the ERROR: when the key
    does not exist the cmdlet returns $null, and $null.Anything is a terminating
    PropertyNotFoundStrict under `Set-StrictMode -Version Latest`. On a machine that was never
    installed -- i.e. every dev box -- that killed the whole inventory at the "Installed layout"
    section and the sections after it were never printed.

    -ErrorAction on the cmdlet is therefore NOT sufficient; the property has to be probed too.
    #>
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Name)
    try {
        $fuItem = Get-ItemProperty -LiteralPath $Path -Name $Name -ErrorAction SilentlyContinue
        if ($null -eq $fuItem) { return $null }
        if (-not $fuItem.PSObject.Properties[$Name]) { return $null }
        return $fuItem.$Name
    }
    catch { return $null }
}

function Invoke-Section {
    <#
    Run one inventory section so that a failure inside it cannot truncate the rest.

    The point of phase A is a COMPLETE picture of the machine. A section that throws must cost
    that section, not the eight after it -- an inventory that stops early looks identical to an
    inventory that found nothing, and the operator cannot tell which they are reading.
    #>
    param([Parameter(Mandatory)][string]$Title, [Parameter(Mandatory)][scriptblock]$Body)
    Write-Host ''
    Write-Host ("=== {0} ===" -f $Title) -ForegroundColor Cyan
    try { & $Body }
    catch {
        Write-Host ("  [ERROR] this section could not be read: {0}" -f $_.Exception.Message) `
                   -ForegroundColor Red
        Write-Host '          the inventory continues; treat this section as UNKNOWN, not empty.' `
                   -ForegroundColor Red
    }
}

# ===========================================================================
# PHASE A -- INVENTORY. Everything below this banner only READS.
# ===========================================================================
function Invoke-Inventory {
    # Pre-seeded with a safe value for every key. Two reasons: the verification pass at the end
    # of phase B reads these back and calling .Count on an absent key would be the very fault
    # this function is being hardened against, and a section that fails must leave a defined
    # "nothing found" rather than a hole.
    $fuFound = [ordered]@{
        tasks  = @()
        cpKeys = @()
        appDir = $null
        venv   = $false
        data   = $false
        models = $false
        temp   = $false
    }

    Invoke-Section 'Scheduled tasks' {
        $fuTasks = @(Get-ScheduledTask -ErrorAction SilentlyContinue |
                     Where-Object { $_.TaskName -like "$fuTaskPrefix*" })
        $fuFound['tasks'] = $fuTasks
        if ($fuTasks) {
            foreach ($fuT in $fuTasks) {
                Write-Trace ("task {0}" -f $fuT.TaskName) $true ("state=" + $fuT.State)
            }
        }
        else { Write-Trace "no $fuTaskPrefix* tasks" $false }
    }

    Invoke-Section 'Credential Provider registration' {
        $fuCpPresent = @()
        foreach ($fuKey in $fuCpKeys) {
            $fuIs = Test-Path -LiteralPath $fuKey
            if ($fuIs) { $fuCpPresent += $fuKey }
            $fuDetail = ''
            if ($fuIs -and $fuKey -like '*Classes\CLSID*') {
                $fuInproc = Join-Path $fuKey 'InprocServer32'
                if (Test-Path -LiteralPath $fuInproc) {
                    # The default value is optional like any other: a CLSID key can exist with an
                    # InprocServer32 subkey that has no (default) set, and reading it the naive way
                    # would take the whole inventory down. Same class as the InstallLocation read.
                    $fuDll = Get-RegValueOrNull -Path $fuInproc -Name '(default)'
                    if ($fuDll) { $fuDetail = "-> $fuDll" }
                }
            }
            Write-Trace $fuKey $fuIs $fuDetail
        }
        $fuFound['cpKeys'] = $fuCpPresent
    }

    Invoke-Section 'Installed layout' {
        $fuAppDir = $InstallDir
        if (-not $fuAppDir) {
            $fuAppDir = Get-RegValueOrNull -Path 'HKLM:\SOFTWARE\WindowsFaceUnlock' -Name 'InstallLocation'
        }
        $fuFound['appDir'] = $fuAppDir
        if ($fuAppDir) {
            $fuSz = Get-DirSize $fuAppDir
            Write-Trace $fuAppDir ([bool]$fuSz) $(if ($fuSz) { "{0} files, {1}" -f $fuSz.Files, (Format-Size $fuSz.Bytes) } else { '' })
        }
        else { Write-Trace 'installed layout: not present (no InstallLocation recorded in HKLM)' $false }
        $fuVer = Get-RegValueOrNull -Path 'HKLM:\SOFTWARE\WindowsFaceUnlock' -Name 'Version'
        Write-Trace 'HKLM:\SOFTWARE\WindowsFaceUnlock' (Test-Path -LiteralPath 'HKLM:\SOFTWARE\WindowsFaceUnlock') `
                    $(if ($fuVer) { "Version=$fuVer; written by installer.iss, removed by its own uninstaller" }
                      else { 'written by installer.iss; removed by its own uninstaller' })
    }

    Invoke-Section 'Repository (dev layout)' {
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
    }

    Invoke-Section 'User data' {
    $fuSz = Get-DirSize $fuDataDir
    Write-Trace $fuDataDir ([bool]$fuSz) $(if ($fuSz) { "{0} files, {1}" -f $fuSz.Files, (Format-Size $fuSz.Bytes) } else { '' })
    $fuFound['data'] = [bool]$fuSz
    if ($env:FACE_UNLOCK_HOME) {
        Write-Host "     FACE_UNLOCK_HOME is set, so this is NOT the default location." -ForegroundColor DarkGray
    }
    if ($fuSz) {
        # ONE RECURSIVE PASS, and every list below is derived from it. The previous
        # version ran three separate NON-recursive scans -- secrets, enroll count,
        # unmanaged -- so anything in a subdirectory was invisible to all three at once:
        # fifteen aligned face crops under enroll\_qc_crops\ appeared in no list at all
        # while the report still claimed to name everything. A single enumeration plus a
        # single classifier means a file cannot be counted by one list and missed by
        # another; the arithmetic printed at the end of this block proves it each run.
        $fuRoot = $fuDataDir.TrimEnd('\')
        $fuAll = @(Get-ChildItem -LiteralPath $fuDataDir -File -Force -Recurse -ErrorAction SilentlyContinue |
                   ForEach-Object {
                       $fuRel = $_.FullName
                       if ($fuRel.StartsWith($fuRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
                           $fuRel = $fuRel.Substring($fuRoot.Length + 1)
                       }
                       [pscustomobject]@{
                           Rel   = $fuRel
                           Name  = $_.Name
                           Sub   = $(if ($fuRel -match '[\\/]') { Split-Path -Parent $fuRel } else { '' })
                           Bytes = $_.Length
                           Class = Get-FuDataClass $fuRel
                       }
                   })
        $fuSecret   = @($fuAll | Where-Object { $_.Class -eq 'keymaterial' })
        $fuTemplate = @($fuAll | Where-Object { $_.Class -eq 'biometric:template' })
        $fuImage    = @($fuAll | Where-Object { $_.Class -eq 'biometric:image' })
        if ($fuSecret.Count) {
            Write-Host ("     SECRETS: {0} (DPAPI-encrypted, this account only)" `
                        -f (($fuSecret | ForEach-Object { $_.Rel }) -join ', ')) -ForegroundColor Yellow
            # A copy is not a lesser secret. Name them separately so the operator sees that
            # -RemoveData is what clears them and that leaving them is a decision, not a gap.
            $fuStaleSecret = @($fuSecret | Where-Object { $fuSecretStems -notcontains $_.Name })
            if ($fuStaleSecret.Count) {
                Write-Host ("       {0} of those are ORPHANED COPIES -- nothing in this repo writes them:" `
                            -f $fuStaleSecret.Count) -ForegroundColor Yellow
                foreach ($fuS in $fuStaleSecret) {
                    Write-Host ("       - {0}  ({1})" -f $fuS.Rel, (Format-Size $fuS.Bytes)) `
                               -ForegroundColor Yellow
                }
                Write-Host '       Pre-migration copies decrypt with a constant printed in the source;' `
                           -ForegroundColor Yellow
                Write-Host '       they are removed with the data directory (-RemoveData).' -ForegroundColor Yellow
            }
        }
        if ($fuTemplate.Count -or $fuImage.Count) {
            Write-Host '     BIOMETRIC (a face cannot be reissued the way a password can):' `
                       -ForegroundColor Yellow
            # Templates are few, so they are named with sizes -- an orphaned copy has to be
            # identifiable to be deleted.
            foreach ($fuT in $fuTemplate) {
                Write-Host ("       template  {0}  ({1})" -f $fuT.Rel, (Format-Size $fuT.Bytes)) `
                           -ForegroundColor Yellow
            }
            # Images are counted per directory, never listed: one enrollment is dozens of
            # frames, and forty-five paths would bury the rest of this report. Grouping by
            # subdirectory keeps _qc_crops visible as its own line instead of folding it
            # into the enrollment count, which is how it went unnoticed in the first place.
            foreach ($fuG in ($fuImage | Group-Object Sub | Sort-Object Name)) {
                $fuGBytes = [long](($fuG.Group | Measure-Object -Property Bytes -Sum).Sum)
                $fuWhere = $(if ($fuG.Name) { $fuG.Name + '\' } else { '.\' })
                Write-Host ("       images    {0,-24} {1} file(s), {2}" `
                            -f $fuWhere, $fuG.Count, (Format-Size $fuGBytes)) -ForegroundColor Yellow
            }
        }
        # Files nothing in the repo claims to write. Named, never auto-deleted.
        # Sensitive files are excluded FIRST: a stale credential blob or a leftover face
        # crop has no writer either, but each belongs in its own block above, not in a junk
        # list. Known names/patterns only count at the TOP level -- a familiar name appearing
        # inside a subdirectory is not a thing this repo writes, so it stays visible.
        $fuUnknown = @($fuAll | Where-Object {
                           $fuN = $_.Name
                           (-not $_.Class) -and
                           (($_.Sub -ne '') -or (
                               (-not ($fuKnownData -contains $fuN)) -and
                               (-not ($fuKnownPatterns | Where-Object { $fuN -match $_ }))))
                       })
        if ($fuUnknown.Count) {
            Write-Host '     UNMANAGED (no writer anywhere in this repo):' -ForegroundColor Magenta
            foreach ($fuU in $fuUnknown) {
                Write-Host ("       - {0}  ({1})" -f $fuU.Rel, (Format-Size $fuU.Bytes)) -ForegroundColor Magenta
            }
        }
        # The arithmetic, printed every run. Each file lands in exactly one bucket by
        # construction, so this line is the claim "nothing under the data directory fell
        # out of the inventory" in a form that can be checked at a glance rather than
        # trusted. A mismatch against the directory total is itself the finding.
        $fuAccounted = $fuSecret.Count + $fuTemplate.Count + $fuImage.Count + $fuUnknown.Count
        Write-Host ("     accounted: {0} secret + {1} template + {2} image + {3} known + {4} unmanaged = {5} of {6} file(s)" `
                    -f $fuSecret.Count, $fuTemplate.Count, $fuImage.Count,
                       ($fuAll.Count - $fuAccounted), $fuUnknown.Count, $fuAll.Count, $fuSz.Files) `
                   -ForegroundColor DarkGray
    }
    }

    Invoke-Section 'Caches and downloads' {
        $fuSz = Get-DirSize $fuModelDir
        Write-Trace $fuModelDir ([bool]$fuSz) $(if ($fuSz) { "{0} files, {1} -- SHARED with any InsightFace app" -f $fuSz.Files, (Format-Size $fuSz.Bytes) } else { '' })
        $fuFound['models'] = [bool]$fuSz
        $fuSz = Get-DirSize $fuTempDir
        Write-Trace $fuTempDir ([bool]$fuSz) $(if ($fuSz) { "{0} installer(s), {1}" -f $fuSz.Files, (Format-Size $fuSz.Bytes) } else { '' })
        $fuFound['temp'] = [bool]$fuSz
    }

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
