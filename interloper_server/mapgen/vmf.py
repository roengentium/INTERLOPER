"""
vmf.py - minimal VMF writer for axis-aligned box brushes (the only brush shape the procedural
generator needs; boxes always compile cleanly).
"""

from typing import Callable, Dict, List, Optional, Sequence, Tuple

Vec = Tuple[float, float, float]

# axis-aligned face table: name -> (outward normal, uaxis, vaxis)
FACE_AXES = {
    "+x": ((1, 0, 0), "[0 1 0 0]", "[0 0 -1 0]"),
    "-x": ((-1, 0, 0), "[0 1 0 0]", "[0 0 -1 0]"),
    "+y": ((0, 1, 0), "[1 0 0 0]", "[0 0 -1 0]"),
    "-y": ((0, -1, 0), "[1 0 0 0]", "[0 0 -1 0]"),
    "+z": ((0, 0, 1), "[1 0 0 0]", "[0 -1 0 0]"),
    "-z": ((0, 0, -1), "[1 0 0 0]", "[0 -1 0 0]"),
}


def _fmt(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


def box_faces(mins: Vec, maxs: Vec) -> Dict[str, List[Vec]]:
    """Corners of each face, counter-clockwise when viewed from outside the box."""
    x0, y0, z0 = mins
    x1, y1, z1 = maxs
    return {
        "+x": [(x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1)],
        "-x": [(x0, y0, z0), (x0, y0, z1), (x0, y1, z1), (x0, y1, z0)],
        "+y": [(x0, y1, z0), (x0, y1, z1), (x1, y1, z1), (x1, y1, z0)],
        "-y": [(x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1)],
        "+z": [(x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)],
        "-z": [(x0, y0, z0), (x0, y1, z0), (x1, y1, z0), (x1, y0, z0)],
    }


def plane_points(corners: Sequence[Vec]) -> Tuple[Vec, Vec, Vec]:
    """VMF wants three points, clockwise seen from the front (outside): d, c, b of a CCW quad a,b,c,d."""
    a, b, c, d = corners
    return d, c, b


class Ids:
    def __init__(self, start: int = 2):
        self.n = start

    def next(self) -> int:
        self.n += 1
        return self.n


MaterialFn = Callable[[str], str]   # face name -> material


def solid_text(ids: Ids, mins: Vec, maxs: Vec, material: "str | MaterialFn", scale: float = 0.25) -> str:
    lines = ["\tsolid", "\t{", f'\t\t"id" "{ids.next()}"']
    for name, corners in box_faces(mins, maxs).items():
        p = plane_points(corners)
        _, ua, va = FACE_AXES[name]
        mat = material(name) if callable(material) else material
        pts = " ".join("(" + " ".join(_fmt(c) for c in pt) + ")" for pt in p)
        lines += [
            "\t\tside", "\t\t{",
            f'\t\t\t"id" "{ids.next()}"',
            f'\t\t\t"plane" "{pts}"',
            f'\t\t\t"material" "{mat}"',
            f'\t\t\t"uaxis" "{ua} {scale}"',
            f'\t\t\t"vaxis" "{va} {scale}"',
            '\t\t\t"rotation" "0"',
            '\t\t\t"lightmapscale" "16"',
            '\t\t\t"smoothing_groups" "0"',
            "\t\t}",
        ]
    lines.append("\t}")
    return "\n".join(lines)


def entity_text(ids: Ids, classname: str, kv: Dict[str, str], solids: Sequence[str] = ()) -> str:
    lines = ["entity", "{", f'\t"id" "{ids.next()}"', f'\t"classname" "{classname}"']
    for k, v in kv.items():
        lines.append(f'\t"{k}" "{v}"')
    lines.extend(solids)
    lines.append("}")
    return "\n".join(lines)


class VMF:
    def __init__(self, skyname: str = "sky_day01_01", comment: str = "", message: str = ""):
        self.ids = Ids()
        self.skyname = skyname
        self.comment = comment
        self.message = message
        self.world_solids: List[str] = []
        self.entities: List[str] = []
        self.boxes: List[Tuple[Vec, Vec, str]] = []     # (mins, maxs, kind) for validation: world|detail

    def brush(self, mins: Vec, maxs: Vec, material: "str | MaterialFn") -> None:
        self.world_solids.append(solid_text(self.ids, mins, maxs, material))
        self.boxes.append((mins, maxs, "world"))

    def detail(self, mins: Vec, maxs: Vec, material: "str | MaterialFn") -> None:
        s = solid_text(self.ids, mins, maxs, material)
        self.entities.append(entity_text(self.ids, "func_detail", {}, [s]))
        self.boxes.append((mins, maxs, "detail"))

    def entity(self, classname: str, **kv) -> None:
        self.entities.append(entity_text(self.ids, classname, {k: str(v) for k, v in kv.items()}))

    def text(self) -> str:
        head = (
            'versioninfo\n{\n\t"editorversion" "400"\n\t"editorbuild" "8000"\n\t"mapversion" "1"\n'
            '\t"formatversion" "100"\n\t"prefab" "0"\n}\n'
            'visgroups\n{\n}\n'
            'viewsettings\n{\n\t"bSnapToGrid" "1"\n\t"bShowGrid" "1"\n\t"bShowLogicalGrid" "0"\n'
            '\t"nGridSpacing" "64"\n\t"bShow3DGrid" "0"\n}\n'
        )
        world = ["world", "{", '\t"id" "1"', '\t"mapversion" "1"', '\t"classname" "worldspawn"',
                 f'\t"skyname" "{self.skyname}"', '\t"maxpropscreenwidth" "-1"',
                 '\t"detailvbsp" "detail.vbsp"', '\t"detailmaterial" "detail/detailsprites"']
        if self.comment:
            world.append(f'\t"comment" "{self.comment}"')
        if self.message:
            world.append(f'\t"message" "{self.message}"')
        world.extend(self.world_solids)
        world.append("}")
        tail = 'cameras\n{\n\t"activecamera" "-1"\n}\ncordons\n{\n\t"active" "0"\n}\n'
        return head + "\n".join(world) + "\n" + "\n".join(self.entities) + "\n" + tail
