"""
validate.py - parse VMF text, check brush geometry, and run a leak test that mirrors what VBSP does.

VBSP flood-fills from every point entity through empty space; if the fill escapes the sealed
world brushes the map "leaks" and cannot be vis'd/lit. We reproduce that on a voxel grid so a bad
map (ours or an LLM-written one) is rejected before spending minutes in the compiler.
func_detail and other brush entities do NOT seal a map, so only worldspawn solids count.
"""

import itertools
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

Vec = Tuple[float, float, float]
_TOKEN = re.compile(r'"([^"]*)"|([{}])|([^\s{}"]+)')


@dataclass
class Node:
    name: str
    kv: Dict[str, str] = field(default_factory=dict)
    kids: List["Node"] = field(default_factory=list)

    def all(self, name: str) -> List["Node"]:
        return [k for k in self.kids if k.name == name]


class VMFError(ValueError):
    pass


def parse_vmf(text: str) -> Node:
    toks = [(m.group(1), m.group(2), m.group(3)) for m in _TOKEN.finditer(text)]
    pos = 0
    root = Node("root")
    stack = [root]
    pending: Optional[str] = None
    i = 0
    while i < len(toks):
        q, brace, word = toks[i]
        if brace == "{":
            if pending is None:
                raise VMFError("'{' without a section name")
            node = Node(pending)
            stack[-1].kids.append(node)
            stack.append(node)
            pending = None
        elif brace == "}":
            if len(stack) < 2:
                raise VMFError("unbalanced '}'")
            stack.pop()
        elif word is not None:
            pending = word
        elif q is not None:
            # key "value" pair: next token must be a quoted string
            if i + 1 >= len(toks) or toks[i + 1][0] is None:
                raise VMFError(f"key {q!r} without a value")
            stack[-1].kv[q] = toks[i + 1][0]
            i += 1
        i += 1
    if len(stack) != 1:
        raise VMFError("unclosed section")
    return root


def _vec(s: str) -> Vec:
    nums = re.findall(r"-?\d+(?:\.\d+)?(?:e-?\d+)?", s)
    if len(nums) != 3:
        raise VMFError(f"bad vector {s!r}")
    return float(nums[0]), float(nums[1]), float(nums[2])


def _planes_of(side_plane: str):
    pts = re.findall(r"\(([^)]*)\)", side_plane)
    if len(pts) != 3:
        raise VMFError(f"bad plane {side_plane!r}")
    return tuple(_vec(p) for p in pts)


def _sub(a, b): return (a[0] - b[0], a[1] - b[1], a[2] - b[2])
def _cross(a, b): return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])
def _dot(a, b): return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _norm(a):
    l = _dot(a, a) ** 0.5
    return (a[0] / l, a[1] / l, a[2] / l) if l > 1e-9 else (0.0, 0.0, 0.0)


class Brush:
    def __init__(self, solid: Node):
        self.planes = []         # (outward unit normal, d)
        sides = solid.all("side")
        if len(sides) < 4:
            raise VMFError("solid with fewer than 4 sides")
        for s in sides:
            p1, p2, p3 = _planes_of(s.kv.get("plane", ""))
            n = _norm(_cross(_sub(p3, p1), _sub(p2, p1)))
            if n == (0.0, 0.0, 0.0):
                raise VMFError("degenerate side plane")
            self.planes.append((n, _dot(n, p1)))
        self.verts = self._vertices()
        if len(self.verts) < 4 or self._volume() < 1.0:
            raise VMFError("brush has no volume (planes don't enclose a convex solid)")
        xs, ys, zs = zip(*self.verts)
        self.mins = (min(xs), min(ys), min(zs))
        self.maxs = (max(xs), max(ys), max(zs))

    def _vertices(self):
        out = []
        for (n1, d1), (n2, d2), (n3, d3) in itertools.combinations(self.planes, 3):
            c = _cross(n2, n3)
            det = _dot(n1, c)
            if abs(det) < 1e-9:
                continue
            a, b, c2 = _cross(n2, n3), _cross(n3, n1), _cross(n1, n2)
            p = tuple((d1 * a[k] + d2 * b[k] + d3 * c2[k]) / det for k in range(3))
            if all(_dot(n, p) <= d + 0.01 for n, d in self.planes):
                if not any(abs(p[0] - q[0]) + abs(p[1] - q[1]) + abs(p[2] - q[2]) < 0.05 for q in out):
                    out.append(p)
        return out

    def _volume(self) -> float:
        v = self.verts
        best = 0.0
        for i in range(1, len(v) - 2):
            for j in range(i + 1, len(v) - 1):
                for k in range(j + 1, len(v)):
                    best = max(best, abs(_dot(_sub(v[i], v[0]), _cross(_sub(v[j], v[0]), _sub(v[k], v[0])))) / 6.0)
        return best

    def contains(self, p: Vec) -> bool:
        return all(_dot(n, p) <= d + 1e-6 for n, d in self.planes)


