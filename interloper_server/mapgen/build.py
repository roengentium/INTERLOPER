"""
build.py - the three generation modes behind one function.

  procedural : seeded code only (no AI)
  theme      : procedural geometry, an LLM picks the style / size / lighting / mood
  llm        : the LLM writes the whole VMF; validated + repaired, falls back to procedural if it
               doesn't produce a sealed, compilable map
  auto       : theme when an LLM is configured, otherwise procedural
"""

import logging
import random
from dataclasses import dataclass, field
from typing import Optional

from . import validate
from .layout import generate
from .llm import LLMClient, extract_json, extract_vmf
from .themes import SIZES, Theme, family, from_dict, materials, sanitize, styles
from .vmf import VMF

log = logging.getLogger("interloper.mapgen")

THEME_SYSTEM = (
    "You are a level designer for eerie, liminal Source-engine maps. Reply with ONLY one JSON object, "
    "no prose. Keys: name (short title), style (one of: {styles}), size (tiny|small|medium|large|huge), "
    "height (192-384, multiple of 32), darkness (0-0.9, fraction of unlit rooms), pillar_chance (0-0.8), "
    "crate_chance (0-0.8), loop_chance (0-0.8, extra doorways), brightness (40-600), light_rgb ([r,g,b] 0-255), "
    "fog (true/false), fog_rgb ([r,g,b]), fog_end (400-8000), items (0-30 pickups), notes (one sentence)."
)

VMF_SYSTEM = (
    "You write Source engine VMF map files. Output ONLY the VMF text (inside a ```vmf fence is fine).\n"
    "Rules: use only axis-aligned box brushes, every coordinate a multiple of 32. Each solid has exactly 6 "
    "sides with a 'plane' of 3 points in the same format as the example. The playable space must be fully "
    "enclosed by world solids (no gaps to the void), with doorways cut between rooms. Include at least one "
    "info_player_start inside the space about 8 units above the floor, and some 'light' entities near the "
    "ceiling. Keep it under 120 solids. Use only these materials: {mats}. Here is a valid example of a "
    "single box room (copy its structure):\n{example}"
)


@dataclass
class BuildResult:
    vmf_text: str
    mode_used: str
    theme: Theme
    info: dict
    report: validate.Report
    notes: list = field(default_factory=list)
    seed: int = 0


def _example(game: str) -> str:
    v = VMF()
    m = materials(game, "dev")["wall"]
    v.brush((-256, -256, -32), (256, 256, 0), m)
    v.brush((-288, -288, 0), (-256, 288, 256), m)
    return v.text()


def llm_theme(client: LLMClient, game: str, hint: str, seed: int) -> Theme:
    system = THEME_SYSTEM.format(styles=", ".join(styles(game)))
    user = f"Game: {game}. Seed: {seed}. Mood/hint: {hint or 'surprise me'}. Lore tag: FSKY_SVR_GEN."
    data = extract_json(client.ask(system, user, max_tokens=500, temperature=0.9))
    if data is None:
        raise ValueError("model did not return JSON")
    return from_dict(data, game, Theme())


def llm_vmf(client: LLMClient, game: str, theme: Theme, hint: str, tag: str, attempts: int = 2):
    mats = sorted(set(materials(game, theme.style).values()))
    system = VMF_SYSTEM.format(mats=", ".join(mats), example=_example(game))
    user = (f"Design a {theme.cols}x{theme.rows}-ish room map, height {theme.height}. Mood: {hint or theme.notes or 'abandoned and quiet'}. "
            f"Game: {game}. Output the full VMF.")
    last = None
    for n in range(attempts):
        text = extract_vmf(client.ask(system, user, max_tokens=6000, temperature=0.7))
        try:
            root = validate.parse_vmf(text)
        except validate.VMFError as exc:
            last = f"parse error: {exc}"
            continue
        fixed = validate.repair(root)
        for top in root.kids:
            if top.name == "world":
                top.kv.setdefault("classname", "worldspawn")
                top.kv["comment"] = tag
                top.kv.setdefault("skyname", theme.skyname)
        out = validate.dump(root)
        rep = validate.check(out, voxel=16)
        if rep.ok:
            return out, rep, fixed
        last = "; ".join(rep.errors) or "invalid"
        user += f"\nYour previous attempt was rejected: {last}. Fix it and output the full VMF again."
    raise ValueError(last or "model produced no valid VMF")


def build(game: str, seed: Optional[int] = None, mode: str = "auto", size: str = "", style: str = "",
          hint: str = "", tag: str = "FSKY_SVR_GEN", client: Optional[LLMClient] = None) -> BuildResult:
    seed = int(seed) if seed is not None else random.randrange(1, 10**6)
    notes = []
    mode = (mode or "auto").lower()
    if mode == "auto":
        mode = "theme" if client else "procedural"
    if mode in ("theme", "llm") and client is None:
        notes.append(f"mode {mode} needs an LLM (--ollama / --llm-url); using procedural")
        mode = "procedural"

    theme = sanitize(Theme(), game)
    rng = random.Random(seed ^ 0x5EED)
    theme.style = rng.choice(styles(game))
    if mode in ("theme", "llm"):
        try:
            theme = llm_theme(client, game, hint, seed)
            notes.append(f"theme from model: {theme.name} / {theme.style} ({theme.cols}x{theme.rows})")
        except Exception as exc:                    # network, bad JSON, anything
            notes.append(f"theme model failed ({exc}); random theme instead")
            theme = sanitize(Theme(style=theme.style), game)
    if size in SIZES:
        theme.cols, theme.rows = SIZES[size]
    if style:
        theme.style = style
    theme = sanitize(theme, game)

    if mode == "llm":
        try:
            text, rep, fixed = llm_vmf(client, game, theme, hint, tag)
            notes.append(f"model-written VMF accepted ({rep.brushes} brushes, {fixed} sides re-wound)")
            return BuildResult(text, "llm", theme, {"spawn": rep.spawn}, rep, notes, seed)
        except Exception as exc:
            notes.append(f"model-written VMF rejected ({exc}); built procedurally instead")
            mode = "procedural"

    vmf, info = generate(theme, game, seed, tag)
    text = vmf.text()
    rep = validate.check(text, voxel=32)
    if not rep.ok:                                   # should never happen; refuse rather than ship a leak
        raise RuntimeError("generated map failed validation: " + "; ".join(rep.errors))
    return BuildResult(text, mode, theme, info, rep, notes, seed)
