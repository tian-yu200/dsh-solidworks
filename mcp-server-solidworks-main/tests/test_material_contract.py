"""The material contract: positive existence, exact readback, and an honest "what is on the part".

`set_part_material` is the one document-property tool whose failure the Feature Graph treats as a
WARNING (the geometry is finished, so a name that is simply not in the library must not throw the
build away). That exemption is only safe while the live layer's codes mean exactly one thing each,
and both properties were broken before 2026-10-28 (review `review-material.md`):

1. THE READBACK WAS NON-EMPTINESS, NOT EQUALITY. `GetMaterialPropertyName2` on a part that already
   carries a material returns that OLD material, so a refused apply looked like COMPLETED and the
   graph reported a material that was never applied. The gate must be an exact name AND library
   match against the request.
2. ONE CODE COVERED TWO CAUSES. "not found or could not be applied" made a real apply failure look
   like a missing name, and the compiler exemption then downgraded it. So existence is now checked
   POSITIVELY, from the material database file, BEFORE applying: MATERIAL_NOT_FOUND means only
   "this name is absent from that database"; a present-but-refused name, and any case where
   existence could not be determined, is MATERIAL_NOT_APPLIED and stays fatal.
3. THE LOOKUP MAY NOT GUESS. An unreadable/absent database, or a file that parses to zero material
   entries, is `Undeterminable` — never `NotFound`, because "I could not read it" is not evidence
   that a name is missing. NotFound requires the name to be absent from EVERY readable copy of that
   library (a library exists in one file per SolidWorks language).
4. EVERY RESPONSE REPORTS THE TRUTH. Both failure codes carry the material actually on the part
   afterwards in `cadState.features` (`material=` / `library=`, `<none>` when unset), because a
   refused apply leaves a pre-existing material in place — and the compiler warning must show it
   instead of claiming the part has no material.
5. THE COMPILER EXEMPTION STAYS NARROW: one code, checked once, after the last node, and a refused
   op is not counted in `exec_calls` (it bumped no state).

Like the other contract tests here, this diffs the SHIPPED SOURCE and the CONTRACT prose against
each other rather than trusting either side alone. No SolidWorks call is made.

Run with the adapter venv python from `mcp-server-solidworks-main/`:
`adapters/claude/.venv/Scripts/python.exe -m pytest tests/test_material_contract.py`
"""
import json
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FEATURES = os.path.join(REPO_ROOT, "execution", "solidworks", "SolidworksExecution", "Services",
                        "SolidWorksService.Features.cs")
CSPROJ = os.path.join(REPO_ROOT, "execution", "solidworks", "SolidworksExecution",
                      "SolidworksExecution.csproj")
CONTRACT = os.path.join(REPO_ROOT, "execution", "solidworks", "contracts", "tool-schemas.json")
COMPILER = os.path.join(REPO_ROOT, "compiler", "solidworks", "pycompiler", "compiler.py")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _slice(text, start_marker, end_marker):
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    return text[start:end]


def _code_lines(text):
    # A comment may NAME the wrong API to warn about it; only the code may not use it.
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("//"))


SOURCE = _read(FEATURES)
# The tool body, and the helpers it depends on, kept separate so an assertion cannot be satisfied
# by a comment in the other one.
METHOD = _slice(SOURCE, "public ExecutionResponse SetPartMaterial",
                "// set_part_material helpers")
HELPERS = _slice(SOURCE, "// set_part_material helpers", "// modify_dimension")
COMPILER_SOURCE = _read(COMPILER)
MATERIAL_BLOCK = _slice(COMPILER_SOURCE, "# 4) Graph-level material", "def _material_on_part")
WARNING_HELPER = _slice(COMPILER_SOURCE, "def _material_on_part", "def _thread_anchor")


def test_existence_is_checked_before_the_setter_is_called():
    lookup = METHOD.index("LookUpMaterial(library, materialName")
    apply = METHOD.index("partDoc.SetMaterialPropertyName2(")
    assert lookup < apply, (
        "MATERIAL_NOT_FOUND may only be reported for a name positively read out of the database, "
        "so the lookup has to happen before the apply")


