Stop-ScheduledTask -TaskName 'FaceUnlock-Service' -ErrorAction SilentlyContinue
Stop-ScheduledTask -TaskName 'FaceUnlock-Presence' -ErrorAction SilentlyContinue

# One criterion, reused by the kill and the death-wait below so the two cannot drift apart. It
# matches BOTH halves of a venv pair: the .venv pythonw.exe launcher stub and the base-interpreter
# worker it spawns. This collapses two redundant kill passes into one: the first was a Get-Process
# sweep testing `$_.Path -like '*face-unlock*'` (matches only the launcher, which lives under the
# repo's .venv; never the worker, which lives under Python312) OR `$_.CommandLine`, a property
# Get-Process does not expose at all on PowerShell 5.1 -- so on 5.1 it was launcher-only. The
# worker was still covered, by the second (WMI) pass, whose filter is the one kept below.
$matching = {
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*face_service*' -or $_.CommandLine -like '*presence_monitor*' }
}

& $matching | ForEach-Object {
    Write-Host "Killing PID $($_.ProcessId): $($_.CommandLine)"
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}

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
Write-Host "Done."
