[CmdletBinding()]
param(
    [string]$DshHome = "$HOME\.dsh",
    [string]$DshRoot = "D:\deepseek-harness",
    [string]$Python = "",
    # A preset directory holding files this integration does not know about is the user's work.
    # Install backs it all up first, then REFUSES; pass this only when replacing it is intended.
    [switch]$MigrateCustomPreset,
    # Same for a skill link that points somewhere else than the two targets this installer owns.
    [switch]$TakeOverSkillLinks
)

. (Join-Path $PSScriptRoot "SolidWorksDshLinks.ps1")

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
$TauriPatch = Join-Path $ProfilesRoot "tauri\cordis.patch.yml"
$SolidworksProfile = Join-Path $ProfilesRoot "solidworks"
$SolidworksPreset = Join-Path $PresetRoot "solidworks-cad"
$SolidworksPolicy = Join-Path $PluginsRoot "solidworks-policy"
# The desktop core resolves '@dsh-solidworks/policy' from the tauri profile's own node_modules,
# never from $PluginsRoot. Measured 2026-10-31: that copy was two days stale (no intake-progress,
# no read_drawing_text, the per-agent restriction that never applied) because nothing synced it.
$TauriPolicy = Join-Path $ProfilesRoot "tauri\node_modules\@dsh-solidworks\policy"
$PolicyRuntimeFiles = @("index.js", "intake-progress.js", "package.json", "cordis.patch.yml")
# Single source of truth: the skill content lives in the repo and is LINKED, never copied, so a
# session can never read a stale SKILL.md (measured 2026-10-28: the installed copy was two months
# behind the repo, 20385 B against 26552 B).
$RepoPresetSource = Join-Path $IntegrationRoot "presets\solidworks-cad"
$RepoPresetSkills = Join-Path $RepoPresetSource "skills"
$RepoSkillDir = Join-Path $RepoPresetSkills "solidworks-cad"
$PresetSkillsLink = Join-Path $SolidworksPreset "skills"
$UserSkillRoot = Join-Path $DshHome "skills"
$UserSkillLink = Join-Path $UserSkillRoot "solidworks-cad"

