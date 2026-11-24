"""Editability helpers for the SolidWorks Feature Graph compiler.

Provides:
1. Safe SolidWorks feature-tree name sanitization and semantic name derivation from IR node IDs
   (preserving meaningful IDs like 'base_plate' while enriching generic IDs like 'n1' -> 'n1_box').
2. Explicit parent-child dependency extraction across Feature Graph nodes so engineers and models
   can see downstream impact before editing or deleting a feature.
3. Optional post-geometry best-effort renaming via `edit_feature(action='rename')` so the built
   .SLDPRT carries human-readable feature/sketch/plane names without risking build rollback if
   SolidWorks rejects a rename.
"""
import re

_FORBIDDEN_CHARS_RE = re.compile(r'[@/\\:*?"<>|\r\n\t\s]+')
_MULTI_UNDERSCORE_RE = re.compile(r"_+")
_GENERIC_NODE_ID_RE = re.compile(r"^(?:[a-zA-Z]{1,2}|node_?|feat_?|step_?)\d+$")

_RESERVED_SW_NAMES = frozenset({
    "front plane",
    "top plane",
    "right plane",
    "origin",
    "material",
    "annotations",
    "equations",
    "sensors",
    "history",
})

_IMPLICIT_SKETCH_CONSUMERS = frozenset({
    "extrude",
    "revolve",
    "rib",
    "sheet_metal",
    "sketched_bend",
})


def sanitize_feature_name(raw_name, max_len=56):
    """Sanitize an identifier into a safe SolidWorks FeatureManager tree name.

    `@` is strictly forbidden in SolidWorks feature names because parameter full-names use
    `D1@FeatureName@Part.Part` syntax. Path/wildcard characters and whitespace are normalized to `_`.
    """
    text = str(raw_name or "").strip()
    text = _FORBIDDEN_CHARS_RE.sub("_", text)
    text = _MULTI_UNDERSCORE_RE.sub("_", text).strip("_.")
    if not text:
        text = "feature"
    if len(text) > max_len:
        text = text[:max_len].rstrip("_.")
    return text or "feature"


def semantic_base_name(node):
    """Derive a readable base feature-tree name for a Feature Graph node."""
    nid = str((node or {}).get("id") or "").strip()
    ntype = str((node or {}).get("type") or "feature").strip()
    clean_id = sanitize_feature_name(nid or ntype, max_len=44)
    if _GENERIC_NODE_ID_RE.match(clean_id):
        if ntype in ("extrude", "revolve"):
            op = sanitize_feature_name((node or {}).get("operation") or "boss", max_len=12)
            return sanitize_feature_name("%s_%s_%s" % (clean_id, op, ntype))
        return sanitize_feature_name("%s_%s" % (clean_id, ntype))
    return clean_id


def _role_target_name(base, role):
    if role in ("primary", "primary_sketch"):
        return base
    suffix = "_" + role
    if base.lower().endswith(suffix.lower()):
        return base
    return sanitize_feature_name(base + suffix, max_len=60)


def _dedupe_name(candidate, taken_lower):
    low = candidate.lower()
    if low not in taken_lower and low not in _RESERVED_SW_NAMES:
        taken_lower.add(low)
        return candidate
    for idx in range(2, 500):
        trial = sanitize_feature_name("%s_%d" % (candidate, idx), max_len=62)
        tlow = trial.lower()
        if tlow not in taken_lower and tlow not in _RESERVED_SW_NAMES:
            taken_lower.add(tlow)
            return trial
    return candidate


def extract_node_dependencies(nodes):
    """Return {node_id: [upstream_node_id, ...]} for explicit and sketch-coupled references."""
    valid_ids = {
        n.get("id") for n in (nodes or [])
        if isinstance(n, dict) and isinstance(n.get("id"), str) and n.get("id")
    }
    deps = {}
    prev_sketch_id = None
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        nid = node.get("id")
        ntype = node.get("type")
        if not isinstance(nid, str) or not nid:
            continue
        found = []

        def _add(ref_id):
            if isinstance(ref_id, str) and ref_id in valid_ids and ref_id != nid and ref_id not in found:
                found.append(ref_id)

        if isinstance(node.get("sketch"), str):
            _add(node["sketch"])
        elif ntype in _IMPLICIT_SKETCH_CONSUMERS and prev_sketch_id:
            _add(prev_sketch_id)

        if ntype == "sweep":
            if prev_sketch_id:
                _add(prev_sketch_id)
            _add(node.get("path"))
        elif ntype == "loft":
            for pid in node.get("profiles") or []:
                _add(pid)
            for gid in node.get("guides") or []:
                _add(gid)
        elif ntype == "hole":
            _add(((node.get("ref") or {}).get("node_face") or {}).get("node"))
        elif ntype == "shell":
            _add(node.get("body"))
        elif ntype in ("linear_pattern", "circular_pattern"):
            _add(node.get("feature"))
        elif ntype == "mirror":
            for fid in node.get("features") or []:
                _add(fid)
        elif ntype == "mate":
            for side in node.get("sides") or []:
                if isinstance(side, dict):
                    _add(side.get("component"))

        deps[nid] = found
        prev_sketch_id = nid if ntype == "sketch" else None
    return deps


