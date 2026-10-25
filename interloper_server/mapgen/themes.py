"""
themes.py - per-game material sets and the Theme the generator consumes.

Material names are stock content that ships with each game, but they have NOT been checked against
a live install: a name that doesn't exist just renders as the pink/black error texture (the compile
still succeeds). Edit TEXTURES here (or drop a mapgen_textures.json next to the server to override
whole style tables) if one looks wrong in game. 'dev' styles use developer textures that always exist.
"""

import json
import os
from dataclasses import dataclass, field, fields
from typing import Dict, Optional, Tuple

NODRAW = "TOOLS/TOOLSNODRAW"

GAME_FAMILY = {
    "hl2": "hl2", "hl2_complete": "hl2", "episodic": "hl2", "ep2": "hl2", "lostcoast": "hl2",
    "hl2mp": "hl2", "sdk2013": "hl2", "halflife2": "hl2",
    "portal": "portal",
    "garrysmod": "gmod", "gmod": "gmod",
}

# style -> material slots
TEXTURES: Dict[str, Dict[str, Dict[str, str]]] = {
    "hl2": {
        "dev":      {"wall": "DEV/DEV_MEASUREWALL01A", "floor": "DEV/DEV_MEASUREGENERIC01B",
                     "ceil": "DEV/DEV_MEASUREGENERIC01B", "trim": "DEV/DEV_MEASUREGENERIC01B",
                     "pillar": "DEV/DEV_MEASUREWALL01A", "crate": "DEV/DEV_MEASUREGENERIC01B"},
        "concrete": {"wall": "CONCRETE/CONCRETEWALL001A", "floor": "CONCRETE/CONCRETEFLOOR001A",
                     "ceil": "CONCRETE/CONCRETEFLOOR001A", "trim": "CONCRETE/CONCRETEWALL001A",
                     "pillar": "CONCRETE/CONCRETEWALL001A", "crate": "CONCRETE/CONCRETEFLOOR001A"},
        "brick":    {"wall": "BRICK/BRICKWALL001A", "floor": "BRICK/BRICKFLOOR001A",
                     "ceil": "CONCRETE/CONCRETEFLOOR001A", "trim": "BRICK/BRICKWALL001A",
                     "pillar": "BRICK/BRICKWALL001A", "crate": "BRICK/BRICKFLOOR001A"},
        "plaster":  {"wall": "PLASTER/PLASTERWALL001A", "floor": "TILE/TILEFLOOR001A",
                     "ceil": "PLASTER/PLASTERWALL001A", "trim": "PLASTER/PLASTERWALL001A",
                     "pillar": "PLASTER/PLASTERWALL001A", "crate": "TILE/TILEFLOOR001A"},
    },
    "portal": {
        "dev":      {"wall": "DEV/DEV_MEASUREWALL01A", "floor": "DEV/DEV_MEASUREGENERIC01B",
                     "ceil": "DEV/DEV_MEASUREGENERIC01B", "trim": "DEV/DEV_MEASUREGENERIC01B",
                     "pillar": "DEV/DEV_MEASUREWALL01A", "crate": "DEV/DEV_MEASUREGENERIC01B"},
        "sterile":  {"wall": "TILE/WHITE_WALL_TILE003A", "floor": "TILE/WHITE_FLOOR_TILE002A",
                     "ceil": "TILE/WHITE_CEILING_TILE002A", "trim": "METAL/BLACK_WALL_METAL_002A",
                     "pillar": "TILE/WHITE_WALL_TILE003A", "crate": "METAL/BLACK_WALL_METAL_002A"},
        "dark":     {"wall": "METAL/BLACK_WALL_METAL_002A", "floor": "METAL/BLACK_WALL_METAL_002A",
                     "ceil": "METAL/BLACK_WALL_METAL_002A", "trim": "TILE/WHITE_WALL_TILE003A",
                     "pillar": "METAL/BLACK_WALL_METAL_002A", "crate": "TILE/WHITE_WALL_TILE003A"},
    },
    "gmod": {},   # filled below from hl2
}
TEXTURES["gmod"] = TEXTURES["hl2"]

SKYNAMES = {"hl2": "sky_day01_01", "portal": "sky_black", "gmod": "painted"}

