"""The cosmetic-thread tool: one wire path, four assertions, and an attribution that must survive.

`add_cosmetic_thread` is the annotation-only thread (design doc "cosmetic path only"). Nothing else
in this repository checks the things that make it correct, because they are all facts about
SolidWorks that a plausible-looking edit would silently break:

1. SEVEN POSITIONAL arguments in a fixed order. The interop has no named-argument form at runtime
   and no overload to pick wrong, so a swapped `Size`/`Diameter` or `EndType`/`Depth` compiles and
   runs — it just threads nothing or threads at the wrong length.
2. MARK 0 on the edge selection. The real (cut) Thread family wants MARK 1 on the same edge; the
   day someone "cleans this up" behind a shared helper default, one of the two families silently
   stops finding its selection.
3. SUBFEATURE persistence + a NEGATIVE topology diff. A cosmetic thread adds zero faces/edges/
   vertices and hangs UNDER its cut, so it is invisible to the tree tail. Asserting persistence from
   `LastFeatureName()` would report every successful thread as a failure — and asserting success
   from the non-null return alone would report every silently-refused thread as a success.
4. The MIT attribution for the call shape and the edge-selection rule, and the failure codes the
   contract promises the caller.
5. THE SPEC WRITE-BACK, which exists only because of what the live probes MEASURED: the
   Standard/StandardType/Size arguments of InsertCosmeticThread3 are ignored by the feature it
   creates, so the tool writes the three values onto the data object and rebuilds. Two things can
   rot here: the write-back can be "cleaned up" away (leaving a thread that stores no spec), or it
   can be promoted into an assertion (making a cosmetic diagnostic fail a job that delivered the
   annotation). And `spec_stored` can be misread as "this install's thread table knows that size",
   which the probes explicitly disproved.
6. THE SHAPE OF result_geometry — every key the service emits must be named by the contract and by
   the adapter docstring, and every key the contract names must be emitted. The counters
   `threads_in_document_before`/`_after` are in that set for a reason: threading an already-threaded
   mouth is ADDITIVE, so a retried job piles up a second subfeature, and the response has to show it.

Like `test_analyze_read_contract.py`, this diffs the CONTRACT's prose against the shipped emitter
rather than trusting either side alone.

Run with the adapter venv python from `mcp-server-solidworks-main/`:
`adapters/claude/.venv/Scripts/python.exe -m pytest tests/test_cosmetic_thread_contract.py`
"""
import json
import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTRACT = os.path.join(REPO_ROOT, "execution", "solidworks", "contracts", "tool-schemas.json")
THREADS = os.path.join(REPO_ROOT, "execution", "solidworks", "SolidworksExecution", "Services",
                       "SolidWorksService.Threads.cs")
CONTROLLER = os.path.join(REPO_ROOT, "execution", "solidworks", "SolidworksExecution",
                          "Controllers", "ToolController.cs")
CSPROJ = os.path.join(REPO_ROOT, "execution", "solidworks", "SolidworksExecution",
                      "SolidworksExecution.csproj")
SERVER = os.path.join(REPO_ROOT, "adapters", "claude", "server.py")

TOOL = "add_cosmetic_thread"
PARAMS = ["edge_index", "standard", "standard_type", "size", "diameter", "end_type", "depth", "note"]

# Every code the tool can answer with. The four lifecycle codes are BuildFailed's house pattern and
# are not this tool's contract; the rest must all be documented in tool-schemas.json.
LIFECYCLE = {"INVALID_STATE_VERSION", "COM_ATTACH_FAILED", "NO_ACTIVE_DOCUMENT",
             "WRONG_DOCUMENT_TYPE", "COM_ERROR", "UNEXPECTED_ERROR"}


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _spec():
    return json.load(open(CONTRACT, encoding="utf-8"))[TOOL]


def _src():
    return _read(THREADS)


def _collapsed():
    """The service source with every run of whitespace flattened, so multi-line call sites can be
    matched as one string (the arg order is the fact under test, not the formatting of the call)."""
    return re.sub(r"\s+", " ", _src())


