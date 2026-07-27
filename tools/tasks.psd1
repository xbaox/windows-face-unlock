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
            # No frozen entry point exists: installer/windows_face_unlock.spec
            # builds face_service.exe and face_unlock_tray.exe only. And even
            # with one, tools/watchdog.py matches the service by
            # Name='pythonw.exe' + commandline, which never matches an
            # installed face_service.exe -- so its kill-then-start restart
            # would be inert. Both must be fixed before this flips on;
            # registering it now would be supervision in name only.
            InstalledExe = ''
            SkipReason   = 'no frozen watchdog executable, and the restart path is dev-layout only'
        }
    )
}
