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
    Register    unregister-then-register every declared task, then start it.
    Unregister  stop and remove every declared task.
    Start       start every declared task that exists.

.PARAMETER InstallDir
    Required for -Mode Installed: the directory holding the frozen exes.

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

    [string]$InstallDir
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RepoRoot     = Split-Path -Parent $PSScriptRoot
$DeclPath     = Join-Path $PSScriptRoot 'tasks.psd1'
$DeathWaitSec = 10

if (-not (Test-Path -LiteralPath $DeclPath)) {
    Write-Host "ERROR: task declaration not found: $DeclPath" -ForegroundColor Red
    exit 1
}
$decl = Import-PowerShellDataFile -LiteralPath $DeclPath
$tasks = $decl.Tasks

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


# --- the one definition of what a Face Unlock task looks like ----------------
function New-FaceUnlockTaskParts {
    [pscustomobject]@{
        Trigger   = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME `
                                               -LogonType Interactive -RunLevel Limited
        Settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
                                                 -DontStopIfGoingOnBatteries `
                                                 -StartWhenAvailable -Hidden `
                                                 -ExecutionTimeLimit ([TimeSpan]::Zero)
    }
}


# Resolve a declared task to what the scheduler needs, or $null when this
# layout cannot run it.
function Resolve-FaceUnlockTask {
    param([hashtable]$Task)

    if ($Mode -eq 'Dev') {
        return [pscustomobject]@{
            Name             = $Task.Name
            Execute          = Join-Path $RepoRoot '.venv\Scripts\pythonw.exe'
            Argument         = $Task.DevArgs
            WorkingDirectory = $RepoRoot
        }
    }

    if (-not $Task.InstalledExe) {
        Write-Host ("SKIP  {0} -- {1}" -f $Task.Name, $Task.SkipReason) -ForegroundColor Yellow
        return $null
    }
    return [pscustomobject]@{
        Name             = $Task.Name
        Execute          = Join-Path $InstallDir $Task.InstalledExe
        Argument         = ''
        WorkingDirectory = $InstallDir
    }
}


# One process-matching criterion, reused by the kill and by the death-wait so
# the two cannot drift. Dev runs the modules under python/pythonw, so match on
# the commandline; Installed runs the frozen exes, so match on image name.
function Get-FaceUnlockProcess {
    param([string[]]$ExeNames, [string[]]$CommandLineNeedles)

    if ($Mode -eq 'Dev') {
        return @(Get-CimInstance Win32_Process `
                    -Filter "Name='python.exe' OR Name='pythonw.exe'" `
                    -ErrorAction SilentlyContinue |
                 Where-Object {
                     $cl = $_.CommandLine
                     $cl -and ($CommandLineNeedles | Where-Object { $cl -like "*$_*" })
                 })
    }
    if (-not $ExeNames) { return @() }
    $filter = ($ExeNames | ForEach-Object { "Name='$_'" }) -join ' OR '
    return @(Get-CimInstance Win32_Process -Filter $filter -ErrorAction SilentlyContinue)
}


# Stop-Process only SIGNALS. The Local\FaceUnlockService mutex and the
# FIRST_PIPE_INSTANCE pipe name stay held until the last handle is gone, so
# starting on a blind delay races a slow-dying process into a mutex-loser exit.
# Bounded: on timeout warn and continue, never hang.
function Stop-FaceUnlockProcess {
    param([string[]]$ExeNames, [string[]]$CommandLineNeedles)

    $running = Get-FaceUnlockProcess -ExeNames $ExeNames -CommandLineNeedles $CommandLineNeedles
    if (-not $running) { return }

    Write-Host ("Stopping {0} running Face Unlock process(es)..." -f $running.Count)
    $running | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $DeathWaitSec) {
        $left = Get-FaceUnlockProcess -ExeNames $ExeNames -CommandLineNeedles $CommandLineNeedles
        if (-not $left) { return }
        Start-Sleep -Milliseconds 200
    }
    $left = Get-FaceUnlockProcess -ExeNames $ExeNames -CommandLineNeedles $CommandLineNeedles
    if ($left) {
        Write-Warning ("{0} process(es) still alive after {1}s; continuing anyway " -f $left.Count, $DeathWaitSec +
                       "(a new instance may exit as a mutex-loser)")
    }
}


# --- resolve everything up front so a bad declaration fails before any change -
$resolved = @()
foreach ($task in $tasks) {
    $r = Resolve-FaceUnlockTask -Task $task
    if ($r) { $resolved += $r }
}

$exeNames = @($tasks | Where-Object { $_.InstalledExe } | ForEach-Object { $_.InstalledExe })
$needles  = @('face_service', 'presence_monitor', 'tools.watchdog')

Write-Host ("Mode={0} Action={1} -- {2} of {3} declared task(s) apply" -f `
            $Mode, $Action, $resolved.Count, $tasks.Count)


switch ($Action) {

    'Unregister' {
        Stop-FaceUnlockProcess -ExeNames $exeNames -CommandLineNeedles $needles
        foreach ($task in $tasks) {
            Unregister-ScheduledTask -TaskName $task.Name -Confirm:$false -ErrorAction SilentlyContinue
            Write-Host ("Unregistered: {0}" -f $task.Name)
        }
        Write-Host "All declared tasks removed."
    }

    'Start' {
        foreach ($r in $resolved) {
            Start-ScheduledTask -TaskName $r.Name -ErrorAction SilentlyContinue
            Write-Host ("Started: {0}" -f $r.Name)
        }
    }

    'Register' {
        # Replace any previous registration cleanly: -Force overwrites a task
        # but leaves one that has since been dropped from the declaration.
        foreach ($task in $tasks) {
            Unregister-ScheduledTask -TaskName $task.Name -Confirm:$false -ErrorAction SilentlyContinue
        }
        Stop-FaceUnlockProcess -ExeNames $exeNames -CommandLineNeedles $needles

        $parts = New-FaceUnlockTaskParts
        foreach ($r in $resolved) {
            if ($r.Argument) {
                $action = New-ScheduledTaskAction -Execute $r.Execute -Argument $r.Argument `
                                                  -WorkingDirectory $r.WorkingDirectory
            }
            else {
                $action = New-ScheduledTaskAction -Execute $r.Execute `
                                                  -WorkingDirectory $r.WorkingDirectory
            }
            Register-ScheduledTask -TaskName $r.Name -Action $action -Trigger $parts.Trigger `
                                   -Principal $parts.Principal -Settings $parts.Settings -Force | Out-Null
            Write-Host ("Registered (hidden, no time limit): {0} -> {1} {2}" -f `
                        $r.Name, $r.Execute, $r.Argument)
        }
        foreach ($r in $resolved) {
            Start-ScheduledTask -TaskName $r.Name -ErrorAction SilentlyContinue
        }
        Write-Host "Started."
    }
}

exit 0
