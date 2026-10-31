"""Contract for the DSH integration scripts: one link implementation, recorded targets, no
unguarded recursive deletes.

The DRAWING-INTAKE review found three defects that all came from the same root — Install and Restore
each carried their own copy of the link helpers:

1. Install wrote `userSkillWasLink` (a Boolean) while Restore read `userSkillLink`, so restoring a
   normal install backup DELETED the user-root skill link instead of putting it back, and a Boolean
   could not say what to relink to anyway.
2. A pre-existing junction the installer did not create was replaced without recording its old
   target, so the user's routing was unrecoverable.
3. The whole existing preset directory was removed after backing up only two YAML files, so any
   file the user had added there was gone with no copy and no refusal.

They are pinned here the way the other contract tests pin the C#: against the shipped source. The
behavioural proof is the scratch run recorded in result-review-fixes.md (junctions, foreign links
and reparse ancestors exercised under a temporary DSH_HOME); it is not wired into pytest because
creating reparse points on every suite run would make the suite depend on the machine.
"""
import os
import re
import shutil
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS = os.path.join(REPO_ROOT, "dsh-integration", "scripts")
MODULE = os.path.join(SCRIPTS, "SolidWorksDshLinks.ps1")
INSTALL = os.path.join(SCRIPTS, "Install-SolidWorksDsh.ps1")
RESTORE = os.path.join(SCRIPTS, "Restore-SolidWorksDsh.ps1")
DOCTOR = os.path.join(SCRIPTS, "Doctor-SolidWorksDsh.ps1")

# Fields the backup manifest must agree on, or a restore silently does the wrong thing.
LINK_FIELDS = ("presetSkillsLink", "userSkillLink", "presetLayout")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _code_lines(source):
    # Comments explain the danger; only the code may not perform it.
    return "\n".join(line for line in source.splitlines() if not line.strip().startswith("#"))


def test_all_three_scripts_share_one_link_implementation():
    module = _read(MODULE)
    for name, path in (("Install", INSTALL), ("Restore", RESTORE), ("Doctor", DOCTOR)):
        source = _read(path)
        assert '. (Join-Path $PSScriptRoot "SolidWorksDshLinks.ps1")' in source, (
            f"{name} must dot-source the shared helpers, not carry its own copy")
    for helper in ("Get-DshLinkTarget", "Remove-DshLink", "Remove-DshManagedTree",
                   "New-DshOwnedLink", "Copy-DshPresetBackup", "Restore-DshPresetFromBackup",
                   "Get-DshReparseAncestor", "Assert-DshManagedPath",
                   # gate round: the home itself, nested links, and one ownership classifier
                   "Assert-DshHomeNotReparse", "Get-DshNestedReparsePoints",
                   "Copy-DshTreeNoReparse", "Remove-DshTreeGuarded", "Get-DshLinkOwnership"):
        assert f"function {helper}" in module, f"{helper} missing from the shared module"
    # the drifted copies that caused the bug must not come back
    for stale in ("Get-JunctionTarget", "Get-LinkTargetOf", "Remove-LinkIfExists",
                  "New-SolidWorksLink", "Repair-PresetSkillsLink"):
        assert stale not in _read(INSTALL) + _read(RESTORE) + _read(DOCTOR), stale


def test_manifest_records_link_targets_with_the_names_restore_reads():
    install = _read(INSTALL)
    restore = _read(RESTORE)
    for field in LINK_FIELDS:
        assert re.search(rf"\b{field} = ", install), f"Install never writes {field}"
        assert field in restore, f"Restore never reads {field}"
    # a TARGET, not a Boolean: restoring a link needs to know what it pointed at
    assert "presetSkillsLink = Get-DshLinkTarget $PresetSkillsLink" in install
    assert "userSkillLink = Get-DshLinkTarget $UserSkillLink" in install
    assert "userSkillWasLink" not in install and "presetSkillsWasLink" not in install


