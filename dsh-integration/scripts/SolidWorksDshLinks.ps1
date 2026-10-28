# Shared DSH link / backup / teardown helpers for the SolidWorks integration scripts.
#
#   . (Join-Path $PSScriptRoot "SolidWorksDshLinks.ps1")
#
# These used to be copied into Install- and Restore-SolidWorksDsh.ps1 separately, and the copies
# drifted: Install wrote `userSkillWasLink` while Restore read `userSkillLink`, so a restore of a
# normal install backup deleted the user-root skill link instead of putting it back. One file, one
# set of names, and the link's TARGET is recorded (not a Boolean) so a restore reproduces what was
# actually there.
#
# Everything here is defensive about the same two things:
#   * a reparse point (junction/symlink) in a path we are about to delete recursively — recursing
#     through one can follow it into a directory nobody intended to touch, possibly the repository;
#   * a path outside the managed DSH-owned roots — a script must never delete "whatever happens to
#     be at that name".

function Get-DshCanonicalPath {
    param([Parameter(Mandatory)][string]$Path)
    return [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
}

function Get-DshLinkTarget {
    # The target of a link, or $null when the path is not a link (a real directory, a file, or
    # absent). Get-Item on a reparse point reports LinkType; a plain directory does not.
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return $null
    }
    $Item = Get-Item -LiteralPath $Path -Force
    if (-not $Item.LinkType) {
        return $null
    }
    $Raw = $Item.Target
    if ($Raw -is [array]) {
        $Raw = $Raw[0]
    }
    if (-not $Raw) {
        return $null
    }
    return [System.IO.Path]::GetFullPath($Raw).TrimEnd('\', '/')
}

function Test-DshReparsePoint {
    # Attributes, not LinkType: this must also answer "yes" for a reparse point PowerShell would
    # happily walk into.
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return $false
    }
    $Attributes = [System.IO.File]::GetAttributes($Path)
    return ($Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
}

function Get-DshReparseAncestor {
    # The first component between Root (exclusive) and Path (inclusive) that is a reparse point,
    # or $null. A junction placed on an ANCESTOR ($DshHome\profiles -> somebody's repo) turns every
    # "safe" recursive delete below it into a delete through that link.
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$Path
    )
    $RootFull = Get-DshCanonicalPath $Root
    $PathFull = Get-DshCanonicalPath $Path
    if (-not $PathFull.StartsWith($RootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        return "(outside root)"
    }
    $Relative = $PathFull.Substring($RootFull.Length).TrimStart('\', '/')
    if ($Relative -eq "") {
        return $null
    }
    $Cursor = $RootFull
    foreach ($Segment in ($Relative -split '[\\/]')) {
        if (-not $Segment) { continue }
        $Cursor = Join-Path $Cursor $Segment
        if (Test-DshReparsePoint $Cursor) {
            return $Cursor
        }
    }
    return $null
}

function Assert-DshHomeNotReparse {
    # The whole safety story below assumes $DshHome is a real directory: every managed path is
    # built from it, so if IT is a junction, "delete the preset directory" means "delete whatever
    # that junction points at". Refuse before anything is read or written.
    param([Parameter(Mandatory)][string]$DshHome)
    $Resolved = Get-DshCanonicalPath $DshHome
    if (Test-DshReparsePoint $Resolved) {
        throw "'$Resolved' is itself a reparse point (junction or symlink). Refusing to operate: " +
              "every managed path is derived from it, so a recursive delete or a backup copy would " +
              "follow it into a directory this tool does not own."
    }
    return $Resolved
}

function Get-DshChildItemsOrThrow {
    # Enumerate a directory, distinguishing ABSENT from UNREADABLE. Every caller here uses the
    # result to decide whether a tree is safe to delete or has been backed up completely, and a
    # non-terminating enumeration error would otherwise look like an empty directory — the same
    # fail-open mistake as `Test-Path` on an access-denied path. Absent yields nothing; anything
    # else that goes wrong raises.
    param([Parameter(Mandatory)][string]$Path)
    try {
        return @(Get-ChildItem -LiteralPath $Path -Force -ErrorAction Stop)
    } catch [System.Management.Automation.ItemNotFoundException] {
        return @()
    } catch [System.IO.DirectoryNotFoundException] {
        return @()
    } catch {
        throw "cannot enumerate '$Path': $($_.Exception.Message). Refusing to treat a directory " +
              "that could not be read as empty — the caller would then delete or back up a tree it " +
              "has not fully inspected."
    }
}

function Assert-DshSourceNotReparse {
    # A copy/backup source ROOT is checked like any other link: when the root itself is a junction,
    # enumerating it reads the target's content, so a backup would pull an unrelated tree in and a
    # delete that follows would reach outside the managed roots. The nested-link scans below only
    # look at CHILDREN, so this has to be said about the root explicitly.
    param(
        [Parameter(Mandatory)][string]$Path,
        [string]$Purpose = "copy"
    )
    if (Test-DshReparsePoint $Path) {
        throw ("refusing to $Purpose from '$(Get-DshCanonicalPath $Path)', which is itself a " +
               "reparse point (junction or symlink). Its content lives elsewhere; unlink it (or " +
               "point it at its real home) and re-run.")
    }
}

function Get-DshNestedReparsePoints {
    # Every reparse point under $Dir (self excluded), so a nested junction can be skipped when
    # copying and refused when deleting. DirectoryInfo.EnumerateFiles/EnumerateDirectories report a
    # junction as a directory, hence the explicit attribute test.
    param(
        [Parameter(Mandatory)][string]$Dir,
        [string[]]$Allow = @()
    )
    $Found = @()
    $Allowed = @($Allow | ForEach-Object { if ($_) { Get-DshCanonicalPath $_ } })
    $Stack = New-Object System.Collections.Generic.Stack[string]
    $Stack.Push((Get-DshCanonicalPath $Dir))
    while ($Stack.Count -gt 0) {
        $Current = $Stack.Pop()
        foreach ($Child in (Get-DshChildItemsOrThrow -Path $Current)) {
            $Full = Get-DshCanonicalPath $Child.FullName
            if ($Allowed -contains $Full) { continue }
            if (Test-DshReparsePoint $Full) {
                $Found += $Full
                continue                      # never walk THROUGH a link
            }
            if ($Child.PSIsContainer) { $Stack.Push($Full) }
        }
    }
    return $Found
}

function Copy-DshTreeNoReparse {
    # A recursive copy that never follows a link: reparse points are recorded and skipped, so a
    # backup can neither duplicate an external tree nor be talked into writing through one.
    param(
        [Parameter(Mandatory)][string]$Source,
        [Parameter(Mandatory)][string]$Destination
    )
    Assert-DshSourceNotReparse -Path $Source -Purpose "copy"
    $Skipped = @()
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    foreach ($Child in (Get-DshChildItemsOrThrow -Path $Source)) {
        $Target = Join-Path $Destination $Child.Name
        if (Test-DshReparsePoint $Child.FullName) {
            $Skipped += (Get-DshCanonicalPath $Child.FullName)
            continue
        }
        if ($Child.PSIsContainer) {
            $Nested = Copy-DshTreeNoReparse -Source $Child.FullName -Destination $Target
            $Skipped += $Nested
        } else {
            Copy-Item -LiteralPath $Child.FullName -Destination $Target -Force
        }
    }
    return $Skipped
}

function Remove-DshTreeGuarded {
    # Recursive delete that refuses rather than walking through a nested junction. Links the caller
    # declares as managed are unlinked first (their own targets are never deleted).
    param(
        [Parameter(Mandatory)][string]$Path,
        [string[]]$ManagedLinks = @()
    )
    foreach ($Link in $ManagedLinks) {
        if ($Link -and (Test-DshReparsePoint $Link)) {
            Remove-DshLink -Path $Link | Out-Null
        }
    }
    $Nested = @(Get-DshNestedReparsePoints -Dir $Path)
    if ($Nested.Count -gt 0) {
        throw ("Refusing to delete '$Path' recursively: it still contains reparse point(s) " +
               "$($Nested -join ', ') which this tool does not manage. Remove them (or point them " +
               "at their real home) and re-run.")
    }
    Remove-Item -LiteralPath $Path -Recurse -Force
}

function Get-DshLinkOwnership {
    # 'absent' | 'right' | 'ours' | 'foreign' | 'copy' for a managed link path.
    param(
        [Parameter(Mandatory)][string]$LinkPath,
        [Parameter(Mandatory)][string]$TargetPath,
        [Parameter(Mandatory)][string[]]$OwnedTargets
    )
    $Target = Get-DshCanonicalPath $TargetPath
    $Current = Get-DshLinkTarget $LinkPath
    if ($Current) {
        if ($Current -eq $Target) { return "right" }
        foreach ($Candidate in $OwnedTargets) {
            if ($Candidate -and (Get-DshCanonicalPath $Candidate) -eq $Current) { return "ours" }
        }
        return "foreign"
    }
    if (Test-Path -LiteralPath $LinkPath) { return "copy" }
    return "absent"
}

function Assert-DshManagedPath {
    # $Path must be one of, or a direct child of, the allowed managed roots. Returns the canonical
    # path; throws otherwise.
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string[]]$AllowedRoots,
        [string]$Purpose = "modify"
    )
    $Resolved = Get-DshCanonicalPath $Path
    foreach ($Allowed in $AllowedRoots) {
        $Root = Get-DshCanonicalPath $Allowed
        if ($Resolved -eq $Root -or
            $Resolved.StartsWith($Root + [System.IO.Path]::DirectorySeparatorChar,
                                 [System.StringComparison]::OrdinalIgnoreCase)) {
            return $Resolved
        }
    }
    throw "Refusing to $Purpose '$Resolved': it is not one of the managed paths ($($AllowedRoots -join ', '))."
}

