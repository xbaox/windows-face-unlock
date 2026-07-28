# Restart the Face Unlock tasks: stop them, kill any leftover processes with
# the bounded death-wait, start them again.
#
# This is a thin wrapper. The task list, the process-matching criterion and the
# death-wait all live in register_tasks.ps1 + tasks.psd1, so there is exactly
# one definition of each. It used to be a hand-rolled copy that named two of the
# three tasks and carried its own kill filter.
#
# Pass -DryRun to see the plan without touching anything.
[CmdletBinding()]
param(
    [switch]$DryRun
)

& (Join-Path $PSScriptRoot 'register_tasks.ps1') -Action Restart -DryRun:$DryRun
exit $LASTEXITCODE
