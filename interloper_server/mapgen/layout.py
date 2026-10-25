"""
layout.py - seeded grid-of-rooms level generator. Everything is axis-aligned boxes on a 32-unit
grid, so VBSP gets simple closed brushes and the leak check can be exact.
"""

import random
from typing import Dict, List, Set, Tuple

from .themes import GAME_ITEMS, NODRAW, Theme, family, materials
from .vmf import VMF

CELL = 512
WALL = 32
PITCH = CELL + WALL
DOOR_W = 128
DOOR_H = 128

Cell = Tuple[int, int]


def _connections(rng: random.Random, cols: int, rows: int, loop_chance: float) -> Set[Tuple[Cell, Cell]]:
    """Random spanning tree (depth-first maze) plus some extra doorways so it isn't a pure maze."""
    seen = {(0, 0)}
    stack = [(0, 0)]
    edges: Set[Tuple[Cell, Cell]] = set()
    while stack:
        c = stack[-1]
        nbrs = [(c[0] + dx, c[1] + dy) for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
                if 0 <= c[0] + dx < cols and 0 <= c[1] + dy < rows and (c[0] + dx, c[1] + dy) not in seen]
        if not nbrs:
            stack.pop()
            continue
        n = rng.choice(nbrs)
        seen.add(n)
        edges.add(tuple(sorted((c, n))))
        stack.append(n)
    for i in range(cols):
        for j in range(rows):
            for dx, dy in ((1, 0), (0, 1)):
                n = (i + dx, j + dy)
                if n[0] < cols and n[1] < rows and rng.random() < loop_chance:
                    edges.add(tuple(sorted(((i, j), n))))
    return edges


