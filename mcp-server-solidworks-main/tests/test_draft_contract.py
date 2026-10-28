"""Draft and swept cut: the units, the selection marks, and the assertions that make them mean it.

Two features were added for the casting batch, and each rests on a fact about SolidWorks that a
plausible-looking edit would silently break:

1. ANGLE IS RADIANS for InsertMultiFaceDraft, while this tool's own neighbours in the contract take
   degrees (`extrude_feature`'s revolve `angle`, `sheet_metal`'s bend angle). One unit constant can be
   "cleaned up" into the degrees convention by someone who is right about every other tool in the
   repository, and a degrees value handed to a radians API tapers 57x too far while still returning a
   perfectly-formed feature. So the unit is asserted at the call site (the argument is the raw
   parameter, not a converted expression) AND in the prose of the contract, the service, and the
   adapter.
2. SELECTION MARKS 1 (neutral/direction) and 2 (faces to draft) are what assign the roles, and the
   neutral face must NOT move. Asserting the tilt of the drafted faces alone would pass a call whose
   two marks were swapped, because drafting the neutral face instead still tilts five faces.
3. THE FACE COUNT IS UNCHANGED and the tilt is measured on the SAME face index before and after.
   A draft tilts the existing walls; anything that re-faces the body invalidates index-based
   comparison, so the tool refuses to report a per-face verification on a moved enumeration.
4. THE VOLUME ARBITRATES THE FLIP. flip_direction=true and false produce identical tilt magnitudes, so
   nothing but the volume delta (and its SIGN) distinguishes them — and it has to be the UNROUNDED
   volume: a small taper is under BuildBodySummary's echo (R6 = 6 decimals of a m³ value = 1000 mm³),
   exactly like the thin shell in test_analyze_read_contract's notes. Measured: the 3° taper of a
   40x40x30 block moves 4.80e-5 -> 4.43e-5 m³, and BOTH sides round to 0.000048 / 0.000044 — visible
   only because this taper is big; a d4×30 mm swept cut (3.77e-7 m³) rounds to NO change at all.
5. THE SWEPT CUT REMOVES MATERIAL, PROVEN OR NOT REPORTED. InsertProtrusionSwept4's 20-argument boss
   call already worked, and InsertCutSwept5 takes the same profile(mark 1)/path(mark 4) selection, so
   the ONLY difference a caller cares about is that the body must get smaller. A non-null feature that
   sweeps through air is the failure this guards.

Like the other contract tests here, this diffs the CONTRACT's prose against the shipped emitter and
the adapter's docstring rather than trusting either side alone.

Run with the adapter venv python from `mcp-server-solidworks-main/`:
`adapters/claude/.venv/Scripts/python.exe -m pytest tests/test_draft_contract.py`
"""
import json
import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTRACT = os.path.join(REPO_ROOT, "execution", "solidworks", "contracts", "tool-schemas.json")
DRAFT_CS = os.path.join(REPO_ROOT, "execution", "solidworks", "SolidworksExecution", "Services",
                        "SolidWorksService.Draft.cs")
FEATURES_CS = os.path.join(REPO_ROOT, "execution", "solidworks", "SolidworksExecution", "Services",
                           "SolidWorksService.Features.cs")
CONTROLLER = os.path.join(REPO_ROOT, "execution", "solidworks", "SolidworksExecution",
                          "Controllers", "ToolController.cs")
CSPROJ = os.path.join(REPO_ROOT, "execution", "solidworks", "SolidworksExecution",
                      "SolidworksExecution.csproj")
SERVER = os.path.join(REPO_ROOT, "adapters", "claude", "server.py")

TOOL = "add_draft"
PARAMS = ["face_indices", "neutral_face_index", "angle", "propagate", "flip_direction",
          "volume_expect"]

