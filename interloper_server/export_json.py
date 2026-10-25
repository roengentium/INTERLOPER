#!/usr/bin/env python3
"""
export_json.py - generate one demo and write it as JSON for the Garry's Mod addon.

Run it from inside the interloper_server folder (imports are flat), then in GMod
type `interlope` in the console.

Examples:
  py export_json.py --all --gmod "E:\\gmod\\GarrysMod\\garrysmod"   (one demo of each type)
  py export_json.py --type 2 --gmod "<garrysmod folder>"                 (just type 2)
  py export_json.py --gmod "<garrysmod folder>"                          (random weighted type)
  py export_json.py --type 2 --seed 7 --out demo.json                    (write to a file)

Each type is saved as demo_<type>.json. In GMod: `interlope 1`, `interlope 2`,
`interlope 3`, `interlope 6`, `interlope 7`, or plain `interlope` to play a random one of whatever exists.
"""
import argparse
import json
import os
import random
import sys

from generators.type1 import generate_type1
from generators.type2 import MapBounds, generate_type2
from generators.type3 import generate_type3
from generators.type6 import generate_type6
from generators.type7 import generate_type7
from repo import Repo
from server import InterloperServer

IMPLEMENTED = {1: generate_type1, 2: generate_type2, 3: generate_type3, 6: generate_type6, 7: generate_type7}


def to_json(rec) -> dict:
    return {
        "type": rec.demo_type,
        "commit": rec.commit_number,
        "ticks": [
            [round(t.origin[0], 2), round(t.origin[1], 2), round(t.origin[2], 2),
             round(t.angles[0], 3), round(t.angles[1], 3), round(t.angles[2], 3),
             int(t.buttons),
             round(t.move[0], 2), round(t.move[1], 2), round(t.move[2], 2)]
            for t in rec.ticks
        ],
        "events": [
            [e.tick_index, int(e.event_type), e.param.decode("ascii", "replace")]
            for e in rec.events
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--type", type=int, help="demo type 1, 2, 3, 6 or 7 (omit for a random weighted pick)")
    ap.add_argument("--all", action="store_true", help="export one demo of every implemented type")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--mins", type=float, nargs=3, default=(-4096.0, -4096.0, -512.0),
                    help="map bounds minimum x y z (only type 2 cares)")
    ap.add_argument("--maxs", type=float, nargs=3, default=(4096.0, 4096.0, 2048.0),
                    help="map bounds maximum x y z (only type 2 cares)")
    ap.add_argument("--gmod", help="path to the garrysmod folder; writes data/interloper/demo.json there")
    ap.add_argument("--out", help="explicit output file (overrides --gmod)")
    ap.add_argument("--opt", action="append", default=[], metavar="KEY=VALUE",
                    help="generator option, same keys as the server (e.g. --opt yaw_speed=90 --opt pos=void). Repeatable.")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    bounds = MapBounds(mins=tuple(args.mins), maxs=tuple(args.maxs))

    if args.all:
        types = sorted(IMPLEMENTED)
    else:
        demo_type = args.type if args.type else Repo().pick_type(rng)
        if demo_type not in IMPLEMENTED:
            print(f"type {demo_type} has no generator yet, using type 2 (same as the server does)")
            demo_type = 2
        types = [demo_type]

    if args.out and len(types) > 1:
        print("--out writes a single file; use --gmod with --all")
        return 2
    if not args.out and not args.gmod:
        print("give --gmod <garrysmod folder> or --out <file>")
        return 2

    for demo_type in types:
        opts = InterloperServer._parse_opts(args.opt)
        opts.pop("type", None)
        if args.seed is not None:
            opts.setdefault("seed", str(args.seed + demo_type))
        # Reuse the server's option handling so GMod and HL2 accept the same keys.
        rec = InterloperServer()._generate(demo_type, bounds, None, opts)

        if args.out:
            path = args.out
        else:
            path = os.path.join(args.gmod, "data", "interloper", f"demo_{demo_type}.json")

        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(to_json(rec), f, separators=(",", ":"))

        print(f"type {rec.demo_type}, commit {rec.commit_number}, {len(rec.ticks)} ticks, "
              f"{len(rec.events)} events -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
