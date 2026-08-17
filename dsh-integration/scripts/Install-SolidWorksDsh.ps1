[CmdletBinding()]
param(
    [string]$DshHome = "$HOME\.dsh",
    [string]$DshRoot = "D:\deepseek-harness",
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$IntegrationRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ProjectRoot = (Resolve-Path (Join-Path $IntegrationRoot "..")).Path
$DshRoot = (Resolve-Path -LiteralPath $DshRoot).Path
$DshHome = [System.IO.Path]::GetFullPath($DshHome)
$ProfilesRoot = Join-Path $DshHome "profiles"
$PresetRoot = Join-Path $DshHome ".agent-presets"
$PluginsRoot = Join-Path $DshHome "plugins"
$Stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmss.fffZ")
$BackupRoot = Join-Path $ProjectRoot "config-backups\$Stamp"
$StateRoot = Join-Path $ProjectRoot "state"
$InstallManifest = Join-Path $StateRoot "solidworks-dsh-install.json"

New-Item -ItemType Directory -Force -Path `
    $BackupRoot, `
    (Join-Path $StateRoot "jobs"), `
    (Join-Path $ProjectRoot "backups"), `
    (Join-Path $ProjectRoot "config-backups") | Out-Null

$WebPatch = Join-Path $ProfilesRoot "web\cordis.patch.yml"
$SolidworksProfile = Join-Path $ProfilesRoot "solidworks"
$SolidworksPreset = Join-Path $PresetRoot "solidworks-cad"
$SolidworksPolicy = Join-Path $PluginsRoot "solidworks-policy"

function Assert-UnderDshHome([string]$Candidate) {
    $Root = $DshHome.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    $Resolved = [System.IO.Path]::GetFullPath($Candidate)
    if (-not $Resolved.StartsWith($Root, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to modify path outside DSH home: $Resolved"
    }
    return $Resolved
}

$WebPatch = Assert-UnderDshHome $WebPatch
$SolidworksProfile = Assert-UnderDshHome $SolidworksProfile
$SolidworksPreset = Assert-UnderDshHome $SolidworksPreset
$SolidworksPolicy = Assert-UnderDshHome $SolidworksPolicy

function Find-Pnpm {
    $Candidates = @(
        (Join-Path $DshRoot "node_modules\.bin\pnpm.CMD"),
        (Join-Path $DshRoot "node_modules\.bin\pnpm"),
        (Get-Command pnpm.CMD -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1),
        (Get-Command pnpm -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1)
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
    if (-not $Candidates) {
        throw "pnpm was not found. Install pnpm or provide a DSH root containing node_modules\.bin\pnpm.CMD."
    }
    return [System.IO.Path]::GetFullPath($Candidates[0])
}

function Read-PackageVersion([string]$PackagePath) {
    if (-not (Test-Path -LiteralPath $PackagePath)) {
        return $null
    }
    return [string]((Get-Content -Raw -LiteralPath $PackagePath | ConvertFrom-Json).version)
}

$Pnpm = Find-Pnpm

if (-not $Python) {
    $Python = Join-Path $ProjectRoot "mcp-server-solidworks-main\adapters\claude\.venv\Scripts\python.exe"
}
$Python = [System.IO.Path]::GetFullPath($Python)
$AdapterDir = Join-Path $ProjectRoot "mcp-server-solidworks-main\adapters\dsh"
$Server = Join-Path $AdapterDir "server.py"

function Convert-ToYamlPath([string]$Path) {
    return ($Path -replace '\\', '/')
}

function Render-ProfileTemplate([string]$TemplatePath, [string]$DestinationPath) {
    $Source = Get-Content -Raw -LiteralPath $TemplatePath
    $Rendered = $Source.Replace('__PYTHON__', (Convert-ToYamlPath $Python))
    $Rendered = $Rendered.Replace('__DSH_SERVER__', (Convert-ToYamlPath $Server))
    $Rendered = $Rendered.Replace('__DSH_ADAPTER_DIR__', (Convert-ToYamlPath $AdapterDir))
    [System.IO.File]::WriteAllText(
        $DestinationPath,
        $Rendered,
        [System.Text.UTF8Encoding]::new($false)
    )
}

$Manifest = [ordered]@{
    createdAtUtc = (Get-Date).ToUniversalTime().ToString("o")
    dshHome = $DshHome
    projectRoot = $ProjectRoot
    webPatchExisted = Test-Path -LiteralPath $WebPatch
    solidworksProfileExisted = Test-Path -LiteralPath $SolidworksProfile
    solidworksPresetExisted = Test-Path -LiteralPath $SolidworksPreset
    solidworksPolicyExisted = Test-Path -LiteralPath $SolidworksPolicy
    installManifestExisted = Test-Path -LiteralPath $InstallManifest
}

if ($Manifest.webPatchExisted) {
    Copy-Item -LiteralPath $WebPatch -Destination (Join-Path $BackupRoot "web.cordis.patch.yml")
}
if ($Manifest.solidworksProfileExisted) {
    Copy-Item -LiteralPath $SolidworksProfile -Destination (Join-Path $BackupRoot "solidworks-profile") -Recurse
}
if ($Manifest.solidworksPresetExisted) {
    Copy-Item -LiteralPath $SolidworksPreset -Destination (Join-Path $BackupRoot "solidworks-cad-preset") -Recurse
}
if ($Manifest.solidworksPolicyExisted) {
    Copy-Item -LiteralPath $SolidworksPolicy -Destination (Join-Path $BackupRoot "solidworks-policy") -Recurse
}
if ($Manifest.installManifestExisted) {
    Copy-Item -LiteralPath $InstallManifest -Destination (Join-Path $BackupRoot "solidworks-dsh-install.json")
}
$Manifest | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $BackupRoot "manifest.json") -Encoding utf8

foreach ($Target in @($SolidworksProfile, $SolidworksPreset, $SolidworksPolicy)) {
    $ResolvedTarget = Assert-UnderDshHome $Target
    if (Test-Path -LiteralPath $ResolvedTarget) {
        Remove-Item -LiteralPath $ResolvedTarget -Recurse -Force
    }
}
New-Item -ItemType Directory -Force -Path `
    (Split-Path $WebPatch), `
    $SolidworksProfile, `
    $SolidworksPreset, `
    $SolidworksPolicy | Out-Null
Render-ProfileTemplate (Join-Path $IntegrationRoot "profiles\web\cordis.patch.yml") $WebPatch
Copy-Item -Path (Join-Path $IntegrationRoot "profiles\solidworks\*") -Destination $SolidworksProfile -Recurse -Force
Copy-Item -Path (Join-Path $IntegrationRoot "presets\solidworks-cad\*") -Destination $SolidworksPreset -Recurse -Force
Copy-Item -Path (Join-Path $IntegrationRoot "plugins\solidworks-policy\*") -Destination $SolidworksPolicy -Recurse -Force
Render-ProfileTemplate (Join-Path $IntegrationRoot "profiles\solidworks\cordis.patch.yml") (Join-Path $SolidworksProfile "cordis.patch.yml")

& $Pnpm install --prod --dir $SolidworksProfile
if ($LASTEXITCODE -ne 0) {
    throw "pnpm install failed for the SolidWorks profile (exit $LASTEXITCODE)"
}

$DshPackage = Join-Path $DshRoot "node_modules\@deepseek-ai\dsh\package.json"
$ModLensPackage = Join-Path $SolidworksProfile "node_modules\@liustack\modlens\package.json"
$PolicyPackage = Join-Path $SolidworksPolicy "package.json"
$Installed = [ordered]@{
    installedAtUtc = (Get-Date).ToUniversalTime().ToString("o")
    backupRoot = $BackupRoot
    dshHome = $DshHome
    dshRoot = $DshRoot
    python = $Python
    pnpm = $Pnpm
    versions = [ordered]@{
        dsh = Read-PackageVersion $DshPackage
        modlens = Read-PackageVersion $ModLensPackage
        solidworksPolicy = Read-PackageVersion $PolicyPackage
    }
    paths = [ordered]@{
        webPatch = $WebPatch
        profile = $SolidworksProfile
        preset = $SolidworksPreset
        policy = $SolidworksPolicy
    }
}
$Installed | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $InstallManifest -Encoding utf8

Write-Host "Installed SolidWorks DSH profile and preset."
Write-Host "Backup: $BackupRoot"
Write-Host "Manifest: $InstallManifest"
Write-Host "Start web: $DshRoot\node_modules\.bin\dsh.CMD web"
Write-Host "Start dedicated profile: $DshRoot\node_modules\.bin\dsh.CMD --profile solidworks"