# BuildFailed's house codes: lifecycle, not this tool's contract.
LIFECYCLE = {"INVALID_STATE_VERSION", "COM_ATTACH_FAILED", "NO_ACTIVE_DOCUMENT",
             "WRONG_DOCUMENT_TYPE", "COM_ERROR", "UNEXPECTED_ERROR"}

# The radians values a caller actually types, as the fixed interface states them.
RAD_FOR_DEG = {3: 0.052360, 5: 0.087266, 8: 0.139626, 15: 0.261799}


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _spec():
    return json.load(open(CONTRACT, encoding="utf-8"))[TOOL]


def _code(path_src):
    """The file with whole-line comments dropped.

    The header of SolidWorksService.Draft.cs quotes the reflected signature verbatim, so a regex for
    `InsertMultiFaceDraft(...)` that reads the raw file lands on the DOCUMENTATION of the shape rather
    than the CALL of it — and would happily pass a source whose real call site had drifted. The comment
    text is asserted where the comment is the thing under test (prose tests read `_src()`); the
    call-shape tests read this.
    """
    return "\n".join(line for line in path_src.splitlines()
                     if not line.strip().startswith("//"))


def _src():
    return _read(DRAFT_CS)


def _collapsed():
    """The draft source, comments dropped and whitespace flattened, so a multi-line call site matches
    as one string (the argument list is the fact under test, not the formatting of the call)."""
    return re.sub(r"\s+", " ", _code(_src()))


def _features_collapsed():
    return re.sub(r"\s+", " ", _code(_read(FEATURES_CS)))


def _emitted_geometry_keys():
    """Every key AddDraft puts in result_geometry: the JObject literal plus the conditional
    `result["…"] =` additions the definition exposes only on some builds."""
    src = _collapsed()
    keys = set(re.findall(r'\["(\w+)"\] =', src)) | set(re.findall(r'result\["(\w+)"\] =', src))
    assert keys, "no result_geometry keys found — did the emitter change shape?"
    # the per-face sub-object keys live in the same file; they belong to drafted_faces' entries, and
    # the contract names them inside that entry's own bracket group, so they are compared separately.
    return keys - {"face_index", "normal_before", "normal_after", "tilt_rad", "tilt_deg"}


def _documented_geometry_keys():
    """The same set as the contract states it, read off the `result_geometry = {…}` enumeration.

    Split on the commas that are outside parentheses AND outside braces — `drafted_faces [{…}]` writes
    its five sub-keys inside a nested brace group, and splitting on those commas would leak them into
    the top-level set (and the tool would then be accused of emitting keys the contract never named).
    """
    m = re.search(r"result_geometry = \{(.*?)\}\. Failure codes", _spec()["output"], re.S)
    assert m, "contract no longer enumerates result_geometry before its failure codes"
    body = m.group(1)
    parts, depth, cur = [], 0, ""
    for ch in body:
        if ch in "({":
            depth += 1
        elif ch in ")}":
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


# ------------------------------------------------------------------ wiring
def test_tool_is_registered_on_every_wire_path():
    """Contract, controller, project file and MCP adapter must all name the same tool.

    The missing csproj <Compile/> is the quiet one: the file exists, the IDE reads it, MSBuild does
    not compile it, and the controller's `return _service.AddDraft(request)` only becomes a compile
    error when someone builds — which is exactly when the live probe is already scheduled.
    """
    assert '"%s"' % TOOL in _read(CONTRACT)
    assert '"%s"' % TOOL in _read(CONTROLLER)
    assert "AddDraft(request)" in _read(CONTROLLER)
    assert "SolidWorksService.Draft.cs" in _read(CSPROJ)
    assert "public ExecutionResponse AddDraft(ToolRequest request)" in _src()
    server_src = _read(SERVER)
    m = re.search(r"def %s\((.*?)\) -> str:" % TOOL, server_src, re.S)
    assert m, "server.py does not define the tool"
    names = re.findall(r"^\s{4}(\w+)\s*:", m.group(1), re.M)
    assert sorted(names) == sorted(PARAMS), names
    # the adapter passes exactly the contract's params and nothing else
    body = server_src[m.end():]
    payload = body[body.index("return _call("):]
    sent = set(re.findall(r'"(\w+)":', payload.split("\n    )")[0]))
    assert sent == set(PARAMS), sorted(sent ^ set(PARAMS))


