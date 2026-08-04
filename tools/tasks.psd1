# The Face Unlock scheduled tasks. THIS IS THE ONLY PLACE THEY ARE LISTED.
#
# tools/register_tasks.ps1 registers, unregisters and starts exactly what is
# declared here, in both layouts:
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

@{
    Tasks = @(
        @{
            Name         = 'FaceUnlock-Service'
            Description  = 'Face Unlock: named-pipe verification service'
            DevArgs      = '-m face_service'
            InstalledExe = 'face_service.exe'
            SkipReason   = ''
        },
        @{
            Name         = 'FaceUnlock-Presence'
            Description  = 'Face Unlock: tray icon and presence probe'
            DevArgs      = '-m presence_monitor'
            InstalledExe = 'face_unlock_tray.exe'
            SkipReason   = ''
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
            # Until it is exercised on a real installed machine, treat the
            # installed restart path as untested rather than proven.
            InstalledExe = 'face_unlock_watchdog.exe'
            SkipReason   = ''
        }
    )
}
