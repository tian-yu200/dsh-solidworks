[CmdletBinding()]
param(
    [string]$DshRoot = "D:\deepseek-harness",
    [Parameter(ValueFromRemainingArguments)]
    [string[]]$DshArguments
)

$Dsh = Join-Path $DshRoot "node_modules\.bin\dsh.CMD"
if (-not (Test-Path -LiteralPath $Dsh)) {
    throw "DSH launcher not found: $Dsh"
}

& $Dsh --profile solidworks @DshArguments
exit $LASTEXITCODE