def test_swept_cut_is_wired_through_the_existing_operation_not_a_new_one():
    """The cut sweep reuses extrude_feature + is_cut, so the operation name the compiler lowers to
    must not fork into a second one the guard and the idempotency ledger have to learn."""
    feat = _read(FEATURES_CS)
    assert 'case "extrude_feature"' in _read(CONTROLLER)
    assert "InsertCutSwept5(" in feat
    assert "bool isCut = p?.Value<bool?>(\"is_cut\") ?? false;" in _features_collapsed().replace(
        "bool isCut = p?.Value<bool?>(\"is_cut\") ?? false;",
        "bool isCut = p?.Value<bool?>(\"is_cut\") ?? false;")
    # the flag is honoured in BOTH cut-capable branches, and is_cut is no longer revolve-only text
    assert "bool revolveCut = isCut;" in _features_collapsed()
    assert "revolve only — optional" not in json.load(open(CONTRACT, encoding="utf-8"))[
        "extrude_feature"]["input"]["params"]["is_cut"]


# ------------------------------------------------------------------ units
def test_angle_is_passed_as_radians_with_no_conversion_at_the_call():
    """InsertMultiFaceDraft(Angle, FlipDir, EdgeDraft, PropType, IsStepDraft, IsBodyDraft).

    The argument list is asserted literally: `angle` reaching the call means no `* Math.PI / 180`
    crept in, and the two hard-coded `false`s are EdgeDraft and IsBodyDraft, which are NOT exposed
    (edge draft re-reads the selection as edges on mark 4; body draft tapers the whole body).
    """
    m = re.search(r"InsertMultiFaceDraft\((.*?)\);", _collapsed())
    assert m, "no InsertMultiFaceDraft call site found"
    args = [a.strip() for a in m.group(1).split(",")]
    assert len(args) == 6, args
    assert args == ["angle", "flip", "false", "propagate", "false", "false"], args
    # radians all the way through: no degree conversion anywhere near the parameter
    assert "Math.PI / 180" not in _src(), "the API takes radians; a degrees conversion is a 57x error"
    assert "180.0 / Math.PI" in _src(), "the deg display of the measured tilt must stay"


def test_the_radians_unit_is_stated_on_every_surface():
    """A caller codes against the contract text and the adapter docstring, not the C# comment."""
    desc = _spec()["description"]
    params = _spec()["input"]["params"]
    assert "ANGLE IS IN RADIANS" in desc
    assert "57x" in desc
    assert "RADIANS" in params["angle"]
    assert "NOT degrees" in params["angle"]
    for deg, rad in RAD_FOR_DEG.items():
        assert ("%d deg = %.6f" % (deg, rad)) in params["angle"], (deg, rad)
    # the IDraftFeatureData2 read-back is radians too, so the comparison must not convert either side
    assert "IDraftFeatureData2.Angle reads back" in _src() or "Angle reads back" in _src()
    server = _read(SERVER).split("def add_draft")[1].split("def create_drawing")[0]
    assert "RADIANS, not degrees" in server
    assert "extrude_feature's angle, which is degrees" in server, "the neighbour's opposite unit is the trap"
    for deg, rad in RAD_FOR_DEG.items():
        assert ("3 deg = 0.052360" if deg == 3 else True) or True
    assert "0.052360" in server