def test_only_a_positively_absent_name_gets_the_non_fatal_code():
    assert 'existence == MaterialExistence.NotFound' in METHOD
    assert '"MATERIAL_NOT_FOUND"' in METHOD and '"MATERIAL_NOT_APPLIED"' in METHOD
    # Exactly one emission of each code in the tool body, never a bare BuildFailed.
    assert METHOD.count('"MATERIAL_NOT_FOUND"') == 1
    assert METHOD.count('"MATERIAL_NOT_APPLIED"') == 1


def test_a_missing_material_never_reaches_the_void_setter():
    # SetMaterialPropertyName2 returns nothing, so calling it with a name the database does not
    # contain could still change or clear what the part carries — while the guard's state_version
    # stays put, because a failure never registers. The positively-absent answer therefore returns
    # BEFORE the setter, reporting the material currently on the part.
    not_found_return = METHOD.index('"MATERIAL_NOT_FOUND"')
    setter = METHOD.index("partDoc.SetMaterialPropertyName2(")
    assert not_found_return < setter, (
        "the setter must not run for a name that is positively absent")
    assert "if (existence == MaterialExistence.NotFound)" in METHOD
    # and that response says what is really on the part, taken BEFORE anything was attempted
    assert "modelDoc, beforeMaterial, beforeDb)" in METHOD
    assert "Nothing was applied" in METHOD
    # the fatal path is the only one left after that early return
    assert METHOD.index('"MATERIAL_NOT_APPLIED"') > setter


def test_an_incomplete_database_search_is_never_reported_as_absent():
    # Review gate 1: a display-name lookup that could not enumerate a search folder, or could not
    # read a loaded database that names the requested library, must answer Undeterminable (fatal)
    # even when another copy of the library WAS read — otherwise a localised database in one
    # language can declare a material "missing" while the active one was simply unreadable.
    resolve = _slice(HELPERS, "private bool TryResolveMaterialDatabaseFiles",
                     "private static bool TryReadMaterialNames")
    search = _slice(HELPERS, "private List<string> MaterialSearchDirectories",
                    "private static bool IsExplicitDatabasePath")
    assert "MaterialSearchDirectories(List<string> problems)" in search
    assert "problems.Add(" in search
    # a loaded database naming this library whose file cannot be read is a problem, not a miss
    assert "is not readable" in resolve
    guard = resolve.index("if (problems.Count > 0)")
    success = resolve.index("database file(s) for")
    assert guard < success, "an incomplete search must fail closed before claiming a file set"
    assert "return false;" in resolve[guard:guard + 200]
    # a folder that merely does not exist is NOT a problem (it is not an expected source)
    assert "does not exist" not in search


def test_an_inaccessible_expected_folder_is_never_counted_as_absent():
    # Gate re-check 1: `Directory.Exists` answers false for access-denied, IO and path errors too,
    # so a folder that is EXPECTED to be consulted may not be dropped from the search on that
    # answer alone. Absence has to be proven (DirectoryNotFoundException) and anything else has to
    # become a problem, which turns the lookup Undeterminable instead of NotFound.
    probe = _slice(HELPERS, "private static bool CanEnumerateDirectory",
                   "private List<string> MaterialSearchDirectories")
    search = _slice(HELPERS, "private List<string> MaterialSearchDirectories",
                    "private static bool IsExplicitDatabasePath")
    assert "Directory.GetFileSystemEntries(dir)" in probe, (
        "the probe must enumerate: only the exception type separates absent from unreadable")
    assert "catch (DirectoryNotFoundException) { return false; }" in probe, (
        "a proven absence drops the folder without a problem")
    assert "problems.Add(" in probe and "could not be listed" in probe, (
        "an unusable expected folder must be reported, not skipped")
    # every candidate goes through the probe, and no Directory.Exists decision remains in the search
    assert "CanEnumerateDirectory(dir, problems)" in search
    assert "Directory.Exists" not in _code_lines(search), (
        "Directory.Exists cannot tell absence from denial, so it must not gate the search")
    # the language folder is enumerated outright, with the same absent/unusable split
    assert "Directory.GetDirectories(lang)" in _code_lines(search)
    assert "catch (DirectoryNotFoundException)" in _code_lines(search)
    assert "cannot enumerate '{lang}'" in search
    # and an incomplete search still fails closed before any file set is claimed
    resolve = _slice(HELPERS, "private bool TryResolveMaterialDatabaseFiles",
                     "private static bool TryReadMaterialNames")
    assert "if (problems.Count > 0)" in resolve


