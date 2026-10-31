"""Bounded automatic placement repairs; other changes require explicit user approval."""
import copy
import json
import math

from policy import payload_hash

DIMENSION_TOLERANCE = 1e-9
DESCRIPTIVE = {"description", "hint", "comment", "metadata", "plan_version"}
POSITION_FIELDS = {"ref", "frame", "reversed", "reverse", "flip", "axis", "direction", "origin", "position", "transform", "placement", "plane"}
REFERENCE_FIELDS = {"node", "sketch", "seed", "body", "path", "profiles", "feature", "features"}


def _profile_contract(profile):
    """Keep primitive dimensions and points for whole-sketch rigid congruence."""
    geometry = []
    for p in profile:
        kind = p["kind"]
        center = (p.get("cx", 0), p.get("cy", 0))
        coord = {"cx", "cy", "x1", "y1", "x2", "y2", "points", "angle", "dir"}
        item = {k: v for k, v in p.items() if k not in coord | DESCRIPTIVE}
        item.setdefault("construction", False)
        pts = []
        if kind == "circle":
            pts = [center]
        elif kind in ("rectangle", "rounded_rect"):
            w, h = p["width"] / 2, p["height"] / 2
            item.pop("width")
            item.pop("height")
            item["sides"] = sorted([p["width"], p["height"]])
            pts = [(center[0]+x*w, center[1]+y*h) for x,y in ((-1,-1),(1,-1),(1,1),(-1,1))]
        elif kind == "slot":
            a = p.get("angle", 0)
            c, s = math.cos(a), math.sin(a)
            pts = [(center[0]+x*c-y*s, center[1]+x*s+y*c)
                   for x,y in ((-p["length"]/2,0),(p["length"]/2,0),(0,-p["width"]/2),(0,p["width"]/2))]
        elif kind in ("line", "arc", "ellipse"):
            a, b = (p["x1"],p["y1"]), (p["x2"],p["y2"])
            pts = [a,b]
            if kind == "line":
                item["length"] = math.dist(a,b)
            else:
                pts = [center,a,b]
                u,v = (a[0]-center[0],a[1]-center[1]), (b[0]-center[0],b[1]-center[1])
                item["radii"] = [math.hypot(*u),math.hypot(*v)]
                if kind == "arc":
                    item["sweep"] = (math.atan2(u[0]*v[1]-u[1]*v[0],u[0]*v[0]+u[1]*v[1])*p["dir"]) % (2*math.pi)
        elif kind == "spline":
            pts = list(zip(p["points"][::2],p["points"][1::2]))
            item["point_count"] = len(pts)
        else:
            # Validation owns the grammar; future primitives must not silently lose dimensions.
            item = copy.deepcopy(p)
        geometry.append({"dimensions": item, "points": pts})
    return {"_rigid_profile": geometry}


def _profile_equal(left, right):
    """Match whole primitives under one 2-D isometry, never separate local moves.

    A distance multiset alone admits non-congruent point sets and loses spline order.
    Candidate transforms map one spanning pair onto an equal-length pair, in both
    orientations. Bipartite matching permits primitive reordering without losing
    the association between a primitive's size, flags and its position.
    """
    if len(left) != len(right):
        return False
    points = [p for primitive in left for p in primitive["points"]]
    targets = [p for primitive in right for p in primitive["points"]]
    if len(points) != len(targets):
        return False

    def same_primitive(a, b, transform):
        if not dimensions_equal(a["dimensions"], b["dimensions"]):
            return False
        ap = [transform(p) for p in a["points"]]
        bp = b["points"]
        if len(ap) != len(bp):
            return False
        def near(p, q):
            return math.dist(p, q) <= DIMENSION_TOLERANCE
        kind = a["dimensions"]["kind"]
        if kind in ("rectangle", "rounded_rect", "slot", "line"):
            # Corners/endpoints have no start label. Keep multiplicities.
            remaining = list(bp)
            for p in ap:
                j = next((j for j,q in enumerate(remaining) if near(p,q)), None)
                if j is None:return False
                remaining.pop(j)
            return True
        if all(near(p,q) for p,q in zip(ap,bp)):
            return True
        return kind == "spline" and all(near(p,q) for p,q in zip(ap,reversed(bp)))

    def matches(transform):
        candidates = [[j for j,b in enumerate(right) if same_primitive(a,b,transform)] for a in left]
        assigned = {}
        def assign(i, seen):
            for j in candidates[i]:
                if j in seen:continue
                seen.add(j)
                if j not in assigned or assign(assigned[j],seen):
                    assigned[j] = i
                    return True
            return False
        return all(assign(i,set()) for i in sorted(range(len(left)),key=lambda i:len(candidates[i])))

    if not points:
        return matches(lambda p:p)
    a = points[0]
    b = max(points,key=lambda p:math.dist(a,p))
    length = math.dist(a,b)
    if length <= DIMENSION_TOLERANCE:
        return any(matches(lambda p, c=c:(p[0]-a[0]+c[0],p[1]-a[1]+c[1])) for c in targets)
    ux,uy = (b[0]-a[0])/length,(b[1]-a[1])/length
    for c in targets:
        for d in targets:
            distance = math.dist(c,d)
            if distance == 0 or abs(distance-length) > DIMENSION_TOLERANCE:continue
            vx,vy = (d[0]-c[0])/distance,(d[1]-c[1])/distance
            for sign in (1,-1):
                def transform(p):
                    x,y = p[0]-a[0],p[1]-a[1]
                    along,across = x*ux+y*uy,sign*(-x*uy+y*ux)
                    return (c[0]+along*vx-across*vy,c[1]+along*vy+across*vx)
                if matches(transform):return True
    return False