def _emitted_geometry_keys():
    """Every key AddCosmeticThread puts in result_geometry: the JObject literal plus the
    conditional `result["…"] =` additions that only appear when this build exposes them."""
    src = _src()
    keys = set(re.findall(r'\["(\w+)"\] =', src)) | set(re.findall(r'result\["(\w+)"\] =', src))
    assert keys, "no result_geometry keys found — did the emitter change shape?"
    return keys


def _documented_geometry_keys():
    """The same set as the contract states it, read off the `result_geometry = {…}` enumeration.

    Split on the commas that are outside parentheses, drop each entry's gloss, and take the leading
    token(s) of every `/`-separated group — the contract groups keys that travel together
    ("edge_center/edge_normal/edge_radius", "and end_condition / diameter_type / blind_depth when
    this build exposes them").
    """
    m = re.search(r"result_geometry = \{(.*?)\}\.", _spec()["output"], re.S)
    assert m, "contract no longer enumerates result_geometry"
    body = m.group(1)
    parts, depth, cur = [], 0, ""
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    keys = set()
    for part in parts:
        head = part.split("(")[0].strip()
        head = re.sub(r"^(and\s+)", "", head)
        for group in head.split("/"):
            word = group.strip().split(" ")
            if word and re.fullmatch(r"[a-z][a-z_0-9]*", word[0]):
                keys.add(word[0])
    assert keys, "could not parse the contract's result_geometry enumeration"
    return keys


def _writeback_states():
    """Every value spec_writeback can be reported as — including the ones reached through a ternary,
    which is why this reads the assignment statements instead of matching a literal after `=`."""
    states = set()
    for stmt in re.findall(r"specWriteback = ([^;]+);", _src()):
        states |= set(re.findall(r'"([a-z_]+)"', stmt))
    return states


def _writeback_block():
    """The SPEC WRITE-BACK region, from its banner to the assertion that follows it, so a test can
    say things about the write-back alone (it must contain no failure return). The banner is matched
    with its dashes because the header comment mentions the same name in prose."""
    src = _src()
    start = src.index("---- SPEC WRITE-BACK")
    end = src.index("if (readCallout == null)", start)
    return src[start:end]



def test_tool_is_registered_on_every_wire_path():
    """Contract, controller, project file and MCP adapter must all name the same tool.

    A missing csproj <Compile/> is the quiet one: the file exists, the IDE reads it, MSBuild does
    not compile it, and the controller's `return _service.AddCosmeticThread(request)` becomes a
    compile error only when someone builds.
    """
    assert TOOL in _read(CONTRACT)
    assert '"%s"' % TOOL in _read(CONTROLLER)
    assert "AddCosmeticThread(request)" in _read(CONTROLLER)
    assert "SolidWorksService.Threads.cs" in _read(CSPROJ)
    assert "public ExecutionResponse AddCosmeticThread(ToolRequest request)" in _src()
    # the low-level MCP surface, with exactly the eight contract params and nothing else
    server_src = _read(SERVER)
    m = re.search(r"def %s\((.*?)\) -> str:" % TOOL, server_src, re.S)
    assert m, "server.py does not define the tool"
    names = re.findall(r"^\s{4}(\w+)\s*:", m.group(1), re.M)
    # SET equality (as the drift test does): Python forces the three required params ahead of the
    # five defaulted ones, so the signature order is not the contract's order.
    assert sorted(names) == sorted(PARAMS), names


def test_seven_positional_arguments_in_the_documented_order():
    """InsertCosmeticThread3(Standard, StandardType, Size, Diameter, EndType, Depth, Note)."""
    call = _collapsed()
    m = re.search(r"modelDoc\.FeatureManager\.InsertCosmeticThread3\((.*?)\);", call)
    assert m, "no InsertCosmeticThread3 call site found"
    args = [a.strip() for a in m.group(1).split(",")]
    assert len(args) == 7, args
    assert args[0] == "standard" and args[1] == "standardType" and args[2] == "size"
    assert args[3] == "diameter.Value"
    assert args[4] == "endType"
    assert args[5] == "through ? 0.0 : depth", "a stale depth must never reach a through call"
    assert args[6] == "note"
    # the contract describes the same order in the same words
    assert ("InsertCosmeticThread3(Standard, StandardType, Size, Diameter, EndType, Depth, Note)"
            in _spec()["description"])


