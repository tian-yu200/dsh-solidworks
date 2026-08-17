[CmdletBinding()]
param(
    [string]$ProjectRoot = "",
    [string]$DshRoot = "D:\deepseek-harness",
    [string]$OutputPath = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = if ($ProjectRoot) { [System.IO.Path]::GetFullPath($ProjectRoot) } else { (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path }
$OutputPath = if ($OutputPath) { $OutputPath } else { Join-Path $ProjectRoot "state\solidworks-profile.dump.txt" }
$Dsh = Join-Path $DshRoot "node_modules\.bin\dsh.CMD"
$Raw = & $Dsh --profile solidworks --dump-config 2>&1 | Out-String
$Redacted = $Raw `
    -replace '(?im)(api[_-]?key\s*[:=]\s*)\S+', '$1<redacted>' `
    -replace '(?im)(authorization\s*[:=]\s*)\S+', '$1<redacted>' `
    -replace '(?im)(bearer\s+)[A-Za-z0-9._~+/-]+=*', '$1<redacted>' `
    -replace '(?im)(token\s*[:=]\s*)\S+', '$1<redacted>'

$Directory = Split-Path -Parent $OutputPath
if ($Directory) {
    New-Item -ItemType Directory -Force -Path $Directory | Out-Null
}
$Redacted | Set-Content -LiteralPath $OutputPath -Encoding utf8
Write-Host "Redacted config dump written to $OutputPath"