def test_install_backs_up_the_whole_preset_and_refuses_foreign_content():
    install = _read(INSTALL)
    assert "Copy-DshPresetBackup -PresetDir $SolidworksPreset" in install
    assert "-SkillsLinkPath $PresetSkillsLink" in install
    assert "[switch]$MigrateCustomPreset" in install, (
        "non-canonical preset content must stop the install unless the user opts in")
    assert "does not own" in install and "backed up" in install
    # the skills tree is only skipped when it IS the managed link
    module = _read(MODULE)
    backup_fn = module[module.index("function Copy-DshPresetBackup"):
                      module.index("function Restore-DshPresetFromBackup")]
    assert "Test-DshReparsePoint" in backup_fn
    assert "Copy-Item -LiteralPath $Entry.FullName" in backup_fn


def test_a_link_this_installer_does_not_own_is_refused_not_replaced():
    install = _read(INSTALL)
    module = _read(MODULE)
    assert "[switch]$TakeOverSkillLinks" in install
    assert install.count('State -eq "foreign"') == 2, (
        "both managed links must be checked before replacement")
    link_fn = module[module.index("function New-DshOwnedLink"):]
    assert 'return [pscustomobject]@{ State = "foreign"; OldTarget = $OldTarget }' in link_fn
    assert "previousTarget = $PresetLinkResult.OldTarget" in install, (
        "the replaced target is recorded so a restore can put it back")
    assert "previousTarget = $UserSkillResult.OldTarget" in install


def test_no_recursive_delete_remains_outside_the_guarded_helper():
    # Every recursive removal goes through Remove-DshManagedTree, which asserts the managed root and
    # rejects a reparse point anywhere on the path. Copying recursively is still fine — only
    # deleting is the danger.
    module = _read(MODULE)
    for path, label in ((INSTALL, "Install"), (RESTORE, "Restore")):
        source = _code_lines(_read(path))
        assert not re.search(r"Remove-Item[^\r\n]*-Recurse", source), (
            f"{label} still contains an unguarded recursive delete")
        assert "Remove-DshManagedTree -Path" in source, f"{label} does not use the guarded delete"
    install = _code_lines(_read(INSTALL))
    assert re.search(r"foreach \(\$Target in @\(\$SolidworksProfile, \$SolidworksPreset, "
                     r"\$SolidworksPolicy\)\)", install), (
        "Install's teardown must cover all three managed trees")
    guarded = module[module.index("function Remove-DshManagedTree"):
                     module.index("function Copy-DshPresetBackup")]
    assert "Assert-DshManagedPath" in guarded
    assert "Get-DshReparseAncestor" in guarded
    assert "Remove-DshLink -Path $Resolved" in guarded
    # the actual recursion lives in one guarded helper, not at the call site
    assert "Remove-DshTreeGuarded -Path $Resolved -ManagedLinks $ManagedLinks" in guarded


def test_dsh_home_being_a_junction_is_refused_by_both_scripts():
    # Every managed path is derived from $DshHome, so a junction there turns "delete the preset" into
    # "delete whatever the user's junction points at". Both writers check it before touching disk.
    for path, label in ((INSTALL, "Install"), (RESTORE, "Restore")):
        source = _read(path)
        assert "Assert-DshHomeNotReparse $DshHome" in source, f"{label} does not check DSH_HOME"
    install = _read(INSTALL)
    assert install.index("Assert-DshHomeNotReparse") < install.index("Copy-DshPresetBackup"), (
        "the check must precede the first read/copy")


def test_no_copy_follows_a_nested_reparse_point():
    # Backups and install copies go through Copy-DshTreeNoReparse, so a junction inside a managed
    # tree can neither pull an external tree into a backup nor be written through.
    for path, label in ((INSTALL, "Install"), (RESTORE, "Restore")):
        source = _code_lines(_read(path))
        assert not re.search(r"Copy-Item[^\r\n]*-Recurse", source), (
            f"{label} still copies recursively, following any link it meets")
        assert "Copy-DshTreeNoReparse" in source, f"{label} does not use the link-safe copy"
    module = _read(MODULE)
    copier = module[module.index("function Copy-DshTreeNoReparse"):
                    module.index("function Remove-DshTreeGuarded")]
    assert "Test-DshReparsePoint" in copier
    assert not re.search(r"Copy-Item[^\r\n]*-Recurse", copier)
    backup = module[module.index("function Copy-DshPresetBackup"):
                    module.index("function Restore-DshPresetFromBackup")]
    # a top-level link is recorded and skipped, never copied; a non-skipped one is a refusal reason
    assert "Test-DshReparsePoint $Entry.FullName" in backup
    assert "$Links += (Get-DshCanonicalPath $Entry.FullName)" in backup


