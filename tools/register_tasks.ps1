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
    Restart     stop the tasks, kill leftover processes with the death-wait,
                start them again. Touches no registration at all.

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
.EXAMPLE
    .\register_tasks.ps1 -Action Restart
#>
[CmdletBinding()]
param(
    [ValidateSet('Dev', 'Installed')]
    [string]$Mode = 'Dev',

    [ValidateSet('Register', 'Unregister', 'Start', 'Restart')]
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

# Resolved install root for the Installed layout. A SEPARATE name from the -InstallDir parameter
# on purpose: see the NAMING RULE above -- nothing in this script assigns to a parameter name.
$fuInstallDir = $InstallDir

if ($Mode -eq 'Installed') {
    if (-not $fuInstallDir) {
        # Fall back to what the installer recorded. installer.iss writes InstallLocation under
        # HKLM\SOFTWARE\WindowsFaceUnlock and its own comment claims the value is read back by an
        # auto-updater fallback -- nothing in the tree ever read it, so this is its first real
        # consumer. It makes -Action Unregister and clean_restart.ps1 -Mode Installed usable
        # without re-typing the path; an explicit -InstallDir still wins.
        # -ErrorAction alone is NOT enough here. When the key does not exist the cmdlet returns
        # $null, and $null.InstallLocation is a terminating PropertyNotFoundStrict under the
        # Set-StrictMode -Version Latest at the top of this file. Probe the property too. The
        # identical idiom killed tools/uninstall.ps1's inventory on its first live run; here it
        # was latent, because this branch is only reached with -Mode Installed.
        $fuReg = Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\WindowsFaceUnlock' `
                                  -Name 'InstallLocation' -ErrorAction SilentlyContinue
        if ($fuReg -and $fuReg.PSObject.Properties['InstallLocation']) {
            $fuInstallDir = $fuReg.InstallLocation
        }
        if ($fuInstallDir) {
            Write-Host ("-InstallDir not given; using " +
                        "HKLM\SOFTWARE\WindowsFaceUnlock\InstallLocation = $fuInstallDir")
        }
    }
    if (-not $fuInstallDir) {
        Write-Host ("ERROR: -Mode Installed requires -InstallDir " +
                    "(and HKLM\SOFTWARE\WindowsFaceUnlock\InstallLocation is not set).") `
                   -ForegroundColor Red
        exit 1
    }
    if (-not (Test-Path -LiteralPath $fuInstallDir -PathType Container)) {
        Write-Host "ERROR: -InstallDir does not exist: $fuInstallDir" -ForegroundColor Red
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
        $fuExecute  = Join-Path $fuInstallDir $fuTask.InstalledExe
        $fuArgument = ''
        $fuWorkDir  = $fuInstallDir
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
        if ($fuExisting -contains $fuItem.Name) {
            $fuStateText = if ($Action -eq 'Restart') { 'registered (will be stopped, then started)' }
                           else { 'registered (will be overwritten)' }
        }
        else {
            $fuStateText = if ($Action -eq 'Restart') { 'NOT REGISTERED -- restart cannot create it; run -Action Register' }
                           else { 'not registered (will be created)' }
        }
        Write-Host ("  - {0}" -f $fuItem.Name)
        Write-Host ("      Execute          : {0}" -f $fuItem.Execute)
        Write-Host ("      Argument         : {0}" -f $(if ($fuItem.Argument) { $fuItem.Argument } else { '(none)' }))
        Write-Host ("      WorkingDirectory : {0}" -f $fuItem.WorkingDirectory)
        Write-Host ("      Currently        : {0}" -f $fuStateText)
    }
    if ($Action -eq 'Restart') {
        Write-Host "  (Restart touches no registration: no task is created, overwritten or removed.)"
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
# Only the actions that actually stop things say how they stop them; printing this under
# -Action Start, which stops nothing, would describe a strategy that is not going to run.
#
# Unregister belongs in this list and was excluded on a premise that is no longer true: the
# comment here used to read "-Action Unregister (which has no graceful step)". It has one --
# the branch below opens with Invoke-GracefulServiceShutdown, added so that removing the
# software stopped being the single path that always hard-killed the live owner of a capture
# (KNOWN_ISSUES #2). Saying so matters most exactly there, because that branch now also
# reports whether the stack really went down.
if ($Action -ne 'Start') {
    Write-Host "Stop strategy: graceful pipe shutdown first, then task stop, hard kill as fallback."
}

if ($DryRun) {
    Write-Host ""
    Write-Host "DRY RUN -- phase A only. Nothing was registered, removed, started or stopped." -ForegroundColor Cyan
    exit 0
}


# ===========================================================================
# PHASE B -- commit. Everything below this point changes the system.
# ===========================================================================

# Both functions below live here, under the dry-run gate, on purpose: between them they hold every
# Stop-Process / Start-Process in this file, and keeping every mutating call textually inside
# phase B is what makes the "dry-run path is clean" check a one-line AST walk instead of a
# judgement call.

# Ask the service to stop THROUGH THE PIPE, before anything gets killed.
#
# Why: Stop-Process is TerminateProcess. No Python runs, so serve_forever never reaches its
# trailing _release_camera(), and the `shutdown` handler never writes the watchdog pause -- the
# supervisor sees an unexplained death rather than a deliberate stop. Asking first makes the
# service hand its persistent capture back itself.
#
# It is also insurance for KNOWN_ISSUES #2: hard-killing the live owner of a persistent capture is
# the current suspect for wedging the Windows Camera Frame Server, which nothing short of a reboot
# clears. That link is NOT proven -- this is cheap insurance against it, not a fix for it.
#
# Only the service gets this. Presence and the watchdog hold no camera and expose no graceful IPC,
# so their path stays Stop-ScheduledTask + kill.
#
# Never throws: every failure degrades to "fall back to the hard kill", which is exactly what this
# script did before the function existed.
function Invoke-GracefulServiceShutdown {
    # WHICH client, per layout. This used to be dev-only in effect: it shelled out to
    # `python -m tools.pipe_client shutdown` from $fuRepoRoot, which on an installed machine is
    # {app} -- a directory with no tools/ tree in it and, usually, no Python on the box at all.
    # So the graceful path silently degraded to a hard kill on exactly the machines the installer
    # produces, which is the opposite of where the insurance is wanted.
    if ($Mode -eq 'Installed') {
        # The frozen tray exe routes --pipe-shutdown to the same tools.pipe_client entry point
        # (presence_monitor/__main__.py), so both layouts send the identical request through the
        # identical client, SID checks included. Name derived from the declaration, not hardcoded.
        $fuTrayExe = @($fuTasks |
                       Where-Object { $_.DevArgs -like '*presence_monitor*' } |
                       ForEach-Object { $_.InstalledExe } |
                       Where-Object { $_ }) | Select-Object -First 1
        if (-not $fuTrayExe) {
            Write-Warning "no frozen tray executable declared; falling back to hard kill"
            return
        }
        $fuClientExe  = Join-Path $fuInstallDir $fuTrayExe
        $fuClientArgs = @('--pipe-shutdown')
        $fuClientCwd  = $fuInstallDir
        if (-not (Test-Path -LiteralPath $fuClientExe -PathType Leaf)) {
            Write-Warning ("pipe client not found at {0}; falling back to hard kill" -f $fuClientExe)
            return
        }
    }
    else {
        # Same interpreter the tasks run under, one file over: the console python.exe beside the
        # pythonw.exe the Dev layout launches. Gating on pythonw keeps this resolution identical to
        # the task one. Deliberately NOT a bare "python" while the venv exists -- pywin32 lives in
        # the venv, and tools/pipe_client.py cannot import win32file without it.
        $fuVenvPythonw = Join-Path $fuRepoRoot '.venv\Scripts\pythonw.exe'
        if (Test-Path -LiteralPath $fuVenvPythonw) {
            $fuClientExe = Join-Path $fuRepoRoot '.venv\Scripts\python.exe'
        }
        else {
            # No venv (a partial checkout). Try PATH and let it fall into the fallback if pywin32
            # is missing there: a graceful path we cannot take costs a hard kill, not the run.
            $fuClientExe = 'python'
        }
        $fuClientArgs = @('-m', 'tools.pipe_client', 'shutdown')
        $fuClientCwd  = $fuRepoRoot
    }

    # Only the SERVICE is graceful-stoppable, so only the service is waited for. Derived from the
    # same declaration everything else uses instead of hardcoding a name: Dev matches the module
    # on the command line, Installed matches the frozen exe.
    $fuSvcNeedles  = @('face_service')
    $fuSvcExeNames = @($fuTasks | Where-Object { $_.DevArgs -like '*face_service*' } |
                       ForEach-Object { $_.InstalledExe } | Where-Object { $_ })

    Write-Host "Asking the service to shut down over the pipe..."
    $fuGraceWatch = [Diagnostics.Stopwatch]::StartNew()
    try {
        $fuClient = Start-Process -FilePath $fuClientExe `
                                  -ArgumentList $fuClientArgs `
                                  -WorkingDirectory $fuClientCwd `
                                  -WindowStyle Hidden -PassThru
    }
    catch {
        Write-Warning ("could not start the pipe client ({0}); falling back to hard kill" -f `
                       $_.Exception.Message)
        return
    }

    # pipe_client blocks in ReadFile with NO timeout, so a server that accepts the connection and
    # then wedges would hang this client forever. Bound it, and kill the CLIENT on timeout --
    # never the service, which is Stop-FuAndWait's job after the tasks are stopped.
    if (-not $fuClient.WaitForExit(3000)) {
        Write-Warning "pipe client did not return within 3s; killing the client and falling back to hard kill"
        try { $fuClient.Kill() } catch { }
        return
    }

    # The client returning only means the request was ANSWERED: the service replies first and
    # unwinds serve_forever afterwards. Wait for the process to actually be gone -- that is what
    # proves the camera was released and the mutex / pipe name freed.
    $fuGracePoll = [Diagnostics.Stopwatch]::StartNew()
    while ($fuGracePoll.Elapsed.TotalSeconds -lt 5) {
        if (-not (Get-FuProcess -LayoutMode $Mode -ExeNames $fuSvcExeNames `
                                -CommandLineNeedles $fuSvcNeedles)) {
            Write-Host ("graceful shutdown accepted; service exited in {0:N1}s" -f `
                        $fuGraceWatch.Elapsed.TotalSeconds)
            return
        }
        Start-Sleep -Milliseconds 200
    }
    Write-Warning "pipe shutdown failed or timed out; falling back to hard kill"
}

# The FALLBACK, not the primary stop any more: Invoke-GracefulServiceShutdown runs first and the
# scheduler is asked next, so by the time this runs it is finishing off whatever ignored both --
# a wedged service, or presence/watchdog, which have no graceful path at all.
#
# Stop-Process only SIGNALS. The Local\FaceUnlockService mutex and the
# FIRST_PIPE_INSTANCE pipe name stay held until the last handle closes, so
# starting on a blind delay races a slow-dying process into a mutex-loser exit.
# Bounded -- on timeout warn and continue, never hang.
function Stop-FuAndWait {
    $fuRunning = @(Get-FuProcess -LayoutMode $Mode -ExeNames $fuExeNames -CommandLineNeedles $fuNeedles)
    if (-not $fuRunning) { return }

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
        Write-Warning ("{0} process(es) still alive after {1}s; continuing anyway (a new instance may exit as a mutex-loser)" -f `
                       $fuLeft.Count, $fuDeathWaitSec)
    }
}

# READ-ONLY, and deliberately not part of Stop-FuAndWait. Reporting what survived
# is a different job from killing, and fusing the two is how the old code came to
# claim success it had not verified: Stop-FuAndWait's own warning was the only
# statement about survivors, it was bounded by the death-wait, and the caller then
# printed "All tasks removed" regardless. A counter that cannot kill can be
# trusted to answer "is the stack down?" -- which is the question the D-series
# control and PrepareToInstall both actually ask.
#
# Get-FuProcess is Get-CimInstance underneath: nothing here mutates. Returns the
# survivor count; the PIDs are printed because "1 alive" without a PID sent one
# 7g smoke chasing an unrelated process.
function Get-FuSurvivorCount {
    $fuStillUp = @(Get-FuProcess -LayoutMode $Mode -ExeNames $fuExeNames -CommandLineNeedles $fuNeedles)
    Write-Host ("Face Unlock processes still alive: {0}" -f $fuStillUp.Count)
    foreach ($fuOne in $fuStillUp) {
        Write-Host ("  pid {0,-7} {1}" -f $fuOne.ProcessId, $fuOne.Name)
    }
    return $fuStillUp.Count
}

Write-Host ""

if ($Action -eq 'Unregister') {
    # Destruction is the intent here, so ordering carries no invariant: stop the
    # processes first so nothing holds files, then remove declared and orphaned
    # tasks alike.
    #
    # Graceful FIRST, mirroring Register (B3) and Restart. This path used to be the only stop in
    # the script that went straight to Stop-FuAndWait -- i.e. uninstalling was the one operation
    # that ALWAYS hard-killed the live owner of a persistent capture, which is exactly the
    # condition this function exists to avoid (see its header and KNOWN_ISSUES #2). Removing the
    # software is a poor moment to leave the Frame Server wedged until the next reboot.
    Invoke-GracefulServiceShutdown
    Stop-FuAndWait
    foreach ($fuName in @($fuDeclaredSet + $fuOrphans)) {
        Unregister-ScheduledTask -TaskName $fuName -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host ("Unregistered: {0}" -f $fuName)
    }
    Write-Host "All declared and orphaned tasks removed."

    # SECOND PASS, and the order is the point. Stop-FuAndWait takes ONE snapshot,
    # signals everything in it, then merely WAITS -- so anything that appeared
    # after the snapshot is observed by the death-wait and never actually killed,
    # and Stop-Process runs with -ErrorAction SilentlyContinue, so a kill that
    # failed on permissions is indistinguishable in the log from one that worked.
    # Running the pass again here, with the tasks already gone, is what makes it
    # final: there is no longer any registration for the scheduler to start a
    # replacement from, so a survivor now is a real survivor rather than a race.
    Stop-FuAndWait

    # Then ask, without killing. This used to be the gap: the line above printed
    # a warning at most, and the exit was unconditionally 0, so "All tasks
    # removed" was emitted identically whether or not the stack was down. Any
    # D-series control that trusted this script's output rather than counting
    # processes itself was reading a message that could not fail.
    $fuSurvivors = Get-FuSurvivorCount
    if ($fuSurvivors -gt 0) {
        Write-Warning ("Unregister did NOT bring the stack down: {0} process(es) still running." -f $fuSurvivors)
        Write-Warning "Files under the install directory may still be held open. Stop them before installing over this."
        exit 1
    }
    Write-Host "Stack confirmed down: 0 Face Unlock processes."
    exit 0
}

if ($Action -eq 'Start') {
    foreach ($fuItem in $fuPlan) {
        Start-ScheduledTask -TaskName $fuItem.Name -ErrorAction SilentlyContinue
        Write-Host ("Started: {0}" -f $fuItem.Name)
    }
    exit 0
}

if ($Action -eq 'Restart') {
    # B3 + B4 only -- deliberately NO B1/B2. A restart must never touch a
    # registration: that way a typo in the declaration or a moved venv cannot
    # cost you a working set of tasks while you were only trying to bounce the
    # service.
    #
    # Graceful first: the service releases its own camera and records a deliberate stop. Then
    # Stop-ScheduledTask, because killing the process alone can leave the scheduler still
    # believing the task is Running, and Start-ScheduledTask on a Running task is a silent no-op.
    # Then the kill, for whatever survived both.
    Invoke-GracefulServiceShutdown
    foreach ($fuItem in $fuPlan) {
        Stop-ScheduledTask -TaskName $fuItem.Name -ErrorAction SilentlyContinue
    }
    Stop-FuAndWait
    foreach ($fuItem in $fuPlan) {
        Start-ScheduledTask -TaskName $fuItem.Name -ErrorAction SilentlyContinue
        Write-Host ("Restarted: {0}" -f $fuItem.Name)
    }
    Write-Host "Done."
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

# B3: stop what is still running from the previous registration. Three steps, weakest force
# first: ask the service over the pipe (it then releases its own camera and marks the stop
# deliberate), tell the scheduler, and only then kill the remainder.
Invoke-GracefulServiceShutdown
foreach ($fuItem in $fuPlan) {
    # SilentlyContinue: after a successful graceful stop some of these are already not running,
    # and the scheduler says so rather than staying quiet about it.
    Stop-ScheduledTask -TaskName $fuItem.Name -ErrorAction SilentlyContinue
}
Stop-FuAndWait

# B4
foreach ($fuItem in $fuPlan) {
    Start-ScheduledTask -TaskName $fuItem.Name -ErrorAction SilentlyContinue
    Write-Host ("Started: {0}" -f $fuItem.Name)
}

Write-Host "Done."
exit 0
