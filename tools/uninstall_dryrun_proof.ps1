<#
.SYNOPSIS
    Prove -- statically, without executing it -- that tools\uninstall.ps1 cannot
    change anything before its phase-A gate. Run this before trusting a -DryRun.

.DESCRIPTION
    uninstall.ps1 is built on one promise: "PHASE A -- INVENTORY. Everything below
    this banner only READS", and phase B is entered only with -Force. A -DryRun is
    safe to run on a live machine exactly as far as that promise holds. Comments do
    not hold promises; this script checks the code.

    It PARSES uninstall.ps1 (Parser::ParseFile) and never dot-sources, invokes or
    otherwise runs it, so running THIS script has no effect on the machine either.

    The proof has four parts:

      1. GATE. Find the `exit` inside `if (-not $Force) { ... }` and assert it sits
         at script top level, so reaching it ends the process rather than a nested
         scope. Its line number is the gate line.

      2. REACHABILITY. Starting from every top-level statement at or above the gate
         line, follow function calls transitively. The result is the set of code
         that CAN run before the gate: pre-gate top-level statements, plus the
         bodies of every function they reach. A function defined above the gate but
         never called before it is NOT in this set, and neither is one defined below.

      3. MUTATION SCAN. Walk every CommandAst in the file and classify it against a
         deny-list of state-changing cmdlets, their aliases, and known external
         mutators; separately walk every method call for mutating members
         ([IO.File]::Delete and friends). Any hit inside the reachable set fails the
         proof.

      4. INDIRECTION. `& $something` can invoke code this file does not contain, so
         every ampersand invocation is treated as mutating BY DEFAULT. One pattern
         is resolved instead of assumed: `& $Body` where $Body is a [scriptblock]
         parameter and every call site passes a scriptblock LITERAL written in this
         same file -- literals are part of this AST, so part 3 already scanned them.
         Anything else indirect must live below the gate.

    Exit 0 = the proof holds. Exit 1 = it does not; do not run -DryRun until the
    findings are understood.

.EXAMPLE
    .\tools\uninstall_dryrun_proof.ps1
.EXAMPLE
    .\tools\uninstall_dryrun_proof.ps1 -Path .\tools\uninstall.ps1