def test_a_copy_source_root_that_is_itself_a_link_is_refused():
    # Re-check 3: the nested scans only ever look at CHILDREN, so a junction sitting ON the source
    # root (a managed profile/policy/preset directory that someone redirected) would be enumerated
    # through — copying the target's content into a backup, and justifying a recursive delete of a
    # tree nobody inspected. Every copy/backup/restore root states the refusal before enumerating.
    module = _read(MODULE)
    assert "function Assert-DshSourceNotReparse" in module
    for marker, label in (("function Copy-DshTreeNoReparse", "function Remove-DshTreeGuarded"),
                          ("function Copy-DshPresetBackup", "function Restore-DshPresetFromBackup"),
                          ("function Restore-DshPresetFromBackup", "function New-DshOwnedLink")):
        body = module[module.index(marker):module.index(label)]
        assert "Assert-DshSourceNotReparse" in body, f"{marker} copies without checking its root"
    assert 'Assert-DshSourceNotReparse -Path $Source' in module
    assert 'Assert-DshSourceNotReparse -Path $PresetDir' in module
    assert 'Assert-DshSourceNotReparse -Path $Tree' in module, (
        "a tree backup whose root is a junction must not be restored through")
    assert "Test-DshReparsePoint $Path" in module[
        module.index("function Assert-DshSourceNotReparse"):
        module.index("function Get-DshNestedReparsePoints")]


def test_an_unreadable_directory_is_never_read_as_an_empty_one():
    # Re-check 3, second half: Get-DshNestedReparsePoints fed Remove-DshTreeGuarded, and a
    # `-ErrorAction SilentlyContinue` enumeration turns "access denied" into "no links in here" —
    # the delete then runs on a tree that was never fully inspected. Absence yields nothing; any
    # other failure raises.
    module = _read(MODULE)
    code = _code_lines(module)
    assert "-ErrorAction SilentlyContinue" not in code, (
        "the link helpers must not swallow an enumeration failure")
    probe = module[module.index("function Get-DshChildItemsOrThrow"):
                   module.index("function Assert-DshSourceNotReparse")]
    assert "Get-ChildItem -LiteralPath $Path -Force -ErrorAction Stop" in probe
    assert "ItemNotFoundException" in probe and "DirectoryNotFoundException" in probe, (
        "a path that genuinely is not there must still enumerate as nothing")
    assert "throw" in probe, "an unreadable directory must raise, not return empty"
    discovery = module[module.index("function Get-DshNestedReparsePoints"):
                       module.index("function Copy-DshTreeNoReparse")]
    assert "Get-DshChildItemsOrThrow -Path $Current" in discovery
    assert "Test-Path -LiteralPath $Dir" not in _code_lines(discovery), (
        "a Test-Path gate is the same fail-open in a different spelling")
    for marker, label in (("function Copy-DshTreeNoReparse", "function Remove-DshTreeGuarded"),
                          ("function Copy-DshPresetBackup", "function Restore-DshPresetFromBackup")):
        body = _code_lines(module[module.index(marker):module.index(label)])
        assert "Get-DshChildItemsOrThrow" in body, f"{marker} still enumerates with a raw Get-ChildItem"
        assert "Get-ChildItem" not in body, f"{marker} bypasses the fail-closed enumerator"