function Remove-DshLink {
    # Deletes the reparse point ALONE. Never with recursion: following it into the target is exactly
    # the accident this whole file exists to prevent.
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-DshReparsePoint $Path)) {
        return $false
    }
    [System.IO.Directory]::Delete($Path) | Out-Null
    return $true
}

function Remove-DshManagedTree {
    # Recursive delete with the two guards applied first: the path must be a managed one, and no
    # component of it may be a reparse point. A reparse point AT the path itself is unlinked instead
    # of deleted (its target is not ours to remove).
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string[]]$AllowedRoots,
        [Parameter(Mandatory)][string]$GuardRoot,
        [string]$Purpose = "remove",
        # Links this tool owns that live INSIDE the tree being removed: they are unlinked (their
        # targets untouched) before the rest is deleted. Anything else nested inside is a refusal.
        [string[]]$ManagedLinks = @()
    )
    $Resolved = Assert-DshManagedPath -Path $Path -AllowedRoots $AllowedRoots -Purpose $Purpose
    if (-not (Test-Path -LiteralPath $Resolved)) {
        return $false
    }
    if (Test-DshReparsePoint $Resolved) {
        Remove-DshLink -Path $Resolved | Out-Null
        return $true
    }
    $Offender = Get-DshReparseAncestor -Root $GuardRoot -Path $Resolved
    if ($Offender) {
        throw "Refusing to remove '$Resolved' recursively: '$Offender' on that path is a reparse " +
              "point (junction or symlink), so the delete would follow it out of the DSH-owned tree."
    }
    # The ancestors are clean; anything NESTED inside the tree is the remaining way out.
    Remove-DshTreeGuarded -Path $Resolved -ManagedLinks $ManagedLinks
    return $true
}