def apply_semantic_renames(port, sv, nodes, created_entries, *, do_rename=False):
    """Optionally rename created SolidWorks features to semantic names and build a feature map.

    `created_entries` is a list of dicts:
        {"node_id": str, "node_type": str, "role": str, "runtime_name": str}

    Returns `(new_sv, extra_exec_calls, feature_map)`.
    Renaming is strictly best-effort: if an `edit_feature(rename)` call fails or raises, the
    original runtime name is kept and compilation remains COMPLETED.
    """
    node_by_id = {
        n.get("id"): n for n in (nodes or [])
        if isinstance(n, dict) and isinstance(n.get("id"), str)
    }
    deps = extract_node_dependencies(nodes)

    # Seed collision set with runtime names that will NOT be renamed so we never collide with them.
    taken_lower = set(_RESERVED_SW_NAMES)
    for entry in created_entries:
        if entry.get("role") == "keep" and entry.get("runtime_name"):
            taken_lower.add(str(entry["runtime_name"]).lower())

    extra_calls = 0
    resolved_entries = []
    for entry in created_entries:
        nid = entry.get("node_id")
        ntype = entry.get("node_type")
        role = entry.get("role") or "primary"
        runtime_name = entry.get("runtime_name")
        if not runtime_name:
            continue

        if role == "keep":
            resolved_entries.append({
                "node_id": nid,
                "node_type": ntype,
                "role": role,
                "runtime_name": runtime_name,
                "final_name": runtime_name,
                "renamed": False,
            })
            continue

        base = semantic_base_name(node_by_id.get(nid) or {"id": nid, "type": ntype})
        desired = _dedupe_name(_role_target_name(base, role), taken_lower)
        final_name = runtime_name
        renamed = False

        if do_rename and desired != runtime_name:
            try:
                resp = port.execute(
                    "edit_feature",
                    {"feature_name": runtime_name, "action": "rename", "new_name": desired},
                    sv,
                )
            except Exception:
                resp = None
            if isinstance(resp, dict) and resp.get("status") == "COMPLETED":
                nv = resp.get("stateVersion")
                if nv is not None:
                    sv = nv
                extra_calls += 1
                feats = ((resp.get("cadState") or {}).get("features")) or []
                final_name = feats[0] if feats and feats[0] else desired
                renamed = True
            else:
                taken_lower.add(str(runtime_name).lower())
        elif desired == runtime_name:
            final_name = runtime_name

        resolved_entries.append({
            "node_id": nid,
            "node_type": ntype,
            "role": role,
            "runtime_name": runtime_name,
            "desired_name": desired,
            "final_name": final_name,
            "renamed": renamed,
        })

    feature_map = {}
    for node in nodes or []:
        if not isinstance(node, dict) or not node.get("id"):
            continue
        nid = node["id"]
        ntype = node.get("type")
        own = [e for e in resolved_entries if e["node_id"] == nid]
        tree_features = []
        primary_feature = None
        for e in own:
            fname = e["final_name"]
            if fname and fname not in tree_features:
                tree_features.append(fname)
            if e["role"] in ("primary", "primary_sketch") and primary_feature is None:
                primary_feature = fname
            elif e["role"] == "keep" and ntype in ("cosmetic_thread", "component") and primary_feature is None:
                primary_feature = fname
        if primary_feature is None and tree_features:
            primary_feature = tree_features[-1]
        feature_map[nid] = {
            "type": ntype,
            "primary_feature": primary_feature,
            "tree_features": tree_features,
            "depends_on": deps.get(nid, []),
        }

    return sv, extra_calls, feature_map
