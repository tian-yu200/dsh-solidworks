[CmdletBinding()]
param(
    [string]$DshHome = "$HOME\.dsh",
    [string]$DshRoot = "D:\deepseek-harness",
    [string]$ProjectRoot = "",
    [string]$Python = ""
)

$ErrorActionPreference = "Continue"
$ProjectRoot = if ($ProjectRoot) { [System.IO.Path]::GetFullPath($ProjectRoot) } else { (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path }
$Python = if ($Python) { [System.IO.Path]::GetFullPath($Python) } else { Join-Path $ProjectRoot "mcp-server-solidworks-main\adapters\claude\.venv\Scripts\python.exe" }
$Checks = [System.Collections.Generic.List[object]]::new()
function Add-Check([string]$Name, [bool]$Ok, [string]$Detail) {
    $Checks.Add([pscustomobject]@{ Check = $Name; Ok = $Ok; Detail = $Detail })
}

function Read-PackageVersion([string]$PackagePath) {
    if (-not (Test-Path -LiteralPath $PackagePath)) {
        return $null
    }
    try {
        return [string]((
            Get-Content -Raw -LiteralPath $PackagePath -ErrorAction Stop |
                ConvertFrom-Json -ErrorAction Stop
        ).version)
    } catch {
        return $null
    }
}

function Redact-Config([string]$Raw) {
    return $Raw `
        -replace '(?im)(api[_-]?key\s*[:=]\s*)\S+', '$1<redacted>' `
        -replace '(?im)(authorization\s*[:=]\s*)\S+', '$1<redacted>' `
        -replace '(?im)(bearer\s+)[A-Za-z0-9._~+/-]+=*', '$1<redacted>' `
        -replace '(?im)(token\s*[:=]\s*)\S+', '$1<redacted>'
}

$Dsh = Join-Path $DshRoot "node_modules\.bin\dsh.CMD"
$DshPackage = Join-Path $DshRoot "node_modules\@deepseek-ai\dsh\package.json"
$AdapterDir = Join-Path $ProjectRoot "mcp-server-solidworks-main\adapters\dsh"
$Server = Join-Path $AdapterDir "server.py"
$Execution = Join-Path $ProjectRoot "mcp-server-solidworks-main\execution\solidworks\SolidworksExecution\bin\Debug\SolidworksExecution.exe"
$WebProfileRoot = Join-Path $DshHome "profiles\web"
$WebProfile = Join-Path $WebProfileRoot "cordis.patch.yml"
$ProfileRoot = Join-Path $DshHome "profiles\solidworks"
$Profile = Join-Path $ProfileRoot "cordis.patch.yml"
$Preset = Join-Path $DshHome ".agent-presets\solidworks-cad\agent.cordis.yml"
$WebProfileTemplate = Join-Path $ProjectRoot "dsh-integration\profiles\web\cordis.patch.yml"
$ProfileTemplate = Join-Path $ProjectRoot "dsh-integration\profiles\solidworks\cordis.patch.yml"
$PresetTemplate = Join-Path $ProjectRoot "dsh-integration\presets\solidworks-cad\agent.cordis.yml"
$Policy = Join-Path $DshHome "plugins\solidworks-policy\index.js"
$PolicyPackage = Join-Path $DshHome "plugins\solidworks-policy\package.json"
$ModLensPackage = Join-Path $ProfileRoot "node_modules\@liustack\modlens\package.json"
$DumpPath = Join-Path $ProjectRoot "state\solidworks-profile.dump.txt"

function Convert-ToYamlPath([string]$Path) {
    return ($Path -replace '\\', '/')
}

function Render-ProfileTemplate([string]$TemplatePath) {
    $AdapterPath = Join-Path $ProjectRoot "mcp-server-solidworks-main\adapters\dsh"
    $Source = Get-Content -Raw -LiteralPath $TemplatePath
    $Source = $Source.Replace('__PYTHON__', (Convert-ToYamlPath $Python))
    $Source = $Source.Replace('__DSH_SERVER__', (Convert-ToYamlPath (Join-Path $AdapterPath "server.py")))
    return $Source.Replace('__DSH_ADAPTER_DIR__', (Convert-ToYamlPath $AdapterPath))
}

$DshVersion = Read-PackageVersion $DshPackage
$DshDetail = if ($DshVersion) { "version $DshVersion" } else { $DshPackage }
Add-Check "DSH launcher" ((Test-Path -LiteralPath $Dsh) -and $null -ne $DshVersion) $DshDetail
Add-Check "Python venv" (Test-Path -LiteralPath $Python) $Python
Add-Check "High-level MCP" (Test-Path -LiteralPath $Server) $Server
Add-Check "Execution server" (Test-Path -LiteralPath $Execution) $Execution
Add-Check "Web profile" (Test-Path -LiteralPath $WebProfile) $WebProfile
Add-Check "SolidWorks profile" (Test-Path -LiteralPath $Profile) $Profile
Add-Check "SolidWorks preset" (Test-Path -LiteralPath $Preset) $Preset

function Test-SolidWorksMcpConfig([string]$Source) {
    return (
        $Source.Contains("name: '@deepseek-ai/dsh-mcp-client'") -and
        $Source.Contains("serverName: solidworks") -and
        $Source.Contains("adapters/dsh/server.py") -and
        $Source.Contains("failOnStartupError: true")
    )
}

$WebProfileSource = if (Test-Path -LiteralPath $WebProfile) {
    Get-Content -Raw -LiteralPath $WebProfile
} else {
    ""
}
$ProfileSource = if (Test-Path -LiteralPath $Profile) {
    Get-Content -Raw -LiteralPath $Profile
} else {
    ""
}
$PresetSource = if (Test-Path -LiteralPath $Preset) {
    Get-Content -Raw -LiteralPath $Preset
} else {
    ""
}
Add-Check "Web profile MCP client" (Test-SolidWorksMcpConfig $WebProfileSource) "host-mounted for dsh web"
Add-Check "SolidWorks profile MCP client" (Test-SolidWorksMcpConfig $ProfileSource) "host-mounted for --profile solidworks"
$PresetHasNoMcp = -not $PresetSource.Contains("@deepseek-ai/dsh-mcp-client")
Add-Check "Preset has no duplicate MCP" $PresetHasNoMcp "solidworks-cad agent.cordis.yml"

$WebProfileSynchronized = (Test-Path -LiteralPath $WebProfile) -and (Test-Path -LiteralPath $WebProfileTemplate) -and
    ((Get-Content -Raw -LiteralPath $WebProfile) -eq (Render-ProfileTemplate $WebProfileTemplate))
Add-Check "Installed web profile synchronized" $WebProfileSynchronized $WebProfileTemplate

$ProfileSynchronized = (Test-Path -LiteralPath $Profile) -and (Test-Path -LiteralPath $ProfileTemplate) -and
    ((Get-Content -Raw -LiteralPath $Profile) -eq (Render-ProfileTemplate $ProfileTemplate))
Add-Check "Installed solidworks profile synchronized" $ProfileSynchronized $ProfileTemplate

$PresetSynchronized = (
    (Test-Path -LiteralPath $Preset) -and
    (Test-Path -LiteralPath $PresetTemplate) -and
    ((Get-FileHash -Algorithm SHA256 -LiteralPath $Preset).Hash -eq
        (Get-FileHash -Algorithm SHA256 -LiteralPath $PresetTemplate).Hash)
)
Add-Check "Installed preset synchronized" $PresetSynchronized $PresetTemplate

$PolicyVersion = Read-PackageVersion $PolicyPackage
$PolicyDetail = if ($PolicyVersion) { "version $PolicyVersion" } else { $Policy }
Add-Check "Policy plugin" ((Test-Path -LiteralPath $Policy) -and $null -ne $PolicyVersion) $PolicyDetail

$ServerSource = if (Test-Path -LiteralPath $Server) {
    Get-Content -Raw -LiteralPath $Server
} else {
    ""
}
$PolicySource = if (Test-Path -LiteralPath $Policy) {
    Get-Content -Raw -LiteralPath $Policy
} else {
    ""
}
$ApprovalOk = (
    $ServerSource.Contains("def request_confirmation(") -and
    $ServerSource.Contains("def confirm_action(") -and
    $PolicySource.Contains("mcp__solidworks__confirm_action")
)
Add-Check "Approval workflow" $ApprovalOk "request_confirmation + confirm_action + policy guard"

$ModLensVersion = Read-PackageVersion $ModLensPackage
$ModLensDetail = if ($ModLensVersion) { "version $ModLensVersion" } else { $ModLensPackage }
Add-Check "ModLens" ($null -ne $ModLensVersion) $ModLensDetail

if (Test-Path -LiteralPath $Python) {
    & $Python -c "import mcp,httpx,pydantic; print('ok')" *> $null
    Add-Check "Python dependencies" ($LASTEXITCODE -eq 0) "mcp, httpx, pydantic"

    Push-Location $AdapterDir
    try {
        & $Python -c "import asyncio,server; assert len(asyncio.run(server.mcp.list_tools())) == 9" *> $null
        Add-Check "MCP import / 9 tools" ($LASTEXITCODE -eq 0) $Server
    } finally {
        Pop-Location
    }
}

if (Test-Path -LiteralPath $Dsh) {
    $RawDump = & $Dsh --profile solidworks --dump-config 2>&1 | Out-String
    $DumpOk = $LASTEXITCODE -eq 0
    if ($RawDump) {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $DumpPath) | Out-Null
        Redact-Config $RawDump | Set-Content -LiteralPath $DumpPath -Encoding utf8
    }
    Add-Check "Redacted config dump" $DumpOk $DumpPath
    $HasPolicy = $DumpOk -and $RawDump.Contains("@dsh-solidworks/policy")
    Add-Check "Policy in profile" $HasPolicy "solidworks profile dump"
}

