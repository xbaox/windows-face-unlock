<#
.SYNOPSIS
    Register, unregister or start the Face Unlock scheduled tasks.

.DESCRIPTION
    The single registrar for both layouts. The task list lives in tasks.psd1
    next to this script and is the only place task names appear; the trigger,
    principal and settings are built once, here, so the dev and installed
    deployments cannot drift apart.

    Task settings (identical for every task):
        Trigger    -AtLogOn for the current user
        Principal  current user, Interactive, RunLevel Limited
        Settings   AllowStartIfOnBatteries, DontStopIfGoingOnBatteries,
                   StartWhenAvailable, Hidden, ExecutionTimeLimit PT0S

    ExecutionTimeLimit PT0S ("no limit") is load-bearing: the Windows default
    of PT72H makes the scheduler kill these always-on tasks after three days.

.PARAMETER Mode
    Dev       -- run the modules from the repo .venv via pythonw.exe.
    Installed -- run the frozen executables from -InstallDir.

.PARAMETER Action
    Register    register (overwriting) every declared task, then start it.
    Unregister  stop and remove every declared task.
    Start       start every declared task.

.PARAMETER InstallDir
    Required for -Mode Installed: the directory holding the frozen exes.

.PARAMETER DryRun
    Run phase A only and print the plan. Changes nothing: no task is created,
    removed or started and no process is stopped. Use this to verify a change
    to this script or to tasks.psd1 before running it for real.

.EXAMPLE
    .\register_tasks.ps1 -DryRun
.EXAMPLE
    .\register_tasks.ps1
.EXAMPLE
    .\register_tasks.ps1 -Mode Installed -InstallDir "C:\Program Files\WindowsFaceUnlock"
.EXAMPLE
    .\register_tasks.ps1 -Action Unregister
