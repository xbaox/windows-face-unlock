# The Face Unlock scheduled tasks for the DEVELOPER layout (tools/register_tasks.ps1 -Mode Dev).
# Stage 9 (act 9b R17): an installed copy registers the same three tasks through
# face_service/taskreg.py (TASKS); tools/packaging_selftest.py keeps the two lists equal.
#
# The layouts, for reference:
#
#   Dev       -- .venv\Scripts\pythonw.exe <DevArgs>, working dir = repo root
#   Installed -- <InstallDir>\<InstalledExe>,          working dir = InstallDir
#
# Uninstall walks the same list, so a task can never be created by one path and
# left behind by another. Adding a task here is the whole change; nothing else
# enumerates task names (installer.iss included).
#
# InstalledExe = '' means "this task has no frozen executable yet" -- the
# registrar skips it in Installed mode and says so out loud, rather than
# creating a task that points at a file which does not exist.
#
# Optional per-task keys (Stage 8b, F-36):
#   Priority         -- Task Scheduler priority, 0 (highest) .. 10; default 7 (below normal).
#   RestartOnFailure -- $true = restart a failed run 3 times, one minute apart.

@{
    Tasks = @(
        @{
            Name         = 'FaceUnlock-Service'
            Description  = 'Face Unlock: named-pipe verification service'
            DevArgs      = '-m face_service'
            InstalledExe = 'face_service.exe'
            SkipReason   = ''
            # Normal priority (5) instead of the scheduler's below-normal 7: this is the process
            # the lock screen waits on. Only the service -- the tray and watchdog stay at 7 (P-10).
            Priority     = 5
        },
        @{
            Name         = 'FaceUnlock-Presence'
            Description  = 'Face Unlock: tray icon and presence probe'
            DevArgs      = '-m presence_monitor'
            InstalledExe = 'face_unlock_tray.exe'
            SkipReason   = ''
            RestartOnFailure = $true
        },
        @{
            Name         = 'FaceUnlock-Watchdog'
            Description  = 'Face Unlock: service supervisor (pings the pipe, restarts on hang)'
            DevArgs      = '-m tools.watchdog'
            # Enabled in Stage 7d-H. This was '' because BOTH halves were
            # missing: the spec built no watchdog executable, and
            # tools/watchdog.py matched the service by Name='pythonw.exe' +
            # commandline, which never matches an installed face_service.exe --
            # so its kill-then-start restart would have been inert and every
            # attempt would still have reported "unrecoverable". Registering it
            # then would have been supervision in name only, which is worse than
            # the honest skip, so this line was flipped LAST: 7d-D made the
            # matcher layout-aware, 7d-H added the third Analysis to
            # installer/windows_face_unlock.spec, and only then did this change.
            # (Stage 9, D-128: the installed restart path HAS run on a real machine -- 9 restarts in
            # watchdog.log, 08-08..09-25; installed copies are now registered by
            # face_service/taskreg.py, whose TASKS mirror this list.)
            InstalledExe = 'face_unlock_watchdog.exe'
            SkipReason   = ''
            RestartOnFailure = $true
        }
    )
}
