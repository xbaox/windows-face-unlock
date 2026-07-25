# Register the FaceUnlock-Watchdog scheduled task (Stage 3 / Step 5).
# It pings the pipe and restarts FaceUnlock-Service (kill-then-start) on N consecutive failures.
# Runs hidden via pythonw (no CMD window). Safe to re-run. ASCII only (PowerShell 5.1 BOM-safe).
$root = Split-Path -Parent $PSScriptRoot
$py   = Join-Path $root '.venv\Scripts\pythonw.exe'

$action  = New-ScheduledTaskAction  -Execute $py -Argument '-m tools.watchdog' -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger  -AtLogOn -User $env:USERNAME
$prins   = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
# -ExecutionTimeLimit ([TimeSpan]::Zero) serialises to PT0S = "no limit". Without it the task takes
# the Windows default of PT72H -- and this one self-loops forever, so the scheduler would kill the
# watchdog after 3 days and nothing would restart it until the next logon.
$set     = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -Hidden -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName 'FaceUnlock-Watchdog' -Action $action -Trigger $trigger -Principal $prins -Settings $set -Force | Out-Null
Write-Host "Registered (hidden, pythonw, no time limit): FaceUnlock-Watchdog"

Start-ScheduledTask -TaskName 'FaceUnlock-Watchdog'
Write-Host "Started."

# To remove:
#   Unregister-ScheduledTask -TaskName 'FaceUnlock-Watchdog' -Confirm:$false
