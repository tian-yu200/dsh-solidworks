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
        -replace '(?im)(api[_-]?key\s*[:=]\s*).*$', '$1<redacted>' `
        -replace '(?im)(authorization\s*[:=]\s*).*$', '$1<redacted>' `
        -replace '(?im)(bearer\s+)[A-Za-z0-9._~+/-]+=*', '$1<redacted>' `
        -replace '(?im)(token\s*[:=]\s*).*$', '$1<redacted>'
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
$PolicyTemplate = Join-Path $ProjectRoot "dsh-integration/plugins/solidworks-policy/index.js"
$ResolvedPolicy = Join-Path $ProfileRoot "node_modules/@dsh-solidworks/policy/index.js"
$PolicyPackage = Join-Path $DshHome "plugins\solidworks-policy\package.json"
$ModLensPackage = Join-Path $ProfileRoot "node_modules\@liustack\modlens\package.json"
$DumpPath = Join-Path $ProjectRoot "state\solidworks-profile.dump.txt"

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

# User patches also carry model/provider settings and other MCPs. Compare only managed fields.
$ProfilePatchScript = Join-Path $PSScriptRoot "SolidWorksProfilePatch.mjs"
foreach ($Patch in @(
    @{ Name = "web"; Source = $WebProfile; Template = $WebProfileTemplate },
    @{ Name = "solidworks"; Source = $Profile; Template = $ProfileTemplate }
)) {
    $PatchOutput = & node $ProfilePatchScript --mode check --dsh-root $DshRoot `
        --template $Patch.Template --source $Patch.Source --python $Python `
        --server $Server --adapter-dir $AdapterDir 2>&1 | Out-String
    $PatchExitCode = $LASTEXITCODE
    Add-Check "Installed $($Patch.Name) profile synchronized" ($PatchExitCode -eq 0) $PatchOutput.Trim()
}

$PresetSynchronized = (
    (Test-Path -LiteralPath $Preset) -and
    (Test-Path -LiteralPath $PresetTemplate) -and
    ((Get-FileHash -Algorithm SHA256 -LiteralPath $Preset).Hash -eq
        (Get-FileHash -Algorithm SHA256 -LiteralPath $PresetTemplate).Hash)
)
Add-Check "Installed preset synchronized" $PresetSynchronized $PresetTemplate

$TauriPatch = Join-Path $DshHome "profiles/tauri/cordis.patch.yml"
if (Test-Path -LiteralPath $TauriPatch) {
    $TauriOutput = & node $ProfilePatchScript --mode check-tauri --dsh-root $DshRoot `
        --source $TauriPatch --preset $PresetTemplate 2>&1 | Out-String
    $TauriExitCode = $LASTEXITCODE
    Add-Check "Tauri embedded persona synchronized" ($TauriExitCode -eq 0) $TauriOutput.Trim()
}