function Copy-DshPresetBackup {
    # Backs up the WHOLE existing preset directory — every real file and directory it holds,
    # including anything a user added — and skips only the skills entry when that is a link this
    # integration owns (its content is the repository, and copying it would duplicate the repo into
    # the backup tree).
    param(
        [Parameter(Mandatory)][string]$PresetDir,
        [Parameter(Mandatory)][string]$BackupDir,
        [Parameter(Mandatory)][string]$SkillsLinkPath,
        [string[]]$CanonicalEntries = @("agent.cordis.yml", "preset.yml", "skills")
    )
    $Skipped = @()
    $Copied = @()
    $Links = @()
    # The preset root is a copy source like any other: if IT is a junction, the backup would copy
    # whatever it points at, and the teardown that follows would then be justified by a directory
    # this tool never looked inside.
    Assert-DshSourceNotReparse -Path $PresetDir -Purpose "back up"
    if (-not (Test-Path -LiteralPath $PresetDir)) {
        return [pscustomobject]@{ Copied = $Copied; Skipped = $Skipped; Extra = @(); Links = @() }
    }
    New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
    foreach ($Entry in (Get-DshChildItemsOrThrow -Path $PresetDir)) {
        $IsSkillsEntry = (Get-DshCanonicalPath $Entry.FullName) -eq (Get-DshCanonicalPath $SkillsLinkPath)
        if (Test-DshReparsePoint $Entry.FullName) {
            # A link at the top of the preset is NEVER copied — its content lives elsewhere. The
            # managed skills link is expected and only recorded; any other link is a surprise, so it
            # goes into Links, which the caller treats as non-canonical content to refuse on.
            $Target = Get-DshLinkTarget $Entry.FullName
            if ($IsSkillsEntry) { $Skipped += $Target } else { $Links += (Get-DshCanonicalPath $Entry.FullName) }
            continue
        }
        $Destination = Join-Path $BackupDir $Entry.Name
        if ($Entry.PSIsContainer) {
            # A real directory may still contain junctions of its own. Copy it WITHOUT following
            # them: a backup must not pull an external tree in, and must not write through a link.
            $Nested = Copy-DshTreeNoReparse -Source $Entry.FullName -Destination $Destination
            $Links += $Nested
        } else {
            Copy-Item -LiteralPath $Entry.FullName -Destination $Destination -Force
        }
        $Copied += $Entry.Name
    }
    $Extra = @($Copied | Where-Object { $CanonicalEntries -notcontains $_ })
    return [pscustomobject]@{ Copied = $Copied; Skipped = $Skipped; Extra = $Extra; Links = @($Links) }
}