def test_the_preset_side_link_is_owned_before_the_parent_tree_is_deleted():
    # Gate finding A: Remove-DshManagedTree unlinks the skills child on its way through the parent,
    # so New-DshOwnedLink used to see an ABSENT path and create the repo link — the foreign-junction
    # consent gate never fired. Ownership is now checked while both links are untouched.
    install = _read(INSTALL)
    preflight = install.index("Get-DshLinkOwnership -LinkPath $Entry.Path")
    teardown = install.index("Remove-DshManagedTree -Path $Target")
    assert preflight < teardown, "ownership must be checked before the teardown"
    assert install.count('$Ownership -eq "foreign"') >= 1
    assert '$Ownership -eq "foreign" -and -not $TakeOverSkillLinks' in install, (
        "the explicit takeover switch must be the only way past the foreign-link preflight")
    assert "-TakeOverSkillLinks" in install[install.index("foreach ($Entry in $Preflight)"):teardown]
    assert "Get-DshNestedReparsePoints -Dir $SolidworksProfile" in install
    assert "ProfilePnpmStoreRoot" in install and "StartsWith($ProfilePnpmStoreRoot" in install, (
        "only package links targeting the profile's own pnpm store may be unlinked")
    assert "-ManagedLinks @($ManagedLinksForTarget)" in install
    assert "@($PresetSkillsLink)" in install
    assert "-ManagedLinks @($PresetSkillsLink, $UserSkillLink)" not in install, (
        "the user's previous skill target must remain present until New-DshOwnedLink records it")
    module = _read(MODULE)
    guarded = module[module.index("function Remove-DshManagedTree"):
                     module.index("function Copy-DshPresetBackup")]
    assert "Remove-DshTreeGuarded -Path $Resolved -ManagedLinks $ManagedLinks" in guarded
    tree = module[module.index("function Remove-DshTreeGuarded"):
                  module.index("function Get-DshLinkOwnership")]
    assert "Get-DshNestedReparsePoints" in tree
    assert "Refusing to delete" in tree


def test_ownership_is_classified_in_one_place():
    module = _read(MODULE)
    classifier = module[module.index("function Get-DshLinkOwnership"):
                        module.index("function New-DshOwnedLink")]
    for state in ('"right"', '"ours"', '"foreign"', '"copy"', '"absent"'):
        assert state in classifier, state
    link_fn = module[module.index("function New-DshOwnedLink"):]
    assert "Get-DshLinkOwnership -LinkPath $LinkPath" in link_fn, (
        "the helper must use the same classifier the pre-flight used")


def test_restore_relinks_to_the_recorded_target():
    restore = _read(RESTORE)
    assert 'Restore-DshPresetFromBackup -BackupDir $BackupRoot' in restore
    assert "-FallbackSkillsTarget $RepoPresetSkillRoot" in restore
    assert '[string]$Manifest.userSkillLink' in restore, (
        "the user-root link is restored to where it was, not to what the repo wants")
    module = _read(MODULE)
    fn = module[module.index("function Restore-DshPresetFromBackup"):
                module.index("function New-DshOwnedLink")]
    # a tree backup may contain a real skills dir: unlink first or the copy writes through the link
    assert "Remove-DshLink -Path $SkillsLinkPath | Out-Null" in fn
    assert "backup predates the link" in fn


def test_installer_prepares_user_preserving_patches_before_teardown():
    source = _code_lines(_read(INSTALL))
    assert source.index('--mode merge') < source.index('Remove-DshManagedTree -Path $Target')
    assert 'Copy-Item -LiteralPath $PreparedProfilePatch' in source
    assert 'Copy-Item -LiteralPath $PreparedWebPatch' in source
    assert 'Render-ProfileTemplate' not in source
    assert '$PatchExitCode = $LASTEXITCODE' in _read(DOCTOR)
    assert '$CapabilityExitCode = $LASTEXITCODE' in _read(DOCTOR)


@pytest.mark.parametrize('script_name', ['Doctor-SolidWorksDsh.ps1', 'Export-SolidWorksDshConfig.ps1'])
def test_diagnostics_redact_the_entire_authorization_value(script_name):
    powershell = shutil.which('pwsh') or shutil.which('powershell')
    if not powershell:
        pytest.skip('PowerShell is not installed')
    source = _read(os.path.join(SCRIPTS, script_name))
    fixture = 'Authorization: Bearer fixture-secret-auth' + chr(10) + 'apiKey: fixture-secret-key'
    if script_name.startswith('Doctor'):
        function = source[source.index('function Redact-Config'):source.index('$Dsh =')]
        command = function + "\nRedact-Config @'\n" + fixture + "\n'@"
    else:
        redaction = source[source.index('$Redacted = $Raw'):source.index('$Directory =')]
        command = "$Raw = @'\n" + fixture + "\n'@\n" + redaction + "\n$Redacted"
    result = subprocess.run([powershell, '-NoProfile', '-NonInteractive', '-Command', command],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert 'fixture-secret' not in result.stdout
    assert result.stdout.count('<redacted>') == 2