def test_edge_is_pre_selected_with_mark_zero_after_clearing():
    """Mark 0 (cosmetic) vs mark 1 (real Thread) is an asymmetry that must stay visible in code."""
    call = _collapsed()
    assert "modelDoc.ClearSelection2(true); var selEntity = edge as IEntity" in call or \
           "ClearSelection2(true)" in call
    assert "selData.Mark = 0;" in _src()
    assert "Select4(false, selData)" in call, "append must be false on a pre-cleared selection"
    # the real family's mark is documented HERE, next to the 0, so a "cleanup" cannot merge them
    assert "mark 1" in _src().lower() or "MARK 1" in _src()


def test_edge_index_uses_the_same_enumeration_the_reader_reports():
    """An `edge_index` is only stable if the writer walks the edges the reader numbered."""
    assert "GetBodies2((int)swBodyType_e.swSolidBody, true)" in _src()
    assert "body.GetEdges()" in _collapsed()
    desc = _spec()["input"]["params"]["edge_index"]
    assert "analyze_model" in desc and "circle" in desc.lower()
    # and the reader side documents the same shared enumeration for the descriptor
    am = json.load(open(CONTRACT, encoding="utf-8"))["analyze_model"]["description"]
    assert "add_cosmetic_thread edge_index" in am


def test_verification_is_a_recursive_subfeature_walk_not_the_tree_tail():
    src = _src()
    assert 'e.Type == "CosmeticThread"' in src
    assert "GetFirstSubFeature" in src and "GetNextSubFeature" in src
    assert "WalkFeatureTree(modelDoc)" in src
    # a cosmetic thread must NOT become the last top-level feature — that assertion is kept
    assert "lastAfter != lastBefore" in src
    # the negative topology diff
    assert "facesAfter != facesBefore || edgesAfter != edgesBefore || vertsAfter != vertsBefore" in src
    # the callout is read back and compared to the note that was sent
    assert "ThreadCallout" in src and 'readCallout != note' in src
    assert "ICosmeticThreadFeatureData" in src


def test_every_failable_answer_is_documented():
    codes = set(re.findall(r'BuildFailed\(\s*request\.OperationId,\s*_guard\.GetCurrentStateVersion\(\),\s*"([A-Z0-9_]+)"',
                           _src()))
    assert codes, "no BuildFailed call found"
    thread_codes = {c for c in codes if c.startswith("COSMETIC_THREAD_")}
    assert thread_codes >= {
        "COSMETIC_THREAD_FAILED", "COSMETIC_THREAD_NOT_PERSISTED", "COSMETIC_THREAD_AMBIGUOUS",
        "COSMETIC_THREAD_NOT_SUBFEATURE", "COSMETIC_THREAD_ADDED_TOP_LEVEL_FEATURE",
        "COSMETIC_THREAD_NOT_READABLE", "COSMETIC_THREAD_CALLOUT_MISMATCH",
        "COSMETIC_THREAD_TOPOLOGY_CHANGED",
    }, sorted(thread_codes)
    out = _spec()["output"]
    undocumented = sorted(c for c in codes - LIFECYCLE if c not in out)
    assert not undocumented, f"codes the tool can return but the contract never names: {undocumented}"
    # the reverse direction: a code promised but not in the source is a stale contract
    invented = sorted(m for m in re.findall(r"COSMETIC_THREAD_[A-Z_]+", out) if m not in codes)
    assert not invented, f"contract promises {invented} that the service cannot return"


def test_defaults_match_the_fixed_interface():
    params = _spec()["input"]["params"]
    assert "default 8" in params["standard"] and "ISO" in params["standard"]
    assert "Tapped Hole" in params["standard_type"]
    assert "default 0" in params["end_type"]
    assert "NOMINAL MAJOR" in params["diameter"].upper()
    assert "METERS" in params["diameter"].upper()
    assert "required unless end_type=2" in params["depth"] or "required when" in params["depth"]
    assert "default = size" in params["note"]
    # the service reads exactly these eight names and no others
    read = set(re.findall(r'Value<[^>]+>\("(\w+)"\)', _src()))
    assert read == set(PARAMS), sorted(read ^ set(PARAMS))