# The desktop core loads the policy from its own profile's node_modules, not from plugins/.
$TauriPolicyDir = Join-Path $DshHome "profiles/tauri/node_modules/@dsh-solidworks/policy"
if (Test-Path -LiteralPath (Join-Path $DshHome "profiles/tauri")) {
    $RepoPolicyDir = Join-Path $PSScriptRoot "..\plugins\solidworks-policy"
    $StalePolicyFiles = @(foreach ($File in "index.js", "intake-progress.js", "package.json", "cordis.patch.yml") {
        $Installed = Join-Path $TauriPolicyDir $File
        if (-not (Test-Path -LiteralPath $Installed) -or
            (Get-FileHash -LiteralPath $Installed).Hash -ne (Get-FileHash -LiteralPath (Join-Path $RepoPolicyDir $File)).Hash) {
            $File
        }
    })
    Add-Check "Desktop policy plugin synchronized" ($StalePolicyFiles.Count -eq 0) `
        $(if ($StalePolicyFiles.Count) { "stale: " + ($StalePolicyFiles -join ", ") } else { $TauriPolicyDir })
}

# --- single source of truth: the skill content is LINKED, never copied -------------------------
# Measured 2026-10-28: the installed SKILL.md was a two-month-old copy (20385 B against the repo's
# 26552 B), so sessions outside the repo silently read stale instructions. The link helpers are the
# SAME functions Install and Restore use (SolidWorksDshLinks.ps1), so a diagnosis cannot disagree
# with what the installer believes about a path.
. (Join-Path $PSScriptRoot "SolidWorksDshLinks.ps1")

$RepoPresetSkillRoot = Join-Path $ProjectRoot "dsh-integration\presets\solidworks-cad\skills"
$RepoSkillDir = Join-Path $RepoPresetSkillRoot "solidworks-cad"
$PresetSkillsPath = Join-Path $DshHome ".agent-presets\solidworks-cad\skills"
$UserSkillPath = Join-Path $DshHome "skills\solidworks-cad"
$ProjectSkillPath = Join-Path $ProjectRoot ".dsh\skills\solidworks-cad"

$PresetLinkTarget = Get-DshLinkTarget $PresetSkillsPath
Add-Check "Preset skills link -> repo" (
    $PresetLinkTarget -and
    ($PresetLinkTarget -eq [System.IO.Path]::GetFullPath($RepoPresetSkillRoot).TrimEnd('\', '/'))
) ("{0} -> {1}" -f $PresetSkillsPath, $PresetLinkTarget)

$UserLinkTarget = Get-DshLinkTarget $UserSkillPath
Add-Check "User skill link -> repo (standard preset)" (
    $UserLinkTarget -and
    ($UserLinkTarget -eq [System.IO.Path]::GetFullPath($RepoSkillDir).TrimEnd('\', '/'))
) ("{0} -> {1}" -f $UserSkillPath, $UserLinkTarget)

# A REAL directory at either link path means a copy is back, which is exactly how the drift started.
Add-Check "No stale skill copy in the preset" (
    (Get-DshLinkTarget $PresetSkillsPath) -ne $null
) "$PresetSkillsPath must be a junction to the repo, not a copied directory"
Add-Check "No stale skill copy in ~/.dsh/skills" (
    (Get-DshLinkTarget $UserSkillPath) -ne $null
) "$UserSkillPath must be a junction to the repo, not a copied directory"

# Readable THROUGH the links, and byte-equal to the repo source (a broken target reads as absent).
$InstalledSkill = Join-Path $PresetSkillsPath "solidworks-cad\SKILL.md"
$UserCopySkill = Join-Path $UserSkillPath "SKILL.md"
$RepoSkill = Join-Path $RepoSkillDir "SKILL.md"
$SkillHash = if (Test-Path -LiteralPath $RepoSkill) {
    (Get-FileHash -Algorithm SHA256 -LiteralPath $RepoSkill).Hash
} else {
    ""
}
Add-Check "Preset SKILL.md == repo" (
    ($SkillHash -ne "") -and (Test-Path -LiteralPath $InstalledSkill) -and
    ((Get-FileHash -Algorithm SHA256 -LiteralPath $InstalledSkill).Hash -eq $SkillHash)
) $InstalledSkill
Add-Check "User-root SKILL.md == repo" (
    ($SkillHash -ne "") -and (Test-Path -LiteralPath $UserCopySkill) -and
    ((Get-FileHash -Algorithm SHA256 -LiteralPath $UserCopySkill).Hash -eq $SkillHash)
) $UserCopySkill
Add-Check "Project SKILL.md == repo" (
    ($SkillHash -ne "") -and (Test-Path -LiteralPath (Join-Path $ProjectSkillPath "SKILL.md")) -and
    ((Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $ProjectSkillPath "SKILL.md")).Hash -eq $SkillHash)
) "the repo mirror (.dsh/skills) is byte-identical"

# The skill-filesystem row silently drops an unknown key: `roots:` there means the preset's own
# skills are not being scanned at all (the bug that hid the skill from every session).
$PresetUsesCustomSkillDirs = (
    $PresetSource.Contains("customSkillDirs:") -and
    -not ($PresetSource -match "(?m)^\s{4}roots:\s*$")
)
Add-Check "Preset skill row uses customSkillDirs" $PresetUsesCustomSkillDirs "roots: is not a schema key and is dropped silently"

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

$PolicySynchronized = Test-Path -LiteralPath $PolicyTemplate
if ($PolicySynchronized) {
    $ExpectedPolicyHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $PolicyTemplate).Hash
    foreach ($Entry in @($Policy, $ResolvedPolicy)) {
        if (-not (Test-Path -LiteralPath $Entry) -or
            (Get-FileHash -Algorithm SHA256 -LiteralPath $Entry).Hash -ne $ExpectedPolicyHash) {
            $PolicySynchronized = $false
        }
    }
}
foreach ($Module in @("intake-progress.js", "package.json")) {
    $ExpectedModule = Join-Path (Split-Path -Parent $PolicyTemplate) $Module
    foreach ($Entry in @($Policy, $ResolvedPolicy)) {
        $InstalledModule = Join-Path (Split-Path -Parent $Entry) $Module
        if (-not (Test-Path -LiteralPath $ExpectedModule) -or
            -not (Test-Path -LiteralPath $InstalledModule) -or
            (Get-FileHash -Algorithm SHA256 -LiteralPath $InstalledModule).Hash -ne
                (Get-FileHash -Algorithm SHA256 -LiteralPath $ExpectedModule).Hash) {
            $PolicySynchronized = $false
        }
    }
}
Add-Check "Installed policy synchronized" $PolicySynchronized "user plugin and resolved profile dependency"

if (Test-Path -LiteralPath $Python) {
    & $Python -c "import mcp,httpx,pydantic; print('ok')" *> $null
    Add-Check "Python dependencies" ($LASTEXITCODE -eq 0) "mcp, httpx, pydantic"

    Push-Location $AdapterDir
    try {
        & $Python -c "import asyncio,server; assert len(asyncio.run(server.mcp.list_tools())) == 12" *> $null
        Add-Check "MCP import / 12 tools" ($LASTEXITCODE -eq 0) $Server
    } finally {
        Pop-Location
    }
}

$DumpOk = $false
if (Test-Path -LiteralPath $Dsh) {
    $PreviousDshHome = $env:DSH_HOME
    try {
        $env:DSH_HOME = $DshHome
        $RawDump = & $Dsh --profile solidworks --dump-config 2>&1 | Out-String
        $DumpOk = $LASTEXITCODE -eq 0
    } finally {
        $env:DSH_HOME = $PreviousDshHome
    }
    if ($RawDump) {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $DumpPath) | Out-Null
        Redact-Config $RawDump | Set-Content -LiteralPath $DumpPath -Encoding utf8
    }
    Add-Check "Redacted config dump" $DumpOk $DumpPath
    $HasPolicy = $DumpOk -and $RawDump.Contains("@dsh-solidworks/policy")
    Add-Check "Policy in profile" $HasPolicy "solidworks profile dump"
}

$ImageCapabilityScript = Join-Path $PSScriptRoot "Check-ModelImageCapability.mjs"
if ($DumpOk -and (Get-Command node -ErrorAction SilentlyContinue) -and (Test-Path -LiteralPath $ImageCapabilityScript)) {
    $CapabilityOutput = & node $ImageCapabilityScript `
        --profile-dump $DumpPath `
        --profiles (Join-Path $DshHome "profiles") 2>&1 | Out-String
    $CapabilityExitCode = $LASTEXITCODE
    $CapabilityLines = $CapabilityOutput -split "`n" |
        Where-Object { $_ -match "default model|FAIL|ERROR:|image capability:" } |
        ForEach-Object { $_.Trim() }
    if (-not $CapabilityLines) { $CapabilityLines = @("Image checker returned exit $CapabilityExitCode without a capability report") }
    Add-Check "Model image capability" ($CapabilityExitCode -eq 0) ($CapabilityLines -join " | ")
} else {
    Add-Check "Model image capability" $false "fresh profile dump or image checker unavailable"
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