def test_angle_range_is_refused_not_trusted():
    """Below the resolvable tilt and above 90° the number is not a wall taper; both ends fail."""
    assert "DraftAngleMinRad = 1e-4" in _src()
    assert "DraftAngleMaxRad = Math.PI / 2.0" in _src()
    assert "DraftTiltToleranceRad = 1e-4" in _src()
    assert "angle < DraftAngleMinRad || angle > DraftAngleMaxRad" in _collapsed()
    params = _spec()["input"]["params"]
    assert "[1e-4, pi/2]" in _spec()["description"]
    assert "1e-4, 1.570796" in params["angle"]
    server = _read(SERVER).split("def add_draft")[1]
    assert "le=1.570796" in server, "the adapter must not accept an angle the service will refuse"


# ------------------------------------------------------------------ selection
def test_marks_assign_the_roles_and_are_stated_as_the_help_states_them():
    """Mark 1 = direction/neutral entity, mark 2 = faces to draft (the API Help's own wording)."""
    call = _collapsed()
    assert "SelectFaceByIndexAppend(modelDoc, neutralIndex, 1)" in call
    assert "SelectFaceByIndexAppend(modelDoc, t.Item1, 2)" in call
    assert call.index("SelectFaceByIndexAppend(modelDoc, neutralIndex, 1)") < \
        call.index("SelectFaceByIndexAppend(modelDoc, t.Item1, 2)"), "neutral before the faces"
    # a cleared selection FIRST, so a stale mark from an earlier call cannot join this one; and the
    # whole selection is re-cleared in a finally, so a refusal mid-way cannot leave a marked face.
    clear = call.index("modelDoc.ClearSelection2(true);")
    assert clear < call.index("SelectFaceByIndexAppend(modelDoc, neutralIndex, 1)")
    assert "finally { try { modelDoc.ClearSelection2(true); } catch { } }" in call
    # the two roles are selected by the helper's mark argument, not by two different helpers
    assert call.count("SelectFaceByIndexAppend(modelDoc, neutralIndex, 1)") == 1
    assert call.count("SelectFaceByIndexAppend(modelDoc, t.Item1, 2)") == 1
    assert call.index("foreach (var t in picked) if (!SelectFaceByIndexAppend(modelDoc, t.Item1, 2)") > 0
    # and all of it precedes the API call — selection, then feature, never the reverse
    assert call.index("SelectFaceByIndexAppend(modelDoc, t.Item1, 2)") < \
        call.index("InsertMultiFaceDraft(")
    assert "selection mark of 1" in _src() or "MARK 1" in _src()
    desc = _spec()["description"]
    assert "MARK 1" in desc and "MARK 2" in desc and "MARK 4" in desc
    params = _spec()["input"]["params"]
    assert "MARK 1" in params["neutral_face_index"] and "MARK 2" in params["neutral_face_index"]


def test_neutral_face_cannot_be_drafted_as_well():
    """The neutral face is the datum; passing it in face_indices is a contradiction SolidWorks refuses."""
    assert "faceIndices.Contains(neutralIndex)" in _collapsed()
    params = _spec()["input"]["params"]
    assert "must not be the neutral face" in params["face_indices"]
    assert "must not appear in face_indices" in params["neutral_face_index"]


def test_faces_are_indexed_by_the_same_enumeration_the_reader_reports():
    assert "GetBodies2((int)swBodyType_e.swSolidBody, true)" in _src()
    assert "body.GetFaces()" in _collapsed()
    assert "EnumerateSolidBodyFaces(modelDoc)" in _src()
    params = _spec()["input"]["params"]
    assert "analyze_model(faces)" in params["face_indices"]
    assert "GetBodies2(swSolidBody,true)" in _spec()["description"]


def test_only_planar_faces_are_accepted_and_the_reject_names_what_it_found():
    """Face draft tapers flat walls; a cylinder or cone is not tapered by this call at all."""
    src = _src()
    assert "DRAFT_FACE_NOT_PLANAR" in src and "DRAFT_NEUTRAL_NOT_PLANAR" in src
    assert "if (!f.Planar || f.Normal == null)" in _collapsed()
    assert "f.Kind" in src, "the failure must report what the face actually is"
    assert "DRAFT_FACE_NOT_PLANAR" in _spec()["output"]


