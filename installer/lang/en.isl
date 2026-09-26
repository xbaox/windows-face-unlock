; Face Unlock installer texts, English (Stage 9, act 9b R16 / F-215). %n = new line, %1.. = values.
[CustomMessages]
TaskGroup=Optional components
TaskCP=Sign in to Windows with your face (registers the Face Unlock sign-in tile)
StatusRegCP=Registering the Face Unlock sign-in tile...
StatusRegTasks=Registering the background tasks...
RunSavePassword=Save your Windows password for face sign-in
RunUpdatePassword=Update your saved Windows password for face sign-in
RunEnroll=Set up face sign-in now
IconTray=Face Unlock
IconUninstall=Uninstall Face Unlock
WhyOwnerArg=/OWNER=%1 is not a person's account on this PC (SYSTEM and service accounts cannot own Face Unlock).
WhyNoConsole=Nobody is signed in at this PC's console, so Setup cannot tell whose Face Unlock this is. Sign in at the PC itself and run Setup there, or pass /OWNER=DOMAIN\user.
WhyNoConsoleUser=The console session has no signed-in user. Sign in at the PC and run Setup there, or pass /OWNER=DOMAIN\user.
WhyUnresolved=The console user %1 could not be resolved to a person's account (%2). Pass /OWNER=DOMAIN\user or /OWNER=<SID>.
CannotInstall=Face Unlock cannot be installed yet.%n%n%1
BelongsToOther=Face Unlock on this PC belongs to %1.%n%nMake %2 the owner instead? Face sign-in then works for %2 only; the previous owner signs in with PIN or password as usual.
ReadyOwner=Face Unlock owner (the only account that can sign in with its face):
UnpackFailed=Setup could not unpack its task registrar: %1
RegistrarRunFailed=Setup could not run the Face Unlock task registrar:%n%1%n%nThe existing installation has to be stopped before it can be replaced. Stop it manually, then run Setup again.
StillRunning=The installed Face Unlock is still running, so Setup will not overwrite it.%n%nThe task registrar exited with code %1 after trying twice to stop it; it lists the surviving process IDs in its own output.%n%nStop it and run Setup again:%n  %2
TasksFailed=Face Unlock could not register its background tasks (exit code %1).%n%nFace sign-in will not work until they are registered. Details: %2
RemoveData=Also remove your saved Face Unlock data at %1?%n%nThis includes the photos of your face, your face profile and the encrypted Windows password. Choose No to keep them for a future reinstall.