def test_mit_attribution_for_the_borrowed_call_shape():
    """The call shape and the edge-selection rule come from an MIT-licensed reference — it must stay
    attributed, with the license, the copyright line, and the file it came from."""
    header = _src()[:4000]
    assert "MIT" in header
    assert "https://github.com/wzyn20051216/solidworks-automation-skill" in header
    assert "Copyright (c) 2026 SolidWorks Automation Skill Contributors" in header
    assert "create_threaded_hole_template.py" in header
    # and the API facts are credited to Dassault's own documentation, not to the repo
    assert "InsertCosmeticThread3 is available from SOLIDWORKS 2015 FCS" in header


def test_real_thread_path_is_absent_by_design():
    """Cosmetic only: no helix, no cut-thread API.

    Checked against the CODE, not the file — the header comment deliberately explains the real
    thread family (and names IThreadFeatureData's mark-1 requirement) so the next reader does not
    unify the two.
    """
    code = "\n".join(line for line in _src().splitlines()
                     if not line.strip().startswith("//"))
    for forbidden in ("InsertThread", "Helix", "IThreadFeatureData"):
        assert forbidden not in code, forbidden
    # the ONLY thread-creating call in the file is the cosmetic one
    assert code.count("InsertCosmeticThread3(") == 1, code.count("InsertCosmeticThread3(")


# ------------------------------------------------------------------ spec write-back (measured)
def test_writeback_sets_the_three_arguments_the_api_ignores():
    """The fix for the core probe finding, asserted as a sequence: set Standard/StandardType/Size on
    the data object, ModifyDefinition, rebuild, read back, re-measure topology."""
    block = _writeback_block()
    assert "wData.Standard = standard;" in block
    assert "wData.StandardType = standardType;" in block
    assert "wData.Size = size;" in block
    # the exact ModifyDefinition overload: (data, modelDoc, null). Dropping the third argument or
    # passing a different document silently leaves the feature at its ignored-spec state.
    assert "ModifyDefinition(wData, modelDoc, null)" in block
    assert "modelDoc.EditRebuild3();" in block, "an unrebuilt write-back is not a stored spec"
    # the read-back is re-taken from a FRESH GetDefinition, not reused from the pre-write-back one
    assert "aData.Standard" in block and "aData.Size" in block and "aData.ThreadCallout" in block
    # and the topology is re-measured, because the zero-diff assertion below must judge the FINAL state
    assert "TopologyCounts(modelDoc, out facesAfter, out edgesAfter, out vertsAfter);" in block
    # it runs BEFORE the callout/topology assertions, and after the first read-back
    src = _src()
    assert (src.index("---- SPEC WRITE-BACK") < src.index("if (readCallout == null)")
            < src.index("COSMETIC_THREAD_TOPOLOGY_CHANGED"))
    # guarded so a compliant install does no pointless work
    assert "if (readStandard != standard || readSize != size ||" in block


def test_writeback_is_a_diagnostic_that_can_never_fail_a_job():
    """A COM refusal to store the spec must not turn a delivered annotation into a FAILED job."""
    block = _writeback_block()
    assert "BuildFailed" not in block, "the write-back must not return a failure"
    assert "throw" not in block
    assert "catch (Exception wEx)" in block, "an unexpected COM exception must be swallowed here"
    assert '"exception"' in block
    # every exit path of the block still lands on a value, so spec_writeback is never left unset
    assert _writeback_states() >= {"not_needed", "ignored", "applied", "modify_rejected",
                                   "data_unavailable", "unreadable_after_modify", "exception"}
    assert 'string specWriteback = "not_needed";' in _src(), \
        "a install whose read-back already matches must report not_needed, not nothing"