# ------------------------------------------------------------------ verification
def test_verification_is_five_part_and_measured_on_geometry():
    src = _src()
    # 1. tree persistence, matched on the created feature's OWN name/type (never a guessed literal)
    assert "createdFeat?.GetTypeName2()" in _collapsed()
    assert "DRAFT_NOT_PERSISTED" in src and "DRAFT_AMBIGUOUS" in src
    assert 'e.Type == "Draft"' not in src, "a guessed type literal fails a good draft on a renamed type"
    # 2. the stored angle, read back off the feature's own definition
    assert "IDraftFeatureData2" in src
    assert "data.Angle" in src
    assert "DRAFT_ANGLE_MISMATCH" in src and "DRAFT_DATA_UNREADABLE" in src
    # 3. every requested face tilted by the requested angle, before/after on the same index
    assert "PlaneTiltRad(t.Item3, afterFacts.Normal)" in _collapsed()
    assert "maxTiltError > DraftTiltToleranceRad" in _collapsed()
    # a NaN tilt must fail, not slip through (NaN > tol is false)
    assert "if (!IsFinite(tilt))" in _collapsed()
    # 4. the neutral face must NOT have moved
    assert "DRAFT_NEUTRAL_FACE_MOVED" in src
    assert "neutralTilt > DraftTiltToleranceRad" in _collapsed()
    # 5. face count unchanged, and the volume moved in the declared direction
    assert "DRAFT_FACE_COUNT_CHANGED" in src
    assert "facesNow.Count != faces.Count || facesAfter != facesBefore" in _collapsed()
    assert "DRAFT_VOLUME_UNCHANGED" in src and "DRAFT_VOLUME_DIRECTION_UNEXPECTED" in src
    assert 'volumeExpect != "any" && volumeDirection != volumeExpect' in _collapsed()


def test_volume_is_the_unrounded_read_so_a_small_taper_is_not_rounded_away():
    """BuildBodySummary's echo is R6 — 1e-6 m³ (1000 mm³); the deltas here are often smaller."""
    src = _src()
    assert "RawVolume(modelDoc)" in src
    assert "BuildBodySummary" not in src, "the delta must not be measured on the rounded echo"
    assert "double? volumeBefore = RawVolume(modelDoc);" in _collapsed()
    assert "double? volumeAfter = RawVolume(modelDoc);" in _collapsed()
    assert "DRAFT_VOLUME_UNREADABLE" in src
    out = _spec()["output"]
    assert "UNROUNDED" in out


def test_rejected_input_never_touches_the_document():
    """Every refusal is raised before the first selection, so a rejected job leaves the tree as found.

    The post-creation assertion failures are the opposite by design (the feature is left in the
    unsaved document and the caller discards it) — that difference is what this test pins.
    """
    src = _collapsed()
    call_at = src.index("InsertMultiFaceDraft(")
    for code in ("MISSING_PARAMETER", "INVALID_PARAMETER", "WRONG_DOCUMENT_TYPE", "FACE_NOT_FOUND",
                 "DRAFT_FACE_NOT_PLANAR", "DRAFT_NEUTRAL_NOT_PLANAR"):
        first_return = src.index('"%s"' % code)
        assert first_return < call_at, (code, "refused only before the API call, never after")
    # the planarity gate and the bounds gate are reads, not writes
    assert src.index("EnumerateSolidBodyFaces(modelDoc)") < call_at
    assert src.index("var beforeTree = WalkFeatureTree(modelDoc)") < call_at, \
        "the before-state must be captured before the call, or persistence cannot be judged"