#>
[CmdletBinding()]
param(
    [string]$Path
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# NAMING RULE (as in uninstall.ps1 / register_tasks.ps1): no local may share a name
# with a parameter, in any casing. Internal names are fu*-prefixed.
$fuTarget = if ($Path) { $Path } else { Join-Path $PSScriptRoot 'uninstall.ps1' }
if (-not (Test-Path -LiteralPath $fuTarget)) {
    Write-Host "target not found: $fuTarget" -ForegroundColor Red
    exit 1
}
$fuTarget = (Resolve-Path -LiteralPath $fuTarget).Path

# Commands that change machine state. Aliases are listed explicitly because the AST
# records what was written, not what it resolves to at run time.
$fuMutatingCommands = @(
    'Remove-Item', 'ri', 'rm', 'rmdir', 'rd', 'del', 'erase',
    'Remove-ItemProperty', 'Clear-Item', 'Clear-ItemProperty', 'Clear-Content',
    'Set-Item', 'Set-ItemProperty', 'New-Item', 'ni', 'New-ItemProperty',
    'Move-Item', 'mi', 'move', 'mv', 'Rename-Item', 'rni', 'ren',
    'Copy-Item', 'cpi', 'copy', 'cp',
    'Set-Content', 'sc', 'Add-Content', 'ac', 'Out-File', 'Export-Csv', 'Export-Clixml',
    'Register-ScheduledTask', 'Unregister-ScheduledTask', 'Set-ScheduledTask',
    'Start-ScheduledTask', 'Stop-ScheduledTask', 'Enable-ScheduledTask', 'Disable-ScheduledTask',
    'Start-Service', 'Stop-Service', 'Set-Service', 'New-Service', 'Remove-Service',
    'Stop-Process', 'kill', 'spps', 'Start-Process', 'saps',
    'Set-Acl', 'New-PSDrive', 'Remove-PSDrive', 'Write-EventLog',
    'regsvr32', 'regsvr32.exe', 'reg', 'reg.exe', 'schtasks', 'schtasks.exe',
    'taskkill', 'taskkill.exe', 'icacls', 'cacls', 'attrib', 'cmd', 'cmd.exe'
)
# Mutating .NET members, matched on the member name alone (the type is not always
# statically knowable, so this errs toward reporting).
$fuMutatingMembers = @(
    'Delete', 'DeleteSubKey', 'DeleteSubKeyTree', 'DeleteValue',
    'Create', 'CreateDirectory', 'CreateSubKey',
    'WriteAllText', 'WriteAllBytes', 'WriteAllLines', 'AppendAllText', 'AppendAllLines',
    'Move', 'MoveTo', 'Replace', 'SetAccessControl', 'SetValue', 'Kill'
)

$fuTokens = $null
$fuErrors = $null
$fuAst = [System.Management.Automation.Language.Parser]::ParseFile(
    $fuTarget, [ref]$fuTokens, [ref]$fuErrors)
if ($fuErrors -and $fuErrors.Count) {
    Write-Host "PROOF FAILED: $fuTarget does not parse" -ForegroundColor Red
    foreach ($fuE in $fuErrors) { Write-Host ("  {0}" -f $fuE) -ForegroundColor Red }
    exit 1
}

function Get-FuAll {
    param([Parameter(Mandatory)][type]$Kind)
    return @($fuAst.FindAll({ param($fuN) $fuN -is $Kind }, $true))
}

function Get-FuEnclosingFunction {
    <# Name of the function a node lives in, or '' for script top level. #>
    param([Parameter(Mandatory)]$Node)
    $fuP = $Node.Parent
    while ($null -ne $fuP) {
        if ($fuP -is [System.Management.Automation.Language.FunctionDefinitionAst]) { return $fuP.Name }
        $fuP = $fuP.Parent
    }
    return ''
}

$fuCommands  = Get-FuAll ([System.Management.Automation.Language.CommandAst])
$fuFunctions = Get-FuAll ([System.Management.Automation.Language.FunctionDefinitionAst])
$fuMembers   = Get-FuAll ([System.Management.Automation.Language.InvokeMemberExpressionAst])

Write-Host ''
Write-Host "AST dry-run proof -- $fuTarget" -ForegroundColor White
Write-Host ('=' * 72)

# --- 1. GATE ---------------------------------------------------------------------
$fuGateLine = 0
$fuGateText = ''
foreach ($fuIf in (Get-FuAll ([System.Management.Automation.Language.IfStatementAst]))) {
    $fuCond = $fuIf.Clauses[0].Item1.Extent.Text
    if ($fuCond -notmatch '-not\s+\$Force') { continue }
    $fuExits = @($fuIf.Clauses[0].Item2.FindAll(
        { param($fuN) $fuN -is [System.Management.Automation.Language.ExitStatementAst] }, $true))
    if (-not $fuExits.Count) { continue }
    if ((Get-FuEnclosingFunction $fuIf) -ne '') { continue }   # must end the SCRIPT
    $fuGateLine = $fuExits[0].Extent.StartLineNumber
    $fuGateText = $fuCond
    break
}
if (-not $fuGateLine) {
    Write-Host 'PROOF FAILED: no top-level `exit` inside an `if (-not $Force)` block.' -ForegroundColor Red
    Write-Host '              Without that gate a run without -Force is not bounded at all.' -ForegroundColor Red
    exit 1
}
Write-Host ''
Write-Host '[1] GATE' -ForegroundColor Cyan
Write-Host ("    condition   : {0}" -f $fuGateText)
Write-Host ("    exits at    : line {0} (script top level -- ends the process)" -f $fuGateLine)

# --- 2. REACHABILITY -------------------------------------------------------------
# Seed: every command invoked by a top-level statement at or above the gate line.
$fuFnByName = @{}
foreach ($fuF in $fuFunctions) { $fuFnByName[$fuF.Name] = $fuF }

$fuReachable = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
$fuQueue = New-Object System.Collections.Queue
foreach ($fuC in $fuCommands) {
    if ($fuC.Extent.StartLineNumber -gt $fuGateLine) { continue }
    if ((Get-FuEnclosingFunction $fuC) -ne '') { continue }   # top-level statements only
    $fuName = $fuC.GetCommandName()
    if ($fuName -and $fuFnByName.ContainsKey($fuName)) { $fuQueue.Enqueue($fuName) }
}
while ($fuQueue.Count) {
    $fuName = [string]$fuQueue.Dequeue()
    if (-not $fuReachable.Add($fuName)) { continue }
    $fuBody = $fuFnByName[$fuName]
    foreach ($fuInner in @($fuBody.FindAll(
                { param($fuN) $fuN -is [System.Management.Automation.Language.CommandAst] }, $true))) {
        $fuCallee = $fuInner.GetCommandName()
        if ($fuCallee -and $fuFnByName.ContainsKey($fuCallee)) { $fuQueue.Enqueue($fuCallee) }
    }
}
Write-Host ''
Write-Host '[2] REACHABLE BEFORE THE GATE' -ForegroundColor Cyan
Write-Host ("    top-level statements : lines 1..{0}" -f $fuGateLine)
Write-Host ("    functions reached    : {0}" -f $(if ($fuReachable.Count) { (@($fuReachable) | Sort-Object) -join ', ' } else { '(none)' }))
$fuUnreached = @($fuFunctions | ForEach-Object { $_.Name } | Where-Object { -not $fuReachable.Contains($_) } | Sort-Object)
Write-Host ("    functions NOT reached: {0}" -f $(if ($fuUnreached.Count) { $fuUnreached -join ', ' } else { '(none)' }))

function Test-FuPreGate {
    <# True when this node can execute before the gate. #>
    param([Parameter(Mandatory)]$Node)
    $fuFn = Get-FuEnclosingFunction $Node
    if ($fuFn -eq '') { return ($Node.Extent.StartLineNumber -le $fuGateLine) }
    return $fuReachable.Contains($fuFn)
}

# --- 3./4. MUTATION SCAN + INDIRECTION -------------------------------------------
$fuFindings = @()
$fuBelow    = @()
$fuResolved = @()

foreach ($fuC in $fuCommands) {
    $fuName = $fuC.GetCommandName()
    $fuLine = $fuC.Extent.StartLineNumber
    $fuKind = ''

    if ($fuName) {
        if ($fuMutatingCommands -contains $fuName) { $fuKind = 'MUTATING' }
    }
    else {
        # No static name: `& $var` / `. $var` / a call through an expression.
        $fuKind = 'INDIRECT'
        $fuFirst = $fuC.CommandElements[0]
        if ($fuFirst -is [System.Management.Automation.Language.VariableExpressionAst]) {
            $fuVar = $fuFirst.VariablePath.UserPath
            $fuOwner = Get-FuEnclosingFunction $fuC
            if ($fuOwner -ne '') {
                # Is $fuVar a [scriptblock] parameter of the owning function, and does every
                # call site pass a LITERAL scriptblock? Then the invoked code is in this file
                # and was already scanned above -- no unseen code can run.
                $fuFn = $fuFnByName[$fuOwner]
                $fuParams = @()
                if ($null -ne $fuFn.Body.ParamBlock) { $fuParams = @($fuFn.Body.ParamBlock.Parameters) }
                $fuIsSb = @($fuParams | Where-Object {
                    $_.Name.VariablePath.UserPath -eq $fuVar -and
                    $_.StaticType -eq [scriptblock]
                }).Count -gt 0
                if ($fuIsSb) {
                    $fuSites = @($fuCommands | Where-Object { $_.GetCommandName() -eq $fuOwner })
                    $fuAllLiteral = $fuSites.Count -gt 0
                    foreach ($fuSite in $fuSites) {
                        $fuHasLiteral = $false
                        foreach ($fuArg in $fuSite.CommandElements) {
                            if ($fuArg -is [System.Management.Automation.Language.ScriptBlockExpressionAst]) {
                                $fuHasLiteral = $true
                            }
                            elseif ($fuArg -is [System.Management.Automation.Language.VariableExpressionAst]) {
                                $fuAllLiteral = $false   # a variable could carry any scriptblock
                            }
                        }
                        if (-not $fuHasLiteral) { $fuAllLiteral = $false }
                    }
                    if ($fuAllLiteral) {
                        $fuKind = 'RESOLVED'
                        $fuResolved += [pscustomobject]@{
                            Line = $fuLine
                            What = ("& `$$fuVar in {0}() -- every call site passes a scriptblock literal ({1} site(s)); those bodies are scanned in place" -f $fuOwner, $fuSites.Count)
                        }
                    }
                }
            }
        }
    }

    if ($fuKind -eq '' -or $fuKind -eq 'RESOLVED') { continue }
    $fuWhat = if ($fuName) { $fuName } else { $fuC.Extent.Text }
    if ($fuWhat.Length -gt 58) { $fuWhat = $fuWhat.Substring(0, 55) + '...' }
    $fuRow = [pscustomobject]@{
        Line = $fuLine
        Kind = $fuKind
        In   = $(if ((Get-FuEnclosingFunction $fuC) -eq '') { '<top level>' } else { (Get-FuEnclosingFunction $fuC) + '()' })
        What = $fuWhat
    }
    if (Test-FuPreGate $fuC) { $fuFindings += $fuRow } else { $fuBelow += $fuRow }
}

foreach ($fuM in $fuMembers) {
    if ($fuMutatingMembers -notcontains $fuM.Member.Extent.Text) { continue }
    $fuWhat = $fuM.Extent.Text
    if ($fuWhat.Length -gt 58) { $fuWhat = $fuWhat.Substring(0, 55) + '...' }
    $fuRow = [pscustomobject]@{
        Line = $fuM.Extent.StartLineNumber
        Kind = 'MEMBER'
        In   = $(if ((Get-FuEnclosingFunction $fuM) -eq '') { '<top level>' } else { (Get-FuEnclosingFunction $fuM) + '()' })
        What = $fuWhat
    }
    if (Test-FuPreGate $fuM) { $fuFindings += $fuRow } else { $fuBelow += $fuRow }
}

Write-Host ''
Write-Host '[3] INDIRECTION RESOLVED (not assumed safe -- shown so it can be checked)' -ForegroundColor Cyan
if ($fuResolved.Count) {
    foreach ($fuR in ($fuResolved | Sort-Object Line)) {
        Write-Host ("    line {0,4}  {1}" -f $fuR.Line, $fuR.What) -ForegroundColor DarkGray
    }
}
else { Write-Host '    (none)' -ForegroundColor DarkGray }

Write-Host ''
Write-Host '[4] MUTATING / INDIRECT CALLS -- BELOW the gate (phase B; expected)' -ForegroundColor Cyan
if ($fuBelow.Count) {
    ($fuBelow | Sort-Object Line | Format-Table -AutoSize | Out-String).TrimEnd() -split "`n" |
        ForEach-Object { Write-Host ("    " + $_) -ForegroundColor DarkGray }
}
else { Write-Host '    (none)' -ForegroundColor DarkGray }

Write-Host ''
Write-Host ('=' * 72)
Write-Host ("scanned: {0} command(s), {1} function(s), {2} method call(s)" -f `
            $fuCommands.Count, $fuFunctions.Count, $fuMembers.Count) -ForegroundColor DarkGray
if ($fuFindings.Count) {
    Write-Host ''
    Write-Host 'PROOF FAILED -- reachable before the gate:' -ForegroundColor Red
    ($fuFindings | Sort-Object Line | Format-Table -AutoSize | Out-String).TrimEnd() -split "`n" |
        ForEach-Object { Write-Host ("    " + $_) -ForegroundColor Red }
    Write-Host ''
    Write-Host 'Do NOT run -DryRun against a live machine until these are resolved.' -ForegroundColor Red
    exit 1
}
Write-Host ''
Write-Host ("PROOF HOLDS -- no mutating or unresolved-indirect call is reachable before line {0}." -f $fuGateLine) `
           -ForegroundColor Green
Write-Host '-DryRun (and a bare run) can only read.' -ForegroundColor Green
exit 0