# per-game extra entities placed in the level
GAME_ITEMS = {
    "hl2": ["item_healthkit", "item_battery", "item_ammo_smg1", "weapon_smg1", "item_ammo_pistol"],
    "gmod": ["item_healthkit", "item_battery", "item_ammo_smg1"],
    "portal": [],
}


@dataclass
class Theme:
    name: str = "untitled"
    style: str = "dev"
    cols: int = 5
    rows: int = 5
    height: int = 256                  # multiple of 32, 192..384
    loop_chance: float = 0.25          # extra doorways beyond the spanning tree
    darkness: float = 0.3              # fraction of rooms with no light (spawn room is always lit)
    pillar_chance: float = 0.25        # per room
    crate_chance: float = 0.25         # per room
    items: int = 6                     # pickups scattered around
    light_rgb: Tuple[int, int, int] = (255, 244, 220)
    brightness: int = 200
    fog: bool = False
    fog_rgb: Tuple[int, int, int] = (20, 20, 24)
    fog_end: int = 1800
    skyname: str = ""
    notes: str = ""


SIZES = {"tiny": (3, 3), "small": (4, 4), "medium": (6, 6), "large": (8, 8), "huge": (10, 10)}

# hard limits the LLM / CLI values are clamped to (keeps maps compilable and small)
LIMITS = {"cols": (2, 12), "rows": (2, 12), "height": (192, 384), "loop_chance": (0.0, 0.8),
          "darkness": (0.0, 0.9), "pillar_chance": (0.0, 0.8), "crate_chance": (0.0, 0.8),
          "items": (0, 30), "brightness": (40, 600), "fog_end": (400, 8000)}


def family(game: str) -> str:
    return GAME_FAMILY.get((game or "").lower(), "hl2")


def _override_file() -> Optional[dict]:
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mapgen_textures.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None
    return None


def style_table(game: str) -> Dict[str, Dict[str, str]]:
    fam = family(game)
    table = {k: dict(v) for k, v in TEXTURES[fam].items()}
    extra = _override_file()
    if extra and fam in extra:
        for style, slots in extra[fam].items():
            table.setdefault(style, {}).update(slots)
    return table


def styles(game: str):
    return sorted(style_table(game))


def materials(game: str, style: str) -> Dict[str, str]:
    table = style_table(game)
    if style not in table:
        style = "dev"
    base = dict(table["dev"]) if "dev" in table else {}
    base.update(table.get(style, {}))
    return base


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def sanitize(theme: Theme, game: str) -> Theme:
    """Clamp every field into a safe range; unknown styles fall back to dev."""
    for key, (lo, hi) in LIMITS.items():
        val = getattr(theme, key)
        try:
            val = type(lo)(val)
        except (TypeError, ValueError):
            val = getattr(Theme(), key)
        setattr(theme, key, _clamp(val, lo, hi))
    theme.height = int(round(theme.height / 32.0)) * 32
    if theme.style not in style_table(game):
        theme.style = "dev"
    theme.light_rgb = tuple(int(_clamp(int(c), 0, 255)) for c in (list(theme.light_rgb) + [255, 255, 255])[:3])
    theme.fog_rgb = tuple(int(_clamp(int(c), 0, 255)) for c in (list(theme.fog_rgb) + [0, 0, 0])[:3])
    theme.name = "".join(ch for ch in str(theme.name) if ch.isalnum() or ch in " _-")[:40] or "untitled"
    theme.notes = str(theme.notes)[:200].replace('"', "'")
    if not theme.skyname:
        theme.skyname = SKYNAMES[family(game)]
    theme.skyname = "".join(ch for ch in theme.skyname if ch.isalnum() or ch == "_")[:40] or SKYNAMES[family(game)]
    return theme


def from_dict(d: dict, game: str, base: Optional[Theme] = None) -> Theme:
    t = base or Theme()
    names = {f.name for f in fields(Theme)}
    for k, v in (d or {}).items():
        if k in names:
            try:
                setattr(t, k, v)
            except Exception:
                pass
    if "size" in (d or {}) and d["size"] in SIZES:
        t.cols, t.rows = SIZES[d["size"]]
    return sanitize(t, game)