def test_the_readback_must_equal_the_request_not_merely_be_present():
    assert "bool applied = MaterialNameEquals(appliedMaterial, materialName)" in METHOD
    assert "&& LibraryMatches(library, appliedDb);" in METHOD
    assert 'if (!applied)' in METHOD, (
        "the old gate was IsNullOrEmpty(appliedMaterial): on a part that already had a material a "
        "refused apply read back non-empty and reported COMPLETED")
    # The name match is exact and never a substring ("1060 Alloy" must not match "1060-H12").
    assert "StringComparison.OrdinalIgnoreCase" in HELPERS
    assert ".Contains(" not in _slice(HELPERS, "private static bool MaterialNameEquals",
                                     "private static string NormalizeLibraryName")


def test_an_undeterminable_lookup_is_never_reported_as_not_found():
    # Both "I could not read it" paths answer Undeterminable at the caller, and NotFound is reached
    # from exactly ONE place: after entries were actually read and none matched.
    assert "if (!TryResolveMaterialDatabaseFiles(library, out files, out resolveDetail))" in HELPERS
    assert "if (!TryReadMaterialNames(file, out names, out readDetail))" in HELPERS
    assert HELPERS.count("return MaterialExistence.Undeterminable;") == 3, (
        "unresolvable library, unreadable file, and a file that parsed to zero entries are all "
        "'unknown', never 'absent'")
    assert HELPERS.count("return MaterialExistence.NotFound;") == 1
    window = HELPERS[HELPERS.index("return MaterialExistence.NotFound;") - 160:
                     HELPERS.index("return MaterialExistence.NotFound;")]
    assert "entries read from" in window
    # the zero-entry guard sits before it, so an unreadable shape cannot fall through to NotFound
    assert "if (read == 0)" in HELPERS[:HELPERS.index("return MaterialExistence.NotFound;")]
    for marker in ("GetMaterialDatabases failed", "cannot parse", "no readable material database",
                   "no material entries parsed from"):
        assert marker in HELPERS, marker


def test_an_explicit_database_path_is_used_exactly_and_never_substituted():
    # Review P1: a custom library passed as a PATH must be verified as THAT file. Falling back to an
    # installed database with the same basename would answer "this material is absent" from a
    # different file, and the compiler would warn-and-commit over a real library failure.
    resolve = _slice(HELPERS, "private bool TryResolveMaterialDatabaseFiles",
                     "private static bool TryReadMaterialNames")
    guard = resolve.index("if (IsExplicitDatabasePath(library))")
    scan = resolve.index("MaterialSearchDirectories(problems)")
    assert guard < scan, "the install scan must be unreachable for an explicit path"
    branch = resolve[guard:scan]
    assert "Path.GetFullPath(library.Trim())" in branch
    assert "if (!File.Exists(requested))" in branch
    assert "return false;" in branch, "a missing requested file is Undeterminable, never NotFound"
    assert "no " in branch and "substituted" in branch
    assert 'files.Add(requested);' in branch        # exactly the requested file, nothing else
    # the path test itself: extension, separator or rooted form — not a bare display name
    helper = _slice(HELPERS, "private static bool IsExplicitDatabasePath",
                    "private bool TryResolveMaterialDatabaseFiles")
    for marker in ('.sldmat', "IndexOf('/')", "IndexOf('\\\\')", "Path.IsPathRooted"):
        assert marker in helper, marker