@dataclass
class Report:
    ok: bool = True
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    brushes: int = 0
    leaked: bool = False
    spawn: Optional[Vec] = None
    sealed_voxels: int = 0


def parse_origin(s: str) -> Optional[Vec]:
    try:
        return _vec(s)
    except VMFError:
        return None


def check(text: str, voxel: int = 32, max_brushes: int = 4000) -> Report:
    """Full validation of VMF text. Never raises: problems are returned in the report."""
    rep = Report()
    try:
        root = parse_vmf(text)
    except VMFError as exc:
        rep.ok = False
        rep.errors.append(f"parse: {exc}")
        return rep
    worlds = root.all("world")
    if len(worlds) != 1:
        rep.ok = False
        rep.errors.append("needs exactly one world section")
        return rep
    brushes: List[Brush] = []
    try:
        for s in worlds[0].all("solid"):
            brushes.append(Brush(s))
    except VMFError as exc:
        rep.ok = False
        rep.errors.append(f"brush: {exc}")
        return rep
    rep.brushes = len(brushes)
    if not brushes:
        rep.ok = False
        rep.errors.append("no world brushes")
        return rep
    if len(brushes) > max_brushes:
        rep.ok = False
        rep.errors.append("too many brushes")
        return rep

    ents = root.all("entity")
    starts = [e for e in ents if e.kv.get("classname") in ("info_player_start", "info_player_deathmatch")]
    if not starts:
        rep.ok = False
        rep.errors.append("no info_player_start")
        return rep
    spawn = parse_origin(starts[0].kv.get("origin", ""))
    if spawn is None:
        rep.ok = False
        rep.errors.append("info_player_start has no origin")
        return rep
    rep.spawn = spawn

    # voxelise the sealed world
    pad = 2
    lo = [min(b.mins[k] for b in brushes) - pad * voxel for k in range(3)]
    hi = [max(b.maxs[k] for b in brushes) + pad * voxel for k in range(3)]
    dims = [int((hi[k] - lo[k]) // voxel) + 1 for k in range(3)]
    if dims[0] * dims[1] * dims[2] > 40_000_000:
        rep.ok = False
        rep.errors.append("map too large to check")
        return rep
    nx, ny, nz = dims
    solid = bytearray(nx * ny * nz)

    def idx(i, j, k):
        return (k * ny + j) * nx + i

    half = voxel / 2.0
    for b in brushes:
        i0, i1 = [int((b.mins[0] - lo[0]) // voxel), int((b.maxs[0] - lo[0]) // voxel)]
        j0, j1 = [int((b.mins[1] - lo[1]) // voxel), int((b.maxs[1] - lo[1]) // voxel)]
        k0, k1 = [int((b.mins[2] - lo[2]) // voxel), int((b.maxs[2] - lo[2]) // voxel)]
        for k in range(k0, k1 + 1):
            z = lo[2] + k * voxel + half
            for j in range(j0, j1 + 1):
                y = lo[1] + j * voxel + half
                for i in range(i0, i1 + 1):
                    if b.contains((lo[0] + i * voxel + half, y, z)):
                        solid[idx(i, j, k)] = 1

    def vox(p):
        return (int((p[0] - lo[0]) // voxel), int((p[1] - lo[1]) // voxel), int((p[2] - lo[2]) // voxel))

    start = vox((spawn[0], spawn[1], spawn[2] + 36))
    if not all(0 <= start[a] < dims[a] for a in range(3)) or solid[idx(*start)]:
        rep.ok = False
        rep.errors.append("player start is inside a wall or outside the map")
        return rep
    seen = bytearray(nx * ny * nz)
    stack = [start]
    seen[idx(*start)] = 1
    count = 0
    while stack:
        i, j, k = stack.pop()
        count += 1
        if i in (0, nx - 1) or j in (0, ny - 1) or k in (0, nz - 1):
            rep.leaked = True
            rep.ok = False
            rep.errors.append("LEAK: the playable space is open to the void")
            rep.sealed_voxels = count
            return rep
        for di, dj, dk in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
            a, b2, c = i + di, j + dj, k + dk
            n = idx(a, b2, c)
            if not solid[n] and not seen[n]:
                seen[n] = 1
                stack.append((a, b2, c))
    rep.sealed_voxels = count

    outside = 0
    for e in ents:
        cls = e.kv.get("classname", "")
        if e.kids or cls in ("func_detail",):
            continue
        o = parse_origin(e.kv.get("origin", ""))
        if o is None:
            continue
        v = vox(o)
        if not all(0 <= v[a] < dims[a] for a in range(3)) or not seen[idx(*v)]:
            outside += 1
            rep.warnings.append(f"{cls} at {o} is outside the sealed space")
    if outside:
        rep.warnings.append(f"{outside} entities are in solid/void and will be dropped")
    return rep


# ---------------------------------------------------------------------------
# repair: LLM-written VMF usually has the right shapes but wrong winding / ids
# ---------------------------------------------------------------------------

def dump(node: Node, depth: int = 0) -> str:
    pad = "\t" * depth
    out = []
    if node.name != "root":
        out += [f"{pad}{node.name}", f"{pad}{{"]
        inner = depth + 1
    else:
        inner = depth
    ip = "\t" * inner
    for k, v in node.kv.items():
        out.append(f'{ip}"{k}" "{v}"')
    for kid in node.kids:
        out.append(dump(kid, inner))
    if node.name != "root":
        out.append(f"{pad}}}")
    return "\n".join(out)


def repair(root: Node) -> int:
    """Flip inward-facing side planes and renumber ids. Returns the number of sides flipped."""
    flipped = 0
    counter = [0]

    def nid():
        counter[0] += 1
        return str(counter[0])

    def fix_solid(solid: Node):
        nonlocal flipped
        sides = solid.all("side")
        pts_all = []
        for s in sides:
            try:
                pts_all.extend(_planes_of(s.kv.get("plane", "")))
            except VMFError:
                return
        if not pts_all:
            return
        c = tuple(sum(p[k] for p in pts_all) / len(pts_all) for k in range(3))
        for s in sides:
            p1, p2, p3 = _planes_of(s.kv["plane"])
            n = _norm(_cross(_sub(p3, p1), _sub(p2, p1)))
            if _dot(n, _sub(c, p1)) > 0:          # normal points toward the brush middle -> inward
                s.kv["plane"] = " ".join("(" + " ".join(f"{x:g}" for x in p) + ")" for p in (p1, p3, p2))
                flipped += 1
            s.kv["id"] = nid()
        solid.kv["id"] = nid()

    for top in root.kids:
        if top.name in ("world", "entity"):
            top.kv["id"] = nid()
            for solid in top.all("solid"):
                fix_solid(solid)
    return flipped
