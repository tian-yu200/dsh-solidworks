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

. (Join-Path $PSScriptRoot "SolidWorksDshLinks.ps1")

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
$UserSkillRoot = Join-Path $DshHome "skills"
$PresetSkillsLink = Join-Path $SolidworksPreset "skills"
$UserSkillLink = Join-Path $UserSkillRoot "solidworks-cad"
$RepoPresetSkillRoot = Join-Path $ProjectRoot "dsh-integration\presets\solidworks-cad\skills"
# The only trees this restore may delete recursively, and the root no component of their paths may
# leave (a junction on $DshHome\profiles would otherwise be followed into somebody else's tree).
$ManagedRoots = @($SolidworksProfile, $SolidworksPreset, $SolidworksPolicy, $PresetSkillsLink,
                  $UserSkillLink)
# Every managed path is derived from $DshHome; if it is a junction, "restore" would write and
# delete through that junction. Refuse before the snapshot, before the reads, before anything.
Assert-DshHomeNotReparse $DshHome | Out-Null

# Capture what the managed links point at NOW, before anything is unlinked: the snapshot has to be
# able to put the same links back, and the incoming manifest has to be read for the layout it
# recorded. Both use the same field names Install writes (presetSkillsLink / userSkillLink /
# presetLayout) — an earlier revision of this script read a field Install never wrote, and a
# restore therefore deleted the user-root skill link instead of restoring it.
$FoundPresetLink = Get-DshLinkTarget $PresetSkillsLink
$FoundUserLink = Get-DshLinkTarget $UserSkillLink

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
    presetSkillsLink = $FoundPresetLink
    userSkillLink = $FoundUserLink
    presetLayout = $(if ($FoundPresetLink) { "linked" } else { "copy" })
}
if ($PreRestoreManifest.webPatchExisted) {
    Copy-Item -LiteralPath $WebPatch -Destination (Join-Path $PreRestoreRoot "web.cordis.patch.yml")
}
if ($PreRestoreManifest.solidworksProfileExisted) {
    Copy-DshTreeNoReparse -Source $SolidworksProfile `
        -Destination (Join-Path $PreRestoreRoot "solidworks-profile") | Out-Null
}
if ($PreRestoreManifest.solidworksPresetExisted) {
    # Everything real in the preset directory is snapshotted (a skills JUNCTION is recorded as its
    # target instead of copied), so restoring this snapshot reproduces the current state exactly.
    Copy-DshPresetBackup -PresetDir $SolidworksPreset `
        -BackupDir (Join-Path $PreRestoreRoot "solidworks-cad-preset") `
        -SkillsLinkPath $PresetSkillsLink | Out-Null
}
if ($PreRestoreManifest.solidworksPolicyExisted) {
    Copy-DshTreeNoReparse -Source $SolidworksPolicy `
        -Destination (Join-Path $PreRestoreRoot "solidworks-policy") | Out-Null
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
    Remove-DshManagedTree -Path $SolidworksProfile -AllowedRoots $ManagedRoots `
        -GuardRoot $DshHome -Purpose "restore" | Out-Null
    New-Item -ItemType Directory -Force -Path $SolidworksProfile | Out-Null
    Copy-DshTreeNoReparse -Source (Join-Path $BackupRoot "solidworks-profile") `
        -Destination $SolidworksProfile | Out-Null
} elseif (Test-Path -LiteralPath $SolidworksProfile) {
    Remove-DshManagedTree -Path $SolidworksProfile -AllowedRoots $ManagedRoots `
        -GuardRoot $DshHome -Purpose "restore" | Out-Null
}
if ($Manifest.solidworksPresetExisted) {
    Remove-DshManagedTree -Path $SolidworksPreset -AllowedRoots $ManagedRoots `
        -GuardRoot $DshHome -Purpose "restore" -ManagedLinks @($PresetSkillsLink) | Out-Null
    $Restored = Restore-DshPresetFromBackup -BackupDir $BackupRoot -PresetDir $SolidworksPreset `
        -SkillsLinkPath $PresetSkillsLink -Manifest $Manifest `
        -FallbackSkillsTarget $RepoPresetSkillRoot
    Write-Host ("Preset restored ({0}): {1}; skills link: {2}" -f $Restored.layout,
                ($Restored.restored -join ", "), $Restored.skills)
} elseif (Test-Path -LiteralPath $SolidworksPreset) {
    Remove-DshManagedTree -Path $SolidworksPreset -AllowedRoots $ManagedRoots `
        -GuardRoot $DshHome -Purpose "restore" | Out-Null
}
# The user-root skill link exists so the `standard` preset can still find the skill. The manifest
# says what it pointed at: a recorded target is recreated, an empty one means the link did not
# exist in the state being restored, so only a link is removed — never a real directory.
if ($Manifest.PSObject.Properties.Name -contains "userSkillLink") {
    if ($Manifest.userSkillLink -and (Test-Path -LiteralPath ([string]$Manifest.userSkillLink))) {
        Remove-DshLink -Path $UserSkillLink | Out-Null
        if (-not (Test-Path -LiteralPath $UserSkillLink)) {
            New-Item -ItemType Junction -Path $UserSkillLink `
                -Target ([string]$Manifest.userSkillLink) | Out-Null
            Write-Host "User skill link restored -> $([string]$Manifest.userSkillLink)"
        }
    } elseif (-not $Manifest.userSkillLink) {
        Remove-DshLink -Path $UserSkillLink | Out-Null
    }
} elseif (Test-DshReparsePoint $UserSkillLink) {
    # A manifest from before links were recorded: this backup cannot say whether the link existed,
    # so it is left alone rather than guessed at.
    Write-Warning "backup manifest records no userSkillLink; leaving $UserSkillLink as it is"
}
if ($Manifest.solidworksPolicyExisted) {
    Remove-DshManagedTree -Path $SolidworksPolicy -AllowedRoots $ManagedRoots `
        -GuardRoot $DshHome -Purpose "restore" | Out-Null
    New-Item -ItemType Directory -Force -Path $SolidworksPolicy | Out-Null
    Copy-DshTreeNoReparse -Source (Join-Path $BackupRoot "solidworks-policy") `
        -Destination $SolidworksPolicy | Out-Null
} elseif (Test-Path -LiteralPath $SolidworksPolicy) {
    Remove-DshManagedTree -Path $SolidworksPolicy -AllowedRoots $ManagedRoots `
        -GuardRoot $DshHome -Purpose "restore" | Out-Null
}
if ($Manifest.installManifestExisted) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $InstallManifest) | Out-Null
    Copy-Item -LiteralPath (Join-Path $BackupRoot "solidworks-dsh-install.json") -Destination $InstallManifest -Force
} elseif (Test-Path -LiteralPath $InstallManifest) {
    Remove-Item -LiteralPath $InstallManifest -Force
}

Write-Host "Restored DSH configuration from $BackupRoot"