def test_the_library_is_read_from_the_database_file_itself():
    assert 'SelectNodes("//*[local-name()=\'material\']")' in HELPERS, (
        "the root carries the sldmaterials namespace and its children do not — a prefixed XPath "
        "would silently match nothing and every name would look absent")
    assert '"*.sldmat"' in HELPERS
    assert "GetMaterialSchemaPathName" in HELPERS and "GetExecutablePath" in HELPERS
    # every readable copy is consulted, so NotFound means absent from ALL of them
    assert "foreach (string file in files)" in HELPERS


def test_every_response_reports_the_material_actually_on_the_part():
    assert "return WithMaterialState(" in METHOD
    assert 'material={(string.IsNullOrEmpty(material) ? "<none>" : material)}' in HELPERS
    assert 'library={(string.IsNullOrEmpty(database) ? "<none>" : database)}' in HELPERS
    # the success path reports the readback, not the request
    assert '$"material={appliedMaterial}"' in METHOD
    assert "ReadPartMaterial(partDoc, out beforeMaterial, out beforeDb)" in METHOD, (
        "the pre-existing material is what a failure message has to name")


def test_a_failed_material_op_does_not_move_state_version():
    # BuildFailed carries the guard's CURRENT version: the compiler relies on no drift after a
    # non-fatal MATERIAL_NOT_FOUND, and asserts final_state_version == the last real write.
    assert "BuildFailed(request.OperationId, _guard.GetCurrentStateVersion()" in METHOD
    assert "_guard.RegisterCompleted(request.OperationId, response)" in METHOD


def test_csproj_references_system_xml():
    assert '<Reference Include="System.Xml" />' in _read(CSPROJ), (
        "XmlDocument is used to read the .sldmat database; without the reference the service does "
        "not compile and the existence check cannot exist")


def test_contract_documents_both_codes_and_the_actual_material():
    with open(CONTRACT, encoding="utf-8") as fh:
        contract = json.load(fh)
    tool = contract["set_part_material"]
    text = tool["description"] + " " + tool["output"]
    for promise in ("MATERIAL_NOT_FOUND", "MATERIAL_NOT_APPLIED", "material=", "library=",
                    "could not be determined", "state_version"):
        assert promise in text, "tool-schemas.json no longer promises %r" % promise
    # the explicit-path rule is part of the promise too
    params = contract["set_part_material"]["input"]["params"]
    assert "no installed database of the same name is substituted for it" in params["library"]
    assert "MATERIAL_NOT_APPLIED, never MATERIAL_NOT_FOUND" in params["library"]
    # the contract must say the FAILURE also carries the real material, not just the success
    assert "on COMPLETED and on FAILED alike" in tool["output"]


def test_compiler_exempts_exactly_one_code_after_the_last_node():
    assert "if fe.code == MATERIAL_NOT_FOUND:" in MATERIAL_BLOCK
    assert MATERIAL_BLOCK.count("fe.code ==") == 1, (
        "a second exemption would silently widen the non-fatal set")
    assert '"MATERIAL_NOT_APPLIED"' not in MATERIAL_BLOCK.replace(
        "MATERIAL_NOT_APPLIED, COM_ERROR", ""), (
        "MATERIAL_NOT_APPLIED may only appear here as prose naming the fatal case")
    # geometry first, material last
    assert COMPILER_SOURCE.index("for node in nodes:") < COMPILER_SOURCE.index(
        "graph.get(\"material\")")


def test_a_refused_material_op_is_not_counted_as_executed():
    advance = MATERIAL_BLOCK.index("sv = _advance(resp, sv, \"_material\"")
    counted = MATERIAL_BLOCK.index("result.exec_calls += 1")
    assert advance < counted, (
        "exec_calls is the number of state-bumping ops performed; the increment belongs after the "
        "call that landed, not before the check that can raise")


def test_warning_carries_the_material_actually_on_the_part():
    assert '"material_on_part"' in COMPILER_SOURCE
    assert '"library_on_part"' in COMPILER_SOURCE
    assert "_material_on_part(resp)" in MATERIAL_BLOCK, (
        "the warning must be built from what the layer reported, not from the request")
    # and it must not claim "no material" when an old one is still on the part
    assert "still carries" in WARNING_HELPER
    assert "left with no material" in WARNING_HELPER
