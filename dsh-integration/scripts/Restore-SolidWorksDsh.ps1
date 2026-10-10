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

# Reversible restore: snapshot the CURRENT state of the five targets BEFORE anything is
# removed or overwritten, so this restore can itself be undone. The snapshot is a sibling
# of BackupRoot in the SAME layout and the SAME manifest format Install writes, so
# Restore-SolidWorksDsh.ps1 can restore it in turn (restoring the snapshot == rolling the
# restore back). Only targets that currently exist are captured, exactly like Install's
# backup step; the flags in the snapshot's manifest record which ones those were.
$PreRestoreStamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmss.fffZ")
$PreRestoreRoot = Join-Path (Split-Path -Parent $BackupRoot) "pre-restore-$PreRestoreStamp"
New-Item -ItemType Directory -Force -Path $PreRestoreRoot | Out-Null
$PreRestoreManifest = [ordered]@{
    createdAtUtc = (Get-Date).ToUniversalTime().ToString("o")
    dshHome = $DshHome
    projectRoot = $ProjectRoot
    webPatchExisted = Test-Path -LiteralPath $WebPatch
    solidworksProfileExisted = Test-Path -LiteralPath $SolidworksProfile
    solidworksPresetExisted = Test-Path -LiteralPath $SolidworksPreset
    solidworksPolicyExisted = Test-Path -LiteralPath $SolidworksPolicy
    installManifestExisted = Test-Path -LiteralPath $InstallManifest
}
if ($PreRestoreManifest.webPatchExisted) {
    Copy-Item -LiteralPath $WebPatch -Destination (Join-Path $PreRestoreRoot "web.cordis.patch.yml")
}
if ($PreRestoreManifest.solidworksProfileExisted) {
    Copy-Item -LiteralPath $SolidworksProfile -Destination (Join-Path $PreRestoreRoot "solidworks-profile") -Recurse
}
if ($PreRestoreManifest.solidworksPresetExisted) {
    Copy-Item -LiteralPath $SolidworksPreset -Destination (Join-Path $PreRestoreRoot "solidworks-cad-preset") -Recurse
}
if ($PreRestoreManifest.solidworksPolicyExisted) {
    Copy-Item -LiteralPath $SolidworksPolicy -Destination (Join-Path $PreRestoreRoot "solidworks-policy") -Recurse
}
if ($PreRestoreManifest.installManifestExisted) {
    Copy-Item -LiteralPath $InstallManifest -Destination (Join-Path $PreRestoreRoot "solidworks-dsh-install.json")
}
$PreRestoreManifest | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $PreRestoreRoot "manifest.json") -Encoding utf8
Write-Host "Pre-restore snapshot: $PreRestoreRoot"

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
