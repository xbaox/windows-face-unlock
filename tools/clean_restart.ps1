# Restart the Face Unlock tasks: stop them, kill any leftover processes with
# the bounded death-wait, start them again.
#
# This is a thin wrapper. The task list, the process-matching criterion and the
# death-wait all live in register_tasks.ps1 + tasks.psd1, so there is exactly
# one definition of each. It used to be a hand-rolled copy that named two of the
# three tasks and carried its own kill filter.
#
# -Mode exists for the same reason the watchdog grew one in 7d-D: without it
# this wrapper always fell through to the registrar's default, 'Dev', so on an
# installed machine its process matching found nothing and "clean restart"
# quietly degraded into "start the tasks again". -Mode Installed additionally
# needs -InstallDir, which is passed straight through; omitting it lets the
# registrar produce its own error rather than inventing a second one here.
#
# Pass -DryRun to see the plan without touching anything. This file contains no
# mutating call of its own -- every side effect is inside register_tasks.ps1,
# textually below its own A/B phase gate.
[CmdletBinding()]
param(
    [ValidateSet('Dev', 'Installed')]
    [string]$Mode = 'Dev',

    [string]$InstallDir,

    [switch]$DryRun
)

# Splatted rather than positional so -InstallDir is omitted entirely when empty.
# fu* prefix per the naming rule in register_tasks.ps1: never assign to a name
# that is a parameter of the script being called.
$fuArgs = @{
    Action = 'Restart'
    Mode   = $Mode
    DryRun = $DryRun
}
if ($InstallDir) { $fuArgs['InstallDir'] = $InstallDir }

& (Join-Path $PSScriptRoot 'register_tasks.ps1') @fuArgs
exit $LASTEXITCODE