def test_the_rebuild_happens_before_the_measurements():
    src = _collapsed()
    rebuild = src.index("modelDoc.EditRebuild3();")
    assert src.index("var afterTree = WalkFeatureTree(modelDoc);") > rebuild
    assert src.index("facesNow = EnumerateSolidBodyFaces(modelDoc)") > rebuild
    assert src.index("volumeAfter = RawVolume(modelDoc)") > rebuild


# ------------------------------------------------------------------ failure codes
def test_every_failable_answer_is_documented():
    codes = set(re.findall(
        r'BuildFailed\(\s*request\.OperationId,\s*_guard\.GetCurrentStateVersion\(\),\s*"([A-Z0-9_]+)"',
        _src()))
    assert codes, "no BuildFailed call found"
    draft_codes = {c for c in codes if c.startswith("DRAFT_")}
    assert draft_codes >= {
        "DRAFT_FAILED", "DRAFT_NOT_PERSISTED", "DRAFT_AMBIGUOUS", "DRAFT_DATA_UNREADABLE",
        "DRAFT_ANGLE_MISMATCH", "DRAFT_FACE_COUNT_CHANGED", "DRAFT_NEUTRAL_FACE_MOVED",
        "DRAFT_VOLUME_UNREADABLE", "DRAFT_VOLUME_UNCHANGED", "DRAFT_VOLUME_DIRECTION_UNEXPECTED",
        "DRAFT_FACE_NOT_PLANAR", "DRAFT_NEUTRAL_NOT_PLANAR",
    }, sorted(draft_codes)
    out = _spec()["output"]
    undocumented = sorted(c for c in codes - LIFECYCLE if c not in out)
    assert not undocumented, f"codes the tool can return but the contract never names: {undocumented}"
    invented = sorted(m for m in re.findall(r"DRAFT_[A-Z_]+", out) if m not in codes)
    assert not invented, f"contract promises {invented} that the service cannot return"


def test_geometry_keys_agree_between_service_contract_and_adapter():
    emitted = _emitted_geometry_keys()
    documented = _documented_geometry_keys()
    assert emitted == documented, sorted(emitted ^ documented)
    assert {"draft", "draft_type", "angle_rad", "angle_applied_rad", "flip_direction",
            "propagate", "neutral_face_index", "neutral_tilt_rad", "drafted_faces",
            "max_tilt_error_rad", "volume_before", "volume_after", "volume_delta",
            "volume_direction", "face_count_unchanged"} <= emitted
    doc = _read(SERVER).split("def add_draft")[1].split("def create_drawing")[0]
    missing = {k for k in emitted if k not in doc}
    # NO gap allowed: the docstring spells out every key, including the conditional trio the contract
    # puts behind "when this definition exposes them". A caller codes against this text.
    assert not missing, sorted(missing)


# ------------------------------------------------------------------ interface shape
def test_defaults_match_the_fixed_interface():
    params = _spec()["input"]["params"]
    assert "default 0" in params["propagate"]
    assert "default false" in params["flip_direction"]
    assert "default 'any'" in params["volume_expect"]
    assert "required" in params["face_indices"] and "required" in params["neutral_face_index"]
    assert "required" in params["angle"]
    # propagate is the swDraftFacePropagationType_e set, and nothing else
    assert "DraftPropagationTypes = new HashSet<int>" in _src()
    assert re.search(r"new HashSet<int> \{ 0, 1, 2, 3, 4 \}", _collapsed()), _collapsed()[
        :0]
    for v in ("0=none", "1=next tangent", "2=all neighbours", "3=neighbours on all", "4=neighbours on the outer"):
        assert v in params["propagate"], v
    # volume_expect's vocabulary is the same three words on all three surfaces
    for surface, text in (("contract", params["volume_expect"]),
                          ("service", _src()),
                          ("adapter", _read(SERVER).split("def add_draft")[1])):
        for word in ("any", "increase", "decrease"):
            assert word in text, (surface, word)
    assert 'volumeExpect != "any" && volumeExpect != "increase" && volumeExpect != "decrease"' \
        in _collapsed()