function Assert-UnderDshHome([string]$Candidate) {
    $Root = $DshHome.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    $Resolved = [System.IO.Path]::GetFullPath($Candidate)
    if (-not $Resolved.StartsWith($Root, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to modify path outside DSH home: $Resolved"
    }
    return $Resolved
}

# The only trees this installer may delete recursively, and the root no component of that path may
# leave. A junction placed on $DshHome\profiles or on a managed target itself would otherwise turn
# "Remove-Item -Recurse" into a delete that walks into somebody else's directory.
$ManagedRoots = @($SolidworksProfile, $SolidworksPreset, $SolidworksPolicy, $PresetSkillsLink,
                  $UserSkillLink)

function Get-PreviouslyOwnedLinkTargets {
    # Targets this installer recorded last time, so a link it created is recognised as ours even
    # when the repository moved between installs.
    if (-not (Test-Path -LiteralPath $InstallManifest)) {
        return @()
    }
    try {
        $Previous = Get-Content -Raw -LiteralPath $InstallManifest | ConvertFrom-Json
    } catch {
        return @()
    }
    $Found = @()
    if ($Previous.links) {
        foreach ($Property in $Previous.links.PSObject.Properties) {
            if ($Property.Value.target) { $Found += [string]$Property.Value.target }
        }
    }
    return $Found
}

$WebPatch = Assert-UnderDshHome $WebPatch
$SolidworksProfile = Assert-UnderDshHome $SolidworksProfile
$SolidworksPreset = Assert-UnderDshHome $SolidworksPreset
$SolidworksPolicy = Assert-UnderDshHome $SolidworksPolicy

# Every managed path is derived from $DshHome, so if IT is a junction the whole tree of "safe"
# deletes below is pointed at somebody else's directory. Refuse before reading or writing anything.
Assert-DshHomeNotReparse $DshHome | Out-Null

# Links this installer is allowed to replace: its two canonical targets, plus anything a previous
# install of this script recorded as its own.
$OwnedLinkTargets = @($RepoPresetSkills, $RepoSkillDir) + (Get-PreviouslyOwnedLinkTargets)

# Ownership BEFORE teardown: the preset's own `skills` child lives inside the directory this script
# is about to delete, and Remove-DshManagedTree would unlink it on the way through — so a foreign
# junction there would silently become a repository link and the -TakeOverSkillLinks consent gate
# would never be asked. Check it (and the user-root link) while both are still untouched.
$Preflight = @(
    @{ Path = $PresetSkillsLink; Target = $RepoPresetSkills; Label = "preset skills" },
    @{ Path = $UserSkillLink;    Target = $RepoSkillDir;     Label = "user skill" }
)
foreach ($Entry in $Preflight) {
    $Ownership = Get-DshLinkOwnership -LinkPath $Entry.Path -TargetPath $Entry.Target `
        -OwnedTargets $OwnedLinkTargets
    if ($Ownership -eq "foreign" -and -not $TakeOverSkillLinks) {
        $ForeignTarget = Get-DshLinkTarget $Entry.Path
        throw ("'$($Entry.Path)' ($($Entry.Label)) is a link to '$ForeignTarget', which this " +
               "installer does not own. Nothing has been changed. Re-run with -TakeOverSkillLinks " +
               "to replace that routing, or point the link at its own home.")
    }
}

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

$ProfilePatchScript = Join-Path $PSScriptRoot "SolidWorksProfilePatch.mjs"

$Manifest = [ordered]@{
    createdAtUtc = (Get-Date).ToUniversalTime().ToString("o")
    dshHome = $DshHome
    projectRoot = $ProjectRoot
    webPatchExisted = Test-Path -LiteralPath $WebPatch
    tauriPatchExisted = Test-Path -LiteralPath $TauriPatch
    solidworksProfileExisted = Test-Path -LiteralPath $SolidworksProfile
    solidworksPresetExisted = Test-Path -LiteralPath $SolidworksPreset
    solidworksPolicyExisted = Test-Path -LiteralPath $SolidworksPolicy
    # The TARGET each managed link pointed at before this run — a Boolean was not enough, because a
    # restore has to put back the link the user had, not the one the repository wants.
    presetSkillsLink = Get-DshLinkTarget $PresetSkillsLink
    userSkillLink = Get-DshLinkTarget $UserSkillLink
    # 'linked' when the existing preset already kept its skills in a junction, 'copy' when it held a
    # real skills tree. Restore reproduces whichever it was.
    presetLayout = $(if (Get-DshLinkTarget $PresetSkillsLink) { "linked" } else { "copy" })
    installManifestExisted = Test-Path -LiteralPath $InstallManifest
}

if ($Manifest.tauriPatchExisted) {
    Copy-Item -LiteralPath $TauriPatch -Destination (Join-Path $BackupRoot "tauri.cordis.patch.yml")
}
if ($Manifest.webPatchExisted) {
    Copy-Item -LiteralPath $WebPatch -Destination (Join-Path $BackupRoot "web.cordis.patch.yml")
}
if ($Manifest.solidworksProfileExisted) {
    # No copy follows a link: the profile directory holds node_modules, and a junction inside it
    # would otherwise pull an unrelated tree into the backup (or be written through).
    $ProfileLinks = Copy-DshTreeNoReparse -Source $SolidworksProfile `
        -Destination (Join-Path $BackupRoot "solidworks-profile")
    $Manifest.profileBackupLinks = @($ProfileLinks)
}
if ($Manifest.solidworksPresetExisted) {
    # EVERYTHING real in the preset directory is backed up first — composition files, any user-added
    # file, and a real skills tree. Only the skills JUNCTION is skipped, because its content is the
    # repository and its target is recorded in the manifest above. Any OTHER nested reparse point is
    # skipped too, which makes the backup incomplete, so it is reported as unexpected content.
    $PresetBackup = Copy-DshPresetBackup -PresetDir $SolidworksPreset `
        -BackupDir (Join-Path $BackupRoot "solidworks-cad-preset") `
        -SkillsLinkPath $PresetSkillsLink
    $Manifest.presetBackupCopied = @($PresetBackup.Copied)
    $Manifest.presetBackupSkippedLinks = @($PresetBackup.Links)
    $SkillsIsRealDirectory = (Test-Path -LiteralPath $PresetSkillsLink) -and
        -not (Get-DshLinkTarget $PresetSkillsLink)
    $Unexpected = @($PresetBackup.Extra)
    if ($SkillsIsRealDirectory) { $Unexpected += "skills (a real directory, not the managed link)" }
    if (@($PresetBackup.Links).Count -gt 0) {
        $Unexpected += "nested reparse point(s): $(@($PresetBackup.Links) -join ', ')"
    }
    if ($Unexpected.Count -gt 0 -and -not $MigrateCustomPreset) {
        throw ("The existing preset '$SolidworksPreset' holds content this integration does not " +
               "own: $($Unexpected -join ', '). It is backed up under '$BackupRoot', and nothing " +
               "was removed. Re-run with -MigrateCustomPreset to replace it anyway.")
    }
}
if ($Manifest.solidworksPolicyExisted) {
    $PolicyLinks = Copy-DshTreeNoReparse -Source $SolidworksPolicy `
        -Destination (Join-Path $BackupRoot "solidworks-policy")
    $Manifest.policyBackupLinks = @($PolicyLinks)
}
if (Test-Path -LiteralPath $TauriPolicy) {
    if (Test-DshReparsePoint $TauriPolicy) {
        throw "'$TauriPolicy' is a link; refusing to write the desktop policy through it."
    }
    Copy-DshTreeNoReparse -Source $TauriPolicy -Destination (Join-Path $BackupRoot "tauri-solidworks-policy") | Out-Null
}
if ($Manifest.installManifestExisted) {
    Copy-Item -LiteralPath $InstallManifest -Destination (Join-Path $BackupRoot "solidworks-dsh-install.json")
}
$Manifest | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $BackupRoot "manifest.json") -Encoding utf8

# Parse and merge both user-owned patches BEFORE removing any installed tree.
# Only managed fields change; model/provider overrides and other MCPs survive.
$PreparedWebPatch = Join-Path $BackupRoot "web.cordis.prepared.yml"
$PreparedProfilePatch = Join-Path $BackupRoot "solidworks.cordis.prepared.yml"
foreach ($Patch in @(
    @{ Template = "profiles/web/cordis.patch.yml"; Source = $WebPatch; Output = $PreparedWebPatch },
    @{ Template = "profiles/solidworks/cordis.patch.yml"; Source = (Join-Path $SolidworksProfile "cordis.patch.yml"); Output = $PreparedProfilePatch }
)) {
    & node $ProfilePatchScript --mode merge --dsh-root $DshRoot `
        --template (Join-Path $IntegrationRoot $Patch.Template) --source $Patch.Source `
        --destination $Patch.Output --python $Python --server $Server --adapter-dir $AdapterDir
    if ($LASTEXITCODE -ne 0) {
        throw "Profile patch preparation failed. Installed trees have not been removed; backup: $BackupRoot"
    }
}
$PreparedTauriPatch = Join-Path $BackupRoot "tauri.cordis.prepared.yml"
if ($Manifest.tauriPatchExisted) {
    & node $ProfilePatchScript --mode sync-tauri --dsh-root $DshRoot --source $TauriPatch `
        --preset (Join-Path $RepoPresetSource "agent.cordis.yml") --destination $PreparedTauriPatch
    if ($LASTEXITCODE -ne 0) { throw "Tauri persona/vision preparation failed; installed files are unchanged." }
}
$WebPatchState = "merged"
$ProfilePatchState = "merged"

# Teardown checks managed roots and rejects reparse ancestors. Nested links are refused except
# verified pnpm links into this profile's own store and skill links checked in the preflight.
# Only the links are removed; their targets remain intact.
$ProfileManagedLinks = if (Test-Path -LiteralPath $SolidworksProfile) {
    $ProfileModulesRoot = (Get-DshCanonicalPath (Join-Path $SolidworksProfile "node_modules")).TrimEnd('\') + '\'
    $ProfilePnpmStoreRoot = (Get-DshCanonicalPath (Join-Path $SolidworksProfile "node_modules\.pnpm")).TrimEnd('\') + '\'
    $Links = @()
    foreach ($Link in (Get-DshNestedReparsePoints -Dir $SolidworksProfile)) {
        $LinkPath = Get-DshCanonicalPath $Link
        $LinkTarget = Get-DshLinkTarget $Link
        if (-not $LinkPath.StartsWith($ProfileModulesRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
            -not $LinkTarget -or
            -not $LinkTarget.StartsWith($ProfilePnpmStoreRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw ("Refusing to replace the SolidWorks profile: it contains an unmanaged link " +
                   "'$LinkPath'. Only package links under node_modules that point into its .pnpm " +
                   "store can be safely unlinked during reinstall.")
        }
        $Links += $LinkPath
    }
    $Links
} else {
    @()
}
foreach ($Target in @($SolidworksProfile, $SolidworksPreset, $SolidworksPolicy)) {
    Assert-UnderDshHome $Target | Out-Null
    $ManagedLinksForTarget = if ($Target -eq $SolidworksProfile) {
        $ProfileManagedLinks
    } elseif ($Target -eq $SolidworksPreset) {
        @($PresetSkillsLink)
    } else {
        @()
    }
    Remove-DshManagedTree -Path $Target -AllowedRoots $ManagedRoots -GuardRoot $DshHome `
        -Purpose "reinstall" -ManagedLinks @($ManagedLinksForTarget) | Out-Null
}
New-Item -ItemType Directory -Force -Path `
    (Split-Path $WebPatch), `
    $SolidworksProfile, `
    $SolidworksPreset, `
    $SolidworksPolicy | Out-Null
Copy-Item -LiteralPath $PreparedWebPatch -Destination $WebPatch -Force
# Neither install copy follows a link either: the source is the repository, and a junction placed in
# it (by an editor, a sync tool, a previous experiment) must not be resolved into the DSH tree.
Copy-DshTreeNoReparse -Source (Join-Path $IntegrationRoot "profiles\solidworks") `
    -Destination $SolidworksProfile | Out-Null
# The preset is a real directory (DSH lists a root's children with readdir(withFileTypes) and skips
# anything whose dirent is not a directory, so the PRESET itself cannot be a link) holding only the
# composition files; the skill content is linked, so no SKILL.md copy can go stale.
Copy-Item -Path (Join-Path $RepoPresetSource "agent.cordis.yml") -Destination $SolidworksPreset -Force
Copy-Item -Path (Join-Path $RepoPresetSource "preset.yml") -Destination $SolidworksPreset -Force
$PresetLinkResult = New-DshOwnedLink -LinkPath $PresetSkillsLink -TargetPath $RepoPresetSkills `
    -OwnedTargets $OwnedLinkTargets -BackupRoot $BackupRoot -BackupName "preset-skills" `
    -TakeOver:$TakeOverSkillLinks
if ($PresetLinkResult.State -eq "foreign") {
    throw ("'$PresetSkillsLink' is a link to '$($PresetLinkResult.OldTarget)', which this installer " +
           "does not own. Refusing to replace it; pass -TakeOverSkillLinks if that routing is " +
           "meant to become the repository's.")
}
$UserSkillResult = New-DshOwnedLink -LinkPath $UserSkillLink -TargetPath $RepoSkillDir `
    -OwnedTargets $OwnedLinkTargets -BackupRoot $BackupRoot -BackupName "user-skill" `
    -TakeOver:$TakeOverSkillLinks
if ($UserSkillResult.State -eq "foreign") {
    throw ("'$UserSkillLink' is a link to '$($UserSkillResult.OldTarget)', which this installer does " +
           "not own. Refusing to replace it; pass -TakeOverSkillLinks to take it over.")
}
Copy-DshTreeNoReparse -Source (Join-Path $IntegrationRoot "plugins\solidworks-policy") `
    -Destination $SolidworksPolicy | Out-Null
if (Test-Path -LiteralPath (Join-Path $ProfilesRoot "tauri")) {
    New-Item -ItemType Directory -Force -Path $TauriPolicy | Out-Null
    foreach ($File in $PolicyRuntimeFiles) {
        Copy-Item -LiteralPath (Join-Path $IntegrationRoot "plugins\solidworks-policy\$File") `
            -Destination (Join-Path $TauriPolicy $File) -Force
    }
}
if ($Manifest.tauriPatchExisted) {
    Copy-Item -LiteralPath $PreparedTauriPatch -Destination $TauriPatch -Force
}
Copy-Item -LiteralPath $PreparedProfilePatch -Destination (Join-Path $SolidworksProfile "cordis.patch.yml") -Force

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
    links = [ordered]@{
        presetSkills = [ordered]@{
            path = $PresetSkillsLink
            target = (Get-DshLinkTarget $PresetSkillsLink)
            state = $PresetLinkResult.State
            previousTarget = $PresetLinkResult.OldTarget
        }
        userSkill = [ordered]@{
            path = $UserSkillLink
            target = (Get-DshLinkTarget $UserSkillLink)
            state = $UserSkillResult.State
            previousTarget = $UserSkillResult.OldTarget
        }
    }
    webPatchState = $WebPatchState
    profilePatchState = $ProfilePatchState
}
$Installed | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $InstallManifest -Encoding utf8

Write-Host "Installed SolidWorks DSH profile and preset."
Write-Host "Backup: $BackupRoot"
Write-Host "Manifest: $InstallManifest"
Write-Host "Skill links: preset=$($PresetLinkResult.State) user=$($UserSkillResult.State) (both point at the repo, no copies)"
Write-Host "Web patch: $WebPatchState ($WebPatch)"
Write-Host "Start web: $DshRoot\node_modules\.bin\dsh.CMD web"
Write-Host "Start dedicated profile: $DshRoot\node_modules\.bin\dsh.CMD --profile solidworks"
Write-Host "In the web UI: pick the 'SolidWorks CAD' preset chip on the new-session screen; a config"
Write-Host "change here only takes effect when the DSH process is next started."