#>
[CmdletBinding()]
param(
    [ValidateSet('Dev', 'Installed')]
    [string]$Mode = 'Dev',

    [ValidateSet('Register', 'Unregister', 'Start')]
    [string]$Action = 'Register',

    [string]$InstallDir,

    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# NAMING RULE -- do not break this.
#
# PowerShell variable names are CASE-INSENSITIVE, and a validation attribute on
# a parameter binds to the NAME for the whole scope: every later assignment to
# it is re-validated. So `$action = New-ScheduledTaskAction ...` assigns to the
# -Action parameter and throws
#     "the value MSFT_TaskExecAction is not a valid value for the Action variable"
# mid-script. That is exactly what happened on the first live run, after the
# destructive steps had already executed.
#
# Therefore: no local variable and no loop variable may be named Mode, Action,
# InstalledDir/InstallDir or DryRun in any casing. Internal names below are
# prefixed (fu*/plan*/task*) to keep them clear of the parameter namespace.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# PHASE ORDER -- do not reorder. This is the fix for a live incident where an
# unregister-first pass ran, the script then threw, and the machine was left
# with no tasks, no processes and a dead pipe.
#
#   Phase A -- BUILD & VALIDATE. Pure. Reads the declaration, validates its
#              keys, resolves executables, and constructs every
#              New-ScheduledTask* object for every applicable task. Touches
#              nothing. Any failure in A leaves the system byte-for-byte
#              unchanged, which is the whole point.
#
#   Phase B -- COMMIT. Entered only after A completed in full:
#                B1  Register-ScheduledTask -Force for every planned task.
#                    -Force overwrites an existing registration in place, so
#                    there is NO pre-unregister pass. Never reintroduce one.
#                B2  Remove orphans: FaceUnlock-* tasks on the system that the
#                    declaration no longer lists.
#                B3  Stop stale processes, with the bounded death-wait.
#                B4  Start every planned task.
#              A failure between B steps leaves the tasks REGISTERED, which a
#              reboot or `-Action Start` recovers from.
#
# -DryRun exits between A and B. Every mutating call in this file is textually
# below that exit.
# ---------------------------------------------------------------------------

$fuRepoRoot     = Split-Path -Parent $PSScriptRoot
$fuDeclPath     = Join-Path $PSScriptRoot 'tasks.psd1'
$fuDeathWaitSec = 10
$fuTaskPrefix   = 'FaceUnlock-'
$fuRequiredKeys = @('Name', 'Description', 'DevArgs', 'InstalledExe', 'SkipReason')
$fuNeedles      = @('face_service', 'presence_monitor', 'tools.watchdog')


# --- helpers (pure) ---------------------------------------------------------

# The one process-matching criterion, reused by the kill, the death-wait and the
# dry-run count so the three cannot drift. Dev runs modules under python/pythonw
# so match the commandline; Installed runs frozen exes so match the image name.
# Read-only: Get-CimInstance never changes anything.
function Get-FuProcess {
    param([string]$LayoutMode, [string[]]$ExeNames, [string[]]$CommandLineNeedles)

    if ($LayoutMode -eq 'Dev') {
        return @(Get-CimInstance Win32_Process `
                    -Filter "Name='python.exe' OR Name='pythonw.exe'" `
                    -ErrorAction SilentlyContinue |
                 Where-Object {
                     $fuCmdLine = $_.CommandLine
                     $fuCmdLine -and ($CommandLineNeedles | Where-Object { $fuCmdLine -like "*$_*" })
                 })
    }
    if (-not $ExeNames) { return @() }
    $fuFilter = ($ExeNames | ForEach-Object { "Name='$_'" }) -join ' OR '
    return @(Get-CimInstance Win32_Process -Filter $fuFilter -ErrorAction SilentlyContinue)
}

# Read-only: which FaceUnlock-* tasks exist right now.
function Get-FuExistingTaskName {
    return @(Get-ScheduledTask -TaskName "$fuTaskPrefix*" -ErrorAction SilentlyContinue |
             ForEach-Object { $_.TaskName })
}


# ===========================================================================
# PHASE A -- build & validate. No side effects below this line until PHASE B.
# ===========================================================================

if (-not (Test-Path -LiteralPath $fuDeclPath)) {
    Write-Host "ERROR: task declaration not found: $fuDeclPath" -ForegroundColor Red
    exit 1
}
$fuDecl = Import-PowerShellDataFile -LiteralPath $fuDeclPath
if (-not $fuDecl.ContainsKey('Tasks') -or -not $fuDecl.Tasks) {
    Write-Host "ERROR: $fuDeclPath declares no Tasks." -ForegroundColor Red
    exit 1
}
$fuTasks = @($fuDecl.Tasks)

# Validate the declaration before anything else looks at it.
$fuDeclErrors = @()
foreach ($fuTask in $fuTasks) {
    foreach ($fuKey in $fuRequiredKeys) {
        if (-not $fuTask.ContainsKey($fuKey)) {
            $fuDeclErrors += "task entry is missing the '$fuKey' key"
        }
    }
}
if ($fuDeclErrors) {
    Write-Host "ERROR: $fuDeclPath is malformed:" -ForegroundColor Red
    foreach ($fuErr in $fuDeclErrors) { Write-Host "  - $fuErr" }
    exit 1
}

if ($Mode -eq 'Installed') {
    if (-not $InstallDir) {
        Write-Host "ERROR: -Mode Installed requires -InstallDir." -ForegroundColor Red
        exit 1
    }
    if (-not (Test-Path -LiteralPath $InstallDir -PathType Container)) {
        Write-Host "ERROR: -InstallDir does not exist: $InstallDir" -ForegroundColor Red
        exit 1
    }
}

# Resolve every declared task for this layout, and build its scheduler objects.
# A task the layout cannot run is skipped out loud, not silently.
$fuPlan     = @()
$fuSkipped  = @()
foreach ($fuTask in $fuTasks) {
    if ($Mode -eq 'Dev') {
        $fuExecute  = Join-Path $fuRepoRoot '.venv\Scripts\pythonw.exe'
        $fuArgument = $fuTask.DevArgs
        $fuWorkDir  = $fuRepoRoot
    }
    else {
        if (-not $fuTask.InstalledExe) {
            $fuSkipped += [pscustomobject]@{ Name = $fuTask.Name; Reason = $fuTask.SkipReason }
            continue
        }
        $fuExecute  = Join-Path $InstallDir $fuTask.InstalledExe
        $fuArgument = ''
        $fuWorkDir  = $InstallDir
    }

    # NOTE the name: NOT $action. See the NAMING RULE block above.
    if ($fuArgument) {
        $fuTaskAction = New-ScheduledTaskAction -Execute $fuExecute -Argument $fuArgument `
                                                -WorkingDirectory $fuWorkDir
    }
    else {
        $fuTaskAction = New-ScheduledTaskAction -Execute $fuExecute -WorkingDirectory $fuWorkDir
    }

    $fuPlan += [pscustomobject]@{
        Name             = $fuTask.Name
        Execute          = $fuExecute
        Argument         = $fuArgument
        WorkingDirectory = $fuWorkDir
        TaskAction       = $fuTaskAction
    }
}

# The shared trigger / principal / settings -- built once, for every task.
$fuTrigger   = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$fuPrincipal = New-ScheduledTaskPrincipal -UserId $env:USERNAME `
                                          -LogonType Interactive -RunLevel Limited
$fuSettings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
                                            -DontStopIfGoingOnBatteries `
                                            -StartWhenAvailable -Hidden `
                                            -ExecutionTimeLimit ([TimeSpan]::Zero)

# Every one of these is wrapped in @() AT THE CALL SITE. A function that returns
# @(...) still unrolls it into the caller's pipeline, so a single match arrives
# as a bare object -- and under Set-StrictMode -Version Latest, .Count on that
# object throws PropertyNotFoundStrict. The -DryRun added in this commit caught
# exactly that on its first run, with one process matching.
$fuExeNames    = @($fuTasks | Where-Object { $_.InstalledExe } | ForEach-Object { $_.InstalledExe })
$fuDeclaredSet = @($fuTasks | ForEach-Object { $_.Name })
$fuExisting    = @(Get-FuExistingTaskName)
$fuOrphans     = @($fuExisting | Where-Object { $fuDeclaredSet -notcontains $_ })
$fuCandidates  = @(Get-FuProcess -LayoutMode $Mode -ExeNames $fuExeNames -CommandLineNeedles $fuNeedles)

# --- the plan ---------------------------------------------------------------
Write-Host ("Mode={0} Action={1}{2}" -f $Mode, $Action, $(if ($DryRun) { '  [DRY RUN]' } else { '' }))
Write-Host ("Declaration: {0}" -f $fuDeclPath)
Write-Host ("Declared {0} task(s); {1} apply to this layout, {2} skipped." -f `
            $fuTasks.Count, $fuPlan.Count, $fuSkipped.Count)
Write-Host ""

if ($Action -eq 'Unregister') {
    Write-Host "Would remove (declared):"
    foreach ($fuName in $fuDeclaredSet) {
        $fuState = if ($fuExisting -contains $fuName) { 'present' } else { 'not registered' }
        Write-Host ("  - {0,-24} [{1}]" -f $fuName, $fuState)
    }
}
else {
    Write-Host "Planned tasks:"
    foreach ($fuItem in $fuPlan) {
        Write-Host ("  - {0}" -f $fuItem.Name)
        Write-Host ("      Execute          : {0}" -f $fuItem.Execute)
        Write-Host ("      Argument         : {0}" -f $(if ($fuItem.Argument) { $fuItem.Argument } else { '(none)' }))
        Write-Host ("      WorkingDirectory : {0}" -f $fuItem.WorkingDirectory)
        Write-Host ("      Currently        : {0}" -f $(if ($fuExisting -contains $fuItem.Name) { 'registered (will be overwritten)' } else { 'not registered (will be created)' }))
    }
}

foreach ($fuSkip in $fuSkipped) {
    Write-Host ("  ! SKIP {0} -- {1}" -f $fuSkip.Name, $fuSkip.Reason) -ForegroundColor Yellow
}

Write-Host ""
Write-Host ("Orphan {0}* tasks (present, not declared): {1}" -f `
            $fuTaskPrefix, $(if ($fuOrphans) { $fuOrphans -join ', ' } else { 'none' }))
Write-Host ("Processes matching the kill criterion right now: {0}" -f $fuCandidates.Count)
foreach ($fuProc in $fuCandidates) {
    Write-Host ("  pid {0,-7} {1}" -f $fuProc.ProcessId, $fuProc.Name)
}

if ($DryRun) {
    Write-Host ""
    Write-Host "DRY RUN -- phase A only. Nothing was registered, removed, started or stopped." -ForegroundColor Cyan
    exit 0
}


# ===========================================================================
# PHASE B -- commit. Everything below this point changes the system.
# ===========================================================================

Write-Host ""

if ($Action -eq 'Unregister') {
    # Destruction is the intent here, so ordering carries no invariant: stop the
    # processes first so nothing holds files, then remove declared and orphaned
    # tasks alike.
    $fuRunning = @(Get-FuProcess -LayoutMode $Mode -ExeNames $fuExeNames -CommandLineNeedles $fuNeedles)
    if ($fuRunning) {
        Write-Host ("Stopping {0} running Face Unlock process(es)..." -f $fuRunning.Count)
        $fuRunning | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    }
    foreach ($fuName in @($fuDeclaredSet + $fuOrphans)) {
        Unregister-ScheduledTask -TaskName $fuName -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host ("Unregistered: {0}" -f $fuName)
    }
    Write-Host "All declared and orphaned tasks removed."
    exit 0
}

if ($Action -eq 'Start') {
    foreach ($fuItem in $fuPlan) {
        Start-ScheduledTask -TaskName $fuItem.Name -ErrorAction SilentlyContinue
        Write-Host ("Started: {0}" -f $fuItem.Name)
    }
    exit 0
}

# --- Register ---------------------------------------------------------------

# B1: overwrite in place. No pre-unregister -- see the PHASE ORDER block.
foreach ($fuItem in $fuPlan) {
    Register-ScheduledTask -TaskName $fuItem.Name -Action $fuItem.TaskAction `
                           -Trigger $fuTrigger -Principal $fuPrincipal `
                           -Settings $fuSettings -Force | Out-Null
    Write-Host ("Registered (hidden, no time limit): {0} -> {1} {2}" -f `
                $fuItem.Name, $fuItem.Execute, $fuItem.Argument)
}

# B2: drop tasks the declaration no longer lists. Bounded to our own prefix, and
# deliberately after B1: if this fails, every declared task is already in place.
# Without it the declaration is only half a source of truth -- a task removed
# from tasks.psd1 would linger on every machine that ever had it.
foreach ($fuName in $fuOrphans) {
    Unregister-ScheduledTask -TaskName $fuName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host ("Removed orphan (not in the declaration): {0}" -f $fuName)
}

# B3: stop what is still running from the previous registration. Stop-Process
# only SIGNALS: the Local\FaceUnlockService mutex and the FIRST_PIPE_INSTANCE
# pipe name stay held until the last handle closes, so starting on a blind delay
# races a slow-dying process into a mutex-loser exit. Bounded -- on timeout warn
# and continue, never hang.
$fuRunning = @(Get-FuProcess -LayoutMode $Mode -ExeNames $fuExeNames -CommandLineNeedles $fuNeedles)
if ($fuRunning) {
    Write-Host ("Stopping {0} running Face Unlock process(es)..." -f $fuRunning.Count)
    $fuRunning | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

    $fuStopwatch = [Diagnostics.Stopwatch]::StartNew()
    while ($fuStopwatch.Elapsed.TotalSeconds -lt $fuDeathWaitSec) {
        if (-not (Get-FuProcess -LayoutMode $Mode -ExeNames $fuExeNames -CommandLineNeedles $fuNeedles)) {
            break
        }
        Start-Sleep -Milliseconds 200
    }
    $fuLeft = @(Get-FuProcess -LayoutMode $Mode -ExeNames $fuExeNames -CommandLineNeedles $fuNeedles)
    if ($fuLeft) {
        Write-Warning ("{0} process(es) still alive after {1}s; starting anyway (a new instance may exit as a mutex-loser)" -f `
                       $fuLeft.Count, $fuDeathWaitSec)
    }
}

# B4
foreach ($fuItem in $fuPlan) {
    Start-ScheduledTask -TaskName $fuItem.Name -ErrorAction SilentlyContinue
    Write-Host ("Started: {0}" -f $fuItem.Name)
}

Write-Host "Done."
exit 0