def _normalized_graph(graph, *, design):
    nodes = graph.get("nodes") or []
    ids = {n["id"]: "node-%s" % i for i,n in enumerate(nodes)}

    def clean(value, key=""):
        if isinstance(value,dict):
            return {k:clean(v,k) for k,v in value.items()
                    if k not in DESCRIPTIVE and not (design and k in {"near", "at"})}
        if isinstance(value,list):
            return [clean(v,key) for v in value]
        if isinstance(value,str) and key in REFERENCE_FIELDS:
            return ids.get(value,value)
        return value

    result = clean(copy.deepcopy(graph))
    result.pop("id",None)
    result.pop("name",None)
    for node in result["nodes"]:
        node.pop("id",None)
        node.pop("name",None)
        if not design:
            continue
        for key in POSITION_FIELDS:
            node.pop(key,None)
        if isinstance(node.get("profile"),list):
            node["profile"] = _profile_contract(node["profile"])
        if node["type"] == "extrude":
            end = node.get("end") or ("through_all" if node.get("through") or node.get("depth") in ("through","through_all") else "blind")
            node["end"] = end
            node.pop("through",None)
            if end == "through_all":
                node.pop("depth",None)
    return result


def design_contract(graph):
    return _normalized_graph(graph,design=True)


def dimensions_equal(left,right):
    if isinstance(left,bool) or isinstance(right,bool):
        return type(left) is type(right) and left == right
    if isinstance(left,(float,int)) and isinstance(right,(float,int)):
        return math.isfinite(left) and math.isfinite(right) and math.isclose(left,right,rel_tol=0,abs_tol=DIMENSION_TOLERANCE)
    if isinstance(left,dict) and isinstance(right,dict):
        if "_rigid_profile" in left and "_rigid_profile" in right:
            return _profile_equal(left["_rigid_profile"], right["_rigid_profile"])
        return left.keys() == right.keys() and all(dimensions_equal(left[k],right[k]) for k in left)
    if isinstance(left,list) and isinstance(right,list):
        return len(left) == len(right) and all(dimensions_equal(a,b) for a,b in zip(left,right))
    return left == right


def graph_fingerprints(graph):
    return payload_hash(design_contract(graph)),payload_hash(_normalized_graph(graph,design=False))


def design_change_description(old,new):
    def first(a,b,path=""):
        if isinstance(a,dict) and isinstance(b,dict):
            for k in sorted(a.keys()|b.keys()):
                if k not in a or k not in b:return path+"."+k,a.get(k),b.get(k)
                if not dimensions_equal(a[k],b[k]):return first(a[k],b[k],path+"."+k)
        if isinstance(a,list) and isinstance(b,list):
            if len(a)!=len(b):return path+".数量",len(a),len(b)
            for i,(x,y) in enumerate(zip(a,b)):
                if not dimensions_equal(x,y):return first(x,y,path+"[%s]"%i)
        return path,a,b
    path,a,b=first(old,new)
    if path.endswith("diameter") and isinstance(a,(int,float)) and isinstance(b,(int,float)):
        return "设计有改动：Ø%g→Ø%g mm" % (a*1000,b*1000)
    return "设计有改动：%s %s→%s" % (path,a,b)


def repair_evidence(raw):
    evidence=json.loads(raw or "{}") if isinstance(raw,str) else raw
    if not isinstance(evidence,dict):raise ValueError("repair_evidence_json must be an object")
    return evidence


def admit(chain,graph,evidence):
    design,representation=graph_fingerprints(graph)
    if not chain:
        if evidence:raise RuntimeError("REPAIR_NO_PREDECESSOR: 没有可关联的上次失败，需确认开始新执行")
        return design,representation
    if not chain.get("attempts"):
        raise RuntimeError("REPAIR_INCOMPLETE_CHAIN: 上次执行账本不完整，需用户确认后开始新执行")
    last=chain["attempts"][-1]
    if last.get("recovery_status") != "discarded":
        raise RuntimeError("REPAIR_RECOVERY_UNVERIFIED: 上次执行或恢复未验证，新建文档可能未关闭")
    if len(chain["attempts"])>=3:
        raise RuntimeError("REPAIR_LIMIT_REACHED: 已自动修复 2 次仍失败")
    previous=chain.get("design_contract")
    if previous is None:
        raise RuntimeError("REPAIR_LEGACY_CONTRACT: 上次为旧版恢复账本，需确认后采用新规则")
    if not dimensions_equal(previous,design_contract(graph)):
        raise RuntimeError("REPAIR_DESIGN_CHANGED: "+design_change_description(previous,design_contract(graph)))
    if any(a.get("representation")==representation for a in chain["attempts"]):
        raise RuntimeError("REPAIR_IDENTICAL_REPLAY: 原样重放不属于自动修复，需用户确认")
    if (any(not isinstance(evidence.get(k),str) or not evidence[k].strip() for k in ("error_code","cause","readback","change"))
            or evidence["error_code"] != last.get("error_code")):
        raise RuntimeError("REPAIR_EVIDENCE_REQUIRED: 修复证据须完整且对应上次错误")
    digest=payload_hash(evidence)
    if any(a.get("evidence_hash")==digest for a in chain["attempts"]):
        raise RuntimeError("REPAIR_NEW_EVIDENCE_REQUIRED: 第二次修复需要新的证据")
    return design,representation