def generate(theme: Theme, game: str, seed: int, tag: str = "") -> Tuple[VMF, Dict]:
    rng = random.Random(seed)
    mats = materials(game, theme.style)
    cols, rows, H = theme.cols, theme.rows, theme.height
    fam = family(game)

    vmf = VMF(skyname=theme.skyname, comment=tag, message=theme.name)
    ext_x, ext_y = cols * PITCH, rows * PITCH

    def wall_mat(face):      # visible sides get the wall texture, top/bottom are hidden
        return mats["wall"] if face in ("+x", "-x", "+y", "-y") else NODRAW

    def trim_mat(face):
        return mats["trim"] if face in ("+x", "-x", "+y", "-y", "-z") else NODRAW

    def floor_mat(face):
        return mats["floor"] if face == "+z" else NODRAW

    def ceil_mat(face):
        return mats["ceil"] if face == "-z" else NODRAW

    # floor and ceiling slabs over the whole footprint
    vmf.brush((-WALL, -WALL, -WALL), (ext_x, ext_y, 0), floor_mat)
    vmf.brush((-WALL, -WALL, H), (ext_x, ext_y, H + WALL), ceil_mat)

    edges = _connections(rng, cols, rows, theme.loop_chance)

    # corner posts at every grid vertex (closes the gaps where wall slabs meet)
    for i in range(cols + 1):
        for j in range(rows + 1):
            vmf.brush((i * PITCH - WALL, j * PITCH - WALL, 0), (i * PITCH, j * PITCH, H), wall_mat)

    def wall_run(xa, xb, ya, yb, along_x: bool, connected: bool):
        """One wall slab across the gap between two cells (or the outer boundary)."""
        if not connected:
            vmf.brush((xa, ya, 0), (xb, yb, H), wall_mat)
            return
        if along_x:       # slab runs along x, doorway centred in x
            c = (xa + xb) // 2
            vmf.brush((xa, ya, 0), (c - DOOR_W // 2, yb, H), wall_mat)
            vmf.brush((c + DOOR_W // 2, ya, 0), (xb, yb, H), wall_mat)
            vmf.brush((c - DOOR_W // 2, ya, DOOR_H), (c + DOOR_W // 2, yb, H), trim_mat)
        else:             # slab runs along y
            c = (ya + yb) // 2
            vmf.brush((xa, ya, 0), (xb, c - DOOR_W // 2, H), wall_mat)
            vmf.brush((xa, c + DOOR_W // 2, 0), (xb, yb, H), wall_mat)
            vmf.brush((xa, c - DOOR_W // 2, DOOR_H), (xb, c + DOOR_W // 2, H), trim_mat)

    # walls between horizontally adjacent cells (gap index i = wall left of cell i), and around the edge
    for i in range(cols + 1):
        for j in range(rows):
            x1 = i * PITCH
            conn = 0 < i < cols and tuple(sorted(((i - 1, j), (i, j)))) in edges
            wall_run(x1 - WALL, x1, j * PITCH, j * PITCH + CELL, along_x=False, connected=conn)
    for j in range(rows + 1):
        for i in range(cols):
            y1 = j * PITCH
            conn = 0 < j < rows and tuple(sorted(((i, j - 1), (i, j)))) in edges
            wall_run(i * PITCH, i * PITCH + CELL, y1 - WALL, y1, along_x=True, connected=conn)

    # spawn room: random, always lit
    spawn_cell = (rng.randrange(cols), rng.randrange(rows))

    def centre(c: Cell):
        return c[0] * PITCH + CELL // 2, c[1] * PITCH + CELL // 2

    lights = 0
    for i in range(cols):
        for j in range(rows):
            cx, cy = centre((i, j))
            lit = (i, j) == spawn_cell or rng.random() >= theme.darkness
            if lit:
                r, g, b = theme.light_rgb
                vmf.entity("light", origin=f"{cx} {cy} {H - 16}", _light=f"{r} {g} {b} {theme.brightness}",
                           _lightHDR="-1 -1 -1 1", _lightscaleHDR="1", _constant_attn="0",
                           _linear_attn="0", _quadratic_attn="1", _fifty_percent_distance="0",
                           _zero_percent_distance="0", style="0")
                lights += 1
            if (i, j) != spawn_cell:
                if rng.random() < theme.pillar_chance:
                    ox = rng.choice((-128, 128))
                    oy = rng.choice((-128, 128))
                    vmf.detail((cx + ox - 32, cy + oy - 32, 0), (cx + ox + 32, cy + oy + 32, H),
                               lambda f: mats["pillar"] if f != "-z" else NODRAW)
                if rng.random() < theme.crate_chance:
                    ox = rng.choice((-160, -96, 96, 160))
                    oy = rng.choice((-160, -96, 96, 160))
                    s = rng.choice((32, 64, 96))
                    vmf.detail((cx + ox - 32, cy + oy - 32, 0), (cx + ox + 32, cy + oy + 32, s),
                               lambda f: mats["crate"] if f != "-z" else NODRAW)

    sx, sy = centre(spawn_cell)
    vmf.entity("info_player_start", origin=f"{sx} {sy} 8", angles=f"0 {rng.choice((0, 90, 180, 270))} 0")
    vmf.entity("info_target", origin=f"{sx} {sy} {H - 48}", targetname=(tag or "mapgen"))

    if theme.fog:
        r, g, b = theme.fog_rgb
        vmf.entity("env_fog_controller", origin=f"{sx} {sy} {H - 32}", fogenable="1", fogblend="0",
                   fogcolor=f"{r} {g} {b}", fogcolor2=f"{r} {g} {b}", fogstart="0", fogend=str(theme.fog_end),
                   fogmaxdensity="1", farz="-1", targetname="mapgen_fog")

    # game specific props / pickups so the live player (Type 4) has things to interact with
    placed = []
    spots = [(i, j) for i in range(cols) for j in range(rows) if (i, j) != spawn_cell]
    rng.shuffle(spots)
    if fam == "portal":
        vmf.entity("weapon_portalgun", origin=f"{sx + 64} {sy} 24", CanFirePortal1="1", CanFirePortal2="1",
                   targetname="mapgen_portalgun")
        for c in spots[: max(0, min(theme.items, 6))]:
            cx, cy = centre(c)
            vmf.entity("prop_physics", origin=f"{cx} {cy} 24", model="models/props/metal_box.mdl")
            placed.append(("prop_physics", c))
    else:
        pool = GAME_ITEMS.get(fam, [])
        for k in range(theme.items):
            if not pool or not spots:
                break
            c = spots[k % len(spots)]
            cx, cy = centre(c)
            cls = rng.choice(pool)
            vmf.entity(cls, origin=f"{cx + rng.randrange(-96, 97)} {cy + rng.randrange(-96, 97)} 16")
            placed.append((cls, c))

    info = {"spawn": (sx, sy, 8), "spawn_cell": spawn_cell, "cells": cols * rows, "lights": lights,
            "doors": len(edges), "items": placed, "extent": (ext_x, ext_y, H)}
    return vmf, info