function Restore-DshPresetFromBackup {
    # Rebuild one preset directory from a backup, in whichever layout that backup was taken in, and
    # put the skills junction back where the manifest says it was.
    #
    #   tree layout      — `solidworks-cad-preset\` holds everything that was really there
    #                      (composition files, a copied skills tree, any user-added file)
    #   files layout     — `solidworks-cad-agent.cordis.yml` + `solidworks-cad-preset.yml` only
    #   linked layout    — the same files, plus manifest.presetLayout = 'linked' and the recorded
    #                      skills target, which is restored as a junction to THAT target
    param(
        [Parameter(Mandatory)][string]$BackupDir,
        [Parameter(Mandatory)][string]$PresetDir,
        [Parameter(Mandatory)][string]$SkillsLinkPath,
        [Parameter(Mandatory)]$Manifest,
        [string]$FallbackSkillsTarget = ""
    )
    $Result = [ordered]@{ layout = ""; restored = @(); skills = "none" }
    New-Item -ItemType Directory -Force -Path $PresetDir | Out-Null
    $Tree = Join-Path $BackupDir "solidworks-cad-preset"
    $Names = @($Manifest.PSObject.Properties.Name)
    $Layout = if ($Names -contains "presetLayout") { [string]$Manifest.presetLayout } else { "" }

    if (Test-Path -LiteralPath $Tree) {
        # A tree backup can contain a REAL skills directory. If a junction is still sitting at that
        # path, Copy-Item would write through it into the target tree — so the link goes first.
        Remove-DshLink -Path $SkillsLinkPath | Out-Null
        Assert-DshSourceNotReparse -Path $Tree -Purpose "restore"
        foreach ($Entry in (Get-DshChildItemsOrThrow -Path $Tree)) {
            $Destination = Join-Path $PresetDir $Entry.Name
            Remove-DshLink -Path $Destination | Out-Null
            if ($Entry.PSIsContainer) {
                # Never follow a link in either direction, in either direction.
                Copy-DshTreeNoReparse -Source $Entry.FullName -Destination $Destination | Out-Null
            } else {
                Copy-Item -LiteralPath $Entry.FullName -Destination $Destination -Force
            }
            $Result.restored += $Entry.Name
        }
        $Result.layout = "tree"
    } else {
        foreach ($File in @("agent.cordis.yml", "preset.yml")) {
            $Source = Join-Path $BackupDir "solidworks-cad-$File"
            if (Test-Path -LiteralPath $Source -PathType Leaf) {
                Copy-Item -LiteralPath $Source -Destination (Join-Path $PresetDir $File) -Force
                $Result.restored += $File
            }
        }
        $Result.layout = "files"
    }

    if ($Layout -eq "linked") {
        $Result.layout = "linked"
        $Target = $null
        if ($Names -contains "presetSkillsLink" -and $Manifest.presetSkillsLink) {
            $Target = [string]$Manifest.presetSkillsLink
        }
        if (-not $Target -and $FallbackSkillsTarget) { $Target = $FallbackSkillsTarget }
        if ($Target -and (Test-Path -LiteralPath $Target)) {
            Remove-DshLink -Path $SkillsLinkPath | Out-Null
            if (-not (Test-Path -LiteralPath $SkillsLinkPath)) {
                New-Item -ItemType Junction -Path $SkillsLinkPath -Target $Target | Out-Null
                $Result.skills = "linked -> $Target"
            } else {
                $Result.skills = "skipped (a real directory is in the way at $SkillsLinkPath)"
            }
        } else {
            $Result.skills = "refused (no readable target to link: '$Target')"
        }
    } elseif (Test-DshReparsePoint $SkillsLinkPath) {
        # This backup predates the linked layout: the link is not part of what is being restored.
        Remove-DshLink -Path $SkillsLinkPath | Out-Null
        $Result.skills = "removed (backup predates the link)"
    }
    return [pscustomobject]$Result
}