def test_writeback_states_agree_between_service_and_contract():
    """The vocabulary of a diagnostic is a contract: a new value in the code that the schema does not
    name is a caller reading 'ignored' where the service actually meant something else."""
    emitted = _writeback_states()
    m = re.search(r"spec_writeback \('([^)]*)'\)", _spec()["output"])
    assert m, "contract no longer enumerates spec_writeback's values"
    documented = {v.strip().strip("'") for v in m.group(1).split("|")}
    assert emitted == documented, sorted(emitted ^ documented)


def test_spec_stored_is_named_for_what_it_proves():
    """MEASURED: the size text is stored, never validated ('ZZZ-no-such-size' stores and reads back
    identically). A field called spec_resolved would claim a database lookup that does not happen."""
    src = _src()
    assert '["spec_stored"] = readStandard == standard && readSize == size,' in src
    assert "spec_resolved" not in src, "the old name claimed a lookup that never happens"
    params = _spec()["input"]["params"]
    assert "NOT validated" in params["size"]
    assert "NOT RESOLVED BY THE API" in _spec()["description"].upper()
    assert "thread table knows this size" in _spec()["output"]
    server = _read(SERVER).split("def add_cosmetic_thread")[1]
    assert "NOT looked up in a database" in server
    assert "never validated" in server
    # and the code says where the knowledge came from
    assert "MEASURED" in src and "IGNORED by the feature" in src



def test_geometry_keys_agree_between_service_contract_and_adapter():
    """One list, three surfaces. The contract and the docstring are what a caller codes against."""
    emitted = _emitted_geometry_keys()
    assert emitted == _documented_geometry_keys(), sorted(emitted ^ _documented_geometry_keys())
    assert {"cosmetic_thread", "parent_feature", "size", "callout", "spec_stored", "spec_writeback",
            "threads_in_document_before", "threads_in_document_after", "topology_unchanged",
            "edge_index", "edge_is_circle", "faces", "edges", "vertices"} <= emitted
    doc = _read(SERVER).split("def add_cosmetic_thread")[1].split("def create_drawing")[0]
    # the docstring shortens the counter pair to "…_before/_after", which is the only gap allowed
    missing = {k for k in emitted if k not in doc}
    assert missing == {"threads_in_document_after"}, sorted(missing)
    assert "threads_in_document_before/_after" in doc


def test_additive_per_edge_is_documented_on_every_surface():
    """Threading a mouth that already has a thread adds a SECOND subfeature (measured: the name
    gained a unique-suffix digit). A caller that retries blindly must be warned, and shown why."""
    assert "ADDITIVE" in _spec()["description"].upper()
    assert "NOT IDEMPOTENT" in _read(SERVER).split("def add_cosmetic_thread")[1]
    assert "additive" in _src().lower()
    out = _spec()["output"]
    assert "threads_in_document_before" in out and "threads_in_document_after" in out
    assert '["threads_in_document_before"] = beforeThreads.Count,' in _src()


def test_mouth_versus_far_end_is_documented_for_both_tools():
    """A blind hole reports TWO circles of identical radius, separated along the normal by exactly
    the cut depth (measured: z 0.02 vs 0.0076 for a 12.4 mm cut). Picking the wrong one threads the
    bottom of the bore, and nothing in the response would say so."""
    params = _spec()["input"]["params"]
    assert "center·normal" in params["edge_index"] or "center" in params["edge_index"]
    assert "mouth" in params["edge_index"]
    am = json.load(open(CONTRACT, encoding="utf-8"))["analyze_model"]["description"]
    assert "mouth" in am and "normal" in am
    server = _read(SERVER).split("def add_cosmetic_thread")[1]
    assert "far end" in server and "mouth" in server


def test_diameter_and_depth_are_documented_as_stored_not_converted():
    """Both units were measured, not assumed: 5.0 in stores as 5.0 (no conversion), and a through
    thread reports the bore's real length in BlindDepth (0.12 on a 120 mm bore)."""
    params = _spec()["input"]["params"]
    assert "NO unit conversion" in params["diameter"]
    assert "METERS" in params["diameter"].upper()
    assert "BlindDepth" in params["depth"]
    server = _read(SERVER).split("def add_cosmetic_thread")[1]
    assert "unit conversion" in server and "BlindDepth" in server