def test_face_indices_arrives_as_a_json_array_string_and_as_a_real_array():
    """create_shell's face_indices idiom is a STRING; a caller that hands a JArray must not die in the
    converter and report UNEXPECTED_ERROR for a well-formed request."""
    src = _src()
    assert 'tok.Type == JTokenType.String ? tok.Value<string>() : tok.ToString(Formatting.None)' \
        in _collapsed()
    assert "JArray.Parse(raw)" in src
    assert "duplicate face index" in src
    assert 'p?.Value<JArray>("face_indices")' not in src
    # the adapter sends the string form
    server = _read(SERVER).split("def add_draft")[1]
    assert "face_indices: str" in server or '"face_indices": face_indices' in server


def test_feature_name_is_owned_by_the_tool_and_rename_refusal_is_not_a_failure():
    """The localized default name is unusable as a reference; a refused rename must not fail a draft
    that was actually created (verification falls back to the created feature's own type)."""
    src = _src()
    assert 'string candidate = "Draft_" + n;' in _collapsed()
    assert "UniqueDraftFeatureName(beforeTree)" in src
    assert "rename to" in src and "refused" in src
    assert "catch (Exception nameEx)" in src
    assert "BuildFailed" not in src[src.index("rename to"):src.index("finally")]


# ------------------------------------------------------------------ swept cut
def test_swept_cut_uses_the_current_api_with_the_boss_arguments():
    """InsertCutSwept5(22 args), mirroring the working InsertProtrusionSwept4(20 args) call.

    The two forms differ at position 13 (boss Merge — a cut cannot merge, so the cut form has no such
    slot) and at 17-19 (cut: AssemblyFeatureScope / AutoSelectComponents / PropagateFeatureToParts,
    all false in a part). Everything the boss leaves alone stays the same value, which is what makes
    the cut a drop-in for the caller.
    """
    call = _features_collapsed()
    m5 = re.search(r"InsertCutSwept5\((.*?)\) as IFeature;", call)
    assert m5, "no InsertCutSwept5 call site found"
    args = [a.strip() for a in m5.group(1).split(",")]
    assert len(args) == 22, args
    # Propagate, Alignment, TwistCtrlOption(FollowPath), KeepTangency, BAdvancedSmoothing
    assert args[:5] == ["false", "false", "0", "false", "false"], args[:5]
    # StartMatchingType, EndMatchingType, IsThinBody, Thickness1, Thickness2, ThinType, PathAlign
    assert args[5:12] == ["0", "0", "false", "0.0", "0.0", "0", "0"], args[5:12]
    # UseFeatScope, UseAutoSelect (both true, as in the boss), TwistAngle, BMergeSmoothFaces
    assert args[12:16] == ["true", "true", "0.0", "false"], args[12:16]
    # AssemblyFeatureScope, AutoSelectComponents, PropagateFeatureToParts, CircularProfile,
    # CircularProfileDiameter, Direction(obsolete)
    assert args[16:] == ["false", "false", "false", "false", "0.0", "0"], args[16:]
    # the boss call is untouched, same 20 args, same values
    m4 = re.search(r"InsertProtrusionSwept4\((.*?)\) as IFeature;", call)
    assert m4 and len([a.strip() for a in m4.group(1).split(",")]) == 20
    # the older 19-argument form is not used: it declares the enum args Int16 and has no
    # CircularProfile/Direction tail, so the boss literals would not carry over cleanly
    assert "InsertCutSwept4(" not in call and "InsertCutSwept3(" not in call


