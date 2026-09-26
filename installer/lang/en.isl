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
StillRunning=The installed Face Unlock is still running (%1 process(es) survived the stop), so Setup will not overwrite it. Sign out of Windows and sign in again, then run Setup again.
TasksFailed=Face Unlock could not register its background tasks. Face sign-in will not work until they are registered. Details: %1
RemoveData=Also remove your saved Face Unlock data at %1?%n%nThis includes the photos of your face, your face profile and the encrypted Windows password. Choose No to keep them for a future reinstall.
RunReEnroll=Set up your face again
DirRefused=Face Unlock installs only into %1 (it holds the sign-in component Windows loads at the lock screen).
WinOld=This Windows version is older than Windows 11 24H2. Face Unlock is officially supported on Windows 11 24H2 and 25H2; it installs, but it is not tested here.
ModelsTitle=Face recognition models
ModelsDesc=Face Unlock uses the InsightFace "buffalo_l" models. They are not included in this installer.
ModelsTerms=The face recognition models (InsightFace buffalo_l: det_10g, w600k_r50, 2d106det, 1k3d68, genderage) are made by the InsightFace project and are licensed by it for NON-COMMERCIAL RESEARCH PURPOSES ONLY: "The pretrained models we provided with this library are available for non-commercial research purposes only, including both auto-downloading models and manual-downloading models."%n%nFace Unlock does not redistribute them. Setup downloads the official archive buffalo_l.zip from the InsightFace GitHub release (the same source the insightface library uses), or takes a copy you already have, checks its size and SHA-256 against the values Face Unlock was built with, and unpacks the five files into the program folder. Nothing else is sent anywhere.%n%nIf your use is commercial, you need a license from InsightFace.
ModelsAccept=I have read the InsightFace terms and accept them for these models
ModelsDownload=Download buffalo_l.zip (about 275 MB) from the InsightFace release
ModelsLocal=Use a buffalo_l.zip I already have:
ModelsBrowse=Browse...
ModelsBrowseTitle=Choose buffalo_l.zip
ModelsNeedAccept=Face Unlock cannot work without these models. Accept the InsightFace terms to continue, or cancel Setup.
ModelsBadFile=This file is not the expected buffalo_l.zip (size or SHA-256 does not match).
ModelsDownloading=Downloading the face recognition models...
ModelsDownloadFailed=The face recognition models could not be downloaded: %1%n%nCheck the internet connection, or download buffalo_l.zip yourself and choose it on the models page (silent install: /MODELZIP=<path>).
ModelsInstallFailed=The face recognition models could not be unpacked and checked. Face sign-in will not work until Setup is run again.
ReadyModels=Face recognition models will be downloaded from:
StatusModels=Unpacking and checking the face recognition models...
AclFailed=The program folder can be changed by accounts other than administrators, so the sign-in tile was NOT registered. Remove the extra permissions from the folder and run Setup again.
CPFailed=The Face Unlock sign-in tile could not be registered (regsvr32 exit code %1). Face sign-in will not appear at the lock screen.
