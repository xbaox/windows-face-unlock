# Re-register the FaceUnlock tasks to run with pythonw.exe (no CMD window)
# and the hidden-task attribute set. Safe to re-run.
$root = Split-Path -Parent $PSScriptRoot
$py   = Join-Path $root '.venv\Scripts\pythonw.exe'

function Register-HiddenTask {
    param([string]$Name, [string]$Module)
    $action  = New-ScheduledTaskAction -Execute $py -Argument "-m $Module" -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $prins   = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
    # -ExecutionTimeLimit ([TimeSpan]::Zero) serialises to PT0S = "no limit". Without it the task
    # takes the Windows default of PT72H and the scheduler kills these always-on tasks after 3 days.
    $set     = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -Hidden -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Principal $prins -Settings $set -Force | Out-Null
    Write-Host "Registered (hidden, pythonw, no time limit): $Name"
}

Register-HiddenTask -Name 'FaceUnlock-Service'  -Module 'face_service'
Register-HiddenTask -Name 'FaceUnlock-Presence' -Module 'presence_monitor'

# Stop anything running then restart with new settings. One criterion, reused by the kill and the
# death-wait below so the two cannot drift apart. It matches BOTH halves of a venv pair: the
# .venv pythonw.exe launcher stub and the base-interpreter worker it spawns.
$matching = {
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*face_service*' -or $_.CommandLine -like '*presence_monitor*' }
}

& $matching | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# Death-wait: Stop-Process only SIGNALS. The Local\FaceUnlockService mutex and the
# FIRST_PIPE_INSTANCE pipe name stay held until the last handle is gone, so starting on a blind
# delay races a slow-dying process into a mutex-loser exit. Bounded: on timeout warn and start
# anyway, never hang.
$sw = [Diagnostics.Stopwatch]::StartNew()
while ((@(& $matching).Count) -gt 0 -and $sw.Elapsed.TotalSeconds -lt 10) {
    Start-Sleep -Milliseconds 200
}
$left = @(& $matching).Count
if ($left -gt 0) {
    Write-Warning "$left face-unlock process(es) still alive after 10s; starting anyway (the new instance may exit as a mutex-loser)"
}

Start-ScheduledTask -TaskName 'FaceUnlock-Service'
Start-ScheduledTask -TaskName 'FaceUnlock-Presence'
Write-Host "Started."