function New-DshOwnedLink {
    # Idempotent, ownership-aware junction:
    #   right target already in place  -> leave it ("linked")
    #   a link to one of OUR targets   -> retarget it, reporting the old target ("retargeted")
    #   a link to anything else        -> REFUSE unless -TakeOver is given ("foreign")
    #   a real directory               -> copy it into the backup, then replace ("replaced-copy")
    #   absent                         -> create ("created")
    # The caller records the returned OldTarget so a restore can put the foreign link back.
    param(
        [Parameter(Mandatory)][string]$LinkPath,
        [Parameter(Mandatory)][string]$TargetPath,
        [Parameter(Mandatory)][string[]]$OwnedTargets,
        [string]$BackupRoot = "",
        [string]$BackupName = "link",
        [switch]$TakeOver
    )
    $Target = Get-DshCanonicalPath $TargetPath
    if (-not (Test-Path -LiteralPath $Target)) {
        throw "link target is missing: $Target"
    }
    # One classifier for the pre-flight check and for here, so the two can never disagree about
    # what "ours" means.
    $Ownership = Get-DshLinkOwnership -LinkPath $LinkPath -TargetPath $Target `
        -OwnedTargets $OwnedTargets
    $OldTarget = Get-DshLinkTarget $LinkPath
    $State = "created"
    if ($Ownership -eq "right") {
        return [pscustomobject]@{ State = "linked"; OldTarget = $OldTarget }
    }
    if ($Ownership -eq "foreign") {
        if (-not $TakeOver) {
            return [pscustomobject]@{ State = "foreign"; OldTarget = $OldTarget }
        }
        Remove-DshLink -Path $LinkPath | Out-Null
        $State = "retargeted"
    } elseif ($Ownership -eq "ours") {
        Remove-DshLink -Path $LinkPath | Out-Null
        $State = "retargeted"
    } elseif ($Ownership -eq "copy") {
        # A copied tree: preserved in the backup before it is replaced by the link. Neither the copy
        # nor the delete may follow a junction nested inside it.
        if (-not $BackupRoot) {
            throw "refusing to replace the real directory '$LinkPath' without a backup root"
        }
        New-Item -ItemType Directory -Force -Path $BackupRoot | Out-Null
        $Nested = Copy-DshTreeNoReparse -Source $LinkPath `
            -Destination (Join-Path $BackupRoot "stale-$BackupName")
        Remove-DshTreeGuarded -Path $LinkPath
        $State = "replaced-copy"
        if (@($Nested).Count -gt 0) {
            Write-Warning ("nested reparse point(s) skipped in the backup of '$LinkPath': " +
                           (@($Nested) -join ', '))
        }
    }
    New-Item -ItemType Junction -Path $LinkPath -Target $Target | Out-Null
    return [pscustomobject]@{ State = $State; OldTarget = $OldTarget }
}