$ImageCapabilityScript = Join-Path $PSScriptRoot "Check-ModelImageCapability.mjs"
if ((Get-Command node -ErrorAction SilentlyContinue) -and (Test-Path -LiteralPath $ImageCapabilityScript)) {
    $CapabilityOutput = & node $ImageCapabilityScript `
        --settings (Join-Path $DshHome "settings.yaml") `
        --profiles (Join-Path $DshHome "profiles") 2>&1 | Out-String
    $CapabilityLines = $CapabilityOutput -split "`n" |
        Where-Object { $_ -match "default model|FAIL" } |
        ForEach-Object { $_.Trim() }
    Add-Check "Model image capability" ($LASTEXITCODE -eq 0) ($CapabilityLines -join " | ")
}

$Process = Get-Process -Name SLDWORKS -ErrorAction SilentlyContinue
$ProcessDetail = if ($Process) {
    "PID: " + (($Process.Id | Sort-Object) -join ", ")
} else {
    "not running (adapter can launch it)"
}
Add-Check "SolidWorks process" ($null -ne $Process) $ProcessDetail

$Port = Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue
$PortDetail = if ($Port) { "listening" } else { "not listening (adapter can auto-start it)" }
Add-Check "Execution port 5000" ($null -ne $Port) $PortDetail

$Checks | Format-Table -AutoSize
$Optional = @("SolidWorks process", "Execution port 5000")
if ($Checks.Where({ -not $_.Ok -and $_.Check -notin $Optional }).Count) {
    exit 1
}
