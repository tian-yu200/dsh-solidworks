[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$BackupRoot
)

$ErrorActionPreference = "Stop"
$BackupRoot = (Resolve-Path -LiteralPath $BackupRoot).Path
$ManifestPath = Join-Path $BackupRoot "manifest.json"
if (-not (Test-Path -LiteralPath $ManifestPath)) {
    throw "No manifest.json found in $BackupRoot"
}

$Manifest = Get-Content -Raw -LiteralPath $ManifestPath | ConvertFrom-Json
$DshHome = [System.IO.Path]::GetFullPath([string]$Manifest.dshHome)
$ProfilesRoot = Join-Path $DshHome "profiles"
$PresetRoot = Join-Path $DshHome ".agent-presets"
$PluginsRoot = Join-Path $DshHome "plugins"
$WebPatch = Join-Path $ProfilesRoot "web\cordis.patch.yml"
$SolidworksProfile = Join-Path $ProfilesRoot "solidworks"
$SolidworksPreset = Join-Path $PresetRoot "solidworks-cad"
$SolidworksPolicy = Join-Path $PluginsRoot "solidworks-policy"
$ProjectRoot = [System.IO.Path]::GetFullPath([string]$Manifest.projectRoot)
$InstallManifest = Join-Path $ProjectRoot "state\solidworks-dsh-install.json"

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

if ($Manifest.webPatchExisted) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $WebPatch) | Out-Null
    Copy-Item -LiteralPath (Join-Path $BackupRoot "web.cordis.patch.yml") -Destination $WebPatch -Force
} elseif (Test-Path -LiteralPath $WebPatch) {
    Remove-Item -LiteralPath $WebPatch -Force
}
if ($Manifest.solidworksProfileExisted) {
    if (Test-Path -LiteralPath $SolidworksProfile) {
        Remove-Item -LiteralPath $SolidworksProfile -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $SolidworksProfile | Out-Null
    Copy-Item -Path (Join-Path $BackupRoot "solidworks-profile\*") -Destination $SolidworksProfile -Recurse -Force
} elseif (Test-Path -LiteralPath $SolidworksProfile) {
    Remove-Item -LiteralPath $SolidworksProfile -Recurse -Force
}
if ($Manifest.solidworksPresetExisted) {
    if (Test-Path -LiteralPath $SolidworksPreset) {
        Remove-Item -LiteralPath $SolidworksPreset -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $SolidworksPreset | Out-Null
    Copy-Item -Path (Join-Path $BackupRoot "solidworks-cad-preset\*") -Destination $SolidworksPreset -Recurse -Force
} elseif (Test-Path -LiteralPath $SolidworksPreset) {
    Remove-Item -LiteralPath $SolidworksPreset -Recurse -Force
}
if ($Manifest.solidworksPolicyExisted) {
    if (Test-Path -LiteralPath $SolidworksPolicy) {
        Remove-Item -LiteralPath $SolidworksPolicy -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $SolidworksPolicy | Out-Null
    Copy-Item -Path (Join-Path $BackupRoot "solidworks-policy\*") -Destination $SolidworksPolicy -Recurse -Force
} elseif (Test-Path -LiteralPath $SolidworksPolicy) {
    Remove-Item -LiteralPath $SolidworksPolicy -Recurse -Force
}
if ($Manifest.installManifestExisted) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $InstallManifest) | Out-Null
    Copy-Item -LiteralPath (Join-Path $BackupRoot "solidworks-dsh-install.json") -Destination $InstallManifest -Force
} elseif (Test-Path -LiteralPath $InstallManifest) {
    Remove-Item -LiteralPath $InstallManifest -Force
}

Write-Host "Restored DSH configuration from $BackupRoot"