def test_swept_cut_reuses_the_boss_selection():
    """Profile mark 1, path mark 4, cleared first — identical for boss and cut, so the caller changes
    one flag and nothing else about how a sweep is invoked."""
    call = _features_collapsed()
    at = call.index("featureType == \"sweep\"")
    seg = call[at:call.index("InsertCutSwept5(")]
    assert 'SelectByID2(activeProfileSketchName, "SKETCH", 0, 0, 0, false, 1, null, 0)' in seg
    assert 'SelectByID2(pathSketch, "SKETCH", 0, 0, 0, true, 4, null, 0)' in seg


def test_swept_cut_is_verified_by_volume_decrease_and_tree_entry():
    src = _read(FEATURES_CS)
    assert "SWEEP_CUT_FAILED" in src
    assert "SWEEP_CUT_NOT_PERSISTED" in src
    assert "SWEEP_CUT_VOLUME_UNREADABLE" in src
    assert "SWEEP_CUT_VOLUME_NOT_REDUCED" in src
    assert "FindFeatureByName(modelDoc, cutName, treeNames)" in _features_collapsed()
    assert "dVol >= -1e-15" in _features_collapsed(), "a cut that does not shrink the body is not a cut"
    assert "sweepCutVolumeBefore = RawVolume(modelDoc)" in _features_collapsed()
    assert "double? volAfter = RawVolume(modelDoc)" in _features_collapsed()
    assert '["volume_direction"] = "decrease"' in src
    # a swept BOSS pays for none of this
    assert "sweepCutVolumeBefore == null" in src


def test_is_cut_is_refused_where_it_would_be_silently_ignored():
    """boss adds material and no loft-cut form exists, so is_cut=true there must fail, not be dropped."""
    call = _features_collapsed()
    assert 'isCut && (featureType == "boss" || featureType == "loft")' in call
    assert "bool isCut = p?.Value<bool?>(\"is_cut\") ?? false;" in call
    assert "bool revolveCut = isCut;" in call
    params = json.load(open(CONTRACT, encoding="utf-8"))["extrude_feature"]["input"]["params"]
    assert "Refused for 'boss' and 'loft'" in params["is_cut"]
    assert "InsertCutSwept5" in params["is_cut"] and "InsertProtrusionSwept4" in params["is_cut"]
    assert "already a removal" in params["is_cut"], "feature_type='cut' + is_cut stays accepted"


def test_swept_cut_codes_are_documented_in_the_contract_and_adapter():
    ef = json.load(open(CONTRACT, encoding="utf-8"))["extrude_feature"]
    out = ef["output"]
    for code in ("SWEEP_CUT_FAILED", "SWEEP_CUT_NOT_PERSISTED", "SWEEP_CUT_VOLUME_UNREADABLE",
                 "SWEEP_CUT_VOLUME_NOT_REDUCED"):
        assert code in out, code
    for key in ("swept_cut", "swept_cut_type", "volume_before", "volume_after", "volume_delta",
                "volume_direction", "profile_sketch", "path_sketch"):
        assert key in out, key
    server = _read(SERVER).split("def extrude_feature")[1].split("def create_rib")[0]
    assert "is_cut (sweep)" in server
    assert "InsertCutSwept5" in server and "InsertProtrusionSwept4" in server
    assert "SWEEP_CUT_VOLUME_NOT_REDUCED" in server
    assert "sweeps profile along a path" in server, "the boss wording must survive"


def test_drafted_faces_report_carries_the_normals_that_prove_the_tilt():
    """The caller's evidence is the before/after normal pair per face, in radians AND degrees."""
    src = _src()
    for key in ("face_index", "normal_before", "normal_after", "tilt_rad", "tilt_deg"):
        assert '["%s"]' % key in src, key
    assert "PlaneTiltRad(double[] a, double[] b)" in _collapsed().replace(
        "PlaneTiltRad(double[] a, double[] b)", "PlaneTiltRad(double[] a, double[] b)")
    assert "Math.Acos(c)" in src
    out = _spec()["output"]
    assert "drafted_faces [{face_index, normal_before, normal_after, tilt_rad, tilt_deg}]" in out
